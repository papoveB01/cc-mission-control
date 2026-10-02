"""In-memory session store built from hook events (SPEC sections 5.1 and 9.3)."""

from __future__ import annotations

import json
import logging
import math
import re
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .config import Config
from .redact import Redactor, truncate

log = logging.getLogger(__name__)

MAIN = "main"
BIG_WINDOW = 1_000_000
IDENT_CHARS = 200
CWD_CHARS = 1000
DEFAULT_SUBAGENT = "general-purpose"
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LINK_CAP = 256
STDERR_MARK = "\n[stderr]\n"
LANE_CALLS = 60
ACTIVITY_MAX = 80
ACTIVITY_CHARS = 240
SUMMARY_CHARS = 200
SPAWN_TOOLS = ("Agent", "Task")
SUMMARY_KEYS = (
    "command", "file_path", "notebook_path", "pattern", "url",
    "query", "description", "prompt", "path", "skill",
)
OUTPUT_KEYS = ("stdout", "output", "content", "result", "text", "message")
WAIT_MARKERS = ("permission", "idle", "elicitation", "input")


@dataclass
class Call:
    id: str
    agent_id: str
    tool: str
    summary: str
    started: float
    status: str = "running"
    ended: Optional[float] = None
    duration_ms: Optional[int] = None
    subagent_id: Optional[str] = None
    input: Any = field(default_factory=dict)
    output: str = ""
    error: Optional[str] = None

    def to_dict(self, detail: bool = False) -> dict:
        d: Dict[str, Any] = {
            "id": self.id,
            "agent_id": self.agent_id,
            "tool": self.tool,
            "summary": self.summary,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "duration_ms": self.duration_ms,
            "subagent_id": self.subagent_id,
        }
        if detail:
            d.update(input=self.input, output=self.output, error=self.error)
        return d


@dataclass
class Agent:
    id: str
    label: str
    started: float
    max_calls: int
    agent_type: Optional[str] = None
    status: str = "idle"
    task: str = ""
    result: str = ""
    ended: Optional[float] = None
    context_tokens: Optional[int] = None
    context_window: Optional[int] = None
    model: Optional[str] = None
    errors: int = 0
    total_calls: int = 0
    transcript_path: Optional[str] = None
    spawn_call_id: Optional[str] = None
    meta_linked: bool = False
    tool_counts: Dict[str, int] = field(default_factory=dict)
    calls: Deque[Call] = field(init=False)
    index: Dict[str, Call] = field(default_factory=dict, init=False)

    def __post_init__(self) -> None:
        self.calls = deque(maxlen=max(1, self.max_calls))

    def add_call(self, call: Call) -> Optional[Call]:
        """Append a call; returns the call pushed out of the deque, if any."""
        evicted = None
        if len(self.calls) == self.calls.maxlen:
            evicted = self.calls[0]
            if self.index.get(evicted.id) is evicted:
                del self.index[evicted.id]
        self.calls.append(call)
        self.index[call.id] = call
        self.total_calls += 1
        self.tool_counts[call.tool] = self.tool_counts.get(call.tool, 0) + 1
        return evicted

    def to_dict(self) -> dict:
        counts = dict(sorted(self.tool_counts.items(), key=lambda kv: -kv[1]))
        return {
            "id": self.id,
            "label": self.label,
            "agent_type": self.agent_type,
            "status": self.status,
            "task": self.task,
            "result": self.result,
            "started": self.started,
            "ended": self.ended,
            "context_tokens": self.context_tokens,
            "context_window": self.context_window,
            "model": self.model,
            "tool_counts": counts,
            "errors": self.errors,
            "total_calls": self.total_calls,
            "calls": [c.to_dict() for c in list(self.calls)[-LANE_CALLS:]],
        }


@dataclass
class Session:
    id: str
    title: str
    started: float
    last_event: float
    cwd: str = ""
    model: Optional[str] = None
    status: str = "active"
    ended: Optional[float] = None
    end_reason: Optional[str] = None
    source: Optional[str] = None
    transcript_path: Optional[str] = None
    compactions: int = 0
    agents: Dict[str, Agent] = field(default_factory=dict)
    activity: Deque[dict] = field(default_factory=lambda: deque(maxlen=ACTIVITY_MAX))
    pending: List[dict] = field(default_factory=list)
    spawns: Dict[str, dict] = field(default_factory=dict)
    spawn_links: Dict[str, str] = field(default_factory=dict)
    spawn_models: Dict[str, str] = field(default_factory=dict)

    def ordered_agents(self) -> List[Agent]:
        subs = [a for a in self.agents.values() if a.id != MAIN]
        live = sorted((a for a in subs if a.status != "done"), key=lambda a: a.started)
        done = sorted((a for a in subs if a.status == "done"), key=lambda a: -(a.ended or 0))
        return [self.agents[MAIN], *live, *done]

    def to_dict(self) -> dict:
        return {
            "id": self.id,
            "title": self.title,
            "cwd": self.cwd,
            "model": self.model,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "last_event": self.last_event,
            "compactions": self.compactions,
            "agents": [a.to_dict() for a in self.ordered_agents()],
            "activity": list(self.activity),
        }


def _norm_agent(raw: Any) -> str:
    if not isinstance(raw, str) or not raw.strip():
        return MAIN
    raw = raw.strip()
    return raw[len("agent-"):] or MAIN if raw.startswith("agent-") else raw


def _title(cwd: str, sid: str) -> str:
    name = cwd.replace("\\", "/").rstrip("/").rsplit("/", 1)[-1] if cwd else ""
    return name or sid[:8]


def _first_line(text: str) -> str:
    text = text.strip()
    return text.splitlines()[0] if text else ""


def _dumps(obj: Any) -> str:
    return json.dumps(obj, separators=(",", ":"), ensure_ascii=False, default=str)


def _str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def _millis(value: Any) -> Optional[int]:
    """A usable duration: finite, non-negative int or float, rounded to ms."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    if isinstance(value, float) and not math.isfinite(value):
        return None
    return int(round(value)) if value >= 0 else None


def _blocks_text(value: Any) -> str:
    """Flatten a tool_response field: text blocks are joined, other values dumped."""
    if isinstance(value, str):
        return value
    if isinstance(value, list) and any(isinstance(b, dict) and "text" in b for b in value):
        return "\n".join(str(b["text"]) for b in value if isinstance(b, dict) and "text" in b)
    return _dumps(value)


def _has_text(resp: dict) -> bool:
    """True if a tool_response carries real output text under one of OUTPUT_KEYS."""
    for key in OUTPUT_KEYS:
        v = resp.get(key)
        if isinstance(v, list):
            if any(isinstance(b, dict) and b.get("text") for b in v):
                return True
        elif v:
            return True
    return False


class Store:
    def __init__(
        self,
        config: Config,
        redactor: Optional[Redactor] = None,
        clock: Callable[[], float] = time.time,
    ) -> None:
        self.config = config
        self.redactor = redactor or Redactor.from_config(config)
        self.clock = clock
        self._sessions: Dict[str, Session] = {}
        self._changed: set = set()
        self._seq = 0
        self._lock = threading.RLock()

    # ---- sanitizing -------------------------------------------------------

    def _ident(self, value: Any, limit: int = IDENT_CHARS) -> Optional[str]:
        """Redacted, length-capped copy of a payload string; None if empty or not a string."""
        if not isinstance(value, str) or not value:
            return None
        return self.redactor.text(value)[:limit] or None

    def _aid(self, raw: Any) -> str:
        return self._ident(_norm_agent(raw)) or MAIN

    # ---- public API -------------------------------------------------------

    def ingest(self, event: dict) -> Optional[str]:
        """Apply one hook payload. Returns the session id touched, or None if dropped."""
        with self._lock:
            try:
                return self._ingest(event)
            except Exception:  # bad payloads must never take the server down
                log.debug("dropping malformed event", exc_info=True)
                return None

    def pop_changed(self) -> set:
        with self._lock:
            changed, self._changed = self._changed, set()
            return changed

    def snapshot(self) -> List[dict]:
        with self._lock:
            sessions = sorted(self._sessions.values(), key=lambda s: -s.started)
            return [s.to_dict() for s in sessions]

    def session_dict(self, session_id: str) -> Optional[dict]:
        with self._lock:
            s = self._sessions.get(session_id)
            return s.to_dict() if s else None

    def call_detail(self, session_id: str, call_id: str) -> Optional[dict]:
        with self._lock:
            s = self._sessions.get(session_id)
            call = self._find_call(s, call_id) if s else None
            return call.to_dict(detail=True) if call else None

    def set_context(self, session_id: str, agent_id: str, tokens: int, model: Optional[str]) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            agent = s.agents.get(self._aid(agent_id)) if s else None
            if s is None or agent is None or not _is_int(tokens) or tokens < 0:
                return
            model = self._ident(model)
            if model and model != "<synthetic>":
                agent.model = model
                if agent.id == MAIN:
                    s.model = model
            base = BIG_WINDOW if "1m" in (agent.model or "").lower() else self.config.context_window
            window = max(agent.context_window or 0, base)
            if tokens > window:
                window = BIG_WINDOW
            agent.context_tokens = tokens
            agent.context_window = window
            self._changed.add(session_id)

    def apply_subagent_meta(
        self, session_id: str, agent_id: str, description: Optional[str], tool_use_id: Optional[str]
    ) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            agent = s.agents.get(self._aid(agent_id)) if s else None
            if s is None or agent is None or agent.id == MAIN:
                return
            tool_use_id = self._ident(tool_use_id)
            desc = self.redactor.text(description) if description else ""
            if not tool_use_id:
                if desc:
                    agent.task = desc
                    self._changed.add(session_id)
                return
            self._link(s, agent, desc, tool_use_id)
            self._changed.add(session_id)

    def _link(self, s: Session, agent: Agent, desc: str, tool_use_id: str) -> None:
        """Authoritatively tie a subagent lane to the spawn call that launched it."""
        if agent.spawn_call_id == tool_use_id:
            agent.meta_linked = True
            if not agent.task and desc:
                agent.task = desc
            return
        prev = agent.spawn_call_id
        prev_task = agent.task
        retry = None
        other = next((a for a in s.agents.values() if a is not agent and a.spawn_call_id == tool_use_id), None)
        if other is not None:
            if prev:
                other.task = s.spawns.get(prev, {}).get("description") or prev_task
                other.spawn_call_id = prev
                self._link_call(s, prev, other.id)
            else:
                other.spawn_call_id = None
                other.task = ""
                retry = other
        elif prev:
            self._link_call(s, prev, None)
            if prev in s.spawns:
                s.pending.insert(0, s.spawns[prev])
        s.pending = [p for p in s.pending if p["tool_use_id"] != tool_use_id]
        agent.task = desc or s.spawns.get(tool_use_id, {}).get("description") or agent.task
        agent.spawn_call_id = tool_use_id
        agent.meta_linked = True
        self._link_call(s, tool_use_id, agent.id)
        if retry is not None:
            self._fifo(s, retry)

    def touch(self, session_id: str) -> None:
        with self._lock:
            s = self._sessions.get(session_id)
            if s:
                s.last_event = self.clock()

    def expire_stale(self) -> None:
        limit = self.config.stale_minutes * 60
        if limit <= 0:
            return
        with self._lock:
            now = self.clock()
            for s in self._sessions.values():
                if s.status == "active" and now - s.last_event > limit:
                    self._close(s, now, "stale")
                    self._act(s, now, MAIN, "session", "No activity; marked ended", "info")
                    self._changed.add(s.id)

    def transcript_targets(self) -> List[Tuple[str, str, Optional[str]]]:
        with self._lock:
            out: List[Tuple[str, str, Optional[str]]] = []
            for s in self._sessions.values():
                if s.status != "active":
                    continue
                for a in s.agents.values():
                    out.append((s.id, a.id, s.transcript_path if a.id == MAIN else a.transcript_path))
            return out

    # ---- event dispatch ---------------------------------------------------

    def _ingest(self, event: Any) -> Optional[str]:
        if not isinstance(event, dict):
            return None
        sid = self._ident(event.get("session_id"))
        name = event.get("hook_event_name")
        handler = _HANDLERS.get(name) if isinstance(name, str) else None
        if not sid or handler is None:
            return None
        now = self.clock()
        s = self._sessions.get(sid)
        if s is None:
            cwd = self._ident(event.get("cwd"), CWD_CHARS) or ""
            s = Session(id=sid, title=_title(cwd, sid), started=now, last_event=now, cwd=cwd)
            s.agents[MAIN] = self._new_agent(MAIN, "Main", now)
            self._sessions[sid] = s
        elif s.status == "ended" and name != "SessionStart":
            if s.end_reason == "session_end":
                return None
            if name != "SessionEnd":
                s.status, s.ended, s.end_reason = "active", None, None
        s.last_event = now
        cwd = self._ident(event.get("cwd"), CWD_CHARS)
        if cwd and cwd != s.cwd:
            s.cwd, s.title = cwd, _title(cwd, sid)
        s.transcript_path = _str(event.get("transcript_path")) or s.transcript_path
        handler(self, s, event, self._aid(event.get("agent_id")), now)
        self._changed.add(sid)
        return sid

    # ---- helpers ----------------------------------------------------------

    def _new_agent(self, agent_id: str, label: str, now: float, agent_type: Optional[str] = None) -> Agent:
        return Agent(id=agent_id, label=label, started=now, max_calls=self.config.max_calls, agent_type=agent_type)

    def _agent(self, s: Session, agent_id: str, now: float, agent_type: Any = None) -> Agent:
        agent_type = self._ident(agent_type)
        agent = s.agents.get(agent_id)
        if agent is None:
            agent = self._new_agent(agent_id, agent_type or "Subagent", now, agent_type)
            agent.status = "running"
            s.agents[agent_id] = agent
        elif agent_id != MAIN:
            if agent_type and not agent.agent_type:
                agent.agent_type = agent_type
                if agent.label == "Subagent":
                    agent.label = agent_type
            if agent.status == "done":
                agent.status, agent.ended = "running", None
        return agent

    def _act(self, s: Session, now: float, agent_id: str, kind: str, text: str, status: str) -> None:
        s.activity.append({"t": now, "agent_id": agent_id, "kind": kind, "text": self.redactor.redact(text)[:ACTIVITY_CHARS], "status": status})

    def _find_call(self, s: Session, call_id: str) -> Optional[Call]:
        for a in s.agents.values():
            call = a.index.get(call_id)
            if call:
                return call
        return None

    def _link_call(self, s: Session, call_id: str, subagent_id: Optional[str]) -> None:
        call = self._find_call(s, call_id)
        if call:
            call.subagent_id = subagent_id

    def _next_id(self) -> str:
        self._seq += 1
        return f"call-{self._seq}"

    def _summary(self, tool_input: Any) -> str:
        if isinstance(tool_input, dict):
            for key in SUMMARY_KEYS:
                v = tool_input.get(key)
                if not v:
                    continue
                text = self.redactor.redact(v if isinstance(v, str) else _dumps(v))
                if text.strip():
                    return _first_line(text)[:SUMMARY_CHARS]
            if tool_input:
                return self.redactor.redact(_dumps(tool_input))[:SUMMARY_CHARS]
            return ""
        if tool_input in (None, ""):
            return ""
        text = tool_input if isinstance(tool_input, str) else _dumps(tool_input)
        return _first_line(self.redactor.redact(text))[:SUMMARY_CHARS]

    def _output(self, response: Any) -> str:
        text, stderr = "", ""
        if isinstance(response, dict):
            stderr = response.get("stderr") if isinstance(response.get("stderr"), str) else ""
            for key in OUTPUT_KEYS:
                if response.get(key):
                    text = _blocks_text(response[key])
                    break
            else:
                text = "" if stderr.strip() else _dumps(response)
        elif response not in (None, "", [], {}):
            text = _blocks_text(response)
        if not stderr.strip():
            return self.redactor.text(text)
        out, err = self.redactor.redact(text), self.redactor.redact(stderr)
        mark = STDERR_MARK if out else STDERR_MARK.lstrip("\n")
        cap = self.redactor.max_chars
        if cap > 0 and len(out) + len(mark) + len(err) > cap:
            keep_err = min(len(err), cap // 4)
            out = truncate(out, max(0, cap - len(mark) - keep_err))
            err = truncate(err, max(keep_err, cap - len(mark) - len(out)))
        return out + mark + err

    def _make_call(self, s: Session, agent: Agent, e: dict, now: float) -> Call:
        call = self._build_call(agent.id, e, now)
        evicted = agent.add_call(call)
        if evicted is not None:
            self._drop_links(s, evicted.id)
        return call

    def _drop_links(self, s: Session, call_id: str) -> None:
        for sub_id in [k for k, v in s.spawn_links.items() if v == call_id]:
            del s.spawn_links[sub_id]
            s.spawn_models.pop(sub_id, None)

    def _trim_spawns(self, s: Session) -> None:
        for d in (s.spawn_links, s.spawn_models):
            while len(d) > LINK_CAP:
                del d[next(iter(d))]
        del s.pending[: max(0, len(s.pending) - LINK_CAP)]
        if len(s.spawns) > LINK_CAP:
            keep = {a.spawn_call_id for a in s.agents.values()}
            for key in [k for k in s.spawns if k not in keep][: len(s.spawns) - LINK_CAP]:
                del s.spawns[key]

    def _fifo(self, s: Session, agent: Agent) -> None:
        """Give a lane the oldest pending spawn of its type that no held link has claimed."""
        held = set(s.spawn_links.values())
        want = agent.agent_type or DEFAULT_SUBAGENT
        idx = next(
            (i for i, p in enumerate(s.pending) if p["subagent_type"] == want and p["tool_use_id"] not in held),
            None,
        )
        if idx is None:
            return
        spawn = s.pending.pop(idx)
        agent.task = agent.task or spawn["description"]
        agent.spawn_call_id = spawn["tool_use_id"]
        self._link_call(s, agent.spawn_call_id, agent.id)

    def _build_call(self, agent_id: str, e: dict, now: float) -> Call:
        tool_input = e.get("tool_input")
        if isinstance(tool_input, dict):
            stored = self.redactor.value(tool_input)
        else:
            stored = {} if tool_input is None else {"value": self.redactor.value(tool_input)}
        call = Call(
            id=self._ident(e.get("tool_use_id")) or self._next_id(),
            agent_id=agent_id,
            tool=self._ident(e.get("tool_name")) or "unknown",
            summary=self._summary(tool_input),
            started=now,
            input=stored,
        )
        return call

    def _match(self, s: Session, agent: Agent, e: dict, now: float) -> Call:
        call_id = self._ident(e.get("tool_use_id"))
        if call_id:
            call = agent.index.get(call_id) or self._find_call(s, call_id)
        else:
            tool = self._ident(e.get("tool_name")) or "unknown"
            call = next((c for c in reversed(agent.calls) if c.status == "running" and c.tool == tool), None)
        if call is None:
            call = self._make_call(s, agent, e, now)
            dur = _millis(e.get("duration_ms"))
            if dur is not None:
                call.started = now - dur / 1000
        return call

    def _finish(self, call: Call, e: dict, now: float, status: str) -> None:
        dur = _millis(e.get("duration_ms"))
        call.status = status
        call.ended = now
        call.duration_ms = dur if dur is not None else max(0, int((now - call.started) * 1000))

    def _close(self, s: Session, now: float, reason: str) -> None:
        for a in s.agents.values():
            for c in a.calls:
                if c.status == "running":
                    c.status, c.ended, c.error = "error", now, "Session ended"
                    c.duration_ms = max(0, int((now - c.started) * 1000))
            if a.id == MAIN:
                a.status = "idle"
            elif a.status in ("running", "waiting", "idle"):
                a.status, a.ended = "done", now
        s.spawn_links.clear()
        s.spawn_models.clear()
        s.status, s.ended, s.end_reason = "ended", now, reason

    # ---- handlers ---------------------------------------------------------

    def _h_session_start(self, s: Session, e: dict, aid: str, now: float) -> None:
        if s.status == "ended":
            s.status, s.ended, s.end_reason = "active", None, None
        main = s.agents[MAIN]
        s.source = self._ident(e.get("source")) or s.source
        model = self._ident(e.get("model"))
        if model and model != "<synthetic>":
            s.model = main.model = model
        tokens = e.get("context_tokens")
        if _is_int(tokens) and tokens > 0:
            self.set_context(s.id, MAIN, tokens, None)
        self._act(s, now, MAIN, "session", f"Session started ({s.source or 'startup'})", "info")

    def _h_prompt(self, s: Session, e: dict, aid: str, now: float) -> None:
        main = s.agents[MAIN]
        prompt = e.get("prompt")
        main.task = self.redactor.text(prompt) if isinstance(prompt, str) else main.task
        main.status = "running"
        self._act(s, now, MAIN, "prompt", main.task, "info")

    def _h_pre_tool(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"))
        if aid == MAIN and agent.status in ("waiting", "idle", "error"):
            agent.status = "running"
        existing = self._find_call(s, self._ident(e.get("tool_use_id")) or "")
        if existing is not None:
            fresh = self._build_call(aid, e, now)
            existing.summary, existing.input = fresh.summary, fresh.input
            return
        call = self._make_call(s, agent, e, now)
        if call.tool in SPAWN_TOOLS:
            tool_input = e.get("tool_input") if isinstance(e.get("tool_input"), dict) else {}
            desc = tool_input.get("description")
            spawn = {
                "tool_use_id": call.id,
                "subagent_type": self._ident(tool_input.get("subagent_type")) or DEFAULT_SUBAGENT,
                "description": self.redactor.text(desc) if isinstance(desc, str) else "",
            }
            s.spawns[call.id] = spawn
            s.pending.append(spawn)
            self._trim_spawns(s)
        self._act(s, now, aid, "tool", f"{call.tool}: {call.summary}", "running")

    def _h_post_tool(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"))
        call = self._match(s, agent, e, now)
        self._finish(call, e, now, "ok")
        resp = e.get("tool_response")
        call.output = self._output(resp)
        if call.tool in SPAWN_TOOLS and isinstance(resp, dict):
            label = {"async_launched": "Launched in background", "completed": "Completed"}.get(resp.get("status"))
            if label and not _has_text(resp):
                call.output = label
            self._link_from_response(s, call, resp)

    def _link_from_response(self, s: Session, call: Call, resp: dict) -> None:
        """tool_response.agentId is the documented link from a spawn call to its subagent."""
        raw = resp.get("agentId")
        if not isinstance(raw, str):
            return
        raw = raw.strip()
        sub_id = raw[len("agent-"):] if raw.startswith("agent-") else raw
        if not AGENT_ID_RE.match(sub_id) or sub_id == MAIN or self._aid(sub_id) != sub_id:
            return
        model = self._ident(resp.get("resolvedModel"))
        sub = s.agents.get(sub_id)
        if sub is None:
            s.spawn_links[sub_id] = call.id
            if model:
                s.spawn_models[sub_id] = model
            self._trim_spawns(s)
            return
        desc = ""
        if not s.spawns.get(call.id, {}).get("description") and isinstance(resp.get("description"), str):
            desc = self.redactor.text(resp["description"])
        self._link(s, sub, desc, call.id)
        if model and not sub.model:
            sub.model = model

    def _h_tool_failure(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"))
        call = self._match(s, agent, e, now)
        self._finish(call, e, now, "error")
        err = e.get("error")
        call.error = self.redactor.text(err if isinstance(err, str) else _dumps(err) if err else "")
        agent.errors += 1
        if call.tool in SPAWN_TOOLS and call.subagent_id is None:
            s.pending = [p for p in s.pending if p["tool_use_id"] != call.id]
            s.spawns.pop(call.id, None)
        if call.tool in SPAWN_TOOLS:
            self._drop_links(s, call.id)
        if e.get("is_interrupt") is True:
            text = f"{call.tool} interrupted"
        else:
            text = f"{call.tool} failed: {_first_line(call.error)}"
        self._act(s, now, aid, "error", text, "error")

    def _h_subagent_start(self, s: Session, e: dict, aid: str, now: float) -> None:
        if aid == MAIN:
            return
        agent = self._agent(s, aid, now, e.get("agent_type"))
        agent.status = "running"
        agent.transcript_path = _str(e.get("agent_transcript_path")) or agent.transcript_path
        linked = s.spawn_links.pop(agent.id, None)
        model = s.spawn_models.pop(agent.id, None)
        if model and not agent.model:
            agent.model = model
        if linked:
            self._link(s, agent, "", linked)
        elif agent.spawn_call_id is None and not agent.meta_linked:
            self._fifo(s, agent)
        text = f"{agent.label} started" + (f": {agent.task}" if agent.task else "")
        self._act(s, now, aid, "agent", text, "running")

    def _h_subagent_stop(self, s: Session, e: dict, aid: str, now: float) -> None:
        if aid == MAIN:
            return
        agent = self._agent(s, aid, now, e.get("agent_type"))
        agent.status, agent.ended = "done", now
        msg = e.get("last_assistant_message")
        if isinstance(msg, str):
            agent.result = self.redactor.text(msg)
        agent.transcript_path = _str(e.get("agent_transcript_path")) or agent.transcript_path
        self._act(s, now, aid, "agent", f"{agent.label} finished", "ok")

    def _h_stop(self, s: Session, e: dict, aid: str, now: float) -> None:
        s.agents[MAIN].status = "idle"
        self._act(s, now, MAIN, "turn", "Turn finished", "ok")

    def _h_stop_failure(self, s: Session, e: dict, aid: str, now: float) -> None:
        s.agents[MAIN].status = "error"
        parts = []
        for key in ("error", "error_details"):
            v = e.get(key)
            if v:
                parts.append(self.redactor.text(v if isinstance(v, str) else _dumps(v)))
        self._act(s, now, MAIN, "error", "Turn failed: " + " - ".join(parts) if parts else "Turn failed", "error")

    def _h_notification(self, s: Session, e: dict, aid: str, now: float) -> None:
        msg = e.get("message")
        text = self.redactor.text(msg) if isinstance(msg, str) else "Notification"
        kind = e.get("notification_type")
        if isinstance(kind, str) and any(m in kind.lower() for m in WAIT_MARKERS):
            s.agents[MAIN].status = "waiting"
        self._act(s, now, MAIN, "notice", text, "info")

    def _h_pre_compact(self, s: Session, e: dict, aid: str, now: float) -> None:
        trigger = self._ident(e.get("trigger"))
        self._act(s, now, MAIN, "compact", f"Compacting context ({trigger})" if trigger else "Compacting context", "info")

    def _h_post_compact(self, s: Session, e: dict, aid: str, now: float) -> None:
        s.compactions += 1
        self._act(s, now, MAIN, "compact", "Context compacted", "ok")

    def _h_task_created(self, s: Session, e: dict, aid: str, now: float) -> None:
        subject = e.get("task_subject")
        subject = self.redactor.text(subject) if isinstance(subject, str) else ""
        self._act(s, now, aid, "task", f"Task created: {subject}".rstrip(": "), "info")

    def _h_task_completed(self, s: Session, e: dict, aid: str, now: float) -> None:
        subject = e.get("task_subject")
        subject = self.redactor.text(subject) if isinstance(subject, str) else ""
        self._act(s, now, aid, "task", f"Task completed: {subject}".rstrip(": "), "ok")

    def _h_session_end(self, s: Session, e: dict, aid: str, now: float) -> None:
        self._close(s, now, "session_end")
        reason = self._ident(e.get("reason"))
        self._act(s, now, MAIN, "session", f"Session ended ({reason})" if reason else "Session ended", "info")


_HANDLERS: Dict[str, Callable[..., None]] = {
    "SessionStart": Store._h_session_start,
    "UserPromptSubmit": Store._h_prompt,
    "PreToolUse": Store._h_pre_tool,
    "PostToolUse": Store._h_post_tool,
    "PostToolUseFailure": Store._h_tool_failure,
    "SubagentStart": Store._h_subagent_start,
    "SubagentStop": Store._h_subagent_stop,
    "Stop": Store._h_stop,
    "StopFailure": Store._h_stop_failure,
    "Notification": Store._h_notification,
    "PreCompact": Store._h_pre_compact,
    "PostCompact": Store._h_post_compact,
    "TaskCreated": Store._h_task_created,
    "TaskCompleted": Store._h_task_completed,
    "SessionEnd": Store._h_session_end,
}

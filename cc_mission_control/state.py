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

from . import describe, drift
from .config import Config
from .pricing import UsageTotals, Pricing, clean_usage, counts_of, load_pricing, merge_counts, message_cost, normalize_model
from .redact import Redactor, truncate
from .summarize import SUMMARY_CHARS as SUMMARY_TEXT_CHARS, SummaryRequest

log = logging.getLogger(__name__)

MAIN = "main"
DRIFT_EVENTS = ("PostToolUseFailure", "PostCompact")  # events that can move the drift level
SUMMARIZER = "ccmc-summarizer"  # the dashboard's own AI summary calls (SPEC 13.6): tooling, not an agent
USAGE_MESSAGES_MAX = 50_000  # message ids remembered per session
USAGE_PENDING_MAX = 5_000  # usage held per session for agents that have not appeared yet
BIG_WINDOW = 1_000_000
IDENT_CHARS = 200
CWD_CHARS = 1000
DEFAULT_SUBAGENT = "general-purpose"
AGENT_ID_RE = re.compile(r"^[A-Za-z0-9_-]{1,128}$")
LINK_CAP = 256
INJECTED_TAGS = (
    "task-notification", "cross-session-message", "system-reminder", "local-command-stdout",
    "local-command-caveat", "command-name", "command-message", "command-args",
)
INJECTED_RE = re.compile(r"<(" + "|".join(INJECTED_TAGS) + r")(?=[\s>/])")
COMMAND_TAGS = ("command-name", "command-message", "command-args")
COMMAND_RE = {t: re.compile(r"<" + t + r">(.*?)</" + t + r">", re.S) for t in COMMAND_TAGS}
FROM_NAME_RE = re.compile(r"""from-name=(?:"([^"]*)"|'([^']*)')""")
TASK_ID_RE = re.compile(r"<task-id>\s*([^<]*?)\s*</task-id>")
TOOL_ID_RE = re.compile(r"<tool-use-id>\s*([^<]*?)\s*</tool-use-id>")
UNREPORTED = "No result reported (blocked or cancelled)"
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
WAITING_FOR_CHARS = 160
INPUT_CHARS = 400  # PermissionRequest input kept for waiting_detail (SPEC 11.17)
LAST_REPLY_CHARS = 600
RULE_CHARS = 120
MAX_SUGGESTIONS = 3
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
    synthetic: bool = False  # closed by us, not by a Post event
    counted: bool = False  # the synthetic close incremented agent.errors
    plain: str = ""  # plain-language description (set at Pre)
    outcome: str = ""  # plain-language result; "" while running

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
            "plain": self.plain,
            "outcome": self.outcome,
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
    start_entry: Optional[dict] = field(default=None, repr=False)  # its "started" activity dict
    tool_counts: Dict[str, int] = field(default_factory=dict)
    usage: UsageTotals = field(default_factory=UsageTotals, repr=False)
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

    def to_dict(self, full: bool = False) -> dict:
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
            "usage": self.usage.to_dict(),
            "calls": [c.to_dict() for c in (list(self.calls) if full else list(self.calls)[-LANE_CALLS:])],
        }


@dataclass
class Session:
    id: str
    title: str
    started: float
    last_event: float
    cwd: str = ""
    current_cwd: str = ""
    title_source: str = ""  # where cwd/title came from: "start", "transcript" or "event"
    custom_title: Optional[str] = None
    ai_title: Optional[str] = None
    model: Optional[str] = None
    status: str = "active"
    ended: Optional[float] = None
    end_reason: Optional[str] = None
    source: Optional[str] = None
    transcript_path: Optional[str] = None
    compactions: int = 0
    drift_level: str = "low"  # last observed drift level and when it changed (SPEC 11.12)
    drift_since: Optional[float] = None
    drift_score: int = 0
    drift_reasons: List[dict] = field(default_factory=list, repr=False)
    inbox: bool = False  # a nudge socket is known for this session (the socket and token are never stored here)
    prompts: int = 0  # real user prompts to Main (not injected system prompts)
    turns: int = 0  # Main turns finished (Stop, not StopFailure)
    agents: Dict[str, Agent] = field(default_factory=dict)
    activity: Deque[dict] = field(default_factory=lambda: deque(maxlen=ACTIVITY_MAX))
    pending: List[dict] = field(default_factory=list)
    spawns: Dict[str, dict] = field(default_factory=dict)
    stopped: Dict[str, float] = field(default_factory=dict)
    spawn_links: Dict[str, str] = field(default_factory=dict)
    spawn_models: Dict[str, str] = field(default_factory=dict)
    usage: UsageTotals = field(default_factory=UsageTotals, repr=False)  # every agent plus tooling
    tooling_usage: UsageTotals = field(default_factory=UsageTotals, repr=False)
    usage_msgs: Dict[str, list] = field(default_factory=dict, repr=False)  # message id -> [agent, model, speed, geo, counts, cost, split, attributed]
    usage_pending: List[Tuple[str, dict]] = field(default_factory=list, repr=False)  # (agent id, cleaned usage)
    wait_marks: Dict[str, Tuple[float, str]] = field(default_factory=dict, repr=False)  # waiting agent -> (since, text)
    wait_blocked: Dict[str, str] = field(default_factory=dict, repr=False)  # waiting agent -> blocked call id ("" if unknown)
    wait_text: str = field(default="", repr=False)  # text of the notification being handled (consumed by the sync)
    wait_kind: Dict[str, str] = field(default_factory=dict, repr=False)  # waiting agent -> kind from the Notification (11.17)
    wait_cands: Dict[str, List[str]] = field(default_factory=dict, repr=False)  # ambiguous PermissionRequest: candidate call ids
    perm_info: Dict[str, dict] = field(default_factory=dict, repr=False)  # waiting agent -> PermissionRequest facts (11.17)
    last_reply: str = field(default="", repr=False)  # main's last reply (Stop), cleared by the next prompt

    def _waiter(self) -> Optional[Tuple[str, float, str]]:
        """(agent id, since, text) of the earliest-entered agent that is still waiting (SPEC 11.15)."""
        if self.status == "ended":
            return None
        live = [(aid, m[0], m[1]) for aid, m in self.wait_marks.items()
                if aid in self.agents and self.agents[aid].status == "waiting"]
        return min(live, key=lambda m: m[1]) if live else None

    def waiting(self) -> Tuple[Optional[float], Optional[str]]:
        """(waiting_since, waiting_for): the earliest-entered agent that is still waiting (SPEC 11.15)."""
        w = self._waiter()
        return (None, None) if w is None else (w[1], w[2] or None)

    def waiting_detail(self) -> Optional[dict]:
        """What the longest-waiting agent is waiting for (SPEC 11.17). Every string is already redacted."""
        w = self._waiter()
        if w is None:
            return None
        aid, since, text = w
        agent = self.agents[aid]
        info = self.perm_info.get(aid)
        kind = "permission" if info else self.wait_kind.get(aid) or "input"
        call = agent.index.get(self.wait_blocked.get(aid, ""))
        info = info or {}
        return {
            "kind": kind,
            "agent_id": aid,
            "agent_label": "Main" if aid == MAIN else agent.label,
            "since": since,
            "message": text or None,
            "tool": info.get("tool"),
            "mcp_server": info.get("mcp_server"),
            "call_id": call.id if call else None,
            "summary": info.get("summary") or (call.plain if call else None) or None,
            "input": info.get("input") or (call.summary if call else None) or None,
            "suggestions": list(info.get("suggestions", [])),
            "last_reply": (self.last_reply or None) if aid == MAIN and kind in ("idle", "input") else None,
            "task": agent.task[:200] or None,
            "cwd": self.current_cwd or self.cwd or None,
        }

    def ordered_agents(self) -> List[Agent]:
        subs = [a for a in self.agents.values() if a.id != MAIN]
        live = sorted((a for a in subs if a.status != "done"), key=lambda a: a.started)
        done = sorted((a for a in subs if a.status == "done"), key=lambda a: -(a.ended or 0))
        return [self.agents[MAIN], *live, *done]

    def to_dict(self, full: bool = False) -> dict:
        """full=True sends every kept call and activity entry (hub replay), not the live last-N."""
        return {
            "id": self.id,
            "title": self.title,
            "name": self.custom_title or self.ai_title or self.title,
            "name_source": "custom" if self.custom_title else "generated" if self.ai_title else "folder",
            "cwd": self.cwd,
            "current_cwd": self.current_cwd,
            "model": self.model,
            "status": self.status,
            "started": self.started,
            "ended": self.ended,
            "last_event": self.last_event,
            "compactions": self.compactions,
            "waiting_since": self.waiting()[0],
            "waiting_for": self.waiting()[1],
            "waiting_detail": self.waiting_detail(),
            "drift": drift.drift_dict(self),
            "inbox": self.inbox,
            "prompts": self.prompts,
            "turns": self.turns,
            "agents": [a.to_dict(full) for a in self.ordered_agents()],
            "activity": list(self.activity) if full else list(self.activity)[-ACTIVITY_MAX:],
            "usage": self.usage.to_dict(),
            "tooling_usage": self.tooling_usage.to_dict(),
        }


_CONTROL_RE = re.compile(r"[\x00-\x1f\x7f-\x9f\u2028\u2029]+")
# bidi controls and zero-width characters can disguise a name, so they are dropped outright
_INVISIBLE_RE = re.compile(r"[\u200b-\u200f\u202a-\u202e\u2066-\u2069\ufeff]")


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
        activity_max: int = ACTIVITY_MAX,
        pricing: Optional[Pricing] = None,
    ) -> None:
        self.config = config
        self.pricing = pricing or load_pricing(config.pricing_file)
        self.activity_max = max(ACTIVITY_MAX, activity_max)
        self.redactor = redactor or Redactor.from_config(config)
        self.clock = clock
        self._sessions: Dict[str, Session] = {}
        self._changed: set = set()
        self._seq = 0
        self._lock = threading.RLock()
        # Set by the live server when AI summaries are enabled (SPEC 11.6.2). It only enqueues, so
        # the ingestion path never waits. A replaying Store (the hub) leaves it None: no API calls.
        self.summary_sink: Optional[Callable[[SummaryRequest], None]] = None

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

    def session_dict(self, session_id: str, full: bool = False) -> Optional[dict]:
        with self._lock:
            s = self._sessions.get(session_id)
            return s.to_dict(full) if s else None

    def series_counters(self) -> List[dict]:
        """A light read-only view for the live series (SPEC 11.15): only what the ring buffers need."""
        with self._lock:
            out = []
            for s in self._sessions.values():
                since = s.waiting()[0]
                out.append({
                    "id": s.id, "cwd": s.cwd, "started": s.started, "status": s.status, "ended": s.ended,
                    "prompts": s.prompts, "compactions": s.compactions, "waiting_since": since,
                    "cost": s.usage.cost,
                    "agents": [
                        {"id": a.id, "status": a.status, "started": a.started, "errors": a.errors,
                         "tool_counts": dict(a.tool_counts), "context_tokens": a.context_tokens,
                         "context_window": a.context_window}
                        for a in s.agents.values()
                    ],
                })
            return out

    def refresh_drift(self) -> List[str]:
        """Housekeeping: re-evaluate sessions that are not low so idle ones decay; returns those whose level moved."""
        with self._lock:
            now = self.clock()
            moved = [s.id for s in self._sessions.values() if (s.drift_level != "low" or s.drift_score) and drift.evaluate(s, now)]
            self._changed.update(moved)
            return moved

    def set_inbox(self, session_id: str, present: bool) -> None:
        """Record only whether a nudge socket is known (the server keeps the socket and token itself)."""
        with self._lock:
            s = self._sessions.get(session_id)
            if s is not None and s.inbox != present:
                s.inbox = present
                self._changed.add(session_id)

    def handoff(self, session_id: str) -> Optional[dict]:
        """Paste-ready /compact line and handoff brief (SPEC 11.12), redacted."""
        with self._lock:
            s = self._sessions.get(session_id)
            return drift.build_handoff(s, self.redactor.redact) if s else None

    def nudge_text(self, session_id: str) -> Optional[str]:
        with self._lock:
            s = self._sessions.get(session_id)
            return drift.nudge_text(drift.drift_dict(s), self.redactor.redact) if s else None

    def session_calls(self, session_id: str) -> List[Call]:
        """Every kept call of a session, oldest first (the hub derives its file index from these)."""
        with self._lock:
            s = self._sessions.get(session_id)
            calls = [c for a in s.agents.values() for c in a.calls] if s else []
            return sorted(calls, key=lambda c: c.started)

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
            changed = False
            if model and model != "<synthetic>":
                changed = normalize_model(model) != normalize_model(agent.model)
                agent.model = model
                if agent.id == MAIN:
                    s.model = model
            base = self.default_window(agent.model)
            # the window never shrinks while the model stays the same; a new model gets its own window (5.2)
            window = base if changed else max(agent.context_window or 0, base)
            if tokens > window:
                window = BIG_WINDOW
            agent.context_tokens = tokens
            agent.context_window = window
            if agent.id == MAIN:
                drift.evaluate(s, self.clock())
            self._changed.add(session_id)

    def default_window(self, model: Optional[str]) -> int:
        """Window for a model: the table (5.2), else 1M for an id with `1m`, else CCMC_CONTEXT_WINDOW."""
        known = self.pricing.window(model)
        if "1m" in (model or "").lower():  # an explicit `[1m]` beats a 200K table row
            return max(known or 0, BIG_WINDOW)
        return known or self.config.context_window

    def agent_context(self, session_id: str, agent_id: str) -> Optional[Tuple[Optional[int], Optional[int]]]:
        """(context_tokens, context_window) of a lane, or None for an unknown lane."""
        with self._lock:
            s = self._sessions.get(session_id)
            agent = s.agents.get(self._aid(agent_id)) if s else None
            return (agent.context_tokens, agent.context_window) if agent else None

    def agent_type(self, session_id: str, agent_id: str) -> Optional[str]:
        """The lane's agent type: "main" for the main agent, None while unknown."""
        with self._lock:
            s = self._sessions.get(session_id)
            agent = s.agents.get(self._aid(agent_id)) if s else None
            if agent is None:
                return MAIN if agent_id == MAIN else None
            return MAIN if agent.id == MAIN else agent.agent_type

    def add_usage(self, session_id: str, agent_id: str, data: Any) -> bool:
        """Count one API message's usage (SPEC 13.6). Returns False when the data was ignored.

        Messages are keyed by `message_id` per session and merged field-by-field by the highest value,
        because a transcript repeats a message across streamed entries. Usage for a lane that has not
        appeared yet counts in the session at once; only its attribution to the lane waits. `ccmc-summarizer` goes to `tooling_usage`."""
        with self._lock:
            try:
                usage = clean_usage(data)
                s = self._sessions.get(session_id)
                if usage is None or s is None:
                    return False
                aid = SUMMARIZER if agent_id == SUMMARIZER else self._aid(agent_id)
                if aid != SUMMARIZER and aid not in s.agents:
                    mid = usage["message_id"]
                    prior = s.usage_msgs.get(mid) if mid else None
                    self._count_usage(s, aid, usage, held=True)
                    # Known limit: past USAGE_PENDING_MAX held records a message is counted in the session but never
                    # attributed to its lane (SPEC 13.6). A repeat of a held message only merges; one record per message is enough to attribute
                    if not (prior is not None and prior[0] == aid and not prior[7]) \
                            and len(s.usage_pending) < USAGE_PENDING_MAX:
                        s.usage_pending.append((aid, usage))
                    self._changed.add(session_id)
                    return True
                self._count_usage(s, aid, usage)
                self._changed.add(session_id)
                return True
            except Exception:
                log.debug("dropping malformed usage", exc_info=True)
                return False

    def _count_usage(self, s: Session, aid: str, usage: dict, held: bool = False) -> None:
        """Count one message. `held` means its lane is unknown: it counts in the session at once and its
        attribution to the lane waits (`usage_pending`). Entry layout: [agent, model, speed, geo, counts,
        cost, split, attributed]."""
        counts, split = counts_of(usage), usage["split"]
        model, speed, geo = self._ident(usage["model"]), usage["speed"], usage["inference_geo"]
        mid = usage["message_id"]
        old = s.usage_msgs.get(mid) if mid else None
        attributed = not held and aid != SUMMARIZER
        targets = [s.usage, s.tooling_usage] if aid == SUMMARIZER else [s.usage]
        if attributed:
            targets.append(s.agents[aid].usage)
        if old is not None:  # take the earlier version back out, then add the merged one
            aid = old[0]
            merged, merged_split = merge_counts(old[4], old[6], counts, split)
            new_model, new_speed, new_geo = old[1] or model, old[2] or speed, old[3] or geo
            if (merged, merged_split, new_model, new_speed, new_geo) == (old[4], old[6], old[1], old[2], old[3]):
                return
            owner = s.agents.get(aid) if old[7] else None
            targets = [s.usage, s.tooling_usage] if aid == SUMMARIZER else [s.usage, *([owner.usage] if owner else [])]
            for t in targets:
                t.add(old[4], old[5], -1)
            counts, split, model, speed, geo = merged, merged_split, new_model, new_speed, new_geo
            attributed = old[7]
        cost = message_cost(self.pricing, model, counts, speed, geo)
        for t in targets:
            t.add(counts, cost)
        if mid:
            s.usage_msgs[mid] = [aid, model, speed, geo, counts, cost, split, attributed]
            while len(s.usage_msgs) > USAGE_MESSAGES_MAX:  # forget the oldest ids; their counts stay in the totals
                del s.usage_msgs[next(iter(s.usage_msgs))]

    def _apply_pending_usage(self, s: Session, agent_id: str) -> None:
        """Attribute held usage to the lane that just appeared. The session already counted it."""
        if not s.usage_pending:
            return
        mine = [u for a, u in s.usage_pending if a == agent_id]
        if not mine:
            return
        s.usage_pending = [(a, u) for a, u in s.usage_pending if a != agent_id]
        agent = s.agents[agent_id]
        seen = set()
        for usage in mine:
            mid = usage["message_id"]
            if mid:
                if mid in seen:
                    continue
                seen.add(mid)
                entry = s.usage_msgs.get(mid)
                if entry is not None:
                    if entry[0] == agent_id and not entry[7]:
                        agent.usage.add(entry[4], entry[5])
                        entry[7] = True
                    continue
            # no message id, or its id was forgotten: the held record is the only trace
            counts = counts_of(usage)
            agent.usage.add(counts, message_cost(self.pricing, self._ident(usage["model"]), counts,
                                                 usage["speed"], usage["inference_geo"]))
        self._changed.add(s.id)

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
        others = [a for a in s.agents.values() if a is not agent and a.id != MAIN and a.start_entry is not None]
        self._relink(s, agent, desc, tool_use_id)
        for a in [agent, *others]:
            self._refresh_start(s, a)

    def _relink(self, s: Session, agent: Agent, desc: str, tool_use_id: str) -> None:
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

    def set_project_cwd(self, session_id: str, cwd: Any) -> bool:
        """Project folder read from the transcript head. A SessionStart cwd always wins."""
        with self._lock:
            s = self._sessions.get(session_id)
            cwd = self._ident(cwd, CWD_CHARS)
            if s is None or not cwd or s.title_source == "start":
                return False
            title = _title(cwd, session_id)
            if (s.cwd, s.title, s.title_source) == (cwd, title, "transcript"):
                return False
            s.cwd, s.title, s.title_source = cwd, title, "transcript"
            self._changed.add(session_id)
            return True

    def set_custom_title(self, session_id: str, title: Any) -> bool:
        """Custom session name (--name, /rename, hook). Returns True if it changed."""
        return self._set_name(session_id, "custom_title", title)

    def set_ai_title(self, session_id: str, title: Any) -> bool:
        """Generated session name from the transcript. Returns True if it changed."""
        return self._set_name(session_id, "ai_title", title)

    def _set_name(self, session_id: str, attr: str, title: Any) -> bool:
        with self._lock:
            s = self._sessions.get(session_id)
            name = self._name(title)
            if s is None or name is None or getattr(s, attr) == name:
                return False
            setattr(s, attr, name)
            self._changed.add(session_id)
            return True

    def _name(self, value: Any) -> Optional[str]:
        if not isinstance(value, str):
            return None
        flat = _CONTROL_RE.sub(" ", _INVISIBLE_RE.sub("", value)).strip()
        return self._ident(flat) if flat else None

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
            s = Session(id=sid, title=_title(cwd, sid), started=now, last_event=now, cwd=cwd, current_cwd=cwd)
            if self.activity_max != ACTIVITY_MAX:
                s.activity = deque(maxlen=self.activity_max)
            s.title_source = "start" if name == "SessionStart" and cwd else "event" if cwd else ""
            s.agents[MAIN] = self._new_agent(MAIN, "Main", now)
            self._sessions[sid] = s
        elif s.status == "ended" and name != "SessionStart":
            if s.end_reason == "session_end":
                return None
            if name != "SessionEnd":
                s.status, s.ended, s.end_reason = "active", None, None
        s.last_event = now
        cwd = self._ident(event.get("cwd"), CWD_CHARS)
        if cwd:
            s.current_cwd = cwd
            if name == "SessionStart" or not s.cwd:  # the start directory names the project
                s.cwd, s.title = cwd, _title(cwd, sid)
                s.title_source = "start" if name == "SessionStart" else "event"
        s.transcript_path = _str(event.get("transcript_path")) or s.transcript_path
        handler(self, s, event, self._aid(event.get("agent_id")), now)
        self._sync_waiting(s, now)
        if name in DRIFT_EVENTS or s.drift_level != "low" or s.drift_score:
            drift.evaluate(s, now)  # on every event while not low, so decay and `since` replay identically
        self._changed.add(sid)
        return sid

    # ---- helpers ----------------------------------------------------------

    def _end_wait(self, s: Session, agent: Agent, call: Call) -> None:
        """A result ends the wait only if it is for the blocked call (or the agent's only running call)."""
        if agent.status != "waiting":
            return
        blocked = s.wait_blocked.get(agent.id, "")
        cands = s.wait_cands.get(agent.id)
        if cands:  # the request matched several running calls: any of them ending ends the wait
            ends = call.id in cands
        elif blocked:
            ends = call.id == blocked
        else:
            ends = sum(1 for c in agent.calls if c.status == "running") <= 1
        if ends:
            agent.status = "running"

    def _sync_waiting(self, s: Session, now: float) -> None:
        """Track when each agent entered `waiting` and why. Runs after every event, so live and replay agree."""
        text, s.wait_text = s.wait_text, ""
        waiting = {a.id for a in s.agents.values() if a.status == "waiting"}
        for aid in [k for k in s.wait_marks if k not in waiting]:
            del s.wait_marks[aid]
        for aid in [k for k in s.wait_blocked if k not in waiting]:
            del s.wait_blocked[aid]
        for d in (s.wait_kind, s.perm_info, s.wait_cands):
            for aid in [k for k in d if k not in waiting]:
                del d[aid]
        for aid in waiting:
            if aid not in s.wait_marks:
                s.wait_marks[aid] = (now, text[:WAITING_FOR_CHARS])
            elif text and aid not in s.perm_info:  # a newer wait notice: same entry time, newer text (a permission wait keeps its own)
                s.wait_marks[aid] = (s.wait_marks[aid][0], text[:WAITING_FOR_CHARS])

    def _new_agent(self, agent_id: str, label: str, now: float, agent_type: Optional[str] = None) -> Agent:
        return Agent(id=agent_id, label=label, started=now, max_calls=self.config.max_calls, agent_type=agent_type)

    def _agent(
        self, s: Session, agent_id: str, now: float, agent_type: Any = None, revive: bool = False
    ) -> Agent:
        """Get or create a lane. Only revive=True events bring a finished lane back to running."""
        agent_type = self._ident(agent_type)
        agent = s.agents.get(agent_id)
        if agent is None:
            agent = self._new_agent(agent_id, agent_type or "Subagent", now, agent_type)
            stopped_at = s.stopped.pop(agent_id, None)
            if stopped_at is None:
                agent.status = "running"
            else:
                agent.status, agent.ended = "done", stopped_at
            s.agents[agent_id] = agent
            self._apply_pending_usage(s, agent_id)
        elif agent_id != MAIN:
            if agent_type and not agent.agent_type:
                agent.agent_type = agent_type
                if agent.label == "Subagent":
                    agent.label = agent_type
            if revive and agent.status == "done":
                agent.status, agent.ended = "running", None
        return agent

    def _act(
        self, s: Session, now: float, agent_id: str, kind: str, text: str, status: str,
        plain: Optional[str] = None, call_id: Optional[str] = None,
    ) -> None:
        text = self.redactor.redact(text)[:ACTIVITY_CHARS]
        entry = {"t": now, "agent_id": agent_id, "kind": kind, "text": text, "status": status}
        entry["plain"] = text if plain is None else self.redactor.redact(plain)[:ACTIVITY_CHARS]
        if call_id:
            entry["call_id"] = call_id
        s.activity.append(entry)

    def add_summary(self, session_id: str, agent_id: Any, t_start: Any, t_end: Any, text: Any, model: Any = None) -> Optional[str]:
        """Append an AI summary row (SPEC 11.6.2). Also used by hub replay.

        Returns the stored body (redacted, clipped), or None when ignored (session gone, bad text).
        Never raises: this runs inside hub replay, where one bad record must not break a session."""
        try:
            if not isinstance(text, str) or not text.strip():
                return None
            aid = agent_id[:128] if isinstance(agent_id, str) and agent_id else MAIN
            with self._lock:
                s = self._sessions.get(session_id)
                if s is None:
                    return None
                body = self.redactor.redact(text).strip()[:SUMMARY_TEXT_CHARS]
                if not body:
                    return None
                now = self.clock()
                nums = [self._span_time(v, now) for v in (t_start, t_end)]
                entry = {"t": now, "agent_id": self._aid(aid), "kind": "summary", "text": body,
                         "status": "info", "plain": body, "span": nums}
                s.activity.append(entry)
                self._changed.add(session_id)
                return body
        except Exception:
            log.debug("add_summary failed", exc_info=True)
            return None

    @staticmethod
    def _span_time(value: Any, fallback: float) -> float:
        try:
            if isinstance(value, (int, float)) and not isinstance(value, bool):
                v = float(value)
                if math.isfinite(v):
                    return v
        except (OverflowError, ValueError, TypeError):
            pass
        return fallback

    def _request_summary(self, s: Session, aid: str, now: float, final: Any) -> None:
        """Queue a summary request for a finished turn (aid == MAIN) or subagent. Never raises."""
        sink = self.summary_sink
        if sink is None:
            return
        try:
            rows = list(s.activity)
            if aid == MAIN:
                start = 0
                for i in range(len(rows) - 1, -1, -1):
                    if rows[i]["kind"] == "prompt" and rows[i]["agent_id"] == MAIN:
                        start = i
                        break
                t0 = rows[start]["t"] if rows and rows[start]["kind"] == "prompt" else (rows[0]["t"] if rows else now)
                span = [r for r in rows[start:] if r["kind"] not in ("prompt", "summary", "turn")]
                prompt = s.agents[MAIN].task
                if len(span) < 3:
                    return
            else:
                agent = s.agents[aid]
                t0 = agent.started
                span = [r for r in rows if r["agent_id"] == aid and r["t"] >= t0 and r["kind"] not in ("agent", "summary")]
                prompt = agent.task
            lines = []
            for r in span:
                line = r.get("plain") or r["text"]
                call = self._find_call(s, r["call_id"]) if r.get("call_id") else None
                if call is not None and call.outcome:
                    line += f" \u2014 {call.outcome}"
                lines.append(self.redactor.redact(line))
            result = self.redactor.redact(final) if isinstance(final, str) else ""
            if aid != MAIN and not lines and not result:
                return
            sink(SummaryRequest(s.id, aid, t0, now, self.redactor.redact(prompt or ""), lines, result))
        except Exception:
            log.debug("summary request failed", exc_info=True)

    def _plain(self, text: str, limit: int) -> str:
        """Redact first, then clip, so a cut can never expose part of a secret."""
        return describe.clip(self.redactor.redact(text), limit)

    def _set_outcome(self, call: Call, e: dict, resp: Any, error: Any, fixed: Optional[str] = None) -> None:
        """Set the call's outcome. The relay truncates hook strings, so the live result travels with the
        event as `ccmc_outcome` and a replay (the hub) reuses it instead of recomputing from cut-down text."""
        carried = e.get("ccmc_outcome")
        if isinstance(carried, str):
            text = carried
        else:
            text = fixed if fixed is not None else describe.describe_outcome(call.tool, call.input, resp, error)
        call.outcome = self._plain(text, describe.OUTCOME_MAX)
        e["ccmc_outcome"] = call.outcome

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
        call = self._build_call(agent.id, e, now, s.cwd)
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

    def _build_call(self, agent_id: str, e: dict, now: float, cwd: str = "") -> Call:
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
        call.plain = self._plain(describe.describe_call(call.tool, stored, cwd), describe.CALL_MAX)
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
                    c.outcome = "Session ended"
                    c.synthetic, c.counted = True, False
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
        self._custom_from_event(s, e)
        if s.status == "ended":
            s.status, s.ended, s.end_reason = "active", None, None
        main = s.agents[MAIN]
        s.source = self._ident(e.get("source")) or s.source
        if e.get("source") == "resume" and main.status == "waiting":
            main.status = "idle"  # a resumed session cannot still be waiting on the old prompt
        model = self._ident(e.get("model"))
        if model and model != "<synthetic>":
            s.model = main.model = model
        tokens = e.get("context_tokens")
        if _is_int(tokens) and tokens > 0:
            self.set_context(s.id, MAIN, tokens, None)
        self._act(s, now, MAIN, "session", f"Session started ({s.source or 'startup'})", "info")

    def _custom_from_event(self, s: Session, e: dict) -> None:
        name = self._name(e.get("session_title"))
        if name:  # absent or empty never clears: the docs only send it when a custom title exists
            s.custom_title = name

    def _h_prompt(self, s: Session, e: dict, aid: str, now: float) -> None:
        self._custom_from_event(s, e)
        main = s.agents[MAIN]
        prompt = e.get("prompt")
        main.status = "running"
        s.last_reply = ""
        m = INJECTED_RE.match(prompt.lstrip()) if isinstance(prompt, str) else None
        command = self._slash_command(prompt) if m and m.group(1) in COMMAND_TAGS else None
        if command is not None:
            # A typed slash command is real user input: show it as the task, like a normal prompt.
            s.prompts += 1
            main.task = self.redactor.text(command)
            self._act(s, now, MAIN, "prompt", main.task, "info", plain=describe.plain_prompt(main.task))
            return
        # A command-tag prompt that does not parse falls through to the plain-prompt path below.
        if m and m.group(1) not in COMMAND_TAGS:
            tag = m.group(1)
            if tag == "task-notification":
                lane = self._notified_lane(s, prompt)
                text = f"{lane.label} result delivered to Main" if lane else "Background task notification"
                self._act(s, now, MAIN, "agent", text, "ok")
            elif tag == "cross-session-message":
                n = FROM_NAME_RE.search(prompt[: prompt.find(">") if ">" in prompt else 500])
                sender = self._ident((n.group(1) or n.group(2)) if n else None)
                self._act(s, now, MAIN, "notice", f"Message from {sender}" if sender else "Cross-session message", "info")
            else:
                self._act(s, now, MAIN, "notice", "System message", "info")
            return
        if isinstance(prompt, str):
            s.prompts += 1
        main.task = self.redactor.text(prompt) if isinstance(prompt, str) else main.task
        self._act(s, now, MAIN, "prompt", main.task, "info", plain=describe.plain_prompt(main.task))

    @staticmethod
    def _slash_command(prompt: str) -> Optional[str]:
        """Build "/<name> <args>" from Claude Code's command tags, or None if they do not parse.

        The name comes from <command-name> (else <command-message>); a leading slash is stripped
        before one is added back. Whitespace is collapsed. Malformed input returns None so the
        caller treats the prompt as plain text.
        """
        found = {t: COMMAND_RE[t].search(prompt) for t in COMMAND_TAGS}
        name_m = found["command-name"] or found["command-message"]
        if name_m is None:
            return None
        name = " ".join(name_m.group(1).split()).lstrip("/").strip()
        if not name:
            return None
        args = " ".join(found["command-args"].group(1).split()) if found["command-args"] else ""
        return f"/{name} {args}".strip()

    def _notified_lane(self, s: Session, prompt: str) -> Optional[Agent]:
        m = TASK_ID_RE.search(prompt)
        raw = m.group(1) if m else ""
        sub_id = raw[len("agent-"):] if raw.startswith("agent-") else raw
        lane = s.agents.get(sub_id) if AGENT_ID_RE.match(sub_id) and sub_id != MAIN else None
        if lane is None:
            m = TOOL_ID_RE.search(prompt)
            tool_id = m.group(1) if m else ""
            lane = next((a for a in s.agents.values() if tool_id and a.spawn_call_id == tool_id), None)
        return lane

    def _close_unreported(self, s: Session, agent: Agent, now: float) -> None:
        """Calls still running when their agent finishes were blocked or cancelled: no Post will come."""
        for c in agent.calls:
            if c.status != "running":
                continue
            c.status, c.ended, c.error = "error", now, UNREPORTED
            c.outcome = "Blocked or cancelled"
            c.duration_ms = max(0, int((now - c.started) * 1000))
            c.synthetic = c.counted = True
            agent.errors += 1
            self._act(s, now, agent.id, "error", f"{c.tool} blocked or cancelled", "error",
                      plain=self._plain(f"{c.plain or c.tool} — blocked or cancelled", ACTIVITY_CHARS))

    def _h_pre_tool(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"), revive=True)
        if aid == MAIN and agent.status in ("waiting", "idle", "error"):
            agent.status = "running"
        elif agent.status == "waiting":  # a subagent that was asked for permission moves on (11.17)
            agent.status = "running"
        existing = self._find_call(s, self._ident(e.get("tool_use_id")) or "")
        if existing is not None:
            fresh = self._build_call(aid, e, now, s.cwd)
            existing.summary, existing.input, existing.plain = fresh.summary, fresh.input, fresh.plain
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
        self._act(s, now, aid, "tool", f"{call.tool}: {call.summary}", "running", plain=call.plain, call_id=call.id)

    def _h_post_tool(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"))
        call = self._match(s, agent, e, now)
        self._end_wait(s, agent, call)
        self._finish(call, e, now, "ok")
        if call.synthetic:  # a real result arrived after we gave up on it
            owner = s.agents.get(call.agent_id, agent)
            if call.counted:
                owner.errors = max(0, owner.errors - 1)
            call.error, call.synthetic, call.counted = None, False, False
        resp = e.get("tool_response")
        call.output = self._output(resp)
        self._set_outcome(call, e, resp, None)
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
        self._end_wait(s, agent, call)
        self._finish(call, e, now, "error")
        err = e.get("error")
        call.error = self.redactor.text(err if isinstance(err, str) else _dumps(err) if err else "")
        self._count_error(s, agent, call)
        self._drop_spawn(s, call)
        if e.get("is_interrupt") is True:
            text = f"{call.tool} interrupted"
            self._set_outcome(call, e, None, None, fixed="Interrupted")
        else:
            text = f"{call.tool} failed: {_first_line(call.error)}"
            self._set_outcome(call, e, None, err if err else (call.error or "Failed"))
        self._act(s, now, aid, "error", text, "error", plain=f"{call.plain or call.tool} — {call.outcome}")

    def _count_error(self, s: Session, agent: Agent, call: Call) -> None:
        """Count a real failure once, even if a synthetic close already counted it."""
        owner = s.agents.get(call.agent_id, agent)
        if not (call.synthetic and call.counted):
            owner.errors += 1
        call.synthetic = call.counted = False

    def _start_text(self, agent: Agent) -> str:
        return f"{agent.label} started" + (f": {agent.task}" if agent.task else "")

    @staticmethod
    def _helper_name(agent: Agent) -> str:
        task = _first_line(agent.task)
        return task[:80] if task else agent.label

    def _refresh_start(self, s: Session, agent: Agent) -> None:
        """Keep a lane's "started" feed entry in step with its (re)linked task."""
        entry = agent.start_entry
        if entry is not None and any(x is entry for x in s.activity):
            entry["text"] = self.redactor.redact(self._start_text(agent))[:ACTIVITY_CHARS]
            entry["plain"] = self._plain(describe.plain_helper(self._helper_name(agent), "started"), ACTIVITY_CHARS)

    def _drop_spawn(self, s: Session, call: Call) -> None:
        """A spawn call that failed or was denied will never get a lane."""
        if call.tool not in SPAWN_TOOLS:
            return
        if call.subagent_id is None:
            s.pending = [p for p in s.pending if p["tool_use_id"] != call.id]
            s.spawns.pop(call.id, None)
        self._drop_links(s, call.id)

    def _h_permission_denied(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"))
        call = self._match(s, agent, e, now)
        self._finish(call, e, now, "error")
        reason = e.get("reason")
        reason = self.redactor.text(reason) if isinstance(reason, str) and reason.strip() else ""
        call.error = f"Denied: {reason}" if reason else "Denied"
        self._count_error(s, agent, call)
        self._drop_spawn(s, call)
        call.outcome = self._plain(f"Not allowed: {reason}" if reason else "Not allowed", describe.OUTCOME_MAX)
        self._act(s, now, aid, "error", f"{call.tool} denied", "error", plain=describe.plain_denied(call.tool))

    def _h_permission_request(self, s: Session, e: dict, aid: str, now: float) -> None:
        agent = self._agent(s, aid, now, e.get("agent_type"))
        tool_name = self._ident(e.get("tool_name"))
        fake = dict(e, tool_use_id="x")  # a throwaway call: same summary as PreToolUse, no id consumed
        built = self._build_call(aid, fake, now, s.cwd)
        if agent.status != "done":
            running = [c for c in agent.calls if c.status == "running"]
            matches = [c for c in running if tool_name and c.tool == tool_name]
            blocked = matches[0] if len(matches) == 1 else None if matches else (running[-1] if running else None)
            agent.status = "waiting"
            s.wait_blocked[agent.id] = blocked.id if blocked else ""
            s.wait_cands.pop(agent.id, None)
            if len(matches) > 1:  # ambiguous: no guessed call, any candidate's result ends the wait
                s.wait_cands[agent.id] = [c.id for c in matches]
            s.wait_kind.pop(agent.id, None)
            if agent.id not in s.wait_marks:  # this request starts the wait: its text is the banner (SPEC 11.15)
                what = "Permission requested" if not tool_name else (
                    f"Permission to use {tool_name}: {built.plain}" if built.plain else f"Permission to use {tool_name}")
                s.wait_marks[agent.id] = (now, self.redactor.text(what)[:WAITING_FOR_CHARS])
            s.perm_info[agent.id] = {
                "tool": tool_name,
                "mcp_server": self._ident(e.get("mcp_server")),
                "input": self._render_input(e.get("tool_input"))[:INPUT_CHARS] or None,
                "summary": (built.plain if tool_name else "") or None,
                "suggestions": self._suggestions(e.get("permission_suggestions")),
            }
        self._act(s, now, aid, "notice", f"Permission asked: {tool_name or 'unknown tool'} {built.summary}".strip(), "info")

    def _render_input(self, tool_input: Any) -> str:
        """tool_input as text for the permission panel: redacted in full here, cut by the caller."""
        if isinstance(tool_input, dict):
            for key in SUMMARY_KEYS:
                v = tool_input.get(key)
                if v:
                    text = self.redactor.redact(v if isinstance(v, str) else _dumps(v))
                    if text.strip():
                        return text.strip()
            return self.redactor.redact(_dumps(tool_input)) if tool_input else ""
        if tool_input in (None, ""):
            return ""
        return self.redactor.redact(tool_input if isinstance(tool_input, str) else _dumps(tool_input)).strip()

    def _suggestions(self, raw: Any) -> List[dict]:
        out: List[dict] = []
        if not isinstance(raw, list):
            return out
        for entry in raw:
            rules = entry.get("rules") if isinstance(entry, dict) and entry.get("type") == "addRules" else None
            if not isinstance(rules, list):
                continue
            where = self._ident(entry.get("destination"), 40)
            for r in rules:
                name = r.get("toolName") if isinstance(r, dict) else None
                if not isinstance(name, str) or not name.strip():
                    continue
                content = r.get("ruleContent")
                rule = f"{name}({content})" if isinstance(content, str) and content else name
                out.append({"rule": self.redactor.text(rule)[:RULE_CHARS], "where": where})
                if len(out) == MAX_SUGGESTIONS:
                    return out
        return out

    def _h_subagent_start(self, s: Session, e: dict, aid: str, now: float) -> None:
        if aid == MAIN:
            return
        fresh_done = aid not in s.agents and aid in s.stopped
        agent = self._agent(s, aid, now, e.get("agent_type"), revive=True)
        if not fresh_done:
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
        self._act(s, now, aid, "agent", self._start_text(agent), "running",
                  plain=describe.plain_helper(self._helper_name(agent), "started"))
        agent.start_entry = s.activity[-1]

    def _h_subagent_stop(self, s: Session, e: dict, aid: str, now: float) -> None:
        if aid == MAIN:
            return
        if aid not in s.agents:
            kind = self._ident(e.get("agent_type"))
            s.stopped[aid] = now
            while len(s.stopped) > LINK_CAP:
                del s.stopped[next(iter(s.stopped))]
            self._act(s, now, MAIN, "agent", f"{kind} finished" if kind else "Background agent finished", "ok",
                      plain=describe.plain_helper(kind, "finished") if kind else "Background helper agent finished")
            return
        agent = self._agent(s, aid, now, e.get("agent_type"))
        agent.status, agent.ended = "done", now
        self._close_unreported(s, agent, now)
        msg = e.get("last_assistant_message")
        if isinstance(msg, str):
            agent.result = self.redactor.text(msg)
        agent.transcript_path = _str(e.get("agent_transcript_path")) or agent.transcript_path
        self._act(s, now, aid, "agent", f"{agent.label} finished", "ok",
                  plain=describe.plain_helper(self._helper_name(agent), "finished"))
        self._request_summary(s, aid, now, agent.result)

    def _h_stop(self, s: Session, e: dict, aid: str, now: float) -> None:
        s.agents[MAIN].status = "idle"
        s.turns += 1
        self._close_unreported(s, s.agents[MAIN], now)
        self._act(s, now, MAIN, "turn", "Turn finished", "ok", plain=describe.TURN_FINISHED)
        reply = e.get("last_assistant_message")
        s.last_reply = self.redactor.text(reply)[:LAST_REPLY_CHARS] if isinstance(reply, str) else ""
        self._request_summary(s, MAIN, now, e.get("last_assistant_message"))

    def _h_stop_failure(self, s: Session, e: dict, aid: str, now: float) -> None:
        s.agents[MAIN].status = "error"
        self._close_unreported(s, s.agents[MAIN], now)
        parts = []
        for key in ("error", "error_details"):
            v = e.get(key)
            if v:
                parts.append(self.redactor.text(v if isinstance(v, str) else _dumps(v)))
        self._act(s, now, MAIN, "error", "Turn failed: " + " - ".join(parts) if parts else "Turn failed", "error")
        self._request_summary(s, MAIN, now, e.get("last_assistant_message"))

    def _h_notification(self, s: Session, e: dict, aid: str, now: float) -> None:
        msg = e.get("message")
        text = self.redactor.text(msg) if isinstance(msg, str) else "Notification"
        kind = e.get("notification_type")
        if isinstance(kind, str) and any(m in kind.lower() for m in WAIT_MARKERS):
            main = s.agents[MAIN]
            if main.status != "waiting":  # the call it waits on: the newest one still running
                s.wait_blocked[MAIN] = next((c.id for c in reversed(main.calls) if c.status == "running"), "")
            main.status = "waiting"
            s.wait_text = text
            low = kind.lower()  # a recorded PermissionRequest still forces kind "permission" in waiting_detail
            s.wait_kind[MAIN] = ("permission" if "permission" in low else "idle" if "idle" in low
                                 else "elicitation" if "elicitation" in low else "input")
        self._act(s, now, MAIN, "notice", text, "info")

    def _h_pre_compact(self, s: Session, e: dict, aid: str, now: float) -> None:
        trigger = self._ident(e.get("trigger"))
        self._act(s, now, MAIN, "compact", f"Compacting context ({trigger})" if trigger else "Compacting context", "info",
                  plain=describe.COMPACTING)

    def _h_post_compact(self, s: Session, e: dict, aid: str, now: float) -> None:
        s.compactions += 1
        self._act(s, now, MAIN, "compact", "Context compacted", "ok", plain=describe.COMPACTED)

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
    "PermissionDenied": Store._h_permission_denied,
    "PermissionRequest": Store._h_permission_request,
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

"""Task and error report (SPEC 13.7): tasks from hook boundaries, failures from the replayed calls.

Pure functions, no I/O, and nothing here raises on odd input. The hub feeds every hook record of a
session to a `TaskTracker` (in replay order) and calls `build_report` on the replayed Store.
"""

from __future__ import annotations

import json
import logging
import math
from bisect import bisect_left, bisect_right
from typing import Any, Dict, List, Optional, Tuple

from . import describe, efficiency
from .state import BIG_WINDOW, COMMAND_TAGS, INJECTED_RE, MAIN, UNREPORTED, Call, Store, _norm_agent

log = logging.getLogger(__name__)

DESC_MAX = 2000
OUTCOME_MAX = 300
REASON_MAX = 500
CHANGES_MAX = 10
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
UNTRACKED = "(activity before the first recorded prompt)"
READ_TOOLS = ("Read", "NotebookRead", "Grep", "Glob")
INF = math.inf


def tool_class(tool: str) -> str:
    """A fix must be in the failed call's class: reading never fixes a failed write (13.7)."""
    return "read" if tool in READ_TOOLS else "write" if tool in EDIT_TOOLS else tool


def classify_prompt(prompt: Any) -> Tuple[str, str]:
    """("task", description), ("injected", "") or ("none", "") by the Store's own rules (state.INJECTED_RE)."""
    if not isinstance(prompt, str) or not prompt.strip():
        return "none", ""
    m = INJECTED_RE.match(prompt.lstrip())
    if m and m.group(1) in COMMAND_TAGS:
        command = Store._slash_command(prompt)
        if command is not None:
            return "task", command
    elif m:
        return "injected", ""
    return "task", prompt.strip()

TURN, SUBAGENT = "turn", "subagent"


def _text(redactor: Any, value: Any, limit: int) -> str:
    """Redact first, then collapse whitespace and clip."""
    if not isinstance(value, str) or not value:
        return ""
    try:
        return describe.clip(redactor.redact(value), limit)
    except Exception:
        return ""


def _num(value: Any, default: float = 0.0) -> float:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return default


class TaskTracker:
    """Task boundaries and StopFailure reasons for one session, from its hook records in replay order."""

    def __init__(self, redactor: Any, stale_seconds: float = 0.0) -> None:
        self.redactor = redactor
        self.stale_seconds = stale_seconds  # CCMC_STALE_MINUTES: a silent subagent is closed (0 = never)
        self.first_t: Optional[float] = None
        self._prompted = False  # a real prompt has been seen
        self._untracked_made = False
        self._injected_open = False  # an injected prompt's own turn is running
        self._sub_seen: Dict[str, float] = {}
        self.tasks: List[dict] = []  # in start order
        self._open_turn: Optional[dict] = None
        self._subs: Dict[str, dict] = {}
        self._ids: set = set()
        self.stop_failures: List[Tuple[float, str, Optional[str]]] = []  # (t, reason, task_id)
        self.interrupts: set = set()  # tool_use_ids of interrupted calls
        self.summaries: List[Tuple[str, float, float, str]] = []  # (agent_id, t_start, t_end, text)
        self._ended = False  # after SessionEnd the Store ignores events until a SessionStart
        # applied context updates per agent: parallel lists of (t, tokens, window), in replay (time) order (13.8)
        self.ctx: Dict[str, Tuple[List[float], List[int], List[Optional[int]]]] = {}

    # ---- feeding ----------------------------------------------------------

    def feed(self, event: Any, t: Any) -> None:
        """Record one hook event. Never raises."""
        try:
            self._feed(event, _num(t))
        except Exception:
            log.debug("tracker skipped a record", exc_info=True)

    def note_summary(self, agent_id: Any, t_start: Any, t_end: Any, text: Any) -> None:
        try:
            if isinstance(text, str) and text:
                aid = _norm_agent(agent_id)[:128] if isinstance(agent_id, str) and agent_id else MAIN
                self.summaries.append((aid or MAIN, _num(t_start), _num(t_end, INF), text))
        except Exception:
            log.debug("tracker skipped a summary", exc_info=True)

    def note_context(self, agent_id: Any, t: Any, tokens: Any, window: Any) -> None:
        """Record one context update the Store accepted (peak context per task, SPEC 13.8). Never raises."""
        try:
            if isinstance(tokens, bool) or not isinstance(tokens, int):
                return
            aid = _norm_agent(agent_id)[:128] if isinstance(agent_id, str) and agent_id else MAIN
            ts, toks, wins = self.ctx.setdefault(aid or MAIN, ([], [], []))
            ts.append(_num(t))
            toks.append(tokens)
            wins.append(window if isinstance(window, int) and not isinstance(window, bool) else None)
        except Exception:
            log.debug("tracker skipped a context update", exc_info=True)

    def _new(self, task_id: str, agent_id: str, kind: str, description: str, t: float) -> dict:
        base, n = task_id, 1
        while task_id in self._ids:
            n += 1
            task_id = f"{base}.{n}"
        self._ids.add(task_id)
        task = {"task_id": task_id, "agent_id": agent_id, "kind": kind, "description": description,
                "started": t, "ended": None, "status": "running", "message": ""}
        self.tasks.append(task)
        return task

    def _end(self, task: Optional[dict], status: str, t: float, message: str = "") -> None:
        if task is not None and task["status"] == "running":
            task["status"], task["ended"] = status, t
            if message:
                task["message"] = message

    def _feed(self, e: Any, t: float) -> None:
        if not isinstance(e, dict):
            return
        if self.first_t is None:
            self.first_t = t
        name = e.get("hook_event_name")
        if name == "SessionStart":
            self._ended = False
            return
        if self._ended or not isinstance(name, str):
            return
        agent = _norm_agent(e.get("agent_id"))[:128] or MAIN
        self._expire_stale(t)
        if agent != MAIN:
            self._sub_seen[agent] = t
        if name == "UserPromptSubmit":
            self._prompt(e.get("prompt"), t)
        elif name in ("Stop", "StopFailure"):
            self._stop(name, e, t)
        elif name == "SubagentStart":
            if agent != MAIN and agent not in self._subs:
                self._sub(agent, t)
        elif name == "SubagentStop":
            if agent != MAIN and agent in self._subs:
                self._end(self._subs[agent], "ok", t, _text(self.redactor, e.get("last_assistant_message"), OUTCOME_MAX))
        elif name == "SessionEnd":
            self._end(self._open_turn, "incomplete", t)
            self._open_turn = None
            for task in self._subs.values():
                self._end(task, "incomplete", t)
            self._ended = True
        elif name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            if agent != MAIN and agent not in self._subs:  # the first event of a new lane
                self._sub(agent, t)
            if agent == MAIN:
                self._untracked(t)
            if name == "PostToolUseFailure" and e.get("is_interrupt") is True and isinstance(e.get("tool_use_id"), str):
                self.interrupts.add(e["tool_use_id"])

    def _prompt(self, prompt: Any, t: float) -> None:
        kind, text = classify_prompt(prompt)
        if kind == "none":
            return
        if kind == "injected":  # runs its own turn: its Stop must not end an open user turn
            self._injected_open = True
            return
        self._injected_open = False
        self._prompted = True
        self._end(self._open_turn, "incomplete", t)
        self._open_turn = self._new(f"main@{t:.3f}", MAIN, TURN, _text(self.redactor, text, DESC_MAX), t)

    def _untracked(self, t: float) -> Optional[dict]:
        """Main-agent activity before the first recorded prompt goes into a synthetic turn (the hub joined
        mid-session, or a resume). Its id comes from the first record of the session."""
        if self._prompted or self._open_turn is not None or self._untracked_made:
            return self._open_turn
        first = self.first_t if self.first_t is not None else t
        self._untracked_made = True
        self._open_turn = self._new(f"main@{first:.3f}", MAIN, TURN, UNTRACKED, first)
        return self._open_turn

    def ensure_untracked(self) -> dict:
        """The synthetic turn for failures nothing else owns (idempotent). It closes where the first prompt starts."""
        found = next((x for x in self.tasks if x["description"] == UNTRACKED and x["kind"] == TURN), None)
        if found is not None:
            return found
        self._untracked_made = True
        first = self.first_t if self.first_t is not None else 0.0
        task = self._new(f"main@{first:.3f}", MAIN, TURN, UNTRACKED, first)
        later = [x["started"] for x in self.tasks if x is not task and x["agent_id"] == MAIN and x["started"] >= first]
        if later:
            task["status"], task["ended"] = "incomplete", min(later)
        return task

    def _stop(self, name: str, e: dict, t: float) -> None:
        injected = self._injected_open
        self._injected_open = False
        task = self._open_turn
        if task is None and not injected:
            task = self._untracked(t)
        message = _text(self.redactor, e.get("last_assistant_message"), OUTCOME_MAX)
        if name == "StopFailure":
            parts = []
            for key in ("error", "error_details"):
                v = e.get(key)
                if v:
                    parts.append(v if isinstance(v, str) else json.dumps(v, default=str))
            reason = _text(self.redactor, " - ".join(parts), REASON_MAX) or "Turn failed"
            owner = task or next((x for x in reversed(self.tasks) if x["kind"] == TURN), None)
            self.stop_failures.append((t, reason, owner["task_id"] if owner else None))
        if injected:  # the injected turn ended; the user turn (if any) is still open
            return
        if name == "Stop":
            self._end(task, "ok", t, message)
        else:
            self._end(task, "error", t, message)
        self._open_turn = None

    def _expire_stale(self, t: float) -> None:
        if self.stale_seconds <= 0:
            return
        for agent, task in self._subs.items():
            seen = self._sub_seen.get(agent, task["started"])
            if task["status"] == "running" and t - seen > self.stale_seconds:
                self._end(task, "incomplete", seen)

    def _sub(self, agent: str, t: float) -> dict:
        task = self._new(f"agent:{agent}", agent, SUBAGENT, "", t)
        self._subs[agent] = task
        return task


def _needs_untracked(tracker: TaskTracker, calls: List[Call], permissions: Any) -> bool:
    """Main-agent activity that no task window can hold (safety net: the tracker normally makes the turn itself)."""
    mains = [x["started"] for x in tracker.tasks if x["agent_id"] == MAIN]
    first = min(mains) if mains else INF
    if any(c.agent_id == MAIN and c.status == "error" and c.started < first for c in calls):
        return True
    return not mains and (any(c.status == "error" for c in calls) or any(len(p) >= 6 and p[3] == "denied" for p in permissions))


def _context_of(tracker: TaskTracker, task: dict) -> Tuple[Optional[int], Optional[int]]:
    """(peak context, window) of a task (13.8). Peak: the highest update with started <= t < window_end, null when
    there is none. Window: the agent's window as of the last update at or before the task's end."""
    sample = tracker.ctx.get(task["agent_id"])
    if not sample:
        return None, None
    ts, toks, wins = sample
    lo, hi = bisect_left(ts, task["started"]), bisect_left(ts, task["window_end"])
    peak = max(toks[lo:hi]) if hi > lo else None
    end = task["ended"] if task.get("ended") is not None else INF
    i = bisect_right(ts, end)
    seen = [w for w in wins[lo:hi] if w]  # a later update may have raised the window (the peak can come from one)
    if i > 0 and wins[i - 1]:
        seen.append(wins[i - 1])
    return peak, fit_window(max(seen) if seen else None, peak)


def fit_window(window: Optional[int], peak: Optional[int]) -> Optional[int]:
    """A window is never below the peak it is shown against; if it is, the Store's own rule applies (BIG_WINDOW)."""
    if peak is not None and window is not None and window < peak:
        return max(BIG_WINDOW, peak)
    return window


def _empty_report() -> Tuple[List[dict], List[dict]]:
    return [], []


def build_report(
    tracker: TaskTracker, store: Store, session_id: str, permissions: Any = (), summaries: Any = None,
) -> Tuple[List[dict], List[dict]]:
    """(tasks, failures) for one session. Never raises."""
    try:
        return _build(tracker, store, session_id, permissions or (), tracker.summaries if summaries is None else summaries)
    except Exception:
        log.warning("could not build the task report for a session", exc_info=True)
        return _empty_report()


def _build(tracker: TaskTracker, store: Store, session_id: str, permissions: Any, summaries: Any) -> Tuple[List[dict], List[dict]]:
    red = store.redactor
    sd = store.session_dict(session_id)
    if sd is None:
        return _empty_report()
    cwd = sd.get("cwd") or ""
    agents = {a["id"]: a for a in sd.get("agents", [])}
    session_over = sd.get("status") == "ended"
    calls = store.session_calls(session_id)
    by_agent: Dict[str, List[Call]] = {}
    for c in calls:
        by_agent.setdefault(c.agent_id, []).append(c)
    spawn_desc = {c.subagent_id: c.input.get("description") for c in calls
                  if c.subagent_id and isinstance(c.input, dict) and isinstance(c.input.get("description"), str)}

    # ---- tasks and windows
    tasks: List[dict] = []
    windows: Dict[str, List[dict]] = {}
    pending_orphans = _needs_untracked(tracker, calls, permissions)
    if pending_orphans:
        tracker.ensure_untracked()
    for tt in tracker.tasks:
        task = dict(tt)
        agent = agents.get(task["agent_id"], {})
        task["agent_type"] = MAIN if task["agent_id"] == MAIN else agent.get("agent_type")
        if task["kind"] == SUBAGENT:
            desc = spawn_desc.get(task["agent_id"]) or agent.get("task") or agent.get("label") or ""
            task["description"] = _text(red, desc, DESC_MAX)
        if task["status"] == "running" and session_over:
            task["status"], task["ended"] = "incomplete", _num(sd.get("ended"), task["started"])
        tasks.append(task)
        windows.setdefault(task["agent_id"], []).append(task)
    for lst in windows.values():
        lst.sort(key=lambda x: x["started"])
        for i, task in enumerate(lst):
            task["window_end"] = lst[i + 1]["started"] if i + 1 < len(lst) else INF

    win_starts = {a: [x["started"] for x in lst] for a, lst in windows.items()}

    def owner(agent_id: str, when: float) -> Optional[dict]:
        """The task whose window holds `when`. A failure is never dropped: before an agent's first task it
        belongs to that task, and an agent with no task of its own falls back to the main turns."""
        found = None
        lst = windows.get(agent_id, ())
        i = bisect_right(win_starts.get(agent_id, ()), when) - 1  # windows are disjoint and sorted: one candidate
        if i >= 0 and when < lst[i]["window_end"]:
            found = lst[i]
        if found is None:
            lst = windows.get(agent_id) or windows.get(MAIN) or []
            found = (lst[0] if when < lst[0]["started"] else lst[-1]) if lst else None
        return found

    # ---- failures from calls
    perm_by_call = {p[1]: p for p in permissions if len(p) >= 6 and p[3] == "denied" and p[1]}
    keys: Dict[str, str] = {c.id: describe.target_key(c.tool, c.input, cwd) for c in calls}
    ok_calls = [c for c in calls if c.status == "ok"]
    failures: List[dict] = []
    seen = set()

    def add(fail: dict, key: Optional[str], call: Optional[Call], task: Optional[dict] = None) -> None:
        task = task or owner(fail["agent_id"], fail["t"])
        if task is None or fail["call_id"] in seen:
            return
        seen.add(fail["call_id"])
        fail["task_id"] = task["task_id"]
        fail["agent_type"] = task["agent_type"]
        fail.update(resolution="", fix_call_id=None, fix_description=None, fix_outcome=None, fix_t=None,
                    attempts=0, changes=[], changes_total=0)
        fail["_key"], fail["_call"], fail["_task"] = key, call, task
        failures.append(fail)

    for c in calls:
        if c.status != "error":
            continue
        t = c.ended if c.ended is not None else c.started
        error = c.error or ""
        if c.synthetic and error == UNREPORTED:
            kind, reason = "blocked", UNREPORTED
        elif c.synthetic:  # still running when the session ended ("Session ended")
            kind, reason = "abandoned", error or "Session ended"
        elif error.startswith("Denied"):
            kind = "denied"
            perm = perm_by_call.get(c.id)
            reason = (perm[4] if perm and perm[4] else error[len("Denied"):].lstrip(": ")) or "Denied"
            t = _num(perm[5], t) if perm else t
        elif c.id in tracker.interrupts or c.outcome == "Interrupted":
            kind, reason = "interrupted", error or "Interrupted"
        else:
            kind, reason = "tool_error", error or c.outcome or "Failed"
        add({"call_id": c.id, "agent_id": c.agent_id, "t": t, "tool": c.tool, "description": _text(red, c.plain or c.tool, 160),
             "kind": kind, "reason": _text(red, reason, REASON_MAX)}, keys.get(c.id), c)

    # a denial whose call the Store has since evicted: the permissions index still has it
    for p in permissions:
        if len(p) >= 6 and p[3] == "denied" and p[1] and p[1] not in keys:
            add({"call_id": p[1], "agent_id": p[0], "t": _num(p[5]), "tool": p[2],
                 "description": _text(red, describe.plain_denied(p[2]), 160), "kind": "denied",
                 "reason": _text(red, p[4] or "Denied", REASON_MAX)}, None, None)

    for t, reason, task_id in tracker.stop_failures:
        task = next((x for x in tasks if x["task_id"] == task_id), None) or owner(MAIN, t)
        if task is None:
            continue
        add({"call_id": f"stopfail@{t:.3f}", "agent_id": MAIN, "t": t, "tool": "StopFailure",
             "description": "The turn stopped because of an API error", "kind": "api_error",
             "reason": reason}, None, None, task)

    # ---- resolutions
    for f in failures:
        task, key, call = f.pop("_task"), f.pop("_key"), f.pop("_call")
        status = task["status"]
        if f["kind"] == "interrupted":
            f["resolution"] = "n/a"
            continue
        fix = None
        if call is not None and key is not None:
            lo = f["t"]
            hi = task["window_end"]
            for c in ok_calls:
                if (c.agent_id == f["agent_id"] and c.id != f["call_id"] and keys.get(c.id) == key
                        and tool_class(c.tool) == tool_class(f["tool"])
                        and lo <= c.started < hi and c.started >= task["started"]):
                    fix = c
                    break  # ok_calls is oldest first
        if fix is not None:
            f["resolution"] = "fixed"
            f["fix_call_id"], f["fix_description"] = fix.id, _text(red, fix.plain or fix.tool, 160)
            f["fix_outcome"] = _text(red, fix.outcome, 160)
            f["fix_t"] = fix.ended if fix.ended is not None else fix.started
            cls = tool_class(f["tool"])
            prev_ok = max([c.started for c in ok_calls if c.agent_id == f["agent_id"] and keys.get(c.id) == key
                           and tool_class(c.tool) == cls and c.started < f["t"] and c.id != fix.id] or [-INF])
            f["attempts"] = max(1, sum(
                1 for c in by_agent.get(f["agent_id"], ()) if c.status == "error" and keys.get(c.id) == key
                and tool_class(c.tool) == cls and not c.synthetic and c.id not in tracker.interrupts and c.outcome != "Interrupted"
                and prev_ok < (c.ended if c.ended is not None else c.started) < fix.started
                and task["started"] <= c.started < task["window_end"]))
            edits = [c for c in by_agent.get(f["agent_id"], ()) if c.tool in EDIT_TOOLS and c.status == "ok"
                     and f["t"] <= c.started < fix.started]
            f["changes_total"] = len(edits)
            f["changes"] = [_text(red, c.plain or c.tool, 160) for c in edits[:CHANGES_MAX]]
        elif status == "running":
            f["resolution"] = "pending"
        elif status == "ok":
            f["resolution"] = "worked_around"
        else:
            f["resolution"] = "unresolved"

    # ---- tools and call attribution (13.8): every call goes to the task that owns its start time
    tool_counts: Dict[str, Dict[str, List[int]]] = {}
    call_ids: Dict[str, List[str]] = {}
    task_calls: Dict[str, List[Call]] = {}
    for c in calls:
        owner_task = owner(c.agent_id, c.started)
        if owner_task is None:
            continue
        cell = tool_counts.setdefault(owner_task["task_id"], {}).setdefault(c.tool or "", [0, 0])
        cell[0] += 1
        cell[1] += 1 if c.status == "error" else 0
        call_ids.setdefault(owner_task["task_id"], []).append(c.id)
        task_calls.setdefault(owner_task["task_id"], []).append(c)

    # ---- task fields
    call_starts = {a: [c.started for c in lst] for a, lst in by_agent.items()}  # calls are in start order
    for task in tasks:
        task["tools"] = {k: (v[0], v[1]) for k, v in tool_counts.get(task["task_id"], {}).items()}
        task["call_ids"] = call_ids.get(task["task_id"], [])
        task["peak_context"], task["context_window"] = _context_of(tracker, task)
        if task["peak_context"] is not None and not task["context_window"]:  # never leave the percentage unknowable
            ag = agents.get(task["agent_id"], {})
            task["context_window"] = ag.get("context_window") or store.default_window(ag.get("model"))
        task["context_window"] = fit_window(task["context_window"], task["peak_context"])
        starts = call_starts.get(task["agent_id"], ())
        task["calls"] = bisect_left(starts, task["window_end"]) - bisect_left(starts, task["started"])
        mine = [f for f in failures if f["task_id"] == task["task_id"]]
        task["errors"] = len(mine)
        task["fixed"] = sum(1 for f in mine if f["resolution"] == "fixed")
        task["unresolved"] = sum(1 for f in mine if f["resolution"] == "unresolved")
        text = ""
        best = None
        for aid, t0, t1, body in summaries:
            if aid == task["agent_id"] and task["started"] <= t1 < task["window_end"] and (best is None or t1 >= best[0]):
                best = (t1, body)
        if best:
            text = _text(red, best[1], OUTCOME_MAX)
        text = text or task.get("message") or f"{task['calls']} actions, {task['errors']} errors"
        task["outcome"] = text
        # call-based efficiency measures (13.10): retries and waste signals; tokens are joined in by the caller
        task["eff"] = efficiency.call_measures(task_calls.get(task["task_id"], ()), keys, cwd, red.redact)
        task["eff"]["denied"] = max(task["eff"]["denied"], sum(1 for f in mine if f["kind"] == "denied"))
        task.pop("message", None)
        task["window_end"] = None if task["window_end"] == INF else task["window_end"]
    failures.sort(key=lambda f: (f["t"], f["call_id"]))
    for f in failures:
        f["changes"] = list(f["changes"])
    return tasks, failures

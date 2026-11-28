"""Live dashboard ring buffers (SPEC 11.15).

Memory only: nothing here touches the disk, is relayed, or runs on the `/hook` path. The server's
broadcast loop feeds `LiveSeries.sample` with the session dicts it already builds; counters are
turned into events by differencing between samples.
"""

from __future__ import annotations

import time
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

CONTEXT_EVERY = 15.0  # seconds between context points per active session
COST_EVERY = 60.0
COMPACT_WAIT = 30.0  # a compaction's marker point waits this long for the context value to change
CONTEXT_WINDOW = 60 * 60  # context and cost history, seconds
SHORT_WINDOW = 30 * 60  # errors, tools and timeline history, seconds
ENDED_KEEP = 60 * 60  # a buffer outlives its session's end by this long
TIMELINE_MAX = 500
MIN_INTERVAL = 1.0  # sampling throttle: the broadcast loop ticks far faster than this
UNKNOWN = "unknown"  # project key of a session whose project is not known

KINDS = ("prompt", "compaction", "launch", "error", "wait_start", "wait_end")


def _num(value: Any) -> int:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else 0


class _Seen:
    """What the last sample saw of one session, so the next one can emit the difference."""

    __slots__ = ("prompts", "compactions", "errors", "tools", "waiting", "last_ctx", "last_pct", "compact_t", "agents")

    def __init__(self) -> None:
        self.prompts = 0
        self.compactions = 0
        self.errors = 0
        self.tools: Dict[str, int] = {}
        self.waiting = False
        self.last_ctx: Optional[float] = None
        self.last_pct: Optional[float] = None
        self.compact_t: Optional[float] = None  # a compaction still waiting for its marker point
        self.agents: set = set()


class LiveSeries:
    def __init__(
        self,
        clock: Callable[[], float] = time.time,
        project_of: Callable[[dict], Optional[str]] = lambda s: None,
        min_interval: float = MIN_INTERVAL,
    ) -> None:
        self.clock = clock
        self.project_of = project_of
        self.min_interval = min_interval
        self.t0 = clock()
        self._last_sample: Optional[float] = None
        self._last_cost: Optional[float] = None
        self._seen: Dict[str, _Seen] = {}
        self._context: Dict[str, Deque[tuple]] = {}
        self._cost: Deque[Tuple[float, float]] = deque()
        self._cost_by_project: Dict[str, float] = {}
        self._errors: Dict[int, Dict[str, int]] = {}  # minute start -> {project: n}
        self._tools: Deque[Tuple[float, str, int]] = deque()
        self._timeline: Deque[dict] = deque(maxlen=TIMELINE_MAX)

    # ---- sampling ---------------------------------------------------------

    def due(self, now: Optional[float] = None) -> bool:
        now = self.clock() if now is None else now
        return self._last_sample is None or now - self._last_sample >= self.min_interval or now < self._last_sample

    def sample(self, sessions: List[dict], now: Optional[float] = None, force: bool = False) -> bool:
        """Fold one look at the sessions into the buffers. Returns False when throttled."""
        now = self.clock() if now is None else now
        if not force and not self.due(now):
            return False
        self._last_sample = now
        present = set()
        total = 0.0
        by_project: Dict[str, float] = {}
        for s in sessions:
            sid = s.get("id")
            if not isinstance(sid, str):
                continue
            present.add(sid)
            pk = self.project_of(s) or UNKNOWN
            cost = _num(s.get("cost"))
            total += cost
            by_project[pk] = by_project.get(pk, 0.0) + cost
            self._sample_session(s, sid, pk, now)
        self._cost_by_project = {k: round(v, 6) for k, v in by_project.items()}
        if self._last_cost is None or now - self._last_cost >= COST_EVERY or now < self._last_cost:
            self._last_cost = now
            self._cost.append((now, round(total, 6)))
        for sid in [k for k in self._seen if k not in present]:
            del self._seen[sid]
            self._context.pop(sid, None)
        self._trim(now)
        return True

    def _sample_session(self, s: dict, sid: str, pk: str, now: float) -> None:
        seen = self._seen.get(sid)
        agents = [a for a in s.get("agents") or [] if isinstance(a, dict)]
        prompts, compactions = _num(s.get("prompts")), _num(s.get("compactions"))
        errors = sum(_num(a.get("errors")) for a in agents)
        tools: Dict[str, int] = {}
        for a in agents:
            for tool, n in (a.get("tool_counts") or {}).items():
                tools[tool] = tools.get(tool, 0) + _num(n)
        waiting = s.get("waiting_since") is not None
        ids = {a.get("id") for a in agents if a.get("id") != "main"}
        if seen is None:
            seen = self._seen[sid] = _Seen()
            born = _num(s.get("started")) >= self.t0 - 1
            if not born:  # it started before the series did (a restart): baseline only, no back-fill
                seen.prompts, seen.compactions, seen.errors, seen.tools, seen.waiting = prompts, compactions, errors, dict(tools), waiting
                seen.agents = set(ids)
        new_agents = ids - seen.agents
        if new_agents:  # `launch` is a subagent appearing
            self._event(now, sid, pk, "launch", len(new_agents))
            seen.agents |= new_agents
        if prompts > seen.prompts:
            self._event(now, sid, pk, "prompt", prompts - seen.prompts)
        if compactions > seen.compactions:
            self._event(now, sid, pk, "compaction", compactions - seen.compactions)
            seen.compact_t = now
        if errors > seen.errors:
            n = errors - seen.errors
            self._event(now, sid, pk, "error", n)
            minute = self._errors.setdefault(int(now // 60) * 60, {})
            minute[pk] = minute.get(pk, 0) + n
        for tool, n in tools.items():
            d = n - seen.tools.get(tool, 0)
            if d > 0:
                self._tools.append((now, tool, d))
        if waiting and not seen.waiting:
            self._event(_num(s.get("waiting_since")) or now, sid, pk, "wait_start", 1)
        elif seen.waiting and not waiting:
            self._event(now, sid, pk, "wait_end", 1)
        seen.prompts, seen.compactions, seen.errors, seen.tools, seen.waiting = prompts, compactions, errors, tools, waiting
        self._sample_context(s, sid, seen, agents, now)

    def _sample_context(self, s: dict, sid: str, seen: _Seen, agents: List[dict], now: float) -> None:
        ended = s.get("ended")
        if s.get("status") == "ended":
            if isinstance(ended, (int, float)) and now - ended > ENDED_KEEP:
                self._context.pop(sid, None)
            return
        main = agents[0] if agents else {}
        tokens, window = main.get("context_tokens"), main.get("context_window")
        if not isinstance(tokens, (int, float)) or not isinstance(window, (int, float)) or window <= 0:
            if seen.compact_t is not None and now - seen.compact_t >= COMPACT_WAIT:
                seen.compact_t = None  # the context stayed unknown: no marker is stamped later
            return
        pct = round(min(100.0, max(0.0, tokens / window * 100)), 1)
        if seen.compact_t is not None and (pct != seen.last_pct or now - seen.compact_t >= COMPACT_WAIT):
            seen.compact_t, seen.last_ctx, seen.last_pct = None, now, pct
            self._context.setdefault(sid, deque()).append((now, pct, "compaction"))
            return
        if seen.last_ctx is not None and 0 <= now - seen.last_ctx < CONTEXT_EVERY:
            return
        seen.last_ctx, seen.last_pct = now, pct
        self._context.setdefault(sid, deque()).append((now, pct))

    def _event(self, t: float, sid: str, pk: str, kind: str, n: int) -> None:
        self._timeline.append({"t": t, "sid": sid, "project_key": pk, "kind": kind, "n": n})

    def _trim(self, now: float) -> None:
        for pts in self._context.values():
            while pts and now - pts[0][0] > CONTEXT_WINDOW:
                pts.popleft()
        while self._cost and now - self._cost[0][0] > CONTEXT_WINDOW:
            self._cost.popleft()
        oldest = int(now // 60) * 60 - SHORT_WINDOW + 60
        for minute in [m for m in self._errors if m < oldest]:
            del self._errors[minute]
        while self._tools and now - self._tools[0][0] > SHORT_WINDOW:
            self._tools.popleft()
        while self._timeline and now - self._timeline[0]["t"] > SHORT_WINDOW:
            self._timeline.popleft()

    # ---- reading ----------------------------------------------------------

    def to_dict(self, now: Optional[float] = None) -> dict:
        now = self.clock() if now is None else now
        last = int(now // 60) * 60
        errors = []
        for i in range(SHORT_WINDOW // 60 - 1, -1, -1):
            m = last - i * 60
            errors.append({"t": m, "by_project": dict(self._errors.get(m, {}))})
        tools: Dict[str, int] = {}
        for t, tool, n in self._tools:
            if now - t <= SHORT_WINDOW:
                tools[tool] = tools.get(tool, 0) + n
        return {
            "now": now,
            "context": {sid: [list(p) for p in pts if now - p[0] <= CONTEXT_WINDOW] for sid, pts in self._context.items() if pts},
            "cost": {
                "points": [[t, c] for t, c in self._cost if now - t <= CONTEXT_WINDOW],
                "by_project": dict(self._cost_by_project),
            },
            "errors": errors,
            "tools": dict(sorted(tools.items(), key=lambda kv: -kv[1])),
            "timeline": sorted((dict(e) for e in self._timeline if now - e["t"] <= SHORT_WINDOW), key=lambda e: e["t"])[-TIMELINE_MAX:],
        }

"""Live task efficiency for the local server (SPEC 13.10).

Keeps the 13.7 task split for the live sessions with the same `TaskTracker` code the hub replays, measures
running and finished tasks, scores them with `efficiency.evaluate`, and maintains the local baseline file
(`<data_dir>/efficiency_baseline.json`, mode 0600, ratios only) merged with the hub's baseline.

Ingestion never waits: hooks, usage and context updates are queued and applied the next time the lock is free.
Everything that can take more than a few milliseconds (the report build, the file, the hub fetch) runs off the
event loop. Nothing here raises into the caller, and a failed hub fetch is silent.
"""

from __future__ import annotations

import asyncio
import json
import logging
import math
import os
import threading
import time
from bisect import bisect_left
from collections import OrderedDict, deque
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from . import describe, efficiency
from .pricing import clean_usage, counts_of, merge_counts, message_cost
from .report import TaskTracker, build_report
from .state import MAIN, SUMMARIZER, _norm_agent

log = logging.getLogger(__name__)

BASELINE_FILE = "efficiency_baseline.json"
FILE_MODE = 0o600
RECENT_SECONDS = 3600.0  # finished tasks stay in the live answer this long
ACTIVE_TASKS_MAX = 100  # newest tasks of an active session
HARVEST_DELAY = 60.0  # a finished task joins the baseline once its usage has had time to arrive
CACHE_TTL = 5.0  # the computed answer is reused this long
TASKS_MAX = 300  # newest tasks in an answer, plus every running one
PENDING_MAX = 20_000  # queued hook, usage and context notes; the oldest are dropped past this
HUB_MAX_BYTES = 4 * 1024 * 1024
DEDUPE_SECONDS = 0.05  # rows whose finish times are this close are one task
MAX_FAILURE_RATE = 10.0
SAVE_INTERVAL = 30.0
HUB_INTERVAL = 6 * 3600.0
HUB_RETRY = 600.0
HUB_TIMEOUT = 10.0
MAX_SESSIONS = 64
MAX_MESSAGES = 20_000  # usage messages kept per session
POOLS_MAX = 300
TS_PAST = 10 * 365 * 86400
POOL_CHUNK = 1  # pools merged per lock hold
BASELINE_PATH = "/api/history/efficiency/baseline"
PUBLIC_MEASURES = ("duration_s", "tokens", "cost_usd", "calls", "errors", "retries", "cache_reuse", "subagent_share",
                   "peak_pct", "tokens_per_call", "failure_rate", "seconds_per_call", "waste")


def _finite(v: Any) -> bool:
    return isinstance(v, (int, float)) and not isinstance(v, bool) and math.isfinite(v)


class Baseline:
    """Ratio rows `[t, tokens_per_call, failure_rate, cache_reuse, seconds_per_call]`, newest first, per (kind, project).

    Projects are held as short hashes, so neither the file nor the hub exchange carries text, paths or ids."""

    def __init__(self) -> None:
        self.pools: Dict[Tuple[str, str], List[list]] = {}
        self.dirty = False
        self._kind_cache: Dict[str, List[list]] = {}

    @staticmethod
    def clean_row(row: Any) -> Optional[list]:
        """A valid row, or None: finite numbers, nothing negative, failure_rate <= 10, cache_reuse within 0-1."""
        if not isinstance(row, (list, tuple)) or len(row) != 1 + len(efficiency.RATIOS):
            return None
        if not _finite(row[0]) or any(v is not None and not _finite(v) for v in row[1:]):
            return None
        out = [round(float(row[0]), 3)] + [None if v is None else round(float(v), 6) for v in row[1:]]
        if any(v is not None and v < 0 for v in out):
            return None
        if out[2] is not None and out[2] > MAX_FAILURE_RATE:
            return None
        if out[3] is not None and out[3] > 1:
            return None
        return out

    @staticmethod
    def _near(times: List[float], t: float) -> bool:
        """True when an ascending list has a time within DEDUPE_SECONDS of t."""
        i = bisect_left(times, t - DEDUPE_SECONDS)
        return i < len(times) and times[i] <= t + DEDUPE_SECONDS

    def add(self, kind: str, pkey: str, row: Any) -> bool:
        """Add one local row unless a row of the same task (within 50 ms) is already there."""
        row = self.clean_row(row)
        if row is None or not isinstance(kind, str) or not kind or not isinstance(pkey, str):
            return False
        rows = self.pools.setdefault((kind[:200], pkey[:64]), [])
        if self._near(sorted(r[0] for r in rows), row[0]):
            return False
        rows.append(row)
        rows.sort(key=lambda r: -r[0])
        del rows[efficiency.BASELINE_MAX:]
        self.dirty = True
        self._kind_cache.pop(kind, None)
        self._trim()
        return True

    def _trim(self) -> None:
        if len(self.pools) > POOLS_MAX:
            for k in sorted(self.pools, key=lambda k: self.pools[k][0][0] if self.pools[k] else 0)[: len(self.pools) - POOLS_MAX]:
                del self.pools[k]
                self._kind_cache.pop(k[0], None)

    @staticmethod
    def prepare(doc: Any) -> List[Tuple[str, str, List[list]]]:
        """Validate a hub document without touching the baseline: [(kind, project key, clean rows newest first)]."""
        pools = doc.get("pools") if isinstance(doc, dict) and doc.get("version") == efficiency.BASELINE_VERSION else None
        out: List[Tuple[str, str, List[list]]] = []
        if not isinstance(pools, list):
            return out
        for p in pools[:POOLS_MAX]:
            if not (isinstance(p, dict) and isinstance(p.get("rows"), list) and isinstance(p.get("kind"), str)
                    and isinstance(p.get("project_key"), str) and p["kind"]):
                continue
            rows = [r for r in (Baseline.clean_row(x) for x in p["rows"][: efficiency.BASELINE_MAX]) if r is not None]
            if rows:
                rows.sort(key=lambda r: -r[0])
                out.append((p["kind"][:200], p["project_key"][:64], rows))
        return out

    def merge_pool(self, kind: str, pkey: str, hub_rows: List[list]) -> int:
        """Merge hub rows into one pool in a single linear pass. A local row within 50 ms of a hub row is the same
        task, and the hub's row is kept. Returns rows added."""
        have = self.pools.setdefault((kind, pkey), [])
        hub_times = sorted(r[0] for r in hub_rows)
        kept = [r for r in have if not self._near(hub_times, r[0])]
        merged = sorted(kept + hub_rows, key=lambda r: -r[0])[: efficiency.BASELINE_MAX]
        added = len(merged) - len(have)
        self.pools[(kind, pkey)] = merged
        self._kind_cache.pop(kind, None)
        if merged != have:
            self.dirty = True
        self._trim()
        return max(0, added)

    def merge(self, doc: Any) -> int:
        """Merge a whole hub document (a convenience for tests; the server merges pool by pool)."""
        return sum(self.merge_pool(k, p, rows) for k, p, rows in self.prepare(doc))

    def rows(self, kind: str, pkey: str, before: Optional[float] = None) -> Tuple[List[list], List[list]]:
        """(project rows, kind rows across projects), newest first, only rows older than `before`."""
        proj = self.pools.get((kind, pkey), [])
        every = self._kind_cache.get(kind)
        if every is None:
            every = []
            for (k, _), rows in self.pools.items():
                if k == kind:
                    every += rows
            every.sort(key=lambda r: -r[0])
            kept: List[list] = []
            for r in every:  # the same task can sit in two pools only through a clock quirk: count it once
                if not kept or abs(kept[-1][0] - r[0]) > DEDUPE_SECONDS:
                    kept.append(r)
            every = kept[: efficiency.BASELINE_MAX]
            self._kind_cache[kind] = every
        if before is None:
            return proj, every
        b = round(before, 3)
        return [r for r in proj if r[0] < b], [r for r in every if r[0] < b]

    def size(self) -> int:
        return sum(len(r) for r in self.pools.values())

    def to_doc(self) -> dict:
        return {"version": efficiency.BASELINE_VERSION,
                "pools": [{"kind": k, "project": p, "rows": rows} for (k, p), rows in sorted(self.pools.items())]}

    def load(self, path: Path) -> None:
        try:
            doc = json.loads(path.read_text(encoding="utf-8"))
            pools = doc.get("pools") if isinstance(doc, dict) and doc.get("version") == efficiency.BASELINE_VERSION else None
            for p in pools[:POOLS_MAX] if isinstance(pools, list) else ():
                if isinstance(p, dict) and isinstance(p.get("rows"), list):
                    for row in p["rows"][: efficiency.BASELINE_MAX]:
                        self.add(p.get("kind"), p.get("project"), row)
        except (OSError, ValueError, TypeError, AttributeError, RecursionError):
            pass
        self.dirty = False

    def save(self, path: Path) -> None:
        body = json.dumps(self.to_doc(), separators=(",", ":"), allow_nan=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        tmp = path.with_name(path.name + ".tmp")
        fd = os.open(str(tmp), os.O_WRONLY | os.O_CREAT | os.O_TRUNC, FILE_MODE)
        try:
            with os.fdopen(fd, "w", encoding="utf-8") as f:
                f.write(body)
            os.chmod(tmp, FILE_MODE)
            os.replace(tmp, path)
        finally:
            try:
                tmp.unlink()
            except OSError:
                pass
        self.dirty = False


class _Sess:
    def __init__(self, redactor: Any, stale_seconds: float) -> None:
        self.tracker = TaskTracker(redactor, stale_seconds)
        self.perms: List[tuple] = []  # (agent_id, call_id, tool, kind, text, t), as the hub's replay keeps them
        self.msgs: Dict[str, Dict[str, list]] = {}  # agent -> message key -> [t, counts, split, cost]
        self.harvested: set = set()
        self.effs: Dict[str, dict] = {}  # task id -> call measures, never going down (evicted calls)
        self.final: Dict[str, tuple] = {}  # task id -> (measures, result) kept once harvested
        self.last = 0.0
        self.anon = 0
        self.settled = False  # ended long ago and fully harvested: nothing left to compute


class LiveEfficiency:
    def __init__(
        self,
        config: Any,
        store: Any,
        *,
        clock: Callable[[], float] = time.time,
        hub_url: Optional[str] = None,
        hub_token: Optional[str] = None,
        client_factory: Optional[Callable[[], Any]] = None,
    ) -> None:
        self.store = store
        self.clock = clock
        self.stale_seconds = max(0, getattr(config, "stale_minutes", 0)) * 60.0
        self.path = Path(config.data_dir) / BASELINE_FILE
        self.hub_url = hub_url
        self.hub_token = hub_token
        self.client_factory = client_factory
        self.baseline = Baseline()
        self.baseline.load(self.path)
        self.hub_ok = False  # the hub baseline has been merged this run
        self.hub_last: Optional[float] = None
        self._sessions: "OrderedDict[str, _Sess]" = OrderedDict()
        self._pending: Deque[tuple] = deque()
        self._lock = threading.Lock()
        self._rev = 0
        self._cache: Optional[Tuple[float, int, dict]] = None
        self._saved = 0.0
        self._attached = False
        self.dropped = 0  # queued notes dropped because the queue was full

    # ---- ingestion (queued; never waits) -------------------------------------------------------------------

    def attach(self) -> None:
        """Observe the Store's usage and context updates (the transcript tailer calls the Store directly)."""
        if self._attached:
            return
        self._attached = True
        store = self.store
        add_usage, set_context = store.add_usage, store.set_context

        def watched_usage(session_id: str, agent_id: str, data: Any) -> bool:
            ok = add_usage(session_id, agent_id, data)
            if ok:
                self.note_usage(session_id, agent_id, data)
            return ok

        def watched_context(session_id: str, agent_id: str, tokens: int, model: Optional[str]) -> None:
            set_context(session_id, agent_id, tokens, model)
            try:
                got = store.agent_context(session_id, agent_id)
                if got is not None and got[0] == tokens:  # the Store applied it
                    self.note_context(session_id, agent_id, tokens, got[1])
            except Exception:
                log.debug("efficiency context note failed", exc_info=True)

        store.add_usage, store.set_context = watched_usage, watched_context

    def feed(self, session_id: str, event: Any) -> None:
        self._queue(("hook", session_id, event, self.clock()))

    def note_usage(self, session_id: str, agent_id: Any, data: Any) -> None:
        if agent_id != SUMMARIZER:  # the dashboard's own summary calls are tooling, not a task's tokens
            self._queue(("usage", session_id, agent_id, data, self.clock()))

    def note_context(self, session_id: str, agent_id: Any, tokens: int, window: Any) -> None:
        self._queue(("ctx", session_id, agent_id, tokens, window, self.clock()))

    def _queue(self, op: tuple) -> None:
        if len(self._pending) >= PENDING_MAX:  # bounded: drop the oldest note and count it
            try:
                self._pending.popleft()
                self.dropped += 1
            except IndexError:
                pass
        self._pending.append(op)
        self._rev += 1
        if self._lock.acquire(blocking=False):
            try:
                self._drain()
            finally:
                self._lock.release()

    def _sess(self, sid: str) -> _Sess:
        s = self._sessions.get(sid)
        if s is None:
            s = self._sessions[sid] = _Sess(self.store.redactor, self.stale_seconds)
            while len(self._sessions) > MAX_SESSIONS:
                self._sessions.popitem(last=False)
        else:
            self._sessions.move_to_end(sid)
        return s

    def _drain(self) -> None:
        """Apply the queued operations in order. Called with the lock held."""
        while self._pending:
            try:
                op = self._pending.popleft()
            except IndexError:
                return
            try:
                getattr(self, "_apply_" + op[0])(*op[1:])
            except Exception:
                log.debug("efficiency op skipped", exc_info=True)

    def _apply_hook(self, sid: str, event: Any, t: float) -> None:
        if not isinstance(event, dict):
            return
        s = self._sess(sid)
        s.last = t
        s.settled = False
        s.tracker.feed(event, t)
        name = event.get("hook_event_name")
        if name == "PermissionDenied":  # as the hub's replay notes it
            agent = _norm_agent(event.get("agent_id"))[:128] or MAIN
            reason = event.get("reason") if isinstance(event.get("reason"), str) else ""
            s.perms.append((agent, str(event.get("tool_use_id") or ""), str(event.get("tool_name") or "")[:200], "denied",
                            self.store.redactor.text(reason), t))
            del s.perms[:-2000]

    def _apply_ctx(self, sid: str, agent_id: Any, tokens: int, window: Any, t: float) -> None:
        self._sess(sid).tracker.note_context(agent_id, t, tokens, window)

    def _apply_usage(self, sid: str, agent_id: Any, data: Any, now: float) -> None:
        usage = clean_usage(data)
        if usage is None:
            return
        s = self._sess(sid)
        s.settled = False
        aid = _norm_agent(agent_id)[:128] or MAIN
        ts = data.get("ts") if isinstance(data, dict) else None
        t = ts if _finite(ts) and now - TS_PAST <= ts <= now + 86400 else now  # as the hub trusts a message time
        counts, split = counts_of(usage), usage["split"]
        mid = usage.get("message_id")
        lane = s.msgs.setdefault(aid, {})
        if not mid:
            s.anon += 1
        key = mid or f"~{s.anon}"
        old = lane.get(key)
        if old is not None:
            counts, split = merge_counts(old[1], old[2], counts, split)
            t = min(t, old[0])
        lane[key] = [t, counts, split, message_cost(self.store.pricing, usage["model"], counts, usage["speed"], usage["inference_geo"])]
        while len(lane) > MAX_MESSAGES:
            lane.pop(next(iter(lane)))

    # ---- the live answer -------------------------------------------------------------------------------------

    def snapshot(self) -> dict:
        """`/api/efficiency/live` (13.10). Cached for a moment; safe to call from a worker thread."""
        with self._lock:
            self._drain()
            now = self.clock()
            if self._cache and 0 <= now - self._cache[0] < CACHE_TTL:
                return self._cache[2]
            try:
                tasks = self._compute(now)
            except Exception:
                log.exception("live efficiency failed")
                tasks = []
            body = {"tasks": tasks, "baseline": self.baseline_info()}
            self._cache = (now, self._rev, body)
            return body

    def baseline_info(self) -> dict:
        size = self.baseline.size()
        return {"source": "hub" if self.hub_ok else "local" if size else "none", "size": size}

    def _usage(self, s: _Sess, agent: str, lo: float, hi: float) -> dict:
        tot = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0, "cost_usd": 0.0}
        for t, c, _, cost in s.msgs.get(agent, {}).values():
            if lo <= t < hi:
                tot["input"] += c[0]
                tot["cache_creation"] += c[1] + c[2]
                tot["cache_read"] += c[3]
                tot["output"] += c[4]
                tot["cost_usd"] += cost or 0.0
        return tot

    def _compute(self, now: float) -> List[dict]:
        out: List[dict] = []
        for sid, s in list(self._sessions.items()):
            sd = self.store.session_dict(sid)
            if sd is None:
                continue
            tasks, _ = build_report(s.tracker, self.store, sid, s.perms)
            if not tasks:
                continue
            active = sd.get("status") == "active"
            if s.settled and not active:
                continue
            pkey = efficiency.project_key(sd.get("title"))
            inf = float("inf")
            busy = False
            usage, tokens = {}, {}
            for x in tasks:
                u = self._usage(s, x["agent_id"], x["started"], inf if x.get("window_end") is None else x["window_end"])
                usage[x["task_id"]] = u
                tokens[x["task_id"]] = u["input"] + u["cache_creation"] + u["cache_read"] + u["output"]
            subs = [x for x in tasks if x["kind"] == "subagent"]
            newest = {x["task_id"] for x in sorted(tasks, key=lambda x: -x["started"])[:ACTIVE_TASKS_MAX]}
            for x in tasks:
                running = x["status"] == "running"
                ended = x.get("ended")
                recent = ended is not None and now - ended <= RECENT_SECONDS
                shown = running or recent or (active and x["task_id"] in newest)
                ready = ended is not None and now - ended >= HARVEST_DELAY and x["task_id"] not in s.harvested
                if not shown and not ready:
                    continue
                sub_tokens = None
                if x["kind"] == "turn":
                    hi = inf if x.get("window_end") is None else x["window_end"]
                    sub_tokens = sum(tokens[y["task_id"]] for y in subs if x["started"] <= y["started"] < hi)
                tid = x["task_id"]
                kind = efficiency.kind_of(x["kind"], x.get("agent_type"))
                if tid in s.final:  # computed once its usage had arrived; evicted calls cannot change it now
                    m, res = s.final[tid]
                else:
                    s.effs[tid] = efficiency.merge_eff(s.effs.get(tid), x.get("eff"))  # never goes down
                    m = efficiency.build_measures(x, s.effs[tid], usage[tid], sub_tokens, now=now)
                    pool: list = []
                    if not running and m["calls"] >= efficiency.MIN_CALLS and ended is not None:
                        proj, every = self.baseline.rows(kind, pkey, before=ended)
                        pool, _ = efficiency.select_pool(proj, [] if len(proj) >= efficiency.POOL_PROJECT_MIN else every)
                    res = efficiency.evaluate(m, pool, kind, running)
                    if ready:
                        s.harvested.add(tid)
                        s.final[tid] = (m, res)
                        if x["status"] == "ok" and m["calls"] >= efficiency.MIN_CALLS:
                            self.baseline.add(kind, pkey, [ended, *efficiency.ratio_row(m)])
                busy = busy or running or shown or ready
                if shown:
                    out.append({
                        "sid": sid, "task_id": tid, "agent_id": x["agent_id"], "kind": kind, "status": x["status"],
                        "title": describe.task_title(x.get("description") or "")[0],
                        "started": x["started"], "ended": ended,
                        "measures": {k: m[k] for k in PUBLIC_MEASURES},
                        "score": res["score"], "label": res["label"], "reasons": res["reasons"],
                    })
            if not active and not busy and now - s.last > RECENT_SECONDS + HARVEST_DELAY:
                s.settled = True
        out.sort(key=lambda r: (-(r["started"] or 0), r["sid"], r["task_id"]))
        running = [r for r in out if r["status"] == "running"]
        rest = [r for r in out if r["status"] != "running"][:TASKS_MAX]
        return sorted(running + rest, key=lambda r: (-(r["started"] or 0), r["sid"], r["task_id"]))

    # ---- maintenance and the hub ---------------------------------------------------------------------------

    def maintain(self) -> None:
        """Harvest finished tasks into the baseline and save the file when it changed. Run off the event loop."""
        self.snapshot()
        if self.baseline.dirty and self.clock() - self._saved >= SAVE_INTERVAL and self._lock.acquire(blocking=False):
            try:
                self.baseline.save(self.path)
                self._saved = self.clock()
            except OSError:
                log.debug("could not save the efficiency baseline", exc_info=True)
            finally:
                self._lock.release()

    def flush(self) -> None:
        with self._lock:
            if self.baseline.dirty:
                try:
                    self.baseline.save(self.path)
                except OSError:
                    log.debug("could not save the efficiency baseline", exc_info=True)

    async def _download(self) -> Optional[bytes]:
        """The hub baseline body, or None. At most HUB_MAX_BYTES (checked on Content-Length and while streaming)."""
        headers = {"Authorization": "Bearer " + self.hub_token} if self.hub_token else {}
        async with self.client_factory() as client:
            async with client.stream("GET", self.hub_url + BASELINE_PATH, params={"limit": 200}, headers=headers,
                                     timeout=HUB_TIMEOUT) as resp:
                if resp.status_code != 200:
                    return None
                declared = resp.headers.get("content-length", "")
                if declared.isdigit() and int(declared) > HUB_MAX_BYTES:
                    log.warning("hub baseline refused: %s bytes is over the %d byte cap", declared, HUB_MAX_BYTES)
                    return None
                body = bytearray()
                async for chunk in resp.aiter_bytes():
                    body += chunk
                    if len(body) > HUB_MAX_BYTES:
                        log.warning("hub baseline refused: more than the %d byte cap", HUB_MAX_BYTES)
                        return None
                return bytes(body)

    async def fetch_hub(self) -> bool:
        """Fetch the hub's baseline and merge it (13.10). Silent on any failure; True when merged.

        The body is capped, the whole fetch is bounded by HUB_TIMEOUT, and parsing and merging run in a worker thread."""
        if not self.hub_url or self.client_factory is None:
            return False
        try:
            body = await asyncio.wait_for(self._download(), HUB_TIMEOUT + 2)
            if body is None:
                return False
            prepared = await asyncio.to_thread(self._parse_hub, body)
            if prepared is None:
                return False
            await asyncio.to_thread(self._merge_hub, prepared)
            return True
        except Exception:
            log.debug("hub baseline fetch failed", exc_info=True)
            return False

    @staticmethod
    def _parse_hub(body: bytes) -> Optional[List[Tuple[str, str, List[list]]]]:
        doc = json.loads(body)
        if not (isinstance(doc, dict) and doc.get("version") == efficiency.BASELINE_VERSION and isinstance(doc.get("pools"), list)):
            return None  # another formula version, or not a baseline at all
        return Baseline.prepare(doc)

    def _merge_hub(self, prepared: List[Tuple[str, str, List[list]]]) -> None:
        """Merge pool by pool, taking the lock for one linear pass at a time (never for the whole document)."""
        for kind, pkey, rows in prepared:
            with self._lock:
                self.baseline.merge_pool(kind, pkey, rows)
        with self._lock:
            self.hub_ok = True
            self.hub_last = self.clock()
            self._rev += 1

    async def run_hub(self, sleep: Callable[[float], Any]) -> None:
        while True:
            ok = await self.fetch_hub()
            await sleep(HUB_INTERVAL if ok else HUB_RETRY)

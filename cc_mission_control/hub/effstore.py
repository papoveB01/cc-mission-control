"""Hub side of task efficiency (SPEC 13.10): measures per session, one global scoring pass, the row field, the
overview and the baseline export.

Measures are written per session (`write_measures`). Scores are assigned in a separate pass over every session
(`score_pass`), ordered by task end time, so the result never depends on the order sessions were replayed. A task's
baseline is the tasks that finished before it, so a stored score only changes with EFFICIENCY_VERSION.
"""

from __future__ import annotations

import json
import logging
import sqlite3
from bisect import bisect_left
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from .. import efficiency as eff

log = logging.getLogger(__name__)
INF = float("inf")
FREEZE_GRACE = 300.0  # a finished task is recomputed until this long after its session's last received record
OVERVIEW_GROUPS = 50
BASELINE_POOLS = 200
BASELINE_LIMIT_MAX = eff.BASELINE_MAX
PASS_BATCH = 2000
EFFICIENCY_FILTERS = ("efficient", "typical", "wasteful", "unrated")
RATED_SQL = ",".join(f"'{x}'" for x in eff.RATED_LABELS)
PENDING = "pending"  # measured, not scored yet (never shown: the API reports it as no label)

# The four baseline ratios of a finished task, from the inputs stored when its measures were written (eff_*, retries,
# cache_reuse), never from columns that keep changing. One expression scores and exports a pool.
RATIO_COLS = (
    "t.ended, CASE WHEN t.eff_tokens > 0 THEN t.eff_tokens * 1.0 / t.eff_calls END,"
    " (t.eff_errors + COALESCE(t.retries, 0)) * 1.0 / t.eff_calls, t.cache_reuse, t.eff_duration / t.eff_calls"
)
POOL_WHERE = ("t.efficiency_version = ? AND t.status = 'ok' AND t.eff_calls >= ? AND t.ended IS NOT NULL"
              " AND t.efficiency_kind = ?")


def _row(r: Any) -> list:
    return [None if v is None else round(float(v), 6) for v in tuple(r)]


def pool_sql(project: bool, extra: str = "") -> str:
    """The pool query. INDEXED BY pins the plan: the project pool reads (kind, project, ended) already ordered."""
    idx = "tasks_eff_project" if project else "tasks_eff_kind"
    return (f"SELECT {RATIO_COLS} FROM tasks AS t INDEXED BY {idx} WHERE {POOL_WHERE}{extra}"
            + (" AND t.eff_project = ?" if project else "") + " AND t.ended < ? ORDER BY t.ended DESC LIMIT ?")


def pool_rows(conn: sqlite3.Connection, kind: str, project_key: Optional[str], before: float, limit: int = eff.BASELINE_MAX) -> List[list]:
    """Baseline rows `[t, tokens_per_call, failure_rate, cache_reuse, seconds_per_call]` of finished `ok` tasks
    with at least MIN_CALLS calls that ended strictly before `before`, newest first. `project_key` is the stored
    project hash; None pools every project."""
    args: list = [eff.EFFICIENCY_VERSION, eff.MIN_CALLS, kind]
    if project_key is not None:
        args.append(project_key)
    args += [before, limit]
    return [_row(r) for r in conn.execute(pool_sql(project_key is not None), args)]


def _pool_for(conn: sqlite3.Connection, kind: str, pkey: str, before: float) -> List[list]:
    proj = pool_rows(conn, kind, pkey, before)
    return eff.select_pool(proj, [] if len(proj) >= eff.POOL_PROJECT_MIN else pool_rows(conn, kind, None, before))[0]


# ---- writing measures -------------------------------------------------------------------------------------------

def _usage_index(db: sqlite3.Connection, key: Tuple[str, str]) -> Dict[str, tuple]:
    """Per agent: sorted message times and running sums of input, cache_creation, cache_read and output."""
    out: Dict[str, tuple] = {}
    for r in db.execute(
        "SELECT agent_id, t, input, cache_creation, cache_read, output FROM agent_usage"
        " WHERE machine_id=? AND session_id=? ORDER BY agent_id, t", key):
        ts, sums = out.setdefault(r[0], ([], [[0, 0, 0, 0]]))
        ts.append(r[1])
        prev = sums[-1]
        sums.append([prev[i] + (r[2 + i] or 0) for i in range(4)])
    return out


def _window_usage(index: Dict[str, tuple], agent_id: str, lo: float, hi: Optional[float]) -> dict:
    ts, sums = index.get(agent_id, ([], [[0, 0, 0, 0]]))
    a, b = bisect_left(ts, lo), bisect_left(ts, INF if hi is None else hi)
    d = [sums[b][i] - sums[a][i] for i in range(4)] if b > a else [0, 0, 0, 0]
    return {"input": d[0], "cache_creation": d[1], "cache_read": d[2], "output": d[3]}


def _loads(text: Any) -> list:
    try:
        value = json.loads(text) if text else []
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def _touched(r: dict, touched: List[Tuple[str, float]]) -> bool:
    hi = r["window_end"] if r["window_end"] is not None else INF
    return any(a == r["agent_id"] and r["started"] <= t < hi for a, t in touched)


def write_measures(svc: Any, m: str, sid: str, project: str, task_effs: Dict[str, Optional[dict]],
                   old: Dict[str, tuple], last_received: float, touched: Optional[List[Tuple[str, float]]] = None) -> None:
    """Compute and store the measures of a session's tasks (13.10), inside the caller's transaction.

    `task_effs` maps task id to the call measures from the replay. `old` maps task id to the stored
    (efficiency_version, efficiency_label, ended, retries, waste, retry_tools, eff_tokens, eff_calls, eff_errors,
    eff_duration, cache_reuse, efficiency_kind, subagent_share, peak_context, context_window) from before this write. `touched` lists the
    (agent id, time) of the records in this batch: only tasks whose window and agent got one, the open tasks and
    new tasks are recomputed (None recomputes every task, as a rebuild does). A rated task whose inputs did not
    change keeps its stored score; it never drops back to `pending` on an unrelated event.
    A finished, rated task keeps its stored values once 300 s have passed since the session's last received record
    (usage can arrive late, e.g. after a hub outage). A finished task with at least 3 calls is left `pending` for
    `score_pass`. Stored retries and waste counts never go down for an existing row of the same version."""
    db, key = svc.db, (m, sid)
    rows = [dict(r) for r in db.execute(
        "SELECT task_id, kind, agent_id, agent_type, started, ended, window_end, status, calls, errors, peak_context,"
        " context_window FROM tasks WHERE machine_id=? AND session_id=?", key) if r[0] in task_effs]
    if not rows:
        return
    pkey = eff.project_key(project)
    denied = {r[0]: r[1] for r in db.execute(
        "SELECT task_id, COUNT(*) FROM failures WHERE machine_id=? AND session_id=? AND kind='denied' GROUP BY task_id", key)}
    index = _usage_index(db, key)
    tokens: Dict[str, int] = {}
    usage: Dict[str, dict] = {}
    for r in rows:
        u = _window_usage(index, r["agent_id"], r["started"], r["window_end"])
        usage[r["task_id"]] = u
        tokens[r["task_id"]] = u["input"] + u["cache_creation"] + u["cache_read"] + u["output"]
    subs = [r for r in rows if r["kind"] == "subagent"]
    now = svc.clock()
    db.execute("UPDATE tasks SET eff_project=? WHERE machine_id=? AND session_id=? AND eff_project IS NOT ?", (pkey, *key, pkey))
    if touched is None:
        todo = {r["task_id"] for r in rows}
    else:
        todo = {r["task_id"] for r in rows if r["status"] == "running" or r["task_id"] not in old
               or old[r["task_id"]][0] != eff.EFFICIENCY_VERSION or _touched(r, touched)}
        for r in rows:  # a turn's subagent share moves with the subagents it started
            if r["kind"] == "turn" and r["task_id"] not in todo:
                hi = r["window_end"] if r["window_end"] is not None else INF
                if any(s["task_id"] in todo and r["started"] <= s["started"] < hi for s in subs):
                    todo.add(r["task_id"])
    for r in rows:
        tid = r["task_id"]
        if tid not in todo:
            continue
        before = old.get(tid)
        running = r["status"] == "running"
        # the peak or window the stored measures were made with has moved (e.g. the M48 window table): the stored
        # near_full flag and the frozen score no longer hold
        moved = bool(before) and (before[13], before[14]) != (r["peak_context"], r["context_window"])
        if (before and not moved and before[0] == eff.EFFICIENCY_VERSION and before[1] in eff.RATED_LABELS and not running
                and r["ended"] is not None and before[2] == r["ended"] and now - last_received > FREEZE_GRACE):
            continue  # a finished, rated task keeps its stored score
        sub_tokens = None
        if r["kind"] == "turn":
            hi = r["window_end"] if r["window_end"] is not None else INF
            sub_tokens = sum(tokens[s["task_id"]] for s in subs if r["started"] <= s["started"] < hi)
        calls_eff = task_effs.get(tid) or {}
        measures = eff.build_measures(r, calls_eff, usage[tid], sub_tokens, denied=denied.get(tid, 0))
        retries, waste, tools = measures["retries"], measures["waste"], list(measures["retry_tools"])
        if before and before[0] == eff.EFFICIENCY_VERSION:  # evicted calls must not lower what was stored
            retries = max(retries, int(before[3] or 0))
            kept = [w for w in _loads(before[4]) if not (moved and isinstance(w, dict) and w.get("kind") == "near_full")]
            waste = eff.merge_waste_lists(kept, waste)
            tools = sorted(set(tools) | {str(x) for x in _loads(before[5])})
        kind = eff.kind_of(r["kind"], r["agent_type"])
        label = (eff.IN_PROGRESS if running else
                 PENDING if r["calls"] >= eff.MIN_CALLS and r["ended"] is not None else None)
        if before and before[0] == eff.EFFICIENCY_VERSION and not running and before[1] != eff.IN_PROGRESS:
            same = (before[2], before[3], before[6], before[7], before[8], before[9], before[10], before[11], before[12], before[13], before[14]) == (
                r["ended"], retries, tokens[tid], r["calls"], r["errors"], measures["duration_s"], measures["cache_reuse"], kind,
                measures["subagent_share"], r["peak_context"], r["context_window"]) and _loads(before[4]) == waste
            if same:
                continue  # its own inputs did not change: keep the stored label and score
        db.execute(
            "UPDATE tasks SET retries=?, cache_reuse=?, subagent_share=?, waste=?, efficiency_score=NULL,"
            " efficiency_label=?, efficiency_reasons='[]', efficiency_kind=?, efficiency_version=?, eff_tokens=?,"
            " eff_calls=?, eff_errors=?, eff_duration=?, eff_retry_tools=?"
            " WHERE machine_id=? AND session_id=? AND task_id=?",
            (retries, measures["cache_reuse"], measures["subagent_share"], json.dumps(waste, ensure_ascii=False), label,
             kind, eff.EFFICIENCY_VERSION, tokens[tid], r["calls"], r["errors"], measures["duration_s"],
             json.dumps(tools), *key, tid))
        if label == eff.IN_PROGRESS:  # a running task is shown with its measures, scored never
            db.execute("UPDATE tasks SET efficiency_reasons=? WHERE machine_id=? AND session_id=? AND task_id=?",
                       (json.dumps(eff.evaluate({**measures, "waste": waste}, [], kind, True)["reasons"], ensure_ascii=False), *key, tid))


# ---- the scoring pass -------------------------------------------------------------------------------------------

_SCORE_COLS = ("machine_id, session_id, task_id, efficiency_kind, eff_project, eff_tokens, eff_calls, eff_errors,"
               " eff_duration, retries, cache_reuse, waste, eff_retry_tools, ended")


def _stored_measures(r: sqlite3.Row) -> dict:
    calls = r["eff_calls"] or 0
    return {
        "calls": calls, "errors": r["eff_errors"] or 0, "retries": r["retries"] or 0, "cache_reuse": r["cache_reuse"],
        "tokens_per_call": r["eff_tokens"] / calls if calls and (r["eff_tokens"] or 0) > 0 else None,
        "failure_rate": ((r["eff_errors"] or 0) + (r["retries"] or 0)) / calls if calls else None,
        "seconds_per_call": r["eff_duration"] / calls if calls and r["eff_duration"] is not None else None,
        "waste": [w for w in _loads(r["waste"]) if isinstance(w, dict)],
        "retry_tools": [str(x) for x in _loads(r["eff_retry_tools"])],
    }


KINDS_SQL = "SELECT DISTINCT efficiency_kind FROM tasks WHERE efficiency_label = ? AND efficiency_version = ?"  # covering index scan


def _barrier(svc: Any) -> Optional[float]:
    """The end time of the earliest task still waiting for a rebuild that will actually happen, or None.

    A row below the current version only holds the pass back while its session exists and has not already been
    replayed this run; a ghost row (no session) or a row a replay did not rewrite can never be rebuilt."""
    tried = getattr(svc, "_report_backfilled", ())
    for ended, m, sid in svc.db.execute(
        "SELECT ended, machine_id, session_id FROM tasks WHERE efficiency_version < ? AND ended IS NOT NULL ORDER BY ended",
        (eff.EFFICIENCY_VERSION,)):
        if (m, sid) in tried:
            continue
        if svc.db.execute("SELECT 1 FROM sessions WHERE machine_id=? AND session_id=?", (m, sid)).fetchone():
            return ended
    return None


def settle_orphans(svc: Any) -> int:
    """After a backfill run: rows below the current version that no replay will rewrite stop holding the pass back.

    They are marked current with no efficiency (never in a baseline: no kind, no stored inputs). Returns how many."""
    tried = getattr(svc, "_report_backfilled", ())
    db = svc.db
    ids = [r[0] for r in db.execute(
        "SELECT rowid, machine_id, session_id FROM tasks WHERE efficiency_version < ?", (eff.EFFICIENCY_VERSION,))
        if (r[1], r[2]) in tried or not db.execute("SELECT 1 FROM sessions WHERE machine_id=? AND session_id=?", (r[1], r[2])).fetchone()]
    for i in range(0, len(ids), 500):
        chunk = ids[i:i + 500]
        db.execute(
            "UPDATE tasks SET efficiency_version=?, efficiency_label=NULL, efficiency_score=NULL, efficiency_reasons='[]'"
            f" WHERE rowid IN ({','.join('?' * len(chunk))})", (eff.EFFICIENCY_VERSION, *chunk))
    if ids:
        log.warning("%d task rows could not be rebuilt for efficiency (no session or not rewritten by a replay); left unrated", len(ids))
    return len(ids)


def score_pass(svc: Any) -> int:
    """Score every task that is waiting for one, across all sessions, ordered by end time (13.10).

    A task is scored once no task that ended before it is still waiting for a rebuild (a version below the current
    one), so a rebuild gives the same scores whatever order its sessions were replayed in. The pass also scores,
    once, tasks stored as `not enough history` when enough earlier tasks now exist; each task is looked at once per
    pass, so tasks that stay `not enough history` cannot crowd out the rest. Runs inside the caller's transaction.
    Returns how many tasks it scored."""
    db = svc.db
    waiting = db.execute("SELECT 1 FROM tasks WHERE efficiency_label IN (?, ?) AND efficiency_version = ? LIMIT 1",
                         (PENDING, eff.NOT_ENOUGH, eff.EFFICIENCY_VERSION)).fetchone()
    if waiting is None:
        return 0  # nothing to score
    barrier = _barrier(svc)
    limit = INF if barrier is None else barrier
    seen: set = set()
    done = 0

    def score(r: sqlite3.Row) -> None:
        nonlocal done
        seen.add((r["machine_id"], r["session_id"], r["task_id"]))
        pool = _pool_for(db, r["efficiency_kind"], r["eff_project"], r["ended"])
        res = eff.evaluate(_stored_measures(r), pool, r["efficiency_kind"])
        db.execute(
            "UPDATE tasks SET efficiency_score=?, efficiency_label=?, efficiency_reasons=?"
            " WHERE machine_id=? AND session_id=? AND task_id=?",
            (res["score"], res["label"], json.dumps(res["reasons"], ensure_ascii=False), r["machine_id"], r["session_id"], r["task_id"]))
        done += 1

    while True:  # pending tasks leave the set once scored, so this ends
        batch = db.execute(
            f"SELECT {_SCORE_COLS} FROM tasks WHERE efficiency_label = ? AND efficiency_version = ? AND ended < ?"
            " ORDER BY ended, machine_id, session_id, task_id LIMIT ?", (PENDING, eff.EFFICIENCY_VERSION, limit, PASS_BATCH)).fetchall()
        batch = [r for r in batch if (r["machine_id"], r["session_id"], r["task_id"]) not in seen]
        if not batch:
            break
        for r in batch:
            score(r)
    # tasks without enough history can be scored from their kind's 20th task on
    for (kind,) in db.execute(KINDS_SQL, (eff.NOT_ENOUGH, eff.EFFICIENCY_VERSION)).fetchall():
        kth = db.execute(
            "SELECT t.ended FROM tasks AS t INDEXED BY tasks_eff_kind WHERE " + POOL_WHERE + " ORDER BY t.ended LIMIT 1 OFFSET ?",
            (eff.EFFICIENCY_VERSION, eff.MIN_CALLS, kind, eff.BASELINE_MIN - 1)).fetchone()
        if kth is None:
            continue
        after = (kth[0], "", "", "")
        while True:  # keyset paging: each task is read once
            batch = db.execute(
                f"SELECT {_SCORE_COLS} FROM tasks WHERE efficiency_label = ? AND efficiency_version = ? AND efficiency_kind = ?"
                " AND ended < ? AND (ended, machine_id, session_id, task_id) > (?, ?, ?, ?)"
                " ORDER BY ended, machine_id, session_id, task_id LIMIT ?",
                (eff.NOT_ENOUGH, eff.EFFICIENCY_VERSION, kind, limit, *after, PASS_BATCH)).fetchall()
            if not batch:
                break
            for r in batch:
                if (r["machine_id"], r["session_id"], r["task_id"]) not in seen:
                    score(r)
            last = batch[-1]
            after = (last["ended"], last["machine_id"], last["session_id"], last["task_id"])
    return done


# ---- reading ---------------------------------------------------------------------------------------------------

EFFICIENCY_COLS = ("t.retries, t.cache_reuse, t.subagent_share, t.waste, t.efficiency_score, t.efficiency_label,"
                   " t.efficiency_reasons, t.efficiency_kind")
RAW = ("retries", "cache_reuse", "subagent_share", "waste", "efficiency_score", "efficiency_label",
       "efficiency_reasons", "efficiency_kind")


def _json_list(text: Any) -> list:
    try:
        value = json.loads(text) if text else []
    except (ValueError, TypeError):
        return []
    return value if isinstance(value, list) else []


def decorate(r: dict) -> dict:
    """Replace the raw efficiency columns of a task row with its `efficiency` object (13.10)."""
    raw = {k: r.pop(k, None) for k in RAW}
    label = raw["efficiency_label"]
    if label == PENDING:  # measured, waiting for the scoring pass
        label = None
    if label is None and r.get("status") == "running":
        label = eff.IN_PROGRESS
    r["efficiency"] = {
        "score": raw["efficiency_score"], "label": label, "reasons": [str(x) for x in _json_list(raw["efficiency_reasons"])],
        "kind": raw["efficiency_kind"], "retries": raw["retries"], "cache_reuse": raw["cache_reuse"],
        "subagent_share": raw["subagent_share"], "waste": [w for w in _json_list(raw["waste"]) if isinstance(w, dict)],
    }
    return r


def filter_clause(values: List[str]) -> Tuple[str, list]:
    """SQL for the `efficiency` filter: rated labels match exactly, `unrated` is everything else."""
    parts = []
    named = [v for v in values if v in eff.RATED_LABELS]
    if named:
        parts.append(f"t.efficiency_label IN ({','.join('?' * len(named))})")
    if "unrated" in values:
        parts.append(f"(t.efficiency_label IS NULL OR t.efficiency_label NOT IN ({RATED_SQL}))")
    return "(" + " OR ".join(parts) + ")", named


def _day(t: float, tz: int) -> str:
    return datetime.fromtimestamp(t + tz * 60, timezone.utc).strftime("%Y-%m-%d")


class _Group:
    def __init__(self) -> None:
        self.n = {"tasks": 0, "efficient": 0, "typical": 0, "wasteful": 0, "unrated": 0}
        self.scores: List[float] = []

    def add(self, label: Optional[str], score: Optional[int]) -> None:
        self.n["tasks"] += 1
        if label in eff.RATED_LABELS:
            self.n[label] += 1
            if score is not None:
                self.scores.append(score)
        else:
            self.n["unrated"] += 1

    def out(self, **name: Any) -> dict:
        m = eff.median(self.scores)
        return {**name, **self.n, "median_score": None if m is None else round(m, 1)}


def overview(svc: Any, common: Any, task_join: str, start_from=None, start_to=None, machine=None, project=None,
             kind=None, tz: int = 0) -> dict:
    """Labels per day, kind and project, and the most common waste signals (13.10)."""
    where: List[str] = []
    args: list = []
    common("t", "started", start_from, start_to, machine, project, where, args)
    if kind:
        where.append("t.efficiency_kind = ?")
        args.append(kind)
    cond = " WHERE " + " AND ".join(where) if where else ""
    days: Dict[str, _Group] = {}
    kinds: Dict[str, _Group] = {}
    projects: Dict[str, _Group] = {}
    waste: Dict[str, int] = {}
    for r in svc.reader().execute(
        "SELECT t.started, t.efficiency_kind, COALESCE(s.project, ''), t.efficiency_label, t.efficiency_score, t.waste"
        f" {task_join}{cond}", args):
        started, k, proj, label, score, w = r
        days.setdefault(_day(started, tz), _Group()).add(label, score)
        kinds.setdefault(k or "unknown", _Group()).add(label, score)
        projects.setdefault(proj, _Group()).add(label, score)
        if w and w != "[]":
            for kind_name in {x.get("kind") for x in _json_list(w) if isinstance(x, dict)}:
                if isinstance(kind_name, str):
                    waste[kind_name] = waste.get(kind_name, 0) + 1

    def ranked(groups: Dict[str, _Group], name: str) -> List[dict]:
        order = sorted(groups.items(), key=lambda kv: (-kv[1].n["tasks"], kv[0]))[:OVERVIEW_GROUPS]
        return [g.out(**{name: k}) for k, g in order]

    return {
        "by_day": [days[d].out(day=d) for d in sorted(days)],
        "by_kind": ranked(kinds, "kind"),
        "by_project": ranked(projects, "project"),
        "top_waste": [{"kind": k, "count": n} for k, n in sorted(waste.items(), key=lambda kv: (-kv[1], kv[0]))],
    }


def baseline(svc: Any, kind: Optional[str] = None, project: Optional[str] = None, limit: int = eff.BASELINE_MAX) -> dict:
    """The baseline pools as ratio lists only, never text (13.10). Projects travel as short hashes, and the
    `project` filter is hashed the same way."""
    conn = svc.reader()
    limit = max(1, min(int(limit), BASELINE_LIMIT_MAX))
    where = ["t.efficiency_version = ?", "t.status = 'ok'", "t.eff_calls >= ?", "t.ended IS NOT NULL", "t.efficiency_kind IS NOT NULL"]
    args: list = [eff.EFFICIENCY_VERSION, eff.MIN_CALLS]
    if kind:
        where.append("t.efficiency_kind = ?")
        args.append(kind)
    if project is not None:
        where.append("t.eff_project = ?")
        args.append(eff.project_key(project))
    pools = []
    for k, p, _ in conn.execute(
        "SELECT t.efficiency_kind, t.eff_project, MAX(t.ended) AS last FROM tasks t WHERE "
        + " AND ".join(where) + " GROUP BY t.efficiency_kind, t.eff_project ORDER BY last DESC LIMIT ?",
        (*args, BASELINE_POOLS)).fetchall():
        pools.append({"kind": k, "project_key": p or "", "rows": pool_rows(conn, k, p or "", INF, limit)})
    return {"version": eff.BASELINE_VERSION, "pools": pools}

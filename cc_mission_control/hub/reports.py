"""Task and error report queries (SPEC 13.7): lists, summary, trend, per-session rows and CSV.

Every statement is parameterized; user text only ever reaches SQL as a bound LIKE pattern or value.
"""

from __future__ import annotations

import base64
import binascii
import csv
import io
import json
import sqlite3
from typing import Any, Dict, Iterable, List, Optional, Tuple

from ..describe import task_title
from . import effstore

DAY = 86400
PAGE = 50
PAGE_MAX = 200
CSV_MAX = 10_000
FAR = 1e18  # an open window (window_end NULL)
TASK_STATUSES = ("running", "ok", "error", "incomplete")
FAIL_KINDS = ("tool_error", "interrupted", "denied", "blocked", "api_error", "abandoned")
RESOLUTIONS = ("fixed", "worked_around", "unresolved", "pending", "n/a")
RATED = ("fixed", "worked_around", "unresolved")
TOP_N = 10
TASK_TOOLS_TOP = 8
TASK_KINDS = ("turn", "subagent")
ERROR_FILTERS = ("any", "unresolved")
EFFICIENCY_FILTERS = effstore.EFFICIENCY_FILTERS
REPEAT_MIN = 3  # a turn task repeats when this many turn tasks on its machine share its title (13.9)
REPEAT_FILTERS = ("only", "hide")
REPEAT_CHUNK = 300  # (machine, title) pairs per repeat-count query
FILES_PER_TASK = 200
OVERVIEW_TOOLS = 15
FACET_SESSIONS = 200
FACET_TOOLS = 200
BY_DAY_FILL_MAX = 1000
REASON_GROUP = 120
TREND_TYPES = 8
TREND_BUCKETS_MAX = 1000
TREND_DEFAULT_DAYS = 30
FORMULA_START = ("=", "+", "-", "@", "\t", "\r")
TASK_COLS = (
    "t.machine_id, t.session_id, t.task_id, t.agent_id, t.agent_type, t.kind, t.description, t.started, t.ended,"
    " t.window_end, t.status, t.outcome, t.calls, t.errors, t.fixed, t.unresolved, t.peak_context, t.context_window,"
    " t.title, " + effstore.EFFICIENCY_COLS + ", s.name AS session_name, COALESCE(s.project, '') AS project"
)
FAIL_COLS = (
    "f.machine_id, f.session_id, f.call_id, f.task_id, f.agent_id, f.agent_type, f.t, f.tool, f.description, f.kind,"
    " f.reason, f.resolution, f.fix_call_id, f.fix_description, f.fix_outcome, f.fix_t, f.attempts, f.changes,"
    " f.changes_total, s.name AS session_name, COALESCE(s.project, '') AS project, k.description AS task_description,"
    " k.title AS task_title"
)
TASK_JOIN = "FROM tasks t LEFT JOIN sessions s ON s.machine_id = t.machine_id AND s.session_id = t.session_id"
FAIL_JOIN = (
    "FROM failures f LEFT JOIN sessions s ON s.machine_id = f.machine_id AND s.session_id = f.session_id"
    " LEFT JOIN tasks k ON k.machine_id = f.machine_id AND k.session_id = f.session_id AND k.task_id = f.task_id"
)


class BadCursor(ValueError):
    pass


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def encode_cursor(t: float, machine_id: str, session_id: str, key: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([t, machine_id, session_id, key]).encode()).decode()


def decode_cursor(cursor: str) -> Tuple[float, str, str, str]:
    try:
        t, m, s, k = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if isinstance(t, (int, float)) and not isinstance(t, bool) and all(isinstance(x, str) for x in (m, s, k)):
            return float(t), m, s, k
    except (ValueError, binascii.Error, TypeError):
        pass
    raise BadCursor("invalid cursor")


def _row_dict(r: sqlite3.Row) -> dict:
    return {k: r[k] for k in r.keys()}


# ---- usage per task ------------------------------------------------------------------------------------------

def _task_usage(svc: Any, conn: sqlite3.Connection, r: dict) -> Optional[dict]:
    """The 13.6 usage totals of one task: agent_usage of its agent with started <= t < window_end, priced."""
    end = r["window_end"] if r["window_end"] is not None else FAR
    usage_rows = svc._usage_rows(
        conn, "1 AS k", "", "u.machine_id=? AND u.session_id=? AND u.agent_id=? AND u.t >= ? AND u.t < ?",
        [r["machine_id"], r["session_id"], r["agent_id"], r["started"], end])
    totals, _ = svc._price_rows(usage_rows, set())
    u = totals.get(1)
    return u.to_dict() if u else None


def context_pct(peak: Any, window: Any) -> Optional[int]:
    if isinstance(peak, (int, float)) and isinstance(window, (int, float)) and window > 0:
        return int(round(peak * 100.0 / window))
    return None


def _task_tools(conn: sqlite3.Connection, r: dict, limit: Optional[int] = None) -> List[dict]:
    sql = ("SELECT tool, calls, errors FROM task_tools WHERE machine_id=? AND session_id=? AND task_id=?"
           " ORDER BY calls DESC, tool" + (" LIMIT ?" if limit else ""))
    args = [r["machine_id"], r["session_id"], r["task_id"]] + ([limit] if limit else [])
    return [{"tool": x["tool"], "calls": x["calls"], "errors": x["errors"]} for x in conn.execute(sql, args)]


def _placeholder(title: Optional[str]) -> bool:
    """A placeholder title such as "(pasted text)" never repeats (13.9)."""
    return bool(title) and title.startswith("(") and title.endswith(")")


def _repeat_counts(conn: sqlite3.Connection, rows: List[dict]) -> Dict[Tuple[str, str], int]:
    """How many turn tasks share each (machine, title) of the turn rows, over all history (13.9).

    One grouped query per chunk of pairs, never one per row."""
    pairs = sorted({(r["machine_id"], r["title"]) for r in rows
                    if r.get("kind") == "turn" and r.get("title") and not _placeholder(r["title"])})
    out: Dict[Tuple[str, str], int] = {}
    for i in range(0, len(pairs), REPEAT_CHUNK):
        chunk = pairs[i:i + REPEAT_CHUNK]
        for x in conn.execute(
            "SELECT machine_id, title, COUNT(*) AS n FROM tasks WHERE kind = 'turn' AND (machine_id, title) IN (VALUES "
            + ",".join(["(?,?)"] * len(chunk)) + ") GROUP BY machine_id, title", [v for p in chunk for v in p]):
            out[(x["machine_id"], x["title"])] = x["n"]
    return out


def _decorate_tasks(svc: Any, conn: sqlite3.Connection, rows: List[dict]) -> List[dict]:
    """Add duration, label, tokens and estimated cost (13.6), context and the top tools (13.8), the title, detail
    and repeat count (13.9) to task rows."""
    counts = _repeat_counts(conn, rows)
    for r in rows:
        title, detail = task_title(r.get("description") or "")
        unfilled = r.get("title") is None
        if unfilled:  # NULL-title rows (before fill_titles runs) get a computed title but show repeats 0
            r["title"] = title
        r["detail"] = detail
        n = counts.get((r["machine_id"], r["title"]), 0) if r["kind"] == "turn" and not unfilled and not _placeholder(r["title"]) else 0
        r["repeats"] = n if n >= REPEAT_MIN else 0
        r["duration_s"] = round(r["ended"] - r["started"], 3) if r["ended"] is not None else None
        r["agent_label"] = "Main" if r["agent_id"] == "main" else (r["agent_type"] or "Subagent")
        d = _task_usage(svc, conn, r)
        r["tokens"] = (d["input"] + d["cache_creation"] + d["cache_read"] + d["output"]) if d else 0
        r["cost_usd"] = d["cost_usd"] if d else 0.0
        r["context_pct"] = context_pct(r.get("peak_context"), r.get("context_window"))
        r["tools"] = _task_tools(conn, r, TASK_TOOLS_TOP)
        effstore.decorate(r)
    return rows


def _decorate_failures(rows: List[dict]) -> List[dict]:
    for r in rows:
        try:
            r["changes"] = [str(x) for x in json.loads(r["changes"])]
        except (ValueError, TypeError):
            r["changes"] = []
    return rows


# ---- filters ---------------------------------------------------------------------------------------------------

def _common(alias: str, col: str, start_from, start_to, machine, project, where: List[str], args: list) -> None:
    if start_from is not None:
        where.append(f"{alias}.{col} >= ?")
        args.append(start_from)
    if start_to is not None:
        where.append(f"{alias}.{col} < ?")
        args.append(start_to)
    if machine:
        where.append(f"{alias}.machine_id = ?")
        args.append(machine)
    if project is not None:
        where.append("COALESCE(s.project, '') = ?")
        args.append(project)


def as_list(value: Any) -> List[str]:
    """A multi-value filter: None, one string, or a list of strings (any value matches)."""
    if value is None or value == "":
        return []
    return [value] if isinstance(value, str) else [v for v in value if isinstance(v, str) and v]


def _type_clause(alias: str, values: List[str], kind_col: str, id_col: str, main_sql: str) -> Tuple[str, list]:
    """agent_type multi filter: 'main' matches main turns, 'unknown' a NULL type, anything else an exact type."""
    parts, args = [], []  # type: List[str], list
    names = [v for v in values if v not in ("main", "unknown")]
    if "main" in values:
        parts.append(main_sql)
    if "unknown" in values:
        parts.append(f"({alias}.agent_type IS NULL AND {id_col} <> 'main')")
    if names:
        parts.append(f"{alias}.agent_type IN ({','.join('?' * len(names))})")
        args += names
    return "(" + " OR ".join(parts) + ")", args


def task_conditions(
    start_from=None, start_to=None, machine=None, project=None, agent_type=None, status=None, q=None,
    session=None, kind=None, agent_id=None, tool=None, file=None, errors=None, repeat=None, efficiency=None,
) -> Tuple[List[str], list]:
    """WHERE parts (over `tasks t` and `sessions s`) shared by the task list, the overview and the CSV (13.8)."""
    where: List[str] = []
    args: list = []
    _common("t", "started", start_from, start_to, machine, project, where, args)
    if session is not None:
        where.append("t.machine_id = ? AND t.session_id = ?")
        args += list(session)
        if agent_id:  # an agent id is only meaningful inside one session
            where.append("t.agent_id = ?")
            args.append(agent_id)
    types = as_list(agent_type)
    if types:
        clause, a = _type_clause("t", types, "t.kind", "t.agent_id", "t.kind = 'turn'")
        where.append(clause)
        args += a
    if kind:
        where.append("t.kind = ?")
        args.append(kind)
    statuses = as_list(status)
    if statuses:
        where.append(f"t.status IN ({','.join('?' * len(statuses))})")
        args += statuses
    tools = as_list(tool)
    if tools:
        where.append(
            "EXISTS (SELECT 1 FROM task_tools tx WHERE tx.machine_id = t.machine_id AND tx.session_id = t.session_id"
            f" AND tx.task_id = t.task_id AND tx.tool IN ({','.join('?' * len(tools))}))")
        args += tools
    if file:
        where.append(
            "EXISTS (SELECT 1 FROM file_access fx WHERE fx.machine_id = t.machine_id AND fx.session_id = t.session_id"
            " AND fx.task_id = t.task_id AND fx.path LIKE ? ESCAPE '\\')")
        args.append(_like(file))
    if errors == "any":
        where.append("t.errors > 0")
    elif errors == "unresolved":
        where.append("t.unresolved > 0")
    if repeat in REPEAT_FILTERS:  # a correlated count through tasks_title; a NULL title never repeats
        rep_sql = ("(SELECT COUNT(*) FROM tasks r WHERE r.machine_id = t.machine_id AND r.title = t.title"
                   f" AND t.title <> '' AND t.title NOT LIKE '(%)' AND r.kind = 'turn') >= {int(REPEAT_MIN)}")
        where.append(f"(t.kind = 'turn' AND {rep_sql})" if repeat == "only" else f"NOT (t.kind = 'turn' AND {rep_sql})")
    eff_values = as_list(efficiency)
    if eff_values:  # efficient | typical | wasteful | unrated (13.10)
        clause, a = effstore.filter_clause(eff_values)
        where.append(clause)
        args += a
    if q:
        where.append("(t.description LIKE ? ESCAPE '\\' OR t.outcome LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\')")
        args += [_like(q)] * 3
    return where, args


def list_tasks(
    svc: Any, start_from=None, start_to=None, machine=None, project=None, agent_type=None, status=None, q=None,
    session=None, kind=None, agent_id=None, tool=None, file=None, errors=None, repeat=None, efficiency=None,
    cursor=None, limit: int = PAGE, cap: int = PAGE_MAX,
) -> dict:
    where, args = task_conditions(start_from, start_to, machine, project, agent_type, status, q,
                                  session, kind, agent_id, tool, file, errors, repeat, efficiency)
    if cursor:
        where.append("(t.started, t.machine_id, t.session_id, t.task_id) < (?, ?, ?, ?)")
        args += list(decode_cursor(cursor))
    limit = max(1, min(limit, cap))
    conn = svc.reader()
    rows = [_row_dict(r) for r in conn.execute(
        f"SELECT {TASK_COLS} {TASK_JOIN}" + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY t.started DESC, t.machine_id DESC, t.session_id DESC, t.task_id DESC LIMIT ?", (*args, limit + 1))]
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        nxt = encode_cursor(last["started"], last["machine_id"], last["session_id"], last["task_id"])
    return {"tasks": _decorate_tasks(svc, conn, rows), "next_cursor": nxt}


def task_detail(svc: Any, machine_id: str, session_id: str, task_id: str) -> Optional[dict]:
    """One task: its row fields, every tool count, the files it touched (newest first) and its failures (13.8)."""
    conn = svc.reader()
    found = conn.execute(
        f"SELECT {TASK_COLS} {TASK_JOIN} WHERE t.machine_id=? AND t.session_id=? AND t.task_id=?",
        (machine_id, session_id, task_id)).fetchone()
    if found is None:
        return None
    row = _decorate_tasks(svc, conn, [_row_dict(found)])[0]
    row["tools"] = _task_tools(conn, row)
    usage = _task_usage(svc, conn, row)
    row["usage"] = usage
    key = (machine_id, session_id, task_id)
    row["files"] = [_row_dict(r) for r in conn.execute(
        "SELECT path, op, tool, status, t, call_id FROM file_access WHERE machine_id=? AND session_id=? AND task_id=?"
        " ORDER BY t DESC, call_id DESC LIMIT ?", (*key, FILES_PER_TASK))]
    row["failures"] = _decorate_failures([_row_dict(r) for r in conn.execute(
        f"SELECT {FAIL_COLS} {FAIL_JOIN} WHERE f.machine_id=? AND f.session_id=? AND f.task_id=? ORDER BY f.t, f.call_id",
        key)])
    return row


def list_failures(
    svc: Any, start_from=None, start_to=None, machine=None, project=None, agent_type=None, tool=None, kind=None,
    resolution=None, q=None, session=None, agent_id=None, cursor=None, limit: int = PAGE, cap: int = PAGE_MAX,
) -> dict:
    where: List[str] = []
    args: list = []
    _common("f", "t", start_from, start_to, machine, project, where, args)
    if session is not None:
        where.append("f.machine_id = ? AND f.session_id = ?")
        args += list(session)
        if agent_id:
            where.append("f.agent_id = ?")
            args.append(agent_id)
    types = as_list(agent_type)
    if types:
        clause, a = _type_clause("f", types, "", "f.agent_id", "f.agent_id = 'main'")
        where.append(clause)
        args += a
    # `kind` is a failure kind (13.7) and/or a task kind (turn|subagent, 13.8); values may be comma-separated
    kinds = as_list(kind)
    fail_kinds = [k for k in kinds if k in FAIL_KINDS]
    if fail_kinds:
        where.append(f"f.kind IN ({','.join('?' * len(fail_kinds))})")
        args += fail_kinds
    task_kinds = [k for k in kinds if k in TASK_KINDS]
    if task_kinds and len(task_kinds) < len(TASK_KINDS):
        where.append("f.agent_id = 'main'" if task_kinds[0] == "turn" else "f.agent_id <> 'main'")
    tools = as_list(tool)
    if tools:  # multi: any value matches
        where.append(f"f.tool IN ({','.join('?' * len(tools))})")
        args += tools
    if resolution:
        where.append("f.resolution = ?")
        args.append(resolution)
    if q:
        where.append("(f.reason LIKE ? ESCAPE '\\' OR f.description LIKE ? ESCAPE '\\' OR s.name LIKE ? ESCAPE '\\')")
        args += [_like(q)] * 3
    if cursor:
        where.append("(f.t, f.machine_id, f.session_id, f.call_id) < (?, ?, ?, ?)")
        args += list(decode_cursor(cursor))
    limit = max(1, min(limit, cap))
    rows = [_row_dict(r) for r in svc.reader().execute(
        f"SELECT {FAIL_COLS} {FAIL_JOIN}" + (" WHERE " + " AND ".join(where) if where else "")
        + " ORDER BY f.t DESC, f.machine_id DESC, f.session_id DESC, f.call_id DESC LIMIT ?", (*args, limit + 1))]
    nxt = None
    if len(rows) > limit:
        rows = rows[:limit]
        last = rows[-1]
        nxt = encode_cursor(last["t"], last["machine_id"], last["session_id"], last["call_id"])
    return {"failures": _decorate_failures(rows), "next_cursor": nxt}


def session_report(svc: Any, machine_id: str, session_id: str) -> Optional[dict]:
    conn = svc.reader()
    if not conn.execute("SELECT 1 FROM sessions WHERE machine_id=? AND session_id=?", (machine_id, session_id)).fetchone():
        return None
    key = (machine_id, session_id)
    tasks = [_row_dict(r) for r in conn.execute(
        f"SELECT {TASK_COLS} {TASK_JOIN} WHERE t.machine_id=? AND t.session_id=? ORDER BY t.started, t.task_id", key)]
    failures = [_row_dict(r) for r in conn.execute(
        f"SELECT {FAIL_COLS} {FAIL_JOIN} WHERE f.machine_id=? AND f.session_id=? ORDER BY f.t, f.call_id", key)]
    return {"tasks": _decorate_tasks(svc, conn, tasks), "failures": _decorate_failures(failures)}


# ---- summary and trend -----------------------------------------------------------------------------------------

def fixed_rate(fixed: int, worked_around: int, unresolved: int) -> Optional[float]:
    rated = fixed + worked_around + unresolved
    return round(fixed / rated, 4) if rated else None


def summary(svc: Any, start_from=None, start_to=None, machine=None, project=None) -> dict:
    conn = svc.reader()
    tw: List[str] = []
    targs: list = []
    _common("t", "started", start_from, start_to, machine, project, tw, targs)
    fw: List[str] = []
    fargs: list = []
    _common("f", "t", start_from, start_to, machine, project, fw, fargs)
    tcond = " WHERE " + " AND ".join(tw) if tw else ""
    fcond = " WHERE " + " AND ".join(fw) if fw else ""
    tasks = {s: 0 for s in TASK_STATUSES}
    for r in conn.execute(f"SELECT t.status AS k, COUNT(*) AS n {TASK_JOIN}{tcond} GROUP BY t.status", targs):
        tasks[r["k"]] = r["n"]
    tasks = {"total": sum(tasks.values()), **tasks}
    by_kind = {k: 0 for k in FAIL_KINDS}
    by_res = {k: 0 for k in RESOLUTIONS}
    for r in conn.execute(f"SELECT f.kind AS a, f.resolution AS b, COUNT(*) AS n {FAIL_JOIN}{fcond} GROUP BY f.kind, f.resolution", fargs):
        by_kind[r["a"]] = by_kind.get(r["a"], 0) + r["n"]
        by_res[r["b"]] = by_res.get(r["b"], 0) + r["n"]
    extra = (" AND " if fw else " WHERE ")
    top_tools = [{"tool": r["k"], "count": r["n"]} for r in conn.execute(
        f"SELECT f.tool AS k, COUNT(*) AS n {FAIL_JOIN}{fcond}{extra}f.tool <> '' GROUP BY f.tool"
        " ORDER BY n DESC, k LIMIT ?", (*fargs, TOP_N))]
    top_reasons = [{"reason": r["k"], "count": r["n"]} for r in conn.execute(
        f"SELECT substr(f.reason, 1, ?) AS k, COUNT(*) AS n {FAIL_JOIN}{fcond}{extra}f.reason <> ''"
        " GROUP BY k ORDER BY n DESC, k LIMIT ?", (REASON_GROUP, *fargs, TOP_N))]
    return {
        "tasks": tasks,
        "failures": {
            "total": sum(by_kind.values()), "by_kind": by_kind, "by_resolution": by_res,
            "fixed_rate": fixed_rate(by_res["fixed"], by_res["worked_around"], by_res["unresolved"]),
            "top_tools": top_tools, "top_reasons": top_reasons,
        },
    }


def bucket_start(t: float, bucket: str, tz: int) -> float:
    """Start (epoch seconds) of the day or Monday-based week containing t, with days cut at tz minutes east of UTC."""
    d = int((t + tz * 60) // DAY)
    if bucket == "week":
        d -= (d + 3) % 7  # 1970-01-01 was a Thursday
    return float(d * DAY - tz * 60)


def trend(svc: Any, start: float, end: float, bucket: str = "day", tz: int = 0, machine=None, project=None) -> dict:
    """Fixed rate per agent type per bucket over [start, end) on failures.t."""
    step = (7 if bucket == "week" else 1) * DAY
    first, last = bucket_start(start, bucket, tz), bucket_start(max(start, end - 1e-6), bucket, tz)
    n = int(round((last - first) / step)) + 1
    if n > TREND_BUCKETS_MAX:
        raise ValueError("range too long for this bucket")
    starts = [first + i * step for i in range(n)]
    where, args = ["f.t >= ?", "f.t < ?"], [start, end]  # type: List[str], list
    if machine:
        where.append("f.machine_id = ?")
        args.append(machine)
    join = "LEFT JOIN sessions s ON s.machine_id = f.machine_id AND s.session_id = f.session_id" if project is not None else ""
    if project is not None:
        where.append("COALESCE(s.project, '') = ?")
        args.append(project)
    day = "CAST((f.t + ? * 60) / 86400 AS INTEGER)"
    key = f"({day} - (({day} + 3) % 7))" if bucket == "week" else day
    targs = [tz, tz] if bucket == "week" else [tz]
    rows = svc.reader().execute(
        f"SELECT COALESCE(f.agent_type, 'unknown') AS atype, {key} AS b, COUNT(*) AS n,"
        " SUM(f.resolution = 'fixed') AS fx, SUM(f.resolution = 'worked_around') AS wa,"
        f" SUM(f.resolution = 'unresolved') AS un FROM failures f {join} WHERE {' AND '.join(where)} GROUP BY atype, b",
        (*targs, *args)).fetchall()
    totals: Dict[str, int] = {}
    for r in rows:
        totals[r["atype"]] = totals.get(r["atype"], 0) + r["n"]
    ranked = sorted(totals, key=lambda a: (-totals[a], a))
    keep = set(ranked[:TREND_TYPES])
    cells: Dict[str, Dict[float, List[int]]] = {}
    for r in rows:
        name = r["atype"] if r["atype"] in keep else "other"
        start_b = r["b"] * DAY - tz * 60
        c = cells.setdefault(name, {}).setdefault(float(start_b), [0, 0, 0, 0])
        for i, v in enumerate((r["n"], r["fx"], r["wa"], r["un"])):
            c[i] += v or 0
    series = []
    for name, by_start in cells.items():
        points = []
        for s in starts:
            n_, fx, wa, un = by_start.get(s, [0, 0, 0, 0])
            points.append({"start": s, "failures": n_, "fixed": fx, "worked_around": wa, "unresolved": un,
                           "fixed_rate": fixed_rate(fx, wa, un)})
        sums = [sum(p[k] for p in points) for k in ("failures", "fixed", "worked_around", "unresolved")]
        series.append({"agent_type": name, "failures": sums[0], "fixed_rate": fixed_rate(*sums[1:]), "points": points})
    series.sort(key=lambda s: (-s["failures"], s["agent_type"]))
    return {"bucket": bucket, "buckets": starts, "series": series}


# ---- overview and facets (13.8) ---------------------------------------------------------------------------------

def _pct(peak: Any, window: Any) -> Optional[float]:
    if isinstance(peak, (int, float)) and isinstance(window, (int, float)) and window > 0:
        return peak * 100.0 / window
    return None


def _round1(value: Optional[float]) -> Optional[float]:
    return None if value is None else round(value, 1)


def overview(svc: Any, **filters: Any) -> dict:
    """Summary tiles and breakdowns over the tasks that match the shared filters (13.8).

    Tokens and cost are priced per task by the same grouping as the task rows (a task's agent_usage with
    started <= t < window_end, grouped by model, speed, geo and over-long flags) and then summed, so the
    overview always equals the sum of the rows."""
    tz = filters.pop("tz", 0)
    where, args = task_conditions(**filters)
    cond = " WHERE " + " AND ".join(where) if where else ""
    conn = svc.reader()
    light = conn.execute(
        "SELECT t.rowid AS id, t.machine_id, t.session_id, t.task_id, t.description, t.title, t.started, t.agent_type, t.kind,"
        f" t.status, t.peak_context, t.context_window {TASK_JOIN}{cond}", args).fetchall()

    usage_rows = svc._usage_rows(
        conn, "t.rowid AS k", "", f"{' AND '.join(where) + ' AND ' if where else ''}u.t >= t.started AND u.t < COALESCE(t.window_end, ?)",
        [*args, FAR],
        source=("tasks t LEFT JOIN sessions s ON s.machine_id = t.machine_id AND s.session_id = t.session_id"
                " JOIN agent_usage u ON u.machine_id = t.machine_id AND u.session_id = t.session_id AND u.agent_id = t.agent_id"))
    unpriced: set = set()
    totals, _ = svc._price_rows(usage_rows, unpriced)
    per_task = {k: v.to_dict() for k, v in totals.items()}

    counts = {"total": 0, "turns": 0, "subagents": 0, "ok": 0, "error": 0, "incomplete": 0, "running": 0}
    tokens = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0}
    cost = 0.0
    pcts: List[float] = []
    best = None
    types: Dict[str, dict] = {}
    days: Dict[float, List[float]] = {}
    for r in light:
        counts["total"] += 1
        counts["turns" if r["kind"] == "turn" else "subagents"] += 1
        if r["status"] in counts:
            counts[r["status"]] += 1
        d = per_task.get(r["id"])
        task_cost = d["cost_usd"] if d else 0.0
        if d:
            for k in tokens:
                tokens[k] += d[k]
            cost += task_cost
        pct = _pct(r["peak_context"], r["context_window"])
        if pct is not None:
            pcts.append(pct)
            if best is None or pct > best[0]:
                best = (pct, r)
        name = r["agent_type"] or "unknown"
        g = types.setdefault(name, {"agent_type": name, "tasks": 0, "cost_usd": 0.0, "_p": []})
        g["tasks"] += 1
        g["cost_usd"] += task_cost
        if pct is not None:
            g["_p"].append(pct)
        day = days.setdefault(bucket_start(r["started"], "day", tz), [0, 0.0])
        day[0] += 1
        day[1] += task_cost

    fx = conn.execute(
        "SELECT COUNT(*) AS n, COALESCE(SUM(f.resolution = 'fixed'), 0) AS fx,"
        " COALESCE(SUM(f.resolution = 'worked_around'), 0) AS wa, COALESCE(SUM(f.resolution = 'unresolved'), 0) AS un"
        f" {TASK_JOIN} JOIN failures f ON f.machine_id = t.machine_id AND f.session_id = t.session_id"
        f" AND f.task_id = t.task_id{cond}", args).fetchone()
    by_tool = [{"tool": r["tool"], "tasks": r["n"], "calls": r["calls"], "errors": r["errors"]} for r in conn.execute(
        "SELECT x.tool AS tool, COUNT(*) AS n, SUM(x.calls) AS calls, SUM(x.errors) AS errors"
        f" {TASK_JOIN} JOIN task_tools x ON x.machine_id = t.machine_id AND x.session_id = t.session_id"
        f" AND x.task_id = t.task_id{cond} GROUP BY x.tool ORDER BY SUM(x.calls) DESC, x.tool LIMIT ?",
        (*args, OVERVIEW_TOOLS))]

    by_type = [{"agent_type": g["agent_type"], "tasks": g["tasks"], "cost_usd": round(g["cost_usd"], 6),
                "avg_peak_pct": _round1(sum(g["_p"]) / len(g["_p"])) if g["_p"] else None} for g in types.values()]
    by_type.sort(key=lambda g: (-g["cost_usd"], -g["tasks"], g["agent_type"]))

    series = sorted(days)
    by_day = [{"start": s0, "tasks": days[s0][0], "cost_usd": round(days[s0][1], 6)} for s0 in series]
    if series:  # empty days inside the range are included, so the chart has no holes
        first, last = series[0], series[-1]
        lo, hi = filters.get("start_from"), filters.get("start_to")
        if lo is not None:
            first = min(first, bucket_start(lo, "day", tz))
        if hi is not None:
            last = max(last, bucket_start(max(lo or 0.0, hi - 1e-6), "day", tz))
        n = int(round((last - first) / DAY)) + 1
        if n <= BY_DAY_FILL_MAX:
            by_day = [{"start": first + i * DAY, "tasks": days.get(first + i * DAY, [0, 0.0])[0],
                       "cost_usd": round(days.get(first + i * DAY, [0, 0.0])[1], 6)} for i in range(n)]

    fixed, wa, un = fx["fx"], fx["wa"], fx["un"]
    return {
        "tasks": counts,
        "errors": {"total": fx["n"], "fixed": fixed, "unresolved": un, "fixed_rate": fixed_rate(fixed, wa, un)},
        "tokens": {**tokens, "total": sum(tokens.values())},
        "cost_usd": round(cost, 6), "unpriced_models": sorted(unpriced),
        "context": {
            "avg_peak_pct": _round1(sum(pcts) / len(pcts)) if pcts else None,
            "max_peak_pct": _round1(best[0]) if best else None,
            "max_task": {"machine_id": best[1]["machine_id"], "session_id": best[1]["session_id"],
                         "task_id": best[1]["task_id"], "description": best[1]["description"],
                         "title": best[1]["title"] if best[1]["title"] is not None else task_title(best[1]["description"])[0]} if best else None,
        },
        "by_agent_type": by_type, "by_tool": by_tool, "by_day": by_day,
    }


def facets(svc: Any, start_from=None, start_to=None, machine=None, project=None) -> dict:
    """Values for the filter pickers, over the same time, machine and project filters (13.8)."""
    where: List[str] = []
    args: list = []
    _common("t", "started", start_from, start_to, machine, project, where, args)
    cond = " WHERE " + " AND ".join(where) if where else ""
    conn = svc.reader()
    machines = [r[0] for r in conn.execute(f"SELECT DISTINCT t.machine_id {TASK_JOIN}{cond} ORDER BY 1", args)]
    projects = [r[0] for r in conn.execute(f"SELECT DISTINCT COALESCE(s.project, '') {TASK_JOIN}{cond} ORDER BY 1", args)]
    agent_types = [r[0] for r in conn.execute(
        f"SELECT DISTINCT CASE WHEN t.kind = 'turn' THEN 'main' ELSE COALESCE(t.agent_type, 'unknown') END {TASK_JOIN}{cond} ORDER BY 1", args)]
    tools = [r[0] for r in conn.execute(
        "SELECT x.tool FROM tasks t LEFT JOIN sessions s ON s.machine_id = t.machine_id AND s.session_id = t.session_id"
        " JOIN task_tools x ON x.machine_id = t.machine_id AND x.session_id = t.session_id AND x.task_id = t.task_id"
        f"{cond} GROUP BY x.tool ORDER BY SUM(x.calls) DESC, x.tool LIMIT ?", (*args, FACET_TOOLS))]
    sw: List[str] = []
    sargs: list = []
    if start_from is not None:  # sessions that overlap the range
        sw.append("s.last_event >= ?")
        sargs.append(start_from)
    if start_to is not None:
        sw.append("s.started < ?")
        sargs.append(start_to)
    if machine:
        sw.append("s.machine_id = ?")
        sargs.append(machine)
    if project is not None:
        sw.append("s.project = ?")
        sargs.append(project)
    sessions = [{"machine_id": r["machine_id"], "session_id": r["session_id"], "name": r["name"],
                 "project": r["project"], "started": r["started"]} for r in conn.execute(
        "SELECT s.machine_id, s.session_id, s.name, s.project, s.started FROM sessions s"
        + (" WHERE " + " AND ".join(sw) if sw else "") + " ORDER BY s.started DESC LIMIT ?", (*sargs, FACET_SESSIONS))]
    return {"machines": machines, "projects": projects, "agent_types": agent_types, "tools": tools, "sessions": sessions}


# ---- CSV -------------------------------------------------------------------------------------------------------

TASK_CSV = ("started", "ended", "duration_s", "session_name", "project", "machine_id", "session_id", "task_id", "agent",
            "agent_type", "kind", "title", "repeats", "description", "status", "outcome", "calls", "errors", "fixed", "unresolved",
            "tokens", "cost_usd", "peak_context", "context_pct", "tools")
FAIL_CSV = ("t", "session_name", "project", "machine_id", "session_id", "call_id", "task_id", "agent", "agent_type",
            "tool", "description", "kind", "reason", "resolution", "attempts", "changes", "fix_call_id",
            "fix_description", "fix_outcome", "fix_t", "task_description")


def _cell(value: Any) -> Any:
    if isinstance(value, str) and value.startswith(FORMULA_START):
        return "'" + value
    return "" if value is None else value


def to_csv(columns: Iterable[str], rows: List[dict]) -> str:
    out = io.StringIO()
    w = csv.writer(out, lineterminator="\r\n")
    cols = list(columns)
    w.writerow(cols)
    for r in rows:
        w.writerow([_cell(r.get(c)) for c in cols])
    return out.getvalue()


def tasks_csv(svc: Any, **filters: Any) -> Tuple[str, bool]:
    page = list_tasks(svc, limit=CSV_MAX, cap=CSV_MAX, **filters)
    rows = page["tasks"]
    for r in rows:
        r["agent"] = r["agent_label"]
        r["tools"] = ";".join(f"{x['tool']}\u00d7{x['calls']}" for x in r["tools"])
    return to_csv(TASK_CSV, rows), page["next_cursor"] is not None


def failures_csv(svc: Any, **filters: Any) -> Tuple[str, bool]:
    page = list_failures(svc, limit=CSV_MAX, cap=CSV_MAX, **filters)
    rows = page["failures"]
    for r in rows:
        r["agent"] = "Main" if r["agent_id"] == "main" else (r["agent_type"] or "Subagent")
        r["changes"] = "; ".join(r["changes"])
    return to_csv(FAIL_CSV, rows), page["next_cursor"] is not None

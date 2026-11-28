"""Hub storage service: ingest, derived indexes, replay, queries, retention, backups.

Design: one write connection behind `write_lock`; reads use a per-thread read-only
connection (WAL lets them run beside a writer) and never take the write lock. Ingests are
serialized by `ingest_lock`: records commit first, then each touched session's replay is
advanced (or rebuilt from committed rows, outside the write lock) and its derived rows are
upserted in a short second transaction. Derived rows are only ever upserted, never deleted,
so they survive Store call eviction. `cache_lock` guards the replay cache and the replayed
Stores. Because the summary always comes from a replay, it equals a full replay by construction.
"""

from __future__ import annotations

import base64
import binascii
import contextlib
import json
import logging
import os
import sqlite3
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Tuple

from .. import __version__
from ..config import Config, HubConfig
from ..describe import task_title
from ..pricing import COUNT_FIELDS, UsageTotals, empty_usage, load_pricing
from ..redact import Redactor, load_patterns
from . import HUB_APP_ID
from .db import db_size, open_db
from .ingest import Record, parse_envelope, usage_row
from ..state import MAIN, _norm_agent
from . import effstore, reports
from .. import efficiency
from .reports import BadCursor
from .replay import ReplayCache, ReplayState, replay_config

log = logging.getLogger(__name__)
PAGE = 50
PAGE_MAX = 200
FILES_MAX = 1000
STORED_FIELD_CHARS = 65_536  # defense-in-depth redaction must not re-truncate relay-truncated text
DAY = 86400
USAGE_GROUPS = ("agent_type", "model", "project", "machine", "session", "day")
USAGE_GROUP_MAX = 500
USAGE_DEFAULT_DAYS = 7
USAGE_SKEW = 300.0  # added to an omitted `to`
BACKFILL_SESSIONS = 50
REPORT_VERSION = 5  # bump to make the backfill rebuild task rows written by older code (no schema change); 4: titles, 2000-char descriptions (13.9); 5: window table, held subagent usage (M48)
TITLE_BATCH = 500
TS_PAST = 10 * 365 * DAY  # a message time older than this is not believed
BACKFILL_YIELD = 0.025  # between steps the ingest lock is free this long, so a waiting ingest gets in
BACKFILL_STEP = 2.0  # unused since M48 (a rebuild no longer holds the ingest lock for a step); kept for the tests that patch it
BACKFILL_SECONDS = 5.0  # one backfill run stops after about this long
REBUILD_CHUNK_SECONDS = 0.25  # a rebuild's replay breathes (sleeps BACKFILL_YIELD) this often, so ingest threads get the CPU
REBUILD_TRIES = 3  # optimistic rebuilds of one session before it is skipped for this run
REBUILD_ROWS = 500  # records fetched per statement during a replay
BACKFILL_AGE = 600.0  # only rows this old: a live session's agents may not be known yet
MAX_PATH_KEY = "\U0010ffff"
SESSION_COLS = (
    "machine_id, session_id, user, host, name, name_source, project, cwd, model, status, started, ended, "
    "last_event, agents, tool_calls, errors, compactions, tokens_in, tokens_cache_create, tokens_cache_read, "
    "tokens_out, peak_context"
)
ROW_ERRORS = (sqlite3.Error, OverflowError, ValueError, TypeError, RecursionError)


def _like(text: str) -> str:
    return "%" + text.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_") + "%"


def encode_cursor(started: float, machine_id: str, session_id: str) -> str:
    return base64.urlsafe_b64encode(json.dumps([started, machine_id, session_id]).encode()).decode()


def decode_cursor(cursor: str) -> Tuple[float, str, str]:
    try:
        started, machine_id, session_id = json.loads(base64.urlsafe_b64decode(cursor.encode()))
        if isinstance(started, (int, float)) and isinstance(machine_id, str) and isinstance(session_id, str):
            return float(started), machine_id, session_id
    except (ValueError, binascii.Error, TypeError):
        pass
    raise BadCursor("invalid cursor")


class HubService:
    def __init__(
        self,
        cfg: HubConfig,
        store_config: Optional[Config] = None,
        redactor: Optional[Redactor] = None,
        clock=time.time,
    ) -> None:
        self.cfg = cfg
        self.clock = clock
        base = store_config or Config.from_env()
        self.store_config = replay_config(base)
        self.pricing = load_pricing(cfg.pricing_file)
        self._report_backfilled: set = set()  # sessions rebuilt for the task report this run
        self._report_skipped: set = set()  # sessions too busy to rebuild in the current upgrade pass
        self._backfilled: set = set()  # sessions rebuilt for agent_type this run; unresolvable ones are not retried
        self.redactor = redactor or Redactor(load_patterns(base.redact_file), STORED_FIELD_CHARS)
        self.write_lock = threading.RLock()
        self.lock = self.write_lock  # the lock around self.db
        self.ingest_lock = threading.Lock()
        self.cache_lock = threading.RLock()
        self.cache = ReplayCache()
        self.db = open_db(cfg.db)
        self._local = threading.local()
        self._readers: List[sqlite3.Connection] = []
        self._records = self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]

    def close(self) -> None:
        with self.write_lock:
            self.db.close()
        for conn in self._readers:
            with contextlib.suppress(sqlite3.Error):
                conn.close()

    def reader(self) -> sqlite3.Connection:
        """This thread's read-only connection."""
        conn = getattr(self._local, "conn", None)
        if conn is None:
            conn = sqlite3.connect(str(self.cfg.db), check_same_thread=False, isolation_level=None)
            conn.row_factory = sqlite3.Row
            conn.execute("PRAGMA query_only=ON")
            self._local.conn = conn
            self._readers.append(conn)
        return conn

    def _rollback(self) -> None:
        try:
            self.db.execute("ROLLBACK")
        except sqlite3.Error:  # never mask the error that got us here
            pass

    # ---- ingest -----------------------------------------------------------

    def parse(self, items: Iterable[Any]) -> Tuple[List[Record], int]:
        records, invalid = [], 0
        for item in items:
            try:
                rec = parse_envelope(item, self.redactor)
            except (OverflowError, RecursionError, ValueError, TypeError):
                rec = None
            if rec is None:
                invalid += 1
            else:
                records.append(rec)
        return records, invalid

    def ingest(self, records: List[Record]) -> Tuple[int, int, int]:
        """Insert records, one savepoint each. Returns (accepted, duplicates, invalid)."""
        accepted = duplicates = invalid = 0
        fresh: Dict[Tuple[str, str], list] = {}
        with self.ingest_lock:
            with self.write_lock:
                received = self.clock()
                self.db.execute("BEGIN")
                try:
                    for rec in records:
                        self.db.execute("SAVEPOINT rec")
                        try:
                            cur = self.db.execute(
                                "INSERT OR IGNORE INTO records(eid, machine_id, user, host, session_id, kind, t,"
                                " received, data) VALUES (?,?,?,?,?,?,?,?,?)",
                                (rec.eid, rec.machine_id, rec.user, rec.host, rec.session_id, rec.kind, rec.t,
                                 received, json.dumps(rec.data, ensure_ascii=False, allow_nan=False)),
                            )
                            if cur.rowcount == 1 and rec.kind == "usage":
                                self._upsert_usage(rec)
                            self.db.execute("RELEASE rec")
                        except ROW_ERRORS:  # one bad row must not reject the batch
                            self.db.execute("ROLLBACK TO rec")
                            self.db.execute("RELEASE rec")
                            invalid += 1
                            continue
                        if cur.rowcount != 1:
                            duplicates += 1
                            continue
                        accepted += 1
                        fresh.setdefault((rec.machine_id, rec.session_id), []).append(
                            ((rec.t, received, cur.lastrowid), rec))
                    self.db.execute("COMMIT")
                except BaseException:
                    self._rollback()
                    raise
            self._records += accepted
            for key, items in fresh.items():
                try:
                    self._refresh(key, items)
                except Exception:  # records are safe; the summary catches up on the next ingest
                    log.exception("could not refresh derived rows for %s", key)
                    with self.cache_lock:
                        self.cache.drop(key)
        return accepted, duplicates, invalid

    def _message_time(self, rec: Record, row: dict) -> float:
        """The message's own time (`ts`) when plausible, else the envelope time (13.6)."""
        ts, now = row.get("ts"), self.clock()
        return ts if ts is not None and now - TS_PAST <= ts <= now + DAY else rec.t

    def _upsert_usage(self, rec: Record) -> None:
        row = usage_row(rec)
        if row is None:
            return
        t = self._message_time(rec, row)
        # Counts take the highest value and t the lowest, so totals do not depend on arrival order. The
        # cache writes follow the merge rule (13.6): the report with the larger total supplies both 5m and
        # 1h, a real split wins a tie. Session and agent are those of the earliest report (a message copied
        # into a forked session counts once).
        new_wins = ("(excluded.cache_creation > cache_creation OR"
                    " (excluded.cache_creation = cache_creation AND excluded.split > split))")
        self.db.execute(
            "INSERT INTO agent_usage(machine_id, session_id, agent_id, agent_type, message_id, model, input,"
            " cache_creation, cache_creation_5m, cache_creation_1h, cache_read, output, web_search, web_fetch,"
            " speed, inference_geo, split, t) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(machine_id, message_id) DO UPDATE SET"
            " session_id=CASE WHEN excluded.t < t THEN excluded.session_id ELSE session_id END,"
            " agent_type=CASE WHEN excluded.t < t THEN COALESCE(excluded.agent_type,"
            "   CASE WHEN agent_id = excluded.agent_id THEN agent_type END)"
            "   ELSE COALESCE(agent_type, CASE WHEN agent_id = excluded.agent_id THEN excluded.agent_type END) END,"
            " agent_id=CASE WHEN excluded.t < t THEN excluded.agent_id ELSE agent_id END,"
            " input=MAX(input, excluded.input),"
            f" cache_creation_5m=CASE WHEN {new_wins} THEN excluded.cache_creation_5m ELSE cache_creation_5m END,"
            f" cache_creation_1h=CASE WHEN {new_wins} THEN excluded.cache_creation_1h ELSE cache_creation_1h END,"
            f" split=CASE WHEN {new_wins} THEN excluded.split ELSE split END,"
            " cache_creation=MAX(cache_creation, excluded.cache_creation),"
            " cache_read=MAX(cache_read, excluded.cache_read), output=MAX(output, excluded.output),"
            " web_search=MAX(web_search, excluded.web_search), web_fetch=MAX(web_fetch, excluded.web_fetch),"
            " model=COALESCE(model, excluded.model), speed=COALESCE(speed, excluded.speed),"
            " inference_geo=COALESCE(inference_geo, excluded.inference_geo), t=MIN(t, excluded.t)",
            (rec.machine_id, rec.session_id, row["agent_id"], row["agent_type"], row["message_id"], row["model"],
             row["input"], row["cache_creation"], row["cache_creation_5m"], row["cache_creation_1h"],
             row["cache_read"], row["output"], row["web_search"], row["web_fetch"], row["speed"],
             row["inference_geo"], 1 if row["split"] else 0, t),
        )

    def _refresh(self, key: Tuple[str, str], items: list) -> None:
        items.sort(key=lambda it: it[0])
        snap = None
        advanced = False
        with self.cache_lock:
            state = self.cache.get(key)
            if state is not None and state.last_key is not None and items[0][0] > state.last_key:
                try:
                    for k, rec in items:
                        state.apply(rec.kind, rec.t, rec.data, rec.user, rec.host, k)
                except BaseException:
                    self.cache.drop(key)
                    raise
                snap = self._snapshot(key, state)
                advanced = True
        if not advanced:  # cold or out-of-order: replay committed rows without holding any lock
            state = self._rebuild(key, self.reader())
            with self.cache_lock:
                self.cache.put(key, state)
                snap = self._snapshot(key, state)
        if snap is not None:
            snap["received"] = max(k[1] for k, _ in items)
            snap["touched"] = self._touched(items)
            self._write_derived(key, snap)

    def _touched(self, items: list) -> list:
        """(agent id, time) of each record in a batch: the tasks whose window and agent got one are recomputed."""
        out = []
        for _, rec in items:
            agent = _norm_agent(rec.data.get("agent_id") if isinstance(rec.data, dict) else None)[:128] or MAIN
            t = rec.t
            if rec.kind == "usage":
                row = usage_row(rec)
                if row is not None:
                    t = self._message_time(rec, row)
            out.append((agent, t))
        return out

    def _rebuild(self, key: Tuple[str, str], conn: sqlite3.Connection, cut: Optional[int] = None,
                 breathe: bool = False) -> ReplayState:
        """Replay a session's committed records. `cut` limits it to rowids up to that one, and `breathe` sleeps now
        and then (the backfill: it holds no lock, and other threads need the CPU)."""
        state = ReplayState(key[1], self.store_config, self.pricing)
        # The replay order is (t, received, rowid), which rowid ranges cannot give. So fetch the ordered ids first
        # (fully, one short statement), then the rows in bounded chunks. Every statement is closed before a sleep,
        # so no read transaction stays open and the WAL can checkpoint.
        ids = [r[0] for r in conn.execute(
            "SELECT rowid FROM records WHERE machine_id=? AND session_id=?" + ("" if cut is None else " AND rowid<=?") +
            " ORDER BY t, received, rowid", key if cut is None else (*key, cut)).fetchall()]
        mark = time.monotonic()
        for i in range(0, len(ids), REBUILD_ROWS):
            chunk = ids[i:i + REBUILD_ROWS]
            got = {r["rowid"]: r for r in conn.execute(
                "SELECT kind, user, host, t, received, rowid, data FROM records WHERE rowid IN (%s)"
                % ",".join("?" * len(chunk)), chunk).fetchall()}
            for rid in chunk:
                r = got.get(rid)  # a row removed by retention meanwhile is simply gone
                if r is not None:
                    state.apply(r["kind"], r["t"], json.loads(r["data"]), r["user"], r["host"], (r["t"], r["received"], r["rowid"]))
            if breathe and time.monotonic() - mark >= REBUILD_CHUNK_SECONDS:
                time.sleep(BACKFILL_YIELD)
                mark = time.monotonic()
        return state

    def _rebuild_session(self, key: Tuple[str, str], score: bool = True) -> bool:
        """Rebuild one session's derived rows without holding the ingest lock during the replay (13.8).

        The replay reads committed records up to a cut (the highest rowid). Under the ingest lock it then checks
        that no record has arrived for the session since; if one has, it replays again (an ingest that ran meanwhile
        already wrote from its own full state). After REBUILD_TRIES misses the session is skipped, never replayed
        under the lock: a session that busy is kept fresh by ingest's own refresh, and the next maintenance run
        picks it up if it is still stale. The lock is held only to compare and to write.
        Returns False when the session was skipped."""
        conn = self.reader()
        maxrow = "SELECT MAX(rowid) FROM records WHERE machine_id=? AND session_id=?"
        for _ in range(REBUILD_TRIES):
            cut = conn.execute(maxrow, key).fetchone()[0]
            if cut is None:
                return True
            snap = self._snapshot(key, self._rebuild(key, conn, cut, breathe=True))
            with self.ingest_lock:
                if conn.execute(maxrow, key).fetchone()[0] == cut:
                    if snap is not None:
                        self._write_derived(key, snap, score)
                    return True
        return False

    @staticmethod
    def _snapshot(key: Tuple[str, str], state: ReplayState) -> Optional[dict]:
        sd = state.store.session_dict(key[1])
        if sd is None:  # records but no hook yet: nothing to summarize
            return None
        tasks, failures = state.report()
        call_task = {cid: x["task_id"] for x in tasks for cid in x.get("call_ids", ())}
        return {"sd": sd, "files": state.file_rows(call_task), "perms": list(state.permissions), "user": state.user,
                "tasks": tasks, "failures": failures, "kept": state.call_ids(),
                "host": state.host, "peak": state.peak_context, "prompts": "\n".join(state.prompts),
                "project": sd["title"]}

    def _write_derived(self, key: Tuple[str, str], snap: dict, score: bool = True) -> None:
        m, sid = key
        sd = snap["sd"]
        agents = sd["agents"]
        with self.write_lock:
            self.db.execute("BEGIN")
            try:
                tok = self.db.execute(
                    "SELECT COALESCE(SUM(input),0) i, COALESCE(SUM(cache_creation),0) cc,"
                    " COALESCE(SUM(cache_read),0) cr, COALESCE(SUM(output),0) o"
                    " FROM agent_usage WHERE machine_id=? AND session_id=?", key).fetchone()
                # agent types the hub only learns from hook/meta records, never from the usage records
                self.db.executemany(
                    "UPDATE agent_usage SET agent_type=? WHERE machine_id=? AND session_id=? AND agent_id=?"
                    " AND (agent_type IS NULL OR agent_type = 'unknown')",
                    [(MAIN if a["id"] == MAIN else a["agent_type"], m, sid, a["id"])
                     for a in agents if a["id"] == MAIN or a["agent_type"]],
                )
                # upserts only: rows for calls the Store has since evicted must stay
                self.db.executemany(
                    "INSERT OR REPLACE INTO file_access(machine_id, session_id, agent_id, call_id, path, op, tool,"
                    " status, t, task_id) VALUES (?,?,?,?,?,?,?,?,?,?)",
                    [(m, sid, *row) for row in snap["files"]],
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO permissions(machine_id, session_id, agent_id, call_id, tool, kind, text, t)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(m, sid, *row) for row in snap["perms"]],
                )
                received = snap.get("received")
                if received is None:  # a rebuild: when the session's last record reached the hub
                    received = self.db.execute("SELECT MAX(received) FROM records WHERE machine_id=? AND session_id=?", key).fetchone()[0] or 0.0
                self._write_report(m, sid, snap["tasks"], snap["failures"], snap["kept"], snap["project"], received, snap.get("touched"))
                if score:  # a backfill scores once at its end instead
                    effstore.score_pass(self)  # one pass over every session, ordered by end time (13.10)
                self.db.execute(
                    "INSERT OR REPLACE INTO sessions(machine_id, session_id, user, host, name, name_source, project,"
                    " cwd, model, status, started, ended, last_event, agents, tool_calls, errors, compactions,"
                    " tokens_in, tokens_cache_create, tokens_cache_read, tokens_out, peak_context, prompts)"
                    " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                    (m, sid, snap["user"], snap["host"], sd["name"], sd["name_source"], sd["title"], sd["cwd"],
                     sd["model"], sd["status"], sd["started"], sd["ended"], sd["last_event"], len(agents),
                     sum(a["total_calls"] for a in agents), sum(a["errors"] for a in agents), sd["compactions"],
                     tok["i"], tok["cc"], tok["cr"], tok["o"], snap["peak"], snap["prompts"]),
                )
                self.db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def _write_report(self, m: str, sid: str, tasks: List[dict], failures: List[dict], kept: set, project: str = "", received: float = 0.0,
                      touched: Optional[list] = None) -> None:
        """Write the task report (13.7). A row for a call the Store has since evicted stays as stored, but a
        row whose call is still held and is no longer a failure (a late real result) is deleted. A call that
        was fixed keeps its stored fix and counts if the retry or edits have since been evicted. Task counts
        are recomputed from the failure rows, and a task's call count never goes down."""
        key = (m, sid)
        old_eff = {r[0]: tuple(r)[1:] for r in self.db.execute(  # what a task was measured with (13.10)
            "SELECT task_id, efficiency_version, efficiency_label, ended, retries, waste, eff_retry_tools, eff_tokens, eff_calls,"
            " eff_errors, eff_duration, cache_reuse, efficiency_kind, subagent_share, peak_context, context_window FROM tasks"
            " WHERE machine_id=? AND session_id=?", key)}
        old = {r[0]: r for r in self.db.execute(
            "SELECT call_id, resolution, attempts, changes, changes_total FROM failures"
            " WHERE machine_id=? AND session_id=?", key)}
        rows = []
        for f in failures:
            prev = old.get(f["call_id"])
            if prev is not None and prev[1] == "fixed":
                if f["resolution"] != "fixed":
                    continue  # the retry has been evicted: keep what was stored
                if prev[4] > f["changes_total"]:
                    f["changes"], f["changes_total"] = json.loads(prev[3]), prev[4]
                f["attempts"] = max(f["attempts"], prev[2])
            rows.append((m, sid, f["call_id"], f["task_id"], f["agent_id"], f["agent_type"], f["t"], f["tool"],
                         f["description"], f["kind"], f["reason"], f["resolution"], f["fix_call_id"],
                         f["fix_description"], f["fix_outcome"], f["fix_t"], f["attempts"],
                         json.dumps(f["changes"], ensure_ascii=False), f["changes_total"]))
        current = {f["call_id"] for f in failures}
        self.db.executemany(
            "DELETE FROM failures WHERE machine_id=? AND session_id=? AND call_id=?",
            [(m, sid, c) for c in old if c in kept and c not in current])
        self.db.executemany(
            "INSERT INTO tasks(machine_id, session_id, task_id, agent_id, agent_type, kind, description, started,"
            " ended, window_end, status, outcome, calls, errors, fixed, unresolved, peak_context, context_window,"
            " report_version, title) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(machine_id, session_id, task_id) DO UPDATE SET agent_id=excluded.agent_id,"
            " agent_type=excluded.agent_type, kind=excluded.kind, description=excluded.description,"
            " started=excluded.started, ended=excluded.ended, window_end=excluded.window_end,"
            " status=excluded.status, outcome=excluded.outcome, calls=MAX(calls, excluded.calls),"
            " peak_context=excluded.peak_context, context_window=excluded.context_window, report_version=excluded.report_version,"
            " title=excluded.title",
            [(m, sid, x["task_id"], x["agent_id"], x["agent_type"], x["kind"], x["description"], x["started"],
              x["ended"], x["window_end"], x["status"], x["outcome"], x["calls"], x["errors"], x["fixed"],
              x["unresolved"], x.get("peak_context"), x.get("context_window"), REPORT_VERSION,
              task_title(x["description"])[0]) for x in tasks],
        )
        # tool counts never shrink: a call the Store has evicted keeps its stored count (13.8)
        self.db.executemany(
            "INSERT INTO task_tools(machine_id, session_id, task_id, tool, calls, errors) VALUES (?,?,?,?,?,?)"
            " ON CONFLICT(machine_id, session_id, task_id, tool) DO UPDATE SET"
            " calls=MAX(calls, excluded.calls), errors=MAX(errors, excluded.errors)",
            [(m, sid, x["task_id"], tool, n, e) for x in tasks for tool, (n, e) in x.get("tools", {}).items()],
        )
        self.db.executemany(
            "INSERT OR REPLACE INTO failures(machine_id, session_id, call_id, task_id, agent_id, agent_type, t, tool,"
            " description, kind, reason, resolution, fix_call_id, fix_description, fix_outcome, fix_t, attempts,"
            " changes, changes_total) VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)
        # an evicted failure that was pending follows its task's end
        self.db.execute(
            "UPDATE failures SET resolution = CASE (SELECT t.status FROM tasks t WHERE t.machine_id=failures.machine_id"
            " AND t.session_id=failures.session_id AND t.task_id=failures.task_id)"
            " WHEN 'ok' THEN 'worked_around' WHEN 'running' THEN 'pending' ELSE 'unresolved' END"
            " WHERE machine_id=? AND session_id=? AND resolution='pending'"
            " AND EXISTS (SELECT 1 FROM tasks t WHERE t.machine_id=failures.machine_id"
            " AND t.session_id=failures.session_id AND t.task_id=failures.task_id)", key)
        if tasks:
            self.db.execute(
                "UPDATE tasks SET"
                " errors=(SELECT COUNT(*) FROM failures f WHERE f.machine_id=tasks.machine_id AND f.session_id=tasks.session_id AND f.task_id=tasks.task_id),"
                " fixed=(SELECT COUNT(*) FROM failures f WHERE f.machine_id=tasks.machine_id AND f.session_id=tasks.session_id AND f.task_id=tasks.task_id AND f.resolution='fixed'),"
                " unresolved=(SELECT COUNT(*) FROM failures f WHERE f.machine_id=tasks.machine_id AND f.session_id=tasks.session_id AND f.task_id=tasks.task_id AND f.resolution='unresolved')"
                " WHERE machine_id=? AND session_id=?", key)
            effstore.write_measures(self, m, sid, project, {x["task_id"]: x.get("eff") for x in tasks}, old_eff, received, touched)

    # ---- reads ------------------------------------------------------------

    def _with_state(self, machine_id: str, session_id: str, fn):
        """Run fn(ReplayState) while the state is protected from a concurrent ingest; None if unknown."""
        key = (machine_id, session_id)
        with self.cache_lock:
            state = self.cache.get(key)
            if state is not None:
                return fn(state)
        state = self._rebuild(key, self.reader())
        if state.last_key is None:
            return None
        with self.cache_lock:
            existing = self.cache.get(key)  # an ingest may have cached a newer one meanwhile
            if existing is None:
                self.cache.put(key, state)
            return fn(existing or state)

    def health(self) -> dict:
        """Unauthenticated liveness: counts only (the oldest-session time is in stats(), behind the token)."""
        sessions = self.reader().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        try:
            size = db_size(self.cfg.db)
        except OSError:
            size = 0
        return {"app": HUB_APP_ID, "version": __version__, "records": self._records, "sessions": sessions, "db_bytes": size}

    def stats(self) -> dict:
        """What Settings shows (13.12): session count, database size with its WAL files, the oldest session's start."""
        out = self.health()
        oldest = self.reader().execute("SELECT MIN(started) FROM sessions").fetchone()[0]
        return {"sessions": out["sessions"], "db_bytes": out["db_bytes"], "oldest_started": oldest}

    def _usage_rows(
        self, conn: sqlite3.Connection, key_sql: str, join: str, where: str, args: list, source: Optional[str] = None,
    ) -> List[sqlite3.Row]:
        """agent_usage summed by (key, model, speed, geo, over-long-context flags): SQL does the heavy lifting,
        Python prices the aggregates (the long-context price depends on each message, so it is a group key)."""
        flags = "".join(
            f", (u.input + u.cache_creation + u.cache_read > {int(o)}) AS o{i}" for i, o in enumerate(self.pricing.over_values())
        )
        flag_cols = "".join(f", o{i}" for i in range(len(self.pricing.over_values())))
        sums = "".join(f", SUM(u.{c}) AS {c}" for c in COUNT_FIELDS)
        sql = (
            f"SELECT {key_sql}, u.model AS model, u.speed AS speed, u.inference_geo AS geo, COUNT(*) AS n{sums}{flags}"
            f" FROM {source or 'agent_usage u ' + join} WHERE {where} GROUP BY k, u.model, u.speed, u.inference_geo{flag_cols}"
        )
        return conn.execute(sql, args).fetchall()

    def _price_rows(self, rows: List[sqlite3.Row], unpriced: set) -> Tuple[Dict[Any, UsageTotals], Dict[Any, sqlite3.Row]]:
        """UsageTotals per key from `_usage_rows` output, and one row per key (for its extra columns)."""
        overs = self.pricing.over_values()
        totals: Dict[Any, UsageTotals] = {}
        first: Dict[Any, sqlite3.Row] = {}
        for r in rows:
            counts = [r[c] or 0 for c in COUNT_FIELDS]
            hint = {o: bool(r[f"o{i}"]) for i, o in enumerate(overs)}
            cost = self.pricing.cost(r["model"], counts, r["speed"], r["geo"], hint)
            if cost is None:
                unpriced.add(r["model"] or "(none)")
            totals.setdefault(r["k"], UsageTotals()).add(counts, cost, 1, messages=r["n"])
            first.setdefault(r["k"], r)
        return totals, first

    def usage(self, machine_id: str, session_id: str) -> dict:
        rows = self._usage_rows(
            self.reader(), "u.agent_id AS k, MAX(u.agent_type) AS agent_type", "",
            "u.machine_id=? AND u.session_id=?", [machine_id, session_id])
        types: Dict[str, Optional[str]] = {}
        for r in rows:
            types[r["k"]] = types.get(r["k"]) or r["agent_type"]
        totals, _ = self._price_rows(rows, set())
        out: Dict[str, dict] = {}
        total = UsageTotals()
        for agent_id in sorted(totals):
            out[agent_id] = {**totals[agent_id].to_dict(), "agent_type": types.get(agent_id)}
            total.merge(totals[agent_id])
        out["total"] = total.to_dict()
        return out

    def usage_report(
        self, start: float, end: float, group_by: str = "agent_type", tz: int = 0,
        machine: Optional[str] = None, project: Optional[str] = None,
        agent_type: Any = None, model: Optional[str] = None, session: Optional[Tuple[str, str]] = None,
        kind: Optional[str] = None,
    ) -> dict:
        """Token totals and estimated cost over [start, end) on agent_usage.t, grouped (13.6)."""
        needs_join = group_by in ("project", "session") or project is not None
        key = {
            "agent_type": "COALESCE(u.agent_type, 'unknown')",
            "model": "COALESCE(u.model, 'unknown')",
            "project": "COALESCE(s.project, '')",
            "machine": "u.machine_id",
            "session": "u.machine_id || '/' || u.session_id",
            "day": "strftime('%Y-%m-%d', u.t + ? * 60, 'unixepoch')",
        }[group_by]
        extra = ", MAX(s.name) AS sname, MAX(u.machine_id) AS mid, MAX(u.session_id) AS sid" if group_by == "session" else ""
        args: list = [tz] if group_by == "day" else []
        where = ["u.t >= ?", "u.t < ?"]
        args += [start, end]
        if machine is not None:
            where.append("u.machine_id = ?")
            args.append(machine)
        if project is not None:
            where.append("COALESCE(s.project, '') = ?")
            args.append(project)
        if session is not None:
            where.append("u.machine_id = ? AND u.session_id = ?")
            args += list(session)
        if kind:  # turn = the main agent, subagent = every other lane (13.8)
            where.append("u.agent_id = 'main'" if kind == "turn" else "u.agent_id <> 'main'")
        types = [agent_type] if isinstance(agent_type, str) else list(agent_type or [])
        if types:  # multi: any value matches; 'main' is the main agent
            where.append(f"COALESCE(u.agent_type, 'unknown') IN ({','.join('?' * len(types))})")
            args += types
        if model is not None:
            where.append("u.model = ?")
            args.append(model)
        join = "LEFT JOIN sessions s ON s.machine_id = u.machine_id AND s.session_id = u.session_id" if needs_join else ""
        rows = self._usage_rows(self.reader(), f"{key} AS k{extra}", join, " AND ".join(where), args)
        unpriced: set = set()
        totals, first = self._price_rows(rows, unpriced)
        total = UsageTotals()
        groups = []
        for k, t in totals.items():
            total.merge(t)
            g = {"key": k, "label": k}
            if group_by == "session":
                r = first[k]
                g.update(label=r["sname"] or r["sid"], machine_id=r["mid"], session_id=r["sid"])
            g.update(t.to_dict())
            groups.append(g)
        groups.sort(key=lambda g: (-g["cost_usd"], -(g["input"] + g["cache_creation"] + g["cache_read"] + g["output"]), g["key"]))
        return {
            "from": start, "to": end, "group_by": group_by,
            "groups": groups[:USAGE_GROUP_MAX], "total": total.to_dict() if totals else empty_usage(),
            "truncated": len(groups) > USAGE_GROUP_MAX, "pricing": self.pricing.to_info(unpriced),
        }

    def replay(self, machine_id: str, session_id: str) -> Optional[dict]:
        def build(state: ReplayState) -> Optional[dict]:
            sd = state.store.session_dict(session_id, full=True)
            if sd is None:
                return None
            sd.update(machine_id=machine_id, user=state.user, host=state.host)
            return sd

        found = self._with_state(machine_id, session_id, build)
        if found is not None:
            found["usage"] = self.usage(machine_id, session_id)
        return found

    def call_detail(self, machine_id: str, session_id: str, call_id: str) -> Optional[dict]:
        return self._with_state(machine_id, session_id, lambda st: st.store.call_detail(session_id, call_id))

    def session_files(self, machine_id: str, session_id: str) -> Optional[List[dict]]:
        conn = self.reader()
        if not conn.execute("SELECT 1 FROM sessions WHERE machine_id=? AND session_id=?", (machine_id, session_id)).fetchone():
            return None
        rows = conn.execute(
            "SELECT agent_id, call_id, path, op, tool, status, t FROM file_access"
            " WHERE machine_id=? AND session_id=? ORDER BY t, call_id",
            (machine_id, session_id),
        )
        return [dict(r) for r in rows]

    def session_permissions(self, machine_id: str, session_id: str) -> List[dict]:
        rows = self.reader().execute(
            "SELECT agent_id, call_id, tool, kind, text, t FROM permissions"
            " WHERE machine_id=? AND session_id=? ORDER BY t, rowid",
            (machine_id, session_id),
        )
        return [dict(r) for r in rows]

    def list_sessions(
        self,
        machine: Optional[str] = None,
        project: Optional[str] = None,
        q: Optional[str] = None,
        start_from: Optional[float] = None,
        start_to: Optional[float] = None,
        has_file: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = PAGE,
    ) -> dict:
        where, args = [], []  # type: List[str], List[Any]
        if machine:
            where.append("machine_id = ?")
            args.append(machine)
        if project:
            where.append("project = ?")
            args.append(project)
        if q:
            where.append("(name LIKE ? ESCAPE '\\' OR project LIKE ? ESCAPE '\\' OR prompts LIKE ? ESCAPE '\\')")
            args += [_like(q)] * 3
        if start_from is not None:
            where.append("started >= ?")
            args.append(start_from)
        if start_to is not None:
            where.append("started <= ?")
            args.append(start_to)
        if has_file:
            prefix = has_file.endswith("/")
            clause = "f.path >= ? AND f.path < ?" if prefix else "f.path = ?"
            where.append(
                "EXISTS (SELECT 1 FROM file_access f WHERE f.machine_id = sessions.machine_id"
                f" AND f.session_id = sessions.session_id AND {clause})"
            )
            args += [has_file, has_file + MAX_PATH_KEY] if prefix else [has_file]
        if cursor:
            where.append("(started, machine_id, session_id) < (?, ?, ?)")
            args += list(decode_cursor(cursor))
        limit = max(1, min(limit, PAGE_MAX))
        sql = (
            f"SELECT {SESSION_COLS} FROM sessions" + (" WHERE " + " AND ".join(where) if where else "")
            + " ORDER BY started DESC, machine_id DESC, session_id DESC LIMIT ?"
        )
        rows = [dict(r) for r in self.reader().execute(sql, (*args, limit + 1))]
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            last = rows[-1]
            nxt = encode_cursor(last["started"], last["machine_id"], last["session_id"])
        return {"sessions": rows, "next_cursor": nxt}

    # ---- task and error report (13.7) ---------------------------------------

    def list_tasks(self, **kw: Any) -> dict:
        return reports.list_tasks(self, **kw)

    def list_failures(self, **kw: Any) -> dict:
        return reports.list_failures(self, **kw)

    def report_summary(self, **kw: Any) -> dict:
        return reports.summary(self, **kw)

    def report_trend(self, start: float, end: float, **kw: Any) -> dict:
        return reports.trend(self, start, end, **kw)

    def report_overview(self, **kw: Any) -> dict:
        return reports.overview(self, **kw)

    def efficiency_overview(self, **kw: Any) -> dict:
        return effstore.overview(self, reports._common, reports.TASK_JOIN, **kw)

    def efficiency_baseline(self, **kw: Any) -> dict:
        return effstore.baseline(self, **kw)

    def report_facets(self, **kw: Any) -> dict:
        return reports.facets(self, **kw)

    def task_detail(self, machine_id: str, session_id: str, task_id: str) -> Optional[dict]:
        return reports.task_detail(self, machine_id, session_id, task_id)

    def session_report(self, machine_id: str, session_id: str) -> Optional[dict]:
        return reports.session_report(self, machine_id, session_id)

    def lookup_files(
        self, path: Optional[str], prefix: Optional[str], limit: int = 200, cursor: Optional[str] = None
    ) -> dict:
        """One row per session: its latest access to the path(s), the access count, and the ops seen."""
        if path:
            cond, args = "f.path = ?", [path]
        else:
            cond, args = "f.path >= ? AND f.path < ?", [prefix, (prefix or "") + MAX_PATH_KEY]
        having, hargs = "", []  # type: str, list
        if cursor:
            having = " HAVING (MAX(f.t), f.machine_id, f.session_id) < (?, ?, ?)"
            hargs = list(decode_cursor(cursor))
        limit = max(1, min(limit, FILES_MAX))
        rows = [dict(r) for r in self.reader().execute(
            "SELECT f.machine_id, f.session_id, s.name AS session_name, s.project, f.agent_id, f.call_id,"
            " f.path, f.op, f.tool, f.status, MAX(f.t) AS t, COUNT(*) AS accesses,"
            " group_concat(DISTINCT f.op) AS ops FROM file_access f"
            " LEFT JOIN sessions s ON s.machine_id=f.machine_id AND s.session_id=f.session_id"
            f" WHERE {cond} GROUP BY f.machine_id, f.session_id{having}"
            " ORDER BY MAX(f.t) DESC, f.machine_id DESC, f.session_id DESC LIMIT ?",
            (*args, *hargs, limit + 1),
        )]
        nxt = None
        if len(rows) > limit:
            rows = rows[:limit]
            nxt = encode_cursor(rows[-1]["t"], rows[-1]["machine_id"], rows[-1]["session_id"])
        for r in rows:
            r["ops"] = sorted((r["ops"] or "").split(","))
        return {"files": rows, "next_cursor": nxt}

    def machines(self) -> List[dict]:
        rows = self.reader().execute(
            "SELECT m.machine_id,"
            " (SELECT user FROM sessions s WHERE s.machine_id=m.machine_id ORDER BY last_event DESC LIMIT 1) AS user,"
            " (SELECT host FROM sessions s WHERE s.machine_id=m.machine_id ORDER BY last_event DESC LIMIT 1) AS host,"
            " MIN(m.started) AS first_seen, MAX(m.last_event) AS last_seen, COUNT(*) AS sessions"
            " FROM sessions m GROUP BY m.machine_id ORDER BY last_seen DESC"
        )
        return [dict(r) for r in rows]

    # ---- maintenance ------------------------------------------------------

    def run_retention(self, now: Optional[float] = None) -> int:
        """Delete sessions idle longer than the retention window. Returns how many were removed."""
        days = self.cfg.retention_days
        if days <= 0:
            return 0
        cutoff = (self.clock() if now is None else now) - days * DAY
        orphan = (" WHERE t < ? AND NOT EXISTS (SELECT 1 FROM sessions s"
                  " WHERE s.machine_id={0}.machine_id AND s.session_id={0}.session_id)")
        with self.ingest_lock, self.write_lock:
            keys = [tuple(r) for r in self.db.execute(
                "SELECT machine_id, session_id FROM sessions WHERE last_event < ?", (cutoff,))]
            self.db.execute("BEGIN")
            try:
                # agent_usage stays: it holds only counts and ids, and keeps long-range totals right (13.6)
                for table in ("records", "file_access", "permissions", "tasks", "task_tools", "failures", "sessions"):
                    self.db.executemany(f"DELETE FROM {table} WHERE machine_id=? AND session_id=?", keys)
                # rows of sessions that never produced a summary row
                self.db.execute("DELETE FROM records" + orphan.format("records"), (cutoff,))
                self.db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise
            self._records = self.db.execute("SELECT COUNT(*) FROM records").fetchone()[0]
            with self.cache_lock:
                for key in keys:
                    self.cache.drop(key)
            self.db.execute("PRAGMA incremental_vacuum")
        return len(keys)

    def backup_dir(self) -> Path:
        return Path(self.cfg.db).parent / "backups"

    def run_backup(self, when: Optional[datetime] = None) -> Optional[Path]:
        """VACUUM INTO history-YYYYMMDD.db from its own connection, then keep the newest CCMC_HUB_BACKUPS."""
        keep = self.cfg.backups
        if keep <= 0:
            return None
        day = (when or datetime.fromtimestamp(self.clock(), timezone.utc)).strftime("%Y%m%d")
        folder = self.backup_dir()
        folder.mkdir(parents=True, exist_ok=True)
        target = folder / f"history-{day}.db"
        tmp = folder / (target.name + ".tmp")
        tmp.unlink(missing_ok=True)
        conn = sqlite3.connect(str(self.cfg.db), isolation_level=None)  # a reader snapshot; no write lock
        try:
            conn.execute("VACUUM INTO ?", (str(tmp),))
        finally:
            conn.close()
        os.replace(tmp, target)
        for old in sorted(folder.glob("history-????????.db"))[:-keep]:
            old.unlink(missing_ok=True)
        return target

    def backfill_agent_types(self, limit: int = BACKFILL_SESSIONS, seconds: float = BACKFILL_SECONDS) -> int:
        """Rebuild up to `limit` sessions that still have usage rows without an agent_type (13.6).

        Only rows older than BACKFILL_AGE count. Rows whose session no longer exists can never resolve and
        are set to 'unknown' once (a later hook for that session still overwrites it). A session that stays
        unresolved after one rebuild is left alone until the next start, so it cannot starve the others.
        The ingest lock is taken per session, and the run stops after about `seconds`."""
        cutoff = self.clock() - BACKFILL_AGE
        with self.ingest_lock, self.write_lock:
            self.db.execute(
                "UPDATE agent_usage SET agent_type='unknown' WHERE agent_type IS NULL AND t < ? AND NOT EXISTS"
                " (SELECT 1 FROM sessions s WHERE s.machine_id=agent_usage.machine_id"
                " AND s.session_id=agent_usage.session_id)", (cutoff,))
        keys = [
            (r[0], r[1]) for r in self.reader().execute(
                "SELECT DISTINCT machine_id, session_id FROM agent_usage WHERE agent_type IS NULL AND t < ?"
                " ORDER BY machine_id, session_id", (cutoff,))
            if (r[0], r[1]) not in self._backfilled
        ][:limit]
        done, deadline = 0, time.monotonic() + seconds
        for key in keys:
            if time.monotonic() > deadline:
                break
            self._backfilled.add(key)
            done += 1
            try:
                self._rebuild_session(key)
            except Exception:
                log.exception("could not backfill agent types for %s", key)
        return done

    def backfill_reports(self, limit: int = BACKFILL_SESSIONS, finalize: bool = True) -> int:
        """Rebuild up to `limit` sessions that have no task rows yet (those ingested before migration 4, 13.7) or any
        task row below REPORT_VERSION (13.8) or below EFFICIENCY_VERSION (13.10).

        Oldest sessions first. A session is replayed without the ingest lock (`_rebuild_session`), and scoring runs
        once at the end (`finalize`), not per session: the pass scores every pending task in end-time order, so
        one pass gives each task a baseline of the earlier tasks that already have theirs.
        A session with nothing to report is not retried until the next start."""
        keys = [
            (r[0], r[1]) for r in self.reader().execute(
                "SELECT s.machine_id, s.session_id FROM sessions s WHERE NOT EXISTS (SELECT 1 FROM tasks t"
                " WHERE t.machine_id=s.machine_id AND t.session_id=s.session_id)"
                " OR EXISTS (SELECT 1 FROM tasks t WHERE t.machine_id=s.machine_id AND t.session_id=s.session_id"
                " AND (t.report_version < ? OR t.efficiency_version < ?)) ORDER BY s.started", (REPORT_VERSION, efficiency.EFFICIENCY_VERSION))
            if (r[0], r[1]) not in self._report_backfilled and (r[0], r[1]) not in self._report_skipped
        ][:limit]
        for key in keys:
            try:
                if self._rebuild_session(key, score=False):
                    self._report_backfilled.add(key)  # only now: a session still replaying is not "rebuilt" yet
                else:
                    self._report_skipped.add(key)  # too busy this run; ingest keeps it fresh, the next run retries
                    log.info("hub upgrade: %s/%s is receiving records, left to the next run", *key)
            except Exception:
                self._report_backfilled.add(key)  # it can never be rebuilt: it must not hold scoring back
                log.exception("could not backfill the task report for %s", key)
            time.sleep(BACKFILL_YIELD)  # let a waiting ingest take the lock before the next session
        if finalize:
            self._finalize_scoring()
        return len(keys)

    def _finalize_scoring(self) -> None:
        with self.ingest_lock, self.write_lock:  # rows no replay will rewrite must not hold scoring back
            self.db.execute("BEGIN")
            try:
                effstore.settle_orphans(self)
                effstore.score_pass(self)
                self.db.execute("COMMIT")
            except BaseException:
                self._rollback()
                raise

    def fill_titles(self, batch: int = TITLE_BATCH) -> int:
        """Set `title` on task rows that have none, from the stored description and without a replay (13.9).

        Runs in batches of `batch` rows, one transaction each, so a large table never holds the write lock long."""
        total = 0
        while True:
            with self.write_lock:
                rows = self.db.execute(
                    "SELECT rowid, description FROM tasks WHERE title IS NULL LIMIT ?", (batch,)).fetchall()
                if not rows:
                    return total
                self.db.execute("BEGIN")
                try:
                    self.db.executemany("UPDATE tasks SET title=? WHERE rowid=?",
                                        [(task_title(r[1])[0], r[0]) for r in rows])
                    self.db.execute("COMMIT")
                except Exception:
                    self.db.execute("ROLLBACK")
                    raise
            total += len(rows)

    def backfill_stale(self) -> int:
        """Run the report backfill; after an upgrade (rows below the current efficiency version), keep going until
        it has nothing left, so new tasks are not left unscored for days (13.10). Each run works in steps of at most
        replay without the ingest lock, and scoring runs once at the end. Returns sessions rebuilt."""
        stale = self.reader().execute(
            "SELECT 1 FROM tasks WHERE efficiency_version < ? OR report_version < ? LIMIT 1",
            (efficiency.EFFICIENCY_VERSION, REPORT_VERSION)).fetchone()
        if stale is None:
            return self.backfill_reports()
        self._report_skipped.clear()
        started, total = time.monotonic(), 0
        while True:
            n = self.backfill_reports(finalize=False)
            total += n
            if n == 0:
                break
        self._finalize_scoring()  # one scoring pass for the whole upgrade
        log.info("hub upgrade: rebuilt %d sessions in %.1f s", total, time.monotonic() - started)
        return total

    def maintenance(self) -> None:
        self.run_retention()
        self.backfill_agent_types()
        self.fill_titles()
        self.backfill_stale()
        self.run_backup()

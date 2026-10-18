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
from ..redact import Redactor, load_patterns
from . import HUB_APP_ID
from .db import open_db
from .ingest import Record, parse_envelope, usage_row
from .replay import ReplayCache, ReplayState, replay_config

log = logging.getLogger(__name__)
PAGE = 50
PAGE_MAX = 200
FILES_MAX = 1000
STORED_FIELD_CHARS = 65_536  # defense-in-depth redaction must not re-truncate relay-truncated text
DAY = 86400
MAX_PATH_KEY = "\U0010ffff"
SESSION_COLS = (
    "machine_id, session_id, user, host, name, name_source, project, cwd, model, status, started, ended, "
    "last_event, agents, tool_calls, errors, compactions, tokens_in, tokens_cache_create, tokens_cache_read, "
    "tokens_out, peak_context"
)
ROW_ERRORS = (sqlite3.Error, OverflowError, ValueError, TypeError, RecursionError)


class BadCursor(ValueError):
    pass


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

    def _upsert_usage(self, rec: Record) -> None:
        row = usage_row(rec)
        if row is None:
            return
        # max per field, so totals do not depend on the arrival order of repeated streamed entries
        self.db.execute(
            "INSERT INTO agent_usage(machine_id, session_id, agent_id, message_id, model, input, cache_creation,"
            " cache_read, output, t) VALUES (?,?,?,?,?,?,?,?,?,?)"
            " ON CONFLICT(machine_id, session_id, agent_id, message_id) DO UPDATE SET"
            " input=MAX(input, excluded.input), cache_creation=MAX(cache_creation, excluded.cache_creation),"
            " cache_read=MAX(cache_read, excluded.cache_read), output=MAX(output, excluded.output),"
            " model=COALESCE(model, excluded.model), t=MIN(t, excluded.t)",
            (rec.machine_id, rec.session_id, row[0], row[1], row[2], *row[3:], rec.t),
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
            self._write_derived(key, snap)

    def _rebuild(self, key: Tuple[str, str], conn: sqlite3.Connection) -> ReplayState:
        state = ReplayState(key[1], self.store_config)
        rows = conn.execute(
            "SELECT kind, user, host, t, received, rowid, data FROM records"
            " WHERE machine_id=? AND session_id=? ORDER BY t, received, rowid",
            key,
        )
        for r in rows:
            state.apply(r["kind"], r["t"], json.loads(r["data"]), r["user"], r["host"], (r["t"], r["received"], r["rowid"]))
        return state

    @staticmethod
    def _snapshot(key: Tuple[str, str], state: ReplayState) -> Optional[dict]:
        sd = state.store.session_dict(key[1])
        if sd is None:  # records but no hook yet: nothing to summarize
            return None
        return {"sd": sd, "files": state.file_rows(), "perms": list(state.permissions), "user": state.user,
                "host": state.host, "peak": state.peak_context, "prompts": "\n".join(state.prompts)}

    def _write_derived(self, key: Tuple[str, str], snap: dict) -> None:
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
                # upserts only: rows for calls the Store has since evicted must stay
                self.db.executemany(
                    "INSERT OR REPLACE INTO file_access(machine_id, session_id, agent_id, call_id, path, op, tool,"
                    " status, t) VALUES (?,?,?,?,?,?,?,?,?)",
                    [(m, sid, *row) for row in snap["files"]],
                )
                self.db.executemany(
                    "INSERT OR IGNORE INTO permissions(machine_id, session_id, agent_id, call_id, tool, kind, text, t)"
                    " VALUES (?,?,?,?,?,?,?,?)",
                    [(m, sid, *row) for row in snap["perms"]],
                )
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
        sessions = self.reader().execute("SELECT COUNT(*) FROM sessions").fetchone()[0]
        try:
            size = os.path.getsize(self.cfg.db)
        except OSError:
            size = 0
        return {"app": HUB_APP_ID, "version": __version__, "records": self._records, "sessions": sessions, "db_bytes": size}

    def usage(self, machine_id: str, session_id: str) -> dict:
        rows = self.reader().execute(
            "SELECT agent_id, SUM(input) i, SUM(cache_creation) cc, SUM(cache_read) cr, SUM(output) o, COUNT(*) n"
            " FROM agent_usage WHERE machine_id=? AND session_id=? GROUP BY agent_id ORDER BY agent_id",
            (machine_id, session_id),
        ).fetchall()
        out: Dict[str, dict] = {}
        total = {"input": 0, "cache_creation": 0, "cache_read": 0, "output": 0, "messages": 0}
        for r in rows:
            row = {"input": r["i"], "cache_creation": r["cc"], "cache_read": r["cr"], "output": r["o"], "messages": r["n"]}
            out[r["agent_id"]] = row
            for k, v in row.items():
                total[k] += v
        out["total"] = total
        return out

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
                for table in ("records", "agent_usage", "file_access", "permissions", "sessions"):
                    self.db.executemany(f"DELETE FROM {table} WHERE machine_id=? AND session_id=?", keys)
                # rows of sessions that never produced a summary row
                for table in ("records", "agent_usage"):
                    self.db.execute(f"DELETE FROM {table}" + orphan.format(table), (cutoff,))
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

    def maintenance(self) -> None:
        self.run_retention()
        self.run_backup()

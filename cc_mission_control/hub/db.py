"""SQLite schema and migrations for the hub (SPEC 13.2)."""

from __future__ import annotations

import sqlite3
from pathlib import Path

# One entry per schema version; index + 1 == PRAGMA user_version after applying it.
MIGRATIONS = [
    """
    CREATE TABLE records(
        eid TEXT PRIMARY KEY, machine_id TEXT NOT NULL, user TEXT NOT NULL DEFAULT '',
        host TEXT NOT NULL DEFAULT '', session_id TEXT NOT NULL, kind TEXT NOT NULL,
        t REAL NOT NULL, received REAL NOT NULL, data TEXT NOT NULL);
    CREATE INDEX records_session ON records(machine_id, session_id, t);
    CREATE TABLE sessions(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, user TEXT NOT NULL DEFAULT '',
        host TEXT NOT NULL DEFAULT '', name TEXT NOT NULL DEFAULT '', name_source TEXT NOT NULL DEFAULT 'folder',
        project TEXT NOT NULL DEFAULT '', cwd TEXT NOT NULL DEFAULT '', model TEXT, status TEXT NOT NULL,
        started REAL NOT NULL, ended REAL, last_event REAL NOT NULL,
        agents INTEGER NOT NULL DEFAULT 0, tool_calls INTEGER NOT NULL DEFAULT 0,
        errors INTEGER NOT NULL DEFAULT 0, compactions INTEGER NOT NULL DEFAULT 0,
        tokens_in INTEGER NOT NULL DEFAULT 0, tokens_cache_create INTEGER NOT NULL DEFAULT 0,
        tokens_cache_read INTEGER NOT NULL DEFAULT 0, tokens_out INTEGER NOT NULL DEFAULT 0,
        peak_context INTEGER NOT NULL DEFAULT 0, prompts TEXT NOT NULL DEFAULT '',
        PRIMARY KEY(machine_id, session_id));
    CREATE INDEX sessions_started ON sessions(started);
    CREATE INDEX sessions_project ON sessions(project);
    CREATE INDEX sessions_last_event ON sessions(last_event);
    CREATE TABLE agent_usage(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL, message_id TEXT NOT NULL,
        model TEXT, input INTEGER NOT NULL, cache_creation INTEGER NOT NULL, cache_read INTEGER NOT NULL,
        output INTEGER NOT NULL, t REAL NOT NULL,
        PRIMARY KEY(machine_id, session_id, agent_id, message_id));
    CREATE TABLE file_access(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL, call_id TEXT NOT NULL,
        path TEXT NOT NULL, op TEXT NOT NULL, tool TEXT NOT NULL, status TEXT NOT NULL, t REAL NOT NULL,
        PRIMARY KEY(machine_id, session_id, call_id));
    CREATE INDEX file_access_path ON file_access(path);
    CREATE INDEX file_access_session ON file_access(machine_id, session_id);
    CREATE TABLE permissions(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL, call_id TEXT NOT NULL,
        tool TEXT NOT NULL, kind TEXT NOT NULL, text TEXT NOT NULL, t REAL NOT NULL);
    CREATE INDEX permissions_session ON permissions(machine_id, session_id);
    """,
    # permissions are upserted (never deleted) so they survive call eviction; the unique index is the key
    """
    DELETE FROM permissions WHERE rowid NOT IN (
        SELECT MIN(rowid) FROM permissions GROUP BY machine_id, session_id, kind, call_id, agent_id, t, text);
    CREATE UNIQUE INDEX permissions_unique ON permissions(machine_id, session_id, kind, call_id, agent_id, t, text);
    """,
]


def open_db(path: Path | str) -> sqlite3.Connection:
    """Open (creating and migrating) the hub database. Autocommit; callers issue BEGIN/COMMIT."""
    if str(path) != ":memory:":
        Path(path).parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(str(path), check_same_thread=False, isolation_level=None)
    conn.row_factory = sqlite3.Row
    version = conn.execute("PRAGMA user_version").fetchone()[0]
    if version > len(MIGRATIONS):
        conn.close()
        raise RuntimeError(f"database schema v{version} is newer than this hub (v{len(MIGRATIONS)})")
    if version == 0:
        conn.execute("PRAGMA auto_vacuum=INCREMENTAL")  # only takes effect before the first table
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA synchronous=NORMAL")
    for n in range(version, len(MIGRATIONS)):
        conn.executescript(f"BEGIN;{MIGRATIONS[n]}PRAGMA user_version={n + 1};COMMIT;")
    return conn

"""SQLite schema and migrations for the hub (SPEC 13.2)."""

from __future__ import annotations

import os
import sqlite3
from pathlib import Path


def db_size(path) -> int:
    """Bytes the database takes on disk: the file plus its -wal and -shm files when they exist."""
    total = 0
    for suffix in ("", "-wal", "-shm"):
        try:
            total += os.path.getsize(str(path) + suffix)
        except OSError:
            if suffix == "":
                raise
    return total

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
    # 13.6: agent_usage is keyed by (machine, message) with the TTL split, web counts, speed and geo.
    # Duplicates merge by the highest count and earliest t; session and agent come from the earliest row.
    """
    CREATE INDEX agent_usage_tmp ON agent_usage(machine_id, message_id);
    CREATE TABLE agent_usage_new(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, agent_id TEXT NOT NULL, agent_type TEXT,
        message_id TEXT NOT NULL, model TEXT,
        input INTEGER NOT NULL DEFAULT 0, cache_creation INTEGER NOT NULL DEFAULT 0,
        cache_creation_5m INTEGER NOT NULL DEFAULT 0, cache_creation_1h INTEGER NOT NULL DEFAULT 0,
        cache_read INTEGER NOT NULL DEFAULT 0, output INTEGER NOT NULL DEFAULT 0,
        web_search INTEGER NOT NULL DEFAULT 0, web_fetch INTEGER NOT NULL DEFAULT 0,
        speed TEXT, inference_geo TEXT, split INTEGER NOT NULL DEFAULT 0, t REAL NOT NULL,
        PRIMARY KEY(machine_id, message_id));
    INSERT INTO agent_usage_new(machine_id, session_id, agent_id, agent_type, message_id, model, input,
            cache_creation, cache_creation_5m, cache_creation_1h, cache_read, output, web_search, web_fetch,
            speed, inference_geo, split, t)
        SELECT a.machine_id,
               (SELECT x.session_id FROM agent_usage x WHERE x.machine_id=a.machine_id AND x.message_id=a.message_id
                ORDER BY x.t, x.rowid LIMIT 1),
               (SELECT x.agent_id FROM agent_usage x WHERE x.machine_id=a.machine_id AND x.message_id=a.message_id
                ORDER BY x.t, x.rowid LIMIT 1),
               NULL, a.message_id, MAX(a.model), MAX(a.input), MAX(a.cache_creation), MAX(a.cache_creation), 0,
               MAX(a.cache_read), MAX(a.output), 0, 0, NULL, NULL, 0, MIN(a.t)
        FROM agent_usage a GROUP BY a.machine_id, a.message_id;
    UPDATE agent_usage_new SET agent_type='main' WHERE agent_id='main';
    DROP TABLE agent_usage;
    ALTER TABLE agent_usage_new RENAME TO agent_usage;
    CREATE INDEX agent_usage_t ON agent_usage(t);
    CREATE INDEX agent_usage_session ON agent_usage(machine_id, session_id, agent_id);
    """,
    # 13.7: the task and error report, derived from replay and upserted like file_access
    """
    CREATE TABLE tasks(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, agent_id TEXT NOT NULL,
        agent_type TEXT, kind TEXT NOT NULL, description TEXT NOT NULL DEFAULT '',
        started REAL NOT NULL, ended REAL, window_end REAL, status TEXT NOT NULL, outcome TEXT NOT NULL DEFAULT '',
        calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0, fixed INTEGER NOT NULL DEFAULT 0,
        unresolved INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(machine_id, session_id, task_id));
    CREATE INDEX tasks_started ON tasks(started);
    CREATE INDEX tasks_status ON tasks(status);
    CREATE TABLE failures(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, call_id TEXT NOT NULL, task_id TEXT NOT NULL,
        agent_id TEXT NOT NULL, agent_type TEXT, t REAL NOT NULL, tool TEXT NOT NULL DEFAULT '',
        description TEXT NOT NULL DEFAULT '', kind TEXT NOT NULL, reason TEXT NOT NULL DEFAULT '',
        resolution TEXT NOT NULL, fix_call_id TEXT, fix_description TEXT, fix_outcome TEXT, fix_t REAL,
        attempts INTEGER NOT NULL DEFAULT 0, changes TEXT NOT NULL DEFAULT '[]',
        changes_total INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(machine_id, session_id, call_id));
    CREATE INDEX failures_t ON failures(t);
    CREATE INDEX failures_kind ON failures(kind);
    CREATE INDEX failures_resolution ON failures(resolution);
    CREATE INDEX failures_task ON failures(machine_id, session_id, task_id);
    """,
    # 13.8: peak context per task, per-task tool counts, and the task of each file access.
    # report_version 1 rows (written before this migration) are rebuilt by the backfill.
    """
    ALTER TABLE tasks ADD COLUMN peak_context INTEGER;
    ALTER TABLE tasks ADD COLUMN context_window INTEGER;
    ALTER TABLE tasks ADD COLUMN report_version INTEGER NOT NULL DEFAULT 1;
    CREATE TABLE task_tools(
        machine_id TEXT NOT NULL, session_id TEXT NOT NULL, task_id TEXT NOT NULL, tool TEXT NOT NULL,
        calls INTEGER NOT NULL DEFAULT 0, errors INTEGER NOT NULL DEFAULT 0,
        PRIMARY KEY(machine_id, session_id, task_id, tool));
    CREATE INDEX task_tools_tool ON task_tools(tool);
    ALTER TABLE file_access ADD COLUMN task_id TEXT;
    CREATE INDEX file_access_task ON file_access(machine_id, session_id, task_id);
    """,
    # 13.9: a short readable title per task. Rows written before this migration get a NULL title, which the hub
    # fills from the stored description on start (no replay); the repeat filter counts through this index.
    """
    ALTER TABLE tasks ADD COLUMN title TEXT;
    CREATE INDEX tasks_title ON tasks(machine_id, title, kind);
    """,
    # 13.10: task efficiency (eff_* columns hold the stored inputs of a score, so a baseline never reads columns that keep changing). Rows written before this migration have efficiency_version 0 and are rebuilt by the
    # backfill. eff_tokens and eff_project are extra to the SPEC list: the baseline needs tokens per call and the
    # project pool without a replay or a join.
    """
    ALTER TABLE tasks ADD COLUMN retries INTEGER;
    ALTER TABLE tasks ADD COLUMN cache_reuse REAL;
    ALTER TABLE tasks ADD COLUMN subagent_share REAL;
    ALTER TABLE tasks ADD COLUMN waste TEXT;
    ALTER TABLE tasks ADD COLUMN efficiency_score INTEGER;
    ALTER TABLE tasks ADD COLUMN efficiency_label TEXT;
    ALTER TABLE tasks ADD COLUMN efficiency_reasons TEXT;
    ALTER TABLE tasks ADD COLUMN efficiency_kind TEXT;
    ALTER TABLE tasks ADD COLUMN efficiency_version INTEGER NOT NULL DEFAULT 0;
    ALTER TABLE tasks ADD COLUMN eff_tokens INTEGER;
    ALTER TABLE tasks ADD COLUMN eff_project TEXT;
    ALTER TABLE tasks ADD COLUMN eff_calls INTEGER;
    ALTER TABLE tasks ADD COLUMN eff_errors INTEGER;
    ALTER TABLE tasks ADD COLUMN eff_duration REAL;
    ALTER TABLE tasks ADD COLUMN eff_retry_tools TEXT;
    CREATE INDEX tasks_efficiency_label ON tasks(efficiency_label);
    CREATE INDEX tasks_eff_kind ON tasks(efficiency_kind, ended);
    CREATE INDEX tasks_eff_project ON tasks(efficiency_kind, eff_project, ended);
    CREATE INDEX tasks_eff_version ON tasks(efficiency_version, ended);
    CREATE INDEX tasks_eff_label_kind ON tasks(efficiency_label, efficiency_version, efficiency_kind);
    """,
]


# What migration 7 grew into across the 0.9.x development rounds. A database migrated by an earlier round has the
# version number but not every column or index, so open_db adds what is missing (idempotent, a no-op when current).
HEAL_COLUMNS = (
    ("retries", "INTEGER"), ("cache_reuse", "REAL"), ("subagent_share", "REAL"), ("waste", "TEXT"),
    ("efficiency_score", "INTEGER"), ("efficiency_label", "TEXT"), ("efficiency_reasons", "TEXT"),
    ("efficiency_kind", "TEXT"), ("efficiency_version", "INTEGER NOT NULL DEFAULT 0"), ("eff_tokens", "INTEGER"),
    ("eff_project", "TEXT"), ("eff_calls", "INTEGER"), ("eff_errors", "INTEGER"), ("eff_duration", "REAL"),
    ("eff_retry_tools", "TEXT"),
)
HEAL_INDEXES = (
    ("tasks_efficiency_label", "tasks(efficiency_label)"),
    ("tasks_eff_kind", "tasks(efficiency_kind, ended)"),
    ("tasks_eff_project", "tasks(efficiency_kind, eff_project, ended)"),
    ("tasks_eff_version", "tasks(efficiency_version, ended)"),
    ("tasks_eff_label_kind", "tasks(efficiency_label, efficiency_version, efficiency_kind)"),
)


def heal(conn: sqlite3.Connection) -> None:
    """Add any efficiency column or index migration 7 lacks (13.10). Rows scored by an older round, which had no
    stored inputs, are marked version 0 so the backfill rebuilds them."""
    if conn.execute("PRAGMA user_version").fetchone()[0] < 7:
        return
    have = {r[1] for r in conn.execute("PRAGMA table_info(tasks)")}
    added = [c for c, _ in HEAL_COLUMNS if c not in have]
    for name, decl in HEAL_COLUMNS:
        if name not in have:
            conn.execute(f"ALTER TABLE tasks ADD COLUMN {name} {decl}")
    for name, on in HEAL_INDEXES:
        conn.execute(f"CREATE INDEX IF NOT EXISTS {name} ON {on}")
    if "eff_calls" in added:
        conn.execute("UPDATE tasks SET efficiency_version=0, efficiency_label=NULL, efficiency_score=NULL WHERE efficiency_version > 0")


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
    heal(conn)
    return conn

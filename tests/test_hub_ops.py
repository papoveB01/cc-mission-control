from datetime import datetime, timezone

from test_hub_common import AUTH, envelopes, make_client, post, scenario, live_store
from cc_mission_control.state import Store
from cc_mission_control.config import Config

DAY = 86400


def count(client, table):
    svc = client.app.state.service
    with svc.lock:
        return svc.db.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]


def test_retention_removes_old_sessions_and_all_rows(tmp_path):
    client = make_client(tmp_path, retention_days=30)
    base = 1_000_000_000.0
    old = [(k, t + base, d) for k, t, d in scenario("old")]
    new = [(k, t + base + 95 * DAY, d) for k, t, d in scenario("new")]
    post(client, envelopes(old, sid="old") + envelopes(new, sid="new"))
    svc = client.app.state.service
    now = base + 100 * DAY  # old is ~100 days stale, new ~5
    assert svc.run_retention(now=now) == 1
    assert [r["session_id"] for r in svc.db.execute("SELECT session_id FROM sessions")] == ["new"]
    for table in ("records", "agent_usage", "file_access", "permissions"):
        left = {r[0] for r in svc.db.execute(f"SELECT DISTINCT session_id FROM {table}")}
        assert left == {"new"}, table
    assert client.get("/api/history/sessions/mach-a/old", headers=AUTH).status_code == 404


def test_retention_disabled_by_default(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario()))
    assert client.app.state.service.run_retention(now=1e12) == 0
    assert count(client, "sessions") == 1


def test_backup_creates_file_and_prunes(tmp_path):
    client = make_client(tmp_path, backups=3)
    post(client, envelopes(scenario()))
    svc = client.app.state.service
    made = []
    for day in range(1, 6):
        made.append(svc.run_backup(datetime(2026, 1, day, tzinfo=timezone.utc)))
    folder = tmp_path / "data" / "backups"
    assert sorted(p.name for p in folder.iterdir()) == ["history-20260103.db", "history-20260104.db", "history-20260105.db"]
    import sqlite3

    copy = sqlite3.connect(made[-1])
    assert copy.execute("SELECT COUNT(*) FROM records").fetchone()[0] == count(client, "records")
    copy.close()
    svc.run_backup(datetime(2026, 1, 5, tzinfo=timezone.utc))  # same day again just replaces the file
    assert len(list(folder.glob("history-*.db"))) == 3


def test_backups_zero_disables(tmp_path):
    client = make_client(tmp_path, backups=0)
    assert client.app.state.service.run_backup() is None


def test_schema_version_and_wal(tmp_path):
    client = make_client(tmp_path)
    svc = client.app.state.service
    assert svc.db.execute("PRAGMA user_version").fetchone()[0] == 2
    assert svc.db.execute("PRAGMA journal_mode").fetchone()[0] == "wal"
    tables = {r[0] for r in svc.db.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"records", "sessions", "agent_usage", "file_access", "permissions"} <= tables


def test_live_session_dict_unchanged_by_full_flag():
    store = live_store(scenario())
    default = store.session_dict("sess-1")
    assert default == store.session_dict("sess-1", full=False)
    assert len(default["activity"]) <= 80 and all(len(a["calls"]) <= 60 for a in default["agents"])


def test_full_sends_everything_past_the_live_limits():
    now = [0.0]
    store = Store(Config(max_calls=500), clock=lambda: now[0], activity_max=1000)
    store.ingest({"hook_event_name": "SessionStart", "session_id": "s", "cwd": "/p"})
    for i in range(100):
        now[0] = i + 1
        store.ingest({"hook_event_name": "PreToolUse", "session_id": "s", "tool_name": "Read",
                      "tool_use_id": f"c{i}", "tool_input": {"file_path": f"/f{i}"}})
    short, full = store.session_dict("s"), store.session_dict("s", full=True)
    assert len(short["activity"]) == 80 and len(full["activity"]) == 101
    assert len(short["agents"][0]["calls"]) == 60 and len(full["agents"][0]["calls"]) == 100

import json
import random

from test_hub_common import AUTH, MACHINE, SID, env, envelopes, hook, live_store, make_app, make_client, post, scenario
from fastapi.testclient import TestClient


def rows(client, sql, *args):
    svc = client.app.state.service
    with svc.lock:
        return [dict(r) for r in svc.db.execute(sql, args)]


def replayed(client, sid=SID, machine=MACHINE):
    svc = client.app.state.service
    return svc._with_state(machine, sid, lambda st: st.store.session_dict(sid))


def derived(client):
    return {
        "sessions": rows(client, "SELECT * FROM sessions"),
        "files": rows(client, "SELECT * FROM file_access ORDER BY call_id"),
        "perms": rows(client, "SELECT * FROM permissions ORDER BY t"),
    }


def test_ingest_is_idempotent(tmp_path):
    client = make_client(tmp_path)
    batch = envelopes(scenario())
    first = post(client, batch).json()
    assert first == {"accepted": len(batch), "duplicates": 0, "invalid": 0}
    before = derived(client)
    second = post(client, batch).json()
    assert second == {"accepted": 0, "duplicates": len(batch), "invalid": 0}
    assert derived(client) == before
    assert rows(client, "SELECT COUNT(*) n FROM agent_usage")[0]["n"] == 3


def test_replay_matches_live_store(tmp_path):
    events = scenario()
    live = live_store(events)
    client = make_client(tmp_path)
    post(client, envelopes(events))
    assert replayed(client) == live.session_dict(SID)
    body = client.get(f"/api/history/sessions/{MACHINE}/{SID}", headers=AUTH).json()
    extra = {k: body.pop(k) for k in ("machine_id", "user", "host", "usage")}
    assert body == live.session_dict(SID, full=True)
    assert extra["machine_id"] == MACHINE and extra["user"] == "alice" and extra["host"] == "box"
    detail = client.get(f"/api/history/sessions/{MACHINE}/{SID}/calls/t2", headers=AUTH).json()
    assert detail == live.call_detail(SID, "t2")
    assert client.get(f"/api/history/sessions/{MACHINE}/{SID}/calls/nope", headers=AUTH).status_code == 404


def test_shuffled_and_batched_ingest_replays_identically(tmp_path):
    events = scenario()
    live = live_store(events)
    items = envelopes(events)
    random.Random(7).shuffle(items)
    client = make_client(tmp_path)
    for i in range(0, len(items), 6):
        assert post(client, items[i:i + 6]).status_code == 200
    assert replayed(client) == live.session_dict(SID)


def test_incremental_summary_equals_full_rebuild(tmp_path):
    client = make_client(tmp_path)
    items = envelopes(scenario())
    for i in range(0, len(items), 5):  # in-order batches take the incremental path
        post(client, items[i:i + 5])
    incremental = derived(client)
    svc = client.app.state.service
    with svc.cache_lock:
        svc.cache.drop((MACHINE, SID))
    key = (MACHINE, SID)
    svc._write_derived(key, svc._snapshot(key, svc._rebuild(key, svc.reader())))
    assert derived(client) == incremental


def test_file_access_and_permissions(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario()))
    files = client.get(f"/api/history/sessions/{MACHINE}/{SID}/files", headers=AUTH).json()["files"]
    got = [(f["op"], f["path"], f["status"], f["agent_id"]) for f in files]
    assert got == [
        ("read", "/work/proj/a.py", "ok", "main"),
        ("edit", "/work/proj/b.py", "ok", "main"),
        ("write", "/work/proj/c.py", "error", "main"),
        ("search", "/work/proj", "ok", "main"),
        ("read", "/work/proj/d.py", "ok", "ag1"),
    ]
    perms = client.get(f"/api/history/sessions/{MACHINE}/{SID}/permissions", headers=AUTH).json()["permissions"]
    assert [(p["kind"], p["tool"], p["call_id"]) for p in perms] == [("prompt", "", ""), ("denied", "Bash", "t7")]
    assert perms[1]["text"] == "not allowed"


def test_running_file_call_status_follows_post(tmp_path):
    client = make_client(tmp_path)
    pre = env("hook", 1, hook(1, "SessionStart")[2])
    read = env("hook", 2, hook(2, "PreToolUse", tool_name="Read", tool_use_id="r", tool_input={"file_path": "/x"})[2])
    post(client, [pre, read])
    assert rows(client, "SELECT status FROM file_access")[0]["status"] == "running"
    post(client, [env("hook", 3, hook(3, "PostToolUse", tool_name="Read", tool_use_id="r", tool_response={"output": "o"})[2])])
    assert rows(client, "SELECT status FROM file_access")[0]["status"] == "ok"


def test_token_totals_and_peak_context(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario()))
    s = rows(client, "SELECT * FROM sessions")[0]
    assert (s["tokens_in"], s["tokens_cache_create"], s["tokens_cache_read"], s["tokens_out"]) == (151, 11, 26, 38)
    assert s["peak_context"] == 12000
    assert (s["agents"], s["tool_calls"], s["errors"]) == (2, 7, 3)  # Write failed, Bash denied, Agent spawn closed unreported
    assert s["name"] == "My parser session" and s["name_source"] == "custom" and s["project"] == "proj"
    usage = client.get(f"/api/history/sessions/{MACHINE}/{SID}", headers=AUTH).json()["usage"]
    assert usage["main"] == {"input": 100, "cache_creation": 10, "cache_read": 20, "output": 30, "messages": 1}
    assert usage["ag1"]["messages"] == 2 and usage["ag1"]["input"] == 51
    assert usage["total"] == {"input": 151, "cache_creation": 11, "cache_read": 26, "output": 38, "messages": 3}


def test_invalid_envelopes_are_counted_and_skipped(tmp_path):
    client = make_client(tmp_path)
    good = env("hook", 1, hook(1, "SessionStart")[2])
    bad = [
        {**good, "eid": "short"},
        {**good, "eid": "f" * 32, "v": 1},
        {**good, "eid": "e" * 32, "kind": "mystery"},
        {**good, "eid": "d" * 32, "session_id": ""},
        {**good, "eid": "c" * 32, "machine_id": "m" * 129},
        {**good, "eid": "b" * 32, "t": float("inf")},
        {**good, "eid": "a" * 32, "t": True},
        {**good, "eid": "9" * 32, "data": []},
        {**good, "eid": "8" * 32, "kind": "usage", "data": {"agent_id": "main", "input": -1}},
        "nope",
    ]
    body = json.dumps([good, *bad]).replace("Infinity", "1e999")
    r = client.post("/ingest", content=body, headers={**AUTH, "content-type": "application/json"})
    assert r.json() == {"accepted": 1, "duplicates": 0, "invalid": len(bad)}
    assert client.post("/ingest", json={"not": "a list"}, headers=AUTH).status_code == 400


def test_body_over_2mb_is_413(tmp_path):
    client = make_client(tmp_path)
    payload = {"pad": "x" * (2 * 1024 * 1024 + 10)}
    assert client.post("/ingest", json=[payload], headers=AUTH).status_code == 413


def test_stored_data_is_redacted_again(tmp_path):
    secret = "gh" + "p_" + "a1B2c3D4e5F6g7H8i9J0" * 2
    client = make_client(tmp_path)
    post(client, [env("hook", 1, hook(1, "UserPromptSubmit", prompt="use " + secret)[2])])
    stored = rows(client, "SELECT data FROM records")[0]["data"]
    assert secret not in stored and "[redacted]" in stored


def test_batch_ingest_on_large_session_is_fast(tmp_path):
    import time

    client = make_client(tmp_path)
    events = [hook(1, "SessionStart")]
    for i in range(2500):
        t = 2 + i * 0.01
        events.append(hook(t, "PreToolUse", tool_name="Read", tool_use_id=f"c{i}", tool_input={"file_path": f"/f/{i}"}))
        events.append(hook(t + 0.005, "PostToolUse", tool_name="Read", tool_use_id=f"c{i}", tool_response={"output": "o"}))
    items = envelopes(events)
    for i in range(0, 4800, 200):
        post(client, items[i:i + 200])
    start = time.time()
    r = post(client, items[4800:5001])
    assert r.json()["accepted"] == 201 and time.time() - start < 1.0

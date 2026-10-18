import json
import random
import threading
import time

import pytest
from fastapi.testclient import TestClient

from cc_mission_control.config import Config, HubConfig
from cc_mission_control.hub.app import create_app
from test_hub_common import (AUTH, CWD, MACHINE, SID, TOKEN, env, envelopes, hook, hub_config, live_store, make_app,
                             make_client, post, scenario)


def table(client, sql, *args):
    svc = client.app.state.service
    with svc.lock:
        return [dict(r) for r in svc.db.execute(sql, args)]


def replayed(client, sid=SID, machine=MACHINE):
    return client.app.state.service._with_state(machine, sid, lambda st: st.store.session_dict(sid))


def raw_post(client, body: str):
    return client.post("/ingest", content=body, headers={**AUTH, "content-type": "application/json"})


# ---- item 1: a bad item never makes /ingest fail ---------------------------------------------


def test_overflowing_numbers_and_deep_nesting_are_counted_invalid(tmp_path):
    client = make_client(tmp_path)
    good = env("hook", 1000, hook(1000, "SessionStart")[2])
    base = {"v": 2, "machine_id": "m", "session_id": "s", "user": "", "host": ""}
    usage = {"agent_id": "main", "message_id": "x", "input": 1, "cache_creation": 0, "cache_read": 0, "output": 0}
    items = [
        {**base, "eid": "1" * 32, "t": 1.0, "kind": "usage", "data": {**usage, "input": 2**70}},
        {**base, "eid": "2" * 32, "t": 1.0, "kind": "usage", "data": {**usage, "output": 2**53 + 1}},
        {**base, "eid": "3" * 32, "t": 10**400, "kind": "cwd", "data": {"cwd": "/"}},
        {**base, "eid": "4" * 32, "t": 0, "kind": "cwd", "data": {"cwd": "/"}},
        {**base, "eid": "5" * 32, "t": 5e9, "kind": "cwd", "data": {"cwd": "/"}},
        {**base, "eid": "6" * 32, "t": 1.0, "kind": "cwd", "data": {"cwd": "/"}},
    ]
    deep = []
    for _ in range(100):
        deep = [deep]
    items.append({**base, "eid": "7" * 32, "t": 1.0, "kind": "cwd", "data": {"x": deep}})
    body = json.dumps([good, *items])
    body = body[:-1] + ', {"v":2,"eid":"' + "8" * 32 + '","machine_id":"m","session_id":"s","t":1e400,"kind":"cwd","data":{}}]'
    r = raw_post(client, body)
    assert r.status_code == 200
    out = r.json()
    assert out["accepted"] == 2 and out["invalid"] == 7  # the good hook and the valid cwd (item 6)


def test_deeply_nested_body_is_400_not_500(tmp_path):
    client = make_client(tmp_path)
    assert raw_post(client, "[" * 200_000 + "]" * 200_000).status_code == 400
    assert raw_post(client, "not json").status_code == 400
    assert raw_post(client, "{}").status_code == 400


def test_one_failing_row_does_not_reject_the_batch(tmp_path, monkeypatch):
    client = make_client(tmp_path)
    svc = client.app.state.service
    real = svc._upsert_usage

    def flaky(rec):
        if rec.data.get("message_id") == "boom":
            raise OverflowError("simulated")
        real(rec)

    monkeypatch.setattr(svc, "_upsert_usage", flaky)
    items = envelopes([hook(1, "SessionStart"),
                       ("usage", 2, {"agent_id": "main", "message_id": "boom", "input": 1, "cache_creation": 0, "cache_read": 0, "output": 0}),
                       ("usage", 3, {"agent_id": "main", "message_id": "ok", "input": 5, "cache_creation": 0, "cache_read": 0, "output": 0})])
    assert post(client, items).json() == {"accepted": 2, "duplicates": 0, "invalid": 1}
    assert table(client, "SELECT COUNT(*) n FROM records")[0]["n"] == 2
    assert [r["message_id"] for r in table(client, "SELECT message_id FROM agent_usage")] == ["ok"]


# ---- item 2: malformed cookies -----------------------------------------------------------------


def test_cookie_parser_rejects_non_ascii_digits():
    from cc_mission_control.hub.auth import cookie_ok

    assert cookie_ok(TOKEN, "٣٤.abc") is False
    assert cookie_ok(TOKEN, "9" * 13 + ".abc") is False


@pytest.mark.parametrize("value", ["9" * 50 + ".x", "12345678901234567890.x", "abc", ".", "1.", "-5.x", ""])
def test_malformed_cookies_get_401(tmp_path, value):
    client = make_client(tmp_path)
    client.cookies.set("ccmc_hub_session", value)
    assert client.get("/api/history/machines").status_code == 401


# ---- item 3: totals are independent of arrival order ---------------------------------------------


def test_usage_totals_do_not_depend_on_arrival_order(tmp_path):
    def usage(t, mid, out, agent="main"):
        return ("usage", t, {"agent_id": agent, "message_id": mid, "input": 10, "cache_creation": 1, "cache_read": 2, "output": out})

    events = [hook(1, "SessionStart"), usage(2, "a", 5), usage(3, "a", 12), usage(4, "a", 30), usage(5, "b", 7), usage(6, "b", 7),
              usage(7, "c", 1, "ag1"), usage(8, "c", 4, "ag1")]
    totals = set()
    for seed in range(6):
        items = envelopes(events)
        random.Random(seed).shuffle(items)
        client = make_client(tmp_path / str(seed))
        for i in range(0, len(items), 3):
            post(client, items[i:i + 3])
        s = table(client, "SELECT tokens_in, tokens_cache_create, tokens_cache_read, tokens_out FROM sessions")[0]
        totals.add(tuple(s.values()))
    assert totals == {(30, 3, 6, 30 + 7 + 4)}


# ---- item 4: login body cap, rate limit, headers ------------------------------------------------


def test_login_body_cap_and_rate_limit(tmp_path):
    client = make_client(tmp_path)
    assert client.post("/login", content=b"x" * 5000).status_code == 413
    for _ in range(10):
        assert client.post("/login", json={"token": "wrong"}).status_code == 401
    assert client.post("/login", json={"token": "wrong"}).status_code == 429
    assert client.post("/login", json={"token": TOKEN}).status_code == 429  # blocked for the window


def test_security_headers(tmp_path):
    client = make_client(tmp_path)
    for r in (client.get("/history"), client.post("/login", json={"token": TOKEN})):
        assert r.headers["content-security-policy"] == "default-src 'self'; frame-ancestors 'none'"
        assert r.headers["x-content-type-options"] == "nosniff"
        assert r.headers["referrer-policy"] == "no-referrer"


# ---- item 5: default host allowlist -------------------------------------------------------------


def test_default_hosts_include_tailnet_name(tmp_path):
    cfg = HubConfig.from_env({"CCMC_HUB_TOKEN": TOKEN, "CCMC_HUB_DB": str(tmp_path / "h.db")})
    client = TestClient(create_app(cfg, store_config=Config()))
    for host in ("ccmc-hub", "ccmc-hub:4318", "ccmc-hub.tail1234.ts.net:4318", "CCMC-HUB.my-net.ts.net"):
        assert client.get("/api/history/machines", headers={**AUTH, "Host": host}).status_code == 200, host
    for host in ("ccmc-hub.a.b.ts.net", "evil.ts.net", "ccmc-hub.evil.com", "xccmc-hub.a.ts.net", "ccmc-hub..ts.net"):
        assert client.get("/api/history/machines", headers={**AUTH, "Host": host}).status_code == 403, host


def test_explicit_hosts_replace_defaults(tmp_path):
    cfg = HubConfig.from_env({"CCMC_HUB_TOKEN": TOKEN, "CCMC_HUB_DB": str(tmp_path / "h.db"), "CCMC_HUB_HOSTS": "other"})
    client = TestClient(create_app(cfg, store_config=Config()))
    assert client.get("/api/history/machines", headers={**AUTH, "Host": "other"}).status_code == 200
    assert client.get("/api/history/machines", headers={**AUTH, "Host": "ccmc-hub"}).status_code == 403
    assert client.get("/api/history/machines", headers={**AUTH, "Host": "ccmc-hub.a.ts.net"}).status_code == 403


def test_origin_port_must_match_request_host_port(tmp_path):
    client = make_client(tmp_path)
    h = {**AUTH, "Host": "testserver:4318"}
    assert client.get("/api/history/machines", headers={**h, "Origin": "http://testserver:4318"}).status_code == 200
    assert client.get("/api/history/machines", headers={**h, "Origin": "http://testserver:9999"}).status_code == 403
    assert client.get("/api/history/machines", headers={**h, "Origin": "http://testserver"}).status_code == 403


def test_token_not_in_config_repr():
    assert TOKEN not in repr(HubConfig(token=TOKEN))


# ---- item 6: reads and health do not wait for the write lock ------------------------------------


def run_in_thread(fn, timeout=10):
    box = {}
    t = threading.Thread(target=lambda: box.update(r=fn()))
    t.start()
    t.join(timeout)
    assert not t.is_alive(), "request blocked on the write lock"
    return box["r"]


def test_health_and_reads_return_while_the_write_lock_is_held(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario()))
    svc = client.app.state.service
    ingest_started, release = threading.Event(), threading.Event()

    def long_ingest():  # stands in for a slow write transaction
        with svc.ingest_lock, svc.write_lock:
            ingest_started.set()
            release.wait(30)

    writer = threading.Thread(target=long_ingest)
    writer.start()
    assert ingest_started.wait(10)
    try:
        assert run_in_thread(lambda: client.get("/health")).json()["sessions"] == 1
        assert run_in_thread(lambda: client.get("/api/history/sessions", headers=AUTH)).status_code == 200
        assert run_in_thread(lambda: client.get(f"/api/history/sessions/{MACHINE}/{SID}", headers=AUTH)).status_code == 200
        assert run_in_thread(lambda: client.get("/api/history/machines", headers=AUTH)).status_code == 200
    finally:
        release.set()
        writer.join(10)


def test_backup_does_not_need_the_write_lock(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario()))
    svc = client.app.state.service
    with svc.ingest_lock, svc.write_lock:
        path = run_in_thread(svc.run_backup)
    assert path.exists()


# ---- item 7: derived rows survive call eviction --------------------------------------------------


def test_file_access_survives_store_eviction(tmp_path, monkeypatch):
    monkeypatch.setenv("CCMC_MAX_CALLS", "3")
    client = make_client(tmp_path)
    events = [hook(1, "SessionStart")]
    for i in range(8):
        events.append(hook(2 + i * 2, "PreToolUse", tool_name="Read", tool_use_id=f"c{i}", tool_input={"file_path": f"/f/{i}"}))
        events.append(hook(3 + i * 2, "PostToolUse", tool_name="Read", tool_use_id=f"c{i}", tool_response={"output": "o"}))
    events.append(hook(30, "PermissionDenied", tool_name="Bash", tool_use_id="bx", reason="no"))
    items = envelopes(events)
    for i in range(0, len(items), 6):
        post(client, items[i:i + 6])
    def paths():
        return [r["path"] for r in table(client, "SELECT path FROM file_access ORDER BY t")]
    assert paths() == [f"/f/{i}" for i in range(8)]
    # an out-of-order record forces a rebuild with the small cap; nothing may disappear
    post(client, envelopes([hook(1.5, "UserPromptSubmit", prompt="late")]))
    assert paths() == [f"/f/{i}" for i in range(8)]
    assert len(table(client, "SELECT * FROM permissions")) == 1
    assert {r["status"] for r in table(client, "SELECT status FROM file_access")} == {"ok"}


# ---- nits: ordering, isolation, speed ----------------------------------------------------------


def test_out_of_order_batches_on_a_warm_cache(tmp_path):
    events = scenario()
    live = live_store(events)
    client = make_client(tmp_path)
    items = envelopes(events)
    post(client, items[:20])    # warm cache
    post(client, items[24:])    # in order, skipping a gap
    post(client, items[20:24])  # late records sort before the cached tail: forces a rebuild
    assert post(client, items[22:26]).json()["duplicates"] == 4
    assert replayed(client) == live.session_dict(SID)
    full = scenario()
    assert table(client, "SELECT COUNT(*) n FROM records")[0]["n"] == len(full)


def test_two_machines_may_share_a_session_id(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario(), machine="mach-a", user="ann"))
    post(client, envelopes(scenario()[:6], machine="mach-b", user="bob"))
    assert {(r["machine_id"], r["user"]) for r in table(client, "SELECT machine_id, user FROM sessions")} == {("mach-a", "ann"), ("mach-b", "bob")}
    a = client.get(f"/api/history/sessions/mach-a/{SID}", headers=AUTH).json()
    b = client.get(f"/api/history/sessions/mach-b/{SID}", headers=AUTH).json()
    assert a["user"] == "ann" and b["user"] == "bob"
    assert len(a["agents"]) == 2 and len(b["agents"]) == 1
    assert a["usage"]["total"]["messages"] == 3 and b["usage"]["total"]["messages"] == 0
    assert table(client, "SELECT COUNT(*) n FROM permissions WHERE machine_id='mach-b'")[0]["n"] == 0


def test_out_of_order_batch_on_a_5000_record_session_is_fast(tmp_path):
    client = make_client(tmp_path)
    events = [hook(1000, "SessionStart")]
    for i in range(2499):
        t = 1001 + i * 0.01
        events.append(hook(t, "PreToolUse", tool_name="Read", tool_use_id=f"c{i}", tool_input={"file_path": f"/f/{i}"}))
        events.append(hook(t + 0.005, "PostToolUse", tool_name="Read", tool_use_id=f"c{i}", tool_response={"output": "o"}))
    items = envelopes(events)
    late = [items.pop(len(items) // 2 + k * 7) for k in range(100)]  # 100 records that belong mid-session
    items = items[:4800]
    for i in range(0, len(items), 200):
        post(client, items[i:i + 200])
    start = time.time()
    r = post(client, late).json()
    assert r["accepted"] == 100 and time.time() - start < 2.0


def test_machines_report_latest_user_and_host(tmp_path):
    client = make_client(tmp_path)
    post(client, envelopes(scenario("old"), sid="old", user="ann", host="h-old"))
    later = [(k, t + 500, d) for k, t, d in scenario("new")]
    post(client, envelopes(later, sid="new", user="bob", host="h-new"))
    (m,) = client.get("/api/history/machines", headers=AUTH).json()["machines"]
    assert (m["user"], m["host"], m["sessions"]) == ("bob", "h-new", 2)


def test_retention_sweeps_orphan_usage(tmp_path):
    client = make_client(tmp_path, retention_days=30)
    base = 1_000_000_000.0
    post(client, [env("usage", base, {"agent_id": "main", "message_id": "g", "input": 1, "cache_creation": 0, "cache_read": 0, "output": 0}, sid="ghost")])
    assert table(client, "SELECT COUNT(*) n FROM agent_usage")[0]["n"] == 1
    client.app.state.service.run_retention(now=base + 40 * 86400)
    assert table(client, "SELECT COUNT(*) n FROM agent_usage")[0]["n"] == 0
    assert table(client, "SELECT COUNT(*) n FROM records")[0]["n"] == 0
    assert client.get("/health").json()["records"] == 0


def test_file_lookup_is_one_row_per_session_and_paged(tmp_path):
    client = make_client(tmp_path)
    for n, sid in enumerate(("a", "b", "c")):
        evs = [hook(1000 + n * 100, "SessionStart", sid)]
        for k, tool in enumerate(("Read", "Edit") if sid == "a" else ("Read",)):
            evs.append(hook(1001 + n * 100 + k, "PreToolUse", sid, tool_name=tool, tool_use_id=f"x{k}", tool_input={"file_path": "/p/f.py"}))
        post(client, envelopes(evs, sid=sid))
    page = client.get("/api/history/files", params={"path": "/p/f.py", "limit": 2}, headers=AUTH).json()
    assert [f["session_id"] for f in page["files"]] == ["c", "b"] and page["next_cursor"]
    rest = client.get("/api/history/files", params={"path": "/p/f.py", "limit": 2, "cursor": page["next_cursor"]}, headers=AUTH).json()
    (only,) = rest["files"]
    assert only["session_id"] == "a" and only["accesses"] == 2 and only["ops"] == ["edit", "read"] and rest["next_cursor"] is None
    assert client.get("/api/history/files", params={"path": "/p/f.py", "cursor": "zz"}, headers=AUTH).status_code == 400

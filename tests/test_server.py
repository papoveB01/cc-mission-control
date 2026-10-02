import asyncio
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from cc_mission_control import __version__
from cc_mission_control import server as server_mod
from cc_mission_control.config import Config
from cc_mission_control.redact import MASK
from cc_mission_control.server import create_app, dumps
from cc_mission_control.state import Store

SID = "sess-1234567890"
BASE = "http://127.0.0.1:4317"
A20 = "a1B2c3D4e5F6g7H8i9J0"
SECRET = "sk-" + "ant-api03-" + A20 + A20


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def ev(name, **kw):
    base = {"session_id": SID, "transcript_path": "", "cwd": "/work/demo", "hook_event_name": name}
    base.update(kw)
    return base


def pre(call_id="toolu_1", **inp):
    return ev("PreToolUse", tool_name="Bash", tool_use_id=call_id, tool_input=inp or {"command": "ls"})


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def make(tmp_path, clock):
    def build(**cfg):
        cfg.setdefault("data_dir", tmp_path)
        config = Config(**cfg)
        return create_app(config, clock=clock, start_loops=False)

    return build


@pytest.fixture
def app(make):
    return make()


@pytest.fixture
def client(app):
    with TestClient(app, base_url=BASE) as c:
        yield c


def connect(client, headers=None, host="127.0.0.1:4317"):
    # TestClient's websocket_connect ignores base_url, so set Host explicitly
    return client.websocket_connect("/ws", headers={"Host": host, **(headers or {})})


def post(client, event, **kw):
    return client.post("/hook", content=json.dumps(event), **kw)


# ---- /hook -----------------------------------------------------------------


def assert_empty_200(r):
    assert r.status_code == 200
    assert r.content == b""
    assert r.headers.get("content-length") == "0"
    assert "application/json" not in r.headers.get("content-type", "")


def test_hook_empty_200_valid(client):
    assert_empty_200(post(client, ev("SessionStart")))


@pytest.mark.parametrize("body", [b"{not json", b"", b"[1, 2]", b'"str"', b"123", b"null", b"\xff\xfe"])
def test_hook_empty_200_malformed(client, body):
    assert_empty_200(client.post("/hook", content=body))


def test_hook_empty_200_unknown_event(client):
    assert_empty_200(post(client, ev("TotallyNewEvent")))
    assert_empty_200(post(client, {"hello": "world"}))


def test_hook_empty_200_when_store_raises(client, app, monkeypatch):
    def boom(event):
        raise RuntimeError("boom")

    monkeypatch.setattr(app.state.store, "ingest", boom)
    assert_empty_200(post(client, ev("SessionStart")))


def test_hook_ingests_and_serves_detail(client):
    post(client, ev("SessionStart"))
    post(client, pre(command="echo " + SECRET))
    sessions = client.get("/api/sessions").json()
    assert [s["id"] for s in sessions] == [SID]
    call = sessions[0]["agents"][0]["calls"][0]
    detail = client.get(f"/api/sessions/{SID}/calls/{call['id']}")
    assert detail.status_code == 200
    body = detail.text
    assert SECRET not in body and MASK in body


def test_unknown_call_404(client):
    post(client, ev("SessionStart"))
    assert client.get(f"/api/sessions/{SID}/calls/nope").status_code == 404
    assert client.get("/api/sessions/nope/calls/nope").status_code == 404


def test_health(client):
    h = client.get("/health").json()
    assert h == {"app": "cc-mission-control", "version": __version__, "clients": 0, "sessions": 0, "active": 0}
    post(client, ev("SessionStart"))
    assert client.get("/health").json()["sessions"] == 1
    assert client.get("/health").json()["active"] == 1
    with connect(client):
        assert client.get("/health").json()["clients"] == 1


# ---- Host / Origin ---------------------------------------------------------


@pytest.mark.parametrize("host", ["evil.com", "127.0.0.1:9999", "127.0.0.1", "localhost", "evil.com:4317"])
def test_bad_host_rejected(app, host):
    with TestClient(app, base_url="http://" + host) as c:
        hdr = {"Host": host}
        assert c.get("/health", headers=hdr).status_code == 403
        assert c.get("/api/sessions", headers=hdr).status_code == 403
        assert c.get("/", headers=hdr).status_code == 403
        assert c.post("/hook", content=b"{}", headers=hdr).status_code == 403


def test_localhost_host_allowed(app):
    with TestClient(app, base_url="http://localhost:4317") as c:
        assert c.get("/health").status_code == 200
        assert c.get("/api/sessions").status_code == 200


def test_ws_bad_host_rejected(app):
    with TestClient(app, base_url="http://evil.com") as c:
        with pytest.raises(WebSocketDisconnect) as exc:
            with connect(c, host="evil.com"):
                pass
        assert exc.value.code == 1008


@pytest.mark.parametrize("path", ["/api/sessions", "/health", "/"])
def test_allowed_origins(client, path):
    for origin in (BASE, "http://localhost:4317"):
        assert client.get(path, headers={"Origin": origin}).status_code == 200


def test_foreign_origin_rejected(client):
    for origin in ("http://evil.com", "http://127.0.0.1:5173", "https://127.0.0.1:4317", "null"):
        h = {"Origin": origin}
        assert client.post("/hook", content=b"{}", headers=h).status_code == 403
        assert client.get("/api/sessions", headers=h).status_code == 403
        assert client.get(f"/api/sessions/{SID}/calls/x", headers=h).status_code == 403
        with pytest.raises(WebSocketDisconnect) as exc:
            with connect(client, h):
                pass
        assert exc.value.code == 1008


def test_foreign_origin_not_ingested(client):
    client.post("/hook", content=json.dumps(ev("SessionStart")), headers={"Origin": "http://evil.com"})
    assert client.get("/api/sessions").json() == []


def test_no_origin_allowed(client):
    assert_empty_200(post(client, ev("SessionStart")))


def test_dev_origin_accepted_others_rejected(make):
    app = make(dev_origins=("http://localhost:5173",))
    with TestClient(app, base_url=BASE) as c:
        ok = {"Origin": "http://localhost:5173"}
        assert c.get("/api/sessions", headers=ok).status_code == 200
        assert c.post("/hook", content=b"{}", headers=ok).status_code == 200
        with connect(c, ok) as ws:
            assert ws.receive_json()["type"] == "snapshot"
        assert c.get("/api/sessions", headers={"Origin": "http://evil.com"}).status_code == 403


def test_root_served_with_foreign_origin(client):
    r = client.get("/", headers={"Origin": "http://evil.com"})
    assert r.status_code == 200
    assert "text/html" in r.headers["content-type"]
    assert "cc-mission-control" in r.text


# ---- WebSocket -------------------------------------------------------------


def test_ws_snapshot_then_changed_only(client, app):
    other = dict(ev("SessionStart"), session_id="other-session-0001")
    post(client, other)
    app.state.store.pop_changed()
    with connect(client) as ws:
        snap = ws.receive_json()
        assert snap["type"] == "snapshot" and snap["version"] == __version__
        assert [s["id"] for s in snap["sessions"]] == ["other-session-0001"]
        post(client, ev("SessionStart"))
        post(client, pre())
        client.portal.call(app.state.broadcast_tick)
        msg = ws.receive_json()
        assert msg["type"] == "sessions"
        assert [s["id"] for s in msg["sessions"]] == [SID]
        # nothing changed: no further message is queued
        client.portal.call(app.state.broadcast_tick)
        ws.send_text("ping")
        post(client, ev("Stop"))
        client.portal.call(app.state.broadcast_tick)
        assert ws.receive_json()["type"] == "sessions"


def test_ws_disconnect_removes_client(client, app):
    with connect(client) as ws:
        ws.receive_json()
        assert len(app.state.clients) == 1
    deadline = time.time() + 2
    while app.state.clients and time.time() < deadline:
        time.sleep(0.01)
    assert not app.state.clients


def test_broadcast_without_clients_still_drains(client, app):
    post(client, ev("SessionStart"))
    client.portal.call(app.state.broadcast_tick)
    assert app.state.store.pop_changed() == set()


def test_broadcast_survives_poll_error(client, app, monkeypatch):
    def boom():
        raise RuntimeError("poll")

    monkeypatch.setattr(app.state.watcher, "poll", boom)
    client.portal.call(app.state.broadcast_tick)  # must not raise


class FakeWS:
    def __init__(self, stuck=False):
        self.stuck = stuck
        self.sent = []
        self.closed = None

    async def send_text(self, text):
        if self.stuck:
            await asyncio.sleep(30)
        self.sent.append(json.loads(text))

    async def close(self, code=1000):
        self.closed = code


def test_stuck_client_does_not_delay_healthy_and_is_dropped(client, app, monkeypatch):
    monkeypatch.setattr(server_mod, "SEND_TIMEOUT", 0.1)

    async def run():
        stuck, healthy = FakeWS(stuck=True), FakeWS()
        app.state.register_client(stuck)
        app.state.register_client(healthy)
        app.state.store.ingest(ev("SessionStart"))
        t0 = time.monotonic()
        await app.state.broadcast_tick()
        assert time.monotonic() - t0 < 0.05  # broadcast never awaits client I/O
        await asyncio.sleep(0.02)
        assert [m["type"] for m in healthy.sent] == ["snapshot", "sessions"]
        assert stuck.sent == []
        await asyncio.sleep(0.3)  # the stuck send times out
        return stuck, healthy

    stuck, healthy = client.portal.call(run)
    assert stuck.closed == 1011
    assert list(app.state.clients) == [healthy]


def test_overflowing_client_dropped_and_closed(client, app):
    async def run():
        stuck = FakeWS(stuck=True)
        app.state.register_client(stuck)
        for i in range(server_mod.CLIENT_QUEUE + 4):
            app.state.store.ingest(dict(ev("SessionStart"), session_id=f"sess-overflow-{i:04d}"))
            await app.state.broadcast_tick()
            await asyncio.sleep(0)
        await asyncio.sleep(0.05)
        return stuck

    stuck = client.portal.call(run)
    assert stuck.closed == 1011
    assert not app.state.clients


def test_unencodable_session_skipped_others_sent(client, app, monkeypatch):
    real = app.state.store.session_dict

    def fake(sid):
        d = real(sid)
        if d and sid == "bad-session-00001":
            d["x"] = float("nan")
        return d

    monkeypatch.setattr(app.state.store, "session_dict", fake)
    with connect(client) as ws:
        ws.receive_json()
        post(client, dict(ev("SessionStart"), session_id="bad-session-00001"))
        post(client, ev("SessionStart"))
        client.portal.call(app.state.broadcast_tick)
        assert [s["id"] for s in ws.receive_json()["sessions"]] == [SID]


def test_ws_binary_frame_ignored(client, app):
    with connect(client) as ws:
        ws.receive_json()
        ws.send_bytes(b"\x00\x01\xff")
        ws.send_text("ping")
        post(client, ev("SessionStart"))
        client.portal.call(app.state.broadcast_tick)
        assert ws.receive_json()["type"] == "sessions"


def test_writer_task_cleaned_up_on_disconnect(client, app):
    with connect(client) as ws:
        ws.receive_json()
        task = next(iter(app.state.clients.values())).task
    deadline = time.time() + 2
    while not task.done() and time.time() < deadline:
        time.sleep(0.01)
    assert task.done() and not app.state.clients


def test_poll_runs_before_pop_changed(client, app, monkeypatch):
    order = []
    store = app.state.store
    monkeypatch.setattr(app.state.watcher, "poll", lambda: order.append("poll") or set())
    real = store.pop_changed
    monkeypatch.setattr(store, "pop_changed", lambda: order.append("pop") or real())
    client.portal.call(app.state.broadcast_tick)
    assert order == ["poll", "pop"]


def test_oversized_hook_ignored(client, app, monkeypatch):
    monkeypatch.setattr(server_mod, "MAX_HOOK_BYTES", 1000)
    big = json.dumps(ev("SessionStart", pad="x" * 5000))
    assert_empty_200(client.post("/hook", content=big))
    # chunked upload without a Content-Length is capped too
    assert_empty_200(client.post("/hook", content=iter([big.encode()[:2000], big.encode()[2000:]])))
    assert client.get("/api/sessions").json() == []
    assert_empty_200(post(client, ev("SessionStart")))
    assert len(client.get("/api/sessions").json()) == 1


def test_guard_403_empty_body(app):
    with TestClient(app, base_url="http://evil.com") as c:
        r = c.get("/health", headers={"Host": "evil.com"})
        assert r.status_code == 403 and r.content == b""
    with TestClient(app, base_url=BASE) as c:
        r = c.post("/hook", content=b"{}", headers={"Origin": "http://evil.com"})
        assert r.status_code == 403 and r.content == b""


# ---- housekeeping ----------------------------------------------------------


def shutdown_flag(app):
    calls = []
    app.state.request_shutdown = lambda: calls.append(1)
    return calls


def test_idle_shutdown_after_period(make, clock):
    app = make(idle_minutes=1)
    calls = shutdown_flag(app)
    app.state.housekeeping_tick()
    clock.advance(59)
    app.state.housekeeping_tick()
    assert calls == []
    clock.advance(2)
    app.state.housekeeping_tick()
    assert calls == [1]


def test_idle_not_with_active_session(make, clock):
    app = make(idle_minutes=1, stale_minutes=1000)
    calls = shutdown_flag(app)
    app.state.store.ingest(ev("SessionStart"))
    for _ in range(5):
        clock.advance(60)
        app.state.housekeeping_tick()
    assert calls == []
    # once the session ends, the idle clock starts from then
    app.state.store.ingest(ev("SessionEnd"))
    app.state.housekeeping_tick()
    assert calls == []
    clock.advance(61)
    app.state.housekeeping_tick()
    assert calls == [1]


def test_idle_not_with_client(make, clock):
    app = make(idle_minutes=1)
    calls = shutdown_flag(app)
    with TestClient(app, base_url=BASE) as c:
        with connect(c) as ws:
            ws.receive_json()
            clock.advance(600)
            app.state.housekeeping_tick()
            assert calls == []


def test_idle_zero_never(make, clock):
    app = make(idle_minutes=0)
    calls = shutdown_flag(app)
    for _ in range(5):
        clock.advance(100_000)
        app.state.housekeeping_tick()
    assert calls == []


def test_housekeeping_expires_stale(make, clock):
    app = make(stale_minutes=1, idle_minutes=0)
    app.state.store.ingest(ev("SessionStart"))
    app.state.store.ingest(pre())
    clock.advance(61)
    app.state.housekeeping_tick()
    s = app.state.store.snapshot()[0]
    assert s["status"] == "ended"


# ---- relay -----------------------------------------------------------------


def test_relay_unset_no_queue_no_httpx(make, monkeypatch):
    import httpx

    def forbid(*a, **k):
        raise AssertionError("httpx client constructed")

    monkeypatch.setattr(httpx, "AsyncClient", forbid)
    app = make()
    assert app.state.relay is None
    with TestClient(app, base_url=BASE) as c:
        assert_empty_200(post(c, ev("SessionStart")))


def test_relay_enqueues_redacted(make):
    app = make(upstream_url="http://upstream.invalid/ingest", upstream_token="tok")
    with TestClient(app, base_url=BASE) as c:
        post(c, pre(command="echo " + SECRET))
    q = app.state.relay.queue
    assert q.qsize() == 1
    item = q.get_nowait()
    assert SECRET not in json.dumps(item)
    assert item["machine_id"] == app.state.relay.machine_id
    assert len(item["machine_id"]) == 12 and int(item["machine_id"], 16) >= 0
    assert isinstance(item["user"], str)
    assert item["hook_event_name"] == "PreToolUse"


def test_relay_full_queue_drops(tmp_path, clock):
    app = create_app(
        Config(data_dir=tmp_path, upstream_url="http://upstream.invalid/"),
        clock=clock,
        start_loops=False,
        relay_maxsize=2,
    )
    with TestClient(app, base_url=BASE) as c:
        for i in range(5):
            assert_empty_200(post(c, pre(call_id=f"t{i}")))
    assert app.state.relay.queue.qsize() == 2
    assert app.state.relay.dropped == 3


def test_relay_send_one_sets_bearer_and_swallows_errors(make):
    import asyncio

    app = make(upstream_url="http://upstream.invalid/", upstream_token="tok")
    seen = {}

    class Fake:
        async def post(self, url, json=None, headers=None):
            seen.update(url=url, headers=headers)
            raise RuntimeError("down")

    asyncio.run(app.state.relay.send_one(Fake(), {"a": 1}))
    assert seen["headers"] == {"Authorization": "Bearer tok"}


# ---- misc ------------------------------------------------------------------


def test_nan_never_on_wire():
    assert dumps({"x": float("nan")}) is None
    assert dumps({"x": float("inf")}) is None
    assert dumps({"x": "é"}) == '{"x": "é"}'


def test_nan_hook_does_not_crash(client):
    body = b'{"session_id": "s-nan-0000001", "hook_event_name": "PreToolUse", "tool_name": "X", "tool_input": {"v": NaN}}'
    assert_empty_200(client.post("/hook", content=body))
    assert client.get("/api/sessions").status_code == 200
    with connect(client) as ws:
        assert ws.receive_json()["type"] == "snapshot"


def test_root_placeholder_or_index(client):
    r = client.get("/")
    assert r.status_code == 200
    assert "<!doctype" in r.text.lower()


# ---- subprocess smoke test -------------------------------------------------


def _free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_python_dash_m_smoke(tmp_path):
    try:
        port = _free_port()
    except OSError:
        pytest.skip("cannot bind a local port")
    env = dict(os.environ, CCMC_PORT=str(port), CCMC_DATA_DIR=str(tmp_path), CCMC_NO_BROWSER="1")
    for k in ("CCMC_UPSTREAM_URL", "CCMC_IDLE_MINUTES", "CCMC_DEV_ORIGINS"):
        env.pop(k, None)
    proc = subprocess.Popen([sys.executable, "-m", "cc_mission_control"], env=env, stderr=subprocess.PIPE)
    base = f"http://127.0.0.1:{port}"
    try:
        deadline = time.time() + 10
        health = None
        while time.time() < deadline:
            if proc.poll() is not None:
                pytest.fail("server exited early: " + proc.stderr.read().decode())
            try:
                with urllib.request.urlopen(base + "/health", timeout=1) as r:
                    health = json.loads(r.read())
                break
            except OSError:
                time.sleep(0.1)
        assert health and health["app"] == "cc-mission-control"
        req = urllib.request.Request(base + "/hook", data=json.dumps(ev("SessionStart")).encode(), method="POST")
        with urllib.request.urlopen(req, timeout=2) as r:
            assert r.status == 200 and r.read() == b""
        with urllib.request.urlopen(base + "/api/sessions", timeout=2) as r:
            assert [s["id"] for s in json.loads(r.read())] == [SID]
        pid_file = tmp_path / "server.pid"
        assert pid_file.read_text().strip() == str(proc.pid)
    finally:
        proc.terminate()
        try:
            proc.wait(timeout=10)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()
        if proc.stderr:
            proc.stderr.close()
    assert not (tmp_path / "server.pid").exists()


def _spawn(tmp_path, port):
    env = dict(os.environ, CCMC_PORT=str(port), CCMC_DATA_DIR=str(tmp_path), CCMC_NO_BROWSER="1")
    for k in ("CCMC_UPSTREAM_URL", "CCMC_IDLE_MINUTES", "CCMC_DEV_ORIGINS"):
        env.pop(k, None)
    return subprocess.Popen([sys.executable, "-m", "cc_mission_control"], env=env, stderr=subprocess.PIPE)


def _stop(proc):
    if proc.poll() is None:
        proc.terminate()
    try:
        proc.wait(timeout=10)
    except subprocess.TimeoutExpired:
        proc.kill()
        proc.wait()
    if proc.stderr:
        proc.stderr.close()


def test_second_instance_leaves_pid_file(tmp_path):
    port = _free_port()
    first = _spawn(tmp_path, port)
    second = None
    try:
        deadline = time.time() + 10
        while time.time() < deadline:
            try:
                urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=1).close()
                break
            except OSError:
                time.sleep(0.1)
        pid_file = tmp_path / "server.pid"
        assert pid_file.read_text().strip() == str(first.pid)
        second = _spawn(tmp_path, port)
        assert second.wait(timeout=10) == 1
        assert pid_file.read_text().strip() == str(first.pid)
        assert first.poll() is None
        with urllib.request.urlopen(f"http://127.0.0.1:{port}/health", timeout=2) as r:
            assert json.loads(r.read())["app"] == "cc-mission-control"
    finally:
        if second is not None:
            _stop(second)
        _stop(first)
    assert not (tmp_path / "server.pid").exists()


def test_idle_shutdown_removes_opened_marker(make, clock, tmp_path):
    marker = tmp_path / "browser.opened"
    marker.write_text("x")
    app = make(idle_minutes=1)
    calls = shutdown_flag(app)
    app.state.housekeeping_tick()
    clock.advance(30)
    app.state.housekeeping_tick()
    assert marker.exists() and calls == []  # not idle long enough yet
    clock.advance(31)
    app.state.housekeeping_tick()
    assert calls == [1] and not marker.exists()


def test_idle_shutdown_without_marker_is_fine(make, clock):
    app = make(idle_minutes=1)
    calls = shutdown_flag(app)
    app.state.housekeeping_tick()
    clock.advance(61)
    app.state.housekeeping_tick()
    assert calls == [1]


def test_lifespan_shutdown_keeps_opened_marker(make, tmp_path):
    marker = tmp_path / "browser.opened"
    marker.write_text("x")
    with TestClient(make(), base_url=BASE) as c:
        assert_empty_200(post(c, ev("SessionStart")))
    assert marker.exists()

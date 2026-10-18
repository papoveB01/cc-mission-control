"""Relay v2 (SPEC 13.1): envelopes, spool, batching, retries."""

import asyncio
import json
import os
import stat
import threading

import httpx
import pytest
from starlette.testclient import TestClient

from cc_mission_control.config import Config
from cc_mission_control.relay import Relay
from cc_mission_control.server import create_app
from cc_mission_control.state import Store
from cc_mission_control.transcript import TranscriptWatcher

SID = "sess-1234567890"
BASE = "http://127.0.0.1:4317"
URL = "http://hub.invalid/ingest"
A20 = "a1B2c3D4e5F6g7H8i9J0"
SECRET = "sk-" + "ant-api03-" + A20 + A20
TOKEN = "tok" + "-" + "xyz"


def cfg(tmp_path, **kw):
    kw.setdefault("upstream_url", URL)
    return Config(data_dir=tmp_path, **kw)


def mk_relay(tmp_path, **kw):
    config = kw.pop("config", None) or cfg(tmp_path, upstream_token=TOKEN)
    kw.setdefault("seal_age", 0)
    return Relay(config, Store(config).redactor, **kw)


class Hub:
    """Fake upstream. `status` may be an int or a callable returning one (or raising)."""

    def __init__(self, status=200):
        self.status = status
        self.requests = []

    def handler(self, request):
        self.requests.append(request)
        status = self.status() if callable(self.status) else self.status
        return httpx.Response(status)

    def client(self):
        return httpx.AsyncClient(transport=httpx.MockTransport(self.handler))


def sent_records(hub):
    return [r for req in hub.requests for r in json.loads(req.content)]


def segments(tmp_path):
    return sorted((tmp_path / "relay").glob("seg-*.jsonl"))


def fill(relay, n, kind="ctx"):
    for i in range(n):
        assert relay.emit(kind, SID, {"i": i})


def run(coro):
    return asyncio.run(coro)


async def flush_and_send(relay, hub):
    await relay.flush()
    async with hub.client() as client:
        return await relay.send_pending(client)


# ---- envelope -------------------------------------------------------------


def test_envelope_fields(tmp_path):
    relay = mk_relay(tmp_path, clock=lambda: 1760000000.5)
    assert relay.emit("title", SID, {"source": "custom", "title": "x"})
    env = relay.queue[0]
    assert set(env) == {"v", "eid", "machine_id", "user", "host", "t", "kind", "session_id", "data"}
    assert env["v"] == 2 and env["t"] == 1760000000.5 and env["kind"] == "title"
    assert env["session_id"] == SID and len(env["eid"]) == 32
    assert env["machine_id"] == relay.machine_id and env["host"] == relay.host
    assert env["data"] == {"source": "custom", "title": "x"}


def test_secrets_redacted_in_envelope(tmp_path):
    relay = mk_relay(tmp_path)
    relay.emit("hook", SID, {"tool_input": {"command": "echo " + SECRET}})
    relay.emit("title", SID, {"source": "generated", "title": "fix " + SECRET})
    assert SECRET not in json.dumps(list(relay.queue))


# ---- /hook ----------------------------------------------------------------


def hook_ev(name="SessionStart", **kw):
    return {"session_id": SID, "transcript_path": "", "cwd": "/work/demo", "hook_event_name": name, **kw}


def test_hook_enqueues_envelope_only_when_accepted(tmp_path):
    app = create_app(cfg(tmp_path), start_loops=False)
    with TestClient(app, base_url=BASE) as c:
        r = c.post("/hook", content=json.dumps(hook_ev(prompt=SECRET)))
        assert r.status_code == 200 and r.content == b""
        queued = list(app.state.relay.queue)
        assert len(queued) == 1
        env = queued[0]
        assert env["kind"] == "hook" and env["session_id"] == SID
        assert env["data"]["hook_event_name"] == "SessionStart"
        assert SECRET not in json.dumps(env)
        for body in (b"not json", b"[1]", json.dumps({"hook_event_name": "Stop"}).encode()):
            r = c.post("/hook", content=body)
            assert r.status_code == 200 and r.content == b""
        assert len(app.state.relay.queue) == 1


# ---- watcher emits --------------------------------------------------------


def line(obj):
    return json.dumps(obj) + "\n"


def test_watcher_emits_records(tmp_path):
    store = Store(Config.from_env({}))
    got = []
    w = TranscriptWatcher(store, search_interval=0, emit=lambda *a: got.append(a))
    root = tmp_path / "proj"
    main = root / (SID + ".jsonl")
    base = {"session_id": SID, "transcript_path": str(main), "cwd": "/x/proj/sub"}
    store.ingest({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t"})
    store.ingest({**base, "hook_event_name": "SubagentStart", "agent_id": "abc123", "agent_type": "Explore"})
    usage = {"input_tokens": 1, "cache_creation_input_tokens": 2, "cache_read_input_tokens": 3, "output_tokens": 4}
    root.mkdir()
    main.write_text(
        line({"type": "user", "cwd": "/x/proj"})
        + line({"type": "ai-title", "aiTitle": "Gen"})
        + line({"type": "custom-title", "customTitle": "Mine"})
        + line({"type": "assistant", "uuid": "u1", "message": {"id": "msg_1", "model": "claude-sonnet-5", "usage": usage}})
        + line({"type": "assistant", "uuid": "u2", "message": {"model": "claude-sonnet-5", "usage": {"output_tokens": 9}}})
    )
    meta = root / SID / "subagents" / "agent-abc123.meta.json"
    meta.parent.mkdir(parents=True)
    meta.write_text(json.dumps({"description": "d", "toolUseId": "toolu_9", "agentType": "Explore"}))
    w.poll()
    by_kind = {}
    for kind, sid, data in got:
        assert sid == SID
        by_kind.setdefault(kind, []).append(data)
    assert by_kind["cwd"] == [{"cwd": "/x/proj"}]
    assert {"source": "generated", "title": "Gen"} in by_kind["title"]
    assert {"source": "custom", "title": "Mine"} in by_kind["title"]
    assert by_kind["usage"][0] == {
        "agent_id": "main", "message_id": "msg_1", "model": "claude-sonnet-5",
        "input": 1, "cache_creation": 2, "cache_read": 3, "output": 4,
    }
    assert by_kind["usage"][1]["message_id"] == "u2" and by_kind["usage"][1]["input"] == 0
    assert by_kind["ctx"] == [{"agent_id": "main", "tokens": 9, "model": "claude-sonnet-5"}]
    assert by_kind["meta"] == [{"agent_id": "abc123", "agent_type": "Explore", "description": "d", "tool_use_id": "toolu_9"}]


def test_watcher_without_emit_is_unchanged(tmp_path):
    store = Store(Config.from_env({}))
    w = TranscriptWatcher(store)
    assert w.emit is None
    main = tmp_path / (SID + ".jsonl")
    store.ingest({"session_id": SID, "transcript_path": str(main), "cwd": "/a", "hook_event_name": "SessionStart"})
    main.write_text(line({"type": "ai-title", "aiTitle": "T"}))
    assert w.poll() == {SID}


def test_create_app_wires_watcher_only_when_enabled(tmp_path):
    assert create_app(cfg(tmp_path), start_loops=False).state.watcher.emit is not None
    off = create_app(Config(data_dir=tmp_path / "off"), start_loops=False)
    assert off.state.watcher.emit is None and off.state.relay is None


# ---- spool and sender -----------------------------------------------------


def test_spool_send_batches_and_delete(tmp_path):
    relay = mk_relay(tmp_path, batch_records=200)
    hub = Hub()
    fill(relay, 450)
    assert run(flush_and_send(relay, hub)) is True
    sizes = [len(json.loads(r.content)) for r in hub.requests]
    assert sizes == [200, 200, 50]
    assert all(r.headers["authorization"] == "Bearer " + TOKEN for r in hub.requests)
    assert str(hub.requests[0].url) == URL
    assert len({e["eid"] for e in sent_records(hub)}) == 450
    assert segments(tmp_path) == [] and relay.stats()["spooled_bytes"] == 0
    assert relay.stats()["last_ok"] is not None and relay.stats()["last_error"] is None


def test_batch_byte_limit_and_no_auth_header_without_token(tmp_path):
    relay = mk_relay(tmp_path, config=cfg(tmp_path), batch_bytes=1500)
    hub = Hub()
    fill(relay, 20)
    run(flush_and_send(relay, hub))
    assert len(hub.requests) > 1 and len(sent_records(hub)) == 20
    assert all("authorization" not in r.headers for r in hub.requests)


def test_segments_rotate_and_send_oldest_first(tmp_path):
    relay = mk_relay(tmp_path, segment_bytes=2000)
    hub = Hub()
    fill(relay, 30)
    run(relay.flush())
    assert len(segments(tmp_path)) > 2
    assert not any(p.stat().st_size > 2000 + 600 for p in segments(tmp_path))
    run(flush_and_send(relay, hub))
    assert [r["data"]["i"] for r in sent_records(hub)] == list(range(30))
    assert segments(tmp_path) == []


@pytest.mark.parametrize("failure", ["500", "raise"])
def test_failure_keeps_records_backs_off_then_recovers(tmp_path, failure):
    relay = mk_relay(tmp_path, backoff_min=0.01, backoff_max=0.04)
    state = {"down": True}

    def status():
        if state["down"]:
            if failure == "raise":
                raise httpx.ConnectError("down")
            return 500
        return 200

    hub = Hub(status)
    delays = []

    async def sleep(d):
        delays.append(d)
        if len(delays) == 4:
            state["down"] = False
        await asyncio.sleep(0.001)

    relay.sleep = sleep
    relay.client_factory = hub.client
    fill(relay, 5)

    async def scenario():
        await relay.flush()
        task = asyncio.create_task(relay.run_sender())
        for _ in range(200):
            await asyncio.sleep(0.001)
            if relay.last_ok is not None and relay.stats()["spooled_bytes"] == 0:
                break
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task

    run(scenario())
    assert delays[:3] == [0.01, 0.02, 0.04] and max(delays[:4]) == 0.04
    assert relay.last_ok is not None and relay.last_error is None
    assert segments(tmp_path) == []
    assert len({e["eid"] for e in sent_records(hub)}) == 5


def test_failure_reports_safe_error_and_keeps_segment(tmp_path):
    relay = mk_relay(tmp_path)
    hub = Hub(503)
    fill(relay, 3)
    assert run(flush_and_send(relay, hub)) is False
    assert relay.stats()["last_error"] == "HTTP 503"
    assert len(segments(tmp_path)) == 1 and relay.stats()["spooled_bytes"] > 0

    def boom():
        raise httpx.ConnectError(URL + TOKEN)

    assert run(flush_and_send(relay, Hub(boom))) is False
    err = relay.stats()["last_error"]
    assert err == "ConnectError" and URL not in err and TOKEN not in err


def test_partial_failure_resends_only_unsent_batches(tmp_path):
    relay = mk_relay(tmp_path, batch_records=2)
    calls = {"n": 0}

    def status():
        calls["n"] += 1
        return 500 if calls["n"] == 2 else 200

    hub = Hub(status)
    fill(relay, 6)

    async def scenario():
        await relay.flush()
        async with hub.client() as client:
            assert await relay.send_pending(client) is False
            assert await relay.send_pending(client) is True

    run(scenario())
    ok = [json.loads(r.content) for i, r in enumerate(hub.requests) if i != 1]
    assert [[e["data"]["i"] for e in b] for b in ok] == [[0, 1], [2, 3], [4, 5]]
    assert segments(tmp_path) == []


def test_restart_sends_old_segments(tmp_path):
    old = mk_relay(tmp_path)
    fill(old, 4)
    old.flush_sync()
    assert len(segments(tmp_path)) == 1
    new = mk_relay(tmp_path)
    assert new.stats()["spooled_bytes"] > 0
    hub = Hub()

    async def scenario():
        async with hub.client() as client:
            await new.send_pending(client)

    run(scenario())
    assert len(sent_records(hub)) == 4 and segments(tmp_path) == []


def test_spool_cap_drops_oldest_and_counts(tmp_path):
    relay = mk_relay(tmp_path, segment_bytes=1000, spool_bytes=3000)
    fill(relay, 60)
    run(relay.flush())
    total = sum(p.stat().st_size for p in segments(tmp_path))
    assert total <= 3000 and relay.stats()["spooled_bytes"] == total
    kept = sum(p.read_bytes().count(b"\n") for p in segments(tmp_path))
    assert relay.dropped > 0 and relay.dropped + kept == 60
    first = json.loads(segments(tmp_path)[0].read_text().splitlines()[0])
    assert first["data"]["i"] == relay.dropped  # the oldest went first


def test_full_queue_counts_dropped(tmp_path):
    relay = mk_relay(tmp_path, maxsize=3)
    fill(relay, 3)
    assert relay.emit("ctx", SID, {}) is False
    assert relay.stats()["dropped"] == 1 and relay.stats()["queued"] == 3


def test_shutdown_flushes_queue_to_disk(tmp_path):
    app = create_app(cfg(tmp_path), start_loops=False)
    with TestClient(app, base_url=BASE) as c:
        c.post("/hook", content=json.dumps(hook_ev()))
        assert segments(tmp_path) == []
    files = segments(tmp_path)
    assert len(files) == 1
    assert json.loads(files[0].read_text().splitlines()[0])["kind"] == "hook"
    assert len(app.state.relay.queue) == 0


def test_health_relay_block_and_disabled(tmp_path):
    on = create_app(cfg(tmp_path / "on"), start_loops=False)
    off = create_app(Config(data_dir=tmp_path / "off"), start_loops=False)
    with TestClient(on, base_url=BASE) as c:
        block = c.get("/health").json()["relay"]
    assert block == {"queued": 0, "spooled_bytes": 0, "dropped": 0, "last_ok": None, "last_error": None}
    with TestClient(off, base_url=BASE) as c:
        assert "relay" not in c.get("/health").json()
    assert not (tmp_path / "off").exists()


def test_ingestion_does_not_wait_for_network(tmp_path):
    seed = mk_relay(tmp_path)
    fill(seed, 2)
    seed.flush_sync()  # a spooled segment makes the sender start a post that never completes
    started = threading.Event()

    class Hanging:
        async def __aenter__(self):
            return self

        async def __aexit__(self, *a):
            return False

        async def post(self, *a, **k):
            started.set()
            await asyncio.Event().wait()  # never set

    app = create_app(
        cfg(tmp_path),
        relay_options={"client_factory": Hanging, "flush_interval": 1000, "send_interval": 0.01, "seal_age": 0},
    )
    with TestClient(app, base_url=BASE) as c:
        assert started.wait(30), "sender never reached the hanging post"
        r = c.post("/hook", content=json.dumps(hook_ev()))
        assert r.status_code == 200 and r.content == b""
        assert [e["kind"] for e in app.state.relay.queue] == ["hook"]


def test_stats_safe_during_appends_and_rotation(tmp_path):
    relay = mk_relay(tmp_path, segment_bytes=500, spool_bytes=2000)
    errors, stop = [], threading.Event()

    def writer():
        try:
            for _ in range(30):
                fill(relay, 5)
                relay._append(relay._drain())
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
        finally:
            stop.set()

    t = threading.Thread(target=writer)
    t.start()
    while not stop.is_set():
        try:
            relay.stats()
        except Exception as exc:  # pragma: no cover
            errors.append(exc)
            break
    t.join()
    assert not errors
    assert relay.stats()["spooled_bytes"] == sum(p.stat().st_size for p in segments(tmp_path))


def test_corrupt_line_in_middle_does_not_wedge(tmp_path):
    relay = mk_relay(tmp_path)
    fill(relay, 2)
    run(relay.flush())
    seg = segments(tmp_path)[0]
    good = seg.read_bytes().splitlines(keepends=True)
    seg.write_bytes(good[0] + b'{"v": 2, "torn\n' + b"not json\n" + good[1])
    relay._spooled += seg.stat().st_size - relay._sizes[str(seg)]
    relay._sizes[str(seg)] = seg.stat().st_size
    hub = Hub()

    async def scenario():
        async with hub.client() as client:
            return await relay.send_pending(client)

    assert run(scenario()) is True
    assert [r["data"]["i"] for r in sent_records(hub)] == [0, 1]
    assert relay.dropped == 2 and segments(tmp_path) == []


def test_write_error_seals_segment_and_continues(tmp_path, monkeypatch):
    relay = mk_relay(tmp_path)
    fill(relay, 2)
    run(relay.flush())
    first = relay._cur
    real, calls = Relay._write, {"n": 0}

    def flaky(path, data):
        calls["n"] += 1
        if calls["n"] == 1:
            with open(path, "ab") as f:
                f.write(data[:10])  # torn
            raise OSError("disk")
        real(path, data)

    monkeypatch.setattr(Relay, "_write", staticmethod(flaky))
    fill(relay, 2)
    run(relay.flush())
    assert relay._cur not in (None, first) and relay.last_error == "OSError"
    assert relay.stats()["spooled_bytes"] == sum(p.stat().st_size for p in segments(tmp_path))
    hub = Hub()
    run(flush_and_send(relay, hub))
    assert len(sent_records(hub)) == 4 and relay.dropped == 0  # the torn tail was rewritten in the new segment


def test_poison_batch_dropped_but_transient_codes_retry(tmp_path):
    relay = mk_relay(tmp_path, batch_records=2)
    codes = iter([200, 400, 200])
    hub = Hub(lambda: next(codes))
    fill(relay, 6)
    assert run(flush_and_send(relay, hub)) is True
    assert relay.dropped == 2 and segments(tmp_path) == []
    assert len(hub.requests) == 3
    for code in (401, 403, 408, 429, 500, 503):
        r = mk_relay(tmp_path / str(code))
        fill(r, 1)
        assert run(flush_and_send(r, Hub(code))) is False
        assert r.dropped == 0 and r.last_error == f"HTTP {code}"
        assert len(segments(tmp_path / str(code))) == 1


def test_spool_permissions(tmp_path):
    (tmp_path / "relay").mkdir()
    os.chmod(tmp_path / "relay", 0o755)
    relay = mk_relay(tmp_path)
    fill(relay, 1)
    run(relay.flush())
    assert stat.S_IMODE(os.stat(tmp_path / "relay").st_mode) == 0o700
    assert stat.S_IMODE(os.stat(segments(tmp_path)[0]).st_mode) == 0o600


def test_cap_never_deletes_segment_being_sent(tmp_path):
    relay = mk_relay(tmp_path, segment_bytes=500, spool_bytes=10**9)
    fill(relay, 20)
    run(relay.flush())
    oldest = sorted(relay._sizes)[0]
    relay._send_path = oldest
    relay.cap_bytes = 100
    relay._enforce_cap()
    assert oldest in relay._sizes and os.path.exists(oldest)


def test_seal_waits_for_age_or_size(tmp_path):
    now = {"t": 0.0}
    relay = mk_relay(tmp_path, seal_age=5.0, monotonic=lambda: now["t"], segment_bytes=10**6)
    fill(relay, 2)
    run(relay.flush())
    assert relay._next_segment() is None
    now["t"] = 5.0
    assert relay._next_segment() == str(segments(tmp_path)[0])
    small = mk_relay(tmp_path / "s", seal_age=1000.0, segment_bytes=100)
    fill(small, 3)
    run(small.flush())
    assert small._next_segment() is not None  # full segments seal regardless of age


def test_late_emit_after_shutdown_flush_is_written(tmp_path):
    relay = mk_relay(tmp_path)
    fill(relay, 1)
    relay.flush_sync()
    assert relay.emit("ctx", SID, {"late": True})
    lines = [ln for p in segments(tmp_path) for ln in p.read_text().splitlines()]
    assert len(lines) == 2 and len(relay.queue) == 0


def test_queue_byte_bound_counts_dropped(tmp_path):
    relay = mk_relay(tmp_path, queue_bytes=700)
    results = [relay.emit("ctx", SID, {"x": "y" * 200}) for _ in range(5)]
    assert results.count(True) < 5
    assert relay.dropped == results.count(False)


def test_dropped_counter_is_thread_safe(tmp_path):
    relay = mk_relay(tmp_path)
    threads = [threading.Thread(target=lambda: [relay._drop(1) for _ in range(2000)]) for _ in range(8)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert relay.dropped == 16000

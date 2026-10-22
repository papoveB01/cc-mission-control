import asyncio
import json
import logging

import httpx
from starlette.testclient import TestClient

from cc_mission_control import summarize
from cc_mission_control.config import Config
from cc_mission_control.redact import MASK
from cc_mission_control.server import create_app
from cc_mission_control.state import Store
from test_hub_common import AUTH, MACHINE, env, hook, make_client, post
from test_server import BASE, SID, clock, ev, post as hook_post, pre  # noqa: F401 (fixtures)

KEY = "sk-" + "ant-" + "test-key-" + "0123456789abcdef"
A20 = "a1B2c3D4e5F6g7H8i9J0"
FAKE_SECRET = "sk-" + "ant-api03-" + A20 + A20


def key_file(tmp_path, mode=0o600, text=KEY + "\n"):
    p = tmp_path / "summary_api_key"
    p.write_text(text)
    p.chmod(mode)
    return p


class Api:
    """MockTransport handler that records requests and replays scripted responses."""

    def __init__(self, *responses):
        self.requests = []
        self.responses = list(responses)

    def __call__(self, request: httpx.Request) -> httpx.Response:
        self.requests.append(request)
        r = self.responses.pop(0) if len(self.responses) > 1 else self.responses[0]
        if isinstance(r, Exception):
            raise r
        return r


def ok(text="Fixed the parser bug and the tests pass."):
    return httpx.Response(200, json={"content": [{"type": "text", "text": text}]})


def build(tmp_path, clock, api, **cfg):
    cfg.setdefault("summaries", True)
    cfg.setdefault("data_dir", tmp_path)
    return create_app(
        Config(**cfg), clock=clock, start_loops=False,
        summary_options={"transport": httpx.MockTransport(api), "retry_delay": 0},
    )


_turns = [0]


def turn(c, n=3, final="All done."):
    _turns[0] += 1
    hook_post(c, ev("SessionStart", source="startup"))
    hook_post(c, ev("UserPromptSubmit", prompt="fix the parser"))
    for i in range(n):
        cid = f"t{_turns[0]}_{i}"
        hook_post(c, pre(call_id=cid, command=f"pytest -q tests/t{i}"))
        hook_post(c, ev("PostToolUse", tool_name="Bash", tool_use_id=cid, tool_input={"command": "pytest"},
                        tool_response={"stdout": "3 passed in 1s"}))
    hook_post(c, ev("Stop", last_assistant_message=final))


def rows(app, kind="summary"):
    return [r for r in app.state.store.session_dict(SID)["activity"] if r["kind"] == kind]


def drain(app):
    asyncio.run(app.state.summarizer.drain())


def test_disabled_without_flag(tmp_path, clock):
    key_file(tmp_path)
    app = create_app(Config(data_dir=tmp_path), clock=clock, start_loops=False)
    assert app.state.summarizer is None and app.state.store.summary_sink is None
    with TestClient(app, base_url=BASE) as c:
        assert "summaries" not in c.get("/health").json()


def test_disabled_without_key_file_logs_once(tmp_path, clock, caplog):
    with caplog.at_level(logging.WARNING):
        app = build(tmp_path, clock, Api(ok()))
    assert app.state.summarizer is None
    assert len([r for r in caplog.records if "summaries disabled" in r.getMessage()]) == 1


def test_loose_permissions_rejected(tmp_path, clock, caplog):
    key_file(tmp_path, mode=0o644)
    with caplog.at_level(logging.WARNING):
        app = build(tmp_path, clock, Api(ok()))
    assert app.state.summarizer is None
    assert KEY not in caplog.text and "chmod 600" in caplog.text


def test_custom_key_file_path(tmp_path, clock):
    other = tmp_path / "elsewhere"
    other.write_text(KEY)
    other.chmod(0o600)
    app = build(tmp_path, clock, Api(ok()), summary_key_file=other)
    assert app.state.summarizer is not None


def test_enabled_sends_request_adds_row_and_relays(tmp_path, clock):
    key_file(tmp_path)
    api = Api(ok())
    app = build(tmp_path, clock, api, summary_model="claude-test-model",
                upstream_url="http://upstream.invalid/ingest", upstream_token="tok")
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        assert rows(app) == []  # nothing on the ingestion path
        assert c.get("/health").json()["summaries"]["sent"] == 0
        drain(app)
        (req,) = api.requests
        assert str(req.url) == "https://api.anthropic.com/v1/messages"
        assert req.headers["x-api-key"] == KEY
        assert req.headers["anthropic-version"] == "2023-06-01"
        assert req.headers["content-type"] == "application/json"
        body = json.loads(req.content)
        assert body["model"] == "claude-test-model" and body["max_tokens"] == 200
        assert "past tense" in body["system"] and body["messages"][0]["role"] == "user"
        content = body["messages"][0]["content"]
        assert "fix the parser" in content and "All done." in content
        assert content.count("Ran the test suite") >= 3
        (row,) = rows(app)
        assert row["status"] == "info" and row["agent_id"] == "main"
        assert row["text"] == row["plain"] == "Fixed the parser bug and the tests pass."
        assert len(row["span"]) == 2 and row["span"][0] <= row["span"][1]
        sent = [i for i in app.state.relay.queue if i["kind"] == "summary"]
        assert len(sent) == 1
        assert sent[0]["data"]["text"] == row["text"] and sent[0]["data"]["model"] == "claude-test-model"
        assert set(sent[0]["data"]) == {"agent_id", "t_start", "t_end", "text", "model"}
        health = c.get("/health").json()
        assert health["summaries"] == {"enabled": True, "sent": 1, "failed": 0, "skipped": 0, "last_error": None}
        assert KEY not in json.dumps(health)


def test_short_turn_skipped(tmp_path, clock):
    key_file(tmp_path)
    api = Api(ok())
    app = build(tmp_path, clock, api)
    with TestClient(app, base_url=BASE) as c:
        turn(c, n=2)
        drain(app)
    assert api.requests == [] and rows(app) == []


def test_subagent_stop_summarized(tmp_path, clock):
    key_file(tmp_path)
    api = Api(ok("The helper listed the files."))
    app = build(tmp_path, clock, api)
    with TestClient(app, base_url=BASE) as c:
        hook_post(c, ev("SessionStart", source="startup"))
        hook_post(c, ev("SubagentStart", agent_id="ag1", agent_type="Explore"))
        hook_post(c, dict(pre(call_id="s1", command="ls"), agent_id="ag1", agent_type="Explore"))
        hook_post(c, ev("SubagentStop", agent_id="ag1", agent_type="Explore", last_assistant_message="found 3 files"))
        drain(app)
    (row,) = rows(app)
    assert row["agent_id"] == "ag1"
    assert "found 3 files" in json.loads(api.requests[0].content)["messages"][0]["content"]


def test_retry_on_429_then_success(tmp_path, clock):
    key_file(tmp_path)
    api = Api(httpx.Response(429), ok())
    app = build(tmp_path, clock, api)
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        drain(app)
    assert len(api.requests) == 2 and len(rows(app)) == 1
    assert app.state.summarizer.sent == 1 and app.state.summarizer.failed == 0


def test_retry_on_transport_error(tmp_path, clock):
    key_file(tmp_path)
    api = Api(httpx.ConnectError("boom"), ok())
    app = build(tmp_path, clock, api)
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        drain(app)
    assert len(api.requests) == 2 and len(rows(app)) == 1


def test_failure_counted_without_row_and_without_body(tmp_path, clock):
    key_file(tmp_path)
    api = Api(httpx.Response(500, text="secret-body " + KEY))
    app = build(tmp_path, clock, api)
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        drain(app)
        health = c.get("/health").json()["summaries"]
    assert rows(app) == [] and len(api.requests) == 2
    assert health["failed"] == 1 and health["sent"] == 0 and health["last_error"] == "HTTP 500"


def test_bad_response_shape_fails(tmp_path, clock):
    key_file(tmp_path)
    app = build(tmp_path, clock, Api(httpx.Response(200, json={"content": []})))
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        drain(app)
    assert app.state.summarizer.failed == 1 and rows(app) == []


def test_rate_cap_and_rolling_window(tmp_path, clock):
    key_file(tmp_path)
    api = Api(ok())
    app = build(tmp_path, clock, api, summary_max_per_hour=2)
    sm = app.state.summarizer
    with TestClient(app, base_url=BASE) as c:
        for _ in range(3):
            turn(c)
        drain(app)
        assert len(api.requests) == 2 and sm.skipped == 1
        clock.advance(3601)
        turn(c)
        drain(app)
    assert len(api.requests) == 3


def test_queue_overflow_drops_oldest(tmp_path, clock):
    key_file(tmp_path)
    app = build(tmp_path, clock, Api(ok()))
    sm = app.state.summarizer
    for i in range(summarize.QUEUE_MAX + 3):
        sm.submit(summarize.SummaryRequest(SID, "main", 1.0, 2.0, f"p{i}", [], ""))
    assert len(sm._queue) == summarize.QUEUE_MAX and sm.skipped == 3
    assert sm._queue[0].prompt == "p3"


def test_input_limits_and_redaction(tmp_path, clock):
    key_file(tmp_path)
    app = build(tmp_path, clock, Api(ok()))
    redactor = app.state.store.redactor
    req = summarize.SummaryRequest(
        SID, "main", 1.0, 2.0, "p" * 3000,
        [f"line {i} " + "x" * 200 for i in range(100)] + ["token " + FAKE_SECRET], "r" * 3000,
    )
    content = summarize.build_content(req, redactor)
    activity = content.split("Activity:\n")[1].split("\n\nFinal result:")[0]
    assert len(activity.splitlines()) <= 60 and len(activity) <= 6000
    assert FAKE_SECRET not in content and "line 0 " not in content  # oldest dropped
    assert content.split("\n\n")[0].count("p") <= 1000
    assert content.endswith("r" * 1000)


def test_output_redacted_and_truncated(tmp_path, clock):
    key_file(tmp_path)
    api = Api(ok("Used " + FAKE_SECRET + " " + "z" * 600))
    app = build(tmp_path, clock, api)
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        drain(app)
    (row,) = rows(app)
    assert FAKE_SECRET not in row["text"] and MASK in row["text"] and len(row["text"]) <= 300


def test_add_summary_ignores_unknown_session():
    store = Store(Config(), clock=lambda: 5.0)
    assert store.add_summary("nope", "main", 1, 2, "text", "m") is None


def _store_with_session():
    store = Store(Config(), clock=lambda: 5.0)
    store.ingest({"session_id": "s1", "cwd": "/w", "hook_event_name": "SessionStart", "source": "startup"})
    return store


def test_summary_clip_is_300_not_200(tmp_path, clock):
    key_file(tmp_path)
    for reply, want in (("a" * 290, 290), ("b" * 400, 300)):
        app = build(tmp_path, clock, Api(ok(reply)))
        with TestClient(app, base_url=BASE) as c:
            turn(c)
            drain(app)
        assert len(rows(app)[-1]["text"]) == want


def test_add_summary_is_defensive():
    store = _store_with_session()
    huge = 10**400
    assert store.add_summary("s1", "main", huge, float("nan"), "ok text", "m") == "ok text"
    assert store.add_summary("s1", 5, True, None, "x" * 500, ["bad"]) == "x" * 300
    for bad in (None, 5, "", "   "):
        assert store.add_summary("s1", "main", 1, 2, bad, "m") is None
    spans = [r["span"] for r in store.session_dict("s1")["activity"] if r["kind"] == "summary"]
    assert spans[0] == [5.0, 5.0] and all(isinstance(v, float) for sp in spans for v in sp)


def test_hub_rejects_malformed_summary_records(tmp_path):
    client = make_client(tmp_path)
    sid = "hub-sess-1234"
    good = {"agent_id": "main", "t_start": 101, "t_end": 109, "text": "Did it.", "model": "m"}
    bads = [dict(good, t_start=10**400), dict(good, text=""), dict(good, text=5), dict(good, agent_id=None),
            dict(good, t_end="x"), dict(good, model=7), dict(good, t_start=True)]
    items = [env("summary", 110, d, sid=sid) for d in bads] + [env("summary", 111, good, sid=sid)]
    res = post(client, items).json()
    assert res["accepted"] == 1 and res["invalid"] == len(bads)


def test_replay_skips_a_record_that_raises(tmp_path, caplog, monkeypatch):
    from cc_mission_control.hub.replay import ReplayState

    state = ReplayState("s1", Config())
    state.apply("hook", 100, {"hook_event_name": "SessionStart", "cwd": "/w"}, "u", "h", (100, 100, 1))

    def boom(*a, **k):
        raise OverflowError("bad")

    monkeypatch.setattr(state.store, "add_summary", boom)
    with caplog.at_level(logging.WARNING):
        for i in range(3):
            state.apply("summary", 101 + i, {"text": "SECRETDATA"}, "u", "h", (101 + i, 1, i))
    state.apply("hook", 110, {"hook_event_name": "UserPromptSubmit", "prompt": "go"}, "u", "h", (110, 1, 9))
    assert len([r for r in caplog.records if "failed to replay" in r.getMessage()]) == 1
    assert "SECRETDATA" not in caplog.text
    assert state.store.session_dict("s1")["agents"][0]["task"] == "go"


def test_relay_envelope_text_is_the_clipped_row(tmp_path, clock):
    key_file(tmp_path)
    app = build(tmp_path, clock, Api(ok("c" * 450)),
                upstream_url="http://upstream.invalid/ingest", upstream_token="tok")
    with TestClient(app, base_url=BASE) as c:
        turn(c)
        drain(app)
        (item,) = [i for i in app.state.relay.queue if i["kind"] == "summary"]
    assert len(item["data"]["text"]) <= 300 and item["data"]["text"] == rows(app)[-1]["text"]


def test_key_file_rejections(tmp_path, clock, caplog):
    cases = {}
    cases["big"] = lambda p: p.write_text("k" * 600)
    cases["binary"] = lambda p: p.write_bytes(b"\xff\xfe\x00key")
    cases["space"] = lambda p: p.write_text("two words")
    cases["empty"] = lambda p: p.write_text("\n")
    for name, write in cases.items():
        d = tmp_path / name
        d.mkdir()
        p = d / "summary_api_key"
        write(p)
        p.chmod(0o600)
        with caplog.at_level(logging.WARNING):
            app = build(d, clock, Api(ok()))
        assert app.state.summarizer is None, name
    assert "kkkk" not in caplog.text
    # symlink to a world-readable file
    d = tmp_path / "link"
    d.mkdir()
    target = tmp_path / "target"
    target.write_text(KEY)
    target.chmod(0o644)
    (d / "summary_api_key").symlink_to(target)
    assert build(d, clock, Api(ok())).state.summarizer is None
    # a directory is not a regular file
    d2 = tmp_path / "dirkey"
    (d2 / "summary_api_key").mkdir(parents=True)
    assert build(d2, clock, Api(ok())).state.summarizer is None


def test_stuck_request_does_not_hang_shutdown(tmp_path, clock):
    key_file(tmp_path)

    async def stuck(request):
        await asyncio.sleep(3600)

    app = create_app(Config(summaries=True, data_dir=tmp_path), clock=clock, start_loops=False,
                     summary_options={"transport": httpx.MockTransport(stuck), "retry_delay": 0})
    sm = app.state.summarizer
    sm.submit(summarize.SummaryRequest(SID, "main", 1.0, 2.0, "p", ["l"], "r"))

    async def go():
        task = asyncio.create_task(sm.run())
        await asyncio.sleep(0.3)
        task.cancel()
        await asyncio.wait_for(asyncio.gather(task, return_exceptions=True), 2)

    asyncio.run(go())


def test_overall_deadline_counts_a_failure(tmp_path, clock, monkeypatch):
    key_file(tmp_path)

    async def stuck(request):
        await asyncio.sleep(3600)

    monkeypatch.setattr(summarize, "DEADLINE", 0.1)
    app = create_app(Config(summaries=True, data_dir=tmp_path), clock=clock, start_loops=False,
                     summary_options={"transport": httpx.MockTransport(stuck), "retry_delay": 0})
    sm = app.state.summarizer
    sm.submit(summarize.SummaryRequest(SID, "main", 1.0, 2.0, "p", ["l"], "r"))
    drain(app)
    assert sm.failed == 1 and sm.last_error == "Timeout"


def test_config_parse_and_repr_has_no_key(tmp_path):
    cfg = Config.from_env({
        "CCMC_SUMMARIES": "1", "CCMC_SUMMARY_MODEL": "m1", "CCMC_SUMMARY_MAX_PER_HOUR": "5",
        "CCMC_SUMMARY_KEY_FILE": str(tmp_path / "k"),
    })
    assert cfg.summaries and cfg.summary_model == "m1" and cfg.summary_max_per_hour == 5
    assert cfg.summary_key_file == tmp_path / "k"
    d = Config.from_env({})
    assert (d.summaries, d.summary_model, d.summary_max_per_hour, d.summary_key_file) == (False, "claude-haiku-5-5", 30, None)
    key_file(tmp_path)
    app = build(tmp_path, lambda: 1.0, Api(ok()))
    assert KEY not in repr(app.state.config) and KEY not in repr(app.state.summarizer)


def test_hub_ingest_and_replay_reproduce_row_without_network(tmp_path, monkeypatch):
    def no_network(*a, **k):
        raise AssertionError("hub replay must never call the API")

    monkeypatch.setattr(httpx.AsyncClient, "post", no_network)
    client = make_client(tmp_path)
    sid = "hub-sess-1234"
    items = [
        env("hook", 100, hook(100, "SessionStart", sid, source="startup")[2], sid=sid),
        env("hook", 101, hook(101, "UserPromptSubmit", sid, prompt="go")[2], sid=sid),
        env("summary", 110, {"agent_id": "main", "t_start": 101, "t_end": 109,
                             "text": "Did it. " + FAKE_SECRET, "model": "claude-haiku-5-5"}, sid=sid),
    ]
    assert post(client, items).json()["accepted"] == 3
    body = client.get(f"/api/history/sessions/{MACHINE}/{sid}", headers=AUTH).json()
    (row,) = [r for r in body["activity"] if r["kind"] == "summary"]
    assert row["text"].startswith("Did it.") and FAKE_SECRET not in row["text"]
    assert row["span"] == [101, 109] and row["t"] == 110 and row["status"] == "info"

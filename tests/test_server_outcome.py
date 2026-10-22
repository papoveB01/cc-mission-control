from starlette.testclient import TestClient

from test_server import BASE, assert_empty_200, clock, ev, make, post, pre  # noqa: F401 (fixtures)


def test_relayed_post_event_carries_live_outcome(make):
    app = make(upstream_url="http://upstream.invalid/ingest", upstream_token="tok")
    with TestClient(app, base_url=BASE) as c:
        post(c, pre(call_id="t1", command="pytest"))
        done = ev("PostToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": "pytest"},
                  tool_response={"stdout": "x\n" * 5000 + "== 489 passed, 1 skipped in 3s ==\n"})
        assert_empty_200(post(c, done))  # the /hook response is still an empty 200
        items = list(app.state.relay.queue)
    post_item = [i for i in items if i["data"]["hook_event_name"] == "PostToolUse"][0]
    assert post_item["data"]["ccmc_outcome"] == "489 passed, 1 skipped"

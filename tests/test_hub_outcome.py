from test_hub_common import MACHINE, SID, env, hook, make_client, post

from cc_mission_control.config import Config
from cc_mission_control.redact import Redactor
from cc_mission_control.state import Store


def replayed(client):
    svc = client.app.state.service
    return svc._with_state(MACHINE, SID, lambda st: st.store.session_dict(SID))


def test_replay_reuses_live_outcome_for_long_output(tmp_path):
    """The relay truncates strings to 4000 chars; the hub must reuse the live outcome, not recompute."""
    noise = "noise line\n" * 2000
    events = [
        dict(hook(1, "SessionStart", source="startup")[2]),
        dict(hook(2, "PreToolUse", tool_name="Bash", tool_use_id="p1", tool_input={"command": "pytest"})[2]),
        dict(hook(3, "PostToolUse", tool_name="Bash", tool_use_id="p1", tool_input={"command": "pytest"},
                  tool_response={"stdout": noise + "=== 489 passed, 1 skipped in 33.5s ===\n", "stderr": ""})[2]),
        dict(hook(4, "PreToolUse", tool_name="Bash", tool_use_id="p2", tool_input={"command": "pytest"})[2]),
        dict(hook(5, "PostToolUseFailure", tool_name="Bash", tool_use_id="p2", tool_input={"command": "pytest"},
                  error="Exit code 1\n" + noise + "=== 2 failed, 10 passed in 3s ===\n")[2]),
    ]
    now = [0.0]
    live = Store(Config(), clock=lambda: now[0])
    redactor = Redactor(max_chars=4000)
    records = []
    for i, e in enumerate(events):
        now[0] = float(i + 1)
        live.ingest(e)  # as server.py does: ingest first, then the annotated event is relayed
        records.append(env("hook", float(i + 1), redactor.value(e)))
    client = make_client(tmp_path)
    assert post(client, records).status_code == 200
    calls = live.session_dict(SID)["agents"][0]["calls"]
    assert [c["outcome"] for c in calls] == ["489 passed, 1 skipped", "2 failed, 10 passed (exit code 1)"]
    assert replayed(client) == live.session_dict(SID)

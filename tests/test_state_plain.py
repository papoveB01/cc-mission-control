import pytest

from cc_mission_control.config import Config
from cc_mission_control.redact import MASK
from cc_mission_control.state import Store

SID = "sess-1234567890"
A20 = "a1B2c3D4e5F6g7H8i9J0"
SECRET = "sk-" + "ant-api03-" + A20 + A20


@pytest.fixture
def store():
    return Store(Config(stale_minutes=60, max_calls=500), clock=lambda: 1000.0)


def ev(name, **kw):
    base = {"session_id": SID, "transcript_path": "/p/" + SID + ".jsonl", "cwd": "/work/demo", "hook_event_name": name}
    base.update(kw)
    return base


def pre(tid="t1", tool="Bash", **inp):
    return ev("PreToolUse", tool_name=tool, tool_use_id=tid, tool_input=inp)


def test_plain_and_outcome_on_calls(store):
    store.ingest(ev("SessionStart", source="startup"))
    store.ingest(pre(command="uv run --extra dev pytest -q"))
    call = store.session_dict(SID)["agents"][0]["calls"][0]
    assert call["plain"] == "Ran the test suite" and call["outcome"] == ""
    store.ingest(ev("PostToolUse", tool_name="Bash", tool_use_id="t1", tool_input={"command": "pytest"},
                    tool_response={"stdout": "== 489 passed, 1 skipped in 3s ==", "stderr": ""}))
    assert store.session_dict(SID)["agents"][0]["calls"][0]["outcome"] == "489 passed, 1 skipped"
    detail = store.call_detail(SID, "t1")
    assert detail["outcome"] == "489 passed, 1 skipped" and detail["plain"] == "Ran the test suite"


def test_tool_row_has_call_id_and_unchanged_text(store):
    store.ingest(pre(tid="r1", tool="Read", file_path="/work/demo/a.py"))
    row = store.session_dict(SID)["activity"][-1]
    assert row["kind"] == "tool" and row["call_id"] == "r1"
    assert row["text"] == "Read: /work/demo/a.py"
    assert row["plain"] == "Read a.py"


def test_failure_denied_and_other_rows(store):
    store.ingest(pre())
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="t1", tool_input={"command": "x"},
                    error="Exit code 3\nnope"))
    d = store.session_dict(SID)
    assert d["agents"][0]["calls"][0]["outcome"] == "exit code 3: nope"
    err = d["activity"][-1]
    assert err["kind"] == "error" and "call_id" not in err
    assert err["plain"] == "Ran a shell command — exit code 3: nope"
    store.ingest(pre(tid="t2", tool="Edit", file_path="/work/demo/f"))
    store.ingest(ev("PermissionDenied", tool_name="Edit", tool_use_id="t2", reason="no"))
    d = store.session_dict(SID)
    assert d["agents"][0]["calls"][1]["outcome"] == "Not allowed: no"
    assert d["activity"][-1]["plain"] == "Edit was not allowed"
    store.ingest(ev("UserPromptSubmit", prompt="hello there"))
    store.ingest(ev("Stop"))
    acts = store.session_dict(SID)["activity"]
    assert acts[-2]["plain"] == "You asked: hello there" and acts[-2]["text"] == "hello there"
    assert acts[-1]["plain"] == "Finished responding" and acts[-1]["text"] == "Turn finished"
    assert all("plain" in a for a in acts)


def test_blocked_call_outcome(store):
    store.ingest(pre())
    store.ingest(ev("Stop"))
    c = store.session_dict(SID)["agents"][0]["calls"][0]
    assert c["status"] == "error" and c["outcome"] == "Blocked or cancelled"


def test_plain_is_redacted(store):
    store.ingest(pre(tool="Glob", pattern="*" + SECRET))
    c = store.session_dict(SID)["agents"][0]["calls"][0]
    assert SECRET not in c["plain"] and MASK in c["plain"]

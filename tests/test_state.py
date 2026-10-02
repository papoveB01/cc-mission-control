import json

import pytest

from cc_mission_control.config import Config
from cc_mission_control.redact import MASK
from cc_mission_control.state import Store

SID = "sess-1234567890"
TP = "/home/u/.claude/projects/p/" + SID + ".jsonl"
A20 = "a1B2c3D4e5F6g7H8i9J0"
SECRET = "sk-" + "ant-api03-" + A20 + A20


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


@pytest.fixture
def clock():
    return Clock()


@pytest.fixture
def store(clock):
    return Store(Config(stale_minutes=60, max_calls=500), clock=clock)


def ev(name, **kw):
    base = {"session_id": SID, "transcript_path": TP, "cwd": "/work/demo", "hook_event_name": name}
    base.update(kw)
    return base


def pre(tool, tool_input, uid, **kw):
    return ev("PreToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=uid, **kw)


def post(tool, tool_input, uid, response, **kw):
    return ev("PostToolUse", tool_name=tool, tool_input=tool_input, tool_use_id=uid, tool_response=response, **kw)


def agent(store, aid, sid=SID):
    return next(a for a in store.session_dict(sid)["agents"] if a["id"] == aid)


def call(store, aid, cid):
    return next(c for c in agent(store, aid)["calls"] if c["id"] == cid)


def spawn_agent(store, uid, atype, desc, aid):
    store.ingest(pre("Agent", {"subagent_type": atype, "description": desc, "prompt": "go"}, uid))
    store.ingest(ev("SubagentStart", agent_id=aid, agent_type=atype))


# ---- session lifecycle ----------------------------------------------------

def test_lifecycle(store, clock):
    assert store.ingest(ev("SessionStart", source="startup", model="claude-sonnet-4-5")) == SID
    s = store.session_dict(SID)
    assert s["title"] == "demo" and s["status"] == "active" and s["model"] == "claude-sonnet-4-5"
    assert agent(store, "main")["status"] == "idle"

    store.ingest(ev("UserPromptSubmit", prompt="fix the build"))
    assert agent(store, "main")["status"] == "running"
    assert agent(store, "main")["task"] == "fix the build"

    clock.advance(1)
    store.ingest(pre("Bash", {"command": "npm test\nsecond line"}, "t1"))
    c = call(store, "main", "t1")
    assert c["status"] == "running" and c["summary"] == "npm test"
    clock.advance(2)
    store.ingest(post("Bash", {"command": "npm test"}, "t1", {"stdout": "ok", "stderr": ""}, duration_ms=1900))
    c = call(store, "main", "t1")
    assert c["status"] == "ok" and c["duration_ms"] == 1900 and c["ended"] == clock.t
    assert agent(store, "main")["tool_counts"] == {"Bash": 1}
    assert agent(store, "main")["total_calls"] == 1

    store.ingest(ev("Stop"))
    assert agent(store, "main")["status"] == "idle"
    kinds = [a["kind"] for a in store.session_dict(SID)["activity"]]
    assert kinds == ["session", "prompt", "tool", "turn"]

    store.ingest(ev("SessionEnd", reason="logout"))
    s = store.session_dict(SID)
    assert s["status"] == "ended" and s["ended"] == clock.t
    assert s["activity"][-1]["kind"] == "session"


def test_duration_computed_when_missing(store, clock):
    store.ingest(pre("Read", {"file_path": "/a"}, "t1"))
    clock.advance(1.5)
    store.ingest(post("Read", {"file_path": "/a"}, "t1", {"content": "x"}))
    assert call(store, "main", "t1")["duration_ms"] == 1500


def test_session_end_closes_running(store, clock):
    store.ingest(ev("SessionStart"))
    store.ingest(pre("Bash", {"command": "sleep 9"}, "t1"))
    spawn_agent(store, "ag1", "Explore", "look", "a1")
    store.ingest(ev("SessionEnd", reason="other"))
    c = store.call_detail(SID, "t1")
    assert c["status"] == "error" and c["error"] == "Session ended"
    assert agent(store, "a1")["status"] == "done"
    assert agent(store, "main")["status"] == "idle"


def test_stray_events_do_not_reactivate_session_end(store):
    store.ingest(ev("SessionStart"))
    store.ingest(ev("SessionEnd", reason="clear"))
    assert store.ingest(pre("Bash", {"command": "ls"}, "t1")) is None
    assert store.session_dict(SID)["status"] == "ended"
    store.ingest(ev("SessionStart", source="resume"))
    assert store.session_dict(SID)["status"] == "active"


def test_notification_and_stop_failure(store):
    store.ingest(ev("SessionStart"))
    store.ingest(ev("Notification", notification_type="permission_prompt", message="Claude needs permission"))
    assert agent(store, "main")["status"] == "waiting"
    store.ingest(pre("Bash", {"command": "ls"}, "t1"))
    assert agent(store, "main")["status"] == "running"
    store.ingest(ev("Notification", notification_type="auth_success", message="hi"))
    assert agent(store, "main")["status"] == "running"
    store.ingest(ev("StopFailure", error="rate_limit", error_details="429 too many"))
    assert agent(store, "main")["status"] == "error"
    last = store.session_dict(SID)["activity"][-1]
    assert last["kind"] == "error" and "rate_limit" in last["text"] and "429" in last["text"]


def test_compact_and_task_activity(store):
    store.ingest(ev("PreCompact", trigger="auto"))
    store.ingest(ev("PostCompact", trigger="auto"))
    store.ingest(ev("TaskCreated", task_subject="Write docs"))
    store.ingest(ev("TaskCompleted", task_subject="Write docs"))
    s = store.session_dict(SID)
    assert s["compactions"] == 1
    assert [a["kind"] for a in s["activity"]] == ["compact", "compact", "task", "task"]
    assert "auto" in s["activity"][0]["text"] and "Write docs" in s["activity"][2]["text"]


def test_bad_input_and_unknown_events(store):
    assert store.ingest("nope") is None
    assert store.ingest({"hook_event_name": "Stop"}) is None
    assert store.ingest(ev("Mystery")) is None
    assert store.ingest({"session_id": SID, "hook_event_name": 5}) is None
    assert store.ingest(pre("Bash", 12, None)) == SID
    assert store.ingest(ev("PostToolUse", tool_response=object(), duration_ms="x")) == SID
    assert store.snapshot()


def test_pop_changed(store):
    store.ingest(ev("Stop"))
    assert store.pop_changed() == {SID}
    assert store.pop_changed() == set()
    store.set_context(SID, "main", 5, None)
    assert store.pop_changed() == {SID}


def test_snapshot_newest_first(store, clock):
    store.ingest({"session_id": "old", "hook_event_name": "Stop"})
    clock.advance(5)
    store.ingest({"session_id": "new", "hook_event_name": "Stop"})
    assert [s["id"] for s in store.snapshot()] == ["new", "old"]
    assert store.session_dict("old")["title"] == "old"
    assert store.session_dict("zzz") is None


# ---- subagents ------------------------------------------------------------

def test_subagent_lane(store, clock):
    store.ingest(ev("SessionStart"))
    spawn_agent(store, "ag1", "Explore", "Find usages of foo", "agent-abc123")
    a = agent(store, "abc123")
    assert a["label"] == "Explore" and a["task"] == "Find usages of foo" and a["status"] == "running"
    assert call(store, "main", "ag1")["subagent_id"] == "abc123"

    store.ingest(pre("Grep", {"pattern": "foo"}, "g1", agent_id="abc123", agent_type="Explore"))
    assert call(store, "abc123", "g1")["tool"] == "Grep"
    assert [c["id"] for c in agent(store, "main")["calls"]] == ["ag1"]

    clock.advance(3)
    store.ingest(ev("SubagentStop", agent_id="abc123", agent_type="Explore",
                    agent_transcript_path="/t/agent-abc123.jsonl", last_assistant_message="Found 3 usages"))
    a = agent(store, "abc123")
    assert a["status"] == "done" and a["result"] == "Found 3 usages" and a["ended"] == clock.t
    assert ("main" in [x[1] for x in store.transcript_targets()])
    assert (SID, "abc123", "/t/agent-abc123.jsonl") in store.transcript_targets()
    assert (SID, "main", TP) in store.transcript_targets()


def test_agent_ordering(store, clock):
    store.ingest(ev("SessionStart"))
    for aid in ("a1", "a2", "a3"):
        clock.advance(1)
        store.ingest(ev("SubagentStart", agent_id=aid, agent_type="Explore"))
    clock.advance(1)
    store.ingest(ev("SubagentStop", agent_id="a1", agent_type="Explore"))
    clock.advance(1)
    store.ingest(ev("SubagentStop", agent_id="a3", agent_type="Explore"))
    assert [a["id"] for a in store.session_dict(SID)["agents"]] == ["main", "a2", "a3", "a1"]


def test_subagent_late_start_from_tool_event(store):
    store.ingest(pre("Read", {"file_path": "/x"}, "r1", agent_id="agent-zz", agent_type="Plan"))
    assert agent(store, "zz")["label"] == "Plan"
    store.ingest(pre("Read", {"file_path": "/x"}, "r2", agent_id="agent-yy"))
    assert agent(store, "yy")["label"] == "Subagent"


def test_tool_counts_sorted(store):
    for i, t in enumerate(["Read", "Bash", "Bash", "Bash", "Edit", "Edit"]):
        store.ingest(pre(t, {}, f"c{i}"))
    counts = agent(store, "main")["tool_counts"]
    assert list(counts) == ["Bash", "Edit", "Read"]


# ---- call matching --------------------------------------------------------

def test_post_matches_by_id(store):
    store.ingest(pre("Bash", {"command": "a"}, "t1"))
    store.ingest(pre("Bash", {"command": "b"}, "t2"))
    store.ingest(post("Bash", {"command": "b"}, "t2", {"stdout": "B"}))
    assert call(store, "main", "t1")["status"] == "running"
    assert call(store, "main", "t2")["status"] == "ok"


def test_post_fallback_by_tool_name_most_recent(store):
    store.ingest(pre("Bash", {"command": "a"}, "t1"))
    store.ingest(pre("Read", {"file_path": "/f"}, "t2"))
    store.ingest(pre("Bash", {"command": "b"}, "t3"))
    store.ingest(ev("PostToolUse", tool_name="Bash", tool_response={"stdout": "x"}))
    assert call(store, "main", "t3")["status"] == "ok"
    assert call(store, "main", "t1")["status"] == "running"
    assert agent(store, "main")["total_calls"] == 3


def test_orphan_post_creates_and_completes(store):
    store.ingest(post("Bash", {"command": "ls"}, "orphan", {"stdout": "files"}, duration_ms=50))
    c = call(store, "main", "orphan")
    assert c["status"] == "ok" and c["duration_ms"] == 50
    assert agent(store, "main")["total_calls"] == 1
    assert store.call_detail(SID, "orphan")["output"] == "files"


def test_output_rules(store):
    store.ingest(post("Bash", {}, "t1", {"stdout": "out", "stderr": "warn"}))
    assert store.call_detail(SID, "t1")["output"] == "out\n[stderr]\nwarn"
    store.ingest(post("mcp", {}, "t2", {"content": [{"type": "text", "text": "one"}, {"type": "text", "text": "two"}]}))
    assert store.call_detail(SID, "t2")["output"] == "one\ntwo"
    store.ingest(post("X", {}, "t3", {"weird": 1}))
    assert json.loads(store.call_detail(SID, "t3")["output"]) == {"weird": 1}
    store.ingest(post("X", {}, "t4", "plain string"))
    assert store.call_detail(SID, "t4")["output"] == "plain string"


def test_summary_rules(store):
    store.ingest(pre("Read", {"file_path": "/a/b.py", "limit": 5}, "t1"))
    store.ingest(pre("Weird", {"alpha": 1}, "t2"))
    store.ingest(pre("Bash", {"command": "x" * 500}, "t3"))
    assert call(store, "main", "t1")["summary"] == "/a/b.py"
    assert call(store, "main", "t2")["summary"] == '{"alpha":1}'
    assert len(call(store, "main", "t3")["summary"]) == 200


def test_missing_tool_use_id_gets_synthetic_ids(store):
    store.ingest(pre("Bash", {"command": "a"}, None))
    store.ingest(pre("Bash", {"command": "b"}, None))
    ids = [c["id"] for c in agent(store, "main")["calls"]]
    assert len(set(ids)) == 2


def test_call_eviction(clock):
    st = Store(Config(max_calls=3), clock=clock)
    for i in range(5):
        st.ingest(pre("Bash", {"command": str(i)}, f"c{i}"))
    assert [c["id"] for c in agent(st, "main")["calls"]] == ["c2", "c3", "c4"]
    assert st.call_detail(SID, "c0") is None
    assert agent(st, "main")["total_calls"] == 5


def test_lane_sends_last_60(store):
    for i in range(70):
        store.ingest(pre("Bash", {"command": str(i)}, f"c{i}"))
    calls = agent(store, "main")["calls"]
    assert len(calls) == 60 and calls[0]["id"] == "c10"


def test_activity_limits(store):
    for i in range(100):
        store.ingest(pre("Bash", {"command": "y" * 300}, f"c{i}"))
    act = store.session_dict(SID)["activity"]
    assert len(act) == 80 and all(len(a["text"]) <= 240 for a in act)


# ---- failures -------------------------------------------------------------

def test_failure(store):
    store.ingest(pre("Bash", {"command": "false"}, "t1"))
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="t1", tool_input={"command": "false"},
                    error="Command exited with non-zero status code 1\nmore", is_interrupt=False, duration_ms=12))
    c = store.call_detail(SID, "t1")
    assert c["status"] == "error" and c["error"].startswith("Command exited") and c["duration_ms"] == 12
    assert agent(store, "main")["errors"] == 1
    last = store.session_dict(SID)["activity"][-1]
    assert last["kind"] == "error" and last["status"] == "error"
    assert last["text"] == "Bash failed: Command exited with non-zero status code 1"


def test_failure_interrupt_and_orphan(store):
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="t9", tool_input={"command": "x"},
                    error="User interrupted", is_interrupt=True))
    assert store.session_dict(SID)["activity"][-1]["text"] == "Bash interrupted"
    assert store.call_detail(SID, "t9")["status"] == "error"
    assert agent(store, "main")["errors"] == 1


# ---- late start -----------------------------------------------------------

def test_late_start(store):
    store.ingest(post("Bash", {"command": "ls"}, "t1", {"stdout": "x"}))
    s = store.session_dict(SID)
    assert s["status"] == "active" and s["title"] == "demo"
    store.ingest({"session_id": "abcdefghijkl", "hook_event_name": "Stop"})
    assert store.session_dict("abcdefghijkl")["title"] == "abcdefgh"


def test_session_start_context_seed(store):
    store.ingest(ev("SessionStart", source="resume", model="claude-opus-4-1", context_tokens=150_000))
    m = agent(store, "main")
    assert m["context_tokens"] == 150_000 and m["context_window"] == 200_000 and m["model"] == "claude-opus-4-1"


# ---- context --------------------------------------------------------------

def test_context_windows(store):
    store.ingest(ev("SessionStart"))
    store.set_context(SID, "main", 1000, "claude-sonnet-4-5")
    assert agent(store, "main")["context_window"] == 200_000
    store.set_context(SID, "main", 1000, "claude-sonnet-4-5[1m]")
    assert agent(store, "main")["context_window"] == 1_000_000
    store.set_context(SID, "main", 1000, "<synthetic>")
    assert agent(store, "main")["model"] == "claude-sonnet-4-5[1m]"
    assert store.session_dict(SID)["model"] == "claude-sonnet-4-5[1m]"
    store.set_context(SID, "main", 1000, "claude-sonnet-4-5")
    assert agent(store, "main")["context_window"] == 1_000_000  # never shrinks


def test_context_overflow_raises_window(store):
    store.ingest(ev("SessionStart"))
    store.set_context(SID, "main", 250_000, "claude-sonnet-4-5")
    assert agent(store, "main")["context_window"] == 1_000_000
    store.set_context("nope", "main", 1, None)
    store.set_context(SID, "ghost", 1, None)


# ---- subagent linking -----------------------------------------------------

def test_fifo_by_type(store):
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "first"}, "p1"))
    store.ingest(pre("Task", {"subagent_type": "Plan", "description": "plan it"}, "p2"))
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "second"}, "p3"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStart", agent_id="a2", agent_type="Explore"))
    store.ingest(ev("SubagentStart", agent_id="a3", agent_type="Plan"))
    assert agent(store, "a1")["task"] == "first" and call(store, "main", "p1")["subagent_id"] == "a1"
    assert agent(store, "a2")["task"] == "second" and call(store, "main", "p3")["subagent_id"] == "a2"
    assert agent(store, "a3")["task"] == "plan it" and call(store, "main", "p2")["subagent_id"] == "a3"


def test_meta_corrects_wrong_fifo(store):
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "first"}, "p1"))
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "second"}, "p2"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStart", agent_id="a2", agent_type="Explore"))
    # meta says a1 actually belongs to p2: a2 takes p1
    store.apply_subagent_meta(SID, "agent-a1", "second (meta)", "p2")
    assert agent(store, "a1")["task"] == "second (meta)"
    assert agent(store, "a2")["task"] == "first"
    assert call(store, "main", "p2")["subagent_id"] == "a1"
    assert call(store, "main", "p1")["subagent_id"] == "a2"


def test_meta_when_unlinked_agent_steals_and_restores_pending(store):
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "first"}, "p1"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStart", agent_id="a2", agent_type="Explore"))  # no spawn left
    assert agent(store, "a2")["task"] == ""
    store.apply_subagent_meta(SID, "a2", "meta desc", "p1")
    assert agent(store, "a2")["task"] == "meta desc"
    assert agent(store, "a1")["task"] == ""
    assert call(store, "main", "p1")["subagent_id"] == "a2"
    # a1's spawn returned to pending only if it had one; a later start can use p-less pending
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "third"}, "p3"))
    store.ingest(ev("SubagentStart", agent_id="a4", agent_type="Explore"))
    assert agent(store, "a4")["task"] == "third"


def test_meta_idempotent_and_fills_task(store):
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": ""}, "p1"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.apply_subagent_meta(SID, "a1", "from meta", "p1")
    assert agent(store, "a1")["task"] == "from meta"
    store.apply_subagent_meta(SID, "a1", "changed", "p1")
    assert agent(store, "a1")["task"] == "from meta"


def test_meta_removes_pending_and_prevents_fifo(store):
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "d1"}, "p1"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.apply_subagent_meta(SID, "a1", None, "p1")
    assert agent(store, "a1")["task"] == "d1"
    store.ingest(ev("SubagentStart", agent_id="a2", agent_type="Explore"))
    assert agent(store, "a2")["task"] == ""


def test_meta_before_spawn_seen_and_unknown_agent(store):
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.apply_subagent_meta(SID, "a1", "orphan meta", "never-seen")
    assert agent(store, "a1")["task"] == "orphan meta"
    store.apply_subagent_meta(SID, "ghost", "x", "y")
    store.apply_subagent_meta("nosession", "ghost", "x", "y")


# ---- stale ----------------------------------------------------------------

def test_stale_and_reactivation(store, clock):
    store.ingest(ev("SessionStart"))
    store.ingest(pre("Bash", {"command": "hang"}, "t1"))
    clock.advance(59 * 60)
    store.expire_stale()
    assert store.session_dict(SID)["status"] == "active"
    clock.advance(2 * 60)
    store.pop_changed()
    store.expire_stale()
    s = store.session_dict(SID)
    assert s["status"] == "ended" and s["ended"] == clock.t
    assert s["activity"][-1]["text"] == "No activity; marked ended"
    assert store.call_detail(SID, "t1")["status"] == "error"
    assert store.pop_changed() == {SID}
    store.ingest(ev("Stop"))
    s = store.session_dict(SID)
    assert s["status"] == "active" and s["ended"] is None


def test_touch_defers_stale(store, clock):
    store.ingest(ev("SessionStart"))
    clock.advance(50 * 60)
    store.touch(SID)
    clock.advance(50 * 60)
    store.expire_stale()
    assert store.session_dict(SID)["status"] == "active"
    store.touch("nope")


def test_stale_disabled(clock):
    st = Store(Config(stale_minutes=0), clock=clock)
    st.ingest(ev("SessionStart"))
    clock.advance(10**6)
    st.expire_stale()
    assert st.session_dict(SID)["status"] == "active"


def test_transcript_targets_skip_ended(store, clock):
    store.ingest(ev("SessionStart"))
    spawn_agent(store, "p1", "Explore", "d", "a1")
    assert set(store.transcript_targets()) == {(SID, "main", TP), (SID, "a1", None)}
    store.ingest(ev("SessionEnd"))
    assert store.transcript_targets() == []


# ---- call detail / redaction ----------------------------------------------

def test_lane_omits_input_output_detail_has_them(store):
    tin = {"command": "echo hi", "env": {"nested": ["a", {"deep": "b"}]}}
    store.ingest(pre("Bash", tin, "t1"))
    store.ingest(post("Bash", tin, "t1", {"stdout": "hi"}))
    lane = call(store, "main", "t1")
    assert not {"input", "output", "error"} & set(lane)
    d = store.call_detail(SID, "t1")
    assert d["input"] == tin and d["output"] == "hi" and d["error"] is None
    assert d["id"] == "t1" and d["tool"] == "Bash"


def test_call_detail_unknown(store):
    store.ingest(ev("SessionStart"))
    assert store.call_detail(SID, "nope") is None
    assert store.call_detail("nosession", "nope") is None


def test_secrets_never_leak(store):
    tin = {"command": "curl -H 'x: y' --data " + SECRET, "nested": {"list": [SECRET, {"k": "token=" + SECRET}]}}
    store.ingest(ev("SessionStart"))
    store.ingest(ev("UserPromptSubmit", prompt="use " + SECRET + " please"))
    store.ingest(pre("Bash", tin, "t1"))
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "with " + SECRET}, "p1"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.ingest(post("Bash", tin, "t1", {"stdout": "out " + SECRET, "stderr": "err " + SECRET}))
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="t2", tool_input=tin, error="bad " + SECRET))
    store.ingest(ev("SubagentStop", agent_id="a1", agent_type="Explore", last_assistant_message="done " + SECRET))
    store.ingest(ev("Notification", notification_type="idle_prompt", message="n " + SECRET))
    store.ingest(ev("StopFailure", error="e " + SECRET, error_details={"x": SECRET}))
    store.ingest(ev("TaskCreated", task_subject="t " + SECRET))
    blob = json.dumps(store.snapshot())
    for cid in ("t1", "t2", "p1"):
        blob += json.dumps(store.call_detail(SID, cid))
    assert A20 not in blob
    assert MASK in blob


# ---- review fixes ---------------------------------------------------------

def test_metadata_fields_redacted_and_capped(store):
    sk = "x-" + SECRET
    store.ingest(ev("SessionStart", cwd="/w/" + sk, model="m-" + sk, source="s-" + sk))
    store.ingest(pre("T-" + sk, {"command": "ls"}, "a", agent_id="g1", agent_type="at-" + sk))
    store.ingest(ev("PreCompact", trigger="t-" + sk))
    store.ingest(ev("SessionEnd", reason="r-" + sk))
    assert A20 not in json.dumps(store.snapshot())
    store.ingest(pre("n" * 5000, {}, "b", cwd="/" + "y" * 5000))
    s = store.session_dict(SID)
    assert max(len(c["tool"]) for a in s["agents"] for c in a["calls"]) <= 200
    assert len(s["cwd"]) <= 1000


def test_failed_spawn_leaves_pending(store):
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "first"}, "p1"))
    store.ingest(ev("PostToolUseFailure", tool_name="Agent", tool_use_id="p1", error="denied"))
    store.ingest(pre("Agent", {"subagent_type": "Explore", "description": "second"}, "p2"))
    store.ingest(ev("SubagentStart", agent_id="a", agent_type="Explore"))
    assert agent(store, "a")["task"] == "second"


def test_spawn_without_type_defaults_to_general_purpose(store):
    store.ingest(pre("Agent", {"description": "d"}, "p1"))
    store.ingest(ev("SubagentStart", agent_id="a", agent_type="general-purpose"))
    assert agent(store, "a")["task"] == "d"


def test_post_matches_call_on_other_agent(store):
    store.ingest(pre("Bash", {"command": "x"}, "t1", agent_id="a1", agent_type="Explore"))
    store.ingest(post("Bash", {"command": "x"}, "t1", {"stdout": "o"}))  # no agent_id
    assert call(store, "a1", "t1")["status"] == "ok"
    assert agent(store, "main")["total_calls"] == 0


def test_duplicate_pre_not_double_counted(store):
    clock_calls = ("d", "d", "e")
    st = Store(Config(max_calls=2), clock=Clock())
    for cid in clock_calls:
        st.ingest(pre("Bash", {"command": "x"}, cid))
    a = agent(st, "main")
    assert a["total_calls"] == 2 and a["tool_counts"] == {"Bash": 2}
    assert st.call_detail(SID, "d") is not None


def test_subagent_and_main_revival(store):
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStop", agent_id="a1", agent_type="Explore"))
    assert agent(store, "a1")["status"] == "done"
    store.ingest(pre("Read", {"file_path": "/x"}, "r1", agent_id="a1"))
    assert agent(store, "a1")["status"] == "running" and agent(store, "a1")["ended"] is None
    store.ingest(ev("SubagentStop", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    assert agent(store, "a1")["status"] == "running"
    store.ingest(ev("StopFailure", error="x"))
    store.ingest(pre("Bash", {}, "t1"))
    assert agent(store, "main")["status"] == "running"


def test_synthetic_model_ignored_at_session_start(store):
    store.ingest(ev("SessionStart", model="<synthetic>"))
    assert store.session_dict(SID)["model"] is None


def test_set_context_bad_tokens(store):
    store.ingest(ev("SessionStart"))
    for bad in ("abc", -1, True, 2.5, None):
        store.set_context(SID, "main", bad, None)
    assert agent(store, "main")["context_tokens"] is None


def test_duration_validation(store, clock):
    store.ingest(pre("A", {}, "t1"))
    clock.advance(2)
    store.ingest(post("A", {}, "t1", {}, duration_ms=-5))
    assert call(store, "main", "t1")["duration_ms"] == 2000
    store.ingest(post("B", {}, "t2", {}, duration_ms=float("nan")))
    assert call(store, "main", "t2")["duration_ms"] == 0
    store.ingest(post("C", {}, "t3", {}, duration_ms=12.6))
    assert call(store, "main", "t3")["duration_ms"] == 13


def test_stderr_kept_when_stdout_huge(clock):
    st = Store(Config(max_field_chars=400), clock=clock)
    st.ingest(post("Bash", {}, "t1", {"stdout": "o" * 5000, "stderr": "boom " * 40}))
    out = st.call_detail(SID, "t1")["output"]
    assert "[stderr]\n" + "boom " * 20 in out
    assert len(out) <= 400 + 120


def test_activity_text_surrogate_safe(store):
    store.ingest(ev("UserPromptSubmit", prompt="a\ud800b"))
    json.dumps(store.snapshot(), ensure_ascii=False).encode("utf-8")


def test_poisoned_fields_end_to_end(store):
    sk = "x-" + SECRET
    bad = "\ud800" + sk
    nan = float("nan")
    store.ingest(ev("SessionStart", cwd="/w/" + sk, model="m-" + sk, source="s-" + sk, context_tokens=10))
    store.ingest(ev("UserPromptSubmit", prompt=bad))
    tin = {sk: nan, "command": bad, "nested": {sk: [bad, nan]}}
    store.ingest(pre("T-" + sk, tin, "t-" + sk, agent_id="g-" + sk, agent_type="at-" + sk))
    store.ingest(pre("Agent", {"subagent_type": "at-" + sk, "description": bad}, "p1"))
    store.ingest(ev("SubagentStart", agent_id="g2", agent_type="at-" + sk, agent_transcript_path=sk))
    store.ingest(post("T-" + sk, tin, "t-" + sk, {"stdout": bad, "stderr": bad}, duration_ms=nan, agent_id="g-" + sk))
    store.ingest(ev("PostToolUseFailure", tool_name="T-" + sk, tool_use_id="f", tool_input=tin, error=bad, duration_ms=nan))
    store.ingest(ev("Notification", notification_type="idle_" + sk, message=bad))
    store.ingest(ev("StopFailure", error=bad, error_details={sk: nan}))
    store.ingest(ev("PreCompact", trigger=sk))
    store.ingest(ev("TaskCreated", task_subject=bad))
    store.ingest(ev("SubagentStop", agent_id="g2", agent_type="at-" + sk, last_assistant_message=bad))
    store.apply_subagent_meta(SID, "g2", bad, "p1")
    store.ingest(ev("SessionEnd", reason=bad))
    blob = json.dumps(store.snapshot(), allow_nan=False)
    for cid in ("t-" + sk, "f", "p1"):
        detail = store.call_detail(SID, cid)
        if detail:
            blob += json.dumps(detail, allow_nan=False)
    for aid in [a["id"] for a in store.session_dict(SID)["agents"]]:
        for c in agent(store, aid)["calls"]:
            blob += json.dumps(store.call_detail(SID, c["id"]), allow_nan=False)
    blob.encode("utf-8")
    assert A20 not in blob
    assert MASK in blob


# ---- agentId linking from the Agent tool_response -------------------------

def async_resp(aid, **kw):
    return {"status": "async_launched", "agentId": aid, "description": "d", "prompt": "p",
            "outputFile": "/tmp/out", **kw}


def spawn_pre(store, uid, desc, atype="Explore"):
    store.ingest(pre("Agent", {"subagent_type": atype, "description": desc, "prompt": "go"}, uid))


def test_response_agent_id_before_subagent_start(store):
    spawn_pre(store, "p1", "first")
    spawn_pre(store, "p2", "second")
    store.ingest(post("Agent", {}, "p2", async_resp("agent-a9")))
    store.ingest(ev("SubagentStart", agent_id="a9", agent_type="Explore"))
    assert agent(store, "a9")["task"] == "second"
    assert call(store, "main", "p2")["subagent_id"] == "a9"
    store.ingest(ev("SubagentStart", agent_id="a8", agent_type="Explore"))
    assert agent(store, "a8")["task"] == "first"


def test_response_corrects_wrong_fifo(store):
    spawn_pre(store, "p1", "first")
    spawn_pre(store, "p2", "second")
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))  # FIFO: p1
    store.ingest(ev("SubagentStart", agent_id="a2", agent_type="Explore"))  # FIFO: p2
    store.ingest(post("Agent", {}, "p2", async_resp("a1")))
    store.ingest(post("Agent", {}, "p1", async_resp("a2")))
    assert agent(store, "a1")["task"] == "second" and agent(store, "a2")["task"] == "first"
    assert call(store, "main", "p2")["subagent_id"] == "a1"
    assert call(store, "main", "p1")["subagent_id"] == "a2"


def test_foreground_completed_response_links(store):
    spawn_pre(store, "p1", "fg")
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    spawn_pre(store, "p2", "other")
    store.ingest(post("Agent", {}, "p2", {"status": "completed", "agentId": "a1", "content": [{"type": "text", "text": "done"}],
                                          "resolvedModel": "claude-haiku-4-5", "totalTokens": 5}))
    assert call(store, "main", "p2")["subagent_id"] == "a1"
    assert agent(store, "a1")["task"] == "other"
    assert store.call_detail(SID, "p2")["output"] == "done"
    assert agent(store, "a1")["model"] == "claude-haiku-4-5"


@pytest.mark.parametrize("bad", ["../etc", "a b", "", "x" * 129, 5, None, "main", "agent-"])
def test_hostile_agent_id_ignored(store, bad):
    spawn_pre(store, "p1", "first")
    store.ingest(post("Agent", {}, "p1", async_resp(bad)))
    assert call(store, "main", "p1")["subagent_id"] is None
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    assert agent(store, "a1")["task"] == "first"  # FIFO still works


def test_meta_agreeing_with_response_is_noop(store):
    spawn_pre(store, "p1", "first")
    store.ingest(post("Agent", {}, "p1", async_resp("a1")))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.apply_subagent_meta(SID, "a1", "meta text", "p1")
    a = agent(store, "a1")
    assert a["task"] == "first"
    assert call(store, "main", "p1")["subagent_id"] == "a1"
    spawn_pre(store, "p2", "second")
    store.ingest(ev("SubagentStart", agent_id="a2", agent_type="Explore"))
    assert agent(store, "a2")["task"] == "second"


def test_async_launch_output_text(store):
    spawn_pre(store, "p1", "first")
    store.ingest(post("Agent", {}, "p1", async_resp("a1", resolvedModel="claude-sonnet-4-5")))
    d = store.call_detail(SID, "p1")
    assert d["status"] == "ok" and d["output"] == "Launched in background"


def test_resolved_model_applied_when_lane_created_later(store):
    spawn_pre(store, "p1", "first")
    store.ingest(post("Agent", {}, "p1", async_resp("a1", resolvedModel="m-" + SECRET)))
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    assert agent(store, "a1")["model"].startswith("m-")
    assert A20 not in json.dumps(store.snapshot())


# ---- agentId link fix round -----------------------------------------------

def _tasks(store):
    return {a["id"]: a["task"] for a in store.session_dict(SID)["agents"] if a["id"] != "main"}


@pytest.mark.parametrize("order", [("b", "a"), ("a", "b")])
def test_held_link_not_stolen_by_fifo(store, order):
    spawn_pre(store, "pA", "A")
    spawn_pre(store, "pB", "B")
    store.ingest(post("Agent", {}, "pA", async_resp("a")))
    for aid in order:
        store.ingest(ev("SubagentStart", agent_id=aid, agent_type="Explore"))
    assert _tasks(store) == {"a": "A", "b": "B"}
    assert store._sessions[SID].pending == []


def test_displaced_lane_retries_fifo(store):
    spawn_pre(store, "pA", "A")
    spawn_pre(store, "pB", "B")
    store.ingest(ev("SubagentStart", agent_id="x", agent_type="Explore"))  # FIFO: pA
    store.ingest(post("Agent", {}, "pA", async_resp("y")))  # y does not exist yet: held
    store.ingest(ev("SubagentStart", agent_id="z", agent_type="Explore"))
    store.apply_subagent_meta(SID, "z", None, "pA")  # displaces x, which retries and takes pB
    assert _tasks(store)["x"] == "B" and _tasks(store)["z"] == "A"


def test_spawn_structures_bounded(store):
    for i in range(5000):
        spawn_pre(store, f"p{i}", f"d{i}")
        store.ingest(post("Agent", {}, f"p{i}", async_resp(f"zz{i}", resolvedModel="m")))
    s = store._sessions[SID]
    assert max(len(s.spawn_links), len(s.spawn_models), len(s.pending), len(s.spawns)) <= 256
    store.ingest(ev("SessionEnd"))
    assert not s.spawn_links and not s.spawn_models


def test_held_link_dropped_on_failure_and_eviction(clock):
    st = Store(Config(max_calls=2), clock=clock)
    spawn_pre(st, "p1", "d")
    st.ingest(post("Agent", {}, "p1", async_resp("a1")))
    st.ingest(ev("PostToolUseFailure", tool_name="Agent", tool_use_id="p1", error="x"))
    assert st._sessions[SID].spawn_links == {}
    spawn_pre(st, "p2", "d")
    st.ingest(post("Agent", {}, "p2", async_resp("a2")))
    for i in range(3):
        st.ingest(pre("Read", {}, f"r{i}"))
    assert st._sessions[SID].spawn_links == {}
    assert st._sessions[SID].spawn_models == {}


def test_secret_shaped_agent_id_ignored(store):
    spawn_pre(store, "p1", "first")
    store.ingest(post("Agent", {}, "p1", async_resp(SECRET, resolvedModel="m")))
    s = store._sessions[SID]
    assert s.spawn_links == {} and s.spawn_models == {}
    assert A20 not in json.dumps(store.snapshot())


def test_completed_without_text_says_completed(store):
    spawn_pre(store, "p1", "d")
    store.ingest(post("Agent", {}, "p1", {"status": "completed", "agentId": "a1", "prompt": "the whole prompt",
                                          "content": [{"type": "tool_use", "name": "x"}]}))
    assert store.call_detail(SID, "p1")["output"] == "Completed"


# ---- acceptance-test fixes ------------------------------------------------

NOTIFICATION = (
    "<task-notification>\n<task-id>a1b2c3</task-id>\n<tool-use-id>toolu_p1</tool-use-id>\n"
    "<output-file>/tmp/out.output</output-file>\n<status>completed</status>\n"
    "<summary>Agent \"Find usages\" completed</summary>\n<result>Found 3 usages</result>\n</task-notification>"
)


def test_task_notification_does_not_overwrite_task(store):
    store.ingest(ev("UserPromptSubmit", prompt="fix the build"))
    spawn_pre(store, "toolu_p1", "Find usages")
    store.ingest(ev("SubagentStart", agent_id="a1b2c3", agent_type="Explore"))
    store.ingest(ev("Stop"))
    store.ingest(ev("UserPromptSubmit", prompt="\n  " + NOTIFICATION))
    m = agent(store, "main")
    assert m["task"] == "fix the build" and m["status"] == "running"
    last = store.session_dict(SID)["activity"][-1]
    assert (last["kind"], last["status"], last["text"]) == ("agent", "ok", "Explore result delivered to Main")


def test_task_notification_unknown_lane(store):
    store.ingest(ev("UserPromptSubmit", prompt="real task"))
    store.ingest(ev("UserPromptSubmit", prompt=NOTIFICATION.replace("a1b2c3", "../x")))
    assert agent(store, "main")["task"] == "real task"
    assert store.session_dict(SID)["activity"][-1]["text"] == "Background task notification"


def test_task_notification_falls_back_to_tool_use_id(store):
    spawn_pre(store, "toolu_p1", "Find usages")
    store.ingest(ev("SubagentStart", agent_id="zzz", agent_type="Explore"))
    store.ingest(ev("UserPromptSubmit", prompt=NOTIFICATION))
    assert store.session_dict(SID)["activity"][-1]["text"] == "Explore result delivered to Main"


def test_stop_closes_unreported_main_calls_only(store):
    store.ingest(ev("UserPromptSubmit", prompt="go"))
    store.ingest(pre("Bash", {"command": "ls /outside"}, "t1"))
    store.ingest(pre("Read", {"file_path": "/ok"}, "t2"))
    store.ingest(post("Read", {"file_path": "/ok"}, "t2", {"content": "x"}))
    store.ingest(pre("Grep", {"pattern": "x"}, "s1", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("Stop"))
    c = store.call_detail(SID, "t1")
    assert c["status"] == "error" and c["error"] == "No result reported (blocked or cancelled)"
    assert call(store, "main", "t2")["status"] == "ok"
    assert call(store, "a1", "s1")["status"] == "running"
    assert agent(store, "main")["errors"] == 1
    texts = [a["text"] for a in store.session_dict(SID)["activity"] if a["kind"] == "error"]
    assert texts == ["Bash blocked or cancelled"]
    store.ingest(ev("SessionEnd"))
    assert store.call_detail(SID, "s1")["error"] == "Session ended"


def test_stop_failure_and_subagent_stop_close_calls(store):
    store.ingest(pre("Bash", {"command": "x"}, "t1"))
    store.ingest(pre("Grep", {"pattern": "x"}, "s1", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStop", agent_id="a1", agent_type="Explore"))
    assert store.call_detail(SID, "s1")["error"].startswith("No result")
    assert agent(store, "a1")["errors"] == 1
    assert call(store, "main", "t1")["status"] == "running"
    store.ingest(ev("StopFailure", error="x"))
    assert call(store, "main", "t1")["status"] == "error"


def test_permission_denied(store):
    store.ingest(pre("Bash", {"command": "rm -rf /tmp/build"}, "t1"))
    store.ingest(ev("PermissionDenied", tool_name="Bash", tool_input={"command": "rm -rf /tmp/build"},
                    tool_use_id="t1", permission_mode="auto", reason="[Irreversible Local Destruction]"))
    d = store.call_detail(SID, "t1")
    assert d["status"] == "error" and d["error"] == "Denied: [Irreversible Local Destruction]"
    assert agent(store, "main")["errors"] == 1
    last = store.session_dict(SID)["activity"][-1]
    assert (last["kind"], last["status"], last["text"]) == ("error", "error", "Bash denied")


def test_permission_denied_orphan_without_reason(store):
    store.ingest(ev("PermissionDenied", tool_name="Write", tool_input={"file_path": "/etc/x"}, tool_use_id="t9"))
    assert store.call_detail(SID, "t9")["error"] == "Denied"
    assert agent(store, "main")["total_calls"] == 1


def test_permission_denied_spawn_drops_pending(store):
    spawn_pre(store, "p1", "first")
    store.ingest(ev("PermissionDenied", tool_name="Agent", tool_input={}, tool_use_id="p1", reason="r"))
    assert store._sessions[SID].pending == []


def test_bare_subagent_stop_creates_no_lane(store):
    store.ingest(ev("SessionStart"))
    before = len(store.session_dict(SID)["activity"])
    store.ingest(ev("SubagentStop", agent_id="compact-1"))
    s = store.session_dict(SID)
    assert [a["id"] for a in s["agents"]] == ["main"]
    assert len(s["activity"]) == before + 1
    assert s["activity"][-1]["text"] == "Background agent finished"
    store.ingest(ev("SubagentStop", agent_id="x2", agent_type="Plan"))
    assert store.session_dict(SID)["activity"][-1]["text"] == "Plan finished"
    store.ingest(pre("Read", {"file_path": "/x"}, "r1", agent_id="late"))
    assert agent(store, "late")["total_calls"] == 1


# ---- late events after synthetic closes -----------------------------------

def test_late_post_after_stop_restores_call_and_count(store, clock):
    store.ingest(pre("Bash", {"command": "ls"}, "t1"))
    store.ingest(ev("Stop"))
    assert agent(store, "main")["errors"] == 1
    clock.advance(3)
    store.ingest(post("Bash", {"command": "ls"}, "t1", {"stdout": "x"}))
    d = store.call_detail(SID, "t1")
    assert d["status"] == "ok" and d["error"] is None and d["output"] == "x"
    assert agent(store, "main")["errors"] == 0
    assert agent(store, "main")["total_calls"] == 1
    assert [a["kind"] for a in store.session_dict(SID)["activity"]].count("error") == 1  # the close entry only


def test_late_failure_after_stop_not_double_counted(store):
    store.ingest(pre("Bash", {"command": "ls"}, "t1"))
    store.ingest(ev("Stop"))
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="t1", tool_input={}, error="boom"))
    assert agent(store, "main")["errors"] == 1
    assert store.call_detail(SID, "t1")["error"] == "boom"
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="t1", tool_input={}, error="again"))
    assert agent(store, "main")["errors"] == 2  # a genuinely second failure event counts


def test_late_denied_after_stop_not_double_counted(store):
    store.ingest(pre("Bash", {"command": "ls"}, "t1"))
    store.ingest(ev("Stop"))
    store.ingest(ev("PermissionDenied", tool_name="Bash", tool_use_id="t1", tool_input={}, reason="no"))
    assert agent(store, "main")["errors"] == 1
    assert store.call_detail(SID, "t1")["error"] == "Denied: no"


def test_late_post_after_session_end_close(store):
    store.ingest(pre("Bash", {"command": "ls"}, "t1"))
    store.ingest(ev("SessionEnd"))
    assert agent(store, "main")["errors"] == 0
    store.ingest(ev("SessionStart", source="resume"))
    store.ingest(post("Bash", {"command": "ls"}, "t1", {"stdout": "x"}))
    assert store.call_detail(SID, "t1")["status"] == "ok"
    assert agent(store, "main")["errors"] == 0


def test_late_post_on_subagent_lane_keeps_lane_done(store):
    spawn_agent(store, "u1", "Explore", "d", "sa1")
    store.ingest(pre("Read", {"file_path": "/a"}, "t2", agent_id="sa1", agent_type="Explore"))
    store.ingest(ev("SubagentStop", agent_id="sa1", agent_type="Explore"))
    ended = agent(store, "sa1")["ended"]
    assert agent(store, "sa1")["errors"] == 1
    store.ingest(post("Read", {"file_path": "/a"}, "t2", {"x": 1}, agent_id="sa1", agent_type="Explore"))
    a = agent(store, "sa1")
    assert a["status"] == "done" and a["ended"] == ended and a["errors"] == 0
    assert call(store, "sa1", "t2")["status"] == "ok"


def test_late_failure_and_denied_do_not_revive_lane(store):
    store.ingest(ev("SubagentStart", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("SubagentStop", agent_id="a1", agent_type="Explore"))
    store.ingest(ev("PostToolUseFailure", tool_name="Bash", tool_use_id="x1", tool_input={}, error="e", agent_id="a1"))
    store.ingest(ev("PermissionDenied", tool_name="Bash", tool_use_id="x2", tool_input={}, agent_id="a1"))
    assert agent(store, "a1")["status"] == "done"
    store.ingest(pre("Read", {}, "x3", agent_id="a1"))
    assert agent(store, "a1")["status"] == "running"


def test_stop_before_lane_known_creates_done_lane(store):
    store.ingest(ev("SubagentStop", agent_id="zz9"))
    assert store.session_dict(SID)["activity"][-1]["agent_id"] == "main"
    store.ingest(ev("SubagentStart", agent_id="zz9", agent_type="Explore"))
    a = agent(store, "zz9")
    assert a["status"] == "done" and a["ended"] is not None
    store.ingest(pre("Read", {}, "r1", agent_id="zz9"))
    assert agent(store, "zz9")["status"] == "running"


def test_stop_before_lane_known_then_tool_event(store):
    store.ingest(ev("SubagentStop", agent_id="zz8"))
    store.ingest(post("Read", {}, "r1", {"content": "x"}, agent_id="zz8"))
    assert agent(store, "zz8")["status"] == "done"
    assert call(store, "zz8", "r1")["status"] == "ok"


def test_stopped_set_capped(store):
    for i in range(600):
        store.ingest(ev("SubagentStop", agent_id=f"u{i}"))
    assert len(store._sessions[SID].stopped) <= 256

import json

import pytest

from cc_mission_control.config import Config
from cc_mission_control.state import Store
from cc_mission_control.transcript import Tail, TranscriptWatcher, Usage, usage_from_entry

SID = "sess-1234567890"


class Clock:
    def __init__(self, t=1000.0):
        self.t = t

    def __call__(self):
        return self.t

    def advance(self, dt):
        self.t += dt


def asst(inp=0, cc=0, cr=0, out=0, model="claude-sonnet-5", **extra):
    e = {
        "type": "assistant",
        "message": {
            "model": model,
            "usage": {
                "input_tokens": inp,
                "cache_creation_input_tokens": cc,
                "cache_read_input_tokens": cr,
                "output_tokens": out,
            },
        },
    }
    e.update(extra)
    return e


def line(obj):
    return json.dumps(obj) + "\n"


def write(path, *objs, mode="w"):
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, mode, encoding="utf-8") as f:
        for o in objs:
            f.write(o if isinstance(o, str) else line(o))


# ---- usage_from_entry -----------------------------------------------------

def test_usage_sums_all_four_fields():
    u = usage_from_entry(asst(inp=10, cc=200, cr=3000, out=40), main=True)
    assert u == Usage(3250, "claude-sonnet-5")


def test_usage_missing_and_non_int_fields():
    e = asst(inp=5)
    e["message"]["usage"] = {"input_tokens": 5, "output_tokens": "9", "cache_read_input_tokens": 1.5,
                             "cache_creation_input_tokens": True}
    assert usage_from_entry(e, main=True).tokens == 5
    e["message"]["usage"] = {}
    assert usage_from_entry(e, main=True).tokens == 0


@pytest.mark.parametrize("entry", [
    {"type": "user", "message": {"usage": {"input_tokens": 1}}},
    {"type": "assistant"},
    {"type": "assistant", "message": "x"},
    {"type": "assistant", "message": {"usage": []}},
    [],
    "str",
])
def test_usage_unusable_entries(entry):
    assert usage_from_entry(entry, main=True) is None


def test_usage_sidechain_main_vs_subagent():
    e = asst(inp=7, isSidechain=True)
    assert usage_from_entry(e, main=True) is None
    assert usage_from_entry(e, main=False).tokens == 7


def test_usage_synthetic_model_is_none():
    assert usage_from_entry(asst(inp=1, model="<synthetic>"), main=True).model is None
    e = asst(inp=1)
    del e["message"]["model"]
    assert usage_from_entry(e, main=True).model is None


# ---- Tail -----------------------------------------------------------------

def test_tail_partial_line_held_back(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, {"a": 1}, '{"b": ')
    t = Tail(p)
    assert t.read() == ([{"a": 1}], True)
    assert t.read() == ([], False)
    write(p, "2}\n", mode="a")
    assert t.read() == ([{"b": 2}], True)


def test_tail_skips_bad_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, "not json\n", "\n", "   \n", "[1,2]\n", "42\n", {"ok": True}, b"".decode())
    with open(p, "ab") as f:
        f.write(b'{"bad": "\xff"}\n')
    entries, grew = Tail(p).read()
    assert grew and entries == [{"ok": True}, {"bad": "�"}]


def test_tail_truncation_resets(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, {"n": 1}, {"n": 2}, '{"partial')
    t = Tail(p)
    t.read()
    write(p, {"n": 3})
    assert t.read() == ([{"n": 3}], True)


def test_tail_large_file_starts_near_end(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, *({"n": i} for i in range(100)))
    size = p.stat().st_size
    t = Tail(p, start_tail_bytes=size // 4)
    entries, grew = t.read()
    assert grew
    ns = [e["n"] for e in entries]
    assert ns[-1] == 99 and ns == list(range(ns[0], 100)) and ns[0] > 50
    write(p, {"n": 100}, mode="a")
    assert t.read() == ([{"n": 100}], True)


def test_tail_large_file_without_newline_in_window(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, {"pad": "x" * 500}, {"n": 1})
    t = Tail(p, start_tail_bytes=5)
    assert t.read() == ([], True)  # still inside the discarded partial line
    assert t.read() == ([], False)
    write(p, {"n": 2}, mode="a")
    assert t.read() == ([{"n": 2}], True)


def test_tail_max_read_bytes_spreads_append(tmp_path):
    p = tmp_path / "t.jsonl"
    objs = [{"n": i} for i in range(20)]
    write(p, *objs)
    t = Tail(p, max_read_bytes=40)
    got, reads = [], 0
    while True:
        entries, grew = t.read()
        if not grew:
            break
        reads += 1
        got.extend(entries)
    assert reads > 3 and got == objs


def test_tail_missing_file(tmp_path):
    t = Tail(tmp_path / "nope.jsonl")
    assert t.read() == ([], False)
    write(tmp_path / "nope.jsonl", {"a": 1})
    assert t.read() == ([{"a": 1}], True)


def test_tail_directory_never_raises(tmp_path):
    assert Tail(tmp_path).read() == ([], False)


# ---- watcher --------------------------------------------------------------

class Env:
    def __init__(self, tmp_path):
        self.root = tmp_path / "proj"
        self.main = self.root / (SID + ".jsonl")
        self.sdir = self.root / SID
        self.sclock = Clock()
        self.wclock = Clock(0.0)
        self.cfg = Config.from_env({})
        self.store = Store(self.cfg, clock=self.sclock)
        self.w = TranscriptWatcher(self.store, clock=self.wclock, search_interval=2.0)
        self.ev("SessionStart", source="startup")

    def ev(self, name, sid=SID, **kw):
        base = {"session_id": sid, "transcript_path": str(self.main), "cwd": "/w/demo",
                "hook_event_name": name}
        base.update(kw)
        return self.store.ingest(base)

    def sub(self, aid, atype="Explore", **kw):
        self.ev("SubagentStart", agent_id=aid, agent_type=atype, **kw)

    def agent(self, aid="main", sid=SID):
        s = self.store.session_dict(sid)
        return next(a for a in s["agents"] if a["id"] == aid)


@pytest.fixture
def env(tmp_path):
    return Env(tmp_path)


def test_watcher_main_context(env):
    write(env.main, asst(inp=1, cc=2, cr=3, out=4, model="claude-sonnet-5"))
    assert env.w.poll() == {SID}
    a = env.agent()
    assert a["context_tokens"] == 10 and a["model"] == "claude-sonnet-5"
    assert a["context_window"] == env.cfg.context_window
    assert env.store.session_dict(SID)["model"] == "claude-sonnet-5"


def test_watcher_takes_last_usable_entry_and_skips_sidechain(env):
    write(env.main, asst(inp=100), asst(inp=500, isSidechain=True), {"type": "user"})
    env.w.poll()
    assert env.agent()["context_tokens"] == 100


def test_watcher_1m_model_name(env):
    write(env.main, asst(inp=1000, model="claude-opus-4-1m"))
    env.w.poll()
    assert env.agent()["context_window"] == 1_000_000


def test_watcher_1m_by_exceeding_window(env):
    write(env.main, asst(inp=env.cfg.context_window + 1))
    env.w.poll()
    assert env.agent()["context_window"] == 1_000_000
    write(env.main, asst(inp=10), mode="a")
    env.w.poll()
    a = env.agent()
    assert a["context_tokens"] == 10 and a["context_window"] == 1_000_000


def test_watcher_synthetic_keeps_model(env):
    write(env.main, asst(inp=1, model="claude-sonnet-5"))
    env.w.poll()
    write(env.main, asst(inp=2, model="<synthetic>"), mode="a")
    env.w.poll()
    a = env.agent()
    assert a["context_tokens"] == 2 and a["model"] == "claude-sonnet-5"


def test_watcher_no_changes_returns_empty(env):
    write(env.main, asst(inp=1))
    env.w.poll()
    assert env.w.poll() == set()


def test_watcher_growth_touches_and_reports(env):
    write(env.main, {"type": "user"})
    t0 = env.store.session_dict(SID)["last_event"]
    env.sclock.advance(30)
    assert env.w.poll() == {SID}  # grew, but no usage
    assert env.store.session_dict(SID)["last_event"] == t0 + 30
    assert env.agent()["context_tokens"] is None
    env.sclock.advance(30)
    assert env.w.poll() == set()
    assert env.store.session_dict(SID)["last_event"] == t0 + 30


def test_watcher_missing_transcript_and_no_path(env):
    assert env.w.poll() == set()
    env.ev("SessionStart", sid="other-session-1", transcript_path=None)
    env.store.ingest({"session_id": "nopath-session", "hook_event_name": "SessionStart"})
    assert env.w.poll() == set()


@pytest.mark.parametrize("where", ["subagents", "session", "sibling"])
def test_watcher_subagent_search_patterns(env, where):
    env.sub("abc12345")
    target = {
        "subagents": env.sdir / "subagents" / "agent-abc12345.jsonl",
        "session": env.sdir / "xx-abc12345-yy.jsonl",
        "sibling": env.root / "agent-abc12345.jsonl",
    }[where]
    write(target, asst(inp=50, out=5, model="claude-haiku-4-5"))
    assert env.w.poll() == {SID}
    a = env.agent("abc12345")
    assert a["context_tokens"] == 55 and a["model"] == "claude-haiku-4-5"


def test_watcher_subagent_search_order(env):
    env.sub("abc12345")
    write(env.root / "agent-abc12345.jsonl", asst(inp=3))
    write(env.sdir / "subagents" / "agent-abc12345.jsonl", asst(inp=1))
    write(env.sdir / "other-abc12345.jsonl", asst(inp=2))
    env.w.poll()
    assert env.agent("abc12345")["context_tokens"] == 1


def test_watcher_subagent_counts_sidechain_entries(env):
    env.sub("abc123")
    write(env.sdir / "subagents" / "agent-abc123.jsonl", asst(inp=9, isSidechain=True))
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 9


def test_watcher_agent_prefix_is_normalized(env):
    env.sub("agent-abc123")
    write(env.sdir / "subagents" / "agent-abc123.jsonl", asst(inp=4))
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 4


def test_watcher_never_matches_main_transcript(env):
    # an agent id contained in the session id would match the main transcript by glob
    env.sub("1234567890")
    write(env.main, asst(inp=100))
    env.w.poll()
    assert env.agent()["context_tokens"] == 100
    assert env.agent("1234567890")["context_tokens"] is None


def test_watcher_search_is_throttled(env):
    env.sub("abc123")
    assert env.w.poll() == set()
    write(env.sdir / "subagents" / "agent-abc123.jsonl", asst(inp=8))
    env.wclock.advance(1.0)
    assert env.w.poll() == set()
    assert env.agent("abc123")["context_tokens"] is None
    env.wclock.advance(1.5)
    assert env.w.poll() == {SID}
    assert env.agent("abc123")["context_tokens"] == 8


def test_watcher_uses_store_path_and_remembers_found(env):
    env.sub("abc123")
    p = env.sdir / "subagents" / "agent-abc123.jsonl"
    write(p, asst(inp=1))
    env.w.poll()
    # later events do not clear the discovered path
    env.sub("abc123")
    write(p, asst(inp=2), mode="a")
    env.wclock.advance(10)
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 2
    # an explicit path (SubagentStop) takes over from the discovered one
    other = env.root / "elsewhere" / "x.jsonl"
    write(other, asst(inp=77))
    env.ev("SubagentStop", agent_id="abc123", agent_type="Explore", agent_transcript_path=str(other))
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 77


def test_watcher_meta_json_never_a_transcript(env):
    env.sub("abc123")
    write(env.sdir / "subagents" / "agent-abc123.meta.json", {"description": "d", "toolUseId": "t1"})
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] is None
    assert all(not k[2].endswith(".meta.json") for k in env.w._tails)


def test_watcher_meta_links_spawn_call_once(env, monkeypatch):
    env.ev("PreToolUse", tool_name="Agent", tool_use_id="toolu_1",
           tool_input={"subagent_type": "Explore", "description": "orig", "prompt": "go"})
    env.ev("PreToolUse", tool_name="Agent", tool_use_id="toolu_2",
           tool_input={"subagent_type": "Explore", "description": "second", "prompt": "go"})
    env.sub("abc123")  # FIFO would pick toolu_1
    meta = env.sdir / "subagents" / "agent-abc123.meta.json"
    env.w.poll()  # no meta yet: retried later
    write(meta, {"description": "from meta", "toolUseId": "toolu_2", "agentType": "Explore"})
    calls = []
    real = env.store.apply_subagent_meta
    monkeypatch.setattr(env.store, "apply_subagent_meta", lambda *a: (calls.append(a), real(*a))[1])
    env.wclock.advance(3)
    env.w.poll()
    env.wclock.advance(3)
    env.w.poll()
    assert calls == [(SID, "abc123", "from meta", "toolu_2")]
    main = env.agent("main")
    by_id = {c["id"]: c for c in main["calls"]}
    assert by_id["toolu_2"]["subagent_id"] == "abc123"
    assert by_id["toolu_1"]["subagent_id"] is None
    assert env.agent("abc123")["task"] == "from meta"


def test_watcher_meta_throttled_and_defensive(env, monkeypatch):
    env.sub("abc123")
    meta = env.sdir / "subagents" / "agent-abc123.meta.json"
    write(meta, "{not json")
    calls = []
    monkeypatch.setattr(env.store, "apply_subagent_meta", lambda *a: calls.append(a))
    env.w.poll()
    write(meta, {"description": 5, "toolUseId": ["x"]})
    env.wclock.advance(1)
    env.w.poll()
    assert calls == []  # throttled
    env.wclock.advance(2)
    env.w.poll()
    assert calls == [(SID, "abc123", None, None)]


def test_watcher_meta_next_to_transcript_elsewhere(env, monkeypatch):
    env.sub("abc12345")
    write(env.root / "agent-abc12345.jsonl", asst(inp=1))
    write(env.root / "agent-abc12345.meta.json", {"description": "d", "toolUseId": "t9"})
    calls = []
    monkeypatch.setattr(env.store, "apply_subagent_meta", lambda *a: calls.append(a))
    env.w.poll()
    assert calls == [(SID, "abc12345", "d", "t9")]


def test_watcher_poll_returns_right_sessions(env):
    other = "sess-other-0001"
    other_main = env.root / (other + ".jsonl")
    env.store.ingest({"session_id": other, "transcript_path": str(other_main), "cwd": "/w/b",
                      "hook_event_name": "SessionStart"})
    write(env.main, asst(inp=1))
    write(other_main, asst(inp=2))
    assert env.w.poll() == {SID, other}
    write(other_main, asst(inp=3), mode="a")
    assert env.w.poll() == {other}
    assert env.agent(sid=other)["context_tokens"] == 3
    assert env.agent()["context_tokens"] == 1


def test_watcher_drops_state_for_ended_sessions(env):
    write(env.main, asst(inp=1))
    env.w.poll()
    assert env.w._tails
    env.ev("SessionEnd", reason="other")
    assert env.w.poll() == set()
    assert not env.w._tails


def test_watcher_broken_target_does_not_affect_others(env, monkeypatch):
    other = "sess-other-0001"
    other_main = env.root / (other + ".jsonl")
    env.store.ingest({"session_id": other, "transcript_path": str(other_main), "cwd": "/w/b",
                      "hook_event_name": "SessionStart"})
    write(env.main, asst(inp=1))
    write(other_main, asst(inp=2))
    real = env.store.set_context

    def flaky(sid, aid, tokens, model):
        if sid == SID:
            raise RuntimeError("boom")
        return real(sid, aid, tokens, model)

    monkeypatch.setattr(env.store, "set_context", flaky)
    assert env.w.poll() == {SID, other}
    assert env.agent(sid=other)["context_tokens"] == 2


def test_watcher_transcript_targets_failure(env, monkeypatch):
    def boom():
        raise RuntimeError("x")

    monkeypatch.setattr(env.store, "transcript_targets", boom)
    assert env.w.poll() == set()


# ---- hardening ------------------------------------------------------------

def drain(t):
    out = []
    while True:
        entries, grew = t.read()
        if not grew:
            return out
        out.extend(entries)


def test_tail_multibyte_char_split_across_reads(tmp_path):
    p = tmp_path / "t.jsonl"
    objs = [{"t": "é€\U0001f600", "n": i} for i in range(4)]
    p.write_bytes("".join(json.dumps(o, ensure_ascii=False) + "\n" for o in objs).encode("utf-8"))
    for m in range(24, 60):
        assert drain(Tail(p, max_read_bytes=m)) == objs, m


def test_tail_buffer_bounded_without_newline(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b"x" * 1000)
    t = Tail(p, max_read_bytes=10)
    for _ in range(100):
        t.read()
        assert len(t._buf) <= 10
    assert t.read() == ([], False)
    with open(p, "ab") as f:
        f.write(b"\n" + line({"n": 1}).encode())
    assert drain(t) == [{"n": 1}]


def test_tail_runaway_line_then_good_lines(tmp_path):
    p = tmp_path / "t.jsonl"
    write(p, line({"pad": "y" * 200})[:-1], "\n", {"n": 1}, {"n": 2})
    assert drain(Tail(p, max_read_bytes=30)) == [{"n": 1}, {"n": 2}]


def test_tail_partial_dropped_on_truncation(tmp_path):
    p = tmp_path / "t.jsonl"
    p.write_bytes(b'{"a":1,"padpadpadpadpad":')
    t = Tail(p)
    assert t.read() == ([], True)
    p.write_bytes(b"2}\n")  # shorter than the old offset
    assert t.read() == ([], True)  # stale half-line must not be glued onto this


@pytest.mark.parametrize("aid", ["../x", "../../outside/secret", "a/b", "*", "[x]", "x" * 200, "a b", "..", "a\x00b"])
def test_watcher_hostile_agent_ids_never_search(env, tmp_path, aid):
    (tmp_path / "outside").mkdir()
    write(tmp_path / "outside" / "secret.jsonl", asst(inp=777))
    write(env.root / "x.jsonl", asst(inp=778))
    write(env.sdir / "subagents" / "agent-real.jsonl", asst(inp=779))
    env.sub(aid)

    def no(*a, **k):
        raise AssertionError("search or meta lookup with unsafe id")

    env.w._search = no
    env.w._apply_meta = no
    env.w.poll()
    s = env.store.session_dict(SID)
    assert all(a["context_tokens"] is None for a in s["agents"])
    assert env.w._found == {}


def test_watcher_empty_agent_id_is_main(env):
    env.sub("")
    write(env.main, asst(inp=5))
    env.w.poll()
    assert env.agent()["context_tokens"] == 5


def test_watcher_store_path_outside_dir_ignored(env, tmp_path):
    env.sub("abc123")
    outside = tmp_path / "outside" / "secret.jsonl"
    write(outside, asst(inp=777))
    env.ev("SubagentStop", agent_id="abc123", agent_type="Explore", agent_transcript_path=str(outside))
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] is None
    # ... and falls back to normal search
    write(env.sdir / "subagents" / "agent-abc123.jsonl", asst(inp=3))
    env.wclock.advance(5)
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 3


def test_watcher_store_path_wrong_suffix_or_symlink_ignored(env, tmp_path):
    env.sub("abc123")
    bad = env.root / "notes.txt"
    write(bad, asst(inp=9))
    env.ev("SubagentStop", agent_id="abc123", agent_type="Explore", agent_transcript_path=str(bad))
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] is None
    (tmp_path / "outside").mkdir()
    write(tmp_path / "outside" / "s.jsonl", asst(inp=777))
    (env.sdir / "subagents").mkdir(parents=True)
    (env.sdir / "subagents" / "agent-abc123.jsonl").symlink_to(tmp_path / "outside" / "s.jsonl")
    env.wclock.advance(5)
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] is None


def test_watcher_meta_outside_dir_ignored(env, tmp_path, monkeypatch):
    env.sub("abc123")
    out = tmp_path / "outside"
    write(out / "agent-abc123.meta.json", {"description": "EVIL", "toolUseId": "t1"})
    env.sdir.mkdir(parents=True)
    (env.sdir / "subagents").symlink_to(out, target_is_directory=True)
    calls = []
    monkeypatch.setattr(env.store, "apply_subagent_meta", lambda *a: calls.append(a))
    env.w.poll()
    assert calls == []


def test_watcher_non_jsonl_main_path_ignored(env, tmp_path):
    write(tmp_path / "evil.txt", asst(inp=1))
    env.ev("SessionStart", transcript_path=str(tmp_path / "evil.txt"))
    env.w.poll()
    assert env.agent()["context_tokens"] is None


def test_watcher_store_path_replacing_found_is_single_tail(env):
    env.sub("abc123")
    found = env.sdir / "subagents" / "agent-abc123.jsonl"
    write(found, asst(inp=1))
    env.w.poll()
    other = env.root / "other.jsonl"
    write(other, asst(inp=40))
    env.ev("SubagentStop", agent_id="abc123", agent_type="Explore", agent_transcript_path=str(other))
    env.w.poll()
    write(found, asst(inp=999), mode="a")  # old file no longer followed
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 40
    assert [k for k in env.w._tails if k[1] == "abc123"] == [(SID, "abc123", str(other))]


def test_watcher_main_path_change_picks_up_new_file(env):
    write(env.main, asst(inp=1))
    env.w.poll()
    new = env.root / "new-main.jsonl"
    write(new, asst(inp=2))
    env.ev("UserPromptSubmit", prompt="hi", transcript_path=str(new))
    assert env.agent()["context_tokens"] == 1  # kept until the new file reports
    env.w.poll()
    assert env.agent()["context_tokens"] == 2
    assert [k[2] for k in env.w._tails] == [str(new)]


def test_watcher_search_backoff_doubles_and_resets(env):
    env.sub("abc123")
    calls = []
    real = env.w._search
    env.w._search = lambda *a: (calls.append(env.wclock.t), real(*a))[1]
    for t in (0, 1, 2, 5):  # misses at 0 and 2; next attempt due at 6
        env.wclock.t = t
        env.w.poll()
    assert calls == [0, 2]
    env.wclock.t = 6
    env.w.poll()  # miss; wait now 8
    assert calls == [0, 2, 6]
    write(env.sdir / "subagents" / "agent-abc123.jsonl", asst(inp=8))
    env.wclock.t = 13
    env.w.poll()
    assert calls == [0, 2, 6] and env.agent("abc123")["context_tokens"] is None
    env.wclock.t = 14
    env.w.poll()
    assert calls[-1] == 14 and env.agent("abc123")["context_tokens"] == 8
    assert (SID, "abc123") not in env.w._searched  # reset after the hit


def test_watcher_backoff_is_capped(env):
    env.sub("abc123")
    for i in range(20):
        env.wclock.t = i * 100
        env.w.poll()
    nxt, wait = env.w._searched[(SID, "abc123")]
    assert wait <= 30 and nxt - env.wclock.t <= 30


def test_watcher_meta_lookups_back_off(env, monkeypatch):
    env.sub("abc123")
    reads = []
    import cc_mission_control.transcript as tr

    monkeypatch.setattr(tr, "_read_meta", lambda p: (reads.append(env.wclock.t), None)[1])
    for t in (0, 1, 2, 3, 5, 6):
        env.wclock.t = t
        env.w.poll()
    assert sorted(set(reads)) == [0, 2, 6]


def test_watcher_short_aid_skips_loose_patterns(env):
    env.sub("abc123")
    write(env.sdir / "x-abc123-y.jsonl", asst(inp=1))
    write(env.root / "agent-abc123.jsonl", asst(inp=2))
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] is None
    write(env.sdir / "subagents" / "agent-abc123.jsonl", asst(inp=3))
    env.wclock.advance(5)
    env.w.poll()
    assert env.agent("abc123")["context_tokens"] == 3


# ---- session names --------------------------------------------------------

def name(env):
    s = env.store.session_dict(SID)
    return s["name"], s["name_source"], s["title"]


def test_ai_title_latest_wins_and_folder_fallback(env):
    assert name(env) == ("demo", "folder", "demo")
    write(env.main, {"type": "ai-title", "aiTitle": "First idea"}, asst(inp=1), {"type": "ai-title", "aiTitle": "Refined idea"})
    assert env.w.poll() == {SID}
    assert name(env) == ("Refined idea", "generated", "demo")
    env.store.pop_changed()
    write(env.main, {"type": "ai-title", "aiTitle": "Refined idea"}, mode="a")
    env.w.poll()
    assert env.store.pop_changed() == set()  # same title: not a change
    write(env.main, {"type": "ai-title", "aiTitle": "Third"}, mode="a")
    env.w.poll()
    assert name(env)[0] == "Third"


def test_custom_title_record_beats_ai_title(env):
    write(env.main, {"type": "custom-title", "customTitle": "My name"}, {"type": "ai-title", "aiTitle": "Gen"})
    env.w.poll()
    assert name(env) == ("My name", "custom", "demo")


def test_backward_scan_finds_title_before_tail_window_once(env, monkeypatch):
    from cc_mission_control import transcript as tr
    write(env.main, {"type": "ai-title", "aiTitle": "Old but gold"})
    pad = line(asst(inp=1, extra="x" * 5000))
    write(env.main, *([pad] * 500), mode="a")  # ~2.5 MB, pushing the title out of the 2 MB window
    calls = []
    real = tr.scan_titles
    monkeypatch.setattr(tr, "scan_titles", lambda *a, **k: calls.append(1) or real(*a, **k))
    env.w.poll()
    assert name(env)[:2] == ("Old but gold", "generated")
    assert len(calls) == 1
    write(env.main, asst(inp=2), mode="a")
    env.w.poll()
    assert len(calls) == 1


def test_no_backward_scan_when_tail_has_both_titles(env, monkeypatch):
    from cc_mission_control import transcript as tr
    pad = line(asst(inp=1, extra="x" * 5000))
    write(env.main, *([pad] * 500), {"type": "custom-title", "customTitle": "Mine"}, {"type": "ai-title", "aiTitle": "Recent"})
    monkeypatch.setattr(tr, "scan_titles", lambda *a, **k: pytest.fail("should not scan"))
    env.w.poll()
    assert name(env)[:2] == ("Mine", "custom")


def test_scan_titles_ignores_partial_first_line_and_junk(tmp_path):
    from cc_mission_control.transcript import scan_titles
    p = tmp_path / "t.jsonl"
    write(p, {"type": "ai-title", "aiTitle": "cut"}, "not json ai-title\n", {"type": "ai-title", "aiTitle": "kept"}, {"type": "user"})
    assert scan_titles(str(p)) == ("kept", None)
    assert scan_titles(str(p), max_bytes=len(line({"type": "user"})) + 20)[0] is None
    assert scan_titles(str(tmp_path / "missing")) == (None, None)


def test_title_with_secret_is_masked_and_flattened(env):
    secret = "sk-" + "ant-api03-" + "a1B2c3D4e5F6g7H8i9J0" * 2
    write(env.main, {"type": "ai-title", "aiTitle": "Fix\nthe\tbug with " + secret + " " + "z" * 300})
    env.w.poll()
    n = env.store.session_dict(SID)["name"]
    assert "a1B2c3D4e5F6" not in n and "\n" not in n and "\t" not in n and len(n) <= 200
    assert n.startswith("Fix the bug with ")


def test_custom_title_before_window_with_ai_title_inside(env):
    write(env.main, {"type": "custom-title", "customTitle": "Named by user"})
    pad = line(asst(inp=1, extra="x" * 5000))
    write(env.main, *([pad] * 500), {"type": "ai-title", "aiTitle": "Generated"}, mode="a")
    env.w.poll()
    assert name(env)[:2] == ("Named by user", "custom")
    assert env.store._sessions[SID].ai_title == "Generated"


@pytest.mark.parametrize("pad_lines", [0, 1, 40, 52, 53, 120, 410])
def test_scan_titles_distance_from_end(tmp_path, pad_lines):
    from cc_mission_control.transcript import scan_titles
    p = tmp_path / "t.jsonl"
    pad = line({"type": "user", "text": "p" * 4999})  # ~5 KB per line, so 256 KB is ~52 lines
    write(p, {"type": "ai-title", "aiTitle": "old"}, *([pad] * 3), {"type": "custom-title", "customTitle": "cust"},
          {"type": "ai-title", "aiTitle": "new"}, *([pad] * pad_lines))
    assert scan_titles(str(p)) == ("new", "cust")


def test_scan_titles_respects_budget_and_line_across_chunks(tmp_path):
    from cc_mission_control.transcript import scan_titles
    p = tmp_path / "t.jsonl"
    big = {"type": "ai-title", "aiTitle": "T" * 300_000}  # one line spanning several chunks
    write(p, big, {"type": "user"})
    assert scan_titles(str(p))[0] == "T" * 300_000
    pad = line({"type": "user", "text": "p" * 4999})
    write(p, {"type": "ai-title", "aiTitle": "too far"}, *([pad] * 100))
    assert scan_titles(str(p), max_bytes=200_000) == (None, None)
    assert scan_titles(str(p), max_bytes=2_000_000)[0] == "too far"

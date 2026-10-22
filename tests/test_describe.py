import os
import time

import pytest

from cc_mission_control.config import Config
from cc_mission_control.describe import (
    clip,
    describe_call as dc,
    describe_outcome as do,
    plain_denied,
    plain_helper,
    plain_prompt,
)
from cc_mission_control.state import Store

CWD = "/work/demo"


def test_read_variants():
    assert dc("Read", {"file_path": "/work/demo/src/a.py"}, CWD) == "Read src/a.py"
    assert dc("Read", {"file_path": "/work/demo/a.py", "offset": 10, "limit": 5}, CWD) == "Read a.py (lines 10–14)"
    assert dc("Read", {"file_path": "/work/demo/a.py", "limit": 30}, CWD) == "Read a.py (lines 1–30)"
    assert dc("Read", {"file_path": "/work/demo/a.py", "offset": 7}, CWD) == "Read a.py (from line 7)"
    assert dc("Read", {}, CWD) == "Read a file"


def test_paths_relative_home_and_outside():
    assert dc("Write", {"file_path": "/work/demo/x/y.txt"}, CWD) == "Wrote x/y.txt"
    assert dc("Edit", {"file_path": "/etc/hosts"}, CWD) == "Edited etc/hosts"
    assert dc("Edit", {"file_path": "/opt/app/conf/a.toml"}, CWD) == "Edited conf/a.toml"
    assert dc("MultiEdit", {"file_path": "/work/demoX/a"}, CWD) == "Edited demoX/a"
    home = os.path.expanduser("~")
    assert dc("Edit", {"file_path": home + "/notes/a.md"}, CWD) == "Edited ~/notes/a.md"
    assert dc("NotebookEdit", {"notebook_path": "/work/demo/n.ipynb"}, CWD) == "Edited notebook n.ipynb"


def test_bash_description_wins():
    assert dc("Bash", {"command": "ls", "description": "stop the old server"}, CWD) == "Stop the old server"


@pytest.mark.parametrize("cmd,want", [
    ("uv run --extra dev pytest -q", "Ran the test suite"),
    ("cd ui && npm test", "Ran the test suite"),
    ("npx vitest run", "Ran the test suite"),
    ("go test ./...", "Ran the test suite"),
    ("cargo test", "Ran the test suite"),
    ("git status", "Ran git status"),
    ("git -C /x commit -m hi", "Ran git commit"),
    ("npm ci", "Installed packages"),
    ("pip install foo", "Installed packages"),
    ("npm run build", "Built the project"),
    ("docker compose up -d", "Ran docker compose up"),
    ("docker ps", "Ran docker ps"),
    ("curl -s http://127.0.0.1:4317/health", "Made an HTTP request to 127.0.0.1"),
    ("curl https://example.com/x", "Made an HTTP request to example.com"),
    ("ssh me@box uptime", "Ran a command on box over SSH"),
    ("ls -la", "Listed files"),
    ("grep -rn foo .", "Searched files"),
    ("set -e\nexport A=1\ngit push", "Ran git push"),
    ("foo --bar", "Ran a shell command"),
    ("", "Ran a shell command"),
])
def test_bash_classification(cmd, want):
    assert dc("Bash", {"command": cmd}, CWD) == want


def test_other_tools_calls():
    assert dc("Grep", {"pattern": "foo", "path": "/work/demo/src"}, CWD) == 'Searched src for "foo"'
    assert dc("Grep", {"pattern": "foo"}, CWD) == 'Searched the project for "foo"'
    assert dc("Glob", {"pattern": "**/*.py"}, CWD) == "Listed files matching **/*.py"
    assert dc("WebFetch", {"url": "https://docs.example.com/a/b?x=1"}, CWD) == "Fetched docs.example.com"
    assert dc("WebSearch", {"query": "cats"}, CWD) == 'Searched the web for "cats"'
    assert dc("Agent", {"description": "Review it", "subagent_type": "general-purpose"}, CWD) == \
        'Started helper agent "Review it" (general-purpose)'
    assert dc("Task", {}, CWD) == "Started a helper agent"
    assert dc("TodoWrite", {"todos": []}, CWD) == "Updated the task list"
    assert dc("Skill", {"skill": "simplify"}, CWD) == "Used the simplify skill"
    assert dc("AskUserQuestion", {"questions": []}, CWD) == "Asked you a question"
    assert dc("Monitor", {}, CWD) == "Used Monitor"


def test_mcp_names():
    assert dc("mcp__github__create_pull_request", {}, CWD) == "Used create pull request (github)"
    assert dc("mcp__claude_ai_Gmail__search_threads", {}, CWD) == "Used search threads (Gmail)"
    assert dc("mcp__plugin_marketing_notion__fetch", {}, CWD) == "Used fetch (marketing notion)"
    assert dc("mcp__broken", {}, CWD) == "Used mcp__broken"


def test_clip_and_state_caps():
    assert len(clip("x" * 1000, 160)) == 160 and clip("a   b", 10) == "a b"
    st = Store(Config(stale_minutes=60, max_calls=50), clock=lambda: 10.0)
    base = {"session_id": "sess-1234567890", "cwd": CWD}
    st.ingest({**base, "hook_event_name": "PreToolUse", "tool_name": "Glob", "tool_use_id": "t1",
               "tool_input": {"pattern": "y" * 1000}})
    st.ingest({**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Glob", "tool_use_id": "t1",
               "error": "e" * 5000})
    call = st.session_dict("sess-1234567890")["agents"][0]["calls"][0]
    assert len(call["plain"]) <= 160 and len(call["outcome"]) <= 120


def test_outcome_files():
    assert do("Read", {}, {"type": "text", "file": {"numLines": 30, "totalLines": 400}}, None) == "30 lines"
    assert do("Read", {}, {"file": {"content": "a\nb\n"}}, None) == "2 lines"
    assert do("Read", {}, {"file": {"numLines": 1}}, None) == "1 line"
    assert do("Write", {"content": "a\nb\nc"}, {"type": "create"}, None) == "3 lines"
    patch = [{"lines": ["--- a/f", "+++ b/f", " c", "+a", "+b", "-x", " c"]}]
    assert do("Edit", {}, {"structuredPatch": patch}, None) == "+2 −1 lines"
    assert do("Edit", {}, {"structuredPatch": []}, None) == "updated"
    assert do("NotebookEdit", {}, {}, None) == "updated"


def test_outcome_search_tools():
    assert do("Grep", {}, {"mode": "files_with_matches", "numFiles": 3, "filenames": ["a", "b", "c"]}, None) == "3 files"
    assert do("Grep", {}, {"numFiles": 0, "filenames": []}, None) == "no matches"
    assert do("Grep", {}, {"content": "a:1:x\nb:2:y", "numLines": 2}, None) == "2 matches"
    assert do("Grep", {}, "", None) == "no matches"
    assert do("Glob", {}, {"numFiles": 5, "filenames": []}, None) == "5 files"
    assert do("Glob", {}, {"filenames": ["a"]}, None) == "1 file"
    assert do("WebFetch", {}, {"code": 200, "codeText": "OK"}, None) == "HTTP 200 OK"
    assert do("WebFetch", {}, {"result": "x"}, None) == "fetched"
    res = {"results": [{"content": [{"title": "a"}, {"title": "b"}]}, "text"]}
    assert do("WebSearch", {}, res, None) == "2 results"
    assert do("WebSearch", {}, {"results": []}, None) == ""


def test_outcome_misc_tools():
    assert do("Agent", {}, {"status": "completed"}, None) == "finished"
    assert do("Agent", {}, {"status": "async_launched"}, None) == "started in the background"
    todos = {"todos": [{"status": "completed"}, {"status": "pending"}, "junk"]}
    assert do("TodoWrite", todos, {}, None) == "3 tasks, 1 done"
    qs = {"questions": [{"options": [{"label": "Option A"}, {"label": "B"}]}]}
    assert do("AskUserQuestion", qs, {"answers": {"Which?": "Option A", "Other?": "B"}}, None) == "Chose: Option A; B"
    assert do("AskUserQuestion", qs, {"answers": {"Which?": "my own secret text"}}, None) == "You typed an answer"
    mixed = do("AskUserQuestion", qs, {"answers": {"1": "B", "2": "free text"}}, None)
    assert mixed == "Chose: B; You typed an answer" and "free" not in mixed
    assert do("AskUserQuestion", {}, {"answers": {"1": "anything"}}, None) == "You typed an answer"
    assert do("AskUserQuestion", {}, {}, None) == ""
    assert do("Skill", {}, {}, None) == ""
    assert do("mcp__a__b", {}, {"x": 1}, None) == ""


def test_bash_outcomes():
    out = "...\n=========== 489 passed, 1 skipped in 33.5s ===========\n"
    assert do("Bash", {"command": "pytest"}, {"stdout": out, "stderr": ""}, None) == "489 passed, 1 skipped"
    assert do("Bash", {"command": "pytest"}, {"stdout": "== 2 failed, 10 passed in 1.2s ==\n"}, None) == "2 failed, 10 passed"
    assert do("Bash", {}, {"stdout": "\n Test Files  1 passed (1)\n      Tests  12 passed (12)\n"}, None) == "12 passed"
    assert do("Bash", {}, {"stdout": "Tests:       1 failed, 12 passed, 13 total\n"}, None) == "1 failed, 12 passed"
    assert do("Bash", {}, {"stdout": "hello", "stderr": ""}, None) == "done"
    assert do("Bash", {}, {"stdout": "", "interrupted": True}, None) == "interrupted"
    commit = {"stdout": "[main 69b4355] Release 0.3.1: launcher\n 2 files changed\n"}
    assert do("Bash", {"command": "git commit -m x"}, commit, None) == "committed: Release 0.3.1: launcher"
    push = {"stderr": "To github.com:o/r.git\n   abc1234..def5678  main -> main\n"}
    assert do("Bash", {"command": "git push origin main"}, push, None) == "pushed main"


def test_bash_failures():
    assert do("Bash", {}, None, "Exit code 1\nboom: bad thing\nmore") == "exit code 1: boom: bad thing"
    assert do("Bash", {}, None, "Exit code 2") == "exit code 2"
    err = "Exit code 1\n=== 2 failed, 10 passed in 3s ===\n"
    assert do("Bash", {}, None, err) == "2 failed, 10 passed (exit code 1)"
    assert do("Bash", {}, None, "Exit code 127\nShell cwd was reset to /x\nzsh: command not found: foo") == \
        "exit code 127: zsh: command not found: foo"
    assert do("Bash", {}, None, "weird failure") == "Failed: weird failure"


def test_generic_errors_and_rejections():
    assert do("Read", {}, None, "File does not exist.\nNote: cwd") == "Failed: File does not exist."
    assert do("Edit", {}, None, "The user doesn't want to proceed with this tool use.") == "Declined by you"
    assert do("Read", {}, None, {"weird": 1}).startswith("Failed:")
    assert do("Read", {}, {"file": {"numLines": 3}}, "") == "3 lines"


def test_outcome_is_bounded_before_state_clips():
    assert len(do("Read", {}, None, "e" * 50000)) <= 20000


def test_plain_non_tool():
    assert plain_prompt("fix the bug\nplease") == "You asked: fix the bug"
    assert plain_prompt("") == "You sent a message"
    assert plain_helper("Explorer", "started") == 'Helper agent "Explorer" started'
    assert plain_denied("Bash") == "Bash was not allowed"


def test_redaction_applies_through_store():
    secret = "sk-" + "ant-api03-" + "a1B2c3D4e5F6g7H8i9J0" * 2
    st = Store(Config(stale_minutes=60, max_calls=50), clock=lambda: 10.0)
    sid = "sess-1234567890"
    base = {"session_id": sid, "cwd": CWD}
    st.ingest({**base, "hook_event_name": "PreToolUse", "tool_name": "Bash", "tool_use_id": "t1",
               "tool_input": {"command": "x"}})
    st.ingest({**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Bash", "tool_use_id": "t1",
               "tool_input": {"command": "x"}, "error": "Exit code 1\nfailed with " + secret})
    d = st.session_dict(sid)
    call = d["agents"][0]["calls"][0]
    assert secret not in call["outcome"] and secret not in str(d["activity"])
    assert call["outcome"].startswith("exit code 1")


GARBAGE = [None, 0, 7, -1, 1.5, float("nan"), [], [1, "a"], {}, {"a": {"b": [1]}}, "x" * 200000, b"bytes", object()]


@pytest.mark.parametrize("tool", ["Read", "Write", "Edit", "Bash", "Grep", "Glob", "WebFetch", "WebSearch",
                                  "Agent", "TodoWrite", "Skill", "AskUserQuestion", "mcp__a__b", "", None, 5])
def test_never_raises(tool):
    for g in GARBAGE:
        assert isinstance(dc(tool, g, g), str)
        assert isinstance(do(tool, g, g, g), str)
        fields = {"file_path": g, "command": g, "pattern": g, "url": g, "todos": g, "offset": g, "limit": g,
                  "description": g, "path": g, "skill": g, "query": g, "content": g}
        assert len(dc(tool, fields, CWD)) <= 20000
        resp = {"file": g, "stdout": g, "stderr": g, "answers": g, "results": g, "structuredPatch": g,
                "filenames": g, "numFiles": g, "code": g, "content": g, "status": g}
        out = do(tool, fields, resp, g)
        assert isinstance(out, str) and len(out) <= 20000
        assert len(do(tool, fields, resp, None)) <= 20000
    assert isinstance(plain_prompt(None), str) and isinstance(plain_helper(5, "started"), str)


def _token(prefix="ghp_", n=36):
    return prefix + "B" * n


def test_secret_cut_by_the_clip_does_not_survive():
    """Redact first, then clip: a token straddling the 120 / 160 char cut must not leak a prefix."""
    tok = _token()
    st = Store(Config(stale_minutes=60, max_calls=50), clock=lambda: 10.0)
    sid = "sess-1234567890"
    base = {"session_id": sid, "cwd": CWD}
    st.ingest({**base, "hook_event_name": "PreToolUse", "tool_name": "Glob", "tool_use_id": "t1",
               "tool_input": {"pattern": "f" * 140 + " " + tok}})
    st.ingest({**base, "hook_event_name": "PostToolUseFailure", "tool_name": "Read", "tool_use_id": "t1",
               "error": "x" * 100 + " " + tok})
    d = st.session_dict(sid)
    call = d["agents"][0]["calls"][0]
    for text in (call["plain"], call["outcome"], d["activity"][-1]["plain"], d["activity"][0]["plain"]):
        assert "ghp_" not in text and "BBBBBB" not in text


ADVERSARIAL = {
    "spaces": " " * 20000,
    "dashes": "-a " * 6000,
    "ssh_flags": "ssh " + "-a " * 38 + "x",
    "ssh_flags_long": "ssh " + "-a " * 6000,
    "digits": "9" * 20000,
    "digits_space": "1 " * 10000,
    "newlines": "\n" * 20000,
    "equals": "=" * 20000,
    "pytest_like": ("1 passed, " * 2000) + " in 1s",
    "tests_lines": "Tests  " * 3000,
    "commas": ", 1 a" * 4000,
    "mixed": "git -C x " * 2000,
    "uv": "uv run " + "-p 1 " * 3000,
    "arrows": " -> " * 5000,
    "brackets": "[main abcd] " * 1600,
    "lines": "\n".join("x" * 299 for _ in range(60)),
}


@pytest.mark.parametrize("name", sorted(ADVERSARIAL))
def test_adversarial_inputs_are_fast(name):
    payload = ADVERSARIAL[name]
    cut = payload[:2000]
    started = time.perf_counter()
    for text in (payload, cut):
        for tool in ("Bash", "Read", "Grep", "WebFetch", "AskUserQuestion", "Agent", "mcp__a__b"):
            dc(tool, {"command": text, "description": text, "file_path": text, "pattern": text, "url": text,
                      "query": text, "path": text}, CWD)
            for cmd in ("git push", "git commit", "pytest"):
                do(tool, {"command": cmd + " " + text}, {"stdout": text, "stderr": text, "file": {"content": text},
                                                         "content": text}, None)
            do(tool, {"command": text}, None, text)
            do(tool, {"command": "git push"}, {"stdout": text}, "Exit code 1\n" + text)
    assert time.perf_counter() - started < 1.0
    # and per call
    for call in (lambda: dc("Bash", {"command": payload}, CWD), lambda: do("Bash", {"command": "pytest"}, {"stdout": payload}, None),
                 lambda: do("Bash", {}, None, "Exit code 1\n" + payload)):
        t0 = time.perf_counter()
        call()
        assert time.perf_counter() - t0 < 0.05, name


def test_known_redos_shape_is_fast():
    t0 = time.perf_counter()
    for n in (38, 50, 200):
        dc("Bash", {"command": "ssh " + "-a " * n}, CWD)
    assert time.perf_counter() - t0 < 0.05

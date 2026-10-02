"""End-to-end: scripts/simulate.py against a real server process (SPEC 15.2)."""

import importlib.util
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "simulate.py"

spec = importlib.util.spec_from_file_location("ccmc_simulate", SCRIPT)
sim = importlib.util.module_from_spec(spec)
sys.modules["ccmc_simulate"] = sim
spec.loader.exec_module(sim)


def free_port():
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def get(port, path):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=3) as r:
        return r.read().decode()


@pytest.fixture
def server(tmp_path):
    port = free_port()
    env = dict(os.environ, CCMC_PORT=str(port), CCMC_DATA_DIR=str(tmp_path / "data"), CCMC_NO_BROWSER="1")
    env.pop("CCMC_UPSTREAM_URL", None)
    proc = subprocess.Popen(
        [sys.executable, "-m", "cc_mission_control"], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        deadline = time.time() + 15
        while True:
            if proc.poll() is not None:
                pytest.fail("server exited early")
            try:
                if json.loads(get(port, "/health")).get("app") == "cc-mission-control":
                    break
            except OSError:
                pass
            if time.time() > deadline:
                pytest.fail("server did not start")
            time.sleep(0.1)
        yield port
    finally:
        proc.terminate()
        try:
            proc.wait(5)
        except subprocess.TimeoutExpired:
            proc.kill()
            proc.wait()


def sessions(port):
    return json.loads(get(port, "/api/sessions"))


def wait_for(port, pred, timeout=5.0):
    deadline = time.time() + timeout
    while True:
        data = sessions(port)
        if pred(data) or time.time() > deadline:
            return data
        time.sleep(0.1)


def contexts_ready(data, agents=None):
    """Every lane has a gauge and every subagent a task (the meta files were read)."""
    if not data:
        return False
    for s in data:
        if agents is not None and len(s["agents"]) != agents + 1:
            return False
        for a in s["agents"]:
            if a["context_tokens"] is None or (a["id"] != "main" and not a["task"]):
                return False
    return True


def last_main_tokens(transcript_dir, sid):
    """Expected main context: last non-sidechain assistant entry, summed like SPEC 5.2."""
    last = None
    for line in (Path(transcript_dir) / f"{sid}.jsonl").read_text().splitlines():
        e = json.loads(line)
        if e.get("type") == "assistant" and not e.get("isSidechain"):
            u = e["message"]["usage"]
            last = sum(u[k] for k in ("input_tokens", "cache_creation_input_tokens",
                                      "cache_read_input_tokens", "output_tokens"))
    return last


def test_full_scenario(server, tmp_path):
    tdir = tmp_path / "tr"
    ids = sim.run(server, agents=3, speed=200, failures=True, sessions=1, transcript_dir=str(tdir),
                  seed=7, keep_open=True)
    assert len(ids) == 1
    data = wait_for(server, lambda d: contexts_ready(d, 3) and all(
        a["status"] == "done" for a in d[0]["agents"][1:]))
    assert len(data) == 1
    s = data[0]
    assert s["id"] == ids[0]
    assert s["title"] == "fraud-scoring"
    assert s["status"] == "active"  # keep_open: no SessionEnd
    assert len(s["agents"]) == 4
    main = s["agents"][0]
    subs = s["agents"][1:]
    assert main["id"] == "main" and len(subs) == 3
    assert all(a["status"] == "done" for a in subs)
    assert all(a["result"] for a in subs)

    # task == spawn description, and each Agent call points at its own lane (meta linking,
    # including the deliberately out-of-order, same-type pair)
    spawns = [c for c in main["calls"] if c["tool"] == "Agent"]
    assert len(spawns) == 3
    lanes = {a["id"]: a for a in subs}
    assert sorted(c["subagent_id"] for c in spawns) == sorted(lanes)
    for call in spawns:
        detail = json.loads(get(server, f"/api/sessions/{ids[0]}/calls/{call['id']}"))
        assert lanes[call["subagent_id"]]["task"] == detail["input"]["description"]
        assert call["status"] == "ok"
    assert len({a["task"] for a in subs}) == 3

    # context gauges
    assert main["context_window"] == 200000
    assert main["context_tokens"] == last_main_tokens(tdir, ids[0])
    assert main["context_tokens"] < 100000  # the 190k sidechain entry was skipped
    for a in subs:
        assert a["context_tokens"] and 15000 <= a["context_tokens"] <= 100000
        assert a["context_window"] == 200000
    assert main["model"] == "claude-opus-5"

    assert s["compactions"] == 1

    # failures
    all_calls = [c for a in s["agents"] for c in a["calls"]]
    assert any(c["status"] == "error" for c in all_calls)
    assert any(x["kind"] == "error" and "failed" in x["text"] for x in s["activity"])
    assert not any("interrupted" in x["text"] for x in s["activity"])
    assert main["status"] == "error"  # StopFailure
    assert any(x["kind"] == "error" and "Turn failed" in x["text"] for x in s["activity"])

    # redaction
    key = sim.fake_key(ids[0])
    blob = get(server, "/api/sessions")
    for a in s["agents"]:
        for c in a["calls"]:
            blob += get(server, f"/api/sessions/{ids[0]}/calls/{c['id']}")
    assert key not in blob
    assert key.split("api03-")[1] not in blob
    assert "[redacted]" in blob


def test_session_ends(server, tmp_path):
    ids = sim.run(server, agents=2, speed=200, failures=False, transcript_dir=str(tmp_path), seed=3)
    data = wait_for(server, lambda d: contexts_ready(d, 2) and d[0]["status"] == "ended")
    s = data[0]
    assert s["id"] == ids[0] and s["status"] == "ended"
    assert len(s["agents"]) == 3
    assert all(a["status"] == "done" for a in s["agents"][1:])
    assert all(a["context_tokens"] for a in s["agents"])
    assert all(c["status"] == "ok" for a in s["agents"] for c in a["calls"])


def test_agents_zero(server, tmp_path):
    sim.run(server, agents=0, speed=200, transcript_dir=str(tmp_path), seed=1, keep_open=True)
    s = wait_for(server, contexts_ready)[0]
    assert len(s["agents"]) == 1 and s["compactions"] == 1


def test_two_sessions(server, tmp_path):
    ids = sim.run(server, agents=2, speed=200, sessions=2, transcript_dir=str(tmp_path), seed=5)
    assert len(set(ids)) == 2
    data = wait_for(server, lambda d: len(d) == 2 and contexts_ready(d))
    assert {s["id"] for s in data} == set(ids)
    assert len({s["title"] for s in data}) == 2
    assert all(len(s["agents"]) == 3 for s in data)


def test_unreachable_port_exits_1(tmp_path):
    t0 = time.time()
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--port", str(free_port()), "--speed", "100",
         "--transcript-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=20,
    )
    assert r.returncode == 1
    assert "Cannot reach cc-mission-control" in r.stderr
    assert r.stdout == ""
    assert time.time() - t0 < 10
    assert "Traceback" not in r.stdout + r.stderr


def test_cli_end_to_end(server, tmp_path):
    r = subprocess.run(
        [sys.executable, str(SCRIPT), "--port", str(server), "--speed", "200", "--agents", "1",
         "--seed", "2", "--transcript-dir", str(tmp_path)],
        capture_output=True, text=True, timeout=30,
    )
    assert r.returncode == 0, r.stdout + r.stderr
    assert "1 session(s)" in r.stdout
    assert len(sessions(server)) == 1


def test_bad_ccmc_port_is_an_argparse_error(tmp_path):
    r = subprocess.run([sys.executable, str(SCRIPT)], capture_output=True, text=True, timeout=20,
                       env=dict(os.environ, CCMC_PORT="not-a-port"))
    assert r.returncode == 2
    assert "invalid port" in r.stderr and "Traceback" not in r.stderr


def test_one_m_window(server, tmp_path):
    sim.run(server, agents=1, speed=200, transcript_dir=str(tmp_path), seed=4, keep_open=True,
            model="claude-opus-5[1m]")
    s = wait_for(server, lambda d: contexts_ready(d, 1))[0]
    assert s["agents"][0]["context_window"] == 1000000
    assert s["agents"][1]["context_window"] == 200000


def test_foreground_flow(server, tmp_path):
    ids = sim.run(server, agents=2, speed=200, transcript_dir=str(tmp_path), seed=6, keep_open=True,
                  foreground=True)
    s = wait_for(server, lambda d: contexts_ready(d, 2))[0]
    spawns = [c for c in s["agents"][0]["calls"] if c["tool"] == "Agent"]
    assert len(spawns) == 2 and all(c["status"] == "ok" for c in spawns)
    meta = list((tmp_path / ids[0] / "subagents").glob("*.meta.json"))
    assert len(meta) == 2
    assert all(json.loads(m.read_text())["requestShape"] == "foreground" for m in meta)


class Capture(sim.Poster):
    def __init__(self):
        super().__init__(0)
        self.events = []

    def check(self):
        pass

    def post(self, event):
        with self._lock:
            self.events.append(event)


def capture(tmp_path, name, **kw):
    cap = Capture()
    sim.run(agents=3, speed=1000, failures=True, seed=11, settle=0, poster=cap,
            transcript_dir=str(tmp_path / name), keep_open=False, **kw)
    return cap.events


def test_seed_is_deterministic(tmp_path):
    a, b = capture(tmp_path, "a"), capture(tmp_path, "b")

    def key(e):
        return (e["hook_event_name"], e.get("tool_use_id"), e.get("agent_id"))

    from collections import Counter, defaultdict

    assert Counter(map(key, a)) == Counter(map(key, b))

    def per_agent(events):
        out = defaultdict(list)
        for e in events:
            out[e.get("agent_id")].append(key(e))
        return dict(out)

    assert per_agent(a) == per_agent(b)
    # bare ids by default, and the async flow: Post(Agent) comes before SubagentStop
    ids = {e["agent_id"] for e in a if e.get("agent_id")}
    assert len(ids) == 3 and all(len(i) == 17 and not i.startswith("agent-") for i in ids)
    posts = [e for e in a if e["hook_event_name"] == "PostToolUse" and e.get("tool_name") == "Agent"]
    assert len(posts) == 3 and all(p["tool_response"]["status"] == "async_launched" for p in posts)
    assert all(p["tool_response"]["agentId"] in ids for p in posts)
    names = [e["hook_event_name"] for e in a]
    assert names.index("PostToolUse") < len(names)  # sanity
    for e in a:
        has = "permission_mode" in e
        assert has == (e["hook_event_name"] in sim.PERMISSION_EVENTS), e["hook_event_name"]

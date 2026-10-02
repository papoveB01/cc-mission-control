"""Launcher tests (SPEC 8, milestone 6). The launcher is loaded from scripts/launch.py."""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import signal
import socket
import subprocess
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parent.parent
SCRIPT = REPO / "scripts" / "launch.py"

_spec = importlib.util.spec_from_file_location("ccmc_launch", SCRIPT)
launch = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(launch)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


class FakeService:
    """Tiny http server answering GET anything with a fixed body."""

    def __init__(self, body: bytes):
        outer = body

        class Handler(BaseHTTPRequestHandler):
            def do_GET(self):  # noqa: N802
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.end_headers()
                self.wfile.write(outer)

            def log_message(self, *a):
                pass

        self.httpd = HTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.httpd.server_address[1]
        threading.Thread(target=self.httpd.serve_forever, daemon=True).start()

    def close(self):
        self.httpd.shutdown()
        self.httpd.server_close()


@pytest.fixture
def fake_service():
    made = []

    def make(body: bytes):
        svc = FakeService(body)
        made.append(svc)
        return svc

    yield make
    for svc in made:
        svc.close()


@pytest.fixture
def inproc(monkeypatch, tmp_path):
    """In-process main(): keep pytest's fd 1 and sys.stdout intact, isolate env, silence side effects."""
    monkeypatch.setattr(os, "dup2", lambda *a: None)
    monkeypatch.setattr(sys, "stdout", sys.stdout)  # restored after main() reassigns it
    for name in ("CCMC_PORT", "CCMC_DATA_DIR", "CLAUDE_PLUGIN_DATA", "CCMC_NO_BROWSER", "CLAUDE_CODE_REMOTE"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("CCMC_DATA_DIR", str(tmp_path / "data"))
    monkeypatch.setenv("CCMC_PORT", str(free_port()))
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO(json.dumps({"session_id": "s1", "source": "startup"})))
    calls = {"popen": [], "post": [], "open": []}
    monkeypatch.setattr(launch, "post_event", lambda port, ev: calls["post"].append(ev))
    monkeypatch.setattr(launch, "open_browser", lambda url: calls["open"].append(url))
    return calls


def test_no_stdout_server_already_running(inproc, monkeypatch, capfd):
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: ("ours", {"app": "cc-mission-control", "clients": 1}))
    assert launch.main() == 0
    out, _ = capfd.readouterr()
    assert out == ""
    assert inproc["post"] == [{"session_id": "s1", "source": "startup"}]


def test_no_stdout_not_running_and_starts(inproc, monkeypatch, capfd):
    states = iter([("down", None)])
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: next(states, ("ours", {"app": "cc-mission-control", "clients": 0})))
    monkeypatch.setattr(launch, "start_server", lambda cfg: type("P", (), {"poll": lambda self: None})())
    monkeypatch.setattr(launch.time, "sleep", lambda s: None)
    assert launch.main() == 0
    out, _ = capfd.readouterr()
    assert out == ""
    assert inproc["open"] and inproc["post"]


def test_no_stdout_no_uv_no_fastapi(inproc, monkeypatch, capfd):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: None)
    spawned = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert launch.main() == 0
    out, err = capfd.readouterr()
    assert out == ""
    assert "uv" in err and "https://docs.astral.sh/uv/" in err
    assert spawned == []
    assert inproc["post"] == [] and inproc["open"] == []


def test_foreign_service_on_port(inproc, monkeypatch, capfd, fake_service):
    svc = fake_service(b'{"app": "something-else"}')
    monkeypatch.setenv("CCMC_PORT", str(svc.port))
    spawned = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    assert launch.main() == 0
    out, err = capfd.readouterr()
    assert out == ""
    assert f"port {svc.port} is used by another service" in err
    assert spawned == [] and inproc["post"] == []


def test_invalid_stdin(inproc, monkeypatch, capfd):
    monkeypatch.setattr(sys, "stdin", __import__("io").StringIO("{not json"))
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: ("ours", {"app": "cc-mission-control", "clients": 0}))
    assert launch.main() == 0
    out, _ = capfd.readouterr()
    assert out == ""
    # invalid stdin is treated as {} (still a dict), so the event is forwarded and the browser may open
    assert inproc["post"] == [{}]


def test_exception_inside_main(inproc, monkeypatch, capfd):
    def boom():
        raise RuntimeError("kaboom")

    monkeypatch.setattr(launch, "settings", boom)
    assert launch.main() == 0
    out, err = capfd.readouterr()
    assert out == ""
    assert "kaboom" in err


def test_exit_code_zero_as_subprocess(tmp_path):
    env = {**os.environ, "CCMC_PORT": str(free_port()), "CCMC_DATA_DIR": str(tmp_path), "PATH": str(tmp_path)}
    env["CLAUDE_PLUGIN_ROOT"] = str(tmp_path)  # no project there, so any start would fail
    # make it impossible to find fastapi too, via a python that cannot import it
    proc = subprocess.run([sys.executable, "-S", str(SCRIPT)], input=b"garbage", env=env,
                          capture_output=True, timeout=30)
    assert proc.returncode == 0
    assert proc.stdout == b""


def test_health_identity(fake_service):
    other = fake_service(b'{"app": "something-else"}')
    assert launch.health(other.port) == ("foreign", None)
    junk = fake_service(b"<html>hello</html>")
    assert launch.health(junk.port) == ("foreign", None)
    listy = fake_service(b"[1, 2]")
    assert launch.health(listy.port) == ("foreign", None)
    ours = fake_service(b'{"app": "cc-mission-control", "clients": 0}')
    state, data = launch.health(ours.port)
    assert state == "ours" and data["clients"] == 0
    assert launch.health(free_port()) == ("down", None)


def test_settings_precedence(tmp_path):
    root = str(tmp_path / "root")
    both = {"CCMC_DATA_DIR": str(tmp_path / "a"), "CLAUDE_PLUGIN_DATA": str(tmp_path / "b"), "CCMC_PORT": "5000",
            "CLAUDE_PLUGIN_ROOT": root}
    cfg = launch.settings(both)
    assert cfg["data_dir"] == tmp_path / "a" and cfg["port"] == 5000 and cfg["root"] == Path(root)
    assert launch.settings({"CLAUDE_PLUGIN_DATA": str(tmp_path / "b")})["data_dir"] == tmp_path / "b"
    default = launch.settings({})
    assert default["data_dir"] == Path.home() / ".cc-mission-control"
    assert default["port"] == 4317
    assert default["root"] == REPO
    assert launch.settings({"CCMC_PORT": "bogus"})["port"] == 4317
    assert launch.settings({"CCMC_PORT": " 6000 "})["port"] == 6000


def test_command_uv(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/usr/local/bin/uv")
    cfg = {"port": 1, "data_dir": tmp_path / "d", "root": tmp_path / "r"}
    argv, env = launch.build_command(cfg, {"CCMC_PORT": "9"})
    assert argv[:2] == ["/usr/local/bin/uv", "run"]
    assert "--frozen" in argv and "--no-dev" in argv
    assert argv[argv.index("--project") + 1] == str(tmp_path / "r")
    assert argv[-3:] == ["python", "-m", "cc_mission_control"]
    assert env["UV_PROJECT_ENVIRONMENT"] == str(tmp_path / "d" / "venv")
    assert env["CCMC_DATA_DIR"] == str(tmp_path / "d") and env["CCMC_PORT"] == "9"
    assert env["PYTHONSAFEPATH"] == "1"
    _, env2 = launch.build_command(cfg, {"PYTHONPATH": "/x"})
    assert "PYTHONPATH" not in env2


def test_command_fallback_python(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: object())
    cfg = {"port": 1, "data_dir": tmp_path / "d", "root": tmp_path / "r"}
    argv, env = launch.build_command(cfg, {"PYTHONPATH": "/x"})
    assert argv == [sys.executable, "-m", "cc_mission_control"]
    assert env["PYTHONPATH"] == str(tmp_path / "r")  # inherited entries dropped
    assert env["PYTHONSAFEPATH"] == "1"


def test_command_neither_spawns_nothing(monkeypatch, tmp_path, capfd):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    monkeypatch.setattr(launch.importlib.util, "find_spec", lambda name: None)
    spawned = []
    monkeypatch.setattr(launch.subprocess, "Popen", lambda *a, **k: spawned.append(a))
    cfg = {"port": 1, "data_dir": tmp_path / "d", "root": tmp_path / "r"}
    assert launch.start_server(cfg) is None
    out, err = capfd.readouterr()
    assert out == "" and "uv" in err and spawned == []


def test_start_server_popen_args(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/bin/uv")
    seen = {}

    def fake_popen(argv, **kw):
        seen.update(kw, argv=argv)
        return "proc"

    monkeypatch.setattr(launch.subprocess, "Popen", fake_popen)
    cfg = {"port": 1, "data_dir": tmp_path / "d", "root": tmp_path / "r"}
    assert launch.start_server(cfg) == "proc"
    assert seen["stdin"] == subprocess.DEVNULL and seen["close_fds"] is True
    if os.name != "nt":
        assert seen["start_new_session"] is True
    assert (tmp_path / "d" / "server.log").exists()
    assert seen["cwd"] == str(tmp_path / "d")


def test_start_server_cwd_falls_back_to_root(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/bin/uv")
    seen = {}
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: seen.update(kw) or "proc")
    blocker = tmp_path / "file"
    blocker.write_text("x")
    cfg = {"port": 1, "data_dir": blocker / "sub", "root": tmp_path / "r"}  # mkdir must fail
    assert launch.start_server(cfg) == "proc"
    assert seen["cwd"] == str(tmp_path / "r")


def test_log_rotation(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: "proc")
    data = tmp_path / "d"
    data.mkdir()
    (data / "server.log").write_bytes(b"x" * (launch.LOG_MAX_BYTES + 1))
    (data / "server.log.1").write_bytes(b"old")
    launch.start_server({"port": 1, "data_dir": data, "root": tmp_path})
    assert (data / "server.log.1").stat().st_size == launch.LOG_MAX_BYTES + 1
    assert (data / "server.log").stat().st_size == 0


def test_log_not_rotated_when_small(monkeypatch, tmp_path):
    monkeypatch.setattr(launch.shutil, "which", lambda name: "/bin/uv")
    monkeypatch.setattr(launch.subprocess, "Popen", lambda argv, **kw: "proc")
    (tmp_path / "server.log").write_bytes(b"small")
    launch.start_server({"port": 1, "data_dir": tmp_path, "root": tmp_path})
    assert not (tmp_path / "server.log.1").exists()
    assert (tmp_path / "server.log").read_bytes() == b"small"


def test_late_server_event_forwarded_without_browser(inproc, monkeypatch, capfd):
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: ("down", None))
    monkeypatch.setattr(launch, "start_server", lambda cfg: type("P", (), {"poll": lambda self: None})())
    monkeypatch.setattr(launch, "wait_healthy", lambda port, proc, deadline: None)
    assert launch.main() == 0
    out, err = capfd.readouterr()
    assert out == "" and "server.log" in err
    assert inproc["post"] == [{"session_id": "s1", "source": "startup"}]
    assert inproc["open"] == []


def test_dead_child_event_not_forwarded(inproc, monkeypatch):
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: ("down", None))
    monkeypatch.setattr(launch, "start_server", lambda cfg: type("P", (), {"poll": lambda self: 1})())
    monkeypatch.setattr(launch, "wait_healthy", lambda port, proc, deadline: None)
    assert launch.main() == 0
    assert inproc["post"] == [] and inproc["open"] == []


def test_no_opener_logs_instead_of_webbrowser(monkeypatch, capfd):
    monkeypatch.setattr(launch.shutil, "which", lambda name: None)
    import webbrowser

    monkeypatch.setattr(webbrowser, "open", lambda *a: pytest.fail("webbrowser must not be used"))
    if sys.platform == "win32":
        pytest.skip("startfile path")
    launch.open_browser("http://127.0.0.1:1/")
    out, err = capfd.readouterr()
    assert out == "" and "http://127.0.0.1:1/" in err


BROWSER_OK = {"app": "cc-mission-control", "clients": 0}


@pytest.mark.parametrize(
    "health_data, event, env, stamp_age",
    [
        ({"clients": 1}, {"source": "startup"}, {}, None),
        (BROWSER_OK, {"source": "startup"}, {"CCMC_NO_BROWSER": "1"}, None),
        (BROWSER_OK, {"source": "startup"}, {"CLAUDE_CODE_REMOTE": "true"}, None),
        (BROWSER_OK, {"source": "clear"}, {}, None),
        (BROWSER_OK, {"source": "compact"}, {}, None),
        (BROWSER_OK, {"source": "startup"}, {}, 3),
    ],
)
def test_browser_not_opened(tmp_path, health_data, event, env, stamp_age):
    if stamp_age is not None:
        stamp = tmp_path / "browser.lock"
        stamp.write_text("x")
        t = time.time() - stamp_age
        os.utime(stamp, (t, t))
    assert launch.should_open_browser(health_data, event, tmp_path, env) is False


@pytest.mark.parametrize("event", [{}, {"source": "startup"}, {"source": "resume"}])
def test_browser_opened(tmp_path, event):
    assert launch.should_open_browser(BROWSER_OK, event, tmp_path, {}) is True
    assert (tmp_path / "browser.lock").exists()
    # immediately again: debounced
    assert launch.should_open_browser(BROWSER_OK, event, tmp_path, {}) is False


def test_browser_old_stamp_allows_open(tmp_path):
    stamp = tmp_path / "browser.lock"
    stamp.write_text("x")
    t = time.time() - 60
    os.utime(stamp, (t, t))
    assert launch.should_open_browser(BROWSER_OK, {}, tmp_path, {}) is True
    assert time.time() - stamp.stat().st_mtime < 5


def test_claim_browser_concurrent_exactly_one_wins(tmp_path):
    results, barrier = [], threading.Barrier(5)

    def worker():
        barrier.wait()
        results.append(launch.claim_browser(tmp_path))

    threads = [threading.Thread(target=worker) for _ in range(5)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()
    assert results.count(True) == 1 and len(results) == 5


def _restart_setup(inproc, monkeypatch, tmp_path, marker, reconnect_after):
    """Server 'just started' with clients == 0; later health calls show a client after N polls."""
    data = tmp_path / "data"
    data.mkdir(exist_ok=True)
    if marker:
        (data / "browser.opened").write_text("x")
    calls = {"n": 0, "sleeps": 0}

    def fake_health(port, timeout=0.5):
        calls["n"] += 1
        if calls["n"] == 1:
            return "down", None
        clients = 1 if reconnect_after is not None and calls["n"] - 1 > reconnect_after else 0
        return "ours", {"app": "cc-mission-control", "clients": clients}

    monkeypatch.setattr(launch, "health", fake_health)
    monkeypatch.setattr(launch, "start_server", lambda cfg: type("P", (), {"poll": lambda self: None})())
    monkeypatch.setattr(launch, "wait_healthy", lambda port, proc, deadline: {"app": "cc-mission-control", "clients": 0})
    monkeypatch.setattr(launch, "POLL_SECONDS", 0.01)
    monkeypatch.setattr(launch, "RECONNECT_WAIT_SECONDS", 0.3)
    real_sleep = time.sleep

    def counting_sleep(sec):
        calls["sleeps"] += 1
        real_sleep(sec)

    monkeypatch.setattr(launch.time, "sleep", counting_sleep)
    return calls


def test_restart_recent_marker_client_reconnects_no_open(inproc, monkeypatch, tmp_path):
    calls = _restart_setup(inproc, monkeypatch, tmp_path, marker=True, reconnect_after=2)
    assert launch.main() == 0
    assert inproc["open"] == [] and inproc["post"]
    assert calls["sleeps"] >= 1


def test_restart_recent_marker_no_reconnect_opens(inproc, monkeypatch, tmp_path):
    calls = _restart_setup(inproc, monkeypatch, tmp_path, marker=True, reconnect_after=None)
    t0 = time.monotonic()
    assert launch.main() == 0
    assert len(inproc["open"]) == 1
    assert calls["sleeps"] >= 1 and time.monotonic() - t0 >= 0.25
    assert (tmp_path / "data" / "browser.opened").exists()


def test_restart_no_marker_opens_immediately(inproc, monkeypatch, tmp_path):
    calls = _restart_setup(inproc, monkeypatch, tmp_path, marker=False, reconnect_after=None)
    assert launch.main() == 0
    assert len(inproc["open"]) == 1 and calls["sleeps"] == 0
    assert (tmp_path / "data" / "browser.opened").exists()  # marker is created when we open


def test_after_idle_shutdown_marker_removed_decides_immediately(inproc, monkeypatch, tmp_path):
    # the server deletes browser.opened when it shuts down with no clients, so the next start sees no marker
    marker = tmp_path / "data" / "browser.opened"
    marker.parent.mkdir(exist_ok=True)
    marker.write_text("x")
    marker.unlink()
    calls = _restart_setup(inproc, monkeypatch, tmp_path, marker=False, reconnect_after=None)
    assert launch.main() == 0
    assert len(inproc["open"]) == 1 and calls["sleeps"] == 0


def test_restart_stale_marker_does_not_wait(inproc, monkeypatch, tmp_path):
    calls = _restart_setup(inproc, monkeypatch, tmp_path, marker=True, reconnect_after=None)
    old = time.time() - 13 * 3600
    os.utime(tmp_path / "data" / "browser.opened", (old, old))
    assert launch.main() == 0
    assert len(inproc["open"]) == 1 and calls["sleeps"] == 0


def test_already_running_does_not_wait(inproc, monkeypatch, tmp_path):
    data = tmp_path / "data"
    data.mkdir()
    (data / "browser.opened").write_text("x")
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: ("ours", {"app": "cc-mission-control", "clients": 0}))
    slept = []
    monkeypatch.setattr(launch.time, "sleep", lambda s: slept.append(s))
    assert launch.main() == 0
    assert slept == [] and len(inproc["open"]) == 1


def test_stamp_written_before_open(inproc, monkeypatch, tmp_path):
    monkeypatch.setattr(launch, "health", lambda port, timeout=0.5: ("ours", dict(BROWSER_OK)))
    stamp = tmp_path / "data" / "browser.lock"
    seen = []
    monkeypatch.setattr(launch, "open_browser", lambda url: seen.append((url, stamp.exists())))
    assert launch.main() == 0
    assert len(seen) == 1 and seen[0][1] is True
    assert seen[0][0].startswith("http://127.0.0.1:") and seen[0][0].endswith("/")


# ---------------------------------------------------------------- integration (real processes)


def _get_json(port: int, path: str, timeout: float = 1.0):
    with urllib.request.urlopen(f"http://127.0.0.1:{port}{path}", timeout=timeout) as r:
        return json.loads(r.read())


def _port_free(port: int) -> bool:
    with socket.socket() as s:
        return s.connect_ex(("127.0.0.1", port)) != 0


def _run_launcher(tmp_path, port, sid, path_value, extra_env=None, cwd=None):
    env = {
        "PATH": path_value,
        "HOME": str(tmp_path / "home"),
        "CCMC_PORT": str(port),
        "CCMC_DATA_DIR": str(tmp_path / "data"),
        "CCMC_NO_BROWSER": "1",
        "CCMC_IDLE_MINUTES": "0",
        "CLAUDE_PLUGIN_ROOT": str(REPO),
    }
    env.update(extra_env or {})
    event = {"session_id": sid, "hook_event_name": "SessionStart", "source": "startup",
             "cwd": str(tmp_path), "transcript_path": ""}
    return subprocess.run([sys.executable, str(SCRIPT)], input=json.dumps(event).encode(), env=env,
                          capture_output=True, timeout=30, cwd=cwd)


def _wait(cond, seconds):
    end = time.time() + seconds
    while time.time() < end:
        if cond():
            return True
        time.sleep(0.2)
    return cond()


def _stop_server(tmp_path, port):
    pid_file = tmp_path / "data" / "server.pid"
    try:
        pid = int(pid_file.read_text().strip())
    except (OSError, ValueError):
        return
    try:
        os.kill(pid, signal.SIGTERM)
    except OSError:
        return
    if not _wait(lambda: _port_free(port), 10):
        try:
            os.kill(pid, signal.SIGKILL)
        except OSError:
            pass
        _wait(lambda: _port_free(port), 5)


def _integration(tmp_path, path_value, extra_env=None, health_wait=5):
    port = free_port()
    (tmp_path / "home").mkdir(exist_ok=True)
    # hooks run inside the user's project: a malicious uvicorn.py there must never be imported
    evil = tmp_path / "project"
    evil.mkdir()
    marker = tmp_path / "PWNED"
    (evil / "uvicorn.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    (evil / "fastapi.py").write_text(f"open({str(marker)!r}, 'w').close()\n")
    try:
        first = _run_launcher(tmp_path, port, "sess-one", path_value, extra_env, cwd=str(evil))
        assert first.returncode == 0, first.stderr
        assert first.stdout == b""
        assert _wait(lambda: _port_free(port) is False, health_wait)
        assert _get_json(port, "/health")["app"] == "cc-mission-control"
        assert "sess-one" in [s["id"] for s in _get_json(port, "/api/sessions")]
        assert (tmp_path / "data" / "server.log").exists()
        pid1 = (tmp_path / "data" / "server.pid").read_text().strip()

        second = _run_launcher(tmp_path, port, "sess-two", path_value, extra_env, cwd=str(evil))
        assert second.returncode == 0, second.stderr
        assert second.stdout == b""
        assert (tmp_path / "data" / "server.pid").read_text().strip() == pid1
        ids = [s["id"] for s in _get_json(port, "/api/sessions")]
        assert "sess-one" in ids and "sess-two" in ids
        assert not marker.exists()
    finally:
        _stop_server(tmp_path, port)
    assert _port_free(port)


def test_integration_fallback_python(tmp_path):
    empty_bin = tmp_path / "emptybin"
    empty_bin.mkdir()
    assert shutil.which("uv", path=str(empty_bin)) is None
    _integration(tmp_path, str(empty_bin))


@pytest.mark.skipif(os.environ.get("CCMC_SLOW_TESTS") != "1", reason="set CCMC_SLOW_TESTS=1 for the uv path")
def test_integration_uv(tmp_path):
    uv = shutil.which("uv")
    if not uv:
        pytest.skip("uv not on PATH")
    path_value = os.pathsep.join([str(Path(uv).parent), os.environ.get("PATH", "")])
    _integration(tmp_path, path_value, {"UV_PROJECT_ENVIRONMENT": str(tmp_path / "uvenv"),
                                         "UV_CACHE_DIR": os.environ.get("UV_CACHE_DIR", str(Path.home() / ".cache" / "uv"))},
                 health_wait=15)


# ---------------------------------------------------------------- time bounds


class DripService:
    """Sends headers, then one body byte every 0.3 s for a long while."""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(8)
        self.port = self.sock.getsockname()[1]
        self.stop = False
        threading.Thread(target=self._serve, daemon=True).start()

    def _serve(self):
        while not self.stop:
            try:
                conn, _ = self.sock.accept()
            except OSError:
                return
            threading.Thread(target=self._drip, args=(conn,), daemon=True).start()

    def _drip(self, conn):
        try:
            conn.recv(4096)
            conn.sendall(b"HTTP/1.0 200 OK\r\nContent-Type: application/json\r\n\r\n")
            for _ in range(300):
                if self.stop:
                    break
                conn.sendall(b" ")
                time.sleep(0.3)
        except OSError:
            pass
        finally:
            conn.close()

    def close(self):
        self.stop = True
        self.sock.close()


def _bounded_env(tmp_path, port):
    return {"PATH": str(tmp_path), "HOME": str(tmp_path), "CCMC_PORT": str(port), "CCMC_DATA_DIR": str(tmp_path / "data"),
            "CCMC_NO_BROWSER": "1", "CCMC_LAUNCH_DEADLINE": "4", "CLAUDE_PLUGIN_ROOT": str(tmp_path)}


def test_slow_drip_health_is_bounded(tmp_path):
    svc = DripService()
    try:
        t0 = time.monotonic()
        proc = subprocess.run([sys.executable, str(SCRIPT)], input=b'{"session_id": "x"}',
                              env=_bounded_env(tmp_path, svc.port), capture_output=True, timeout=30)
        elapsed = time.monotonic() - t0
    finally:
        svc.close()
    assert proc.returncode == 0 and proc.stdout == b""
    assert elapsed < 8


def test_health_deadline_in_process(monkeypatch):
    svc = DripService()
    try:
        t0 = time.monotonic()
        assert launch.health(svc.port) == ("down", None)
        assert time.monotonic() - t0 < 3
    finally:
        svc.close()


def test_open_stdin_is_bounded(tmp_path):
    proc = subprocess.Popen([sys.executable, str(SCRIPT)], stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                            stderr=subprocess.PIPE, env=_bounded_env(tmp_path, free_port()))
    try:
        t0 = time.monotonic()
        proc.wait(timeout=20)  # stdin stays open: communicate() would close it
        elapsed = time.monotonic() - t0
        out = proc.stdout.read()
    finally:
        if proc.poll() is None:
            proc.kill()
        for f in (proc.stdin, proc.stdout, proc.stderr):
            f.close()
    assert proc.returncode == 0 and out == b""
    assert elapsed < 10

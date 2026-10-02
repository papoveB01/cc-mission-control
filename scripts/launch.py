"""SessionStart launcher (SPEC section 8).

Hard rules: standard library only, never write to stdout (SessionStart stdout is
injected into Claude's context), always exit 0, finish well inside the 20 s hook timeout.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import subprocess
import sys
import threading
import time
import urllib.error
import urllib.request
from pathlib import Path

APP_ID = "cc-mission-control"
BUDGET_SECONDS = 18.0
WATCHDOG_SECONDS = 17.0  # hard stop, under the 20 s hook timeout; CCMC_LAUNCH_DEADLINE overrides
HEALTH_DEADLINE_SECONDS = 1.0
RECONNECT_WAIT_SECONDS = 6.0  # after a restart, let an already-open tab reconnect before opening another
OPENED_MARKER_MAX_AGE = 12 * 3600
LOG_MAX_BYTES = 5 * 1024 * 1024
START_WAIT_SECONDS = 15.0
POLL_SECONDS = 0.25
BROWSER_DEBOUNCE_SECONDS = 15.0


def log(message: str) -> None:
    print(f"cc-mission-control: {message}", file=sys.stderr)


def read_event() -> object:
    """The SessionStart JSON from stdin; {} when empty or invalid."""
    try:
        raw = sys.stdin.read()
        return json.loads(raw) if raw.strip() else {}
    except Exception:  # noqa: BLE001
        return {}


def settings(env=None) -> dict:
    """Mirrors config.py: CCMC_PORT, CCMC_DATA_DIR > CLAUDE_PLUGIN_DATA > ~/.cc-mission-control."""
    env = os.environ if env is None else env
    try:
        port = int(env.get("CCMC_PORT", "").strip() or 4317)
    except ValueError:
        port = 4317
    data_dir = env.get("CCMC_DATA_DIR", "").strip()
    plugin_data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
    if data_dir:
        data = Path(data_dir).expanduser()
    elif plugin_data:
        data = Path(plugin_data)
    else:
        data = Path.home() / ".cc-mission-control"
    root = env.get("CLAUDE_PLUGIN_ROOT", "").strip()
    return {
        "port": port,
        "data_dir": data,
        "root": Path(root) if root else Path(__file__).resolve().parent.parent,
    }


def health(port: int, timeout: float = 0.5):
    """('ours', data) | ('foreign', None) when something else answers | ('down', None)."""
    url = f"http://127.0.0.1:{port}/health"
    deadline = time.monotonic() + HEALTH_DEADLINE_SECONDS  # a slow-drip body must not stall us
    try:
        with urllib.request.urlopen(url, timeout=timeout) as resp:
            chunks, total = [], 0
            while total < 65536:
                if time.monotonic() > deadline:
                    return "down", None
                chunk = resp.read1(4096) if hasattr(resp, "read1") else resp.read(4096)
                if not chunk:
                    break
                chunks.append(chunk)
                total += len(chunk)
            body = b"".join(chunks)
    except urllib.error.HTTPError:
        return "foreign", None
    except Exception:  # noqa: BLE001  (refused, timeout, reset ...)
        return "down", None
    try:
        data = json.loads(body)
    except ValueError:
        return "foreign", None
    if isinstance(data, dict) and data.get("app") == APP_ID:
        return "ours", data
    return "foreign", None


def build_command(cfg: dict, env=None):
    """(argv, child env) for starting the server, or None when no way to run it exists."""
    env = dict(os.environ if env is None else env)
    data_dir, root = cfg["data_dir"], cfg["root"]
    env["CCMC_DATA_DIR"] = str(data_dir)
    env["PYTHONSAFEPATH"] = "1"  # no implicit cwd on sys.path (hooks run inside the user's project)
    uv = shutil.which("uv")
    if uv:
        env["UV_PROJECT_ENVIRONMENT"] = str(data_dir / "venv")
        env.pop("PYTHONPATH", None)
        argv = [uv, "run", "--quiet", "--frozen", "--no-dev", "--project", str(root),
                "python", "-m", "cc_mission_control"]
        return argv, env
    if importlib.util.find_spec("fastapi") and importlib.util.find_spec("uvicorn"):
        env["PYTHONPATH"] = str(root)
        return [sys.executable, "-m", "cc_mission_control"], env
    return None


def rotate_log(path: Path) -> None:
    try:
        if path.stat().st_size > LOG_MAX_BYTES:
            os.replace(path, str(path) + ".1")
    except OSError:
        pass


def start_server(cfg: dict):
    """Spawn the detached server; returns the Popen, or None (with a stderr hint) if it can't."""
    built = build_command(cfg)
    if built is None:
        log("needs uv: https://docs.astral.sh/uv/ (install it, then start a new session); "
            "alternatively pip install fastapi uvicorn for python3")
        return None
    argv, env = built
    data_dir = cfg["data_dir"]
    cwd = str(data_dir)  # never the project dir: a repo-local uvicorn.py must not be importable
    try:
        data_dir.mkdir(parents=True, exist_ok=True)
        rotate_log(data_dir / "server.log")
        out = open(data_dir / "server.log", "ab")
    except OSError:
        out = subprocess.DEVNULL
        if not data_dir.is_dir():
            cwd = str(cfg["root"])
    kwargs = {}
    if os.name == "nt":
        kwargs["creationflags"] = (getattr(subprocess, "DETACHED_PROCESS", 0x00000008)
                                   | getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0x00000200))
    else:
        kwargs["start_new_session"] = True
    try:
        return subprocess.Popen(argv, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=out,
                                close_fds=True, cwd=cwd, **kwargs)
    except OSError as exc:
        log(f"could not start the server: {exc}")
        return None
    finally:
        if out is not subprocess.DEVNULL:
            out.close()


def wait_healthy(port: int, proc, deadline: float):
    """Poll /health until ours answers; None if the child exits or the deadline passes."""
    while True:
        state, data = health(port)
        if state == "ours":
            return data
        if proc is not None and proc.poll() is not None:
            return None
        if time.monotonic() + POLL_SECONDS > deadline:
            return None
        time.sleep(POLL_SECONDS)


def post_event(port: int, event: dict) -> None:
    req = urllib.request.Request(
        f"http://127.0.0.1:{port}/hook",
        data=json.dumps(event).encode(),
        headers={"Content-Type": "application/json"},
        method="POST",
    )
    try:
        with urllib.request.urlopen(req, timeout=1.0) as resp:
            resp.read()
    except Exception as exc:  # noqa: BLE001
        log(f"could not forward the SessionStart event: {exc}")


def claim_browser(data_dir: Path) -> bool:
    """Atomically claim the right to open the browser; a lock younger than 15 s means someone else did."""
    lock = Path(data_dir) / "browser.lock"
    try:
        lock.parent.mkdir(parents=True, exist_ok=True)
        for _ in range(2):
            try:
                os.close(os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600))
                return True
            except FileExistsError:
                try:
                    if time.time() - lock.stat().st_mtime < BROWSER_DEBOUNCE_SECONDS:
                        return False
                    lock.unlink()
                except FileNotFoundError:
                    pass  # vanished between calls; retry the claim
        return False
    except OSError:
        return True  # can't debounce; better one tab than none


def browser_allowed(event: dict, env=None) -> bool:
    """The environment and event-source conditions (everything except clients and the lock)."""
    env = os.environ if env is None else env
    if env.get("CCMC_NO_BROWSER", "").strip() == "1" or env.get("CLAUDE_CODE_REMOTE", "").strip() == "true":
        return False
    return event.get("source") in (None, "startup", "resume")


def should_open_browser(health_data: dict, event: dict, data_dir: Path, env=None) -> bool:
    """All conditions of SPEC 8.1 step 6. Claims the debounce lock when it returns True."""
    if health_data.get("clients") != 0 or not browser_allowed(event, env):
        return False
    return claim_browser(data_dir)


def opened_recently(data_dir: Path) -> bool:
    try:
        return time.time() - (Path(data_dir) / "browser.opened").stat().st_mtime < OPENED_MARKER_MAX_AGE
    except OSError:
        return False


def touch_opened(data_dir: Path) -> None:
    try:
        (Path(data_dir) / "browser.opened").touch()
    except OSError:
        pass


def wait_for_client(port: int, data: dict, deadline: float) -> dict:
    """Poll /health until a dashboard connects; returns the latest health (or `data` if none arrived)."""
    end = min(time.monotonic() + RECONNECT_WAIT_SECONDS, deadline)
    while time.monotonic() + POLL_SECONDS <= end:
        time.sleep(POLL_SECONDS)
        state, latest = health(port)
        if state == "ours":
            data = latest
            if data.get("clients") != 0:
                break
    return data


def open_browser(url: str) -> None:
    try:
        if sys.platform == "win32":
            os.startfile(url)  # type: ignore[attr-defined]
            return
        opener = "open" if sys.platform == "darwin" else "xdg-open"
        path = shutil.which(opener)
        if path:
            subprocess.Popen([path, url], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                             stderr=subprocess.DEVNULL, start_new_session=True, close_fds=True)
        else:
            log(f"no browser opener ({opener}) found; open {url} manually")
    except Exception as exc:  # noqa: BLE001
        log(f"could not open the browser: {exc}")


def _watchdog_fire() -> None:
    try:
        log("launcher timed out; exiting")
    finally:
        os._exit(0)


def start_watchdog() -> threading.Timer:
    try:
        seconds = float(os.environ.get("CCMC_LAUNCH_DEADLINE", "") or WATCHDOG_SECONDS)
    except ValueError:
        seconds = WATCHDOG_SECONDS
    timer = threading.Timer(seconds, _watchdog_fire)
    timer.daemon = True
    timer.start()
    return timer


def main() -> int:
    watchdog = None
    try:
        # fd-level guard: nothing, not even a child process, may reach Claude's stdout
        try:
            os.dup2(2, 1)
        except Exception:  # noqa: BLE001
            pass
        sys.stdout = sys.stderr
        watchdog = start_watchdog()
        started = time.monotonic()
        event = read_event()
        cfg = settings()
        port = cfg["port"]
        state, data = health(port)
        just_started = False
        if state == "foreign":
            log(f"port {port} is used by another service; not starting the server "
                "(set CCMC_PORT and update hooks.json to use a different port)")
            return 0
        if state == "down":
            proc = start_server(cfg)
            if proc is None:
                return 0
            deadline = started + min(START_WAIT_SECONDS, BUDGET_SECONDS)
            data = wait_healthy(port, proc, deadline)
            if data is None:
                log(f"the server did not become healthy; see {cfg['data_dir'] / 'server.log'}")
                # still starting: forward the event once so the session is captured if it came up late
                if isinstance(event, dict) and proc.poll() is None and time.monotonic() < started + BUDGET_SECONDS - 1.5:
                    post_event(port, event)
                return 0
            just_started = True
        if isinstance(event, dict):
            post_event(port, event)
        ev = event if isinstance(event, dict) else {}
        if (just_started and data.get("clients") == 0 and browser_allowed(ev)
                and opened_recently(cfg["data_dir"])):
            data = wait_for_client(port, data, started + BUDGET_SECONDS - 2)
        if should_open_browser(data, ev, cfg["data_dir"]):
            touch_opened(cfg["data_dir"])
            open_browser(f"http://127.0.0.1:{port}/")
    except BaseException as exc:  # noqa: BLE001
        try:
            log(f"launcher error: {exc!r}")
        except Exception:  # noqa: BLE001
            pass
    finally:
        if watchdog is not None:
            watchdog.cancel()
    return 0


if __name__ == "__main__":
    main()
    sys.exit(0)

"""The local history worker, supervised by the server (SPEC 13.12).

The server starts `python -m cc_mission_control.hub --local` as a child process (same interpreter), gives it a
fresh random token on stdin, learns its port from one JSON line on its stdout, restarts it with a backoff when it
exits, and stops it by its recorded pid when the server stops. The worker's output goes to
`<data_dir>/history/worker.log`.
"""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import secrets
import shutil
import subprocess
import sys
import threading
import time
from pathlib import Path
from typing import Any, Callable, Deque, List, Optional
from collections import deque

from .config import Config

log = logging.getLogger(__name__)

BACKOFF = (1.0, 5.0, 30.0, 120.0)  # seconds before the 1st..4th restart
MAX_EXITS = 5  # this many exits ...
EXIT_WINDOW = 600.0  # ... within this many seconds and the server stops trying
RETRY_AFTER_GIVE_UP = 600.0  # after giving up, a fresh round of attempts every 10 minutes
LOG_CHECK = 30.0  # seconds between size checks of worker.log while the worker runs
START_TIMEOUT = 30.0  # how long a new worker may take to report its port
STOP_GRACE = 3.0  # SIGTERM, then SIGKILL after this long (TerminateProcess on Windows)
DISK_LOW_BYTES = 2 * 1024 ** 3
LOG_MAX = 1024 * 1024  # worker.log is rotated once at this size

DOWN_ALERT = {"level": "watch", "code": "history_down", "text": "History is not recording. Live still works."}


def disk_low_alert(free: int) -> dict:
    gib = free / 1024 ** 3
    return {"level": "watch", "code": "history_disk_low",
            "text": f"The disk holding the data folder has {gib:.1f} GB free, so history may stop recording."}


def is_windows() -> bool:
    return os.name == "nt"


def history_dir(config: Config) -> Path:
    return config.data_dir / "history"


def worker_command(executable: Optional[str] = None) -> List[str]:
    return [executable or sys.executable, "-m", "cc_mission_control.hub", "--local"]


def new_token() -> str:
    return secrets.token_urlsafe(32)  # 32 random bytes


class HistorySupervisor:
    """Starts, watches, restarts and stops the worker. `base_url` and `token` are None while it is not up."""

    def __init__(
        self,
        config: Config,
        *,
        popen: Callable[..., Any] = subprocess.Popen,
        executable: Optional[str] = None,
        monotonic: Callable[[], float] = time.monotonic,
        sleep: Callable[[float], Any] = asyncio.sleep,
        on_target: Optional[Callable[[Optional[str], Optional[str]], None]] = None,
        disk_free: Optional[Callable[[Path], int]] = None,
        env: Optional[dict] = None,
        start_timeout: float = START_TIMEOUT,
        stop_grace: float = STOP_GRACE,
    ) -> None:
        self.config = config
        self.dir = history_dir(config)
        self.popen = popen
        self.executable = executable
        self.monotonic = monotonic
        self.sleep = sleep
        self.on_target = on_target
        self.disk_free = disk_free or (lambda p: shutil.disk_usage(p).free)
        self.env = env
        self.start_timeout = start_timeout
        self.stop_grace = stop_grace
        self.state = "starting"  # starting | ok | down
        self.gave_up = False
        self.port: Optional[int] = None
        self.token: Optional[str] = None
        self.proc: Any = None
        self.restarts = 0
        self.exits: Deque[float] = deque()
        self._lock = threading.Lock()
        self._task: Optional["asyncio.Task"] = None
        self._stopped = False

    # ---- what the rest of the server reads

    @property
    def base_url(self) -> Optional[str]:
        return f"http://127.0.0.1:{self.port}" if self.state == "ok" and self.port else None

    @property
    def pid(self) -> Optional[int]:
        return getattr(self.proc, "pid", None)

    @property
    def log_path(self) -> Path:
        return self.dir / "worker.log"

    @property
    def pid_path(self) -> Path:
        return self.dir / "worker.pid"

    def alerts(self) -> List[dict]:
        """System alerts (11.16) about history: down, and low disk space on the data folder's volume."""
        out: List[dict] = []
        if self.state == "down":
            out.append(dict(DOWN_ALERT))
        try:
            free = self.disk_free(self._existing_dir())
            if free < DISK_LOW_BYTES:
                out.append(disk_low_alert(free))
        except OSError:
            pass
        return out

    def _existing_dir(self) -> Path:
        p = self.config.data_dir
        for cand in (p, *p.parents):
            if cand.exists():
                return cand
        return Path(".")

    # ---- starting

    def _open_log(self):
        self.dir.mkdir(parents=True, exist_ok=True)
        path = self.log_path
        with contextlib.suppress(OSError):
            if path.stat().st_size > LOG_MAX:
                os.replace(path, str(path) + ".1")
        return open(path, "ab")

    def rotate_log(self) -> bool:
        """Keep worker.log under LOG_MAX while the worker runs: copy it to worker.log.1 and empty it in place (the
        worker keeps appending to the same file). True when it rotated.

        Not on Windows: the worker's inherited stderr handle has no append flag there, so after the truncate it would
        keep writing at its old offset and leave a NUL-filled file that is still over the limit. Windows rotates at
        worker start only (`_open_log`)."""
        if is_windows():
            return False
        path = self.log_path
        try:
            if path.stat().st_size <= LOG_MAX:
                return False
            shutil.copyfile(path, str(path) + ".1")
            with open(path, "r+b") as f:
                f.truncate(0)
            return True
        except OSError:
            return False

    def _child_env(self) -> dict:
        env = dict(os.environ if self.env is None else self.env)
        for name in ("CCMC_HUB_TOKEN", "CCMC_HUB_BIND", "CCMC_HUB_DB", "CCMC_UPSTREAM_URL", "CCMC_UPSTREAM_TOKEN"):
            env.pop(name, None)  # the worker takes its settings from CCMC_HISTORY_* and the data folder
        env["CCMC_DATA_DIR"] = str(self.config.data_dir)
        env["CCMC_HISTORY_PORT"] = str(self.config.history_port)
        env["PYTHONUNBUFFERED"] = "1"
        return env

    def _spawn(self, token: str) -> Any:
        logf = self._open_log()
        try:
            kwargs: dict = {}
            if os.name == "nt":
                kwargs["creationflags"] = getattr(subprocess, "CREATE_NO_WINDOW", 0)
            proc = self.popen(
                worker_command(self.executable), stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=logf,
                env=self._child_env(), close_fds=True, **kwargs,
            )
        finally:
            logf.close()  # the child holds its own copy
        with self._lock:  # recorded at once, so a stop that raced with this thread can still find it
            stopped = self._stopped
            if not stopped:
                self.proc = proc
        if stopped:
            self._stop_proc(proc)
            raise OSError("the supervisor was stopped while the worker started")
        try:
            proc.stdin.write((token + "\n").encode("ascii"))
            proc.stdin.flush()  # stdin stays open: its end tells the worker that the server is gone
        except (OSError, ValueError):
            pass
        with contextlib.suppress(OSError):
            self.dir.mkdir(parents=True, exist_ok=True)
            self.pid_path.write_text(str(proc.pid))
        return proc

    def _read_port(self, proc: Any) -> Optional[int]:
        """Block until the worker prints its `{"port": N}` line (run in a thread). None when it never does."""
        result: List[Optional[int]] = []

        def read() -> None:
            try:
                line = proc.stdout.readline()
                port = json.loads(line)["port"] if line else None
                result.append(port if isinstance(port, int) and 0 < port < 65536 else None)
            except (OSError, ValueError, KeyError, TypeError):
                result.append(None)

        t = threading.Thread(target=read, daemon=True)
        t.start()
        t.join(self.start_timeout)
        return result[0] if result else None

    def _drain(self, proc: Any) -> None:
        """Keep reading what is left on the worker's stdout so a full pipe never blocks it."""
        def run() -> None:
            with contextlib.suppress(OSError, ValueError):
                while proc.stdout.read(4096):
                    pass

        threading.Thread(target=run, daemon=True).start()

    def _set_target(self, port: Optional[int], token: Optional[str]) -> None:
        self.port, self.token = port, token
        if self.on_target is not None:
            try:
                self.on_target(f"http://127.0.0.1:{port}" if port and self.state == "ok" else None, token)
            except Exception:
                log.debug("history target callback failed", exc_info=True)

    async def start_once(self) -> bool:
        """Start one worker and wait for its port. False (state `down`) when it could not be started."""
        token = new_token()
        self.state = "starting"
        try:
            proc = await asyncio.to_thread(self._spawn, token)
        except (OSError, ValueError) as exc:
            log.error("cannot start the history worker (%s)", type(exc).__name__)
            self.state = "down"
            return False
        port = await asyncio.to_thread(self._read_port, proc)
        if port is None or proc.poll() is not None:
            log.error("the history worker did not start; see %s", self.log_path)
            await asyncio.to_thread(self._stop_proc, proc)
            self.state = "down"
            return False
        self.state = "ok"
        self.gave_up = False
        self._set_target(port, token)
        self._drain(proc)
        log.info("history worker pid %s on 127.0.0.1:%d", proc.pid, port)
        return True

    # ---- the supervising loop

    async def run(self) -> None:
        """Start the worker and keep it running: restart after 1, 5, 30 and 120 s; give up on the 5th exit in 10 min."""
        while not self._stopped:
            started = await self.start_once()
            if started:
                proc = self.proc
                last_check = self.monotonic()
                while not self._stopped and proc.poll() is None:
                    await self.sleep(0.5)
                    if self.monotonic() - last_check >= LOG_CHECK:
                        last_check = self.monotonic()
                        await asyncio.to_thread(self.rotate_log)
                if self._stopped:
                    return
                log.warning("the history worker exited with %s", proc.returncode)
            self.state = "down"
            self._set_target(None, None)
            now = self.monotonic()
            self.exits.append(now)
            while self.exits and now - self.exits[0] > EXIT_WINDOW:
                self.exits.popleft()
            if len(self.exits) >= MAX_EXITS:
                self.gave_up = True
                log.error("the history worker exited %d times in %d minutes; trying again in %d minutes",
                          len(self.exits), EXIT_WINDOW // 60, RETRY_AFTER_GIVE_UP // 60)
                await self.sleep(RETRY_AFTER_GIVE_UP)
                self.exits.clear()  # a fresh round, with the same backoff for each attempt
                self.restarts += 1
                continue
            delay = BACKOFF[min(len(self.exits) - 1, len(BACKOFF) - 1)]
            log.info("restarting the history worker in %d s", delay)
            await self.sleep(delay)
            self.restarts += 1

    # ---- stopping

    def _stop_proc(self, proc: Any) -> None:
        """Stop exactly this process: terminate (SIGTERM, or TerminateProcess on Windows), kill after the grace period."""
        if proc is None or proc.poll() is not None:
            return
        with contextlib.suppress(OSError, ValueError):
            proc.terminate()
        try:
            proc.wait(timeout=self.stop_grace)
        except subprocess.TimeoutExpired:
            with contextlib.suppress(OSError, ValueError):
                proc.kill()
            with contextlib.suppress(Exception):
                proc.wait(timeout=self.stop_grace)
        for stream in (getattr(proc, "stdin", None), getattr(proc, "stdout", None)):
            with contextlib.suppress(Exception):
                if stream is not None:
                    stream.close()

    def stop_sync(self) -> None:
        """Stop the worker and forget it. Safe to call twice."""
        with self._lock:
            self._stopped = True
            proc, self.proc = self.proc, None
        self._stop_proc(proc)
        if proc is not None:
            with contextlib.suppress(OSError):
                if self.pid_path.read_text().strip() == str(proc.pid):
                    self.pid_path.unlink()
        self.state = "down"
        self.port = self.token = None

    async def stop(self) -> None:
        self._stopped = True
        if self._task is not None:
            self._task.cancel()
            with contextlib.suppress(BaseException):
                await self._task
            self._task = None
        await asyncio.to_thread(self.stop_sync)

    def start(self) -> "asyncio.Task":
        """Run the supervising loop as a task on the running event loop."""
        self._task = asyncio.ensure_future(self.run())
        return self._task


# ---- the old relay spool (SPEC 13.12, upgrade from 0.12.x) --------------------------------------------------------

SPOOL_MARKER = ".local-history"  # inside relay/: from here on every segment there belongs to the local worker
UNSENT_DIR = "relay-remote-unsent"


def set_aside_remote_spool(data_dir: Path) -> int:
    """Move spool segments left by a remote-hub relay to `relay-remote-unsent/`, once.

    Their records came from sessions the local database never saw, so the local worker must not get them; a later pro
    install can send them. A marker file inside relay/ makes it happen only once: it is written first, and when it
    cannot be written nothing is moved (a second run would otherwise move the local worker's own segments).
    Returns the number of files moved."""
    relay_dir = Path(data_dir) / "relay"
    marker = relay_dir / SPOOL_MARKER
    try:
        if marker.exists():
            return 0
        relay_dir.mkdir(mode=0o700, parents=True, exist_ok=True)
        old = sorted(relay_dir.glob("seg-*.jsonl"))
        marker.write_text("local history: segments in this folder are for the local worker\n")
    except OSError as exc:
        log.warning("cannot check the old relay spool (%s)", type(exc).__name__)
        return 0
    if not old:
        return 0
    dest = Path(data_dir) / UNSENT_DIR
    moved = 0
    try:
        dest.mkdir(mode=0o700, parents=True, exist_ok=True)
        for seg in old:
            target = dest / seg.name
            if target.exists():
                target = dest / f"{seg.stem}-{time.time_ns()}{seg.suffix}"
            os.replace(seg, target)
            moved += 1
    except OSError as exc:
        log.warning("could not move all old relay spool files (%s)", type(exc).__name__)
    if moved:
        log.info("moved %d relay spool file(s) from a remote hub to %s; they are not sent to local history", moved, dest)
    return moved

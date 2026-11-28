"""Entry point: python -m cc_mission_control (SPEC section 9)."""

from __future__ import annotations

import logging
import os
import socket
import sys

from .config import Config

log = logging.getLogger("cc_mission_control")


def _remove_pid(path, pid: int) -> None:
    try:
        if path.read_text().strip() == str(pid):
            path.unlink()
    except OSError:
        pass


def bind_socket(port: int, *, windows: bool = os.name == "nt", factory=socket.socket):
    """A listening socket on 127.0.0.1:<port>; raises OSError when it cannot bind.

    On Windows SO_REUSEADDR lets a second process bind the same address and port and receive hook
    payloads, so Windows uses SO_EXCLUSIVEADDRUSE instead and never SO_REUSEADDR. POSIX keeps SO_REUSEADDR.
    """
    sock = factory(socket.AF_INET, socket.SOCK_STREAM)
    try:
        if windows:
            sock.setsockopt(socket.SOL_SOCKET, getattr(socket, "SO_EXCLUSIVEADDRUSE", ~socket.SO_REUSEADDR), 1)
        else:
            sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # host is fixed: the dashboard must never be reachable beyond loopback
        sock.bind(("127.0.0.1", port))
        sock.listen(128)
    except OSError:
        sock.close()
        raise
    return sock


def _ws_impl() -> str:
    """The sans-io websockets protocol where uvicorn has it; the legacy one prints a deprecation warning."""
    from uvicorn.config import WS_PROTOCOLS

    return "websockets-sansio" if "websockets-sansio" in WS_PROTOCOLS else "auto"


def main() -> int:
    import uvicorn

    from .server import CONTROL_FILE, create_app

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    try:
        sock = bind_socket(config.port)
    except OSError as exc:
        log.error("cannot listen on 127.0.0.1:%d (%s); not starting", config.port, exc)
        return 1
    pid = os.getpid()
    pid_file = config.data_dir / "server.pid"
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid))
    except OSError as exc:
        log.error("cannot write %s (%s); continuing without a pid file", pid_file, exc)
    from .history import HistorySupervisor

    history = HistorySupervisor(config) if config.history else None  # SPEC 13.12: the local history worker
    try:
        app = create_app(config, control_path=config.data_dir / CONTROL_FILE, history=history)
        server = uvicorn.Server(
            uvicorn.Config(app, host="127.0.0.1", port=config.port, log_level="warning", ws=_ws_impl())
        )

        def stop() -> None:
            server.should_exit = True

        app.state.request_shutdown = stop
        # uvicorn re-raises SIGTERM/SIGINT after serve(), so clean up while the lifespan unwinds
        app.state.on_shutdown.append(lambda: _remove_pid(pid_file, pid))
        server.run(sockets=[sock])
    finally:
        if history is not None:
            history.stop_sync()  # the lifespan already stopped it; this covers a crash before or after it
        _remove_pid(pid_file, pid)
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

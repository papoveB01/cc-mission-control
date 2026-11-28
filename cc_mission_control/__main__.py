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


def _ws_impl() -> str:
    """The sans-io websockets protocol where uvicorn has it; the legacy one prints a deprecation warning."""
    from uvicorn.config import WS_PROTOCOLS

    return "websockets-sansio" if "websockets-sansio" in WS_PROTOCOLS else "auto"


def main() -> int:
    import uvicorn

    from .server import create_app

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    config = Config.from_env()
    sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    try:
        sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        # host is fixed: the dashboard must never be reachable beyond loopback
        sock.bind(("127.0.0.1", config.port))
        sock.listen(128)
    except OSError as exc:
        sock.close()
        log.error("cannot listen on 127.0.0.1:%d (%s); not starting", config.port, exc)
        return 1
    pid = os.getpid()
    pid_file = config.data_dir / "server.pid"
    try:
        config.data_dir.mkdir(parents=True, exist_ok=True)
        pid_file.write_text(str(pid))
    except OSError as exc:
        log.error("cannot write %s (%s); continuing without a pid file", pid_file, exc)
    try:
        app = create_app(config)
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
        _remove_pid(pid_file, pid)
        sock.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())

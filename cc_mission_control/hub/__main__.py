"""Entry point: python -m cc_mission_control.hub --local (SPEC 13.12).

The free edition runs the hub code as the local history worker. The remote hub (Docker, Tailscale, a shared
token) is part of the pro edition, so running this module without --local only prints a pointer.
"""

from __future__ import annotations

import json
import logging
import os
import sys
import threading
from pathlib import Path
from typing import Optional, Sequence

from ..config import Config, HubConfig, MIN_HUB_TOKEN

log = logging.getLogger("cc_mission_control.hub")

PRO_NOTICE = (
    "cc-mission-control: the remote hub is part of the pro edition; "
    "this edition keeps history on this computer and starts it itself."
)
LOOPBACK = "127.0.0.1"
NICE = 10


def history_dir(config: Config) -> Path:
    return config.data_dir / "history"


def local_hub_config(config: Config, token: str, port: int, bind: str = LOOPBACK) -> HubConfig:
    """The worker's settings: one database under the data folder, only the two loopback Host values for its port."""
    return HubConfig(
        token=token,
        db=history_dir(config) / "history.db",
        port=port,
        bind=bind,
        hosts=(f"{LOOPBACK}:{port}", f"localhost:{port}"),
        tailnet_hosts=False,
        retention_days=config.history_retention_days,
        backups=config.history_backups,
        pricing_file=config.pricing_file,
        local=True,
    )


def read_token(stream=None) -> Optional[str]:
    """The per-start token: one line on stdin, never argv, env or a file. None when it is missing or too short."""
    stream = sys.stdin if stream is None else stream
    try:
        line = stream.readline()
    except (OSError, ValueError):
        return None
    token = line.strip()
    return token if len(token) >= MIN_HUB_TOKEN else None


def _watch_stdin(stream, on_eof) -> None:
    """The server keeps our stdin open for as long as it lives; its end (even a crash) means we stop."""
    try:
        while stream.read(4096):
            pass
    except (OSError, ValueError):
        pass
    on_eof()


def run_local(stdin=None, stdout=None) -> int:
    from ..__main__ import bind_socket

    stdout = sys.stdout if stdout is None else stdout
    stdin = sys.stdin if stdin is None else stdin
    bind = os.environ.get("CCMC_HUB_BIND", "").strip() or LOOPBACK
    if bind != LOOPBACK:  # refused before anything else happens, not silently replaced
        print(f"ccmc-history: --local binds {LOOPBACK} only (got {bind})", file=sys.stderr)
        return 2
    token = read_token(stdin)
    if token is None:
        print(f"ccmc-history: expected a token of at least {MIN_HUB_TOKEN} characters on stdin", file=sys.stderr)
        return 2
    config = Config.from_env()
    try:
        sock = bind_socket(config.history_port)
    except OSError:
        sock = bind_socket(0)  # the usual port is busy: let the OS pick a free loopback one
    port = sock.getsockname()[1]
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    cfg = local_hub_config(config, token, port)
    problem = cfg.problem()
    if problem:
        print(f"ccmc-history: {problem}", file=sys.stderr)
        sock.close()
        return 2
    try:
        history_dir(config).mkdir(parents=True, exist_ok=True)
        app = create_app(cfg, store_config=config)
    except Exception as exc:  # unreadable or too-new database
        print(f"ccmc-history: cannot open {cfg.db}: {exc}", file=sys.stderr)
        sock.close()
        return 1
    if hasattr(os, "nice"):  # POSIX only: rebuilds and rescoring must not slow Live
        try:
            os.nice(NICE)
        except OSError:
            pass
    server = uvicorn.Server(uvicorn.Config(app, log_level="info", access_log=False, lifespan="on"))
    threading.Thread(target=_watch_stdin, args=(stdin, lambda: setattr(server, "should_exit", True)), daemon=True).start()
    stdout.write(json.dumps({"port": port}) + "\n")
    stdout.flush()
    try:
        server.run(sockets=[sock])
    finally:
        sock.close()
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    if "--local" not in args:
        print(PRO_NOTICE, file=sys.stderr)
        return 2
    return run_local()


if __name__ == "__main__":
    sys.exit(main())

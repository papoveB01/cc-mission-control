"""Entry point: ccmc-hub / python -m cc_mission_control.hub (SPEC 13.2)."""

from __future__ import annotations

import logging
import sys

from ..config import HubConfig

log = logging.getLogger("cc_mission_control.hub")


def main() -> int:
    cfg = HubConfig.from_env()
    problem = cfg.problem()
    if problem:
        print(f"ccmc-hub: {problem}", file=sys.stderr)
        return 2
    import uvicorn

    from .app import create_app

    logging.basicConfig(level=logging.INFO, stream=sys.stderr, format="%(asctime)s %(levelname)s %(name)s: %(message)s")
    try:
        app = create_app(cfg)
    except Exception as exc:  # unreadable or too-new database
        print(f"ccmc-hub: cannot open {cfg.db}: {exc}", file=sys.stderr)
        return 1
    uvicorn.run(app, host=cfg.bind, port=cfg.port, log_level="info", access_log=False)
    return 0


if __name__ == "__main__":
    sys.exit(main())

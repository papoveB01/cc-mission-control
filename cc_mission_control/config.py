"""CCMC_* settings (SPEC section 12), read once from the environment."""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping


def _int(env: Mapping[str, str], name: str, default: int) -> int:
    raw = env.get(name, "").strip()
    if not raw:
        return default
    try:
        return int(raw)
    except ValueError:
        return default


def _default_data_dir(env: Mapping[str, str]) -> Path:
    plugin_data = env.get("CLAUDE_PLUGIN_DATA", "").strip()
    if plugin_data:
        return Path(plugin_data)
    return Path.home() / ".cc-mission-control"


@dataclass(frozen=True)
class Config:
    port: int = 4317
    data_dir: Path = field(default_factory=lambda: Path.home() / ".cc-mission-control")
    context_window: int = 200_000
    idle_minutes: int = 30
    stale_minutes: int = 60
    max_calls: int = 500
    max_field_chars: int = 4000
    redact_file: Path | None = None
    no_browser: bool = False
    upstream_url: str | None = None
    upstream_token: str | None = None
    dev_origins: tuple[str, ...] = ()

    @classmethod
    def from_env(cls, env: Mapping[str, str] | None = None) -> "Config":
        env = os.environ if env is None else env
        data_dir = env.get("CCMC_DATA_DIR", "").strip()
        redact_file = env.get("CCMC_REDACT_FILE", "").strip()
        dev_origins = env.get("CCMC_DEV_ORIGINS", "")
        return cls(
            port=_int(env, "CCMC_PORT", 4317),
            data_dir=Path(data_dir).expanduser() if data_dir else _default_data_dir(env),
            context_window=_int(env, "CCMC_CONTEXT_WINDOW", 200_000),
            idle_minutes=_int(env, "CCMC_IDLE_MINUTES", 30),
            stale_minutes=_int(env, "CCMC_STALE_MINUTES", 60),
            max_calls=_int(env, "CCMC_MAX_CALLS", 500),
            max_field_chars=_int(env, "CCMC_MAX_FIELD_CHARS", 4000),
            redact_file=Path(redact_file).expanduser() if redact_file else None,
            no_browser=env.get("CCMC_NO_BROWSER", "").strip() == "1",
            upstream_url=env.get("CCMC_UPSTREAM_URL", "").strip() or None,
            upstream_token=env.get("CCMC_UPSTREAM_TOKEN", "").strip() or None,
            dev_origins=tuple(o.strip().rstrip("/") for o in dev_origins.split(",") if o.strip()),
        )

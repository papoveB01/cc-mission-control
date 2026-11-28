"""Plan detection (SPEC 13.6): subscription or API billing, from two fields of Claude Code's `.claude.json`.

Privacy: the file also holds the user's email, name and ids. Only `oauthAccount.billingType` and
`oauthAccount.organizationType` are ever copied out of the parsed document; nothing else is kept, logged or put
in an error. Failures log only the exception class. Reading happens off the event loop (callers use a thread).
"""

from __future__ import annotations

import json
import logging
import os
import stat
import threading
import time
from pathlib import Path
from typing import Any, Callable, Dict, Mapping, Optional

log = logging.getLogger(__name__)

CACHE_SECONDS = 600.0
MAX_BYTES = 64 * 1024 * 1024
NAMES = {"claude_max": "Max", "claude_pro": "Pro", "claude_team": "Team", "claude_enterprise": "Enterprise"}
UNKNOWN: Dict[str, Optional[str]] = {"kind": None, "name": None}


def plan_path(env: Mapping[str, str] | None = None, home: Path | None = None) -> Path:
    env = os.environ if env is None else env
    base = env.get("CLAUDE_CONFIG_DIR", "").strip()
    if base:
        return Path(base).expanduser() / ".claude.json"
    return (home or Path.home()) / ".claude.json"


def classify(billing: Any, org: Any) -> Dict[str, Optional[str]]:
    """The `/api/plan` body from the two raw values."""
    if not isinstance(billing, str) or not billing.strip():
        return dict(UNKNOWN)
    if "subscription" in billing.lower():
        name = NAMES.get(org.strip().lower()) if isinstance(org, str) else None
        return {"kind": "subscription", "name": name}
    return {"kind": "api", "name": None}


def read_plan(path: Path) -> Dict[str, Optional[str]]:
    """Read the two plan fields. Any problem gives the unknown plan. Never raises."""
    try:
        st = path.stat()
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_BYTES:  # never open a FIFO or device
            return dict(UNKNOWN)
        with open(path, "rb") as fh:
            raw = fh.read(MAX_BYTES + 1)  # bounded: the file may grow after the stat
        if len(raw) > MAX_BYTES:
            return dict(UNKNOWN)
        doc = json.loads(raw.decode("utf-8"))
        del raw
        account = doc.get("oauthAccount") if isinstance(doc, dict) else None
        if not isinstance(account, dict):
            return dict(UNKNOWN)
        billing, org = account.get("billingType"), account.get("organizationType")
        del doc, account  # nothing else of the file stays referenced
        return classify(billing, org)
    except Exception as exc:  # the message could quote file content: log the class only
        log.debug("plan not readable (%s)", type(exc).__name__)
        return dict(UNKNOWN)


class PlanDetector:
    """Cached plan lookup. `get()` may block on a file read: call it from a thread."""

    def __init__(self, enabled: bool = True, path: Path | None = None, clock: Callable[[], float] = time.time) -> None:
        self.enabled = enabled
        self.path = path or plan_path()
        self.clock = clock
        self._lock = threading.Lock()
        self._at: Optional[float] = None
        self._value: Dict[str, Optional[str]] = dict(UNKNOWN)

    def get(self) -> Dict[str, Optional[str]]:
        if not self.enabled:
            return dict(UNKNOWN)
        with self._lock:
            now = self.clock()
            if self._at is None or now - self._at >= CACHE_SECONDS or now < self._at:
                self._value = read_plan(self.path)
                self._at = now
            return dict(self._value)

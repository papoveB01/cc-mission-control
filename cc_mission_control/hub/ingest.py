"""Validation of relay-v2 envelopes (SPEC 13.1, 13.2)."""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Optional

from ..redact import Redactor
from ..state import MAIN, _norm_agent

KINDS = frozenset({"hook", "ctx", "usage", "meta", "title", "cwd"})
EID_RE = re.compile(r"^[0-9a-fA-F]{32}$")
ID_MAX = 128
LABEL_MAX = 200
MESSAGE_ID_MAX = 256
USAGE_FIELDS = ("input", "cache_creation", "cache_read", "output")
MAX_COUNT = 2**53  # JSON-safe integer range
MAX_T = 4e9
MAX_DEPTH = 64


@dataclass
class Record:
    eid: str
    machine_id: str
    user: str
    host: str
    session_id: str
    kind: str
    t: float
    data: dict


def _id(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and 0 < len(value) <= ID_MAX else None


def _time(value: Any) -> bool:
    """A plausible epoch time. Comparisons are False for NaN and inf, and safe for huge ints."""
    return isinstance(value, (int, float)) and not isinstance(value, bool) and 0 < value < MAX_T


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNT


def depth_ok(value: Any, limit: int = MAX_DEPTH) -> bool:
    """True if nested dicts/lists go no deeper than limit (iterative, so it can't recurse)."""
    stack = [(value, 1)]
    while stack:
        item, depth = stack.pop()
        if isinstance(item, dict):
            children = item.values()
        elif isinstance(item, list):
            children = item
        else:
            continue
        if depth > limit:
            return False
        stack.extend((c, depth + 1) for c in children)
    return True


def usage_row(rec: Record) -> Optional[tuple]:
    """(agent_id, message_id, model, input, cache_creation, cache_read, output), or None if malformed."""
    d = rec.data
    if not all(_count(d.get(k)) for k in USAGE_FIELDS):
        return None
    message_id = d.get("message_id")
    if not (isinstance(message_id, str) and 0 < len(message_id) <= MESSAGE_ID_MAX):
        message_id = rec.eid  # no message id: every record counts once, keyed by its own eid
    model = d.get("model") if isinstance(d.get("model"), str) else None
    agent_id = _norm_agent(d.get("agent_id"))[:ID_MAX] or MAIN
    return (agent_id, message_id, model, *(d[k] for k in USAGE_FIELDS))


def parse_envelope(item: Any, redactor: Redactor) -> Optional[Record]:
    """A validated, re-redacted Record, or None if the envelope is malformed."""
    if not isinstance(item, dict):
        return None
    v, eid, kind, data, t = item.get("v"), item.get("eid"), item.get("kind"), item.get("data"), item.get("t")
    if isinstance(v, bool) or v != 2 or not isinstance(eid, str) or not EID_RE.match(eid):
        return None
    if kind not in KINDS or not isinstance(data, dict) or not _time(t) or not depth_ok(data):
        return None
    machine_id, session_id = _id(item.get("machine_id")), _id(item.get("session_id"))
    if machine_id is None or session_id is None:
        return None
    user, host = item.get("user"), item.get("host")
    rec = Record(
        eid=eid.lower(),
        machine_id=machine_id,
        user=redactor.text(user)[:LABEL_MAX] if isinstance(user, str) else "",
        host=redactor.text(host)[:LABEL_MAX] if isinstance(host, str) else "",
        session_id=session_id,
        kind=kind,
        t=float(t),
        data=redactor.value(data),
    )
    if kind == "usage" and usage_row(rec) is None:
        return None
    return rec

"""Token, session cookie, Host and Origin checks for the hub (SPEC 13.2)."""

from __future__ import annotations

import hmac
import re
import time
from hashlib import sha256
from typing import Iterable, Optional
from urllib.parse import urlsplit

COOKIE_NAME = "ccmc_hub_session"
COOKIE_DAYS = 30
LOOPBACK = ("127.0.0.1", "localhost", "[::1]")
TAILNET_RE = re.compile(r"^ccmc-hub\.[a-z0-9-]+\.ts\.net$", re.IGNORECASE)


def token_ok(token: str, supplied: Optional[str]) -> bool:
    if not supplied:
        return False
    return hmac.compare_digest(token.encode(), supplied.encode("utf-8", "replace"))


def bearer_ok(token: str, header: Optional[str]) -> bool:
    if not header or header[:7].lower() != "bearer ":
        return False
    return token_ok(token, header[7:].strip())


def _sign(token: str, expiry: int) -> str:
    return hmac.new(token.encode(), f"ccmc-hub-session:{expiry}".encode(), sha256).hexdigest()


def make_cookie(token: str, now: Optional[float] = None) -> str:
    expiry = int((time.time() if now is None else now) + COOKIE_DAYS * 86400)
    return f"{expiry}.{_sign(token, expiry)}"


def cookie_ok(token: str, value: Optional[str], now: Optional[float] = None) -> bool:
    if not value or "." not in value:
        return False
    expiry_s, sig = value.split(".", 1)
    if not (expiry_s.isascii() and expiry_s.isdigit() and len(expiry_s) <= 12):
        return False
    expiry = int(expiry_s)
    if expiry < (time.time() if now is None else now):
        return False
    return hmac.compare_digest(_sign(token, expiry).encode(), sig.encode("utf-8", "replace"))


def _split(host: str) -> tuple:
    """(name, port or None) of a Host header value or allowlist entry."""
    host = host.strip().lower()
    name, sep, port = host.rpartition(":")
    if sep and port.isdigit() and name:
        return name, port
    return host, None


def host_allowed(header: Optional[str], allowed: Iterable[str], tailnet: bool = False) -> bool:
    """Allowlist entries without a port match any port; entries with a port match exactly.

    tailnet=True also accepts ccmc-hub.<one label>.ts.net on any port.
    """
    if not header:
        return False
    name, port = _split(header)
    if tailnet and TAILNET_RE.match(name):
        return True
    for entry in allowed:
        e_name, e_port = _split(entry)
        if e_name == name and (e_port is None or e_port == port):
            return True
    return False


def origin_allowed(origin: str, allowed: Iterable[str], tailnet: bool = False, request_host: Optional[str] = None) -> bool:
    """The Origin must be an allowed host and, when the request Host names a port, the same port."""
    parts = urlsplit(origin)
    if parts.scheme not in ("http", "https") or not host_allowed(parts.netloc, allowed, tailnet):
        return False
    host_port = _split(request_host)[1] if request_host else None
    if host_port is None:
        return True
    try:
        origin_port = parts.port or (443 if parts.scheme == "https" else 80)
    except ValueError:
        return False
    return str(origin_port) == host_port


def is_loopback(header: Optional[str]) -> bool:
    return bool(header) and _split(header or "")[0] in LOOPBACK

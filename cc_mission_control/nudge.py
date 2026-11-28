"""Send a plain-text note to a session's inbox socket (SPEC 11.12, docs: cross-session-messaging).

Documented (code.claude.com/docs/en/cross-session-messaging, "The session's inbox socket"):
  - a Unix domain socket on macOS/Linux, path in CLAUDE_CODE_MESSAGING_SOCKET;
  - "send {"type":"auth","token":"<token>"} as the first line of its connection";
  - a connection with no complete line within 30 seconds is closed.
NOT documented: the frame of the message itself. Until it is, message_frame() refuses and
nothing is sent. Errors here must never contain the token."""

from __future__ import annotations

import json
import os
import socket
import stat
from typing import Optional

TIMEOUT = 2.0

# Off until the message frame is documented: see "The session's inbox socket" at
# https://code.claude.com/docs/en/cross-session-messaging (it gives the auth line, not the message
# frame) and message_frame() below. While False the server stores no socket or token and the route 404s.
NUDGE_SUPPORTED = False


class NudgeError(Exception):
    """A user-safe reason; never contains the token."""


def auth_line(token: str) -> bytes:
    return json.dumps({"type": "auth", "token": token}, separators=(",", ":")).encode() + b"\n"


def message_frame(text: str) -> bytes:
    """The documented message frame for `text`. The doc does not specify it, so we do not guess."""
    raise NudgeError("the inbox message format is not documented yet, so nothing was sent")


def check_path(path: str) -> None:
    """The inbox must be a real socket owned by us: not a symlink, a regular file or someone else's."""
    if ".." in path.split("/"):
        raise NudgeError("the session's inbox path is not valid")
    try:
        st = os.lstat(path)
    except OSError:
        raise NudgeError("could not reach the session's inbox") from None
    if not stat.S_ISSOCK(st.st_mode) or (hasattr(os, "getuid") and st.st_uid != os.getuid()):
        raise NudgeError("the session's inbox is not a socket owned by you")


def send(path: str, token: str, text: str, timeout: float = TIMEOUT) -> None:
    """Connect, write the auth line then the message frame. Raises NudgeError."""
    frame = message_frame(text)  # before connecting: an unknown format must not touch the socket
    if not hasattr(socket, "AF_UNIX"):
        raise NudgeError("unix sockets are not available on this platform")
    check_path(path)
    sock: Optional[socket.socket] = None
    try:
        sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        sock.settimeout(timeout)
        sock.connect(path)
        sock.sendall(auth_line(token) + frame)
    except socket.timeout:
        raise NudgeError("the session's inbox did not answer in time") from None
    except OSError as exc:
        raise NudgeError(f"could not reach the session's inbox ({exc.__class__.__name__})") from None
    finally:
        if sock is not None:
            try:
                sock.close()
            except OSError:
                pass

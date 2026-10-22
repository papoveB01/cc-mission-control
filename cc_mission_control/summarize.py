"""Opt-in AI summaries (SPEC 11.6.2).

The Store (sync, on the ingestion path) only calls `Summarizer.submit`, which appends to a bounded
deque. One asyncio worker drains it, calls the Messages API and hands the text to `deliver`.
The API key is read once from a private file and lives only in this object's `_key`.
"""

from __future__ import annotations

import asyncio
import logging
import os
import stat
import time
from collections import deque
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Optional

import httpx

from .config import Config
from .redact import Redactor

log = logging.getLogger(__name__)

API_URL = "https://api.anthropic.com/v1/messages"
API_VERSION = "2023-06-01"
QUEUE_MAX = 20
TIMEOUT = 20.0
MAX_TOKENS = 200
RETRY_DELAY = 2.0
IDLE_POLL = 0.25
PROMPT_CHARS = 1000
RESULT_CHARS = 1000
LINES_MAX = 60
LINES_CHARS = 6000
SUMMARY_CHARS = 300
KEY_MAX_BYTES = 512
DEADLINE = 45.0
SYSTEM_PROMPT = (
    "You summarize what a coding agent just did, for a dashboard. Write one or two sentences in the past "
    "tense, at most 300 characters in total, saying what was done and the result. Do not speculate. "
    "Never include secrets, tokens, or credentials."
)


@dataclass
class SummaryRequest:
    session_id: str
    agent_id: str
    t_start: float
    t_end: float
    prompt: str
    lines: list
    result: str


def key_problem(path: Path) -> Optional[str]:
    """Why the key file cannot be used, or None. Never includes the key."""
    try:
        st = os.stat(path)
    except OSError:
        return "key file not found"
    if not stat.S_ISREG(st.st_mode):
        return "key file is not a regular file"
    if st.st_mode & 0o077:
        return "key file must not be readable by group or others (chmod 600)"
    return None


def _read_key(path: Path) -> "tuple[str, Optional[str]]":
    try:
        with open(path, "rb") as f:
            raw = f.read(KEY_MAX_BYTES + 1)
    except OSError:
        return "", "key file cannot be read"
    if len(raw) > KEY_MAX_BYTES:
        return "", "key file is too large"
    try:
        key = raw.decode("ascii").strip()
    except UnicodeDecodeError:
        return "", "key file is not plain ASCII text"
    if not key or not all(c.isprintable() and not c.isspace() for c in key):
        return "", "key file is empty or contains whitespace or non-printable characters"
    return key, None


def build_content(req: SummaryRequest, redactor: Redactor) -> str:
    """The user message: prompt, the newest <=60 lines (<=6000 chars), the agent's final result."""
    lines = [redactor.redact(str(x)) for x in req.lines][-LINES_MAX:]
    total, kept = 0, []
    for line in reversed(lines):  # oldest dropped first
        if total + len(line) + 1 > LINES_CHARS and kept:
            break
        kept.append(line[:LINES_CHARS])
        total += len(line) + 1
    kept.reverse()
    return (
        f"User request:\n{redactor.redact(req.prompt)[:PROMPT_CHARS]}\n\n"
        f"Activity:\n" + "\n".join(kept) + "\n\n"
        f"Final result:\n{redactor.redact(req.result)[:RESULT_CHARS]}"
    )


class _Failure(Exception):
    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


class Summarizer:
    def __init__(
        self,
        config: Config,
        key: str,
        redactor: Redactor,
        *,
        clock: Callable[[], float] = time.time,
        transport: Optional[httpx.AsyncBaseTransport] = None,
        retry_delay: float = RETRY_DELAY,
    ) -> None:
        self.model = config.summary_model
        self.max_per_hour = config.summary_max_per_hour
        self.redactor = redactor
        self.clock = clock
        self.retry_delay = retry_delay
        self.deliver: Callable[[SummaryRequest, str, str], None] = lambda *_: None
        self._key = key
        self._transport = transport
        self._queue: Deque[SummaryRequest] = deque()
        self._sent_times: Deque[float] = deque()
        self.sent = 0
        self.failed = 0
        self.skipped = 0
        self.last_error: Optional[str] = None

    def __repr__(self) -> str:
        return f"Summarizer(model={self.model!r})"

    @classmethod
    def from_config(cls, config: Config, redactor: Redactor, **kw: Any) -> Optional["Summarizer"]:
        """None (with one log line) unless CCMC_SUMMARIES=1 and a private key file is present."""
        if not config.summaries:
            return None
        path = config.summary_key_file or config.data_dir / "summary_api_key"
        problem = key_problem(path)
        key = ""
        if problem is None:
            key, problem = _read_key(path)
        if problem is not None:
            log.warning("AI summaries disabled: %s (%s)", problem, path)
            return None
        return cls(config, key, redactor, **kw)

    # ---- ingestion side (sync, no I/O) --------------------------------------

    def submit(self, req: SummaryRequest) -> None:
        if len(self._queue) >= QUEUE_MAX:
            self._queue.popleft()
            self.skipped += 1
        self._queue.append(req)

    def stats(self) -> dict:
        return {
            "enabled": True,
            "sent": self.sent,
            "failed": self.failed,
            "skipped": self.skipped,
            "last_error": self.last_error,
        }

    # ---- worker ---------------------------------------------------------------

    def _rate_ok(self) -> bool:
        now = self.clock()
        while self._sent_times and now - self._sent_times[0] >= 3600:
            self._sent_times.popleft()
        if len(self._sent_times) >= self.max_per_hour:
            return False
        self._sent_times.append(now)
        return True

    async def _post(self, client: httpx.AsyncClient, body: dict) -> str:
        headers = {"x-api-key": self._key, "anthropic-version": API_VERSION, "content-type": "application/json"}
        for attempt in (0, 1):
            try:
                resp = await client.post(API_URL, headers=headers, json=body)
            except httpx.HTTPError as exc:
                if attempt == 0:
                    await asyncio.sleep(self.retry_delay)
                    continue
                raise _Failure(type(exc).__name__) from None
            if (resp.status_code == 429 or resp.status_code >= 500) and attempt == 0:
                await asyncio.sleep(self.retry_delay)
                continue
            if resp.status_code != 200:
                raise _Failure(f"HTTP {resp.status_code}")
            try:
                text = resp.json()["content"][0]["text"]
            except Exception:
                raise _Failure("BadResponse") from None
            if not isinstance(text, str) or not text.strip():
                raise _Failure("EmptyResponse")
            return text.strip()
        raise _Failure("Unreachable")

    async def process_one(self, client: httpx.AsyncClient) -> bool:
        """Handle the oldest queued request. False when the queue was empty."""
        try:
            req = self._queue.popleft()
        except IndexError:
            return False
        if not self._rate_ok():
            self.skipped += 1
            return True
        body = {
            "model": self.model,
            "max_tokens": MAX_TOKENS,
            "system": SYSTEM_PROMPT,
            "messages": [{"role": "user", "content": build_content(req, self.redactor)}],
        }
        try:
            text = await asyncio.wait_for(self._post(client, body), DEADLINE)
        except asyncio.TimeoutError:
            self.failed += 1
            self.last_error = "Timeout"
            return True
        except _Failure as exc:
            self.failed += 1
            self.last_error = exc.reason
            return True
        except Exception as exc:  # never let one request stop the worker
            self.failed += 1
            self.last_error = type(exc).__name__
            return True
        self.sent += 1
        try:
            self.deliver(req, text, self.model)
        except Exception:
            log.debug("summary delivery failed", exc_info=True)
        return True

    def _client(self) -> httpx.AsyncClient:
        return httpx.AsyncClient(timeout=TIMEOUT, transport=self._transport)

    async def drain(self) -> None:
        """Process everything queued now (tests)."""
        async with self._client() as client:
            while await self.process_one(client):
                pass

    async def run(self) -> None:
        async with self._client() as client:
            while True:
                if not await self.process_one(client):
                    await asyncio.sleep(IDLE_POLL)

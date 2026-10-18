"""Upstream relay v2 (SPEC section 13.1): envelopes, a disk spool and batched, retried sending.

Ingestion only appends to a bounded in-memory deque. A writer task moves records to
append-only JSONL segments; a sender task posts closed segments to the hub and deletes
each one only after every batch from it got a 2xx.
"""

from __future__ import annotations

import asyncio
import collections
import getpass
import hashlib
import json
import logging
import os
import socket
import threading
import time
import uuid
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Tuple

from .config import Config

log = logging.getLogger(__name__)

ENVELOPE_VERSION = 2
RELAY_MAX = 5000
QUEUE_BYTES = 32 * 1024 * 1024
RELAY_TIMEOUT = 10.0
SEGMENT_BYTES = 4 * 1024 * 1024
SEAL_AGE = 5.0
BATCH_RECORDS = 200
BATCH_BYTES = 1024 * 1024
BACKOFF_MIN = 1.0
BACKOFF_MAX = 300.0
FLUSH_INTERVAL = 0.5
SEND_INTERVAL = 2.0
SEG_PREFIX = "seg-"
SEG_SUFFIX = ".jsonl"
RETRY_4XX = frozenset({401, 403, 408, 429})  # every other 4xx means the batch itself is bad
DIR_MODE = 0o700
FILE_MODE = 0o600

# a batch is (JSON array body, end offset in the segment, records, unparseable lines before it)
Batch = Tuple[bytes, int, int, int]


def machine_id() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8", "replace")).hexdigest()[:12]


def os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


def _default_client() -> Any:
    import httpx

    return httpx.AsyncClient(timeout=RELAY_TIMEOUT)


class Relay:
    """Spooling relay. `emit` is thread-safe and never touches the network."""

    def __init__(
        self,
        config: Config,
        redactor: Any,
        maxsize: int = RELAY_MAX,
        *,
        clock: Callable[[], float] = time.time,
        monotonic: Callable[[], float] = time.monotonic,
        client_factory: Callable[[], Any] = _default_client,
        sleep: Callable[[float], Any] = asyncio.sleep,
        spool_bytes: Optional[int] = None,
        queue_bytes: int = QUEUE_BYTES,
        segment_bytes: int = SEGMENT_BYTES,
        seal_age: float = SEAL_AGE,
        batch_records: int = BATCH_RECORDS,
        batch_bytes: int = BATCH_BYTES,
        backoff_min: float = BACKOFF_MIN,
        backoff_max: float = BACKOFF_MAX,
        flush_interval: float = FLUSH_INTERVAL,
        send_interval: float = SEND_INTERVAL,
    ) -> None:
        self.url = config.upstream_url or ""
        self.token = config.upstream_token
        self.redactor = redactor
        self.clock = clock
        self.monotonic = monotonic
        self.client_factory = client_factory
        self.sleep = sleep
        self.maxsize = maxsize
        self.queue_bytes = queue_bytes
        self.cap_bytes = spool_bytes if spool_bytes is not None else max(0, config.spool_mb) * 1024 * 1024
        self.segment_bytes = segment_bytes
        self.seal_age = seal_age
        self.batch_records = batch_records
        self.batch_bytes = batch_bytes
        self.backoff_min = backoff_min
        self.backoff_max = backoff_max
        self.flush_interval = flush_interval
        self.send_interval = send_interval
        self.machine_id = machine_id()
        self.user = os_user()
        self.host = redactor.text(socket.gethostname())
        self.dir = Path(config.data_dir) / "relay"
        self.queue: Deque[dict] = collections.deque()  # deque: emit may run on a worker thread
        self.dropped = 0
        self.last_ok: Optional[float] = None
        self.last_error: Optional[str] = None
        self.backoff = backoff_min
        self._qlock = threading.Lock()  # guards queue, _qbytes and _closed
        self._dlock = threading.Lock()  # guards dropped
        self._lock = threading.Lock()  # guards the spool directory and the fields below
        self._qbytes = 0
        self._closed = False  # after shutdown, emit writes straight to the spool
        self._sizes: Dict[str, int] = {}  # segment path -> bytes
        self._spooled = 0  # sum of _sizes, kept so stats() needs no iteration
        self._cur: Optional[str] = None  # segment still being appended to
        self._cur_born = 0.0
        self._counter = 0
        self._send_path: Optional[str] = None
        self._send_off = 0
        try:
            self._ensure_dir()
            for p in self.dir.glob(SEG_PREFIX + "*" + SEG_SUFFIX):
                self._sizes[str(p)] = p.stat().st_size
            self._spooled = sum(self._sizes.values())
            self._enforce_cap()
        except OSError as exc:
            log.warning("relay spool unavailable (%s)", type(exc).__name__)

    def _drop(self, n: int) -> None:
        if n > 0:
            with self._dlock:
                self.dropped += n

    def _ensure_dir(self) -> None:
        self.dir.mkdir(mode=DIR_MODE, parents=True, exist_ok=True)
        os.chmod(self.dir, DIR_MODE)  # also narrows a directory that already existed

    # ---- ingestion (no network, no disk except after shutdown) -------------

    def emit(self, kind: str, session_id: str, data: Any) -> bool:
        """Queue one envelope. Everything stored in it has been through the redactor."""
        try:
            envelope = {
                "v": ENVELOPE_VERSION,
                "eid": uuid.uuid4().hex,
                "machine_id": self.machine_id,
                "user": self.user,
                "host": self.host,
                "t": self.clock(),
                "kind": kind,
                "session_id": self.redactor.value(session_id),
                "data": self.redactor.value(data),
            }
            size = len(json.dumps(envelope, allow_nan=False, ensure_ascii=False))
        except Exception:
            log.debug("relay envelope failed", exc_info=True)
            self._drop(1)
            return False
        with self._qlock:
            if not self._closed:
                if len(self.queue) >= self.maxsize or self._qbytes + size > self.queue_bytes:
                    self._drop(1)
                    return False
                self.queue.append(envelope)
                self._qbytes += size
                return True
        self._append([envelope])  # a late emit (a watcher thread outliving the flush) must not be lost
        return True

    def stats(self) -> dict:
        return {
            "queued": len(self.queue),
            "spooled_bytes": self._spooled,
            "dropped": self.dropped,
            "last_ok": self.last_ok,
            "last_error": self.last_error,
        }

    # ---- writer ------------------------------------------------------------

    def _drain(self, close: bool = False) -> List[dict]:
        with self._qlock:
            items = list(self.queue)
            self.queue.clear()
            self._qbytes = 0
            if close:
                self._closed = True
        return items

    def _append(self, items: List[dict]) -> None:
        """Serialize and append records, rotating segments. Runs in a worker thread (or at shutdown)."""
        lines: List[bytes] = []
        for item in items:
            try:
                text = json.dumps(item, allow_nan=False, ensure_ascii=False)
            except (TypeError, ValueError):
                self._drop(1)
                continue
            lines.append(text.encode("utf-8", "replace") + b"\n")
        with self._lock:
            i, failures = 0, 0
            while i < len(lines):
                path = None
                try:
                    self._ensure_dir()
                    path = self._current_segment()
                    room = self.segment_bytes - self._sizes[path]
                    j, size = i, 0
                    while j < len(lines) and (j == i or size + len(lines[j]) <= room):
                        size += len(lines[j])
                        j += 1
                    self._write(path, b"".join(lines[i:j]))
                except OSError as exc:
                    self.last_error = type(exc).__name__
                    log.warning("relay spool write failed (%s)", type(exc).__name__)
                    self._abandon(path)
                    failures += 1
                    if failures >= 2:
                        self._drop(len(lines) - i)
                        break
                    continue
                failures = 0
                self._sizes[path] += size
                self._spooled += size
                i = j
            self._enforce_cap()

    @staticmethod
    def _write(path: str, data: bytes) -> None:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, FILE_MODE)
        with os.fdopen(fd, "ab") as f:
            f.write(data)

    def _abandon(self, path: Optional[str]) -> None:
        """After a failed write, stop appending to that segment (it may end in a torn line)."""
        self._cur = None
        if path is None or path not in self._sizes:
            return
        try:
            actual = os.stat(path).st_size
        except OSError:
            actual = 0
        self._spooled += actual - self._sizes[path]
        self._sizes[path] = actual

    def _current_segment(self) -> str:
        if self._cur is not None and self._sizes.get(self._cur, 0) < self.segment_bytes:
            return self._cur
        self._counter += 1
        path = str(self.dir / f"{SEG_PREFIX}{time.time_ns():020d}-{self._counter:06d}{SEG_SUFFIX}")
        self._cur = path
        self._cur_born = self.monotonic()
        self._sizes[path] = 0
        return path

    def _enforce_cap(self) -> None:
        """Delete the oldest segments until the spool fits; their records count as dropped."""
        while self._spooled > self.cap_bytes:
            candidates = sorted(p for p in self._sizes if p != self._send_path)  # never the one being sent
            if not candidates:
                return
            oldest = candidates[0]
            try:
                self._drop(Path(oldest).read_bytes().count(b"\n"))
            except OSError:
                pass
            self._remove(oldest)

    def _remove(self, path: str) -> None:
        self._spooled -= self._sizes.pop(path, 0)
        if self._cur == path:
            self._cur = None
        try:
            os.unlink(path)
        except OSError:
            pass

    async def flush(self) -> None:
        items = self._drain()
        if items:
            # a worker thread keeps big writes off the event loop; the lock serializes with the sender
            await asyncio.to_thread(self._append, items)

    def flush_sync(self) -> None:
        """Move everything still queued to disk (shutdown). Never touches the network."""
        items = self._drain(close=True)
        if items:
            self._append(items)
        with self._lock:
            self._cur = None

    async def run_writer(self) -> None:
        while True:
            await self.flush()
            await self.sleep(self.flush_interval)

    # ---- sender ------------------------------------------------------------

    def _next_segment(self) -> Optional[str]:
        """Oldest closed segment. With none, seal the current one if it is old or full, so it is
        never sent mid-append and the spool does not fill with tiny files."""
        with self._lock:
            closed = sorted(p for p in self._sizes if p != self._cur)
            if closed:
                return closed[0]
            cur = self._cur
            if cur is not None and self._sizes.get(cur, 0) > 0:
                full = self._sizes[cur] >= self.segment_bytes
                if full or self.monotonic() - self._cur_born >= self.seal_age:
                    self._cur = None
                    return cur
            return None

    def _plan(self, path: str, offset: int) -> Optional[Tuple[List[Batch], int]]:
        """Batches for the unsent part of a segment plus the unparseable lines after the last
        one; None if the segment is gone. Corrupt lines are skipped so they can't wedge the spool."""
        try:
            data = Path(path).read_bytes()
        except OSError:
            return None
        data = data[offset : data.rfind(b"\n") + 1]  # drops a torn last line
        batches: List[Batch] = []
        group: List[bytes] = []
        size, end, bad = 0, offset, 0

        def finish() -> None:
            nonlocal group, size, bad
            batches.append((b"[" + b",".join(group) + b"]", end, len(group), bad))
            group, size, bad = [], 0, 0

        for line in data.splitlines(keepends=True):
            body = line.rstrip(b"\r\n")
            try:
                ok = isinstance(json.loads(body), dict)
            except ValueError:
                ok = False
            if not ok:
                bad += 1
                end += len(line)
                continue
            if group and (len(group) >= self.batch_records or size + len(line) > self.batch_bytes):
                finish()
            group.append(body)
            size += len(line)
            end += len(line)
        if group:
            finish()
        return batches, bad

    def _delete(self, path: str) -> None:
        with self._lock:
            self._remove(path)

    async def _send_segment(self, client: Any, path: str) -> bool:
        if self._send_path != path:
            self._send_path, self._send_off = path, 0
        planned = await asyncio.to_thread(self._plan, path, self._send_off)
        if planned is None:  # removed meanwhile
            self._send_path = None
            return True
        batches, trailing_bad = planned
        headers = {"Content-Type": "application/json"}
        if self.token:
            headers["Authorization"] = "Bearer " + self.token
        for body, end, count, bad in batches:
            try:
                resp = await client.post(self.url, content=body, headers=headers)
            except Exception as exc:
                self.last_error = type(exc).__name__
                return False
            code = resp.status_code
            if 200 <= code < 300:
                self.last_ok, self.last_error = self.clock(), None
            elif 400 <= code < 500 and code not in RETRY_4XX:
                # retrying can never succeed: count the records as lost and move on
                log.warning("relay batch rejected (HTTP %d); dropping %d records", code, count)
                self.last_error = f"HTTP {code}"
                self._drop(count)
            else:
                self.last_error = f"HTTP {code}"
                return False
            self._drop(bad)
            self._send_off = end
        self._drop(trailing_bad)
        await asyncio.to_thread(self._delete, path)
        self._send_path = None
        return True

    async def send_pending(self, client: Any) -> bool:
        """Send every spooled segment, oldest first. False on the first failure."""
        while True:
            path = await asyncio.to_thread(self._next_segment)
            if path is None:
                return True
            if not await self._send_segment(client, path):
                return False

    async def run_sender(self) -> None:
        async with self.client_factory() as client:
            while True:
                try:
                    ok = await self.send_pending(client)
                except Exception as exc:
                    self.last_error = type(exc).__name__
                    ok = False
                if ok:
                    self.backoff = self.backoff_min
                    await self.sleep(self.send_interval)
                else:
                    await self.sleep(self.backoff)
                    self.backoff = min(self.backoff * 2, self.backoff_max)

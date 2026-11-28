"""Transcript tailing: context usage per agent (SPEC sections 5.2 and 5.3)."""

from __future__ import annotations

import contextlib
import glob
import json
import logging
import os
import re
import tempfile
import time
from collections import deque
from datetime import datetime, timezone
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Deque, Dict, List, Optional, Set, Tuple, Union

from .pricing import SPEED_MAX, split_cache
from .state import MAIN, Store

log = logging.getLogger(__name__)

USAGE_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens", "output_tokens")
SYNTHETIC = "<synthetic>"
JSONL = ".jsonl"
META = ".meta.json"
META_MAX_BYTES = 1_000_000
SAFE_AID = re.compile(r"[A-Za-z0-9_-]{1,128}")
LOOSE_MIN_AID = 8  # shorter ids only get the exact `agent-<id>*` pattern
MAX_BACKOFF = 30.0
TITLE_SCAN_BYTES = 4_000_000
TITLE_CHUNK_BYTES = 256 * 1024
HEAD_BYTES = 64 * 1024
HEAD_LINES = 200
OFFSETS_FILE = "usage_relayed.json"
OFFSETS_MAX_PATHS = 5000
OFFSETS_INTERVAL = 5.0
BACKLOG_MAX = 5000  # usage entries read but not yet accepted by the relay, per file; reading pauses beyond it
RELAY_FILL_MAX = 0.8  # skip relaying while the relay queue is fuller than this
_TS_RE = re.compile(r"(\d{4})-(\d\d)-(\d\d)[T ](\d\d):(\d\d):(\d\d)(?:\.(\d+))?\s*(Z|[+-]\d\d:?\d\d)?", re.I)


@dataclass
class Usage:
    tokens: int
    model: Optional[str]
    input: int = 0
    cache_creation: int = 0
    cache_read: int = 0
    output: int = 0
    message_id: Optional[str] = None
    cache_creation_5m: int = 0
    cache_creation_1h: int = 0
    web_search: int = 0
    web_fetch: int = 0
    speed: Optional[str] = None
    inference_geo: Optional[str] = None
    ts: Optional[float] = None  # the entry's own time, epoch seconds
    split: bool = False  # the TTL split was reported by the transcript, not derived

    def data(self) -> dict:
        """The count and label fields of a relay `usage` record or a `Store.add_usage` call (SPEC 13.1)."""
        return {
            "message_id": self.message_id, "model": self.model, "input": self.input,
            "cache_creation": self.cache_creation, "cache_creation_5m": self.cache_creation_5m,
            "cache_creation_1h": self.cache_creation_1h, "cache_read": self.cache_read, "output": self.output,
            "web_search": self.web_search, "web_fetch": self.web_fetch, "speed": self.speed,
            "inference_geo": self.inference_geo, "ts": self.ts, "split": self.split,
        }


def parse_ts(value: Any) -> Optional[float]:
    """Epoch seconds of an ISO 8601 timestamp (no zone means UTC), or None."""
    if not isinstance(value, str) or len(value) > 64:
        return None
    m = _TS_RE.fullmatch(value.strip())
    if not m:
        return None
    y, mo, d, h, mi, sec, frac, zone = m.groups()
    try:
        dt = datetime(int(y), int(mo), int(d), int(h), int(mi), int(sec), tzinfo=timezone.utc)
        offset = 0
        if zone and zone.upper() != "Z":
            offset = (int(zone[1:3]) * 60 + int(zone[-2:])) * 60 * (1 if zone[0] == "+" else -1)
        return dt.timestamp() - offset + (float("0." + frac) if frac else 0.0)
    except (ValueError, OverflowError):
        return None


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def usage_from_entry(entry: dict, *, main: bool) -> Optional[Usage]:
    """Context usage of one assistant transcript entry, or None if it carries none."""
    if not isinstance(entry, dict) or entry.get("type") != "assistant":
        return None
    if main and entry.get("isSidechain") is True:
        return None
    message = entry.get("message")
    usage = message.get("usage") if isinstance(message, dict) else None
    if not isinstance(usage, dict):
        return None
    counts = [v if _is_int(v) and v > 0 else 0 for v in (usage.get(k) for k in USAGE_KEYS)]
    model = message.get("model")
    if not isinstance(model, str) or not model or model == SYNTHETIC:
        model = None
    mid = message.get("id")
    if not isinstance(mid, str) or not mid:
        mid = entry.get("uuid")
    nested = usage.get("cache_creation")
    nested = nested if isinstance(nested, dict) else {}
    c5, c1 = (nested.get(k) for k in ("ephemeral_5m_input_tokens", "ephemeral_1h_input_tokens"))
    c5, c1 = (v if _is_int(v) and v >= 0 else None for v in (c5, c1))
    total = usage.get("cache_creation_input_tokens")
    total = total if _is_int(total) and total >= 0 else None
    has_split = c5 is not None or c1 is not None
    c5, c1 = split_cache(total, c5, c1)
    counts[1] = c5 + c1  # equals the reported total unless only the split was reported
    tokens = sum(counts)
    server = usage.get("server_tool_use")
    server = server if isinstance(server, dict) else {}
    web = [v if _is_int(v) and v > 0 else 0 for v in (server.get("web_search_requests"), server.get("web_fetch_requests"))]
    speed, geo = usage.get("speed"), usage.get("inference_geo")
    return Usage(
        tokens, model, counts[0], counts[1], counts[2], counts[3], mid if isinstance(mid, str) and mid else None,
        c5, c1, web[0], web[1],
        speed[:SPEED_MAX] if isinstance(speed, str) and speed else None,
        geo[:SPEED_MAX] if isinstance(geo, str) and geo else None,
        parse_ts(entry.get("timestamp")), has_split,
    )


def title_from_entry(entry: Any) -> Tuple[Optional[str], Optional[str]]:
    """(ai title, custom title) carried by one transcript record."""
    if not isinstance(entry, dict):
        return None, None
    kind = entry.get("type")
    if kind == "ai-title":
        return _opt_str(entry.get("aiTitle")), None
    if kind == "custom-title":
        return None, _opt_str(entry.get("customTitle"))
    return None, None


def latest_titles(entries: List[dict]) -> Tuple[Optional[str], Optional[str]]:
    ai = custom = None
    for entry in entries:
        a, c = title_from_entry(entry)
        ai, custom = a or ai, c or custom
    return ai, custom


def scan_titles(path: str, max_bytes: int = TITLE_SCAN_BYTES) -> Tuple[Optional[str], Optional[str]]:
    """Last ai-title / custom-title in the final `max_bytes` of a file, read backwards in chunks."""
    ai = custom = None
    try:
        with open(path, "rb") as f:
            pos = f.seek(0, os.SEEK_END)
            budget = max_bytes
            carry = b""  # start of the line that spans into the chunk read after this one
            while pos > 0 and budget > 0 and not (ai and custom):
                n = min(TITLE_CHUNK_BYTES, pos, budget)
                pos -= n
                budget -= n
                f.seek(pos)
                lines = (f.read(n) + carry).split(b"\n")
                carry = b""
                if pos > 0:
                    if budget > 0:
                        carry = lines[0]  # may be cut off; completed by the next chunk
                    lines = lines[1:]
                for raw in reversed(lines):
                    if b"ai-title" not in raw and b"custom-title" not in raw:
                        continue
                    try:
                        entry = json.loads(raw.decode("utf-8", errors="replace"))
                    except ValueError:
                        continue
                    a, c = title_from_entry(entry)
                    ai, custom = ai or a, custom or c
    except OSError:
        pass
    return ai, custom


def head_cwd(path: str, max_bytes: int = HEAD_BYTES, max_lines: int = HEAD_LINES) -> Tuple[Optional[str], bool]:
    """First top-level `cwd` in the head of a transcript, and whether the answer is final.

    The first entry with a cwd holds the folder the session started in. The answer is final
    once a cwd is found or the head is already full (max_bytes or max_lines); a short head
    may still grow a cwd later. Never raises.
    """
    try:
        with open(path, "rb") as f:
            data = f.read(max_bytes)
    except OSError:
        return None, False
    for raw in data.split(b"\n")[:max_lines]:
        if b'"cwd"' not in raw:
            continue
        try:
            entry = json.loads(raw.decode("utf-8", errors="replace"))
        except ValueError:
            continue
        cwd = entry.get("cwd") if isinstance(entry, dict) else None
        if isinstance(cwd, str) and cwd:
            return cwd, True
    return None, len(data) >= max_bytes or data.count(b"\n") >= max_lines


class Tail:
    """Incremental JSONL reader for one file (SPEC 5.3)."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        start_tail_bytes: Optional[int] = None,
        max_read_bytes: int = 4_000_000,
    ) -> None:
        self.path = str(path)
        # None: read from byte 0, so usage is counted for the whole file (SPEC 13.6). A number skips
        # to the last N bytes of a larger file on the first read (tests, tools).
        self.start_tail_bytes = None if start_tail_bytes is None else max(0, start_tail_bytes)
        self.max_read_bytes = max(1, max_read_bytes)
        self._offset: Optional[int] = None  # None until the first successful stat
        self._buf = b""
        self._skip_partial = False
        self.skipped_head = False  # the first read started mid-file
        self.title_checked = False
        self.at_eof = False  # the last read reached the end of the file
        self.ends: List[int] = []  # byte offset just past each entry returned by the last read
        self.reset = False  # the file shrank and reading restarted at 0 (cleared by the caller)
        self.ctx_pending: Optional[Usage] = None  # newest usage from reads that had not reached EOF yet
        self.relayed = 0  # usage entries ending at or before this byte offset were relayed already
        self.backlog: Deque[Tuple[int, dict]] = deque()  # (end offset, usage data) fed to the Store, not yet relayed

    def read(self) -> Tuple[List[dict], bool]:
        """New parsed entries and whether any bytes were read. Never raises."""
        try:
            return self._read()
        except Exception:
            log.debug("tail read failed: %s", self.path, exc_info=True)
            self.ends = []
            return [], False

    def _read(self) -> Tuple[List[dict], bool]:
        self.ends = []
        try:
            size = os.stat(self.path).st_size
        except OSError:
            return [], False
        if self._offset is None:
            self._offset = 0
            if self.start_tail_bytes is not None and size > self.start_tail_bytes:
                self._offset = size - self.start_tail_bytes
                self._skip_partial = True
                self.skipped_head = True
        elif size < self._offset:
            self._offset, self._buf, self._skip_partial = 0, b"", False
            self.reset = True
        if size == self._offset:
            self.at_eof = True
            return [], False
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            data = f.read(min(size - self._offset, self.max_read_bytes))
        if not data:
            return [], False
        start = self._offset - len(self._buf)  # file position where self._buf + data begins
        self._offset += len(data)
        self.at_eof = self._offset >= size
        if self._skip_partial:
            nl = data.find(b"\n")
            if nl < 0:
                return [], True
            start += nl + 1
            data, self._skip_partial = data[nl + 1:], False
        lines = (self._buf + data).split(b"\n")
        self._buf = lines.pop()
        if len(self._buf) > self.max_read_bytes:  # runaway line: drop it, resync at next newline
            self._buf, self._skip_partial = b"", True
        entries: List[dict] = []
        pos = start
        for raw in lines:
            pos += len(raw) + 1
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
                self.ends.append(pos)
        return entries, True


def _glob(pattern: str, exclude: Set[str]) -> Optional[str]:
    for hit in sorted(glob.glob(pattern)):
        if hit.endswith(META) or hit in exclude or not os.path.isfile(hit):
            continue
        return hit
    return None


def _read_meta(path: str) -> Optional[dict]:
    try:
        if os.stat(path).st_size > META_MAX_BYTES:
            return None
        with open(path, "rb") as f:
            meta = json.loads(f.read().decode("utf-8", errors="replace"))
    except (OSError, ValueError):
        return None
    return meta if isinstance(meta, dict) else None


def _inside(path: str, root: str, suffix: str) -> bool:
    """True if `path` ends in `suffix` and resolves (symlinks included) under `root`."""
    if not path.endswith(suffix):
        return False
    try:
        real = os.path.realpath(path)
        base = os.path.realpath(root)
        return os.path.commonpath([real, base]) == base
    except ValueError:
        return False


def _opt_str(value: Any) -> Optional[str]:
    return value if isinstance(value, str) and value else None


class TranscriptWatcher:
    def __init__(
        self,
        store: Store,
        *,
        clock: Callable[[], float] = time.monotonic,
        search_interval: float = 2.0,
        emit: Optional[Callable[[str, str, dict], None]] = None,
        data_dir: Optional[Union[str, Path]] = None,
        relay_offsets: bool = False,
    ) -> None:
        self.store = store
        self.emit = emit
        self.relay_fill: Optional[Callable[[], float]] = None  # how full the relay queue is, 0..1 (set by the server)
        # relay_offsets: remember in <data_dir>/usage_relayed.json how far each transcript's usage was
        # relayed, so a restart re-reads the files (the Store needs the totals) without re-sending.
        self.data_dir = Path(data_dir) if data_dir is not None else None
        self.relay_offsets = relay_offsets and self.data_dir is not None
        self._offsets: Optional[Dict[str, dict]] = None  # loaded on first use
        self._offsets_dirty = False
        self._offsets_saved: Optional[float] = None
        self.clock = clock
        self.search_interval = search_interval
        self._tails: Dict[Tuple[str, str, str], Tail] = {}
        self._found: Dict[Tuple[str, str], str] = {}
        # per-agent backoff: (next attempt time, wait to use after the next miss)
        self._searched: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._meta_looked: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._meta_done: Set[Tuple[str, str]] = set()
        self._head_done: Dict[str, str] = {}  # session id -> main transcript path already scanned
        self._head_looked: Dict[Tuple[str, str], Tuple[float, float]] = {}  # (session id, path) backoff

    def poll(self) -> Set[str]:
        """Read every transcript; returns session ids whose context changed or files grew."""
        changed: Set[str] = set()
        try:
            targets = self.store.transcript_targets()
        except Exception:
            log.debug("transcript_targets failed", exc_info=True)
            return changed
        main_paths = {
            sid: path for sid, aid, path in targets if aid == MAIN and path and path.endswith(JSONL)
        }
        live = {(sid, aid) for sid, aid, _ in targets}
        for key in [k for k in self._found if k not in live]:
            del self._found[key]
        for table in (self._searched, self._meta_looked):
            for key in [k for k in table if k not in live]:
                del table[key]
        self._meta_done &= live
        sessions = {sid for sid, _ in live}
        for sid in [k for k in self._head_done if k not in sessions]:
            del self._head_done[sid]
        for key in [k for k in self._head_looked if k[0] not in sessions]:
            del self._head_looked[key]
        seen: Set[Tuple[str, str, str]] = set()
        for sid, aid, path in targets:
            try:
                self._poll_target(sid, aid, path, main_paths.get(sid), changed, seen)
            except Exception:
                log.debug("transcript poll failed for %s/%s", sid, aid, exc_info=True)
        for key in [k for k in self._tails if k not in seen]:
            del self._tails[key]
        self._save_offsets()
        return changed

    # ---- relayed-offset file (SPEC 13.6) ----------------------------------------------------

    def enable_relay_offsets(self, data_dir: Union[str, Path]) -> None:
        self.data_dir, self.relay_offsets = Path(data_dir), True

    def _offsets_path(self) -> Path:
        assert self.data_dir is not None
        return self.data_dir / OFFSETS_FILE

    def _load_offsets(self) -> Dict[str, dict]:
        if self._offsets is None:
            self._offsets = {}
            try:
                path = self._offsets_path()
                if path.stat().st_size <= OFFSETS_MAX_PATHS * 1024:
                    raw = json.loads(path.read_text(encoding="utf-8"))
                    if isinstance(raw, dict):
                        for key, ent in raw.items():
                            if isinstance(key, str) and isinstance(ent, dict) and all(
                                _is_int(ent.get(k)) and ent[k] >= 0 for k in ("dev", "ino", "offset")
                            ):
                                self._offsets[key] = {k: ent[k] for k in ("dev", "ino", "offset")}
            except (OSError, ValueError, RecursionError, UnicodeError):
                self._offsets = {}  # missing or corrupt: nothing relayed yet (the hub drops duplicates)
            while len(self._offsets) > OFFSETS_MAX_PATHS:
                del self._offsets[next(iter(self._offsets))]
        return self._offsets

    def _relayed_start(self, path: str) -> int:
        """Byte offset up to which this transcript's usage was already relayed (0 if unknown or replaced)."""
        if not self.relay_offsets:
            return 0
        ent = self._load_offsets().get(path)
        try:
            st = os.stat(path)
        except OSError:
            return 0
        if ent is None or ent["dev"] != st.st_dev or ent["ino"] != st.st_ino or ent["offset"] > st.st_size:
            return 0
        return ent["offset"]

    def _remember_relayed(self, path: str, offset: int) -> None:
        try:
            st = os.stat(path)
        except OSError:
            return
        offsets = self._load_offsets()
        offsets.pop(path, None)  # re-insert last: the oldest paths are dropped first
        offsets[path] = {"dev": st.st_dev, "ino": st.st_ino, "offset": offset}
        while len(offsets) > OFFSETS_MAX_PATHS:
            del offsets[next(iter(offsets))]
        self._offsets_dirty = True

    def _save_offsets(self, force: bool = False) -> None:
        """Write the offset file atomically, at most every OFFSETS_INTERVAL seconds unless forced."""
        if not (self.relay_offsets and self._offsets_dirty and self._offsets is not None):
            return
        now = self.clock()
        if not force and self._offsets_saved is not None and now - self._offsets_saved < OFFSETS_INTERVAL:
            return
        try:
            self.data_dir.mkdir(parents=True, exist_ok=True)  # type: ignore[union-attr]
            fd, tmp = tempfile.mkstemp(dir=str(self.data_dir), prefix=OFFSETS_FILE + ".")
            try:
                with os.fdopen(fd, "w", encoding="utf-8") as f:
                    json.dump(dict(self._offsets), f, separators=(",", ":"))
                os.replace(tmp, self._offsets_path())
            except BaseException:
                with contextlib.suppress(OSError):
                    os.unlink(tmp)
                raise
            self._offsets_dirty = False
            self._offsets_saved = now
        except Exception:
            log.debug("could not write %s", OFFSETS_FILE, exc_info=True)
            self._offsets_saved = now

    def flush_offsets(self) -> None:
        """Write pending offsets now (server shutdown)."""
        self._save_offsets(force=True)

    def _due(self, table: Dict[Tuple[str, str], Tuple[float, float]], key: Tuple[str, str]) -> bool:
        ent = table.get(key)
        return ent is None or self.clock() >= ent[0]

    def _miss(self, table: Dict[Tuple[str, str], Tuple[float, float]], key: Tuple[str, str]) -> None:
        ent = table.get(key)
        wait = ent[1] if ent else self.search_interval
        table[key] = (self.clock() + wait, min(wait * 2, max(MAX_BACKOFF, self.search_interval)))

    def _poll_target(
        self,
        sid: str,
        aid: str,
        path: Optional[str],
        main_path: Optional[str],
        changed: Set[str],
        seen: Set[Tuple[str, str, str]],
    ) -> None:
        key = (sid, aid)
        is_main = aid == MAIN
        if is_main:
            if main_path is None or path != main_path:
                return
        else:
            if main_path is None:
                return
            root = os.path.dirname(main_path)
            if path and not _inside(path, root, JSONL):
                log.debug("ignoring transcript path outside session dir: %r", path)
                path = None
            if path:
                self._found[key] = path
            path = path or self._found.get(key)
            if not path and SAFE_AID.fullmatch(aid) and self._due(self._searched, key):
                path = self._search(main_path, aid)
                if path:
                    self._found[key] = path
                    self._searched.pop(key, None)
                else:
                    self._miss(self._searched, key)
            if key not in self._meta_done and SAFE_AID.fullmatch(aid):
                self._apply_meta(sid, aid, main_path, path)
        if not path:
            return
        hkey = (sid, path)
        if is_main and self._head_done.get(sid) != path and self._due(self._head_looked, hkey):
            cwd, final = head_cwd(path)
            if final:
                self._head_done[sid] = path
                self._head_looked.pop(hkey, None)
            else:  # missing, empty or still short: look again later, with backoff
                self._miss(self._head_looked, hkey)
            if cwd and self.store.set_project_cwd(sid, cwd):
                changed.add(sid)
                self._emit("cwd", sid, {"cwd": cwd})
        tkey = (sid, aid, path)
        seen.add(tkey)
        tail = self._tails.get(tkey)
        if tail is None:
            # A changed main transcript_path gets a fresh Tail; the agent's context
            # stays at its last value until the new file reports usage.
            tail = self._tails[tkey] = Tail(path)
            tail.relayed = self._relayed_start(path)
        if self.emit is not None and len(tail.backlog) >= BACKLOG_MAX:
            entries, grew, tail.ends = [], False, []  # the relay is behind: let it catch up before reading more
        else:
            entries, grew = tail.read()
        if grew:
            self.store.touch(sid)
            changed.add(sid)
        if is_main:
            ai, custom = latest_titles(entries)
            if tail.skipped_head and not tail.title_checked:
                tail.title_checked = True
                if ai is None or custom is None:  # a title may sit before the tail window
                    back_ai, back_custom = scan_titles(path)
                    ai, custom = ai or back_ai, custom or back_custom
            if ai and self.store.set_ai_title(sid, ai):
                changed.add(sid)
                self._emit("title", sid, {"source": "generated", "title": ai})
            if custom and self.store.set_custom_title(sid, custom):
                changed.add(sid)
                self._emit("title", sid, {"source": "custom", "title": custom})
        if tail.reset:  # the file shrank: it is new content, so nothing of it was relayed
            tail.reset, tail.relayed, tail.ctx_pending = False, 0, None
            tail.backlog.clear()
        for entry, end in zip(entries, tail.ends):
            usage = usage_from_entry(entry, main=is_main)
            if usage is None:
                continue
            tail.ctx_pending = usage
            self.store.add_usage(sid, aid, usage.data())  # the Store sees every entry exactly once
            changed.add(sid)
            if self.emit is not None and end > max(tail.relayed, tail.backlog[-1][0] if tail.backlog else 0):
                tail.backlog.append((end, usage.data()))
        self._relay_backlog(sid, aid, path, tail)
        latest = tail.ctx_pending
        if latest is not None and tail.at_eof:  # while catching up, the gauge waits for the end of the file
            tail.ctx_pending = None
            self.store.set_context(sid, aid, latest.tokens, latest.model)
            changed.add(sid)
            self._emit("ctx", sid, {"agent_id": aid, "tokens": latest.tokens, "model": latest.model})

    def _emit(self, kind: str, sid: str, data: dict) -> bool:
        """Hand one record to `emit`; False when it was refused (a callback returning None accepted it)."""
        if self.emit is None:
            return True
        try:
            return self.emit(kind, sid, data) is not False
        except Exception:
            log.debug("emit failed", exc_info=True)
            return True

    def _relay_backlog(self, sid: str, aid: str, path: str, tail: Tail) -> None:
        """Relay queued usage in order. The offset moves past an entry only once `emit` accepted it; the
        first refusal stops this file until the next tick. A nearly full relay queue pauses relaying."""
        if not tail.backlog or self.emit is None:
            return
        try:
            if self.relay_fill is not None and self.relay_fill() > RELAY_FILL_MAX:
                return
        except Exception:
            log.debug("relay_fill failed", exc_info=True)
        advanced = False
        while tail.backlog:
            end, data = tail.backlog[0]
            if not self._emit("usage", sid, {"agent_id": aid, "agent_type": self.store.agent_type(sid, aid), **data}):
                break
            tail.backlog.popleft()
            tail.relayed, advanced = end, True
        if advanced and self.relay_offsets:
            self._remember_relayed(path, tail.relayed)

    def _search(self, main_path: str, aid: str) -> Optional[str]:
        base = main_path[: -len(JSONL)]
        esc_aid = glob.escape(aid)
        esc_dir = glob.escape(base)
        esc_parent = glob.escape(os.path.dirname(main_path))
        patterns = [f"{esc_dir}/subagents/agent-{esc_aid}*{JSONL}"]
        if len(aid) >= LOOSE_MIN_AID:
            patterns += [f"{esc_dir}/*{esc_aid}*{JSONL}", f"{esc_parent}/*{esc_aid}*{JSONL}"]
        root = os.path.dirname(main_path)
        for pattern in patterns:
            hit = _glob(pattern, {main_path})
            if hit and _inside(hit, root, JSONL):
                return hit
        return None

    def _apply_meta(self, sid: str, aid: str, main_path: str, path: Optional[str]) -> None:
        key = (sid, aid)
        if not self._due(self._meta_looked, key):
            return
        root = os.path.dirname(main_path)
        candidates = [f"{main_path[: -len(JSONL)]}/subagents/agent-{aid}{META}"]
        if path and path.endswith(JSONL):
            candidates.append(path[: -len(JSONL)] + META)
        for cand in candidates:
            if not _inside(cand, root, META):
                continue
            meta = _read_meta(cand)
            if meta is None:
                continue
            self._meta_done.add(key)
            self._meta_looked.pop(key, None)
            desc, tool_use_id = _opt_str(meta.get("description")), _opt_str(meta.get("toolUseId"))
            self.store.apply_subagent_meta(sid, aid, desc, tool_use_id)
            self._emit("meta", sid, {
                "agent_id": aid, "agent_type": _opt_str(meta.get("agentType")),
                "description": desc, "tool_use_id": tool_use_id,
            })
            return
        self._miss(self._meta_looked, key)

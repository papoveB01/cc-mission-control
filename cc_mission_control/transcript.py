"""Transcript tailing: context usage per agent (SPEC sections 5.2 and 5.3)."""

from __future__ import annotations

import glob
import json
import logging
import os
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Dict, List, Optional, Set, Tuple, Union

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


@dataclass
class Usage:
    tokens: int
    model: Optional[str]


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
    tokens = sum(v for v in (usage.get(k) for k in USAGE_KEYS) if _is_int(v) and v > 0)
    model = message.get("model")
    if not isinstance(model, str) or not model or model == SYNTHETIC:
        model = None
    return Usage(tokens, model)


class Tail:
    """Incremental JSONL reader for one file (SPEC 5.3)."""

    def __init__(
        self,
        path: Union[str, Path],
        *,
        start_tail_bytes: int = 2_000_000,
        max_read_bytes: int = 4_000_000,
    ) -> None:
        self.path = str(path)
        self.start_tail_bytes = max(0, start_tail_bytes)
        self.max_read_bytes = max(1, max_read_bytes)
        self._offset: Optional[int] = None  # None until the first successful stat
        self._buf = b""
        self._skip_partial = False

    def read(self) -> Tuple[List[dict], bool]:
        """New parsed entries and whether any bytes were read. Never raises."""
        try:
            return self._read()
        except Exception:
            log.debug("tail read failed: %s", self.path, exc_info=True)
            return [], False

    def _read(self) -> Tuple[List[dict], bool]:
        try:
            size = os.stat(self.path).st_size
        except OSError:
            return [], False
        if self._offset is None:
            self._offset = 0
            if size > self.start_tail_bytes:
                self._offset = size - self.start_tail_bytes
                self._skip_partial = True
        elif size < self._offset:
            self._offset, self._buf, self._skip_partial = 0, b"", False
        if size == self._offset:
            return [], False
        with open(self.path, "rb") as f:
            f.seek(self._offset)
            data = f.read(min(size - self._offset, self.max_read_bytes))
        if not data:
            return [], False
        self._offset += len(data)
        if self._skip_partial:
            nl = data.find(b"\n")
            if nl < 0:
                return [], True
            data, self._skip_partial = data[nl + 1:], False
        lines = (self._buf + data).split(b"\n")
        self._buf = lines.pop()
        if len(self._buf) > self.max_read_bytes:  # runaway line: drop it, resync at next newline
            self._buf, self._skip_partial = b"", True
        entries: List[dict] = []
        for raw in lines:
            raw = raw.strip()
            if not raw:
                continue
            try:
                obj = json.loads(raw.decode("utf-8", errors="replace"))
            except ValueError:
                continue
            if isinstance(obj, dict):
                entries.append(obj)
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
    ) -> None:
        self.store = store
        self.clock = clock
        self.search_interval = search_interval
        self._tails: Dict[Tuple[str, str, str], Tail] = {}
        self._found: Dict[Tuple[str, str], str] = {}
        # per-agent backoff: (next attempt time, wait to use after the next miss)
        self._searched: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._meta_looked: Dict[Tuple[str, str], Tuple[float, float]] = {}
        self._meta_done: Set[Tuple[str, str]] = set()

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
        seen: Set[Tuple[str, str, str]] = set()
        for sid, aid, path in targets:
            try:
                self._poll_target(sid, aid, path, main_paths.get(sid), changed, seen)
            except Exception:
                log.debug("transcript poll failed for %s/%s", sid, aid, exc_info=True)
        for key in [k for k in self._tails if k not in seen]:
            del self._tails[key]
        return changed

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
        tkey = (sid, aid, path)
        seen.add(tkey)
        tail = self._tails.get(tkey)
        if tail is None:
            # A changed main transcript_path gets a fresh Tail; the agent's context
            # stays at its last value until the new file reports usage.
            tail = self._tails[tkey] = Tail(path)
        entries, grew = tail.read()
        if grew:
            self.store.touch(sid)
            changed.add(sid)
        latest: Optional[Usage] = None
        for entry in entries:
            usage = usage_from_entry(entry, main=is_main)
            if usage is not None:
                latest = usage
        if latest is not None:
            self.store.set_context(sid, aid, latest.tokens, latest.model)
            changed.add(sid)

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
            self.store.apply_subagent_meta(
                sid, aid, _opt_str(meta.get("description")), _opt_str(meta.get("toolUseId"))
            )
            return
        self._miss(self._meta_looked, key)

"""Token accounting helpers and the API price table (SPEC 13.6), shared by the server and the hub.

Costs are an estimate at Anthropic API list prices. Nothing here raises on bad input.
"""

from __future__ import annotations

import json
import logging
import math
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

log = logging.getLogger(__name__)

AS_OF = "2026-11-27"
# the order of every counts tuple below
COUNT_FIELDS = ("input", "cache_creation_5m", "cache_creation_1h", "cache_read", "output", "web_search", "web_fetch")
IN, C5, C1, READ, OUT, SEARCH, FETCH = range(7)
MAX_COUNT = 2**53  # JSON-safe integer range
LABEL_MAX = 200
SPEED_MAX = 32
MESSAGE_ID_MAX = 256
RATE_KEYS = ("input", "cache_write_5m", "cache_write_1h", "cache_read", "output")
MAX_PRICE_FILE_BYTES = 1_000_000
_DATE_TAIL = re.compile(r"[-@]\d{8}(?:\D.*)?$", re.S)
_BRACKET_TAIL = re.compile(r"\[[^\]]*\]$")

Rate = Dict[str, Any]  # RATE_KEYS -> float, plus an optional "long_context" Rate with "over"


def _row(i: float, w5: float, w1: float, r: float, o: float) -> Rate:
    return {"input": i, "cache_write_5m": w5, "cache_write_1h": w1, "cache_read": r, "output": o}


def _builtin_models() -> Dict[str, Rate]:
    models: Dict[str, Rate] = {}

    def put(keys: Sequence[str], row: Rate) -> None:
        for k in keys:
            models[k] = dict(row)

    put(("claude-fable-5-2", "claude-mythos-5-2"), _row(8, 10, 16, 0.2, 40))
    put(("claude-fable-5-1", "claude-mythos-5-1"), _row(10, 12.5, 20, 0.25, 50))
    put(("claude-fable-5", "claude-mythos-5"), _row(10, 12.5, 20, 1, 50))
    put(("claude-opus-5-5",), _row(4, 5, 8, 0.2, 20))
    put(("claude-opus-5", "claude-opus-4-8", "claude-opus-4-7", "claude-opus-4-6", "claude-opus-4-5"),
        _row(5, 6.25, 10, 0.5, 25))
    put(("claude-opus-4-1", "claude-opus-4"), _row(15, 18.75, 30, 1.5, 75))
    put(("claude-sonnet-5-5",), _row(2, 2.5, 4, 0.1, 10))
    put(("claude-sonnet-5",), _row(2, 2.5, 4, 0.2, 10))
    put(("claude-sonnet-4-6", "claude-sonnet-4-5", "claude-sonnet-4"), _row(3, 3.75, 6, 0.3, 15))
    put(("claude-haiku-5-5",), _row(0.1, 0.125, 0.2, 0.01, 0.5))
    models["claude-haiku-5-5"]["long_context"] = dict(_row(0.5, 0.625, 1, 0.05, 2.5), over=100_000)
    put(("claude-haiku-4-5",), _row(1, 1.25, 2, 0.1, 5))
    put(("claude-3-5-haiku",), _row(0.8, 1, 1.6, 0.08, 4))
    return models


WINDOW_1M = 1_000_000
WINDOW_200K = 200_000
MAX_WINDOW = 2**31


def _builtin_windows() -> Dict[str, int]:
    """Context window per model key (SPEC 5.2, checked 2026-11-27)."""
    out: Dict[str, int] = {}
    for k in ("claude-fable-5-2", "claude-fable-5-1", "claude-fable-5", "claude-mythos-5-2", "claude-mythos-5-1",
              "claude-mythos-5", "claude-opus-5-5", "claude-opus-5", "claude-opus-4-8", "claude-opus-4-7",
              "claude-opus-4-6", "claude-sonnet-5-5", "claude-sonnet-5", "claude-sonnet-4-6", "claude-haiku-5-5"):
        out[k] = WINDOW_1M
    for k in ("claude-opus-4-5", "claude-opus-4-1", "claude-opus-4", "claude-sonnet-4-5", "claude-sonnet-4",
              "claude-haiku-4-5"):
        out[k] = WINDOW_200K
    return out


_FUTURE = re.compile(r"claude-[a-z]+-(\d{1,2})(?:-(\d{1,2})(?!\d))?(?:[-@]\d{8}.*)?", re.S)


def future_window(model: Any) -> Optional[int]:
    """1M for an id shaped `claude-<family>-<major>[-<minor>]` at version 4.6 or later (SPEC 5.2), else None."""
    name = normalize_model(model)
    m = _FUTURE.fullmatch(name) if name else None
    if m is None:
        return None
    return WINDOW_1M if (int(m.group(1)), int(m.group(2) or 0)) >= (4, 6) else None


def normalize_model(model: Any) -> Optional[str]:
    """Lowercase, drop a trailing `[...]` (e.g. `[1m]`) and any provider prefix ending in `anthropic.`."""
    if not isinstance(model, str):
        return None
    name = _BRACKET_TAIL.sub("", model.strip().lower())
    if "anthropic." in name:
        name = name.split("anthropic.", 1)[1]
    return name or None


@dataclass
class Pricing:
    models: Dict[str, Rate]
    as_of: str = AS_OF
    source: str = "builtin"
    web_search_per_1k: float = 10.0
    fast_multiplier: float = 2.0
    us_geo_multiplier: float = 1.1
    windows: Optional[Dict[str, int]] = None  # None = the built-in window table
    _keys: List[str] = field(default_factory=list, init=False, repr=False)  # longest first
    _wkeys: List[str] = field(default_factory=list, init=False, repr=False)
    _wmemo: Dict[Optional[str], Optional[int]] = field(default_factory=dict, init=False, repr=False)
    _memo: Dict[Optional[str], Optional[Tuple[str, Rate]]] = field(default_factory=dict, init=False, repr=False)

    def __post_init__(self) -> None:
        self._keys = sorted(self.models, key=lambda k: (-len(k), k))
        if self.windows is None:
            self.windows = _builtin_windows()
        self._wkeys = sorted(self.windows, key=lambda k: (-len(k), k))

    def lookup(self, model: Any) -> Optional[Tuple[str, Rate]]:
        """(key, row) of the longest matching key, or None (unpriced). A key matches exactly, or
        followed by `-` or `@` and an 8-digit date (plus anything after the date)."""
        raw = model if isinstance(model, str) else None
        if raw in self._memo:
            return self._memo[raw]
        name = normalize_model(raw)
        found = None
        if name:
            for key in self._keys:
                if name == key or (name.startswith(key) and _DATE_TAIL.fullmatch(name, len(key))):
                    found = (key, self.models[key])
                    break
        if len(self._memo) > 2048:
            self._memo.clear()
        self._memo[raw] = found
        return found

    def window(self, model: Any) -> Optional[int]:
        """Context window of a model by the longest matching key (same matching as prices), or None."""
        raw = model if isinstance(model, str) else None
        if raw in self._wmemo:
            return self._wmemo[raw]
        name = normalize_model(raw)
        found = None
        if name:
            for key in self._wkeys:
                if name == key or (name.startswith(key) and _DATE_TAIL.fullmatch(name, len(key))):
                    found = self.windows[key]  # type: ignore[index]
                    break
        if found is None:
            found = future_window(raw)
        if len(self._wmemo) > 2048:
            self._wmemo.clear()
        self._wmemo[raw] = found
        return found

    def over_values(self) -> List[int]:
        """Distinct long-context thresholds in this table (the hub buckets its SQL by them)."""
        return sorted({int(r["long_context"]["over"]) for r in self.models.values() if r.get("long_context")})

    def cost(
        self,
        model: Any,
        counts: Sequence[int],
        speed: Any = None,
        geo: Any = None,
        long_hint: Optional[Dict[int, bool]] = None,
    ) -> Optional[float]:
        """Estimated USD of one message (or of an aggregate of messages sharing model, speed, geo and
        long-context bucket), or None when the model has no price. `long_hint` maps a threshold to
        whether the aggregate is over it; without it the threshold is checked on `counts`."""
        found = self.lookup(model)
        if found is None:
            return 0.0 if not any(counts[:5]) and not counts[SEARCH] else None
        row = found[1]
        long = row.get("long_context")
        if long:
            if long_hint is not None:
                over = bool(long_hint.get(int(long["over"])))
            else:
                over = counts[IN] + counts[C5] + counts[C1] + counts[READ] > long["over"]
            if over:
                row = long
        usd = (counts[IN] * row["input"] + counts[C5] * row["cache_write_5m"] + counts[C1] * row["cache_write_1h"]
               + counts[READ] * row["cache_read"] + counts[OUT] * row["output"]) / 1e6
        if speed == "fast":
            usd *= self.fast_multiplier
        if geo == "us":
            usd *= self.us_geo_multiplier
        return usd + counts[SEARCH] * self.web_search_per_1k / 1000

    def to_info(self, unpriced: Sequence[str] = ()) -> dict:
        return {"source": self.source, "as_of": self.as_of, "unpriced_models": sorted(unpriced)}


BUILTIN = Pricing(_builtin_models())


def message_cost(
    pricing: Pricing, model: Any, counts: Sequence[int], speed: Any = None, geo: Any = None
) -> Optional[float]:
    """Estimated USD of one message at list prices; None when its model is unpriced (13.6)."""
    return pricing.cost(model, counts, speed, geo)


def _num(value: Any, low: float = 0.0) -> Optional[float]:
    if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value) or value < low:
        return None
    return float(value)


def _rate(raw: Any, long: bool = False) -> Optional[Rate]:
    if not isinstance(raw, dict):
        return None
    out: Rate = {}
    for k in RATE_KEYS:
        v = _num(raw.get(k))
        if v is None:
            return None
        out[k] = v
    if long:
        over = raw.get("over")
        if isinstance(over, bool) or not isinstance(over, int) or over < 0:
            return None
        out["over"] = over
    elif raw.get("long_context") is not None:
        lc = _rate(raw["long_context"], long=True)
        if lc is None:
            return None
        out["long_context"] = lc
    return out


def _parse_windows(raw: Any) -> Dict[str, int]:
    """The built-in window table with the file's entries laid over it."""
    if not isinstance(raw, dict):
        raise ValueError("not an object")
    extra: Dict[str, int] = {}
    for key, v in raw.items():
        if (not isinstance(key, str) or not normalize_model(key) or isinstance(v, bool)
                or not isinstance(v, int) or not 0 < v <= MAX_WINDOW):
            raise ValueError("bad entry")
        extra[normalize_model(key)] = v  # type: ignore[index]
    return {**_builtin_windows(), **extra}


def _parse_pricing(doc: Any) -> Pricing:
    if not isinstance(doc, dict) or not isinstance(doc.get("models"), dict) or not doc["models"]:
        raise ValueError("missing models")
    models: Dict[str, Rate] = {}
    for key, raw in doc["models"].items():
        rate = _rate(raw)
        if rate is None or not isinstance(key, str) or not normalize_model(key):
            raise ValueError("bad model entry")
        models[normalize_model(key)] = rate  # type: ignore[index]
    extras = {}
    for name, default in (("web_search_per_1k", 10.0), ("fast_multiplier", 2.0), ("us_geo_multiplier", 1.1)):
        v = _num(doc.get(name), 0.0) if name in doc else default
        if v is None:
            raise ValueError(f"bad {name}")
        extras[name] = v
    windows = None
    if doc.get("context_windows") is not None:
        try:
            windows = _parse_windows(doc["context_windows"])
        except ValueError as exc:  # the prices of the file still apply
            log.warning("CCMC_PRICING_FILE context_windows ignored, using the built-in windows: %s", exc)
    as_of = doc.get("as_of")
    return Pricing(models, as_of[:32] if isinstance(as_of, str) and as_of else "unknown", "file",
                   windows=windows, **extras)


def load_pricing(path: Optional[Any] = None) -> Pricing:
    """The built-in table, or the JSON file at `path` (replacing it entirely). A missing, unreadable or
    invalid file logs one line and falls back to the built-in table."""
    if not path:
        return BUILTIN
    try:
        p = Path(path)
        if p.stat().st_size > MAX_PRICE_FILE_BYTES:
            raise ValueError("file too large")
        return _parse_pricing(json.loads(p.read_text(encoding="utf-8")))
    except (OSError, ValueError, RecursionError, TypeError, UnicodeError) as exc:
        log.warning("CCMC_PRICING_FILE ignored, using the built-in price table: %s", exc)
        return BUILTIN


# ---- usage validation -----------------------------------------------------------------------


def _count(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and 0 <= value <= MAX_COUNT


def split_cache(total: Optional[int], c5: Optional[int], c1: Optional[int]) -> Tuple[int, int]:
    """(5m, 1h) cache-write counts from the total and the optional TTL split (13.6).

    No split: all of the total is 5m. A split that disagrees with the total: the total wins, and the
    difference goes to (or comes off) 5m, then 1h. No total: the split's sum is the total."""
    if c5 is None and c1 is None:
        return max(0, total or 0), 0
    c5, c1 = max(0, c5 or 0), max(0, c1 or 0)
    if total is None:
        return c5, c1
    diff = total - c5 - c1
    if diff > 0:
        c5 += diff
    elif diff < 0:
        cut = min(c5, -diff)
        c5 -= cut
        c1 -= -diff - cut
    return c5, c1


def merge_counts(
    old: Sequence[int], old_split: bool, new: Sequence[int], new_split: bool
) -> Tuple[Tuple[int, ...], bool]:
    """Merge two reports of one message (COUNT_FIELDS order). Every count takes the highest value, except
    the cache writes: the report with the larger total supplies both 5m and 1h, and on a tie a real split
    wins over a fallback one (13.6). The 5m and 1h counts are never maximized independently."""
    out = [max(a, b) for a, b in zip(old, new)]
    old_total, new_total = old[C5] + old[C1], new[C5] + new[C1]
    new_wins = new_total > old_total or (new_total == old_total and new_split and not old_split)
    src = new if new_wins else old
    out[C5], out[C1] = src[C5], src[C1]
    return tuple(out), new_split if new_wins else old_split


def _label(value: Any, limit: int) -> Optional[str]:
    return value[:limit] if isinstance(value, str) and value else None


def clean_usage(data: Any) -> Optional[dict]:
    """Validated usage data (13.1 fields), or None when malformed.

    `input`, `cache_read` and `output` are required, as is `cache_creation` unless a TTL split is given.
    The newer fields are optional. Counts must be non-negative ints. Returns `message_id` (str or None),
    `model`, `speed`, `inference_geo` (capped strings or None), `ts` (epoch seconds or None), `split`
    (bool: the TTL split was reported, not derived) and every COUNT_FIELDS key, plus
    `cache_creation` (= 5m + 1h)."""
    if not isinstance(data, dict):
        return None
    for k in ("input", "cache_read", "output"):
        if not _count(data.get(k)):
            return None
    for k in ("cache_creation", "cache_creation_5m", "cache_creation_1h", "web_search", "web_fetch"):
        if data.get(k) is not None and not _count(data[k]):
            return None
    total, c5, c1 = data.get("cache_creation"), data.get("cache_creation_5m"), data.get("cache_creation_1h")
    if total is None and c5 is None and c1 is None:
        return None
    c5, c1 = split_cache(total, c5, c1)
    mid = data.get("message_id")
    ts = data.get("ts")
    ts = float(ts) if isinstance(ts, (int, float)) and not isinstance(ts, bool) and math.isfinite(ts) and ts > 0 else None
    split = data["split"] if isinstance(data.get("split"), bool) else (
        data.get("cache_creation_5m") is not None or data.get("cache_creation_1h") is not None)
    out = {
        "ts": ts, "split": split,
        "message_id": mid if isinstance(mid, str) and 0 < len(mid) <= MESSAGE_ID_MAX else None,
        "model": _label(data.get("model"), LABEL_MAX),
        "speed": _label(data.get("speed"), SPEED_MAX),
        "inference_geo": _label(data.get("inference_geo"), SPEED_MAX),
        "input": data["input"], "cache_creation_5m": c5, "cache_creation_1h": c1, "cache_creation": c5 + c1,
        "cache_read": data["cache_read"], "output": data["output"],
        "web_search": data.get("web_search") or 0, "web_fetch": data.get("web_fetch") or 0,
    }
    return out


def counts_of(d: dict) -> Tuple[int, ...]:
    """The COUNT_FIELDS tuple of a cleaned usage dict."""
    return tuple(d[k] for k in COUNT_FIELDS)


class UsageTotals:
    """Running sums for a `Usage` object (SPEC 9.3). `add` with sign -1 takes a message back out."""

    __slots__ = ("counts", "messages", "cost", "unpriced")

    def __init__(self) -> None:
        self.counts = [0] * len(COUNT_FIELDS)
        self.messages = 0
        self.cost = 0.0
        self.unpriced = 0

    def add(self, counts: Sequence[int], cost: Optional[float], sign: int = 1, messages: int = 1) -> None:
        for i, v in enumerate(counts):
            self.counts[i] += sign * v
        self.messages += sign * messages
        if cost is None:
            self.unpriced += sign * messages
        else:
            self.cost += sign * cost

    def merge(self, other: "UsageTotals") -> None:
        for i, v in enumerate(other.counts):
            self.counts[i] += v
        self.messages += other.messages
        self.cost += other.cost
        self.unpriced += other.unpriced

    def to_dict(self) -> dict:
        return usage_dict(self.counts, self.messages, self.cost, self.unpriced)


def usage_dict(counts: Sequence[int], messages: int = 0, cost: float = 0.0, unpriced: int = 0) -> dict:
    """The `Usage` object of SPEC 9.3 from COUNT_FIELDS-ordered counts."""
    c = list(counts)
    return {
        "input": c[IN], "cache_creation": c[C5] + c[C1], "cache_creation_5m": c[C5], "cache_creation_1h": c[C1],
        "cache_read": c[READ], "output": c[OUT], "web_search": c[SEARCH], "web_fetch": c[FETCH],
        "messages": messages, "cost_usd": round(max(cost, 0.0), 6), "unpriced_messages": unpriced,
    }


def empty_usage() -> dict:
    return usage_dict([0] * len(COUNT_FIELDS))

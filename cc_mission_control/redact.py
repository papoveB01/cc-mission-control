"""Secret redaction (SPEC section 10.2).

Every string captured from a hook payload goes through Redactor before it is
stored, displayed, or forwarded. Redaction is best effort.
"""

from __future__ import annotations

import logging
import math
import re
from pathlib import Path
from typing import Any, Callable, Iterable

log = logging.getLogger(__name__)

MASK = "[redacted]"

_KEY_NAMES = (
    r"api[_-]?key|client[_-]?secret|private[_-]?key|access[_-]?key|secret[_-]?key"
    r"|secret|token|password|passwd|pwd"
)

# Keyed secret: an identifier that ends in one of the key names, an optional
# closing quote, then ':' or '=', then the value. The key and separator are kept
# (group 1), the value is masked. Done with a capture group because Python's re
# has no variable-width lookbehind.
_KEYED = re.compile(
    r"(?i)(\b[A-Za-z0-9_.-]*?(?:" + _KEY_NAMES + r")[\"']?\s*[:=]\s*)"
    r"(\"[^\"\n]*\"|'[^'\n]*'|[^\s\"',;&]+)"
)

_BEARER = re.compile(r"(?i)(\bbearer\s+)[A-Za-z0-9._~+/=-]{12,}")

# Whole-match patterns, applied in order. Private keys first so their bodies are
# gone before the token patterns run; a block cut off by truncation upstream
# (no END line) is still masked to the end of the string.
_WHOLE: tuple[re.Pattern[str], ...] = (
    re.compile(
        r"-----BEGIN [A-Z0-9 ]*PRIVATE KEY-----.*?(?:-----END [A-Z0-9 ]*PRIVATE KEY-----|\Z)",
        re.DOTALL,
    ),
    re.compile(r"\bsk-ant-[A-Za-z0-9_-]{8,}"),
    re.compile(r"\bsk-[A-Za-z0-9_-]{20,}"),
    re.compile(r"\b(?:gh[pousr]_[A-Za-z0-9]{20,}|github_pat_[A-Za-z0-9_]{20,})"),
    re.compile(r"\bxox[abprs]-[A-Za-z0-9-]{10,}"),
    re.compile(r"\b(?:AKIA|ASIA)[0-9A-Z]{16}\b"),
    re.compile(r"\bAIza[0-9A-Za-z_-]{30,}"),
    re.compile(r"\beyJ[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}\.[A-Za-z0-9_-]{4,}"),
)


def _keyed_sub(m: re.Match[str]) -> str:
    if m.group(2).strip("\"'") == MASK:
        return m.group(0)
    return m.group(1) + MASK


def load_patterns(path: Path | str | None) -> list[re.Pattern[str]]:
    """Read extra regexes, one per line. '#' lines are comments; invalid ones are skipped."""
    if not path:
        return []
    try:
        lines = Path(path).read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        log.warning("cannot read redaction file %s: %s", path, exc)
        return []
    patterns = []
    for n, line in enumerate(lines, 1):
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        try:
            patterns.append(re.compile(line))
        except re.error as exc:
            log.warning("skipping invalid redaction pattern on line %d of %s: %s", n, path, exc)
    return patterns


def truncate(text: str, limit: int) -> str:
    if limit <= 0 or len(text) <= limit:
        return text
    return f"{text[:limit]}… [{len(text) - limit} more characters]"


class Redactor:
    def __init__(self, extra: Iterable[re.Pattern[str]] = (), max_chars: int = 4000) -> None:
        self.extra = tuple(extra)
        self.max_chars = max_chars

    @classmethod
    def from_config(cls, config: Any) -> "Redactor":
        return cls(load_patterns(config.redact_file), config.max_field_chars)

    def redact(self, text: str) -> str:
        """Mask secrets without truncating."""
        if not text:
            return text
        text = text.encode("utf-8", "replace").decode("utf-8")  # lone surrogates break UTF-8 output
        for pattern in _WHOLE:
            text = pattern.sub(MASK, text)
        text = _BEARER.sub(lambda m: m.group(1) + MASK, text)
        text = _KEYED.sub(_keyed_sub, text)
        for pattern in self.extra:
            text = pattern.sub(MASK, text)
        return text

    def text(self, text: str) -> str:
        """Mask secrets, then truncate to max_chars. Redact first so a cut can't expose a partial secret."""
        return truncate(self.redact(text), self.max_chars)

    def value(self, obj: Any) -> Any:
        """Redact and truncate every string in a nested structure.

        Dict keys are redacted (not truncated) and non-finite floats become None.
        """
        return _walk(obj, self.text, self.redact)


def _walk(obj: Any, fn: Callable[[str], str], key_fn: Callable[[str], str]) -> Any:
    if isinstance(obj, str):
        return fn(obj)
    if isinstance(obj, dict):
        return {key_fn(k if isinstance(k, str) else str(k)): _walk(v, fn, key_fn) for k, v in obj.items()}
    if isinstance(obj, (list, tuple)):
        return [_walk(v, fn, key_fn) for v in obj]
    if isinstance(obj, float) and not math.isfinite(obj):
        return None
    return obj

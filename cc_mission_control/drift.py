"""Drift risk (SPEC 11.12): warning signs that a session is losing track, plus the text the user can paste.

Never claims to detect hallucination. Pure functions over a Session; state.py owns the Store."""

from __future__ import annotations

from typing import Any, Dict, List, Optional, Tuple

from . import describe

WINDOW_SECONDS = 15 * 60
ELEVATED, HIGH = 3, 5
LOOP_MIN = 3
TARGET_CHARS = 120
COMPACT_CHARS = 1000
AUTOCOMPACT_PCT = 0.7
FILES_COMPACT, FILES_HANDOFF, ERRORS_HANDOFF = 5, 15, 5
WRITE_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
FILE_ERROR_TOOLS = ("Read", "Glob", "Grep", "LS", "Edit", "Write", "NotebookEdit")
TARGET_KEYS = ("file_path", "path", "pattern", "notebook_path")
IGNORED_PREFIXES = ("blocked:", "denied")  # harness policy errors and permission denials never count

# code -> (case-insensitive substrings, restrict to FILE_ERROR_TOOLS?). Every pattern has a test.
PATTERNS: Dict[str, List[Tuple[str, bool]]] = {
    "invented": [
        ("No such tool available", False),
        ("InputValidationError", False),
    ],
    "stale": [
        ("File has been modified since read", False),
        ("File has not been read yet", False),
        ("String to replace not found", False),
    ],
    "missing": [
        ("File does not exist", False),
        ("No such file or directory", False),
        ("Path does not exist", False),
        ("does not exist", True),
    ],
}


def error_text(call: Any) -> str:
    """The call's error if it counts toward drift, else ''."""
    if call.synthetic or call.outcome == "Interrupted":  # closed by us, or the user stopped it: not a model error
        return ""
    err = (call.error or "").strip()
    return "" if not err or err.lower().startswith(IGNORED_PREFIXES) else err


def matches(code: str, tool: str, err: str) -> bool:
    low = err.lower()
    return any(p.lower() in low and (not only or tool in FILE_ERROR_TOOLS) for p, only in PATTERNS[code])


def target(call: Any) -> str:
    inp = call.input if isinstance(call.input, dict) else {}
    for k in TARGET_KEYS:
        v = inp.get(k)
        if isinstance(v, str) and v:
            return v
    cmd = inp.get("command")
    return cmd.strip()[:TARGET_CHARS] if isinstance(cmd, str) else ""


def _count_points(n: int) -> int:
    return 0 if n <= 0 else 1 if n == 1 else 2


def level_for(score: int) -> str:
    return "high" if score >= HIGH else "elevated" if score >= ELEVATED else "low"


def main_context_pct(s: Any) -> Optional[int]:
    main = s.agents.get("main")
    if main is None or main.context_tokens is None or not main.context_window:
        return None
    return int(main.context_tokens * 100 / main.context_window)


def compute(s: Any, now: float) -> Tuple[int, List[dict]]:
    """(score, reasons) at `now`; reasons are ordered by points, highest first."""
    found: List[Tuple[int, str, str]] = []  # (points, code, text)
    pct = main_context_pct(s)
    if pct is not None and pct >= 50:
        found.append((3 if pct >= 85 else 2 if pct >= 70 else 1, "context", f"Context at {pct}%"))
    if s.compactions:
        found.append((1 if s.compactions == 1 else 2, "compactions",
                      "Compacted once" if s.compactions == 1 else "Compacted twice" if s.compactions == 2
                      else f"Compacted {s.compactions} times"))
    cutoff = now - WINDOW_SECONDS
    counts = {"invented": 0, "stale": 0, "missing": 0}
    repeats: Dict[Tuple[str, str], int] = {}
    errs: List[Tuple[Any, str]] = []
    wins: List[Any] = []
    for agent in s.agents.values():
        for c in agent.calls:
            if c.ended is None or not (cutoff < c.ended <= now):
                continue
            wins.append(c)
            err = error_text(c) if c.status == "error" else ""
            if err:
                errs.append((c, err))
    last_ok: Dict[Tuple[str, str], float] = {}  # (tool, target) -> latest success in the window
    for c in wins:
        if c.status == "ok":
            k = (c.tool, target(c))
            if k[1]:
                last_ok[k] = max(last_ok.get(k, 0.0), c.ended or 0.0)
    for c, err in errs:
        t = target(c)
        resolved = bool(t) and last_ok.get((c.tool, t), -1.0) > (c.ended or 0.0)
        for code in counts:
            if matches(code, c.tool, err) and not (code != "invented" and resolved):
                counts[code] += 1
        if t:
            repeats[(c.tool, t)] = repeats.get((c.tool, t), 0) + 1
    n = counts["invented"]
    if n:
        found.append((_count_points(n), "invented",
                      "A call to a tool or parameter that doesn't exist" if n == 1
                      else f"{n} calls to a tool or parameter that doesn't exist"))
    n = counts["stale"]
    if n:
        found.append((_count_points(n), "stale", "Edited a file after it changed" + (f" ({n})" if n > 1 else "")))
    n = counts["missing"]
    if n:
        found.append((_count_points(n), "missing", "Referenced a file or path that doesn't exist"
                      + (f" ({n})" if n > 1 else "")))
    loops = [(v, k) for k, v in repeats.items() if v >= LOOP_MIN]
    if loops:
        v, (tool, t) = max(loops)
        found.append((2, "loop", f"{v} failing retries of {tool} `{describe.clip(t, 60)}`"))
    found.sort(key=lambda f: -f[0])  # stable: ties keep the table order
    return sum(f[0] for f in found), [{"code": f[1], "text": f[2]} for f in found]


def evaluate(s: Any, now: float) -> bool:
    """Recompute and cache the drift on the session; True when the level moved.

    Called from ingest (while not low, or on a drift event) and the housekeeping tick, never from
    to_dict, so live and hub replay see the same sequence. Ended sessions keep their last value."""
    if s.status == "ended":
        return False
    score, reasons = compute(s, now)
    s.drift_score, s.drift_reasons = score, reasons
    level = level_for(score)
    if level == s.drift_level:
        return False
    s.drift_level, s.drift_since = level, now
    return True


def drift_dict(s: Any) -> dict:
    """The cached value; read-only."""
    return {"level": s.drift_level, "score": s.drift_score, "reasons": list(s.drift_reasons), "since": s.drift_since}


# ---- paste-ready text -------------------------------------------------------

def _all_calls(s: Any) -> List[Any]:
    return sorted((c for a in s.agents.values() for c in a.calls), key=lambda c: c.started)


def changed_files(s: Any, limit: int) -> List[str]:
    seen: List[str] = []
    for c in reversed(_all_calls(s)):
        if c.tool in WRITE_TOOLS and c.status != "error":
            inp = c.input if isinstance(c.input, dict) else {}
            p = inp.get("file_path") or inp.get("notebook_path")
            if isinstance(p, str) and p and p not in seen:
                seen.append(p)
                if len(seen) >= limit:
                    break
    return seen


def unresolved_errors(s: Any, limit: int) -> List[str]:
    """Latest failures whose tool+target has not succeeded since."""
    calls, out, seen = _all_calls(s), [], set()
    for i in range(len(calls) - 1, -1, -1):
        c = calls[i]
        err = error_text(c) if c.status == "error" else ""
        key = (c.tool, target(c))
        if not err or key in seen:
            continue
        seen.add(key)
        if any(d.status == "ok" and (d.tool, target(d)) == key for d in calls[i + 1:]):
            continue
        t = f" {describe.clip(key[1], 80)}" if key[1] else ""
        out.append(f"{c.tool}{t}: {describe.clip(err.splitlines()[0], 160)}")
        if len(out) >= limit:
            break
    return out


def build_handoff(s: Any, redact: Any) -> dict:
    """{compact, handoff, autocompact_tokens}; every text is redacted before it is clipped."""
    main = s.agents.get("main")
    task = describe.clip(redact(main.task if main else ""), 300) or "the current work"
    files = changed_files(s, FILES_HANDOFF)
    keep = f" Keep: {', '.join(files[:FILES_COMPACT])}." if files else ""
    compact = describe.clip(redact(f"/compact Focus on: {task}.{keep}"), COMPACT_CHARS)
    summary = next((e["text"] for e in reversed(s.activity) if e.get("kind") == "summary"), "")
    name = s.custom_title or s.ai_title or s.title
    lines = [f"# Session handoff: {name}", f"Folder: {s.cwd or s.current_cwd or 'unknown'}", "",
             "## Current task", task]
    if summary:
        lines += ["", "## Last summary", summary]
    if files:
        lines += ["", "## Files changed", *[f"- {f}" for f in files]]
    errs = unresolved_errors(s, ERRORS_HANDOFF)
    if errs:
        lines += ["", "## Unresolved errors", *[f"- {e}" for e in errs]]
    lines += ["", "Continue from here."]
    win = main.context_window if main and main.context_window else None
    return {"compact": compact, "handoff": redact("\n".join(lines)),
            "autocompact_tokens": round(AUTOCOMPACT_PCT * win) if win else None}


def nudge_text(drift: dict, redact: Any) -> str:
    why = "; ".join(r["text"] for r in drift["reasons"]) or "no specific signal"
    return redact(
        f"Mission Control: drift risk is {drift['level']} in this session ({why}). "
        "Before editing, re-read the files you change; if you are unsure about a file or tool, "
        "check instead of assuming. Suggest the user run /compact if context is above 80%."
    )

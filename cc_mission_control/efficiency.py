"""Task efficiency (SPEC 13.10): measures, waste signals, baseline pooling, percentile score, label and reasons.

Pure functions with no I/O, shared by the live server and the hub. Nothing here raises on odd input, and no
AI call is ever made: a task is judged against the user's own baseline of similar finished tasks.

Formula changes must bump `EFFICIENCY_VERSION`; the hub then rebuilds the stored rows (SPEC 13.10).
"""

from __future__ import annotations

import hashlib
import math
from fractions import Fraction
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import describe, drift

BASELINE_VERSION = 1  # the baseline document (local file and hub route) does not depend on the task window; it changes only with the row layout
EFFICIENCY_VERSION = 2  # 2 (M48): context windows come from the model table, so near_full flags are recomputed

MIN_CALLS = 3  # a task needs this many calls to be rated, and to join a baseline
BASELINE_MIN = 20  # fewer baseline tasks than this: "not enough history"
POOL_PROJECT_MIN = 30  # the same project and kind pool is used from this size, else the kind across projects
BASELINE_MAX = 500  # only the latest tasks count
EFFICIENT_AT, TYPICAL_AT = 70, 40
WASTE_PENALTY = 8
READ_REPEAT_MIN = 3  # the same file read this many times with no edit between
RETRY_CHAIN_MIN = 2  # consecutive failures of the same tool and target
NEAR_FULL = 0.9
REASONS_MAX = 3
TEXT_MAX = 160
MIN_RATIO_FACTOR = 1.25  # "x times the usual" is only said from this factor up
MIN_CACHE_GAP = 0.10  # cache reuse must be this far below the usual to be called out

EFFICIENT, TYPICAL, WASTEFUL = "efficient", "typical", "wasteful"
NOT_ENOUGH = "not enough history"
IN_PROGRESS = "in progress"
RATED_LABELS = (EFFICIENT, TYPICAL, WASTEFUL)

# The four ratios, in baseline-row order (after the time column), and which way is better.
RATIOS = ("tokens_per_call", "failure_rate", "cache_reuse", "seconds_per_call")
HIGHER_IS_BETTER = frozenset({"cache_reuse"})
WASTE_KINDS = ("repeated_read", "retry_chain", "loop", "denied", "near_full")
READ_TOOLS = ("Read", "NotebookRead")
EDIT_TOOLS = ("Edit", "Write", "MultiEdit", "NotebookEdit")
IGNORED_PREFIXES = drift.IGNORED_PREFIXES  # "blocked:" and "denied": policy errors are not the model's failures


def _num(value: Any) -> Optional[float]:
    if isinstance(value, (int, float)) and not isinstance(value, bool) and math.isfinite(value):
        return float(value)
    return None


def _clip(text: str, limit: int = TEXT_MAX) -> str:
    return describe.clip(text, limit)


def kind_of(task_kind: Any, agent_type: Any) -> str:
    """`main` for main turns, else the subagent's agent_type (`unknown` when it has none)."""
    if task_kind == "turn":
        return "main"
    if agent_type == "main":  # a subagent whose type is literally "main" must not share the main turns' baseline
        return "main (subagent)"
    return agent_type if isinstance(agent_type, str) and agent_type else "unknown"


def project_key(project: Any) -> str:
    """The name a baseline pool travels under: a short hash, so the baseline holds no project text."""
    text = project if isinstance(project, str) else ""
    return hashlib.sha256(text.encode("utf-8", "replace")).hexdigest()[:12]


# ---- call-based measures ---------------------------------------------------------------------------------------

def failed(call: Any) -> bool:
    """A call the model got wrong: it ended in error and was not interrupted, closed by us, blocked or denied."""
    if getattr(call, "status", None) != "error" or getattr(call, "synthetic", False):
        return False
    if getattr(call, "outcome", "") == "Interrupted":
        return False
    err = (getattr(call, "error", None) or "").strip().lower()
    return not err.startswith(IGNORED_PREFIXES)


def _label(tool: str, key: str) -> str:
    if key.startswith("file:"):
        return f"{tool} {_clip(key[5:], 60)}"
    if key.startswith("bash:"):
        return f"Bash `{_clip(key[5:], 40)}`"
    if ":" in key:
        return f"{tool} `{_clip(key.split(':', 1)[1], 40)}`"
    return tool


def call_measures(calls: Iterable[Any], keys: Optional[Dict[str, str]] = None, cwd: str = "",
                  redact: Any = None) -> dict:
    """The measures that come from a task's own calls (13.10).

    `calls` are the task's calls (objects with the Store's Call fields). `keys` maps call id to its target key
    (describe.target_key); missing ones are computed with `cwd`. Returns
    `{retries, retry_tools, waste: {kind: {count, text}}, denied}`. Never raises."""
    try:
        return _call_measures(calls, keys or {}, cwd, redact)
    except Exception:
        return {"retries": 0, "retry_tools": [], "waste": {}, "denied": 0}


def _call_measures(calls: Iterable[Any], keys: Dict[str, str], cwd: str, redact: Any) -> dict:
    ordered = sorted((c for c in calls if getattr(c, "status", None) in ("ok", "error")),
                     key=lambda c: (c.started, c.id))

    def key_of(c: Any) -> str:
        k = keys.get(c.id)
        return k if k is not None else describe.target_key(c.tool, c.input, cwd)

    def say(text: str) -> str:
        text = _clip(text)
        if redact is not None:
            try:
                return redact(text)
            except Exception:
                return ""
        return text

    # retries: a call that repeats a tool and target whose latest earlier call failed
    last_failed: Dict[Tuple[str, str], bool] = {}
    retries, retry_tools = 0, set()
    runs: Dict[Tuple[str, str], int] = {}  # current run of consecutive failures per (tool, target)
    chains: List[Tuple[int, Tuple[str, str]]] = []  # finished runs of >= RETRY_CHAIN_MIN failures
    # repeated reads: reads of a file since its last edit
    reads: Dict[str, int] = {}
    read_runs: List[Tuple[int, str]] = []
    # drift's loop pattern: the same tool and (drift) target failing again and again
    loops: Dict[Tuple[str, str], int] = {}
    denied = 0

    for c in ordered:
        k = (c.tool, key_of(c))
        bad = failed(c)
        if last_failed.get(k):
            retries += 1
            retry_tools.add(c.tool)
        last_failed[k] = bad
        if bad:
            runs[k] = runs.get(k, 0) + 1
        else:
            if runs.get(k, 0) >= RETRY_CHAIN_MIN:
                chains.append((runs[k], k))
            runs[k] = 0
        if drift.error_text(c) and c.status == "error":
            t = drift.target(c)
            if t:
                loops[(c.tool, t)] = loops.get((c.tool, t), 0) + 1
        if c.status == "error" and (getattr(c, "error", None) or "").startswith("Denied"):
            denied += 1
        path = k[1]
        if c.tool in READ_TOOLS and c.status == "ok":
            reads[path] = reads.get(path, 0) + 1
        elif c.tool in EDIT_TOOLS:
            if reads.get(path, 0) >= READ_REPEAT_MIN:
                read_runs.append((reads[path], path))
            reads[path] = 0
    for k, n in runs.items():
        if n >= RETRY_CHAIN_MIN:
            chains.append((n, k))
    for path, n in reads.items():
        if n >= READ_REPEAT_MIN:
            read_runs.append((n, path))

    waste: Dict[str, dict] = {}
    if read_runs:
        n, path = max(read_runs, key=lambda r: (r[0], r[1]))
        shown = path[5:] if path.startswith("file:") else path
        waste["repeated_read"] = {"count": len(read_runs), "text": say(f"Read {_clip(shown, 80)} {n} times without editing it")}
    if chains:
        n, (tool, key) = max(chains, key=lambda r: (r[0], r[1]))
        waste["retry_chain"] = {"count": len(chains), "text": say(f"{_label(tool, key)} failed {n} times in a row")}
    hot = [(n, k) for k, n in loops.items() if n >= drift.LOOP_MIN]
    if hot:
        n, (tool, t) = max(hot)
        waste["loop"] = {"count": len(hot), "text": say(f"{n} failing retries of {tool} `{_clip(t, 60)}`")}
    return {"retries": retries, "retry_tools": sorted(retry_tools), "waste": waste, "denied": denied}


def _plural(n: int, one: str, many: str) -> str:
    return f"{n} {one if n == 1 else many}"


def build_measures(task: dict, eff: Optional[dict], usage: Optional[dict] = None, sub_tokens: Optional[int] = None,
                   now: Optional[float] = None, denied: Optional[int] = None) -> dict:
    """The full measures of a task (13.10). Never raises.

    `task` has started, ended (None while running), calls, errors, kind, peak_context and context_window.
    `eff` is `call_measures` output. `usage` is the 13.6 totals of the task's window (input, cache_creation,
    cache_read, output, cost_usd). `sub_tokens` is the tokens of subagent runs started inside a main turn, and
    None for subagents. `now` is only used for the duration of a running task. `denied` overrides the call count."""
    try:
        return _build_measures(task, eff or {}, usage, sub_tokens, now, denied)
    except Exception:
        return _build_measures({}, {}, None, None, None, None)


def _build_measures(task: dict, eff: dict, usage: Optional[dict], sub_tokens: Optional[int],
                    now: Optional[float], denied: Optional[int]) -> dict:
    started, ended = _num(task.get("started")), _num(task.get("ended"))
    duration = None
    if started is not None:
        end = ended if ended is not None else now
        duration = max(0.0, end - started) if end is not None else None
    calls = int(task.get("calls") or 0)
    errors = int(task.get("errors") or 0)
    retries = int(eff.get("retries") or 0)
    u = usage or {}
    cache_in = (u.get("input") or 0) + (u.get("cache_creation") or 0) + (u.get("cache_read") or 0)
    tokens = cache_in + (u.get("output") or 0)
    cache_reuse = (u.get("cache_read") or 0) / cache_in if cache_in > 0 else None
    share = None
    if task.get("kind") == "turn" and sub_tokens is not None and tokens + sub_tokens > 0:
        share = sub_tokens / (tokens + sub_tokens)
    peak, window = _num(task.get("peak_context")), _num(task.get("context_window"))
    peak_pct = peak / window if peak is not None and window and window > 0 else None

    waste: Dict[str, dict] = {k: dict(v) for k, v in (eff.get("waste") or {}).items() if k in WASTE_KINDS}
    n_denied = max(int(denied or 0), int(eff.get("denied") or 0)) if denied is not None else int(eff.get("denied") or 0)
    if n_denied > 0:
        waste["denied"] = {"count": n_denied, "text": _plural(n_denied, "permission denial", "permission denials")}
    if peak_pct is not None and peak_pct >= NEAR_FULL:
        waste["near_full"] = {"count": 1, "text": f"Context reached {int(round(peak_pct * 100))}% of the window"}
    return {
        "duration_s": None if duration is None else round(duration, 3),
        "tokens": tokens, "cost_usd": round(float(u.get("cost_usd") or 0.0), 6),
        "calls": calls, "errors": errors, "retries": retries,
        "cache_reuse": None if cache_reuse is None else round(cache_reuse, 6),
        "subagent_share": None if share is None else round(share, 6),
        "peak_pct": None if peak_pct is None else round(peak_pct, 6),
        "tokens_per_call": tokens / calls if calls > 0 and tokens > 0 else None,  # no usage recorded: unknown, not 0
        "failure_rate": (errors + retries) / calls if calls > 0 else None,
        "seconds_per_call": duration / calls if calls > 0 and duration is not None else None,
        "retry_tools": list(eff.get("retry_tools") or []),
        "waste": [{"kind": k, "count": waste[k]["count"], "text": waste[k]["text"]} for k in WASTE_KINDS if k in waste],
    }


def ratio_row(measures: dict) -> Tuple[Optional[float], ...]:
    """The four baseline ratios of a task, in RATIOS order."""
    return tuple(_num(measures.get(r)) for r in RATIOS)


# ---- baseline ---------------------------------------------------------------------------------------------------

def select_pool(project_rows: Sequence[Sequence], kind_rows: Sequence[Sequence]) -> Tuple[List[Sequence], str]:
    """The baseline rows for a task, newest first, and which pool they came from ("project" or "kind").

    The same project and kind when that pool has at least POOL_PROJECT_MIN tasks, else the same kind across
    projects. Only the latest BASELINE_MAX count."""
    if len(project_rows) >= POOL_PROJECT_MIN:
        return list(project_rows[:BASELINE_MAX]), "project"
    return list(kind_rows[:BASELINE_MAX]), "kind"


def merge_eff(old: Optional[dict], new: Optional[dict]) -> dict:
    """Combine two `call_measures` of one task, never going down: older calls can be evicted from memory, so a
    count only rises (13.10, "Evicted calls"). The text of the higher count wins, the newer one on a tie."""
    old, new = old or {}, new or {}
    waste: Dict[str, dict] = {}
    for src in (old.get("waste") or {}, new.get("waste") or {}):
        for k, w in src.items():
            if k not in waste or w.get("count", 0) >= waste[k].get("count", 0):
                waste[k] = dict(w)
    tools = sorted(set(old.get("retry_tools") or []) | set(new.get("retry_tools") or []))
    return {"retries": max(int(old.get("retries") or 0), int(new.get("retries") or 0)), "retry_tools": tools,
            "waste": waste, "denied": max(int(old.get("denied") or 0), int(new.get("denied") or 0))}


def merge_waste_lists(old: Sequence[dict], new: Sequence[dict]) -> List[dict]:
    """The same rule for measure waste lists `[{kind, count, text}]`, in the fixed kind order."""
    by: Dict[str, dict] = {}
    for src in (old, new):
        for w in src:
            if isinstance(w, dict) and w.get("kind") in WASTE_KINDS:
                if w["kind"] not in by or int(w.get("count") or 0) >= int(by[w["kind"]].get("count") or 0):
                    by[w["kind"]] = {"kind": w["kind"], "count": int(w.get("count") or 0), "text": str(w.get("text") or "")}
    return [by[k] for k in WASTE_KINDS if k in by]


def percentile(value: Optional[float], baseline: Iterable[Optional[float]], higher_is_better: bool = False) -> Optional[Fraction]:
    """The share of baseline tasks that do worse than `value`; ties count as half. None when either side is unknown."""
    if value is None:
        return None
    vals = [v for v in baseline if v is not None]
    if not vals:
        return None
    mine = round(value, 9)
    worse = ties = 0
    for v in vals:
        v = round(v, 9)
        if v == mine:
            ties += 1
        elif (v < mine) if higher_is_better else (v > mine):
            worse += 1
    return Fraction(2 * worse + ties, 2 * len(vals))  # exact, so a half point never flips on float error


def median(values: Iterable[Optional[float]]) -> Optional[float]:
    vals = sorted(v for v in values if v is not None)
    if not vals:
        return None
    mid = len(vals) // 2
    return vals[mid] if len(vals) % 2 else (vals[mid - 1] + vals[mid]) / 2


def label_for(score: int) -> str:
    return EFFICIENT if score >= EFFICIENT_AT else TYPICAL if score >= TYPICAL_AT else WASTEFUL


def score_from(percentiles: Sequence[Any], waste_kinds: int) -> int:
    """round(100 x mean(percentiles)) - 8 x distinct waste kinds, clamped to 0-100. Halves round up, and the sum
    is exact (floats are read as the nearest short fraction), so 39.5 is 40 whatever the float error."""
    fr = [p if isinstance(p, Fraction) else Fraction(p).limit_denominator(1_000_000) for p in percentiles]
    base = math.floor(100 * sum(fr) / len(fr) + Fraction(1, 2))
    return max(0, min(100, base - WASTE_PENALTY * waste_kinds))


def evaluate(measures: dict, pool: Sequence[Sequence], kind: str, running: bool = False) -> dict:
    """Score, label and reasons of one task against a baseline pool (13.10). Never raises.

    `pool` rows are the four ratios in RATIOS order, optionally with the task time in front (a row of 5)."""
    try:
        return _evaluate(measures, pool, kind, running)
    except Exception:
        return {"score": None, "label": None, "reasons": [], "kind": kind, "pool": 0}


def _ratios_of(row: Sequence) -> Sequence[Optional[float]]:
    return row[-len(RATIOS):] if len(row) > len(RATIOS) else row


def _evaluate(measures: dict, pool: Sequence[Sequence], kind: str, running: bool) -> dict:
    out = {"score": None, "label": None, "reasons": [], "kind": kind, "pool": len(pool)}
    waste = measures.get("waste") or []
    waste_reasons = [(WASTE_PENALTY + 0.1 * min(int(w.get("count") or 0), 9), i, w["text"]) for i, w in enumerate(waste) if w.get("text")]
    if running:
        out["label"] = IN_PROGRESS
        out["reasons"] = [t for _, _, t in sorted(waste_reasons, key=lambda r: (-r[0], r[1]))[:REASONS_MAX]]
        return out
    if int(measures.get("calls") or 0) < MIN_CALLS:
        return out  # too short to rate
    rows = [_ratios_of(r) for r in pool]
    percentiles: Dict[str, Fraction] = {}
    for i, name in enumerate(RATIOS):
        col = [r[i] for r in rows if r[i] is not None]
        if len(col) >= BASELINE_MIN:  # per-ratio minimum: a ratio with fewer values than this is left out
            p = percentile(_num(measures.get(name)), col, name in HIGHER_IS_BETTER)
            if p is not None:
                percentiles[name] = p
    if not percentiles:
        out["label"] = NOT_ENOUGH
        out["reasons"] = [t for _, _, t in sorted(waste_reasons, key=lambda r: (-r[0], r[1]))[:REASONS_MAX]]
        return out
    out["score"] = score_from(list(percentiles.values()), len({w["kind"] for w in waste}))
    out["label"] = label_for(out["score"])
    cands = list(waste_reasons)
    cands += _ratio_reasons(measures, rows, percentiles, kind, len(waste_reasons))
    out["reasons"] = [t for _, _, t in sorted(cands, key=lambda r: (-r[0], r[1]))[:REASONS_MAX]]
    return out


def _ratio_reasons(m: dict, rows: Sequence[Sequence], pct: Dict[str, Fraction], kind: str, offset: int) -> List[Tuple[float, int, str]]:
    usual = "main-agent" if kind == "main" else kind
    found: List[Tuple[float, int, str]] = []
    n = len(pct)
    for i, name in enumerate(RATIOS):
        p = pct.get(name)
        if p is None or p >= Fraction(1, 2):
            continue  # only ratios worse than the median of the baseline are called out
        impact = float(Fraction(1, 2) - p) * 100.0 / n
        mine = _num(m.get(name))
        med = median(r[i] for r in rows)
        if mine is None or med is None:
            continue
        text = None
        if name in ("tokens_per_call", "seconds_per_call") and med > 0 and round(mine / med, 9) >= MIN_RATIO_FACTOR:
            what = "tokens" if name == "tokens_per_call" else "time"
            text = f"{mine / med:.1f}× the {what} per action of your usual {usual} task"
        elif name == "failure_rate" and mine > med:
            retries, errors, calls = int(m.get("retries") or 0), int(m.get("errors") or 0), int(m.get("calls") or 0)
            if retries > 0:
                same = "command" if m.get("retry_tools") == ["Bash"] else "call"
                text = f"{_plural(retries, 'retry', 'retries')} of the same {same}"
            elif errors > 0:
                text = f"{errors} of {calls} calls failed"
        elif name == "cache_reuse" and round(med - mine, 9) >= MIN_CACHE_GAP:
            text = f"Cache reuse {mine * 100:.0f}% (usually {med * 100:.0f}%)"
        if text:
            found.append((impact, offset + i, text))
    return found

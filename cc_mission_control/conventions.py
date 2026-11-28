"""Agentic conventions review (SPEC 11.14): scan a project's CLAUDE.md files and skills against a rubric.

Everything here is a deterministic local checklist: no model calls, no network. The evaluator
(`evaluate_rules`, `assemble`) is pure. File discovery, the hygiene checks and the apply/undo
machinery do filesystem work and run in a worker thread (the server wraps calls in `asyncio.to_thread`).

Apply only ever appends text that this module produced itself (a rule's suggestion) to a
CLAUDE.md inside the project, after a preview, with a backup and an undo.
"""

from __future__ import annotations

import hashlib
import itertools
import json
import logging
import math
import os
import re
import secrets
import stat
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Collection, Optional, Sequence

from .projects import canonical, is_fs_root, same_path

log = logging.getLogger(__name__)

RUBRIC_PATH = Path(__file__).with_name("conventions_rubric.json")

SKIP_DIRS = frozenset({
    ".git", "node_modules", "vendor", "build", "dist", ".dart_tool", "Pods", ".venv", "venv",
    "__pycache__", "keys", ".worktrees",
})
MAX_DEPTH = 4
MAX_FILES = 300
MAX_FILE_BYTES = 512 * 1024
MAX_TOTAL_BYTES = 8 * 1024 * 1024
MAX_LINE_CHARS = 2000
MAX_ENTRIES = 2000  # directory entries read per folder
SCAN_BUDGET = 3.0  # seconds of matching per scan before it stops early
MAX_DIRS = 4000  # a bound on the walk itself, so a huge tree cannot stall the worker
MAX_SKIPPED_LISTED = 20
CACHE_SECONDS = 30.0
EVIDENCE_CHARS = 160
LARGE_CLAUDE_CHARS = 40_000
MAX_TARGET_BYTES = 1024 * 1024
O_BINARY = getattr(os, "O_BINARY", 0)  # Windows: no newline translation on os.read / os.write
MAX_SUGGESTION_CHARS = 8000
MAX_REGEX_CHARS = 300
MAX_RULES = 60
MAX_RUBRIC_BYTES = 1024 * 1024
MAX_HYGIENE_PER_KIND = 50
MAX_LINK_CHECKS = 300
KEEP_RECORDS = 20
CONTEXT_LINES = 5
CONTEXT_LINE_CHARS = 500
INHERITED_PATH = "~/.claude/CLAUDE.md"

CHANGED = "The file changed; preview again"
CHANGED_AFTER = "The file changed after the apply; undo it by hand"
BACKUP_NAME = re.compile(r"^[0-9a-f]{16}-\d{8}T\d{6}Z-\d+\.md$")
PARTIAL = "The scan did not finish; rescan first"
NOT_NEEDED = "This rule is already covered or marked not applicable"

KIND_ORDER = {"claude": 0, "local": 1, "agents": 2, "rule": 3, "skill": 4, "reference": 5, "agent": 6, "command": 7}


class ConventionError(Exception):
    """An error the server maps to an HTTP status. The message is safe to show: it never quotes file content."""

    def __init__(self, status: int, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.message = message


# ---------------------------------------------------------------- rubric


@dataclass(frozen=True)
class Rule:
    id: str
    title: str
    area: str
    weight: int
    why: str
    groups: tuple  # tuple of groups; a group is a tuple of compiled patterns, hit when any matches a line
    suggestion: str


@dataclass(frozen=True)
class Rubric:
    rules: tuple
    source: str = "builtin"  # "builtin" or "custom"
    warning: Optional[str] = None


class RubricError(Exception):
    """An invalid rubric. The message names the problem and never quotes the file's contents."""


_ID_RE = re.compile(r"^[A-Za-z0-9_.-]{1,64}$")


def _compile_group(patterns: Any, where: str, limit: int = MAX_REGEX_CHARS) -> tuple:
    if not isinstance(patterns, list) or not patterns:
        raise RubricError(f"{where} has an empty signal group")
    compiled = []
    for p in patterns:
        if not isinstance(p, str) or not p or len(p) > limit:
            raise RubricError(f"{where} has a signal that is not a valid string of at most {MAX_REGEX_CHARS} characters")
        try:
            compiled.append(re.compile(p, re.IGNORECASE))
        except (re.error, RecursionError, OverflowError):
            raise RubricError(f"{where} has an invalid regular expression") from None
    try:  # one alternation is faster to run than a list; the list is the fallback
        return (re.compile("|".join(f"(?:{c.pattern})" for c in compiled), re.IGNORECASE),)
    except (re.error, RecursionError, OverflowError):
        return tuple(compiled)


def _str_field(d: dict, name: str, limit: int, where: str, default: Optional[str] = None) -> str:
    value = d.get(name, default)
    if not isinstance(value, str) or (not value.strip() and default is None) or len(value) > limit:
        raise RubricError(f"{where} has an invalid {name}")
    return value.strip()


def _section(base_dir: Path, spec: Any, where: str) -> str:
    """The Markdown section under `heading` in `file` (relative to the rubric's folder), per SPEC 11.14."""
    fail = RubricError(f"suggest_from in {where} did not resolve")
    if not isinstance(spec, dict) or not isinstance(spec.get("file"), str) or not isinstance(spec.get("heading"), str):
        raise fail
    rel = spec["file"]
    if not rel or "\0" in rel or os.path.isabs(rel):
        raise fail
    try:
        base_real = os.path.realpath(base_dir)
        target = os.path.realpath(os.path.join(base_real, rel))
        # a filesystem-root base is accepted on purpose: everything under "/" is inside it (the old "//" prefix rejected all)
        if target != base_real and not target.startswith(_with_sep(base_real)):
            raise fail  # the template must sit under the rubric's own folder
        st = os.stat(target)
        if not stat.S_ISREG(st.st_mode) or st.st_size > MAX_RUBRIC_BYTES:
            raise fail
        with open(target, "rb") as fh:
            text = fh.read(MAX_RUBRIC_BYTES + 1).decode("utf-8")
    except (OSError, UnicodeDecodeError):
        raise fail from None
    wanted = re.sub(r"^#+\s*", "", spec["heading"].strip()).strip()
    if not wanted:
        raise fail
    out: list = []
    level = 0
    fenced = False
    for line in text.splitlines():
        if re.match(r"^\s*(```|~~~)", line):
            fenced = not fenced
        m = None if fenced else re.match(r"^(#{1,6})\s+(.*?)\s*#*\s*$", line)
        if not level:
            if m and m.group(2).strip() == wanted:
                level = len(m.group(1))
                out.append(line)
            continue
        if m and len(m.group(1)) <= level:
            break
        out.append(line)
    if not level:
        raise fail
    return "\n".join(out).rstrip()[:MAX_SUGGESTION_CHARS].rstrip()


def _build_rule(d: Any, index: int, base_dir: Optional[Path], redact: Optional[Callable[[str], str]]) -> Rule:
    where = f"rule {index}"
    if not isinstance(d, dict):
        raise RubricError(f"{where} is not an object")
    rid = d.get("id")
    if not isinstance(rid, str) or not _ID_RE.match(rid):
        raise RubricError(f"{where} has an invalid id")
    weight = d.get("weight", 1)
    if isinstance(weight, bool) or weight not in (1, 2):
        raise RubricError(f"{where} has an invalid weight")
    signals = d.get("signals")
    if not isinstance(signals, list) or not signals or len(signals) > 20:
        raise RubricError(f"{where} has invalid signals")
    groups = tuple(_compile_group(g, where, MAX_REGEX_CHARS if base_dir is not None else 10_000) for g in signals)
    if "suggest_from" in d:
        if base_dir is None:
            raise RubricError(f"suggest_from in {where} did not resolve")
        suggestion = _section(base_dir, d["suggest_from"], where)
    else:
        suggestion = d.get("suggestion", "")
        if not isinstance(suggestion, str) or len(suggestion) > MAX_SUGGESTION_CHARS:
            raise RubricError(f"{where} has an invalid suggestion")
        suggestion = suggestion.strip()
    if redact is not None and suggestion:
        suggestion = redact(suggestion).strip()  # what is shown is what is written: no secret can ride along
    return Rule(
        id=rid,
        title=_str_field(d, "title", 120, where),
        area=_str_field(d, "area", 60, where, "Custom") or "Custom",
        weight=weight,
        why=_str_field(d, "why", 400, where, ""),
        groups=groups,
        suggestion=suggestion,
    )


def load_builtin_rules() -> tuple:
    data = json.loads(RUBRIC_PATH.read_text(encoding="utf-8"))
    return tuple(_build_rule(r, i + 1, None, None) for i, r in enumerate(data["rules"]))


def _load_custom(path: Path, builtin: tuple, redact: Optional[Callable[[str], str]]) -> tuple:
    try:
        st = os.stat(path)
        if not stat.S_ISREG(st.st_mode):
            raise RubricError("the rubric file cannot be read")
        if st.st_size > MAX_RUBRIC_BYTES:
            raise RubricError("the rubric file is too large")
        with open(path, "rb") as fh:
            raw = fh.read(MAX_RUBRIC_BYTES + 1)
    except OSError:
        raise RubricError("the rubric file cannot be read") from None
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        raise RubricError("the rubric file is not valid JSON") from None
    if not isinstance(data, dict) or data.get("version") != 1 or isinstance(data.get("version"), bool):
        raise RubricError("the rubric file must be a JSON object with version 1")
    extends = data.get("extends")
    if extends not in ("builtin", None):
        raise RubricError("the rubric file has an invalid extends value")
    disable = data.get("disable", [])
    if not isinstance(disable, list) or not all(isinstance(x, str) for x in disable):
        raise RubricError("the rubric file has an invalid disable list")
    rules = data.get("rules", [])
    if not isinstance(rules, list):
        raise RubricError("the rubric file has an invalid rules list")
    base_dir = Path(path).parent
    custom = [_build_rule(r, i + 1, base_dir, redact) for i, r in enumerate(rules)]
    ids = [r.id for r in custom]
    if len(set(ids)) != len(ids):
        raise RubricError("the rubric file repeats a rule id")
    merged: list = []
    if extends == "builtin":
        off = set(disable)
        replacements = {r.id: r for r in custom}
        for b in builtin:
            if b.id in off:
                continue
            merged.append(replacements.pop(b.id, b))
        merged.extend(r for r in custom if r.id in replacements)
    else:
        merged = list(custom)
    if not merged:
        raise RubricError("the rubric has no rules")
    if len(merged) > MAX_RULES:
        raise RubricError(f"the rubric has more than {MAX_RULES} rules")
    return tuple(merged)


def load_rubric(path: "Path | str | None", redact: Optional[Callable[[str], str]] = None) -> Rubric:
    """The built-in rubric, or the user's (`CCMC_CONVENTION_RUBRIC`). Any problem falls back to the built-in one with a warning."""
    builtin = load_builtin_rules()
    if not path:
        return Rubric(builtin)
    try:
        return Rubric(_load_custom(Path(path), builtin, redact), "custom")
    except RubricError as exc:
        log.warning("convention rubric ignored: %s", exc)
        return Rubric(builtin, "builtin", str(exc))


# ---------------------------------------------------------------- evaluator (pure)


@dataclass
class Source:
    """One convention file. Only `path` and `lines` matter to the evaluator."""

    path: str  # relative to the project root, or INHERITED_PATH
    lines: list = field(default_factory=list)  # each cut at MAX_LINE_CHARS, for matching
    kind: str = "claude"
    inherited: bool = False
    skipped: Optional[str] = None
    abs: str = ""
    chars: int = 0
    full: dict = field(default_factory=dict)  # line number -> the uncut text, for lines that were cut (credential check)

    def public(self) -> dict:
        return {"path": self.path, "kind": self.kind, "lines": len(self.lines),
                "inherited": self.inherited, "skipped": self.skipped}


@dataclass(frozen=True)
class RawResult:
    status: str  # covered | partial | missing | unknown (the scan stopped early)
    evidence: tuple  # of {path, line, excerpt}


def evaluate_rules(rules: Sequence[Rule], sources: Sequence[Any], redact: Optional[Callable[[str], str]] = None,
                   stop: Optional[Callable[[], bool]] = None) -> list:
    """Hit each rule's signal groups against the sources' lines. Pure: no I/O, no clock.

    `stop` (optional) is polled between sources and every 256 lines; when it returns True the scan ends and
    the groups hit so far decide the statuses.
    """
    redact = redact or (lambda s: s)
    hits: dict = {}  # (rule index, group index) -> (path, line number, text)
    pending = [(ri, gi, group) for ri, rule in enumerate(rules) for gi, group in enumerate(rule.groups)]
    halted = False
    for src in sources:
        if not pending or halted:
            break
        for n, line in enumerate(src.lines, 1):
            if not pending:
                break
            if stop is not None and n % 256 == 1 and stop():
                halted = True
                break
            still = []
            for item in pending:
                if any(p.search(line) for p in item[2]):
                    hits[(item[0], item[1])] = (src.path, n, line)
                else:
                    still.append(item)
            pending = still
    out = []
    for ri, rule in enumerate(rules):
        found = [hits[(ri, gi)] for gi in range(len(rule.groups)) if (ri, gi) in hits]
        if len(found) == len(rule.groups):
            status = "covered"
        elif halted:
            status = "unknown"  # the scan stopped before this rule was decided
        else:
            status = "partial" if found else "missing"
        seen, evidence = set(), []
        for path, n, text in found:
            if (path, n) in seen:
                continue
            seen.add((path, n))
            evidence.append({"path": path, "line": n, "excerpt": redact(text.strip())[:EVIDENCE_CHARS]})  # redact, then cut
        out.append(RawResult(status, tuple(evidence)))
    return out


def grade_for(score: Optional[int]) -> Optional[str]:
    if score is None:
        return None
    return "Strong" if score >= 80 else "Partial" if score >= 50 else "Weak"


@dataclass(frozen=True)
class Evaluation:
    statuses: list  # per rule: covered | partial | missing | n/a
    evidence: list  # per rule: list of {path, line, excerpt}
    score: Optional[int]
    grade: Optional[str]


def assemble(rules: Sequence[Rule], raw: Sequence[RawResult], na: Collection[str] = ()) -> Evaluation:
    """Statuses (with the user's n/a marks), score and grade from raw results. Pure."""
    statuses, evidence = [], []
    num = den = 0.0
    undecided = False
    for rule, res in zip(rules, raw):
        if rule.id in na:
            statuses.append("n/a")
        elif res.status == "unknown":
            statuses.append("unknown")
            undecided = True
        else:
            statuses.append(res.status)
            den += rule.weight
            num += rule.weight * (1 if res.status == "covered" else 0.5 if res.status == "partial" else 0)
        evidence.append([dict(e) for e in res.evidence])
    score = None if den == 0 or undecided else int(math.floor(100 * num / den + 0.5))
    return Evaluation(statuses, evidence, score, grade_for(score))


def evaluate(rules: Sequence[Rule], sources: Sequence[Any], na: Collection[str] = (),
             redact: Optional[Callable[[str], str]] = None) -> Evaluation:
    """Sources in, statuses, evidence, score and grade out."""
    return assemble(rules, evaluate_rules(rules, sources, redact), na)


# ---------------------------------------------------------------- discovery


def _with_sep(root: str, pathmod=os.path) -> str:
    """`root` ending in exactly one separator; a root such as `/` or `C:\\` already has it."""
    return root if root.endswith(pathmod.sep) else root + pathmod.sep


def _inside(real: str, root_real: str, pathmod=os.path, same: Callable[[str, str], bool] = same_path) -> bool:
    """True when `real` is the root or lies under it. Compared by identity, so case differences do not matter."""
    sep = pathmod.sep
    base = _with_sep(root_real, pathmod)
    if real == root_real or real.startswith(base):
        return True
    rc, bc = real.casefold(), base.casefold()
    if rc != root_real.casefold() and not rc.startswith(bc):
        return False
    # rebuild the root's prefix from `real`, keeping the drive / share anchor (a bare "C:" is drive-relative)
    _, rrest = pathmod.splitdrive(root_real)
    drive, rest = pathmod.splitdrive(real)
    depth = len([c for c in rrest.split(sep) if c])
    comps = [c for c in rest.split(sep) if c][:depth]
    return same(drive + sep + sep.join(comps), root_real)


def _classify(parts: tuple) -> Optional[str]:
    name = parts[-1]
    if name == "CLAUDE.md":
        return "claude"
    if name == "CLAUDE.local.md":
        return "local"
    if name == "AGENTS.md":
        return "agents"
    if not name.endswith(".md"):
        return None
    dirs = parts[:-1]
    at = [i for i, p in enumerate(dirs) if p == ".claude"]
    if not at:
        return None
    sub = parts[at[-1] + 1:]
    if sub[0] == "rules" and len(sub) >= 2:
        return "rule"
    if sub[0] == "skills":
        if len(sub) == 3 and sub[2] == "SKILL.md":
            return "skill"
        if len(sub) == 4 and sub[2] == "reference":
            return "reference"
    if sub[0] == "agents" and len(sub) == 2:
        return "agent"
    if sub[0] == "commands" and len(sub) == 2:
        return "command"
    return None


@dataclass(frozen=True)
class Cand:
    abs: str
    rel: str
    kind: str
    inherited: bool = False


def _discover(root_real: str, home_real: str) -> tuple:
    """Candidate convention files under the root (SPEC 11.14 sources), most important first.

    Returns (candidates, notes): a note names a folder whose entries were cut at MAX_ENTRIES.
    """
    found: list = []
    notes: list = []
    if not os.path.isdir(root_real):
        return found, notes
    only_claude = is_fs_root(root_real) or same_path(root_real, home_real)  # never walk a whole home folder or "/"
    stack = [(root_real, (), 0)]
    visited = 0
    while stack:
        folder, parts, depth = stack.pop()
        visited += 1
        if visited > MAX_DIRS:
            break
        try:
            with os.scandir(folder) as it:
                entries = list(itertools.islice(it, MAX_ENTRIES + 1))
        except OSError:
            continue
        if len(entries) > MAX_ENTRIES:
            notes.append("/".join(parts) or ".")
            entries = entries[:MAX_ENTRIES]
        entries.sort(key=lambda e: e.name)
        try:
            if parts and any(e.name == ".git" and e.is_file(follow_symlinks=False) for e in entries):
                continue  # a linked worktree: its files duplicate the main checkout's
        except OSError:
            continue
        for e in entries:
            name = e.name
            try:
                is_dir = e.is_dir(follow_symlinks=False)
                is_link = e.is_symlink()
                is_file = e.is_file(follow_symlinks=False)
            except OSError:
                continue
            if is_dir:
                if name in SKIP_DIRS or name.lower().endswith("worktrees"):
                    continue
                if only_claude and depth == 0 and name != ".claude":
                    continue
                if depth < MAX_DEPTH:
                    stack.append((e.path, parts + (name,), depth + 1))
            elif is_file or is_link:
                kind = _classify(parts + (name,))
                if kind is None:
                    continue
                if is_link:  # symlinked files are read only when they really live inside the root
                    real = os.path.realpath(e.path)
                    if not os.path.isfile(real) or not _inside(real, root_real):
                        continue
                    if "keys" in os.path.relpath(real, root_real).split(os.sep):
                        continue
                found.append(Cand(e.path, "/".join(parts + (name,)), kind))
    found.sort(key=lambda c: (KIND_ORDER[c.kind], c.rel.count("/"), c.rel))
    return found, sorted(notes)


def _inherited(home_real: str, found: Sequence[Cand]) -> Optional[Cand]:
    path = os.path.join(home_real, ".claude", "CLAUDE.md")
    try:
        real = os.path.realpath(path)
        if not os.path.isfile(real) or not _inside(real, home_real):
            return None
    except OSError:
        return None
    if any(same_path(c.abs, path) for c in found):
        return None  # the root is the home folder: it is already listed as a normal source
    return Cand(path, INHERITED_PATH, "claude", True)


def _read_regular(path: str, root_real: str, limit: int) -> tuple:
    """(data, too_big). Reads a regular file that really lives inside the root, or returns (None, False).

    The real path is opened with O_NOFOLLOW and O_NONBLOCK and checked with fstat, so a symlink or a FIFO swapped
    in after discovery is skipped rather than followed or waited on.
    """
    try:
        real = os.path.realpath(path)
        if not _inside(real, root_real):
            return None, False
        fd = os.open(real, os.O_RDONLY | O_BINARY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except OSError:
        return None, False
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            return None, False
        if st.st_size > limit:
            return None, True
        chunks, total = [], 0
        while total <= limit:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
            total += len(chunk)
        data = b"".join(chunks)
        return (None, True) if len(data) > limit else (data, False)
    except OSError:
        return None, False
    finally:
        os.close(fd)


def _settings_counts(root_real: str) -> dict:
    """Only three counts are kept from settings files: hook entries, deny rules and ask rules."""
    hooks = deny = ask = 0
    for name in ("settings.json", "settings.local.json"):
        raw, _ = _read_regular(os.path.join(root_real, ".claude", name), root_real, MAX_FILE_BYTES)
        if raw is None:
            continue
        try:
            data = json.loads(raw.decode("utf-8"))
        except (ValueError, UnicodeDecodeError, RecursionError):
            continue
        if not isinstance(data, dict):
            continue
        h = data.get("hooks")
        if isinstance(h, dict):
            for groups in h.values():
                for group in groups if isinstance(groups, list) else []:
                    inner = group.get("hooks") if isinstance(group, dict) else None
                    hooks += len(inner) if isinstance(inner, list) else 0
        perms = data.get("permissions")
        if isinstance(perms, dict):
            d, a = perms.get("deny"), perms.get("ask")
            deny += len(d) if isinstance(d, list) else 0
            ask += len(a) if isinstance(a, list) else 0
    return {"hooks": hooks, "deny": deny, "ask": ask}


def _stat_key(path: str, rel: str) -> tuple:
    try:
        st = os.stat(path)
        return (rel, st.st_mtime_ns, st.st_size)
    except OSError:
        return (rel, 0, -1)


def _fingerprint(cands: Sequence[Cand], root_real: str, notes: Sequence[str] = ()) -> tuple:
    keys = [_stat_key(c.abs, c.rel) for c in cands]
    for name in ("settings.json", "settings.local.json"):
        keys.append(_stat_key(os.path.join(root_real, ".claude", name), ".claude/" + name))
    return tuple(keys) + tuple(("note", n) for n in notes)


def _split_lines(text: str, cut: bool = True) -> list:
    lines = text.replace("\r\n", "\n").replace("\r", "\n").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [ln[:MAX_LINE_CHARS] for ln in lines] if cut else lines


def _read_sources(cands: Sequence[Cand], root_real: str, home_real: str) -> list:
    """Read the candidates within the caps; files that are not regular files inside their root are left out."""
    sources: list = []
    total = 0
    for i, c in enumerate(cands):
        src = Source(path=c.rel, kind=c.kind, inherited=c.inherited, abs=c.abs)
        if i >= MAX_FILES:
            if i - MAX_FILES >= MAX_SKIPPED_LISTED:
                break
            src.skipped = "file limit"
            sources.append(src)
            continue
        data, too_big = _read_regular(c.abs, home_real if c.inherited else root_real, MAX_FILE_BYTES)
        if too_big:
            src.skipped = "too large"
        elif data is None:
            continue
        elif total + len(data) > MAX_TOTAL_BYTES:
            src.skipped = "size limit"
        else:
            total += len(data)
            text = data.decode("utf-8", "replace")
            src.chars = len(text)
            raw = _split_lines(text, cut=False)
            src.lines = [ln[:MAX_LINE_CHARS] for ln in raw]
            src.full = {n: ln for n, ln in enumerate(raw, 1) if len(ln) > MAX_LINE_CHARS}
            if len(src.full) > 50:
                src.full = dict(list(src.full.items())[:50])
        sources.append(src)
    return sources


# ---------------------------------------------------------------- hygiene

_TRIGGER = re.compile(r"use when|use at|use before|use for|whenever|trigger", re.IGNORECASE)
_MD_LINK = re.compile(r"\[[^\]\n]*\]\(\s*<?([^)\s>]+)>?(?:\s+[\"'][^)]*[\"'])?\s*\)")
_TICK_MD = re.compile(r"`([^`\s]+\.md)`")
_SCHEME = re.compile(r"^[A-Za-z][A-Za-z0-9+.-]*:")
_FRONT_KEY = re.compile(r"^([A-Za-z_][A-Za-z0-9_-]*):\s*(.*)$")


def _frontmatter(lines: Sequence[str]) -> Optional[dict]:
    """Key -> (value, line number) of a leading `---` block. A small YAML subset is enough for name and description."""
    if not lines or lines[0].strip() != "---":
        return None
    end = next((i for i in range(1, len(lines)) if lines[i].strip() == "---"), None)
    if end is None:
        return None
    out: dict = {}
    i = 1
    while i < end:
        m = _FRONT_KEY.match(lines[i])
        if not m:
            i += 1
            continue
        key, value, at = m.group(1).lower(), m.group(2).strip(), i + 1
        i += 1
        while i < end and (lines[i].startswith((" ", "\t")) or not lines[i].strip()):
            value += " " + lines[i].strip()
            i += 1
        value = value.strip()
        if value[:1] in (">", "|"):
            value = value.lstrip(">|+-").strip()
        out[key] = (value.strip("\"'").strip(), at)
    return out


def _relative_target(raw: str) -> Optional[str]:
    t = raw.strip().split("#", 1)[0].split("?", 1)[0]
    if not t or _SCHEME.match(t) or t[0] in "/~#$@%" or "\0" in t or any(ch in t for ch in "*{}<>|"):
        return None
    return t


def _link_exists(folder: str, root_real: str, target: str) -> bool:
    """A reference counts when it resolves next to the file or in any folder above it, up to the root (inclusive)."""
    def found(candidate: str) -> bool:
        return os.path.exists(candidate) and _inside(os.path.realpath(candidate), root_real)  # outside the root never counts

    cur = folder
    for _ in range(MAX_DEPTH + 8):
        if found(os.path.join(cur, target)):
            return True
        parent = os.path.dirname(cur)
        if parent == cur or same_path(cur, root_real) or not _inside(parent, root_real):
            break
        cur = parent
    return found(os.path.join(root_real, target))


def _hygiene(sources: Sequence[Source], root_real: str, redact: Callable[[str], str],
             notes: Sequence[str] = (), stop: Optional[Callable[[], bool]] = None) -> list:
    out: list = []
    counts: dict = {}

    def add(kind: str, path: str, line: Optional[int], message: str) -> None:
        if counts.get(kind, 0) < MAX_HYGIENE_PER_KIND:
            counts[kind] = counts.get(kind, 0) + 1
            out.append({"kind": kind, "path": path, "line": line, "message": message})

    own = [s for s in sources if not s.inherited]
    if not any(s.path in ("CLAUDE.md", ".claude/CLAUDE.md") for s in own):
        add("no_claude_md", "CLAUDE.md", None, "The project has no CLAUDE.md")
    for s in own:
        if s.skipped:
            add("skipped_file", s.path, None, f"File skipped: {s.skipped}")
    for n in notes:
        add("skipped_file", n, None, f"Folder has more than {MAX_ENTRIES:,} entries; the rest were skipped")
    for s in own:
        if s.kind == "claude" and not s.skipped and s.chars > LARGE_CLAUDE_CHARS:
            add("large_file", s.path, None, f"CLAUDE.md is over {LARGE_CLAUDE_CHARS:,} characters")
    for s in own:
        if s.kind != "skill" or s.skipped:
            continue
        fm = _frontmatter(s.lines)
        if fm is None:
            add("skill_frontmatter", s.path, 1, "Skill has no frontmatter")
            continue
        if not fm.get("name", ("", 0))[0]:
            add("skill_frontmatter", s.path, fm.get("name", ("", 1))[1], "Skill frontmatter has no name")
        desc = fm.get("description", ("", 0))
        if not desc[0]:
            add("skill_frontmatter", s.path, desc[1] or 1, "Skill frontmatter has no description")
        elif not _TRIGGER.search(desc[0]):
            add("skill_frontmatter", s.path, desc[1], "Skill description does not say when to use it")
    checks = 0
    halted = False
    for s in own:
        if s.skipped:
            continue
        if halted or (stop is not None and stop()):
            halted = True
            break
        folder = os.path.dirname(s.abs)
        fenced = False
        for n, line in enumerate(s.lines, 1):
            if stop is not None and n % 256 == 0 and stop():
                halted = True
                break
            whole = s.full.get(n, line)  # redact the whole line, never a cut one
            if redact(whole) != whole:
                add("credential", s.path, n, "This line looks like it contains a credential")
            if re.match(r"^\s*(```|~~~)", line):
                fenced = not fenced
                continue
            if fenced:
                continue
            for raw in [*_MD_LINK.findall(line), *_TICK_MD.findall(line)]:
                target = _relative_target(raw)
                if target is None or checks >= MAX_LINK_CHECKS:
                    continue
                checks += 1
                if not _link_exists(folder, root_real, target):
                    add("broken_link", s.path, n, "Link target not found: " + redact(target)[:80])
    if halted:
        add("scan_stopped", "", None, "The scan stopped early (time budget); results may be incomplete")
    order = ["no_claude_md", "large_file", "skill_frontmatter", "broken_link", "credential", "skipped_file", "scan_stopped"]
    out.sort(key=lambda h: order.index(h["kind"]))  # stable: scan order is kept inside a kind
    return out


# ---------------------------------------------------------------- scan, targets, persistence


@dataclass
class Scan:
    sources: list
    settings: dict
    hygiene: list
    targets: list  # [{target, path (absolute), exists}]
    raw: list
    fingerprint: tuple
    at: float
    partial: bool = False  # the scan ran out of time: statuses may be `unknown`, score and grade are null


def _sha(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _before_hash(data: Optional[bytes]) -> str:
    """Hash of a target's current bytes; an absent file is told apart from an empty one."""
    return "absent" if data is None else _sha(data)


def _check_target(root_real: str, path: str) -> tuple:
    """(usable, exists) for a would-be apply target: no symlink, a regular file under 1 MB, inside the root."""
    try:
        st = os.lstat(path)
    except FileNotFoundError:
        return (os.path.isdir(root_real) and not os.path.islink(os.path.dirname(path)), False)
    except OSError:
        return (False, False)
    if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or st.st_size >= MAX_TARGET_BYTES:
        return (False, True)
    return (_inside(os.path.realpath(path), root_real), True)


def _write_private(path: Path, data: bytes, mode: int = 0o600) -> None:
    """temp + fsync + os.replace, so a crash never leaves half a state file."""
    tmp = path.with_name(f".{path.name}.{secrets.token_hex(4)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
    try:
        with os.fdopen(fd, "wb") as fh:
            fh.write(data)
            fh.flush()
            os.fsync(fh.fileno())
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


def _read_target(path: str) -> tuple:
    """(bytes, mode) of an existing target, or (None, None) when it is absent. Never follows a symlink."""
    try:
        fd = os.open(path, os.O_RDONLY | O_BINARY | getattr(os, "O_NOFOLLOW", 0) | getattr(os, "O_NONBLOCK", 0))
    except FileNotFoundError:
        return None, None
    except OSError:
        raise ConventionError(409, "That file cannot be changed") from None
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode) or st.st_size >= MAX_TARGET_BYTES:
            raise ConventionError(409, "That file cannot be changed")
        chunks = []
        while True:
            chunk = os.read(fd, 65536)
            if not chunk:
                break
            chunks.append(chunk)
        return b"".join(chunks), st.st_mode & 0o7777
    finally:
        os.close(fd)


def _unlink_tmp(tmp: str) -> None:
    """Best-effort removal of the temp file; a read-only one (Windows) is made writable and retried."""
    try:
        os.unlink(tmp)
    except PermissionError:
        try:
            os.chmod(tmp, stat.S_IWRITE)
            os.unlink(tmp)
        except OSError:
            pass
    except OSError:
        pass


def _replace_file(path: str, data: bytes, mode: int, windows: Optional[bool] = None) -> None:
    """Write beside the target, fsync, then os.replace; the mode is set explicitly.

    On Windows a read-only target cannot be replaced (WinError 5), so it is refused up front, before any temp file exists.
    """
    if windows is None:
        windows = os.name == "nt"
    if windows and os.path.exists(path) and not os.access(path, os.W_OK):
        raise ConventionError(409, "That file is read-only; clear the read-only attribute and try again")
    folder = os.path.dirname(path)
    tmp = os.path.join(folder, f".{os.path.basename(path)}.ccmc-{secrets.token_hex(6)}.tmp")
    fd = os.open(tmp, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY | getattr(os, "O_NOFOLLOW", 0), 0o600)
    try:
        try:
            view = memoryview(data)
            while view:
                view = view[os.write(fd, view):]
            if hasattr(os, "fchmod"):  # absent on Windows before Python 3.13
                os.fchmod(fd, mode)
            os.fsync(fd)
        finally:
            os.close(fd)
        if not hasattr(os, "fchmod"):
            os.chmod(tmp, mode)  # by name, after the close; Windows only honours the read-only bit
        os.replace(tmp, path)
    except BaseException:
        _unlink_tmp(tmp)
        raise
    try:
        dfd = os.open(folder, os.O_RDONLY)
        try:
            os.fsync(dfd)
        finally:
            os.close(dfd)
    except OSError:
        pass


class Conventions:
    """Scans, report building, n/a marks, and the apply/undo of suggested text. Thread-safe.

    `_lock` guards the small shared state (cache, n/a marks, apply records) and is never held during a scan.
    `_write_lock` serialises the operations that change files or state (apply, undo, n/a).
    """

    def __init__(self, data_dir: "Path | str", rubric: Rubric, redact: Callable[[str], str],
                 home: "Path | str | None" = None, clock: Callable[[], float] = time.time) -> None:
        self.dir = Path(data_dir) / "conventions"
        self.rubric = rubric
        self.redact = redact
        self.home = canonical(os.fspath(home) if home is not None else os.path.expanduser("~"))
        self.clock = clock
        self._lock = threading.RLock()
        self._write_lock = threading.RLock()
        self._cache: dict = {}
        self._state: Optional[dict] = None
        self._records: Optional[dict] = None
        self._no_prune = False

    # --- persistence

    def _load_records(self) -> dict:
        """applies.json, reconciled.

        Corrupt content (not JSON, or not an object) is set aside as `applies.json.bad-<stamp>`. Any other read error
        (permissions, a transient fault, a directory in the way) leaves the file alone. Either way this process starts
        with no records and never prunes backups (an in-memory flag, nothing persistent).
        """
        path = self.dir / "applies.json"
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if not isinstance(data, dict):
                raise ValueError("not an object")
        except FileNotFoundError:
            return {}
        except ValueError:  # includes bad JSON and bad UTF-8
            stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
            log.warning("conventions: applies.json is corrupt; setting it aside and keeping every backup")
            self._no_prune = True
            try:
                os.replace(path, self.dir / f"applies.json.bad-{stamp}")
            except OSError:
                log.warning("conventions: could not set applies.json aside", exc_info=True)
            return {}
        except OSError:
            log.warning("conventions: applies.json could not be read; leaving it alone and keeping every backup", exc_info=True)
            self._no_prune = True
            return {}
        changed = False
        for key, recs in list(data.items()):
            if not isinstance(recs, list):
                continue
            keep = [r for r in recs if not (isinstance(r, dict) and not r.get("undone") and self._phantom(r))]
            if len(keep) != len(recs):
                data[key] = keep
                changed = True
        if changed:
            try:
                _write_private(path, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))
            except OSError:
                log.warning("conventions: could not save reconciled records", exc_info=True)
        return data

    @staticmethod
    def _phantom(rec: dict) -> bool:
        """A record whose apply never reached the file (saved, then the replace did not happen)."""
        path = rec.get("path")
        if not isinstance(path, str) or not os.path.isabs(path):
            return False
        try:
            data, _ = _read_target(path)
        except ConventionError:
            return False
        if rec.get("existed"):
            return data is not None and _sha(data) == rec.get("before_sha256")
        parent = os.path.dirname(path)  # an absent file only proves "never applied" if its folder is there to look in
        return data is None and os.path.isdir(parent) and os.access(parent, os.R_OK | os.X_OK)

    def _load(self, name: str) -> dict:
        try:
            data = json.loads((self.dir / name).read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _save(self, name: str, data: dict) -> None:
        self.dir.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        _write_private(self.dir / name, json.dumps(data, indent=1, sort_keys=True).encode("utf-8"))

    def _na(self, key: str) -> set:
        with self._lock:
            if self._state is None:
                self._state = self._load("state.json")
            marks = self._state.get("na", {}).get(key, [])
            return {m for m in marks if isinstance(m, str)}

    def _recs(self, key: str) -> list:
        with self._lock:
            if self._records is None:
                self._records = self._load_records()
            lst = self._records.get(key, [])
            return lst if isinstance(lst, list) else []

    # --- scanning

    def _unsafe_root(self, root_real: str) -> bool:
        return is_fs_root(root_real) or same_path(root_real, self.home)

    def _scan(self, root: str, force: bool = False) -> Scan:
        root_real = canonical(root)
        cands, notes = _discover(root_real, self.home)
        inh = _inherited(self.home, cands)
        all_c = cands + ([inh] if inh else [])
        fp = _fingerprint(all_c, root_real, notes)
        now = self.clock()
        with self._lock:
            hit = self._cache.get(root_real)
        if not force and hit is not None and hit.fingerprint == fp and 0 <= now - hit.at < CACHE_SECONDS:
            return hit
        deadline = time.monotonic() + SCAN_BUDGET
        stopped = []

        def stop() -> bool:
            if time.monotonic() >= deadline:
                stopped.append(True)
                return True
            return False

        sources = _read_sources(all_c, root_real, self.home)  # the slow part runs outside any lock
        raw = evaluate_rules(self.rubric.rules, [s for s in sources if not s.skipped], self.redact, stop)
        hyg = _hygiene(sources, root_real, self.redact, notes, stop)
        if stopped and not any(h["kind"] == "scan_stopped" for h in hyg):
            hyg.append({"kind": "scan_stopped", "path": "", "line": None,
                        "message": "The scan stopped early (time budget); results may be incomplete"})
        scan = Scan(
            sources=sources,
            settings=_settings_counts(root_real),
            hygiene=hyg,
            targets=self._targets(root_real, sources),
            raw=raw,
            fingerprint=fp,
            at=now,
            partial=bool(stopped),
        )
        with self._lock:
            self._cache[root_real] = scan
        return scan

    def _targets(self, root_real: str, sources: Sequence[Source]) -> list:
        if self._unsafe_root(root_real) or not os.path.isdir(root_real):
            return []
        out = []
        root_file = os.path.join(root_real, "CLAUDE.md")
        dot_file = os.path.join(root_real, ".claude", "CLAUDE.md")
        ok, exists = _check_target(root_real, root_file)
        dot_ok, dot_exists = _check_target(root_real, dot_file)
        dot_is_the_file = dot_ok and dot_exists and not exists  # the only CLAUDE.md: do not create a second one
        if ok and not dot_is_the_file:
            out.append({"target": "root", "path": root_file, "exists": exists})
        for s in sources:
            if s.kind != "claude" or s.inherited or s.skipped or s.path == "CLAUDE.md":
                continue
            p = os.path.join(root_real, *s.path.split("/"))
            ok, exists = _check_target(root_real, p)
            if ok and exists:
                out.append({"target": s.path, "path": p, "exists": True})
        return out

    # --- reports

    def _undoable(self, key: str) -> list:
        """The latest not-yet-undone apply per target, newest first."""
        seen, out = set(), []
        for rec in reversed(self._recs(key)):
            if rec.get("undone") or rec.get("target") in seen:
                continue
            seen.add(rec.get("target"))
            out.append({"target": rec["target"], "path": rec["path"], "applied_at": rec["applied_at"]})
        return out

    def _report(self, key: str, name: str, root: str, scan: Scan) -> dict:
        rules = self.rubric.rules
        ev = assemble(rules, scan.raw, self._na(key))
        score, grade = (None, None) if scan.partial else (ev.score, ev.grade)
        return {
            "key": key,
            "name": name,
            "root": root,
            "scanned_at": scan.at,
            "partial": scan.partial,
            "score": score,
            "grade": grade,
            "sources": [s.public() for s in scan.sources],
            "settings": dict(scan.settings),
            "rules": [
                {
                    "id": r.id, "title": r.title, "area": r.area, "weight": r.weight,
                    "status": ev.statuses[i], "evidence": ev.evidence[i], "why": r.why,
                    "suggestion": r.suggestion,
                    "can_apply": (not scan.partial and ev.statuses[i] in ("missing", "partial")
                                  and bool(r.suggestion) and bool(scan.targets)),
                }
                for i, r in enumerate(rules)
            ],
            "hygiene": [dict(h) for h in scan.hygiene],
            "targets": [dict(t) for t in scan.targets],
            "undo": self._undoable(key),
        }

    def report(self, key: str, name: str, root: str, rescan: bool = False) -> dict:
        return self._report(key, name, root, self._scan(root, force=rescan))

    def summary(self, key: str, root: str) -> dict:
        """The list-view facts: score, grade, scan time and the number of missing core rules."""
        scan = self._scan(root)
        ev = assemble(self.rubric.rules, scan.raw, self._na(key))
        missing = sum(1 for r, s in zip(self.rubric.rules, ev.statuses) if r.weight == 2 and s == "missing")
        if scan.partial or ev.score is None:
            return {"score": None, "grade": None, "scanned_at": scan.at, "missing_core": None, "partial": scan.partial}
        return {"score": ev.score, "grade": ev.grade, "scanned_at": scan.at, "missing_core": missing, "partial": False}

    def set_na(self, key: str, name: str, root: str, rule_id: str, na: bool) -> dict:
        with self._write_lock:
            if rule_id not in {r.id for r in self.rubric.rules}:
                raise ConventionError(404, "not found")
            with self._lock:
                marks = self._na(key)
                marks.add(rule_id) if na else marks.discard(rule_id)
                state = self._state if self._state is not None else {}
                state.setdefault("na", {})[key] = sorted(marks)
                self._state = state
                self._save("state.json", state)
            return self._report(key, name, root, self._scan(root))

    # --- apply and undo

    def _rule(self, rule_id: str) -> Rule:
        for r in self.rubric.rules:
            if r.id == rule_id:
                return r
        raise ConventionError(404, "not found")

    def _status(self, key: str, scan: Scan, rule: Rule) -> str:
        ev = assemble(self.rubric.rules, scan.raw, self._na(key))
        return ev.statuses[[r.id for r in self.rubric.rules].index(rule.id)]

    def _prepare(self, key: str, root: str, rule_id: str, target: str, fresh: bool) -> tuple:
        root_real = canonical(root)
        if self._unsafe_root(root_real) or not os.path.isdir(root_real):
            raise ConventionError(409, "This project folder cannot be changed")
        rule = self._rule(rule_id)
        if not rule.suggestion:
            raise ConventionError(409, "This rule has no suggested text")
        scan = self._scan(root, force=fresh)
        if scan.partial:
            raise ConventionError(409, PARTIAL)
        tgt = next((t for t in scan.targets if t["target"] == target), None)
        if tgt is None:
            raise ConventionError(400, "Choose a file from the list")
        return root_real, rule, scan, tgt, rule.suggestion.rstrip()

    def preview(self, key: str, root: str, rule_id: str, target: str) -> dict:
        _, rule, scan, tgt, text = self._prepare(key, root, rule_id, target, fresh=False)
        if self._status(key, scan, rule) not in ("missing", "partial"):
            raise ConventionError(409, NOT_NEEDED)
        data, _ = _read_target(tgt["path"])
        tail = _split_lines(data.decode("utf-8", "replace"), cut=False)[-CONTEXT_LINES:] if data else ["# CLAUDE.md"]
        return {
            "target": tgt["target"],
            "path": tgt["path"],
            "exists": data is not None,
            "before_sha256": _before_hash(data),
            "text": text,
            "text_sha256": _sha(text.encode("utf-8")),
            "context": "\n".join(self.redact(ln)[:CONTEXT_LINE_CHARS] for ln in tail),  # redact the whole line, then cut
        }

    def _backup(self, key: str, data: Optional[bytes]) -> str:
        folder = self.dir / "backups"
        folder.mkdir(parents=True, exist_ok=True)
        os.chmod(self.dir, 0o700)
        os.chmod(folder, 0o700)
        stamp = time.strftime("%Y%m%dT%H%M%SZ", time.gmtime(self.clock()))
        body = data if data is not None else b"# absent: the file did not exist before this apply\n"
        for n in range(1, 1000):
            dest = folder / f"{key}-{stamp}-{n}.md"
            try:
                fd = os.open(dest, os.O_WRONLY | os.O_CREAT | os.O_EXCL | O_BINARY, 0o600)
            except FileExistsError:
                continue
            with os.fdopen(fd, "wb") as fh:
                fh.write(body)
                fh.flush()
                os.fsync(fh.fileno())
            return str(dest)
        raise ConventionError(500, "Could not write a backup")

    def _remember(self, key: str, record: dict) -> None:
        with self._lock:
            self._recs(key)  # makes sure the records are loaded
            recs = list(self._recs(key)) + [record]
            self._records[key] = recs[-KEEP_RECORDS:]
            try:
                self._save("applies.json", self._records)
            except OSError:
                self._records[key] = recs[:-1]
                raise

    def _forget(self, key: str, record: dict) -> None:
        with self._lock:
            self._records[key] = [r for r in self._recs(key) if r is not record]
            try:
                self._save("applies.json", self._records)
            except OSError:
                log.warning("could not drop an unused apply record", exc_info=True)

    def _prune_backups(self) -> None:
        """Delete backups that no kept record refers to. Only files named like our backups, and never after a failed load."""
        folder = self.dir / "backups"
        if self._no_prune:
            return
        with self._lock:
            if self._records is None:
                return
            keep = set()
            for recs in self._records.values():
                for r in recs if isinstance(recs, list) else []:
                    if isinstance(r, dict) and r.get("backup"):
                        keep.add(os.path.realpath(str(r["backup"])))
        try:
            for f in folder.iterdir():
                if BACKUP_NAME.match(f.name) and f.is_file() and os.path.realpath(f) not in keep:
                    f.unlink()
        except OSError:
            log.debug("backup pruning failed", exc_info=True)

    def apply(self, key: str, name: str, root: str, rule_id: str, target: str,
              before_sha256: str, text_sha256: str) -> dict:
        with self._write_lock:
            root_real, rule, scan, tgt, text = self._prepare(key, root, rule_id, target, fresh=True)
            path = tgt["path"]
            data, mode = _read_target(path)
            if not isinstance(before_sha256, str) or not isinstance(text_sha256, str) \
                    or before_sha256 != _before_hash(data) or text_sha256 != _sha(text.encode("utf-8")):
                raise ConventionError(409, CHANGED)
            if self._status(key, scan, rule) not in ("missing", "partial"):
                raise ConventionError(409, NOT_NEEDED)
            base = (data if data is not None else b"# CLAUDE.md").rstrip(b"\n")
            new = base + b"\n\n" + text.encode("utf-8") + b"\n"  # exactly one blank line between
            backup = self._backup(key, data)
            record = {
                "target": tgt["target"], "path": path, "rule_id": rule.id, "applied_at": self.clock(),
                "existed": data is not None, "before_sha256": _before_hash(data), "after_sha256": _sha(new),
                "backup": backup, "undone": False,
            }
            try:
                self._remember(key, record)  # saved before the file changes: a failure here touches nothing
            except OSError:
                log.warning("conventions apply could not save its undo record key=%s", key, exc_info=True)
                raise ConventionError(500, "Could not save the undo record") from None
            try:
                again, _ = _read_target(path)
                if _before_hash(again) != _before_hash(data):
                    raise ConventionError(409, CHANGED)
                _replace_file(path, new, mode if mode is not None else 0o644)
            except ConventionError:
                self._forget(key, record)
                raise
            except OSError:
                self._forget(key, record)
                log.warning("conventions apply failed key=%s rule=%s target=%s", key, rule.id, tgt["target"], exc_info=True)
                raise ConventionError(500, "Could not write the file") from None
            self._prune_backups()
            log.info("conventions apply key=%s rule=%s target=%s bytes_before=%d bytes_after=%d",
                     key, rule.id, tgt["target"], len(data or b""), len(new))
            return self._report(key, name, root, self._scan(root, force=True))

    def undo(self, key: str, name: str, root: str, target: str) -> dict:
        with self._write_lock:
            root_real = canonical(root)
            if self._unsafe_root(root_real):
                raise ConventionError(409, "This project folder cannot be changed")
            rel = [] if target == "root" else target.split("/")
            if any(p in ("", ".", "..") for p in rel) or os.path.isabs(target):
                raise ConventionError(409, CHANGED_AFTER)
            path = os.path.join(root_real, *(rel[:-1]), rel[-1] if rel else "CLAUDE.md")
            rec = None
            for cand in [r for r in reversed(self._recs(key)) if r.get("target") == target and not r.get("undone")]:
                if cand.get("path") == path and self._phantom(cand):
                    self._forget(key, cand)  # saved but never applied: drop it and look at the next older one
                    continue
                rec = cand
                break
            if rec is None:
                raise ConventionError(404, "Nothing to undo")
            if rec.get("path") != path:
                raise ConventionError(409, CHANGED_AFTER)
            try:
                st = os.lstat(path)
            except OSError:
                raise ConventionError(409, CHANGED_AFTER) from None
            if stat.S_ISLNK(st.st_mode) or not stat.S_ISREG(st.st_mode) or not _inside(os.path.realpath(path), root_real):
                raise ConventionError(409, CHANGED_AFTER)
            try:
                current, mode = _read_target(path)
            except ConventionError:
                raise ConventionError(409, CHANGED_AFTER) from None
            if current is None or _sha(current) != rec.get("after_sha256"):
                raise ConventionError(409, CHANGED_AFTER)
            restored = 0
            try:
                if not rec.get("existed"):
                    os.unlink(path)  # a file this apply created goes away under the same hash rule
                else:
                    backups = os.path.realpath(self.dir / "backups")
                    bpath = os.path.realpath(str(rec.get("backup", "")))
                    if not _inside(bpath, backups):
                        raise ConventionError(409, CHANGED_AFTER)
                    with open(bpath, "rb") as fh:
                        original = fh.read(MAX_TARGET_BYTES + 1)
                    if _sha(original) != rec.get("before_sha256"):
                        raise ConventionError(409, CHANGED_AFTER)
                    _replace_file(path, original, mode if mode is not None else 0o644)
                    restored = len(original)
            except ConventionError:
                raise
            except OSError:
                log.warning("conventions undo failed key=%s target=%s", key, target, exc_info=True)
                raise ConventionError(500, "Could not restore the file") from None
            with self._lock:
                rec["undone"] = True
                try:
                    self._save("applies.json", self._records or {})
                except OSError:  # the file is already restored: report success, the stale record fails its hash check later
                    log.warning("conventions undo could not save its record key=%s", key, exc_info=True)
            self._prune_backups()
            log.info("conventions undo key=%s rule=%s target=%s bytes_after=%d", key, rec.get("rule_id"), target, restored)
            return self._report(key, name, root, self._scan(root, force=True))

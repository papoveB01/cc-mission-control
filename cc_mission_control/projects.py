"""Project grouping (SPEC 11.14): which project a session's working folder belongs to.

This does filesystem work (a few `stat` calls per distinct cwd, cached), so it runs when the
server first serves a session, never on `/hook` ingestion. `Store`, the relay and the hub
never see it: the hub has no view of this machine's folders.

Paths are compared by identity (`same_path`), never as plain strings: on a case-insensitive
filesystem `/Users/x/HOME` and `/Users/x/home` are the same folder.
"""

from __future__ import annotations

import hashlib
import os
import threading
import time
import unicodedata
from typing import Callable, Optional, Tuple

MAX_LEVELS = 64
CACHE_SECONDS = 60.0
_GITDIR_MAX = 4096
_CASE_SCAN_CAP = 20_000


def same_path(a: str, b: str) -> bool:
    """True when two paths are the same folder or file, by `os.path.samefile`.

    Identity only means something when both exist; if either does not, only identical strings match.
    """
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normpath(a) == os.path.normpath(b)


def _fold(name: str) -> str:
    return unicodedata.normalize("NFC", name).casefold()


def _disk_name(parent: str, comp: str) -> str:
    """The name `comp` really has inside `parent` (fixes the case a case-insensitive filesystem let through)."""
    if not os.path.lexists(os.path.join(parent, comp)):
        return comp
    folded = None
    try:
        with os.scandir(parent) as it:
            for n, entry in enumerate(it):
                if entry.name == comp:
                    return comp
                if folded is None and _fold(entry.name) == _fold(comp):  # case and NFC/NFD spellings
                    folded = entry.name
                if n > _CASE_SCAN_CAP:
                    break
    except OSError:
        return comp
    if folded is not None and same_path(os.path.join(parent, folded), os.path.join(parent, comp)):
        return folded
    return comp


def canonical(path: str) -> str:
    """`realpath` with each component in the case it has on disk, so one folder always has one spelling."""
    real = os.path.realpath(path)
    cur = os.sep
    for comp in real.split(os.sep):
        if comp:
            cur = os.path.join(cur, _disk_name(cur, comp))
    return cur


def project_key(root: str) -> str:
    """First 16 hex characters of sha256(root)."""
    return hashlib.sha256(root.encode("utf-8", "surrogateescape")).hexdigest()[:16]


def _ancestors(start: str, home: str):
    """`start` and its parents, stopping before $HOME and before `/` (neither is ever a root here)."""
    cur = start
    for _ in range(MAX_LEVELS):
        parent = os.path.dirname(cur)
        if parent == cur or same_path(cur, home):  # reached "/", or $HOME by identity
            return
        yield cur
        cur = parent


def _has_marker(folder: str) -> bool:
    return os.path.isfile(os.path.join(folder, "CLAUDE.md")) or os.path.isdir(os.path.join(folder, ".claude"))


def _main_checkout(folder: str, home: str) -> Optional[str]:
    """For a linked worktree (`.git` is a file `gitdir: <main>/.git/worktrees/<name>`) the main checkout's top.

    Parsed by hand: no git subprocess. The gitdir is trusted only when `<gitdir>/gitdir` points back to this
    folder's `.git` file; otherwise (or on any doubt) None.
    """
    try:
        with open(os.path.join(folder, ".git"), "rb") as fh:
            text = fh.read(_GITDIR_MAX).decode("utf-8", "replace")
    except OSError:
        return None
    first = text.strip().splitlines()[0] if text.strip() else ""
    if not first.lower().startswith("gitdir:"):
        return None
    gitdir = first[len("gitdir:"):].strip()
    if not gitdir or "\0" in gitdir:
        return None
    if not os.path.isabs(gitdir):
        gitdir = os.path.join(folder, gitdir)
    gitdir = os.path.normpath(gitdir)
    parts = gitdir.split(os.sep)
    for i in range(len(parts) - 1):
        if parts[i] == ".git" and parts[i + 1] == "worktrees":
            main = canonical(os.sep.join(parts[:i]) or os.sep)
            if main == os.sep or same_path(main, home) or not os.path.isdir(os.path.join(main, ".git")):
                return None
            try:  # the main checkout must agree that this folder is one of its worktrees
                with open(os.path.join(gitdir, "gitdir"), "rb") as fh:
                    back = fh.read(_GITDIR_MAX).decode("utf-8", "replace").strip()
            except OSError:
                return None
            if not back or "\0" in back:
                return None
            if not os.path.isabs(back):
                back = os.path.join(gitdir, back)
            if not same_path(os.path.normpath(back), os.path.join(folder, ".git")):
                return None
            return main
    return None


def resolve_root(cwd: str, home: str) -> Optional[str]:
    """SPEC 11.14 root resolution: outermost marker, else the git checkout's top, else the cwd itself."""
    if not isinstance(cwd, str) or not cwd or "\0" in cwd or not os.path.isabs(cwd):
        return None
    real = canonical(cwd)
    outermost = None
    for folder in _ancestors(real, home):
        if _has_marker(folder):
            outermost = folder  # keep walking: the outermost candidate wins
    if outermost is not None:
        return outermost
    for folder in _ancestors(real, home):
        git = os.path.join(folder, ".git")
        if os.path.isdir(git):
            return folder
        if os.path.isfile(git):
            return _main_checkout(folder, home) or folder
    return real


class Projects:
    """Per-cwd cache of project roots (60 s), plus a registry of the roots seen (for duplicate-name handling)."""

    def __init__(self, home: "os.PathLike[str] | str | None" = None,
                 clock: Callable[[], float] = time.monotonic) -> None:
        raw = home if home is not None else os.path.expanduser("~")
        self.home = canonical(os.fspath(raw))
        self.clock = clock
        self._cache: dict[str, Tuple[Optional[str], float]] = {}
        self._roots: dict[str, str] = {}  # root -> key
        self._names_dirty = False
        self._lock = threading.Lock()

    def root_for(self, cwd: object) -> Optional[str]:
        if not isinstance(cwd, str) or not cwd:
            return None
        now = self.clock()
        with self._lock:
            hit = self._cache.get(cwd)
            if hit is not None and 0 <= now - hit[1] < CACHE_SECONDS:
                return hit[0]
        root = resolve_root(cwd, self.home)  # filesystem work, outside the lock
        with self._lock:
            self._cache[cwd] = (root, now)
            if root is not None and root not in self._roots:
                self._roots[root] = project_key(root)
                if self._base(root) in {self._base(r) for r in self._roots if r != root}:
                    self._names_dirty = True  # an existing project's name just changed
        return root

    @staticmethod
    def _base(root: str) -> str:
        return os.path.basename(root.rstrip(os.sep)) or root

    def name_of(self, root: str) -> str:
        base = self._base(root)
        with self._lock:
            clash = any(r != root and self._base(r) == base for r in self._roots)
        if not clash:
            return base
        parent = os.path.basename(os.path.dirname(root.rstrip(os.sep))) or os.sep
        return f"{base} ({parent})"

    def lookup(self, cwd: object) -> Optional[Tuple[str, str, str]]:
        """(key, name, root) for a cwd, or None when the cwd is unknown."""
        root = self.root_for(cwd)
        if root is None:
            return None
        return project_key(root), self.name_of(root), root

    def info(self, cwd: object) -> Optional[dict]:
        """The `project` field of a Session: {key, name}, or None."""
        hit = self.lookup(cwd)
        return {"key": hit[0], "name": hit[1]} if hit else None

    def take_names_dirty(self) -> bool:
        """True once after a new project made an existing project's name ambiguous."""
        with self._lock:
            dirty, self._names_dirty = self._names_dirty, False
            return dirty

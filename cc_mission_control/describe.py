"""Plain-language descriptions of tool calls and their outcomes (SPEC section 11.6.1).

Pure functions, no I/O. Everything here is defensive: payload shapes are not documented for
every tool, so any unexpected value falls back to the generic rule and nothing ever raises.
"""

from __future__ import annotations

import os
import re
from typing import Any, List, Optional
from urllib.parse import urlparse

CALL_MAX = 160
OUTCOME_MAX = 120
TAIL = 20000  # only the end of command output is scanned for summaries
PRE_MAX = 20000  # CPU bound only; the caller redacts first, then clips to CALL_MAX / OUTCOME_MAX

COMPACTING = "Compacting context to free up space"
COMPACTED = "Context compacted"
TURN_FINISHED = "Finished responding"

_NOISE_RE = re.compile(r"^(exit code[ \t]*\d+|shell cwd was reset.*|\[?stderr\]?:?)$", re.I)
_EXIT_RE = re.compile(r"^[ \t]*exit code[ \t]*(\d+)", re.I)
_REJECT_RE = re.compile(
    r"(the user doesn'?t want to proceed|user (rejected|declined|denied)|"
    r"tool use was rejected|request interrupted by user|permission to use [^\n]{0,200} has been denied)", re.I,
)
# Summary lines are matched one short line at a time (no multi-line patterns, no nested repeats).
_PYTEST_RE = re.compile(r"^[= \t]*(\d+ [a-z]+(?:, \d+ [a-z]+)*) in [0-9.]+s")
_COUNT_RE = re.compile(r"(\d+) (failed|passed|skipped|errors?|todo)", re.I)
_COMMIT_RE = re.compile(r"^\[([^\]\s]+)(?: \([^)]*\))? [0-9a-f]{4,}\] (.+)$")
_PUSH_RE = re.compile(r"^[ \t]*(?:[0-9a-f]{4,}\.\.\.?[0-9a-f]{4,}|\* \[new (?:branch|tag)\])[ \t]+(\S+) -> (\S+)")
SCAN_LINES = 200
LINE_MAX = 300  # longer lines are not summary lines


# ---- tiny helpers -----------------------------------------------------------


def _s(v: Any) -> str:
    return v if isinstance(v, str) else ""


def _d(v: Any) -> dict:
    return v if isinstance(v, dict) else {}


def _int(v: Any) -> Optional[int]:
    if isinstance(v, bool):
        return None
    if isinstance(v, int):
        return v if v >= 0 else None
    if isinstance(v, float) and v == v and v >= 0 and v != float("inf"):
        return int(v)
    return None


def clip(text: str, limit: int) -> str:
    text = " ".join(text.split()) if len(text) < 100000 else " ".join(text[:100000].split())
    return text if len(text) <= limit else text[: limit - 1].rstrip() + "…"


def _plural(n: int, word: str) -> str:
    if n == 1:
        return f"{n} {word}"
    return f"{n} {word}es" if word.endswith("ch") else f"{n} {word}s"


def _first_line(text: str, skip_noise: bool = True) -> str:
    for raw in text[:TAIL].splitlines():
        line = raw.strip()
        if line and not (skip_noise and _NOISE_RE.match(line)):
            return line
    return ""


def _sentence(text: str) -> str:
    text = text.strip()
    return text[:1].upper() + text[1:] if text else text


def _path(p: Any, cwd: Any) -> str:
    """Relative to the session cwd when inside it, else ~/..., else parent/basename."""
    p = _s(p).strip()
    if not p:
        return ""
    try:
        c = _s(cwd).rstrip("/")
        if c and (p == c or p.startswith(c + "/")):
            return p[len(c) + 1:] or "."
        home = os.path.expanduser("~").rstrip("/")
        if home and home != "~" and (p == home or p.startswith(home + "/")):
            return "~" + p[len(home):]
        if p.startswith("/"):
            parts = [x for x in p.split("/") if x]
            return "/".join(parts[-2:]) if parts else p
    except Exception:
        pass
    return p


def _domain(url: Any) -> str:
    url = _s(url).strip()
    if not url:
        return ""
    try:
        host = urlparse(url if "//" in url else "//" + url).hostname or ""
        return host or url[:60]
    except Exception:
        return url[:60]


def _mcp(tool: str) -> Optional[tuple]:
    if not tool.startswith("mcp__"):
        return None
    parts = tool.split("__", 2)
    if len(parts) < 3 or not parts[2]:
        return None
    server = parts[1]
    for prefix in ("claude_ai_", "plugin_"):
        if server.startswith(prefix) and len(server) > len(prefix):
            server = server[len(prefix):]
    return parts[2].replace("_", " "), server.replace("_", " ")


def _lines_in(text: str) -> int:
    if not text:
        return 0
    return text.count("\n") + (0 if text.endswith("\n") else 1)


# ---- Bash ---------------------------------------------------------------------

_SPLIT_CHARS = (";", "|", "&", "\n")
_WRAPPERS = {"sudo", "time", "env", "command", "nohup", "exec", "nice"}
_SKIP_PROGS = {"cd", "set", "export", "echo", "printf", "true", "source", ".", "unset", ":", "test", "["}
_VALUE_OPTS = {"-p", "--python", "--extra", "--with", "--project", "--directory", "-C", "--group", "--from"}
_PKG = {"npm", "pnpm", "yarn", "bun"}
_INSTALL_VERBS = {"install", "ci", "add", "i"}
_SSH_VALUE_OPTS = {"-p", "-i", "-o", "-l", "-F", "-J", "-L", "-R", "-D", "-b", "-c", "-E", "-e", "-I", "-m", "-O", "-S", "-W", "-w"}


def _segments(command: str) -> List[List[str]]:
    """Token lists for up to 8 command segments (split on ; | & and newlines), heredoc bodies dropped."""
    head = command.split("<<", 1)[0][:2000]
    for ch in _SPLIT_CHARS:
        head = head.replace(ch, "\n")
    out = [seg.split() for seg in head.split("\n")]
    return [t for t in out if t][:8]


def _is_assign(tok: str) -> bool:
    name, eq, _ = tok.partition("=")
    return bool(eq) and bool(name) and name.replace("_", "a").isalnum() and not name[0].isdigit()


def _strip_prefix(t: List[str]) -> List[str]:
    i = 0
    while i < len(t) and (t[i] in _WRAPPERS or _is_assign(t[i])):
        i += 1
    return t[i:]


def _skip_opts(rest: List[str]) -> List[str]:
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in _VALUE_OPTS else 1
    return rest[i:]


def _unwrap(prog: str, rest: List[str]) -> tuple:
    """Look through runners: uv/poetry run X, npx X, python -m X."""
    if prog in ("uv", "poetry", "pipenv", "pdm", "rye") and rest[:1] == ["run"]:
        rest = _skip_opts(rest[1:])
        if rest:
            return os.path.basename(rest[0]), rest[1:]
    elif prog in ("npx", "bunx", "pnpx"):
        rest = _skip_opts(rest)
        if rest:
            return os.path.basename(rest[0]), rest[1:]
    elif prog in ("pnpm", "yarn") and rest[:1] in (["dlx"], ["exec"]):
        rest = _skip_opts(rest[1:])
        if rest:
            return os.path.basename(rest[0]), rest[1:]
    elif prog.startswith("python") and rest[:1] == ["-m"] and len(rest) > 1:
        return rest[1], rest[2:]
    return prog, rest


def _git_verb_tokens(rest: List[str]) -> str:
    i = 0
    while i < len(rest) and rest[i].startswith("-"):
        i += 2 if rest[i] in ("-C", "-c", "--git-dir", "--work-tree") else 1
    verb = rest[i] if i < len(rest) else ""
    return verb if verb.replace("-", "").isalpha() else ""


def _http_host(rest: List[str]) -> str:
    for tok in rest:
        tok = tok.strip("'\"")
        if "://" in tok or tok.startswith(("localhost", "127.0.0.1")):
            return _domain(tok)
    return ""


def _ssh_host(rest: List[str]) -> str:
    i = 0
    while i < len(rest):
        if rest[i] in _SSH_VALUE_OPTS:
            i += 2
        elif rest[i].startswith("-"):
            i += 1
        else:
            return rest[i].split("@")[-1][:60]
    return ""


def _classify_tokens(tokens: List[str]) -> Optional[str]:
    """None: not a command worth describing (cd, set, ...). "": unknown command."""
    t = _strip_prefix(tokens)
    if not t:
        return None
    prog, rest = os.path.basename(t[0]), t[1:]
    if prog in _SKIP_PROGS:
        return None
    prog, rest = _unwrap(prog, rest)
    first = rest[0] if rest else ""
    second = rest[1] if len(rest) > 1 else ""
    if prog in ("pytest", "py.test", "vitest", "jest", "unittest"):
        return "Ran the test suite"
    if (prog in _PKG and (first == "test" or (first == "run" and second == "test"))) or (
        prog in ("go", "cargo") and first == "test"
    ):
        return "Ran the test suite"
    if prog == "git":
        verb = _git_verb_tokens(rest)
        return f"Ran git {verb}" if verb else "Ran git"
    if (
        (prog in _PKG and first in _INSTALL_VERBS)
        or (prog in ("pip", "pip3") and first == "install")
        or (prog == "uv" and (first in ("sync", "add") or (first == "pip" and second == "install")))
        or (prog in ("brew", "apt", "apt-get") and first == "install")
        or (prog == "cargo" and first in ("add", "install"))
        or (prog == "poetry" and first in ("install", "add"))
        or (prog == "go" and (first == "get" or (first == "mod" and second == "download")))
    ):
        return "Installed packages"
    if (
        (prog in _PKG and first == "run" and second == "build")
        or (prog in ("uv", "cargo", "go", "vite") and first == "build")
        or prog in ("make", "tsc")
        or (prog == "mvn" and first in ("package", "install"))
        or (prog.startswith("gradle") and first == "build")
    ):
        return "Built the project"
    if prog == "docker-compose" or (prog == "docker" and first == "compose"):
        verb = _skip_opts(rest[1:] if prog == "docker" else rest)
        return f"Ran docker compose {verb[0]}" if verb else "Ran docker compose"
    if prog == "docker":
        verb = _skip_opts(rest)
        return f"Ran docker {verb[0]}" if verb else "Ran docker"
    if prog in ("curl", "wget"):
        host = _http_host(rest)
        return f"Made an HTTP request to {host}" if host else "Made an HTTP request"
    if prog == "ssh":
        host = _ssh_host(rest)
        return f"Ran a command on {host} over SSH" if host else "Ran a command over SSH"
    if prog in ("ls", "tree", "find", "fd"):
        return "Listed files"
    if prog in ("grep", "rg", "ack", "ag"):
        return "Searched files"
    return ""


def _bash_call(tool_input: dict) -> str:
    desc = _s(tool_input.get("description")).strip()
    if desc:
        return _sentence(_first_line(desc, False))
    for seg in _segments(_s(tool_input.get("command"))):
        found = _classify_tokens(seg)
        if found:
            return found
    return "Ran a shell command"


def _bash_verb(tool_input: dict) -> str:
    for seg in _segments(_s(tool_input.get("command"))):
        t = _strip_prefix(seg)
        if t and os.path.basename(t[0]) == "git":
            return _git_verb_tokens(t[1:])
    return ""


def _counts(text: str) -> str:
    seen = []
    for n, word in _COUNT_RE.findall(text[:LINE_MAX]):
        word = word.lower()
        seen.append(f"{n} {'error' if word.startswith('error') and n == '1' else word}")
    return ", ".join(seen)


def _tail_lines(text: str) -> List[str]:
    """The last SCAN_LINES short lines of the output, newest first."""
    lines = text[-TAIL:].splitlines()[-SCAN_LINES:]
    return [x for x in reversed(lines) if len(x) <= LINE_MAX]


def _test_summary(text: str) -> str:
    for line in _tail_lines(text):
        m = _PYTEST_RE.match(line)
        if m:
            return _counts(m.group(1))
        stripped = line.strip()
        if stripped.startswith("Tests  "):  # vitest: "Tests  1 failed | 12 passed (13)"
            body = stripped[5:]
            cut = body.rfind("(")
            found = _counts(body[:cut] if cut > 0 else body)
            if found:
                return found
        elif stripped.startswith("Tests:"):  # jest; the trailing "N total" is dropped on purpose
            found = _counts(stripped[6:])
            if found:
                return found
    return ""


def _bash_outcome(tool_input: dict, resp: Any, error: str) -> str:
    r = _d(resp)
    stdout, stderr = _s(r.get("stdout")), _s(r.get("stderr"))
    if isinstance(resp, str):
        stdout = resp
    if error:
        m = _EXIT_RE.match(error)
        summary = _test_summary(error)
        if m:
            code = m.group(1)
            if summary:
                return f"{summary} (exit code {code})"
            line = _first_line(error[m.end():]) or _first_line(stderr)
            return f"exit code {code}: {line}" if line else f"exit code {code}"
        return f"Failed: {_first_line(error)}" if _first_line(error) else "Failed"
    if r.get("interrupted") is True:
        return "interrupted"
    summary = _test_summary(stdout) or _test_summary(stderr)
    if summary:
        return summary
    verb = _bash_verb(tool_input)
    if verb == "commit":
        for line in _tail_lines(stdout):
            m = _COMMIT_RE.match(line)
            if m:
                return f"committed: {m.group(2)}"
    if verb == "push":
        for line in _tail_lines(stderr) + _tail_lines(stdout):
            m = _PUSH_RE.match(line)
            if m:
                return f"pushed {m.group(2)}"
    return "done"


# ---- per-tool outcome helpers ---------------------------------------------------


def _read_outcome(resp: Any) -> str:
    if isinstance(resp, str):
        n = _lines_in(resp)
        return _plural(n, "line") if n else ""
    r = _d(resp)
    f = _d(r.get("file"))
    n = _int(f.get("numLines"))
    if n is None and isinstance(f.get("content"), str):
        n = _lines_in(f["content"])
    if n is None and isinstance(r.get("content"), str):
        n = _lines_in(r["content"])
    if n is None:
        return _s(r.get("type")) if r.get("type") in ("image", "pdf", "notebook") else ""
    return _plural(n, "line")


def _patch_counts(resp: dict) -> Optional[tuple]:
    patch = resp.get("structuredPatch")
    if not isinstance(patch, list) or not patch:
        return None
    add = rem = 0
    for hunk in patch:
        for line in _d(hunk).get("lines") or []:
            if isinstance(line, str) and not line.startswith(("+++", "---")):
                if line.startswith("+"):
                    add += 1
                elif line.startswith("-"):
                    rem += 1
    return add, rem


def _edit_outcome(resp: Any) -> str:
    counts = _patch_counts(_d(resp))
    if counts is None:
        return "updated"
    return f"+{counts[0]} −{counts[1]} lines"


def _write_outcome(tool_input: dict, resp: Any) -> str:
    content = tool_input.get("content")
    if not isinstance(content, str):
        content = _d(resp).get("content")
    if not isinstance(content, str):
        return ""
    return _plural(_lines_in(content), "line")


def _grep_outcome(resp: Any) -> str:
    if isinstance(resp, str):
        n = _lines_in(resp.strip())
        return _plural(n, "match") if resp.strip() else "no matches"
    r = _d(resp)
    files = r.get("filenames")
    nfiles = _int(r.get("numFiles"))
    if nfiles is None and isinstance(files, list):
        nfiles = len(files)
    content = r.get("content")
    nlines = _int(r.get("numLines"))
    nmatch = _int(r.get("numMatches"))
    if isinstance(content, str) and content.strip():
        n = nmatch if nmatch is not None else nlines if nlines is not None else _lines_in(content.strip())
        return _plural(n, "match")
    if nmatch is not None:
        return "no matches" if nmatch == 0 else _plural(nmatch, "match")
    if nfiles is not None:
        return "no matches" if nfiles == 0 else _plural(nfiles, "file")
    if nlines == 0:
        return "no matches"
    return ""


def _glob_outcome(resp: Any) -> str:
    r = _d(resp)
    n = _int(r.get("numFiles"))
    if n is None and isinstance(r.get("filenames"), list):
        n = len(r["filenames"])
    return _plural(n, "file") if n is not None else ""


def _fetch_outcome(resp: Any) -> str:
    r = _d(resp)
    code = _int(r.get("code"))
    if code is None:
        return "fetched" if r or isinstance(resp, str) else ""
    text = _s(r.get("codeText")).strip()
    return f"HTTP {code} {text}".strip()


def _search_outcome(resp: Any) -> str:
    results = _d(resp).get("results")
    if not isinstance(results, list):
        return ""
    n = 0
    for item in results:
        content = _d(item).get("content") if isinstance(item, dict) else None
        if isinstance(content, list):
            n += sum(1 for c in content if isinstance(c, dict))
    return _plural(n, "result") if n else ""


def _todo_outcome(tool_input: dict) -> str:
    todos = tool_input.get("todos")
    if not isinstance(todos, list):
        return ""
    done = sum(1 for t in todos if isinstance(t, dict) and t.get("status") == "completed")
    return f"{_plural(len(todos), 'task')}, {done} done"


def _ask_outcome(tool_input: dict, resp: Any) -> str:
    """Selected option labels only; a free-text answer is never echoed."""
    answers = _d(resp).get("answers")
    if not isinstance(answers, dict) or not answers:
        return ""
    labels = set()
    questions = tool_input.get("questions")
    for q in questions if isinstance(questions, list) else []:
        options = _d(q).get("options")
        for o in options if isinstance(options, list) else []:
            if isinstance(o, dict) and isinstance(o.get("label"), str):
                labels.add(o["label"])
    chosen: List[str] = []
    typed = False
    for v in list(answers.values())[:10]:
        if not isinstance(v, str) or not v.strip():
            continue
        parts = [v] if v in labels else v.split(", ")
        if all(part in labels for part in parts):
            chosen.extend(clip(part, 60) for part in parts)
        else:
            typed = True
    if typed:
        chosen.append("You typed an answer")
    if not chosen:
        return ""
    return chosen[0] if typed and len(chosen) == 1 else "Chose: " + "; ".join(chosen)


def _agent_outcome(resp: Any) -> str:
    if _d(resp).get("status") == "async_launched":
        return "started in the background"
    return "finished"


# ---- public API -------------------------------------------------------------------


def _generic(tool: str) -> str:
    return f"Used {tool or 'a tool'}"


def describe_call(tool: Any, tool_input: Any, cwd: Any = "") -> str:
    """What the call does, in a plain sentence. Not clipped to CALL_MAX: the caller redacts first, then clips."""
    tool = _s(tool)
    try:
        return clip(_describe_call(tool, _d(tool_input), cwd), PRE_MAX) or _generic(tool)
    except Exception:
        return _generic(tool)


def _describe_call(tool: str, i: dict, cwd: Any) -> str:
    if tool == "Read":
        p = _path(i.get("file_path"), cwd)
        if not p:
            return "Read a file"
        offset, limit = _int(i.get("offset")), _int(i.get("limit"))
        if limit:
            a = offset if offset else 1
            return f"Read {p} (lines {a}–{a + limit - 1})"
        if offset:
            return f"Read {p} (from line {offset})"
        return f"Read {p}"
    if tool == "Write":
        p = _path(i.get("file_path"), cwd)
        return f"Wrote {p}" if p else "Wrote a file"
    if tool in ("Edit", "MultiEdit"):
        p = _path(i.get("file_path"), cwd)
        return f"Edited {p}" if p else "Edited a file"
    if tool == "NotebookEdit":
        p = _path(i.get("notebook_path") or i.get("file_path"), cwd)
        return f"Edited notebook {p}" if p else "Edited a notebook"
    if tool == "Bash":
        return _bash_call(i)
    if tool == "Grep":
        where = _path(i.get("path"), cwd) or "the project"
        pat = _s(i.get("pattern"))
        return f'Searched {where} for "{clip(pat, 60)}"' if pat else f"Searched {where}"
    if tool == "Glob":
        pat = _s(i.get("pattern"))
        return f"Listed files matching {clip(pat, 80)}" if pat else "Listed files"
    if tool == "WebFetch":
        d = _domain(i.get("url"))
        return f"Fetched {d}" if d else "Fetched a web page"
    if tool == "WebSearch":
        q = _s(i.get("query"))
        return f'Searched the web for "{clip(q, 80)}"' if q else "Searched the web"
    if tool in ("Agent", "Task"):
        desc, kind = clip(_s(i.get("description")), 80), clip(_s(i.get("subagent_type")), 40)
        out = f'Started helper agent "{desc}"' if desc else "Started a helper agent"
        return f"{out} ({kind})" if kind else out
    if tool == "TodoWrite":
        return "Updated the task list"
    if tool == "Skill":
        name = clip(_s(i.get("skill")), 60)
        return f"Used the {name} skill" if name else "Used a skill"
    if tool == "AskUserQuestion":
        return "Asked you a question"
    m = _mcp(tool)
    if m:
        return f"Used {m[0]} ({m[1]})"
    return _generic(tool)


def describe_outcome(tool: Any, tool_input: Any, tool_response: Any, error: Any) -> str:
    """How the call went; "" when nothing useful. Not clipped to OUTCOME_MAX: the caller redacts first, then clips."""
    try:
        return clip(_describe_outcome(_s(tool), _d(tool_input), tool_response, error), PRE_MAX)
    except Exception:
        return ""


def _error_text(error: Any) -> str:
    if isinstance(error, str):
        return error
    if error in (None, "", False, [], {}):
        return ""
    return str(error)[:2000]


def _describe_outcome(tool: str, i: dict, resp: Any, error_raw: Any) -> str:
    error = _error_text(error_raw)
    if error:
        if _REJECT_RE.search(error[:500]):
            return "Declined by you"
        if tool == "Bash":
            return _bash_outcome(i, resp, error)
        line = _first_line(error)
        return f"Failed: {line}" if line else "Failed"
    if tool == "Bash":
        return _bash_outcome(i, resp, "")
    if tool == "Read":
        return _read_outcome(resp)
    if tool == "Write":
        return _write_outcome(i, resp)
    if tool in ("Edit", "MultiEdit"):
        return _edit_outcome(resp)
    if tool == "NotebookEdit":
        return "updated"
    if tool == "Grep":
        return _grep_outcome(resp)
    if tool == "Glob":
        return _glob_outcome(resp)
    if tool == "WebFetch":
        return _fetch_outcome(resp)
    if tool == "WebSearch":
        return _search_outcome(resp)
    if tool in ("Agent", "Task"):
        return _agent_outcome(resp)
    if tool == "TodoWrite":
        return _todo_outcome(i)
    if tool == "AskUserQuestion":
        return _ask_outcome(i, resp)
    return ""


# ---- non-tool activity ----------------------------------------------------------


def plain_prompt(text: Any) -> str:
    line = _first_line(_s(text), False)
    return f"You asked: {clip(line, 140)}" if line else "You sent a message"


def plain_helper(label: Any, phase: str) -> str:
    """phase is "started" or "finished"."""
    name = clip(_s(label), 60)
    return f'Helper agent "{name}" {phase}' if name else f"Helper agent {phase}"


def plain_denied(tool: Any) -> str:
    return f"{_s(tool) or 'A tool'} was not allowed"

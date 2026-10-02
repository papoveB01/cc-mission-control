#!/usr/bin/env python3
"""Fake Claude Code session generator for dashboard development and demos (SPEC 15.2).

Posts hook events shaped like the official hooks docs to http://127.0.0.1:<port>/hook
and writes a matching fake transcript tree so the context gauges move:

    <transcript-dir>/<session_id>.jsonl
    <transcript-dir>/<session_id>/subagents/agent-<id>.jsonl
    <transcript-dir>/<session_id>/subagents/agent-<id>.meta.json

Stdlib only. Usable as a library (`run(...)`) and as a CLI (`main()`).
"""

from __future__ import annotations

import argparse
import json
import os
import random
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

APP_ID = "cc-mission-control"
DEFAULT_PORT = 4317
MAIN_MODEL = "claude-opus-5"
SUB_MODEL = "claude-sonnet-5"
SUBAGENT_TYPES = ["Explore", "general-purpose", "code-reviewer"]

# (title, user prompt, source files)
PROJECTS: List[Tuple[str, str, List[str]]] = [
    (
        "fraud-scoring",
        "Add a velocity check to the fraud scoring pipeline and cover it with tests",
        ["src/fraud/scoring.py", "src/fraud/velocity.py", "src/fraud/rules.py", "tests/test_scoring.py"],
    ),
    (
        "payments-api",
        "Fix the duplicate charge bug when the processor retries a webhook",
        ["app/webhooks/stripe.py", "app/payments/charge.py", "app/db/idempotency.py", "tests/test_webhooks.py"],
    ),
    (
        "ledger-service",
        "Make ledger reconciliation resumable after a partial batch failure",
        ["ledger/reconcile.py", "ledger/batch.py", "ledger/models.py", "tests/test_reconcile.py"],
    ),
    (
        "search-indexer",
        "Speed up incremental indexing by batching document upserts",
        ["indexer/pipeline.py", "indexer/upsert.py", "indexer/config.py", "tests/test_pipeline.py"],
    ),
]

DESCRIPTIONS: Dict[str, List[str]] = {
    "Explore": [
        "Map the entry points of the scoring pipeline",
        "Find every caller of the rules engine",
        "Survey existing retry and idempotency handling",
    ],
    "general-purpose": [
        "Draft unit tests for the sliding window logic",
        "Refactor config loading into a dataclass",
        "Write a migration for the new counters table",
        "Implement the batching helper and wire it in",
    ],
    "code-reviewer": [
        "Review the diff for race conditions",
        "Audit error handling in the new code path",
    ],
}

SUB_TOOLS: Dict[str, List[str]] = {
    "Explore": ["Read", "Grep", "Glob", "Read", "Grep"],
    "general-purpose": ["Read", "Grep", "Bash", "Edit", "Read", "Bash", "Edit"],
    "code-reviewer": ["Read", "Grep", "Read", "Bash", "Grep"],
}


class ServerUnreachable(RuntimeError):
    """The dashboard server could not be reached (or is not cc-mission-control)."""


class Stopped(Exception):
    """Raised inside simulator threads when the run is cancelled (Ctrl-C)."""


def fake_key(session_id: str) -> str:
    """Deterministic fake API key for a session, built at runtime (never a source literal)."""
    body = uuid.uuid5(uuid.NAMESPACE_URL, "ccmc-sim-key/" + session_id).hex
    body += uuid.uuid5(uuid.NAMESPACE_URL, "ccmc-sim-key2/" + session_id).hex
    return "sk-" + "ant-" + "api03-" + body


# ---------------------------------------------------------------------------
# HTTP


class Poster:
    def __init__(self, port: int, host: str = "127.0.0.1") -> None:
        self.base = f"http://{host}:{port}"
        self.sent = 0
        self._lock = threading.Lock()
        # never route loopback through an HTTP proxy from the environment
        self._opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))

    def check(self) -> None:
        try:
            with self._opener.open(self.base + "/health", timeout=2) as r:
                info = json.loads(r.read().decode("utf-8", errors="replace"))
        except (urllib.error.URLError, OSError, ValueError) as exc:
            raise ServerUnreachable(
                f"Cannot reach cc-mission-control at {self.base} ({getattr(exc, 'reason', exc)}). "
                "Start it first: python -m cc_mission_control (or pass --port)."
            ) from None
        if not isinstance(info, dict) or info.get("app") != APP_ID:
            raise ServerUnreachable(f"Something is listening on {self.base} but it is not {APP_ID}.")

    def post(self, event: dict) -> None:
        req = urllib.request.Request(
            self.base + "/hook",
            data=json.dumps(event).encode("utf-8"),
            headers={"Content-Type": "application/json"},
            method="POST",
        )
        try:
            with self._opener.open(req, timeout=5) as r:
                r.read()
        except urllib.error.HTTPError:
            pass  # the server answers 200 to everything; anything else is not fatal for a demo
        except (urllib.error.URLError, OSError) as exc:
            raise ServerUnreachable(f"Lost connection to {self.base} ({getattr(exc, 'reason', exc)}).") from None
        with self._lock:
            self.sent += 1


# ---------------------------------------------------------------------------
# Transcript files

HEX = "0123456789abcdef"
ALNUM = "ABCDEFGHJKLMNPQRSTUVWXYZabcdefghijkmnopqrstuvwxyz0123456789"
BRANCHES = ["main", "feat/velocity-check", "fix/webhook-retries", "perf/batch-upserts"]
# Events the hooks docs show `permission_mode` on.
PERMISSION_EVENTS = {
    "PreToolUse", "PostToolUse", "PostToolUseFailure", "UserPromptSubmit",
    "Stop", "SubagentStop", "Notification", "TaskCompleted",
}


def _iso(t: Optional[float] = None) -> str:
    return datetime.fromtimestamp(time.time() if t is None else t, timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%f")[:-3] + "Z"


def tool_id(rng: random.Random) -> str:
    return "toolu_" + "".join(rng.choice(ALNUM) for _ in range(22))


def split_usage(rng: random.Random, total: int) -> Dict[str, Any]:
    """Split a context total across the four usage counters (they sum to `total`)."""
    out = rng.randint(120, max(121, min(1500, total // 4)))
    inp = rng.randint(1, 30)
    create = rng.randint(400, max(401, min(6000, total // 3)))
    read = max(0, total - out - inp - create)
    return {
        "input_tokens": inp,
        "cache_creation_input_tokens": create,
        "cache_read_input_tokens": read,
        "output_tokens": out,
        "service_tier": "standard",
        "cache_creation": {"ephemeral_5m_input_tokens": create, "ephemeral_1h_input_tokens": 0},
    }


class TranscriptWriter:
    def __init__(
        self,
        path: Path,
        rng: random.Random,
        session_id: str,
        cwd: str,
        model: str,
        branch: str,
        sidechain: bool = False,
        agent_id: Optional[str] = None,
    ) -> None:
        self.path, self.rng, self.session_id, self.cwd = path, rng, session_id, cwd
        self.model, self.branch, self.sidechain, self.agent_id = model, branch, sidechain, agent_id
        self._parent: Optional[str] = None
        path.parent.mkdir(parents=True, exist_ok=True)
        path.touch()

    def _uuid(self) -> str:
        return str(uuid.UUID(int=self.rng.getrandbits(128), version=4))

    def _write(self, entry: dict) -> None:
        line = json.dumps(entry, separators=(",", ":")) + "\n"
        with open(self.path, "a", encoding="utf-8") as f:
            f.write(line)  # one write per line so the tailer never sees a torn entry

    def _append(self, entry: dict) -> None:
        entry.update(
            uuid=self._uuid(), parentUuid=self._parent, sessionId=self.session_id, cwd=self.cwd,
            version="2.1.0", gitBranch=self.branch, userType="external", entrypoint="cli", timestamp=_iso(),
        )
        if self.agent_id:
            entry["agentId"] = self.agent_id
        self._parent = entry["uuid"]
        self._write(entry)

    def record(self, rtype: str, **fields: Any) -> None:
        """Non-message records (system notes, file-history snapshots)."""
        self._write({"type": rtype, "sessionId": self.session_id, "uuid": self._uuid(), "timestamp": _iso(), **fields})

    def user(self, text: str) -> None:
        self._append({"type": "user", "isSidechain": self.sidechain,
                      "message": {"role": "user", "content": text}})

    def turn(self, total_tokens: int, text: str = "", tool: Optional[Tuple[str, str, dict]] = None,
             sidechain: Optional[bool] = None) -> None:
        """One model response: a separate entry per content block, sharing message.id and usage."""
        message_id = "msg_" + "".join(self.rng.choice(ALNUM) for _ in range(24))
        request_id = "req_" + "".join(self.rng.choice(ALNUM) for _ in range(24))
        usage = split_usage(self.rng, max(1000, int(total_tokens)))
        blocks: List[dict] = [{"type": "text", "text": text or "Working on it."}]
        if tool:
            blocks.append({"type": "tool_use", "id": tool[0], "name": tool[1], "input": tool[2]})
        for idx, block in enumerate(blocks):
            last = idx == len(blocks) - 1
            self._append(
                {
                    "type": "assistant",
                    "isSidechain": self.sidechain if sidechain is None else sidechain,
                    "requestId": request_id,
                    "apiBlockIndex": idx,
                    "message": {
                        "id": message_id, "type": "message", "role": "assistant", "model": self.model,
                        "content": [block],
                        "stop_reason": ("tool_use" if tool else "end_turn") if last else None,
                        "usage": dict(usage),
                    },
                }
            )


# ---------------------------------------------------------------------------
# Scenario


class SubagentPlan:
    """Everything random about one subagent, drawn in the main thread so --seed is reproducible."""

    def __init__(self, agent_type: str, description: str, spawn_id: str, hex_id: str, prefixed: bool,
                 rng: random.Random) -> None:
        self.agent_type, self.description, self.spawn_id, self.hex_id = agent_type, description, spawn_id, hex_id
        self.agent_id = ("agent-" + hex_id) if prefixed else hex_id  # as it appears in hook payloads
        self.rng = rng
        self.writer: Optional[TranscriptWriter] = None
        self.steps: List[dict] = []
        self.start_tokens = 0.0
        self.final_tokens = 0.0
        self.final_think = 0.0
        self.summary = ""
        self.launch_ms = 0
        self.stagger = 0.3
        self.spawn_started = 0.0
        self.thread: Optional[threading.Thread] = None


class SessionSim:
    def __init__(
        self,
        index: int,
        poster: Poster,
        rng: random.Random,
        speed: float,
        agents: int,
        failures: bool,
        transcript_dir: Path,
        keep_open: bool,
        quiet: bool,
        settle: float,
        stop: threading.Event,
        model: str = MAIN_MODEL,
        foreground: bool = False,
        prefixed_ids: bool = False,
    ) -> None:
        self.poster, self.rng, self.speed = poster, rng, max(speed, 1e-6)
        self.n_agents, self.failures, self.keep_open = agents, failures, keep_open
        self.quiet, self.settle, self.stop = quiet, settle, stop
        self.model, self.foreground, self.prefixed_ids = model, foreground, prefixed_ids
        self.session_id = str(uuid.UUID(int=rng.getrandbits(128), version=4))
        self.title, self.prompt, self.files = PROJECTS[index % len(PROJECTS)]
        self.cwd = f"/Users/dev/work/{self.title}"
        self.branch = BRANCHES[index % len(BRANCHES)]
        self.index = index
        self.root = transcript_dir
        self.transcript = transcript_dir / f"{self.session_id}.jsonl"
        self.session_dir = transcript_dir / self.session_id
        self.main: Optional[TranscriptWriter] = None
        self.plans: List[SubagentPlan] = []
        self.key = fake_key(self.session_id)
        self.errors: List[BaseException] = []

    # ---- plumbing ---------------------------------------------------------

    def nap(self, seconds: float) -> None:
        """Simulated time: scaled by --speed and interruptible."""
        if self.stop.wait(max(0.0, seconds) / self.speed):
            raise Stopped()

    def wait_real(self, seconds: float) -> None:
        if self.stop.wait(max(0.0, seconds)):
            raise Stopped()

    def log(self, text: str) -> None:
        if not self.quiet:
            print(f"[{self.title}] {text}", flush=True)

    def base(self, name: str, plan: Optional[SubagentPlan] = None) -> dict:
        e = {"session_id": self.session_id, "transcript_path": str(self.transcript), "cwd": self.cwd}
        if name in PERMISSION_EVENTS:
            e["permission_mode"] = "default"
        e["hook_event_name"] = name
        if plan is not None:
            e["agent_id"] = plan.agent_id
            e["agent_type"] = plan.agent_type
        return e

    def jitter(self, rng: random.Random, value: float, pct: float = 0.03) -> int:
        return int(value * (1 + rng.uniform(-pct, pct)))

    # ---- tool calls -------------------------------------------------------

    def make_call(self, rng: random.Random, tool: str, force: Optional[str] = None) -> dict:
        """Pick realistic input/response/duration for one tool call (all randomness from `rng`)."""
        f = f"{self.cwd}/{rng.choice(self.files)}"
        tid = tool_id(rng)
        if tool == "Read":
            lines = rng.randint(40, 400)
            return dict(tid=tid, tool=tool, input={"file_path": f}, dur=rng.uniform(0.4, 1.2),
                        response={"type": "text", "file": {
                            "filePath": f, "content": "import logging\n\nlog = logging.getLogger(__name__)\n...",
                            "numLines": lines, "startLine": 1, "totalLines": lines}})
        if tool == "Grep":
            pat = rng.choice(["velocity", "def score", "retry", "idempotency", "class .*Config", "batch_size"])
            hits = [f"{self.cwd}/{p}" for p in rng.sample(self.files, 3)]
            return dict(tid=tid, tool=tool, input={"pattern": pat, "path": "src", "output_mode": "files_with_matches"},
                        dur=rng.uniform(0.6, 1.8),
                        response={"mode": "files_with_matches", "filenames": hits, "numFiles": len(hits)})
        if tool == "Glob":
            names = [f"{self.cwd}/{p}" for p in self.files]
            return dict(tid=tid, tool=tool, input={"pattern": "**/*.py"}, dur=rng.uniform(0.3, 0.8),
                        response={"filenames": names, "durationMs": rng.randint(8, 60), "numFiles": len(names),
                                  "truncated": False})
        if tool == "Edit":
            old, new = "window = 60", "window = settings.velocity_window"
            return dict(tid=tid, tool=tool, input={"file_path": f, "old_string": old, "new_string": new},
                        dur=rng.uniform(0.5, 1.5),
                        response={"filePath": f, "oldString": old, "newString": new,
                                  "originalFile": "def check(events):\n    window = 60\n    return len(events) < 5\n",
                                  "structuredPatch": [{"oldStart": 2, "oldLines": 1, "newStart": 2, "newLines": 1,
                                                       "lines": ["-    " + old, "+    " + new]}],
                                  "userModified": False, "replaceAll": False})
        cmd = force or rng.choice(["git status --short", "git diff --stat", "ruff check .", "pytest -q tests/"])
        out = "39 passed in 2.31s" if cmd.startswith("pytest") else "ok"
        return dict(tid=tid, tool="Bash", input={"command": cmd, "description": "Run " + cmd.split()[0]},
                    dur=rng.uniform(1.0, 3.5) if cmd.startswith("pytest") else rng.uniform(0.4, 1.4),
                    response={"stdout": out, "stderr": "", "interrupted": False, "isImage": False})

    def run_call(self, call: dict, plan: Optional[SubagentPlan] = None, fail: bool = False) -> None:
        """Pre, simulated duration, then Post (or PostToolUseFailure)."""
        pre = self.base("PreToolUse", plan)
        pre.update(tool_name=call["tool"], tool_input=call["input"], tool_use_id=call["tid"])
        self.poster.post(pre)
        self.nap(call["dur"])
        ms = int(call["dur"] * 1000)
        if fail:
            e = self.base("PostToolUseFailure", plan)
            e.update(tool_name=call["tool"], tool_input=call["input"], tool_use_id=call["tid"], duration_ms=ms,
                     error=call["error"], is_interrupt=False)
        else:
            e = self.base("PostToolUse", plan)
            e.update(tool_name=call["tool"], tool_input=call["input"], tool_use_id=call["tid"], duration_ms=ms,
                     tool_response=call["response"])
        self.poster.post(e)

    # ---- main agent -------------------------------------------------------

    def run(self) -> None:
        started = False
        try:
            self.main = TranscriptWriter(self.transcript, self.rng, self.session_id, self.cwd, self.model, self.branch)
            self.main.record("file-history-snapshot", messageId=self.main._uuid(), isSnapshotUpdate=False,
                             snapshot={"trackedFileBackups": {}})
            self.main.user(self.prompt)
            self.log(f"session {self.session_id[:8]} starting")
            started = True
            ss = self.base("SessionStart")
            ss.update(source="startup", model=self.model)
            self.poster.post(ss)
            self.nap(1.0)
            up = self.base("UserPromptSubmit")
            up.update(prompt=self.prompt)
            self.poster.post(up)
            self.nap(1.5)
            self.scenario()
            self.wait_real(self.settle)  # let the server's transcript watcher read the final files
            if self.failures:
                sf = self.base("StopFailure")
                sf.update(error="rate_limit", error_details="429 Too Many Requests",
                          last_assistant_message="API Error: Rate limit reached")
                self.poster.post(sf)
                self.log("turn failed (rate_limit)")
            else:
                st = self.base("Stop")
                st.update(stop_hook_active=False, last_assistant_message="Done. The change is in and the tests pass.")
                self.poster.post(st)
                self.log("turn finished")
            self.nap(0.5)
        except Stopped:
            pass
        except BaseException as exc:  # noqa: BLE001 - reported by run()
            self.errors.append(exc)
        finally:
            if started and not self.keep_open:
                try:
                    end = self.base("SessionEnd")
                    end.update(reason="other")
                    self.poster.post(end)
                    self.log("session ended")
                except ServerUnreachable:
                    pass

    def turn(self, tokens: float, text: str = "", call: Optional[dict] = None) -> None:
        assert self.main is not None
        tool = (call["tid"], call["tool"], call["input"]) if call else None
        self.main.turn(self.jitter(self.rng, tokens), text, tool)

    def main_call(self, tokens: float, text: str, call: dict, **kw: Any) -> None:
        self.turn(tokens, text, call)
        self.run_call(call, None, **kw)
        self.nap(self.rng.uniform(1.0, 2.0))

    def scenario(self) -> None:
        assert self.main is not None
        rng, mk = self.rng, self.make_call
        self.main_call(22_000, "I'll start by reading the code involved.", mk(rng, "Read"))
        self.main_call(27_000, "Let me find where this is used.", mk(rng, "Grep"))
        # redaction demo: a fake credential, built at runtime
        smoke = mk(rng, "Bash", f'curl -s -H "x-api-key: {self.key}" http://localhost:8000/health')
        smoke["dur"] = 1.2
        smoke["response"]["stdout"] = '{"status":"ok"}'
        self.main_call(31_000, "Checking the local service is up.", smoke)
        self.main.record("system", subtype="informational", content="Running tests", level="info")
        self.main_call(36_000, "Running the existing tests for a baseline.", mk(rng, "Bash", "pytest -q tests/"))

        # a permission prompt before the first edit
        edit = mk(rng, "Edit")
        self.turn(41_000, "I need to edit a file.", edit)
        note = self.base("Notification")
        note.update(notification_type="permission_prompt", title="Permission needed",
                    message="Claude needs your permission to use Edit")
        self.poster.post(note)
        self.nap(rng.uniform(3.0, 5.0))
        self.run_call(edit)
        self.nap(1.0)

        self.spawn_subagents()

        edit = mk(rng, "Edit")
        self.turn(128_000, "Both subagents are back; applying their findings.", edit)
        self.run_call(edit)
        self.nap(1.0)
        self.turn(150_000, "Running the full suite again.")
        self.compact()
        # after compaction the context starts small again
        if self.failures:
            call = mk(rng, "Bash", "pytest -q tests/")
            call["error"] = ("Exit code 1\n=== FAILURES ===\n_____ test_velocity_window _____\n"
                             "E   AssertionError: assert 4 == 5\n1 failed, 38 passed in 2.41s")
            call["dur"] = 2.4
            self.main_call(41_000, "Re-running tests after the summary.", call, fail=True)
            self.main_call(46_000, "One test fails; fixing the off-by-one.", mk(rng, "Edit"))
        else:
            self.main_call(41_000, "Re-running tests after the summary.", mk(rng, "Bash", "pytest -q tests/"))
        self.main_call(54_000, "Final check of the diff.", mk(rng, "Bash", "git diff --stat"))
        self.main.record("system", subtype="turn_duration", durationMs=61000)
        self.turn(58_000, "All set. The velocity check is implemented, wired into scoring, and tested.")
        # NOTE: real main-session files do not contain isSidechain entries. This one exists only to
        # exercise the server's skip rule (SPEC 5.2); a correct gauge ignores its 190k tokens.
        self.main.turn(190_000, "(sidechain)", sidechain=True)

    def compact(self) -> None:
        pre = self.base("PreCompact")
        pre.update(trigger="auto", custom_instructions=None)
        self.poster.post(pre)
        self.log("compacting context")
        self.nap(3.0)
        post = self.base("PostCompact")
        post.update(trigger="auto", compact_summary="Summary of earlier work on the task.")
        self.poster.post(post)
        self.nap(0.8)

    # ---- subagents --------------------------------------------------------

    def plan_subagents(self) -> None:
        """Draw every random choice for every subagent here, in the main thread."""
        rng, used = self.rng, set()
        for i in range(self.n_agents):
            # the first two share a type, so FIFO matching on SubagentStart gets them wrong
            # when they start out of order; only the meta file can put that right
            atype = "general-purpose" if (self.n_agents >= 2 and i < 2) else rng.choice(SUBAGENT_TYPES)
            pool = [d for d in DESCRIPTIONS[atype] if d not in used]
            desc = rng.choice(pool) if pool else f"{rng.choice(DESCRIPTIONS[atype])} (part {i + 1})"
            used.add(desc)
            plan = SubagentPlan(atype, desc, tool_id(rng), "%017x" % rng.getrandbits(68), self.prefixed_ids,
                                random.Random(rng.getrandbits(64)))
            self._plan_steps(plan)
            self.plans.append(plan)

    def _plan_steps(self, plan: SubagentPlan) -> None:
        r = plan.rng
        n = r.randint(3, 8)
        plan.start_tokens = r.uniform(15_000, 18_000)
        plan.final_tokens = r.uniform(45_000, 90_000)
        tools = SUB_TOOLS[plan.agent_type]
        fail_at = r.randrange(n) if (self.failures and not self.plans) else -1
        for k in range(n):
            tool = tools[k % len(tools)]
            fail = k == fail_at
            call = self.make_call(r, "Bash" if fail else tool, "pytest -q tests/ -k velocity" if fail else None)
            call["dur"] *= r.uniform(1.0, 2.5)
            if fail:
                call["error"] = "Exit code 1\nFAILED tests/test_scoring.py::test_velocity_window - assert 4 == 5"
            plan.steps.append(dict(
                think=r.uniform(0.6, 1.5), call=call, fail=fail, text=f"Step {k + 1}: using {call['tool']}.",
                tokens=self.jitter(r, plan.start_tokens + (plan.final_tokens - plan.start_tokens) * (k + 1) / (n + 1)),
            ))
        plan.final_think = r.uniform(0.6, 1.2)
        plan.final_tokens = self.jitter(r, plan.final_tokens)
        plan.summary = f"{plan.description}: done. Found and handled the relevant code in {r.randint(2, 5)} files."
        plan.launch_ms = r.randint(40, 400)
        plan.stagger = r.uniform(0.2, 0.5)

    def spawn_input(self, plan: SubagentPlan) -> dict:
        return {"description": plan.description, "subagent_type": plan.agent_type,
                "prompt": f"{plan.description}. Report back with a short summary."}

    def spawn_subagents(self) -> None:
        if self.n_agents <= 0:
            return
        self.plan_subagents()
        self.turn(48_000, "This is parallelizable; delegating to subagents.")
        for plan in self.plans:
            pre = self.base("PreToolUse")
            pre.update(tool_name="Agent", tool_use_id=plan.spawn_id, tool_input=self.spawn_input(plan))
            self.poster.post(pre)
            plan.spawn_started = time.monotonic()
            self.nap(0.3)
        # transcript and meta files exist before SubagentStart, as in real Claude Code
        sub_dir = self.session_dir / "subagents"
        sub_dir.mkdir(parents=True, exist_ok=True)
        for plan in self.plans:
            plan.writer = TranscriptWriter(sub_dir / f"agent-{plan.hex_id}.jsonl", plan.rng, self.session_id,
                                           self.cwd, SUB_MODEL, self.branch, sidechain=True, agent_id=plan.hex_id)
            plan.writer.user(plan.description)
            plan.writer.turn(self.jitter(plan.rng, plan.start_tokens), "Starting the assigned task.")
            meta = {"agentType": plan.agent_type, "description": plan.description, "toolUseId": plan.spawn_id,
                    "spawnDepth": 1, "requestShape": "foreground" if self.foreground else "background",
                    "model": SUB_MODEL}
            (sub_dir / f"agent-{plan.hex_id}.meta.json").write_text(json.dumps(meta), encoding="utf-8")
        order = list(range(len(self.plans)))
        if len(order) >= 2:
            order[0], order[1] = order[1], order[0]  # deliberately out of spawn order
        for i in order:
            plan = self.plans[i]
            self.poster.post(self.base("SubagentStart", plan))
            self.log(f"subagent {plan.agent_type}: {plan.description}")
            if not self.foreground:
                # background (default since Claude Code 2.1.198): the Agent call returns at once
                post = self.base("PostToolUse")
                post.update(
                    tool_name="Agent", tool_use_id=plan.spawn_id, tool_input=self.spawn_input(plan),
                    tool_response={"status": "async_launched", "agentId": plan.hex_id,
                                   "description": plan.description, "prompt": self.spawn_input(plan)["prompt"],
                                   "outputFile": str(self.root / "tasks" / f"{plan.hex_id}.output"),
                                   "resolvedModel": SUB_MODEL},
                    duration_ms=plan.launch_ms)
                self.poster.post(post)
            plan.thread = threading.Thread(target=self.run_subagent, args=(plan,), daemon=True, name=f"sub-{plan.hex_id}")
            plan.thread.start()
            self.nap(plan.stagger)
        for plan in self.plans:
            while plan.thread is not None and plan.thread.is_alive():
                plan.thread.join(0.1)
                if self.stop.is_set():
                    raise Stopped()
        if self.errors:
            raise self.errors[0]

    def run_subagent(self, plan: SubagentPlan) -> None:
        try:
            self._run_subagent(plan)
        except Stopped:
            pass
        except BaseException as exc:  # noqa: BLE001
            self.errors.append(exc)

    def _run_subagent(self, plan: SubagentPlan) -> None:
        w = plan.writer
        assert w is not None
        for step in plan.steps:
            self.nap(step["think"])  # model thinking
            call = step["call"]
            w.turn(step["tokens"], step["text"], (call["tid"], call["tool"], call["input"]))
            self.run_call(call, plan, fail=step["fail"])
        self.nap(plan.final_think)
        w.turn(plan.final_tokens, f"Finished: {plan.description}.")
        stop = self.base("SubagentStop", plan)
        stop.update(stop_hook_active=False, last_assistant_message=plan.summary, agent_transcript_path=str(w.path))
        self.poster.post(stop)
        if not self.foreground:
            return
        self.nap(0.2)
        elapsed_ms = int((time.monotonic() - plan.spawn_started) * self.speed * 1000)
        total = int(plan.final_tokens)
        post = self.base("PostToolUse")
        post.update(
            tool_name="Agent", tool_use_id=plan.spawn_id, tool_input=self.spawn_input(plan),
            tool_response={
                "status": "completed", "agentId": plan.hex_id, "agentType": plan.agent_type,
                "content": [{"type": "text", "text": plan.summary}], "resolvedModel": SUB_MODEL,
                "totalTokens": total, "totalDurationMs": elapsed_ms, "totalToolUseCount": len(plan.steps),
                "usage": {"input_tokens": 12, "output_tokens": max(300, total // 40),
                          "cache_creation_input_tokens": 4000, "cache_read_input_tokens": max(0, total - 4300),
                          "service_tier": "standard"},
            },
            duration_ms=elapsed_ms)
        self.poster.post(post)


# ---------------------------------------------------------------------------
# Public API


def run(
    port: int = DEFAULT_PORT,
    agents: int = 2,
    speed: float = 1.0,
    failures: bool = False,
    sessions: int = 1,
    transcript_dir: Optional[str] = None,
    seed: Optional[int] = None,
    keep_open: bool = False,
    quiet: bool = True,
    settle: float = 1.0,
    stop: Optional[threading.Event] = None,
    poster: Optional[Poster] = None,
    model: str = MAIN_MODEL,
    foreground: bool = False,
    prefixed_ids: bool = False,
) -> List[str]:
    """Run `sessions` concurrent fake sessions to completion. Returns their session ids.

    Raises ServerUnreachable if the server is down. `settle` is real seconds waited before the
    final Stop so the server's transcript watcher (150 ms poll) has read the last files.
    With a fixed `seed` the same events (names, ids, per-agent order) are produced every run.
    """
    poster = poster or Poster(port)
    poster.check()
    stop = stop or threading.Event()
    root = Path(transcript_dir) if transcript_dir else Path(tempfile.mkdtemp(prefix="ccmc-sim-"))
    root.mkdir(parents=True, exist_ok=True)
    master = random.Random(seed)
    sims = [
        SessionSim(i, poster, random.Random(master.getrandbits(64)), speed, max(0, agents), failures, root,
                   keep_open, quiet, settle, stop, model, foreground, prefixed_ids)
        for i in range(max(1, sessions))
    ]
    threads: List[threading.Thread] = []
    for i, sim in enumerate(sims):
        t = threading.Thread(target=_staggered, args=(sim, i * 2.0), daemon=True, name=f"session-{i}")
        t.start()
        threads.append(t)
    try:
        while any(t.is_alive() for t in threads):
            for t in threads:
                t.join(0.1)
    except KeyboardInterrupt:
        stop.set()
        for t in threads:
            t.join(3.0)
        raise
    for sim in sims:
        if sim.errors:
            raise sim.errors[0]
    return [s.session_id for s in sims]


def _staggered(sim: SessionSim, delay: float) -> None:
    try:
        sim.nap(delay)
    except Stopped:
        return
    sim.run()


def _port(value: str) -> int:
    try:
        port = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError(f"invalid port {value!r} (check CCMC_PORT)") from None
    if not 1 <= port <= 65535:
        raise argparse.ArgumentTypeError(f"port {port} out of range")
    return port


def main(argv: Optional[List[str]] = None) -> int:
    ap = argparse.ArgumentParser(description="Post a fake Claude Code session to a running cc-mission-control.")
    ap.add_argument("--port", type=_port, default=os.environ.get("CCMC_PORT") or str(DEFAULT_PORT),
                    help="server port (default: CCMC_PORT or 4317)")
    ap.add_argument("--agents", type=int, default=2, help="parallel subagents per session (default 2)")
    ap.add_argument("--speed", type=float, default=1.0, help="time multiplier (10 = ten times faster)")
    ap.add_argument("--failures", action="store_true", help="inject a failing Bash call and a StopFailure")
    ap.add_argument("--sessions", type=int, default=1, help="concurrent independent sessions")
    ap.add_argument("--transcript-dir", default=None, help="where fake transcripts go (default: a new temp dir)")
    ap.add_argument("--seed", type=int, default=None)
    ap.add_argument("--keep-open", action="store_true", help="do not send SessionEnd")
    ap.add_argument("--loop", action="store_true", help="repeat until Ctrl-C")
    ap.add_argument("--quiet", action="store_true")
    ap.add_argument("--foreground", action="store_true",
                    help="old subagent flow: the Agent call completes after SubagentStop")
    ap.add_argument("--model", default=MAIN_MODEL, help=f"main agent model (default {MAIN_MODEL})")
    ap.add_argument("--1m", dest="one_m", action="store_true", help="main model gets a [1m] suffix (1,000,000 window)")
    ap.add_argument("--prefixed-agent-ids", action="store_true",
                    help='send agent_id as "agent-<hex>" (the docs show both forms)')
    args = ap.parse_args(argv)
    if args.speed <= 0 or args.agents < 0 or args.sessions < 1:
        ap.error("--speed must be > 0, --agents >= 0, --sessions >= 1")
    model = args.model + ("[1m]" if args.one_m and "1m" not in args.model.lower() else "")
    transcript_dir = args.transcript_dir or tempfile.mkdtemp(prefix="ccmc-sim-")
    poster = Poster(args.port)
    t0 = time.monotonic()
    ids: List[str] = []
    rounds = 0
    try:
        while True:
            seed = None if args.seed is None else args.seed + rounds
            ids = run(args.port, args.agents, args.speed, args.failures, args.sessions, transcript_dir, seed,
                      args.keep_open, args.quiet, poster=poster, model=model, foreground=args.foreground,
                      prefixed_ids=args.prefixed_agent_ids)
            rounds += 1
            if not args.loop:
                break
            if not args.quiet:
                print(f"round {rounds} done; starting another (Ctrl-C to stop)", flush=True)
    except ServerUnreachable as exc:
        print(f"simulate: {exc}", file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("\nsimulate: interrupted; stopping.", file=sys.stderr, flush=True)
        return 130
    if not args.quiet:
        total = rounds * args.sessions
        print(
            f"simulate: {total} session(s) x {args.agents} subagent(s), {poster.sent} events in "
            f"{time.monotonic() - t0:.1f}s. Dashboard: http://127.0.0.1:{args.port}/ "
            f"Transcripts: {transcript_dir}",
            flush=True,
        )
        for sid in ids:
            print(f"  session {sid}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())

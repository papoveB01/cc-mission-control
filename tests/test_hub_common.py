"""Shared helpers for the hub tests (no tests in here)."""

from __future__ import annotations

import uuid
from pathlib import Path

from fastapi.testclient import TestClient

from cc_mission_control.config import Config, HubConfig
from cc_mission_control.hub.app import create_app
from cc_mission_control.state import Store

TOKEN = "hub" + "-test-" + "k" * 40
AUTH = {"Authorization": "Bearer " + TOKEN}
SID = "sess-1"
MACHINE = "mach-a"
CWD = "/work/proj"


def hub_config(tmp_path: Path, **kw) -> HubConfig:
    base = dict(token=TOKEN, db=tmp_path / "data" / "history.db", hosts=("testserver",))
    base.update(kw)
    return HubConfig(**base)


def make_app(tmp_path: Path, **kw):
    return create_app(hub_config(tmp_path, **kw), store_config=Config())


def make_client(tmp_path: Path, **kw) -> TestClient:
    return TestClient(make_app(tmp_path, **kw))


def env(kind: str, t: float, data: dict, sid: str = SID, machine: str = MACHINE, user: str = "alice", host: str = "box") -> dict:
    return {"v": 2, "eid": uuid.uuid4().hex, "machine_id": machine, "user": user, "host": host,
            "t": t, "kind": kind, "session_id": sid, "data": data}


def hook(t: float, name: str, sid: str = SID, **fields) -> tuple:
    return ("hook", t, {"hook_event_name": name, "session_id": sid, "cwd": CWD, **fields})


def scenario(sid: str = SID) -> list:
    """(kind, t, data) tuples for a realistic session, in time order with distinct t."""
    ev = [
        hook(100, "SessionStart", sid, source="startup", model="claude-sonnet-4-5"),
        hook(101, "UserPromptSubmit", sid, prompt="fix the parser bug in a.py"),
        hook(102, "PreToolUse", sid, tool_name="Read", tool_use_id="t1", tool_input={"file_path": CWD + "/a.py"}),
        hook(103, "PostToolUse", sid, tool_name="Read", tool_use_id="t1", tool_response={"output": "x"}, duration_ms=5),
        hook(104, "PreToolUse", sid, tool_name="Edit", tool_use_id="t2", tool_input={"file_path": CWD + "/b.py"}),
        hook(105, "PostToolUse", sid, tool_name="Edit", tool_use_id="t2", tool_response={"output": "ok"}),
        hook(106, "PreToolUse", sid, tool_name="Write", tool_use_id="t3", tool_input={"file_path": CWD + "/c.py"}),
        hook(107, "PostToolUseFailure", sid, tool_name="Write", tool_use_id="t3", error="disk full"),
        hook(108, "PreToolUse", sid, tool_name="Grep", tool_use_id="t4", tool_input={"pattern": "foo"}),
        hook(109, "PostToolUse", sid, tool_name="Grep", tool_use_id="t4", tool_response={"output": "hits"}),
        hook(110, "PreToolUse", sid, tool_name="Agent", tool_use_id="t5",
             tool_input={"subagent_type": "Explore", "description": "look around"}),
        hook(111, "SubagentStart", sid, agent_id="ag1", agent_type="Explore"),
        ("meta", 111.5, {"agent_id": "ag1", "agent_type": "Explore", "description": "look around", "tool_use_id": "t5"}),
        hook(112, "PreToolUse", sid, tool_name="Read", tool_use_id="t6", agent_id="ag1", agent_type="Explore",
             tool_input={"file_path": CWD + "/d.py"}),
        hook(113, "PostToolUse", sid, tool_name="Read", tool_use_id="t6", agent_id="ag1", agent_type="Explore",
             tool_response={"output": "d"}),
        ("ctx", 113.5, {"agent_id": "main", "tokens": 12000, "model": "claude-sonnet-4-5"}),
        ("ctx", 113.6, {"agent_id": "ag1", "tokens": 5000, "model": "claude-haiku-4-5"}),
        ("usage", 113.7, {"agent_id": "main", "message_id": "m1", "model": "x", "input": 100, "cache_creation": 10, "cache_read": 20, "output": 30}),
        ("usage", 113.8, {"agent_id": "main", "message_id": "m1", "model": "x", "input": 100, "cache_creation": 10, "cache_read": 20, "output": 30}),
        ("usage", 113.9, {"agent_id": "ag1", "message_id": "m2", "model": "y", "input": 50, "cache_creation": 0, "cache_read": 5, "output": 7}),
        ("usage", 114.0, {"agent_id": "ag1", "message_id": None, "model": "y", "input": 1, "cache_creation": 1, "cache_read": 1, "output": 1}),
        hook(114.5, "Notification", sid, notification_type="permission_prompt", message="Claude needs permission to use Bash"),
        hook(115, "PreToolUse", sid, tool_name="Bash", tool_use_id="t7", tool_input={"command": "rm -rf build"}),
        hook(116, "PermissionDenied", sid, tool_name="Bash", tool_use_id="t7", reason="not allowed"),
        ("title", 117, {"source": "generated", "title": "Parser fixes"}),
        ("cwd", 117.5, {"cwd": CWD}),
        hook(118, "SubagentStop", sid, agent_id="ag1", agent_type="Explore", last_assistant_message="found it"),
        hook(119, "Stop", sid),
        ("title", 120, {"source": "custom", "title": "My parser session"}),
        hook(121, "SessionEnd", sid, reason="clear"),
    ]
    return ev


def envelopes(events: list, sid: str = SID, **kw) -> list:
    return [env(k, t, d, sid=sid, **kw) for k, t, d in events]


def live_store(events: list) -> Store:
    """The reference: feed the same events to a live Store with a clock that follows each record."""
    now = [0.0]
    store = Store(Config(), clock=lambda: now[0])
    for kind, t, data in events:
        now[0] = t
        if kind == "hook":
            store.ingest(dict(data))
        elif kind == "ctx":
            store.set_context(SID, data["agent_id"], data["tokens"], data["model"])
        elif kind == "meta":
            store.apply_subagent_meta(SID, data["agent_id"], data["description"], data["tool_use_id"])
        elif kind == "title":
            (store.set_custom_title if data["source"] == "custom" else store.set_ai_title)(SID, data["title"])
        elif kind == "cwd":
            store.set_project_cwd(SID, data["cwd"])
    return store


def post(client: TestClient, items, headers=AUTH):
    return client.post("/ingest", json=items, headers=headers)

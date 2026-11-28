"""Session replay through the live Store (SPEC 13.2), plus the indexes derived from it."""

from __future__ import annotations

import logging
from collections import OrderedDict
from typing import Any, Dict, List, Optional, Tuple

from ..config import Config
from ..pricing import Pricing
from ..report import TaskTracker, build_report
from ..state import MAIN, Store, _norm_agent

log = logging.getLogger(__name__)

Key = Tuple[float, float, int]  # (t, received, rowid): the replay order
CACHE_SIZE = 16
PROMPT_CHARS = 300
PROMPTS_MAX = 20_000
# tool -> (op, input keys tried in order)
FILE_TOOLS = {
    "Read": ("read", ("file_path",)),
    "NotebookRead": ("read", ("notebook_path", "file_path")),
    "Write": ("write", ("file_path",)),
    "Edit": ("edit", ("file_path",)),
    "MultiEdit": ("edit", ("file_path",)),
    "NotebookEdit": ("edit", ("notebook_path", "file_path")),
    "Glob": ("search", ("path",)),
    "Grep": ("search", ("path",)),
}
SEARCH_TOOLS = ("Glob", "Grep")


def replay_config(base: Config) -> Config:
    """Raise the per-agent call cap for replay unless CCMC_MAX_CALLS was set explicitly."""
    import os
    from dataclasses import replace

    return base if os.environ.get("CCMC_MAX_CALLS", "").strip() else replace(base, max_calls=5000)


class ReplayState:
    """One session's records fed, in order, into a fresh Store whose clock is each record's `t`."""

    def __init__(self, session_id: str, config: Config, pricing: Optional[Pricing] = None) -> None:
        self.session_id = session_id
        self.now = 0.0
        self.store = Store(config, clock=lambda: self.now, activity_max=config.max_calls, pricing=pricing)
        self.last_key: Optional[Key] = None
        self.user = ""
        self.host = ""
        self.peak_context = 0
        self.prompts: List[str] = []
        self._prompt_chars = 0
        self.permissions: List[tuple] = []  # (agent_id, call_id, tool, kind, text, t)
        self.tracker = TaskTracker(self.store.redactor, max(0, config.stale_minutes) * 60.0)  # task boundaries for the report (13.7)
        self._warned = False

    def apply(self, kind: str, t: float, data: Dict[str, Any], user: str, host: str, key: Key) -> None:
        sid, store = self.session_id, self.store
        self.now = t
        self.last_key = key
        self.user, self.host = user or self.user, host or self.host
        try:
            self._dispatch(kind, t, data)
        except Exception:  # one bad record must never make a session unviewable (no data in the log)
            if not self._warned:
                self._warned = True
                log.warning("skipping a record that failed to replay (kind=%s)", kind)

    def _dispatch(self, kind: str, t: float, data: Dict[str, Any]) -> None:
        sid, store = self.session_id, self.store
        if kind == "hook":
            event = dict(data, session_id=sid)
            store.ingest(event)
            self._note_hook(event, t)
            self.tracker.feed(event, t)
        elif kind == "ctx":
            tokens = data.get("tokens")
            store.set_context(sid, data.get("agent_id"), tokens, data.get("model"))
            if isinstance(tokens, int) and not isinstance(tokens, bool):
                self.peak_context = max(self.peak_context, tokens)
                got = store.agent_context(sid, data.get("agent_id"))
                if got is not None and got[0] == tokens:  # the Store applied it: note it for the open task (13.8)
                    self.tracker.note_context(data.get("agent_id"), t, tokens, got[1])
        elif kind == "usage":  # the same accounting as the live Store (SPEC 13.6)
            store.add_usage(sid, data.get("agent_id"), data)
        elif kind == "meta":
            store.apply_subagent_meta(sid, data.get("agent_id"), data.get("description"), data.get("tool_use_id"))
        elif kind == "title":
            title = data.get("title")
            (store.set_custom_title if data.get("source") == "custom" else store.set_ai_title)(sid, title)
        elif kind == "cwd":
            store.set_project_cwd(sid, data.get("cwd"))
        elif kind == "summary":  # recorded text only; the hub has no key and never calls the API
            body = store.add_summary(sid, data.get("agent_id"), data.get("t_start"), data.get("t_end"), data.get("text"), data.get("model"))
            if body:
                self.tracker.note_summary(data.get("agent_id"), data.get("t_start"), data.get("t_end"), body)

    def _note_hook(self, e: Dict[str, Any], t: float) -> None:
        name, redactor = e.get("hook_event_name"), self.store.redactor
        agent = _norm_agent(e.get("agent_id"))[:128] or MAIN
        if name == "UserPromptSubmit" and isinstance(e.get("prompt"), str):
            text = redactor.redact(e["prompt"].strip())[:PROMPT_CHARS]
            if text and not text.startswith("<") and self._prompt_chars < PROMPTS_MAX:
                self.prompts.append(text)
                self._prompt_chars += len(text)
        elif name == "PermissionDenied":
            reason = e.get("reason") if isinstance(e.get("reason"), str) else ""
            tool = str(e.get("tool_name") or "")[:200]
            self.permissions.append((agent, str(e.get("tool_use_id") or ""), tool, "denied", redactor.text(reason), t))
        elif name == "Notification" and e.get("notification_type") == "permission_prompt":
            msg = e.get("message") if isinstance(e.get("message"), str) else ""
            self.permissions.append((agent, "", "", "prompt", redactor.text(msg), t))

    def report(self) -> Tuple[List[dict], List[dict]]:
        """(tasks, failures) of the replayed session (SPEC 13.7)."""
        return build_report(self.tracker, self.store, self.session_id, self.permissions)

    def call_ids(self) -> set:
        """Ids of every call the Store still holds (a failure row for one of these can be judged stale)."""
        return {c.id for c in self.store.session_calls(self.session_id)}

    def file_rows(self, call_task: Optional[Dict[str, str]] = None) -> List[tuple]:
        """(agent_id, call_id, path, op, tool, status, t, task_id) for every kept file-touching call."""
        call_task = call_task or {}
        sd = self.store.session_dict(self.session_id)
        cwd = sd["cwd"] if sd else ""
        rows = []
        for call in self.store.session_calls(self.session_id):
            spec = FILE_TOOLS.get(call.tool)
            if spec is None or not isinstance(call.input, dict):
                continue
            op, keys = spec
            path = next((call.input[k] for k in keys if isinstance(call.input.get(k), str) and call.input[k]), "")
            if not path and call.tool in SEARCH_TOOLS:
                path = cwd
            if path:
                rows.append((call.agent_id, call.id, path, op, call.tool, call.status, call.started, call_task.get(call.id)))
        return rows


class ReplayCache:
    """LRU of replayed sessions, keyed by (machine_id, session_id)."""

    def __init__(self, size: int = CACHE_SIZE) -> None:
        self.size = size
        self._items: "OrderedDict[Tuple[str, str], ReplayState]" = OrderedDict()

    def get(self, key: Tuple[str, str]) -> Optional[ReplayState]:
        state = self._items.get(key)
        if state is not None:
            self._items.move_to_end(key)
        return state

    def put(self, key: Tuple[str, str], state: ReplayState) -> None:
        self._items[key] = state
        self._items.move_to_end(key)
        while len(self._items) > self.size:
            self._items.popitem(last=False)

    def drop(self, key: Tuple[str, str]) -> None:
        self._items.pop(key, None)

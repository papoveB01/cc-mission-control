---
title: "cc-mission-control — Product Description and Technical Specification"
subtitle: "Version 0.1.1 · Build specification for Claude Code"
author: "E&EL Global Inc. — Papove Bombando Mfuana"
date: "October 2026"
---

# 1. Product description

**cc-mission-control** is a live dashboard for Claude Code sessions. It opens automatically when a Claude Code session starts and shows, in real time, every agent working in that session (the main thread and each subagent), what each agent is doing, how much of its context window it has used, and every tool call it makes.

It is distributed from a public GitHub repository as a Claude Code plugin. Any project can opt in by referencing the plugin from a branch's `.claude/settings.json`, so the dashboard travels with the repository.

## 1.1 Problem

Claude Code sessions increasingly run several agents at once. The terminal shows one linear stream of output. It does not show:

- which subagents are running in parallel and what each was asked to do;
- how close each agent is to its context limit, before compaction or degradation happens;
- the full history of tool calls per agent, with inputs, outputs, durations, and failures.

Teams that need to govern agent behavior (security review, audit, cost control) have no live view of what the agents actually did.

## 1.2 Users

| User | Need |
|---|---|
| Individual developer | See what parallel subagents are doing; catch context exhaustion early |
| Team lead / platform engineer | Watch long-running agent work; spot failing tools and loops |
| Governance / security reviewer | Inspect every tool call with secrets masked; evidence for agent governance |
| Content (FraudGate series) | A visual, demonstrable layer showing agent governance in action |

## 1.3 What it shows

- **Agent lanes.** One lane per agent: the main session first, then running subagents, then finished ones.
- **Context gauge per agent.** Tokens in context versus the model's window, updated after every model turn.
- **Current task per agent.** The user's prompt for the main thread; the delegated description for each subagent.
- **Tool calls per agent.** Live list with tool name, one-line summary, status (running, succeeded, failed), and duration. Clicking a call opens its full input and output, redacted.
- **Tool tally per agent.** Count of calls by tool name, plus error count.
- **Session activity feed.** Chronological feed across all agents: prompts, tool calls, subagent start/finish, compactions, notifications, failures.
- **Multiple sessions.** Each Claude Code session on the machine appears as its own tab.

## 1.4 Principles

1. **Observe, never interfere.** The dashboard never blocks, delays, or alters a tool call. If it is down, Claude Code is unaffected.
2. **Local by default.** Nothing leaves the machine unless a hosted upstream is explicitly configured.
3. **Secrets never displayed.** All captured text passes through redaction before storage, display, or forwarding.
4. **Zero build for users.** Installing the plugin gives a working dashboard; Node is needed only to change the UI.

# 2. Locked decisions

| # | Decision | Choice |
|---|---|---|
| 1 | Where it runs | Local first (localhost), hosted team version later |
| 2 | Distribution | Claude Code plugin repo, plus project branch config that references it |
| 3 | Backend stack | Python, FastAPI, uvicorn |
| 4 | Frontend | React + TypeScript (Vite), prebuilt bundle committed to the repo |
| 5 | Repo visibility | Public |
| 6 | License | Elastic License 2.0 (licensor: E&EL Global Inc.) |
| 7 | Name | `cc-mission-control`, repo `papoveB01/cc-mission-control-beta` until v0.1.0, then renamed to `papoveB01/cc-mission-control` (GitHub redirects the old URL; update all references in one commit) |

# 3. Scope

## 3.1 In scope for v0.1

- Plugin manifest, marketplace manifest, and hook configuration.
- SessionStart launcher that starts the server and opens the browser.
- Event server: hook ingestion, state model, transcript tailing for context usage, WebSocket broadcast.
- Redaction of secrets in all captured text.
- React dashboard: session tabs, agent lanes, context gauges, tool call list, detail drawer, activity feed, light and dark themes.
- Event simulator for development and demos.
- GitHub Actions: bundle-drift check and Python tests.
- README with install paths, configuration, and security notes.

## 3.2 Out of scope for v0.1

- Hosted multi-user server, authentication, persistence across restarts (designed for, not built; see Section 13).
- Blocking or approving tool calls (governance enforcement). The dashboard is observational only.
- Cost in dollars. Token counts only.
- Historical replay of past sessions from disk.

# 4. Architecture

```
 Claude Code session
 ┌───────────────────────────────────────────────┐
 │ SessionStart ── command hook ──► launch.py    │──┐ starts server if down,
 │                                               │  │ opens browser, forwards event
 │ PreToolUse / PostToolUse / SubagentStart ...  │  │
 │      └──── HTTP hooks (POST JSON) ────────────┼──┼──►  FastAPI event server
 │                                               │  │      127.0.0.1:4317
 │ transcript .jsonl files (written by CC) ◄─────┼──┼───── transcript tailer
 └───────────────────────────────────────────────┘  │      (context usage)
                                                    │            │
                                                    │      in-memory Store
                                                    │            │
                                                    │      WebSocket /ws
                                                    ▼            ▼
                                              Browser dashboard (React)
                                                         │
                                       (later) optional upstream relay ──► hosted server
```

## 4.1 Components

| Component | Role | Technology |
|---|---|---|
| Hook config | Registers the launcher and HTTP hooks with Claude Code | `hooks/hooks.json` in the plugin |
| Launcher | Runs on SessionStart; starts server, opens browser | `scripts/launch.py`, Python standard library only |
| Event server | Ingests events, keeps state, serves UI, pushes updates | FastAPI + uvicorn |
| Store | Sessions → agents → tool calls, built from events | Python, in memory |
| Transcript tailer | Reads token usage from transcript files | Python, incremental file reads |
| Redactor | Masks secrets in every captured string | Python regex |
| Dashboard | Displays live state | React + TypeScript, built with Vite |
| Upstream relay | (Off by default) forwards redacted events to a hosted server | httpx |

## 4.2 Why HTTP hooks for events

Claude Code supports `type: "http"` hooks, which POST the event JSON directly to a URL. A command hook would start a process on every tool call; an HTTP hook does not. Connection failures and non-2xx responses from HTTP hooks are non-blocking, so a stopped server never breaks a session.

SessionStart is the exception. It supports only `command` and `mcp_tool` hooks, so the launcher is a command hook.

# 5. Data sources

## 5.1 Hook events consumed

All hooks receive common fields: `session_id`, `transcript_path`, `cwd`, `hook_event_name`, and (when fired inside a subagent) `agent_id` and `agent_type`. Tool events also carry `tool_name`, `tool_input`, `tool_use_id`.

| Event | Hook type | Used for |
|---|---|---|
| `SessionStart` | command (launcher) | Create session; `source` (startup/resume/clear/compact/fork); `model` if present; `context_tokens` if present (on resume) seeds the main gauge |
| `UserPromptSubmit` | http | Main agent's current task = `prompt`; status → running |
| `PreToolUse` | http | New tool call (running); attribute to `agent_id` or main; detect subagent spawns |
| `PostToolUse` | http | Complete call (ok), `duration_ms`, output summary from `tool_response` |
| `PostToolUseFailure` | http | Complete call (error), error text from `error`, `duration_ms`; `is_interrupt: true` → activity text says "interrupted" |
| `SubagentStart` | http | New agent lane; label from `agent_type`; task from matching spawn call |
| `SubagentStop` | http | Agent done; `last_assistant_message` as result; `agent_transcript_path` if present |
| `Stop` | http | Main agent → idle (turn finished) |
| `StopFailure` | http | Main agent → error (API error) |
| `Notification` | http | Main agent → waiting when Claude needs input or permission |
| `PreCompact` / `PostCompact` | http | Activity entries; compaction counter |
| `TaskCreated` / `TaskCompleted` | http | Activity entries |
| `SessionEnd` | http | Session ended; close running calls |

**Subagent spawns.** The tool that launches a subagent is named `Agent` (older versions: `Task`). `SubagentStart` carries no link to the spawning call, so linking works in this order:

1. **Meta file (preferred).** Claude Code writes `<session_dir>/subagents/agent-<id>.meta.json` next to the subagent transcript, containing `agentType`, `description`, and `toolUseId`. On `SubagentStart` (and on later polls until found), read it; use `description` as the task and `toolUseId` to link the lane to the spawning `Agent` call. This file is undocumented (observed on Claude Code 2.1.x), so parse defensively.
2. **Pending list (fallback).** On `PreToolUse` for the spawn tool, record `(tool_use_id, subagent_type, description)`. On `SubagentStart`, if no meta file is available, pop the first pending entry whose type matches `agent_type`. When the meta file appears later, correct the task and remove the matching pending entry.

**Agent IDs.** Documentation examples show `agent_id` both with and without an `agent-` prefix. Normalize by stripping a leading `agent-` before using the ID as a key or building file names.

**Late start.** If the server starts mid-session, events may arrive without a prior SessionStart or PreToolUse. The Store creates the session or call on demand rather than dropping the event.

## 5.2 Context usage

Hooks do not report context size. Claude Code writes a transcript (`.jsonl`) per session, and each `assistant` entry carries API usage. The context the model saw on that turn is:

```
context_tokens = input_tokens + cache_creation_input_tokens + cache_read_input_tokens + output_tokens
```

This matches Claude Code's own definition (hooks reference, `context_tokens`: "the input, cache read, cache creation, and output tokens of the last response … combined"): the tokens the next request re-sends.

The latest assistant entry gives the current context size for that agent.

- **Main agent:** tail `transcript_path` from the hook payload. Skip entries with `isSidechain: true` (older layouts mixed subagent entries into the main file).
- **Subagents:** use `agent_transcript_path` from `SubagentStop` when present. While a subagent is running, search next to the session transcript, in order: `<session_dir>/subagents/agent-<agent_id>*.jsonl`, `<session_dir>/*<agent_id>*.jsonl`, `<transcript_dir>/*<agent_id>*.jsonl`, where `<session_dir>` is the transcript path without `.jsonl`.
- **Window size:** 1,000,000 if the model ID contains `1m`; otherwise `CCMC_CONTEXT_WINDOW` (default 200,000). Transcripts usually record the bare model ID (e.g. `claude-sonnet-5`, no `[1m]` suffix), so also switch an agent to 1,000,000 once its observed `context_tokens` exceeds the configured window. The window never shrinks for an agent once raised.
- **Model:** read from `message.model`; ignore `<synthetic>`.
- **Lag:** transcripts are written asynchronously and may trail the live turn slightly. The gauge updates on the next poll.

If no transcript is found for a subagent, its gauge shows "not available" rather than zero.

## 5.3 Tailing method

Keep a byte offset per file. Every poll, read from the offset to the end, split on newlines, hold back the last partial line, parse complete lines as JSON, skip invalid lines. If the file shrinks, reset the offset to zero.

# 6. Repository layout

```
cc-mission-control/
├── .claude-plugin/
│   ├── plugin.json            # plugin manifest
│   └── marketplace.json       # makes the repo installable as a marketplace
├── hooks/
│   └── hooks.json             # launcher + HTTP hooks
├── scripts/
│   ├── launch.py              # SessionStart launcher (stdlib only)
│   └── simulate.py            # fake session generator for dev and demos
├── cc_mission_control/        # Python package (event server)
│   ├── __init__.py            # version, APP_ID
│   ├── __main__.py            # python -m cc_mission_control
│   ├── config.py              # CCMC_* settings
│   ├── redact.py              # Redactor
│   ├── state.py               # Store, Session, Agent, ToolCall
│   ├── transcript.py          # Tail, TranscriptWatcher
│   ├── server.py              # FastAPI app
│   └── static/                # built dashboard (committed)
├── ui/                        # dashboard source
│   ├── package.json
│   ├── package-lock.json
│   ├── vite.config.ts         # outDir: ../cc_mission_control/static
│   ├── index.html
│   └── src/
├── tests/                     # pytest
├── examples/
│   └── project-settings.json  # snippet for a project branch
├── .github/workflows/
│   ├── ui-bundle.yml
│   └── python.yml
├── pyproject.toml
├── LICENSE                    # Elastic License 2.0, verbatim
├── NOTICE                     # copyright + plain-language summary
└── README.md
```

# 7. Plugin packaging

## 7.1 plugin.json

```json
{
  "name": "cc-mission-control",
  "version": "0.1.0",
  "description": "Live dashboard for Claude Code sessions: agents, subagents, context usage, and tool calls in real time.",
  "author": { "name": "Papove Bombando Mfuana", "url": "https://github.com/papoveB01" },
  "homepage": "https://github.com/papoveB01/cc-mission-control-beta",
  "repository": "https://github.com/papoveB01/cc-mission-control-beta",
  "license": "Elastic-2.0",
  "keywords": ["observability", "dashboard", "subagents", "hooks", "agent-governance"]
}
```

Bump `version` on every release; Claude Code caches plugins by version.

## 7.2 marketplace.json

```json
{
  "name": "papoveb01",
  "owner": { "name": "Papove Bombando Mfuana", "url": "https://github.com/papoveB01" },
  "plugins": [
    {
      "name": "cc-mission-control",
      "source": "./",
      "description": "Live dashboard for Claude Code sessions: agents, subagents, context usage, and tool calls in real time."
    }
  ]
}
```

The plugin reference is therefore `cc-mission-control@papoveb01`.

## 7.3 hooks/hooks.json

```js
{
  "description": "cc-mission-control: launch the dashboard and stream session events to it",
  "hooks": {
    "SessionStart": [
      { "hooks": [ {
          "type": "command",
          "command": "python3",
          "args": ["${CLAUDE_PLUGIN_ROOT}/scripts/launch.py"],
          "timeout": 20
      } ] }
    ],
    "UserPromptSubmit": [
      { "hooks": [ { "type": "http", "url": "http://127.0.0.1:4317/hook", "timeout": 2 } ] }
    ]
    // Repeat the UserPromptSubmit block, unchanged, for each of these events:
    // PreToolUse, PostToolUse, PostToolUseFailure, SubagentStart, SubagentStop,
    // Stop, StopFailure, Notification, PreCompact, PostCompact,
    // TaskCreated, TaskCompleted, SessionEnd
  }
}
```

The comments above are for this document only; the real file is plain JSON with all fifteen events written out.

Rules:

- The 2-second timeout caps the delay if the server hangs. Default HTTP hook timeout is 600 seconds and must not be used.
- Port 4317 is fixed in the hook URLs. Hook URLs cannot read environment variables, so changing the port means editing this file. Hosted mode does not change the URL; the local server relays (Section 13).
- If a user or organization sets `allowedHttpHookUrls`, it must include `http://127.0.0.1:4317/*` or the HTTP hooks will not run. Document this in the README.
- On Windows, `python3` may not exist. The README documents replacing it with `python` or `py`.

## 7.4 Project branch config

Committed to `.claude/settings.json` on any project branch that should carry the dashboard:

```json
{
  "extraKnownMarketplaces": {
    "papoveb01": {
      "source": { "source": "github", "repo": "papoveB01/cc-mission-control-beta" }
    }
  },
  "enabledPlugins": {
    "cc-mission-control@papoveb01": true
  }
}
```

**Workspace trust.** Claude Code honors `extraKnownMarketplaces` from a repository's `.claude/settings.json` only after the user accepts the workspace trust dialog for that folder; in an untrusted folder (including `-p` runs) it ignores the entries silently. The README must say so.

**Known issue.** Several open Claude Code issues report that project-level `extraKnownMarketplaces` + `enabledPlugins` does not prompt installation in some versions. The README must therefore also give the manual path, which always works:

```
/plugin marketplace add papoveB01/cc-mission-control-beta
/plugin install cc-mission-control@papoveb01
```

Keep the branch config anyway: when the feature works, it is zero-step for teammates, and it documents intent.

# 8. Launcher (SessionStart)

File: `scripts/launch.py`. Standard library only, so it starts in milliseconds without a virtualenv.

## 8.1 Behavior

1. Read the SessionStart JSON from stdin.
2. `GET http://127.0.0.1:4317/health` (timeout 0.5 s). Accept only a response with `"app": "cc-mission-control"`, so an unrelated service on the port is never mistaken for the server.
3. If not running, start the server detached:
   - If `uv` is on PATH: `uv run --quiet --project $CLAUDE_PLUGIN_ROOT python -m cc_mission_control`, with `UV_PROJECT_ENVIRONMENT=$CLAUDE_PLUGIN_DATA/venv` so the virtualenv survives plugin updates.
   - Otherwise, if `fastapi` and `uvicorn` import, run `python -m cc_mission_control` with `PYTHONPATH=$CLAUDE_PLUGIN_ROOT`.
   - Otherwise, print an install hint for uv to stderr and exit 0.
   - Detach: `start_new_session=True` on POSIX; `DETACHED_PROCESS | CREATE_NEW_PROCESS_GROUP` on Windows. Redirect output to `$CLAUDE_PLUGIN_DATA/server.log`.
4. Poll `/health` every 250 ms for up to 15 s (first run builds the environment).
5. POST the SessionStart event to `/hook` so the session appears immediately.
6. Open `http://127.0.0.1:4317/` in the browser only if `/health` reports zero connected dashboards, `CCMC_NO_BROWSER` is not `1`, and `CLAUDE_CODE_REMOTE` is not `true`.

## 8.2 Hard rules

- **Never write to stdout.** SessionStart stdout is injected into Claude's context.
- **Always exit 0.** Wrap everything in a try/except that writes to stderr.
- Never block longer than the hook timeout (20 s).

# 9. Event server

## 9.1 Endpoints

| Method | Path | Purpose | Response |
|---|---|---|---|
| GET | `/health` | Liveness and identity | `{app, version, clients, sessions, active}` |
| POST | `/hook` | Ingest any hook event | Always `200` with empty body |
| GET | `/api/sessions` | Full snapshot (debugging, initial load fallback) | Array of Session objects |
| GET | `/api/sessions/{session_id}/calls/{call_id}` | Full detail for one call, for the drawer | `ToolCallDetail`, or `404` |
| WS | `/ws` | Live updates | Messages below |
| GET | `/` and `/assets/*` | Dashboard bundle | Static files |

**`/hook` must always return 2xx with an empty body.** An empty 2xx means "no decision" to Claude Code. A JSON body could be interpreted as a decision. Malformed bodies are ignored, still with 200.

## 9.2 WebSocket messages

Server → client:

```json
{ "type": "snapshot", "version": "0.1.0", "sessions": [ Session, ... ] }
{ "type": "sessions", "sessions": [ Session, ... ] }
```

`snapshot` is sent once on connect. `sessions` carries full objects for each session that changed since the last push. The client replaces those sessions by `id`.

Client → server: periodic `"ping"` text frames (every 20 s) as keepalive; content ignored.

## 9.3 Data model

```
Session {
  id: string               // session_id
  title: string            // basename of cwd
  cwd: string
  model: string | null
  status: "active" | "ended"
  started: number          // epoch seconds
  ended: number | null
  last_event: number
  compactions: number
  agents: Agent[]          // main first, then running, then finished (newest first)
  activity: Activity[]     // last 80
}

Agent {
  id: string               // "main" or agent_id
  label: string            // "Main", or agent_type
  agent_type: string | null
  status: "idle" | "running" | "waiting" | "done" | "error"
  task: string             // prompt (main) or delegated description (subagent)
  result: string           // subagent's last message
  started: number
  ended: number | null
  context_tokens: number | null
  context_window: number | null
  model: string | null
  tool_counts: { [tool: string]: number }   // sorted by count desc
  errors: number
  total_calls: number
  calls: ToolCall[]        // last 60 sent; up to CCMC_MAX_CALLS kept in memory
}

// Sent in lanes over /ws and /api/sessions. No full input/output, to keep
// pushes small (60 calls × agents × 2 × 4000 chars would be megabytes per push).

ToolCall {
  id: string               // tool_use_id
  agent_id: string
  tool: string
  summary: string          // one line, redacted
  status: "running" | "ok" | "error"
  started: number
  ended: number | null
  duration_ms: number | null
  subagent_id: string | null  // set on Agent/Task spawn calls once linked to their lane
}

// Returned only by GET /api/sessions/{session_id}/calls/{call_id}.
ToolCallDetail = ToolCall & {
  input: object            // redacted (every nested string), each string truncated
  output: string           // redacted, truncated
  error: string | null     // redacted
}

Activity {
  t: number
  agent_id: string
  kind: "session" | "prompt" | "tool" | "agent" | "turn" | "error" | "notice" | "compact" | "task"
  text: string             // max 240 chars
  status: "info" | "running" | "ok" | "error"
}
```

**Tool summary rule.** From `tool_input`, use the first non-empty of: `command`, `file_path`, `notebook_path`, `pattern`, `url`, `query`, `description`, `prompt`, `path`, `skill`; take its first line, max 200 characters. Fallback: compact JSON, max 200.

**Output summary rule.** From `tool_response`, use the first non-empty of: `stdout`, `output`, `content`, `result`, `text`, `message`. Fallback: JSON. Then redact and truncate to `CCMC_MAX_FIELD_CHARS`.

**Call matching.** Match Post events to Pre by `tool_use_id`. If absent, match the most recent running call with the same `tool_name` on that agent. If no match, create the call then complete it.

**Status transitions (main agent).** `UserPromptSubmit` → running; `Notification` (permission/idle/needs-input) → waiting; next `PreToolUse` → running; `Stop` → idle; `StopFailure` → error. `SessionEnd` closes running calls as error and marks subagents done.

**Stale sessions.** If a terminal is killed, `SessionEnd` never arrives. A session with no hook event and no transcript growth for `CCMC_STALE_MINUTES` (default 60) is marked `ended` (activity: "No activity; marked ended"), with running calls closed as error. A later event for that session reactivates it.

## 9.4 Loops

- **Broadcast loop (150 ms).** Union of sessions changed by events and by transcript polling. If any changed and dashboards are connected, send one `sessions` message. This coalesces bursts of parallel tool calls into one update.
- **Transcript poll.** Runs inside the broadcast loop for active sessions.
- **Stale check (30 s).** Applies the stale-session rule above.
- **Idle shutdown (30 s check).** If no active session and no connected dashboard for `CCMC_IDLE_MINUTES` (default 30), exit. `0` disables.
- **Upstream relay.** Only when `CCMC_UPSTREAM_URL` is set (Section 13).

Write `server.pid` to the data directory at startup.

# 10. Security and redaction

## 10.1 Network

- Bind to `127.0.0.1` only. Never `0.0.0.0`.
- Reject any request whose `Host` header is not `127.0.0.1:<port>` or `localhost:<port>` (prevents DNS-rebinding attacks from web pages).
- Reject `/hook`, `/api/*`, and `/ws` requests that carry an `Origin` header not matching those hosts (prevents a malicious page from injecting events or reading the stream). Exception for UI development: origins listed in `CCMC_DEV_ORIGINS` (comma-separated, e.g. `http://localhost:5173`) are also accepted. Unset by default; never set by the launcher.
- No outbound network traffic unless `CCMC_UPSTREAM_URL` is set.

## 10.2 Redaction

Every string from a hook payload passes through the Redactor before it is stored, displayed, or forwarded. Redaction walks nested objects and arrays in `tool_input` and `tool_response` and redacts every string value; dictionary keys are kept. Matches are replaced with `[redacted]`.

Built-in patterns:

| Secret | Pattern (summary) |
|---|---|
| Private key blocks | `-----BEGIN ... PRIVATE KEY----- ... -----END ... PRIVATE KEY-----` |
| Anthropic keys | `sk-ant-…` |
| Generic `sk-` keys | `sk-` + 20+ chars |
| GitHub tokens | `ghp_ gho_ ghu_ ghs_ ghr_`, `github_pat_` |
| Slack tokens | `xoxa- xoxb- xoxp- xoxr- xoxs-` |
| AWS access key IDs | `AKIA` + 16 |
| Google API keys | `AIza` + 30+ |
| JWTs | three base64url segments starting `eyJ` |
| Bearer tokens | `Bearer` + 12+ chars |
| Keyed secrets | `api_key / secret / token / password / passwd / pwd / access_key / client_secret / private_key` followed by `:` or `=` — keep the key, mask the value |

Extra patterns: one regex per line in the file named by `CCMC_REDACT_FILE`; lines starting with `#` are comments; invalid regexes are skipped.

Python's `re` does not allow variable-width lookbehind. Implement the keyed-secret rule with a capture group and a replacement function (`group(1) + "[redacted]"`), not a lookbehind.

After redaction, truncate to `CCMC_MAX_FIELD_CHARS` (default 4000) with a "[N more characters]" suffix.

Redaction is best effort. The README must say so plainly and must state that nothing leaves the machine by default.

# 11. Dashboard

## 11.1 Layout

```
┌──────────────────────────────────────────────────────────────────────┐
│ Mission Control   [ project-a ● ] [ project-b ]       ● Live         │  header + session tabs
│ claude-opus-5 · started 14:02 · 3 agents running · 214 calls · 2 err │  session summary
├──────────────────────────────────────────────┬───────────────────────┤
│ ▌Main                     running   41m      │ Activity              │
│ ▌Task: Refactor the fraud scoring module…    │ 14:43 Explore started │
│ ▌Context ████████████░░░░░░  118k / 200k     │ 14:43 Bash: pytest    │
│ ▌Bash 22  Read 40  Edit 9  Agent 2   err 1   │ 14:42 Edit: score.py  │
│ ▌● Bash   pytest -q tests/           2.4s    │ ...                   │
│ ▌● Edit   src/fraud/score.py         0.1s    │                       │
├──────────────────────────────────────────────┤                       │
│ ▌Explore                  running   2m       │                       │
│ ▌Task: Map all callers of score_txn          │                       │
│ ▌Context ███░░░░░░░░░░░░░░░  31k / 200k      │                       │
│ ▌● Grep   score_txn                  0.3s    │                       │
├──────────────────────────────────────────────┤                       │
│ ▸ code-reviewer           done      4m       │  (finished: collapsed)│
└──────────────────────────────────────────────┴───────────────────────┘
                          click a call ──► right-side drawer with full input/output
```

Below 900 px wide, the activity feed moves under the lanes. Below 600 px, each lane shows the last 3 calls.

## 11.2 Components

| Component | Behavior |
|---|---|
| `useMissionSocket` hook | Connects to `ws://<host>/ws`; applies snapshot and session updates; reconnects with backoff (0.5 s → 5 s max); sends ping every 20 s; exposes connection state |
| `Header` | Product name, session tabs (active sessions first, ended dimmed), connection indicator (Live / Reconnecting / Offline) |
| `SessionSummary` | Project title, model, start time, running agents, total calls, total errors, compaction count |
| `AgentLane` | Status edge color, label, status, elapsed time, task text (2 lines, expandable), context gauge, tool tally, last 8 calls |
| `ContextGauge` | Bar with tokens / window and percent. Color thresholds: under 60% neutral, 60–80% amber, over 80% red. Shows "not available" when null |
| `ToolCallRow` | Status dot, tool name, summary (single line, ellipsis), duration or live elapsed timer while running |
| `CallDrawer` | Fetches `/api/sessions/{sid}/calls/{id}` on open (and again when the call completes). Full redacted input (formatted JSON), output, error, status, timestamps, duration. Closes with Esc |
| `ActivityFeed` | Newest first, colored by status, agent label per row; auto-scroll pauses when the user scrolls |
| `EmptyState` | "Waiting for a Claude Code session. Start `claude` in a project with cc-mission-control enabled." |

Default selected session: the most recently started active session. If the user selects a tab, keep that selection until they change it.

Finished subagents collapse to a single line (label, status, duration, call count); click to expand.

## 11.3 Visual design

Concept: an air-traffic-control flight-strip bay. Each agent is a strip; the colored left edge encodes status at a glance.

| Token | Light | Dark |
|---|---|---|
| Bay (background) | `#DCE3EA` | `#18212C` |
| Strip (surface) | `#FFFFFF` | `#222D3A` |
| Ink (text) | `#15263B` | `#E5ECF3` |
| Muted text | `#5B6B7E` | `#91A0B2` |
| Running | `#D98E04` | `#E9A82A` |
| Succeeded | `#2E7D5B` | `#4DAE84` |
| Failed | `#B83A2B` | `#E0685A` |
| Subagent accent | `#3B5FA8` | `#7C9BE0` |

Typography: condensed sans for headings and labels (`"Avenir Next Condensed", "Roboto Condensed", "Arial Narrow", system-ui`); `system-ui` for body; `ui-monospace` only for commands, paths, and JSON. No external font loading (the dashboard must work offline). Sentence case throughout.

Theme follows `prefers-color-scheme`. Respect `prefers-reduced-motion` (disable the running pulse). Visible keyboard focus on tabs, rows, and the drawer.

## 11.4 Build

- Vite + React 18 + TypeScript, no UI framework.
- `vite.config.ts`: `build.outDir = "../cc_mission_control/static"`, `emptyOutDir: true`, `base: "/"`.
- Dev: `npm run dev` with a proxy for `/ws`, `/api`, `/hook`, `/health` to `127.0.0.1:4317` (`changeOrigin: true`), and start the server with `CCMC_DEV_ORIGINS=http://localhost:5173`.
- Node 22 (pinned in `ui/.nvmrc` and `engines`) so local builds match CI byte-for-byte.
- Commit `package-lock.json` and the built `static/` folder.

# 12. Configuration

| Variable | Default | Purpose |
|---|---|---|
| `CCMC_PORT` | `4317` | Server port (must match hook URLs) |
| `CCMC_DATA_DIR` | `$CLAUDE_PLUGIN_DATA` or `~/.cc-mission-control` | Logs, pid, virtualenv |
| `CCMC_CONTEXT_WINDOW` | `200000` | Window used for gauges when the model is not a 1M model |
| `CCMC_IDLE_MINUTES` | `30` | Idle shutdown; `0` disables |
| `CCMC_MAX_CALLS` | `500` | Tool calls kept in memory per agent |
| `CCMC_MAX_FIELD_CHARS` | `4000` | Truncation length for inputs/outputs |
| `CCMC_REDACT_FILE` | unset | File of extra redaction regexes |
| `CCMC_NO_BROWSER` | unset | `1` = never open the browser automatically |
| `CCMC_UPSTREAM_URL` | unset | Hosted mode: forward redacted events here |
| `CCMC_UPSTREAM_TOKEN` | unset | Bearer token for the upstream |
| `CCMC_STALE_MINUTES` | `60` | Mark a silent session ended after this long |
| `CCMC_DEV_ORIGINS` | unset | Extra allowed `Origin` values, for the Vite dev server only |

Python package dependencies: `fastapi>=0.110`, `uvicorn>=0.29`, `websockets>=12`, `httpx>=0.27`; dev: `pytest>=8`. Python 3.10+. Build backend: hatchling. Console script: `ccmc-server = cc_mission_control.__main__:main`. The wheel must include `cc_mission_control/static/**` (hatch `artifacts` or `force-include`), since the built bundle is not Python source.

# 13. Hosted mode (designed now, built later)

The event format already supports multiple machines and users.

- **v0.1 (built):** when `CCMC_UPSTREAM_URL` is set, the local server forwards each event, redacted, with two added fields: `machine_id` (first 12 hex chars of SHA-256 of the hostname) and `user` (OS username). Async queue, max 5000; drop when full; never block ingestion.
- **v0.2 (later):** hosted server reuses the same Store and UI, keyed by `(machine_id, session_id)`; adds token authentication, persistence (SQLite or Postgres), retention policy, and a machine/user filter in the UI.
- **Commercial line:** offering the hosted version to third parties is reserved to E&EL under the Elastic License 2.0.

The hook URL never changes: hooks always post to the local server, which relays.

# 14. CI/CD

## 14.1 `.github/workflows/ui-bundle.yml`

Triggers on push and pull request touching `ui/**` or `cc_mission_control/static/**`.

1. Checkout, set up Node 22.
2. `npm ci` and `npm run build` in `ui/`.
3. `git diff --exit-code cc_mission_control/static` — fail with "Run `npm run build` in ui/ and commit the bundle" if the committed bundle doesn't match the source.

## 14.2 `.github/workflows/python.yml`

On push and pull request: Python 3.10 and 3.12 matrix, `pip install -e .[dev]`, `pytest -q`. Also validate that `hooks/hooks.json`, `plugin.json`, and `marketplace.json` parse as JSON.

# 15. Testing

## 15.1 Unit tests (pytest)

| Test | Asserts |
|---|---|
| Redaction | Each built-in pattern masked; keyed secrets keep the key; benign text unchanged; extra-file patterns applied; invalid regex skipped |
| Session lifecycle | SessionStart → prompt → tool calls → Stop → SessionEnd produce correct statuses and activity |
| Subagents | `Agent` PreToolUse + SubagentStart labels the lane with the description; tool events with `agent_id` land in the subagent lane; SubagentStop marks done and stores result |
| Call matching | Post matches Pre by `tool_use_id`; fallback by tool name; orphan Post creates and completes a call |
| Failures | PostToolUseFailure marks error and increments counts; StopFailure sets main to error |
| Late start | Events without SessionStart still create the session |
| Transcript | Usage sum computed correctly; sidechain entries skipped on main; partial last line held back; file truncation resets offset; 1M model detection |
| Server | `/hook` returns 200 with empty body for valid, malformed, and unknown events; foreign `Host` or `Origin` returns 403 / closes WS |
| Subagent linking | Meta file links description and `toolUseId`; fallback FIFO by type; meta file arriving later corrects the task; `agent-` prefix normalized |
| Stale sessions | Silent session marked ended after `CCMC_STALE_MINUTES`; a later event reactivates it |
| Call detail | Lane payload omits input/output; detail endpoint returns redacted nested input; unknown call → 404 |
| Dev origins | Origin in `CCMC_DEV_ORIGINS` accepted; any other foreign origin still rejected |

## 15.2 Simulator

`scripts/simulate.py` posts a realistic fake session to `/hook` and writes a fake transcript so gauges move. Flags: `--agents N` (parallel subagents), `--speed X`, `--failures`. Used for UI development without spending tokens, and for the FraudGate series demo.

## 15.3 Manual acceptance (real Claude Code)

1. Install via `/plugin marketplace add` + `/plugin install`; start `claude` in a project; browser opens to the dashboard; session tab appears.
2. Ask for work that spawns two parallel subagents; both lanes appear with tasks; tool calls stream into the correct lanes.
3. Context gauges rise after model turns; `/compact` drops the main gauge and increments the compaction count.
4. A failing Bash command shows as failed with its error.
5. A command containing a fake `sk-ant-` key shows `[redacted]` in the row and drawer.
6. Stop the server mid-session; Claude Code keeps working with no errors; restart a session and the server comes back.
7. Open a second project's session; it appears as a second tab; no second browser window opens.

# 16. Build plan for Claude Code

Build in this order. Each milestone has a done condition.

| # | Milestone | Done when |
|---|---|---|
| 1 | Scaffold repo: manifests, hooks.json, pyproject, LICENSE, NOTICE | `claude --plugin-dir .` loads the plugin; `/hooks` lists all hooks from the plugin |
| 2 | Redactor + tests | All redaction tests pass |
| 3 | Store + tests | Lifecycle, subagent, matching, failure, late-start tests pass |
| 4 | Transcript tailer + tests | Transcript tests pass against fixture `.jsonl` files |
| 5 | FastAPI server | `/health`, `/hook`, `/api/sessions`, `/ws` work; host/origin checks pass tests |
| 6 | Launcher | SessionStart starts the server, opens browser once, never prints to stdout |
| 7 | Simulator | Simulated session renders correctly via `/api/sessions` |
| 8 | Dashboard UI | All Section 11 components working against the simulator, light and dark |
| 9 | CI workflows | Both workflows green on GitHub |
| 10 | Real-session acceptance | All Section 15.3 checks pass |
| 11 | README + examples, tag v0.1.0 | Public repo installable by a third party from the README alone |

**Instruction for Claude Code:** treat this document as the source of truth. Before implementing hooks, confirm current field names against the Claude Code hooks reference (`https://code.claude.com/docs/en/hooks`), since the hook system changes often. Where the docs and this spec disagree on a Claude Code fact, follow the docs and note the change in the README.

# 17. Risks and open points

| Risk | Impact | Mitigation |
|---|---|---|
| Hook field names change between Claude Code versions | Events misparsed | Defensive parsing; unknown events ignored; tests against fixture payloads; version noted in README |
| Subagent transcript location not documented as stable | Subagent gauges show "not available" | Use `agent_transcript_path` when given; search fallback; degrade gracefully |
| Project-level plugin auto-install bug | Teammates don't get the dashboard automatically | Manual two-command install in README |
| `allowedHttpHookUrls` set by org policy | HTTP hooks silently skipped | README instruction to allowlist `http://127.0.0.1:4317/*` |
| Redaction misses a secret format | Secret visible locally | Local-only by default; extensible patterns; documented as best effort |
| Port 4317 already in use | Server fails to start | Health check verifies identity; README shows how to change port in hooks.json and `CCMC_PORT` |
| Windows `python3` missing | Launcher fails | README Windows section; non-blocking failure |

# Appendix A. Verified Claude Code facts used in this design

Checked against the official hooks reference, October 2026:

- Hook handler types: `command`, `http`, `mcp_tool`, `prompt`, `agent`. HTTP hooks POST the event JSON with `Content-Type: application/json`.
- HTTP hook errors (non-2xx, connection failure) are non-blocking; an empty 2xx body is success with no decision.
- `SessionStart` supports only `command` and `mcp_tool` hooks; its plain stdout is added to Claude's context.
- Tool events fired inside a subagent carry `agent_id` and `agent_type`.
- `SubagentStart` and `SubagentStop` exist and match on agent type; `SubagentStop` provides `last_assistant_message`.
- The subagent-spawning tool is named `Agent`.
- Plugin hooks live in `hooks/hooks.json`; `${CLAUDE_PLUGIN_ROOT}` and `${CLAUDE_PLUGIN_DATA}` are available to command hooks.
- `SessionEnd` hooks share a 1.5-second budget.
- `allowedHttpHookUrls` restricts which HTTP hook URLs run when defined.

Reference: `https://code.claude.com/docs/en/hooks`

# Appendix B. Revision notes

**0.1.1 (2 October 2026)**, after checking against the hooks and settings references and real Claude Code 2.1.287 transcripts:

- Build repo is `papoveB01/cc-mission-control-beta` until v0.1.0.
- `PostToolUse` / `PostToolUseFailure` supply `duration_ms`; failures use `error` and `is_interrupt`.
- Subagents link to their spawn call through `subagents/agent-<id>.meta.json` first, pending-list FIFO second; `agent_id` prefix normalized.
- Context formula includes `output_tokens`; 1M window inferred from observed usage; `SessionStart.context_tokens` seeds the gauge on resume.
- Lane payloads drop full input/output; new call-detail endpoint feeds the drawer.
- Stale-session rule (`CCMC_STALE_MINUTES`), dev-only `CCMC_DEV_ORIGINS`, nested redaction, static bundle in the wheel, Node 22 pinned.
- README notes workspace trust for project-level plugin config.

# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## What this is

A Claude Code plugin that shows a live local dashboard of every agent in a Claude Code session: subagents, context usage, and tool calls. `SPEC.md` is the source of truth for behavior. Section 16 is the milestone build order, and Appendix B lists the changes made after checking against the live hooks docs. If the Claude Code hooks docs (https://code.claude.com/docs/en/hooks) disagree with the spec on a Claude Code fact, follow the docs and record the change in SPEC Appendix B and the README.

## Commands

```bash
uv run --extra dev pytest -q                              # all tests (creates .venv)
uv run --extra dev pytest -q tests/test_redact.py -k bearer   # single file / test
uv run -p 3.10 --extra dev pytest -q                      # CI also runs 3.10 and 3.12
uv build --wheel                                          # wheel must contain cc_mission_control/static/**
claude plugin validate .                                  # marketplace.json
claude plugin validate .claude-plugin/plugin.json
claude --plugin-dir . --init-only --debug hooks           # fire SessionStart hooks only; check ~/.claude/debug/*.txt
```

UI (once `ui/` exists): Node 22 is required so local builds match CI byte-for-byte. Node 22 is a keg-only Homebrew install here; use `/opt/homebrew/opt/node@22/bin` ahead of PATH. `npm run build` in `ui/` writes to `cc_mission_control/static/`, and the built bundle is committed. CI fails if the committed bundle differs from a fresh build.

## Architecture

Event flow: Claude Code → `hooks/hooks.json` → FastAPI server on `127.0.0.1:4317` → in-memory Store → WebSocket `/ws` → React dashboard.

- **SessionStart** is the only `command` hook (`scripts/launch.py`), because SessionStart doesn't support `http` hooks. It starts the server if `/health` doesn't identify as `cc-mission-control`, then forwards the event and opens the browser. It is stdlib-only, must **never write to stdout** (stdout is injected into Claude's context), and always exits 0.
- **All other events** (14 of them) are `type: "http"` hooks POSTing to `/hook` with `timeout: 2`. `/hook` must always return 200 with an **empty body**: an empty 2xx means "no decision", and a JSON body could be read as a decision. The port is hardcoded in `hooks.json` because hook URLs can't read env vars.
- **Context usage** doesn't come from hooks. The transcript tailer reads `message.usage` from transcript `.jsonl` files: input + cache_creation + cache_read + output tokens. Subagent transcripts live at `<session>/subagents/agent-<id>.jsonl`. A sibling `agent-<id>.meta.json` (undocumented) holds `description` and `toolUseId`, and is the preferred way to link a subagent lane to the `Agent` tool call that spawned it. FIFO matching by `agent_type` is the fallback. Strip a leading `agent-` from `agent_id` before using it as a key.
- **Redaction** (`redact.py`) runs on every payload string before storage, display, or forwarding: redact first, then truncate. Lane payloads over `/ws` carry call summaries only. Full redacted input/output is served by `GET /api/sessions/{sid}/calls/{id}` for the detail drawer.
- **Security**: bind 127.0.0.1 only. Reject foreign `Host` headers (DNS rebinding) and foreign `Origin` on `/hook`, `/api/*`, `/ws`. `CCMC_DEV_ORIGINS` is the only escape hatch, for the Vite dev server.
- **Config**: all `CCMC_*` env vars are parsed in `config.py` (`Config.from_env`). The data dir defaults to `$CLAUDE_PLUGIN_DATA`, then `~/.cc-mission-control`.

## Conventions

- Tests never contain credential-shaped string literals. Build fake secrets at runtime by concatenation (see `tests/test_redact.py`), or GitHub push protection blocks the push.
- `plugin.json` `version` must be bumped on every release because Claude Code caches plugins by version.
- Repo references point at `papoveB01/cc-mission-control-beta` until v0.1.0, when the repo is renamed to `cc-mission-control`. The plugin ID stays `cc-mission-control@papoveb01`.
- `keys/` holds a local GitHub token and is gitignored. Pushes use it per command via a credential helper, never stored in git config. The `gh` CLI token cannot push to this repo.
- License is Elastic License 2.0 (licensor E&EL Global Inc.). `LICENSE` is the verbatim upstream text; don't edit it.

## Working model

Hub and spoke. The main session coordinates and does the final review, and does not write code itself. Implementation goes to a Sonnet subagent, and code review to a separate Sonnet subagent. The coordinator then runs the tests, reads the diff, and decides before committing.

"""FastAPI event server (SPEC sections 9 and 10.1) with the optional upstream relay (section 13)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable, Optional
from urllib.parse import urlsplit

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from . import APP_ID, __version__
from .config import Config
from . import nudge as nudge_mod
from .efficiency_live import LiveEfficiency
from .conventions import ConventionError, Conventions, load_rubric
from .projects import Projects, project_key
from .relay import RELAY_MAX, Relay, machine_id, os_user  # noqa: F401
from .series import LiveSeries
from .state import SUMMARIZER, Store
from .plan import PlanDetector, plan_path
from .sysmon import Sampler, ServerMetrics, lag_sampler, own_rss_bytes, own_threads
from .summarize import Summarizer, SummaryRequest
from .transcript import TranscriptWatcher

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
NO_CACHE = {"Cache-Control": "no-cache"}
NO_STORE = {"Cache-Control": "no-store"}
MAX_CONV_BODY = 4 * 1024  # SPEC 11.14: write requests carry a small JSON body
PROJECT_KEY_TTL = 60.0
IMMUTABLE = "public, max-age=31536000, immutable"


class ImmutableStaticFiles(StaticFiles):
    """Hashed bundle files never change under the same name, so browsers may cache them forever."""

    async def get_response(self, path: str, scope: Any) -> Response:
        response = await super().get_response(path, scope)
        if response.status_code == 200:
            response.headers["Cache-Control"] = IMMUTABLE
        return response
BROADCAST_INTERVAL = 0.15
HOUSEKEEPING_INTERVAL = 30.0
EFFICIENCY_INTERVAL = 30.0
SEND_TIMEOUT = 1.0
CLIENT_QUEUE = 16
MAX_HOOK_BYTES = 8 * 1024 * 1024
INBOX_MAX = 256  # sessions whose inbox socket we remember
NUDGE_INTERVAL = 300.0  # one nudge per session per 5 minutes
WS_POLICY_VIOLATION = 1008
WS_INTERNAL_ERROR = 1011

PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><title>cc-mission-control</title>"
    "<h1>cc-mission-control</h1><p>The dashboard bundle is not built. "
    "Run <code>npm run build</code> in <code>ui/</code>.</p>"
)


def dumps(obj: Any) -> Optional[str]:
    """Strict JSON for the wire; None (after logging) if it can't be encoded."""
    try:
        return json.dumps(obj, allow_nan=False, ensure_ascii=False)
    except (TypeError, ValueError):
        log.warning("dropping message that is not valid JSON", exc_info=True)
        return None


class Client:
    """One dashboard connection: a bounded outbound queue drained by its own writer task."""

    def __init__(self, ws: Any) -> None:
        self.ws = ws
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=CLIENT_QUEUE)
        self.task: Optional[asyncio.Task] = None


class GuardMiddleware:
    """Host and Origin checks (SPEC 10.1) for HTTP and WebSocket scopes."""

    def __init__(self, app: Any, config: Config) -> None:
        self.app = app
        port = config.port
        self.hosts = {f"127.0.0.1:{port}", f"localhost:{port}"}
        self.origins = {f"http://127.0.0.1:{port}", f"http://localhost:{port}", *config.dev_origins}

    def _allowed(self, scope: dict) -> bool:
        headers = {k: v for k, v in scope.get("headers", [])}
        if headers.get(b"host", b"").decode("latin-1") not in self.hosts:
            return False
        path = scope.get("path", "")
        if path in ("/hook", "/ws", "/api") or path.startswith("/api/"):
            origin = headers.get(b"origin")
            if origin is not None and origin.decode("latin-1") not in self.origins:
                return False
        return True

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        if scope["type"] in ("http", "websocket") and not self._allowed(scope):
            if scope["type"] == "websocket":
                await receive()  # websocket.connect
                await send({"type": "websocket.close", "code": WS_POLICY_VIOLATION})
            else:
                await Response(status_code=403)(scope, receive, send)
            return
        await self.app(scope, receive, send)


class ProjectsNoStore:
    """`Cache-Control: no-store` on every response under /api/projects, errors from the guard and the router included."""

    def __init__(self, app: Any) -> None:
        self.app = app

    async def __call__(self, scope: dict, receive: Callable, send: Callable) -> None:
        path = scope.get("path", "")
        if scope["type"] != "http" or not (path == "/api/projects" or path.startswith("/api/projects/")):
            await self.app(scope, receive, send)
            return

        async def wrapped(message: dict) -> None:
            if message["type"] == "http.response.start":
                headers = list(message.get("headers", []))
                if not any(k.lower() == b"cache-control" for k, _ in headers):
                    headers.append((b"cache-control", b"no-store"))
                message = {**message, "headers": headers}
            await send(message)

        await self.app(scope, receive, wrapped)


def hub_origin(url: Optional[str]) -> Optional[str]:
    """Scheme, host and port of an http(s) URL, or None. Userinfo, path, query and fragment are dropped."""
    if not url:
        return None
    try:
        parts = urlsplit(url.strip())
        host, port = parts.hostname, parts.port
    except ValueError:
        return None
    if parts.scheme not in ("http", "https") or not host:
        return None
    if ":" in host:
        host = f"[{host}]"
    return f"{parts.scheme}://{host}" + (f":{port}" if port is not None else "")


def create_app(
    config: Config,
    *,
    store: Store | None = None,
    watcher: TranscriptWatcher | None = None,
    clock: Callable[[], float] = time.time,
    start_loops: bool = True,
    relay_maxsize: int = RELAY_MAX,
    relay_options: dict | None = None,
    summary_options: dict | None = None,
    home: Any = None,
    sysmon: Sampler | None = None,
) -> FastAPI:
    store = store or Store(config, clock=clock)
    relay = (
        Relay(config, store.redactor, relay_maxsize, clock=clock, **(relay_options or {}))
        if config.upstream_url
        else None
    )
    watcher = watcher or TranscriptWatcher(store)
    if relay is not None and watcher.emit is None:
        watcher.emit = relay.emit
        watcher.relay_fill = relay.fill
        watcher.enable_relay_offsets(config.data_dir)  # restarts must not re-send usage (SPEC 13.6)

    metrics = ServerMetrics(clock=clock)
    if sysmon is None and config.sysmon:
        sysmon = Sampler(config.data_dir, store.redactor.redact, clock=clock)
    elif not config.sysmon:
        sysmon = None
    live_eff = LiveEfficiency(
        config, store, clock=clock, hub_url=hub_origin(config.upstream_url),
        hub_token=config.upstream_token, client_factory=relay.client_factory if relay is not None else None,
    )
    live_eff.attach()  # observe usage and context updates (13.10); hooks are fed from /hook

    summarizer = Summarizer.from_config(config, store.redactor, clock=clock, **(summary_options or {}))
    if summarizer is not None:
        store.summary_sink = summarizer.submit

        def deliver(req: SummaryRequest, text: str, model: str, usage: Optional[dict] = None) -> None:
            body = store.add_summary(req.session_id, req.agent_id, req.t_start, req.t_end, text, model)
            if usage is not None:  # the summary call's own tokens: tooling, not an agent's (SPEC 13.6)
                data = dict(usage, model=usage.get("model") or model, ts=clock())
                if store.add_usage(req.session_id, SUMMARIZER, data) and relay is not None:
                    relay.emit("usage", req.session_id, {
                        "agent_id": SUMMARIZER, "agent_type": SUMMARIZER, **data,
                    })
            if body is not None and relay is not None:  # relay exactly what the row shows
                relay.emit("summary", req.session_id, {
                    "agent_id": req.agent_id, "t_start": req.t_start, "t_end": req.t_end,
                    "text": body, "model": model,
                })

        summarizer.deliver = deliver

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if start_loops:
            tasks.append(asyncio.create_task(lag_sampler(app.state.metrics)))
            if app.state.sysmon is not None:
                app.state.sysmon.start()
            tasks.append(asyncio.create_task(_forever(app.state.broadcast_tick, BROADCAST_INTERVAL)))
            tasks.append(asyncio.create_task(_forever(app.state.housekeeping_tick, HOUSEKEEPING_INTERVAL)))
            tasks.append(asyncio.create_task(_forever(lambda: asyncio.to_thread(live_eff.maintain), EFFICIENCY_INTERVAL)))
            if app.state.relay is not None:
                tasks.append(asyncio.create_task(_relay_forever(lambda: live_eff.run_hub(asyncio.sleep))))
                tasks.append(asyncio.create_task(_relay_forever(app.state.relay.run_writer)))
                tasks.append(asyncio.create_task(_relay_forever(app.state.relay.run_sender)))
            if app.state.summarizer is not None:
                tasks.append(asyncio.create_task(_relay_forever(app.state.summarizer.run)))
        try:
            yield
        finally:
            if app.state.sysmon is not None:
                app.state.sysmon.stop()
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            if app.state.relay is not None:
                try:
                    app.state.relay.flush_sync()
                except Exception:
                    log.debug("relay flush failed", exc_info=True)
            try:
                live_eff.flush()
            except Exception:
                log.debug("efficiency baseline flush failed", exc_info=True)
            try:
                watcher.flush_offsets()
            except Exception:
                log.debug("offset flush failed", exc_info=True)
            for hook in list(app.state.on_shutdown):
                try:
                    hook()
                except Exception:
                    log.debug("shutdown hook failed", exc_info=True)

    app = FastAPI(title=APP_ID, version=__version__, lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    state = app.state
    state.config = config
    state.store = store
    state.watcher = watcher
    state.clients = {}  # WebSocket -> Client
    state.relay = relay
    state.summarizer = summarizer
    state.metrics = metrics
    state.sysmon = sysmon
    state.efficiency = live_eff
    state.plan = PlanDetector(
        config.plan_detect,
        plan_path({"CLAUDE_CONFIG_DIR": str(config.claude_config_dir)} if config.claude_config_dir else {},
                  home=Path(os.fspath(home)) if home is not None else None),
        clock=clock,
    )
    state.request_shutdown = lambda: None
    projects = Projects(home)
    state.projects = projects
    state.conventions = Conventions(
        config.data_dir, load_rubric(config.convention_rubric, store.redactor.redact), store.redactor.redact,
        home=projects.home, clock=clock,
    )
    state.on_shutdown = []
    state.idle_since = clock()
    inboxes: dict = {}  # session id -> {socket, token}; memory only, the token never leaves this dict
    nudged: dict = {}  # session id -> time of the last sent nudge

    closers: set = set()

    async def close_ws(ws: Any) -> None:
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(ws.close(WS_INTERNAL_ERROR), SEND_TIMEOUT)

    def drop(client: Client, close: bool = True) -> None:
        """Forget a client, stop its writer and (optionally) close its socket."""
        state.clients.pop(client.ws, None)
        task = client.task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
        if close:
            closer = asyncio.ensure_future(close_ws(client.ws))
            closers.add(closer)
            closer.add_done_callback(closers.discard)

    async def writer(client: Client) -> None:
        try:
            while True:
                text = await client.queue.get()
                await asyncio.wait_for(client.ws.send_text(text), SEND_TIMEOUT)
        except asyncio.CancelledError:
            raise
        except BaseException:
            drop(client)

    def decorate(d: dict) -> dict:
        """Add the session's project (SPEC 11.14). Done here, not in Store: ingestion stays I/O-free and the hub is unaffected."""
        d["project"] = projects.info(d.get("cwd"))
        return d

    def snapshot() -> list:
        return [decorate(d) for d in store.snapshot()]

    keys: dict = {}  # session id -> (cwd, key, resolved at): the loop never resolves a project more than once a minute

    def project_key_of(d: dict) -> Optional[str]:
        sid, cwd, now = d.get("id"), d.get("cwd"), clock()
        hit = keys.get(sid)
        if hit is not None and hit[0] == cwd and 0 <= now - hit[2] < PROJECT_KEY_TTL:
            return hit[1]
        root = projects.root_for(cwd)
        key = project_key(root) if root else None
        if len(keys) > 2048:
            keys.clear()
        keys[sid] = (cwd, key, now)
        return key

    series = LiveSeries(clock, project_key_of)  # SPEC 11.15: memory only, sampled in the broadcast loop, never relayed
    state.series = series

    def sample_series() -> None:
        try:
            if series.due():
                series.sample(store.series_counters())
        except Exception:
            log.debug("series sample failed", exc_info=True)

    def register_client(ws: Any) -> Optional[Client]:
        """Queue the snapshot first, start the writer, then start receiving broadcasts."""
        text = dumps({"type": "snapshot", "version": __version__, "sessions": snapshot()})
        client = Client(ws)
        if text is None:
            return None
        client.queue.put_nowait(text)
        client.task = asyncio.ensure_future(writer(client))
        state.clients[ws] = client
        return client

    async def broadcast_tick() -> None:
        t0 = time.perf_counter()
        worked = False
        try:
            polled = await asyncio.to_thread(watcher.poll)
            sample_series()
            changed = store.pop_changed() | polled
            if not changed or not state.clients:
                return
            worked = True
            sessions = []
            for sid in sorted(changed):
                d = store.session_dict(sid)
                if d is None:
                    continue
                sessions.append(decorate(d))
            if projects.take_names_dirty():  # a new project made another's name ambiguous: resend them all
                sessions = snapshot()
            sessions = [d for d in sessions if dumps(d) is not None or log.warning("skipping session that cannot be encoded")]
            if not sessions:
                return
            text = dumps({"type": "sessions", "sessions": sessions})
            if text is None:
                return
            for client in list(state.clients.values()):
                try:
                    client.queue.put_nowait(text)
                except asyncio.QueueFull:
                    log.info("dropping slow dashboard client")
                    drop(client)
        except Exception:
            log.exception("broadcast tick failed")
        finally:
            if worked:  # idle ticks would only pull the p95 down
                metrics.record_tick(time.perf_counter() - t0)

    def housekeeping_tick() -> None:
        try:
            store.expire_stale()
            store.refresh_drift()  # idle sessions decay to low and the UI hears about it
            now = clock()
            busy = bool(state.clients) or any(s.get("status") == "active" for s in store.snapshot())
            if busy:
                state.idle_since = None
                return
            if state.idle_since is None:
                state.idle_since = now
            elif config.idle_minutes > 0 and now - state.idle_since >= config.idle_minutes * 60:
                log.info("idle for %d minutes; shutting down", config.idle_minutes)
                # no tab can reconnect after an idle stop: tell the launcher not to wait for one
                with contextlib.suppress(OSError):
                    (config.data_dir / "browser.opened").unlink()
                state.request_shutdown()
        except Exception:
            log.exception("housekeeping tick failed")

    state.broadcast_tick = broadcast_tick
    state.housekeeping_tick = housekeeping_tick
    state.register_client = register_client

    @app.get("/health")
    async def health() -> dict:
        snap = store.snapshot()
        out = {
            "app": APP_ID,
            "version": __version__,
            "clients": len(state.clients),
            "sessions": len(snap),
            "active": sum(1 for s in snap if s.get("status") == "active"),
        }
        if nudge_enabled():
            out["nudge"] = True  # the launcher sends the inbox token only to a server that says so
        if state.relay is not None:
            out["relay"] = state.relay.stats()
        if state.summarizer is not None:
            out["summaries"] = state.summarizer.stats()
        return out

    async def read_capped(request: Request) -> Optional[bytes]:
        """Request body, or None when it exceeds MAX_HOOK_BYTES (checked without buffering it all)."""
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_HOOK_BYTES:
            return None
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_HOOK_BYTES:
                return None
            chunks.append(chunk)
        return b"".join(chunks)

    def nudge_enabled() -> bool:
        return config.nudge and nudge_mod.NUDGE_SUPPORTED  # read at call time so tests can switch it

    def remember_inbox(sid: str, inbox: Any) -> None:
        sock = inbox.get("socket") if isinstance(inbox, dict) else None
        token = inbox.get("token") if isinstance(inbox, dict) else None
        ok = (nudge_enabled() and isinstance(sock, str) and isinstance(token, str) and token
              and 0 < len(sock) < 1024 and sock.startswith("/"))
        if ok:
            if sid not in inboxes and len(inboxes) >= INBOX_MAX:
                old = next(iter(inboxes))
                inboxes.pop(old)
                nudged.pop(old, None)
            inboxes[sid] = {"socket": sock, "token": token}
        else:
            inboxes.pop(sid, None)
        store.set_inbox(sid, bool(ok))

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        t0 = time.perf_counter()
        try:
            body = await read_capped(request)
            event = json.loads(body) if body is not None else None
            if isinstance(event, dict):
                inbox = event.pop("_ccmc_inbox", None)  # private field: strip before anything else sees it
                sid = store.ingest(event)
                if sid is not None and event.get("hook_event_name") == "SessionStart":
                    remember_inbox(sid, inbox)
                if sid is not None:
                    live_eff.feed(sid, event)
                if sid is not None and state.relay is not None:
                    state.relay.emit("hook", sid, event)
        except Exception:
            log.debug("hook ignored", exc_info=True)
        metrics.record_hook(time.perf_counter() - t0)
        return Response(status_code=200)

    def rubric_value() -> str:
        if config.convention_rubric is None:
            return "built-in"
        warning = state.conventions.rubric.warning
        return f"custom, but ignored: {warning}" if warning else "custom"

    @app.get("/api/settings")
    async def settings() -> JSONResponse:
        """Read-only view of the running server for the Settings page (SPEC 11.13). Built from an explicit allowlist."""
        origin = hub_origin(config.upstream_url)
        hub = {"configured": False, "url": None, "history_url": None, "queued": 0, "spooled_bytes": 0, "dropped": 0, "last_ok": None, "state": "off"}
        if state.relay is not None:
            st = state.relay.stats()
            hub.update(configured=True, url=origin, history_url=origin + "/history" if origin else None,
                       queued=st["queued"], spooled_bytes=st["spooled_bytes"], dropped=st["dropped"], last_ok=st["last_ok"])
            hub["state"] = "error" if st["last_error"] else ("ok" if st["last_ok"] is not None else "waiting")
        if state.summarizer is not None:
            summaries = "on"
        else:
            summaries = "key file missing" if config.summaries else "off"
        rows = [
            ("port", "CCMC_PORT", "Port", config.port),
            ("data_dir", "CCMC_DATA_DIR", "Data folder", str(config.data_dir)),
            ("idle_minutes", "CCMC_IDLE_MINUTES", "Idle shutdown (minutes, 0 = never)", config.idle_minutes),
            ("stale_minutes", "CCMC_STALE_MINUTES", "Stale after (minutes)", config.stale_minutes),
            ("context_window", "CCMC_CONTEXT_WINDOW", "Default context window (tokens)", config.context_window),
            ("max_calls", "CCMC_MAX_CALLS", "Calls kept per agent", config.max_calls),
            ("max_field_chars", "CCMC_MAX_FIELD_CHARS", "Field length limit (characters)", config.max_field_chars),
            ("redact_file", "CCMC_REDACT_FILE", "Custom redaction file", "set" if config.redact_file else "not set"),
            ("pricing", "CCMC_PRICING_FILE", "Price table", "custom" if config.pricing_file else "built-in"),
            ("summaries", "CCMC_SUMMARIES", "AI summaries", summaries),
            ("summary_model", "CCMC_SUMMARY_MODEL", "Summary model", config.summary_model),
            ("summary_max_per_hour", "CCMC_SUMMARY_MAX_PER_HOUR", "Summary cap per hour", config.summary_max_per_hour),
            ("nudge", "CCMC_NUDGE", "Nudge", "on" if nudge_enabled() else "off"),
            ("convention_rubric", "CCMC_CONVENTION_RUBRIC", "Convention rubric", rubric_value()),
            ("sysmon", "CCMC_SYSMON", "System monitor", "on" if state.sysmon is not None else "off"),
        ]
        body = {
            "version": __version__,
            "hub": hub,
            "server": [{"key": k, "env": e, "label": label, "value": v} for k, e, label, v in rows],
        }
        return JSONResponse(body, headers={"Cache-Control": "no-store"})

    def session_count() -> int:
        """Sessions in memory without building the snapshot (that sorts and serialises every session)."""
        with store._lock:
            return len(store._sessions)

    @app.get("/api/plan")
    async def plan() -> JSONResponse:
        """Billing kind and plan name (SPEC 13.6). Two fields only; the file is read in a thread, cached 10 minutes."""
        return JSONResponse(await asyncio.to_thread(state.plan.get), headers=NO_STORE)

    @app.get("/api/system")
    async def system() -> JSONResponse:
        """Host and server metrics for the System page and the Live "This Mac" card (SPEC 11.16)."""
        out: dict = {"host": None, "series": {}, "processes": None, "alerts": []}
        if state.sysmon is not None:
            if not state.sysmon.has_sample:  # first request before the worker's first pass: sample once, off the loop
                await asyncio.to_thread(state.sysmon.sample)
            out.update(state.sysmon.snapshot())
        server = metrics.snapshot()
        rss = await asyncio.to_thread(own_rss_bytes)
        relay = {"configured": False, "queued": 0, "spooled_bytes": 0, "dropped": 0, "last_ok": None, "state": "off"}
        if state.relay is not None:  # the same fields as /api/settings; never error text
            st = state.relay.stats()
            relay.update(configured=True, queued=st["queued"], spooled_bytes=st["spooled_bytes"], dropped=st["dropped"],
                         last_ok=st["last_ok"], state="error" if st["last_error"] else ("ok" if st["last_ok"] is not None else "waiting"))
        server.update(
            rss_bytes=rss, threads=own_threads(), clients=len(state.clients),
            transcripts=len(getattr(watcher, "_tails", {})), sessions=session_count(), relay=relay,
        )
        out["server"] = server
        return JSONResponse(out, headers=NO_STORE)
    @app.get("/api/efficiency/live")
    async def efficiency_live() -> JSONResponse:
        """Live task efficiency (SPEC 13.10), computed off the event loop."""
        try:
            body = await asyncio.to_thread(live_eff.snapshot)
        except Exception:
            log.exception("live efficiency failed")
            body = {"tasks": [], "baseline": {"source": "none", "size": 0}}
        return JSONResponse(body, headers=NO_STORE)

    @app.get("/api/sessions")
    async def sessions() -> list:
        return snapshot()

    @app.get("/api/live/series")
    async def live_series() -> JSONResponse:
        return JSONResponse(series.to_dict(), headers=NO_STORE)

    # ---- projects and conventions (SPEC 11.14)

    def jerr(status: int, message: str) -> JSONResponse:
        return JSONResponse({"error": message}, status_code=status, headers=NO_STORE)

    def project_index() -> dict:
        """key -> project, from the sessions the store has seen. A client can only name a key we produced."""
        idx: dict = {}
        for s in store.snapshot():  # newest first
            hit = projects.lookup(s.get("cwd"))
            if hit is None:
                continue
            key, name, root = hit
            p = idx.get(key)
            if p is None:
                p = idx[key] = {"key": key, "name": name, "root": root, "sessions": [], "active": 0, "latest": 0.0}
            p["sessions"].append(s["id"])
            p["active"] += 1 if s.get("status") == "active" else 0
            p["latest"] = max(p["latest"], s.get("started") or 0.0)
        return idx

    def rubric_info() -> dict:
        r = state.conventions.rubric
        return {"source": r.source, "warning": r.warning}

    @app.get("/api/projects")
    async def projects_list() -> JSONResponse:
        idx = project_index()
        ordered = sorted(idx.values(), key=lambda p: (p["active"] == 0, -p["latest"]))

        def build() -> list:
            out = []
            for p in ordered:
                try:
                    facts = state.conventions.summary(p["key"], p["root"])
                except Exception:
                    log.exception("convention scan failed")
                    facts = {"score": None, "grade": None, "scanned_at": None, "missing_core": None, "partial": False}
                out.append({"key": p["key"], "name": p["name"], "root": p["root"], "sessions": p["sessions"],
                            "active": p["active"], **facts})
            return out

        items = await asyncio.to_thread(build)
        return JSONResponse({"projects": items, "rubric": rubric_info()}, headers=NO_STORE)

    async def read_json(request: Request) -> Any:
        """The JSON object body of a write request, or a ready error response (415, 413 or 400)."""
        if request.headers.get("content-type", "").split(";")[0].strip().lower() != "application/json":
            return jerr(415, "Content-Type must be application/json")
        declared = request.headers.get("content-length", "")
        if declared.isdigit() and int(declared) > MAX_CONV_BODY:
            return jerr(413, "Request body is too large")
        chunks, total = [], 0
        async for chunk in request.stream():
            total += len(chunk)
            if total > MAX_CONV_BODY:
                return jerr(413, "Request body is too large")
            chunks.append(chunk)
        try:
            body = json.loads(b"".join(chunks))
        except (ValueError, RecursionError):  # RecursionError: absurdly nested arrays
            return jerr(400, "Request body must be JSON")
        return body if isinstance(body, dict) else jerr(400, "Request body must be a JSON object")

    async def conv_run(request: Request, key: str, fields: dict, call: Callable) -> JSONResponse:
        """Common path of the conventions POSTs: content type, size, key, field types, then the call in a worker thread."""
        body = await read_json(request)
        if isinstance(body, JSONResponse):
            return body
        proj = project_index().get(key)
        if proj is None:
            return jerr(404, "not found")
        args = []
        for name, kind in fields.items():
            value = body.get(name)
            if not isinstance(value, kind) or (kind is str and not value):
                return jerr(400, f"{name} is missing or invalid")
            args.append(value)
        try:
            out = await asyncio.to_thread(call, proj, *args)
        except ConventionError as exc:
            return jerr(exc.status, exc.message)
        except Exception:
            log.exception("conventions request failed")
            return jerr(500, "Something went wrong")
        return JSONResponse(out, headers=NO_STORE)

    @app.get("/api/projects/{key}/conventions")
    async def conventions_report(key: str, rescan: str = "") -> JSONResponse:
        proj = project_index().get(key)
        if proj is None:
            return jerr(404, "not found")
        try:
            out = await asyncio.to_thread(
                state.conventions.report, key, proj["name"], proj["root"], rescan.lower() in ("1", "true", "yes"))
        except Exception:
            log.exception("convention scan failed")
            return jerr(500, "Something went wrong")
        return JSONResponse(out, headers=NO_STORE)

    @app.post("/api/projects/{key}/conventions/na")
    async def conventions_na(key: str, request: Request) -> JSONResponse:
        return await conv_run(request, key, {"rule_id": str, "na": bool}, lambda p, rule_id, na: state.conventions.set_na(
            key, p["name"], p["root"], rule_id, na))

    @app.post("/api/projects/{key}/conventions/preview")
    async def conventions_preview(key: str, request: Request) -> JSONResponse:
        return await conv_run(request, key, {"rule_id": str, "target": str}, lambda p, rule_id, target: state.conventions.preview(
            key, p["root"], rule_id, target))

    @app.post("/api/projects/{key}/conventions/apply")
    async def conventions_apply(key: str, request: Request) -> JSONResponse:
        return await conv_run(
            request, key, {"rule_id": str, "target": str, "before_sha256": str, "text_sha256": str},
            lambda p, rule_id, target, before, text: state.conventions.apply(
                key, p["name"], p["root"], rule_id, target, before, text))

    @app.post("/api/projects/{key}/conventions/undo")
    async def conventions_undo(key: str, request: Request) -> JSONResponse:
        return await conv_run(request, key, {"target": str}, lambda p, target: state.conventions.undo(
            key, p["name"], p["root"], target))

    @app.get("/api/sessions/{session_id}/calls/{call_id}")
    async def call_detail(session_id: str, call_id: str) -> dict:
        detail = store.call_detail(session_id, call_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="not found")
        return detail

    @app.get("/api/sessions/{session_id}/handoff")
    async def handoff(session_id: str) -> dict:
        out = store.handoff(session_id)
        if out is None:
            raise HTTPException(status_code=404, detail="not found")
        return out

    @app.post("/api/sessions/{session_id}/nudge")
    async def nudge(session_id: str) -> Response:
        if not nudge_enabled():
            raise HTTPException(status_code=404, detail="not found")
        text = store.nudge_text(session_id)
        if text is None:
            raise HTTPException(status_code=404, detail="not found")
        target = inboxes.get(session_id)
        if target is None:
            return JSONResponse({"ok": False, "error": "this session has no inbox"})
        wait = NUDGE_INTERVAL - (clock() - nudged.get(session_id, -NUDGE_INTERVAL))
        if wait > 0:
            secs = int(wait) + 1
            return JSONResponse({"ok": False, "error": f"Already sent; try again in {secs} s", "retry_after": secs},
                                status_code=429, headers={"Retry-After": str(secs)})
        nudged[session_id] = clock()  # claim the slot before awaiting: two quick clicks send one note
        try:
            await asyncio.wait_for(
                asyncio.to_thread(nudge_mod.send, target["socket"], target["token"], text, nudge_mod.TIMEOUT),
                nudge_mod.TIMEOUT + 0.5)
        except nudge_mod.NudgeError as exc:
            nudged.pop(session_id, None)
            return JSONResponse({"ok": False, "error": str(exc)})
        except Exception:  # includes a wait_for timeout; never echo exception text (it could carry the socket path)
            nudged.pop(session_id, None)
            return JSONResponse({"ok": False, "error": "could not send the note"})
        return JSONResponse({"ok": True})

    @app.websocket("/ws")
    async def ws_endpoint(ws: WebSocket) -> None:
        await ws.accept()
        client = register_client(ws)
        if client is None:
            await close_ws(ws)
            return
        try:
            while True:
                message = await ws.receive()  # keepalive pings and anything else are ignored
                if message["type"] == "websocket.disconnect":
                    break
        except Exception:
            pass
        finally:
            drop(client, close=False)

    @app.get("/", include_in_schema=False)
    async def index() -> Response:
        page = STATIC_DIR / "index.html"
        if page.is_file():
            return HTMLResponse(page.read_text(encoding="utf-8"), headers=NO_CACHE)
        return HTMLResponse(PLACEHOLDER, headers=NO_CACHE)

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", ImmutableStaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    app.add_middleware(GuardMiddleware, config=config)
    app.add_middleware(ProjectsNoStore)  # outermost: it also covers the guard's own 403
    return app


async def _forever(tick: Callable, interval: float) -> None:
    while True:
        try:
            result = tick()
            if asyncio.iscoroutine(result):
                await result
        except Exception:
            log.exception("loop tick failed")
        await asyncio.sleep(interval)


async def _relay_forever(run: Callable) -> None:
    while True:
        try:
            await run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("relay task crashed; restarting")
            await asyncio.sleep(1.0)

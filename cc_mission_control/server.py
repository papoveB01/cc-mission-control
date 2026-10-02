"""FastAPI event server (SPEC sections 9 and 10.1) with the optional upstream relay (section 13)."""

from __future__ import annotations

import asyncio
import contextlib
import getpass
import hashlib
import json
import logging
import socket
import time
from pathlib import Path
from typing import Any, Callable, Optional

from fastapi import FastAPI, HTTPException, Request, Response, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from . import APP_ID, __version__
from .config import Config
from .state import Store
from .transcript import TranscriptWatcher

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
BROADCAST_INTERVAL = 0.15
HOUSEKEEPING_INTERVAL = 30.0
SEND_TIMEOUT = 1.0
CLIENT_QUEUE = 16
MAX_HOOK_BYTES = 8 * 1024 * 1024
RELAY_MAX = 5000
RELAY_TIMEOUT = 5.0
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


def machine_id() -> str:
    return hashlib.sha256(socket.gethostname().encode("utf-8", "replace")).hexdigest()[:12]


def os_user() -> str:
    try:
        return getpass.getuser()
    except Exception:
        return ""


class Relay:
    """Bounded fire-and-forget queue to the hosted upstream. Ingestion never waits on it."""

    def __init__(self, config: Config, redactor: Any, maxsize: int = RELAY_MAX) -> None:
        self.url = config.upstream_url or ""
        self.token = config.upstream_token
        self.redactor = redactor
        self.queue: asyncio.Queue = asyncio.Queue(maxsize=maxsize)
        self.machine_id = machine_id()
        self.user = os_user()
        self.dropped = 0

    def enqueue(self, event: Any) -> bool:
        if not isinstance(event, dict):
            return False
        item = self.redactor.value(event)
        item["machine_id"] = self.machine_id
        item["user"] = self.user
        try:
            self.queue.put_nowait(item)
        except asyncio.QueueFull:
            self.dropped += 1
            return False
        return True

    async def send_one(self, client: Any, item: dict) -> None:
        headers = {"Authorization": "Bearer " + self.token} if self.token else {}
        try:
            await client.post(self.url, json=item, headers=headers)
        except Exception as exc:
            log.warning("upstream relay failed (%s); event dropped", type(exc).__name__)

    async def run(self) -> None:
        import httpx

        async with httpx.AsyncClient(timeout=RELAY_TIMEOUT) as client:
            while True:
                item = await self.queue.get()
                await self.send_one(client, item)


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


def create_app(
    config: Config,
    *,
    store: Store | None = None,
    watcher: TranscriptWatcher | None = None,
    clock: Callable[[], float] = time.time,
    start_loops: bool = True,
    relay_maxsize: int = RELAY_MAX,
) -> FastAPI:
    store = store or Store(config, clock=clock)
    watcher = watcher or TranscriptWatcher(store)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        tasks: list[asyncio.Task] = []
        if start_loops:
            tasks.append(asyncio.create_task(_forever(app.state.broadcast_tick, BROADCAST_INTERVAL)))
            tasks.append(asyncio.create_task(_forever(app.state.housekeeping_tick, HOUSEKEEPING_INTERVAL)))
            if app.state.relay is not None:
                tasks.append(asyncio.create_task(_relay_forever(app.state.relay)))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
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
    state.relay = Relay(config, store.redactor, relay_maxsize) if config.upstream_url else None
    state.request_shutdown = lambda: None
    state.on_shutdown = []
    state.idle_since = clock()

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

    def register_client(ws: Any) -> Optional[Client]:
        """Queue the snapshot first, start the writer, then start receiving broadcasts."""
        text = dumps({"type": "snapshot", "version": __version__, "sessions": store.snapshot()})
        client = Client(ws)
        if text is None:
            return None
        client.queue.put_nowait(text)
        client.task = asyncio.ensure_future(writer(client))
        state.clients[ws] = client
        return client

    async def broadcast_tick() -> None:
        try:
            polled = await asyncio.to_thread(watcher.poll)
            changed = store.pop_changed() | polled
            if not changed or not state.clients:
                return
            sessions = []
            for sid in sorted(changed):
                d = store.session_dict(sid)
                if d is None:
                    continue
                if dumps(d) is None:
                    log.warning("skipping session that cannot be encoded")
                    continue
                sessions.append(d)
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

    def housekeeping_tick() -> None:
        try:
            store.expire_stale()
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
        return {
            "app": APP_ID,
            "version": __version__,
            "clients": len(state.clients),
            "sessions": len(snap),
            "active": sum(1 for s in snap if s.get("status") == "active"),
        }

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

    @app.post("/hook")
    async def hook(request: Request) -> Response:
        try:
            body = await read_capped(request)
            event = json.loads(body) if body is not None else None
            if isinstance(event, dict):
                store.ingest(event)
                if state.relay is not None:
                    state.relay.enqueue(event)
        except Exception:
            log.debug("hook ignored", exc_info=True)
        return Response(status_code=200)

    @app.get("/api/sessions")
    async def sessions() -> list:
        return store.snapshot()

    @app.get("/api/sessions/{session_id}/calls/{call_id}")
    async def call_detail(session_id: str, call_id: str) -> dict:
        detail = store.call_detail(session_id, call_id)
        if detail is None:
            raise HTTPException(status_code=404, detail="not found")
        return detail

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
            return HTMLResponse(page.read_text(encoding="utf-8"))
        return HTMLResponse(PLACEHOLDER)

    if (STATIC_DIR / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=STATIC_DIR / "assets"), name="assets")

    app.add_middleware(GuardMiddleware, config=config)
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


async def _relay_forever(relay: Relay) -> None:
    while True:
        try:
            await relay.run()
        except asyncio.CancelledError:
            raise
        except Exception:
            log.exception("relay crashed; restarting")
            await asyncio.sleep(1.0)

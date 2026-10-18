"""FastAPI app for the hub (SPEC 13.2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from ..config import Config, HubConfig
from ..redact import Redactor
from . import auth
from .service import BadCursor, HubService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 2 * 1024 * 1024
MAX_LOGIN_BODY = 4 * 1024
LOGIN_FAILURES = 10
LOGIN_WINDOW = 60.0
MAINTENANCE_INTERVAL = 24 * 3600
SECURITY_HEADERS = {
    "Content-Security-Policy": "default-src 'self'; frame-ancestors 'none'",
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

PLACEHOLDER = (
    "<!doctype html><meta charset=utf-8><title>cc-mission-control hub</title>"
    "<h1>cc-mission-control hub</h1><p>The history UI bundle is not built yet.</p>"
)


async def read_body(request: Request, cap: int) -> bytes:
    """The request body, or 413 as soon as it passes cap bytes (declared or streamed)."""
    declared = request.headers.get("content-length")
    if declared and declared.isdigit() and int(declared) > cap:
        raise HTTPException(413, "body too large")
    chunks, size = [], 0
    async for chunk in request.stream():
        size += len(chunk)
        if size > cap:
            raise HTTPException(413, "body too large")
        chunks.append(chunk)
    return b"".join(chunks)


def create_app(
    cfg: HubConfig,
    store_config: Optional[Config] = None,
    redactor: Optional[Redactor] = None,
    static_dir: Path = STATIC_DIR,
) -> FastAPI:
    problem = cfg.problem()
    if problem:
        raise ValueError(problem)
    token = cfg.token or ""
    service = HubService(cfg, store_config, redactor)

    @contextlib.asynccontextmanager
    async def lifespan(app: FastAPI):
        async def daily() -> None:
            while True:
                try:
                    await run_in_threadpool(service.maintenance)
                except Exception:
                    log.exception("hub maintenance failed")
                await asyncio.sleep(MAINTENANCE_INTERVAL)

        task = asyncio.create_task(daily())
        try:
            yield
        finally:
            task.cancel()
            with contextlib.suppress(asyncio.CancelledError):
                await task
            service.close()

    app = FastAPI(title="cc-mission-control hub", lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None)
    app.state.service = service
    failures: deque = deque()  # monotonic times of recent failed logins, across all clients

    @app.middleware("http")
    async def guard(request: Request, call_next):
        path = request.url.path
        host = request.headers.get("host")
        if not (path == "/health" and auth.is_loopback(host)) and not auth.host_allowed(host, cfg.hosts, cfg.tailnet_hosts):
            return JSONResponse({"detail": "forbidden host"}, status_code=403)
        origin = request.headers.get("origin")
        if origin is not None and (path.startswith("/api/") or path == "/login"):
            if not auth.origin_allowed(origin, cfg.hosts, cfg.tailnet_hosts, host):
                return JSONResponse({"detail": "forbidden origin"}, status_code=403)
        response = await call_next(request)
        if path in ("/history", "/login") or path.startswith("/assets/"):
            response.headers.update(SECURITY_HEADERS)
        return response

    def unauthorized() -> HTTPException:
        return HTTPException(401, "unauthorized", headers={"WWW-Authenticate": "Bearer"})

    def require_bearer(request: Request) -> None:
        if not auth.bearer_ok(token, request.headers.get("authorization")):
            raise unauthorized()

    def require_user(request: Request) -> None:
        if auth.bearer_ok(token, request.headers.get("authorization")):
            return
        if not auth.cookie_ok(token, request.cookies.get(auth.COOKIE_NAME)):
            raise unauthorized()

    @app.get("/health")
    def health() -> dict:
        return service.health()

    @app.post("/login")
    async def login(request: Request) -> JSONResponse:
        now = time.monotonic()
        while failures and now - failures[0] > LOGIN_WINDOW:
            failures.popleft()
        if len(failures) >= LOGIN_FAILURES:
            raise HTTPException(429, "too many failed logins", headers={"Retry-After": str(int(LOGIN_WINDOW))})
        try:
            body = json.loads(await read_body(request, MAX_LOGIN_BODY) or b"{}")
        except (ValueError, RecursionError):
            raise HTTPException(400, "invalid JSON")
        supplied = body.get("token") if isinstance(body, dict) else None
        if not isinstance(supplied, str) or not auth.token_ok(token, supplied):
            failures.append(now)
            raise unauthorized()
        response = JSONResponse({"ok": True})
        response.set_cookie(
            auth.COOKIE_NAME, auth.make_cookie(token), max_age=auth.COOKIE_DAYS * 86400, httponly=True,
            samesite="strict", secure=request.url.scheme == "https", path="/",
        )
        return response

    @app.post("/ingest", dependencies=[Depends(require_bearer)])
    async def ingest(request: Request) -> dict:
        raw = await read_body(request, MAX_BODY)
        try:
            items = json.loads(raw)
        except (ValueError, RecursionError):  # not parseable (or absurdly nested): a bad body, not a bad item
            raise HTTPException(400, "invalid JSON")
        if not isinstance(items, list):
            raise HTTPException(400, "expected a JSON array of envelopes")

        def work() -> dict:
            records, invalid = service.parse(items)
            accepted, duplicates, rejected = service.ingest(records)
            return {"accepted": accepted, "duplicates": duplicates, "invalid": invalid + rejected}

        return await run_in_threadpool(work)

    api = "/api/history"
    guarded = [Depends(require_user)]

    @app.get(f"{api}/sessions", dependencies=guarded)
    def sessions(
        machine: Optional[str] = None,
        project: Optional[str] = None,
        q: Optional[str] = None,
        start_from: Optional[float] = Query(None, alias="from"),
        start_to: Optional[float] = Query(None, alias="to"),
        has_file: Optional[str] = None,
        cursor: Optional[str] = None,
        limit: int = 50,
    ) -> dict:
        try:
            return service.list_sessions(machine, project, q, start_from, start_to, has_file, cursor, limit)
        except BadCursor:
            raise HTTPException(400, "invalid cursor")

    @app.get(f"{api}/sessions/{{machine_id}}/{{session_id}}", dependencies=guarded)
    def session(machine_id: str, session_id: str) -> dict:
        found = service.replay(machine_id, session_id)
        if found is None:
            raise HTTPException(404, "session not found")
        return found

    @app.get(f"{api}/sessions/{{machine_id}}/{{session_id}}/calls/{{call_id}}", dependencies=guarded)
    def call(machine_id: str, session_id: str, call_id: str) -> dict:
        found = service.call_detail(machine_id, session_id, call_id)
        if found is None:
            raise HTTPException(404, "call not found")
        return found

    @app.get(f"{api}/sessions/{{machine_id}}/{{session_id}}/files", dependencies=guarded)
    def session_files(machine_id: str, session_id: str) -> dict:
        rows = service.session_files(machine_id, session_id)
        if rows is None:
            raise HTTPException(404, "session not found")
        return {"files": rows}

    @app.get(f"{api}/sessions/{{machine_id}}/{{session_id}}/permissions", dependencies=guarded)
    def session_permissions(machine_id: str, session_id: str) -> dict:
        return {"permissions": service.session_permissions(machine_id, session_id)}

    @app.get(f"{api}/files", dependencies=guarded)
    def files(
        path: Optional[str] = None, prefix: Optional[str] = None, limit: int = 200, cursor: Optional[str] = None
    ) -> dict:
        if bool(path) == bool(prefix):
            raise HTTPException(400, "give exactly one of path or prefix")
        try:
            return service.lookup_files(path, prefix, limit, cursor)
        except BadCursor:
            raise HTTPException(400, "invalid cursor")

    @app.get(f"{api}/machines", dependencies=guarded)
    def machines() -> dict:
        return {"machines": service.machines()}

    # The page and bundle carry no data, so they are served without a token; the login form lives there.
    @app.get("/history", response_class=HTMLResponse)
    def history() -> Any:
        page = static_dir / "history.html"
        html = page.read_text(encoding="utf-8") if page.is_file() else PLACEHOLDER
        return HTMLResponse(html, headers={"Cache-Control": "no-cache"})

    if (static_dir / "assets").is_dir():
        app.mount("/assets", StaticFiles(directory=static_dir / "assets"), name="assets")
    return app

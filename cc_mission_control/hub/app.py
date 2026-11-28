"""FastAPI app for the hub (SPEC 13.2)."""

from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import math
import time
from collections import deque
from pathlib import Path
from typing import Any, Optional

from fastapi import Depends, FastAPI, HTTPException, Query, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, Response
from fastapi.staticfiles import StaticFiles

from ..config import Config, HubConfig
from ..redact import Redactor
from . import auth, reports
from .service import DAY, USAGE_DEFAULT_DAYS, USAGE_SKEW, USAGE_GROUPS, BadCursor, HubService

log = logging.getLogger(__name__)

STATIC_DIR = Path(__file__).parent / "static"
MAX_BODY = 2 * 1024 * 1024
MAX_LOGIN_BODY = 4 * 1024
LOGIN_FAILURES = 10
MULTI_MAX = 50
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

    def number(q: Any, name: str) -> Optional[float]:
        raw = q.get(name)
        if raw is None or raw == "":
            return None
        try:
            value = float(raw)
        except ValueError:
            raise HTTPException(400, f"invalid {name}")
        if not math.isfinite(value) or not 0 <= value < 4e9:
            raise HTTPException(400, f"invalid {name}")
        return value

    def tz_of(q: Any) -> int:
        try:
            tz = int(q.get("tz") or 0)
        except ValueError:
            raise HTTPException(400, "invalid tz")
        if not -840 <= tz <= 840:
            raise HTTPException(400, "invalid tz")
        return tz

    def multi(q: Any, name: str, allowed: Optional[tuple] = None) -> list:
        """A comma-separated multi-value param as a list of distinct values (any matches); bad values are a 400."""
        out: list = []
        for part in (q.get(name) or "").split(","):
            part = part.strip()
            if not part or part in out:
                continue
            if len(part) > 200 or (allowed is not None and part not in allowed):
                raise HTTPException(400, f"invalid {name}")
            out.append(part)
        if len(out) > MULTI_MAX:
            raise HTTPException(400, f"too many {name} values")
        return out

    def session_of(q: Any) -> Optional[tuple]:
        """`machine_id:session_id` as a pair, or None when absent."""
        raw = q.get("session")
        if not raw:
            return None
        machine_id, sep, session_id = raw.partition(":")
        if not sep or not machine_id or not session_id or len(raw) > 400:
            raise HTTPException(400, "invalid session")
        return machine_id, session_id

    @app.get(f"{api}/usage", dependencies=guarded)
    def usage(request: Request) -> dict:
        # parsed by hand so every bad value is a 400 (FastAPI would answer 422 or accept "nan")
        q = request.query_params

        end, start = number(q, "to"), number(q, "from")
        if end is None:
            now = service.clock()
            end = now + USAGE_SKEW  # slack, so clock skew between machines does not hide fresh rows
            if start is None:
                start = now - USAGE_DEFAULT_DAYS * DAY
        if start is None:
            start = end - USAGE_DEFAULT_DAYS * DAY
        if start > end:
            raise HTTPException(400, "from is after to")
        group_by = q.get("group_by") or "agent_type"
        if group_by not in USAGE_GROUPS:
            raise HTTPException(400, "invalid group_by")
        tz = tz_of(q)
        filters = {name: q.get(name) or None for name in ("machine", "project", "model")}
        if "project" in q and q["project"] == "":
            filters["project"] = ""  # expired sessions group under project ""
        filters["agent_type"] = multi(q, "agent_type") or None  # any of (13.8); 'main' is the main agent
        filters["session"] = session_of(q)
        kind = q.get("kind") or None
        if kind is not None and kind not in reports.TASK_KINDS:
            raise HTTPException(400, "invalid kind")
        filters["kind"] = kind
        return service.usage_report(start, end, group_by, tz, **filters)

    # ---- task and error report (SPEC 13.7); parsed by hand so every bad value is a 400 ----------------------

    def report_filters(q: Any, extra: tuple, enums: dict) -> dict:
        out: dict = {"start_from": number(q, "from"), "start_to": number(q, "to"),
                     "machine": q.get("machine") or None, "project": q.get("project") if "project" in q else None}
        if out["start_from"] is not None and out["start_to"] is not None and out["start_from"] > out["start_to"]:
            raise HTTPException(400, "from is after to")
        for name in extra:
            value = q.get(name) or None
            if value is not None and name in enums and value not in enums[name]:
                raise HTTPException(400, f"invalid {name}")
            out[name] = value
        text = q.get("q") or None
        if text is not None:
            if len(text) > 200:
                raise HTTPException(400, "q is too long")
            out["q"] = text
        return out

    def page_args(q: Any) -> dict:
        raw = q.get("limit")
        try:
            limit = int(raw) if raw not in (None, "") else reports.PAGE
        except ValueError:
            raise HTTPException(400, "invalid limit")
        if limit < 1:
            raise HTTPException(400, "invalid limit")
        return {"cursor": q.get("cursor") or None, "limit": limit}

    def csv_response(body: str, truncated: bool, name: str) -> Response:
        flag = "true" if truncated else "false"
        return Response(body, media_type="text/csv; charset=utf-8", headers={
            "Content-Disposition": f'attachment; filename="{name}.csv"', "X-Truncated": flag, "Truncated": flag,
            "Cache-Control": "no-store"})

    def task_filters(q: Any) -> dict:
        """The shared task filters of 13.8 (tasks, overview, CSV). `agent_id` only counts together with `session`."""
        out = report_filters(q, (), {})
        out["agent_type"] = multi(q, "agent_type") or None
        out["status"] = multi(q, "status", reports.TASK_STATUSES) or None
        out["tool"] = multi(q, "tool") or None
        kind = q.get("kind") or None
        if kind is not None and kind not in reports.TASK_KINDS:
            raise HTTPException(400, "invalid kind")
        out["kind"] = kind
        errors = q.get("errors") or None
        if errors is not None and errors not in reports.ERROR_FILTERS:
            raise HTTPException(400, "invalid errors")
        out["errors"] = errors
        file = q.get("file") or None
        if file is not None and len(file) > 500:
            raise HTTPException(400, "file is too long")
        out["file"] = file
        repeat = q.get("repeat") or None
        if repeat is not None and repeat not in reports.REPEAT_FILTERS:
            raise HTTPException(400, "invalid repeat")
        out["repeat"] = repeat
        out["efficiency"] = multi(q, "efficiency", reports.EFFICIENCY_FILTERS) or None
        out["session"] = session_of(q)
        agent_id = q.get("agent_id") or None
        if agent_id is not None and len(agent_id) > 200:
            raise HTTPException(400, "invalid agent_id")
        out["agent_id"] = agent_id if out["session"] else None
        return out

    TASK_ENUMS = {"status": reports.TASK_STATUSES}
    FAIL_ENUMS = {"kind": reports.FAIL_KINDS, "resolution": reports.RESOLUTIONS}

    @app.get(f"{api}/tasks", dependencies=guarded)
    def tasks(request: Request) -> Any:
        q = request.query_params
        fmt = q.get("format")
        if fmt not in (None, "", "json", "csv"):
            raise HTTPException(400, "invalid format")
        filters = task_filters(q)
        try:
            if fmt == "csv":
                body, truncated = reports.tasks_csv(service, **filters)
                return csv_response(body, truncated, "tasks")
            return service.list_tasks(**filters, **page_args(q))
        except BadCursor:
            raise HTTPException(400, "invalid cursor")

    @app.get(f"{api}/tasks/{{machine_id}}/{{session_id}}/{{task_id}}", dependencies=guarded)
    def task_detail(machine_id: str, session_id: str, task_id: str) -> dict:
        found = service.task_detail(machine_id, session_id, task_id)
        if found is None:
            raise HTTPException(404, "task not found")
        return found

    @app.get(f"{api}/report/overview", dependencies=guarded)
    def report_overview(request: Request) -> dict:
        q = request.query_params
        return service.report_overview(**task_filters(q), tz=tz_of(q))

    @app.get(f"{api}/efficiency/overview", dependencies=guarded)
    def efficiency_overview(request: Request) -> dict:
        q = request.query_params
        f = report_filters(q, (), {})
        f.pop("q", None)
        kind = q.get("kind") or None
        if kind is not None and len(kind) > 200:
            raise HTTPException(400, "invalid kind")
        return service.efficiency_overview(**f, kind=kind, tz=tz_of(q))

    @app.get(f"{api}/efficiency/baseline", dependencies=guarded)
    def efficiency_baseline(request: Request) -> dict:
        q = request.query_params
        kind = q.get("kind") or None
        project = q.get("project") if "project" in q else None
        if (kind is not None and len(kind) > 200) or (project is not None and len(project) > 500):
            raise HTTPException(400, "invalid filter")
        try:
            limit = int(q.get("limit") or 500)
        except ValueError:
            raise HTTPException(400, "invalid limit")
        if limit < 1:
            raise HTTPException(400, "invalid limit")
        return service.efficiency_baseline(kind=kind, project=project, limit=limit)

    @app.get(f"{api}/report/facets", dependencies=guarded)
    def report_facets(request: Request) -> dict:
        f = report_filters(request.query_params, (), {})
        f.pop("q", None)
        return service.report_facets(**f)

    @app.get(f"{api}/failures", dependencies=guarded)
    def failures_(request: Request) -> Any:
        q = request.query_params
        fmt = q.get("format")
        if fmt not in (None, "", "json", "csv"):
            raise HTTPException(400, "invalid format")
        filters = report_filters(q, ("resolution",), FAIL_ENUMS)
        filters["tool"] = multi(q, "tool") or None
        filters["agent_type"] = multi(q, "agent_type") or None
        filters["kind"] = multi(q, "kind", reports.FAIL_KINDS + reports.TASK_KINDS) or None  # failure kind and/or task kind
        filters["session"] = session_of(q)
        agent_id = q.get("agent_id") or None
        if agent_id is not None and len(agent_id) > 200:
            raise HTTPException(400, "invalid agent_id")
        filters["agent_id"] = agent_id if filters["session"] else None
        try:
            if fmt == "csv":
                body, truncated = reports.failures_csv(service, **filters)
                return csv_response(body, truncated, "failures")
            return service.list_failures(**filters, **page_args(q))
        except BadCursor:
            raise HTTPException(400, "invalid cursor")

    @app.get(f"{api}/report/summary", dependencies=guarded)
    def report_summary(request: Request) -> dict:
        f = report_filters(request.query_params, (), {})
        f.pop("q", None)
        return service.report_summary(**f)

    @app.get(f"{api}/report/trend", dependencies=guarded)
    def report_trend(request: Request) -> dict:
        q = request.query_params
        bucket = q.get("bucket") or "day"
        if bucket not in ("day", "week"):
            raise HTTPException(400, "invalid bucket")
        tz = tz_of(q)
        end, start = number(q, "to"), number(q, "from")
        if end is None:
            end = service.clock()
        if start is None:
            start = max(0.0, end - reports.TREND_DEFAULT_DAYS * DAY)
        if start > end:
            raise HTTPException(400, "from is after to")
        machine = q.get("machine") or None
        project = q.get("project") if "project" in q else None
        try:
            return service.report_trend(start, end, bucket=bucket, tz=tz, machine=machine, project=project)
        except ValueError:
            raise HTTPException(400, "range too long for this bucket")

    @app.get(f"{api}/sessions/{{machine_id}}/{{session_id}}/tasks", dependencies=guarded)
    def session_tasks(machine_id: str, session_id: str) -> dict:
        found = service.session_report(machine_id, session_id)
        if found is None:
            raise HTTPException(404, "session not found")
        return found

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

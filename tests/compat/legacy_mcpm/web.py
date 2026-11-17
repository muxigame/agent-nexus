"""HTTP API and web console."""
from __future__ import annotations

import asyncio
import json
import os
import sys
import contextlib
import logging
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any, Callable

from starlette.applications import Starlette
from starlette.background import BackgroundTask
from starlette.concurrency import run_in_threadpool
from starlette.requests import Request
from starlette.responses import FileResponse, JSONResponse, PlainTextResponse, StreamingResponse
from starlette.routing import Route

from . import __version__, logs
from .registry import ICON_TYPES, OBSERVE_EDITABLE_FIELDS, RegistryError, save_override
from .supervisor import Supervisor, SupervisorError

STATIC_DIR = Path(__file__).parent / "static"
ACTIONS = {"start", "stop", "restart"}
CONFIG_FIELDS: dict[str, type] = {"port": int, "autostart": bool, "autorestart": bool, "enabled": bool}
SAFE_METHODS = {"GET", "HEAD", "OPTIONS"}
ICON_HEADERS = {
    "Cache-Control": "max-age=300",
    "X-Content-Type-Options": "nosniff",
    # An SVG opened directly would otherwise run its scripts with the
    # console's origin, which is allowed to start and kill processes.
    "Content-Security-Policy": "default-src 'none'; style-src 'unsafe-inline'; sandbox",
}

log = logging.getLogger(__name__)


class LocalOnly:
    """Same-machine, same-origin use only.

    The console starts and kills processes, and binding to 127.0.0.1 does not
    stop a web page in the user's browser from sending requests to localhost
    (DNS rebinding can even make them look same-origin). So the Host header
    must be a loopback name, and state-changing requests must carry
    ``X-MCPM: 1`` - a custom header forces a CORS preflight this server never
    approves - plus, when the browser sends one, a matching Origin.
    """

    def __init__(self, app: Any, host: str, port: int):
        self.app = app
        self.hosts = {f"{name}:{port}" for name in ("127.0.0.1", "localhost", "[::1]", host)}

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope["type"] == "http":
            headers = {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope["headers"]}
            reason = self._reject(scope["method"], headers)
            if reason:
                await PlainTextResponse(reason, status_code=403)(scope, receive, send)
                return
        await self.app(scope, receive, send)

    def _reject(self, method: str, headers: dict[str, str]) -> str | None:
        if headers.get("host") not in self.hosts:
            return "forbidden: unexpected Host header"
        if method not in SAFE_METHODS:
            if headers.get("x-mcpm") != "1":
                return "forbidden: missing X-MCPM header"
            origin = headers.get("origin")
            if origin and origin.split("://", 1)[-1] not in self.hosts:
                return "forbidden: foreign Origin"
        return None


def _error(exc: Exception) -> JSONResponse:
    if isinstance(exc, SupervisorError) and str(exc).startswith("unknown "):
        status = 404
    elif isinstance(exc, RegistryError):
        status = 400
    else:
        status = 409
    return JSONResponse({"error": str(exc)}, status_code=status)


def _bounded_int(raw: str | None, default: int, low: int, high: int) -> int:
    try:
        return max(low, min(high, int(raw))) if raw is not None else default
    except ValueError:
        return default


def _sse(payload: dict[str, Any]) -> str:
    return f"data: {json.dumps(payload, ensure_ascii=False)}\n\n"


def manager_info(sup: Supervisor) -> dict[str, Any]:
    return {
        "mcpm": __version__,
        "root": str(sup.cfg.root),
        "config": str(sup.cfg.path),
        "config_exists": sup.cfg.path.exists(),
        "scan": sup.cfg.scan,
        "url": sup.cfg.base_url,
        "platform": sys.platform,
        "pid": os.getpid(),
        "builtins": sup.cfg.builtins,
        "tray_visible": bool(getattr(sup, "tray_visible", False)),
    }


def build_app(sup: Supervisor, request_exit: Callable[..., None] | None = None, lifecycle: Any = None) -> LocalOnly:
    cloud_status: dict[str, Any] = {"state": "disabled"}

    @asynccontextmanager
    async def lifespan(_app):
        from .agent import run_configured_agent
        async def connector():
            while True:
                try:
                    if sup.cfg.path.exists():
                        await run_configured_agent(sup.cfg.root, cloud_status)
                    return
                except asyncio.CancelledError:
                    raise
                except Exception as exc:
                    # This used to end the agent silently: the tray kept
                    # running while the machine stayed offline for good.
                    cloud_status.update(state="error", error=type(exc).__name__)
                    log.exception("cloud agent stopped (%s); restarting it in 30s", type(exc).__name__)
                    await asyncio.sleep(30)
        tasks = [asyncio.create_task(connector())]
        if lifecycle is not None:
            tasks.append(asyncio.create_task(lifecycle.beat_loop()))
        try:
            yield
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(asyncio.CancelledError):
                    await task

    async def cloud_info(_request):
        return JSONResponse(cloud_status)
    async def index(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "index.html", headers={"Cache-Control": "no-store"})

    async def favicon(request: Request) -> FileResponse:
        return FileResponse(STATIC_DIR / "icon.png", media_type="image/png", headers={"Cache-Control": "max-age=3600"})

    async def ping(request: Request) -> JSONResponse:
        return JSONResponse(manager_info(sup))

    async def shutdown(request: Request) -> JSONResponse:
        if request_exit is None:
            return JSONResponse({"error": "this manager has no shutdown hook"}, status_code=409)
        # Answer first; the server stops after the response has been sent.
        return JSONResponse({"message": "mcpm stopping; managed servers are stopping too"},
                            background=BackgroundTask(request_exit, "console API /api/manager/shutdown"))

    async def list_services(request: Request) -> JSONResponse:
        services = await run_in_threadpool(sup.snapshots)
        return JSONResponse({"manager": {**manager_info(sup), "cloud": dict(cloud_status)}, "services": services})

    async def rescan(request: Request) -> JSONResponse:
        await run_in_threadpool(sup.rescan)
        return JSONResponse({"services": await run_in_threadpool(sup.snapshots)})

    async def get_service(request: Request) -> JSONResponse:
        try:
            return JSONResponse(await run_in_threadpool(sup.snapshot, request.path_params["name"]))
        except SupervisorError as exc:
            return _error(exc)

    async def get_icon(request: Request) -> FileResponse | JSONResponse:
        try:
            spec = sup.spec(request.path_params["name"])
        except SupervisorError as exc:
            return _error(exc)
        path, problem = await run_in_threadpool(spec.icon_file)
        if path is None:
            return JSONResponse({"error": problem or "no icon"}, status_code=404)
        return FileResponse(path, media_type=ICON_TYPES[path.suffix.lower()], headers=ICON_HEADERS)

    async def act(request: Request) -> JSONResponse:
        name, action = request.path_params["name"], request.path_params["action"]
        if action not in ACTIONS:
            return JSONResponse({"error": f"unknown action: {action}"}, status_code=404)
        kwargs = {} if action == "start" else {"takeover": request.query_params.get("takeover") in ("1", "true")}
        try:
            message = await run_in_threadpool(getattr(sup, action), name, **kwargs)
            await run_in_threadpool(sup.tick)
            return JSONResponse({"message": message, "service": await run_in_threadpool(sup.snapshot, name)})
        except SupervisorError as exc:
            return _error(exc)

    async def put_config(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            spec = sup.spec(name)
            body = await request.json()
        except SupervisorError as exc:
            return _error(exc)
        except ValueError:
            return JSONResponse({"error": "body must be JSON"}, status_code=400)
        if not isinstance(body, dict) or not body:
            return JSONResponse({"error": "body must be a non-empty object"}, status_code=400)
        if spec.observed and set(body) - OBSERVE_EDITABLE_FIELDS:
            return JSONResponse({"error": f"{name} is observe-only: only 'enabled' can be changed"}, status_code=409)
        for key, value in body.items():
            kind = CONFIG_FIELDS.get(key)
            if kind is None:
                return JSONResponse({"error": f"not editable here: {key}"}, status_code=400)
            if kind is bool and not isinstance(value, bool):
                return JSONResponse({"error": f"{key} must be true or false"}, status_code=400)
            if kind is int and (isinstance(value, bool) or not isinstance(value, (int, str))):
                return JSONResponse({"error": f"{key} must be a number"}, status_code=400)
        try:
            await run_in_threadpool(save_override, sup.cfg, name, dict(body))
            await run_in_threadpool(sup.rescan)
            await run_in_threadpool(sup.tick)
            snap = await run_in_threadpool(sup.snapshot, name)
        except (RegistryError, SupervisorError) as exc:
            return _error(exc)
        return JSONResponse({"service": snap, "restart_needed": bool(snap.get("pending_restart"))})

    async def get_logs(request: Request) -> JSONResponse:
        name = request.path_params["name"]
        try:
            path = sup.log_path(name, request.query_params.get("source", "stdout"))
        except SupervisorError as exc:
            return _error(exc)
        lines = _bounded_int(request.query_params.get("lines"), 400, 1, 5000)
        text, offset = await run_in_threadpool(logs.tail, path, lines)
        return JSONResponse({"path": str(path), "text": text, "offset": offset})

    async def stream_logs(request: Request) -> StreamingResponse | JSONResponse:
        name = request.path_params["name"]
        try:
            path = sup.log_path(name, request.query_params.get("source", "stdout"))
        except SupervisorError as exc:
            return _error(exc)
        lines = _bounded_int(request.query_params.get("lines"), 400, 1, 5000)

        def size() -> int:
            try:
                return path.stat().st_size
            except OSError:
                return 0

        async def events():
            text, offset = await run_in_threadpool(logs.tail, path, lines)
            yield _sse({"reset": True, "text": text, "offset": offset, "path": str(path)})
            quiet = 0.0
            while not await request.is_disconnected():
                if await run_in_threadpool(size) < offset:  # rotated or truncated
                    text, offset = await run_in_threadpool(logs.tail, path, lines)
                    yield _sse({"reset": True, "text": text, "offset": offset, "path": str(path)})
                    quiet = 0.0
                    continue
                text, offset = await run_in_threadpool(logs.read_from, path, offset)
                if text:
                    yield _sse({"text": text, "offset": offset})
                    quiet = 0.0
                else:
                    quiet += 0.5
                    if quiet >= 15:
                        yield ": keep-alive\n\n"
                        quiet = 0.0
                await asyncio.sleep(0.5)

        return StreamingResponse(events(), media_type="text/event-stream",
                                 headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"})

    routes = [
        Route("/", index),
        Route("/favicon.png", favicon),
        Route("/favicon.ico", favicon),
        Route("/api/ping", ping),
        Route("/api/cloud", cloud_info),
        Route("/api/manager/shutdown", shutdown, methods=["POST"]),
        Route("/api/services", list_services),
        Route("/api/rescan", rescan, methods=["POST"]),
        Route("/api/services/{name}", get_service),
        Route("/api/services/{name}/config", put_config, methods=["PUT"]),
        Route("/api/services/{name}/icon", get_icon),
        Route("/api/services/{name}/logs", get_logs),
        Route("/api/services/{name}/logs/stream", stream_logs),
        Route("/api/services/{name}/{action}", act, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=lifespan)
    app.state.sup = sup
    return LocalOnly(app, sup.cfg.host, sup.cfg.port)

"""The shared web design system (``agent_nexus/static/ui``), served under ``/ui/``.

Both the gateway (cloud console, login, inbox) and the local manager console
mount :func:`ui_route`, so every page imports the same ``/ui/tokens.css``,
``/ui/ui.css``, ``/ui/icons.js`` and ``/ui/ui.js`` (docs/ui-spec.md).
Only files directly in that directory with a known type are served.
"""
from __future__ import annotations

import re
from pathlib import Path

from starlette.requests import Request
from starlette.responses import FileResponse, PlainTextResponse, Response
from starlette.routing import Route

UI_DIR = Path(__file__).parent / "static" / "ui"
UI_TYPES = {
    ".css": "text/css; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".txt": "text/plain; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
}
UI_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
UI_HEADERS = {"Cache-Control": "no-cache", "X-Content-Type-Options": "nosniff"}


def ui_file(name: str) -> Path | None:
    """The file for ``/ui/<name>``, or None (no subdirectories, no traversal, known types only)."""
    if not UI_NAME.fullmatch(name) or ".." in name:
        return None
    path = UI_DIR / name
    if path.suffix.lower() not in UI_TYPES or not path.is_file():
        return None
    return path


async def ui_asset(request: Request) -> Response:
    path = ui_file(request.path_params["name"])
    if path is None:
        return PlainTextResponse("Not found", status_code=404)
    return FileResponse(path, media_type=UI_TYPES[path.suffix.lower()], headers=UI_HEADERS)


def ui_route() -> Route:
    return Route("/ui/{name}", ui_asset, methods=["GET", "HEAD"])


def vendor_route(name: str, directory: Path) -> Route:
    """``/vendor/<name>/<path>``: an unmodified third-party web package with its own folders
    (noVNC's ES modules import each other by relative path). JavaScript and text only."""
    directory = Path(directory).resolve()
    types = {".js": UI_TYPES[".js"], ".txt": UI_TYPES[".txt"]}

    async def asset(request: Request) -> Response:
        relative = request.path_params["path"]
        parts = relative.split("/")
        if not relative or any(not UI_NAME.fullmatch(part) or part.startswith(".") for part in parts):
            return PlainTextResponse("Not found", status_code=404)
        path = (directory / relative).resolve()
        if directory not in path.parents or path.suffix.lower() not in types or not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(path, media_type=types[path.suffix.lower()], headers=UI_HEADERS)

    return Route(f"/vendor/{name}/{{path:path}}", asset, methods=["GET", "HEAD"])


def static_dir_route(prefix: str, directory: Path) -> Route:
    """``/<prefix>/<name>`` for the files directly in ``directory`` (same rules as ``/ui/``)."""
    directory = Path(directory)

    async def asset(request: Request) -> Response:
        name = request.path_params["name"]
        path = directory / name
        if not UI_NAME.fullmatch(name) or ".." in name or path.suffix.lower() not in UI_TYPES or not path.is_file():
            return PlainTextResponse("Not found", status_code=404)
        return FileResponse(path, media_type=UI_TYPES[path.suffix.lower()], headers=UI_HEADERS)

    return Route(f"/{prefix}/{{name}}", asset, methods=["GET", "HEAD"])

"""Gateway side of file transfer when no P2P tunnel can be punched (``files/1``, the relay).

``GET  /api/devices/{id}/files?path=``                       list a folder (``fx_list``)
``GET  /api/devices/{id}/files/read?path=&offset=&length=``  one chunk, raw bytes (``fx_read``);
                                                             headers X-ANX-Size, X-ANX-Name, X-ANX-EOF
``POST /api/devices/{id}/files/put/begin``  JSON ``{"dir", "name", "size"}`` -> ``{"id", "chunk"}``
``POST /api/devices/{id}/files/put/write?id=&offset=``  raw body, at most one chunk
``POST /api/devices/{id}/files/put/end``    JSON ``{"id"}`` -> ``{"path", "size"}``
``POST /api/devices/{id}/files/put/abort``  JSON ``{"id"}``

Who may: the same callers as the remote terminal (``terminal_bridge.py``): the owner, or a
joined device of the network with its own credential while online and not locked. The bytes
pass through this process chunk by chunk; nothing is stored here.
"""
from __future__ import annotations

import base64
import logging
from urllib.parse import quote

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .federation import RoutingError
from .inbox_web import NO_STORE
from .terminal_bridge import TerminalBridge

log = logging.getLogger(__name__)
FEATURE = "files/1"
CHUNK = 1024 * 1024


class FilesBridge(TerminalBridge):
    def _peer(self, device_id: str):
        agent_id = self.console.facade.devices.resolve(device_id)
        peer = self.console.hub.peer(agent_id)  # RoutingError when offline
        if FEATURE not in getattr(peer, "features", frozenset()):
            raise RoutingError("这台设备的客户端版本不支持文件传输（升级到 0.8.8 以上）")
        return peer

    async def _allowed(self, request: Request) -> Response | None:
        try:
            device = self._device_caller(request) if request.headers.get("x-anx-device") else None
        except RoutingError as exc:
            return JSONResponse({"error": str(exc).removeprefix("locked: "), "code": "locked"}, status_code=423,
                                headers=NO_STORE)
        if device is None and await self.console.owner(request) is None:
            return self.console.unauthorized()
        return None

    async def _call(self, request: Request, method: str, params: dict) -> dict | Response:
        refused = await self._allowed(request)
        if refused is not None:
            return refused
        try:
            peer = self._peer(request.path_params["device_id"])
            result = await peer.call(method, params)
        except RoutingError as exc:
            return JSONResponse({"error": str(exc), "code": "unavailable"}, status_code=409, headers=NO_STORE)
        if not isinstance(result, dict):
            return JSONResponse({"error": "设备没有回应"}, status_code=502, headers=NO_STORE)
        if "error" in result:
            return JSONResponse({"error": result["error"], "code": "files_failed"}, status_code=422, headers=NO_STORE)
        return result

    @staticmethod
    def _json(result) -> Response:
        return result if isinstance(result, Response) else JSONResponse(result, headers=NO_STORE)

    async def list(self, request: Request) -> Response:
        return self._json(await self._call(request, "fx_list", {"path": request.query_params.get("path", "")}))

    async def read(self, request: Request) -> Response:
        query = request.query_params
        result = await self._call(request, "fx_read", {"path": query.get("path", ""),
                                                       "offset": self._int(query.get("offset"), 0),
                                                       "length": min(self._int(query.get("length"), CHUNK), CHUNK)})
        if isinstance(result, Response):
            return result
        data = base64.b64decode(result.get("data") or "")
        headers = {**NO_STORE, "X-ANX-Size": str(result.get("size", 0)),
                   "X-ANX-Name": quote(str(result.get("name") or "")), "X-ANX-EOF": "1" if result.get("eof") else "0"}
        return Response(data, media_type="application/octet-stream", headers=headers)

    async def _body(self, request: Request) -> dict:
        try:
            body = await request.json()
        except ValueError:
            return {}
        return body if isinstance(body, dict) else {}

    async def put_begin(self, request: Request) -> Response:
        refused = await self._allowed(request)
        if refused is not None:
            return refused
        body = await self._body(request)
        return self._json(await self._call(request, "fx_put_begin", {"dir": body.get("dir", ""), "name": body.get("name", ""),
                                                                    "size": body.get("size")}))

    async def put_write(self, request: Request) -> Response:
        refused = await self._allowed(request)
        if refused is not None:
            return refused
        data = b""
        async for part in request.stream():
            data += part
            if len(data) > CHUNK:
                return JSONResponse({"error": "单块数据过大"}, status_code=413, headers=NO_STORE)
        query = request.query_params
        return self._json(await self._call(request, "fx_put_write", {
            "id": query.get("id", ""), "offset": self._int(query.get("offset"), 0),
            "data": base64.b64encode(data).decode("ascii")}))

    async def put_end(self, request: Request) -> Response:
        body = await self._body(request)
        return self._json(await self._call(request, "fx_put_end", {"id": body.get("id", "")}))

    async def put_abort(self, request: Request) -> Response:
        body = await self._body(request)
        return self._json(await self._call(request, "fx_put_abort", {"id": body.get("id", "")}))

    def routes(self) -> list:
        base = "/api/devices/{device_id}/files"
        return [Route(base, self.list, methods=["GET"]),
                Route(base + "/read", self.read, methods=["GET"]),
                Route(base + "/put/begin", self.put_begin, methods=["POST"]),
                Route(base + "/put/write", self.put_write, methods=["POST"]),
                Route(base + "/put/end", self.put_end, methods=["POST"]),
                Route(base + "/put/abort", self.put_abort, methods=["POST"])]

"""Gateway side of file transfer when no P2P tunnel can be punched (``files/1``, the relay).

``GET  /api/devices/{id}/files?path=``                       list a folder (``fx_list``)
``GET  /api/devices/{id}/files/read?path=&offset=&length=``  one chunk, raw bytes (``fx_read``);
                                                             headers X-ANX-Size, X-ANX-Name, X-ANX-EOF
``POST /api/devices/{id}/files/put/begin``  JSON ``{"dir", "name", "size"}`` -> ``{"id", "chunk"}``
``POST /api/devices/{id}/files/put/write?id=&offset=``  raw body, at most one chunk
``POST /api/devices/{id}/files/put/end``    JSON ``{"id"}`` -> ``{"path", "size"}``
``POST /api/devices/{id}/files/put/abort``  JSON ``{"id"}``

files/2 (copy jobs, docs/design.md §18), only to a device that announces ``files/2``:

``POST /api/devices/{id}/files/walk``    ``fx_walk``  ``{"path", "cursor", "limit"}``
``POST /api/devices/{id}/files/stat``    ``fx_stat_many`` ``{"base", "rels", "keys"}``
``POST /api/devices/{id}/files/mkdirs``  ``fx_mkdirs`` ``{"base", "rels"}``
``POST /api/devices/{id}/files/hash``    ``fx_hash``  ``{"path" | "base"+"rel", "offset", "length"}``

and the optional files/2 fields of ``put/begin`` / ``put/end`` / ``put/abort`` / ``read`` are passed on.
A refusal by the device is HTTP 422 ``{"error", "code": "files_failed", "reason": <the device's code>,
"detail": {...}}``.

Who may: the same callers as the remote terminal (``terminal_bridge.py``): the owner, or a
joined device of the network with its own credential while online and not locked. The bytes
pass through this process chunk by chunk; nothing is stored here.
"""
from __future__ import annotations

import base64
import json
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
FEATURE_V2 = "files/2"
CHUNK = 1024 * 1024
#: longest a ``put/end`` waits on a device re-hashing a resumed file before answering "hashing"
END_WAIT_S = 20.0
#: the optional files/2 fields of each call that are passed on
BEGIN_FIELDS = ("base", "rel", "on_exists", "resume_key", "resume", "mtime_ns", "mode")


V2_METHODS = ("fx_walk", "fx_stat_many", "fx_mkdirs", "fx_hash")


class _NeedsV2(RoutingError):
    pass


class FilesBridge(TerminalBridge):
    def _peer(self, device_id: str, method: str = ""):
        agent_id = self.console.facade.devices.resolve(device_id)
        peer = self.console.hub.peer(agent_id)  # RoutingError when offline
        features = getattr(peer, "features", frozenset())
        if FEATURE not in features:
            raise RoutingError("这台设备的客户端版本不支持文件传输（升级到 0.8.8 以上）")
        if method in V2_METHODS and FEATURE_V2 not in features:
            raise _NeedsV2("这台设备的客户端版本不支持复制任务所需的文件操作（升级到 0.8.31 以上）")
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
            peer = self._peer(request.path_params["device_id"], method)
            result = await peer.call(method, params)
        except _NeedsV2 as exc:
            return JSONResponse({"error": str(exc), "code": "needs_files2"}, status_code=409, headers=NO_STORE)
        except RoutingError as exc:
            return JSONResponse({"error": str(exc), "code": "unavailable"}, status_code=409, headers=NO_STORE)
        if not isinstance(result, dict):
            return JSONResponse({"error": "设备没有回应"}, status_code=502, headers=NO_STORE)
        if "error" in result:
            detail = {k: v for k, v in result.items() if k not in ("error", "code") and k in ("have", "exists")}
            return JSONResponse({"error": result["error"], "code": "files_failed", "reason": str(result.get("code") or ""),
                                 "detail": detail}, status_code=422, headers=NO_STORE)
        return result

    @staticmethod
    def _json(result) -> Response:
        return result if isinstance(result, Response) else JSONResponse(result, headers=NO_STORE)

    async def list(self, request: Request) -> Response:
        return self._json(await self._call(request, "fx_list", {"path": request.query_params.get("path", "")}))

    async def read(self, request: Request) -> Response:
        query = request.query_params
        params = {"path": query.get("path", ""), "offset": self._int(query.get("offset"), 0),
                  "length": min(self._int(query.get("length"), CHUNK), CHUNK)}
        if query.get("base"):
            try:
                rel = json.loads(query.get("rel") or "[]")
            except ValueError:
                rel = None
            if not isinstance(rel, list):
                return JSONResponse({"error": "rel 无效"}, status_code=400, headers=NO_STORE)
            params.update(base=query["base"], rel=rel)
        result = await self._call(request, "fx_read", params)
        if isinstance(result, Response):
            return result
        data = base64.b64decode(result.get("data") or "")
        headers = {**NO_STORE, "X-ANX-Size": str(result.get("size", 0)),
                   "X-ANX-Name": quote(str(result.get("name") or "")), "X-ANX-EOF": "1" if result.get("eof") else "0"}
        if isinstance(result.get("mtime_ns"), int):
            headers["X-ANX-Mtime"] = str(result["mtime_ns"])
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
        params = {"dir": body.get("dir", ""), "name": body.get("name", ""), "size": body.get("size")}
        params.update({key: body[key] for key in BEGIN_FIELDS if key in body})
        return self._json(await self._call(request, "fx_put_begin", params))

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
        params = {"id": body.get("id", "")}
        params.update({key: body[key] for key in ("sha256", "mtime_ns") if key in body})
        if "wait_s" in body:  # files/2: answer "hashing" instead of outliving the call's budget
            try:
                params["wait_s"] = max(0.0, min(float(body["wait_s"]), END_WAIT_S))
            except (TypeError, ValueError):
                params["wait_s"] = END_WAIT_S
        return self._json(await self._call(request, "fx_put_end", params))

    async def put_abort(self, request: Request) -> Response:
        body = await self._body(request)
        return self._json(await self._call(request, "fx_put_abort", {"id": body.get("id", ""),
                                                                    "discard": bool(body.get("discard"))}))

    def _v2(self, method: str, fields: tuple[str, ...]):
        async def handler(request: Request) -> Response:
            body = await self._body(request)
            return self._json(await self._call(request, method, {key: body[key] for key in fields if key in body}))
        return handler

    def routes(self) -> list:
        base = "/api/devices/{device_id}/files"
        return [Route(base, self.list, methods=["GET"]),
                Route(base + "/read", self.read, methods=["GET"]),
                Route(base + "/put/begin", self.put_begin, methods=["POST"]),
                Route(base + "/put/write", self.put_write, methods=["POST"]),
                Route(base + "/put/end", self.put_end, methods=["POST"]),
                Route(base + "/put/abort", self.put_abort, methods=["POST"]),
                Route(base + "/walk", self._v2("fx_walk", ("path", "base", "rel", "cursor", "limit")), methods=["POST"]),
                Route(base + "/stat", self._v2("fx_stat_many", ("base", "rels", "keys")), methods=["POST"]),
                Route(base + "/mkdirs", self._v2("fx_mkdirs", ("base", "rels")), methods=["POST"]),
                Route(base + "/hash", self._v2("fx_hash", ("path", "base", "rel", "offset", "length")), methods=["POST"])]

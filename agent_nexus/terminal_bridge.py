"""Gateway side of the remote terminal: owner WebSocket <-> device ``term_*`` reverse RPC.

``GET  /api/devices/{id}/terminal``     shells and command-line tools on the device (``term_info``)
``WS   /api/devices/{id}/terminal/ws``  one terminal; query ``shell``, ``cols``, ``rows``

On the socket, binary frames are terminal bytes in both directions. Text frames are JSON:
the browser sends ``{"type": "resize", "cols", "rows"}`` (input may also come as
``{"type": "input", "data": "..."}``); the gateway sends ``ready``, ``exit`` and ``error``.

The owner signs in with the console cookie (same-origin only) or a bearer token -- the same
credentials as every other owner API. Output is pulled with a long poll (``term_read``), and
input is written by one task in order, coalescing keystrokes that arrive together.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hmac
import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route, WebSocketRoute
from starlette.websockets import WebSocket, WebSocketDisconnect

from .federation import RoutingError
from .inbox_web import COOKIE, NO_STORE

log = logging.getLogger(__name__)
FEATURE = "term/1"
READ_WAIT_S = 20
MAX_INPUT_FRAME = 64 * 1024
MAX_WRITE_CHUNK = 32 * 1024
CLOSE_UNAUTHORIZED = 4401
CLOSE_UNAVAILABLE = 4404


class TerminalBridge:
    def __init__(self, console):
        self.console = console

    # -- helpers -------------------------------------------------------------------
    def _peer(self, device_id: str):
        devices = self.console.facade.devices
        agent_id = devices.resolve(device_id)
        peer = self.console.hub.peer(agent_id)  # RoutingError when offline
        if FEATURE not in getattr(peer, "features", frozenset()):
            raise RoutingError("这台设备的客户端版本不支持远程终端，或终端已在设备上关闭（升级到 0.3.0 以上）")
        return peer

    async def _ws_owner(self, socket: WebSocket) -> bool:
        inbox = self.console.inbox_web
        header = socket.headers.get("authorization", "")
        if header.startswith("Bearer "):
            return bool(await inbox._verify(header.removeprefix("Bearer ")))
        cookie = socket.cookies.get(COOKIE, "")
        if not cookie or not await inbox._verify(cookie):
            return False
        # A cookie rides along on any page's WebSocket; only the console's own origin may use it.
        origin = socket.headers.get("origin", "")
        expected = self.console.public_url or f"{'https' if socket.url.scheme == 'wss' else 'http'}://{socket.url.netloc}"
        return bool(origin) and hmac.compare_digest(origin.rstrip("/"), expected.rstrip("/"))

    @staticmethod
    def _int(value: Any, default: int) -> int:
        try:
            return int(value)
        except (TypeError, ValueError):
            return default

    # -- GET info --------------------------------------------------------------------
    async def info(self, request: Request) -> Response:
        if await self.console.owner(request) is None:
            return self.console.unauthorized()
        try:
            peer = self._peer(request.path_params["device_id"])
            result = await peer.call("term_info", {"refresh": request.query_params.get("refresh") == "1"})
        except RoutingError as exc:
            return JSONResponse({"error": str(exc), "code": "unavailable"}, status_code=409, headers=NO_STORE)
        if not isinstance(result, dict) or "error" in result:
            return JSONResponse({"error": (result or {}).get("error", "terminal info failed")}, status_code=502, headers=NO_STORE)
        return JSONResponse(result, headers=NO_STORE)

    # -- WebSocket -------------------------------------------------------------------
    async def socket(self, socket: WebSocket) -> None:
        if not await self._ws_owner(socket):
            await socket.close(code=CLOSE_UNAUTHORIZED)
            return
        await socket.accept()
        try:
            peer = self._peer(socket.path_params["device_id"])
        except RoutingError as exc:
            await self._fail(socket, str(exc))
            return
        query = socket.query_params
        try:
            opened = await peer.call("term_open", {"shell": query.get("shell", ""), "cols": self._int(query.get("cols"), 120),
                                                   "rows": self._int(query.get("rows"), 32)})
        except RoutingError as exc:
            await self._fail(socket, str(exc))
            return
        if not isinstance(opened, dict) or "sid" not in opened:
            await self._fail(socket, (opened or {}).get("error", "could not open a terminal"))
            return
        sid = opened["sid"]
        await socket.send_text(json.dumps({"type": "ready", "shell": opened.get("shell"), "cwd": opened.get("cwd")}))
        inputs: asyncio.Queue[bytes] = asyncio.Queue(maxsize=256)
        tasks = [asyncio.create_task(self._pump_output(socket, peer, sid)),
                 asyncio.create_task(self._pump_input(peer, sid, inputs)),
                 asyncio.create_task(self._receive(socket, peer, sid, inputs))]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            for task in tasks:
                task.cancel()
            for task in tasks:
                with contextlib.suppress(BaseException):
                    await task
            with contextlib.suppress(Exception):
                await asyncio.wait_for(peer.call("term_close", {"sid": sid}), 5)
            with contextlib.suppress(Exception):
                await socket.close()

    @staticmethod
    async def _fail(socket: WebSocket, message: str) -> None:
        with contextlib.suppress(Exception):
            await socket.send_text(json.dumps({"type": "error", "error": message}, ensure_ascii=False))
            await socket.close(code=CLOSE_UNAVAILABLE)

    async def _pump_output(self, socket: WebSocket, peer, sid: str) -> None:
        while True:
            try:
                result = await peer.call("term_read", {"sid": sid, "wait": READ_WAIT_S})
            except RoutingError as exc:
                await self._fail(socket, f"设备连接中断：{exc}")
                return
            if not isinstance(result, dict) or "error" in result:
                await socket.send_text(json.dumps({"type": "exit", "code": None,
                                                   "reason": (result or {}).get("error")}, ensure_ascii=False))
                return
            data = base64.b64decode(result.get("data") or "")
            if data:
                await socket.send_bytes(data)
            if result.get("exited"):
                await socket.send_text(json.dumps({"type": "exit", "code": result.get("code")}))
                return

    @staticmethod
    async def _pump_input(peer, sid: str, inputs: asyncio.Queue) -> None:
        while True:
            chunk = await inputs.get()
            # Keystrokes that queued up while the last write was in flight go in one call.
            while len(chunk) < MAX_WRITE_CHUNK and not inputs.empty():
                chunk += inputs.get_nowait()
            result = await peer.call("term_write", {"sid": sid, "data": base64.b64encode(chunk).decode("ascii")})
            if isinstance(result, dict) and (result.get("exited") or "error" in result):
                return

    @staticmethod
    async def _receive(socket: WebSocket, peer, sid: str, inputs: asyncio.Queue) -> None:
        while True:
            try:
                message = await socket.receive()
            except WebSocketDisconnect:
                return
            if message.get("type") == "websocket.disconnect":
                return
            if message.get("bytes") is not None:
                data = message["bytes"]
                if len(data) > MAX_INPUT_FRAME:
                    continue
                await inputs.put(data)
                continue
            try:
                payload = json.loads(message.get("text") or "")
            except ValueError:
                continue
            if not isinstance(payload, dict):
                continue
            if payload.get("type") == "input" and isinstance(payload.get("data"), str):
                data = payload["data"].encode("utf-8")
                if len(data) <= MAX_INPUT_FRAME:
                    await inputs.put(data)
            elif payload.get("type") == "resize":
                cols, rows = TerminalBridge._int(payload.get("cols"), 0), TerminalBridge._int(payload.get("rows"), 0)
                if cols > 0 and rows > 0:
                    with contextlib.suppress(RoutingError):
                        await peer.call("term_resize", {"sid": sid, "cols": cols, "rows": rows})

    def routes(self) -> list:
        return [Route("/api/devices/{device_id}/terminal", self.info, methods=["GET"]),
                WebSocketRoute("/api/devices/{device_id}/terminal/ws", self.socket)]

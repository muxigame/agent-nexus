"""Gateway side of the agent chat (``agents/1``, docs/agent-chat.md §9).

``POST /api/devices/{id}/agents/{op}``  JSON params -> the device's ``agent_*`` reverse RPC, answered as is
``POST /api/agents/handle``             ``{device_id, workdir, kind, name?}`` -> a workspace handle and mailbox
                                        address for a run (the same as ``hub_call agent_handle``)

Who may: the same callers as the remote terminal (``terminal_bridge.py``): the owner (console cookie with
``X-ANX: 1``, or an owner bearer token), or a joined device of the network with its own credential while it is
online and not locked. Only the ops in :data:`agents.hub.OPS` pass. A handler's own ``{"error", "code"}`` comes
back with HTTP 200 (the chat shows it); transport problems use HTTP statuses: 401 not allowed, 404 unknown op,
409 device offline or too old, 423 the calling device is locked, 504 the device did not answer in time.

A device call is never cancelled: Peer.call retires the whole device link when its cancel notice cannot go out
within a second. A call that outlives the caller's patience keeps running in the background and is dropped
when it finishes.
"""
from __future__ import annotations

import asyncio
import json
import logging
from typing import Any

from starlette.requests import Request
from starlette.responses import JSONResponse, Response
from starlette.routing import Route

from .agents.hub import FEATURE, OPS, call_timeout
from .federation import RoutingError
from .inbox_web import NO_STORE
from .terminal_bridge import TerminalBridge

log = logging.getLogger(__name__)
MAX_BODY = 1024 * 1024
#: device calls that outlived their HTTP request (kept referenced until they finish)
_INFLIGHT: set = set()


async def settle(call, timeout: float) -> tuple[bool, Any]:
    """Await ``call`` for at most ``timeout`` without ever cancelling it: (finished, result or exception)."""
    task = asyncio.ensure_future(call)
    _INFLIGHT.add(task)
    task.add_done_callback(_INFLIGHT.discard)
    task.add_done_callback(lambda done: done.cancelled() or done.exception())
    done, _ = await asyncio.wait({task}, timeout=timeout)
    if not done:
        return False, None
    exc = task.exception()
    return True, exc if exc is not None else task.result()


class AgentsBridge(TerminalBridge):
    def _peer(self, device_id: str):
        agent_id = self.console.facade.devices.resolve(device_id)
        peer = self.console.hub.peer(agent_id)  # RoutingError when offline
        if FEATURE not in getattr(peer, "features", frozenset()):
            raise RoutingError("这台设备的客户端版本不支持 Agent 对话，或已在设备上关闭（升级客户端后再试）")
        return peer

    async def _caller(self, request: Request) -> tuple[str | None, Response | None]:
        """(the calling device id or None for the owner, a refusal)."""
        try:
            device = self._device_caller(request) if request.headers.get("x-anx-device") else None
        except RoutingError as exc:
            return None, JSONResponse({"error": str(exc).removeprefix("locked: "), "code": "locked"},
                                      status_code=423, headers=NO_STORE)
        if device is None and await self.console.owner(request) is None:
            return None, self.console.unauthorized()
        return device, None

    @staticmethod
    async def _params(request: Request) -> dict | None:
        body = await request.body()
        if len(body) > MAX_BODY:
            return None
        if not body.strip():
            return {}
        try:
            params = json.loads(body)
        except ValueError:
            return None
        return params if isinstance(params, dict) else None

    async def call(self, request: Request) -> Response:
        _device, refused = await self._caller(request)
        if refused is not None:
            return refused
        op = request.path_params["op"]
        if op not in OPS:
            return JSONResponse({"error": f"不支持的操作：{op}", "code": "unknown_op"}, status_code=404, headers=NO_STORE)
        params = await self._params(request)
        if params is None:
            return JSONResponse({"error": "请求内容必须是 JSON 对象", "code": "bad_request"}, status_code=400,
                                headers=NO_STORE)
        try:
            peer = self._peer(request.path_params["device_id"])
        except RoutingError as exc:
            return JSONResponse({"error": str(exc), "code": "unavailable"}, status_code=409, headers=NO_STORE)
        finished, result = await settle(peer.call(op, params), call_timeout(op, params))
        if not finished:
            return JSONResponse({"error": "设备没有及时回应，请稍后再试", "code": "timeout"}, status_code=504,
                                headers=NO_STORE)
        if isinstance(result, RoutingError):
            return JSONResponse({"error": f"设备连接中断：{result}", "code": "unavailable"}, status_code=409,
                                headers=NO_STORE)
        if isinstance(result, BaseException):
            log.warning("agents: %s on %s failed: %r", op, request.path_params["device_id"], result)
            return JSONResponse({"error": "设备没有回应", "code": "device_error"}, status_code=502, headers=NO_STORE)
        if not isinstance(result, dict):
            return JSONResponse({"error": "设备的回答格式不对", "code": "device_error"}, status_code=502,
                                headers=NO_STORE)
        return JSONResponse(result, headers=NO_STORE)

    async def handle(self, request: Request) -> Response:
        device, refused = await self._caller(request)
        if refused is not None:
            return refused
        params = await self._params(request)
        if params is None:
            return JSONResponse({"error": "请求内容必须是 JSON 对象", "code": "bad_request"}, status_code=400,
                                headers=NO_STORE)
        try:
            result = self.console.facade.agent_handle(params, caller=device)
        except RoutingError as exc:
            message = str(exc)
            if message.startswith("locked: "):
                return JSONResponse({"error": message.removeprefix("locked: "), "code": "locked"}, status_code=423,
                                    headers=NO_STORE)
            return JSONResponse({"error": message, "code": "unavailable"}, status_code=409, headers=NO_STORE)
        return JSONResponse(result, headers=NO_STORE)

    def routes(self) -> list:
        return [Route("/api/devices/{device_id}/agents/{op}", self.call, methods=["POST"]),
                Route("/api/agents/handle", self.handle, methods=["POST"])]

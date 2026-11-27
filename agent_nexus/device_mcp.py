"""``<mcp path>/device``: the gateway's MCP for a joined device, authenticated with the device's own credential.

A device's 本机 MCP (``agent_nexus.local_mcp``, http://127.0.0.1:8399/mcp) forwards the JSON-RPC of the CLIs on that
computer here with ``Authorization: Bearer <device token>`` and ``X-ANX-Device: <agent_id>``. This route runs the very
same FastMCP app as ``/mcp`` (``Gateway.build``), so the tools, the instructions and the mail notice are identical;
only the caller differs: ``client_id = "device:<agent_id>"``, ``subject = "device"``.

The owner's check (``server.auth``) is not widened: the console and the inbox keep accepting only the owner
(``InboxWeb._verify`` also refuses any token whose subject is ``device``). Every request is checked afresh -- the
transport is stateless, one request per call -- so a revoked credential (401), a device that is offline (409) or
locked (423), or the owner's switch ``mcp_devices`` being off (403) stop it at once. A locked device gets no gateway
tools at all, the same as the terminal, files and agents bridges, which refuse before any routing.

No ``WWW-Authenticate`` is ever sent: the caller is the device's own manager, never an OAuth client.
"""
from __future__ import annotations

import json
import logging
from collections.abc import Callable
from typing import Any

from fastmcp.server.auth import AccessToken
from mcp.server.auth.middleware.auth_context import auth_context_var
from mcp.server.auth.middleware.bearer_auth import AuthenticatedUser
from starlette.authentication import AuthCredentials
from starlette.responses import JSONResponse

log = logging.getLogger(__name__)
#: The largest request body peeked at for the log line (the body itself is passed on whole).
PEEK_MAX = 64 * 1024
LOCKED_DETAIL = "这台设备已锁定，解锁后才能操作其他设备"
OFF_DETAIL = "网关主人关闭了设备 MCP 接入"


def _headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def describe(body: bytes) -> str:
    """``method[ tool]`` of a JSON-RPC body (a batch: comma separated); never the arguments."""
    try:
        data = json.loads(body[:PEEK_MAX]) if body else None
    except (ValueError, UnicodeDecodeError):
        return "?"
    items = data if isinstance(data, list) else [data]
    parts = []
    for item in items[:20]:
        if not isinstance(item, dict):
            continue
        method = str(item.get("method") or "response")[:64]
        params = item.get("params") if isinstance(item.get("params"), dict) else {}
        if method == "tools/call" and isinstance(params.get("name"), str):
            method += " " + params["name"][:64]
        parts.append(method)
    return ",".join(parts) or "?"


class DeviceMcpAuth:
    """ASGI endpoint wrapping the inner ``StreamableHTTPASGIApp`` with the device credential check."""

    def __init__(self, inner: Any, *, check: Callable[[str, str], bool], hub: Any, enabled: Callable[[], bool],
                 scopes: list[str] | tuple[str, ...] = ("tools",)):
        self.inner = inner
        self.check = check
        self.hub = hub
        self.enabled = enabled
        self.scopes = list(scopes)

    @staticmethod
    async def _refuse(scope: dict, receive: Any, send: Any, status: int, code: str, detail: str = "") -> None:
        body = {"error": detail or code, "code": code}
        if detail:
            body["detail"] = detail
        await JSONResponse(body, status_code=status, headers={"Cache-Control": "no-store"})(scope, receive, send)

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        if scope.get("type") != "http":
            await self.inner(scope, receive, send)
            return
        headers = _headers(scope)
        agent_id = headers.get("x-anx-device", "").strip()
        auth = headers.get("authorization", "")
        token = auth[7:].strip() if auth[:7].lower() == "bearer " else ""
        if not agent_id or not token:
            await self._refuse(scope, receive, send, 401, "device_credential", "需要设备凭据")
            return
        # the credential first: an unauthenticated caller learns nothing about the owner's settings
        try:
            valid = bool(self.check(agent_id, token))
        except Exception:  # noqa: BLE001
            valid = False
        if not valid:  # unknown or revoked credential
            await self._refuse(scope, receive, send, 401, "device_credential", "网关不接受这台设备的凭据")
            return
        try:
            enabled = bool(self.enabled())
        except Exception:  # noqa: BLE001 - a broken setting reads as the default (on)
            enabled = True
        if not enabled:
            await self._refuse(scope, receive, send, 403, "mcp_devices_off", OFF_DETAIL)
            return
        peer = self.hub.peers.get(agent_id)
        if peer is None:
            await self._refuse(scope, receive, send, 409, "offline", "这台设备没有连接到网关")
            return
        if getattr(peer, "locked", False):
            await self._refuse(scope, receive, send, 423, "locked", LOCKED_DETAIL)
            return

        # Peek at the body for the log line, then hand the same bytes on.
        chunks: list[bytes] = []
        size = 0
        more = True
        messages: list[dict] = []
        while more:
            message = await receive()
            messages.append(message)
            if message.get("type") != "http.request":
                break
            chunk = message.get("body", b"")
            if size < PEEK_MAX:
                chunks.append(chunk[:PEEK_MAX - size])
            size += len(chunk)
            more = bool(message.get("more_body"))
        if scope.get("method") == "POST":
            log.info("mcp/device %s %s", agent_id, describe(b"".join(chunks)))
        pending = list(messages)

        async def replay() -> dict:
            if pending:
                return pending.pop(0)
            return await receive()

        token_obj = AccessToken(token="device", client_id=f"device:{agent_id}", scopes=list(self.scopes),
                                subject="device", expires_at=None)
        user = AuthenticatedUser(token_obj)
        inner_scope = {**scope, "user": user, "auth": AuthCredentials(list(self.scopes))}
        reset = auth_context_var.set(user)
        try:
            await self.inner(inner_scope, replay, send)
        finally:
            auth_context_var.reset(reset)

"""本机 MCP: ``http://127.0.0.1:<console port>/mcp``, the gateway's MCP for this computer's CLIs, no credential.

The console's own server answers ``POST|DELETE /mcp`` (Streamable HTTP, stateless). When this device is joined,
connected and unlocked and its gateway knows ``mcp_device/1``, every request is passed through as it is to
``<gateway>/mcp/device`` with this device's credential added (``Authorization: Bearer <device token>``,
``X-ANX-Device``): the CLIs get exactly the gateway's tools (device_list, use_device, the device tools, mail_*,
ask_user, file_copy…), attributed to this device. The device token never leaves this process and nothing secret is
written anywhere a CLI reads.

Otherwise the endpoint answers by itself (:meth:`LocalMcp.degraded`): ``initialize`` succeeds and its
instructions say why the tools are missing, ``tools/list`` offers only ``nexus_status``, and any other call is an
``isError`` result with the reason. A locked or revoked device therefore lists no gateway tool at all, and the CLI
never sees an HTTP 401 (which would make it start an OAuth flow against 127.0.0.1).

Who may call it (:meth:`LocalMcp.guard`, docs/design.md "本机 MCP"): only loopback peers of a console that itself
listens on loopback only; the Host header must be a loopback name with our port (never ``manager.host``: DNS
rebinding); a browser Origin other than our own (or ``null``) is refused; POST must be ``application/json`` (no
"simple" cross-site request); ``mcp_local.peers = "accounts"`` further limits it to the processes of the core's own
user, the run-as accounts and the user at the console.
"""
from __future__ import annotations

import asyncio
import contextlib
import ipaddress
import json
import logging
import os
import time
from pathlib import Path
from typing import Any, Callable

from . import __version__

log = logging.getLogger(__name__)

PATH = "/mcp"
#: ``anx exec .`` and friends: this computer's own device plugin, without the gateway (see :meth:`LocalMcp.self_app`)
SELF_PATH = "/mcp/self"
BODY_MAX = 8 * 1024 * 1024
AUDIT_MAX = 5 * 1024 * 1024
AUDIT_KEEP = 3
CREDENTIALS_TTL_S = 5.0
DEVICE_OFF_TTL_S = 60.0
STATUS_TOOL = "nexus_status"
#: Request headers passed on to the gateway; everything else (Authorization, Cookie, Origin, Host, X-ANX-Device…)
#: is dropped: a local caller can never choose an identity.
FORWARD_HEADERS = ("content-type", "accept", "mcp-protocol-version", "mcp-session-id", "last-event-id")
RETURN_HEADERS = ("content-type", "mcp-session-id", "cache-control")
PEERS = ("any", "accounts")

REASONS = {
    "off": "本机 MCP 已关闭（本机控制台 → 设置 → 本机 MCP）",
    "exposed": "本机 MCP 只在管理端监听本机地址时提供（manager.host 不是回环地址）",
    "not_joined": "这台设备还没加入网关，Agent Nexus 工具不可用",
    "offline": "网关未连接，稍后再试",
    "revoked": "这台设备已被网关吊销，重新加入后才能使用",
    "rejected": "网关不接受这台设备的凭据（已被吊销或需要重新加入）",
    "locked": "这台设备已锁定，解锁后才能使用 Agent Nexus 网关工具",
    "gateway_old": "网关版本过旧，不支持本机 MCP 接入；请把网关更新到 0.8.31 或更新版本",
    "device_access_off": "网关主人关闭了设备 MCP 接入",
    "no_device": "本机的设备插件没有运行（本机控制台 → 插件 → device）",
    "ready": "已就绪：与网关 MCP 相同的工具，调用方记为本设备",
}
#: States in which the endpoint answers at all (off: 404; exposed: 403 to everybody).
SERVING = tuple(state for state in REASONS if state not in ("off", "exposed", "no_device"))


def is_loopback(host: str | None) -> bool:
    """``127.0.0.0/8``, ``::1``, ``::ffff:127.x`` or ``localhost``."""
    if not host:
        return False
    host = host.strip("[]")
    if host.lower() == "localhost":
        return True
    try:
        address = ipaddress.ip_address(host)
    except ValueError:
        return False
    if isinstance(address, ipaddress.IPv6Address) and address.ipv4_mapped is not None:
        address = address.ipv4_mapped
    return address.is_loopback


def settings_of(data: Any) -> dict:
    """``mcp_local`` of agent-nexus.json, with defaults: ``{enabled, auto_config, peers}``."""
    data = data if isinstance(data, dict) else {}
    peers = data.get("peers")
    return {"enabled": data.get("enabled") is not False, "auto_config": data.get("auto_config") is not False,
            "peers": peers if peers in PEERS else "any"}


def _headers(scope: dict) -> dict[str, str]:
    return {k.decode("latin-1").lower(): v.decode("latin-1") for k, v in scope.get("headers") or []}


def _messages(body: bytes) -> tuple[list[dict], bool] | None:
    """The JSON-RPC messages of a body and whether it was a batch; None when it is not JSON-RPC."""
    try:
        data = json.loads(body) if body else None
    except (ValueError, UnicodeDecodeError):
        return None
    if isinstance(data, dict):
        return [data], False
    if isinstance(data, list) and data and all(isinstance(item, dict) for item in data):
        return data, True
    return None


async def _once(data: bytes):
    yield data


class LocalMcp:
    """The ``/mcp`` endpoint of the local console (an ASGI app) plus its state for the console's 设置 card.

    ``cfg``: the manager config (``host``, ``port``, ``root``, ``mcp_local``); ``cloud_status``: the live link
    dict; ``hub``: the :class:`~agent_nexus.agent.HubLink`; ``lock``: the :class:`~agent_nexus.lock.Lock`."""

    def __init__(self, cfg: Any, cloud_status: dict, hub: Any, lock: Any, *,
                 credentials: Callable[[Path], tuple[str, str, str]] | None = None,
                 http: Callable[[], Any] | None = None, accounts: Callable[[], set[str]] | None = None,
                 device_plugin: Callable[[], tuple[str, str | None] | None] | None = None):
        self.cfg = cfg
        self.cloud_status = cloud_status
        self.hub = hub
        self.lock = lock
        from . import remote
        self._credentials_fn = credentials or remote.credentials
        self._http = http or remote.gateway_http
        self._accounts = accounts or self._allowed_users
        if device_plugin is None:
            from .local_device import DevicePluginCache
            device_plugin = DevicePluginCache(Path(cfg.root)).get
        #: () -> (endpoint, token) of this computer's device plugin, or None (``/mcp/self``)
        self._device_plugin = device_plugin
        self._creds: tuple[float, tuple[str, str, str]] | None = None
        self._device_off_until = 0.0
        self._rejected_until = 0.0
        #: observers: () -> None, called when the state may have changed (the CLI link reconciles)
        self.listeners: list[Callable[[], Any]] = []

    # -- configuration ---------------------------------------------------------------------------------------
    @property
    def settings(self) -> dict:
        return settings_of(getattr(self.cfg, "mcp_local", None))

    @property
    def url(self) -> str:
        # 127.0.0.1, never localhost: on Windows localhost may resolve to ::1 first
        return f"http://127.0.0.1:{self.cfg.port}{PATH}"

    @property
    def hosts(self) -> set[str]:
        port = self.cfg.port
        return {f"127.0.0.1:{port}", f"localhost:{port}", f"[::1]:{port}"}

    @property
    def origins(self) -> set[str]:
        return {f"http://{host}" for host in self.hosts}

    # -- state -----------------------------------------------------------------------------------------------
    def state(self) -> str:
        if not self.settings["enabled"]:
            return "off"
        if not is_loopback(getattr(self.cfg, "host", "127.0.0.1")):
            return "exposed"
        live = self.cloud_status.get("state", "disabled")
        if live == "revoked":
            return "revoked"
        if live in ("disabled", None):
            return "not_joined"
        try:
            locked = bool(self.lock.locked)
        except Exception:  # noqa: BLE001
            locked = False
        if locked:
            return "locked"
        if live != "connected" or not getattr(self.hub, "available", False):
            return "offline"
        from .features import MCP_DEVICE
        if MCP_DEVICE not in (getattr(self.hub, "features", None) or ()):
            return "gateway_old"
        if self.cloud_status.get("gateway_device_mcp") is False or time.monotonic() < self._device_off_until:
            return "device_access_off"
        if time.monotonic() < self._rejected_until:
            return "rejected"
        return "ready"

    def self_state(self) -> str:
        """The state of ``/mcp/self``: the switch, the console's address, the lock, and -- as on ``/mcp`` -- a
        revocation and the gateway owner's device-MCP switch. Not being joined, the gateway being offline or old
        do not matter: this route never uses the network."""
        if not self.settings["enabled"]:
            return "off"
        if not is_loopback(getattr(self.cfg, "host", "127.0.0.1")):
            return "exposed"
        if self.cloud_status.get("state") == "revoked":
            return "revoked"
        try:
            locked = bool(self.lock.locked)
        except Exception:  # noqa: BLE001
            locked = False
        if locked:
            return "locked"
        if self.cloud_status.get("gateway_device_mcp") is False or time.monotonic() < self._device_off_until:
            return "device_access_off"
        if time.monotonic() < self._rejected_until:
            return "rejected"
        return "ready" if self._device_plugin() is not None else "no_device"

    def status(self) -> dict:
        state = self.state()
        return {**self.settings, "url": self.url, "state": state, "reason": REASONS.get(state, state),
                "usable": state not in ("off", "exposed")}

    def _changed(self) -> None:
        for listener in list(self.listeners):
            with contextlib.suppress(Exception):
                listener()

    # -- the guard -------------------------------------------------------------------------------------------
    def _allowed_users(self) -> set[str]:
        from . import accounts
        names: set[str] = set()
        with contextlib.suppress(Exception):
            import psutil
            names.add(psutil.Process().username())
        for purpose in ("terminal", "mcp"):
            with contextlib.suppress(Exception):
                account = accounts.effective(Path(self.cfg.root), purpose)
                if account is not None:
                    names.add(account.name)
        if os.name == "nt":  # pragma: no cover - the user at the console of a Windows service
            with contextlib.suppress(Exception):
                from . import winsession
                user = winsession.active_user() if hasattr(winsession, "active_user") else None
                if user:
                    names.add(str(user))
        return {name.rsplit("\\", 1)[-1].lower() for name in names if name}

    @staticmethod
    def _connections() -> list:
        import psutil
        return psutil.net_connections("tcp")

    def peer(self, scope: dict) -> dict:
        """``{pid, user}`` of the process on the other end of this loopback connection ({} when unknown).

        The connection is matched on the full 4-tuple (client ip and port, server ip and port) and must be
        ESTABLISHED; the table is read fresh for every lookup (a cache would miss new connections and keep closed
        ones). More than one candidate, or none, is "unknown": the caller fails closed."""
        client, server = scope.get("client"), scope.get("server")
        if not client or not server:
            return {}

        def norm(host: Any) -> str:
            text = str(host or "").strip("[]").lower()
            if text.startswith("::ffff:"):
                text = text[len("::ffff:"):]
            return text

        want = (norm(client[0]), int(client[1]), norm(server[0]), int(server[1]))
        pids: set[int] = set()
        try:
            for conn in self._connections():
                if not conn.laddr or not conn.raddr or not conn.pid:
                    continue
                if str(getattr(conn, "status", "")).upper() != "ESTABLISHED":
                    continue
                if (norm(conn.laddr.ip), conn.laddr.port, norm(conn.raddr.ip), conn.raddr.port) == want:
                    pids.add(conn.pid)
        except Exception:  # noqa: BLE001 - psutil missing / access denied: unknown
            return {}
        if len(pids) != 1:
            return {}
        pid = pids.pop()
        user = None
        with contextlib.suppress(Exception):
            import psutil
            user = psutil.Process(pid).username()
        return {"pid": pid, "user": user}

    def peer_allowed(self, scope: dict) -> bool:
        """The caller is one of the accounts ``mcp_local.peers = "accounts"`` lets in."""
        found = self.peer(scope)
        user = str(found.get("user") or "").rsplit("\\", 1)[-1].lower()
        return bool(user) and user in self._accounts()

    def guard(self, scope: dict, headers: dict[str, str]) -> tuple[int, str, dict] | None:
        """``(status, reason, extra headers)`` when the request is refused, else None."""
        settings = self.settings
        if not settings["enabled"]:
            return 404, "not found", {}
        if not is_loopback(getattr(self.cfg, "host", "127.0.0.1")):
            return 403, REASONS["exposed"], {}
        client = scope.get("client") or ("", 0)
        if not is_loopback(client[0]):
            return 403, "forbidden: 本机 MCP 只接受本机的连接", {}
        if headers.get("host") not in self.hosts:
            return 403, "forbidden: unexpected Host header", {}
        origin = headers.get("origin")
        if origin is not None and origin not in self.origins:
            return 403, "forbidden: foreign Origin", {}
        method = scope.get("method")
        if method not in ("POST", "DELETE"):
            return 405, "method not allowed", {"Allow": "POST, DELETE"}
        if method == "POST":
            kind = headers.get("content-type", "").split(";", 1)[0].strip().lower()
            if kind != "application/json":
                return 415, "unsupported media type: application/json only", {}
        if settings["peers"] == "accounts":
            if not self.peer_allowed(scope):
                return 403, "forbidden: 这个账户不能使用本机 MCP（mcp_local.peers = accounts）", {}
        try:
            length = int(headers.get("content-length") or 0)
        except ValueError:
            length = 0
        if length > BODY_MAX:
            return 413, "request too large", {}
        return None

    # -- ASGI ------------------------------------------------------------------------------------------------
    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self._serve(scope, receive, send, local=False)

    @property
    def self_app(self) -> "SelfEndpoint":
        """The ASGI app of ``/mcp/self`` (see :class:`SelfEndpoint`)."""
        return SelfEndpoint(self)

    async def _serve(self, scope: dict, receive: Any, send: Any, *, local: bool) -> None:
        if scope.get("type") != "http":  # pragma: no cover - LocalOnly closes websockets on /mcp
            return
        headers = _headers(scope)
        refused = self.guard(scope, headers)
        if refused is not None:
            status, reason, extra = refused
            await self._plain(send, status, reason, extra)
            return
        body = b""
        while True:
            message = await receive()
            if message.get("type") != "http.request":
                return
            body += message.get("body", b"")
            if len(body) > BODY_MAX:
                await self._plain(send, 413, "request too large")
                return
            if not message.get("more_body"):
                break
        started = time.monotonic()
        parsed = _messages(body) if scope["method"] == "POST" else ([], False)
        if parsed is None:
            await self._json(send, 400, {"jsonrpc": "2.0", "id": None,
                                         "error": {"code": -32700, "message": "Parse error"}})
            return
        messages, batch = parsed
        state = self.self_state() if local else self.state()
        outcome = state
        if state == "ready":
            outcome = await self._forward(scope, headers, body, send, messages, batch, receive, local=local)
        else:
            await self.degraded(send, state, messages, batch, method=scope["method"])
        self._audit(scope, messages, outcome, started, local=local)

    async def _plain(self, send: Any, status: int, text: str, extra: dict | None = None) -> None:
        data = text.encode("utf-8")
        head = [(b"content-type", b"text/plain; charset=utf-8"), (b"content-length", str(len(data)).encode())]
        head += [(k.lower().encode(), v.encode()) for k, v in (extra or {}).items()]
        await send({"type": "http.response.start", "status": status, "headers": head})
        await send({"type": "http.response.body", "body": data})

    async def _json(self, send: Any, status: int, payload: Any) -> None:
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        await send({"type": "http.response.start", "status": status,
                    "headers": [(b"content-type", b"application/json"), (b"content-length", str(len(data)).encode()),
                                (b"cache-control", b"no-store")]})
        await send({"type": "http.response.body", "body": data})

    # -- not ready: answered here ----------------------------------------------------------------------------
    def answer(self, state: str, message: dict) -> dict | None:
        """The local answer to one JSON-RPC message while not ready (None for notifications and responses)."""
        if "method" not in message or "id" not in message or message.get("id") is None:
            return None
        reason = REASONS.get(state, state)
        answer = self._local_answer(state, reason, message)
        # machine-readable for `anx exec` and other scripts: never parse the text
        if isinstance(answer.get("result"), dict):
            answer["result"]["_meta"] = {"anx/state": state, "anx/reason": reason}
        elif isinstance(answer.get("error"), dict):
            answer["error"]["data"] = {"anx/state": state, "anx/reason": reason}
        return answer

    @staticmethod
    def _local_answer(state: str, reason: str, message: dict) -> dict:
        method = message.get("method")
        rid = message.get("id")
        params = message.get("params") if isinstance(message.get("params"), dict) else {}
        if method == "initialize":
            version = params.get("protocolVersion") if isinstance(params.get("protocolVersion"), str) else "2025-06-18"
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "protocolVersion": version, "capabilities": {"tools": {"listChanged": False}},
                "serverInfo": {"name": "Agent Nexus", "version": __version__},
                "instructions": f"Agent Nexus 本机 MCP 当前不可用：{reason}。恢复后重新启动 CLI 即可获得完整工具。"}}
        if method == "ping":
            return {"jsonrpc": "2.0", "id": rid, "result": {}}
        if method == "tools/list":
            return {"jsonrpc": "2.0", "id": rid, "result": {"tools": [{
                "name": STATUS_TOOL, "description": "Agent Nexus 本机 MCP 的状态：为什么现在没有网关工具。",
                "inputSchema": {"type": "object", "properties": {}},
                "annotations": {"readOnlyHint": True, "title": "Agent Nexus 状态"}}]}}
        if method == "tools/call":
            name = params.get("name")
            if name == STATUS_TOOL:
                text = f"state: {state}\n{reason}"
                return {"jsonrpc": "2.0", "id": rid, "result": {"content": [{"type": "text", "text": text}],
                                                               "isError": False}}
            return {"jsonrpc": "2.0", "id": rid, "result": {
                "content": [{"type": "text", "text": f"Agent Nexus 工具暂不可用：{reason}"}], "isError": True}}
        if method in ("resources/list", "prompts/list"):
            key = "resources" if method == "resources/list" else "prompts"
            return {"jsonrpc": "2.0", "id": rid, "result": {key: []}}
        return {"jsonrpc": "2.0", "id": rid, "error": {"code": -32001, "message": reason}}

    async def degraded(self, send: Any, state: str, messages: list[dict], batch: bool, *, method: str = "POST") -> None:
        if method == "DELETE":
            await self._plain(send, 405, "method not allowed", {"Allow": "POST"})
            return
        answers = [a for a in (self.answer(state, m) for m in messages) if a is not None]
        if not answers:
            await send({"type": "http.response.start", "status": 202, "headers": [(b"content-length", b"0")]})
            await send({"type": "http.response.body", "body": b""})
            return
        await self._json(send, 200, answers if batch else answers[0])

    # -- ready: passed through to the gateway ----------------------------------------------------------------
    async def credentials(self) -> tuple[str, str, str]:
        now = time.monotonic()
        if self._creds is not None and now - self._creds[0] < CREDENTIALS_TTL_S:
            return self._creds[1]
        value = await asyncio.to_thread(self._credentials_fn, Path(self.cfg.root))
        self._creds = (now, value)
        return value

    def _upstream_state(self, status: int, body: bytes, session: bool = False) -> str | None:
        """The local state an upstream refusal stands for (None: pass the response on). ``session``: the request
        carried an ``Mcp-Session-Id`` -- a 404 then means the (stateful) gateway forgot that session, and the MCP
        client must see it as it is to start a new one."""
        if status == 401:
            if self.cloud_status.get("state") == "revoked":
                return "revoked"
            self._rejected_until = time.monotonic() + 30.0
            return "rejected"
        if status == 403:
            code = None
            with contextlib.suppress(ValueError, UnicodeDecodeError, AttributeError):
                code = json.loads(body).get("code")
            if code == "mcp_devices_off":
                self._device_off_until = time.monotonic() + DEVICE_OFF_TTL_S
                self._changed()
                return "device_access_off"
            return None
        if status in (404, 405):
            return None if session else "gateway_old"
        if status == 409:
            return "offline"
        if status == 423:
            return "locked"
        return None

    async def _forward(self, scope: dict, headers: dict[str, str], body: bytes, send: Any,
                       messages: list[dict], batch: bool, receive: Any = None, *, local: bool = False) -> str:
        """Pass the request on: to ``<gateway>/mcp/device`` with this device's credential, or (``local``) to this
        computer's device plugin with its bearer token."""
        import httpx
        forward = {key: headers[key] for key in FORWARD_HEADERS if key in headers}
        if local:
            target = self._device_plugin()
            if target is None:
                await self.degraded(send, "no_device", messages, batch, method=scope["method"])
                return "no_device"
            url, plugin_token = target
            if plugin_token:
                forward["authorization"] = f"Bearer {plugin_token}"
            unreachable = "no_device"
        else:
            mcp_path = str(self.cloud_status.get("gateway_mcp_path") or "/mcp")
            try:
                origin, agent_id, token = await self.credentials()
            except Exception as exc:  # noqa: BLE001 - RemoteError: not joined / no credential
                log.info("local mcp: no device credential (%s)", getattr(exc, "code", type(exc).__name__))
                await self.degraded(send, "not_joined", messages, batch, method=scope["method"])
                return "not_joined"
            from . import remote
            forward.update(remote.headers(agent_id, token))
            url = origin.rstrip("/") + mcp_path + "/device"
            unreachable = "offline"
        # the body is streamed on as it comes: never let a proxy in front of the gateway compress it
        forward["accept-encoding"] = "identity"
        client = self._http()
        timeout = httpx.Timeout(connect=10.0, read=None, write=60.0, pool=10.0)
        try:
            request = client.build_request(scope["method"], url, content=body, headers=forward, timeout=timeout)
            upstream = await client.send(request, stream=True)
        except (httpx.HTTPError, OSError) as exc:
            log.info("local mcp: %s unreachable (%s)", "device plugin" if local else "gateway", type(exc).__name__)
            await self.degraded(send, unreachable, messages, batch, method=scope["method"])
            return unreachable
        try:
            if upstream.status_code >= 400:
                data = await upstream.aread()
                mapped = None if local else self._upstream_state(upstream.status_code, data,
                                                                 session="mcp-session-id" in headers)
                if mapped is not None:
                    await self.degraded(send, mapped, messages, batch, method=scope["method"])
                    return mapped
                head = [(k.encode(), upstream.headers[k].encode()) for k in RETURN_HEADERS if k in upstream.headers]
                await send({"type": "http.response.start", "status": upstream.status_code, "headers": head})
                await send({"type": "http.response.body", "body": data})
                return f"http_{upstream.status_code}"
            # never content-encoding / content-length: the body is passed on decoded, in chunks
            head = [(k.encode(), upstream.headers[k].encode()) for k in RETURN_HEADERS if k in upstream.headers]
            await send({"type": "http.response.start", "status": upstream.status_code, "headers": head})
            state = {"outcome": "ok"}

            async def pump() -> None:
                chunks = upstream.aiter_bytes() if not upstream.is_stream_consumed else _once(upstream.content)
                async for chunk in chunks:
                    if b'"isError":true' in chunk or b'"error":{' in chunk:
                        state["outcome"] = "error"
                    await send({"type": "http.response.body", "body": chunk, "more_body": True})
                await send({"type": "http.response.body", "body": b""})

            async def gone() -> None:
                # the CLI cancelled or died: uvicorn's send() would just keep returning, so stop reading the
                # gateway's stream ourselves (closing it ends the call upstream)
                if receive is None:
                    await asyncio.Event().wait()
                while True:
                    message = await receive()
                    if message.get("type") == "http.disconnect":
                        return

            streaming = asyncio.ensure_future(pump())
            watcher = asyncio.ensure_future(gone())
            try:
                await asyncio.wait({streaming, watcher}, return_when=asyncio.FIRST_COMPLETED)
            finally:
                for task in (streaming, watcher):
                    if not task.done():
                        task.cancel()
                for task in (streaming, watcher):
                    with contextlib.suppress(asyncio.CancelledError, Exception):
                        await task
            if streaming.done() and not streaming.cancelled() and streaming.exception() is None:
                return state["outcome"]
            if streaming.cancelled():
                return "client_gone"
            raise streaming.exception()
        except (httpx.HTTPError, OSError) as exc:
            log.info("local mcp: gateway stream ended (%s)", type(exc).__name__)
            return "broken"
        finally:
            with contextlib.suppress(Exception):
                await upstream.aclose()

    # -- audit -----------------------------------------------------------------------------------------------
    def _audit(self, scope: dict, messages: list[dict], outcome: str, started: float, *, local: bool = False) -> None:
        calls = [m for m in messages if m.get("method") == "tools/call" and isinstance(m.get("params"), dict)]
        if not calls:
            return
        peer = self.peer(scope) if self.settings["peers"] == "accounts" else {}
        lines = []
        for call in calls:
            params = call["params"]
            arguments = params.get("arguments") if isinstance(params.get("arguments"), dict) else {}
            entry = {"ts": round(time.time(), 3), "state": self.self_state() if local else self.state(),
                     "tool": str(params.get("name") or "")[:80],
                     "ws": str(arguments.get("workspace_id") or "")[:8], "outcome": outcome,
                     "ms": int((time.monotonic() - started) * 1000)}
            if local:
                entry["via"] = "self"
            if peer.get("pid"):
                entry["peer_pid"] = peer["pid"]
            if peer.get("user"):
                entry["peer_user"] = peer["user"]
            lines.append(json.dumps(entry, ensure_ascii=False))
        try:
            folder = Path(self.cfg.root) / ".anx" / "logs"
            folder.mkdir(parents=True, exist_ok=True)
            path = folder / "local-mcp.jsonl"
            if path.exists() and path.stat().st_size > AUDIT_MAX:
                for n in range(AUDIT_KEEP - 1, 0, -1):
                    older = path.with_name(f"{path.name}.{n}")
                    if older.exists():
                        os.replace(older, path.with_name(f"{path.name}.{n + 1}"))
                os.replace(path, path.with_name(f"{path.name}.1"))
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_APPEND, 0o600)
            with os.fdopen(fd, "a", encoding="utf-8") as handle:
                handle.write("\n".join(lines) + "\n")
        except OSError as exc:
            log.debug("local mcp: audit log not written: %s", exc)


class SelfEndpoint:
    """``/mcp/self``: the same guard as ``/mcp``, then this computer's own device plugin as it is (its own tools,
    raw results), with the plugin's bearer token added by the console. Used by ``anx exec .`` / ``anx devices``
    and works when not joined. It runs commands as the plugin's account -- what ``/mcp`` already lets any loopback
    caller do once joined, so no new privilege; the lock applies as on ``/mcp``."""

    def __init__(self, owner: LocalMcp):
        self.owner = owner

    async def __call__(self, scope: dict, receive: Any, send: Any) -> None:
        await self.owner._serve(scope, receive, send, local=True)

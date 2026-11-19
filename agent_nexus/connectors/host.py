"""``ConnectorHost``: third-party MCP servers run inside the gateway (decision D10, design §7.1, §7.5).

Every enabled registry entry is an upstream MCP session owned by the gateway
process and exposed as ``hub/<name>``. ``Hub.peer("hub")`` returns this host,
so mcplist / mcpdetail / toolsearch / tooluse treat it like a device MCP; it is
never in ``Hub.peers`` (it is not a device and has no workdirs) and it works
with every device offline.

* ``transport: auto`` sends a Streamable HTTP ``initialize``; a 400/404/405
  answer falls back to the legacy SSE transport on the same URL (the MCP
  spec's backwards-compatibility rule). The detected transport is cached.
* Credentials are injected per connector: a bearer token, custom headers, or
  an OAuth access token (:mod:`.oauth`), refreshed without any interaction.
* One shared session per connector (``session: shared``); a broken session is
  dropped and reopened on the next call. Tool execution is never retried.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import logging
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable

import httpx2
from fastmcp import Client
from fastmcp.client.transports import SSETransport, StreamableHttpTransport
from fastmcp.tools.base import ToolResult
from jsonschema import Draft202012Validator

from ..federation import RoutingError
from .icons import RETRY_AFTER_S, IconError, IconFinder, IconStore, check_image
from .oauth import ConnectorAuth, LoginRequired, OAuthManager, expand, resource_metadata_hint
from .registry import (ConnectorError, Registry, SecretStore, export_view, fingerprint, normalize,
                       public_view, valid_name)

log = logging.getLogger(__name__)

HUB_ID = "hub"
PROBE_TIMEOUT_S = 20.0
RECONNECT_AFTER_S = 60.0
RELIST_AFTER_S = 600.0
INIT_BODY = {"jsonrpc": "2.0", "id": 1, "method": "initialize", "params": {
    "protocolVersion": "2025-06-18", "capabilities": {},
    "clientInfo": {"name": "agent-nexus-gateway", "version": "0.2"}}}


@dataclass
class Live:
    """In-memory state of one connector."""
    state: str = "connecting"            # ok | needs_login | error | connecting | disabled
    error: str | None = None
    transport: str | None = None         # detected: http | sse
    tools: list[dict] = field(default_factory=list)
    server: dict | None = None
    client: Client | None = None
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    checked_at: float | None = None
    latency_ms: int | None = None
    listed_at: float | None = None
    www_authenticate: str | None = None


def _readable(exc: BaseException) -> str:
    """A short, secret-free description of a connection/call failure."""
    if isinstance(exc, ConnectorError):
        return exc.message
    if isinstance(exc, (asyncio.TimeoutError, TimeoutError)):
        return "连接超时"
    if isinstance(exc, httpx2.ConnectError):
        return "无法连接到服务器（DNS、网络或端口不可达）"
    if isinstance(exc, httpx2.HTTPStatusError):
        return f"服务器返回 HTTP {exc.response.status_code}"
    if isinstance(exc, httpx2.HTTPError):
        return f"网络错误（{type(exc).__name__}）"
    groups = getattr(exc, "exceptions", None)
    if groups:
        return _readable(groups[0])
    text = str(exc).strip().splitlines()[0][:300] if str(exc).strip() else ""
    return f"{type(exc).__name__}: {text}" if text else type(exc).__name__


def _tool_dict(tool) -> dict:
    data = tool.model_dump(by_alias=True, exclude_none=True, mode="json")
    data.setdefault("inputSchema", {"type": "object", "properties": {}})
    return data


async def _first_sse_event(response, wanted: str | None, limit: int = 65536) -> tuple[str | None, str | None]:
    """Read an ``text/event-stream`` until the first (wanted) event: ``(event, data)``."""
    event, data, seen = None, [], 0
    async for line in response.aiter_lines():
        seen += len(line)
        if seen > limit:
            break
        if line.startswith("event:"):
            event = line[6:].strip()
        elif line.startswith("data:"):
            data.append(line[5:].lstrip())
        elif line == "" and data:
            if wanted is None or event == wanted:
                return event, "\n".join(data)
            event, data = None, []
    return (event, "\n".join(data)) if data else (None, None)


async def probe_url(url: str, headers: dict[str, str] | None = None, *, timeout: float = PROBE_TIMEOUT_S) -> dict:
    """Which transport an MCP URL speaks and whether it wants a login (no MCP session is kept).

    ``{"status", "transport": "http"|"sse"|None, "needs_auth", "www_authenticate", "server", "error"}``
    """
    headers = dict(headers or {})
    result: dict[str, Any] = {"status": None, "transport": None, "needs_auth": False,
                              "www_authenticate": None, "server": None, "error": None}
    async with httpx2.AsyncClient(timeout=timeout, follow_redirects=True) as http:
        try:
            async with http.stream("POST", url, json=INIT_BODY, headers={
                    **headers, "Accept": "application/json, text/event-stream",
                    "MCP-Protocol-Version": "2025-06-18"}) as response:
                result["status"] = response.status_code
                if response.status_code in (401, 403):
                    result.update(needs_auth=True, www_authenticate=response.headers.get("www-authenticate"))
                    return result
                if 200 <= response.status_code < 300:
                    result["transport"] = "http"
                    kind = response.headers.get("content-type", "")
                    body = None
                    if "text/event-stream" in kind:
                        _, body = await _first_sse_event(response, None)
                    else:
                        body = (await response.aread())[:65536].decode("utf-8", "replace")
                    with contextlib.suppress(ValueError, TypeError, AttributeError):
                        info = json.loads(body or "").get("result", {}).get("serverInfo")
                        if isinstance(info, dict):
                            result["server"] = {"name": info.get("name"), "version": info.get("version")}
                    session = response.headers.get("mcp-session-id")
                    if session:
                        with contextlib.suppress(httpx2.HTTPError):
                            await http.delete(url, headers={**headers, "Mcp-Session-Id": session})
                    return result
                if response.status_code not in (400, 404, 405, 406):
                    result["error"] = f"服务器返回 HTTP {response.status_code}"
                    return result
        except httpx2.HTTPError as exc:
            result["error"] = _readable(exc)
            return result
        # Legacy HTTP+SSE: GET the URL, the first event names the POST endpoint.
        try:
            async with http.stream("GET", url, headers={**headers, "Accept": "text/event-stream"}) as response:
                if response.status_code in (401, 403):
                    result.update(status=response.status_code, needs_auth=True,
                                  www_authenticate=response.headers.get("www-authenticate"))
                    return result
                if response.status_code == 200 and "text/event-stream" in response.headers.get("content-type", ""):
                    event, _ = await asyncio.wait_for(_first_sse_event(response, "endpoint"), timeout)
                    if event == "endpoint":
                        result.update(status=200, transport="sse")
                        return result
                result["error"] = (f"这个地址既不是 Streamable HTTP 也不是 SSE 的 MCP 端点"
                                   f"（POST 返回 HTTP {result['status']}，GET 返回 HTTP {response.status_code}）")
        except (httpx2.HTTPError, asyncio.TimeoutError) as exc:
            result["error"] = _readable(exc)
    return result


class ConnectorHost:
    """Peer-like owner of all gateway-hosted connectors (``agent_id == "hub"``)."""

    agent_id = HUB_ID
    features = frozenset()
    anx_version = None
    enrolled_via = None

    def __init__(self, store, *, state_dir: Path | None, public_url: str | None = None,
                 clock: Callable[[], float] = time.time):
        self.registry = Registry(store)
        self.secrets = SecretStore(Path(state_dir) / "connectors" if state_dir else None)
        self.icons = IconStore(Path(state_dir) / "connector-icons" if state_dir else None)
        self.icon_finder = IconFinder()
        self._icon_busy: set[str] = set()
        self.public_url = (public_url or "").rstrip("/") or None
        self.origin_fallback: str | None = None
        self.clock = clock
        self.oauth = OAuthManager(self.secrets, redirect_base=self.redirect_base, clock=clock)
        self.oauth.on_login_lost = self._login_lost
        self.live: dict[str, Live] = {}
        self._tasks: set[asyncio.Task] = set()
        #: ``notify_owner(subject, body)``: a system mail to ``me`` (set by the cloud app).
        self.notify_owner: Callable[[str, str], None] | None = None
        self.started = False

    # -- peer surface (federation.Hub) -------------------------------------------
    def redirect_base(self) -> str:
        return self.public_url or self.origin_fallback or "http://127.0.0.1"

    def _entries(self) -> list[dict]:
        try:
            return self.registry.list()
        except Exception as exc:  # a broken database must not break mcplist for devices
            log.warning("connector registry unavailable (%s)", type(exc).__name__)
            return []

    def _live(self, name: str, entry: dict | None = None) -> Live:
        live = self.live.get(name)
        if live is None:
            live = self.live[name] = Live()
            cache = (entry or {}).get("_cache") or {}
            live.tools = list(cache.get("tools") or [])
            live.transport = cache.get("transport")
            live.server = cache.get("server")
            if entry and (entry.get("auth") or {}).get("type") == "oauth" and not self.oauth.has_tokens(name):
                live.state = "needs_login"
                live.error = f"需要登录：请在控制台的连接器页登录 {name}"
        return live

    def auth_state(self, entry: dict) -> str:
        if not entry.get("enabled", True):
            return "disabled"
        return self._live(entry["name"], entry).state

    @property
    def catalog(self) -> dict:
        mcps = {}
        for entry in self._entries():
            if not entry.get("enabled", True):
                continue
            name = entry["name"]
            live = self._live(name, entry)
            tools = [] if live.state == "needs_login" else self._filtered(entry, live.tools)
            title = entry.get("title") or name
            mcps[name] = {"description": f"{title} (third-party MCP connector hosted by the gateway)", "tools": tools,
                          "error": live.error if live.state in ("needs_login", "error") else None,
                          "kind": "connector", "run_on": "gateway", "auth_state": live.state,
                          "connector_id": name, "fingerprint": fingerprint(entry.get("url")),
                          "title": title}
        return {"workspaces": {}, "mcps": mcps}

    @staticmethod
    def _filtered(entry: dict, tools: list[dict]) -> list[dict]:
        rules = entry.get("tools") or {}
        allow, deny = set(rules.get("allow") or []), set(rules.get("deny") or [])
        return [tool for tool in tools if (not allow or tool["name"] in allow) and tool["name"] not in deny]

    async def call(self, method: str, params: dict) -> Any:
        """The Peer interface; only ``tool_call`` exists for the hub."""
        if method != "tool_call":
            raise RoutingError(f"the hub does not answer {method}")
        result = await self.call_tool(params["mcp"], params["tool"], params.get("arguments") or {})
        return result

    # -- connections ---------------------------------------------------------------
    def _secrets(self, name: str) -> dict:
        return self.secrets.read(name, "secrets")

    def _client_secret(self, name: str) -> str | None:
        return self._secrets(name).get("client_secret") or None

    async def _auth_headers(self, entry: dict) -> tuple[dict[str, str], Any]:
        """``(headers, httpx Auth or None)`` for one connector."""
        auth = entry.get("auth") or {"type": "none"}
        kind = auth.get("type", "none")
        stored = self._secrets(entry["name"])
        if kind == "bearer":
            return {"Authorization": f"Bearer {expand(stored.get('token') or '')}"}, None
        if kind == "headers":
            return {key: expand(str(value)) for key, value in (stored.get("headers") or {}).items()}, None
        if kind == "oauth":
            return {}, ConnectorAuth(self.oauth, entry["name"], lambda: self._client_secret(entry["name"]))
        return {}, None

    async def _probe_with_auth(self, entry: dict) -> dict:
        headers, auth = await self._auth_headers(entry)
        if auth is not None:
            token = await self.oauth.access_token(entry["name"], client_secret=self._client_secret(entry["name"]))
            headers = {**headers, "Authorization": f"Bearer {token}"}
        found = await probe_url(entry["url"], headers, timeout=min(PROBE_TIMEOUT_S, entry.get("timeout_s", 120)))
        if found["needs_auth"] and auth is not None:
            # The access token was refused: refresh once, then it is a login problem.
            token = await self.oauth.access_token(entry["name"], force_refresh=True,
                                                  client_secret=self._client_secret(entry["name"]))
            found = await probe_url(entry["url"], {**headers, "Authorization": f"Bearer {token}"})
            if found["needs_auth"]:
                raise LoginRequired(entry["name"], f"连接器 {entry['name']} 的令牌被拒绝：请在控制台重新登录")
        return found

    async def _open(self, entry: dict, live: Live) -> Client:
        name = entry["name"]
        found = await self._probe_with_auth(entry)
        if found["needs_auth"]:
            live.www_authenticate = found.get("www_authenticate")
            if (entry.get("auth") or {}).get("type") in (None, "none"):
                hint = "（服务支持 OAuth：编辑连接器，把鉴权改为 OAuth）" if resource_metadata_hint(found.get("www_authenticate")) else ""
                raise ConnectorError("unauthorized", f"服务器要求认证（HTTP {found['status']}）{hint}")
            raise ConnectorError("unauthorized", f"认证被拒绝（HTTP {found['status']}）：检查 Token 或 Header")
        if found["error"]:
            raise ConnectorError("unreachable", found["error"])
        transport = entry.get("transport", "auto")
        if transport == "auto":
            transport = found["transport"] or "http"
        live.transport = transport
        live.server = found.get("server") or live.server
        headers, auth = await self._auth_headers(entry)
        target = (SSETransport(entry["url"], headers=headers, auth=auth) if transport == "sse"
                  else StreamableHttpTransport(entry["url"], headers=headers, auth=auth))
        timeout = float(entry.get("timeout_s", 120))
        client = Client(target, timeout=timeout, init_timeout=min(timeout, 60.0), name=f"anx-hub-{name}")
        await asyncio.wait_for(client.__aenter__(), min(timeout, 60.0))
        return client

    async def ensure(self, entry: dict, *, relist: bool = False) -> Client:
        """The shared client of a connector, opened (and its tools listed) when needed."""
        name = entry["name"]
        live = self._live(name, entry)
        async with live.lock:
            if live.client is not None and not relist:
                return live.client
            if live.client is not None and relist:
                with contextlib.suppress(Exception):
                    await self._list(entry, live, live.client)
                    return live.client
                self._retire(live)
            if (entry.get("auth") or {}).get("type") == "oauth" and not self.oauth.has_tokens(name):
                live.state, live.error = "needs_login", f"需要登录：请在控制台的连接器页登录 {name}"
                raise LoginRequired(name)
            started = time.monotonic()
            try:
                client = await self._open(entry, live)
                await self._list(entry, live, client)
            except LoginRequired as exc:
                live.state, live.error = "needs_login", exc.message
                raise
            except Exception as exc:
                live.state, live.error = "error", _readable(exc)
                live.checked_at = self.clock()
                raise ConnectorError("connect_failed", f"连接器 {name} 连接失败：{live.error}", 502) from None
            live.client = client
            live.state, live.error = "ok", None
            live.latency_ms = int((time.monotonic() - started) * 1000)
            live.checked_at = self.clock()
            self._icon_soon(entry)
            return client

    async def _list(self, entry: dict, live: Live, client: Client) -> None:
        tools = await asyncio.wait_for(client.list_tools(), float(entry.get("timeout_s", 120)))
        live.tools = [_tool_dict(tool) for tool in tools]
        live.listed_at = self.clock()
        with contextlib.suppress(Exception):
            self.registry.save_cache(entry["name"], {"tools": live.tools, "transport": live.transport,
                                                     "server": live.server, "listed_at": live.listed_at})

    def _retire(self, live: Live) -> None:
        client, live.client = live.client, None
        if client is None:
            return

        async def close():
            with contextlib.suppress(BaseException):
                await asyncio.wait_for(client.__aexit__(None, None, None), 5)
        self._spawn(close())

    def _spawn(self, coroutine) -> asyncio.Task:
        task = asyncio.get_running_loop().create_task(coroutine)
        self._tasks.add(task)
        task.add_done_callback(self._tasks.discard)
        task.add_done_callback(lambda done: done.cancelled() or done.exception())
        return task

    def _entry(self, name: str) -> dict:
        entry = self.registry.get(name) if isinstance(name, str) else None
        if entry is None:
            raise ConnectorError("not_found", f"没有名为 {name} 的连接器", 404)
        return entry

    async def call_tool(self, name: str, tool: str, arguments: dict) -> ToolResult:
        """tooluse("hub/<name>", tool, arguments): readable errors, never an interactive login."""
        try:
            entry = self._entry(name)
        except ConnectorError:
            raise RoutingError(f"unknown MCP hub/{name}") from None
        if not entry.get("enabled", True):
            raise RoutingError(f"连接器 {name} 已停用：在控制台的连接器页启用它")
        try:
            client = await self.ensure(entry)
        except ConnectorError as exc:
            raise RoutingError(exc.message) from None
        live = self._live(name, entry)
        if (entry.get("auth") or {}).get("type") == "oauth":
            # Refresh (or fail with a readable "needs login") before the transport sees the token.
            try:
                await self.oauth.access_token(name, client_secret=self._client_secret(name))
            except ConnectorError as exc:
                if isinstance(exc, LoginRequired):
                    live.state, live.error = "needs_login", exc.message
                    self._retire(live)
                raise RoutingError(exc.message) from None
        descriptor = next((item for item in self._filtered(entry, live.tools) if item["name"] == tool), None)
        if descriptor is None:
            raise RoutingError(f"unknown tool hub/{name}/{tool}")
        problems = list(Draft202012Validator(descriptor.get("inputSchema") or {}).iter_errors(arguments))
        if problems:
            raise RoutingError("invalid tool arguments: " + "; ".join(error.message for error in problems[:3]))
        timeout = float(entry.get("timeout_s", 120))
        self.oauth.refused.pop(name, None)
        try:
            result = await asyncio.wait_for(client.call_tool_mcp(tool, arguments), timeout)
        except asyncio.TimeoutError:
            raise RoutingError(f"连接器 {name} 的工具 {tool} 超时（{int(timeout)} 秒）；执行结果未知") from None
        except LoginRequired as exc:
            live.state, live.error = "needs_login", exc.message
            self._retire(live)
            raise RoutingError(exc.message) from None
        except Exception as exc:
            refused = self._refused_login(entry)
            if refused is not None:
                # The upstream answered 401 and the refresh failed inside the transport, which the MCP
                # client only reports as "Connection closed": the call was refused, not executed.
                live.state, live.error = "needs_login", refused.message
                self._retire(live)
                raise RoutingError(f"{refused.message}（上游拒绝了这次调用，工具未执行）") from None
            # The session may be broken: drop it so the next call reconnects. Never retried here.
            live.state, live.error = "error", _readable(exc)
            self._retire(live)
            raise RoutingError(f"连接器 {name} 调用失败（执行结果未知）：{live.error}") from None
        return ToolResult.from_mcp_result(result)

    def _refused_login(self, entry: dict) -> LoginRequired | None:
        """The login failure behind a failed call, if the transport's auth flow hit one."""
        if (entry.get("auth") or {}).get("type") != "oauth":
            return None
        name = entry["name"]
        refused = self.oauth.refused.pop(name, None)
        if refused is None and not self.oauth.has_tokens(name):
            refused = LoginRequired(name, f"连接器 {name} 的授权已失效：请在控制台重新登录")
        return refused

    # -- icons (connectors/icons.py) ---------------------------------------------------
    def icon_view(self, name: str) -> dict:
        meta = self.icons.meta(name)
        if not meta.get("type"):
            return {"icon_url": None, "icon_source": None}
        return {"icon_url": f"/api/connectors/{name}/icon?v={str(meta.get('sha256', ''))[:12]}",
                "icon_source": meta.get("source")}

    def _icon_soon(self, entry: dict, *, force: bool = False) -> None:
        """Look for an icon in the background, unless one is uploaded or a search failed recently."""
        name = entry["name"]
        meta = self.icons.meta(name)
        if name in self._icon_busy or meta.get("source") == "upload":
            return
        if not force and (meta.get("type") or (meta.get("tried_at") or 0) > time.time() - RETRY_AFTER_S):
            return
        self._icon_busy.add(name)

        async def go():
            try:
                await self.find_icon(entry)
            finally:
                self._icon_busy.discard(name)
        with contextlib.suppress(RuntimeError):
            self._spawn(go())
            return
        self._icon_busy.discard(name)

    async def find_icon(self, entry: dict) -> dict:
        name = entry["name"]
        live = self.live.get(name)
        try:
            found = await self.icon_finder.find(entry["url"], live.server if live else None)
        except Exception as exc:  # never let an icon break a connector
            log.info("connector %s: icon search failed (%s)", name, type(exc).__name__)
            found = None
        if self.icons.meta(name).get("source") == "upload" or self.registry.get(name) is None:
            return self.icon_view(name)  # uploaded or deleted meanwhile
        if found:
            self.icons.save(name, found["data"], {"type": found["type"], "source": found["source"], "url": found["url"]})
        else:
            self.icons.save(name, None, {"tried_at": time.time()})
        return self.icon_view(name)

    def icon(self, name: str) -> tuple[bytes, dict] | None:
        self._entry(name)
        return self.icons.read(name)

    def upload_icon(self, name: str, data: bytes) -> dict:
        entry = self._entry(name)
        try:
            data, kind = check_image(data)
        except IconError as exc:
            raise ConnectorError(exc.code, exc.message, exc.status) from None
        self.icons.save(name, data, {"type": kind, "source": "upload"})
        return self.view(entry)

    async def reset_icon(self, name: str) -> dict:
        """Forget the uploaded (or found) icon and search again."""
        entry = self._entry(name)
        self.icons.remove(name)
        await self.find_icon(entry)
        return self.view(entry)

    # -- registry operations (console API and hub_call connector_registry) -----------
    def view(self, entry: dict) -> dict:
        live = self._live(entry["name"], entry)
        data = public_view(entry)
        state = "disabled" if not entry.get("enabled", True) else live.state
        data.update(state=state, error=live.error if state in ("error", "needs_login") else None,
                    transport_detected=live.transport, tools_count=len(self._filtered(entry, live.tools)) if live.tools else (0 if live.listed_at else None),
                    server=live.server, latency_ms=live.latency_ms, checked_at=live.checked_at,
                    created_at=entry.get("created_at"), updated_at=entry.get("updated_at"),
                    **self.icon_view(entry["name"]))
        if (entry.get("auth") or {}).get("type") == "oauth":
            summary = self.oauth.summary(entry["name"])
            data["oauth"] = {**summary, "redirect_uri": self.oauth.redirect_uri(entry["name"])}
        return data

    def list_views(self) -> list[dict]:
        return [self.view(entry) for entry in self._entries()]

    def summary(self) -> dict:
        views = self.list_views()
        return {"total": len(views), "needs_login": sum(1 for v in views if v["state"] == "needs_login"),
                "error": sum(1 for v in views if v["state"] == "error"), "available": True,
                "tools": sum(v["tools_count"] or 0 for v in views if v["state"] == "ok"),
                "items": [{"name": v["name"], "title": v["title"], "state": v["state"]} for v in views]}

    def _connect_soon(self, name: str) -> None:
        if not self.started:
            return

        async def go():
            with contextlib.suppress(Exception):
                await self.ensure(self._entry(name))
        self._spawn(go())

    async def create(self, body: dict) -> dict:
        config, secrets = normalize(body)
        self.registry.insert(config)
        self.secrets.remove(config["name"])
        self.icons.remove(config["name"])
        self.secrets.apply(config["name"], secrets)
        self.oauth.forget(config["name"])
        self.live.pop(config["name"], None)
        entry = self._entry(config["name"])
        if config.get("enabled", True):
            self._connect_soon(config["name"])
        return self.view(entry)

    async def update(self, name: str, body: dict) -> dict:
        old = self._entry(name)
        config, secrets = normalize(body, old)
        if (old.get("auth") or {}).get("type") != config["auth"].get("type"):
            self.secrets.write(name, "secrets", {})
        if (old.get("auth") or {}) != config["auth"] or old.get("url") != config["url"]:
            if config["auth"].get("type") != "oauth" or old.get("url") != config["url"]:
                self.secrets.remove(name, "oauth")
        self.secrets.apply(name, secrets)
        if old.get("url") != config["url"] and self.icons.meta(name).get("source") != "upload":
            self.icons.remove(name)  # a found icon belongs to the old address
        self.registry.update(config)
        live = self.live.pop(name, None)
        if live is not None:
            self._retire(live)
        entry = self._entry(name)
        if config.get("enabled", True):
            self._connect_soon(name)
        return self.view(entry)

    async def delete(self, name: str) -> None:
        self._entry(name)
        live = self.live.pop(name, None)
        if live is not None:
            self._retire(live)
        with contextlib.suppress(Exception):
            await self.oauth.logout(name, client_secret=self._client_secret(name))
        self.registry.delete(name)
        self.secrets.remove(name)
        self.icons.remove(name)
        self.oauth.forget(name)

    async def probe(self, body: dict) -> dict:
        """``POST /api/connectors/probe``: transport, login need and tool count of a pasted URL."""
        if not isinstance(body, dict):
            raise ConnectorError("invalid_input", "请求体必须是 JSON 对象")
        if body.get("command"):
            return {"transport": "stdio", "needs_oauth": False, "auth_hint": "none", "server": None,
                    "tools_count": None, "run_on": "device",
                    "error": "stdio 命令只能在设备上运行；固定到设备的连接器即将推出"}
        from .registry import valid_url
        url = valid_url(body.get("url"))
        headers = {}
        auth = body.get("auth") if isinstance(body.get("auth"), dict) else {}
        if auth.get("type") == "bearer" and auth.get("token"):
            headers["Authorization"] = f"Bearer {auth['token']}"
        elif auth.get("type") == "headers" and isinstance(auth.get("headers"), dict):
            headers.update({str(k): str(v) for k, v in auth["headers"].items() if v})
        found = await probe_url(url, headers)
        result = {"transport": found["transport"], "needs_oauth": False, "auth_hint": "none",
                  "server": found["server"], "tools_count": None, "error": found["error"], "status": found["status"]}
        if found["needs_auth"]:
            oauth_capable = bool(resource_metadata_hint(found.get("www_authenticate")))
            if not oauth_capable:
                with contextlib.suppress(Exception):
                    discovered = await self.oauth.discover(url, www_authenticate=found.get("www_authenticate"))
                    oauth_capable = bool(discovered.get("prm")) or not discovered["as_metadata"].get("_assumed")
            result.update(needs_oauth=oauth_capable, auth_hint="oauth" if oauth_capable else "bearer", error=None,
                          transport=found["transport"] or (body.get("transport") if body.get("transport") in ("http", "sse") else None))
            if headers and not oauth_capable:
                result["error"] = f"服务器拒绝了提供的凭据（HTTP {found['status']}）"
            return result
        if found["transport"] and not found["error"]:
            transport = found["transport"]
            target = (SSETransport(url, headers=headers) if transport == "sse" else StreamableHttpTransport(url, headers=headers))
            try:
                async with Client(target, timeout=PROBE_TIMEOUT_S, init_timeout=PROBE_TIMEOUT_S) as client:
                    tools = await asyncio.wait_for(client.list_tools(), PROBE_TIMEOUT_S)
                    result["tools_count"] = len(tools)
            except Exception as exc:
                result["error"] = f"握手成功但列出工具失败：{_readable(exc)}"
        return result

    async def test(self, name: str) -> dict:
        entry = self._entry(name)
        live = self._live(name, entry)
        started = time.monotonic()
        error = None
        try:
            async with live.lock:
                self._retire(live)
            await self.ensure(entry)
        except ConnectorError as exc:
            error = exc.message
        latency = int((time.monotonic() - started) * 1000)
        tools = self._filtered(entry, live.tools) if error is None else []
        return {"ok": error is None, "state": live.state, "tools_count": len(tools) if error is None else None,
                "latency_ms": latency, "error": error, "transport": live.transport, "server": live.server,
                "tools": [{"name": tool["name"], "title": tool.get("title"),
                           "description": (tool.get("description") or "")[:300]} for tool in tools],
                "connector": self.view(entry)}

    async def login(self, name: str, *, origin: str | None = None, owner: str | None = None) -> dict:
        entry = self._entry(name)
        if (entry.get("auth") or {}).get("type") != "oauth":
            raise ConnectorError("not_oauth", f"连接器 {name} 不使用 OAuth；编辑连接器把鉴权改为 OAuth", 400)
        if origin and not self.public_url:
            self.origin_fallback = origin.rstrip("/")
        live = self._live(name, entry)
        url = await self.oauth.begin(entry, client_secret=self._client_secret(name), owner=owner,
                                     www_authenticate=live.www_authenticate)
        return {"authorize_url": url, "redirect_uri": self.oauth.redirect_uri(name)}

    async def finish_login(self, name: str, *, code: str, state: str) -> dict:
        self._entry(name)
        await self.oauth.complete(name, code=code, state=state, client_secret=self._client_secret(name))
        entry = self._entry(name)
        live = self._live(name, entry)
        async with live.lock:
            self._retire(live)
            live.state, live.error = "connecting", None
        with contextlib.suppress(ConnectorError):
            await self.ensure(entry)
        return self.view(entry)

    async def logout(self, name: str) -> dict:
        entry = self._entry(name)
        await self.oauth.logout(name, client_secret=self._client_secret(name))
        live = self._live(name, entry)
        self._retire(live)
        if (entry.get("auth") or {}).get("type") == "oauth":
            live.state, live.error = "needs_login", f"已退出登录：请在控制台的连接器页登录 {name}"
        return self.view(entry)

    def export(self) -> dict:
        return {"connectors": {entry["name"]: export_view(entry) for entry in self._entries()}}

    async def import_(self, body: dict) -> dict:
        """``PUT /api/connectors/import``: design §7.2's structure; existing names are updated."""
        items = body.get("connectors") if isinstance(body, dict) else None
        if not isinstance(items, dict):
            raise ConnectorError("invalid_input", "需要 {\"connectors\": {名称: 定义}}")
        created, updated, errors = [], [], {}
        for name, definition in items.items():
            if not isinstance(definition, dict):
                errors[name] = "定义必须是对象"
                continue
            definition = {key: value for key, value in definition.items() if key != "session"}
            auth = definition.get("auth")
            if isinstance(auth, dict) and auth.get("type") == "headers":
                auth = {**auth, "headers": {k: (v or None) for k, v in (auth.get("headers") or {}).items()}}
                definition["auth"] = auth
            try:
                if self.registry.get(name):
                    await self.update(name, definition)
                    updated.append(name)
                else:
                    await self.create({**definition, "name": name})
                    created.append(name)
            except ConnectorError as exc:
                errors[name] = exc.message
        return {"created": created, "updated": updated, "errors": errors}

    async def registry_op(self, args: dict, *, origin: str | None = None) -> Any:
        """``hub_call connector_registry {op, …}`` from the local console (docs/ui-spec.md §3.4)."""
        op = args.get("op")
        try:
            if op == "list":
                return {"connectors": self.list_views()}
            if op == "create":
                return {"connector": await self.create(args.get("connector") or args)}
            if op == "update":
                return {"connector": await self.update(valid_name(args.get("name")), args.get("connector") or {})}
            if op == "delete":
                await self.delete(valid_name(args.get("name")))
                return {"deleted": args.get("name")}
            if op == "probe":
                return await self.probe(args)
            if op == "test":
                return await self.test(valid_name(args.get("name")))
            if op == "login":
                return await self.login(valid_name(args.get("name")), origin=origin)
            if op == "logout":
                return {"connector": await self.logout(valid_name(args.get("name")))}
        except ConnectorError as exc:
            raise RoutingError(f"{exc.code}: {exc.message}") from None
        raise RoutingError(f"unsupported connector_registry op {op!r}")

    # -- life cycle ----------------------------------------------------------------
    def _login_lost(self, name: str) -> None:
        live = self.live.get(name)
        if live is not None:
            live.state, live.error = "needs_login", f"连接器 {name} 的授权已失效：请在控制台重新登录"
            if live.client is not None:
                with contextlib.suppress(RuntimeError):
                    self._retire(live)
        if self.notify_owner is not None:
            link = f"{self.public_url or self.origin_fallback or ''}/#/connectors"
            with contextlib.suppress(Exception):
                self.notify_owner(f"{name} 需要重新登录",
                                  f"连接器 {name}（hub/{name}）的授权已失效，Agent 暂时无法调用它的工具。\n"
                                  f"请打开 {link} ，在“连接器”页点“登录”。")

    async def start(self) -> None:
        self.started = True
        for entry in self._entries():
            if entry.get("enabled", True):
                self._live(entry["name"], entry)
                self._connect_soon(entry["name"])

    async def run(self, interval: float = 30.0) -> None:
        """Background upkeep: proactive token refresh, reconnects after errors, periodic tool relists."""
        await self.start()
        while True:
            await asyncio.sleep(interval)
            await self.tick()

    async def tick(self) -> None:
        now = self.clock()
        for entry in self._entries():
            if not entry.get("enabled", True):
                continue
            name = entry["name"]
            live = self._live(name, entry)
            if (entry.get("auth") or {}).get("type") == "oauth" and self.oauth.has_tokens(name):
                with contextlib.suppress(Exception):
                    await self.oauth.refresh_if_due(name, client_secret=self._client_secret(name))
            try:
                if live.state == "error" and (live.checked_at is None or now - live.checked_at >= RECONNECT_AFTER_S):
                    await self.ensure(entry)
                elif live.state == "ok" and live.client is not None and (live.listed_at is None or now - live.listed_at >= RELIST_AFTER_S):
                    await self.ensure(entry, relist=True)
            except ConnectorError:
                pass

    async def close(self) -> None:
        self.started = False
        for live in self.live.values():
            self._retire(live)
        tasks = list(self._tasks)
        if tasks:
            await asyncio.wait(tasks, timeout=6)

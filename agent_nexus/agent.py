"""Local MCP aggregation and outbound cloud connector."""
from __future__ import annotations

import argparse
import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import os
import platform
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit
from uuid import uuid4

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcpkit import Service
from mcpkit.config import ConfigError, ServiceConfig, read_secret
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
import httpx2

from . import __version__
from . import env as anx_env
from . import device_contract
from . import features as anx_features
from . import paths, sections
from .cloud import env_secret
from .federation import MAX_MESSAGE_BYTES, MCP_ALIASES, RoutingError, valid_id, valid_media_token
from .registry import discover, load_manager_config

log = logging.getLogger(__name__)
MAX_MEDIA_BYTES = 20 * 1024 * 1024
#: Bounds on the device plugin's ``device_info`` (design §5.2): one quick call
#: per catalog refresh, and a descriptor small enough for every hello.
DEVICE_INFO_TIMEOUT_S = 10.0
MAX_DEVICE_INFO_BYTES = 64 * 1024
# A hung local service must not keep the whole computer offline: reconnects
# wait this long for a fresh catalog, then reuse the last one they had.
CONNECT_CATALOG_TIMEOUT_S = 20.0
#: The gateway closes a revoked device credential with this code (design §11.5):
#: the agent stops reconnecting until the device joins again.
CLOSE_REVOKED = 4401
_agent_io_tasks: set[asyncio.Task] = set()


def _close_code(exc: BaseException) -> int | None:
    received = getattr(exc, "rcvd", None) if isinstance(exc, ConnectionClosed) else None
    return getattr(received, "code", None)


def _task_result(task: asyncio.Task) -> None:
    """Observe detached owners, including when their original waiter was cancelled."""
    with contextlib.suppress(asyncio.CancelledError, Exception):
        task.result()


def _agent_io_task(coroutine) -> asyncio.Task:
    # Keep cancelled I/O alive until the transport finishes its cleanup, even
    # after the original caller's deadline. Never wait unboundedly for it.
    task = asyncio.create_task(coroutine)
    _agent_io_tasks.add(task)
    task.add_done_callback(_agent_io_tasks.discard)
    task.add_done_callback(_task_result)
    return task


async def _send_agent_message(socket, lock: asyncio.Lock, message: dict, timeout: float) -> None:
    sending = False

    async def transmit():
        nonlocal sending
        async with lock:
            sending = True
            await socket.send(json.dumps(message, ensure_ascii=False))

    operation = _agent_io_task(transmit())
    try:
        # Include waiting for another sender in the deadline.
        done, _ = await asyncio.wait((operation,), timeout=timeout)
        if not done:
            raise asyncio.TimeoutError
        operation.result()
    except BaseException as exc:
        if not operation.done():
            operation.cancel()
        if isinstance(exc, asyncio.CancelledError) and not sending:
            # Cancellation while merely queued has not damaged the socket.
            raise
        # A failed send must wake the receive loop, not leave this connection
        # apparently healthy while callers keep queuing behind it. Socket
        # close is also an independent owner: its cancellation tail must not
        # turn the original deadline into an unbounded wait.
        closing = _agent_io_task(socket.close(code=1011))
        try:
            await asyncio.wait((closing,), timeout=min(timeout, 1.0))
        finally:
            if not closing.done():
                closing.cancel()
        raise


def _describe_failure(exc: BaseException) -> str:
    """Name the failure, with close codes for WebSocket closes (1006 = no close frame)."""
    if isinstance(exc, ConnectionClosed):
        received, sent = exc.rcvd, exc.sent
        parts = [type(exc).__name__]
        if received is not None:
            parts.append(f"received {received.code} {received.reason}".rstrip())
        if sent is not None:
            parts.append(f"sent {sent.code} {sent.reason}".rstrip())
        if received is None and sent is None:
            parts.append("no close frame")
        return ", ".join(parts)
    return type(exc).__name__


class HubLink:
    """Device -> gateway requests (``hub_call/1``, design §3.2) over the current connection.

    Only usable after the gateway announced ``hub_call/1`` in ``ready``: a
    legacy gateway silently drops unknown messages (§3.1), so nothing is ever
    sent to one. The local console holds one of these across reconnects.
    """

    def __init__(self):
        self.features: frozenset[str] = frozenset()
        self.display_name: str | None = None
        self.gateway_version: str | None = None
        self._send = None
        self._pending: dict[str, asyncio.Future] = {}
        #: Reverse RPCs the hosting process answers beyond the link's own (method -> async handler),
        #: and the optional features they stand for: the manager adds plugin_repo_install /
        #: plugin_repo_job and ``plugin_repo/1`` (design §6.12). A bare ``anx-agent`` has none.
        self.handlers: dict[str, Any] = {}
        self.extra_features: tuple[str, ...] = ()

    @property
    def available(self) -> bool:
        return self._send is not None and anx_features.HUB_CALL in self.features

    def attach(self, send, anx: Any) -> None:
        self.features = anx_features.parse_features(anx)
        self.gateway_version = anx_features.peer_version(anx)
        name = anx.get("display_name") if isinstance(anx, dict) else None
        self.display_name = name if isinstance(name, str) and len(name) <= 255 else None
        self._send = send

    def detach(self) -> None:
        self._send = None
        self.features = frozenset()
        for future in self._pending.values():
            if not future.done():
                future.set_exception(RoutingError("gateway connection closed"))
        self._pending.clear()

    def resolve(self, message: dict) -> bool:
        """Consume a reply to one of our requests; ``False`` for anything else."""
        if "method" in message:
            return False
        future = self._pending.get(message.get("id"))
        if future is None:
            return False
        if not future.done():
            if "error" in message:
                future.set_exception(RoutingError(str(message["error"])))
            else:
                future.set_result(message.get("result"))
        return True

    async def call(self, op: str, args: dict | None = None, *, timeout: float = 30.0) -> Any:
        if not self.available:
            raise RoutingError("the gateway is not connected or does not support hub_call")
        request_id = f"d-{uuid4().hex}"
        future = asyncio.get_running_loop().create_future()
        self._pending[request_id] = future
        try:
            await self._send({"id": request_id, "method": "hub_call", "params": {"op": op, "args": args or {}}})
            return await asyncio.wait_for(future, timeout)
        finally:
            self._pending.pop(request_id, None)


def _platform_name() -> str:
    return {"win32": "windows", "darwin": "darwin"}.get(sys.platform, "linux" if sys.platform.startswith("linux") else sys.platform)


class _SessionOwner:
    """One capacity reservation, from initial connect through final cleanup."""
    def __init__(self):
        self.entered = False


def connection_settings(cloud: dict, root: Path | None = None) -> tuple[str, str, str]:
    agent_id = valid_id(cloud.get("agent_id"))
    url = str(cloud.get("url", "")).rstrip("/")
    parsed = urlsplit(url)
    if parsed.scheme not in ("ws", "wss") or not parsed.hostname or parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise ConfigError("cloud.url must be a ws/wss origin (optionally with an ingress base path)")
    if parsed.scheme != "wss" and parsed.hostname not in ("127.0.0.1", "localhost", "::1"):
        raise ConfigError("non-loopback cloud ingress requires wss")
    if cloud.get("token_secret"):
        if root is None:
            raise ConfigError("token_secret requires a manager configuration root")
        token = read_secret(ServiceConfig(name="manager", root=root, dir=root), cloud["token_secret"])
    else:
        token = env_secret(anx_env.resolve_token_env(cloud.get("token_env"), "AGENT_TOKEN"))
    return agent_id, url, token


class LocalGateway:
    def __init__(self, root: Path, cloud: dict, *, transports: dict | None = None, toaster=None):
        self.root = root.resolve()
        self.cloud = cloud
        self.transports = transports
        #: Shows the gateway's ``toast`` reverse RPCs (design §8.12); see agent_nexus/toast.py.
        self.toaster = toaster
        self.catalog: dict = {"workspaces": {}, "mcps": {}}
        self.targets: dict[str, Any] = {}
        self.target_signatures: dict[str, str] = {}
        self.descriptions: dict[str, str] = {}
        self.media_endpoints: dict[str, str] = {}
        self.sessions: dict[tuple[str, str], tuple[Client, float]] = {}
        self.lock = asyncio.Lock()
        self.active: dict[tuple[str, str], int] = {}
        self.pending: dict[tuple[str, str], asyncio.Task] = {}
        self.opening: set[asyncio.Task] = set()
        self.closing: set[asyncio.Task] = set()
        self.owners: set[_SessionOwner] = set()
        self.client_owners: dict[int, _SessionOwner] = {}
        self.closed = False
        self.generation = 0
        self.rpc_timeout = float(cloud.get("rpc_timeout_s", 180))
        if self.rpc_timeout <= 0:
            raise ConfigError("rpc_timeout_s must be positive")
        self.close_timeout = min(self.rpc_timeout, 5.0)
        #: Answers ``session_wake`` / ``session_probe`` (design §8.7); set by run_agent.
        self.waker = None
        #: Reverse RPCs served by the hosting process (``HubLink.handlers``); set by run_agent.
        self.handlers: dict[str, Any] = {}

    def wake_config(self) -> dict:
        """``cloud.wake``, else the top-level ``wake`` of agent-nexus.json (design §8.7)."""
        if isinstance(self.cloud.get("wake"), dict):
            return self.cloud["wake"]
        with contextlib.suppress(Exception):
            if paths.has_config(self.root):
                section = paths.read_config(self.root).get("wake")
                if isinstance(section, dict):
                    return section
        return {}

    async def companion_event(self, params: dict) -> dict:
        """The companion adapter: the device plugin's ``POST /anx/companion-event`` (it posts to chatx)."""
        endpoint = self.wake_config().get("companion_endpoint") or self.media_endpoints.get(self.resolve_mcp("device"))
        if not endpoint:
            raise RoutingError("the device plugin is not running here")
        payload = {"project_id": params.get("companion_route"), "workdir": params.get("workdir"),
                   "event_type": "mail_received", "summary": str(params.get("prompt") or ""),
                   "job_id": params.get("job_id")}
        async with httpx2.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as http:
            response = await http.post(f"{str(endpoint).rstrip('/')}/anx/companion-event", json=payload,
                                       headers={"X-ANX": "1"})
        try:
            body = response.json()
        except ValueError:
            body = {}
        if response.status_code != 200 or not isinstance(body, dict):
            return {"delivered": False, "error": str(body.get("error") if isinstance(body, dict) else "")
                    or f"HTTP {response.status_code}"}
        return body

    def _toaster(self):
        if self.toaster is None:
            from .toast import DeviceNotifyToaster
            cfg = load_manager_config(self.root)
            console = str(self.cloud.get("console_url") or cfg.base_url)
            self.toaster = DeviceNotifyToaster(self, console, cfg.state_dir)
        return self.toaster

    def resolve_mcp(self, name: str) -> str:
        """A local MCP name, accepting the former name of a renamed one.

        ``workspace`` (the device plugin's name before agent-nexus) and
        ``device`` resolve to whichever of the two this machine actually runs.
        """
        if name in self.targets:
            return name
        alias = MCP_ALIASES.get(name)
        return alias if alias is not None and alias in self.targets else name

    def device_descriptor(self, info: dict | None = None) -> dict | None:
        """The catalog's ``device`` block (design §5.2 a).

        ``info`` is the device plugin's ``device_info`` result: its
        capabilities, platform facts and agent runtimes. Without it (plugin
        down, or a plugin older than the contract) the block is synthesised
        from ``sys.platform``, declares only ``info`` and says ``degraded``.
        The manager then adds what only it knows: the agent id, its own
        version and the local MCP name the plugin runs under.

        The hostname is the device's display name until the owner sets an
        alias (decision D6). ``cloud.hostname`` overrides it; an empty value
        keeps it unreported (and, without ``info``, the block too).
        """
        if info is not None:
            block = {key: value for key, value in info.items() if key != "degraded"}
        else:
            block = {"contract": device_contract.CONTRACT, "platform": _platform_name(), "degraded": True,
                     "capabilities": {"info": {"v": 1}}}
        hostname = self.cloud["hostname"] if "hostname" in self.cloud else (block.get("hostname") or platform.node())
        if isinstance(hostname, str) and hostname.strip():
            block["hostname"] = hostname.strip()[:255]
        else:
            block.pop("hostname", None)
            if info is None:
                return None
        block.update(agent_version=__version__, mcp=self.resolve_mcp("device"))
        # `anx wake doctor` results (design §8.7): shown in mail_who's agent_runtimes.
        from .link.wake.doctor import load_results
        doctor = load_results(self.root)
        if doctor:
            runtimes = dict(block.get("agent_runtimes") or {}) if isinstance(block.get("agent_runtimes"), dict) else {}
            for kind, item in doctor.items():
                if not isinstance(item, dict):
                    continue
                key = "companion" if kind == "gpt" else kind
                entry = dict(runtimes.get(key) or {}) if isinstance(runtimes.get(key), dict) else {}
                entry["wake_doctor"] = {name: item.get(name) for name in ("ok", "at", "version", "permission", "error")
                                        if item.get(name) is not None}
                runtimes[key] = entry
            block["agent_runtimes"] = runtimes
        if isinstance(self.cloud.get("agent_id"), str):
            block["id"] = self.cloud["agent_id"]
        return block

    async def device_info(self, name: str) -> dict | None:
        """The device plugin's ``device_info``, validated against the contract; ``None`` when unusable."""
        client = None
        try:
            client = await self.client(name, "catalog")
            result = await asyncio.wait_for(client.call_tool_mcp("device_info", {}), DEVICE_INFO_TIMEOUT_S)
            info = None
            if not result.is_error:
                info = result.structured_content
                if info is None:
                    text = next((block.text for block in result.content if getattr(block, "text", None)), None)
                    info = json.loads(text) if text else None
            if not isinstance(info, dict):
                raise ValueError("device_info returned no descriptor")
            if len(json.dumps(info, ensure_ascii=False)) > MAX_DEVICE_INFO_BYTES:
                raise ValueError("device_info descriptor is too large")
            problems = device_contract.validate(info)
            if problems:
                raise ValueError("device_info does not match the contract: " + problems[0])
            return info
        except Exception as exc:
            log.warning("device_info unavailable (%s); publishing a degraded device block", type(exc).__name__)
            if client is not None and not isinstance(exc, ValueError):
                # A broken session (not a bad answer): let the next scan reconnect.
                async with self.lock:
                    cached = self.sessions.get((name, "catalog"))
                    if cached and cached[0] is client:
                        self.sessions.pop((name, "catalog"), None)
                    else:
                        cached = None
                if cached:
                    self._retire(cached[0])
            return None

    def configured_workspaces(self) -> dict:
        entries = self.cloud.get("workspaces")
        if entries is None:
            services = sections.plugin_entries(paths.read_config(self.root))
            settings = {**services.get("workspace", {}).get("settings", {}),
                        **services.get("device", {}).get("settings", {})}
            path = settings.get("default_workdir") or str(Path.home())
            entries = {"default": {"path": path, "mcp": "device"}}
        if not isinstance(entries, dict):
            raise ConfigError("cloud.workspaces must be an object")
        result = {}
        for key, value in entries.items():
            valid_id(key)
            value = {"path": value} if isinstance(value, str) else dict(value)
            path = (self.root / value["path"]).resolve()
            if not path.is_dir():
                raise ConfigError(f"workspace directory does not exist: {key}")
            result[key] = {"path": str(path), "mcp": self.resolve_mcp(valid_id(value.get("mcp", "device")))}
        return result

    def configured_targets(self) -> dict:
        self.descriptions = {}
        self.media_endpoints = {}
        if self.transports is not None:  # injectable in-memory transports for integration tests
            return dict(self.transports)
        cfg = load_manager_config(self.root)
        targets = {}
        for name, spec in discover(cfg).items():
            if (spec.observed or not spec.enabled or not spec.supported or spec.error or spec.port is None
                    or not spec.installed or spec.missing):
                continue
            service = Service.load(spec.dir, root=self.root)
            token = None
            if service.config.auth.bearer_secrets:
                token = read_secret(service.config, service.config.auth.bearer_secrets[0])
            elif service.config.auth.mode != "none":
                # OAuth interactive login is not suitable for an unattended agent.
                continue
            host = "127.0.0.1" if spec.host in ("0.0.0.0", "::") else spec.host
            if ":" in host:
                host = f"[{host}]"
            endpoint = f"http://{host}:{spec.port}{service.config.path}"
            targets[name] = lambda endpoint=endpoint, token=token: StreamableHttpTransport(endpoint, auth=token)
            self.media_endpoints[name] = f"http://{host}:{spec.port}"
            self.descriptions[name] = spec.description

        # Extra MCPs needn't be managed processes. HTTP, legacy SSE and stdio
        # are configured locally; credentials never go into the cloud catalog.
        # ``connectors`` (agent-nexus.json) are offered next to the legacy
        # ``cloud.mcps`` while the agent still speaks the old entry shape.
        mcps = {**self.cloud.get("mcps", {}),
                **sections.extra_mcps({"connectors": paths.read_config(self.root).get("connectors") or {}})}
        for name, entry in mcps.items():
            valid_id(name)
            if entry.get("enabled", True) is False:
                targets.pop(name, None)
                continue
            self.descriptions[name] = str(entry.get("description", ""))
            if "url" in entry:
                transport = entry.get("transport", "http")
                token = None
                if entry.get("token_env"):
                    token = os.environ.get(entry["token_env"])
                    if not token:
                        raise ConfigError(f"missing token environment variable for {name}")
                if transport == "sse":
                    from fastmcp.client.transports import SSETransport
                    targets[name] = lambda url=entry["url"], token=token: SSETransport(url, auth=token)
                elif transport == "http":
                    targets[name] = lambda url=entry["url"], token=token: StreamableHttpTransport(url, auth=token)
                    parsed = urlsplit(entry["url"])
                    self.media_endpoints[name] = f"{parsed.scheme}://{parsed.netloc}"
                else:
                    raise ConfigError(f"unsupported transport for {name}")
            else:
                command = entry.get("command")
                if not isinstance(command, list) or not command:
                    raise ConfigError(f"extra MCP {name} needs url or command array")
                options = {"command": command[0], "args": command[1:],
                           "cwd": str((self.root / entry.get("cwd", ".")).resolve()),
                           "env": {**os.environ, **entry.get("env", {})}}
                targets[name] = lambda options=options: StdioTransport(**options)
        return targets

    async def client(self, name: str, scope: str) -> Client:
        key = (name, scope)
        async with self.lock:
            if self.closed:
                raise RoutingError("local MCP gateway is closed")
            if key in self.sessions:
                client, _ = self.sessions[key]
                self.sessions[key] = (client, time.monotonic())
                return client
            if name not in self.targets:
                raise RoutingError(f"unknown local MCP {name}")
            task = self.pending.get(key)
            if task is None:
                if len(self.owners) >= int(self.cloud.get("max_sessions", 256)):
                    raise RoutingError("local MCP session limit reached")
                owner = _SessionOwner()
                self.owners.add(owner)
                task = asyncio.create_task(self._open_client(key, self.targets[name], self.target_signatures.get(name), owner))
                self.pending[key] = task
                self.opening.add(task)

                def initialized(task, owner=owner):
                    self.opening.discard(task)
                    # Failed/cancelled __aenter__ is cleaned up by FastMCP.
                    # A successful enter retains this same reservation until
                    # its matching exit finishes, without counting it twice.
                    if not owner.entered:
                        self.owners.discard(owner)
                    _task_result(task)

                task.add_done_callback(initialized)
        # One owner establishes each scoped connection. Cancelling one waiter
        # must not cancel initialization still needed by another caller.
        return await asyncio.shield(task)

    async def _open_client(self, key: tuple[str, str], target, signature: str | None,
                           owner: _SessionOwner) -> Client:
        entered = False
        client = None
        try:
            client = Client(target() if callable(target) else target,
                            timeout=self.rpc_timeout, init_timeout=self.rpc_timeout)
            await asyncio.wait_for(client.__aenter__(), self.rpc_timeout)
            entered = True
            owner.entered = True
            self.client_owners[id(client)] = owner
            async with self.lock:
                if self.closed or self.target_signatures.get(key[0]) != signature or self.pending.get(key) is not asyncio.current_task():
                    raise RoutingError("local MCP configuration changed during connection")
                self.sessions[key] = (client, time.monotonic())
            return client
        except BaseException as exc:
            # FastMCP 4 cleans up cancelled/failed __aenter__ internally. Once
            # entered successfully, this pool owns exactly one matching exit.
            if entered:
                self._retire(client)
            if isinstance(exc, asyncio.CancelledError):
                # Only the pool cancels this shared owner. Tell its waiters
                # that the connection was revoked; do not make execute() treat
                # this as a downstream caller cancellation and omit a reply.
                raise RoutingError("local MCP connection closed during initialization") from None
            raise
        finally:
            async with self.lock:
                if self.pending.get(key) is asyncio.current_task():
                    self.pending.pop(key, None)

    def _retire(self, client: Client) -> asyncio.Task:
        owner = self.client_owners.pop(id(client), None)

        async def finish():
            try:
                # FastMCP's disconnect cancellation also bounds runner cleanup.
                await asyncio.wait_for(client.__aexit__(None, None, None), self.close_timeout)
            except Exception as exc:
                log.warning("local MCP session cleanup failed (%s)", type(exc).__name__)
            finally:
                # Even a detached/retired connection still consumes capacity
                # until the SDK finishes closing its underlying resources.
                if owner is not None:
                    self.owners.discard(owner)

        task = asyncio.create_task(finish())
        self.closing.add(task)
        task.add_done_callback(self.closing.discard)
        task.add_done_callback(_task_result)
        return task

    async def refresh(self) -> dict:
        targets = self.configured_targets()
        # Factories are rebuilt on every scan; fingerprint their options rather
        # than their identities. Hashes stay local and never enter the catalog.
        signatures = {name: hashlib.sha256(repr(getattr(target, "__defaults__", target)).encode()).hexdigest()
                      for name, target in targets.items()}
        async with self.lock:
            if self.closed:
                raise RoutingError("local MCP gateway is closed")
            retired = []
            for key, (client, _) in list(self.sessions.items()):
                if self.target_signatures.get(key[0]) != signatures.get(key[0]):
                    self.sessions.pop(key, None)
                    retired.append(client)
            for key, task in list(self.pending.items()):
                if self.target_signatures.get(key[0]) != signatures.get(key[0]):
                    self.pending.pop(key, None)
                    task.cancel()
            self.targets = targets
            self.target_signatures = signatures
            self.generation += 1
            generation = self.generation
        # Detached connections can never be borrowed again. Their shutdown
        # must not hold up unrelated services or scopes.
        for client in retired:
            self._retire(client)
        workspaces = self.configured_workspaces()
        missing = {entry["mcp"] for entry in workspaces.values()} - self.targets.keys()
        if missing:
            raise ConfigError(f"workspaces reference unavailable/unconfigured MCPs: {', '.join(sorted(missing))}")

        async def descriptor(name):
            client = None
            try:
                client = await self.client(name, "catalog")
                tools = await client.list_tools()
                return name, {"description": self.descriptions.get(name, ""),
                              "tools": [tool.model_dump(mode="json", by_alias=True, exclude_none=True) for tool in tools]}
            except Exception as exc:
                # Exception strings may include transport URLs or credentials.
                # Do not publish them into the remote registry.
                # Drop failed catalog connections so the next scan can recover
                # a service restarted behind an unchanged endpoint.
                async with self.lock:
                    cached = self.sessions.get((name, "catalog"))
                    if cached and cached[0] is client:
                        self.sessions.pop((name, "catalog"), None)
                    else:
                        cached = None
                if cached:
                    self._retire(cached[0])
                log.warning("local MCP catalog unavailable (%s)", type(exc).__name__)
                return name, {"description": self.descriptions.get(name, ""), "tools": [],
                              "error": "local MCP unavailable or authentication failed"}

        results = dict(await asyncio.gather(*(descriptor(name) for name in self.targets)))
        device_mcp = self.resolve_mcp("device")
        info = None
        if any(tool.get("name") == "device_info" for tool in results.get(device_mcp, {}).get("tools", [])):
            info = await self.device_info(device_mcp)
        async with self.lock:
            if self.closed or self.generation != generation:
                raise RoutingError("local MCP configuration changed during catalog refresh")
            catalog = {"workspaces": workspaces, "mcps": results}
            # Legacy gateways validate only workspaces/mcps and ignore this key.
            device = self.device_descriptor(info)
            if device is not None:
                catalog["device"] = device
            self.catalog = catalog
            return self.catalog

    async def dispatch(self, method: str, params: dict) -> Any:
        # One budget covers acquisition, discovery and tool execution. The
        # Client's per-request timeout alone does not cover the whole route.
        return await asyncio.wait_for(self._dispatch(method, params), self.rpc_timeout)

    async def _dispatch(self, method: str, params: dict) -> Any:
        if method == "media_get":
            name = self.resolve_mcp(valid_id(params.get("mcp", "device")))
            token = valid_media_token(params.get("token"))
            endpoint = self.media_endpoints.get(name)
            if endpoint is None:
                return {"status": 404}
            try:
                async with httpx2.AsyncClient(trust_env=False, timeout=15, follow_redirects=False) as http:
                    response = await http.get(f"{endpoint}/media/{token}")
            except Exception:
                return {"status": 502}
            if response.status_code != 200:
                return {"status": response.status_code if response.status_code in (404, 410) else 502}
            if len(response.content) > MAX_MEDIA_BYTES:
                return {"status": 413}
            return {"status": 200, "content_type": response.headers.get("content-type", "application/octet-stream"),
                    "data": base64.b64encode(response.content).decode("ascii")}
        if method in ("toast", "toast_dismiss"):
            # Announced as toast_actions/1 (design §8.12).
            toaster = self._toaster()
            if method == "toast":
                if not isinstance(params, dict) or not isinstance(params.get("title"), str):
                    raise RoutingError("toast requires a title")
                return await toaster.show(params)
            return await toaster.dismiss(str((params or {}).get("tag") or ""))
        if method in ("session_wake", "session_probe"):
            # Announced as session_wake/1 and session_probe/1 (design §8.7).
            if self.waker is None:
                raise RoutingError("waking is not available on this device")
            return await (self.waker.wake(params) if method == "session_wake" else self.waker.probe(params))
        handler = getattr(self, "handlers", {}).get(method)
        if handler is not None:  # e.g. plugin_repo_install / plugin_repo_job (design §6.12)
            return await handler(params if isinstance(params, dict) else {})
        if method != "tool_call":
            raise RoutingError("unsupported reverse RPC method")
        name, tool = self.resolve_mcp(params["mcp"]), params["tool"]
        workspace_name = params.get("workspace")
        scope = str(params.get("scope") or "default")
        arguments = dict(params.get("arguments") or {})
        key = (name, scope)
        async with self.lock:
            self.active[key] = self.active.get(key, 0) + 1
        client = None
        try:
            client = await self.client(name, scope)
            tools = await client.list_tools()
            descriptor = next((entry for entry in tools if entry.name == tool), None)
            if descriptor is None:
                raise RoutingError(f"unknown local tool {name}/{tool}")
            if workspace_name:
                workspace = self.catalog["workspaces"].get(workspace_name)
                if workspace is None:
                    raise RoutingError("workspace no longer exists")
                if "workdir" in descriptor.input_schema.get("properties", {}):
                    # A workspace is the default working directory; existing
                    # upstream policy continues to govern allowed paths.
                    if not arguments.get("workdir"):
                        arguments["workdir"] = workspace["path"]
            result = await client.call_tool_mcp(tool, arguments)
            return result.model_dump(mode="json", by_alias=True, exclude_none=True)
        except RoutingError:
            raise
        except Exception:
            # Tool failures arrive as isError results; an exception means the
            # local session itself failed (e.g. the service restarted). Sessions
            # outlive cloud reconnects, so drop only this one and let the next
            # call open a fresh session instead of failing until idle expiry.
            if client is not None:
                async with self.lock:
                    cached = self.sessions.get(key)
                    if cached and cached[0] is client:
                        self.sessions.pop(key, None)
                        self._retire(client)
            raise
        finally:
            async with self.lock:
                remaining = self.active.get(key, 1) - 1
                if remaining:
                    self.active[key] = remaining
                else:
                    self.active.pop(key, None)
                cached = self.sessions.get(key)
                if cached and cached[0] is client:
                    self.sessions[key] = (client, time.monotonic())

    async def reap(self) -> None:
        deadline = time.monotonic() - float(self.cloud.get("session_idle_ttl_s", 3600))
        async with self.lock:
            retired = []
            for key, (client, touched) in list(self.sessions.items()):
                if key[1] != "catalog" and touched < deadline and not self.active.get(key):
                    self.sessions.pop(key, None)
                    self.active.pop(key, None)
                    retired.append(client)
        closing = [self._retire(client) for client in retired]
        if closing:
            await asyncio.shield(asyncio.gather(*closing))

    async def close(self):
        async with self.lock:
            self.closed = True
            clients = [client for client, _ in self.sessions.values()]
            self.sessions.clear()
            self.pending.clear()
            opening = list(self.opening)
        for task in opening:
            if not task.cancelling():
                task.cancel()
        for client in clients:
            self._retire(client)
        # The SDK bounds cancellation cleanup after a failed enter (up to two
        # three-second phases). Keep its owner tasks alive to finish that work.
        deadline = time.monotonic() + max(7.0, self.close_timeout * 2)
        for pending in (opening, self.closing):
            if pending:
                done, unfinished = await asyncio.wait(list(pending), timeout=max(0, deadline - time.monotonic()))
                for task in done:
                    _task_result(task)
                if unfinished:
                    log.warning("local MCP cleanup still pending (%d tasks)", len(unfinished))


async def run_agent(root: Path, cloud: dict, *, status: dict | None = None, transports: dict | None = None,
                    ready: asyncio.Event | None = None, hub: HubLink | None = None, toaster=None) -> None:
    status = status if status is not None else {}
    agent_id, url, token = connection_settings(cloud, root)
    # One gateway outlives cloud reconnects: local MCP sessions carry per-scope
    # state (e.g. Unity's set_active_instance) that a dropped cloud socket
    # must not reset.
    gateway = LocalGateway(root, cloud, transports=transports, toaster=toaster)
    hub = hub or HubLink()
    gateway.handlers = hub.handlers
    from .link.wake import WakeRunner

    async def report(args: dict):
        if anx_features.WAKE not in hub.features:
            raise RoutingError("the gateway does not take wake reports")
        return await hub.call("wake_report", args)

    async def notify(payload: dict):
        return await gateway._toaster().show(payload)

    waker = None
    if callable(getattr(gateway, "wake_config", None)):  # test doubles of LocalGateway may lack it
        waker = WakeRunner(root, gateway.wake_config(), report=report, companion=gateway.companion_event,
                           notify=notify)
        gateway.waker = waker
        waker.start()
    try:
        await _run_connections(agent_id, url, token, cloud, gateway, status, ready, hub)
    finally:
        if waker is not None:
            await waker.close()
        await gateway.close()


async def _run_connections(agent_id: str, url: str, token: str, cloud: dict, gateway, status: dict,
                           ready: asyncio.Event | None, hub: HubLink | None = None) -> None:
    hub = hub if hub is not None else HubLink()
    delay = 1.0
    while True:
        tasks: dict[str, asyncio.Task] = {}
        refresh_task = None
        try:
            status.update(state="connecting", agent_id=agent_id)
            try:
                catalog = await asyncio.wait_for(gateway.refresh(), CONNECT_CATALOG_TIMEOUT_S)
            except asyncio.TimeoutError:
                if not gateway.catalog.get("mcps"):
                    raise
                log.warning("local MCP catalog took over %.0fs; connecting with the previous catalog",
                            CONNECT_CATALOG_TIMEOUT_S)
                catalog = gateway.catalog
            async with connect(f"{url}/ingress/{agent_id}", additional_headers={"Authorization": f"Bearer {token}"},
                               max_size=MAX_MESSAGE_BYTES, ping_interval=10, ping_timeout=15,
                               open_timeout=15, close_timeout=5, proxy=None) as socket:
                send_lock = asyncio.Lock()

                async def send(message, socket=socket, send_lock=send_lock, gateway=gateway):
                    await _send_agent_message(socket, send_lock, message, min(gateway.rpc_timeout, 15.0))

                # protocol stays 1; "anx" carries the feature exchange (design §3.2).
                await send({"protocol": 1, "agent_id": agent_id, "catalog": catalog,
                            "anx": anx_features.agent_anx(getattr(hub, "extra_features", ()))})
                response = json.loads(await asyncio.wait_for(socket.recv(), 15))
                if response.get("method") != "ready":
                    raise RoutingError("cloud handshake failed")
                params = response.get("params") if isinstance(response.get("params"), dict) else {}
                hub.attach(send, params.get("anx"))
                delay = 1.0
                status.pop("error", None)
                status.update(state="connected", since=time.time(), mcps=len(catalog["mcps"]), workspaces=len(catalog["workspaces"]),
                              gateway_features=sorted(hub.features), gateway_version=hub.gateway_version,
                              display_name=hub.display_name)
                log.info("connected to %s as %s (%d local MCPs)", url, agent_id, len(catalog["mcps"]))
                if ready:
                    ready.set()

                async def execute(message, gateway=gateway, send=send, tasks=tasks):
                    try:
                        try:
                            result = await gateway.dispatch(message["method"], message.get("params", {}))
                            response = {"id": message["id"], "result": result}
                        except asyncio.CancelledError:
                            raise
                        except Exception as exc:
                            log.warning("local MCP request failed (%s)", type(exc).__name__)
                            response = {"id": message["id"], "error": "local MCP call failed; execution outcome may be unknown"}
                        await send(response)
                    except asyncio.CancelledError:
                        raise
                    except Exception as exc:
                        # send() has already closed this failed connection.
                        log.warning("cloud response send failed (%s)", type(exc).__name__)
                    finally:
                        tasks.pop(message["id"], None)

                async def refresh_loop(gateway=gateway, send=send, socket=socket):
                    try:
                        while True:
                            await asyncio.sleep(max(5, float(cloud.get("refresh_interval_s", 30))))
                            await gateway.reap()
                            await send({"method": "catalog", "params": await gateway.refresh()})
                    except Exception:
                        # Wake the receive loop and rebuild the registry on reconnect.
                        await socket.close(code=1011)

                refresh_task = _agent_io_task(refresh_loop())
                async for raw in socket:
                    message = json.loads(raw)
                    if hub.resolve(message):
                        continue
                    if message.get("method") == "cancel":
                        task = tasks.get(message.get("params", {}).get("id"))
                        if task:
                            task.cancel()
                    elif isinstance(message.get("id"), str):
                        if len(tasks) >= 128 or message["id"] in tasks:
                            await send({"id": message["id"], "error": "manager request limit reached"})
                        else:
                            tasks[message["id"]] = _agent_io_task(execute(message))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            if _close_code(exc) == CLOSE_REVOKED:
                # Revoked in the console: retrying would only be refused again.
                status.update(state="revoked", error="revoked")
                log.warning("the gateway revoked this device (close code %d); not reconnecting", CLOSE_REVOKED)
                return
            status.update(state="disconnected", error=type(exc).__name__)
            log.warning("cloud connection unavailable (%s); reconnecting in %.0fs", _describe_failure(exc), delay)
        finally:
            hub.detach()
            pending = [*tasks.values(), *([refresh_task] if refresh_task else [])]
            for task in pending:
                if not task.cancelling():
                    task.cancel()
            if pending:
                # A cancelled SDK request may still be unwinding a dead
                # transport. Its observed owner can finish independently;
                # reconnecting must never await that cancellation tail forever.
                done, unfinished = await asyncio.wait(pending, timeout=min(gateway.rpc_timeout, 5.0))
                for task in done:
                    _task_result(task)
                if unfinished:
                    log.warning("old cloud request cleanup still pending (%d tasks); continuing reconnect", len(unfinished))
        await asyncio.sleep(delay)
        delay = min(delay * 2, 30)


async def run_configured_agent(root: Path, status: dict | None = None, hub: HubLink | None = None):
    config = paths.read_config(root)
    cloud = config.get("cloud", {})
    if not cloud.get("enabled", False):
        if status is not None:
            status.update(state="disabled")
        return
    await run_agent(root, cloud, status=status, hub=hub)


def main(argv=None) -> int:
    from mcpkit.runtime import install_windows_socket_cleanup
    install_windows_socket_cleanup()
    from .logs import configure_process_logging
    configure_process_logging()
    parser = argparse.ArgumentParser(prog="anx-agent", description="agent-nexus outbound connector (local MCPs to the gateway)")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    from .cli import default_root
    from .supervisor import Supervisor
    root = (args.root or default_root()).resolve()
    try:
        if not paths.config_path(root).is_file():
            raise ConfigError(f"no {paths.CONFIG_NAME} (or {paths.LEGACY_CONFIG_NAME}) in {root}")
        data = paths.read_config(root)
        cloud = data.get("cloud", {})
        connection_settings(cloud, root)
        if args.check_config:
            gateway = LocalGateway(root, cloud)
            gateway.targets = gateway.configured_targets()
            missing = {entry["mcp"] for entry in gateway.configured_workspaces().values()} - gateway.targets.keys()
            if missing:
                raise ConfigError(f"unconfigured workspace MCPs: {', '.join(sorted(missing))}")
            print("client configuration valid")
            return 0
        # Prefer the running console/supervisor. Two client processes must not
        # race to adopt/start the same services.
        from .cli import Api
        cfg = load_manager_config(root)
        sup = None
        if not Api(cfg).alive():
            sup = Supervisor(root)
            if not args.no_autostart:
                for line in sup.autostart():
                    print(line, flush=True)
            sup.run_background()
        try:
            asyncio.run(run_agent(root, cloud))
        finally:
            if sup:
                sup.shutdown()
        return 0
    except (ConfigError, RoutingError, ValueError, OSError) as exc:
        print(f"anx-agent: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


def deprecated_main() -> int:
    """The ``mcpm-client`` console script: same command, one deprecation line."""
    print("mcpm-client: deprecated command name; use `anx-agent` (same arguments)", file=sys.stderr, flush=True)
    return main()


if __name__ == "__main__":
    sys.exit(main())

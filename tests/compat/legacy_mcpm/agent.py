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
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from fastmcp import Client
from fastmcp.client.transports import StdioTransport, StreamableHttpTransport
from mcpkit import Service
from mcpkit.config import ConfigError, ServiceConfig, read_secret
from websockets.asyncio.client import connect
from websockets.exceptions import ConnectionClosed
import httpx2

from .cloud import env_secret
from .federation import MAX_MESSAGE_BYTES, RoutingError, valid_id, valid_media_token
from .registry import discover, load_manager_config, read_json

log = logging.getLogger(__name__)
MAX_MEDIA_BYTES = 20 * 1024 * 1024
# A hung local service must not keep the whole computer offline: reconnects
# wait this long for a fresh catalog, then reuse the last one they had.
CONNECT_CATALOG_TIMEOUT_S = 20.0
_agent_io_tasks: set[asyncio.Task] = set()


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
        token = env_secret(cloud.get("token_env", "MCPM_AGENT_TOKEN"))
    return agent_id, url, token


class LocalGateway:
    def __init__(self, root: Path, cloud: dict, *, transports: dict | None = None):
        self.root = root.resolve()
        self.cloud = cloud
        self.transports = transports
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

    def configured_workspaces(self) -> dict:
        entries = self.cloud.get("workspaces")
        if entries is None:
            data = read_json(self.root / "mcp-manager.json") if (self.root / "mcp-manager.json").exists() else {}
            path = data.get("services", {}).get("workspace", {}).get("settings", {}).get("default_workdir") or str(Path.home())
            entries = {"default": {"path": path, "mcp": "workspace"}}
        if not isinstance(entries, dict):
            raise ConfigError("cloud.workspaces must be an object")
        result = {}
        for key, value in entries.items():
            valid_id(key)
            value = {"path": value} if isinstance(value, str) else dict(value)
            path = (self.root / value["path"]).resolve()
            if not path.is_dir():
                raise ConfigError(f"workspace directory does not exist: {key}")
            result[key] = {"path": str(path), "mcp": valid_id(value.get("mcp", "workspace"))}
        return result

    def configured_targets(self) -> dict:
        self.descriptions = {}
        self.media_endpoints = {}
        if self.transports is not None:  # injectable in-memory transports for integration tests
            return dict(self.transports)
        cfg = load_manager_config(self.root)
        targets = {}
        for name, spec in discover(cfg).items():
            if spec.observed or not spec.enabled or not spec.supported or spec.error or spec.port is None:
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
        for name, entry in self.cloud.get("mcps", {}).items():
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

        results = await asyncio.gather(*(descriptor(name) for name in self.targets))
        async with self.lock:
            if self.closed or self.generation != generation:
                raise RoutingError("local MCP configuration changed during catalog refresh")
            self.catalog = {"workspaces": workspaces, "mcps": dict(results)}
            return self.catalog

    async def dispatch(self, method: str, params: dict) -> Any:
        # One budget covers acquisition, discovery and tool execution. The
        # Client's per-request timeout alone does not cover the whole route.
        return await asyncio.wait_for(self._dispatch(method, params), self.rpc_timeout)

    async def _dispatch(self, method: str, params: dict) -> Any:
        if method == "media_get":
            name = valid_id(params.get("mcp", "workspace"))
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
        if method != "tool_call":
            raise RoutingError("unsupported reverse RPC method")
        name, tool = params["mcp"], params["tool"]
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
                    ready: asyncio.Event | None = None) -> None:
    status = status if status is not None else {}
    agent_id, url, token = connection_settings(cloud, root)
    # One gateway outlives cloud reconnects: local MCP sessions carry per-scope
    # state (e.g. Unity's set_active_instance) that a dropped cloud socket
    # must not reset.
    gateway = LocalGateway(root, cloud, transports=transports)
    try:
        await _run_connections(agent_id, url, token, cloud, gateway, status, ready)
    finally:
        await gateway.close()


async def _run_connections(agent_id: str, url: str, token: str, cloud: dict, gateway, status: dict,
                           ready: asyncio.Event | None) -> None:
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

                await send({"protocol": 1, "agent_id": agent_id, "catalog": catalog})
                response = json.loads(await asyncio.wait_for(socket.recv(), 15))
                if response.get("method") != "ready":
                    raise RoutingError("cloud handshake failed")
                delay = 1.0
                status.pop("error", None)
                status.update(state="connected", mcps=len(catalog["mcps"]), workspaces=len(catalog["workspaces"]))
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
            status.update(state="disconnected", error=type(exc).__name__)
            log.warning("cloud connection unavailable (%s); reconnecting in %.0fs", _describe_failure(exc), delay)
        finally:
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


async def run_configured_agent(root: Path, status: dict | None = None):
    config = read_json(root / "mcp-manager.json")
    cloud = config.get("cloud", {})
    if not cloud.get("enabled", False):
        if status is not None:
            status.update(state="disabled")
        return
    await run_agent(root, cloud, status=status)


def main(argv=None) -> int:
    from mcpkit.runtime import install_windows_socket_cleanup
    install_windows_socket_cleanup()
    from .logs import configure_process_logging
    configure_process_logging()
    parser = argparse.ArgumentParser(description="Local MCP manager and outbound cloud client")
    parser.add_argument("--root", type=Path)
    parser.add_argument("--no-autostart", action="store_true")
    parser.add_argument("--check-config", action="store_true")
    args = parser.parse_args(argv)
    from .cli import default_root
    from .supervisor import Supervisor
    root = (args.root or default_root()).resolve()
    try:
        data = read_json(root / "mcp-manager.json")
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
        print(f"mcpm-client: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        return 130


if __name__ == "__main__":
    sys.exit(main())

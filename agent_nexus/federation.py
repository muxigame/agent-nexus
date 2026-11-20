"""Authenticated reverse RPC. Only the local manager opens a connection."""
from __future__ import annotations

import asyncio
import logging
import re
from dataclasses import dataclass, field
from typing import Any
from uuid import uuid4

ID_PATTERN = re.compile(r"[A-Za-z0-9_.-]{1,100}")
MEDIA_TOKEN_PATTERN = re.compile(r"[A-Za-z0-9_-]{32,128}")
RESERVED_TOOLS = {"set_workspace", "workspace_list", "device_list", "use_device",
                  "mcplist", "mcpdetail", "toolsearch", "tooluse",
                  # The mailbox (M6, design §5.5, §8.5) and ask_user (decision D11).
                  "mail_send", "mail_inbox", "mail_read", "mail_wait", "mail_who", "mail_bye",
                  "mail_status", "mail_recall", "ask_user"}
MAX_MESSAGE_BYTES = 32 * 1024 * 1024
#: The device plugin was called ``workspace`` before agent-nexus. Either name
#: reaches it, whichever one a connected (old or new) agent reports.
MCP_ALIASES = {"workspace": "device", "device": "workspace"}
_peer_io_tasks: set[asyncio.Task] = set()
log = logging.getLogger(__name__)


class RoutingError(ValueError):
    pass


def _observe_task(task: asyncio.Task) -> None:
    _peer_io_tasks.discard(task)
    if not task.cancelled():
        task.exception()


def _owned_task(coroutine) -> asyncio.Task:
    # The event loop keeps weak task references. Retain I/O owners until their
    # cancellation cleanup finishes, even after the caller has already left.
    task = asyncio.create_task(coroutine)
    _peer_io_tasks.add(task)
    task.add_done_callback(_observe_task)
    return task


def valid_id(value: Any) -> str:
    if not isinstance(value, str) or not ID_PATTERN.fullmatch(value):
        raise RoutingError("IDs must be 1-100 letters, digits, dots, underscores or hyphens")
    return value


def valid_media_token(value: Any) -> str:
    if not isinstance(value, str) or not MEDIA_TOKEN_PATTERN.fullmatch(value):
        raise RoutingError("invalid media token")
    return value


@dataclass
class Peer:
    agent_id: str
    socket: Any
    catalog: dict[str, Any]
    timeout: float = 180
    pending: dict[str, asyncio.Future] = field(default_factory=dict)
    lock: asyncio.Lock = field(default_factory=asyncio.Lock)
    slots: asyncio.Semaphore = field(default_factory=lambda: asyncio.Semaphore(128))
    #: Features announced in ``hello.anx`` (empty for a legacy agent). New
    #: reverse methods may only be sent when the matching feature is listed.
    features: frozenset = frozenset()
    anx_version: str | None = None
    #: The client's 锁定 (``hub_call lock_report``): it may be operated on, not operate on others.
    locked: bool = False
    _closed: bool = field(default=False, init=False, repr=False)
    _socket_close_task: asyncio.Task | None = field(default=None, init=False, repr=False)

    def _check_connected(self) -> None:
        if self._closed:
            raise RoutingError(f"manager {self.agent_id} disconnected; execution outcome may be unknown")

    async def send(self, message: dict) -> None:
        self._check_connected()
        async with self.lock:
            self._check_connected()
            await self.socket.send_json(message)

    async def _close_socket(self, timeout: float) -> None:
        if self._socket_close_task is None:
            close = getattr(self.socket, "close", None)
            if close is None:
                return
            try:
                self._socket_close_task = _owned_task(close(code=1011))
            except Exception:
                return
        task = self._socket_close_task
        try:
            await asyncio.wait((task,), timeout=timeout)
        finally:
            # Do not await cancellation: websocket cleanup can itself await
            # I/O. Its owner remains observed until that cleanup finishes.
            if not task.done() and not task.cancelling():
                task.cancel()

    async def _cancel_request(self, request_id: str, timeout: float) -> None:
        task = _owned_task(self.send({"method": "cancel", "params": {"id": request_id}}))
        try:
            await asyncio.wait((task,), timeout=timeout)
        finally:
            if not task.done():
                task.cancel()
                # A stalled sender may retain the send lock while unwinding.
                # Retire this connection so other calls cannot queue forever.
                self.disconnect()
                await self._close_socket(timeout)

    async def call(self, method: str, params: dict) -> Any:
        self._check_connected()
        timeout = self.timeout
        cleanup_timeout = min(timeout, 1.0)
        loop = asyncio.get_running_loop()
        request_id = uuid4().hex
        future = loop.create_future()
        # Register before admission so disconnect() wakes queued calls too.
        self.pending[request_id] = future
        dispatched = False
        cancel_remote = False

        async def request():
            nonlocal dispatched
            async with self.slots:
                self._check_connected()
                async with self.lock:
                    self._check_connected()
                    # A failed/interrupted send may already have reached the
                    # manager. Never retry it, even if no reply was received.
                    dispatched = True
                    await self.socket.send_json({"id": request_id, "method": method, "params": params})
                return await future

        operation = _owned_task(request())
        try:
            # One budget includes queueing, send-lock contention, socket I/O
            # and the reply. A reply/disconnect can also finish the call while
            # its worker is still waiting for admission or socket I/O.
            async with asyncio.timeout(timeout):
                done, _ = await asyncio.wait((operation, future),
                                             return_when=asyncio.FIRST_COMPLETED)
                if future in done:
                    return future.result()
                return operation.result()
        except (asyncio.TimeoutError, asyncio.CancelledError):
            cancel_remote = True
            raise
        finally:
            self.pending.pop(request_id, None)
            if not operation.done():
                operation.cancel()
            try:
                # wait_for/gather would wait for cancellation to finish and
                # defeat the deadline if send_json has asynchronous cleanup.
                if not operation.done():
                    await asyncio.wait((operation,), timeout=cleanup_timeout)
            finally:
                # A disconnect can fail the reply while the operation is
                # still sending; consume that exception even in this case.
                if future.done() and not future.cancelled():
                    future.exception()
                else:
                    future.cancel()
                if not operation.done():
                    self.disconnect()
                    await self._close_socket(cleanup_timeout)
                if cancel_remote and dispatched and not self._closed:
                    await self._cancel_request(request_id, cleanup_timeout)

    def resolve(self, message: dict) -> None:
        future = self.pending.get(message.get("id"))
        if future is None or future.done():
            return
        if "error" in message:
            future.set_exception(RoutingError(str(message["error"])))
        elif "result" in message:
            future.set_result(message["result"])

    def disconnect(self) -> None:
        self._closed = True
        for future in self.pending.values():
            if not future.done():
                future.set_exception(RoutingError(f"manager {self.agent_id} disconnected; execution outcome may be unknown"))


class Hub:
    def __init__(self, *, default_workspace: dict | None = None, timeout: float = 180):
        self.peers: dict[str, Peer] = {}
        self.default_workspace = default_workspace
        self.timeout = timeout
        #: Gateway-hosted connectors (design §7.1, decision D10): a peer-like
        #: ``ConnectorHost`` answering for the reserved agent_id ``hub``. It is
        #: never in ``peers`` (not a device) and is reachable with no device online.
        self.connectors = None

    def register(self, agent_id: str, socket: Any, catalog: dict, *, features: frozenset = frozenset(),
                 anx_version: str | None = None) -> Peer:
        valid_id(agent_id)
        self.validate_catalog(catalog)
        stale = self.peers.get(agent_id)
        if stale is not None:
            # An authenticated reconnect means the manager has given up on its
            # old socket, which the server may not notice until keepalive
            # expires. Rejecting the new socket kept the instance offline for
            # that whole window while calls were still sent to the dead one.
            log.warning("manager %s reconnected; replacing its previous connection", agent_id)
            stale.disconnect()
            _owned_task(stale._close_socket(1.0))
        peer = Peer(agent_id, socket, catalog, self.timeout, features=features, anx_version=anx_version)
        self.peers[agent_id] = peer
        return peer

    @staticmethod
    def validate_catalog(catalog: dict) -> None:
        if not isinstance(catalog, dict) or not isinstance(catalog.get("workspaces"), dict) or not isinstance(catalog.get("mcps"), dict):
            raise RoutingError("catalog requires workspaces and mcps objects")
        for key, workspace in catalog["workspaces"].items():
            valid_id(key)
            if not isinstance(workspace, dict) or not isinstance(workspace.get("path"), str):
                raise RoutingError("invalid workspace descriptor")
            if workspace.get("mcp") not in catalog["mcps"]:
                raise RoutingError("workspace references an unknown MCP")
        for key, mcp in catalog["mcps"].items():
            valid_id(key)
            if not isinstance(mcp, dict) or not isinstance(mcp.get("tools", []), list):
                raise RoutingError("invalid MCP descriptor")
            names = set()
            for tool in mcp.get("tools", []):
                if not isinstance(tool, dict) or not isinstance(tool.get("name"), str) or not isinstance(tool.get("inputSchema"), dict):
                    raise RoutingError("invalid tool descriptor")
                if tool["name"] in names:
                    raise RoutingError("duplicate upstream tool")
                names.add(tool["name"])

    def unregister(self, peer: Peer) -> None:
        if self.peers.get(peer.agent_id) is peer:
            self.peers.pop(peer.agent_id)
        peer.disconnect()

    def peer(self, agent_id: str) -> Peer:
        if agent_id == "hub" and self.connectors is not None:
            return self.connectors
        if agent_id not in self.peers:
            raise RoutingError(f"manager {agent_id} is offline")
        return self.peers[agent_id]

    def workspace(self, selection: dict | None) -> tuple[Peer, dict, str]:
        if selection is None:
            selection = self.default_workspace
        if selection is None:
            choices = [(p.agent_id, key) for p in self.peers.values() for key in p.catalog["workspaces"]]
            if len(choices) != 1:
                raise RoutingError("select a workspace with set_workspace (or provide workspace_id)")
            selection = {"agent_id": choices[0][0], "workspace": choices[0][1]}
        peer = self.peer(selection["agent_id"])
        name = selection["workspace"]
        workspace = peer.catalog["workspaces"].get(name)
        if workspace is None:
            raise RoutingError(f"unknown workspace {peer.agent_id}/{name}")
        return peer, workspace, name

    def inventory(self, agent_id: str | None = None) -> list[dict]:
        peers = [self.peer(agent_id)] if agent_id else list(self.peers.values())
        if not agent_id and self.connectors is not None:
            peers.append(self.connectors)
        items = []
        for peer in peers:
            for name, mcp in peer.catalog["mcps"].items():
                item = {"mcp_id": f"{peer.agent_id}/{name}", "agent_id": peer.agent_id,
                        "name": name, "description": mcp.get("description", ""),
                        "tool_count": len(mcp.get("tools", [])), "error": mcp.get("error")}
                if mcp.get("kind") == "connector":
                    # design §7.5: kind, run_on and auth_state for connector entries.
                    item.update(kind="connector", run_on=mcp.get("run_on", "gateway"),
                                auth_state=mcp.get("auth_state"), fingerprint=mcp.get("fingerprint"),
                                title=mcp.get("title"))
                items.append(item)
        return items

    def mcp(self, mcp_id: str) -> tuple[Peer, str, dict]:
        parts = mcp_id.split("/")
        if len(parts) != 2:
            raise RoutingError("mcp_id must be instance/service, as returned by mcplist")
        peer = self.peer(parts[0])
        name = parts[1]
        mcp = peer.catalog["mcps"].get(name)
        if mcp is None and MCP_ALIASES.get(name) in peer.catalog["mcps"]:
            name = MCP_ALIASES[name]
            mcp = peer.catalog["mcps"][name]
        if mcp is None:
            raise RoutingError(f"unknown MCP {mcp_id}")
        return peer, name, mcp

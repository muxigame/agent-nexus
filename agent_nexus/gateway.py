"""A single MCP facade ("Agent Nexus") over connected devices."""
from __future__ import annotations

import copy
import contextlib
import asyncio
import logging
import re
import time
from typing import Any, Callable
from uuid import uuid4

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_access_token, get_context
from fastmcp.server.middleware import Middleware
from fastmcp.server.providers import Provider
from fastmcp.tools.base import ToolResult
from mcp_types import CallToolResult, ToolAnnotations
from mcpkit.tools import SchemaTool, ToolFailure
from jsonschema import Draft202012Validator

from . import device_contract
from .tool_language import english_metadata
from .devices import Devices, device_block
from .federation import Hub, MCP_ALIASES, Peer, RESERVED_TOOLS, RoutingError
from .ids import RESUME_ID_RULE, valid_resume_id
from .inbox import InboxService
from .mailbox import DEVICE_ASK_NAME, ME, Mailbox, mailbox_config
from .store import GatewayStore
from .escalation import Escalation
from .wake import WakeRouter

log = logging.getLogger(__name__)

# Discovery and routing change nothing on any computer. ChatGPT asks for
# confirmation on every tool without readOnlyHint, which made each new
# conversation start with a prompt for workspace_list and set_workspace.
ROUTING_READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, open_world_hint=False)

_SEARCH_TOKEN = re.compile(r"[a-z0-9]+|[\u3400-\u9fff]+", re.IGNORECASE)


def _search_terms(text: str) -> list[str]:
    """Words suited to tool identifiers and short natural-language queries."""
    text = re.sub(r"(?<=[a-z0-9])(?=[A-Z])", " ", text).replace("_", "-")
    terms = []
    for raw in _SEARCH_TOKEN.findall(text.lower()):
        term = raw
        if term.isascii() and len(term) > 3 and term.endswith("s") and not term.endswith("ss"):
            term = term[:-1]  # pages/tabs/gameobjects -> page/tab/gameobject
        if term not in terms:
            terms.append(term)
    return terms


def score_tool_search(query: str, mcp_id: str, tool: dict) -> tuple[int, list[str]]:
    terms = _search_terms(query)
    if not terms:
        return 0, []
    name = str(tool.get("name", ""))
    description = str(tool.get("description", ""))
    name_terms, description_terms, mcp_terms = map(set, (
        _search_terms(name), _search_terms(description), _search_terms(mcp_id)))
    score, matched, name_matches = 0, [], 0
    if " ".join(terms) == " ".join(_search_terms(name)):
        score += 200
    for term in terms:
        points = 0
        if term in name_terms:
            points = 40
            name_matches += 1
        elif len(term) >= 4 and any(item.startswith(term) or term.startswith(item) for item in name_terms):
            points = 24
            name_matches += 1
        elif term in description_terms:
            points = 12
        elif len(term) >= 4 and any(item.startswith(term) or term.startswith(item)
                                     for item in description_terms if len(item) >= 4):
            points = 8
        elif term in mcp_terms:
            points = 12
        if points:
            score += points
            matched.append(term)
    if matched:
        score += round(30 * len(matched) / len(terms))
    if name_matches >= 2:
        # Prefer the direct, compact tool name over a longer tool that happens
        # to contain the same generic words.
        score += max(4, 24 - 4 * len(name_terms))
    return score, matched


#: ``gateway.legacy_tools`` (design §5.5): ``listed`` shows the old routing
#: names next to the new ones, ``hidden`` keeps them callable but unlisted,
#: ``off`` removes them (never meant for production: existing ChatGPT and
#: Claude connectors call them).
LEGACY_TOOLS_MODES = ("listed", "hidden", "off")
LEGACY_TOOL_NAMES = frozenset({"workspace_list", "set_workspace"})
SESSION_KINDS = ("claude", "codex", "gpt", "human", "other")
WAKE_MODES = ("auto", "never")
MAX_HANDLES = 10000
_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

INSTRUCTIONS = (
    "Use device_list, then use_device to choose a computer (device) and one of its workdirs. "
    "device_list shows each device's display name (the owner's alias or its hostname) and its id; "
    "either one is accepted wherever a device is expected. "
    "The older names workspace_list and set_workspace still work and return the same handle. "
    "Device tools are exposed directly. Other MCPs are accessed through mcplist, mcpdetail, "
    "toolsearch and tooluse. Keep the workspace_id returned by use_device and pass it to every call "
    "for explicit routing, especially with sessionless clients; it stays valid across gateway restarts. "
    "When calling use_device, pass session_name and kind so the session keeps a stable identity; "
    "without session_name the session gets an automatic name from its client and workdir "
    "(such as claude-myproject), and a later use_device or set_workspace with session_name and the "
    "workspace_id you hold renames it, mail included. "
    "Each session then has a mailbox address (name@device): mail_who lists addresses, mail_send sends "
    "(to another session, a group such as @all, or to 'me', the owner), mail_inbox and mail_read read. "
    "Every tool result ends with a notice when you have unread mail. ask_user asks the owner a question. "
    "Third-party MCP connectors added by the owner run on the gateway as hub/<name> (mcplist, toolsearch, "
    "tooluse) and work even when no device is online. "
    "Tool execution is never retried automatically."
)


class HideLegacyTools(Middleware):
    """``legacy_tools = "hidden"``: drop the old names from tools/list only; calls still work."""

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if tool.name not in LEGACY_TOOL_NAMES]


def _client_id() -> str | None:
    with contextlib.suppress(Exception):
        token = get_access_token()
        if token is not None and isinstance(getattr(token, "client_id", None), str):
            return token.client_id[:200]
    return None


def _session_meta(session_name, kind, resume_id, companion_route, wake) -> dict:
    """Optional identity parameters of use_device/set_workspace (design §8.2).

    M4 validates and records them with the handle; the mailbox (M6) binds
    them to an address and the wake router (M7) acts on them.
    """
    meta: dict[str, Any] = {}
    if session_name is not None:
        if not isinstance(session_name, str) or not _SESSION_NAME.fullmatch(session_name):
            raise RoutingError("session_name must be 1-64 letters, digits, dots, underscores or hyphens")
        meta["session_name"] = session_name
    if kind is not None:
        if kind not in SESSION_KINDS:
            raise RoutingError(f"kind must be one of {', '.join(SESSION_KINDS)}")
        meta["kind"] = kind
    if resume_id is not None:
        if not valid_resume_id(resume_id):
            raise RoutingError(RESUME_ID_RULE)
        meta["resume_id"] = resume_id
    for key, value in (("companion_route", companion_route),):
        if value is not None:
            if not isinstance(value, str) or not 0 < len(value) <= 200:
                raise RoutingError(f"{key} must be a short string")
            meta[key] = value
    if wake not in WAKE_MODES:
        raise RoutingError(f"wake must be one of {', '.join(WAKE_MODES)}")
    if wake != "auto":
        meta["wake"] = wake
    return meta


#: The device toasts (design §8.12): the reverse RPCs need ``toast_actions/1``.
TOAST_FEATURE = "toast_actions/1"
TOAST_TIMEOUT_S = 10.0


def client_kind_from_name(name: Any) -> str | None:
    """``kind`` from a DCR ``client_name`` (design §8.2): claude, codex, gpt, else unknown."""
    if not isinstance(name, str):
        return None
    lowered = name.lower()
    if "codex" in lowered:
        return "codex"
    if "claude" in lowered or "anthropic" in lowered:
        return "claude"
    if "chatgpt" in lowered or "openai" in lowered:
        return "gpt"
    return None


class Gateway:
    def __init__(self, hub: Hub, store: GatewayStore | None = None, *, legacy_tools: str = "listed",
                 mailbox: dict | None = None):
        if legacy_tools not in LEGACY_TOOLS_MODES:
            raise ValueError(f"gateway.legacy_tools must be one of {', '.join(LEGACY_TOOLS_MODES)}")
        self.hub = hub
        # Handles are application-level workspace contexts, also usable by
        # clients using the sessionless MCP transport. They are persisted
        # (design §8.1) so a gateway restart does not invalidate them.
        self.store = store if store is not None else GatewayStore(None)
        self.devices = Devices(hub, self.store)
        self.legacy_tools = legacy_tools
        self.mailbox_config, self.mailbox_warnings = mailbox_config(mailbox)
        self.mailbox = Mailbox(self.store, self.mailbox_config, display_device=self.devices.display_name,
                               resolve_device=self.devices.resolve, online=lambda agent_id: agent_id in hub.peers)
        self.inbox = InboxService(self)
        # Waking recipients (M7, design §8.7) and unread escalation (decision D9, §8.11).
        self.wake = WakeRouter(self)
        self.mailbox.router = self.wake if self.mailbox_config["wake"]["enabled"] else None
        self.escalation = Escalation(self)
        #: DCR client_id -> session kind (set by the cloud app from its OAuth store).
        self.client_kind: Callable[[str | None], str | None] = lambda client_id: None
        #: DCR client_id -> its client_name (automatic session names, design §8.2 layer 1).
        self.client_name: Callable[[str | None], str | None] = lambda client_id: None
        self._mail_event: asyncio.Event | None = None
        self._ask_waiters: dict[str, list[asyncio.Future]] = {}
        self._background: set[asyncio.Task] = set()
        #: Every toast/notify the gateway sent: (agent_id, method, params), newest last.
        self.toast_log: list[tuple[str, str, dict]] = []
        #: Invites and per-device credentials (M16, design §11.5); set by the cloud app.
        self.enrollment = None
        #: P2PGateway (p2p/1), set by build_cloud_app.
        self.p2p = None

    async def selection(self, workspace_id: str | None = None) -> dict | None:
        if workspace_id:
            record = self.store.touch_handle(workspace_id) if isinstance(workspace_id, str) else None
            if record is None:
                raise RoutingError("workspace_id is unknown or expired; call use_device (or set_workspace) again")
            return {"agent_id": record["agent_id"], "workspace": record["workspace"], "scope": workspace_id}
        try:
            return await get_context().get_state("mcpm_workspace")
        except RuntimeError:
            return None

    async def caller_address(self, workspace_id: str | None) -> str | None:
        """The mailbox address of the caller: its handle, else the stateful session's selection."""
        if workspace_id:
            if not isinstance(workspace_id, str) or self.store.touch_handle(workspace_id) is None:
                raise RoutingError("workspace_id is unknown or expired; call use_device (or set_workspace) again")
            return self._address_of(workspace_id)
        selection = None
        with contextlib.suppress(RuntimeError):
            selection = await get_context().get_state("mcpm_workspace")
        if isinstance(selection, dict) and selection.get("scope"):
            return self._address_of(selection["scope"])
        return None

    def _address_of(self, handle: str) -> str | None:
        displaced = self.mailbox.displaced(handle)
        if displaced is not None:
            # §8.2: another conversation took this automatic address over (or it was renamed);
            # this handle must not read or send as it.
            raise RoutingError(f"this workspace_id no longer holds {self.mailbox.display(displaced)}: another "
                               "session in the same folder uses that address now. Call set_workspace (or "
                               "use_device) again to get your own address, or pass session_name to choose one")
        return self.mailbox.address_for_handle(handle)

    # -- mail events: waiting, toasts (design §8.5, §8.12) ---------------------
    def _changed(self) -> None:
        event, self._mail_event = self._mail_event, None
        if event is not None:
            event.set()

    def _event(self) -> asyncio.Event:
        if self._mail_event is None:
            self._mail_event = asyncio.Event()
        return self._mail_event

    def wait_budget(self, timeout_s: Any) -> float:
        try:
            requested = float(timeout_s)
        except (TypeError, ValueError):
            raise RoutingError("timeout_s must be a number") from None
        return max(0.0, min(requested, self.hub.timeout - 10))

    async def caller_handle(self, workspace_id: str | None) -> str | None:
        """The handle a mail tool call acts through: its workspace_id, else the session's selection."""
        if isinstance(workspace_id, str) and workspace_id:
            return workspace_id
        selection = None
        with contextlib.suppress(RuntimeError):
            selection = await get_context().get_state("mcpm_workspace")
        if isinstance(selection, dict) and isinstance(selection.get("scope"), str):
            return selection["scope"]
        return None

    async def wait_for_mail(self, address: str, timeout_s: Any, thread_id: str | None = None,
                            handle: str | None = None) -> dict:
        """``mail_wait``: the unread mail of ``address`` once there is some, or the timeout.

        With ``handle`` the wait counts as a use of that handle (its address is not "idle" for a
        takeover while the call is pending), and the handle's identity is checked again before
        anything is returned: mail that arrived after another conversation took the address
        over is never handed to this one.
        """
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self.wait_budget(timeout_s)
        with self.mailbox.in_use(handle):
            while True:
                event = self._event()
                if handle is not None:
                    address = self._address_of(handle) or address  # displaced: RoutingError; renamed: follows
                view = self.mailbox.inbox(address, unread_only=True, thread_id=thread_id)
                remaining = deadline - loop.time()
                if view["messages"] or remaining <= 0:
                    view["timed_out"] = not view["messages"]
                    return view
                with contextlib.suppress(asyncio.TimeoutError):
                    await asyncio.wait_for(event.wait(), remaining)

    async def wait_for_answer(self, ask_id: str, timeout_s: Any) -> dict | None:
        """``ask_user(blocking=True)``: the answer, or ``None`` when the budget runs out."""
        budget = self.wait_budget(timeout_s)
        future = asyncio.get_running_loop().create_future()
        self._ask_waiters.setdefault(ask_id, []).append(future)
        try:
            current = self.mailbox.ask_get(ask_id)
            if current["state"] == "answered":
                return current["answer"]
            try:
                answered = await asyncio.wait_for(asyncio.shield(future), budget)
            except asyncio.TimeoutError:
                return None
            # The caller has the answer in hand: its answer mail needs no notice.
            if answered.get("message_id"):
                self.mailbox.mark_read(answered["message_id"], answered["asker"])
            return answered["answer"]
        finally:
            waiters = self._ask_waiters.get(ask_id, [])
            if future in waiters:
                waiters.remove(future)
            if not waiters:
                self._ask_waiters.pop(ask_id, None)

    def mail_sent(self, result: dict) -> None:
        """After any send: wake waiters; mail for the owner also toasts on the devices."""
        self._changed()
        if any(item["to"] == ME for item in result.get("deliveries", [])):
            message = self.mailbox.read(ME, message_id=result["message_id"], mark_read=False)["messages"]
            if message and message[0]["kind"] != "question":  # questions toast through ask_created
                item = message[0]
                self.toast_all({"tag": f"anx-thread-{result['thread_id']}", "thread_id": result["thread_id"],
                                "title": f"{item['from']}: {item['subject'] or '新邮件'}"[:256],
                                "body": item["body"][:500], "link": f"#/inbox/{result['thread_id']}",
                                "level": "urgent" if item["priority"] == "urgent" else "info", "actions": []})

    def ask_created(self, ask_id: str) -> None:
        self._changed()
        ask = self.mailbox.ask_get(ask_id)
        max_buttons = int(self.mailbox_config["inbox"]["toast_max_buttons"])
        link = f"#/inbox/{ask['thread_id']}"
        if ask["kind"] == "single" and ask["options"] and len(ask["options"]) <= max_buttons:
            # Simple questions are answered from the toast itself (§8.12).
            actions = [{"id": f"choice-{index}", "label": (item + ("（推荐）" if item == ask["recommended"] else ""))[:60],
                        "ask_id": ask_id, "choice": item} for index, item in enumerate(ask["options"])]
        else:
            actions = [{"id": "open", "label": "打开回答", "link": link}]
        self.toast_all({"tag": f"anx-ask-{ask_id}", "thread_id": ask["thread_id"], "ask_id": ask_id,
                        "title": f"{ask['from']} 提问"[:256], "body": ask["question"][:500], "link": link,
                        "level": "urgent", "actions": actions})

    async def answer_ask(self, ask_id: str, *, choice=None, text=None, choices=None, via: str = "inbox") -> dict:
        result = self.mailbox.ask_answer(ask_id, choice=choice, text=text, choices=choices, via=via)
        if result.get("already"):
            return result
        self._changed()
        for future in self._ask_waiters.get(ask_id, []):
            if not future.done():
                future.set_result(result)
        # The same question's toast disappears from every other device (§8.12).
        self.toast_all({"tag": f"anx-ask-{ask_id}"}, method="toast_dismiss")
        return result

    def toast_all(self, payload: dict, *, method: str = "toast") -> None:
        if not self.mailbox_config["inbox"]["toast"]:
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        for peer in list(self.hub.peers.values()):
            task = loop.create_task(self._toast_peer(peer, method, payload))
            self._background.add(task)
            task.add_done_callback(self._background.discard)

    def notify_device(self, peer: Peer, payload: dict) -> None:
        """One toast (or plain notify) on one device, in the background."""
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return
        task = loop.create_task(self._toast_peer(peer, "toast", payload))
        self._background.add(task)
        task.add_done_callback(self._background.discard)

    async def _toast_peer(self, peer: Peer, method: str, payload: dict) -> None:
        """``toast``/``toast_dismiss`` to devices that announced them; a plain ``notify`` to older ones."""
        try:
            if TOAST_FEATURE in peer.features:
                self.toast_log.append((peer.agent_id, method, payload))
                await asyncio.wait_for(peer.call(method, payload), TOAST_TIMEOUT_S)
                return
            if method != "toast":
                return
            for name, mcp in peer.catalog.get("mcps", {}).items():
                if self.is_device_mcp(peer, name) and any(tool.get("name") == "notify" for tool in mcp.get("tools", [])):
                    arguments = {"title": payload["title"], "body": payload.get("body", ""),
                                 "level": payload.get("level", "info")}
                    self.toast_log.append((peer.agent_id, "notify", arguments))
                    await asyncio.wait_for(peer.call("tool_call", {"mcp": name, "tool": "notify",
                                                                   "arguments": arguments, "scope": "toast"}),
                                           TOAST_TIMEOUT_S)
                    return
        except Exception as exc:  # a toast is best effort; the mail is already delivered
            log.info("toast to %s failed (%s)", peer.agent_id, type(exc).__name__)

    async def maintenance(self) -> None:
        """Retention (design §8.4), once a day; runs in the app lifespan."""
        interval = float(self.mailbox_config["retention"]["interval_s"]) or 86400.0
        while True:
            await asyncio.sleep(min(interval, 300.0))
            try:
                removed = await asyncio.to_thread(self.mailbox.retention)
                if any(removed.values()):
                    log.info("mailbox retention: %s", removed)
            except Exception as exc:
                log.warning("mailbox retention failed (%s)", type(exc).__name__)
            await asyncio.sleep(max(0.0, interval - 300.0))

    async def _touch(self, workspace_id: str | None) -> None:
        """Optional caller identification on discovery tools (design §3.4): never an error."""
        if workspace_id:
            with contextlib.suppress(RoutingError):
                await self.selection(workspace_id)

    # -- device capabilities (design §5.2 rule 2, §5.4) -----------------------
    @staticmethod
    def is_device_mcp(peer: Peer, name: str) -> bool:
        block = device_block(peer.catalog)
        return name in MCP_ALIASES or (bool(block.get("mcp")) and name == block.get("mcp"))

    def tool_capability(self, tool: str) -> str | None:
        """The capability a tool name belongs to: any connected device's ``_meta`` tag, else the contract table."""
        for peer in list(self.hub.peers.values()):
            for name, mcp in peer.catalog["mcps"].items():
                if not self.is_device_mcp(peer, name):
                    continue
                for descriptor in mcp.get("tools", []):
                    if descriptor.get("name") == tool:
                        found = device_contract.tool_capability(descriptor)
                        if found:
                            return found
        return device_contract.standard_capability(tool)

    def missing_capability(self, peer: Peer, tool: str) -> RoutingError | None:
        """A readable error for a device tool this device does not have, naming the capability."""
        capability = self.tool_capability(tool)
        if capability is None:
            return None
        block = device_block(peer.catalog)
        declared = (block.get("capabilities") or {}).get(capability) if isinstance(block.get("capabilities"), dict) else None
        if isinstance(declared, dict) and declared.get("available") is not False:
            return None  # declared and available, but this tool is not offered: an ordinary unknown tool
        message = (f"device {self.devices.label(peer.agent_id)} lacks capability '{capability}' "
                   f"(platform={block.get('platform') or 'unknown'})")
        if isinstance(declared, dict) and isinstance(declared.get("reason"), str) and declared["reason"]:
            message += f": {declared['reason'][:300]}"
        return RoutingError(message)

    @staticmethod
    def declared_capabilities(peer: Peer) -> dict | None:
        """The capabilities a contract device declares; ``None`` for a legacy or degraded device (no filtering)."""
        block = device_block(peer.catalog)
        capabilities = block.get("capabilities")
        if not isinstance(capabilities, dict) or block.get("degraded"):
            return None
        return capabilities

    def agent_filter(self, agent_id: str | None) -> str | None:
        """mcplist/toolsearch ``agent_id``: a device id or name, or ``hub`` for the gateway's connectors."""
        if not agent_id:
            return None
        if agent_id == "hub" and self.hub.connectors is not None:
            return "hub"
        return self.devices.resolve(agent_id)

    async def call(self, mcp_id: str, tool: str, arguments: dict, workspace_id: str | None = None) -> ToolResult:
        if self.hub.connectors is not None and isinstance(mcp_id, str) and mcp_id.startswith("hub/") and mcp_id.count("/") == 1:
            # Gateway-hosted connector (design §7.5): called in-process; a
            # workspace_id only identifies the caller, it never routes.
            if workspace_id:
                await self.selection(workspace_id)
            return await self.hub.connectors.call_tool(mcp_id[4:], tool, arguments)
        peer, name, mcp = self.hub.mcp(mcp_id)
        descriptor = next((entry for entry in mcp.get("tools", []) if entry["name"] == tool), None)
        if descriptor is None:
            missing = self.missing_capability(peer, tool) if self.is_device_mcp(peer, name) else None
            raise missing or RoutingError(f"unknown tool {mcp_id}/{tool}")
        problems = list(Draft202012Validator(descriptor["inputSchema"]).iter_errors(arguments))
        if problems:
            raise RoutingError("invalid tool arguments: " + "; ".join(error.message for error in problems[:3]))
        selection = await self.selection(workspace_id)
        if workspace_id and selection and selection["agent_id"] != peer.agent_id:
            raise RoutingError("workspace_id belongs to a different manager than mcp_id")
        if selection is None:
            with contextlib.suppress(RoutingError):
                default_peer, default_ws, default_name = self.hub.workspace(None)
                if default_peer is peer and default_ws["mcp"] == name:
                    selection = {"agent_id": peer.agent_id, "workspace": default_name}
        workspace_name = None
        if selection and selection["agent_id"] == peer.agent_id:
            workspace_name = selection["workspace"]
        # Isolate upstream MCP sessions by downstream context, not just service.
        try:
            context = get_context()
            request_context = context.request_context
            # Modern HTTP has no session identity. Explicit workspace handles
            # isolate workflows; anonymous/default calls use one stable pool.
            if request_context and request_context.request and request_context.protocol_version >= "2026-07-28":
                scope = "default"
            else:
                scope = context.session_id
        except RuntimeError:
            scope = "default"
        if selection:
            scope = selection.get("scope", scope)
        try:
            result = await peer.call("tool_call", {"mcp": name, "tool": tool, "arguments": arguments,
                                                   "workspace": workspace_name, "scope": scope})
        except asyncio.TimeoutError:
            raise RoutingError("local MCP call timed out; execution outcome may be unknown") from None
        return ToolResult.from_mcp_result(CallToolResult.model_validate(result))

    async def _previous_handle(self, previous: Any, ctx: Context, client_id: str | None) -> str | None:
        """The caller's earlier handle: the workspace_id it passed, else this MCP session's selection.

        Only a live handle of the same OAuth client counts (layers 1 and 3 of §8.2 reuse or rename
        the automatic address it holds); nothing is ever guessed from the client and folder alone.
        """
        if not (isinstance(previous, str) and previous):
            previous = None
            with contextlib.suppress(Exception):
                selection = await ctx.get_state("mcpm_workspace")
                if isinstance(selection, dict) and isinstance(selection.get("scope"), str):
                    previous = selection["scope"]
        if previous is None or len(previous) > 200:
            return None
        record = self.store.touch_handle(previous)
        if record is None:
            return None
        if record.get("client_id") and client_id and record["client_id"] != client_id:
            return None
        return previous

    async def select(self, device: str, workspace: str | None, ctx: Context, meta: dict,
                     previous: str | None = None) -> dict:
        """The one implementation behind use_device and set_workspace."""
        agent_id = self.devices.resolve(device)
        peer = self.hub.peer(agent_id)
        if workspace is None or (workspace == "default" and "default" not in peer.catalog["workspaces"]
                                 and len(peer.catalog["workspaces"]) == 1):
            # use_device's default workdir: "default", or the only one there is.
            workspace = next(iter(peer.catalog["workspaces"]), "default")
        selection = {"agent_id": agent_id, "workspace": workspace}
        try:
            _, descriptor, _ = self.hub.workspace(selection)
        except RoutingError:
            names = ", ".join(sorted(peer.catalog["workspaces"])) or "none"
            raise RoutingError(f"unknown workdir {workspace!r} on {self.devices.label(agent_id)}; "
                               f"available: {names}") from None
        if self.store.live_handle_count() >= MAX_HANDLES:
            raise RoutingError("workspace context limit reached")
        handle = uuid4().hex
        client_id = _client_id()
        self.store.put_handle(handle, agent_id, workspace, client_id=client_id, meta=meta or None)
        # Identity (design §8.2): every handle gets a mailbox address.
        kind_hint = client_name = None
        with contextlib.suppress(Exception):
            kind_hint = self.client_kind(client_id)
        with contextlib.suppress(Exception):
            client_name = self.client_name(client_id)
        previous = await self._previous_handle(previous, ctx, client_id)
        try:
            bound = self.mailbox.bind(handle, agent_id, workdir=descriptor.get("path"), meta=meta,
                                      client_id=client_id, kind_hint=kind_hint, client_name=client_name,
                                      previous=previous)
        except RoutingError:
            self.store.drop_handle(handle)
            raise
        selection["scope"] = handle
        await ctx.set_state("mcpm_workspace", selection)
        # Older stateful transports support notifications; modern clients
        # rediscover per request and use the explicit workspace handle.
        with contextlib.suppress(RuntimeError, AttributeError):
            await ctx.session.send_tool_list_changed()
        result = {"workspace_id": handle, "agent_id": agent_id, "workspace": workspace, **descriptor,
                  "display_name": self.devices.display_name(agent_id),
                  "device_display": self.devices.label(agent_id),
                  "address": bound["address"], "address_display": bound["address_display"],
                  "name_source": bound["name_source"], "mail": {"unread": bound["unread"]}}
        if bound.get("renamed_from"):
            result["renamed_from"] = bound["renamed_from_display"]
        return result

    async def hub_call(self, peer, params: Any) -> dict:
        """A device -> gateway request (``hub_call/1``, design §3.2).

        M4 implements ``device_admin`` (list devices, set or clear an alias:
        the local console's device page); M16 adds revoke and the invite ops
        (decision D3, design §11.5). Other ops arrive with their milestones
        and are refused by name until then.
        """
        if not isinstance(params, dict) or not isinstance(params.get("op"), str):
            raise RoutingError("hub_call requires params.op")
        op = params["op"]
        args = params.get("args") or {}
        if not isinstance(args, dict):
            raise RoutingError("hub_call args must be an object")
        if op == "p2p_config":
            # This device's tunnel agent: control plane, its own token, reflector addresses.
            if self.p2p is None:
                raise RoutingError("p2p is not available on this gateway")
            return await self.p2p.device_config(peer.agent_id)
        if op == "remote_ticket":
            # A one-time ticket from another device's remote hub, for a stream over the tunnel.
            if self.p2p is None:
                raise RoutingError("p2p is not available on this gateway")
            return await self.p2p.ticket(peer, args)
        if op == "lock_report":
            # The client's 锁定 (private-network model): a locked device can be operated on
            # but may not operate on other devices. Reported on connect and on every change.
            peer.locked = bool(args.get("locked"))
            return {"locked": peer.locked}
        if op == "device_admin":
            action = args.get("op")
            if action != "list" and getattr(peer, "locked", False):
                raise RoutingError("locked: 这台设备已锁定，解锁后才能操作其他设备")
            if action == "list":
                if self.enrollment is not None:
                    return {"devices": self.devices.console_list()}
                return {"devices": self.devices.list(include_offline=bool(args.get("include_offline", True)))}
            if action == "set_alias":
                return {"device": self.devices.set_alias(args.get("device_id"), args.get("alias"))}
            if action == "delete":
                return {"deleted": self.devices.delete(args.get("device_id"))}
            if action in ("revoke", "invite_create", "invite_list", "invite_revoke") and self.enrollment is not None:
                return await self.device_admin_enrollment(action, args)
            raise RoutingError(f"unsupported device_admin op {action!r}")
        if op == "connector_registry" and self.hub.connectors is not None:
            # The local console's connectors page writes the same registry (design §7.1).
            return await self.hub.connectors.registry_op(args)
        if op == "mail_local":
            # The local console's inbox page (design §8.12).
            return await self.inbox.call(args.get("op"), args, via="inbox")
        if op == "ask_create":
            # The device plugin's request_user_input, merged into ask_user (§8.12).
            asker = self.mailbox.ensure_address(DEVICE_ASK_NAME, peer.agent_id)
            options = args.get("options") if isinstance(args.get("options"), list) and args.get("options") else None
            kind = args.get("kind") if args.get("kind") in ("single", "multi", "text") else None
            created = self.mailbox.ask_create(
                asker, args.get("question"), options=options, recommended=args.get("recommended"),
                context=str(args.get("context") or ""),
                origin="request_user_input" if args.get("origin") == "request_user_input" else "ask_user",
                kind=(kind if kind != "text" else "single") if options else "text",
                title=args.get("title") if isinstance(args.get("title"), str) else None,
                allow_other=bool(args.get("allow_custom", True)) or not options)
            self.ask_created(created["ask_id"])
            return created
        if op == "ask_get":
            return self.mailbox.ask_get(str(args.get("ask_id") or ""))
        if op == "wake_report":
            # The device that ran a wake job reports its process (design §8.7).
            return self.wake.report(peer.agent_id, args)
        if op == "presence_report":
            # Claude / Codex hooks via the local manager (design §8.3 source 1).
            return self.mailbox.presence_report(peer.agent_id, args)
        if op == "ask_answer":
            via = args.get("via") if args.get("via") in ("toast", "inbox", "cli", "pwa") else "toast"
            return await self.answer_ask(str(args.get("ask_id") or ""), choice=args.get("choice"),
                                         text=args.get("text"), choices=args.get("choices"), via=via)
        raise RoutingError(f"unsupported hub_call op {op!r}")

    async def device_admin_enrollment(self, action: str, args: dict) -> dict:
        """The local console's invite and revoke buttons (same bodies as the gateway console API)."""
        from .invites import EnrollError
        try:
            if action == "revoke":
                agent_id = self.devices.resolve(args.get("device_id"))
                result = await self.enrollment.revoke_device(agent_id)
                return {"device": self.devices.console_describe(agent_id), "closed": result["closed"]}
            if action == "invite_create":
                return self.enrollment.create_invite(label=args.get("label"), ttl_s=args.get("ttl_s"),
                                                     agent_id=args.get("agent_id"))
            if action == "invite_list":
                return {"invites": self.enrollment.list_invites(args.get("state", "unused"))}
            return {"invite": self.enrollment.revoke_invite(args.get("invite_id"))}
        except EnrollError as exc:
            raise RoutingError(f"{exc.code}: {exc.message}") from None

    def build(self) -> FastMCP:
        server = FastMCP("Agent Nexus", instructions=INSTRUCTIONS, providers=[WorkspaceProvider(self)])
        if self.legacy_tools == "hidden":
            server.add_middleware(HideLegacyTools())
        legacy = self.legacy_tools != "off"

        @server.tool(title="List devices", annotations=ROUTING_READ_ONLY)
        async def device_list(include_offline: bool = False, capability: str | None = None,
                              workspace_id: str | None = None) -> dict:
            """List devices (computers) with their display names, ids, capabilities and workdirs.
            display_name is the owner's alias or the device's hostname; device_id is the stable id."""
            await self._touch(workspace_id)
            return {"devices": self.devices.list(include_offline=include_offline, capability=capability)}

        @server.tool(title="Choose a device", annotations=ROUTING_READ_ONLY)
        async def use_device(device_id: str, ctx: Context, workdir: str = "default",
                             session_name: str | None = None, kind: str | None = None,
                             resume_id: str | None = None, companion_route: str | None = None,
                             wake: str = "auto", workspace_id: str | None = None) -> dict:
            """Choose a device (by device_id or display name) and one of its workdirs.
            Returns the workspace_id to pass to every later call. Pass the workspace_id you already
            hold to keep (or, with session_name, rename) your mailbox address."""
            meta = _session_meta(session_name, kind, resume_id, companion_route, wake)
            selected = await self.select(device_id, workdir, ctx, meta, previous=workspace_id)
            selected["device_id"] = selected["agent_id"]
            selected["hint"] = (f"Pass workspace_id={selected['workspace_id']} with every call. "
                                f"Device: {selected['device_display']}, workdir: {selected['workspace']}.")
            return selected

        if legacy:
            @server.tool(title="List computers and workspaces", annotations=ROUTING_READ_ONLY)
            async def workspace_list(workspace_id: str | None = None) -> dict:
                """List connected computers/manager instances and their configured workspaces."""
                await self._touch(workspace_id)
                return {"workspaces": [{"agent_id": peer.agent_id, "workspace": name, **workspace,
                                        "device_form": (peer.catalog.get("device") or {}).get("form")
                                        if isinstance(peer.catalog.get("device"), dict) else None,
                                        "online": True,
                                        "display_name": self.devices.display_name(peer.agent_id)}
                                       for peer in list(self.hub.peers.values())
                                       for name, workspace in peer.catalog["workspaces"].items()]}

            @server.tool(title="Choose a workspace", annotations=ROUTING_READ_ONLY)
            async def set_workspace(agent_id: str, workspace: str, ctx: Context,
                                    session_name: str | None = None, kind: str | None = None,
                                    resume_id: str | None = None, companion_route: str | None = None,
                                    wake: str = "auto", workspace_id: str | None = None) -> dict:
                """Choose a workspace provided by a specific computer/manager instance."""
                meta = _session_meta(session_name, kind, resume_id, companion_route, wake)
                return await self.select(agent_id, workspace, ctx, meta, previous=workspace_id)

        @server.tool(title="List internal MCP services", annotations=ROUTING_READ_ONLY)
        async def mcplist(agent_id: str | None = None, workspace_id: str | None = None) -> dict:
            """List internal MCPs without expanding all their tools into the public tool list."""
            await self._touch(workspace_id)
            return {"mcps": english_metadata(self.hub.inventory(self.agent_filter(agent_id)))}

        @server.tool(title="Inspect an MCP service", annotations=ROUTING_READ_ONLY)
        async def mcpdetail(mcp_id: str, workspace_id: str | None = None) -> dict:
            """Return an instance/service descriptor and its full tool schemas."""
            await self._touch(workspace_id)
            _, _, descriptor = self.hub.mcp(mcp_id)
            return {"mcp_id": mcp_id, **english_metadata(descriptor)}

        @server.tool(title="Search internal tools", annotations=ROUTING_READ_ONLY)
        async def toolsearch(query: str, agent_id: str | None = None, limit: int = 20, offset: int = 0,
                             workspace_id: str | None = None) -> dict:
            """Rank internal tools by partial natural-language matches. Tool-name terms weigh most,
            descriptions next, and MCP names add context. Results include schemas, scores and matched terms."""
            await self._touch(workspace_id)
            if not 1 <= limit <= 100 or offset < 0:
                raise RoutingError("limit must be 1-100 and offset nonnegative")
            query_terms = _search_terms(query)
            if not query_terms:
                raise RoutingError("query must contain searchable words")
            matches = []
            waiting = []
            for mcp in self.hub.inventory(self.agent_filter(agent_id)):
                _, _, descriptor = self.hub.mcp(mcp["mcp_id"])
                if mcp.get("kind") == "connector" and mcp.get("auth_state") == "needs_login":
                    # design §7.5: say where to log in instead of silently finding nothing.
                    waiting.append({"mcp_id": mcp["mcp_id"], "kind": "connector", "auth_state": "needs_login",
                                    "score": 0, "message": mcp.get("error") or "needs login in the gateway console"})
                    continue
                extra = {"kind": "connector", "auth_state": mcp.get("auth_state")} if mcp.get("kind") == "connector" else {}
                for tool in english_metadata(descriptor).get("tools", []):
                    if device_contract.is_internal(tool):
                        continue  # internal tools are never listed (design §5.2 rule 4)
                    score, matched_terms = score_tool_search(query, mcp["mcp_id"], tool)
                    if score:
                        matches.append({"mcp_id": mcp["mcp_id"], **tool, **extra,
                                        "score": score, "matched_terms": matched_terms})
            matches.sort(key=lambda item: (-item["score"], item["mcp_id"], item["name"]))
            result = {"tools": matches[offset:offset + limit], "total": len(matches),
                      "query_terms": query_terms,
                      "next_offset": offset + limit if offset + limit < len(matches) else None}
            if waiting:
                result["connectors_needing_login"] = waiting
            return result

        @server.tool(title="Run an internal MCP tool")
        async def tooluse(mcp_id: str, tool: str, arguments: dict[str, Any], workspace_id: str | None = None) -> ToolResult:
            """Execute an internal MCP tool with the arguments given by mcpdetail/toolsearch."""
            return await self.call(mcp_id, tool, arguments, workspace_id)

        if self.mailbox_config["enabled"]:
            from . import mail_tools
            from .mail_notice import MailNoticeMiddleware
            mail_tools.register(server, self)
            # Added last: the notice goes on the final result (design §8.6).
            server.add_middleware(MailNoticeMiddleware(self))
        return server


class WorkspaceProvider(Provider):
    """Exposes the selected device's tools directly (``DeviceProvider`` in the design)."""

    def __init__(self, gateway: Gateway):
        super().__init__()
        self.gateway = gateway

    @staticmethod
    def exposed(peer: Peer, descriptor: dict) -> bool:
        """Whether a device tool is exposed directly: not reserved, not internal, of a declared capability."""
        if descriptor["name"] in RESERVED_TOOLS or device_contract.is_internal(descriptor):
            return False
        declared = Gateway.declared_capabilities(peer)
        capability = device_contract.tool_meta(descriptor).get(device_contract.META_CAPABILITY)
        return declared is None or not isinstance(capability, str) or capability in declared

    async def _list_tools(self):
        selection = await self.gateway.selection()
        try:
            peer, workspace, _ = self.gateway.hub.workspace(selection)
        except RoutingError:
            # Publish only workspace tool names even before a computer is
            # chosen. Execution still requires an unambiguous selection.
            tools = {}
            for candidate in self.gateway.hub.peers.values():
                for entry in candidate.catalog["workspaces"].values():
                    for descriptor in candidate.catalog["mcps"][entry["mcp"]].get("tools", []):
                        if self.exposed(candidate, descriptor):
                            tools.setdefault(descriptor["name"], self.make_tool(descriptor))
            return list(tools.values())
        return [self.make_tool(tool) for tool in peer.catalog["mcps"][workspace["mcp"]].get("tools", [])
                if self.exposed(peer, tool) and "workspace_id" not in tool["inputSchema"].get("properties", {})]

    async def _get_tool(self, name, version=None):
        # Schema resolution must work before a handle argument is available.
        # Search the selected workspace first, then all workspace providers.
        for tool in await self._list_tools():
            if tool.name == name:
                return tool
        for peer in self.gateway.hub.peers.values():
            for workspace in peer.catalog["workspaces"].values():
                for tool in peer.catalog["mcps"][workspace["mcp"]].get("tools", []):
                    if tool["name"] == name and self.exposed(peer, tool):
                        return self.make_tool(tool)
        return None

    def make_tool(self, descriptor: dict) -> SchemaTool:
        descriptor = english_metadata(descriptor)
        schema = copy.deepcopy(descriptor["inputSchema"])
        schema.setdefault("properties", {})["workspace_id"] = {
            "type": "string", "description": "Workspace context returned by set_workspace; explicit computer routing."}

        async def run(arguments: dict) -> ToolResult:
            arguments = dict(arguments)
            try:
                handle = arguments.pop("workspace_id", None)
                selection = await self.gateway.selection(handle)
                peer, workspace, workspace_name = self.gateway.hub.workspace(selection)
                # Default selection also gets a concrete workdir on the agent.
                if selection is None:
                    selection = {"agent_id": peer.agent_id, "workspace": workspace_name}
                return await self.gateway.call(f'{peer.agent_id}/{workspace["mcp"]}', descriptor["name"],
                                               arguments, handle)
            except RoutingError as exc:
                raise ToolFailure({"error": "RoutingError", "message": str(exc)}) from None

        # Keep the upstream label; without it clients show the name title-cased.
        title = descriptor.get("title") or (descriptor.get("annotations") or {}).get("title")
        # The device's _meta (anx/capability, anx/contract) is passed through.
        meta = device_contract.tool_meta(descriptor) or None
        return SchemaTool(name=descriptor["name"], title=title, description=descriptor.get("description", ""),
                          parameters=schema, output_schema=descriptor.get("outputSchema"),
                          annotations=descriptor.get("annotations"), meta=meta, handler=run)


DeviceProvider = WorkspaceProvider

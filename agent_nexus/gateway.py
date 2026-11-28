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
from mcp_types import CallToolResult, TextContent, ToolAnnotations
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
SESSION_KINDS = ("claude", "codex", "gpt", "dsh", "gemini", "human", "other")
WAKE_MODES = ("auto", "never")
MAX_HANDLES = 10000
#: The gateway setting holding the agent chat's synced API provider config (docs/agent-chat.md §16.4).
API_CONFIG_SETTING = "agents.api.providers"
_SESSION_NAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,63}")

#: ``gateway.tool_listing``: ``compact`` lists the everyday tools and keeps the
#: rarely used ones (desktop control, watchers, asks, ...) behind toolsearch /
#: tooluse, still callable by name; ``full`` lists every tool as before 0.8.35.
TOOL_LISTING_MODES = ("compact", "full")
#: Device tool groups found through toolsearch instead of tools/list (compact).
DEFERRED_CAPABILITIES = frozenset({"uia", "screen", "input", "companion", "watch", "ask", "notify",
                                   "agents", "info"})
DEFERRED_TOOLS = frozenset({"sleep", "curr_time", "update_plan"})
#: A device may also mark a tool itself: ``_meta["anx/listing"] = "search"``.
META_LISTING = "anx/listing"
#: Gateway-native tools that are rarely needed: unlisted (compact) but callable,
#: by name or through tooluse (they appear in toolsearch as MCP "nexus").
HIDDEN_GATEWAY_TOOLS = frozenset({"mail_bye", "mail_recall", "mail_status", "file_copy_cancel",
                                  "file_copy_list", "mcpdetail"})
NEXUS_MCP = "nexus"
#: ``gateway.tool_wait_max_s``: the longest one tool call blocks before it returns
#: a resumable handle. MCP clients (Claude Code: 60 s) cut a call themselves.
DEFAULT_TOOL_WAIT_MAX_S = 50.0
#: Device exec tools whose results reach the model as plain text with a header.
EXEC_TEXT_TOOLS = frozenset({"exec_command", "write_stdin", "exec_wait", "exec_kill"})
#: Blocking arguments the gateway caps for device tools (old devices included):
#: tool -> (argument, unit factor to seconds).
WAIT_ARGUMENTS = {"exec_command": (("yield_time_ms", 1000),), "write_stdin": (("yield_time_ms", 1000),),
                  "exec_wait": (("timeout_ms", 1000),), "sleep": (("duration_ms", 1000),),
                  "request_user_input": (("timeout_s", 1),), "request_user_input_result": (("wait_s", 1),)}
PROGRESS_INTERVAL_S = 10.0
WORKSPACE_ID_SCHEMA = {"type": "string", "description": "Device handle from use_device; default: current device."}
EXEC_WAIT_DESCRIPTION = ("Wait for a command exec_command left running: returns its new output, and exit_code "
                         "once it exits. timeout_ms default 30000, max 50000; still running after it: call "
                         "exec_wait again with the same session_id.")

INSTRUCTIONS = (
    "Agent Nexus runs tools on the owner's devices. device_list lists them (with their shells, default "
    "first); use_device(device_id, session_name, kind) returns a workspace_id: pass it on every call meant "
    "for that device, mail_* and ask_user included. Without workspace_id a call goes to this device "
    "(local MCP) or the default device (several online and no default: refused). "
    "exec_command runs a shell command; when it returns a session_id, call exec_wait. "
    "Desktop control, watchers, plugins and connectors: toolsearch(query), then tooluse(tool, arguments). "
    "mail_* exchange mail with other sessions; ask_user asks the owner. Nothing is retried automatically."
)


class HiddenTools(Middleware):
    """Drop ``names`` from tools/list only; calling them still works."""

    def __init__(self, names):
        super().__init__()
        self.names = frozenset(names)

    async def on_list_tools(self, context, call_next):
        tools = await call_next(context)
        return [tool for tool in tools if tool.name not in self.names]


class HideLegacyTools(HiddenTools):
    """``legacy_tools = "hidden"``: drop the old names from tools/list only; calls still work."""

    def __init__(self):
        super().__init__(LEGACY_TOOL_NAMES)


GENERIC_OUTPUT_SCHEMA = {"additionalProperties": True, "type": "object"}


class NexusServer(FastMCP):
    """FastMCP with a leaner tools/list (compact): no empty ``_meta.fastmcp.tags`` on every tool,
    ``X | null`` parameters shown as ``X``, and no generic "any object" output schema.

    Overrides FastMCP's private list handler (pinned by uv.lock; a test checks
    the result still has every tool)."""

    lean_meta = False

    async def _on_list_tools(self, ctx, params):
        result = await super()._on_list_tools(ctx, params)
        if self.lean_meta:
            for tool in result.tools:
                meta = getattr(tool, "meta", None)
                if isinstance(meta, dict) and meta.get("fastmcp") in ({"tags": []}, {}):
                    rest = {key: value for key, value in meta.items() if key != "fastmcp"}
                    tool.meta = rest or None
                # Presentation only: FastMCP validates the call against the function itself,
                # which accepts an omitted optional argument exactly like an explicit null.
                tool.input_schema = compact_schema(tool.input_schema)
                if tool.output_schema == GENERIC_OUTPUT_SCHEMA:
                    tool.output_schema = None
        return result


def compact_schema(schema: Any) -> Any:
    """A smaller presentation of an input schema: ``X | null`` (default null) becomes ``X``.

    Used where the gateway presents device and plugin tools; the call itself is
    still validated against the original schema (nulls are dropped first)."""
    if isinstance(schema, list):
        return [compact_schema(item) for item in schema]
    if not isinstance(schema, dict):
        return schema
    result = {}
    for key, value in schema.items():
        if key == "properties" and isinstance(value, dict):
            result[key] = {name: compact_schema(prop) for name, prop in value.items()}
        elif key in ("items", "additionalProperties", "$defs", "anyOf", "oneOf", "allOf"):
            result[key] = compact_schema(value) if not isinstance(value, dict) or key != "$defs" else \
                {name: compact_schema(item) for name, item in value.items()}
        else:
            result[key] = value
    options = result.get("anyOf")
    if isinstance(options, list) and len(options) == 2 and {"type": "null"} in options:
        other = next(item for item in options if item != {"type": "null"})
        if isinstance(other, dict):
            merged = {k: v for k, v in result.items() if k != "anyOf"}
            if merged.get("default", 0) is None:
                merged.pop("default")
            merged = {**other, **merged}
            result = merged
    if result.get("title") and set(result) - {"title"} and "properties" not in result:
        result.pop("title")  # pydantic's per-property title repeats the name
    return result


_DEFAULT_HERE = re.compile(r"\(default here: [^)]*\)")
EXEC_SHELL_DEFAULT = "(default: the device's own, first in its device_list / use_device shells)"


def neutral_exec_description(text: str) -> str:
    """exec_command's description without the one device's "(default here: ...)" clause."""
    return _DEFAULT_HERE.sub(EXEC_SHELL_DEFAULT, text, count=1)


def deferred(descriptor: dict) -> bool:
    """Whether a device tool is found through toolsearch instead of tools/list (compact)."""
    meta = device_contract.tool_meta(descriptor)
    if meta.get(META_LISTING) == "search":
        return True
    name = descriptor.get("name")
    if name in DEFERRED_TOOLS:
        return True
    capability = device_contract.tool_capability(descriptor) or device_contract.standard_capability(name)
    return capability in DEFERRED_CAPABILITIES


def exec_wait_descriptor() -> dict:
    """exec_wait for a device that predates it (0.8.34): the gateway maps it to write_stdin(chars='')."""
    return {"name": "exec_wait", "title": "Wait for a running command", "description": EXEC_WAIT_DESCRIPTION,
            "inputSchema": {"type": "object", "required": ["session_id"], "properties": {
                "session_id": {"type": "integer"}, "timeout_ms": {"type": "number"},
                "max_output_tokens": {"type": "number"}}},
            "annotations": {"readOnlyHint": True, "destructiveHint": False, "openWorldHint": False},
            "_meta": {device_contract.META_CAPABILITY: "exec"}}


def device_tools(peer: Peer, mcp_name: str, *, is_device: bool) -> list[dict]:
    """A device MCP's tool descriptors, plus exec_wait for a device that only has write_stdin."""
    tools = peer.catalog["mcps"].get(mcp_name, {}).get("tools", [])
    if is_device:
        names = {tool.get("name") for tool in tools}
        if "write_stdin" in names and "exec_wait" not in names:
            source = next(tool for tool in tools if tool.get("name") == "write_stdin")
            synthetic = exec_wait_descriptor()
            capability = device_contract.tool_capability(source)
            if capability:
                synthetic["_meta"] = {device_contract.META_CAPABILITY: capability}
            tools = [*tools, synthetic]
    return tools


def _fmt_seconds(value: Any) -> str:
    try:
        return f"{float(value):.1f}s"
    except (TypeError, ValueError):
        return ""


def exec_text(data: dict, device: str) -> tuple[str, bool]:
    """A device exec result as one header line plus the raw output; (text, is_error)."""
    shell = data.get("shell") if isinstance(data.get("shell"), str) else None
    output = data.get("output") if isinstance(data.get("output"), str) else ""
    if data.get("status") == "error":
        return f"[error · {device}] {data.get('error') or 'failed'}", True
    session = data.get("session_id")
    code = data.get("exit_code")
    wall = _fmt_seconds(data.get("wall_time_seconds"))
    if code is None and data.get("error"):
        return f"[error · {device}{' · ' + shell if shell else ''}] {output or data['error']}", True
    if code is None and session is not None:
        parts = ["running", f"session {session}", wall, device, shell]
        head = "[" + " · ".join(part for part in parts if part) + f"] exec_wait(session_id={session}) to continue"
    else:
        parts = [f"exit {code}", wall, device, shell]
        head = "[" + " · ".join(part for part in parts if part) + "]"
    if "[output truncated" in output and data.get("original_token_count"):
        head += f" (truncated from ~{data['original_token_count']} tokens)"
    if isinstance(data.get("note"), str) and data["note"]:
        head += f" ({data['note']})"
    return head + "\n" + (output if output else "(no output)"), False


def _client_id() -> str | None:
    with contextlib.suppress(Exception):
        token = get_access_token()
        if token is not None and isinstance(getattr(token, "client_id", None), str):
            return token.client_id[:200]
    return None


def caller_device() -> str | None:
    """The device id when the MCP caller is a joined device on ``/mcp/device`` (``client_id`` ``device:<id>``),
    else None (the owner). Attribution only: a locked device never gets this far -- ``DeviceMcpAuth`` refuses
    every request of a locked device with 423 before any routing, so no per-target lock check is needed here."""
    client_id = _client_id()
    return client_id[7:] if client_id and client_id.startswith("device:") else None


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


@contextlib.asynccontextmanager
async def progress_heartbeat(label: str, interval: float | None = None):
    """MCP progress notifications every few seconds while a call waits, when the client sent a
    progressToken (some clients then keep the call open longer). Never relied upon: without a
    token nothing is sent, and a failed notification (client gone) is ignored."""
    context = None
    with contextlib.suppress(Exception):
        candidate = get_context()
        request_context = candidate.request_context
        meta = request_context._srctx.meta if request_context is not None else None
        if meta is not None and meta.get("progress_token") is not None:
            context = candidate
    if context is None:
        yield
        return
    started = time.monotonic()
    every = PROGRESS_INTERVAL_S if interval is None else interval

    async def beat() -> None:
        while True:
            await asyncio.sleep(every)
            elapsed = round(time.monotonic() - started)
            with contextlib.suppress(Exception):
                await context.report_progress(progress=elapsed, total=None, message=f"{label}: {elapsed}s")

    task = asyncio.create_task(beat())
    try:
        yield
    finally:
        task.cancel()


class Gateway:
    def __init__(self, hub: Hub, store: GatewayStore | None = None, *, legacy_tools: str = "listed",
                 mailbox: dict | None = None, tool_listing: str = "full",
                 tool_wait_max_s: float = DEFAULT_TOOL_WAIT_MAX_S):
        if legacy_tools not in LEGACY_TOOLS_MODES:
            raise ValueError(f"gateway.legacy_tools must be one of {', '.join(LEGACY_TOOLS_MODES)}")
        if tool_listing not in TOOL_LISTING_MODES:
            raise ValueError(f"gateway.tool_listing must be one of {', '.join(TOOL_LISTING_MODES)}")
        self.hub = hub
        self.tool_listing = tool_listing
        #: Never more than the device call budget allows (rpc_timeout_s - 10).
        self.tool_wait_max_s = max(1.0, min(float(tool_wait_max_s), hub.timeout - 10))
        # Handles are application-level workspace contexts, also usable by
        # clients using the sessionless MCP transport. They are persisted
        # (design §8.1) so a gateway restart does not invalidate them.
        self.store = store if store is not None else GatewayStore(None)
        self.devices = Devices(hub, self.store)
        # Hub's routing errors name devices the way the owner sees them.
        hub.display = self.devices.display_name
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
        # file_copy (docs/design.md §18): who moves the bytes, and the jobs the gateway pumps itself.
        from .file_copy_gateway import FileCopyService
        self.file_copy = FileCopyService(hub, self.devices)
        # file_copy_status(wait_ms): the same per-call wait limit (the last device ask adds a 5 s grace).
        self.file_copy.wait_max_ms = int(min(50.0, self.tool_wait_max_s) * 1000)

    async def selection(self, workspace_id: str | None = None) -> dict | None:
        """Where a call without mcp_id goes: its workspace_id, else the session's use_device choice,
        else (a call through a device's 本机 MCP) that device itself."""
        if workspace_id:
            record = self.store.touch_handle(workspace_id) if isinstance(workspace_id, str) else None
            if record is None:
                raise RoutingError("workspace_id is unknown or expired; call use_device (or set_workspace) again")
            return {"agent_id": record["agent_id"], "workspace": record["workspace"], "scope": workspace_id}
        selected = None
        with contextlib.suppress(RuntimeError):
            selected = await get_context().get_state("mcpm_workspace")
        return selected if selected is not None else self.local_selection()

    def local_selection(self) -> dict | None:
        """The calling device's own default workdir when the caller is its 本机 MCP (``device:<id>``).

        Routing only: the mailbox identity still needs use_device (caller_address ignores this)."""
        via = caller_device()
        peer = self.hub.peers.get(via) if via else None
        if peer is None:
            return None
        workspaces = peer.catalog.get("workspaces") or {}
        name = "default" if "default" in workspaces else next(iter(workspaces), None)
        if name is None:
            return None
        return {"agent_id": via, "workspace": name, "scope": f"device:{via}"}

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
        return max(0.0, min(requested, self.tool_wait_max_s, self.hub.timeout - 10))

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
        async with progress_heartbeat("mail_wait"):
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
                async with progress_heartbeat("ask_user"):
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
            async with progress_heartbeat(f"{mcp_id}/{tool}"):
                return await self.hub.connectors.call_tool(mcp_id[4:], tool, arguments)
        peer, name, mcp = self.hub.mcp(mcp_id)
        is_device = self.is_device_mcp(peer, name)
        descriptor = next((entry for entry in mcp.get("tools", []) if entry["name"] == tool), None)
        upstream_tool, arguments = tool, dict(arguments or {})
        if descriptor is None and is_device and tool == "exec_wait":
            # A device older than exec_wait (0.8.34): the same wait through write_stdin(chars='').
            fallback = next((entry for entry in mcp.get("tools", []) if entry["name"] == "write_stdin"), None)
            if fallback is not None:
                problems = list(Draft202012Validator(exec_wait_descriptor()["inputSchema"]).iter_errors(arguments))
                if problems:
                    raise RoutingError("invalid tool arguments: " + "; ".join(e.message for e in problems[:3]))
                timeout_ms = arguments.get("timeout_ms", 30000)
                arguments = {"session_id": arguments["session_id"], "chars": "", "yield_time_ms": timeout_ms,
                             **({"max_output_tokens": arguments["max_output_tokens"]}
                                if arguments.get("max_output_tokens") is not None else {})}
                descriptor, upstream_tool = fallback, "write_stdin"
        if descriptor is None:
            missing = self.missing_capability(peer, tool) if is_device else None
            raise missing or RoutingError(f"unknown tool {mcp_id}/{tool}")
        problems = list(Draft202012Validator(descriptor["inputSchema"]).iter_errors(arguments))
        if problems:
            raise RoutingError("invalid tool arguments: " + "; ".join(error.message for error in problems[:3]))
        if is_device and tool == "exec_command":
            self.check_old_device_shell(peer, descriptor, arguments)
        clamped = self.clamp_waits(upstream_tool, arguments) if is_device else {}
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
            request = request_context.request if request_context else None
            # Modern HTTP has no session identity, and a stateless transport makes up a
            # new session id per request: those use one stable pool (else every call
            # would open a new upstream plugin session). Explicit handles isolate workflows.
            headers = getattr(request, "headers", None)
            if request is not None and (request_context.protocol_version >= "2026-07-28"
                                        or not (headers is not None and headers.get("mcp-session-id"))):
                scope = "default"
            else:
                scope = context.session_id
        except RuntimeError:
            scope = "default"
        if selection:
            scope = selection.get("scope", scope)
        try:
            async with progress_heartbeat(f"{tool} ({self.devices.display_name(peer.agent_id)})"):
                result = await peer.call("tool_call", {"mcp": name, "tool": upstream_tool, "arguments": arguments,
                                                       "workspace": workspace_name, "scope": scope})
        except asyncio.TimeoutError:
            raise RoutingError("local MCP call timed out; execution outcome may be unknown") from None
        result = ToolResult.from_mcp_result(CallToolResult.model_validate(result))
        return self.present(peer, tool, result, clamped=clamped) if is_device else result

    def check_old_device_shell(self, peer: Peer, descriptor: dict, arguments: dict) -> None:
        """A 0.8.34 Windows device without Git Bash passes shell='bash' straight to CreateProcess,
        which finds System32\\bash.exe (WSL, another filesystem) or fails unclearly: refuse it here."""
        shell = arguments.get("shell")
        if not isinstance(shell, str) or shell.strip().lower().removesuffix(".exe") not in ("bash", "sh", "zsh"):
            return
        if self.devices.shells(peer.agent_id):
            return  # 0.8.35+: the device checks the shell itself
        if "Git Bash is not installed on this machine" in (descriptor.get("description") or ""):
            raise RoutingError(f"shell {shell!r} is not available on {self.devices.display_name(peer.agent_id)}; "
                               "available: powershell, cmd (default: powershell)")

    def clamp_waits(self, tool: str, arguments: dict) -> dict:
        """Cap a device tool's blocking arguments at ``tool_wait_max_s`` (in place); returns what was cut.

        Old devices allowed waits longer than any MCP client keeps a call open
        (exec 150 s, request_user_input 240 s by default): they return a handle instead."""
        clamped = {}
        for argument, factor in WAIT_ARGUMENTS.get(tool, ()):
            cap = self.tool_wait_max_s * factor
            value = arguments.get(argument)
            if value is None:
                if tool == "request_user_input":
                    arguments[argument] = int(cap) if float(cap).is_integer() else cap
                continue
            if isinstance(value, (int, float)) and not isinstance(value, bool) and value > cap:
                arguments[argument] = int(cap) if isinstance(value, int) else float(cap)
                clamped[argument] = (value, arguments[argument])
        return clamped

    def present(self, peer: Peer, tool: str, result: ToolResult, *, clamped: dict | None = None) -> ToolResult:
        """How a device tool's result reaches the model.

        exec tools: one header line (exit code or running session, time, device, shell) and the
        raw output as text, not JSON with escaped newlines; the fields go to ``_meta["anx/exec"]``.
        Other tools get a short device tag when several devices are online."""
        device = self.devices.display_name(peer.agent_id)
        data = result.structured_content
        if tool in EXEC_TEXT_TOOLS and isinstance(data, dict) and ("output" in data or data.get("status") == "error") \
                and not result.is_error:
            text, failed = exec_text(data, device)
            meta = {**(result.meta or {}),
                    "anx/exec": {key: value for key, value in data.items() if key != "output"}}
            return ToolResult(content=[TextContent(type="text", text=text)], meta=meta, is_error=failed)
        notes = []
        if clamped and tool == "sleep":
            requested, used = clamped["duration_ms"]
            notes.append(f"[waited {used} ms of {requested}: one call waits at most {used} ms; "
                         f"call sleep(duration_ms={requested - used}) for the rest]")
        if len(self.hub.peers) > 1:
            notes.append(f"[device: {device}]")
        if notes:
            with contextlib.suppress(AttributeError):
                result._raw_mcp_result = None  # the edited content must reach the client
            result.content = [*(result.content or []), *(TextContent(type="text", text=note) for note in notes)]
        return result

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
        via = caller_device()
        if via:  # selected through a device's 本机 MCP: who asked, for the gateway's own records
            meta = {**(meta or {}), "via": f"device:{via}"}
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

    def agent_handle(self, args: Any, *, caller: str | None = None) -> dict:
        """A workspace handle and mailbox address for a console agent run (docs/agent-chat.md §1, §9, §10).

        ``args``: ``{device_id, workdir, kind, name?}``. The handle selects the device's registered workdir
        that holds ``workdir`` (the closest one; else its default), and the address is bound with ``workdir``
        itself, ``kind`` and ``name`` (a free ``session_name``; without one an automatic name such as
        ``codex-proj``). ``caller``: the device asking for it (None = the owner's console). Console runs are
        never woken by mail (``wake: never``): the chat shows their mail itself."""
        if not isinstance(args, dict):
            raise RoutingError("agent_handle needs an object")
        device = args.get("device_id") or caller
        if not isinstance(device, str) or not device:
            raise RoutingError("agent_handle needs device_id")
        agent_id = self.devices.resolve(device)
        if caller is not None and agent_id != caller:
            requester = self.hub.peers.get(caller)
            if requester is not None and getattr(requester, "locked", False):
                raise RoutingError("locked: 这台设备已锁定，解锁后才能操作其他设备")
        peer = self.hub.peer(agent_id)  # RoutingError when offline
        workspaces = peer.catalog.get("workspaces") or {}
        if not workspaces:
            raise RoutingError(f"{self.devices.label(agent_id)} 没有登记任何工作目录，无法签发工作区")
        workdir = args.get("workdir") if isinstance(args.get("workdir"), str) else ""
        name, match = self._closest_workspace(workspaces, workdir)
        kind = args.get("kind") if args.get("kind") in SESSION_KINDS else "other"
        # ``console_run``: made here, so agent_release may end it; ``requested_by``: who may (besides the owner).
        meta: dict[str, Any] = {"kind": kind, "wake": "never", "console_run": True}
        if caller is not None:
            meta["requested_by"] = caller
        session_name = args.get("name")
        if isinstance(session_name, str) and _SESSION_NAME.fullmatch(session_name):
            meta["session_name"] = session_name
        if self.store.live_handle_count() >= MAX_HANDLES:
            raise RoutingError("workspace context limit reached")
        handle = uuid4().hex
        self.store.put_handle(handle, agent_id, name, meta=meta)
        try:
            bound = self.mailbox.bind(handle, agent_id, workdir=workdir or workspaces[name].get("path"), meta=meta,
                                      client_id=None, kind_hint=kind, exclusive=True)
        except RoutingError:
            self.store.drop_handle(handle)
            raise
        return {"workspace_id": handle, "agent_id": agent_id, "device_id": agent_id, "workspace": name,
                "path": workdir or workspaces[name].get("path"), "workspace_path": workspaces[name].get("path"),
                "match": match, "display_name": self.devices.display_name(agent_id),
                "device_display": self.devices.label(agent_id), "address": bound["address"],
                "address_display": bound["address_display"], "name_source": bound["name_source"],
                "mail": {"unread": bound["unread"]}}

    def agent_release(self, args: Any, *, caller: str | None = None) -> dict:
        """A console run closed (or never started): end the handle :meth:`agent_handle` gave it, so its
        automatic address is free for the next chat in that folder and no longer listed as live.

        ``args``: ``{workspace_id}``. Only handles made by :meth:`agent_handle`, and only by the device that
        asked for it, the device it points at, or the owner (``caller`` None). Unknown or expired handles are
        not an error (the run may outlive the handle)."""
        if not isinstance(args, dict) or not isinstance(args.get("workspace_id"), str):
            raise RoutingError("agent_release needs workspace_id")
        record = self.store.handle_record(args["workspace_id"])
        if record is None:
            return {"released": False}
        meta = record.get("meta") or {}
        if not meta.get("console_run"):
            raise RoutingError("agent_release: not a console run's handle")
        if caller is not None and caller not in (record.get("agent_id"), meta.get("requested_by")):
            raise RoutingError("agent_release: not this device's handle")
        address = self.store.expire_handle(args["workspace_id"])
        ended = False
        if address and not self.mailbox.has_live_handle(address):
            self.mailbox.bye(address)
            ended = True
        return {"released": True, "address": address, "ended": ended}

    def _console_run_address(self, args: Any, caller: str | None, op: str) -> tuple[str, str]:
        """``(handle, address)`` of a live console run's handle (:meth:`agent_handle`), checked like
        :meth:`agent_release`: only the device that asked for it, the device it points at, or the owner."""
        handle = args.get("workspace_id") if isinstance(args, dict) else None
        if not isinstance(handle, str) or not handle:
            raise RoutingError(f"{op} needs workspace_id")
        record = self.store.handle_record(handle)
        meta = (record or {}).get("meta") or {}
        if record is None or not meta.get("console_run") or self.store.touch_handle(handle) is None:
            raise RoutingError(f"{op}: not a live console run's handle")
        if caller is not None and caller not in (record.get("agent_id"), meta.get("requested_by")):
            raise RoutingError(f"{op}: not this device's handle")
        address = self._address_of(handle)
        if not address:
            raise RoutingError(f"{op}: this run has no mailbox address")
        return handle, address

    def agent_ask(self, args: Any, *, caller: str | None = None) -> dict:
        """``ask_user`` of an API model run (docs/agent-chat.md §16.4): a question from the run's address
        (``{workspace_id, question, options?, recommended?, context?}``); the device polls ``ask_get``."""
        _handle, address = self._console_run_address(args, caller, "agent_ask")
        options = args.get("options") if isinstance(args.get("options"), list) and args.get("options") else None
        recommended = args.get("recommended") if isinstance(args.get("recommended"), str) and options and \
            args.get("recommended") in options else None
        created = self.mailbox.ask_create(address, args.get("question"), options=options, recommended=recommended,
                                          context=str(args.get("context") or "")[:4000], blocking=True)
        self.ask_created(created["ask_id"])
        return created

    def agent_mail_send(self, args: Any, *, caller: str | None = None) -> dict:
        """``mail_send`` of an API model run: mail from the run's address (``{workspace_id, to, body, subject?,
        kind?}``)."""
        _handle, address = self._console_run_address(args, caller, "agent_mail_send")
        kind = args.get("kind") if args.get("kind") in ("message", "task", "result") else "message"
        result = self.mailbox.send(address, args.get("to"), args.get("body"),
                                   subject=str(args.get("subject") or "")[:200], kind=kind)
        self.mail_sent(result)
        return result

    def api_config_get(self) -> dict:
        """The synced API provider config (docs/agent-chat.md §16.4): ``{providers, updated_at}``."""
        value, updated = self.store.setting(API_CONFIG_SETTING)
        providers = value.get("providers") if isinstance(value, dict) else None
        return {"providers": providers if isinstance(providers, list) else [], "updated_at": updated}

    def api_config_put(self, args: Any) -> dict:
        """Replace the synced API provider config: ``{providers: [...]}``, non-secret fields only. Any key-like
        field (key, api_key, token, secret, authorization...) refuses the whole request: keys stay on devices."""
        from .agents.api import config as api_config
        if not isinstance(args, dict) or not isinstance(args.get("providers"), list):
            raise RoutingError("api_config_put needs {providers: [...]}")
        secret = api_config.find_secret(args)
        if secret:
            raise RoutingError(f"网关不保存密钥：请求里有密钥字段 {secret}，密钥只能在设备上设置")
        try:
            providers = api_config.normalize_list(args["providers"], strict=True)
        except api_config.ConfigError as exc:
            raise RoutingError(str(exc)) from exc
        updated = self.store.set_setting(API_CONFIG_SETTING, {"providers": providers})
        return {"ok": True, "providers": providers, "updated_at": updated}

    @staticmethod
    def _closest_workspace(workspaces: dict, workdir: str) -> tuple[str, str]:
        """(registered workdir name, ``exact`` / ``inside`` / ``default``) for a folder on the device."""
        def norm(path: str) -> str:
            return path.replace("\\", "/").rstrip("/").casefold() if path else ""

        target = norm(workdir)
        best, best_len = None, -1
        for key, value in workspaces.items():
            base = norm((value or {}).get("path") or "") if isinstance(value, dict) else ""
            if not base or not target:
                continue
            if target == base:
                return key, "exact"
            if target.startswith(base + "/") and len(base) > best_len:
                best, best_len = key, len(base)
        if best is not None:
            return best, "inside"
        return ("default" if "default" in workspaces else next(iter(workspaces))), "default"

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
        if op == "links_report":
            # How this device reaches the others right now; the console's 拓扑 draws all of them.
            return self.devices.links_report(peer, args)
        if op == "device_admin":
            action = args.get("op")
            if action not in ("list", "topology") and getattr(peer, "locked", False):
                raise RoutingError("locked: 这台设备已锁定，解锁后才能操作其他设备")
            if action == "list":
                if self.enrollment is not None:
                    return {"devices": self.devices.console_list(), "gateway": {"geo": self.devices.gateway_geo()}}
                return {"devices": self.devices.list(include_offline=bool(args.get("include_offline", True)))}
            if action == "topology":
                return self.devices.topology()
            if action == "set_alias":
                return {"device": self.devices.set_alias(args.get("device_id"), args.get("alias"))}
            if action == "set_location":
                return {"device": self.devices.set_location(args.get("device_id"), args.get("location"))}
            if action == "delete":
                return {"deleted": self.devices.delete(args.get("device_id"))}
            if action in ("revoke", "invite_create", "invite_list", "invite_revoke") and self.enrollment is not None:
                return await self.device_admin_enrollment(action, args)
            raise RoutingError(f"unsupported device_admin op {action!r}")
        if op == "connector_registry" and self.hub.connectors is not None:
            # The local console's connectors page writes the same registry (design §7.1).
            return await self.hub.connectors.registry_op(args)
        if op == "agent_handle":
            # A console agent run on this device (docs/agent-chat.md §9): its handle and mailbox address.
            return self.agent_handle(args, caller=peer.agent_id)
        if op == "agent_release":
            # That run closed: its handle ends and its automatic address is free again.
            return self.agent_release(args, caller=peer.agent_id)
        if op == "api_config_get":
            # The agent chat's API providers (docs/agent-chat.md §16.4): the non-secret config devices merge in.
            return self.api_config_get()
        if op == "api_config_put":
            if getattr(peer, "locked", False):
                raise RoutingError("locked: 这台设备已锁定，解锁后才能修改网关上的配置")
            result = self.api_config_put(args)
            log.info("api_config_put by device %s: %d providers", peer.agent_id, len(result.get("providers") or []))
            return result
        if op == "agent_ask":
            # ask_user / mail_send of an API model run on this device, as its run's mailbox address
            return self.agent_ask(args, caller=peer.agent_id)
        if op == "agent_mail_send":
            return self.agent_mail_send(args, caller=peer.agent_id)
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

    # -- toolsearch / tooluse over tools not in tools/list --------------------
    def _hidden_native(self) -> frozenset:
        return HIDDEN_GATEWAY_TOOLS if self.tool_listing == "compact" else frozenset()

    async def _hidden_native_names(self) -> frozenset:
        server = getattr(self, "_server", None)
        if server is None:
            return frozenset()
        names = set()
        for name in self._hidden_native():
            with contextlib.suppress(Exception):
                if await server.get_tool(name) is not None:
                    names.add(name)
        return frozenset(names)

    async def _hidden_native_tools(self) -> list[dict]:
        """The unlisted gateway-native tools as search entries (MCP ``nexus``)."""
        server = getattr(self, "_server", None)
        tools = []
        for name in sorted(await self._hidden_native_names()):
            tool = await server.get_tool(name)
            entry = {"name": name, "title": tool.title, "description": tool.description or "",
                     "inputSchema": compact_schema(tool.parameters)}
            tools.append({key: value for key, value in entry.items() if value is not None})
        return tools

    async def _run_native(self, name: str, arguments: dict, workspace_id: str | None) -> ToolResult:
        """tooluse of a gateway-native tool: run it directly (the tooluse call itself already
        passes the middleware, so the mail notice is not added twice)."""
        tool = await self._server.get_tool(name)
        if workspace_id and "workspace_id" in (tool.parameters.get("properties") or {}):
            arguments.setdefault("workspace_id", workspace_id)
        return await tool.run(arguments)

    async def resolve_tool(self, reference: str, workspace_id: str | None = None) -> tuple[str, str]:
        """tooluse without mcp_id: ``server.tool`` or a bare tool name -> (mcp_id, tool).

        The current device (use_device, workspace_id, or the 本机 MCP's own device) wins over
        other devices; otherwise the name must be unique, else the candidates are listed."""
        inventory = self.hub.inventory()
        cache: dict[str, set] = {}

        def names(mcp_id: str) -> set:
            if mcp_id not in cache:
                peer, mcp_name, _ = self.hub.mcp(mcp_id)
                is_device = peer is not self.hub.connectors and self.is_device_mcp(peer, mcp_name)
                cache[mcp_id] = {tool.get("name") for tool in device_tools(peer, mcp_name, is_device=is_device)}
            return cache[mcp_id]

        candidates: list[tuple[dict, str]] = []
        for index in [i for i, char in enumerate(reference) if char == "."][::-1]:
            prefix, name = reference[:index], reference[index + 1:]
            found = [(item, name) for item in inventory
                     if (item["mcp_id"] == prefix or item["name"] == prefix or MCP_ALIASES.get(prefix) == item["name"])
                     and name in names(item["mcp_id"])]
            if not found:
                # "PC-B.screenshot": a device's display name or id as the prefix (its device MCP first).
                agent = None
                with contextlib.suppress(Exception):
                    agent = self.devices.resolve(prefix)
                if agent in self.hub.peers:
                    found = [(item, name) for item in inventory
                             if item.get("agent_id") == agent and name in names(item["mcp_id"])]
                    device = [entry for entry in found if self.is_device_mcp(self.hub.peers[agent], entry[0]["name"])]
                    found = device or found
            if found:
                candidates = found
                break
        if not candidates:
            candidates = [(item, reference) for item in inventory if reference in names(item["mcp_id"])]
        if not candidates:
            raise RoutingError(f"unknown tool {reference!r}; find it with toolsearch")
        if len(candidates) > 1:
            current = await self.selection(workspace_id) if workspace_id else None
            if current is None:
                with contextlib.suppress(RoutingError):
                    current = await self.selection()
            agent = current.get("agent_id") if isinstance(current, dict) else None
            preferred = [item for item in candidates if item[0]["agent_id"] == agent]
            if len(preferred) > 1:
                peer = self.hub.peers.get(agent)
                device = [item for item in preferred if peer is not None and self.is_device_mcp(peer, item[0]["name"])]
                preferred = device or preferred
            if len(preferred) == 1:
                candidates = preferred
        if len(candidates) > 1:
            listed = ", ".join(f"{item['mcp_id']}" for item, _ in candidates[:10])
            raise RoutingError(f"tool {reference!r} is on several MCPs ({listed}): pass mcp_id, use "
                               "'server.tool', or choose a device with use_device / workspace_id")
        item, name = candidates[0]
        return item["mcp_id"], name

    def build(self) -> FastMCP:
        server = NexusServer("Agent Nexus", instructions=INSTRUCTIONS, providers=[WorkspaceProvider(self)])
        compact = self.tool_listing == "compact"
        server.lean_meta = compact
        hidden = set(LEGACY_TOOL_NAMES) if self.legacy_tools == "hidden" else set()
        if compact:
            hidden |= HIDDEN_GATEWAY_TOOLS
        if hidden:
            server.add_middleware(HiddenTools(hidden))
        legacy = self.legacy_tools != "off"
        self._server = server

        @server.tool(title="List devices", annotations=ROUTING_READ_ONLY)
        async def device_list(include_offline: bool = False, capability: str | None = None,
                              workspace_id: str | None = None) -> dict:
            """List devices: display_name (owner's alias or hostname), device_id, capabilities, shells
            (default first, also shell_default), workdirs."""
            await self._touch(workspace_id)
            return {"devices": self.devices.list(include_offline=include_offline, capability=capability)}

        @server.tool(title="Choose a device", annotations=ROUTING_READ_ONLY)
        async def use_device(device_id: str, ctx: Context, workdir: str = "default",
                             session_name: str | None = None, kind: str | None = None,
                             resume_id: str | None = None, companion_route: str | None = None,
                             wake: str = "auto", workspace_id: str | None = None) -> dict:
            """Choose a device (device_id or display name) and workdir; returns the workspace_id to pass
            on later calls for it, its shells (default first) and your mailbox address. Pass the
            workspace_id you hold to keep (or, with session_name, rename) that address."""
            meta = _session_meta(session_name, kind, resume_id, companion_route, wake)
            selected = await self.select(device_id, workdir, ctx, meta, previous=workspace_id)
            selected["device_id"] = selected["agent_id"]
            shells = self.devices.shells(selected["agent_id"])
            if shells:
                selected["shells"] = shells
                selected["shell_default"] = shells[0]
            via = caller_device()
            fallback = (f"this device ({self.devices.display_name(via)})" if via
                        else "the default device (the only online one; with several online they are refused)")
            if via == selected["agent_id"]:
                fallback = "this same device"
            selected["hint"] = (f"Pass workspace_id={selected['workspace_id']} on every call for "
                                f"{selected['device_display']} ({selected['workspace']}), mail_* and ask_user "
                                f"included; calls without it go to {fallback}"
                                + (f". Shells: {', '.join(shells)} (default {shells[0]})" if shells else "") + ".")
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
            if mcp_id == NEXUS_MCP:  # toolsearch's name for the unlisted gateway tools
                return {"mcp_id": NEXUS_MCP, "name": NEXUS_MCP, "tools": await self._hidden_native_tools()}
            _, _, descriptor = self.hub.mcp(mcp_id)
            return {"mcp_id": mcp_id, **english_metadata(descriptor)}

        @server.tool(title="Search internal tools", annotations=ROUTING_READ_ONLY)
        async def toolsearch(query: str, agent_id: str | None = None, limit: int = 8, offset: int = 0,
                             workspace_id: str | None = None) -> dict:
            """Find tools not listed directly (desktop control, watchers, plugins, connectors, rare
            gateway tools) by words; each hit has its inputSchema. Run one with tooluse(tool, arguments)."""
            if not 1 <= limit <= 100 or offset < 0:
                raise RoutingError("limit must be 1-100 and offset nonnegative")
            query_terms = _search_terms(query)
            if not query_terms:
                raise RoutingError("query must contain searchable words")
            current = None
            with contextlib.suppress(RoutingError):
                current = await self.selection(workspace_id) if workspace_id else await self.selection()
            current_agent = current.get("agent_id") if isinstance(current, dict) else None
            matches: list[dict] = []
            waiting = []
            by_device_tool: dict[str, dict] = {}
            for mcp in self.hub.inventory(self.agent_filter(agent_id)):
                peer, mcp_name, descriptor = self.hub.mcp(mcp["mcp_id"])
                if mcp.get("kind") == "connector" and mcp.get("auth_state") == "needs_login":
                    # design §7.5: say where to log in instead of silently finding nothing.
                    waiting.append({"mcp_id": mcp["mcp_id"], "kind": "connector", "auth_state": "needs_login",
                                    "score": 0, "message": mcp.get("error") or "needs login in the gateway console"})
                    continue
                extra = {"kind": "connector", "auth_state": mcp.get("auth_state")} if mcp.get("kind") == "connector" else {}
                is_device = peer is not self.hub.connectors and self.is_device_mcp(peer, mcp_name)
                for tool in english_metadata(device_tools(peer, mcp_name, is_device=is_device)):
                    if device_contract.is_internal(tool):
                        continue  # internal tools are never listed (design §5.2 rule 4)
                    score, _ = score_tool_search(query, mcp["mcp_id"], tool)
                    if not score:
                        continue
                    hit = {"name": tool["name"], "mcp_id": mcp["mcp_id"], "title": tool.get("title"),
                           "description": tool.get("description", ""),
                           "inputSchema": compact_schema(tool.get("inputSchema") or {}), **extra, "score": score}
                    if is_device:
                        # One hit per device tool; the current device's copy first, the others named.
                        known = by_device_tool.get(tool["name"])
                        if known is not None:
                            if peer.agent_id == current_agent:
                                hit["also_on"] = [*known.get("also_on", []),
                                                  {"device": self.devices.display_name(known["_agent"]),
                                                   "mcp_id": known["mcp_id"]}]
                                hit["_agent"] = peer.agent_id
                                known.clear()
                                known.update(hit)
                            else:
                                known.setdefault("also_on", []).append(
                                    {"device": self.devices.display_name(peer.agent_id), "mcp_id": mcp["mcp_id"]})
                            continue
                        hit["_agent"] = peer.agent_id
                        by_device_tool[tool["name"]] = hit
                    matches.append(hit)
            if not agent_id:
                for tool in await self._hidden_native_tools():
                    score, _ = score_tool_search(query, NEXUS_MCP, tool)
                    if score:
                        matches.append({**tool, "mcp_id": NEXUS_MCP, "score": score})
            for hit in matches:
                hit.pop("_agent", None)
                if not hit.get("title"):
                    hit.pop("title", None)
            matches.sort(key=lambda item: (-item["score"], item["mcp_id"], item["name"]))
            result = {"tools": matches[offset:offset + limit], "total": len(matches),
                      "next_offset": offset + limit if offset + limit < len(matches) else None}
            if waiting:
                result["connectors_needing_login"] = waiting
            return result

        @server.tool(title="Run an internal MCP tool")
        async def tooluse(tool: str, arguments: dict[str, Any] | None = None, mcp_id: str | None = None,
                          workspace_id: str | None = None) -> ToolResult:
            """Run a tool found by toolsearch: tool is its name, or "server.tool" (e.g. "unity.get_scene");
            mcp_id is only needed when a name is ambiguous."""
            arguments = dict(arguments or {})
            if not isinstance(tool, str) or not tool:
                raise RoutingError("tool is required")
            if mcp_id == NEXUS_MCP:  # an unlisted gateway tool, as toolsearch names it
                name = tool[len(NEXUS_MCP) + 1:] if tool.startswith(NEXUS_MCP + ".") else tool
                if name not in await self._hidden_native_names():
                    raise RoutingError(f"unknown tool {NEXUS_MCP}/{name}; find it with toolsearch")
                return await self._run_native(name, arguments, workspace_id)
            if mcp_id:
                return await self.call(mcp_id, tool, arguments, workspace_id)
            if tool in await self._hidden_native_names() or (tool.startswith(NEXUS_MCP + ".")
                                                             and tool[len(NEXUS_MCP) + 1:] in await self._hidden_native_names()):
                return await self._run_native(tool.split(".", 1)[1] if tool.startswith(NEXUS_MCP + ".") else tool,
                                              arguments, workspace_id)
            target, name = await self.resolve_tool(tool, workspace_id)
            return await self.call(target, name, arguments, workspace_id)

        from . import file_copy_tools
        file_copy_tools.register(server, self)

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

    def _tools_of(self, peer: Peer, mcp_name: str) -> list[dict]:
        return device_tools(peer, mcp_name, is_device=self.gateway.is_device_mcp(peer, mcp_name))

    def listed(self, peer: Peer, descriptor: dict) -> bool:
        """Exposed and, in compact mode, not one of the rarely used tools kept behind toolsearch."""
        return self.exposed(peer, descriptor) and not (self.gateway.tool_listing == "compact" and deferred(descriptor))

    async def _list_tools(self):
        selection = None
        with contextlib.suppress(RoutingError):
            selection = await self.gateway.selection()
        try:
            peer, workspace, _ = self.gateway.hub.workspace(selection)
        except RoutingError:
            # Publish only workspace tool names even before a computer is
            # chosen. Execution still requires an unambiguous selection.
            tools = {}
            for candidate in self.gateway.hub.peers.values():
                for entry in candidate.catalog["workspaces"].values():
                    for descriptor in self._tools_of(candidate, entry["mcp"]):
                        if self.listed(candidate, descriptor):
                            tools.setdefault(descriptor["name"], self.make_tool(descriptor))
            return list(tools.values())
        return [self.make_tool(tool, peer) for tool in self._tools_of(peer, workspace["mcp"])
                if self.listed(peer, tool) and "workspace_id" not in tool["inputSchema"].get("properties", {})]

    async def _get_tool(self, name, version=None):
        # Schema resolution must work before a handle argument is available.
        # Search the selected workspace first, then all workspace providers;
        # a tool kept out of the list (compact) is still found and callable.
        for tool in await self._list_tools():
            if tool.name == name:
                return tool
        # A tool kept out of the list: the selected device's own descriptor first, so its
        # arguments are checked against that device's schema, not another device's.
        selection = None
        with contextlib.suppress(RoutingError):
            selection = await self.gateway.selection()
        selected = None
        with contextlib.suppress(RoutingError):
            selected = self.gateway.hub.workspace(selection)
        if selected is not None:
            peer, workspace, _ = selected
            for tool in self._tools_of(peer, workspace["mcp"]):
                if tool["name"] == name and self.exposed(peer, tool):
                    return self.make_tool(tool, peer)
        for peer in self.gateway.hub.peers.values():
            for workspace in peer.catalog["workspaces"].values():
                for tool in self._tools_of(peer, workspace["mcp"]):
                    if tool["name"] == name and self.exposed(peer, tool):
                        return self.make_tool(tool)
        return None

    def make_tool(self, descriptor: dict, peer: Peer | None = None) -> SchemaTool:
        """``peer``: the device the descriptor came from; only a 本机 MCP caller's own device keeps
        exec_command's "default here" clause (elsewhere the list is shared by several devices)."""
        descriptor = english_metadata(descriptor)
        if descriptor.get("name") == "exec_command" and (peer is None or peer.agent_id != caller_device()):
            descriptor = {**descriptor, "description": neutral_exec_description(descriptor.get("description") or "")}
        compact = self.gateway.tool_listing == "compact"
        # Validation uses the device's own schema: compacting (X | null -> X) is applied to the
        # listing only (NexusServer.lean_meta), so a nested explicit null stays valid.
        schema = copy.deepcopy(descriptor["inputSchema"])
        schema.setdefault("properties", {})["workspace_id"] = dict(WORKSPACE_ID_SCHEMA)

        async def run(arguments: dict) -> ToolResult:
            arguments = dict(arguments)
            try:
                handle = arguments.pop("workspace_id", None)
                selection = await self.gateway.selection(handle)
                peer, workspace, workspace_name = self.gateway.hub.workspace(selection)
                return await self.gateway.call(f'{peer.agent_id}/{workspace["mcp"]}', descriptor["name"],
                                               arguments, handle)
            except RoutingError as exc:
                raise ToolFailure({"error": "RoutingError", "message": str(exc)}) from None

        # Keep the upstream label; without it clients show the name title-cased.
        title = descriptor.get("title") or (descriptor.get("annotations") or {}).get("title")
        meta = device_contract.tool_meta(descriptor) or None
        output_schema = descriptor.get("outputSchema")
        if descriptor["name"] in EXEC_TEXT_TOOLS:
            output_schema = None  # their results are plain text (Gateway.present)
        if compact:
            # Presentation only: anx/capability stays (clients and tests read it); the device's
            # generic "any object" output schema and contract tag add nothing for a model.
            capability = (meta or {}).get(device_contract.META_CAPABILITY)
            meta = {device_contract.META_CAPABILITY: capability} if capability else None
            if output_schema == GENERIC_OUTPUT_SCHEMA:
                output_schema = None
        return SchemaTool(name=descriptor["name"], title=title, description=descriptor.get("description", ""),
                          parameters=schema, output_schema=output_schema, drop_nulls=compact,
                          annotations=descriptor.get("annotations"), meta=meta, handler=run)


DeviceProvider = WorkspaceProvider

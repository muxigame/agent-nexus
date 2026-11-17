"""A single Workspace MCP facade over connected managers."""
from __future__ import annotations

import copy
import contextlib
import asyncio
import re
import time
from typing import Any
from uuid import uuid4

from fastmcp import Context, FastMCP
from fastmcp.server.dependencies import get_context
from fastmcp.server.providers import Provider
from fastmcp.tools.base import ToolResult
from mcp_types import CallToolResult, ToolAnnotations
from mcpkit.tools import SchemaTool, ToolFailure
from jsonschema import Draft202012Validator

from .federation import Hub, RESERVED_TOOLS, RoutingError

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


class Gateway:
    def __init__(self, hub: Hub):
        self.hub = hub
        # Handles are application-level workspace contexts, also usable by
        # clients using the sessionless MCP transport.
        self.handles: dict[str, tuple[dict, float]] = {}

    async def selection(self, workspace_id: str | None = None) -> dict | None:
        if workspace_id:
            record = self.handles.get(workspace_id)
            if record is None or record[1] < time.monotonic():
                self.handles.pop(workspace_id, None)
                raise RoutingError("workspace_id is unknown or expired; call set_workspace again")
            self.handles[workspace_id] = (record[0], time.monotonic() + 86400)
            return {**record[0], "scope": workspace_id}
        try:
            return await get_context().get_state("mcpm_workspace")
        except RuntimeError:
            return None

    async def call(self, mcp_id: str, tool: str, arguments: dict, workspace_id: str | None = None) -> ToolResult:
        peer, name, mcp = self.hub.mcp(mcp_id)
        descriptor = next((entry for entry in mcp.get("tools", []) if entry["name"] == tool), None)
        if descriptor is None:
            raise RoutingError(f"unknown tool {mcp_id}/{tool}")
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

    def build(self) -> FastMCP:
        server = FastMCP("Workspace", instructions=(
            "Use workspace_list then set_workspace to choose a computer/manager instance and workspace. "
            "Workspace tools are exposed directly. Other MCPs are accessed through mcplist, mcpdetail, "
            "toolsearch and tooluse. Keep the workspace_id returned by set_workspace and pass it to calls "
            "for explicit routing, especially with sessionless clients. Tool execution is never retried automatically."
        ), providers=[WorkspaceProvider(self)])

        @server.tool(title="列出电脑和工作区", annotations=ROUTING_READ_ONLY)
        async def workspace_list() -> dict:
            """List connected computers/manager instances and their configured workspaces."""
            return {"workspaces": [{"agent_id": peer.agent_id, "workspace": name, **workspace}
                                   for peer in self.hub.peers.values()
                                   for name, workspace in peer.catalog["workspaces"].items()]}

        @server.tool(title="选择工作区", annotations=ROUTING_READ_ONLY)
        async def set_workspace(agent_id: str, workspace: str, ctx: Context) -> dict:
            """Choose a workspace provided by a specific computer/manager instance."""
            selection = {"agent_id": agent_id, "workspace": workspace}
            _, descriptor, _ = self.hub.workspace(selection)
            now = time.monotonic()
            self.handles = {key: record for key, record in self.handles.items() if record[1] > now}
            if len(self.handles) >= 10000:
                raise RoutingError("workspace context limit reached")
            handle = uuid4().hex
            selection["scope"] = handle
            self.handles[handle] = (selection, now + 86400)
            await ctx.set_state("mcpm_workspace", selection)
            # Older stateful transports support notifications; modern clients
            # rediscover per request and use the explicit workspace handle.
            with contextlib.suppress(RuntimeError, AttributeError):
                await ctx.session.send_tool_list_changed()
            return {"workspace_id": handle, "agent_id": agent_id, "workspace": workspace, **descriptor}

        @server.tool(title="列出内部 MCP", annotations=ROUTING_READ_ONLY)
        async def mcplist(agent_id: str | None = None) -> dict:
            """List internal MCPs without expanding all their tools into the public tool list."""
            return {"mcps": self.hub.inventory(agent_id)}

        @server.tool(title="查看 MCP 详情", annotations=ROUTING_READ_ONLY)
        async def mcpdetail(mcp_id: str) -> dict:
            """Return an instance/service descriptor and its full tool schemas."""
            _, _, descriptor = self.hub.mcp(mcp_id)
            return {"mcp_id": mcp_id, **descriptor}

        @server.tool(title="搜索内部工具", annotations=ROUTING_READ_ONLY)
        async def toolsearch(query: str, agent_id: str | None = None, limit: int = 20, offset: int = 0) -> dict:
            """Rank internal tools by partial natural-language matches. Tool-name terms weigh most,
            descriptions next, and MCP names add context. Results include schemas, scores and matched terms."""
            if not 1 <= limit <= 100 or offset < 0:
                raise RoutingError("limit must be 1-100 and offset nonnegative")
            query_terms = _search_terms(query)
            if not query_terms:
                raise RoutingError("query must contain searchable words")
            matches = []
            for mcp in self.hub.inventory(agent_id):
                _, _, descriptor = self.hub.mcp(mcp["mcp_id"])
                for tool in descriptor.get("tools", []):
                    score, matched_terms = score_tool_search(query, mcp["mcp_id"], tool)
                    if score:
                        matches.append({"mcp_id": mcp["mcp_id"], **tool,
                                        "score": score, "matched_terms": matched_terms})
            matches.sort(key=lambda item: (-item["score"], item["mcp_id"], item["name"]))
            return {"tools": matches[offset:offset + limit], "total": len(matches),
                    "query_terms": query_terms,
                    "next_offset": offset + limit if offset + limit < len(matches) else None}

        @server.tool(title="调用内部 MCP 工具")
        async def tooluse(mcp_id: str, tool: str, arguments: dict[str, Any], workspace_id: str | None = None) -> ToolResult:
            """Execute an internal MCP tool with the arguments given by mcpdetail/toolsearch."""
            return await self.call(mcp_id, tool, arguments, workspace_id)

        return server


class WorkspaceProvider(Provider):
    def __init__(self, gateway: Gateway):
        super().__init__()
        self.gateway = gateway

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
                        if descriptor["name"] not in RESERVED_TOOLS:
                            tools.setdefault(descriptor["name"], self.make_tool(descriptor))
            return list(tools.values())
        return [self.make_tool(tool) for tool in peer.catalog["mcps"][workspace["mcp"]].get("tools", [])
                if tool["name"] not in RESERVED_TOOLS and "workspace_id" not in tool["inputSchema"].get("properties", {})]

    async def _get_tool(self, name, version=None):
        # Schema resolution must work before a handle argument is available.
        # Search the selected workspace first, then all workspace providers.
        for tool in await self._list_tools():
            if tool.name == name:
                return tool
        for peer in self.gateway.hub.peers.values():
            for workspace in peer.catalog["workspaces"].values():
                for tool in peer.catalog["mcps"][workspace["mcp"]].get("tools", []):
                    if tool["name"] == name and name not in RESERVED_TOOLS:
                        return self.make_tool(tool)
        return None

    def make_tool(self, descriptor: dict) -> SchemaTool:
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
        return SchemaTool(name=descriptor["name"], title=title, description=descriptor.get("description", ""),
                          parameters=schema, output_schema=descriptor.get("outputSchema"),
                          annotations=descriptor.get("annotations"), handler=run)

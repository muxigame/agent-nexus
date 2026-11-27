"""How chat runs attach the 本机 MCP per run: ACP sessions, {mcp_url} of custom CLIs, API models' tools."""
from __future__ import annotations

import asyncio
import json
import types

from fastmcp import Client, FastMCP

from agent_nexus.agents import adapters, registry
from agent_nexus.agents.api.loop import ApiAdapter
from agent_nexus.agents.hub import claude_allowed, mcp_hint

URL = "http://127.0.0.1:8399/mcp"
ATTACH = {"name": "agent-nexus", "url": URL}


def acp(extra):
    d = registry.builtin_definitions()["gemini"]
    adapter = adapters.Acp(d, adapters.AdapterOptions(binary="gemini", workdir="/w", extra=extra))
    adapter.start()
    return adapter


def session_new(adapter, caps):
    adapter.outbox.clear()
    adapter.feed(json.dumps({"jsonrpc": "2.0", "id": 1, "result": {"agentCapabilities": caps}}))
    return json.loads(adapter.outbox[-1])


def test_acp_passes_the_local_mcp_only_to_agents_that_take_http():
    sent = session_new(acp({"mcp_attach": ATTACH}), {"mcpCapabilities": {"http": True}})
    assert sent["method"] == "session/new"
    assert sent["params"]["mcpServers"] == [{"type": "http", "name": "agent-nexus", "url": URL, "headers": []}]
    no_http = acp({"mcp_attach": ATTACH})
    assert session_new(no_http, {})["params"]["mcpServers"] == []
    assert any("HTTP" in note for note in no_http.notices)
    assert session_new(acp({}), {"mcpCapabilities": {"http": True}})["params"]["mcpServers"] == []


def test_custom_clis_get_the_mcp_url_placeholder():
    d = registry.AgentDef(kind="mine", label="Mine", binaries=("mine",), adapter="text",
                          command=["{bin}", "--mcp", "{mcp_url}", "{unknown}", "{prompt}"])
    adapter = adapters.make(d, adapters.AdapterOptions(binary="mine", workdir="/w", extra={"mcp_url": URL}))
    assert adapter.argv("hi") == ["mine", "--mcp", URL, "{unknown}", "hi"]


def test_claude_allowed_tools_per_permission():
    prefix = "mcp__agent-nexus__"
    assert claude_allowed(prefix, "full") == []
    edits = claude_allowed(prefix, "edits")
    assert "mcp__agent-nexus" not in edits and "mcp__agent-nexus__file_write" in edits
    assert "mcp__agent-nexus__exec_command" not in edits and "mcp__agent-nexus__keyboard" not in edits
    assert "mcp__agent-nexus__file_write" not in claude_allowed(prefix, "edits", "direct")
    for mode in ("ask", "readonly", "edits"):
        assert not {prefix + t for t in ("exec_command", "write_stdin", "mouse", "tooluse", "use_device",
                                          "file_copy")} & set(claude_allowed(prefix, mode))
    assert "mcp__agent-nexus__file_read" in claude_allowed(prefix, "readonly")
    assert "mcp__agent-nexus__mail_send" not in claude_allowed(prefix, "readonly")
    assert {"mcp__agent-nexus__mail_send", "mcp__agent-nexus__ask_user"} <= set(claude_allowed(prefix, "ask"))
    assert "mcp__claude_ai_Agent_Nexus__device_list" in claude_allowed("mcp__claude_ai_Agent_Nexus__", "ask")


def test_the_hint_names_one_connector_or_stays_generic():
    v = {"handle": "h1", "target_device": "pc-2", "target_workdir": "/srv/app", "address": "codex-app@pc-2"}
    both = mcp_hint({"state": "both", "use": "agent-nexus", "also": ["claude.ai Agent Nexus"],
                     "tool_prefix": "mcp__agent-nexus__", "local_state": "ready"}, v)
    assert "连接器 agent-nexus" in both and "只用 agent-nexus" in both and 'workspace_id="h1"' in both
    assert "/srv/app" in both and "codex-app@pc-2" in both
    unknown = mcp_hint({"state": "unknown"}, v)
    assert "只用其中一个连接器" in unknown and 'workspace_id="h1"' in unknown
    down = mcp_hint({"state": "local", "use": "agent-nexus", "local_state": "offline",
                     "local_reason": "网关未连接，稍后再试"}, v)
    assert "现在不可用" in down


def fake_gateway_mcp() -> FastMCP:
    server = FastMCP("gw")
    calls = []

    @server.tool(annotations={"readOnlyHint": True})
    def file_read(path: str, workspace_id: str) -> str:
        calls.append(("file_read", path, workspace_id))
        return f"content of {path}"

    @server.tool
    def edit_file(path: str, old_string: str, new_string: str, workspace_id: str) -> str:
        calls.append(("edit_file", path, workspace_id))
        return "edited"

    @server.tool
    def workspace_list() -> str:
        return "legacy"

    server.calls = calls
    return server


def api_adapter(server, *, mode="gateway", permission="full"):
    d = registry.AgentDef(kind="api:p/m", label="M", binaries=(), adapter="api")
    spec = {"provider": {"id": "p"}, "model": {"id": "m"}, "handle": "h-42", "gateway": object(), "mode": mode,
            "mcp_url": URL, "mcp_client": lambda url: Client(server)}
    adapter = ApiAdapter(d, adapters.AdapterOptions(binary="api", workdir="/w", permission=permission,
                                                    extra={"api": spec}))
    adapter._runs = types.SimpleNamespace(_push_all=lambda run, events: None)
    adapter._run = types.SimpleNamespace(closed=False, token="t")
    return adapter


def test_api_models_get_the_gateway_tools_with_the_handle_filled_in():
    async def go():
        server = fake_gateway_mcp()
        adapter = api_adapter(server)
        await adapter._load_mcp()
        offered = {spec["name"]: spec for spec in adapter._offered()}
        # gateway mode: only the gateway's tools; the legacy names are gone; a clash with a local tool gets nx_
        assert set(offered) == {"file_read", "nx_edit_file"}
        assert "workspace_id" not in offered["file_read"]["parameters"]["properties"]
        assert "workspace_id" not in offered["file_read"]["parameters"].get("required", [])
        result = await adapter._call_tool({"id": "c1", "name": "file_read", "args": {"path": "/srv/app/a"}})
        assert result["output"] == "content of /srv/app/a" and not result["error"]
        assert server.calls[-1] == ("file_read", "/srv/app/a", "h-42")
        direct = api_adapter(server, mode="direct")
        await direct._load_mcp()
        names = {spec["name"] for spec in direct._offered()}
        assert {"read_file", "run_command", "file_read", "nx_edit_file"} <= names
        assert "ask_user" not in names or "mail_send" not in names  # the hand-rolled ones are not offered twice
    asyncio.run(go())


def test_api_mcp_approval_follows_read_only_hints():
    async def go():
        server = fake_gateway_mcp()
        adapter = api_adapter(server, permission="ask")
        asked = []

        async def approve(name, args, call_id):
            asked.append(name)
            return "用户拒绝了这个操作"
        adapter._approve = approve
        await adapter._load_mcp()
        await adapter._call_tool({"id": "c1", "name": "file_read", "args": {"path": "/a"}})
        denied = await adapter._call_tool({"id": "c2", "name": "nx_edit_file",
                                           "args": {"path": "/a", "old_string": "x", "new_string": "y"}})
        assert asked == ["nx_edit_file"] and denied["error"]
        readonly = api_adapter(server, permission="readonly")
        await readonly._load_mcp()
        assert {spec["name"] for spec in readonly._offered()} == {"file_read"}
    asyncio.run(go())

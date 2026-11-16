"""ChatGPT prompts for every tool without readOnlyHint; only routing is exempt."""
from fastmcp import Client

from agent_nexus.federation import Hub
from agent_nexus.gateway import Gateway


async def test_routing_tools_are_read_only_and_tooluse_still_confirms():
    async with Client(Gateway(Hub()).build()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("workspace_list", "set_workspace", "device_list", "use_device", "mcplist", "mcpdetail", "toolsearch"):
        assert tools[name].annotations.readOnlyHint is True, name
    # tooluse runs arbitrary upstream tools; it must keep asking.
    assert not (tools["tooluse"].annotations and tools["tooluse"].annotations.readOnlyHint)


async def test_every_tool_has_a_real_label_and_upstream_labels_survive():
    from agent_nexus.gateway import WorkspaceProvider
    gateway = Gateway(Hub())
    async with Client(gateway.build()) as client:
        tools = await client.list_tools()
    # FastMCP's fallback is the name title-cased ("Mcplist"); none may rely on it.
    assert all(tool.title and tool.title != tool.name.replace("_", " ").title() for tool in tools)
    provider = WorkspaceProvider(gateway)
    labelled = provider.make_tool({"name": "exec_command", "title": "运行命令", "inputSchema": {"type": "object"}})
    assert labelled.title == "Run a command"
    custom = provider.make_tool({"name": "custom_action", "title": "用户定义动作", "inputSchema": {"type": "object"}})
    assert custom.title == "用户定义动作"  # Unknown user metadata is never replaced with a vague fallback.


async def test_mail_tool_annotations_follow_the_design():
    """Design §8.5: reads are read-only, sends write but destroy nothing, recall is destructive."""
    async with Client(Gateway(Hub()).build()) as client:
        tools = {tool.name: tool for tool in await client.list_tools()}
    for name in ("mail_inbox", "mail_read", "mail_wait", "mail_who", "mail_status"):
        assert tools[name].annotations.read_only_hint is True, name
    for name in ("mail_send", "mail_bye", "ask_user"):
        hints = tools[name].annotations
        assert hints.read_only_hint is False and hints.destructive_hint is False and hints.open_world_hint is True, name
    assert tools["mail_recall"].annotations.destructive_hint is True
    assert {tools[name].title for name in ("mail_send", "mail_inbox", "ask_user")} == {"Send mail", "Check your inbox", "Ask the owner"}

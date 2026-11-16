"""English metadata must never alter executable tool contracts or source catalogs."""
import copy
import re

from fastmcp import Client

from agent_nexus.federation import Hub
from agent_nexus.gateway import Gateway, WorkspaceProvider
from agent_nexus.tool_language import english_metadata, translations


def test_known_metadata_translates_without_modifying_source_or_literal_values():
    literals = {"title": "运行命令", "description": "请保留原始输入", "nested": ["中文"]}
    source = {"name": "custom_action", "title": "运行命令", "description": "Custom English description.",
              "inputSchema": {"type": "object", "required": ["title"], "properties": {
                  "title": {"type": "object", "title": "运行命令", "default": copy.deepcopy(literals),
                            "const": copy.deepcopy(literals), "enum": [copy.deepcopy(literals)],
                            "examples": [copy.deepcopy(literals)]}}},
              "annotations": {"readOnlyHint": True}, "_meta": copy.deepcopy(literals)}
    original = copy.deepcopy(source)
    result = english_metadata(source)
    assert source == original
    assert result["title"] == "Run a command"
    assert result["inputSchema"]["properties"]["title"]["title"] == "Run a command"
    field = result["inputSchema"]["properties"]["title"]
    assert field["default"] == field["const"] == field["enum"][0] == field["examples"][0] == literals
    assert result["name"] == source["name"] and result["annotations"] == source["annotations"]
    assert result["_meta"] == literals
    assert english_metadata({"title": "未知的自定义标题"})["title"] == "未知的自定义标题"


def test_translation_catalog_is_complete_english_and_idempotent():
    entries = translations()
    assert entries and "运行命令" in entries
    assert all(isinstance(text, str) and text.strip() and not re.search(r"[\u3400-\u9fff]", text)
               for text in entries.values())
    assert english_metadata(english_metadata({"title": "运行命令"})) == english_metadata({"title": "运行命令"})


async def test_all_native_gateway_tool_documentation_is_english():
    async with Client(Gateway(Hub()).build()) as client:
        tools = await client.list_tools()
    for tool in tools:
        assert not re.search(r"[\u3400-\u9fff]", tool.title or ""), tool.name
        assert not re.search(r"[\u3400-\u9fff]", tool.description or ""), tool.name


def test_old_device_metadata_is_english_without_waiting_for_device_upgrade():
    descriptor = {"name": "exec_command", "title": "运行命令", "description": "Run a command.",
                  "inputSchema": {"type": "object", "properties": {}},
                  "_meta": {"anx/contract": "anx.device/1", "anx/capability": "exec"}}
    original = copy.deepcopy(descriptor)
    tool = WorkspaceProvider(Gateway(Hub())).make_tool(descriptor)
    assert tool.title == "Run a command"
    assert descriptor == original


async def test_detail_search_and_public_list_present_the_same_english_metadata(tmp_path):
    descriptor = {"name": "exec_command", "title": "运行命令", "description": "Run a command.",
                  "inputSchema": {"type": "object", "properties": {
                      "value": {"type": "string", "description": "运行命令", "default": "运行命令"}}}}
    hub = Hub()
    hub.register("device-one", object(), {"workspaces": {"home": {"path": str(tmp_path), "mcp": "device"}},
                                         "mcps": {"device": {"description": "Device tools", "tools": [descriptor]}}})
    original = copy.deepcopy(descriptor)
    async with Client(Gateway(hub).build()) as client:
        public = next(tool for tool in await client.list_tools() if tool.name == "exec_command")
        detail = (await client.call_tool("mcpdetail", {"mcp_id": "device-one/device"})).data["tools"][0]
        search = (await client.call_tool("toolsearch", {"query": "command"})).data["tools"][0]
    assert public.title == detail["title"] == search["title"] == "Run a command"
    assert detail["inputSchema"]["properties"]["value"]["description"] == "Run a command"
    assert detail["inputSchema"]["properties"]["value"]["default"] == "运行命令"
    assert descriptor == original


def test_gateway_source_package_contains_the_english_catalog():
    from scripts.build_release import release_files
    names = {name for _, name in release_files("gateway")}
    assert "schemas/tool-metadata.en.json" in names
    assert "schemas/device-contract.v1.json" in names

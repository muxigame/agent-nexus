"""Audit public MCP metadata without reading tool results or printing credentials."""
import argparse
import asyncio
import json
import re
from pathlib import Path

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport


def non_english_fields(value, path=""):
    found = []
    if isinstance(value, dict):
        for key, item in value.items():
            current = f"{path}.{key}" if path else key
            if key in ("title", "description") and isinstance(item, str) and re.search(r"[\u3400-\u9fff]", item):
                found.append(current)
            elif isinstance(item, (dict, list)):
                found.extend(non_english_fields(item, current))
    elif isinstance(value, list):
        for index, item in enumerate(value):
            found.extend(non_english_fields(item, f"{path}[{index}]"))
    return found


async def audit(config_path, export=None, smoke=False):
    config = json.loads(config_path.read_text())
    secret = config["auth"]["bearer_secrets"][0]
    token = (config_path.parent / "secrets" / secret).read_text().strip()
    url = config["public_url"].rstrip("/") + config.get("path", "/mcp")
    async with Client(StreamableHttpTransport(url, auth=token), timeout=30) as client:
        tools = await client.list_tools()
        public = {tool.name: non_english_fields(tool.model_dump(by_alias=True)) for tool in tools}
        inventory = (await client.call_tool("mcplist")).data["mcps"]
        internal = {}
        translations = {}
        def collect(value):
            if isinstance(value, dict):
                for key, item in value.items():
                    if key in ("title", "description") and isinstance(item, str) and re.search(r"[\u3400-\u9fff]", item):
                        translations.setdefault(item, "")
                    elif isinstance(item, (dict, list)):
                        collect(item)
            elif isinstance(value, list):
                for item in value:
                    collect(item)
        for tool in tools:
            collect(tool.model_dump(by_alias=True))
        for entry in inventory:
            detail = (await client.call_tool("mcpdetail", {"mcp_id": entry["mcp_id"]})).data
            collect(detail)
            internal[entry["mcp_id"]] = {tool["name"]: non_english_fields(tool) for tool in detail.get("tools", []) if non_english_fields(tool)}
        if export:
            export.write_text(json.dumps(translations, ensure_ascii=False, indent=2), encoding="utf-8")
        smoke_result = None
        if smoke:
            matches = (await client.call_tool("toolsearch", {"query": "weekly report", "limit": 3})).data["tools"]
            assert matches and not any(non_english_fields(tool) for tool in matches)
            device = next(entry["mcp_id"] for entry in inventory if entry["mcp_id"].endswith("/device"))
            result = await client.call_tool("tooluse", {"mcp_id": device, "tool": "curr_time", "arguments": {}})
            assert not result.is_error
            smoke_result = {"english_tool_search": True, "device_read_call": True}
        print(json.dumps({"public_tools": len(tools), "public_non_english": {name: paths for name, paths in public.items() if paths},
                          "internal_non_english_counts": {name: len(tools) for name, tools in internal.items() if tools},
                          "unique_translations": len(translations), "smoke": smoke_result}, ensure_ascii=False))


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", type=Path, default=Path("/etc/agent-nexus/gateway.json"))
    parser.add_argument("--export", type=Path)
    parser.add_argument("--smoke", action="store_true")
    args = parser.parse_args()
    asyncio.run(audit(args.config, args.export, args.smoke))

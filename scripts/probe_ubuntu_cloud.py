"""Read-only public MCP acceptance probe; credentials are read locally, never printed."""
import asyncio
import json

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hub_args import hub_parser, parse, read_token


async def main(args):
    async with Client(StreamableHttpTransport(args.url + "/mcp", auth=read_token(args))) as client:
        selected = (await client.call_tool("set_workspace", {"agent_id": args.agent_id, "workspace": "home"})).data
        details = (await client.call_tool("mcpdetail", {"mcp_id": args.agent_id + "/workspace"})).data
        names = {tool["name"] for tool in details["tools"]}
        if not {"exec_command", "edit_file", "patch_file", "view_image", "write_stdin"} <= names:
            raise SystemExit("core Workspace tools are missing")
        if names.intersection({"start_control", "read_ui", "mouse", "keyboard", "screenshot", "vision"}):
            raise SystemExit("Windows-only tools are exposed on Linux")
        result = (await client.call_tool("exec_command", {"cmd": "uname -s; pwd", "workspace_id": selected["workspace_id"], "max_output_tokens": 200})).data
        if result["exit_code"] != 0 or "Linux" not in result["output"]:
            raise SystemExit("uname probe failed")
        patch = "--- /dev/null\n+++ b/ubuntu-package-smoke.txt\n@@ -0,0 +1 @@\n+ubuntu-package-ok\n"
        await client.call_tool("patch_file", {"patch": patch, "workspace_id": selected["workspace_id"]})
        checked = (await client.call_tool("exec_command", {"cmd": "cat ubuntu-package-smoke.txt", "workspace_id": selected["workspace_id"], "max_output_tokens": 100})).data
        if checked["output"].strip() != "ubuntu-package-ok":
            raise SystemExit("patch_file round trip failed")
        await client.call_tool("exec_command", {"cmd": "rm -f ubuntu-package-smoke.txt", "workspace_id": selected["workspace_id"], "max_output_tokens": 100})
        info = (await client.call_tool("device_info", {"workspace_id": selected["workspace_id"]})).data
        if info["capabilities"]["uia"].get("available") is not False:
            raise SystemExit("desktop control unexpectedly reported as supported")
        print(json.dumps({"success": True, "agent_id": args.agent_id, "tools": len(names), "output": result["output"]}))


if __name__ == "__main__":
    parser = hub_parser(__doc__)
    parser.add_argument("agent_id")
    asyncio.run(main(parse(parser)))

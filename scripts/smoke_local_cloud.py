"""Exercise the real running local MCPs through an ephemeral cloud endpoint."""
import argparse
import asyncio
import json
import os
import secrets
import socket
import tempfile
from pathlib import Path

import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from agent_nexus.agent import run_agent
from agent_nexus.cloud import build_cloud_app
from agent_nexus.federation import RESERVED_TOOLS


async def smoke(root):
    token = secrets.token_urlsafe(48)
    os.environ["ANX_SMOKE_TOKEN"] = token
    app = build_cloud_app({"token_env": "ANX_SMOKE_TOKEN", "agents": {"local-smoke": "ANX_SMOKE_TOKEN"}}, root)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    server_task = asyncio.create_task(server.serve(sockets=[listener]))
    agent_task = None
    try:
        for _ in range(100):
            if server.started:
                break
            await asyncio.sleep(.05)
        assert server.started
        with tempfile.TemporaryDirectory(prefix="anx-device-smoke-") as temporary:
            ready = asyncio.Event()
            cloud = {"agent_id": "local-smoke", "url": f"ws://127.0.0.1:{port}", "token_env": "ANX_SMOKE_TOKEN",
                     "workspaces": {"repository": str(root), "temporary": temporary}}
            agent_task = asyncio.create_task(run_agent(root, cloud, ready=ready))
            await asyncio.wait_for(ready.wait(), 30)
            async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=token)) as client:
                inventory = (await client.call_tool("mcplist")).data["mcps"]
                assert len(inventory) == 6, inventory
                assert all(not item["error"] for item in inventory), inventory
                public_tools = {tool.name for tool in await client.list_tools()}
                workspace_tools = {tool["name"] for tool in (await client.call_tool("mcpdetail", {"mcp_id": "local-smoke/workspace"})).data["tools"]}
                assert public_tools == workspace_tools | RESERVED_TOOLS
                handle = (await client.call_tool("set_workspace", {"agent_id": "local-smoke", "workspace": "temporary"})).data["workspace_id"]
                args = {"cmd": 'python -c "import os; print(os.path.basename(os.getcwd()))"', "yield_time_ms": 1000, "max_output_tokens": 1000}
                executed = await client.call_tool("exec_command", {**args, "workspace_id": handle})
                payload = executed.data or json.loads(executed.content[0].text)
                assert payload["exit_code"] == 0 and Path(temporary).name in payload["output"], payload
                used = await client.call_tool("tooluse", {"mcp_id": "local-smoke/workspace", "tool": "exec_command",
                                                          "arguments": args, "workspace_id": handle})
                payload = used.data or json.loads(used.content[0].text)
                assert payload["exit_code"] == 0 and Path(temporary).name in payload["output"], payload
                print(f"real local cloud smoke passed: {len(inventory)} MCPs, {len(workspace_tools)} workspace tools, selected directory verified")
    finally:
        if agent_task:
            agent_task.cancel()
            await asyncio.gather(agent_task, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        listener.close()
        os.environ.pop("ANX_SMOKE_TOKEN", None)


if __name__ == "__main__":
    parser = argparse.ArgumentParser()
    parser.add_argument("--root", type=Path, required=True)
    args = parser.parse_args()
    asyncio.run(smoke(args.root.resolve()))

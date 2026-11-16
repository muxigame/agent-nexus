"""Check the public hub and this machine's cloud link (read-only)."""
import asyncio
from pathlib import Path

import httpx
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcpkit.config import ServiceConfig, read_secret

from hub_args import hub_parser, parse
from agent_nexus.cli import Api
from agent_nexus.registry import load_manager_config


async def verify(args):
    root = Path(args.root).resolve()
    async with httpx.AsyncClient(trust_env=False, timeout=15) as http:
        health = await http.get(args.url + "/healthz")
        print("Public health:", health.status_code, "server:", health.headers.get("server"),
              "mitigation:", health.headers.get("cf-mitigated"))
        if health.status_code != 200:
            raise SystemExit("hub health check failed")
    token = read_secret(ServiceConfig(name="workspace", root=root, dir=root), "workspace/bearer-token")
    async with Client(StreamableHttpTransport(args.url + "/mcp", auth=token)) as client:
        tools = await client.list_tools()
        workspaces = (await client.call_tool("workspace_list")).data["workspaces"]
        if args.expect_tools is not None and len(tools) != args.expect_tools:
            raise SystemExit(f"expected {args.expect_tools} tools, got {len(tools)}")
        if not workspaces:
            raise SystemExit("no workspace is registered")
        print("Local public client:", len(tools), "tools;", len(workspaces), "workspace(s)")
    print("Local cloud status:", Api(load_manager_config(root)).call("GET", "/api/cloud")[1])


if __name__ == "__main__":
    parser = hub_parser(__doc__)
    parser.add_argument("--root", default=str(Path(__file__).resolve().parents[2]),
                        help="manager configuration root (default: parent of this checkout)")
    parser.add_argument("--expect-tools", type=int, default=None, help="optional exact public tool count")
    asyncio.run(verify(parse(parser)))

"""Observe public auth and run one read-only command per connected Workspace."""
import asyncio
import json

import httpx

#: /healthz "service" of the gateway; a gateway still on the legacy release says "workspace-cloud".
HEALTH_SERVICES = {"agent-nexus-gateway", "workspace-cloud"}
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hub_args import hub_parser, parse, read_token


async def main(args):
    origin = args.url
    async with httpx.AsyncClient(trust_env=False, timeout=20) as http:
        health = await http.get(origin + "/healthz")
        if health.status_code != 200 or health.json().get("service") not in HEALTH_SERVICES:
            raise SystemExit("healthz failed")
        metadata = (await http.get(origin + "/.well-known/oauth-authorization-server")).json()
        resource = (await http.get(origin + "/.well-known/oauth-protected-resource/mcp")).json()
        if metadata["issuer"] != origin or resource["resource"] != origin + "/mcp":
            raise SystemExit("OAuth issuer/resource do not match --url")
        if (await http.post(origin + "/mcp", json={})).status_code not in (401, 403):
            raise SystemExit("/mcp accepted an unauthenticated request")
        if (await http.get(origin + "/api/services")).status_code != 404:
            raise SystemExit("manager API is exposed on the hub")
    async with Client(StreamableHttpTransport(origin + "/mcp", auth=read_token(args))) as client:
        tools = await client.list_tools()
        names = {tool.name for tool in tools}
        missing = {"workspace_list", "set_workspace", "mcplist", "mcpdetail", "toolsearch", "tooluse"} - names
        if missing:
            raise SystemExit("gateway tools missing: " + ", ".join(sorted(missing)))
        workspaces = (await client.call_tool("workspace_list")).data["workspaces"]
        inventory = (await client.call_tool("mcplist")).data["mcps"]
        results = []
        for workspace in workspaces:
            selected = (await client.call_tool("set_workspace", {"agent_id": workspace["agent_id"], "workspace": workspace["workspace"]})).data
            result = (await client.call_tool("exec_command", {"cmd": "hostname", "workspace_id": selected["workspace_id"], "max_output_tokens": 100})).data
            if result["exit_code"] != 0:
                raise SystemExit(f"command failed on {workspace['agent_id']}")
            results.append({"agent_id": workspace["agent_id"], "workspace": workspace["workspace"], "hostname": result["output"].strip()})
        print(json.dumps({"auth": "passed", "public_tools": len(tools), "mcps": len(inventory), "workspaces": results}, ensure_ascii=False))


if __name__ == "__main__":
    asyncio.run(main(parse(hub_parser(__doc__))))

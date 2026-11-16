"""Read-only public MCP probe using distinct downstream clients and workspace scopes."""
import asyncio
import json
import time

from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from hub_args import hub_parser, parse, read_token


async def main(args):
    token = read_token(args)

    async def branch(name):
        async with Client(StreamableHttpTransport(args.url + "/mcp", auth=token), timeout=20, init_timeout=20) as client:
            handle = (await client.call_tool("set_workspace", {"agent_id": args.agent_id, "workspace": args.workspace})).data["workspace_id"]
            elapsed = []
            for _ in range(3):
                start = time.monotonic()
                result = (await client.call_tool("exec_command", {"cmd": "hostname", "workspace_id": handle,
                                                                 "yield_time_ms": 1000, "max_output_tokens": 100})).data
                if result["exit_code"] != 0:
                    raise RuntimeError(f"command failed in branch {name}")
                elapsed.append(round(time.monotonic() - start, 3))
            return {"branch": name, "calls": 3, "elapsed_s": elapsed}
    results = await asyncio.wait_for(asyncio.gather(branch("main-probe"), branch("parallel-probe")), 55)
    print(json.dumps({"success": True, "distinct_clients": 2, "distinct_workspace_scopes": 2, "results": results}))


if __name__ == "__main__":
    parser = hub_parser(__doc__)
    parser.add_argument("--agent-id", required=True, help="device to probe, as listed by workspace_list")
    parser.add_argument("--workspace", default="default")
    asyncio.run(main(parse(parser)))

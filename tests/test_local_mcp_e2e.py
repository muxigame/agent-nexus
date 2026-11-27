"""本机 MCP end to end (agent_nexus/local_mcp.py, docs/design.md "本机 MCP"): a real gateway on a port, a real device
link, the local endpoint behind the console's LocalOnly on its own loopback port, and a plain MCP client with no
credential. The CLIs get exactly the owner's tools; foreign Host/Origin, a locked device and the switch being off
are refused."""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
import socket
import types

import httpx
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from starlette.applications import Starlette
from starlette.routing import Route

from agent_nexus.agent import HubLink, run_agent
from agent_nexus.cloud import build_cloud_app
from agent_nexus.local_mcp import STATUS_TOOL, LocalMcp
from agent_nexus.web import LocalOnly

from test_remote_m19 import network_config

SERVER = "SERVER_TOKEN"


def device_server() -> FastMCP:
    server = FastMCP("device-pc-a")

    @server.tool
    async def execute(value: str) -> dict:
        """Echo (test device tool)."""
        return {"device": "pc-a", "value": value}

    return server


async def serve(app):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))  # ephemeral: never a live service port
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    return server, task, listener, port


async def stop(server, task, listener):
    server.should_exit = True
    with contextlib.suppress(BaseException):
        await asyncio.wait_for(task, 10)
    listener.close()


def tool_names(tools) -> list[str]:
    return sorted(tool.name for tool in tools)


async def raw(port: int, headers: dict | None = None, *, host: str | None = None) -> httpx.Response:
    body = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
            "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "t", "version": "1"}}}
    base = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
            "Host": host or f"127.0.0.1:{port}"}
    async with httpx.AsyncClient(trust_env=False) as http:
        return await http.post(f"http://127.0.0.1:{port}/mcp", content=json.dumps(body),
                               headers={**base, **(headers or {})})


async def test_a_cli_reaches_the_gateway_tools_through_the_local_mcp_without_a_credential(tmp_path, monkeypatch):
    gateway_app = build_cloud_app(network_config(tmp_path, monkeypatch), tmp_path)
    gw_server, gw_task, gw_listener, gw_port = await serve(gateway_app)

    root = tmp_path / "pc-a"
    home = root / "home"
    home.mkdir(parents=True)
    cloud = {"enabled": True, "agent_id": "pc-a", "url": f"ws://127.0.0.1:{gw_port}", "token_env": "TOKEN_A",
             "workspaces": {"projects": {"path": str(home), "mcp": "device"}}}
    (root / "agent-nexus.json").write_text(json.dumps({"cloud": cloud}), encoding="utf-8")
    status: dict = {}
    hub = HubLink()
    ready = asyncio.Event()
    agent = asyncio.create_task(run_agent(root, cloud, status=status, transports={"device": device_server()},
                                          ready=ready, hub=hub))
    local = None
    try:
        await asyncio.wait_for(ready.wait(), 20)
        for _ in range(100):
            if hub.available:
                break
            await asyncio.sleep(0.05)

        # the console: LocalOnly in front, LocalMcp on /mcp, real device credential lookup and gateway client
        listener = socket.socket()
        listener.bind(("127.0.0.1", 0))
        port = listener.getsockname()[1]
        listener.close()
        cfg = types.SimpleNamespace(host="127.0.0.1", port=port, root=root, mcp_local={})
        lock = types.SimpleNamespace(locked=False)
        local_mcp = LocalMcp(cfg, status, hub, lock)
        app = LocalOnly(Starlette(routes=[Route("/mcp", local_mcp, methods=["POST", "DELETE", "GET"])]),
                        "127.0.0.1", port)
        local = await serve_on(app, port)
        assert local_mcp.state() == "ready", local_mcp.status()
        assert local_mcp.url == f"http://127.0.0.1:{port}/mcp"

        async with Client(StreamableHttpTransport(f"http://127.0.0.1:{gw_port}/mcp", auth=os.environ[SERVER])) as owner:
            owner_tools = tool_names(await owner.list_tools())

        async with Client(StreamableHttpTransport(local_mcp.url)) as cli:  # no credential at all
            local_tools = tool_names(await cli.list_tools())
            assert local_tools == owner_tools
            assert {"device_list", "use_device", "mail_send", "ask_user", "file_copy"} <= set(local_tools)
            listed = (await cli.call_tool("device_list", {})).data
            assert "pc-a" in json.dumps(listed, ensure_ascii=False)
            used = (await cli.call_tool("use_device", {"device_id": "pc-a", "workdir": "projects"})).data
            handle = used["workspace_id"]
            device_tool = next(name for name in local_tools if name == "execute" or name.endswith("execute"))
            echoed = await cli.call_tool(device_tool, {"value": "hi", "workspace_id": handle})
            assert not echoed.is_error and "pc-a" in json.dumps(echoed.data if echoed.data is not None
                                                                 else [c.text for c in echoed.content])

        # browsers and DNS rebinding are refused before anything reaches the gateway
        assert (await raw(port)).status_code == 200
        assert (await raw(port, {"Origin": "https://evil.example"})).status_code == 403
        assert (await raw(port, host="evil.example")).status_code == 403
        assert (await raw(port, host=f"evil.example:{port}")).status_code == 403
        assert (await raw(port, {"Content-Type": "text/plain"})).status_code == 415

        # locked here: no gateway tool at all, every call refused with the reason
        lock.locked = True
        async with Client(StreamableHttpTransport(local_mcp.url)) as cli:
            assert tool_names(await cli.list_tools()) == [STATUS_TOOL]
            refused = await cli.call_tool("device_list", {}, raise_on_error=False)
            assert refused.is_error and "锁定" in refused.content[0].text
        lock.locked = False

        # locked as the gateway sees it (the report reached it, our flag did not): the gateway's 423 is mapped
        await hub.call("lock_report", {"locked": True})
        async with Client(StreamableHttpTransport(local_mcp.url)) as cli:
            assert tool_names(await cli.list_tools()) == [STATUS_TOOL]
        await hub.call("lock_report", {"locked": False})

        # the switch off: the endpoint is gone
        cfg.mcp_local = {"enabled": False}
        assert local_mcp.state() == "off"
        assert (await raw(port)).status_code == 404
    finally:
        if local is not None:
            await stop(*local)
        agent.cancel()
        with contextlib.suppress(BaseException):
            await agent
        await stop(gw_server, gw_task, gw_listener)


async def serve_on(app, port: int):
    listener = socket.socket()
    listener.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    listener.bind(("127.0.0.1", port))
    listener.listen()
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False,
                                           lifespan="off"))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    return server, task, listener

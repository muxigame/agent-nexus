"""Compatibility matrix baseline (design §3.3, milestone M0).

Every combination of {legacy, current} agent and {legacy, current} gateway is
run end to end over a real WebSocket on an ephemeral port. At M0 "current" is
still the baseline code, so these tests pin the behaviour later milestones must
keep: in particular the rollback gate "new agent -> legacy_mcpm gateway".

The handshake and catalog the legacy gateway receives are normalised and
compared with ``fixtures/legacy_handshake.json``. Re-record with
``ANX_RECORD_COMPAT_FIXTURES=1 uv run pytest tests/compat`` only when a change
to the wire format is intended (and never for the legacy pair).
"""
from __future__ import annotations

import asyncio
import importlib
import json
import os
import socket
from pathlib import Path

import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport
from fastmcp.tools.base import ToolResult
from mcp_types import ImageContent, TextContent
from starlette.websockets import WebSocket

FIXTURES = Path(__file__).resolve().parent / "fixtures"
HANDSHAKE_FIXTURE = FIXTURES / "legacy_handshake.json"
SERVER_TOKEN = "s" * 40
AGENT_TOKEN = "a" * 40
AGENT_ID = "pc-main"
DEVICE_TOOLS = {"execute", "picture", "failure"}


def package(kind: str, module: str):
    return importlib.import_module(("legacy_mcpm." if kind == "legacy" else "agent_nexus.") + module)


def device_server(machine: str) -> FastMCP:
    server = FastMCP(f"workspace-{machine}")

    @server.tool
    async def execute(value: str, workdir: str | None = None) -> dict:
        return {"machine": machine, "value": value, "workdir": workdir}

    @server.tool
    async def picture() -> ToolResult:
        return ToolResult(content=[ImageContent(type="image", data="YWJj", mime_type="image/png")],
                          structured_content={"machine": machine}, meta={"origin": machine})

    @server.tool
    async def failure() -> ToolResult:
        return ToolResult(content=[TextContent(type="text", text="expected failure")], is_error=True)

    return server


def extra_server() -> FastMCP:
    server = FastMCP("extra")

    @server.tool
    async def internal_action(number: int) -> dict:
        return {"answer": number * 2}

    return server


def _schema_shape(schema: dict) -> dict:
    """Keep what a cached client schema depends on, not library formatting."""
    schema = schema or {}
    return {"properties": sorted((schema.get("properties") or {}).keys()),
            "required": sorted(schema.get("required") or [])}


def normalise_hello(hello: dict, workdir: Path) -> dict:
    catalog = hello["catalog"]
    mcps = {}
    for name, entry in sorted(catalog["mcps"].items()):
        tools = {tool["name"]: _schema_shape(tool.get("inputSchema") or tool.get("input_schema") or {})
                 for tool in entry.get("tools", [])}
        mcps[name] = {"keys": sorted(entry), "tools": dict(sorted(tools.items()))}
    workspaces = {name: {key: ("<workdir>" if key == "path" and Path(value) == workdir else value)
                         for key, value in sorted(entry.items())}
                  for name, entry in sorted(catalog["workspaces"].items())}
    return {"keys": sorted(hello), "protocol": hello["protocol"], "agent_id": hello["agent_id"],
            "catalog": {"keys": sorted(catalog), "workspaces": workspaces, "mcps": mcps}}


class Recorder:
    """Wraps the gateway's Hub/Peer to see exactly what crosses the link."""

    def __init__(self, federation, monkeypatch):
        self.hellos: list[dict] = []
        self.to_gateway: list[dict] = []
        self.to_agent: list[dict] = []
        receive, send = WebSocket.receive_json, federation.Peer.send
        recorder = self

        async def recording_receive(sock, *args, **kwargs):
            # Only the gateway's ingress uses Starlette WebSockets in this test.
            message = await receive(sock, *args, **kwargs)
            copy = json.loads(json.dumps(message))
            (recorder.hellos if "protocol" in copy else recorder.to_gateway).append(copy)
            return message

        async def recording_send(peer, message):
            recorder.to_agent.append(message)
            return await send(peer, message)

        monkeypatch.setattr(WebSocket, "receive_json", recording_receive)
        monkeypatch.setattr(federation.Peer, "send", recording_send)


async def _start_gateway(app):
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


MATRIX = [("legacy", "legacy"), ("current", "legacy"), ("current", "current"), ("legacy", "current")]


@pytest.mark.parametrize("agent_kind,gateway_kind", MATRIX,
                         ids=[f"{agent}-agent->{gateway}-gateway" for agent, gateway in MATRIX])
async def test_agent_gateway_matrix_baseline(tmp_path, monkeypatch, agent_kind, gateway_kind):
    cloud_module = package(gateway_kind, "cloud")
    federation = package(gateway_kind, "federation")
    agent_module = package(agent_kind, "agent")
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", AGENT_TOKEN)
    recorder = Recorder(federation, monkeypatch)
    app = cloud_module.build_cloud_app({"token_env": "SERVER_TOKEN", "agents": {AGENT_ID: "AGENT_TOKEN"}}, tmp_path)
    server, server_task, listener, port = await _start_gateway(app)
    workdir = (tmp_path / "project").resolve()
    workdir.mkdir()
    ready = asyncio.Event()
    cloud = {"agent_id": AGENT_ID, "url": f"ws://127.0.0.1:{port}", "token_env": "AGENT_TOKEN",
             "workspaces": {"project": {"path": str(workdir)}}, "refresh_interval_s": 60}
    agent = asyncio.create_task(agent_module.run_agent(tmp_path, cloud, transports={
        "workspace": device_server(AGENT_ID), "extra": extra_server()}, ready=ready))
    try:
        await asyncio.wait_for(ready.wait(), 20)
        async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=SERVER_TOKEN)) as client:
            listed = [tool.name for tool in await client.list_tools()]
            assert len(listed) == len(set(listed)), "direct tools must not be duplicated"
            assert set(listed) == federation.RESERVED_TOOLS | DEVICE_TOOLS

            workspaces = (await client.call_tool("workspace_list")).data["workspaces"]
            assert [(item["agent_id"], item["workspace"]) for item in workspaces] == [(AGENT_ID, "project")]

            selected = (await client.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project"})).data
            assert {"workspace_id", "agent_id", "workspace", "mcp"} <= set(selected)
            assert selected["agent_id"] == AGENT_ID and selected["mcp"] == "workspace"

            direct = await client.call_tool("execute", {"value": "x", "workspace_id": selected["workspace_id"]})
            assert direct.data == {"machine": AGENT_ID, "value": "x", "workdir": str(workdir)}
            image = await client.call_tool_mcp("picture", {"workspace_id": selected["workspace_id"]})
            assert image.content[0].type == "image" and image.structured_content == {"machine": AGENT_ID}
            failure = await client.call_tool_mcp("failure", {"workspace_id": selected["workspace_id"]})
            assert failure.is_error

            inventory = (await client.call_tool("mcplist")).data["mcps"]
            ids = [item["mcp_id"] for item in inventory]
            assert len(ids) == len(set(ids)), "one inventory entry per device MCP"
            assert set(ids) == {f"{AGENT_ID}/workspace", f"{AGENT_ID}/extra"}

            used = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/extra", "tool": "internal_action",
                                                      "arguments": {"number": 21}})
            assert used.data == {"answer": 42}
            routed = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/workspace", "tool": "execute",
                                                        "arguments": {"value": "y"}, "workspace_id": selected["workspace_id"]})
            assert routed.data["value"] == "y"
    finally:
        agent.cancel()
        await asyncio.gather(agent, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        listener.close()

    # Wire-level guarantees.
    assert len(recorder.hellos) == 1
    assert not any(message.get("method") == "hub_call" for message in recorder.to_gateway), \
        "an agent must never send hub_call to a gateway that did not announce it"
    assert all(message.get("method", "catalog") == "catalog" for message in recorder.to_gateway), \
        "everything but catalog refreshes is a reply"
    assert recorder.hellos[0]["protocol"] == 1 and recorder.hellos[0]["agent_id"] == AGENT_ID
    readies = [message for message in recorder.to_agent if message.get("method") == "ready"]
    assert len(readies) == 1
    if gateway_kind == "current":
        # M4 feature exchange: ready gains params.anx, which legacy agents ignore.
        anx = readies[0]["params"].pop("anx")
        assert anx["version"] and "hub_call/1" in anx["features"] and anx["display_name"]
    assert readies == [{"method": "ready", "params": {"agent_id": AGENT_ID}}]
    # A legacy agent is never sent a method it does not know (design §3.2).
    reverse = {message.get("method") for message in recorder.to_agent} - {"ready"}
    assert reverse <= {"tool_call", "tools_list", "list_tools", "media_get", "cancel"}, reverse

    raw_hello = json.loads(json.dumps(recorder.hellos[0]))
    if agent_kind == "current":
        # Additive keys (design §3.2, §5.2): protocol stays 1, legacy
        # gateways ignore both. Everything else must match the recording.
        anx = raw_hello.pop("anx")
        assert anx["version"] and {"hub_call/1", "mcp-alias/1"} <= set(anx["features"])
        device = raw_hello["catalog"].pop("device", None)
        assert device is None or device["contract"] == "anx.device/1"
    else:
        assert "anx" not in raw_hello and "device" not in raw_hello["catalog"]
    hello = normalise_hello(raw_hello, workdir)
    if gateway_kind == "legacy":
        if os.environ.get("ANX_RECORD_COMPAT_FIXTURES") == "1" and agent_kind == "legacy":
            FIXTURES.mkdir(exist_ok=True)
            HANDSHAKE_FIXTURE.write_text(json.dumps({"hello": hello, "ready": readies[0]}, indent=2, sort_keys=True) + "\n",
                                         encoding="utf-8", newline="\n")
        recorded = json.loads(HANDSHAKE_FIXTURE.read_text(encoding="utf-8"))
        assert hello == recorded["hello"]
        assert readies[0] == recorded["ready"]


def test_handshake_fixture_is_protocol_1_and_carries_no_paths_or_secrets():
    recorded = json.loads(HANDSHAKE_FIXTURE.read_text(encoding="utf-8"))
    assert recorded["hello"]["protocol"] == 1
    assert recorded["hello"]["catalog"]["workspaces"]["project"]["path"] == "<workdir>"
    text = HANDSHAKE_FIXTURE.read_text(encoding="utf-8")
    assert SERVER_TOKEN not in text and AGENT_TOKEN not in text and ":\\\\" not in text and "/Users/" not in text

"""The device plugin rename (``workspace`` -> ``device``) across old/new ends (design §1, §3.1, §3.3).

* new agent whose plugin is ``device`` -> legacy gateway (the rollback gate):
  ``workspace_list``/``set_workspace``/direct tools work, no duplicated tools,
  one ``mcplist`` entry per device, ``set_workspace`` reports ``mcp: "device"``,
  no ``hub_call``;
* legacy agent (plugin ``workspace``) -> new gateway: ``tooluse("x/device")``
  and ``tooluse("x/workspace")`` both reach it;
* new agent -> new gateway: both spellings reach ``device``.
"""
from __future__ import annotations

import asyncio

import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from test_compat_matrix import (AGENT_ID, AGENT_TOKEN, DEVICE_TOOLS, SERVER_TOKEN, Recorder, _start_gateway,
                                device_server, extra_server, package)

CASES = [("current", "legacy", "device"), ("legacy", "current", "workspace"), ("current", "current", "device")]


@pytest.mark.parametrize("agent_kind,gateway_kind,plugin", CASES,
                         ids=[f"{a}-agent({p})->{g}-gateway" for a, g, p in CASES])
async def test_device_plugin_name_is_interchangeable(tmp_path, monkeypatch, agent_kind, gateway_kind, plugin):
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
    # Workspaces written before the rename still say "workspace"; a new agent
    # resolves that to whichever of the two names it actually runs.
    cloud = {"agent_id": AGENT_ID, "url": f"ws://127.0.0.1:{port}", "token_env": "AGENT_TOKEN",
             "workspaces": {"project": {"path": str(workdir), "mcp": "workspace"}}, "refresh_interval_s": 60}
    agent = asyncio.create_task(agent_module.run_agent(tmp_path, cloud, transports={
        plugin: device_server(AGENT_ID), "extra": extra_server()}, ready=ready))
    try:
        await asyncio.wait_for(ready.wait(), 20)
        async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=SERVER_TOKEN)) as client:
            listed = [tool.name for tool in await client.list_tools()]
            assert len(listed) == len(set(listed)) and set(listed) == federation.RESERVED_TOOLS | DEVICE_TOOLS

            selected = (await client.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project"})).data
            assert selected["mcp"] == plugin
            handle = selected["workspace_id"]
            direct = await client.call_tool("execute", {"value": "x", "workspace_id": handle})
            assert direct.data == {"machine": AGENT_ID, "value": "x", "workdir": str(workdir)}

            ids = [item["mcp_id"] for item in (await client.call_tool("mcplist")).data["mcps"]]
            assert sorted(ids) == sorted({f"{AGENT_ID}/{plugin}", f"{AGENT_ID}/extra"})

            spellings = ["device", "workspace"] if gateway_kind == "current" else [plugin]
            for name in spellings:
                routed = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/{name}", "tool": "execute",
                                                            "arguments": {"value": name}, "workspace_id": handle})
                assert routed.data["value"] == name
    finally:
        agent.cancel()
        await asyncio.gather(agent, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(server_task, 10)
        listener.close()

    assert recorder.hellos[0]["protocol"] == 1
    assert recorder.hellos[0]["catalog"]["workspaces"]["project"]["mcp"] == plugin
    assert not any(message.get("method") == "hub_call" for message in recorder.to_gateway)


async def test_new_agent_media_get_accepts_the_former_name(tmp_path):
    agent_module = package("current", "agent")
    gateway = agent_module.LocalGateway(tmp_path, {"workspaces": {}}, transports={"device": device_server(AGENT_ID)})
    gateway.targets = gateway.configured_targets()
    gateway.media_endpoints["device"] = "http://127.0.0.1:9"  # refused: proves routing, not fetching
    assert gateway.resolve_mcp("workspace") == "device"
    result = await gateway.dispatch("media_get", {"mcp": "workspace", "token": "t" * 43})
    assert result == {"status": 502}
    assert await gateway.dispatch("media_get", {"mcp": "nothing", "token": "t" * 43}) == {"status": 404}

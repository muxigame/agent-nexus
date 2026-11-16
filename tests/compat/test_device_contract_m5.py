"""Milestone M5: the anx.device/1 contract through the agent and the gateway (design §5.2-§5.4, §13).

* the agent publishes the plugin's ``device_info`` as the catalog ``device``
  block (degraded fallback when it fails) and announces ``device-contract/1``;
* the gateway names the missing capability (with platform and reason) when a
  device lacks a tool, hides internal tools from tools/list and toolsearch, and
  exposes only tools of declared capabilities;
* ``tooluse("x/workspace", "exec_command")`` routes through the alias, and
  ``media_get`` with the legacy ``mcp: "workspace"`` still fetches the file
  (legacy gateway and new gateway);
* ``notify(actions=[...], dry_run=true)`` passes the gateway's schema check on a
  device announcing ``toast_actions/1``;
* ``anx device conformance <url>`` passes a conformant device and fails one
  without annotations or ``_meta``.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import socket
from typing import Literal

import httpx
import pytest
import uvicorn
from fastmcp import FastMCP
from mcp_types import ToolAnnotations
from pydantic import BaseModel, ConfigDict, Field
from starlette.applications import Starlette
from starlette.responses import Response
from starlette.routing import Route

from test_compat_matrix import AGENT_ID, SERVER_TOKEN, _start_gateway, device_server, extra_server, package
from test_gateway_m4 import HOST_A, SECOND_ID, mcp_client, running_gateway

READ = ToolAnnotations(read_only_hint=True, open_world_hint=False)
WRITE = ToolAnnotations(read_only_hint=False, destructive_hint=False, open_world_hint=True)
MEDIA_TOKEN = "m" * 43
MEDIA_BYTES = b"\x89PNG-m5-media"


class Action(BaseModel):
    model_config = ConfigDict(extra="forbid")
    id: str = Field(min_length=1, max_length=64, pattern=r"^[A-Za-z0-9_.:-]+$")
    label: str = Field(min_length=1, max_length=64)
    link: str | None = None


def contract_device(machine: str, platform: str = "windows", *, info_fails: bool = False,
                    tagged: bool = True, annotated: bool = True) -> FastMCP:
    """A stand-in device plugin that implements anx.device/1 (core + exec, plus screen on windows)."""
    server = FastMCP(f"device-{machine}")
    capabilities = {"info": {"v": 1}, "notify": {"v": 1, "actions": True}, "files": {"v": 1}, "time": {"v": 1},
                    "exec": {"v": 1}}
    if platform == "windows":
        capabilities["screen"] = {"v": 1, "available": True, "reason": None}
    else:
        capabilities["screen"] = {"v": 1, "available": False,
                                  "reason": f"screen control is implemented on Windows only (platform={platform})"}

    def tool(name: str, capability: str, annotations=READ, **meta):
        kwargs = {"name": name}
        if annotated:
            kwargs["annotations"] = annotations
        if tagged:
            kwargs["meta"] = {"anx/capability": capability, "anx/contract": "anx.device/1", **meta}
        elif meta:
            kwargs["meta"] = meta
        return server.tool(**kwargs)

    @tool("device_info", "info")
    async def device_info() -> dict:
        if info_fails:
            raise RuntimeError("device_info is broken")
        return {"contract": "anx.device/1", "hostname": f"{machine}-plugin-host", "profile": "full",
                "form": "desktop", "platform": platform, "os": "test", "arch": "x86_64", "reachable": "always",
                "plugin_version": "test", "capabilities": capabilities,
                "agent_runtimes": {"claude": {"available": False}, "codex": {"available": False},
                                   "companion": {"available": False}}}

    @tool("notify", "notify", WRITE)
    async def notify(title: str, body: str = "", level: Literal["info", "warn", "urgent"] = "info",
                     actions: list[Action] | None = None, dry_run: bool = False) -> dict:
        return {"status": "ok", "dry_run": dry_run, "shown": not dry_run, "actions": len(actions or [])}

    for name in ("file_list", "file_read", "file_write", "file_stat"):
        @tool(name, "files")
        async def files(path: str = "") -> dict:
            return {"status": "ok", "path": path}

    @tool("curr_time", "time")
    async def curr_time() -> dict:
        return {"current_time": "2026-11-10 00:00:00 UTC"}

    @tool("sleep", "time")
    async def sleep(duration_ms: int = 1) -> dict:
        return {"status": "ok"}

    for name in ("write_stdin", "exec_kill", "exec_list"):
        @tool(name, "exec", WRITE)
        async def exec_other(session_id: int = 0) -> dict:
            return {"status": "ok"}

    @tool("exec_command", "exec", WRITE)
    async def exec_command(cmd: str, workdir: str | None = None) -> dict:
        return {"machine": machine, "cmd": cmd, "workdir": workdir}

    if platform == "windows":
        for name in ("list_screens", "screenshot", "vision"):
            @tool(name, "screen")
            async def screen(scale: float = 1.0) -> dict:
                return {"machine": machine, "screen": True}

    @tool("session_identity", "info", **{"anx/internal": True})
    async def session_identity() -> dict:
        return {"machine": machine, "internal": True}

    return server


async def start_contract_agent(tmp_path, port, agents, *, agent_id=AGENT_ID, token_env="AGENT_TOKEN",
                               platform="windows", plugin="device", hostname=HOST_A, kind="current", **device):
    agent_module = package(kind, "agent")
    workdir = (tmp_path / f"{agent_id}-project").resolve()
    workdir.mkdir(exist_ok=True)
    cloud = {"agent_id": agent_id, "url": f"ws://127.0.0.1:{port}", "token_env": token_env,
             "workspaces": {"project": {"path": str(workdir), "mcp": plugin}}, "refresh_interval_s": 60}
    if kind == "current" and hostname is not None:
        cloud["hostname"] = hostname
    ready = asyncio.Event()
    agents.append(asyncio.create_task(agent_module.run_agent(tmp_path, cloud, ready=ready, transports={
        plugin: contract_device(agent_id, platform, **device), "extra": extra_server()})))
    await asyncio.wait_for(ready.wait(), 20)
    return workdir


# -- the device block --------------------------------------------------------------
async def test_agent_publishes_device_info_as_the_catalog_device_block(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch, recorder=True) as (app, port, agents, record):
        await start_contract_agent(tmp_path, port, agents)
        hello = record.hellos[0]
        assert "device-contract/1" in hello["anx"]["features"]
        block = hello["catalog"]["device"]
        assert block["contract"] == "anx.device/1" and "degraded" not in block
        assert block["hostname"] == HOST_A  # cloud.hostname overrides the plugin's report
        assert block["id"] == AGENT_ID and block["mcp"] == "device" and block["platform"] == "windows"
        assert block["agent_version"] == package("current", "features").__version__
        assert set(block["capabilities"]) == {"info", "notify", "files", "time", "exec", "screen"}
        assert package("current", "device_contract").validate(block) == []
        async with mcp_client(port) as client:
            devices = (await client.call_tool("device_list", {"capability": "screen"})).data["devices"]
            assert [d["device_id"] for d in devices] == [AGENT_ID]
            assert devices[0]["platform"] == "windows" and devices[0]["capabilities"]["notify"]["actions"] is True


async def test_plugin_hostname_is_used_without_an_override(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch, recorder=True) as (app, port, agents, record):
        await start_contract_agent(tmp_path, port, agents, hostname=None)
        assert record.hellos[0]["catalog"]["device"]["hostname"] == f"{AGENT_ID}-plugin-host"


async def test_failing_device_info_falls_back_to_a_degraded_block(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch, recorder=True) as (app, port, agents, record):
        await start_contract_agent(tmp_path, port, agents, info_fails=True)
        block = record.hellos[0]["catalog"]["device"]
        assert block["degraded"] is True and block["capabilities"] == {"info": {"v": 1}}
        assert block["hostname"] == HOST_A
        # A degraded device is not filtered: its tools stay exposed.
        async with mcp_client(port) as client:
            assert "screenshot" in {tool.name for tool in await client.list_tools()}


# -- gateway exposure and errors --------------------------------------------------------
async def test_missing_capability_error_names_capability_platform_and_reason(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start_contract_agent(tmp_path, port, agents)
        await start_contract_agent(tmp_path, port, agents, agent_id=SECOND_ID, token_env="SECOND_TOKEN",
                                   platform="linux", hostname="host-linux")
        async with mcp_client(port) as client:
            # Before a device is chosen tools/list is the union; screenshot comes from the Windows device.
            assert "screenshot" in {tool.name for tool in await client.list_tools()}
            selected = (await client.call_tool("use_device", {"device_id": SECOND_ID, "workdir": "project"})).data
            handle = selected["workspace_id"]
            direct = await client.call_tool("screenshot", {"workspace_id": handle}, raise_on_error=False)
            text = direct.content[0].text
            assert direct.is_error and "lacks capability 'screen'" in text and "platform=linux" in text
            assert "Windows only" in text and SECOND_ID in text
            routed = await client.call_tool("tooluse", {"mcp_id": f"{SECOND_ID}/device", "tool": "screenshot",
                                                        "arguments": {}}, raise_on_error=False)
            assert routed.is_error and "lacks capability 'screen'" in routed.content[0].text
            # An unknown tool that belongs to no capability is still just unknown.
            unknown = await client.call_tool("tooluse", {"mcp_id": f"{SECOND_ID}/device", "tool": "frobnicate",
                                                         "arguments": {}}, raise_on_error=False)
            assert unknown.is_error and "unknown tool" in unknown.content[0].text
            # The Windows device still serves it.
            windows = (await client.call_tool("use_device", {"device_id": AGENT_ID, "workdir": "project"})).data
            shot = await client.call_tool("screenshot", {"workspace_id": windows["workspace_id"]})
            assert shot.data == {"machine": AGENT_ID, "screen": True}
            # Only devices with an available capability are listed for it.
            listed = (await client.call_tool("device_list", {"capability": "screen"})).data["devices"]
            assert [d["device_id"] for d in listed] == [AGENT_ID]


async def test_internal_tools_are_not_listed_or_searchable_but_callable(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start_contract_agent(tmp_path, port, agents)
        async with mcp_client(port) as client:
            listed = {tool.name: tool for tool in await client.list_tools()}
            assert "session_identity" not in listed
            assert listed["exec_command"].meta["anx/capability"] == "exec"
            found = (await client.call_tool("toolsearch", {"query": "session identity"})).data["tools"]
            assert all(item["name"] != "session_identity" for item in found)
            routed = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/device", "tool": "session_identity",
                                                        "arguments": {}})
            assert routed.data == {"machine": AGENT_ID, "internal": True}


def test_undeclared_capability_tools_are_not_exposed():
    gateway_module = package("current", "gateway")
    peer = type("P", (), {"catalog": {"device": {"contract": "anx.device/1", "capabilities": {"exec": {"v": 1}}}}})()
    exposed = gateway_module.DeviceProvider.exposed
    assert exposed(peer, {"name": "exec_command", "_meta": {"anx/capability": "exec"}})
    assert not exposed(peer, {"name": "screenshot", "_meta": {"anx/capability": "screen"}})
    assert exposed(peer, {"name": "untagged"})
    assert not exposed(peer, {"name": "set_workspace"})
    legacy = type("P", (), {"catalog": {}})()
    assert exposed(legacy, {"name": "screenshot", "_meta": {"anx/capability": "screen"}})


# -- the workspace alias ------------------------------------------------------------------
@pytest.mark.parametrize("agent_kind,plugin,spelling", [("current", "device", "workspace"),
                                                        ("legacy", "workspace", "device")])
async def test_tooluse_exec_command_through_the_alias(tmp_path, monkeypatch, agent_kind, plugin, spelling):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        workdir = await start_contract_agent(tmp_path, port, agents, kind=agent_kind, plugin=plugin)
        async with mcp_client(port) as client:
            handle = (await client.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project"})).data
            routed = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/{spelling}", "tool": "exec_command",
                                                        "arguments": {"cmd": "echo hi"},
                                                        "workspace_id": handle["workspace_id"]})
            assert routed.data == {"machine": AGENT_ID, "cmd": "echo hi", "workdir": str(workdir)}


@contextlib.asynccontextmanager
async def media_endpoint():
    async def serve(request):
        if request.path_params["token"] != MEDIA_TOKEN:
            return Response("Not found", status_code=404)
        return Response(MEDIA_BYTES, media_type="image/png")

    app = Starlette(routes=[Route("/media/{token}", serve)])
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    try:
        yield f"http://127.0.0.1:{port}"
    finally:
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.parametrize("gateway_kind", ["legacy", "current"])
async def test_media_get_with_the_legacy_workspace_name_reaches_the_device(tmp_path, monkeypatch, gateway_kind):
    agent_module = package("current", "agent")
    async with media_endpoint() as endpoint:
        original = agent_module.LocalGateway.configured_targets

        def with_media(self):
            targets = original(self)
            self.media_endpoints["device"] = endpoint  # injected transports have no HTTP endpoint
            return targets

        monkeypatch.setattr(agent_module.LocalGateway, "configured_targets", with_media)
        asked = []
        dispatch = agent_module.LocalGateway._dispatch

        async def recording(self, method, params):
            if method == "media_get":
                asked.append(params.get("mcp"))
            return await dispatch(self, method, params)

        monkeypatch.setattr(agent_module.LocalGateway, "_dispatch", recording)
        async with running_gateway(tmp_path, monkeypatch, kind=gateway_kind) as (app, port, agents, _):
            await start_contract_agent(tmp_path, port, agents)
            async with httpx.AsyncClient(trust_env=False) as http:
                response = await http.get(f"http://127.0.0.1:{port}/media/{MEDIA_TOKEN}")
                missing = await http.get(f"http://127.0.0.1:{port}/media/{'n' * 43}")
            assert response.status_code == 200 and response.content == MEDIA_BYTES
            assert missing.status_code == 404
            # The legacy gateway hard-codes "workspace"; the new one asks for the contract's name.
            assert asked and set(asked) == ({"workspace"} if gateway_kind == "legacy" else {"device"})


# -- notify with actions --------------------------------------------------------------------
async def test_notify_with_actions_passes_the_gateway_schema_check(tmp_path, monkeypatch):
    features = package("current", "features")
    monkeypatch.setattr(features, "AGENT_FEATURES", (*features.AGENT_FEATURES, "toast_actions/1"))
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start_contract_agent(tmp_path, port, agents)
        assert "toast_actions/1" in app.state.hub.peers[AGENT_ID].features
        async with mcp_client(port) as client:
            arguments = {"title": "Deploy?", "level": "urgent", "dry_run": True,
                         "actions": [{"id": "yes", "label": "Yes"}, {"id": "no", "label": "No",
                                                                      "link": "agentnexus://inbox"}]}
            routed = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/device", "tool": "notify",
                                                        "arguments": arguments})
            assert routed.data == {"status": "ok", "dry_run": True, "shown": False, "actions": 2}
            bad = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/device", "tool": "notify",
                                                     "arguments": {"title": "x", "actions": [{"id": "yes"}]}},
                                         raise_on_error=False)
            assert bad.is_error and "invalid tool arguments" in bad.content[0].text


# -- anx device conformance -------------------------------------------------------------------
@contextlib.asynccontextmanager
async def serve_mcp(server: FastMCP):
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    listener.listen()
    port = listener.getsockname()[1]
    app = server.http_app(path="/mcp")
    uv = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(uv.serve(sockets=[listener]))
    for _ in range(200):
        if uv.started:
            break
        await asyncio.sleep(.05)
    try:
        yield f"http://127.0.0.1:{port}/mcp"
    finally:
        uv.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


@pytest.mark.parametrize("platform", ["windows", "linux"])
async def test_conformance_cli_passes_a_conformant_device(platform, capsys):
    cli = package("current", "cli")
    async with serve_mcp(contract_device("pc-test", platform)) as url:
        code = await asyncio.to_thread(cli.main, ["device", "conformance", url])
    out = capsys.readouterr().out
    assert code == 0, out
    assert f"conformance: passed (platform={platform})" in out and "FAIL" not in out


async def test_conformance_cli_fails_a_device_without_meta_or_annotations(capsys):
    cli = package("current", "cli")
    async with serve_mcp(contract_device("pc-test", tagged=False, annotated=False)) as url:
        code = await asyncio.to_thread(cli.main, ["device", "conformance", url, "--json"])
    out = capsys.readouterr().out
    assert code == 1
    report = __import__("json").loads(out)
    failed = {item["check"] for item in report["checks"] if not item["ok"]}
    assert {"every tool has annotations", "every tool has _meta anx/capability and anx/contract",
            "declared capabilities have their tools"} <= failed


async def test_conformance_cli_reports_an_unreachable_endpoint(capsys):
    cli = package("current", "cli")
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))
    port = listener.getsockname()[1]
    listener.close()  # nothing listens here now
    code = await asyncio.to_thread(cli.main, ["device", "conformance", f"http://127.0.0.1:{port}/mcp"])
    assert code == 2 and "cannot check" in capsys.readouterr().err


def test_contract_schema_ships_with_the_wheel_build_hook():
    from pathlib import Path
    hooks = (Path(__file__).resolve().parents[2] / "build_hooks.py").read_text(encoding="utf-8")
    assert '"agent_nexus/schemas/"' in hooks
    contract = package("current", "device_contract")
    assert contract.schema()["properties"]["contract"]["const"] == "anx.device/1"
    assert contract.core_capabilities() == ("info", "notify", "files", "time")

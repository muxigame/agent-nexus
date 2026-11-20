"""Milestone M4: gateway evolution (design §3, §5.5, §13 M4, decision D6).

* feature exchange in hello/ready with ``protocol`` still 1, in all four
  agent/gateway pairings; legacy ends are never sent anything new and a new
  agent never sends ``hub_call`` to a gateway that did not announce it;
* ``device_list`` / ``use_device`` beside ``workspace_list`` / ``set_workspace``,
  sharing one implementation and one kind of handle; ``legacy_tools`` switch;
* handles persisted in ``gateway.sqlite3``: still valid after a restart;
* display name = alias or self-reported hostname or agent_id, the alias set
  through the gateway API and through ``hub_call device_admin``;
* rule-based snapshot of the routing tools' schemas against the legacy gateway;
* ``--check-config`` accepts legacy configs and only warns on unknown keys.
"""
from __future__ import annotations

import asyncio
import contextlib
import json
import os
from pathlib import Path

import httpx
import pytest
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport
from mcpkit.config import ConfigError

from test_compat_matrix import (AGENT_ID, AGENT_TOKEN, SERVER_TOKEN, Recorder, _start_gateway, device_server,
                                extra_server, package)

FIXTURES = Path(__file__).resolve().parent / "fixtures"
SCHEMA_SNAPSHOT = Path(__file__).resolve().parent / "tool_schemas.json"
SECOND_ID, SECOND_TOKEN = "remote-1", "r" * 40
#: Placeholder hostnames only (design: no real host names anywhere).
HOST_A, HOST_B = "host-alpha", "host-beta"


@contextlib.asynccontextmanager
async def running_gateway(tmp_path, monkeypatch, kind="current", config_extra=None, recorder=False):
    cloud_module = package(kind, "cloud")
    federation = package(kind, "federation")
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", AGENT_TOKEN)
    monkeypatch.setenv("SECOND_TOKEN", SECOND_TOKEN)
    record = Recorder(federation, monkeypatch) if recorder else None
    config = {"token_env": "SERVER_TOKEN", "agents": {AGENT_ID: "AGENT_TOKEN", SECOND_ID: "SECOND_TOKEN"},
              **(config_extra or {})}
    app = cloud_module.build_cloud_app(config, tmp_path)
    server, task, listener, port = await _start_gateway(app)
    agents: list[asyncio.Task] = []
    try:
        yield app, port, agents, record
    finally:
        for agent in agents:
            agent.cancel()
        await asyncio.gather(*agents, return_exceptions=True)
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


async def start_agent(tmp_path, port, agents, *, kind="current", agent_id=AGENT_ID, token_env="AGENT_TOKEN",
                      hostname=None, status=None, hub=None):
    agent_module = package(kind, "agent")
    workdir = (tmp_path / f"{agent_id}-project").resolve()
    workdir.mkdir(exist_ok=True)
    cloud = {"agent_id": agent_id, "url": f"ws://127.0.0.1:{port}", "token_env": token_env,
             "workspaces": {"project": {"path": str(workdir)}}, "refresh_interval_s": 60}
    if kind == "current":
        cloud["hostname"] = hostname if hostname is not None else ""
    ready = asyncio.Event()
    kwargs = {"transports": {"workspace": device_server(agent_id), "extra": extra_server()}, "ready": ready}
    if kind == "current":
        kwargs.update(status=status if status is not None else {}, hub=hub)
    agents.append(asyncio.create_task(agent_module.run_agent(tmp_path, cloud, **kwargs)))
    await asyncio.wait_for(ready.wait(), 20)
    return workdir


def mcp_client(port):
    return Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=SERVER_TOKEN))


# -- feature exchange ------------------------------------------------------

async def test_new_agent_and_new_gateway_exchange_features(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    hub, status = HubLink(), {}
    async with running_gateway(tmp_path, monkeypatch, recorder=True) as (app, port, agents, recorder):
        await start_agent(tmp_path, port, agents, hostname=HOST_A, status=status, hub=hub)
        peer = app.state.hub.peers[AGENT_ID]
        assert {"hub_call/1", "mcp-alias/1"} <= peer.features and peer.anx_version
        assert hub.available and "hub_call/1" in hub.features
        assert status["gateway_features"] == sorted(hub.features) and status["display_name"] == HOST_A
        listed = await hub.call("device_admin", {"op": "list"})
        assert [device["device_id"] for device in listed["devices"]] == [AGENT_ID]
        with pytest.raises(Exception, match="unsupported hub_call op"):
            await hub.call("relay_report", {})  # M9; wake_report arrived with M7
    hello = recorder.hellos[0]
    assert hello["protocol"] == 1 and hello["anx"]["features"]
    ready = next(message for message in recorder.to_agent if message.get("method") == "ready")
    assert ready["params"]["anx"]["display_name"] == HOST_A
    assert [message["method"] for message in recorder.to_gateway if "method" in message].count("hub_call") == 2


async def test_legacy_agent_gets_no_features_and_no_new_methods(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch, recorder=True) as (app, port, agents, recorder):
        await start_agent(tmp_path, port, agents, kind="legacy")
        assert app.state.hub.peers[AGENT_ID].features == frozenset()
        async with mcp_client(port) as client:
            devices = (await client.call_tool("device_list")).data["devices"]
            # A legacy agent reports no hostname: its id is its display name.
            assert [(d["device_id"], d["display_name"], d["features"]) for d in devices] == [(AGENT_ID, AGENT_ID, [])]
            selected = (await client.call_tool("use_device", {"device_id": AGENT_ID})).data
            assert selected["mcp"] == "workspace" and selected["workspace"] == "project"
            assert (await client.call_tool("execute", {"value": "x", "workspace_id": selected["workspace_id"]})).data["value"] == "x"
    sent = {message.get("method") for message in recorder.to_agent}
    assert sent <= {"ready", "tool_call", "media_get", "cancel"}, sent


async def test_new_agent_never_sends_hub_call_to_legacy_gateway(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    from agent_nexus.federation import RoutingError
    hub, status = HubLink(), {}
    async with running_gateway(tmp_path, monkeypatch, kind="legacy", recorder=True) as (app, port, agents, recorder):
        await start_agent(tmp_path, port, agents, hostname=HOST_A, status=status, hub=hub)
        assert not hub.available and status["gateway_features"] == []
        with pytest.raises(RoutingError, match="does not support hub_call"):
            await hub.call("device_admin", {"op": "list"})
        async with mcp_client(port) as client:
            # The legacy gateway ignores the extra catalog/hello keys.
            assert [item["agent_id"] for item in (await client.call_tool("workspace_list")).data["workspaces"]] == [AGENT_ID]
    assert not any(message.get("method") == "hub_call" for message in recorder.to_gateway)


# -- device tools, display names and aliases (decision D6) -----------------

async def test_display_names_aliases_and_device_resolution(tmp_path, monkeypatch):
    from agent_nexus.agent import HubLink
    hub = HubLink()
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        workdir = await start_agent(tmp_path, port, agents, hostname=HOST_A, hub=hub)
        await start_agent(tmp_path, port, agents, agent_id=SECOND_ID, token_env="SECOND_TOKEN", hostname=HOST_B)
        async with mcp_client(port) as client:
            names = {tool.name for tool in await client.list_tools()}
            assert {"device_list", "use_device", "workspace_list", "set_workspace"} <= names

            devices = {d["device_id"]: d for d in (await client.call_tool("device_list")).data["devices"]}
            assert {k: (d["display_name"], d["hostname"], d["alias"]) for k, d in devices.items()} == {
                AGENT_ID: (HOST_A, HOST_A, None), SECOND_ID: (HOST_B, HOST_B, None)}
            assert devices[AGENT_ID]["workdirs"] == [{"name": "project", "path": str(workdir)}]
            legacy_view = (await client.call_tool("workspace_list")).data["workspaces"]
            assert {(item["agent_id"], item["display_name"], item["online"]) for item in legacy_view} == {
                (AGENT_ID, HOST_A, True), (SECOND_ID, HOST_B, True)}
            assert all({"path", "mcp", "device_form"} <= set(item) for item in legacy_view)

            # Alias through the gateway API (owner credentials = MCP credentials).
            async with httpx.AsyncClient(base_url=f"http://127.0.0.1:{port}") as http:
                assert (await http.put(f"/api/devices/{AGENT_ID}", json={"alias": "Office"})).status_code == 401
                headers = {"Authorization": f"Bearer {SERVER_TOKEN}"}
                response = await http.put(f"/api/devices/{AGENT_ID}", json={"alias": "Office"}, headers=headers)
                assert response.status_code == 200 and response.json()["device"]["display_name"] == "Office"
                assert (await http.put("/api/devices/nobody", json={"alias": "x"}, headers=headers)).status_code == 404
                assert (await http.put(f"/api/devices/{AGENT_ID}", json={"alias": "x" * 65},
                                       headers=headers)).status_code == 400
                listed = (await http.get("/api/devices", headers=headers)).json()["devices"]
                assert {d["device_id"]: d["display_name"] for d in listed} == {AGENT_ID: "Office", SECOND_ID: HOST_B}

            # Alias through hub_call device_admin (the local console's path).
            changed = await hub.call("device_admin", {"op": "set_alias", "device_id": SECOND_ID, "alias": "Lab box"})
            assert changed["device"]["display_name"] == "Lab box" and changed["device"]["device_id"] == SECOND_ID

            devices = {d["device_id"]: d["display_name"] for d in (await client.call_tool("device_list")).data["devices"]}
            assert devices == {AGENT_ID: "Office", SECOND_ID: "Lab box"}
            legacy_view = (await client.call_tool("workspace_list")).data["workspaces"]
            assert {(item["agent_id"], item["display_name"]) for item in legacy_view} == {
                (AGENT_ID, "Office"), (SECOND_ID, "Lab box")}

            # use_device by alias routes; agent_id stays the routing key.
            selected = (await client.call_tool("use_device", {"device_id": "lab box", "workdir": "project",
                                                              "session_name": "claude-main", "kind": "claude"})).data
            assert selected["agent_id"] == selected["device_id"] == SECOND_ID
            assert selected["display_name"] == "Lab box" and selected["device_display"] == f"Lab box ({SECOND_ID})"
            assert selected["workspace_id"] in selected["hint"]
            direct = await client.call_tool("execute", {"value": "v", "workspace_id": selected["workspace_id"]})
            assert direct.data["machine"] == SECOND_ID
            by_legacy = (await client.call_tool("set_workspace", {"agent_id": "Office", "workspace": "project"})).data
            assert by_legacy["agent_id"] == AGENT_ID and by_legacy["display_name"] == "Office"
            assert by_legacy["mcp"] == "workspace"

            # Same alias on two devices: by-name calls fail and list the candidates.
            await hub.call("device_admin", {"op": "set_alias", "device_id": AGENT_ID, "alias": "Twin"})
            await hub.call("device_admin", {"op": "set_alias", "device_id": SECOND_ID, "alias": "Twin"})
            ambiguous = await client.call_tool_mcp("use_device", {"device_id": "Twin"})
            assert ambiguous.is_error
            text = ambiguous.content[0].text
            assert "ambiguous" in text and f"Twin ({AGENT_ID})" in text and f"Twin ({SECOND_ID})" in text
            assert (await client.call_tool("use_device", {"device_id": AGENT_ID})).data["agent_id"] == AGENT_ID

            # Clearing an alias falls back to the hostname; ready carries it on reconnect.
            await hub.call("device_admin", {"op": "set_alias", "device_id": AGENT_ID, "alias": None})
            assert app.state.gateway.devices.display_name(AGENT_ID) == HOST_A

            invalid = await client.call_tool_mcp("use_device", {"device_id": AGENT_ID, "kind": "robot"})
            assert invalid.is_error


async def test_offline_devices_are_listed_on_request(tmp_path, monkeypatch):
    async with running_gateway(tmp_path, monkeypatch) as (app, port, agents, _):
        await start_agent(tmp_path, port, agents, hostname=HOST_A)
        await start_agent(tmp_path, port, agents, agent_id=SECOND_ID, token_env="SECOND_TOKEN", hostname=HOST_B)
        agents[1].cancel()
        await asyncio.gather(agents[1], return_exceptions=True)
        for _ in range(200):
            if SECOND_ID not in app.state.hub.peers:
                break
            await asyncio.sleep(.02)
        async with mcp_client(port) as client:
            online = (await client.call_tool("device_list")).data["devices"]
            assert [d["device_id"] for d in online] == [AGENT_ID]
            everything = (await client.call_tool("device_list", {"include_offline": True})).data["devices"]
            assert {(d["device_id"], d["online"], d["display_name"]) for d in everything} == {
                (AGENT_ID, True, HOST_A), (SECOND_ID, False, HOST_B)}


# -- persisted handles -----------------------------------------------------

class FakePeer:
    def __init__(self, agent_id, catalog):
        self.agent_id, self.catalog, self.features = agent_id, catalog, frozenset()
        self.calls = []

    async def call(self, method, params):
        self.calls.append((method, params))
        return {"content": [{"type": "text", "text": json.dumps(params["arguments"])}],
                "structuredContent": {"workspace": params["workspace"], "scope": params["scope"]}}


def fake_catalog(tmp_path):
    tool = {"name": "execute", "inputSchema": {"type": "object", "properties": {"value": {"type": "string"}}}}
    return {"workspaces": {"project": {"path": str(tmp_path), "mcp": "device"}},
            "mcps": {"device": {"description": "", "tools": [tool]}},
            "device": {"contract": "anx.device/1", "hostname": HOST_A}}


def build(tmp_path, monkeypatch, **extra):
    from agent_nexus.cloud import build_cloud_app
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", AGENT_TOKEN)
    app = build_cloud_app({"token_env": "SERVER_TOKEN", "agents": {AGENT_ID: "AGENT_TOKEN"}, **extra}, tmp_path)
    peer = FakePeer(AGENT_ID, fake_catalog(tmp_path))
    app.state.hub.peers[AGENT_ID] = peer
    app.state.store.device_connected(AGENT_ID, hostname=HOST_A, features=[], device=None)
    return app, peer


async def test_workspace_id_survives_a_gateway_restart(tmp_path, monkeypatch):
    first, _ = build(tmp_path, monkeypatch)
    async with Client(first.state.mcp) as client:
        legacy = (await client.call_tool("set_workspace", {"agent_id": AGENT_ID, "workspace": "project"})).data
        modern = (await client.call_tool("use_device", {"device_id": HOST_A})).data
        await client.call_tool("use_device", {"device_id": AGENT_ID})
    first.state.store.close()
    assert (tmp_path / "gateway.sqlite3").is_file()

    second, peer = build(tmp_path, monkeypatch)  # a new process: nothing in memory
    assert second.state.store.live_handle_count() == 3
    async with Client(second.state.mcp) as client:
        for handle in (legacy["workspace_id"], modern["workspace_id"]):
            result = await client.call_tool("execute", {"value": "after-restart", "workspace_id": handle})
            assert result.structured_content == {"workspace": "project", "scope": handle}
            routed = await client.call_tool("tooluse", {"mcp_id": f"{AGENT_ID}/workspace", "tool": "execute",
                                                        "arguments": {"value": "y"}, "workspace_id": handle})
            assert routed.structured_content["scope"] == handle
        unknown = await client.call_tool_mcp("execute", {"value": "x", "workspace_id": "0" * 32})
        assert unknown.is_error and "unknown or expired" in unknown.content[0].text
    second.state.store.close()


async def test_handles_expire_after_the_sliding_ttl(tmp_path):
    from agent_nexus.store import HANDLE_TTL_S, GatewayStore
    now = [1000.0]
    store = GatewayStore(tmp_path / "gateway.sqlite3", clock=lambda: now[0])
    store.put_handle("h1", AGENT_ID, "project")
    now[0] += HANDLE_TTL_S - 10
    assert store.touch_handle("h1")["agent_id"] == AGENT_ID  # use extends the lifetime
    now[0] += HANDLE_TTL_S - 10
    assert store.touch_handle("h1") is not None
    now[0] += HANDLE_TTL_S + 1
    assert store.touch_handle("h1") is None and store.live_handle_count() == 0
    from agent_nexus.store import MIGRATIONS
    assert store.schema_version() == len(MIGRATIONS)  # M6 added the mailbox tables
    store.close()


def test_state_database_location_and_lazy_open(tmp_path):
    from agent_nexus.cloud import state_db_path
    assert state_db_path({"mailbox": {"db": str(tmp_path / "x" / "g.sqlite3")}}, tmp_path) == (tmp_path / "x" / "g.sqlite3").resolve()
    assert state_db_path({"state_dir": "state"}, tmp_path) == (tmp_path / "state" / "gateway.sqlite3").resolve()
    assert state_db_path({"auth": {"store": str(tmp_path / "lib" / "oauth.sqlite3")}}, tmp_path) == \
        (tmp_path / "lib" / "gateway.sqlite3").resolve()
    assert state_db_path({}, tmp_path) == (tmp_path / "gateway.sqlite3").resolve()


async def test_unopenable_state_database_falls_back_to_memory(tmp_path):
    from agent_nexus.store import GatewayStore
    blocker = tmp_path / "file"
    blocker.write_text("not a directory")
    store = GatewayStore(blocker / "gateway.sqlite3")
    store.put_handle("h", AGENT_ID, "project")
    assert store.touch_handle("h") is not None and not store.persistent


# -- legacy_tools switch ---------------------------------------------------

@pytest.mark.parametrize("mode", ["listed", "hidden", "off"])
async def test_legacy_tools_switch(tmp_path, monkeypatch, mode):
    app, _ = build(tmp_path, monkeypatch, gateway={"legacy_tools": mode})
    async with Client(app.state.mcp) as client:
        names = {tool.name for tool in await client.list_tools()}
        assert {"device_list", "use_device"} <= names
        assert ({"workspace_list", "set_workspace"} <= names) == (mode == "listed")
        called = await client.call_tool_mcp("set_workspace", {"agent_id": AGENT_ID, "workspace": "project"})
        assert called.is_error == (mode == "off")
        listed = await client.call_tool_mcp("workspace_list", {})
        assert listed.is_error == (mode == "off")
    app.state.store.close()


def test_invalid_legacy_tools_value_is_a_config_error(tmp_path, monkeypatch):
    with pytest.raises(ConfigError, match="legacy_tools"):
        build(tmp_path, monkeypatch, gateway={"legacy_tools": "maybe"})


# -- schemas cached by existing ChatGPT/Claude connectors (§3.3) -----------

def _shape(schema):
    if isinstance(schema, dict):
        return {key: _shape(value) for key, value in schema.items() if key not in ("title", "description")}
    if isinstance(schema, list):
        return [_shape(item) for item in schema]
    return schema


async def _schemas(gateway):
    async with Client(gateway.build()) as client:
        return {tool.name: _shape(tool.input_schema) for tool in await client.list_tools()}


async def test_routing_tool_schemas_only_grow_optional_properties():
    legacy_gateway, legacy_federation = package("legacy", "gateway"), package("legacy", "federation")
    if os.environ.get("ANX_RECORD_COMPAT_FIXTURES") == "1":
        recorded = await _schemas(legacy_gateway.Gateway(legacy_federation.Hub()))
        SCHEMA_SNAPSHOT.write_text(json.dumps(recorded, indent=2, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    snapshot = json.loads(SCHEMA_SNAPSHOT.read_text(encoding="utf-8"))
    assert {"workspace_list", "set_workspace", "mcplist", "mcpdetail", "toolsearch", "tooluse"} <= set(snapshot)
    current_gateway, current_federation = package("current", "gateway"), package("current", "federation")
    current = await _schemas(current_gateway.Gateway(current_federation.Hub()))
    for name, old in snapshot.items():
        new = current[name]
        assert sorted(new.get("required", [])) == sorted(old.get("required", [])), name
        for prop, old_schema in old.get("properties", {}).items():
            assert new["properties"].get(prop) == old_schema, f"{name}.{prop} changed"
        added = set(new.get("properties", {})) - set(old.get("properties", {}))
        assert not added & set(new.get("required", [])), f"{name}: new properties must be optional"


# -- --check-config --------------------------------------------------------

def test_legacy_cloud_json_passes_check_with_warnings_only(tmp_path, monkeypatch, capsys):
    from agent_nexus import cloud
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("AGENT_TOKEN", AGENT_TOKEN)
    legacy = {"host": "127.0.0.1", "port": 8440, "public_url": "https://hub.example.test", "token_env": "SERVER_TOKEN",
              "agents": {AGENT_ID: "AGENT_TOKEN"}, "rpc_timeout_s": 180, "media_cache_dir": str(tmp_path / "media"),
              "future_option": {"x": 1}, "gateway": {"legacy_tools": "hidden", "later": True}}
    path = tmp_path / "cloud.json"
    path.write_text(json.dumps(legacy), encoding="utf-8")
    assert cloud.main(["--config", str(path), "--check-config"]) == 0
    captured = capsys.readouterr()
    assert "cloud configuration valid" in captured.out
    assert "'future_option'" in captured.err and "'gateway.later'" in captured.err
    assert not (tmp_path / "gateway.sqlite3").exists(), "a check must not create the state database"
    # The legacy gateway accepts the same file (rollback stays possible).
    assert package("legacy", "cloud").main(["--config", str(path), "--check-config"]) == 0
    for name in ("server.example.json",):
        example = json.loads((Path(__file__).resolve().parents[2] / "config" / name).read_text(encoding="utf-8"))
        assert not cloud.config_warnings(example)


# -- local console device page (hub_call device_admin) ---------------------

def test_local_console_device_page_goes_through_hub_call(tmp_path):
    from starlette.testclient import TestClient
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app
    app = build_app(Supervisor(tmp_path))
    hub = app.app.state.hub
    sent = []

    async def send(message):
        sent.append(message)
        args = message["params"]["args"]
        result = {"device": {"device_id": args["device_id"], "display_name": args["alias"]}} \
            if args["op"] == "set_alias" else {"devices": []}
        asyncio.get_running_loop().call_soon(hub.resolve, {"id": message["id"], "result": result})

    client = TestClient(app, base_url="http://127.0.0.1:8399")
    assert client.get("/api/devices").status_code == 409  # no gateway with hub_call/1 yet
    hub.attach(send, {"version": "0.2.0", "features": ["hub_call/1"], "display_name": HOST_A})
    listed = client.get("/api/devices").json()
    assert listed["devices"] == [] and listed["lock"] == {"locked": False, "has_password": False} and listed["remote"] is False
    assert client.put(f"/api/devices/{AGENT_ID}/alias", json={"alias": "Office"}).status_code == 403  # no X-ANX
    response = client.put(f"/api/devices/{AGENT_ID}/alias", json={"alias": "Office"}, headers={"X-ANX": "1"})
    assert response.status_code == 200 and response.json()["device"]["display_name"] == "Office"
    assert [m["params"] for m in sent] == [
        {"op": "device_admin", "args": {"op": "list", "include_offline": True}},
        {"op": "device_admin", "args": {"op": "set_alias", "device_id": AGENT_ID, "alias": "Office"}}]
    assert all(m["method"] == "hub_call" and m["id"].startswith("d-") for m in sent)

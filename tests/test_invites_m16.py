"""Milestone M16: invite codes and per-device credentials (decision D3, design §11.5, §13 M16).

Over real WebSockets and HTTP on ephemeral ports with temporary roots: a
gateway (uvicorn), and "freshly installed" clients that are temporary manager
roots joining with an invite code. Device MCPs are in-process FastMCP servers.
"""
from __future__ import annotations

import asyncio
import base64
import contextlib
import hashlib
import json
import logging
import socket
import sqlite3
import sys
import time
from pathlib import Path

import httpx
import pytest
import uvicorn
from fastmcp import Client, FastMCP
from fastmcp.client.transports import StreamableHttpTransport

from agent_nexus import join as joining
from agent_nexus.agent import HubLink, run_agent
from agent_nexus.cloud import build_cloud_app, main as gateway_main
from agent_nexus.invites import EnrollError, decode_invite, encode_invite, slugify

SERVER_TOKEN = "s" * 40
LEGACY_TOKEN = "l" * 40
LEGACY_ID = "pc-main"
HOST_A, HOST_B = "host-alpha", "host-beta"


def device_server(name: str) -> FastMCP:
    server = FastMCP(f"device-{name}")

    @server.tool
    async def execute(value: str) -> dict:
        return {"device": name, "value": value}

    return server


@contextlib.asynccontextmanager
async def gateway(tmp_path, monkeypatch, *, extra=None):
    monkeypatch.setenv("SERVER_TOKEN", SERVER_TOKEN)
    monkeypatch.setenv("LEGACY_TOKEN", LEGACY_TOKEN)
    listener = socket.socket()
    listener.bind(("127.0.0.1", 0))  # ephemeral: never a live service port
    listener.listen()
    port = listener.getsockname()[1]
    origin = f"http://127.0.0.1:{port}"
    state = tmp_path / "gateway-state"
    config = {"token_env": "SERVER_TOKEN", "agents": {LEGACY_ID: "LEGACY_TOKEN"}, "public_url": origin,
              "state_dir": str(state), **(extra or {})}
    app = build_cloud_app(config, tmp_path)
    server = uvicorn.Server(uvicorn.Config(app, host="127.0.0.1", port=port, log_level="error", access_log=False))
    task = asyncio.create_task(server.serve(sockets=[listener]))
    for _ in range(200):
        if server.started:
            break
        await asyncio.sleep(.05)
    assert server.started
    agents: list[asyncio.Task] = []
    http = httpx.AsyncClient(base_url=origin, trust_env=False, headers={"Authorization": f"Bearer {SERVER_TOKEN}"})
    try:
        yield {"app": app, "port": port, "origin": origin, "http": http, "agents": agents, "config": config,
               "db": state / "gateway.sqlite3"}
    finally:
        for agent in agents:
            agent.cancel()
        await asyncio.gather(*agents, return_exceptions=True)
        await http.aclose()
        server.should_exit = True
        await asyncio.wait_for(task, 10)
        listener.close()


async def create_invite(gw, **body) -> dict:
    response = await gw["http"].post("/api/invites", json=body)
    assert response.status_code == 201, response.text
    return response.json()


def fresh_root(tmp_path, name) -> Path:
    """A freshly installed client: the installer's disabled placeholder ``cloud`` section."""
    root = tmp_path / name
    root.mkdir()
    (root / "agent-nexus.json").write_text(json.dumps({
        "manager": {"host": "127.0.0.1", "port": 1}, "builtins": False, "scan": [],
        "cloud": {"enabled": False, "agent_id": "office-pc-main", "url": "wss://mcp.example.com",
                  "token_env": "ANX_AGENT_TOKEN", "workspaces": {"projects": {"path": ".", "mcp": "device"}}}}),
        encoding="utf-8")
    return root


async def join_root(root: Path, code: str, *, hostname: str, agent_id: str | None = None) -> dict:
    return await asyncio.to_thread(joining.join, root, code, hostname=hostname, agent_id=agent_id)


async def start_device(gw, root: Path, *, status: dict | None = None, hub: HubLink | None = None) -> tuple[dict, asyncio.Task]:
    cloud = json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]
    status = status if status is not None else {}
    ready = asyncio.Event()
    task = asyncio.create_task(run_agent(root, {**cloud, "refresh_interval_s": 60},
                                         transports={"device": device_server(cloud["agent_id"])},
                                         ready=ready, status=status, hub=hub))
    gw["agents"].append(task)
    await asyncio.wait_for(ready.wait(), 20)
    return status, task


async def eventually(predicate, timeout=5.0):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if predicate():
            return True
        await asyncio.sleep(0.02)
    return predicate()


def mcp(gw) -> Client:
    return Client(StreamableHttpTransport(f"{gw['origin']}/mcp", auth=SERVER_TOKEN))


# -- the code -----------------------------------------------------------------------

def test_code_and_link_encode_hub_id_secret():
    code = encode_invite("https://hub.example.test", "inv_abcdefgh", "x" * 43)
    assert code.startswith("anx1.") and "=" not in code
    assert decode_invite(code) == {"hub": "https://hub.example.test", "id": "inv_abcdefgh", "secret": "x" * 43}
    assert decode_invite("agentnexus://enroll?invite=" + code)["id"] == "inv_abcdefgh"
    assert decode_invite(f"  {code[:20]}\n{code[20:]} ")["secret"] == "x" * 43  # wrapped when pasted
    for bad in ("", "anx2.xx", "anx1.!!!", encode_invite("ftp://x", "inv_abcdefgh", "x" * 43),
                encode_invite("http://example.test", "inv_abcdefgh", "x" * 43),  # http only for loopback
                encode_invite("https://hub.example.test", "bad id", "x" * 43)):
        with pytest.raises(EnrollError):
            decode_invite(bad)
    assert slugify("DESKTOP-7Q2K") == "desktop-7q2k" and slugify("我的 电脑!") == "device" and slugify("a__b") == "a-b"


async def test_invite_holds_only_the_secret_digest(tmp_path, monkeypatch):
    async with gateway(tmp_path, monkeypatch) as gw:
        created = await create_invite(gw, label="家里的台式机", ttl_s=3600)
        decoded = decode_invite(created["code"])
        assert decoded["hub"] == gw["origin"] and decoded["id"] == created["invite"]["id"]
        assert created["link"] == "agentnexus://enroll?invite=" + created["code"]
        assert created["invite"]["state"] == "unused" and created["invite"]["label"] == "家里的台式机"
        assert 3590 < created["invite"]["expires_at"] - time.time() <= 3600
        listed = (await gw["http"].get("/api/invites")).json()["invites"]
        assert [item["id"] for item in listed] == [decoded["id"]] and "code" not in json.dumps(listed)
        with sqlite3.connect(gw["db"]) as db:
            stored = db.execute("SELECT secret_sha256 FROM invites WHERE id = ?", (decoded["id"],)).fetchone()[0]
        assert stored == hashlib.sha256(decoded["secret"].encode()).hexdigest()
        raw = b"".join(path.read_bytes() for path in gw["db"].parent.glob("gateway.sqlite3*"))
        assert decoded["secret"].encode() not in raw
        # ttl bounds: 7 days at most
        assert (await gw["http"].post("/api/invites", json={"ttl_s": 8 * 86400})).status_code == 400
        assert (await gw["http"].post("/api/invites", json={"ttl_s": "7d"})).status_code == 201
        assert (await gw["http"].post("/api/invites", json={})).json()["invite"]["expires_at"] - time.time() > 86000
        unauthenticated = httpx.AsyncClient(base_url=gw["origin"], trust_env=False)
        assert (await unauthenticated.post("/api/invites", json={})).status_code == 401
        await unauthenticated.aclose()


# -- joining ------------------------------------------------------------------------

async def test_fresh_client_joins_connects_and_shows_its_hostname(tmp_path, monkeypatch, caplog):
    # Our own loggers at DEBUG, everything else (HTTP and WebSocket libraries) at INFO.
    caplog.set_level(logging.INFO)
    caplog.set_level(logging.DEBUG, logger="agent_nexus")
    async with gateway(tmp_path, monkeypatch) as gw:
        created = await create_invite(gw)
        root = fresh_root(tmp_path, "client-a")
        result = await join_root(root, created["link"], hostname=HOST_A)  # the agentnexus:// link works too
        assert result["agent_id"] == HOST_A and result["hub"] == gw["origin"] and result["display_name"] == HOST_A
        token_file = root / "secrets" / "cloud" / "device-token"
        token = token_file.read_text(encoding="utf-8").strip()
        assert len(token) >= 32
        cloud = json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]
        assert cloud["enabled"] is True and cloud["url"] == f"ws://127.0.0.1:{gw['port']}"
        assert cloud["agent_id"] == HOST_A and cloud["token_secret"] == "cloud/device-token"
        assert "token_env" not in cloud and cloud["workspaces"] == {"projects": {"path": ".", "mcp": "device"}}
        # The device is listed (offline, invite) before it ever connects.
        listed = {d["device_id"]: d for d in (await gw["http"].get("/api/devices")).json()["devices"]}
        assert listed[HOST_A]["enrolled_via"] == "invite" and not listed[HOST_A]["online"]
        status, _ = await start_device(gw, root)
        assert status["state"] == "connected"
        async with mcp(gw) as client:
            devices = {d["device_id"]: d for d in (await client.call_tool("device_list", {})).data["devices"]}
            assert devices[HOST_A]["display_name"] == HOST_A and devices[HOST_A]["online"]
            used = await client.call_tool("use_device", {"device_id": HOST_A, "session_name": "probe", "kind": "codex"})
            assert used.data["agent_id"] == HOST_A
        listed = {d["device_id"]: d for d in (await gw["http"].get("/api/devices")).json()["devices"]}
        assert listed[HOST_A]["online"] and listed[HOST_A]["version"] and listed[HOST_A]["hostname"] == HOST_A
        # The same invite a second time is refused and creates nothing.
        second = fresh_root(tmp_path, "client-b")
        with pytest.raises(joining.JoinError) as refused:
            await join_root(second, created["code"], hostname=HOST_B)
        assert refused.value.status == 410 and refused.value.code == "used"
        assert not (second / "secrets").exists()
        assert json.loads((second / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]["enabled"] is False
        assert HOST_B not in {d["device_id"] for d in (await gw["http"].get("/api/devices")).json()["devices"]}
        # A root that is already joined refuses a second gateway unless told to replace it.
        with pytest.raises(joining.JoinError) as again:
            await join_root(root, (await create_invite(gw))["code"], hostname=HOST_A)
        assert again.value.code == "already_joined"
    text = caplog.text + "".join(str(record.args) for record in caplog.records)
    assert token not in text and decode_invite(created["code"])["secret"] not in text


async def test_expired_revoked_and_wrong_invites_are_refused_without_a_device(tmp_path, monkeypatch):
    async with gateway(tmp_path, monkeypatch) as gw:
        store = gw["app"].state.store
        expired = await create_invite(gw, ttl_s=60)
        voided = await create_invite(gw)
        deleted = await gw["http"].delete(f"/api/invites/{voided['invite']['id']}")
        assert deleted.status_code == 200 and deleted.json()["invite"]["state"] == "revoked"
        good = decode_invite((await create_invite(gw))["code"])
        wrong = encode_invite(good["hub"], good["id"], "w" * 43)
        unknown = encode_invite(good["hub"], "inv_doesnotexist", "w" * 43)
        real_clock = store.clock
        store.clock = lambda: real_clock() + 120  # the 60 s invite has expired
        try:
            outcomes = {}
            for name, code in (("expired", expired["code"]), ("revoked", voided["code"]), ("wrong", wrong),
                               ("unknown", unknown)):
                with pytest.raises(joining.JoinError) as refused:
                    await join_root(fresh_root(tmp_path, f"client-{name}"), code, hostname=f"host-{name}")
                outcomes[name] = (refused.value.status, refused.value.code)
        finally:
            store.clock = real_clock
        assert outcomes == {"expired": (410, "expired"), "revoked": (410, "revoked"),
                            "wrong": (400, "invalid_invite"), "unknown": (400, "invalid_invite")}
        assert not [d for d in (await gw["http"].get("/api/devices")).json()["devices"] if d["device_id"] != LEGACY_ID]
        # Expired and revoked invites can no longer be voided or listed as unused.
        assert (await gw["http"].get("/api/invites")).json()["invites"][0]["id"] == good["id"]
        # The good one still works (the wrong secret did not burn it).
        assert (await join_root(fresh_root(tmp_path, "client-good"), encode_invite(good["hub"], good["id"], good["secret"]),
                                hostname="host-good"))["agent_id"] == "host-good"
        # unreachable hub
        with pytest.raises(joining.JoinError) as unreachable:
            await join_root(fresh_root(tmp_path, "client-x"), encode_invite("http://127.0.0.1:9", good["id"], "z" * 43),
                            hostname="host-x")
        assert unreachable.value.status == 502 and unreachable.value.code == "hub_unreachable"


async def test_same_hostname_gets_a_suffix_and_reserved_names_are_refused(tmp_path, monkeypatch):
    async with gateway(tmp_path, monkeypatch) as gw:
        first = await join_root(fresh_root(tmp_path, "one"), (await create_invite(gw))["code"], hostname="DESKTOP-7Q2K")
        second = await join_root(fresh_root(tmp_path, "two"), (await create_invite(gw))["code"], hostname="DESKTOP-7Q2K")
        third = await join_root(fresh_root(tmp_path, "three"), (await create_invite(gw))["code"], hostname="desktop-7q2k")
        assert (first["agent_id"], second["agent_id"], third["agent_id"]) == ("desktop-7q2k", "desktop-7q2k-2", "desktop-7q2k-3")
        # a hostname that collides with the agents map or a reserved name
        assert (await join_root(fresh_root(tmp_path, "four"), (await create_invite(gw))["code"],
                                hostname=LEGACY_ID))["agent_id"] == f"{LEGACY_ID}-2"
        assert (await join_root(fresh_root(tmp_path, "five"), (await create_invite(gw))["code"],
                                hostname="me"))["agent_id"] == "me-pc"
        for reserved in ("hub", "me", "all"):
            response = await gw["http"].post("/api/invites", json={"agent_id": reserved})
            assert response.status_code == 400 and response.json()["code"] == "reserved_agent_id"
            with pytest.raises(joining.JoinError) as refused:
                await join_root(fresh_root(tmp_path, f"ask-{reserved}"), (await create_invite(gw))["code"],
                                hostname="host-z", agent_id=reserved)
            assert refused.value.code == "reserved_agent_id"
        taken = await gw["http"].post("/api/invites", json={"agent_id": LEGACY_ID})
        assert taken.status_code == 409 and taken.json()["code"] == "agent_id_taken"
        # an invite that names the agent_id wins over the hostname
        named = await join_root(fresh_root(tmp_path, "six"), (await create_invite(gw, agent_id="build-01"))["code"],
                                hostname="whatever")
        assert named["agent_id"] == "build-01"


# -- ingress and revocation --------------------------------------------------------------

async def test_revoke_closes_with_4401_stops_reconnects_and_spares_others(tmp_path, monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    from websockets.asyncio.client import connect
    from websockets.exceptions import ConnectionClosed
    async with gateway(tmp_path, monkeypatch) as gw:
        root_a, root_b = fresh_root(tmp_path, "a"), fresh_root(tmp_path, "b")
        # one "batch": two invites created together
        code_a, code_b = (await create_invite(gw, label="batch"))["code"], (await create_invite(gw, label="batch"))["code"]
        await join_root(root_a, code_a, hostname=HOST_A)
        await join_root(root_b, code_b, hostname=HOST_B)
        status_a, task_a = await start_device(gw, root_a)
        status_b, task_b = await start_device(gw, root_b)
        # the legacy agents-map device keeps working next to invite devices (compat)
        legacy_status, legacy_ready = {}, asyncio.Event()
        legacy_root = tmp_path / "legacy"
        legacy_root.mkdir()
        monkeypatch.setenv("ANX_AGENT_TOKEN", LEGACY_TOKEN)
        gw["agents"].append(asyncio.create_task(run_agent(
            legacy_root, {"agent_id": LEGACY_ID, "url": f"ws://127.0.0.1:{gw['port']}", "token_env": "ANX_AGENT_TOKEN",
                          "workspaces": {}, "refresh_interval_s": 60},
            transports={"device": device_server(LEGACY_ID)}, ready=legacy_ready, status=legacy_status)))
        await asyncio.wait_for(legacy_ready.wait(), 20)
        hub = gw["app"].state.hub
        assert set(hub.peers) == {HOST_A, HOST_B, LEGACY_ID}
        # the legacy device cannot be revoked from the console (409, agents_map)
        legacy = await gw["http"].post(f"/api/devices/{LEGACY_ID}/revoke")
        assert legacy.status_code == 409 and legacy.json()["code"] == "agents_map"

        started = time.monotonic()
        revoked = await gw["http"].post(f"/api/devices/{HOST_A}/revoke")
        assert revoked.status_code == 200 and revoked.json()["closed"] is True
        assert revoked.json()["device"]["revoked"] is True
        assert await eventually(lambda: status_a.get("state") == "revoked", 1.0)
        assert time.monotonic() - started < 1.0
        await asyncio.wait_for(task_a, 5)  # run_agent returned: no reconnect loop
        assert task_a.done() and not task_a.cancelled()
        assert HOST_A not in hub.peers and {HOST_B, LEGACY_ID} <= set(hub.peers)
        assert status_b["state"] == "connected" and legacy_status["state"] == "connected"
        # a hello with the same token is refused with 4401
        token = (root_a / "secrets" / "cloud" / "device-token").read_text(encoding="utf-8").strip()
        with pytest.raises(ConnectionClosed) as closed:
            async with connect(f"ws://127.0.0.1:{gw['port']}/ingress/{HOST_A}",
                               additional_headers={"Authorization": f"Bearer {token}"}, proxy=None) as ws:
                await ws.send(json.dumps({"protocol": 1, "agent_id": HOST_A, "catalog": {"workspaces": {}, "mcps": {}}}))
                await asyncio.wait_for(ws.recv(), 5)
        assert closed.value.rcvd.code == 4401 and closed.value.rcvd.reason == "revoked"
        # a wrong token for an invite device is a plain 1008-style rejection, not 4401
        with pytest.raises(Exception) as wrong:
            async with connect(f"ws://127.0.0.1:{gw['port']}/ingress/{HOST_B}",
                               additional_headers={"Authorization": "Bearer " + "w" * 43}, proxy=None) as ws:
                await asyncio.wait_for(ws.recv(), 5)
        assert getattr(getattr(wrong.value, "rcvd", None), "code", None) != 4401
        listed = {d["device_id"]: d for d in (await gw["http"].get("/api/devices")).json()["devices"]}
        assert listed[HOST_A]["revoked"] and not listed[HOST_A]["online"] and listed[HOST_B]["online"]
        # the revoked device can join again with a fresh invite and keeps its agent_id
        rejoined = await asyncio.to_thread(joining.join, root_a, (await create_invite(gw))["code"],
                                           hostname=HOST_A, replace=True)
        assert rejoined["agent_id"] == HOST_A
        status_a2, _ = await start_device(gw, root_a)
        assert status_a2["state"] == "connected" and HOST_A in hub.peers
    assert token not in caplog.text


async def test_cli_invites_and_device_revoke_reach_a_running_gateway(tmp_path, monkeypatch, capsys):
    async with gateway(tmp_path, monkeypatch) as gw:
        config_path = tmp_path / "gateway.json"
        config_path.write_text(json.dumps(gw["config"]), encoding="utf-8")
        assert gateway_main(["invite", "create", "--config", str(config_path), "--ttl", "2h", "--label", "cli"]) == 0
        out = capsys.readouterr().out.splitlines()
        code = next(line for line in out if line.startswith("anx1."))
        assert any(line.startswith("agentnexus://enroll?invite=anx1.") for line in out)
        assert decode_invite(code)["hub"] == gw["origin"]
        assert gateway_main(["invite", "list", "--config", str(config_path)]) == 0
        assert "cli" in capsys.readouterr().out
        spare = (await create_invite(gw))["invite"]["id"]
        assert gateway_main(["invite", "revoke", "--config", str(config_path), spare]) == 0
        assert "revoked" in capsys.readouterr().out
        root = fresh_root(tmp_path, "cli-client")
        await join_root(root, code, hostname=HOST_A)
        status, task = await start_device(gw, root)
        token = (root / "secrets" / "cloud" / "device-token").read_text(encoding="utf-8").strip()
        assert gateway_main(["device", "revoke", "--config", str(config_path), LEGACY_ID]) == 1  # agents map
        capsys.readouterr()
        assert gateway_main(["device", "revoke", "--config", str(config_path), HOST_A]) == 0
        printed = capsys.readouterr()
        assert token not in printed.out + printed.err
        # the running gateway's sweep closes the link (it runs every few seconds)
        await gw["app"].state.enrollment.sweep()
        assert await eventually(lambda: status.get("state") == "revoked", 3.0)
        await asyncio.wait_for(task, 5)
        assert gateway_main(["device", "list", "--config", str(config_path)]) == 0
        assert f"{HOST_A}\trevoked\tinvite" in capsys.readouterr().out


async def test_local_console_invites_and_revoke_through_hub_call(tmp_path, monkeypatch):
    """The local console's device page (hub_call device_admin, design §11.5)."""
    async with gateway(tmp_path, monkeypatch) as gw:
        root = fresh_root(tmp_path, "admin")
        await join_root(root, (await create_invite(gw))["code"], hostname=HOST_A)
        link = HubLink()
        await start_device(gw, root, hub=link)
        assert "invites/1" in link.features
        created = await link.call("device_admin", {"op": "invite_create", "label": "from local", "ttl_s": 3600})
        assert decode_invite(created["code"])["hub"] == gw["origin"]
        listed = await link.call("device_admin", {"op": "invite_list"})
        assert created["invite"]["id"] in [item["id"] for item in listed["invites"]]
        assert (await link.call("device_admin", {"op": "invite_revoke", "invite_id": created["invite"]["id"]}))["invite"]["state"] == "revoked"
        other = fresh_root(tmp_path, "other")
        await join_root(other, (await create_invite(gw))["code"], hostname=HOST_B)
        status_b, _ = await start_device(gw, other)
        result = await link.call("device_admin", {"op": "revoke", "device_id": HOST_B})
        assert result["closed"] is True and result["device"]["revoked"] is True
        assert await eventually(lambda: status_b.get("state") == "revoked", 1.0)
        devices = (await link.call("device_admin", {"op": "list"}))["devices"]
        assert {d["device_id"]: d["enrolled_via"] for d in devices}[HOST_A] == "invite"


async def test_local_console_join_page_api(tmp_path, monkeypatch):
    """``POST /api/cloud/join`` on the local manager restarts the link; ``leave`` forgets the gateway."""
    from agent_nexus import agent as agent_module
    from agent_nexus.supervisor import Supervisor
    from agent_nexus.web import build_app

    async with gateway(tmp_path, monkeypatch) as gw:
        root = fresh_root(tmp_path, "console")

        async def configured(root_, status=None, hub=None):
            cloud = json.loads((root_ / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]
            if not cloud.get("enabled"):
                status.update(state="disabled")
                return
            await run_agent(root_, {**cloud, "hostname": HOST_A}, transports={"device": device_server("console")},
                            status=status, hub=hub)
        monkeypatch.setattr(agent_module, "run_configured_agent", configured)
        sup = Supervisor(root)
        app = build_app(sup)
        local = httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://127.0.0.1:1",
                                  headers={"X-ANX": "1"})
        async with contextlib.AsyncExitStack() as stack:
            await stack.enter_async_context(app.app.router.lifespan_context(app.app))
            stack.push_async_callback(local.aclose)
            assert await eventually(lambda: sup.cloud_status.get("state") == "disabled", 5)
            before = (await local.get("/api/cloud")).json()
            assert before["state"] == "not_joined" and before["joined"] is False and before["managed"] is False
            bad = await local.post("/api/cloud/join", json={"invite": "nope"})
            assert bad.status_code == 400 and bad.json()["code"] == "invalid_invite"
            code = (await create_invite(gw))["code"]
            joined = await local.post("/api/cloud/join", json={"invite": code})
            assert joined.status_code == 200, joined.text
            body = joined.json()
            assert body["state"] == "connected" and body["hub"] == gw["origin"] and body["agent_id"]
            view = (await local.get("/api/cloud")).json()
            assert view["state"] == "connected" and view["joined"] and "invites/1" in view["features"]
            again = await local.post("/api/cloud/join", json={"invite": code})
            assert again.status_code == 409 and again.json()["code"] == "already_joined"
            assert (await local.get("/cloud")).status_code == 200
            # revoked from the gateway console: the page sees "revoked" and may join again
            await gw["http"].post(f"/api/devices/{body['agent_id']}/revoke")
            assert await eventually(lambda: sup.cloud_status.get("state") == "revoked", 2)
            assert (await local.get("/api/cloud")).json()["state"] == "revoked"
            rejoin = await local.post("/api/cloud/join", json={"invite": (await create_invite(gw))["code"]})
            assert rejoin.status_code == 200 and rejoin.json()["state"] == "connected"
            left = await local.post("/api/cloud/leave")
            assert left.json() == {"state": "not_joined"}
            assert not (root / "secrets" / "cloud" / "device-token").exists()
            assert (await local.get("/api/cloud")).json()["state"] == "not_joined"
            assert json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]["enabled"] is False


def test_anx_join_cli_without_a_running_manager(tmp_path, monkeypatch, capsys):
    from agent_nexus import cli
    root = fresh_root(tmp_path, "cli-join")

    class FakeHub:
        def __init__(self, **_):
            pass

        def post(self, url, json=None):
            assert url == "https://hub.example.test/enroll" and json["hostname"] and json["invite_id"] == "inv_abcdefgh"
            return httpx.Response(201, json={"agent_id": "cli-box", "token": "t" * 43, "display_name": "cli-box"})

        def close(self):
            pass

    monkeypatch.setattr(httpx, "Client", FakeHub)
    code = encode_invite("https://hub.example.test", "inv_abcdefgh", "x" * 43)
    assert cli.main(["--root", str(root), "join", code]) == 0
    assert "joined https://hub.example.test as cli-box" in capsys.readouterr().out
    cloud = json.loads((root / "agent-nexus.json").read_text(encoding="utf-8"))["cloud"]
    assert cloud["url"] == "wss://hub.example.test" and cloud["agent_id"] == "cli-box"
    assert (root / "secrets" / "cloud" / "device-token").read_text(encoding="utf-8").strip() == "t" * 43
    assert cli.main(["--root", str(root), "leave"]) == 0
    assert not (root / "secrets" / "cloud" / "device-token").exists()

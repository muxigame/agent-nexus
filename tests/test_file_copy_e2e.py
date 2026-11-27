"""file_copy end to end (docs/design.md §18): the real gateway on a port, two real device links, an MCP
client. Device A runs the job and reaches B through the gateway's HTTP relay with its own credential
(no P2P tunnel in a test); once A locks itself the gateway pumps the copy instead."""
from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import os
import socket
import types

import uvicorn
from fastmcp import Client
from fastmcp.client.transports import StreamableHttpTransport

from agent_nexus.agent import HubLink, run_agent
from agent_nexus.cloud import build_cloud_app
from agent_nexus.file_copy_device import CopyJobs, Links
from agent_nexus.file_share import CHUNK, FileShare

from test_remote_m19 import network_config

SERVER = "SERVER_TOKEN"


async def start_gateway(app):
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
    assert server.started
    return server, task, listener, port


def device(tmp_path, name, port, token_env, *, jobs: bool):
    root = tmp_path / name
    home = root / "home"
    home.mkdir(parents=True)
    cloud = {"enabled": True, "agent_id": name, "url": f"ws://127.0.0.1:{port}", "token_env": token_env,
             "workspaces": {}}
    (root / "agent-nexus.json").write_text(json.dumps({"cloud": cloud}), encoding="utf-8")
    account = types.SimpleNamespace(home=str(home), session=None, uid=os.getuid(), gid=os.getgid())
    share = FileShare(account=lambda: account)
    hub = HubLink()
    hub.handlers.update(share.handlers())
    hub.extra_features = ("files/1", "files/2")
    copy_jobs = None
    if jobs:
        async def no_tunnel(device_id):
            return None, "网关中转（测试里没有直连）"
        copy_jobs = CopyJobs(root, share, links=Links(no_tunnel, lambda d: None), me=lambda: name)
        hub.handlers.update(copy_jobs.handlers())
        hub.extra_features = (*hub.extra_features, "fxjob/1")
    return types.SimpleNamespace(root=root, home=home, cloud=cloud, hub=hub, share=share, jobs=copy_jobs)


def digests(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir")
            for p in sorted(root.rglob("*")) if not p.name.endswith(".anxpart")}


async def wait_done(client, job_id):
    for _ in range(60):
        status = (await client.call_tool("file_copy_status", {"job_id": job_id, "wait_ms": 10000})).data
        if status["state"] not in ("queued", "scanning", "copying", "finishing"):
            return status
    raise AssertionError(status)


async def test_an_agent_copies_a_folder_between_devices_through_the_mcp_tools(tmp_path, monkeypatch):
    config = network_config(tmp_path, monkeypatch)
    app = build_cloud_app(config, tmp_path)
    server, server_task, listener, port = await start_gateway(app)
    a = device(tmp_path, "pc-a", port, "TOKEN_A", jobs=True)
    b = device(tmp_path, "pc-b", port, "TOKEN_B", jobs=False)
    candles = a.home / "candles"
    for year in ("2021", "2022"):
        (candles / year).mkdir(parents=True)
        for day in range(15):
            (candles / year / f"{day:02d}.csv").write_bytes(os.urandom(day * 977 + 3))
    (candles / "2022" / "whole.bin").write_bytes(os.urandom(2 * CHUNK + 12345))
    (candles / "empty").mkdir()
    agents = []
    try:
        for node in (a, b):
            ready = asyncio.Event()
            agents.append(asyncio.create_task(run_agent(node.root, node.cloud, transports={}, ready=ready, hub=node.hub)))
            await asyncio.wait_for(ready.wait(), 20)
        for _ in range(100):
            if a.hub.available and b.hub.available:
                break
            await asyncio.sleep(0.05)
        async with Client(StreamableHttpTransport(f"http://127.0.0.1:{port}/mcp", auth=os.environ[SERVER])) as client:
            devices = {d["device_id"]: d for d in (await client.call_tool("device_list", {})).data["devices"]}
            assert devices["pc-a"]["file_copy"] is True and devices["pc-b"]["file_copy"] is False
            started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(candles),
                                                             "dst_device": "pc-b", "dst_path": str(b.home) + "/"})).data
            assert started["mode"] == "push" and started["runner_device"] == "pc-a"
            status = await wait_done(client, started["job_id"])
            assert status["state"] == "completed", status
            assert status["verified"] == "sha256" and status["files"]["done"] == 31
            assert any("网关中转" in route["route"] for route in status["routes"])
            assert digests(b.home / "candles") == digests(candles)

            # A locks itself: it may be operated on but not operate on others -> the gateway pumps
            await a.hub.call("lock_report", {"locked": True})
            (candles / "2022" / "late.csv").write_bytes(b"new day")
            pumped = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(candles),
                                                            "dst_device": "pc-b", "dst_path": str(b.home / "again")})).data
            assert pumped["mode"] == "gateway"
            second = await wait_done(client, pumped["job_id"])
            assert second["state"] == "completed", second
            assert digests(b.home / "again") == digests(candles)

            # cancel a slow job: what is in flight leaves no part behind
            await a.hub.call("lock_report", {"locked": False})
            for index in range(30):
                (candles / "bulk" / "x").mkdir(parents=True, exist_ok=True)
                (candles / "bulk" / "x" / f"{index}.bin").write_bytes(os.urandom(CHUNK))
            slow = b.share.write_bytes

            def write_bytes(params, data):
                import time
                time.sleep(0.05)
                return slow(params, data)
            b.share.write_bytes = write_bytes
            bulk = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(candles / "bulk"),
                                                          "dst_device": "pc-b", "dst_path": str(b.home / "bulk")})).data
            await asyncio.sleep(0.5)
            cancelled = (await client.call_tool("file_copy_cancel", {"job_id": bulk["job_id"]})).data
            assert cancelled["state"] == "cancelled"
            assert not list(b.home.rglob("*.anxpart"))
            listed = (await client.call_tool("file_copy_list", {})).data["jobs"]
            assert {started["job_id"], pumped["job_id"], bulk["job_id"]} <= {job["job_id"] for job in listed}
    finally:
        for task in agents:
            task.cancel()
        for task in agents:
            with contextlib.suppress(BaseException):
                await task
        server.should_exit = True
        with contextlib.suppress(BaseException):
            await asyncio.wait_for(server_task, 10)
        listener.close()

"""file_copy on the gateway (docs/design.md §18): who runs the job, the pump, and the MCP tools."""
from __future__ import annotations

import asyncio
import hashlib
import os
import types

from fastmcp import Client

from agent_nexus.federation import RESERVED_TOOLS, Hub, RoutingError
from agent_nexus.file_copy_device import CopyJobs, Links
from agent_nexus.file_copy_gateway import PeerTransport
from agent_nexus.file_copy_tools import FILE_COPY_TOOL_NAMES
from agent_nexus.file_share import CHUNK, FileShare
from agent_nexus.gateway import Gateway

V1 = ("fx_list", "fx_read", "fx_put_begin", "fx_put_write", "fx_put_end", "fx_put_abort")
NEW = ("files/1", "files/2", "fxjob/1", "remote-hub/1")


class FakeLock:
    def __init__(self, peer):
        self.peer = peer

    def require_unlocked(self):
        if self.peer.locked:
            from agent_nexus.lock import LockError
            raise LockError(423, "locked", "这台设备已锁定，解锁后才能操作其他设备")


class FakePeer:
    """A device on the gateway link: fx_* on a real FileShare, fxjob_* on real CopyJobs (no P2P:
    the job reaches the other device through ``PeerTransport``, standing in for the HTTP relay)."""

    def __init__(self, hub, agent_id, root, features=NEW, links=None):
        self.agent_id = agent_id
        self.catalog = {"workspaces": {}, "mcps": {}, "device": {"hostname": f"host-{agent_id}"}}
        self.features = frozenset(features)
        self.locked = False
        self.anx_version = "0.8.31"
        self.calls = []
        home = root / agent_id
        home.mkdir(parents=True, exist_ok=True)
        self.home = home
        account = types.SimpleNamespace(home=str(home), session=None, uid=getattr(os, "getuid", lambda: 0)(), gid=getattr(os, "getgid", lambda: 0)())
        self.share = FileShare(account=lambda: account)
        handlers = self.share.handlers()
        self.handlers = handlers if "files/2" in self.features else {k: v for k, v in handlers.items() if k in V1}
        if "fxjob/1" in self.features:
            async def no_link(device_id):
                return None, "网关中转"
            self.jobs = CopyJobs(root / f"{agent_id}-state", self.share, links=links or Links(no_link, lambda d: None),
                                 me=lambda: agent_id, lock=FakeLock(self), client=lambda d: PeerTransport(hub, d))
            self.handlers.update(self.jobs.handlers())
        hub.peers[agent_id] = self

    async def call(self, method, params):
        self.calls.append(method)
        handler = self.handlers.get(method)
        if handler is None:
            raise RoutingError(f"unknown method {method}")
        return await handler(params)


def tree(root):
    (root / "data" / "2021").mkdir(parents=True)
    (root / "data" / "empty").mkdir()
    (root / "data" / "2021" / "big.bin").write_bytes(os.urandom(2 * CHUNK + 100))
    for index in range(12):
        (root / "data" / "2021" / f"{index:02d}.csv").write_bytes(os.urandom(index * 211 + 1))
    return root / "data"


def digests(root):
    return {str(p.relative_to(root)): (hashlib.sha256(p.read_bytes()).hexdigest() if p.is_file() else "dir")
            for p in sorted(root.rglob("*")) if not p.name.endswith(".anxpart")}


async def finish(client, job_id):
    for _ in range(100):
        status = (await client.call_tool("file_copy_status", {"job_id": job_id, "wait_ms": 5000})).data
        if status["state"] in ("completed", "completed_with_errors", "failed", "cancelled", "interrupted"):
            return status
    raise AssertionError(status)


def run(body):
    async def main():
        hub = Hub()
        gateway = Gateway(hub)
        async with Client(gateway.build()) as client:
            return await body(hub, gateway, client)
    return asyncio.run(main())


def test_the_tools_are_reserved_titled_and_annotated():
    async def body(hub, gateway, client):
        return {tool.name: tool for tool in await client.list_tools()}
    tools = run(body)
    assert set(FILE_COPY_TOOL_NAMES) <= RESERVED_TOOLS
    assert set(FILE_COPY_TOOL_NAMES) <= set(tools)
    assert tools["file_copy"].title == "Copy files between devices"
    assert tools["file_copy"].annotations.destructive_hint is True and tools["file_copy"].annotations.read_only_hint is False
    assert tools["file_copy_status"].annotations.read_only_hint is True
    assert tools["file_copy_list"].annotations.read_only_hint is True
    assert tools["file_copy_cancel"].annotations.destructive_hint is False
    props = tools["file_copy"].input_schema["properties"]
    assert set(tools["file_copy"].input_schema["required"]) == {"src_device", "src_path", "dst_device", "dst_path"}
    assert props["route"]["enum"] == ["auto", "p2p"]


def test_push_by_the_source_and_a_rerun_skips(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path)
        b = FakePeer(hub, "pc-b", tmp_path)
        src = tree(a.home)
        started = (await client.call_tool("file_copy", {"src_device": "host-pc-a", "src_path": str(src),
                                                         "dst_device": "pc-b", "dst_path": str(b.home / "copy")})).data
        assert started["mode"] == "push" and started["runner_device"] == "pc-a" and started["job_id"].startswith("pc-a:")
        status = await finish(client, started["job_id"])
        again = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                       "dst_device": "pc-b", "dst_path": str(b.home / "copy")})).data
        second = await finish(client, again["job_id"])
        listed = (await client.call_tool("file_copy_list", {})).data["jobs"]
        # a new gateway (restarted) still finds the device's job: the id names its runner
        async with Client(Gateway(hub).build()) as other:
            later = (await other.call_tool("file_copy_status", {"job_id": started["job_id"]})).data
        return src, b, status, second, listed, later, started

    src, b, status, second, listed, later, started = run(body)
    assert status["state"] == "completed", status
    assert status["verified"] == "sha256" and status["src"]["display_name"] == "host-pc-a"
    assert digests(b.home / "copy") == digests(src)
    assert second["files"]["skipped"] == 13 and second["files"]["done"] == 0
    assert {job["job_id"] for job in listed} >= {started["job_id"]}
    assert later["state"] == "completed" and later["job_id"] == started["job_id"]


def test_a_locked_source_makes_the_destination_pull(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path)
        b = FakePeer(hub, "pc-b", tmp_path)
        a.locked = True  # may be operated on, may not operate on others
        src = tree(a.home)
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                         "dst_device": "pc-b", "dst_path": str(b.home) + "/"})).data
        status = await finish(client, started["job_id"])
        return src, b, started, status

    src, b, started, status = run(body)
    assert started["mode"] == "pull" and started["runner_device"] == "pc-b"
    assert status["state"] == "completed" and digests(b.home / "data") == digests(src)


def test_the_gateway_pumps_when_no_side_runs_jobs(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path, features=("files/1", "files/2"))
        b = FakePeer(hub, "pc-b", tmp_path, features=("files/1", "files/2"))
        src = tree(a.home)
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                         "dst_device": "pc-b", "dst_path": str(b.home / "copy")})).data
        status = await finish(client, started["job_id"])
        refused = await client.call_tool_mcp("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                            "dst_device": "pc-b", "dst_path": str(b.home / "x"),
                                                            "route": "p2p"})
        return src, b, started, status, refused

    src, b, started, status, refused = run(body)
    assert started["mode"] == "gateway" and started["job_id"].startswith("gateway:") and "较慢" in started["note"]
    assert status["state"] == "completed" and status["verified"] == "sha256", status
    assert digests(b.home / "copy") == digests(src)
    assert refused.is_error and "route=p2p" in refused.content[0].text


def test_a_files1_destination_cannot_overwrite_and_checks_sizes(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path, features=("files/1", "files/2"))
        b = FakePeer(hub, "pc-b", tmp_path, features=("files/1",))
        src = tree(a.home)
        (b.home / "copy" / "2021").mkdir(parents=True)
        (b.home / "copy" / "2021" / "00.csv").write_bytes(b"other content")
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                         "dst_device": "pc-b", "dst_path": str(b.home / "copy"),
                                                         "overwrite": True})).data
        return src, b, await finish(client, started["job_id"])

    src, b, status = run(body)
    assert status["state"] == "completed_with_errors" and status["verified"] == "size"
    assert any("版本过旧" in e["error"] for e in status["errors"])
    assert (b.home / "copy" / "2021" / "big.bin").read_bytes() == (src / "2021" / "big.bin").read_bytes()
    assert (b.home / "copy" / "2021" / "00.csv").read_bytes() == b"other content"


def test_same_device_runs_a_local_job(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path)
        src = tree(a.home)
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                         "dst_device": "pc-a", "dst_path": str(a.home / "backup")})).data
        inside = await client.call_tool_mcp("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                           "dst_device": "pc-a", "dst_path": str(src / "again")})
        status = await finish(client, started["job_id"])
        bad = None
        if not inside.is_error:
            bad = await finish(client, inside.structured_content["job_id"])
        return src, a, started, status, bad

    src, a, started, status, bad = run(body)
    assert started["mode"] == "local" and status["state"] == "completed"
    assert digests(a.home / "backup") == digests(src)
    assert bad["state"] == "failed" and "自身" in bad["message"]


def test_offline_missing_feature_and_cancel(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path)
        FakePeer(hub, "pc-old", tmp_path, features=())
        b = FakePeer(hub, "pc-b", tmp_path)
        offline = await client.call_tool_mcp("file_copy", {"src_device": "nowhere", "src_path": "/x",
                                                            "dst_device": "pc-b", "dst_path": "/y"})
        old = await client.call_tool_mcp("file_copy", {"src_device": "pc-old", "src_path": "/x",
                                                        "dst_device": "pc-b", "dst_path": "/y"})
        (a.home / "huge").mkdir()
        for index in range(80):
            (a.home / "huge" / f"{index}.bin").write_bytes(os.urandom(CHUNK))
        original = b.share.write_bytes

        def slow(params, data):
            import time
            time.sleep(0.05)
            return original(params, data)
        b.share.write_bytes = slow
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(a.home / "huge"),
                                                         "dst_device": "pc-b", "dst_path": str(b.home / "huge")})).data
        await asyncio.sleep(0.3)
        cancelled = (await client.call_tool("file_copy_cancel", {"job_id": started["job_id"]})).data
        missing = await client.call_tool_mcp("file_copy", {"src_device": "pc-a", "src_path": str(a.home / "nope"),
                                                            "dst_device": "pc-b", "dst_path": "/y"})
        return offline, old, cancelled, missing, b

    offline, old, cancelled, missing, b = run(body)
    assert offline.is_error and "不在线" in offline.content[0].text
    assert old.is_error and "不支持文件传输" in old.content[0].text
    assert cancelled["state"] == "cancelled"
    assert not list((b.home / "huge").rglob("*.anxpart")) if (b.home / "huge").exists() else True
    assert missing.is_error and "不存在" in missing.content[0].text


def test_device_list_shows_copy_capabilities(tmp_path):
    async def body(hub, gateway, client):
        FakePeer(hub, "pc-a", tmp_path)
        FakePeer(hub, "pc-old", tmp_path, features=("files/1",))
        return {d["device_id"]: d for d in (await client.call_tool("device_list", {})).data["devices"]}
    devices = run(body)
    assert devices["pc-a"]["file_copy"] is True and devices["pc-a"]["files_v2"] is True
    assert devices["pc-old"]["file_copy"] is False and devices["pc-old"]["files_v2"] is False


def test_status_of_an_unknown_job_is_an_error():
    async def body(hub, gateway, client):
        bad = await client.call_tool_mcp("file_copy_status", {"job_id": "nonsense"})
        gone = (await client.call_tool("file_copy_status", {"job_id": "gateway:abc"})).data
        offline = (await client.call_tool("file_copy_status", {"job_id": "pc-x:abc"})).data
        return bad, gone, offline
    bad, gone, offline = run(body)
    assert bad.is_error and gone["state"] == "unknown" and offline["state"] == "unknown"
    assert "离线" in offline["message"]


def test_a_locked_runner_refuses_at_the_device_too(tmp_path):
    """A device that locked itself after the gateway looked still refuses to run the job."""
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path)
        b = FakePeer(hub, "pc-b", tmp_path)
        a.locked = True
        return await a.call("fxjob_start", {"spec": {"src_device": "pc-a", "src_path": str(tree(a.home)),
                                                     "dst_device": "pc-b", "dst_path": str(b.home / "y")},
                                            "role": "push"})
    assert run(body)["code"] == "locked"


def test_jobs_use_p2p_streams_both_ways_and_fall_back_when_a_tunnel_breaks(tmp_path):
    """push over putx, pull over getx (real remote hubs on loopback, one ticket per stream); a tunnel that
    dies once is dropped and the copy finishes anyway."""
    from agent_nexus.remote_hub import RemoteHub

    async def body(hub, gateway, client):
        hubs, drops, opened = {}, [], []
        broken = {"left": 1}

        async def link(device_id):
            remote = hubs[device_id]
            ticket = (await remote.issue({"service": "files", "from": "test"}))["ticket"]
            if broken["left"]:
                broken["left"] -= 1
                return (1, "直连（坏的）", ticket), "直连（坏的）"  # nothing listens on port 1
            opened.append(device_id)
            return (remote.port, "直连（局域网）", ticket), "直连（局域网）"

        links = Links(link, drops.append)
        a = FakePeer(hub, "pc-a", tmp_path, links=links)
        b = FakePeer(hub, "pc-b", tmp_path, links=links)
        for peer in (a, b):
            hubs[peer.agent_id] = RemoteHub(terminals=None, files=peer.share)
            await hubs[peer.agent_id].start()
        try:
            src = tree(a.home)
            pushed = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                            "dst_device": "pc-b", "dst_path": str(b.home / "p")})).data
            push = await finish(client, pushed["job_id"])
            a.locked = True
            pulled = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                            "dst_device": "pc-b", "dst_path": str(b.home / "q")})).data
            pull = await finish(client, pulled["job_id"])
            return src, b, pushed, push, pulled, pull, drops, opened
        finally:
            for remote in hubs.values():
                await remote.close()

    src, b, pushed, push, pulled, pull, drops, opened = run(body)
    assert pushed["mode"] == "push" and push["state"] == "completed", push
    assert pulled["mode"] == "pull" and pull["state"] == "completed", pull
    assert digests(b.home / "p") == digests(src) == digests(b.home / "q")
    assert push["verified"] == pull["verified"] == "sha256"
    assert drops and opened  # the broken tunnel was dropped, then streams opened
    assert any("直连（局域网）" in route["route"] for route in push["routes"])
    assert any("直连（局域网）" in route["route"] for route in pull["routes"])


class Stall:
    """Make some of a peer's calls hang forever (a half-open link to a sleeping laptop)."""

    def __init__(self, peer, methods):
        self.original = peer.call
        self.methods = set(methods)
        peer.call = self

    async def __call__(self, method, params):
        if method in self.methods:
            await asyncio.Event().wait()
        return await self.original(method, params)


def test_a_silent_runner_does_not_hang_the_tools(tmp_path):
    async def body(hub, gateway, client):
        service = gateway.file_copy
        service.start_timeout, service.status_grace = 0.5, 0.5
        a = FakePeer(hub, "pc-a", tmp_path)
        b = FakePeer(hub, "pc-b", tmp_path)
        src = tree(a.home)
        Stall(a, {"fxjob_start"})
        Stall(b, {"fxjob_start"})
        loop = asyncio.get_running_loop()
        began = loop.time()
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(src),
                                                         "dst_device": "pc-b", "dst_path": str(b.home / "copy")})).data
        took = loop.time() - began
        status = await finish(client, started["job_id"])
        a2 = FakePeer(hub, "pc-c", tmp_path)
        pushed = (await client.call_tool("file_copy", {"src_device": "pc-c", "src_path": str(tree(a2.home)),
                                                        "dst_device": "pc-b", "dst_path": str(b.home / "c")})).data
        Stall(a2, {"fxjob_status"})
        began = loop.time()
        silent = (await client.call_tool("file_copy_status", {"job_id": pushed["job_id"], "wait_ms": 500})).data
        silent_took = loop.time() - began
        return started, took, status, silent, silent_took

    started, took, status, silent, silent_took = run(body)
    assert started["mode"] == "gateway" and len(started["refused"]) == 2 and took < 5, (started, took)
    assert status["state"] == "completed", status
    assert silent["state"] == "unknown" and "没有回应" in silent["message"] and silent_took < 5


def test_a_pump_status_long_poll_waits_for_the_end(tmp_path):
    async def body(hub, gateway, client):
        a = FakePeer(hub, "pc-a", tmp_path, features=("files/1", "files/2"))
        b = FakePeer(hub, "pc-b", tmp_path, features=("files/1", "files/2"))
        (a.home / "many").mkdir()
        for index in range(40):
            (a.home / "many" / f"{index}.csv").write_bytes(b"x" * 10)
        original = b.share.write_bytes

        def slow(params, data):
            import time
            time.sleep(0.05)
            return original(params, data)
        b.share.write_bytes = slow
        started = (await client.call_tool("file_copy", {"src_device": "pc-a", "src_path": str(a.home / "many"),
                                                         "dst_device": "pc-b", "dst_path": str(b.home / "many")})).data
        loop = asyncio.get_running_loop()
        began = loop.time()
        first = (await client.call_tool("file_copy_status", {"job_id": started["job_id"], "wait_ms": 1000})).data
        took = loop.time() - began
        return first, took, await finish(client, started["job_id"])

    first, took, last = run(body)
    assert first["state"] not in ("completed", "failed") and took >= 0.9, (first["state"], took)
    assert last["state"] == "completed"

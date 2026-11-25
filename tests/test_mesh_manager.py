"""MeshManager (docs/relay-mesh.md): home relay choice, pinning, back-off; ANX-PING through a hub."""
from __future__ import annotations

import asyncio

from agent_nexus import mesh as mesh_mod
from agent_nexus.remote_hub import RemoteHub


class FakeConnector:
    def __init__(self):
        self.pinned: set[str] = set()
        self.forgotten: list[str] = []

    def pin(self, target):
        self.pinned.add(target)

    def unpin(self, target):
        self.pinned.discard(target)

    def forget(self, target):
        self.forgotten.append(target)


def run_mesh(rtts: dict[str, float | None], *, is_relay=False, ticks=1, devices=None, manager=None):
    connector = manager.connector if manager else FakeConnector()
    warmed: list[str] = []

    async def list_devices():
        return devices if devices is not None else [
            {"device_id": d, "relay": True, "online": True} for d in rtts] + [
            {"device_id": "me", "relay": True, "online": True},
            {"device_id": "plain", "relay": False, "online": True}]

    async def tunnel_port(device):
        if rtts.get(device) is None:
            raise RuntimeError("打洞未成功")
        return 1000 + len(device), "P2P"

    async def warm(device):
        warmed.append(device)

    async def fake_ping(port, count=3, timeout=5.0):
        for device, rtt in rtts.items():
            if 1000 + len(device) == port:
                return rtt
        raise AssertionError(port)

    manager = manager or mesh_mod.MeshManager(me=lambda: "me", is_relay=is_relay, list_devices=list_devices,
                                              tunnel_port=tunnel_port, warm=warm, connector=connector)
    manager.list_devices, manager.tunnel_port, manager.warm = list_devices, tunnel_port, warm
    original = mesh_mod.ping
    mesh_mod.ping = fake_ping
    try:
        for _ in range(ticks):
            asyncio.run(manager.tick())
    finally:
        mesh_mod.ping = original
    return manager, connector, warmed


def test_member_keeps_the_two_nearest_relays_pinned_and_warmed():
    manager, connector, warmed = run_mesh({"r1": 80.0, "r22": 20.0, "r333": 50.0, "r4444": None})
    assert manager.home == ["r22", "r333"]
    assert connector.pinned == {"r22", "r333"}
    assert sorted(warmed) == ["r22", "r333"]
    assert "r4444" in manager.backoff and "r4444" in connector.forgotten
    assert manager.preferred()[:3] == ["r22", "r333", "r1"]
    status = manager.status()
    assert status["role"] == "member" and [h["device_id"] for h in status["home"]] == ["r22", "r333"]


def test_home_relays_change_only_for_a_clearly_nearer_one():
    manager, connector, _ = run_mesh({"r1": 40.0, "r22": 50.0, "r333": 60.0})
    assert manager.home == ["r1", "r22"]
    manager.measured_at = 0  # force a re-measure
    manager, _, _ = run_mesh({"r1": 40.0, "r22": 50.0, "r333": 45.0}, manager=manager)
    assert manager.home == ["r1", "r22"]  # 45 is not clearly better than 50
    manager.measured_at = 0
    manager, connector, _ = run_mesh({"r1": 40.0, "r22": 50.0, "r333": 20.0}, manager=manager)
    assert manager.home == ["r333", "r1"] and connector.pinned == {"r333", "r1"}


def test_backoff_grows_and_a_relay_pins_all_other_relays():
    manager, connector, _ = run_mesh({"r1": None, "r22": 30.0}, is_relay=True)
    first = manager.backoff["r1"][1]
    assert connector.pinned == {"r22"} and manager.home == ["r22"]
    manager.backoff["r1"] = (0.0, first)  # due again, fails again
    manager, _, _ = run_mesh({"r1": None, "r22": 30.0}, manager=manager)
    assert manager.backoff["r1"][1] == min(mesh_mod.BACKOFF_MAX_S, first * 2)
    manager.wake()
    assert manager.backoff == {}


def test_ping_through_a_hub_needs_no_ticket():
    async def run():
        hub = RemoteHub(terminals=None)
        port = await hub.start()
        try:
            return await mesh_mod.ping(port, count=3)
        finally:
            await hub.close()

    assert asyncio.run(run()) >= 0

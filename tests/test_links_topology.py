"""links/1: devices report how they reach each other; the gateway merges both ends into one picture."""
import time
from types import SimpleNamespace

from agent_nexus.devices import Devices
from agent_nexus.federation import Peer


def directory(*ids):
    hub = SimpleNamespace(peers={i: Peer(agent_id=i, socket=None, catalog={}) for i in ids})
    devices = Devices(hub, store=None)
    devices.console_list = lambda: [{"device_id": i} for i in ids]
    return devices, hub


def test_report_is_cleaned():
    devices, hub = directory("a", "b")
    result = devices.links_report(hub.peers["a"], {"links": [
        {"peer": "b", "kind": "direct", "rtt_ms": 12.5, "pinned": True},
        {"peer": "a", "kind": "direct"},                       # itself
        {"peer": "b", "kind": "teleport"},                     # unknown kind
        {"peer": "b", "kind": "relay", "via": "b", "rtt_ms": -3},  # via the target itself, bad rtt
        "junk",
    ], "gateway_rtt_ms": 41.2})
    assert result == {"links": 2}
    peer = hub.peers["a"]
    assert peer.gateway_rtt_ms == 41.2
    assert peer.links[0] == {"peer": "b", "kind": "direct", "via": "", "rtt_ms": 12.5, "pinned": True}
    assert peer.links[1]["via"] == "" and peer.links[1]["rtt_ms"] is None


def test_both_ends_merge_into_one_edge():
    devices, hub = directory("a", "b", "r")
    devices.links_report(hub.peers["a"], {"links": [{"peer": "b", "kind": "direct", "rtt_ms": 10},
                                                    {"peer": "r", "kind": "direct", "rtt_ms": 30}]})
    devices.links_report(hub.peers["b"], {"links": [{"peer": "a", "kind": "direct", "rtt_ms": 20},
                                                    {"peer": "x", "kind": "direct"}]})  # x is offline
    edges = devices.topology()["edges"]
    assert [(e["a"], e["b"], e["kind"], e["rtt_ms"]) for e in edges] == [("a", "b", "direct", 15), ("a", "r", "direct", 30)]
    assert edges[0]["reported_by"] == ["a", "b"]


def test_relayed_link_keeps_its_relay_and_stale_reports_drop():
    devices, hub = directory("a", "b", "r")
    devices.links_report(hub.peers["a"], {"links": [{"peer": "b", "kind": "relay", "via": "r"}]})
    assert devices.topology()["edges"][0]["via"] == "r"
    hub.peers["a"].links_at = time.monotonic() - devices.LINKS_TTL_S - 1
    assert devices.topology()["edges"] == []

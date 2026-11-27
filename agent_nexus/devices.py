"""Device directory: display names, aliases and name resolution (design §5.5, decision D6).

``display_name = alias or hostname or agent_id``. The hostname is what the
device itself reports in ``catalog.device.hostname``; the alias is set by the
owner (gateway API or ``hub_call device_admin``) and stored in the gateway
database. Routing, addresses and stored records always use ``agent_id``;
every agent-facing input that takes a device accepts either spelling.
"""
from __future__ import annotations

import time
from typing import Any

from .federation import Hub, Peer, RoutingError
from .osinfo import family
from .store import GatewayStore, clean_hostname


def device_block(catalog: Any) -> dict:
    """The catalog's ``device`` descriptor (design §5.2), or ``{}`` for a legacy agent."""
    block = catalog.get("device") if isinstance(catalog, dict) else None
    return block if isinstance(block, dict) else {}


def catalog_hostname(catalog: Any) -> str | None:
    return clean_hostname(device_block(catalog).get("hostname"))


class Devices:
    def __init__(self, hub: Hub, store: GatewayStore):
        self.hub = hub
        self.store = store

    # -- names --------------------------------------------------------------
    def _known(self) -> dict[str, dict]:
        """Every device the gateway knows: stored rows, overlaid with the live catalogs."""
        known = {row["agent_id"]: row for row in self.store.devices()}
        for agent_id, peer in self.hub.peers.items():
            row = dict(known.get(agent_id) or {"agent_id": agent_id, "alias": None, "hostname": None})
            row["hostname"] = catalog_hostname(peer.catalog) or row.get("hostname")
            row["online"] = True
            known[agent_id] = row
        for agent_id, row in known.items():
            if agent_id not in self.hub.peers:
                row["online"] = False
        return known

    @staticmethod
    def _display(row: dict) -> str:
        return row.get("alias") or row.get("hostname") or row["agent_id"]

    def display_name(self, agent_id: str) -> str:
        row = self.store.device(agent_id) or {"agent_id": agent_id}
        peer = self.hub.peers.get(agent_id)
        hostname = (catalog_hostname(peer.catalog) if peer else None) or row.get("hostname")
        return row.get("alias") or hostname or agent_id

    def label(self, agent_id: str) -> str:
        """``display (agent_id)`` -- how a device is first mentioned in agent-facing text."""
        name = self.display_name(agent_id)
        return name if name == agent_id else f"{name} ({agent_id})"

    def resolve(self, value: Any) -> str:
        """An ``agent_id`` from an agent_id or a display name; ambiguous names list the candidates."""
        if not isinstance(value, str) or not value.strip():
            raise RoutingError("device_id must be a device id or display name, as returned by device_list")
        if value in self.hub.peers:
            return value
        known = self._known()
        if value in known:
            return value
        folded = value.strip().casefold()
        matches = sorted(agent_id for agent_id, row in known.items() if self._display(row).casefold() == folded)
        if len(matches) == 1:
            return matches[0]
        if matches:
            candidates = ", ".join(f"{self._display(known[agent_id])} ({agent_id})" for agent_id in matches)
            raise RoutingError(f"device name {value!r} is ambiguous; use one of these device ids: {candidates}")
        # Unknown names fall through to the ordinary "offline/unknown" errors.
        return value

    # -- listing ------------------------------------------------------------
    def describe(self, agent_id: str, row: dict | None = None) -> dict:
        row = row or self._known().get(agent_id) or {"agent_id": agent_id, "online": False}
        peer: Peer | None = self.hub.peers.get(agent_id)
        catalog = peer.catalog if peer else {}
        block = device_block(catalog) or (row.get("device") or {})
        capabilities = block.get("capabilities") if isinstance(block.get("capabilities"), dict) else {}
        runtimes = block.get("agent_runtimes") if isinstance(block.get("agent_runtimes"), dict) else {}
        workdirs = [{"name": name, "path": entry.get("path")}
                    for name, entry in (catalog.get("workspaces") or {}).items()]
        nested = block.get("device") if isinstance(block.get("device"), dict) else {}
        facts = {**nested, **{key: value for key, value in block.items() if value not in (None, "")}}
        return {"device_id": agent_id, "display_name": self._display(row), "hostname": row.get("hostname"),
                "alias": row.get("alias"), "profile": block.get("profile"), "form": block.get("form"),
                "platform": block.get("platform") or nested.get("platform"), "online": peer is not None,
                "distro": facts.get("distro"), "os_family": family(facts),
                "capabilities": capabilities, "agent_runtimes": runtimes, "workdirs": workdirs,
                # Wake adapters arrive with M7; until then nothing is wakeable.
                "wakeable_kinds": [],
                "features": sorted(peer.features) if peer else list(row.get("features") or [])}

    def list(self, *, include_offline: bool = False, capability: str | None = None) -> list[dict]:
        known = self._known()
        result = []
        for agent_id in sorted(known):
            row = known[agent_id]
            if not include_offline and not row.get("online"):
                continue
            entry = self.describe(agent_id, row)
            if capability:
                found = entry["capabilities"].get(capability)
                if not isinstance(found, dict) or found.get("available") is False:
                    continue
            result.append(entry)
        return result

    #: geoip.GeoLocator (gateway with p2p): where each device roughly is, for the map view
    locator = None
    #: the gateway's own public host (its reflector address, else public_url): where it is on the map
    gateway_host: str | None = None
    _gateway_geo: tuple = (float("-inf"), None)

    def set_location(self, value: Any, location: Any) -> dict:
        agent_id = self.resolve(value)
        try:
            self.store.set_location(agent_id, location if isinstance(location, dict) else None)
        except KeyError:
            raise RoutingError(f"unknown device {agent_id}; it must have connected at least once") from None
        except ValueError as exc:
            raise RoutingError(str(exc)) from None
        return self.console_describe(agent_id)

    def set_alias(self, value: Any, alias: Any) -> dict:
        agent_id = self.resolve(value)
        try:
            self.store.set_alias(agent_id, alias)
        except KeyError:
            raise RoutingError(f"unknown device {agent_id}; it must have connected at least once") from None
        except ValueError as exc:
            raise RoutingError(str(exc)) from None
        return self.describe(agent_id)

    def delete(self, value: Any) -> str:
        """Remove an offline device from the list (the console's 删除); returns its agent_id.

        A device that joined with an invite must be revoked first, or it would
        come straight back. A device on a gateway.env token (agents_map or a
        shared group token) can always be removed; it reappears if it connects again.
        """
        agent_id = self.resolve(value)
        if agent_id in self.hub.peers:
            raise RoutingError("device_online: 设备在线，不能删除；先吊销它")
        if self.store.device(agent_id) is None:
            raise RoutingError(f"unknown device {agent_id}")
        active, _ = self.store.device_token_hashes(agent_id)
        if active:
            raise RoutingError("not_revoked: 这台设备的凭据仍然有效；先吊销再删除")
        self.store.delete_device(agent_id)
        return agent_id

    # -- the console's device table (docs/ui-spec.md §3.2) ------------------------
    #: ``devices.enrolled_via`` as the console shows it.
    ENROLLED_VIA = {"legacy_env": "agents_map", "agents_map": "agents_map", "agent_groups": "agent_groups",
                    "invite": "invite"}

    def gateway_geo(self) -> dict | None:
        """Where the gateway itself is: its public host, resolved here, looked up in ip2region.

        Cached for an hour (five minutes while unknown); DNS is the system resolver, the lookup
        is the same offline database the devices use.
        """
        if self.locator is None or not self.gateway_host:
            return None
        at, geo = self._gateway_geo
        now = time.monotonic()
        if now - at < (3600 if geo else 300):
            return geo
        import socket
        try:
            host = self.gateway_host.split(":")[0] if self.gateway_host.count(":") == 1 else self.gateway_host
            ip = socket.getaddrinfo(host, None, socket.AF_INET)[0][4][0]
        except (OSError, IndexError):
            ip = None
        try:
            self.locator.warm()
            geo = self.locator.lookup(ip)
        except Exception:
            geo = None
        self._gateway_geo = (now, geo)
        return geo

    def _geo(self, agent_id: str, stored: dict) -> dict | None:
        if self.locator is None:
            manual = stored.get("location")
            return {**manual, "source": "manual"} if manual else None
        try:
            return self.locator.locate(agent_id, stored.get("location"))
        except Exception:
            return None

    def console_describe(self, agent_id: str, row: dict | None = None) -> dict:
        """:meth:`describe` plus what the owner's device table shows (version, enrolment, revocation)."""
        row = row or self._known().get(agent_id) or {"agent_id": agent_id, "online": False}
        stored = self.store.device(agent_id) or {}
        entry = self.describe(agent_id, row)
        peer: Peer | None = self.hub.peers.get(agent_id)
        catalog = peer.catalog if peer else {}
        block = device_block(catalog) or (stored.get("device") or {})
        nested = block.get("device") if isinstance(block.get("device"), dict) else {}
        version = (peer.anx_version if peer else None) or block.get("agent_version") or stored.get("version")
        enrolled = stored.get("enrolled_via") or getattr(peer, "enrolled_via", None)
        entry.update(
            version=version, os=block.get("os") or nested.get("os"),
            platform=entry.get("platform") or nested.get("platform"),
            arch=block.get("arch") or nested.get("arch"),
            enrolled_via=self.ENROLLED_VIA.get(enrolled, enrolled),
            last_seen_at=stored.get("last_seen_at"), last_connected_at=stored.get("last_connected_at"),
            revoked=bool(stored.get("revoked_at")) and peer is None, revoked_at=stored.get("revoked_at"),
            plugins_count=len(catalog.get("mcps") or {}) if peer else None,
            # term/1: the device opens remote terminals (console 终端, `agent-nexus term`).
            terminal=bool(peer is not None and "term/1" in getattr(peer, "features", ())),
            locked=bool(peer is not None and getattr(peer, "locked", False)),
            # remote-hub/1 + desktop/1: a P2P tunnel agent, and a desktop to share.
            p2p=bool(peer is not None and "remote-hub/1" in getattr(peer, "features", ())),
            desktop=bool(peer is not None and "desktop/1" in getattr(peer, "features", ())),
            # desktop/2: flow control, an adaptive bit rate and scaling (the viewer's 自动 quality)
            desktop_auto=bool(peer is not None and "desktop/2" in getattr(peer, "features", ())),
            # files/1: send and fetch files (P2P, else relayed by the gateway).
            files=bool(peer is not None and "files/1" in getattr(peer, "features", ())),
            # agents/1: the agent chat runs CLIs there (docs/agent-chat.md); the chat's 运行设备 list.
            agents=bool(peer is not None and "agents/1" in getattr(peer, "features", ())),
            # relay/1: forwards other devices' streams (docs/relay-mesh.md).
            relay=bool(peer is not None and "relay/1" in getattr(peer, "features", ())),
            # roughly where it is (the 设备 page's map): set by hand, else from its egress address
            geo=self._geo(agent_id, stored),
            # its round trip to the gateway (links/1), shown next to 在线
            gateway_rtt_ms=round(peer.gateway_rtt_ms) if peer is not None and peer.gateway_rtt_ms is not None else None,
            # What the device publishes now (names, descriptions, tool counts; never URLs).
            plugins=[{"name": name, "description": str(mcp.get("description") or "")[:300],
                      "tools": len(mcp.get("tools") or []), "error": bool(mcp.get("error"))}
                     for name, mcp in sorted((catalog.get("mcps") or {}).items()) if isinstance(mcp, dict)] if peer else None)
        return entry

    #: what a device may report about one link (``links_report``)
    LINK_KINDS = ("direct", "relay", "gateway", "punching")
    MAX_LINKS = 256
    #: a report older than this is not drawn (devices report every 30 s)
    LINKS_TTL_S = 120.0

    def links_report(self, peer: Peer, args: dict) -> dict:
        """Keep what ``peer`` says about its links (``hub_call links_report``, ``links/1``)."""
        links = []
        for item in (args.get("links") or [])[:self.MAX_LINKS] if isinstance(args.get("links"), list) else []:
            if not isinstance(item, dict) or item.get("kind") not in self.LINK_KINDS:
                continue
            other = item.get("peer")
            if not isinstance(other, str) or not other or other == peer.agent_id:
                continue
            rtt = item.get("rtt_ms")
            via = item.get("via")
            links.append({"peer": other[:200], "kind": item["kind"],
                          "via": via[:200] if isinstance(via, str) and via and via not in (other, peer.agent_id) else "",
                          "rtt_ms": float(rtt) if isinstance(rtt, (int, float)) and 0 <= rtt < 600000 else None,
                          "pinned": bool(item.get("pinned"))})
        peer.links, peer.links_at = links, time.monotonic()
        rtt = args.get("gateway_rtt_ms")
        if isinstance(rtt, (int, float)) and 0 <= rtt < 600000:
            peer.gateway_rtt_ms = float(rtt)
        return {"links": len(links)}

    def topology(self) -> dict:
        """Every device and every live link between them, merged from both ends' reports.

        One edge per pair and kind (a relayed link also names its relay); a round trip both
        ends measured is averaged. Links to offline devices and stale reports are left out.
        """
        now = time.monotonic()
        edges: dict[tuple, dict] = {}
        for agent_id, peer in list(self.hub.peers.items()):
            if not peer.links or now - peer.links_at > self.LINKS_TTL_S:
                continue
            for link in peer.links:
                other = link["peer"]
                if other not in self.hub.peers:
                    continue
                a, b = sorted((agent_id, other))
                key = (a, b, link["kind"], link["via"])
                edge = edges.setdefault(key, {"a": a, "b": b, "kind": link["kind"], "via": link["via"],
                                              "rtt": [], "pinned": False, "reported_by": []})
                edge["reported_by"].append(agent_id)
                edge["pinned"] = edge["pinned"] or link["pinned"]
                if link["rtt_ms"] is not None:
                    edge["rtt"].append(link["rtt_ms"])
        result = []
        for edge in edges.values():
            rtts = edge.pop("rtt")
            edge["rtt_ms"] = round(sum(rtts) / len(rtts)) if rtts else None
            edge["reported_by"] = sorted(set(edge["reported_by"]))
            result.append(edge)
        result.sort(key=lambda e: (e["a"], e["b"], e["kind"]))
        return {"devices": self.console_list(), "edges": result, "gateway": {"geo": self.gateway_geo()}}

    def console_list(self) -> list[dict]:
        known = self._known()
        return [self.console_describe(agent_id, known[agent_id]) for agent_id in sorted(known)]

"""Device locations for the map view: the xdb lookup, place names, manual positions."""
from __future__ import annotations

import asyncio
import hashlib
import struct

import pytest

from agent_nexus import geoip
from agent_nexus.store import GatewayStore


def tiny_xdb(segments: list[tuple[str, str, str]]) -> bytes:
    """A minimal ip2region xdb (v3 header, IPv4) holding the given (start, end, region) segments."""
    import ipaddress
    header = struct.pack("<HHIIIHH", 3, 1, 0, 0, 0, 4, 4).ljust(geoip.HEADER_SIZE, b"\0")
    vector_size = geoip.VECTOR_COLS * geoip.VECTOR_COLS * geoip.VECTOR_ENTRY
    regions = b""
    pointers = []
    data_start = geoip.HEADER_SIZE + vector_size
    for _start, _end, region in segments:
        raw = region.encode("utf-8")
        pointers.append((data_start + len(regions), len(raw)))
        regions += raw
    index_start = data_start + len(regions)
    index = b""
    rows: dict[int, list[int]] = {}
    for i, ((start, end, _region), (pointer, length)) in enumerate(zip(segments, pointers)):
        s, e = int(ipaddress.IPv4Address(start)), int(ipaddress.IPv4Address(end))
        index += struct.pack("<IIHI", s, e, length, pointer)
        for prefix in range(s >> 16, (e >> 16) + 1):
            rows.setdefault(prefix, []).append(i)
    vector = bytearray(vector_size)
    for prefix, members in rows.items():
        first, last = members[0], members[-1]
        struct.pack_into("<II", vector, prefix * geoip.VECTOR_ENTRY,
                         index_start + first * geoip.SEGMENT_V4, index_start + last * geoip.SEGMENT_V4)
    return header + bytes(vector) + regions + index


SEGMENTS = [("192.0.2.0", "192.0.2.127", "中国|浙江省|宁波市|电信|CN"),
            ("203.0.113.0", "203.0.113.255", "United States|California|Los Angeles|0|US"),
            ("198.51.100.0", "198.51.100.255", "中国|吉林省|某个小地方|电信|CN")]


def test_lookup_and_places():
    xdb = geoip.Xdb(tiny_xdb(SEGMENTS))
    assert xdb.search("192.0.2.3") == "中国|浙江省|宁波市|电信|CN"
    assert xdb.search("192.0.2.200") is None
    ningbo = geoip.place(xdb.search("192.0.2.3"))
    assert ningbo["label"] == "浙江 宁波" and ningbo["precision"] == "city" and ningbo["lat"] == 29.87
    la = geoip.place(xdb.search("203.0.113.122"))
    assert la["lon"] == -118.24 and la["precision"] == "city"
    fallback = geoip.place(xdb.search("198.51.100.58"))  # a city we have no coordinates for
    assert fallback["precision"] == "province" and (fallback["lat"], fallback["lon"]) == geoip.PROVINCES["吉林"]
    assert geoip.place("Reserved|Reserved|Reserved|0|0") is None


def test_locator_fetches_once_checks_the_digest_and_prefers_manual(tmp_path, monkeypatch):
    data = tiny_xdb(SEGMENTS)
    monkeypatch.setattr(geoip, "XDB_SHA256", hashlib.sha256(data).hexdigest())
    # the samples are documentation addresses, which the real check (rightly) skips
    monkeypatch.setattr(geoip, "_public", lambda address: not address.is_private or address in
                        __import__("ipaddress").ip_network("192.0.2.0/25"))
    fetched = []

    def fetch():
        fetched.append(1)
        return data

    locator = geoip.GeoLocator(tmp_path, ip_of={"nb": "192.0.2.3", "lan": "192.168.1.5"}.get, fetch=fetch)

    async def run():
        assert locator.locate("nb") is None  # the first call only starts loading
        await locator._loading
        return locator.locate("nb"), locator.locate("lan"), locator.locate("nb", {"lat": 1.5, "lon": 2.5, "label": "x"})

    auto, lan, manual = asyncio.run(run())
    assert auto["label"] == "浙江 宁波" and lan is None
    assert manual == {"lat": 1.5, "lon": 2.5, "label": "x", "source": "manual"}
    assert (tmp_path / geoip.XDB_NAME).exists() and fetched == [1]
    again = geoip.GeoLocator(tmp_path, ip_of=lambda _: "192.0.2.3", fetch=fetch)
    again._load()
    assert fetched == [1]  # the cached file is reused

    bad = geoip.GeoLocator(tmp_path / "other", fetch=lambda: b"tampered")
    with pytest.raises(ValueError):
        bad._load()


def test_store_keeps_manual_locations():
    store = GatewayStore(None)
    store.device_connected("dev1", hostname="h", features=[], device=None)
    assert store.device("dev1")["location"] is None
    assert store.set_location("dev1", {"lat": "30.5", "lon": 120.1, "label": "家里"}) == \
        {"lat": 30.5, "lon": 120.1, "label": "家里"}
    assert store.device("dev1")["location"]["lat"] == 30.5
    with pytest.raises(ValueError):
        store.set_location("dev1", {"lat": 200, "lon": 0})
    store.set_location("dev1", None)
    assert store.device("dev1")["location"] is None

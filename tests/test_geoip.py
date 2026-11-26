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
    assert ningbo["label"] == "浙江 宁波" and ningbo["precision"] == "city" and abs(ningbo["lat"] - 29.87) < 0.05
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


# -- the Chinese city table and DB-IP -----------------------------------------------------------

def test_every_chinese_city_has_its_seat():
    cities = geoip.cn_cities()
    assert len(cities) > 350
    ningbo, kashgar = cities["浙江|宁波"], cities["新疆|喀什"]
    assert abs(ningbo[0] - 29.87) < 0.1 and abs(ningbo[1] - 121.55) < 0.1
    assert abs(kashgar[0] - 39.47) < 0.2 and abs(kashgar[1] - 75.99) < 0.3
    # a prefecture goes by its seat: 西双版纳 is 景洪, not the province capital 昆明
    assert abs(cities["云南|西双版纳"][0] - 22.0) < 0.2
    # a city the hand-written table never had now has its own place, not its province's
    place = geoip.place("中国|广东省|揭阳市|电信|CN")
    assert place["precision"] == "city" and abs(place["lat"] - 23.55) < 0.2


HONG_KONG = {"country": "香港", "province": "Kowloon", "city": "Hong Kong", "iso": "HK", "label": "香港 Hong Kong",
             "lat": 22.32, "lon": 114.17, "precision": "city", "source": "ip", "provider": "dbip"}


def test_merge_prefers_ip2region_in_china_and_db_ip_elsewhere():
    ningbo = geoip.place("中国|浙江省|宁波市|电信|CN")
    china_by_dbip = {**HONG_KONG, "country": "中国", "iso": "CN", "lat": 30.29, "lon": 120.16}  # province capital
    assert geoip.merge(ningbo, china_by_dbip)["label"] == "浙江 宁波"           # ip2region's city
    assert geoip.merge(ningbo, china_by_dbip)["provider"] == "ip2region"
    brazil = geoip.place("Brazil|Santa Catarina|0|0|BR")                    # a block registered abroad
    assert geoip.merge(brazil, HONG_KONG)["label"] == "香港 Hong Kong"
    registered_home = geoip.place("中国|浙江省|杭州市|阿里云|CN")              # a cloud region abroad
    tokyo = {**HONG_KONG, "country": "日本", "iso": "JP", "label": "日本 Tokyo", "lat": 35.69, "lon": 139.69}
    assert geoip.merge(registered_home, tokyo)["label"] == "日本 Tokyo"
    assert geoip.merge(ningbo, None)["label"] == "浙江 宁波"
    assert geoip.merge(None, None) is None
    unplaced = {**geoip.place("中国|0|0|0|CN"), "lat": None, "lon": None}
    filled = geoip.merge(unplaced, {**china_by_dbip})
    assert filled["lat"] == 30.29  # ip2region knew only the country: DB-IP's place is better than none


def test_locator_asks_db_ip_too_and_ipv6_goes_to_it_alone(tmp_path, monkeypatch):
    data = tiny_xdb(SEGMENTS)
    monkeypatch.setattr(geoip, "XDB_SHA256", hashlib.sha256(data).hexdigest())
    monkeypatch.setattr(geoip, "_public", lambda address: True)
    locator = geoip.GeoLocator(tmp_path, fetch=lambda: data)
    locator._load()  # DB-IP cannot be fetched here (conftest): ip2region alone
    assert locator._dbip is None and locator.lookup("192.0.2.3")["label"] == "浙江 宁波"
    asked = []

    class FakeDbip:
        def search(self, ip):
            asked.append(ip)
            return HONG_KONG

    locator._dbip = FakeDbip()
    assert locator.lookup("203.0.113.9")["label"] == "香港 Hong Kong"   # ip2region said Los Angeles
    assert locator.lookup("2001:db8::1")["label"] == "香港 Hong Kong"   # no IPv6 in ip2region
    assert asked == ["203.0.113.9", "2001:db8::1"]


def test_db_ip_is_retried_at_most_hourly_and_a_bad_file_never_replaces_a_good_one(tmp_path, monkeypatch):
    data = tiny_xdb(SEGMENTS)
    monkeypatch.setattr(geoip, "XDB_SHA256", hashlib.sha256(data).hexdigest())
    tries = []

    def broken(dest):
        tries.append(dest.name)
        dest.write_bytes(b"not an mmdb")

    monkeypatch.setattr(geoip, "_public", lambda address: True)
    locator = geoip.GeoLocator(tmp_path, fetch=lambda: data, fetch_dbip=broken)
    locator._load()
    locator._load()
    assert tries == [geoip.DBIP_NAME.replace(".mmdb", ".part")]  # once, not on every load
    assert locator._dbip is None and not (tmp_path / geoip.DBIP_NAME).exists()
    assert not (tmp_path / geoip.DBIP_NAME.replace(".mmdb", ".part")).exists()
    assert locator.lookup("192.0.2.3")["label"] == "浙江 宁波"

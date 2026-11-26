"""Where a device is (city level), for the 设备 page's globe. Gateway side only, all offline.

Two databases, each where it is good; no address is ever sent to a third party:

* ip2region (https://github.com/lionsoul2014/ip2region, Apache-2.0 OR MIT, about 11 MB) for
  addresses in China: the best free source for which province and city a Chinese address is in.
  It has names only; the city's coordinates come from ``geo_cn_cities.json``, the seat of every
  city it names, matched in GeoNames (CC BY 4.0) by ``scripts/build_cn_cities.py``. Fetched once
  from a pinned commit and checked against its sha256.
* DB-IP City Lite (https://db-ip.com, CC BY 4.0, about 130 MB, monthly) for everything else:
  city and coordinates abroad, where ip2region goes by registry data (a Hong Kong server in a
  block registered in Brazil was placed in Brazil; DB-IP has it in Kowloon). In China it is
  mostly province-level, so it does not override ip2region there. Refreshed monthly; optional:
  without it (or without the maxminddb package) ip2region alone is used.

When the two disagree on the country, DB-IP wins (ip2region marks cloud addresses abroad by
where they are registered). The owner can drag a device on the globe; a manual position wins.

The device's address is its own egress as its tunnel agent reported it (its punch candidates),
not the address its gateway link comes from: that one can be a proxy or a VPN exit.
"""
from __future__ import annotations

import asyncio
import hashlib
import ipaddress
import logging
import struct
import threading
from pathlib import Path
from typing import Callable
import json
import time

log = logging.getLogger(__name__)

XDB_COMMIT = "f1781b7f9b7a17c96246433d83804404f0a22120"
XDB_URL = f"https://raw.githubusercontent.com/lionsoul2014/ip2region/{XDB_COMMIT}/data/ip2region_v4.xdb"
XDB_SHA256 = "a29fb3e8b204dfdf45b9bc082e12845b5eaac18cd467849d2888baa9fde383c8"
XDB_NAME = "ip2region_v4.xdb"
DBIP_NAME = "dbip-city-lite.mmdb"
DBIP_URL = "https://download.db-ip.com/free/dbip-city-lite-{month}.mmdb.gz"
DBIP_MAX_AGE_S = 35 * 86400  # a new file comes out monthly
CHINESE = {"CN", "HK", "MO", "TW"}

HEADER_SIZE = 256
VECTOR_COLS = 256
VECTOR_ENTRY = 8
SEGMENT_V4 = 14  # start ip (4) | end ip (4) | data length (2) | data pointer (4), little endian


class Xdb:
    """ip2region xdb (IPv4), the whole file in memory: a vector index on the first two bytes,
    then a binary search over 14-byte segments."""

    def __init__(self, data: bytes):
        if len(data) < HEADER_SIZE + VECTOR_COLS * VECTOR_COLS * VECTOR_ENTRY:
            raise ValueError("not an ip2region xdb file")
        version, _policy, _created, _start, _end, ip_version = struct.unpack_from("<HHIIIH", data, 0)
        if ip_version != 4:
            raise ValueError(f"xdb holds IPv{ip_version}, expected IPv4")
        self.data = data
        self.version = version

    def search(self, ip: str) -> str | None:
        try:
            value = int(ipaddress.IPv4Address(ip))
        except ValueError:
            return None
        a, b = (value >> 24) & 0xFF, (value >> 16) & 0xFF
        offset = HEADER_SIZE + (a * VECTOR_COLS + b) * VECTOR_ENTRY
        start, end = struct.unpack_from("<II", self.data, offset)
        low, high = 0, (end - start) // SEGMENT_V4
        while low <= high:
            middle = (low + high) // 2
            sip, eip, length, pointer = struct.unpack_from("<IIHI", self.data, start + middle * SEGMENT_V4)
            if value < sip:
                high = middle - 1
            elif value > eip:
                low = middle + 1
            else:
                return self.data[pointer:pointer + length].decode("utf-8", "replace")
        return None


# -- coordinates -----------------------------------------------------------------------------
# (lat, lon). Chinese names without their 省/市/自治区 suffix; foreign names as ip2region writes
# them (English). Province entries are the capital, used when the city is not listed.

PROVINCES = {
    "北京": (39.90, 116.40), "天津": (39.13, 117.20), "上海": (31.23, 121.47), "重庆": (29.56, 106.55),
    "河北": (38.04, 114.51), "山西": (37.87, 112.55), "内蒙古": (40.84, 111.75), "辽宁": (41.80, 123.43),
    "吉林": (43.82, 125.32), "黑龙江": (45.80, 126.53), "江苏": (32.06, 118.80), "浙江": (30.27, 120.15),
    "安徽": (31.82, 117.23), "福建": (26.07, 119.30), "江西": (28.68, 115.86), "山东": (36.65, 117.12),
    "河南": (34.75, 113.62), "湖北": (30.59, 114.31), "湖南": (28.23, 112.94), "广东": (23.13, 113.26),
    "广西": (22.82, 108.37), "海南": (20.04, 110.20), "四川": (30.57, 104.07), "贵州": (26.65, 106.63),
    "云南": (25.04, 102.71), "西藏": (29.65, 91.13), "陕西": (34.34, 108.94), "甘肃": (36.06, 103.83),
    "青海": (36.62, 101.78), "宁夏": (38.49, 106.23), "新疆": (43.83, 87.62), "香港": (22.32, 114.17),
    "澳门": (22.20, 113.55), "台湾": (25.03, 121.56),
}

CITIES = {
    # 浙江
    "杭州": (30.27, 120.15), "宁波": (29.87, 121.55), "温州": (28.00, 120.70), "嘉兴": (30.75, 120.76),
    "湖州": (30.89, 120.09), "绍兴": (30.00, 120.58), "金华": (29.08, 119.65), "台州": (28.66, 121.42),
    "舟山": (29.99, 122.21), "衢州": (28.97, 118.86), "丽水": (28.45, 119.92),
    # 江苏
    "南京": (32.06, 118.80), "苏州": (31.30, 120.62), "无锡": (31.49, 120.31), "常州": (31.81, 119.97),
    "南通": (31.98, 120.89), "徐州": (34.26, 117.18), "扬州": (32.39, 119.41), "泰州": (32.46, 119.92),
    "盐城": (33.35, 120.16), "镇江": (32.19, 119.43), "淮安": (33.61, 119.02), "连云港": (34.60, 119.22),
    "宿迁": (33.96, 118.28),
    # 广东
    "广州": (23.13, 113.26), "深圳": (22.54, 114.06), "东莞": (23.02, 113.75), "佛山": (23.02, 113.12),
    "珠海": (22.27, 113.58), "中山": (22.52, 113.39), "惠州": (23.11, 114.42), "汕头": (23.35, 116.68),
    "江门": (22.58, 113.08), "湛江": (21.27, 110.36), "肇庆": (23.05, 112.47), "揭阳": (23.55, 116.37),
    # 福建
    "福州": (26.07, 119.30), "厦门": (24.48, 118.09), "泉州": (24.87, 118.68), "漳州": (24.51, 117.65),
    "莆田": (25.45, 119.01), "龙岩": (25.08, 117.02),
    # 山东
    "济南": (36.65, 117.12), "青岛": (36.07, 120.38), "烟台": (37.46, 121.45), "潍坊": (36.71, 119.16),
    "临沂": (35.10, 118.36), "济宁": (35.41, 116.59), "淄博": (36.81, 118.05), "威海": (37.51, 122.12),
    "日照": (35.42, 119.53), "泰安": (36.20, 117.09), "德州": (37.43, 116.36), "聊城": (36.46, 115.99),
    "菏泽": (35.23, 115.48), "东营": (37.43, 118.67), "滨州": (37.38, 117.97), "枣庄": (34.81, 117.32),
    # 河南、湖北、湖南
    "郑州": (34.75, 113.62), "洛阳": (34.62, 112.45), "开封": (34.80, 114.31), "许昌": (34.04, 113.85),
    "商丘": (34.41, 115.66), "信阳": (32.15, 114.09), "周口": (33.63, 114.70), "驻马店": (32.98, 114.02),
    "平顶山": (33.77, 113.19), "安阳": (36.10, 114.39), "焦作": (35.22, 113.24), "新乡": (35.30, 113.93),
    "南阳": (33.00, 112.53), "武汉": (30.59, 114.31), "宜昌": (30.69, 111.29), "襄阳": (32.01, 112.14),
    "长沙": (28.23, 112.94), "株洲": (27.83, 113.13), "岳阳": (29.36, 113.13), "衡阳": (26.89, 112.57),
    # 华北、东北
    "石家庄": (38.04, 114.51), "唐山": (39.63, 118.18), "保定": (38.87, 115.46), "廊坊": (39.54, 116.68),
    "邯郸": (36.63, 114.49), "太原": (37.87, 112.55), "大同": (40.08, 113.30), "呼和浩特": (40.84, 111.75),
    "包头": (40.66, 109.84), "沈阳": (41.80, 123.43), "大连": (38.91, 121.61), "鞍山": (41.11, 122.99),
    "长春": (43.82, 125.32), "吉林": (43.84, 126.55), "哈尔滨": (45.80, 126.53), "齐齐哈尔": (47.35, 123.92),
    "大庆": (46.59, 125.10),
    # 华东其他、西南、西北、华南其他
    "合肥": (31.82, 117.23), "芜湖": (31.35, 118.43), "南昌": (28.68, 115.86), "赣州": (25.83, 114.93),
    "九江": (29.71, 116.00), "成都": (30.57, 104.07), "绵阳": (31.47, 104.68), "贵阳": (26.65, 106.63),
    "遵义": (27.73, 106.93), "昆明": (25.04, 102.71), "拉萨": (29.65, 91.13), "西安": (34.34, 108.94),
    "宝鸡": (34.36, 107.24), "兰州": (36.06, 103.83), "西宁": (36.62, 101.78), "银川": (38.49, 106.23),
    "乌鲁木齐": (43.83, 87.62), "南宁": (22.82, 108.37), "桂林": (25.27, 110.29), "柳州": (24.33, 109.41),
    "海口": (20.04, 110.20), "三亚": (18.25, 109.51),
    # abroad (ip2region writes these in English)
    "Los Angeles": (34.05, -118.24), "San Jose": (37.34, -121.89), "Santa Clara": (37.35, -121.96),
    "San Francisco": (37.77, -122.42), "Fremont": (37.55, -121.99), "Seattle": (47.61, -122.33),
    "New York": (40.71, -74.01), "Ashburn": (39.04, -77.49), "Dallas": (32.78, -96.80), "Chicago": (41.88, -87.63),
    "Miami": (25.76, -80.19), "Richmond": (37.54, -77.44), "Mountain View": (37.39, -122.08), "Tokyo": (35.68, 139.69), "Osaka": (34.69, 135.50), "Seoul": (37.57, 126.98),
    "Singapore": (1.35, 103.82), "Frankfurt": (50.11, 8.68), "London": (51.51, -0.13), "Paris": (48.86, 2.35),
    "Amsterdam": (52.37, 4.90), "Sydney": (-33.87, 151.21), "Toronto": (43.65, -79.38), "Hong Kong": (22.32, 114.17),
}

REGIONS = {  # states and countries, when the city is unknown
    "California": (36.78, -119.42), "Virginia": (37.43, -78.66), "Texas": (31.00, -100.00),
    "Washington": (47.75, -120.74), "Oregon": (43.80, -120.55), "New Jersey": (40.06, -74.41),
    "United States": (39.80, -98.60), "Japan": (36.20, 138.30), "Singapore": (1.35, 103.82),
    "Korea": (36.50, 127.80), "South Korea": (36.50, 127.80), "Germany": (51.20, 10.40),
    "United Kingdom": (54.00, -2.00), "France": (46.60, 2.40), "Netherlands": (52.20, 5.30),
    "Canada": (56.10, -106.30), "Australia": (-25.30, 133.80), "Russia": (61.50, 105.30),
    "India": (20.60, 78.90), "Vietnam": (14.06, 108.28), "Thailand": (15.87, 100.99),
    "Malaysia": (4.21, 101.98), "Indonesia": (-0.79, 113.92), "Philippines": (12.88, 121.77),
    "Brazil": (-14.20, -51.90), "中国": (35.00, 105.00),
}

_SUFFIXES = ("特别行政区", "维吾尔自治区", "壮族自治区", "回族自治区", "自治区", "省", "市", "地区")


def _bare(name: str) -> str:
    for suffix in _SUFFIXES:
        if name.endswith(suffix) and len(name) > len(suffix):
            return name[: -len(suffix)]
    return name


_CN_CITIES: dict | None = None


def cn_cities() -> dict[str, list[float]]:
    """{"浙江|宁波": [lat, lon]}: the seat of every city ip2region names (geo_cn_cities.json)."""
    global _CN_CITIES
    if _CN_CITIES is None:
        try:
            _CN_CITIES = json.loads((Path(__file__).parent / "geo_cn_cities.json").read_text(encoding="utf-8"))["cities"]
        except (OSError, ValueError, KeyError):
            _CN_CITIES = {}
    return _CN_CITIES


def place(region: str) -> dict | None:
    """``country|province|city|isp|iso`` -> a location dict, coordinates included when known."""
    parts = [(p if p not in ("0", "") else "") for p in region.split("|")]
    parts += [""] * (5 - len(parts))
    country, province, city, isp, iso = parts[:5]
    if country in ("Reserved", "内网IP") or not country:
        return None
    coords, precision = None, ""
    for key, level in ((city, "city"), (province, "province"), (country, "country")):
        if not key:
            continue
        bare = _bare(key)
        if level == "city" and country == "中国":
            found = cn_cities().get(f"{_bare(province)}|{bare}")
            if found:
                coords, precision = tuple(found), level
                break
        if level == "province":  # 吉林 is a province and a city: the province means its capital
            found = PROVINCES.get(bare) or REGIONS.get(key) or REGIONS.get(bare)
        elif level == "city":
            found = CITIES.get(bare) or CITIES.get(key)
        else:
            found = REGIONS.get(key) or REGIONS.get(bare)
        if found:
            coords, precision = found, level
            break
    label = " ".join(x for x in (country if country != "中国" else "", _bare(province), _bare(city)) if x) or country
    return {"country": country, "province": province, "city": city, "isp": isp, "iso": iso,
            "label": label, "lat": coords[0] if coords else None, "lon": coords[1] if coords else None,
            "precision": precision, "source": "ip"}


class Dbip:
    """DB-IP City Lite (mmdb) through the maxminddb package."""

    def __init__(self, path: Path):
        import maxminddb
        self.reader = maxminddb.open_database(str(path))
        kind = str(self.reader.metadata().database_type)
        if not kind.startswith("DBIP-City"):
            self.reader.close()
            raise ValueError(f"not a DB-IP city database: {kind}")

    def search(self, ip: str) -> dict | None:
        try:
            record = self.reader.get(ip) or {}
        except ValueError:
            return None
        country = record.get("country") or {}
        location = record.get("location") or {}
        if not country.get("iso_code"):
            return None
        names = country.get("names") or {}
        subdivision = ((record.get("subdivisions") or [{}])[0].get("names") or {}).get("en", "")
        city = ((record.get("city") or {}).get("names") or {}).get("en", "")
        city = city.split(" (")[0]  # "Los Angeles (Central-Alameda)": the district is too fine
        country_name = names.get("zh-CN") or names.get("en") or country["iso_code"]
        return {"country": country_name, "province": subdivision, "city": city, "isp": "",
                "iso": country["iso_code"], "label": " ".join(x for x in (country_name, city or subdivision) if x),
                "lat": location.get("latitude"), "lon": location.get("longitude"),
                "precision": "city" if city else "province" if subdivision else "country",
                "source": "ip", "provider": "dbip"}


def merge(china: dict | None, abroad: dict | None) -> dict | None:
    """ip2region for Chinese addresses, DB-IP for the rest and when they disagree on the country."""
    if china and china.get("country") == "中国" and (abroad is None or abroad.get("iso") in CHINESE):
        if china.get("lat") is None and abroad and abroad.get("lat") is not None:
            china = {**china, "lat": abroad["lat"], "lon": abroad["lon"], "precision": abroad["precision"]}
        return {**china, "provider": "ip2region"}
    if abroad and abroad.get("lat") is not None:
        return abroad
    return {**china, "provider": "ip2region"} if china else None


class GeoLocator:
    """Lazily fetched database + the device -> egress address lookup the gateway provides."""

    def __init__(self, state_dir: Path, ip_of: Callable[[str], str | None] | None = None,
                 fetch: Callable[[], bytes] | None = None, fetch_dbip: Callable[[Path], None] | None = None):
        self.path = Path(state_dir) / XDB_NAME
        self.dbip_path = Path(state_dir) / DBIP_NAME
        self.ip_of = ip_of
        self._fetch = fetch or _download
        self._fetch_dbip = fetch_dbip or _download_dbip
        self._xdb: Xdb | None = None
        self._dbip: Dbip | None = None
        self._dbip_checked = 0.0
        self._lock = threading.Lock()
        self._loading: asyncio.Task | None = None
        self.error: str | None = None

    def _load(self) -> None:
        with self._lock:
            if self._xdb is None:
                data = self.path.read_bytes() if self.path.exists() else b""
                if hashlib.sha256(data).hexdigest() != XDB_SHA256:
                    data = self._fetch()
                    if hashlib.sha256(data).hexdigest() != XDB_SHA256:
                        raise ValueError("ip2region 数据校验失败")
                    self.path.parent.mkdir(parents=True, exist_ok=True)
                    temp = self.path.with_suffix(".part")
                    temp.write_bytes(data)
                    temp.replace(self.path)
                self._xdb = Xdb(data)
            self._load_dbip()

    def _dbip_due(self) -> bool:
        """DB-IP missing, unopened or a month old, and not tried within the hour."""
        try:
            import maxminddb  # noqa: F401
        except ImportError:
            return False
        stale = not self.dbip_path.exists() or time.time() - self.dbip_path.stat().st_mtime >= DBIP_MAX_AGE_S
        return (stale and time.time() - self._dbip_checked > 3600) or (self._dbip is None and self.dbip_path.exists())

    def _load_dbip(self) -> None:
        """Open DB-IP, fetching it when missing or a month old (at most once an hour on failure)."""
        now = time.time()
        fresh = self.dbip_path.exists() and now - self.dbip_path.stat().st_mtime < DBIP_MAX_AGE_S
        if self._dbip is not None and fresh:
            return
        try:
            import maxminddb  # noqa: F401  (optional: without it ip2region alone is used)
        except ImportError:
            return
        if not fresh and now - self._dbip_checked > 3600:
            self._dbip_checked = now
            try:
                temp = self.dbip_path.with_suffix(".part")
                self.dbip_path.parent.mkdir(parents=True, exist_ok=True)
                self._fetch_dbip(temp)
                Dbip(temp).reader.close()  # a valid DB-IP city database before it replaces the old one
                if self._dbip is not None:
                    self._dbip.reader.close()
                    self._dbip = None
                temp.replace(self.dbip_path)
            except Exception as exc:
                self.dbip_path.with_suffix(".part").unlink(missing_ok=True)
                log.warning("geoip: DB-IP unavailable: %s", exc)
        if self._dbip is None and self.dbip_path.exists():
            try:
                self._dbip = Dbip(self.dbip_path)
            except Exception as exc:
                log.warning("geoip: DB-IP file unusable: %s", exc)

    def warm(self) -> None:
        """Start loading in the background (the first device list must not wait for the files);
        also how DB-IP gets its monthly refresh."""
        if self._loading is not None and not self._loading.done():
            return
        if self._xdb is not None and not self._dbip_due():
            return
        try:
            loop = asyncio.get_running_loop()
        except RuntimeError:
            return

        async def load() -> None:
            try:
                await asyncio.to_thread(self._load)
                self.error = None
            except Exception as exc:
                self.error = str(exc)[:200]
                log.warning("geoip: ip2region unavailable: %s", exc)
        self._loading = loop.create_task(load())

    def lookup(self, ip: str | None) -> dict | None:
        if not ip or self._xdb is None:
            return None
        try:
            address = ipaddress.ip_address(ip)
        except ValueError:
            return None
        if not _public(address):
            return None
        region = self._xdb.search(ip) if address.version == 4 else None
        abroad = self._dbip.search(str(address)) if self._dbip is not None else None
        return merge(place(region) if region else None, abroad)

    def locate(self, agent_id: str, manual: dict | None = None) -> dict | None:
        if manual and manual.get("lat") is not None and manual.get("lon") is not None:
            return {**manual, "source": "manual"}
        self.warm()
        ip = self.ip_of(agent_id) if self.ip_of else None
        return self.lookup(ip)


def _public(address) -> bool:
    """A routable address (private, loopback and documentation ranges are not)."""
    return address.is_global


def _download_dbip(dest: Path) -> None:
    """This month's DB-IP City Lite (last month's early in the month), unpacked to ``dest``."""
    import datetime
    import gzip
    import shutil
    import httpx
    today = datetime.date.today()
    months = [today.strftime("%Y-%m"), (today.replace(day=1) - datetime.timedelta(days=1)).strftime("%Y-%m")]
    status = None
    for month in months:
        packed = dest.with_suffix(".gz")
        with httpx.stream("GET", DBIP_URL.format(month=month), timeout=300, follow_redirects=True) as response:
            status = response.status_code
            if status != 200:
                continue
            with packed.open("wb") as out:
                for chunk in response.iter_bytes(1 << 20):
                    out.write(chunk)
        try:
            with gzip.open(packed, "rb") as source, dest.open("wb") as out:
                shutil.copyfileobj(source, out, 1 << 20)
        finally:
            packed.unlink(missing_ok=True)
        return
    raise ValueError(f"DB-IP download failed (HTTP {status})")


def _download() -> bytes:
    import httpx
    response = httpx.get(XDB_URL, timeout=120, follow_redirects=True)
    response.raise_for_status()
    return response.content

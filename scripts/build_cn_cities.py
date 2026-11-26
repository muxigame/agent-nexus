"""Coordinates for every Chinese city ip2region names (agent_nexus/geo_cn_cities.json).

    curl -LO https://download.geonames.org/export/dump/cities1000.zip && unzip cities1000.zip
    uv run --no-project python scripts/build_cn_cities.py ip2region_v4.xdb cities1000.txt

ip2region is the best free source for *which* Chinese city an address is in, but it carries
names only. GeoNames (CC BY 4.0) lists the seat of each city with its Chinese name among the
alternate names, so the city is matched by name, kept only when it lies in the right province
(within reach of its capital), and the seat's coordinates are written. Autonomous
prefectures, leagues and counties go by their seat (SEATS). (DB-IP City Lite was
tried for this and puts most Chinese addresses on their province capital, so it is used for
addresses abroad only.) Cities without a match fall back to their province at run time.

The output is derived from GeoNames (https://www.geonames.org, CC BY 4.0); see
THIRD_PARTY_NOTICES.md. Re-run when the pinned ip2region file changes.
"""
from __future__ import annotations

import json
import math
import struct
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))
from agent_nexus.geoip import HEADER_SIZE, PROVINCES, SEGMENT_V4, _bare  # noqa: E402

CHINA = {"CN", "HK", "MO", "TW"}
#: how far a city can be from its province's capital; further is a namesake elsewhere
#: (Kashgar is 1050 km from Urumqi, Hulunbuir 1400 km from Hohhot)
MAX_KM_FROM_CAPITAL = 800
WIDE = {"新疆": 1800, "西藏": 1400, "内蒙古": 1700, "甘肃": 1100, "青海": 1000, "黑龙江": 1000, "四川": 900, "云南": 900}
RANK = {"PPLA2": 0, "PPLA": 1, "PPLC": 1, "PPLA3": 2, "PPLA4": 3}  # the seat of a city first
#: places ip2region names that are not a city of that name: where their government sits
SEATS = {
    "锡林郭勒盟": "锡林浩特", "阿拉善盟": "巴彦浩特", "兴安盟": "乌兰浩特", "呼伦贝尔": "海拉尔",
    "黔南": "都匀", "黔西南": "兴义", "黔东南": "凯里", "西双版纳": "景洪", "迪庆": "香格里拉",
    "红河": "蒙自", "德宏": "芒市", "怒江": "泸水", "阿里": "狮泉河", "甘南": "合作", "延边": "延吉",
    "克孜勒苏": "阿图什", "博尔塔拉": "博乐", "巴音郭楞": "库尔勒", "伊犁": "伊宁", "海南": "恰卜恰",
    "黄南": "同仁", "海西": "德令哈", "海北": "西海", "果洛": "大武", "湘西": "吉首", "阿坝": "马尔康",
    "凉山": "西昌", "甘孜": "康定", "邯郸县": "邯郸", "临邑县": "临邑", "衡阳县": "西渡", "陵水县": "陵水",
    "乐东县": "抱由", "屯昌县": "屯昌", "白沙黎族自治县": "牙叉", "湾仔区": "湾仔", "将军澳": "将军澳",
    "桃园": "桃园", "高雄县": "凤山", "Taichung County": "丰原", "彰化县": "彰化", "宜兰县": "宜兰",
    "云林县": "斗六", "南投县": "南投", "新竹县": "竹北", "嘉义县": "太保", "嘉义": "嘉义",
    "花莲县": "花莲", "苗栗县": "苗栗", "台东县": "台东", "屏东县": "屏东", "澎湖县": "马公", "金门县": "金城",
}
#: seats the gazetteer has no Chinese name for (their public coordinates); these win
MANUAL = {
    "黑龙江|大兴安岭": [50.42, 124.12],
    "内蒙古|兴安盟": [46.08, 122.07], "内蒙古|呼伦贝尔": [49.21, 119.74], "新疆|克孜勒苏": [39.72, 76.17],
    "西藏|阿里": [32.50, 80.10], "海南|屯昌县": [19.36, 110.10], "海南|陵水县": [18.50, 110.04],
    "山东|临邑县": [37.19, 116.87], "台湾|Taichung County": [24.25, 120.72], "台湾|台东县": [22.76, 121.15],
    "台湾|嘉义": [23.48, 120.45], "台湾|宜兰县": [24.76, 121.75], "台湾|屏东县": [22.67, 120.49],
    "台湾|桃园": [24.99, 121.30], "台湾|花莲县": [23.99, 121.60], "香港|将军澳": [22.31, 114.26],
    "香港|湾仔区": [22.28, 114.17],
}


def km(a, b) -> float:
    p1, p2 = math.radians(a[0]), math.radians(b[0])
    dl = math.radians(b[1] - a[1])
    return 6371 * math.acos(max(-1.0, min(1.0, math.sin(p1) * math.sin(p2) + math.cos(p1) * math.cos(p2) * math.cos(dl))))


def cities_in(data: bytes) -> dict[str, str]:
    """{"浙江|宁波": "宁波市"}: every Chinese city the xdb names (key bare, value as written)."""
    _version, _policy, _created, start, end, _ipv = struct.unpack_from("<HHIIIH", data, 0)
    assert start >= HEADER_SIZE
    found = {}
    for offset in range(start, end + 1, SEGMENT_V4):
        _sip, _eip, length, pointer = struct.unpack_from("<IIHI", data, offset)
        parts = data[pointer:pointer + length].decode("utf-8", "replace").split("|")
        if len(parts) >= 3 and parts[0] == "中国" and parts[2] not in ("", "0"):
            found.setdefault(f"{_bare(parts[1])}|{_bare(parts[2])}", parts[2])
    return found


def gazetteer(path: str) -> dict[str, list[tuple]]:
    """Chinese name -> [(rank, -population, lat, lon)] for places in China."""
    index: dict[str, list[tuple]] = {}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        f = line.split("\t")
        if len(f) < 15 or f[8] not in CHINA:
            continue
        entry = (RANK.get(f[7], 9), -int(f[14] or 0), float(f[4]), float(f[5]))
        for name in {f[1], *f[3].split(",")}:
            if any("一" <= ch <= "鿿" for ch in name):
                index.setdefault(name, []).append(entry)
    return index


def main() -> int:
    names = cities_in(Path(sys.argv[1]).read_bytes())
    places = gazetteer(sys.argv[2])
    cities, missing = {}, []
    for key, written in sorted(names.items()):
        if key in MANUAL:
            cities[key] = MANUAL[key]
            continue
        province, bare = key.split("|")
        capital = PROVINCES.get(province)
        candidates: list[tuple] = []
        seat = SEATS.get(bare) or SEATS.get(written) or ""
        tried = [seat, seat + "市", seat + "镇"] if seat else []
        for name in dict.fromkeys(tried + [written, bare, bare + "市", bare + "县", bare + "区"]):
            reach = WIDE.get(province, MAX_KM_FROM_CAPITAL)
            found = [c for c in places.get(name, []) if not capital or km(capital, (c[2], c[3])) <= reach]
            if found:
                candidates = found
                break
        if not candidates:
            missing.append(key)
            continue
        best = min(candidates)
        cities[key] = [round(best[2], 3), round(best[3], 3)]
    out = ROOT / "agent_nexus" / "geo_cn_cities.json"
    out.write_text(json.dumps({  # LF on every platform: the file is committed
        "_source": "ip2region city names x GeoNames cities1000 coordinates "
                   "(CC BY 4.0, https://www.geonames.org); scripts/build_cn_cities.py",
        "cities": cities}, ensure_ascii=False, indent=0, sort_keys=True) + "\n", encoding="utf-8", newline="\n")
    print(f"{len(cities)} of {len(names)} cities written to {out.relative_to(ROOT)}; {len(missing)} fall back to the province")
    print("  e.g.", missing[:25])
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""Anchor the draft campus graph onto a real geocoded building.

What this can and cannot do, measured 2026-09-19:

- Google Geocoding returns ROOFTOP points for named buildings (資訊系館, 圖書館).
- It does NOT know entrances, elevators, ramps, junctions or bus stops. 學生宿舍
  collapses to the campus centroid, and 成大公車站 comes back APPROXIMATE with the
  address "台灣臺南市". Those results are unusable and are not used.

So the graph is not "geocoded". One building is geocoded and the whole hand-built
layout is *translated* onto it, preserving every relative position.

A two-anchor similarity fit was tried first and rejected: forcing both 資訊系館
and 圖書館 to land exactly produced a 45.9 degree rotation and a 1.102x scale,
moving nodes by 396 m on average and throwing the off-campus origin 1871 m away.
That is not a correction, it is a different kind of wrong — and on a Google
basemap it would look authoritative. Translation applies one honest offset and
distorts nothing. The second anchor is still geocoded and recorded, as a
measurement of how far the draft layout is from reality.

`draft` stays true, and every node records how its coordinate was obtained.

None of this changes any ETA: the router works from each edge's `length_m`, not
from coordinates. This is a map-display fix.

    .venv/Scripts/python.exe scripts/fix_graph_coordinates.py --dry-run
    .venv/Scripts/python.exe scripts/fix_graph_coordinates.py
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import httpx  # noqa: E402

from app.config import get_settings  # noqa: E402

GEOCODE_URL = "https://maps.googleapis.com/maps/api/geocode/json"

# node-id prefix -> what to ask Google for. Only entities Google actually knows.
ANCHORS = {
    "CSIE": "國立成功大學資訊工程學系",
    "LIB": "國立成功大學圖書館",
}
ACCEPTABLE_LOCATION_TYPES = {"ROOFTOP", "GEOMETRIC_CENTER"}

METRES_PER_DEGREE_LAT = 110_540.0
METRES_PER_DEGREE_LNG = 111_320.0

REJECTED_NOTE = (
    "兩點相似變換：會產生 45.9 度旋轉與 1.102 倍縮放，節點平均位移 396 公尺、"
    "最大 1871 公尺。為了對齊兩個點而扭曲整張圖，不採用。"
)
CAVEAT = (
    "只有錨點建築是 geocode 來的。入口、電梯、斜坡、路口、宿舍與公車站 Google "
    "索引沒有，仍是手繪的估計位置，只是整體平移到了正確的錨點上。"
    "座標不影響 ETA：路徑計算使用每段的 length_m。"
)


@dataclass(frozen=True)
class Point:
    lat: float
    lng: float


def to_metres(p: Point, lat0: float) -> tuple[float, float]:
    return (
        p.lng * METRES_PER_DEGREE_LNG * math.cos(math.radians(lat0)),
        p.lat * METRES_PER_DEGREE_LAT,
    )


def to_degrees(x: float, y: float, lat0: float) -> Point:
    return Point(
        lat=y / METRES_PER_DEGREE_LAT,
        lng=x / (METRES_PER_DEGREE_LNG * math.cos(math.radians(lat0))),
    )


def geocode(query: str, key: str) -> tuple[Point, str] | None:
    response = httpx.get(
        GEOCODE_URL,
        params={"address": query, "key": key, "language": "zh-TW", "region": "tw"},
        timeout=40,
    )
    body = response.json()
    if body.get("status") != "OK":
        print(f"  x {query}: {body.get('status')} {body.get('error_message', '')}")
        return None
    result = body["results"][0]
    location_type = result["geometry"].get("location_type", "")
    if location_type not in ACCEPTABLE_LOCATION_TYPES:
        print(f"  x {query}: location_type={location_type}，精度不足，不採用")
        return None
    loc = result["geometry"]["location"]
    return Point(loc["lat"], loc["lng"]), location_type


def centroid(points: list[Point]) -> Point:
    return Point(
        sum(p.lat for p in points) / len(points),
        sum(p.lng for p in points) / len(points),
    )


def offset_between(src: Point, dst: Point, lat0: float) -> tuple[float, float]:
    sx, sy = to_metres(src, lat0)
    dx, dy = to_metres(dst, lat0)
    return dx - sx, dy - sy


def translated(p: Point, offset: tuple[float, float], lat0: float) -> Point:
    px, py = to_metres(p, lat0)
    return to_degrees(px + offset[0], py + offset[1], lat0)


def metres_between(a: Point, b: Point, lat0: float) -> float:
    ax, ay = to_metres(a, lat0)
    bx, by = to_metres(b, lat0)
    return math.hypot(ax - bx, ay - by)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dry-run", action="store_true", help="只顯示會怎麼改，不寫檔")
    parser.add_argument("--anchor", default="CSIE", help="用哪一群建築當平移錨點")
    args = parser.parse_args()

    settings = get_settings()
    if not settings.has_maps:
        print("MAPS_API_KEY 未設定", file=sys.stderr)
        return 1

    path = settings.graph_path
    graph = json.loads(path.read_text(encoding="utf-8"))
    nodes = graph["nodes"]

    print("geocoding 錨點：")
    anchors: dict[str, tuple[Point, str]] = {}
    for prefix, query in ANCHORS.items():
        found = geocode(query, settings.maps_api_key or "")
        if found is not None:
            anchors[prefix] = found
            print(f"  v {query} -> {found[0].lat:.5f}, {found[0].lng:.5f}  ({found[1]})")

    if args.anchor not in anchors:
        print(f"錨點 {args.anchor} 無法 geocode", file=sys.stderr)
        return 1

    lat0 = anchors[args.anchor][0].lat

    def group_centroid(prefix: str) -> Point | None:
        group = [Point(n["lat"], n["lng"]) for n in nodes if n["id"].startswith(prefix + "_")]
        return centroid(group) if group else None

    src = group_centroid(args.anchor)
    if src is None:
        print(f"草稿圖資沒有 {args.anchor}_* 節點", file=sys.stderr)
        return 1

    offset = offset_between(src, anchors[args.anchor][0], lat0)
    distance = math.hypot(*offset)
    print(f"\n純平移：整張圖移動 {distance:.0f} 公尺，錨定於 {args.anchor}")
    print("  （相對位置全部保留，不旋轉、不縮放）")

    # How far the draft layout is from reality, measured on the other anchor.
    residuals = []
    for prefix, (target, _kind) in anchors.items():
        if prefix == args.anchor:
            continue
        got = group_centroid(prefix)
        if got is None:
            continue
        residual = metres_between(translated(got, offset, lat0), target, lat0)
        residuals.append({"prefix": prefix, "residual_m": round(residual)})
        print(
            f"  平移後 {prefix} 群與實際位置仍差 {residual:.0f} 公尺"
            "（草稿佈局與真實幾何的落差，要人工測繪才能消除）"
        )

    if args.dry_run:
        print("\n前幾個節點的變化：")
        for node in nodes[:4]:
            after = translated(Point(node["lat"], node["lng"]), offset, lat0)
            print(
                f"    {node['id']:18s} {node['lat']:.5f},{node['lng']:.5f}"
                f" -> {after.lat:.5f},{after.lng:.5f}"
            )
        print("\n--dry-run：沒有寫檔")
        return 0

    for node in nodes:
        after = translated(Point(node["lat"], node["lng"]), offset, lat0)
        node["lat"] = round(after.lat, 6)
        node["lng"] = round(after.lng, 6)
        # Every node is `derived`: the anchor pins the group's centroid, not
        # any individual entrance or elevator, which are still hand-placed.
        node["coordinate_source"] = "derived"

    graph["coordinate_basis"] = {
        "method": "single-anchor translation",
        "anchor": args.anchor,
        "geocoded_at": "2026-09-19",
        "translation_m": round(distance),
        "anchors": [
            {
                "prefix": prefix,
                "query": ANCHORS[prefix],
                "lat": point.lat,
                "lng": point.lng,
                "location_type": kind,
            }
            for prefix, (point, kind) in anchors.items()
        ],
        "residuals_after_translation": residuals,
        "rejected": REJECTED_NOTE,
        "caveat": CAVEAT,
    }
    graph["note"] = (
        "DRAFT. One building anchor is geocoded and the hand-built layout is "
        "translated onto it; relative positions are unchanged and unsurveyed. "
        "Coordinates affect the map only - the planner uses each edge's length_m. "
        "See coordinate_basis."
    )
    path.write_text(json.dumps(graph, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(f"\n已寫入 {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

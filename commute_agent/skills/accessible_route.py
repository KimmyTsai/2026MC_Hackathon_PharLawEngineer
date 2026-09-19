"""Skill 層：校內最後一段的無障礙路線。

Google Maps 能把人帶到大樓門口，帶不到「哪個入口進得去、哪台電梯還能用、
下雨要走哪條有頂蓋的路」。這個 Skill 補的就是那最後一百公尺。

三條不可違反的規則：

1. 硬安全規則不因使用者偏好而放寬。輪椅與拐杖不走階梯，輪椅不走陡坡，
   下雨時拐杖也不走陡坡。這些是直接排除，不是加權。
2. 時間一律用每段路的 `length_m` 除以該 profile 的速度算，不看座標、
   也不讓模型估。電梯每次固定加 60 秒。
3. 走不到就說走不到。沒有圖資、沒有可行路線、設施狀態不確定，都要明講，
   不給一條看起來可行但其實不能走的路線。

速度與加權出自 CampusPulse 的規格（CLAUDE.md 5.3）。
"""

from __future__ import annotations

import heapq
from dataclasses import dataclass
from datetime import datetime

from commute_agent.tools.accessible_map import (
    FACILITY_STATUS,
    CampusMap,
    Edge,
    load_campus_map,
)

# 步行速度（公尺／秒）
PROFILE_SPEED_MPS = {
    "default": 1.3,
    "crutches": 0.6,
    "wheelchair": 0.8,
}
PROFILE_LABELS = {
    "default": "一般",
    "crutches": "拄拐杖",
    "wheelchair": "輪椅",
}

# 下雨時沒有頂蓋的路段要多花多少時間
RAIN_UNCOVERED_MULTIPLIER = {"default": 1.1, "crutches": 2.0, "wheelchair": 1.5}
# 陡坡對拐杖的額外負擔（輪椅是直接禁行）
STEEP_MULTIPLIER = {"default": 1.0, "crutches": 2.0, "wheelchair": 1.0}

PROFILES_NO_STAIRS = {"crutches", "wheelchair"}
PROFILES_NO_STEEP = {"wheelchair"}
PROFILES_NO_STEEP_IN_RAIN = {"crutches", "wheelchair"}

ELEVATOR_SECONDS = 60
DEFAULT_ENTRY = "BUS_STOP_CHENGDA"


class NoRouteError(Exception):
    """這個 profile 在目前條件下走不到。"""


@dataclass(frozen=True)
class Conditions:
    raining: bool = False
    flooded: bool = False


def _edge_seconds(edge: Edge, profile: str, conditions: Conditions,
                  blocked: set[str]) -> float | None:
    """走這段路要幾秒；這個 profile 不能走就回 None。"""
    if edge.mode != "walk":
        return None            # 公車與 YouBike 路段由組員的 trip_plan 處理
    if edge.id in blocked:
        return None
    if edge.stairs and profile in PROFILES_NO_STAIRS:
        return None

    steep = edge.slope == "steep"
    if steep and profile in PROFILES_NO_STEEP:
        return None
    if steep and conditions.raining and profile in PROFILES_NO_STEEP_IN_RAIN:
        return None

    seconds = edge.length_m / PROFILE_SPEED_MPS[profile]
    if steep:
        seconds *= STEEP_MULTIPLIER[profile]
    if conditions.raining and not edge.covered:
        seconds *= RAIN_UNCOVERED_MULTIPLIER[profile]
    return seconds


def _node_penalty(campus: CampusMap, node_id: str, profile: str,
                  blocked: set[str]) -> float | None:
    if node_id in blocked:
        return None
    node = campus.node(node_id)
    if not node.step_free and profile in PROFILES_NO_STAIRS:
        return None
    return float(ELEVATOR_SECONDS) if node.type == "elevator" else 0.0


def _shortest_path(campus: CampusMap, origin: str, destination: str, profile: str,
                   conditions: Conditions, blocked: set[str]) -> list[Edge]:
    """Dijkstra。自己寫是為了不替組員的專案多加一個相依套件。

    圖上同兩點之間可能有多條路（短的階梯捷徑與長的有頂蓋走廊），所以走訪的
    是路段而不是節點。
    """
    if _node_penalty(campus, origin, profile, blocked) is None:
        raise NoRouteError(f"起點 {campus.node(origin).name} 不符合{PROFILE_LABELS[profile]}的條件")
    if _node_penalty(campus, destination, profile, blocked) is None:
        raise NoRouteError(f"終點 {campus.node(destination).name} 不符合{PROFILE_LABELS[profile]}的條件")

    best: dict[str, float] = {origin: 0.0}
    came_from: dict[str, tuple[str, Edge]] = {}
    queue: list[tuple[float, str]] = [(0.0, origin)]
    seen: set[str] = set()

    while queue:
        cost, current = heapq.heappop(queue)
        if current in seen:
            continue
        seen.add(current)
        if current == destination:
            break

        for edge in campus.neighbours[current]:
            nxt = edge.to_id if edge.from_id == current else edge.from_id
            if nxt in seen:
                continue
            seconds = _edge_seconds(edge, profile, conditions, blocked)
            if seconds is None:
                continue
            penalty = _node_penalty(campus, nxt, profile, blocked)
            if penalty is None:
                continue
            candidate = cost + seconds + penalty
            if candidate < best.get(nxt, float("inf")):
                best[nxt] = candidate
                came_from[nxt] = (current, edge)
                heapq.heappush(queue, (candidate, nxt))

    if destination not in best:
        raise NoRouteError(
            f"{PROFILE_LABELS[profile]}在目前條件下沒有可行路線"
        )

    path: list[Edge] = []
    cursor = destination
    while cursor != origin:
        previous, edge = came_from[cursor]
        path.append(edge)
        cursor = previous
    path.reverse()
    return path


@dataclass(frozen=True)
class Target:
    """要走到哪個節點，以及走到之後還差什麼。

    圖資不必為每個樓層都畫節點——電梯本身就知道自己到得了哪些樓層。所以
    目標可能是「某樓層的節點」，也可能是「電梯，再搭上去」。分不出來時要
    照實說路線只到大樓入口，不能標著 2F 卻把人放在門口。
    """

    node_id: str
    floor_label: str
    ride_to_floor: str = ""
    floor_note: str = ""


def _resolve_targets(campus: CampusMap, spec: dict, floor: str,
                     blocked: set[str]) -> list[Target]:
    """課表寫的樓層（"4F"、"4"、"四樓"）對到可以走到的目標，依偏好排序。"""
    digits = "".join(ch for ch in str(floor) if ch.isdigit())
    label = f"{digits}F" if digits else ""
    floor_nodes = spec.get("floor_node", {})
    entrances = [e for e in spec.get("entrances", []) if e not in blocked]
    fallback = entrances[0] if entrances else (spec.get("entrances") or [""])[0]

    if digits and digits in floor_nodes:
        return [Target(floor_nodes[digits], label)]

    if digits:
        # 沒有這一層的節點，但如果有電梯到得了，就走到電梯再搭上去。
        reachable = [
            node_id
            for node_id in spec.get("elevators", [])
            if node_id not in blocked and int(digits) in campus.node(node_id).floors
        ]
        if reachable:
            return [
                Target(node_id, label, ride_to_floor=label,
                       floor_note=f"圖資沒有畫 {label} 的節點，路線到電梯為止，再搭電梯上去")
                for node_id in reachable
            ]
        return [
            Target(fallback, "", floor_note=(
                f"圖資沒有 {label} 的資料，也沒有到得了 {label} 的電梯，"
                "路線只到大樓入口"
            ))
        ]

    return [Target(fallback, "")]


def plan_accessible_route(
    destination_building: str,
    floor: str = "",
    profile: str = "default",
    origin: str = "",
    raining: bool = False,
    flooded: bool = False,
    now: datetime | None = None,
) -> dict:
    """規劃校內走到教室的無障礙路線。

    destination_building 用 lookup_room 回傳的 building_name（例如
    「B501 資訊工程系館」）。沒有圖資的大樓會明確回報 no_map，不會猜。
    """
    profile = profile if profile in PROFILE_SPEED_MPS else "default"
    conditions = Conditions(raining=raining, flooded=flooded)
    campus = load_campus_map()
    moment = now or datetime.now()
    blocked = FACILITY_STATUS.blocked_ids(moment)
    unconfirmed = FACILITY_STATUS.unconfirmed_ids(moment)

    found = campus.find_building(destination_building)
    if found is None:
        return {
            "status": "no_map",
            "query": destination_building,
            "profile": profile,
            "profile_label": PROFILE_LABELS[profile],
            "covered_buildings": campus.covered_buildings(),
            "note": campus.coverage_note,
        }

    code, spec = found
    start = origin or DEFAULT_ENTRY
    if start not in campus.nodes:
        start = DEFAULT_ENTRY

    targets = _resolve_targets(campus, spec, floor, blocked)

    # 可能有多個目標（例如兩台電梯都到得了那層），走最快的那個。
    best: tuple[list[Edge], Target] | None = None
    failure: NoRouteError | None = None
    for candidate in targets:
        try:
            path = _shortest_path(campus, start, candidate.node_id, profile,
                                  conditions, blocked)
        except NoRouteError as exc:
            failure = exc
            continue
        cost = sum(_edge_seconds(e, profile, conditions, blocked) or 0.0 for e in path)
        if best is None or cost < sum(
            _edge_seconds(e, profile, conditions, blocked) or 0.0 for e in best[0]
        ):
            best = (path, candidate)

    if best is None:
        exc = failure or NoRouteError("沒有可行路線")
        closed = sorted(blocked & (set(campus.nodes) | set(campus.edges)))
        return {
            "status": "no_route",
            "profile": profile,
            "profile_label": PROFILE_LABELS[profile],
            "building": spec.get("display", code),
            "reason": str(exc),
            "blocked_facilities": [
                campus.nodes[i].name if i in campus.nodes else i for i in closed
            ],
            "note": "沒有可行路線時不會給替代猜測，請改問人或改走其他出入口。",
        }

    path, target = best
    floor_label = target.floor_label

    # 逐段組出可以唸給人聽的步驟
    steps, cursor = [], start
    distance = covered_m = 0.0
    seconds = 0.0
    elevators: list[str] = []

    for edge in path:
        nxt = edge.to_id if edge.from_id == cursor else edge.from_id
        edge_seconds = _edge_seconds(edge, profile, conditions, blocked) or 0.0
        node = campus.node(nxt)
        penalty = _node_penalty(campus, nxt, profile, blocked) or 0.0

        marks = []
        if edge.covered:
            marks.append("有頂蓋")
        if edge.slope == "gentle":
            marks.append("緩坡")
        if edge.indoor:
            marks.append("室內")
        if node.type == "elevator":
            marks.append(f"搭電梯（{ELEVATOR_SECONDS} 秒）")
            elevators.append(node.name)

        steps.append({
            "from": campus.node(cursor).name,
            "to": node.name,
            "metres": round(edge.length_m),
            "minutes": round((edge_seconds + penalty) / 60, 1),
            "marks": marks,
            "edge_id": edge.id,
        })

        distance += edge.length_m
        covered_m += edge.length_m if edge.covered else 0.0
        seconds += edge_seconds + penalty
        cursor = nxt

    if target.ride_to_floor:
        node = campus.node(target.node_id)
        steps.append({
            "from": node.name,
            "to": f"{target.ride_to_floor}",
            "metres": 0,
            "minutes": round(ELEVATOR_SECONDS / 60, 1),
            "marks": [f"搭電梯至 {target.ride_to_floor}（{ELEVATOR_SECONDS} 秒）"],
            "edge_id": "",
        })
        seconds += ELEVATOR_SECONDS
        if node.name not in elevators:
            elevators.append(node.name)

    notes = []
    if target.floor_note:
        notes.append(target.floor_note)
    if conditions.raining:
        notes.append(f"下雨：無遮蔽路段約 {round(distance - covered_m)} 公尺，已優先選有頂蓋的路")
    for facility in elevators:
        notes.append(f"使用 {facility}")
    for closure_id in sorted(blocked):
        if closure_id in campus.nodes:
            detail = FACILITY_STATUS.describe(closure_id, moment)
            notes.append(
                f"{campus.nodes[closure_id].name}停用"
                f"（{detail['reason'] if detail else '原因未註明'}），已改道"
            )
    for uid in sorted(unconfirmed):
        name = campus.nodes[uid].name if uid in campus.nodes else uid
        notes.append(f"{name} 有未確認的回報，狀態不確定，路線未因此改變")
    if campus.draft:
        notes.append("圖資為草稿，座標未實地測繪；時間由各段長度計算，不受座標影響")

    return {
        "status": "ok",
        "profile": profile,
        "profile_label": PROFILE_LABELS[profile],
        "building": spec.get("display", code),
        "floor": floor_label,
        "origin": campus.node(start).name,
        "destination": campus.node(target.node_id).name,
        "minutes": round(seconds / 60, 1),
        "distance_m": round(distance),
        "covered_ratio": round(covered_m / distance, 2) if distance else 1.0,
        "steps": steps,
        "uses_elevators": elevators,
        "avoids_stairs": profile in PROFILES_NO_STAIRS,
        "notes": notes,
    }

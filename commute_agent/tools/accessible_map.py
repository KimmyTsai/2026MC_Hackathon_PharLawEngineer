"""Tool 層：校園無障礙圖資（入口、電梯、斜坡、階梯、有頂蓋路段）。

這層只做資料存取——讀圖檔、查某棟大樓有哪些入口和電梯、記錄設施停用狀態。
要走哪條路是 Skill 層 `skills/accessible_route.py` 的事。

圖資來源與限制寫在 `data/accessible_campus.json` 的 `coverage_note`：
目前只有資訊工程系館與總圖書館有圖資，其他大樓會明確回報沒有資料。
座標是手繪後對齊 geocode 錨點的估計值，只影響地圖顯示；路徑時間一律
用每段路的 `length_m` 計算。
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import datetime
from functools import lru_cache
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent.parent
GRAPH_PATH = PROJECT_ROOT / "data" / "accessible_campus.json"

# 低於這個信心值的回報只標示「不確定」，不會讓路線改道。
MIN_ACTIONABLE_CONFIDENCE = 0.7


class GraphDataError(ValueError):
    """圖資檔案本身有問題（缺節點、路段指向不存在的節點）。"""


@dataclass(frozen=True)
class Node:
    id: str
    name: str
    type: str
    lat: float
    lng: float
    step_free: bool = True
    building: str = ""
    floor: str = ""
    floors: tuple[int, ...] = ()
    source: str = ""
    updated_at: str = ""


@dataclass(frozen=True)
class Edge:
    id: str
    from_id: str
    to_id: str
    length_m: float
    stairs: bool = False
    slope: str = "flat"
    covered: bool = False
    indoor: bool = False
    mode: str = "walk"
    source: str = ""
    updated_at: str = ""


@dataclass
class CampusMap:
    nodes: dict[str, Node]
    edges: dict[str, Edge]
    buildings: dict[str, dict]
    campus_entries: dict[str, str]
    coverage_note: str
    draft: bool = True
    neighbours: dict[str, list[Edge]] = field(default_factory=dict)

    def node(self, node_id: str) -> Node:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise GraphDataError(f"圖資沒有節點 {node_id!r}") from exc

    def find_building(self, name: str) -> tuple[str, dict] | None:
        """把 lookup_room 回的大樓名稱對到圖資裡的建築。

        成大 GIS 回的是「B501 資訊工程系館」這類字串，課表可能只寫「資訊系館」，
        所以用關鍵詞比對而不是完全相等。對不到就回 None，由呼叫端明講沒有圖資。
        """
        text = (name or "").strip().casefold()
        if not text:
            return None
        for code, spec in self.buildings.items():
            for keyword in spec.get("match", []):
                if keyword.casefold() in text:
                    return code, spec
        return None

    def covered_buildings(self) -> list[str]:
        return [spec.get("display", code) for code, spec in self.buildings.items()]


def _as_node(raw: dict) -> Node:
    return Node(
        id=raw["id"],
        name=raw.get("name", raw["id"]),
        type=raw.get("type", "junction"),
        lat=float(raw.get("lat", 0.0)),
        lng=float(raw.get("lng", 0.0)),
        step_free=bool(raw.get("step_free", True)),
        building=raw.get("building") or "",
        floor=str(raw.get("floor") or ""),
        floors=tuple(raw.get("floors") or ()),
        source=raw.get("source", ""),
        updated_at=raw.get("updated_at", ""),
    )


def _as_edge(raw: dict) -> Edge:
    return Edge(
        id=raw["id"],
        from_id=raw["from"],
        to_id=raw["to"],
        length_m=float(raw["length_m"]),
        stairs=bool(raw.get("stairs", False)),
        slope=raw.get("slope", "flat"),
        covered=bool(raw.get("covered", False)),
        indoor=bool(raw.get("indoor", False)),
        mode=raw.get("mode", "walk"),
        source=raw.get("source", ""),
        updated_at=raw.get("updated_at", ""),
    )


@lru_cache(maxsize=1)
def load_campus_map(path: str = "") -> CampusMap:
    """讀入圖資。結果會快取，測試要換檔案就傳不同的 path。"""
    target = Path(path) if path else GRAPH_PATH
    raw = json.loads(target.read_text(encoding="utf-8"))

    nodes = {n["id"]: _as_node(n) for n in raw["nodes"]}
    edges = {e["id"]: _as_edge(e) for e in raw["edges"]}

    missing = [
        (e.id, endpoint)
        for e in edges.values()
        for endpoint in (e.from_id, e.to_id)
        if endpoint not in nodes
    ]
    if missing:
        raise GraphDataError(f"路段指向不存在的節點：{missing}")

    neighbours: dict[str, list[Edge]] = {node_id: [] for node_id in nodes}
    for edge in edges.values():
        neighbours[edge.from_id].append(edge)
        neighbours[edge.to_id].append(edge)

    return CampusMap(
        nodes=nodes,
        edges=edges,
        buildings=raw.get("buildings", {}),
        campus_entries=raw.get("campus_entries", {}),
        coverage_note=raw.get("coverage_note", ""),
        draft=bool(raw.get("draft", True)),
        neighbours=neighbours,
    )


# --------------------------------------------------------------------------- #
# 設施停用狀態
# --------------------------------------------------------------------------- #
@dataclass
class Closure:
    """某個設施在某段時間不能用。公告與使用者回報都寫進這裡，不改圖檔。"""

    target_id: str
    reason: str
    source: str = "notice"
    confidence: float = 1.0
    valid_from: datetime | None = None
    valid_to: datetime | None = None

    def active_at(self, when: datetime) -> bool:
        if self.valid_from and when < self.valid_from:
            return False
        return not (self.valid_to and when > self.valid_to)


class FacilityStatus:
    """記憶體中的設施狀態覆寫層。

    公告與回報不會改動圖檔，而是疊在上面，這樣可以隨時重設、也能追出每個
    判斷的來源。信心低於 0.7 的回報只標示「不確定」，不會讓路線改道——
    照片看起來被擋住不等於真的不能走。
    """

    def __init__(self) -> None:
        self._closures: list[Closure] = []

    def reset(self) -> None:
        self._closures.clear()

    def add(self, closure: Closure) -> Closure:
        self._closures.append(closure)
        return closure

    def close(
        self,
        target_id: str,
        reason: str,
        *,
        source: str = "notice",
        confidence: float = 1.0,
        valid_from: datetime | None = None,
        valid_to: datetime | None = None,
    ) -> Closure:
        return self.add(
            Closure(target_id, reason, source, confidence, valid_from, valid_to)
        )

    def active(self, when: datetime) -> list[Closure]:
        return [c for c in self._closures if c.active_at(when)]

    def blocked_ids(self, when: datetime) -> set[str]:
        """真的要避開的設施：停用且信心足夠。"""
        return {
            c.target_id
            for c in self.active(when)
            if c.confidence >= MIN_ACTIONABLE_CONFIDENCE
        }

    def unconfirmed_ids(self, when: datetime) -> set[str]:
        """信心不足的回報：要告訴使用者，但不改道。"""
        return {
            c.target_id
            for c in self.active(when)
            if c.confidence < MIN_ACTIONABLE_CONFIDENCE
        }

    def describe(self, target_id: str, when: datetime) -> dict | None:
        found = [c for c in self.active(when) if c.target_id == target_id]
        if not found:
            return None
        latest = found[-1]
        return {
            "target_id": latest.target_id,
            "reason": latest.reason,
            "source": latest.source,
            "confidence": latest.confidence,
            "actionable": latest.confidence >= MIN_ACTIONABLE_CONFIDENCE,
        }


# 單一行程內共用的狀態；web 與 agent 都讀同一份。
FACILITY_STATUS = FacilityStatus()


def report_facility_closed(
    facility_id: str, reason: str, confidence: float = 1.0
) -> dict:
    """回報某個設施不能用（公告或使用者現場回報）。

    facility_id 必須是圖資裡存在的節點或路段，避免把猜出來的代碼寫進狀態。
    confidence 低於 0.7 時只會標示「待確認」，不會讓路線改道。
    """
    campus = load_campus_map()
    if facility_id not in campus.nodes and facility_id not in campus.edges:
        return {
            "status": "unknown_facility",
            "facility_id": facility_id,
            "note": "圖資裡沒有這個設施，未寫入任何狀態",
        }
    confidence = max(0.0, min(1.0, float(confidence)))
    FACILITY_STATUS.close(facility_id, reason, source="user_report", confidence=confidence)
    actionable = confidence >= MIN_ACTIONABLE_CONFIDENCE
    return {
        "status": "recorded",
        "facility_id": facility_id,
        "name": campus.nodes[facility_id].name if facility_id in campus.nodes else facility_id,
        "reason": reason,
        "confidence": confidence,
        "actionable": actionable,
        "note": "已納入路線規劃" if actionable else "信心不足，只會標示不確定，不會改道",
    }


def get_accessible_facilities(building_name: str) -> dict:
    """查某棟大樓有哪些無障礙入口與電梯，以及它們現在能不能用。"""
    campus = load_campus_map()
    found = campus.find_building(building_name)
    if found is None:
        return {
            "status": "no_map",
            "query": building_name,
            "covered_buildings": campus.covered_buildings(),
            "note": campus.coverage_note,
        }

    code, spec = found
    now = datetime.now()
    blocked = FACILITY_STATUS.blocked_ids(now)

    def describe(node_id: str) -> dict:
        node = campus.node(node_id)
        closure = FACILITY_STATUS.describe(node_id, now)
        return {
            "id": node.id,
            "name": node.name,
            "step_free": node.step_free,
            "floors": list(node.floors),
            "available": node_id not in blocked,
            "closure": closure,
            "source": node.source,
            "updated_at": node.updated_at,
        }

    return {
        "status": "ok",
        "building_code": code,
        "building": spec.get("display", code),
        "entrances": [describe(n) for n in spec.get("entrances", [])],
        "elevators": [describe(n) for n in spec.get("elevators", [])],
        "draft_map": campus.draft,
    }

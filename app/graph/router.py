"""Deterministic route search over the accessible campus graph.

Every number here is computed by code, never by a model (CLAUDE.md 2.1). The
model may decide *whether* to re-plan; it may not decide how long a walk takes.

Tunables live at the top of the module so they can be adjusted in one place.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timedelta

import networkx as nx

from app.graph.loader import CampusGraph
from app.graph.status import FacilityStatusStore
from app.models import GraphEdge, MobilityProfile, RouteLeg, Slope, TravelMode

# Walking speed, m/s (CLAUDE.md 5.3).
PROFILE_SPEED_MPS: dict[MobilityProfile, float] = {
    MobilityProfile.wheelchair: 0.8,
    MobilityProfile.crutches: 0.6,
    MobilityProfile.default: 1.3,
}

# Uncovered segments while it is raining cost more.
RAIN_UNCOVERED_MULTIPLIER: dict[MobilityProfile, float] = {
    MobilityProfile.wheelchair: 1.5,
    MobilityProfile.crutches: 2.0,
    MobilityProfile.default: 1.1,
}

# Steep segments: forbidden for wheelchairs always, for crutches in the rain.
STEEP_MULTIPLIER: dict[MobilityProfile, float] = {
    MobilityProfile.wheelchair: 1.0,  # unused: forbidden outright
    MobilityProfile.crutches: 2.0,
    MobilityProfile.default: 1.0,
}

PROFILES_FORBIDDING_STAIRS = {MobilityProfile.wheelchair, MobilityProfile.crutches}
PROFILES_FORBIDDING_STEEP = {MobilityProfile.wheelchair}
PROFILES_FORBIDDING_STEEP_IN_RAIN = {MobilityProfile.wheelchair, MobilityProfile.crutches}

# One elevator ride, including waiting for the car.
ELEVATOR_SECONDS = 60
# Slack added to every plan on top of the commitment's own arrival buffer.
PLAN_BUFFER_SECONDS = 180

RAIN_MM_THRESHOLD = 0.5


class NoRouteError(Exception):
    """No route satisfies this profile's hard constraints."""

    def __init__(self, origin: str, destination: str, reason: str) -> None:
        super().__init__(f"{origin} → {destination}: {reason}")
        self.origin = origin
        self.destination = destination
        self.reason = reason


@dataclass(frozen=True)
class RouteConditions:
    """World state the weights depend on. Derived from signals, never guessed."""

    raining: bool = False
    rain_mm_per_hr: float = 0.0
    flood_level: str = "none"

    @property
    def flooded(self) -> bool:
        return self.flood_level in {"advisory", "warning"}


@dataclass
class RouteResult:
    nodes: list[str]
    edge_ids: list[str]
    distance_m: float
    seconds: float
    covered_ratio: float
    uses_facilities: list[str] = field(default_factory=list)
    notes: list[str] = field(default_factory=list)

    def as_leg(self, mode: TravelMode = TravelMode.walk) -> RouteLeg:
        return RouteLeg(
            mode=mode,
            from_node=self.nodes[0] if self.nodes else "",
            to_node=self.nodes[-1] if self.nodes else "",
            nodes=list(self.nodes),
            distance_m=round(self.distance_m, 1),
            seconds=round(self.seconds, 1),
            covered_ratio=round(self.covered_ratio, 3),
            uses_facilities=list(self.uses_facilities),
            notes=list(self.notes),
        )


class Router:
    """Walking route search for one profile under one set of conditions."""

    def __init__(
        self,
        graph: CampusGraph,
        facilities: FacilityStatusStore,
        profile: MobilityProfile,
        conditions: RouteConditions,
        now: datetime,
    ) -> None:
        self.graph = graph
        self.facilities = facilities
        self.profile = profile
        self.conditions = conditions
        self.now = now
        self.blocked = facilities.blocked_ids(now)
        self.speed = PROFILE_SPEED_MPS[profile]

    # -- constraints ---------------------------------------------------------
    def edge_seconds(self, edge: GraphEdge) -> float | None:
        """Traversal cost in seconds, or None when this profile may not use it."""
        if edge.mode is not TravelMode.walk:
            return None  # transit edges are composed by the planner, not walked
        if edge.id in self.blocked:
            return None
        if edge.stairs and self.profile in PROFILES_FORBIDDING_STAIRS:
            return None

        steep = edge.slope is Slope.steep
        if steep and self.profile in PROFILES_FORBIDDING_STEEP:
            return None
        if steep and self.conditions.raining and self.profile in PROFILES_FORBIDDING_STEEP_IN_RAIN:
            return None

        seconds = edge.length_m / self.speed
        if steep:
            seconds *= STEEP_MULTIPLIER[self.profile]
        if self.conditions.raining and not edge.covered:
            seconds *= RAIN_UNCOVERED_MULTIPLIER[self.profile]
        return seconds

    def node_penalty(self, node_id: str) -> float | None:
        """Cost of passing through a node, or None when it is unusable."""
        if node_id in self.blocked:
            return None
        node = self.graph.node(node_id)
        if not node.step_free and self.profile in PROFILES_FORBIDDING_STAIRS:
            return None
        if node.type.value == "elevator":
            return float(ELEVATOR_SECONDS)
        return 0.0

    def _hop_cost(self, u: str, v: str, keydict: dict) -> float | None:
        """Cheapest usable parallel edge from u to v, plus v's own penalty."""
        entering = self.node_penalty(v)
        if entering is None:
            return None
        best: float | None = None
        for data in keydict.values():
            seconds = self.edge_seconds(data["data"])
            if seconds is not None and (best is None or seconds < best):
                best = seconds
        return None if best is None else best + entering

    def _best_edge(self, u: str, v: str) -> GraphEdge:
        candidates = [
            (self.edge_seconds(d["data"]), d["data"]) for d in self.graph.nx[u][v].values()
        ]
        usable = [(s, e) for s, e in candidates if s is not None]
        if not usable:
            raise NoRouteError(u, v, "no usable edge")
        usable.sort(key=lambda pair: pair[0])
        return usable[0][1]

    # -- search --------------------------------------------------------------
    def route(self, origin: str, destination: str) -> RouteResult:
        self.graph.node(origin)
        self.graph.node(destination)

        if origin == destination:
            return RouteResult([origin], [], 0.0, 0.0, 1.0)

        if self.node_penalty(origin) is None:
            raise NoRouteError(origin, destination, f"起點 {origin} 不符合 {self.profile.value} 條件")
        if self.node_penalty(destination) is None:
            raise NoRouteError(
                origin, destination, f"終點 {destination} 不符合 {self.profile.value} 條件"
            )

        try:
            path = nx.dijkstra_path(self.graph.nx, origin, destination, weight=self._hop_cost)
        except nx.NetworkXNoPath as exc:
            raise NoRouteError(
                origin, destination, f"{self.profile.value} 在目前條件下沒有可行路線"
            ) from exc

        edges = [self._best_edge(u, v) for u, v in zip(path, path[1:])]

        distance = sum(e.length_m for e in edges)
        walk_seconds = sum(self.edge_seconds(e) or 0.0 for e in edges)
        elevators = [n for n in path if self.graph.node(n).type.value == "elevator"]
        seconds = walk_seconds + ELEVATOR_SECONDS * len(elevators)

        covered_m = sum(e.length_m for e in edges if e.covered)
        covered_ratio = covered_m / distance if distance else 1.0

        notes: list[str] = []
        if self.conditions.raining:
            exposed = round(distance - covered_m)
            notes.append(f"下雨：無遮蔽路段約 {exposed} 公尺")
        for elevator in elevators:
            state = self.facilities.status_of(elevator, self.now)
            label = self.graph.node(elevator).name
            if state.source is not None:
                notes.append(f"使用 {label}（{state.status.value}，來源 {state.source.value}）")
            else:
                notes.append(f"使用 {label}")

        unconfirmed = self.facilities.unconfirmed_ids(self.now) & set(path + [e.id for e in edges])
        for target in sorted(unconfirmed):
            notes.append(f"{target} 有未確認的回報，狀態不確定")

        return RouteResult(
            nodes=path,
            edge_ids=[e.id for e in edges],
            distance_m=distance,
            seconds=seconds,
            covered_ratio=covered_ratio,
            uses_facilities=elevators,
            notes=notes,
        )

    def route_to_room(self, origin: str, room_id: str) -> RouteResult:
        return self.route(origin, self.graph.room(room_id).node)


def eta_from(depart_at: datetime, seconds: float, *, buffer_seconds: int = PLAN_BUFFER_SECONDS):
    """Arrival time for a journey of `seconds`, including the planning buffer."""
    return depart_at + timedelta(seconds=seconds + buffer_seconds)


def depart_by(arrive_by: datetime, seconds: float, *, buffer_seconds: int = PLAN_BUFFER_SECONDS):
    """Latest departure that still arrives by `arrive_by`."""
    return arrive_by - timedelta(seconds=seconds + buffer_seconds)


def conditions_from_signals(signals: dict) -> RouteConditions:
    """Translate normalized signals into route conditions. Missing = benign."""
    from app.models import SignalKind

    rain = signals.get(SignalKind.rain)
    flood = signals.get(SignalKind.flood)
    mm = float(rain.value.get("mm_per_hr", 0) or 0) if rain and not rain.error else 0.0
    level = str(flood.value.get("level", "none")) if flood and not flood.error else "none"
    return RouteConditions(
        raining=mm >= RAIN_MM_THRESHOLD, rain_mm_per_hr=mm, flood_level=level
    )

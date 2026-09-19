"""Candidate comparison and plan selection.

Deterministic end to end. The profile decides which travel modes are candidates
at all; each candidate is walking legs from the campus graph plus, where
applicable, one transit leg priced from provider evidence. Safety
disqualifications outrank speed, and every rejection carries a reason.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta

from app.graph.loader import CampusGraph
from app.graph.router import (
    PLAN_BUFFER_SECONDS,
    PROFILES_FORBIDDING_STAIRS,
    NoRouteError,
    RouteConditions,
    Router,
    conditions_from_signals,
)
from app.graph.status import FacilityStatusStore
from app.models import (
    ELIGIBLE_MODES,
    Commitment,
    DecisionRecord,
    GraphEdge,
    MobilityProfile,
    Plan,
    PlanStatus,
    RouteLeg,
    Signal,
    SignalKind,
    SourceMode,
    TravelMode,
    TravelOption,
    Trigger,
)

POLICY_VERSION = "0.2.0"

BIKE_SPEED_MPS = 4.2  # ~15 km/h
BIKE_UNLOCK_SECONDS = 60
BIKE_RAIN_MULTIPLIER = 1.5

PARATRANSIT_SPEED_MPS = 7.0  # ~25 km/h door to door
PARATRANSIT_BOOKING_SECONDS = 15 * 60

# Used only when the bus ETA provider gives us nothing. Always flagged.
FALLBACK_BUS_WAIT_SECONDS = 10 * 60
FALLBACK_BUS_SPEED_MPS = 6.5

# Reliability margin on transit: proportional plus a fixed cost per transit leg.
TRANSIT_MARGIN_RATIO = 0.15
TRANSIT_MARGIN_SECONDS = 60
# Stale or missing evidence widens the margin rather than being ignored.
STALE_EVIDENCE_MARGIN_SECONDS = 120

# Each soft risk note is worth this much equivalent travel time when ranking.
SOFT_RISK_PENALTY_SECONDS = 180

HEAVY_RAIN_MM = 10.0
FLOOD_EXPOSED_MODES = {TravelMode.youbike, TravelMode.walk}

RECHECK_INTERVAL = timedelta(minutes=5)


@dataclass
class PlanningInputs:
    graph: CampusGraph
    facilities: FacilityStatusStore
    profile: MobilityProfile
    origin: str
    signals: dict[SignalKind, Signal]
    now: datetime


def _signal(inputs: PlanningInputs, kind: SignalKind) -> Signal | None:
    signal = inputs.signals.get(kind)
    if signal is None or signal.error:
        return None
    return signal


def _freshness(signal: Signal | None, now: datetime) -> SourceMode:
    return signal.freshness(now) if signal else SourceMode.unavailable


def _transit_edges(graph: CampusGraph, mode: TravelMode) -> list[GraphEdge]:
    return [e for e in graph.edges.values() if e.mode is mode]


def _margin_seconds(transit_seconds: float, transit_legs: int, stale_evidence: int) -> float:
    return (
        transit_seconds * TRANSIT_MARGIN_RATIO
        + TRANSIT_MARGIN_SECONDS * transit_legs
        + STALE_EVIDENCE_MARGIN_SECONDS * stale_evidence
    )


@dataclass
class Candidate:
    """A travel option under construction, before timing is applied."""

    mode: TravelMode
    legs: list[RouteLeg]
    transit_seconds: float = 0.0
    transit_legs: int = 0
    stale_evidence: int = 0
    risk_flags: list[str] = None  # type: ignore[assignment]
    evidence: list[Signal] = None  # type: ignore[assignment]
    disqualified_reason: str | None = None

    def __post_init__(self) -> None:
        if self.risk_flags is None:
            self.risk_flags = []
        if self.evidence is None:
            self.evidence = []


def _disqualified(mode: TravelMode, reason: str) -> Candidate:
    return Candidate(mode=mode, legs=[], disqualified_reason=reason)


# --------------------------------------------------------------------------- #
# per-mode candidate builders
# --------------------------------------------------------------------------- #
def _walk_candidate(
    inputs: PlanningInputs, router: Router, room_id: str, conditions: RouteConditions
) -> Candidate:
    if conditions.flooded:
        return _disqualified(
            TravelMode.walk, f"{conditions.flood_level}：全程暴露在積水路段，安全上不建議步行"
        )
    try:
        route = router.route_to_room(inputs.origin, room_id)
    except NoRouteError as exc:
        return _disqualified(TravelMode.walk, exc.reason)
    candidate = Candidate(mode=TravelMode.walk, legs=[route.as_leg(TravelMode.walk)])
    if conditions.raining:
        candidate.risk_flags.append(f"下雨 {conditions.rain_mm_per_hr:.0f}mm/hr，全程步行暴露時間長")
    return candidate


def _station_candidate(
    inputs: PlanningInputs,
    router: Router,
    room_id: str,
    mode: TravelMode,
    transit_edge: GraphEdge,
    ride_seconds: float,
    wait_seconds: float,
    ride_note: str,
) -> Candidate:
    """Walk to a station, take one transit leg, walk from the far station."""
    destination = inputs.graph.room(room_id).node
    best: Candidate | None = None
    for boarding, alighting in ((transit_edge.from_, transit_edge.to), (transit_edge.to, transit_edge.from_)):
        try:
            access = router.route(inputs.origin, boarding)
            egress = router.route(alighting, destination)
        except NoRouteError:
            continue
        ride = RouteLeg(
            mode=mode,
            from_node=boarding,
            to_node=alighting,
            nodes=[boarding, alighting],
            distance_m=transit_edge.length_m,
            seconds=round(wait_seconds + ride_seconds, 1),
            covered_ratio=1.0 if transit_edge.covered else 0.0,
            notes=[ride_note],
        )
        candidate = Candidate(
            mode=mode,
            legs=[access.as_leg(), ride, egress.as_leg()],
            transit_seconds=wait_seconds + ride_seconds,
            transit_legs=1,
        )
        total = sum(leg.seconds for leg in candidate.legs)
        if best is None or total < sum(leg.seconds for leg in best.legs):
            best = candidate
    if best is None:
        return _disqualified(mode, f"沒有可行的{mode.value}接駁路線")
    return best


def _youbike_candidate(
    inputs: PlanningInputs, router: Router, room_id: str, conditions: RouteConditions
) -> Candidate:
    edges = _transit_edges(inputs.graph, TravelMode.youbike)
    if not edges:
        return _disqualified(TravelMode.youbike, "圖資沒有 YouBike 路段")
    if conditions.flooded:
        return _disqualified(
            TravelMode.youbike, f"{conditions.flood_level}：積水路段騎乘不安全"
        )

    bike = _signal(inputs, SignalKind.bike_availability)
    if bike is None:
        return _disqualified(TravelMode.youbike, "沒有車輛可用數資料，無法確認能借到車")
    bikes = int(bike.value.get("bikes", 0) or 0)
    if bikes <= 0:
        return _disqualified(TravelMode.youbike, "起點站可借車輛為 0 台")

    edge = edges[0]
    ride_seconds = edge.length_m / BIKE_SPEED_MPS
    if conditions.raining:
        ride_seconds *= BIKE_RAIN_MULTIPLIER
    candidate = _station_candidate(
        inputs, router, room_id, TravelMode.youbike, edge,
        ride_seconds=ride_seconds,
        wait_seconds=BIKE_UNLOCK_SECONDS,
        ride_note=f"可借 {bikes} 台（觀測於 {bike.observed_at:%H:%M}）",
    )
    if candidate.disqualified_reason:
        return candidate
    candidate.evidence.append(bike)
    if _freshness(bike, inputs.now) is SourceMode.stale:
        candidate.stale_evidence += 1
        candidate.risk_flags.append("車輛可用數資料已過期")
    if bikes <= 2:
        candidate.risk_flags.append(f"起點站只剩 {bikes} 台，可能借不到")
    if conditions.raining:
        candidate.risk_flags.append(f"下雨 {conditions.rain_mm_per_hr:.0f}mm/hr，騎乘風險與時間都增加")
    return candidate


def _bus_candidate(
    inputs: PlanningInputs,
    router: Router,
    room_id: str,
    conditions: RouteConditions,
    mode: TravelMode,
) -> Candidate:
    edges = _transit_edges(inputs.graph, TravelMode.bus)
    if not edges:
        return _disqualified(mode, "圖資沒有公車路段")
    edge = edges[0]

    eta = _signal(inputs, SignalKind.bus_eta)
    stale = 0
    flags: list[str] = []
    if eta is None:
        wait_seconds = float(FALLBACK_BUS_WAIT_SECONDS)
        ride_seconds = edge.length_m / FALLBACK_BUS_SPEED_MPS
        note = "沒有到站資料，使用保守估計"
        stale += 1
        flags.append("缺少公車到站資料，等待時間為保守估計")
    else:
        if mode is TravelMode.bus_lowfloor and not bool(eta.value.get("low_floor")):
            return _disqualified(mode, "下一班不是低地板公車")
        wait_seconds = float(eta.value.get("eta_minutes", 0) or 0) * 60
        ride_minutes = eta.value.get("ride_minutes")
        ride_seconds = (
            float(ride_minutes) * 60
            if ride_minutes is not None
            else edge.length_m / FALLBACK_BUS_SPEED_MPS
        )
        route_name = eta.value.get("route", "公車")
        note = f"{route_name}，{wait_seconds / 60:.0f} 分後到站（觀測於 {eta.observed_at:%H:%M}）"
        if _freshness(eta, inputs.now) is SourceMode.stale:
            stale += 1
            flags.append("公車到站資料已過期")

    candidate = _station_candidate(
        inputs, router, room_id, mode, edge,
        ride_seconds=ride_seconds, wait_seconds=wait_seconds, ride_note=note,
    )
    if candidate.disqualified_reason:
        return candidate
    candidate.stale_evidence += stale
    candidate.risk_flags.extend(flags)
    if eta is not None:
        candidate.evidence.append(eta)
    if conditions.raining:
        candidate.risk_flags.append("下雨：候車與步行段需要遮蔽")
    if mode is TravelMode.bus and inputs.profile in PROFILES_FORBIDDING_STAIRS:
        candidate.risk_flags.append("非低地板班次：上下車需要階梯")
    return candidate


def _paratransit_candidate(
    inputs: PlanningInputs, router: Router, room_id: str, conditions: RouteConditions
) -> Candidate:
    edges = _transit_edges(inputs.graph, TravelMode.bus)
    if not edges:
        return _disqualified(TravelMode.paratransit, "圖資沒有可用的道路路段")
    edge = edges[0]
    candidate = _station_candidate(
        inputs, router, room_id, TravelMode.paratransit, edge,
        ride_seconds=edge.length_m / PARATRANSIT_SPEED_MPS,
        wait_seconds=float(PARATRANSIT_BOOKING_SECONDS),
        ride_note="復康巴士，含 15 分鐘預約與接送緩衝",
    )
    if candidate.disqualified_reason:
        return candidate
    candidate.risk_flags.append("需事先預約，無法臨時改搭")
    candidate.stale_evidence += 1  # no live availability provider exists yet
    return candidate


BUILDERS = {
    TravelMode.walk: lambda i, r, room, c: _walk_candidate(i, r, room, c),
    TravelMode.youbike: lambda i, r, room, c: _youbike_candidate(i, r, room, c),
    TravelMode.bus: lambda i, r, room, c: _bus_candidate(i, r, room, c, TravelMode.bus),
    TravelMode.bus_lowfloor: lambda i, r, room, c: _bus_candidate(
        i, r, room, c, TravelMode.bus_lowfloor
    ),
    TravelMode.paratransit: lambda i, r, room, c: _paratransit_candidate(i, r, room, c),
}


# --------------------------------------------------------------------------- #
# timing and selection
# --------------------------------------------------------------------------- #
def _floor_minute(when: datetime) -> datetime:
    return when.replace(second=0, microsecond=0)


def _to_option(candidate: Candidate, commitment: Commitment, now: datetime) -> TravelOption:
    option_id = f"opt_{candidate.mode.value}"
    if candidate.disqualified_reason:
        return TravelOption(
            id=option_id,
            mode=candidate.mode,
            legs=[],
            depart_at=now,
            eta=now,
            conservative_eta=now,
            feasible=False,
            disqualified_reason=candidate.disqualified_reason,
            evidence=candidate.evidence,
        )

    total = sum(leg.seconds for leg in candidate.legs)
    margin = _margin_seconds(candidate.transit_seconds, candidate.transit_legs,
                             candidate.stale_evidence)
    conservative_total = total + margin + PLAN_BUFFER_SECONDS

    latest_arrival = commitment.latest_arrival
    depart_at = _floor_minute(latest_arrival - timedelta(seconds=conservative_total))
    if depart_at < now:
        depart_at = _floor_minute(now)

    eta = depart_at + timedelta(seconds=total + PLAN_BUFFER_SECONDS)
    conservative_eta = depart_at + timedelta(seconds=conservative_total)

    feasible = conservative_eta <= latest_arrival
    reason = None
    if not feasible:
        late = math.ceil((conservative_eta - latest_arrival).total_seconds() / 60)
        reason = f"最快也會比需抵達時間晚約 {late} 分鐘"

    return TravelOption(
        id=option_id,
        mode=candidate.mode,
        legs=candidate.legs,
        depart_at=depart_at,
        eta=eta,
        conservative_eta=conservative_eta,
        feasible=feasible,
        disqualified_reason=reason,
        risk_flags=candidate.risk_flags,
        evidence=candidate.evidence,
        provider_modes={s.provider: s.freshness(now) for s in candidate.evidence},
    )


def option_score(option: TravelOption) -> float:
    """Equivalent travel seconds: real time plus a cost for each soft risk.

    Hard safety rules (flood, stairs, steep, no bikes, not low-floor) are
    disqualifications handled in the builders, so they never reach the score.
    Ranking on `conservative_eta` would be meaningless: every option's departure
    is derived backwards from the same required arrival time, so they all land
    within a minute of each other.
    """
    return option.total_seconds + SOFT_RISK_PENALTY_SECONDS * len(option.risk_flags)


def _rank_key(option: TravelOption) -> tuple:
    return (option_score(option), option.mode.value)


def build_options(
    inputs: PlanningInputs, commitment: Commitment, conditions: RouteConditions | None = None
) -> list[TravelOption]:
    conditions = conditions or conditions_from_signals(inputs.signals)
    router = Router(inputs.graph, inputs.facilities, inputs.profile, conditions, inputs.now)
    options: list[TravelOption] = []
    for mode in ELIGIBLE_MODES[inputs.profile]:
        candidate = BUILDERS[mode](inputs, router, commitment.room, conditions)
        options.append(_to_option(candidate, commitment, inputs.now))
    return options


def plan_for(
    inputs: PlanningInputs,
    commitment: Commitment,
    *,
    trigger: Trigger | None = None,
    previous: Plan | None = None,
    model_id: str | None = None,
) -> tuple[Plan | None, DecisionRecord]:
    """Compare every eligible option and select one. Returns (plan, decision).

    `plan` is None only when no option is feasible; the decision record then
    explains why instead of inventing an on-time route.
    """
    conditions = conditions_from_signals(inputs.signals)
    options = build_options(inputs, commitment, conditions)
    now = inputs.now

    feasible = sorted([o for o in options if o.feasible], key=_rank_key)
    selected = feasible[0] if feasible else None

    rejected = [
        {
            "option": o.id,
            "mode": o.mode.value,
            "reason": o.disqualified_reason
            or (
                f"總行程 {o.total_seconds / 60:.0f} 分"
                + (f"（含 {len(o.risk_flags)} 項風險註記）" if o.risk_flags else "")
                + f"，不如 {selected.mode.value} 的 {selected.total_seconds / 60:.0f} 分"
                if selected
                else "未被選中"
            ),
        }
        for o in options
        if selected is None or o.id != selected.id
    ]

    decisive = list(inputs.signals.values())
    assumptions = [
        f"profile={inputs.profile.value}，步行速度 "
        f"{Router(inputs.graph, inputs.facilities, inputs.profile, conditions, now).speed} m/s",
        f"電梯每次 {int(60)} 秒，計畫緩衝 {PLAN_BUFFER_SECONDS // 60} 分鐘",
        f"{commitment.importance.value} 需提前 "
        f"{int(commitment.arrival_buffer.total_seconds() // 60)} 分鐘抵達",
    ]
    if conditions.raining:
        assumptions.append(f"降雨 {conditions.rain_mm_per_hr:.0f}mm/hr，無遮蔽路段加權")
    if conditions.flooded:
        assumptions.append(f"積水等級 {conditions.flood_level}，暴露路段禁用")
    blocked = sorted(inputs.facilities.blocked_ids(now))
    if blocked:
        assumptions.append(f"停用設施：{', '.join(blocked)}")

    if selected is None:
        rationale = (
            f"{commitment.title} 需在 {commitment.latest_arrival:%H:%M} 前抵達 "
            f"{commitment.room}，但目前沒有任何可行方案。"
        )
    else:
        legs = "＋".join(
            f"{leg.mode.value} {leg.seconds / 60:.0f}分" for leg in selected.legs if leg.seconds
        )
        rationale = (
            f"選擇 {selected.mode.value}：{selected.depart_at:%H:%M} 出發，"
            f"保守估計 {selected.conservative_eta:%H:%M} 抵達 {commitment.room}，"
            f"需在 {commitment.latest_arrival:%H:%M} 前到（{commitment.importance.value}）。"
            f"路線 {legs}。"
        )
        if selected.risk_flags:
            rationale += "注意：" + "；".join(selected.risk_flags) + "。"

    next_check = now + RECHECK_INTERVAL
    if selected is not None:
        next_check = min(next_check, max(now, selected.depart_at - RECHECK_INTERVAL))

    decision = DecisionRecord(
        id=f"dec_{commitment.id}_{int(now.timestamp())}",
        created_at=now,
        commitment_id=commitment.id,
        trigger=trigger,
        selected_option=selected.id if selected else None,
        rejected=rejected,
        decisive_signals=decisive,
        assumptions=assumptions,
        rationale=rationale,
        policy_version=POLICY_VERSION,
        model_id=model_id,
        next_check_at=next_check,
    )

    if selected is None:
        return None, decision

    plan = Plan(
        id=f"plan_{commitment.id}_{int(now.timestamp())}",
        commitment_id=commitment.id,
        generated_at=now,
        selected=selected,
        alternatives=[o for o in options if o.id != selected.id],
        status=PlanStatus.active,
        decision_id=decision.id,
        next_check_at=next_check,
    )
    if previous is not None and previous.selected.mode is not selected.mode:
        plan_note = f"由 {previous.selected.mode.value} 改為 {selected.mode.value}"
        decision.rationale = f"{plan_note}。{decision.rationale}"
    return plan, decision

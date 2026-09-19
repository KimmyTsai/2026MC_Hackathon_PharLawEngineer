"""Typed domain models.

Merges the accessibility campus graph from CLAUDE.md with the normalized
commute entities from references/architecture-and-contracts.md. The mobility
profile is the seam: it decides which travel modes are even candidates.
"""

from __future__ import annotations

from datetime import datetime, timedelta
from enum import Enum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


# --------------------------------------------------------------------------- #
# enums
# --------------------------------------------------------------------------- #
class SourceMode(str, Enum):
    """Never let a fixture masquerade as live data."""

    live = "live"
    fixture = "fixture"
    stale = "stale"
    unavailable = "unavailable"


class Provenance(str, Enum):
    official_map = "official_map"
    photo_survey = "photo_survey"
    user_report = "user_report"
    notice = "notice"


class MobilityProfile(str, Enum):
    wheelchair = "wheelchair"
    crutches = "crutches"
    default = "default"


class TravelMode(str, Enum):
    walk = "walk"
    youbike = "youbike"
    bus = "bus"
    bus_lowfloor = "bus_lowfloor"
    paratransit = "paratransit"


class Importance(str, Enum):
    routine = "routine"
    graded = "graded"
    lab = "lab"
    presentation = "presentation"
    exam = "exam"
    meeting = "meeting"


class NodeType(str, Enum):
    entrance = "entrance"
    elevator = "elevator"
    ramp = "ramp"
    junction = "junction"
    dorm = "dorm"
    stop = "stop"
    bike_station = "bike_station"
    offcampus = "offcampus"


class Slope(str, Enum):
    flat = "flat"
    gentle = "gentle"
    steep = "steep"


class FacilityStatus(str, Enum):
    open = "open"
    closed = "closed"
    degraded = "degraded"
    unknown = "unknown"


class SignalKind(str, Enum):
    rain = "rain"
    flood = "flood"
    air_quality = "air_quality"
    bike_availability = "bike_availability"
    bus_eta = "bus_eta"
    facility_notice = "facility_notice"
    departure_state = "departure_state"


class PlanStatus(str, Enum):
    active = "active"
    superseded = "superseded"
    infeasible = "infeasible"


class ActionType(str, Enum):
    send_email = "send_email"
    calendar_write = "calendar_write"
    reminder = "reminder"


class ActionState(str, Enum):
    awaiting_confirmation = "awaiting_confirmation"
    approved = "approved"
    executed = "executed"
    rejected = "rejected"
    expired = "expired"


# Arrival buffer by importance. Deterministic policy, never model-decided.
ARRIVAL_BUFFER_MINUTES: dict[Importance, int] = {
    Importance.routine: 3,
    Importance.graded: 5,
    Importance.lab: 8,
    Importance.presentation: 12,
    Importance.exam: 15,
    Importance.meeting: 8,
}

# Which travel modes a profile may even consider. A wheelchair user cannot ride
# a YouBike, so the multimodal comparison changes shape with the profile.
ELIGIBLE_MODES: dict[MobilityProfile, tuple[TravelMode, ...]] = {
    MobilityProfile.wheelchair: (TravelMode.walk, TravelMode.bus_lowfloor, TravelMode.paratransit),
    MobilityProfile.crutches: (TravelMode.walk, TravelMode.bus_lowfloor, TravelMode.bus),
    MobilityProfile.default: (TravelMode.walk, TravelMode.youbike, TravelMode.bus),
}


# --------------------------------------------------------------------------- #
# campus graph
# --------------------------------------------------------------------------- #
class GraphNode(BaseModel):
    model_config = ConfigDict(extra="forbid")

    id: str
    name: str
    type: NodeType
    building: str | None = None
    floor: int | None = None
    floors: list[int] | None = Field(default=None, description="elevator reachable floors")
    lat: float
    lng: float
    step_free: bool = True
    source: Provenance = Provenance.official_map
    updated_at: str


class GraphEdge(BaseModel):
    model_config = ConfigDict(extra="forbid", populate_by_name=True)

    id: str
    from_: str = Field(alias="from")
    to: str
    length_m: float
    stairs: bool = False
    slope: Slope = Slope.flat
    covered: bool = False
    indoor: bool = False
    mode: TravelMode = TravelMode.walk
    source: Provenance = Provenance.official_map
    updated_at: str


class CampusGraphFile(BaseModel):
    model_config = ConfigDict(extra="ignore")

    draft: bool = True
    nodes: list[GraphNode]
    edges: list[GraphEdge]


class RoomRecord(BaseModel):
    model_config = ConfigDict(extra="forbid")

    room: str
    name: str | None = None
    building: str
    floor: int
    node: str = Field(description="graph node that counts as arrival at this room")
    entrances: list[str]
    elevators: list[str] = Field(default_factory=list)
    source: Provenance = Provenance.official_map
    updated_at: str


class FacilityOverride(BaseModel):
    """Notices and user reports never edit the graph file; they land here."""

    model_config = ConfigDict(extra="forbid")

    target_id: str
    status: FacilityStatus
    reason: str
    source: Provenance
    source_ref: str | None = None
    confidence: float = 1.0
    valid_from: datetime
    valid_to: datetime | None = None
    recorded_at: datetime | None = None

    def active_at(self, when: datetime) -> bool:
        if when < self.valid_from:
            return False
        return self.valid_to is None or when <= self.valid_to


class FacilityState(BaseModel):
    target_id: str
    status: FacilityStatus
    reason: str | None = None
    source: Provenance | None = None
    source_ref: str | None = None
    updated_at: datetime | None = None
    confidence: float = 1.0


# --------------------------------------------------------------------------- #
# commitments and signals
# --------------------------------------------------------------------------- #
class Course(BaseModel):
    model_config = ConfigDict(extra="ignore")

    course: str
    weekday: int = Field(ge=1, le=7)
    start: str
    end: str
    room: str
    note: str | None = None
    importance: Importance = Importance.routine
    confidence: float = 1.0


class Commitment(BaseModel):
    """Where the student must be, and how costly being late is."""

    id: str
    title: str
    start: datetime
    end: datetime
    room: str
    building: str | None = None
    importance: Importance = Importance.routine
    source: str = "timetable_image"
    source_ref: str | None = None
    extraction_confidence: float = 1.0
    user_confirmed: bool = False

    @property
    def arrival_buffer(self) -> timedelta:
        return timedelta(minutes=ARRIVAL_BUFFER_MINUTES[self.importance])

    @property
    def latest_arrival(self) -> datetime:
        return self.start - self.arrival_buffer


class Signal(BaseModel):
    """One normalized observation, always carrying its own freshness."""

    kind: SignalKind
    provider: str
    observed_at: datetime
    valid_until: datetime | None = None
    area: str | None = None
    value: dict[str, Any] = Field(default_factory=dict)
    mode: SourceMode = SourceMode.fixture
    confidence: float = 1.0
    error: str | None = None

    def freshness(self, now: datetime) -> SourceMode:
        if self.error:
            return SourceMode.unavailable
        if self.valid_until is not None and now > self.valid_until:
            return SourceMode.stale
        return self.mode


# --------------------------------------------------------------------------- #
# travel options and plans
# --------------------------------------------------------------------------- #
class RouteLeg(BaseModel):
    mode: TravelMode
    from_node: str
    to_node: str
    nodes: list[str] = Field(default_factory=list)
    distance_m: float = 0.0
    seconds: float = 0.0
    covered_ratio: float = 0.0
    uses_facilities: list[str] = Field(default_factory=list)
    notes: list[str] = Field(default_factory=list)


class TravelOption(BaseModel):
    id: str
    mode: TravelMode
    legs: list[RouteLeg] = Field(default_factory=list)
    depart_at: datetime
    eta: datetime
    conservative_eta: datetime
    feasible: bool = True
    disqualified_reason: str | None = None
    risk_flags: list[str] = Field(default_factory=list)
    evidence: list[Signal] = Field(default_factory=list)
    provider_modes: dict[str, SourceMode] = Field(default_factory=dict)

    @property
    def total_seconds(self) -> float:
        return sum(leg.seconds for leg in self.legs)


class Trigger(BaseModel):
    kind: SignalKind | Literal["schedule_change", "manual"]
    old_value: Any = None
    new_value: Any = None
    materiality: str = ""
    observed_at: datetime
    affected_plan: str | None = None


class DecisionRecord(BaseModel):
    """What the judges (and we) read to understand why the plan changed."""

    id: str
    created_at: datetime
    commitment_id: str
    trigger: Trigger | None = None
    selected_option: str | None = None
    rejected: list[dict[str, str]] = Field(default_factory=list)
    decisive_signals: list[Signal] = Field(default_factory=list)
    assumptions: list[str] = Field(default_factory=list)
    rationale: str = ""
    policy_version: str = "0.1.0"
    model_id: str | None = None
    next_check_at: datetime | None = None
    trace_id: str | None = None


class Plan(BaseModel):
    id: str
    commitment_id: str
    generated_at: datetime
    selected: TravelOption
    alternatives: list[TravelOption] = Field(default_factory=list)
    status: PlanStatus = PlanStatus.active
    decision_id: str | None = None
    next_check_at: datetime | None = None


class ProposedAction(BaseModel):
    """Outward-facing actions stop here until the student confirms."""

    id: str
    type: ActionType
    preview: dict[str, Any]
    requires_authorization: bool = True
    idempotency_key: str
    state: ActionState = ActionState.awaiting_confirmation
    created_at: datetime
    expires_at: datetime | None = None
    executed_at: datetime | None = None
    rejected_at: datetime | None = None


class AgentLogEntry(BaseModel):
    at: datetime
    step: int
    phase: Literal["perceive", "plan", "act", "reflect"]
    summary: str
    tool: str | None = None
    tool_args: dict[str, Any] | None = None
    tool_result_digest: str | None = None
    model_id: str | None = None

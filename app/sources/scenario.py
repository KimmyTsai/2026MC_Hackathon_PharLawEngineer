"""Deterministic scenario timeline.

The scenario file is the single source of truth in replay mode: baseline signal
values plus timed events. Fixture providers read from here, so advancing the
SimClock is the only thing that changes what the agent perceives.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from datetime import date, datetime, time
from pathlib import Path
from typing import Any

from app.config import TAIPEI
from app.models import Course, MobilityProfile, Signal, SignalKind, SourceMode
from app.sources.base import make_signal

# Which event types feed which signal stream.
EVENT_SIGNAL_KIND: dict[str, SignalKind] = {
    "weather_update": SignalKind.rain,
    "bike_status_update": SignalKind.bike_availability,
    "flood_warning": SignalKind.flood,
    "air_quality_update": SignalKind.air_quality,
    "bus_eta_update": SignalKind.bus_eta,
    "user_not_departed": SignalKind.departure_state,
    "notice_received": SignalKind.facility_notice,
}

# Default freshness window per kind, used for events that do not state one.
DEFAULT_VALIDITY_MINUTES: dict[SignalKind, int] = {
    SignalKind.rain: 60,
    SignalKind.bike_availability: 10,
    SignalKind.bus_eta: 5,
    SignalKind.flood: 30,
    SignalKind.air_quality: 60,
    SignalKind.departure_state: 15,
    SignalKind.facility_notice: 24 * 60,
}


@dataclass(frozen=True)
class ScenarioEvent:
    at: datetime
    type: str
    payload: dict[str, Any]
    expect: str | None = None


@dataclass
class ScenarioUser:
    profile: MobilityProfile
    home_node: str
    campus_home_node: str | None = None
    email: str | None = None


@dataclass
class Scenario:
    name: str
    day: date
    start_at: datetime
    user: ScenarioUser
    courses: list[Course]
    mailbox_ids: list[str]
    baseline_signals: list[Signal]
    events: list[ScenarioEvent] = field(default_factory=list)
    description: str = ""
    path: Path | None = None

    # -- signals -------------------------------------------------------------
    def signals_at(self, now: datetime) -> dict[SignalKind, Signal]:
        """Latest known value per signal kind as of `now`, baseline then events."""
        latest: dict[SignalKind, Signal] = {s.kind: s for s in self.baseline_signals}
        for event in self.events:
            if event.at > now:
                break
            kind = EVENT_SIGNAL_KIND.get(event.type)
            if kind is None or kind is SignalKind.facility_notice:
                # Notices become facility overrides, not signal values.
                continue
            latest[kind] = make_signal(
                kind=kind,
                provider=f"{kind.value}_fixture",
                observed_at=event.at,
                value=dict(event.payload),
                mode=SourceMode.fixture,
                valid_for_minutes=DEFAULT_VALIDITY_MINUTES.get(kind),
            )
        return latest

    def signal_at(self, kind: SignalKind, now: datetime) -> Signal | None:
        return self.signals_at(now).get(kind)

    # -- events --------------------------------------------------------------
    def events_between(self, after: datetime, until: datetime) -> list[ScenarioEvent]:
        """Events in (after, until]. Used by the replay runner."""
        return [e for e in self.events if after < e.at <= until]

    def notices_until(self, now: datetime) -> list[str]:
        return [
            e.payload["mail_id"]
            for e in self.events
            if e.type == "notice_received" and e.at <= now and "mail_id" in e.payload
        ]

    def inject(self, event: ScenarioEvent) -> None:
        """Live injection during a demo (judge uploads a photo, etc.)."""
        self.events.append(event)
        self.events.sort(key=lambda e: e.at)


def _parse_clock(day: date, hhmm: str) -> datetime:
    hours, minutes = (int(part) for part in hhmm.split(":", 1))
    return datetime.combine(day, time(hours, minutes), tzinfo=TAIPEI)


def load_scenario(path: Path, schedule_root: Path | None = None) -> Scenario:
    raw = json.loads(path.read_text(encoding="utf-8"))
    day = date.fromisoformat(raw["date"])
    start_at = _parse_clock(day, raw.get("start_at", "00:00"))

    user_raw = raw.get("user", {})
    user = ScenarioUser(
        profile=MobilityProfile(user_raw.get("profile", "default")),
        home_node=user_raw.get("home_node", ""),
        campus_home_node=user_raw.get("campus_home_node"),
        email=user_raw.get("email"),
    )

    courses: list[Course] = []
    schedule_ref = raw.get("schedule")
    if schedule_ref:
        root = schedule_root or path.parent.parent.parent
        schedule_path = Path(schedule_ref)
        if not schedule_path.is_absolute():
            schedule_path = root / schedule_ref
        schedule_raw = json.loads(schedule_path.read_text(encoding="utf-8"))
        courses = [Course.model_validate(c) for c in schedule_raw.get("courses", [])]

    baseline: list[Signal] = []
    for item in raw.get("baseline_signals", []):
        kind = SignalKind(item["kind"])
        baseline.append(
            make_signal(
                kind=kind,
                provider=item.get("provider", f"{kind.value}_fixture"),
                observed_at=_parse_clock(day, item.get("at", raw.get("start_at", "00:00"))),
                value=dict(item.get("value", {})),
                mode=SourceMode.fixture,
                valid_for_minutes=item.get("valid_for_minutes", DEFAULT_VALIDITY_MINUTES.get(kind)),
                area=item.get("area"),
            )
        )

    events = [
        ScenarioEvent(
            at=_parse_clock(day, item["t"]),
            type=item["type"],
            payload=dict(item.get("payload", {})),
            expect=item.get("expect"),
        )
        for item in raw.get("events", [])
    ]
    events.sort(key=lambda e: e.at)

    return Scenario(
        name=raw.get("name", path.stem),
        day=day,
        start_at=start_at,
        user=user,
        courses=courses,
        mailbox_ids=list(raw.get("mailbox", [])),
        baseline_signals=baseline,
        events=events,
        description=raw.get("description", ""),
        path=path,
    )

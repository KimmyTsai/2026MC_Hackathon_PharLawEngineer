"""In-memory day state and the SSE event bus.

Single-user PoC, so this lives in the process. Keep every mutation going through
this class: the timeline the judges read is built from `agent_log`, `decisions`
and `triggers`, and a replay reset must be able to clear all of it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import Any

from app.clock import SimClock
from app.config import Settings
from app.graph.loader import CampusGraph
from app.graph.status import FacilityStatusStore
from app.models import (
    AgentLogEntry,
    Commitment,
    DecisionRecord,
    Plan,
    PlanStatus,
    ProposedAction,
    Signal,
    Trigger,
)
from app.sources.registry import ProviderRegistry
from app.sources.scenario import Scenario


class EventBus:
    """Fan-out to SSE subscribers. Slow clients are dropped, not buffered forever."""

    def __init__(self, maxsize: int = 256) -> None:
        self._subscribers: set[asyncio.Queue[dict[str, Any]]] = set()
        self._maxsize = maxsize

    def subscribe(self) -> asyncio.Queue[dict[str, Any]]:
        queue: asyncio.Queue[dict[str, Any]] = asyncio.Queue(maxsize=self._maxsize)
        self._subscribers.add(queue)
        return queue

    def unsubscribe(self, queue: asyncio.Queue[dict[str, Any]]) -> None:
        self._subscribers.discard(queue)

    def publish(self, event: dict[str, Any]) -> None:
        for queue in list(self._subscribers):
            try:
                queue.put_nowait(event)
            except asyncio.QueueFull:
                self._subscribers.discard(queue)

    @property
    def subscriber_count(self) -> int:
        return len(self._subscribers)


def commitments_from_scenario(scenario: Scenario, graph: CampusGraph) -> list[Commitment]:
    """Courses on the scenario's weekday become the day's commitments."""
    weekday = scenario.day.isoweekday()
    commitments: list[Commitment] = []
    for index, course in enumerate(scenario.courses):
        if course.weekday != weekday:
            continue
        start_h, start_m = (int(p) for p in course.start.split(":", 1))
        end_h, end_m = (int(p) for p in course.end.split(":", 1))
        tz = scenario.start_at.tzinfo
        building = graph.rooms[course.room].building if course.room in graph.rooms else None
        commitments.append(
            Commitment(
                id=f"cmt_{index + 1:02d}",
                title=course.course,
                start=datetime.combine(scenario.day, time(start_h, start_m), tzinfo=tz),
                end=datetime.combine(scenario.day, time(end_h, end_m), tzinfo=tz),
                room=course.room,
                building=building,
                importance=course.importance,
                source="timetable_image",
                source_ref=scenario.name,
                extraction_confidence=course.confidence,
                user_confirmed=False,
            )
        )
    commitments.sort(key=lambda c: c.start)
    return commitments


class AgentState:
    def __init__(
        self,
        settings: Settings,
        scenario: Scenario,
        clock: SimClock,
        graph: CampusGraph,
        facilities: FacilityStatusStore,
        providers: ProviderRegistry,
        bus: EventBus,
    ) -> None:
        self.settings = settings
        self.scenario = scenario
        self.clock = clock
        self.graph = graph
        self.facilities = facilities
        self.providers = providers
        self.bus = bus

        self.commitments: list[Commitment] = commitments_from_scenario(scenario, graph)
        self.plans: dict[str, Plan] = {}
        self.decisions: list[DecisionRecord] = []
        self.triggers: list[Trigger] = []
        self.pending_confirmations: dict[str, ProposedAction] = {}
        self.agent_log: list[AgentLogEntry] = []
        self.handled_events: int = 0
        self._step = 0

    # -- lifecycle -----------------------------------------------------------
    def reset(self) -> None:
        """One-click fixture reset (Stage 6 gate depends on this being complete).

        Rebuilds the clock too: a SimClock never rewinds, so a reset gets a new
        one starting at the scenario's opening time.
        """
        self.clock = SimClock(self.scenario.start_at, speed=self.clock.speed)
        self.facilities.reset()
        self.commitments = commitments_from_scenario(self.scenario, self.graph)
        self.plans.clear()
        self.decisions.clear()
        self.triggers.clear()
        self.pending_confirmations.clear()
        self.agent_log.clear()
        self.handled_events = 0
        self._step = 0

    # -- logging -------------------------------------------------------------
    def log(
        self,
        phase: str,
        summary: str,
        *,
        tool: str | None = None,
        tool_args: dict[str, Any] | None = None,
        tool_result_digest: str | None = None,
        model_id: str | None = None,
    ) -> AgentLogEntry:
        self._step += 1
        entry = AgentLogEntry(
            at=self.clock.now(),
            step=self._step,
            phase=phase,  # type: ignore[arg-type]
            summary=summary,
            tool=tool,
            tool_args=tool_args,
            tool_result_digest=tool_result_digest,
            model_id=model_id,
        )
        self.agent_log.append(entry)
        self.bus.publish({"type": "agent_log", "data": entry.model_dump(mode="json")})
        return entry

    def record_trigger(self, trigger: Trigger) -> None:
        self.triggers.append(trigger)
        self.bus.publish({"type": "trigger", "data": trigger.model_dump(mode="json")})

    def record_decision(self, decision: DecisionRecord) -> None:
        self.decisions.append(decision)
        self.bus.publish({"type": "decision", "data": decision.model_dump(mode="json")})

    def set_plan(self, plan: Plan) -> None:
        previous = self.plans.get(plan.commitment_id)
        if previous is not None and previous.id != plan.id:
            previous.status = PlanStatus.superseded
        self.plans[plan.commitment_id] = plan
        self.bus.publish({"type": "plan", "data": plan.model_dump(mode="json")})

    # -- views ---------------------------------------------------------------
    def next_commitment(self, now: datetime | None = None) -> Commitment | None:
        now = now or self.clock.now()
        upcoming = [c for c in self.commitments if c.end >= now]
        return upcoming[0] if upcoming else None

    def current_signals(self) -> dict[str, Signal]:
        now = self.clock.now()
        return {kind.value: signal for kind, signal in self.providers.fetch_all(now).items()}

    def snapshot(self) -> dict[str, Any]:
        now = self.clock.now()
        signals = self.providers.fetch_all(now)
        return {
            "now": now.isoformat(),
            "mode": self.settings.mode.value,
            "scenario": self.scenario.name,
            "clock": {"running": self.clock.running, "speed": self.clock.speed},
            "profile": self.scenario.user.profile.value,
            "commitments": [c.model_dump(mode="json") for c in self.commitments],
            "next_commitment": (
                self.next_commitment(now).model_dump(mode="json")
                if self.next_commitment(now)
                else None
            ),
            "plans": {k: v.model_dump(mode="json") for k, v in self.plans.items()},
            "pending_confirmations": [
                a.model_dump(mode="json") for a in self.pending_confirmations.values()
            ],
            "facility_overrides": [
                o.model_dump(mode="json") for o in self.facilities.active(now)
            ],
            "signals": {
                kind.value: {
                    **signal.model_dump(mode="json"),
                    "freshness": signal.freshness(now).value,
                }
                for kind, signal in signals.items()
            },
            "provider_modes": self.providers.modes(now),
            "agent_log": [e.model_dump(mode="json") for e in self.agent_log[-50:]],
            "decisions": [d.model_dump(mode="json") for d in self.decisions[-10:]],
            "triggers": [t.model_dump(mode="json") for t in self.triggers[-10:]],
        }

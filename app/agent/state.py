"""In-memory day state and the SSE event bus.

Single-user PoC, so this lives in the process. Keep every mutation going through
this class: the timeline the judges read is built from `agent_log`, `decisions`
and `triggers`, and a replay reset must be able to clear all of it.
"""

from __future__ import annotations

import asyncio
from datetime import datetime, time
from typing import Any

from app.agent.llm import LLM, build_llm
from app.agent.orchestrator import AgentRun, Orchestrator
from app.agent.planner import PlanningInputs, plan_for
from app.clock import SimClock
from app.config import Settings
from app.graph.loader import CampusGraph
from app.graph.status import FacilityStatusStore
from app.models import (
    AgentLogEntry,
    Commitment,
    DecisionRecord,
    FacilityStatus,
    Plan,
    PlanStatus,
    ProposedAction,
    Signal,
    Trigger,
)
from app.sources.registry import ProviderRegistry
from app.sources.scenario import EVENT_SIGNAL_KIND, Scenario, ScenarioEvent


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
        llm: LLM | None = None,
    ) -> None:
        self.settings = settings
        self.scenario = scenario
        self.clock = clock
        self.graph = graph
        self.facilities = facilities
        self.providers = providers
        self.bus = bus
        self.llm = llm or build_llm(settings)

        self.commitments: list[Commitment] = commitments_from_scenario(scenario, graph)
        self.plans: dict[str, Plan] = {}
        self.decisions: list[DecisionRecord] = []
        self.triggers: list[Trigger] = []
        self.pending_confirmations: dict[str, ProposedAction] = {}
        self.agent_log: list[AgentLogEntry] = []
        self.handled_events: int = 0
        self._step = 0
        self._processed_until: datetime = scenario.start_at

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
        self._processed_until = self.scenario.start_at

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

    # -- perception and planning ---------------------------------------------
    def _apply_notice(self, event: ScenarioEvent) -> None:
        """Turn a facility notice into an override.

        M1 reads the `expected_override` block the fixture carries. M2 replaces
        this with Gemini extraction from the mail body; the fixture block then
        becomes the manual fallback required by pipeline.md Stage 2.
        """
        mail_id = event.payload.get("mail_id")
        if not mail_id:
            return
        message = getattr(self.providers.mailbox, "get_message", lambda _: None)(mail_id)
        if not message:
            self.log("perceive", f"公告 {mail_id} 讀不到，略過", tool="mailbox")
            return
        override = message.get("expected_override")
        if not override:
            self.log(
                "perceive",
                f"公告 {mail_id}「{message.get('subject', '')}」與設施無關，不改變任何狀態",
                tool="fixture_notice_extraction",
            )
            return
        valid_to = override.get("valid_to")
        self.facilities.from_notice(
            target_id=override["target_id"],
            status=FacilityStatus(override["status"]),
            reason=override["reason"],
            source_ref=mail_id,
            valid_from=datetime.fromisoformat(override["valid_from"]),
            valid_to=datetime.fromisoformat(valid_to) if valid_to else None,
            recorded_at=event.at,
        )
        self.log(
            "perceive",
            f"公告 {mail_id}：{override['target_id']} {override['status']}（{override['reason']}）",
            tool="fixture_notice_extraction",
            tool_args={"mail_id": mail_id},
            tool_result_digest=f"{override['target_id']}={override['status']}",
        )

    def process_event(self, event: ScenarioEvent) -> Trigger:
        """Perceive one event: update state, record the trigger, log it."""
        if event.type == "notice_received":
            self._apply_notice(event)
        trigger = Trigger(
            kind=EVENT_SIGNAL_KIND.get(event.type, "manual"),
            new_value=event.payload or None,
            materiality=event.expect or event.type,
            observed_at=event.at,
        )
        self.record_trigger(trigger)
        self.log("perceive", f"事件 {event.type} @ {event.at:%H:%M}", tool="scenario")
        self.handled_events += 1
        return trigger

    def apply_due_events(self, until: datetime | None = None) -> list[Trigger]:
        """Process scenario events whose time has passed. Returns the triggers."""
        until = until or self.clock.now()
        triggers = [
            self.process_event(event)
            for event in self.scenario.events_between(self._processed_until, until)
        ]
        self._processed_until = until
        return triggers

    def inject_event(self, event: ScenarioEvent) -> Trigger | None:
        """Add an event during a demo and perceive it right away when it is due.

        The scheduled window is (processed, until], so an event stamped with the
        current instant would otherwise never be picked up — which is exactly
        the case when a judge uploads a photo on stage.
        """
        self.scenario.inject(event)
        if event.at <= self._processed_until:
            return self.process_event(event)
        return None

    def agent_info(self) -> dict[str, Any]:
        """What the badge shows. Never implies a live call that did not happen."""
        return {
            "model": self.llm.model_id or None,
            "available": bool(getattr(self.llm, "available", False)),
            "reason": getattr(self.llm, "reason", None),
            "mode": getattr(self.llm, "mode", "live"),
            "replaying": bool(getattr(self.llm, "replaying", False)),
            "cassette_stale": bool(getattr(self.llm, "stale", False)),
        }

    def run_agent(self, trigger: Trigger | None = None) -> AgentRun:
        """The Gemini loop, with the deterministic planner as the fallback.

        The departure check runs afterwards either way: whether the student is
        late is time arithmetic, not a model judgement, and the demo must reach
        the confirmation dialog even when the model is unavailable.
        """
        run = Orchestrator(self, self.llm).run(trigger)
        from app.agent.actions import check_departure

        check_departure(self, run.plan)
        return run

    def replan(self, trigger: Trigger | None = None) -> Plan | None:
        """Recompute the plan for the next commitment. Deterministic in M1."""
        commitment = self.next_commitment()
        if commitment is None:
            return None
        now = self.clock.now()
        inputs = PlanningInputs(
            graph=self.graph,
            facilities=self.facilities,
            profile=self.scenario.user.profile,
            origin=self.scenario.user.home_node,
            signals=self.providers.fetch_all(now),
            now=now,
        )
        previous = self.plans.get(commitment.id)
        plan, decision = plan_for(inputs, commitment, trigger=trigger, previous=previous)
        self.record_decision(decision)
        if plan is not None:
            self.set_plan(plan)
            self.log("plan", decision.rationale)
        else:
            if previous is not None:
                previous.status = PlanStatus.infeasible
            self.log("plan", decision.rationale)
        return plan

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
            "agent": self.agent_info(),
            "scenario_events": [
                {
                    "at": event.at.isoformat(),
                    "type": event.type,
                    "expect": event.expect,
                    "payload": event.payload,
                    "processed": event.at <= self._processed_until,
                }
                for event in self.scenario.events
            ],
            "agent_log": [e.model_dump(mode="json") for e in self.agent_log[-50:]],
            "latest_decision": (
                self.decisions[-1].model_dump(mode="json") if self.decisions else None
            ),
            "decisions": [d.model_dump(mode="json") for d in self.decisions[-10:]],
            "triggers": [t.model_dump(mode="json") for t in self.triggers[-10:]],
        }

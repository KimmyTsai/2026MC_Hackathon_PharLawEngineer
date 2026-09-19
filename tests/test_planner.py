"""Candidate comparison: eligibility, safety disqualification, ranking, honesty."""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agent.planner import (
    PlanningInputs,
    build_options,
    option_score,
    plan_for,
)
from app.agent.state import commitments_from_scenario
from app.config import TAIPEI
from app.graph.status import FacilityStatusStore
from app.models import (
    ELIGIBLE_MODES,
    FacilityStatus,
    MobilityProfile,
    SignalKind,
    TravelMode,
)


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=TAIPEI)


@pytest.fixture
def commitment(scenario, graph):
    return commitments_from_scenario(scenario, graph)[0]


def make_inputs(
    graph,
    scenario,
    now: datetime,
    profile: MobilityProfile = MobilityProfile.crutches,
    closed: tuple[str, ...] = (),
    signal_overrides: dict | None = None,
) -> PlanningInputs:
    store = FacilityStatusStore()
    for target in closed:
        store.from_notice(
            target_id=target,
            status=FacilityStatus.closed,
            reason="電梯年度保養",
            source_ref="mail_003",
            valid_from=at(8, 0),
            valid_to=at(17, 0),
            recorded_at=at(8, 5),
        )
    signals = dict(scenario.signals_at(now))
    if signal_overrides:
        signals.update(signal_overrides)
    return PlanningInputs(
        graph=graph,
        facilities=store,
        profile=profile,
        origin=scenario.user.home_node,
        signals=signals,
        now=now,
    )


def by_mode(options) -> dict[TravelMode, object]:
    return {o.mode: o for o in options}


# --- eligibility ------------------------------------------------------------
def test_candidate_modes_follow_the_profile(graph, scenario, commitment):
    for profile in MobilityProfile:
        options = build_options(make_inputs(graph, scenario, at(7, 50), profile), commitment)
        assert {o.mode for o in options} == set(ELIGIBLE_MODES[profile])


def test_a_wheelchair_is_never_offered_a_bike(graph, scenario, commitment):
    options = build_options(
        make_inputs(graph, scenario, at(7, 50), MobilityProfile.wheelchair), commitment
    )
    assert TravelMode.youbike not in {o.mode for o in options}


def test_the_default_profile_gets_the_youbike_story(graph, scenario, commitment):
    plan, _ = plan_for(
        make_inputs(graph, scenario, at(7, 50), MobilityProfile.default), commitment
    )
    assert plan is not None
    assert plan.selected.mode is TravelMode.youbike


# --- safety and availability outrank speed ----------------------------------
def test_zero_bikes_makes_the_bike_option_infeasible_not_merely_slower(graph, scenario, commitment):
    before = by_mode(
        build_options(make_inputs(graph, scenario, at(8, 14), MobilityProfile.default), commitment)
    )
    after = by_mode(
        build_options(make_inputs(graph, scenario, at(8, 15), MobilityProfile.default), commitment)
    )
    assert before[TravelMode.youbike].feasible is True
    assert after[TravelMode.youbike].feasible is False
    assert "0 台" in after[TravelMode.youbike].disqualified_reason


def test_flood_advisory_disqualifies_exposed_modes_but_not_the_bus(graph, scenario, commitment):
    options = by_mode(
        build_options(make_inputs(graph, scenario, at(8, 17), MobilityProfile.default), commitment)
    )
    assert options[TravelMode.youbike].feasible is False
    assert options[TravelMode.walk].feasible is False
    assert "積水" in options[TravelMode.walk].disqualified_reason
    assert options[TravelMode.bus].feasible is True


def test_a_non_low_floor_bus_is_refused_for_a_wheelchair(graph, scenario, commitment):
    signal = scenario.signals_at(at(7, 50))[SignalKind.bus_eta]
    not_low_floor = signal.model_copy(update={"value": {**signal.value, "low_floor": False}})
    options = by_mode(
        build_options(
            make_inputs(
                graph,
                scenario,
                at(7, 50),
                MobilityProfile.wheelchair,
                signal_overrides={SignalKind.bus_eta: not_low_floor},
            ),
            commitment,
        )
    )
    assert options[TravelMode.bus_lowfloor].feasible is False
    assert "低地板" in options[TravelMode.bus_lowfloor].disqualified_reason


def test_heavy_rain_alone_does_not_disqualify_the_bus(graph, scenario, commitment):
    options = by_mode(
        build_options(make_inputs(graph, scenario, at(8, 16)), commitment)
    )
    bus = options[TravelMode.bus_lowfloor]
    assert bus.feasible is True
    assert any("下雨" in flag for flag in bus.risk_flags)


# --- the on-campus leg reacts to facility status -----------------------------
def test_a_closed_elevator_changes_the_selected_plan(graph, scenario, commitment):
    before, _ = plan_for(make_inputs(graph, scenario, at(8, 4)), commitment)
    after, _ = plan_for(
        make_inputs(graph, scenario, at(8, 6), closed=("CSIE_W_ELEV",)), commitment
    )
    assert before is not None and after is not None
    used_before = [f for leg in before.selected.legs for f in leg.uses_facilities]
    used_after = [f for leg in after.selected.legs for f in leg.uses_facilities]
    assert used_before == ["CSIE_W_ELEV"]
    assert used_after == ["CSIE_E_ELEV"]
    assert after.selected.depart_at < before.selected.depart_at  # longer walk, leave earlier


def test_rain_moves_the_departure_earlier(graph, scenario, commitment):
    dry, _ = plan_for(make_inputs(graph, scenario, at(8, 10)), commitment)
    wet, _ = plan_for(make_inputs(graph, scenario, at(8, 17)), commitment)
    assert dry is not None and wet is not None
    assert wet.selected.depart_at < dry.selected.depart_at


# --- ranking ----------------------------------------------------------------
def test_ranking_prefers_less_travel_time_for_a_slow_profile(graph, scenario, commitment):
    options = by_mode(build_options(make_inputs(graph, scenario, at(7, 50)), commitment))
    walk, bus = options[TravelMode.walk], options[TravelMode.bus_lowfloor]
    assert walk.total_seconds > bus.total_seconds
    assert option_score(bus) < option_score(walk)


def test_soft_risk_costs_time_but_does_not_veto(graph, scenario, commitment):
    options = by_mode(build_options(make_inputs(graph, scenario, at(7, 50)), commitment))
    plain, low_floor = options[TravelMode.bus], options[TravelMode.bus_lowfloor]
    # Same physical bus, but plain `bus` carries the boarding-difficulty note.
    assert plain.total_seconds == pytest.approx(low_floor.total_seconds)
    assert plain.risk_flags and not low_floor.risk_flags
    assert option_score(low_floor) < option_score(plain)
    assert plain.feasible is True  # flagged, not vetoed


def test_a_stale_signal_widens_the_margin(graph, scenario, commitment):
    fresh = by_mode(build_options(make_inputs(graph, scenario, at(7, 50)), commitment))
    stale = by_mode(build_options(make_inputs(graph, scenario, at(8, 10)), commitment))
    assert any("過期" in flag for flag in stale[TravelMode.bus_lowfloor].risk_flags)
    fresh_margin = (
        fresh[TravelMode.bus_lowfloor].conservative_eta - fresh[TravelMode.bus_lowfloor].eta
    )
    stale_margin = (
        stale[TravelMode.bus_lowfloor].conservative_eta - stale[TravelMode.bus_lowfloor].eta
    )
    assert stale_margin > fresh_margin


# --- honesty ----------------------------------------------------------------
def test_no_feasible_option_yields_no_plan_and_an_explanation(graph, scenario, commitment):
    plan, decision = plan_for(make_inputs(graph, scenario, at(8, 40)), commitment)
    assert plan is None
    assert decision.selected_option is None
    assert "沒有任何可行方案" in decision.rationale
    assert len(decision.rejected) == len(ELIGIBLE_MODES[MobilityProfile.crutches])
    assert all(item["reason"] for item in decision.rejected)


def test_every_rejection_carries_a_reason(graph, scenario, commitment):
    _, decision = plan_for(make_inputs(graph, scenario, at(8, 17)), commitment)
    assert decision.rejected
    for item in decision.rejected:
        assert item["reason"].strip()


def test_the_decision_record_shows_its_working(graph, scenario, commitment):
    _, decision = plan_for(
        make_inputs(graph, scenario, at(8, 17), closed=("CSIE_W_ELEV",)), commitment
    )
    joined = " ".join(decision.assumptions)
    assert "crutches" in joined and "0.6" in joined
    assert "presentation" in joined and "12 分鐘" in joined
    assert "CSIE_W_ELEV" in joined  # the closure is stated, not hidden
    assert decision.decisive_signals  # signals are attached with their timestamps
    assert decision.next_check_at is not None
    assert decision.model_id is None  # nothing here came from a model


def test_arrival_buffer_comes_from_importance(graph, scenario, commitment):
    plan, _ = plan_for(make_inputs(graph, scenario, at(7, 50)), commitment)
    assert plan is not None
    assert commitment.importance.value == "presentation"
    assert commitment.latest_arrival == commitment.start - timedelta(minutes=12)
    assert plan.selected.conservative_eta <= commitment.latest_arrival


def test_planning_is_deterministic(graph, scenario, commitment):
    first, first_decision = plan_for(make_inputs(graph, scenario, at(8, 17)), commitment)
    second, second_decision = plan_for(make_inputs(graph, scenario, at(8, 17)), commitment)
    assert first is not None and second is not None
    assert first.selected.model_dump() == second.selected.model_dump()
    assert first_decision.rationale == second_decision.rationale

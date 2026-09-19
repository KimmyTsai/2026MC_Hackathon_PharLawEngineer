"""Route search: the hard accessibility constraints and the arithmetic.

Every expectation here is a rule from CLAUDE.md 5.3, not a snapshot of whatever
the code happened to produce.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.config import TAIPEI
from app.graph.router import (
    ELEVATOR_SECONDS,
    PLAN_BUFFER_SECONDS,
    PROFILE_SPEED_MPS,
    NoRouteError,
    RouteConditions,
    Router,
    conditions_from_signals,
    depart_by,
    eta_from,
)
from app.graph.status import FacilityStatusStore
from app.models import FacilityStatus, MobilityProfile, SignalKind, Slope, SourceMode
from app.sources.base import make_signal, unavailable_signal

NOW = datetime(2026, 9, 23, 7, 50, tzinfo=TAIPEI)
DRY = RouteConditions()
RAIN = RouteConditions(raining=True, rain_mm_per_hr=15)


def router(graph, profile, conditions=DRY, closed: tuple[str, ...] = ()) -> Router:
    store = FacilityStatusStore()
    for target in closed:
        store.from_notice(
            target_id=target,
            status=FacilityStatus.closed,
            reason="test closure",
            source_ref="mail_003",
            valid_from=NOW,
            valid_to=None,
            recorded_at=NOW,
        )
    return Router(graph, store, profile, conditions, NOW)


# --- hard constraints -------------------------------------------------------
def test_default_profile_may_use_the_stairs_shortcut(graph):
    route = router(graph, MobilityProfile.default).route_to_room("DORM_A", "CSIE-4263")
    assert "e_009" in route.edge_ids or "CSIE_S_ENT" in route.nodes


@pytest.mark.parametrize("profile", [MobilityProfile.crutches, MobilityProfile.wheelchair])
def test_stairs_are_never_used_by_a_stairs_forbidding_profile(graph, profile):
    route = router(graph, profile).route_to_room("DORM_A", "CSIE-4263")
    for edge_id in route.edge_ids:
        assert graph.edge(edge_id).stairs is False, edge_id
    assert "CSIE_S_ENT" not in route.nodes  # the non-step-free entrance
    assert "RAMP_07" in route.nodes  # it takes the ramp instead


def test_wheelchair_refuses_a_steep_segment(graph):
    assert graph.edge("e_014").slope is Slope.steep
    wheelchair = router(graph, MobilityProfile.wheelchair)
    assert wheelchair.edge_seconds(graph.edge("e_014")) is None

    # The same segment is usable by the default profile, so this is the rule
    # talking, not a broken edge.
    assert router(graph, MobilityProfile.default).edge_seconds(graph.edge("e_014")) is not None

    # A detour exists via the east entrance, so the rule shows up as avoidance.
    assert "e_014" not in wheelchair.route("JCT_04", "JCT_05").edge_ids


def test_crutches_may_use_a_steep_segment_when_dry_but_not_in_rain(graph):
    dry = router(graph, MobilityProfile.crutches).edge_seconds(graph.edge("e_014"))
    assert dry is not None
    wet = router(graph, MobilityProfile.crutches, RAIN).edge_seconds(graph.edge("e_014"))
    assert wet is None


def test_steep_costs_double_for_crutches(graph):
    edge = graph.edge("e_014")
    seconds = router(graph, MobilityProfile.crutches).edge_seconds(edge)
    assert seconds == pytest.approx(edge.length_m / 0.6 * 2.0)


# --- conditions -------------------------------------------------------------
def test_rain_switches_to_the_fully_covered_corridor(graph):
    dry = router(graph, MobilityProfile.crutches).route_to_room("DORM_A", "CSIE-4263")
    wet = router(graph, MobilityProfile.crutches, RAIN).route_to_room("DORM_A", "CSIE-4263")
    assert dry.covered_ratio < 1.0
    assert wet.covered_ratio == pytest.approx(1.0)
    assert "e_018" in wet.edge_ids  # the covered corridor
    assert wet.distance_m > dry.distance_m  # it accepts a detour to stay dry


def test_rain_multiplier_matches_the_spec(graph):
    edge = graph.edge("e_006")  # uncovered
    assert edge.covered is False
    base = router(graph, MobilityProfile.wheelchair).edge_seconds(edge)
    wet = router(graph, MobilityProfile.wheelchair, RAIN).edge_seconds(edge)
    assert wet == pytest.approx(base * 1.5)


def test_covered_segments_are_unaffected_by_rain(graph):
    edge = graph.edge("e_005")
    assert edge.covered is True
    assert router(graph, MobilityProfile.crutches, RAIN).edge_seconds(edge) == pytest.approx(
        router(graph, MobilityProfile.crutches).edge_seconds(edge)
    )


# --- facility status --------------------------------------------------------
def test_a_closed_elevator_routes_through_the_other_one(graph):
    before = router(graph, MobilityProfile.crutches).route_to_room("DORM_A", "CSIE-4263")
    after = router(
        graph, MobilityProfile.crutches, closed=("CSIE_W_ELEV",)
    ).route_to_room("DORM_A", "CSIE-4263")
    assert before.uses_facilities == ["CSIE_W_ELEV"]
    assert after.uses_facilities == ["CSIE_E_ELEV"]
    assert after.seconds > before.seconds


def test_both_elevators_closed_means_no_route_not_a_guess(graph):
    blocked = router(graph, MobilityProfile.crutches, closed=("CSIE_W_ELEV", "CSIE_E_ELEV"))
    with pytest.raises(NoRouteError):
        blocked.route_to_room("DORM_A", "CSIE-4263")


def test_a_closed_edge_is_avoided(graph):
    route = router(graph, MobilityProfile.crutches, closed=("e_007",)).route_to_room(
        "DORM_A", "CSIE-4263"
    )
    assert "e_007" not in route.edge_ids


def test_route_notes_cite_the_facility_source(graph):
    route = router(graph, MobilityProfile.crutches, closed=("CSIE_W_ELEV",)).route_to_room(
        "DORM_A", "CSIE-4263"
    )
    assert any("東側電梯" in note for note in route.notes)


def test_low_confidence_report_surfaces_as_uncertainty_not_a_detour(graph):
    store = FacilityStatusStore()
    store.from_report(
        target_id="RAMP_07",
        status=FacilityStatus.closed,
        reason="斜坡疑似被機車擋住",
        source_ref="photo",
        recorded_at=NOW,
        confidence=0.4,
    )
    route = Router(graph, store, MobilityProfile.crutches, DRY, NOW).route_to_room(
        "DORM_A", "CSIE-4263"
    )
    assert "RAMP_07" in route.nodes  # not silently avoided
    assert any("未確認" in note for note in route.notes)


# --- arithmetic -------------------------------------------------------------
def test_walking_time_is_distance_over_profile_speed(graph):
    edge = graph.edge("e_006")
    for profile, speed in PROFILE_SPEED_MPS.items():
        seconds = router(graph, profile).edge_seconds(edge)
        assert seconds == pytest.approx(edge.length_m / speed)


def test_each_elevator_ride_costs_sixty_seconds(graph):
    route = router(graph, MobilityProfile.crutches).route_to_room("DORM_A", "CSIE-4263")
    walking = sum(
        router(graph, MobilityProfile.crutches).edge_seconds(graph.edge(e)) for e in route.edge_ids
    )
    assert route.seconds == pytest.approx(walking + ELEVATOR_SECONDS * len(route.uses_facilities))
    assert len(route.uses_facilities) == 1


def test_eta_and_departure_are_exact_inverses():
    arrive = datetime(2026, 9, 23, 9, 18, tzinfo=TAIPEI)
    seconds = 1234.0
    departure = depart_by(arrive, seconds)
    assert eta_from(departure, seconds) == arrive
    assert (arrive - departure).total_seconds() == seconds + PLAN_BUFFER_SECONDS


def test_same_node_route_is_zero(graph):
    route = router(graph, MobilityProfile.crutches).route("JCT_02", "JCT_02")
    assert route.seconds == 0.0
    assert route.nodes == ["JCT_02"]


def test_identical_inputs_produce_identical_routes(graph):
    first = router(graph, MobilityProfile.crutches).route_to_room("DORM_A", "CSIE-4263")
    second = router(graph, MobilityProfile.crutches).route_to_room("DORM_A", "CSIE-4263")
    assert first.nodes == second.nodes
    assert first.seconds == second.seconds


# --- conditions from signals ------------------------------------------------
def test_conditions_read_rain_and_flood_from_signals():
    signals = {
        SignalKind.rain: make_signal(
            SignalKind.rain, "cwa_fixture", NOW, {"mm_per_hr": 15}, mode=SourceMode.fixture
        ),
        SignalKind.flood: make_signal(
            SignalKind.flood, "flood_fixture", NOW, {"level": "advisory"}
        ),
    }
    conditions = conditions_from_signals(signals)
    assert conditions.raining is True
    assert conditions.rain_mm_per_hr == 15
    assert conditions.flooded is True


def test_missing_signals_are_treated_as_benign_not_invented():
    signals = {
        SignalKind.rain: unavailable_signal(SignalKind.rain, "cwa_live", NOW, "not configured")
    }
    conditions = conditions_from_signals(signals)
    assert conditions.raining is False
    assert conditions.flooded is False


def test_a_trace_of_drizzle_is_not_rain():
    signals = {SignalKind.rain: make_signal(SignalKind.rain, "cwa_fixture", NOW, {"mm_per_hr": 0.2})}
    assert conditions_from_signals(signals).raining is False

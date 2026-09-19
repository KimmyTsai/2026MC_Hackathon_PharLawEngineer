from __future__ import annotations

from datetime import datetime

from app.config import TAIPEI
from app.models import MobilityProfile, SignalKind, SourceMode
from app.sources.scenario import ScenarioEvent


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=TAIPEI)


def test_scenario_loads_user_and_schedule(scenario):
    assert scenario.name == "demo_wed"
    assert scenario.user.profile is MobilityProfile.crutches
    assert scenario.start_at == at(7, 45)
    assert any(c.room == "CSIE-4263" for c in scenario.courses)


def test_baseline_signals_hold_before_events(scenario):
    signals = scenario.signals_at(at(7, 50))
    assert signals[SignalKind.bike_availability].value["bikes"] == 7
    assert signals[SignalKind.rain].value["mm_per_hr"] == 0
    assert signals[SignalKind.rain].mode is SourceMode.fixture


def test_events_replace_signal_values_at_their_time(scenario):
    before = scenario.signals_at(at(8, 14))
    after = scenario.signals_at(at(8, 16))
    assert before[SignalKind.bike_availability].value["bikes"] == 7
    assert after[SignalKind.bike_availability].value["bikes"] == 0
    assert after[SignalKind.rain].value["mm_per_hr"] == 15
    assert after[SignalKind.flood].value["level"] == "advisory"


def test_signal_freshness_goes_stale(scenario):
    signal = scenario.signals_at(at(7, 50))[SignalKind.bus_eta]
    assert signal.freshness(at(7, 48)) is SourceMode.fixture
    assert signal.freshness(at(8, 30)) is SourceMode.stale


def test_notices_are_not_signal_values(scenario):
    signals = scenario.signals_at(at(8, 10))
    assert SignalKind.facility_notice not in signals
    assert scenario.notices_until(at(8, 10)) == ["mail_003"]
    assert scenario.notices_until(at(8, 0)) == []


def test_events_between_is_exclusive_on_the_left(scenario):
    window = scenario.events_between(at(8, 5), at(8, 16))
    types = [e.type for e in window]
    assert "notice_received" not in types
    assert "weather_update" in types and "flood_warning" in types


def test_injection_keeps_events_sorted(scenario):
    scenario.inject(ScenarioEvent(at=at(8, 10), type="manual_check", payload={}))
    times = [e.at for e in scenario.events]
    assert times == sorted(times)

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.clock import RealClock, SimClock
from app.config import TAIPEI

START = datetime(2026, 9, 23, 7, 45, tzinfo=TAIPEI)


def test_paused_sim_clock_is_deterministic():
    clock = SimClock(START)
    assert clock.now() == START
    assert clock.now() == START
    assert not clock.running


def test_advance_by_and_to():
    clock = SimClock(START)
    assert clock.advance_by(600) == START + timedelta(minutes=10)
    target = START + timedelta(minutes=40)
    assert clock.advance_to(target) == target
    assert clock.now() == target


def test_clock_never_rewinds():
    clock = SimClock(START)
    clock.advance_by(600)
    with pytest.raises(ValueError, match="cannot rewind"):
        clock.advance_to(START)
    with pytest.raises(ValueError, match="backwards"):
        clock.advance_by(-1)


def test_naive_datetimes_are_assumed_taipei():
    clock = SimClock(datetime(2026, 9, 23, 7, 45))
    assert clock.now().tzinfo is not None
    assert clock.now() == START


def test_speed_change_preserves_current_time():
    clock = SimClock(START)
    clock.advance_by(300)
    before = clock.now()
    clock.set_speed(60)
    assert clock.now() == before
    assert clock.speed == 60

    with pytest.raises(ValueError):
        clock.set_speed(0)


def test_running_clock_advances_then_pauses():
    clock = SimClock(START, speed=3600)  # 1 wall-clock second = 1 simulated hour
    clock.start()
    assert clock.running
    first = clock.now()
    assert first >= START
    clock.pause()
    paused = clock.now()
    assert clock.now() == paused
    assert not clock.running


def test_real_clock_is_timezone_aware():
    assert RealClock().now().tzinfo is not None

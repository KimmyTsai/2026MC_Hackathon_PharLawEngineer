"""Clocks. Replay uses SimClock so a demo run is reproducible to the second."""

from __future__ import annotations

import time
from datetime import datetime, timedelta
from typing import Protocol, runtime_checkable

from app.config import TAIPEI


@runtime_checkable
class Clock(Protocol):
    def now(self) -> datetime: ...


class RealClock:
    """Wall-clock time in Asia/Taipei."""

    mode = "real"

    def now(self) -> datetime:
        return datetime.now(tz=TAIPEI)


class SimClock:
    """Simulated time.

    Paused by default so tests advance it explicitly. When running, simulated
    time advances at `speed` x wall-clock. Time never moves backwards.
    """

    mode = "sim"

    def __init__(self, start: datetime, speed: float = 1.0) -> None:
        if start.tzinfo is None:
            start = start.replace(tzinfo=TAIPEI)
        if speed <= 0:
            raise ValueError("speed must be positive")
        self._base = start
        self._speed = speed
        self._anchor: float | None = None

    @property
    def speed(self) -> float:
        return self._speed

    @property
    def running(self) -> bool:
        return self._anchor is not None

    def now(self) -> datetime:
        if self._anchor is None:
            return self._base
        elapsed = (time.monotonic() - self._anchor) * self._speed
        return self._base + timedelta(seconds=elapsed)

    def start(self) -> None:
        if self._anchor is None:
            self._anchor = time.monotonic()

    def pause(self) -> None:
        if self._anchor is not None:
            self._base = self.now()
            self._anchor = None

    def set_speed(self, speed: float) -> None:
        if speed <= 0:
            raise ValueError("speed must be positive")
        was_running = self.running
        self.pause()
        self._speed = speed
        if was_running:
            self.start()

    def advance_by(self, seconds: float) -> datetime:
        if seconds < 0:
            raise ValueError("cannot advance backwards")
        was_running = self.running
        self.pause()
        self._base = self._base + timedelta(seconds=seconds)
        if was_running:
            self.start()
        return self._base

    def advance_to(self, target: datetime) -> datetime:
        if target.tzinfo is None:
            target = target.replace(tzinfo=TAIPEI)
        current = self.now()
        if target < current:
            raise ValueError(f"cannot rewind from {current.isoformat()} to {target.isoformat()}")
        was_running = self.running
        self.pause()
        self._base = target
        if was_running:
            self.start()
        return self._base

"""Departure reminders and calendar writes.

In replay mode a reminder is just state the frontend renders — no external
side effect. Real calendar writes are Stage 5 and always preview-first.
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.models import SourceMode
from app.sources.base import Unavailable


class FixtureCalendar:
    name = "calendar_fixture"
    mode = SourceMode.fixture

    def __init__(self) -> None:
        self.reminders: list[dict[str, Any]] = []

    def set_reminder(self, at: datetime, message: str) -> dict[str, Any]:
        reminder = {"at": at.isoformat(), "message": message, "delivered": False}
        self.reminders.append(reminder)
        return reminder

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        return {**payload, "would_write_via": self.name, "written": False}

    def execute(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        entry = {**payload, "idempotency_key": idempotency_key, "written": True}
        self.reminders.append(entry)
        return entry


class GoogleCalendar:
    name = "google_calendar_live"
    mode = SourceMode.unavailable

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]:
        raise Unavailable("google_calendar_live not implemented yet (Stage 5)")

    def execute(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]:
        raise Unavailable("google_calendar_live not implemented yet (Stage 5)")

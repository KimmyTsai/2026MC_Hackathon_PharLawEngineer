"""Provider boundary.

Every external data source sits behind one of these interfaces and has both a
fixture and a live implementation returning the same normalized `Signal`.
A live provider that is not wired up yet raises `Unavailable` — it must never
fall back to fixture data silently (references/pipeline.md, Stage 3).
"""

from __future__ import annotations

from datetime import datetime, timedelta
from typing import Any, Protocol

from app.models import Signal, SignalKind, SourceMode


class SourceError(Exception):
    """Base class for normalized provider errors."""

    code = "error"


class Unavailable(SourceError):
    code = "unavailable"


class Unauthorized(SourceError):
    code = "unauthorized"


class RateLimited(SourceError):
    code = "rate_limited"


class InvalidResponse(SourceError):
    code = "invalid_response"


class Stale(SourceError):
    code = "stale"


def make_signal(
    kind: SignalKind,
    provider: str,
    observed_at: datetime,
    value: dict[str, Any],
    *,
    mode: SourceMode = SourceMode.fixture,
    valid_for_minutes: int | None = None,
    area: str | None = None,
    confidence: float = 1.0,
) -> Signal:
    valid_until = (
        observed_at + timedelta(minutes=valid_for_minutes) if valid_for_minutes is not None else None
    )
    return Signal(
        kind=kind,
        provider=provider,
        observed_at=observed_at,
        valid_until=valid_until,
        area=area,
        value=value,
        mode=mode,
        confidence=confidence,
    )


def unavailable_signal(kind: SignalKind, provider: str, at: datetime, reason: str) -> Signal:
    """An honest 'we do not know' signal. Planner must treat it as missing evidence."""
    return Signal(
        kind=kind,
        provider=provider,
        observed_at=at,
        value={},
        mode=SourceMode.unavailable,
        confidence=0.0,
        error=reason,
    )


class SignalProvider(Protocol):
    """One normalized observation stream."""

    name: str
    kind: SignalKind
    mode: SourceMode

    def fetch(self, at: datetime, area: str | None = None) -> Signal: ...


class MessageProvider(Protocol):
    """Course/facility messages. Read-only in every mode."""

    name: str
    mode: SourceMode

    def read_messages(self, since: datetime | None = None) -> list[dict[str, Any]]: ...


class OutboundProvider(Protocol):
    """Anything that writes to the outside world. Preview is always separate."""

    name: str
    mode: SourceMode

    def preview(self, payload: dict[str, Any]) -> dict[str, Any]: ...

    def execute(self, payload: dict[str, Any], idempotency_key: str) -> dict[str, Any]: ...

"""Facility status overrides.

Notices and user reports never edit data/campus_graph.json. They append an
override with its own validity window and provenance, so a replay can be reset
and every claim the agent makes can be traced back to a source.
"""

from __future__ import annotations

from datetime import datetime

from app.models import FacilityOverride, FacilityState, FacilityStatus, Provenance


class FacilityStatusStore:
    def __init__(self) -> None:
        self._overrides: list[FacilityOverride] = []

    def reset(self) -> None:
        self._overrides.clear()

    def add(self, override: FacilityOverride) -> FacilityOverride:
        self._overrides.append(override)
        return override

    @property
    def overrides(self) -> list[FacilityOverride]:
        return list(self._overrides)

    def active(self, now: datetime) -> list[FacilityOverride]:
        return [o for o in self._overrides if o.active_at(now)]

    def status_of(self, target_id: str, now: datetime) -> FacilityState:
        """Last applicable override wins; absence of evidence means `open`.

        `unknown` is reserved for low-confidence evidence, so the agent can say
        'unconfirmed' instead of silently treating a facility as usable.
        """
        applicable = [o for o in self._overrides if o.target_id == target_id and o.active_at(now)]
        if not applicable:
            return FacilityState(target_id=target_id, status=FacilityStatus.open)
        override = applicable[-1]
        return FacilityState(
            target_id=target_id,
            status=override.status,
            reason=override.reason,
            source=override.source,
            source_ref=override.source_ref,
            updated_at=override.recorded_at or override.valid_from,
            confidence=override.confidence,
        )

    def status_many(self, target_ids: list[str], now: datetime) -> dict[str, FacilityState]:
        return {tid: self.status_of(tid, now) for tid in target_ids}

    def blocked_ids(self, now: datetime, min_confidence: float = 0.7) -> set[str]:
        """Targets a planner must avoid: closed, and confident enough to act on."""
        return {
            o.target_id
            for o in self.active(now)
            if o.status is FacilityStatus.closed and o.confidence >= min_confidence
        }

    def unconfirmed_ids(self, now: datetime, min_confidence: float = 0.7) -> set[str]:
        """Low-confidence reports: surface to the user, do not silently apply."""
        return {
            o.target_id
            for o in self.active(now)
            if o.status is not FacilityStatus.open and o.confidence < min_confidence
        }

    def add_override(
        self,
        *,
        target_id: str,
        status: FacilityStatus,
        reason: str,
        source: Provenance,
        valid_from: datetime,
        valid_to: datetime | None = None,
        recorded_at: datetime | None = None,
        source_ref: str | None = None,
        confidence: float = 1.0,
    ) -> FacilityOverride:
        """Generic entry point. `from_notice` / `from_report` are the named cases."""
        return self.add(
            FacilityOverride(
                target_id=target_id,
                status=status,
                reason=reason,
                source=source,
                source_ref=source_ref,
                confidence=confidence,
                valid_from=valid_from,
                valid_to=valid_to,
                recorded_at=recorded_at,
            )
        )

    def from_notice(
        self,
        target_id: str,
        status: FacilityStatus,
        reason: str,
        source_ref: str,
        valid_from: datetime,
        valid_to: datetime | None,
        recorded_at: datetime,
        confidence: float = 1.0,
    ) -> FacilityOverride:
        return self.add_override(
            target_id=target_id,
            status=status,
            reason=reason,
            source=Provenance.notice,
            source_ref=source_ref,
            confidence=confidence,
            valid_from=valid_from,
            valid_to=valid_to,
            recorded_at=recorded_at,
        )

    def from_report(
        self,
        target_id: str,
        status: FacilityStatus,
        reason: str,
        source_ref: str,
        recorded_at: datetime,
        confidence: float,
        valid_to: datetime | None = None,
    ) -> FacilityOverride:
        return self.add_override(
            target_id=target_id,
            status=status,
            reason=reason,
            source=Provenance.user_report,
            source_ref=source_ref,
            confidence=confidence,
            valid_from=recorded_at,
            valid_to=valid_to,
            recorded_at=recorded_at,
        )

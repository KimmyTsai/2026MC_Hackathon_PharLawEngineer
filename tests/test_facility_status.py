from __future__ import annotations

from datetime import datetime

from app.config import TAIPEI
from app.graph.status import FacilityStatusStore
from app.models import FacilityStatus, Provenance


def at(hour: int, minute: int = 0) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=TAIPEI)


def maintenance_store() -> FacilityStatusStore:
    store = FacilityStatusStore()
    store.from_notice(
        target_id="CSIE_W_ELEV",
        status=FacilityStatus.closed,
        reason="電梯年度保養",
        source_ref="mail_003",
        valid_from=at(8),
        valid_to=at(17),
        recorded_at=at(8, 5),
    )
    return store


def test_absence_of_evidence_reads_as_open():
    store = FacilityStatusStore()
    assert store.status_of("CSIE_W_ELEV", at(9)).status is FacilityStatus.open


def test_override_applies_only_inside_its_window():
    store = maintenance_store()
    assert store.status_of("CSIE_W_ELEV", at(7, 30)).status is FacilityStatus.open
    assert store.status_of("CSIE_W_ELEV", at(9)).status is FacilityStatus.closed
    assert store.status_of("CSIE_W_ELEV", at(18)).status is FacilityStatus.open


def test_closed_facility_carries_its_source():
    state = maintenance_store().status_of("CSIE_W_ELEV", at(9))
    assert state.source is Provenance.notice
    assert state.source_ref == "mail_003"
    assert state.updated_at == at(8, 5)


def test_blocked_ids_only_include_confident_closures():
    store = maintenance_store()
    store.from_report(
        target_id="RAMP_07",
        status=FacilityStatus.closed,
        reason="斜坡疑似被機車擋住",
        source_ref="photo_ramp_blocked",
        recorded_at=at(8, 27),
        confidence=0.42,
    )
    now = at(8, 30)
    assert store.blocked_ids(now) == {"CSIE_W_ELEV"}
    assert store.unconfirmed_ids(now) == {"RAMP_07"}


def test_later_override_wins():
    store = maintenance_store()
    store.from_notice(
        target_id="CSIE_W_ELEV",
        status=FacilityStatus.open,
        reason="保養提前結束",
        source_ref="mail_004",
        valid_from=at(10),
        valid_to=at(17),
        recorded_at=at(10),
    )
    assert store.status_of("CSIE_W_ELEV", at(11)).status is FacilityStatus.open
    assert store.status_of("CSIE_W_ELEV", at(9)).status is FacilityStatus.closed


def test_reset_clears_everything():
    store = maintenance_store()
    store.reset()
    assert store.overrides == []
    assert store.blocked_ids(at(9)) == set()

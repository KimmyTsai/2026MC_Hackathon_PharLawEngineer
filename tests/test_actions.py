"""The authorization boundary.

`demo-and-acceptance.md` makes these must-pass items: an unapproved proposed
action has no external side effect, and re-running an approved one does not send
twice. Everything here is deterministic — no model, no network.
"""

from __future__ import annotations

from datetime import datetime, timedelta

import pytest

from app.agent.actions import (
    ConfirmationError,
    assess_late_risk,
    build_email_draft,
    check_departure,
    email_idempotency_key,
    execute_action,
    propose_email,
)
from app.agent.planner import PlanningInputs
from app.config import TAIPEI
from app.models import ActionState, ActionType
from app.sources.mailbox import FixtureMailbox


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=TAIPEI)


@pytest.fixture
def outbox(state, tmp_path):
    """Point the fixture mailbox at a temp dir so tests never touch data/."""
    state.providers.mailbox = FixtureMailbox(
        state.settings.mailbox_dir, tmp_path / "outbox", state.scenario.mailbox_ids
    )
    return tmp_path / "outbox"


def advance_to(state, hour: int, minute: int):
    state.clock.advance_to(at(hour, minute))
    state.apply_due_events()
    return state


def inputs_for(state):
    now = state.clock.now()
    return PlanningInputs(
        graph=state.graph,
        facilities=state.facilities,
        profile=state.scenario.user.profile,
        origin=state.scenario.user.home_node,
        signals=state.providers.fetch_all(now),
        now=now,
    )


# --- late-risk detection is arithmetic, not judgement ------------------------
def test_no_risk_while_the_departure_window_is_still_open(state):
    advance_to(state, 7, 50)
    commitment = state.next_commitment()
    assert assess_late_risk(inputs_for(state), commitment, state.clock.now(), False) is None


def test_no_risk_once_the_student_has_departed(state):
    advance_to(state, 8, 40)
    commitment = state.next_commitment()
    assert assess_late_risk(inputs_for(state), commitment, state.clock.now(), True) is None


def test_the_closing_window_warns_without_drafting(state):
    advance_to(state, 8, 25)
    commitment = state.next_commitment()
    risk = assess_late_risk(inputs_for(state), commitment, state.clock.now(), False)
    assert risk is not None
    assert risk.missed is False
    assert risk.minutes_late is None
    assert "出發" in risk.reason


def test_a_missed_window_reports_how_late_and_why(state):
    advance_to(state, 8, 40)
    commitment = state.next_commitment()
    risk = assess_late_risk(inputs_for(state), commitment, state.clock.now(), False)
    assert risk is not None
    assert risk.missed is True
    assert risk.minutes_late > 0
    assert f"{commitment.latest_arrival:%H:%M}" in risk.reason


# --- drafting never sends ---------------------------------------------------
def test_a_draft_has_no_external_effect(state, outbox):
    advance_to(state, 8, 40)
    commitment = state.next_commitment()
    risk = assess_late_risk(inputs_for(state), commitment, state.clock.now(), False)
    action, created = propose_email(state, commitment, risk)

    assert created is True
    assert action.type is ActionType.send_email
    assert action.state is ActionState.awaiting_confirmation
    assert action.requires_authorization is True
    assert not outbox.exists() or not list(outbox.glob("*.json"))


def test_the_draft_quotes_computed_facts_not_invented_ones(state, outbox):
    advance_to(state, 8, 40)
    commitment = state.next_commitment()
    risk = assess_late_risk(inputs_for(state), commitment, state.clock.now(), False)
    action, _ = propose_email(state, commitment, risk)

    body = action.preview["body"]
    assert commitment.title in body
    assert commitment.room in body
    assert str(risk.minutes_late) in body
    assert action.preview["minutes_late"] == risk.minutes_late
    assert action.preview["to"] == state.settings.ta_email


def test_drafting_twice_reuses_one_action(state, outbox):
    advance_to(state, 8, 40)
    commitment = state.next_commitment()
    risk = assess_late_risk(inputs_for(state), commitment, state.clock.now(), False)
    first, created_first = propose_email(state, commitment, risk)
    second, created_second = propose_email(state, commitment, risk)

    assert created_first is True and created_second is False
    assert first.id == second.id
    assert len(state.pending_confirmations) == 1


def test_the_idempotency_key_is_one_notice_per_commitment_per_day(state):
    commitment = state.next_commitment()
    assert email_idempotency_key(commitment, at(8, 40)) == email_idempotency_key(
        commitment, at(9, 10)
    )
    assert email_idempotency_key(commitment, at(8, 40)) != email_idempotency_key(
        commitment, at(8, 40) + timedelta(days=1)
    )


def test_a_model_supplied_body_replaces_the_text_but_not_the_recipient(state):
    advance_to(state, 8, 40)
    commitment = state.next_commitment()
    action, _ = propose_email(state, commitment, None, body="老師好，我會晚到。")
    assert action.preview["body"] == "老師好，我會晚到。"
    assert action.preview["to"] == state.settings.ta_email
    assert commitment.start.strftime("%H:%M") in action.preview["subject"]


def test_the_draft_reads_sensibly_without_any_risk_assessment(state):
    commitment = state.next_commitment()
    draft = build_email_draft(commitment, None, to="ta@example.edu", student="學生")
    assert commitment.room in draft["body"]
    assert draft["minutes_late"] is None


# --- only /confirm can send -------------------------------------------------
def test_approval_sends_exactly_one_message(state, outbox):
    advance_to(state, 8, 40)
    action, _ = propose_email(
        state, state.next_commitment(),
        assess_late_risk(inputs_for(state), state.next_commitment(), state.clock.now(), False),
    )
    result = execute_action(state, action.id, approve=True)

    assert result["sent"] is True
    assert result["duplicate"] is False
    assert action.state is ActionState.executed
    assert action.executed_at == state.clock.now()
    assert len(list(outbox.glob("*.json"))) == 1


def test_approving_twice_does_not_send_twice(state, outbox):
    advance_to(state, 8, 40)
    action, _ = propose_email(state, state.next_commitment(), None)
    execute_action(state, action.id, approve=True)
    again = execute_action(state, action.id, approve=True)

    assert again["duplicate"] is True
    assert len(list(outbox.glob("*.json"))) == 1


def test_rejecting_sends_nothing_and_cannot_be_undone(state, outbox):
    advance_to(state, 8, 40)
    action, _ = propose_email(state, state.next_commitment(), None)
    result = execute_action(state, action.id, approve=False)

    assert result["sent"] is False
    assert action.state is ActionState.rejected
    assert not outbox.exists() or not list(outbox.glob("*.json"))

    with pytest.raises(ConfirmationError, match="已經被取消"):
        execute_action(state, action.id, approve=True)


def test_an_edited_body_is_what_gets_sent(state, outbox):
    advance_to(state, 8, 40)
    action, _ = propose_email(state, state.next_commitment(), None)
    execute_action(state, action.id, approve=True, edited_body="改過的內容")

    import json

    sent = json.loads(next(outbox.glob("*.json")).read_text(encoding="utf-8"))
    assert sent["body"] == "改過的內容"
    assert sent["idempotency_key"] == action.idempotency_key


def test_an_expired_draft_cannot_be_sent(state, outbox):
    advance_to(state, 8, 40)
    action, _ = propose_email(state, state.next_commitment(), None)
    state.clock.advance_to(action.expires_at + timedelta(minutes=1))

    with pytest.raises(ConfirmationError, match="過期"):
        execute_action(state, action.id, approve=True)
    assert action.state is ActionState.expired
    assert not outbox.exists() or not list(outbox.glob("*.json"))


def test_an_unknown_action_is_refused(state):
    with pytest.raises(ConfirmationError, match="沒有"):
        execute_action(state, "act_made_up", approve=True)


def test_distinct_keys_do_not_collide_on_disk(tmp_path):
    """Windows rejects ':' in filenames, so the key is sanitised — and the
    sanitising must not merge two different keys into one file."""
    mailbox = FixtureMailbox(tmp_path, tmp_path / "outbox")
    first = mailbox._filename("late-notice:cmt_01:20260923")
    second = mailbox._filename("late-notice:cmt_02:20260923")
    assert ":" not in first
    assert first != second

    mailbox.execute({"body": "a"}, "late-notice:cmt_01:20260923")
    mailbox.execute({"body": "b"}, "late-notice:cmt_02:20260923")
    assert len(list((tmp_path / "outbox").glob("*.json"))) == 2


# --- the loop reaches the dialog without a model ----------------------------
def test_the_departure_check_drafts_when_the_window_is_missed(state, outbox):
    advance_to(state, 8, 40)
    risk = check_departure(state)
    assert risk is not None and risk.missed
    assert len(state.pending_confirmations) == 1
    assert any("等待你確認" in e.summary for e in state.agent_log)
    assert not outbox.exists() or not list(outbox.glob("*.json"))


def test_the_departure_check_only_warns_while_still_feasible(state, outbox):
    advance_to(state, 8, 25)
    risk = check_departure(state)
    assert risk is not None and not risk.missed
    assert state.pending_confirmations == {}


def test_running_the_whole_scenario_leaves_exactly_one_draft_unsent(state, outbox):
    for hour, minute in [(7, 50), (8, 5), (8, 15), (8, 25), (8, 35), (8, 45)]:
        advance_to(state, hour, minute)
        state.run_agent()

    drafts = list(state.pending_confirmations.values())
    assert len(drafts) == 1
    assert drafts[0].state is ActionState.awaiting_confirmation
    assert not outbox.exists() or not list(outbox.glob("*.json"))

"""Outward-facing actions and the authorization boundary.

The rule this file exists to enforce: nothing leaves the machine without the
student saying so. Drafting is free and automatic; sending happens only in
`execute_action`, which only `/confirm/{id}` calls. `send_email` is deliberately
absent from the model's toolbox — the boundary is structural, not a prompt
instruction.

Late-risk detection is deterministic time arithmetic (CLAUDE.md 2.1). The model
may write nicer prose for the draft, but it does not decide whether the student
is late, and it cannot send.
"""

from __future__ import annotations

import math
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from app.agent.planner import PlanningInputs, build_options, option_score
from app.models import (
    ActionState,
    ActionType,
    Commitment,
    Plan,
    ProposedAction,
    SignalKind,
    TravelOption,
)

# How close to the departure time counts as "the window is closing".
CLOSING_WINDOW = timedelta(minutes=5)
# A draft that nobody confirms stops being offerable once the class has started.
DRAFT_TTL = timedelta(hours=1)


@dataclass
class LateRisk:
    """Why the student is at risk. `minutes_late` is None while still feasible."""

    depart_at: datetime
    minutes_late: int | None
    reason: str
    best_option: TravelOption | None

    @property
    def missed(self) -> bool:
        return self.minutes_late is not None and self.minutes_late > 0


def assess_late_risk(
    inputs: PlanningInputs, commitment: Commitment, now: datetime, departed: bool
) -> LateRisk | None:
    """Deterministic: is the departure window closing, or already missed?"""
    if departed:
        return None

    options = build_options(inputs, commitment)
    feasible = sorted([o for o in options if o.feasible], key=option_score)

    if feasible:
        best = feasible[0]
        if best.depart_at - now <= CLOSING_WINDOW:
            return LateRisk(
                depart_at=best.depart_at,
                minutes_late=None,
                reason=(
                    f"最晚 {best.depart_at:%H:%M} 就要出發才趕得上"
                    f"{commitment.start:%H:%M} 的{commitment.title}"
                ),
                best_option=best,
            )
        return None

    # Nothing is feasible. Report by how much, using the least-late option that
    # actually has a route.
    with_routes = [o for o in options if o.legs]
    if not with_routes:
        return LateRisk(
            depart_at=now,
            minutes_late=None,
            reason="目前沒有任何可行的交通方式",
            best_option=None,
        )
    best_effort = min(with_routes, key=lambda o: o.conservative_eta)
    late = math.ceil(
        (best_effort.conservative_eta - commitment.latest_arrival).total_seconds() / 60
    )
    return LateRisk(
        depart_at=now,
        minutes_late=max(late, 0),
        reason=(
            f"現在出發最快也要 {best_effort.conservative_eta:%H:%M} 才會到，"
            f"比{commitment.title}需要抵達的 {commitment.latest_arrival:%H:%M} 晚約 {late} 分鐘"
        ),
        best_option=best_effort,
    )


def email_idempotency_key(commitment: Commitment, now: datetime) -> str:
    """One late-notice per commitment per day, however many times it is drafted."""
    return f"late-notice:{commitment.id}:{now:%Y%m%d}"


def build_email_draft(
    commitment: Commitment,
    late: LateRisk | None,
    *,
    to: str,
    student: str,
    body: str | None = None,
) -> dict[str, Any]:
    """Deterministic draft. `body` overrides the text, never the facts."""
    subject = f"[遲到通知] {commitment.start:%m/%d %H:%M} {commitment.title}"
    if body is None:
        lines = [f"老師／助教您好：", ""]
        if late and late.missed:
            lines.append(
                f"我是修習{commitment.title}的同學。{late.reason}，"
                f"因此今天的課我會晚到，預計晚約 {late.minutes_late} 分鐘。"
            )
        elif late:
            lines.append(
                f"我是修習{commitment.title}的同學。{late.reason}，目前正在趕往教室。"
            )
        else:
            lines.append(f"我是修習{commitment.title}的同學，今天可能無法準時抵達 {commitment.room}。")
        lines += [
            "",
            f"上課地點：{commitment.room}",
            "造成不便非常抱歉，我會盡快趕到。",
            "",
            student,
        ]
        body = "\n".join(lines)

    return {
        "to": to,
        "subject": subject,
        "body": body,
        "commitment_id": commitment.id,
        "minutes_late": late.minutes_late if late else None,
        "reason": late.reason if late else None,
    }


def propose_email(
    state: Any,
    commitment: Commitment,
    late: LateRisk | None,
    *,
    body: str | None = None,
) -> tuple[ProposedAction, bool]:
    """Put a draft in front of the student. Returns (action, created).

    Idempotent: the deterministic late-risk path and a model `draft_email` call
    converge on the same action instead of stacking up duplicate dialogs.
    """
    now = state.clock.now()
    key = email_idempotency_key(commitment, now)

    existing = next(
        (a for a in state.pending_confirmations.values() if a.idempotency_key == key), None
    )
    if existing is not None:
        if body and existing.state is ActionState.awaiting_confirmation:
            existing.preview["body"] = body
        return existing, False

    settings = state.settings
    preview = build_email_draft(
        commitment,
        late,
        to=settings.ta_email,
        student=settings.student_name,
        body=body,
    )
    action = ProposedAction(
        id=f"act_{key}",
        type=ActionType.send_email,
        preview=preview,
        requires_authorization=True,
        idempotency_key=key,
        state=ActionState.awaiting_confirmation,
        created_at=now,
        expires_at=commitment.start + DRAFT_TTL,
    )
    state.pending_confirmations[action.id] = action
    state.log(
        "act",
        f"已準備寄給 {preview['to']} 的遲到通知草稿，等待你確認才會寄出",
        tool="draft_email",
        tool_result_digest=f"{action.id}・{preview['subject']}",
    )
    state.bus.publish({"type": "pending", "data": action.model_dump(mode="json")})
    return action, True


class ConfirmationError(Exception):
    """The action cannot be acted on in its current state."""


def execute_action(
    state: Any, action_id: str, *, approve: bool, edited_body: str | None = None
) -> dict[str, Any]:
    """The only path that sends. Reached from `/confirm/{id}` and nowhere else."""
    action = state.pending_confirmations.get(action_id)
    if action is None:
        raise ConfirmationError(f"沒有 {action_id} 這個待確認動作")

    now = state.clock.now()

    if action.state is ActionState.executed:
        # Approving twice must not send twice.
        return {
            "state": action.state.value,
            "sent": True,
            "duplicate": True,
            "note": "這封信已經寄出過了，沒有重複寄送",
        }
    if action.state is ActionState.rejected:
        raise ConfirmationError("這個動作已經被取消，不能再執行")
    if action.expires_at is not None and now > action.expires_at:
        action.state = ActionState.expired
        raise ConfirmationError("草稿已過期，請重新產生")

    if not approve:
        action.state = ActionState.rejected
        action.rejected_at = now
        state.log("act", f"你取消了 {action.preview.get('subject', action.id)}，沒有寄出")
        state.bus.publish({"type": "pending", "data": action.model_dump(mode="json")})
        return {"state": action.state.value, "sent": False}

    if edited_body:
        action.preview["body"] = edited_body
    action.state = ActionState.approved

    result = state.providers.mailbox.execute(action.preview, action.idempotency_key)

    action.state = ActionState.executed
    action.executed_at = now
    state.log(
        "act",
        f"你確認後已寄出：{action.preview.get('subject')}"
        + ("（先前已寄過，未重複寄送）" if result.get("duplicate") else ""),
        tool="send_email",
        tool_result_digest=str(result.get("path", "")),
    )
    state.bus.publish({"type": "pending", "data": action.model_dump(mode="json")})
    return {"state": action.state.value, **result}


def check_departure(state: Any, plan: Plan | None = None) -> LateRisk | None:
    """Run after planning: warn, and draft if the window is already missed."""
    commitment = state.next_commitment()
    if commitment is None:
        return None

    now = state.clock.now()
    signal = state.providers.fetch_all(now).get(SignalKind.departure_state)
    if signal is None or signal.error:
        return None
    if bool(signal.value.get("departed")):
        return None

    inputs = PlanningInputs(
        graph=state.graph,
        facilities=state.facilities,
        profile=state.scenario.user.profile,
        origin=state.scenario.user.home_node,
        signals=state.providers.fetch_all(now),
        now=now,
    )
    late = assess_late_risk(inputs, commitment, now, departed=False)
    if late is None:
        return None

    state.log("reflect", ("遲到風險：" if late.missed else "出發提醒：") + late.reason)
    if late.missed:
        propose_email(state, commitment, late)
    return late

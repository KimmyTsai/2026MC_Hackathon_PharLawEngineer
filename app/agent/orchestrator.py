"""The agent loop.

Event → brief the model → let it call tools → it commits a plan → it explains.
Automatic function calling is disabled, so every step lands in the agent log and
reaches the frontend over SSE (CLAUDE.md 7).

Degradation is explicit: if there is no API key, the provider fails, or the model
never commits a feasible plan, the deterministic planner from M1 decides and the
log says which path was taken. The demo never depends on the model behaving.
"""

from __future__ import annotations

import json
from dataclasses import dataclass, field
from typing import Any

from app.agent import tools as toolkit
from app.agent.llm import LLM, LLMTurn
from app.agent.planner import plan_for
from app.agent.prompts import SYSTEM_PROMPT, state_briefing
from app.models import Plan, Trigger

MAX_STEPS = 6
MAX_TOOL_CALLS_PER_STEP = 4


@dataclass
class AgentRun:
    plan: Plan | None = None
    steps: int = 0
    tool_calls: list[str] = field(default_factory=list)
    final_text: str = ""
    model_id: str = ""
    input_tokens: int = 0
    output_tokens: int = 0
    fell_back: bool = False
    fallback_reason: str | None = None

    @property
    def used_model(self) -> bool:
        return bool(self.model_id) and not self.fell_back


def _digest(result: dict[str, Any], limit: int = 220) -> str:
    text = json.dumps(result, ensure_ascii=False, default=str)
    return text if len(text) <= limit else text[: limit - 1] + "…"


class Orchestrator:
    def __init__(self, state: Any, llm: LLM, max_steps: int = MAX_STEPS) -> None:
        self.state = state
        self.llm = llm
        self.max_steps = max_steps

    # -- entry point ---------------------------------------------------------
    def run(self, trigger: Trigger | None = None) -> AgentRun:
        commitment = self.state.next_commitment()
        if commitment is None:
            self.state.log("reflect", "今日沒有後續行程，不需要規劃")
            return AgentRun()

        if not getattr(self.llm, "available", False):
            return self._fallback(trigger, reason=getattr(self.llm, "reason", "模型不可用"))

        ctx = toolkit.ToolContext(state=self.state, commitment=commitment, llm=self.llm)
        run = AgentRun(model_id=self.llm.model_id)

        now = self.state.clock.now()
        plan = self.state.plans.get(commitment.id)
        history: list[dict[str, Any]] = [
            {
                "role": "user",
                "text": state_briefing(
                    now=now,
                    profile=self.state.scenario.user.profile.value,
                    commitment=commitment.model_dump(mode="json"),
                    current_plan=plan.model_dump(mode="json") if plan else None,
                    signals={
                        kind.value: {
                            **signal.model_dump(mode="json"),
                            "freshness": signal.freshness(now).value,
                        }
                        for kind, signal in self.state.providers.fetch_all(now).items()
                    },
                    trigger=trigger.model_dump(mode="json") if trigger else None,
                    origin=self.state.scenario.user.home_node,
                ),
            }
        ]

        declarations = toolkit.declarations()
        for step in range(1, self.max_steps + 1):
            run.steps = step
            turn: LLMTurn = self.llm.turn(SYSTEM_PROMPT, history, declarations)
            run.input_tokens += turn.input_tokens
            run.output_tokens += turn.output_tokens

            if turn.error:
                self.state.log(
                    "plan",
                    f"模型呼叫失敗（{turn.error}），改用確定性規劃",
                    model_id=self.llm.model_id,
                )
                return self._fallback(trigger, reason=turn.error, run=run)

            if turn.text:
                self.state.log(
                    "plan" if turn.wants_tools else "reflect",
                    turn.text,
                    model_id=turn.model_id,
                )

            if not turn.wants_tools:
                run.final_text = turn.text
                break

            history.append(
                {
                    "role": "model",
                    "tool_calls": turn.tool_calls,
                    "text": turn.text,
                    "raw": turn.raw,
                }
            )
            for call in turn.tool_calls[:MAX_TOOL_CALLS_PER_STEP]:
                result = toolkit.call(ctx, call.name, call.args)
                run.tool_calls.append(call.name)
                self.state.log(
                    "act" if call.name in {"commit_plan", "update_facility",
                                           "set_departure_reminder"} else "perceive",
                    f"呼叫 {call.name}"
                    + (f"（{result.get('error')}）" if not result.get("ok", True) else ""),
                    tool=call.name,
                    tool_args=call.args,
                    tool_result_digest=_digest(result),
                    model_id=turn.model_id,
                )
                history.append({"role": "tool", "name": call.name, "response": result})
        else:
            self.state.log(
                "reflect",
                f"已達 {self.max_steps} 步上限，採用目前最佳計畫",
                model_id=self.llm.model_id,
            )

        return self._commit(ctx, run, trigger)

    # -- committing ----------------------------------------------------------
    def _commit(self, ctx: toolkit.ToolContext, run: AgentRun, trigger: Trigger | None) -> AgentRun:
        """Write the model's choice into state, or fall back to the ranked best.

        The decision record always comes from the deterministic planner, so the
        rationale the judges read is reproducible even when the model wrote its
        own explanation.
        """
        commitment = ctx.commitment
        assert commitment is not None
        inputs = ctx.planning_inputs()
        previous = self.state.plans.get(commitment.id)
        plan, decision = plan_for(
            inputs,
            commitment,
            trigger=trigger,
            previous=previous,
            model_id=run.model_id or None,
        )

        chosen = ctx.committed_option
        if chosen is not None and plan is not None and chosen.id != plan.selected.id:
            # The model picked a feasible option that is not the ranked best.
            # Honour it — it saw the risk notes — but record the divergence.
            alternatives = [o for o in [plan.selected, *plan.alternatives] if o.id != chosen.id]
            plan = plan.model_copy(update={"selected": chosen, "alternatives": alternatives})
            decision.selected_option = chosen.id
            decision.rationale = (
                f"模型選擇 {chosen.mode.value}（非分數最佳），"
                f"{chosen.depart_at:%H:%M} 出發，保守估計 {chosen.conservative_eta:%H:%M} 抵達。"
                + (run.final_text or "")
            )
        elif run.final_text:
            decision.rationale = f"{decision.rationale} 模型說明：{run.final_text}"

        if chosen is None and plan is not None:
            decision.assumptions.append("模型未送出 commit_plan，採用確定性排序的最佳方案")

        self.state.record_decision(decision)
        if plan is not None:
            self.state.set_plan(plan)
        elif previous is not None:
            from app.models import PlanStatus

            previous.status = PlanStatus.infeasible
        if not run.final_text:
            self.state.log("reflect", decision.rationale, model_id=run.model_id or None)
        run.plan = plan
        return run

    # -- fallback ------------------------------------------------------------
    def _fallback(
        self, trigger: Trigger | None, reason: str, run: AgentRun | None = None
    ) -> AgentRun:
        run = run or AgentRun()
        run.fell_back = True
        run.fallback_reason = reason
        plan = self.state.replan(trigger)
        run.plan = plan
        return run

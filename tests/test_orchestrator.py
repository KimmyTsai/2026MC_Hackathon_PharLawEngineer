"""The agent loop: tool plumbing, the authorization boundary, and degradation.

Every test scripts the model. Nothing here touches the network — see the
`never_call_the_model` fixture in conftest.py.
"""

from __future__ import annotations

from datetime import datetime

import pytest

from app.agent.llm import LLMTurn, NullLLM, ToolCall
from app.agent.orchestrator import MAX_STEPS, Orchestrator
from app.agent.tools import TOOLS, ToolContext, resolve_facility_hint, update_facility
from app.config import TAIPEI
from app.models import FacilityStatus


class StubLLM:
    """Replays a script of turns and records what it was asked."""

    available = True
    model_id = "stub-flash"

    def __init__(self, *turns: LLMTurn, extraction: dict | None = None) -> None:
        self.script = list(turns)
        self.calls = 0
        self.histories: list[list[dict]] = []
        self.systems: list[str] = []
        self.extraction = extraction

    def turn(self, system, history, tools):  # noqa: ANN001
        self.calls += 1
        self.systems.append(system)
        self.histories.append(list(history))
        self.tool_names = sorted(t.name for t in tools)
        if self.script:
            return self.script.pop(0)
        return LLMTurn(text="沒有其他要做的了", model_id=self.model_id)

    def extract(self, system, text, schema):  # noqa: ANN001
        return self.extraction


def calls(*pairs: tuple[str, dict]) -> LLMTurn:
    return LLMTurn(
        tool_calls=[ToolCall(name=name, args=args) for name, args in pairs],
        model_id=StubLLM.model_id,
    )


def at(hour: int, minute: int) -> datetime:
    return datetime(2026, 9, 23, hour, minute, tzinfo=TAIPEI)


@pytest.fixture
def ready(state):
    """State advanced to 07:50 with the day_start event perceived."""
    state.clock.advance_to(at(7, 50))
    state.apply_due_events()
    return state


def run_with(state, *turns: LLMTurn, **kwargs):
    llm = StubLLM(*turns, **kwargs)
    state.llm = llm
    run = Orchestrator(state, llm).run()
    return run, llm


# --- the happy path ---------------------------------------------------------
def test_the_model_drives_tools_then_commits(ready):
    run, llm = run_with(
        ready,
        calls(("compare_travel_options", {})),
        calls(("commit_plan", {"option_id": "opt_bus_lowfloor", "why": "步行太久"})),
        LLMTurn(text="建議 08:37 出發搭低地板公車。", model_id=StubLLM.model_id),
    )
    assert run.fell_back is False
    assert run.tool_calls == ["compare_travel_options", "commit_plan"]
    assert run.plan is not None
    assert run.plan.selected.id == "opt_bus_lowfloor"
    assert run.final_text.startswith("建議")

    phases = [(e.phase, e.tool) for e in ready.agent_log]
    assert ("perceive", "compare_travel_options") in phases
    assert ("act", "commit_plan") in phases
    assert any(e.model_id == StubLLM.model_id for e in ready.agent_log)


def test_the_decision_record_names_the_model_and_keeps_its_words(ready):
    run, _ = run_with(
        ready,
        calls(("compare_travel_options", {})),
        calls(("commit_plan", {"option_id": "opt_bus_lowfloor", "why": "風險較低"})),
        LLMTurn(text="已改搭低地板公車。", model_id=StubLLM.model_id),
    )
    decision = ready.decisions[-1]
    assert decision.model_id == StubLLM.model_id
    assert "已改搭低地板公車。" in decision.rationale
    assert decision.decisive_signals  # evidence is still attached
    assert run.plan is not None


def test_the_model_sees_the_brief_and_the_tool_results(ready):
    _, llm = run_with(
        ready,
        calls(("compare_travel_options", {})),
        LLMTurn(text="不需要改變。", model_id=StubLLM.model_id),
    )
    first_brief = llm.histories[0][0]["text"]
    assert "crutches" in first_brief
    assert "CSIE-4263" in first_brief
    assert "presentation" in first_brief

    second_turn_history = llm.histories[1]
    tool_reply = next(item for item in second_turn_history if item["role"] == "tool")
    assert tool_reply["name"] == "compare_travel_options"
    assert tool_reply["response"]["ok"] is True


# --- code keeps the last word ----------------------------------------------
def test_an_infeasible_choice_is_refused_and_explained(ready):
    ready.clock.advance_to(at(8, 20))
    ready.apply_due_events()
    run, _ = run_with(
        ready,
        calls(("compare_travel_options", {})),
        calls(("commit_plan", {"option_id": "opt_walk", "why": "走路比較單純"})),
        LLMTurn(text="改用公車。", model_id=StubLLM.model_id),
    )
    rejection = next(e for e in ready.agent_log if e.tool == "commit_plan")
    assert "不可行" in rejection.summary
    # The plan still exists, chosen by deterministic ranking rather than the model.
    assert run.plan is not None
    assert run.plan.selected.mode.value != "walk"


def test_committing_without_comparing_first_is_refused(ready):
    run, _ = run_with(
        ready,
        calls(("commit_plan", {"option_id": "opt_bus", "why": "直接選"})),
        LLMTurn(text="好。", model_id=StubLLM.model_id),
    )
    entry = next(e for e in ready.agent_log if e.tool == "commit_plan")
    assert "compare_travel_options" in entry.summary
    assert run.plan is not None  # deterministic fallback still produced one


def test_no_commit_falls_back_to_the_ranked_best(ready):
    run, _ = run_with(ready, LLMTurn(text="看起來沒問題。", model_id=StubLLM.model_id))
    assert run.plan is not None
    assert any("未送出 commit_plan" in a for a in ready.decisions[-1].assumptions)


def test_a_model_choice_that_is_feasible_but_not_best_is_honoured_and_recorded(ready):
    run, _ = run_with(
        ready,
        calls(("compare_travel_options", {})),
        calls(("commit_plan", {"option_id": "opt_bus", "why": "這班我熟"})),
        LLMTurn(text="選了一般公車。", model_id=StubLLM.model_id),
    )
    assert run.plan is not None
    assert run.plan.selected.id == "opt_bus"
    assert "非分數最佳" in ready.decisions[-1].rationale
    assert all(o.id != "opt_bus" for o in run.plan.alternatives)


# --- degradation ------------------------------------------------------------
def test_a_provider_failure_degrades_to_deterministic_planning(ready):
    run, _ = run_with(ready, LLMTurn(error="ClientError: 429 RESOURCE_EXHAUSTED"))
    assert run.fell_back is True
    assert "429" in (run.fallback_reason or "")
    assert run.plan is not None  # the demo still has a plan
    assert any("改用確定性規劃" in e.summary for e in ready.agent_log)


def test_no_key_means_no_call_and_a_plan_anyway(ready):
    ready.llm = NullLLM("沒有金鑰")
    run = Orchestrator(ready, ready.llm).run()
    assert run.fell_back is True
    assert run.fallback_reason == "沒有金鑰"
    assert run.plan is not None
    assert run.model_id == ""


def test_the_loop_stops_at_the_step_limit(ready):
    endless = [calls(("get_rain_forecast", {})) for _ in range(MAX_STEPS + 3)]
    run, llm = run_with(ready, *endless)
    assert run.steps == MAX_STEPS
    assert llm.calls == MAX_STEPS
    assert any("步上限" in e.summary for e in ready.agent_log)
    assert run.plan is not None


def test_an_unknown_tool_is_reported_not_raised(ready):
    run, _ = run_with(
        ready,
        calls(("send_email", {"to": "ta@example.edu"})),
        LLMTurn(text="好。", model_id=StubLLM.model_id),
    )
    entry = next(e for e in ready.agent_log if e.tool == "send_email")
    assert "沒有 send_email 這個工具" in entry.summary
    assert run.plan is not None


def test_outbound_mail_is_not_a_tool_the_model_can_reach():
    """The authorization boundary is structural, not a prompt instruction."""
    assert "send_email" not in TOOLS
    assert "draft_email" not in TOOLS  # arrives in M5, behind /confirm


# --- tool-level validation --------------------------------------------------
def test_update_facility_rejects_an_invented_node(ready):
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=NullLLM())
    result = update_facility(
        ctx, target_id="MADE_UP_ELEVATOR", status="closed", reason="模型編的"
    )
    assert result["ok"] is False
    assert ready.facilities.overrides == []


def test_update_facility_rejects_a_bad_status(ready):
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=NullLLM())
    result = update_facility(ctx, target_id="CSIE_W_ELEV", status="broken-ish", reason="x")
    assert result["ok"] is False
    assert ready.facilities.overrides == []


def test_a_low_confidence_update_is_recorded_but_does_not_reroute(ready):
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=NullLLM())
    result = update_facility(
        ctx, target_id="RAMP_07", status="closed", reason="照片看起來被擋住",
        source_ref="photo_1", confidence=0.4,
    )
    assert result["ok"] is True
    assert result["actionable"] is False
    now = ready.clock.now()
    assert ready.facilities.blocked_ids(now) == set()
    assert ready.facilities.unconfirmed_ids(now) == {"RAMP_07"}


def test_a_confident_update_does_reroute(ready):
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=NullLLM())
    update_facility(
        ctx, target_id="CSIE_W_ELEV", status="closed", reason="保養",
        source_ref="mail_003", confidence=1.0,
    )
    assert ready.facilities.blocked_ids(ready.clock.now()) == {"CSIE_W_ELEV"}
    plan = ready.replan()
    assert plan is not None
    assert [f for leg in plan.selected.legs for f in leg.uses_facilities] == ["CSIE_E_ELEV"]


def test_facility_hints_resolve_deterministically(graph):
    assert resolve_facility_hint(graph, "資訊系館西側電梯")[0] == "CSIE_W_ELEV"
    assert resolve_facility_hint(graph, "CSIE_W_ELEV")[0] == "CSIE_W_ELEV"  # an exact id is fine
    assert resolve_facility_hint(graph, "CSIE_MADE_UP")[0] is None  # an invented one is not
    ambiguous_id, note = resolve_facility_hint(graph, "電梯")
    assert ambiguous_id is None and "多個" in note
    missing_id, note = resolve_facility_hint(graph, "游泳池電梯")
    assert missing_id is None and "找不到" in note


def test_notice_reading_falls_back_to_the_fixture_when_the_model_is_silent(ready):
    """NullLLM.extract returns None, so Stage 2's manual path must take over."""
    ready.clock.advance_to(at(8, 10))
    ready.apply_due_events()
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=NullLLM())
    result = TOOLS["check_facility_notices"][1](ctx)
    notice = next(n for n in result["notices"] if n["mail_id"] == "mail_003")
    assert notice["read_by"] == "fixture_fallback"
    assert notice["resolved_target_id"] == "CSIE_W_ELEV"


def test_the_model_reading_a_notice_is_labelled_as_such(ready):
    ready.clock.advance_to(at(8, 10))
    ready.apply_due_events()
    llm = StubLLM(
        extraction={
            "affects_facility": True,
            "facility_hint": "資訊系館西側電梯",
            "status": "closed",
            "reason": "年度保養",
            "valid_from": "2026-09-23T08:00:00+08:00",
            "valid_to": "2026-09-23T17:00:00+08:00",
            "confidence": 0.95,
        }
    )
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=llm)
    result = TOOLS["check_facility_notices"][1](ctx)
    notice = next(n for n in result["notices"] if n["mail_id"] == "mail_003")
    assert notice["read_by"] == "gemini"
    assert notice["resolved_target_id"] == "CSIE_W_ELEV"
    assert notice["confidence"] == 0.95


def test_an_unrelated_notice_is_classified_as_such(ready):
    """mail_001 is a club announcement; it must not produce a facility change."""
    ready.clock.advance_to(at(8, 10))
    ready.apply_due_events()
    llm = StubLLM(extraction={"affects_facility": False, "confidence": 0.9})
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=llm)
    result = TOOLS["check_facility_notices"][1](ctx)
    assert all(n["affects_facility"] is False for n in result["notices"])


def test_plan_route_ignores_a_profile_the_model_tries_to_pass(ready):
    """CLAUDE.md 6: the profile comes from state, never from the model."""
    ctx = ToolContext(state=ready, commitment=ready.next_commitment(), llm=NullLLM())
    result = TOOLS["plan_route"][1](ctx, room_id="CSIE-4263")
    crutches_minutes = result["walking_minutes"]
    assert result["ok"] is True
    declaration = TOOLS["plan_route"][0]
    assert "profile" not in declaration.parameters["properties"]
    assert crutches_minutes > 0

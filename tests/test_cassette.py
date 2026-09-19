"""Recorded model turns.

The free tier allows 20 requests per day per model, so the demo replays recorded
turns instead of calling. These tests use a counting stub — never the network.
"""

from __future__ import annotations

import json

import pytest

from app.agent.cassette import CachedLLM, fingerprint, turn_key
from app.agent.llm import LLMTurn, ToolCall, ToolDeclaration

TOOLS = [ToolDeclaration(name="compare_travel_options", description="比較方案", parameters={})]
SYSTEM = "你是校園行程代理。"


class CountingLLM:
    available = True
    model_id = "counting-flash"

    def __init__(self, *turns: LLMTurn, extraction: dict | None = None) -> None:
        self.script = list(turns)
        self.turn_calls = 0
        self.extract_calls = 0
        self.extraction = extraction

    def turn(self, system, history, tools):  # noqa: ANN001
        self.turn_calls += 1
        if self.script:
            return self.script.pop(0)
        return LLMTurn(text="完成", model_id=self.model_id)

    def extract(self, system, text, schema):  # noqa: ANN001
        self.extract_calls += 1
        return self.extraction


@pytest.fixture
def cassette_path(tmp_path):
    return tmp_path / "cassette.json"


def history(text: str = "現在時間 07:50"):
    return [{"role": "user", "text": text}]


# --- keys -------------------------------------------------------------------
def test_the_same_question_hashes_the_same():
    assert turn_key(SYSTEM, history(), TOOLS) == turn_key(SYSTEM, history(), TOOLS)


def test_a_different_question_hashes_differently():
    assert turn_key(SYSTEM, history(), TOOLS) != turn_key(SYSTEM, history("現在時間 08:17"), TOOLS)
    assert turn_key(SYSTEM, history(), TOOLS) != turn_key("別的系統提示", history(), TOOLS)


def test_provider_signatures_do_not_affect_the_key():
    """`raw` carries a thought_signature that differs run to run; it says nothing
    about the question, so it must not change the key."""
    plain = [{"role": "model", "text": "", "tool_calls": [ToolCall("x", {})]}]
    signed = [{"role": "model", "text": "", "tool_calls": [ToolCall("x", {})], "raw": object()}]
    assert turn_key(SYSTEM, plain, TOOLS) == turn_key(SYSTEM, signed, TOOLS)


def test_tool_results_are_part_of_the_key():
    first = [{"role": "tool", "name": "t", "response": {"bikes": 7}}]
    second = [{"role": "tool", "name": "t", "response": {"bikes": 0}}]
    assert turn_key(SYSTEM, first, TOOLS) != turn_key(SYSTEM, second, TOOLS)


# --- cache mode -------------------------------------------------------------
def test_first_call_records_and_second_replays(cassette_path):
    inner = CountingLLM(
        LLMTurn(
            tool_calls=[ToolCall("compare_travel_options", {})],
            # Gemini 3 hands back a signed turn; the cassette must keep it or the
            # run cannot continue live. See test_an_unsigned_tool_turn_is_rerecorded.
            raw={"role": "model", "parts": [{"thoughtSignature": "c2lnbmF0dXJl"}]},
            model_id="counting-flash",
        )
    )
    llm = CachedLLM(inner, cassette_path, mode="cache")

    first = llm.turn(SYSTEM, history(), TOOLS)
    assert inner.turn_calls == 1
    assert cassette_path.exists()

    second = CachedLLM(CountingLLM(), cassette_path, mode="cache")
    replayed = second.turn(SYSTEM, history(), TOOLS)
    assert second.inner.turn_calls == 0  # nothing reached the provider
    assert second.hits == 1
    assert [c.name for c in replayed.tool_calls] == [c.name for c in first.tool_calls]
    assert replayed.model_id == "counting-flash"  # the recording model is named


def test_an_unsigned_tool_turn_is_rerecorded_in_cache_mode(cassette_path):
    """A recorded tool turn with no thought_signature cannot be continued live:
    Gemini 3 answers 400. Cache mode must re-record it rather than replay it."""
    unsigned = LLMTurn(tool_calls=[ToolCall("compare_travel_options", {})], raw=None,
                       model_id="counting-flash")
    CachedLLM(CountingLLM(unsigned), cassette_path, mode="cache").turn(SYSTEM, history(), TOOLS)

    again = CachedLLM(CountingLLM(unsigned), cassette_path, mode="cache")
    again.turn(SYSTEM, history(), TOOLS)
    assert again.inner.turn_calls == 1  # treated as a miss on purpose
    assert again.hits == 0

    # Pure replay has nothing live to continue into, so it still replays.
    pure = CachedLLM(CountingLLM(), cassette_path, mode="replay")
    assert pure.turn(SYSTEM, history(), TOOLS).tool_calls
    assert pure.inner.turn_calls == 0


def test_a_text_only_turn_replays_without_a_signature(cassette_path):
    CachedLLM(
        CountingLLM(LLMTurn(text="不需要改變。", model_id="counting-flash")),
        cassette_path,
        mode="cache",
    ).turn(SYSTEM, history(), TOOLS)
    again = CachedLLM(CountingLLM(), cassette_path, mode="cache")
    assert again.turn(SYSTEM, history(), TOOLS).text == "不需要改變。"
    assert again.inner.turn_calls == 0


def test_a_failed_turn_is_not_recorded(cassette_path):
    inner = CountingLLM(LLMTurn(error="429 RESOURCE_EXHAUSTED"))
    llm = CachedLLM(inner, cassette_path, mode="cache")
    assert llm.turn(SYSTEM, history(), TOOLS).error
    assert not cassette_path.exists()


def test_extraction_is_cached_by_document(cassette_path):
    inner = CountingLLM(extraction={"affects_facility": True, "confidence": 1.0})
    llm = CachedLLM(inner, cassette_path, mode="cache")
    assert llm.extract("讀公告", "電梯保養", {})["affects_facility"] is True
    assert inner.extract_calls == 1

    second = CachedLLM(CountingLLM(), cassette_path, mode="cache")
    assert second.extract("讀公告", "電梯保養", {})["affects_facility"] is True
    assert second.inner.extract_calls == 0


# --- replay mode ------------------------------------------------------------
def test_the_signature_survives_a_round_trip_through_the_file(cassette_path):
    signed = {"role": "model", "parts": [{"thoughtSignature": "c2lnbmF0dXJl"}]}
    CachedLLM(
        CountingLLM(
            LLMTurn(tool_calls=[ToolCall("x", {})], raw=signed, model_id="counting-flash")
        ),
        cassette_path,
        mode="cache",
    ).turn(SYSTEM, history(), TOOLS)
    replayed = CachedLLM(CountingLLM(), cassette_path, mode="cache").turn(
        SYSTEM, history(), TOOLS
    )
    assert replayed.raw == signed


def test_replay_mode_never_calls_the_provider(cassette_path):
    CachedLLM(
        CountingLLM(LLMTurn(text="錄下來的說明", model_id="counting-flash")),
        cassette_path,
        mode="cache",
    ).turn(SYSTEM, history(), TOOLS)

    inner = CountingLLM(LLMTurn(text="不該被呼叫"))
    replay = CachedLLM(inner, cassette_path, mode="replay")
    assert replay.turn(SYSTEM, history(), TOOLS).text == "錄下來的說明"
    assert inner.turn_calls == 0


def test_a_replay_miss_reports_an_error_so_the_caller_can_fall_back(cassette_path):
    cassette_path.write_text(json.dumps({"turns": []}), encoding="utf-8")
    replay = CachedLLM(CountingLLM(), cassette_path, mode="replay")
    turn = replay.turn(SYSTEM, history("沒錄過的時間 09:99"), TOOLS)
    assert turn.error and "錄音檔沒有這一步" in turn.error
    assert replay.inner.turn_calls == 0


def test_replay_without_a_cassette_is_not_available(cassette_path):
    replay = CachedLLM(CountingLLM(), cassette_path, mode="replay")
    assert replay.available is False
    assert "沒有錄音檔" in (replay.reason or "")


# --- live mode --------------------------------------------------------------
def test_live_mode_neither_reads_nor_writes(cassette_path):
    CachedLLM(
        CountingLLM(LLMTurn(text="舊的")), cassette_path, mode="cache"
    ).turn(SYSTEM, history(), TOOLS)
    before = cassette_path.read_text(encoding="utf-8")

    inner = CountingLLM(LLMTurn(text="新的"))
    live = CachedLLM(inner, cassette_path, mode="live")
    assert live.turn(SYSTEM, history(), TOOLS).text == "新的"
    assert inner.turn_calls == 1
    assert cassette_path.read_text(encoding="utf-8") == before


# --- the shipped cassette ---------------------------------------------------
def test_the_demo_cassette_is_present_and_labelled(settings):
    path = settings.cassette_path
    assert path.exists(), "demo 錄音檔不在，重播模式會退化成確定性規劃"
    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["turns"], "錄音檔是空的"
    assert payload["model"], "錄音檔沒有記下是哪個模型錄的"
    assert all(entry.get("key") for entry in payload["turns"])
    assert any(
        call["name"] == "commit_plan"
        for entry in payload["turns"]
        for call in entry.get("tool_calls", [])
    ), "錄音檔裡沒有 commit_plan，重播時不會真的送出計畫"


def test_replay_names_the_recording_model_not_the_configured_one(cassette_path):
    """The badge must not imply a live call to a model that never answered."""
    CachedLLM(
        CountingLLM(LLMTurn(text="錄下來的", model_id="recorded-model")),
        cassette_path,
        mode="cache",
    ).turn(SYSTEM, history(), TOOLS)

    inner = CountingLLM()
    inner.model_id = "configured-model"
    replay = CachedLLM(inner, cassette_path, mode="replay")
    assert replay.model_id == "recorded-model"
    assert replay.replaying is True

    cache = CachedLLM(inner, cassette_path, mode="cache")
    assert cache.model_id == "configured-model"  # it may still call live
    assert cache.replaying is False


# --- staleness --------------------------------------------------------------
def test_a_cassette_recorded_against_other_questions_is_stale(cassette_path):
    CachedLLM(
        CountingLLM(LLMTurn(text="舊的", model_id="counting-flash")),
        cassette_path,
        mode="cache",
        expected_fingerprint="aaaa1111",
    ).turn(SYSTEM, history(), TOOLS)

    same = CachedLLM(CountingLLM(), cassette_path, mode="replay",
                     expected_fingerprint="aaaa1111")
    assert same.stale is False

    changed = CachedLLM(CountingLLM(), cassette_path, mode="replay",
                        expected_fingerprint="bbbb2222")
    assert changed.stale is True
    assert "已過期" in (changed.reason or "")


def test_an_empty_cassette_is_not_called_stale(cassette_path):
    fresh = CachedLLM(CountingLLM(), cassette_path, mode="replay",
                      expected_fingerprint="aaaa1111")
    assert fresh.stale is False
    assert "沒有錄音檔" in (fresh.reason or "")


def test_the_fingerprint_covers_prompt_tools_and_scenario(tmp_path, settings):
    scenario = tmp_path / "s.json"
    scenario.write_text('{"events": []}', encoding="utf-8")
    base = fingerprint(scenario, SYSTEM, TOOLS)

    assert fingerprint(scenario, SYSTEM, TOOLS) == base
    assert fingerprint(scenario, "別的系統提示", TOOLS) != base

    other_tools = [ToolDeclaration(name="compare_travel_options",
                                   description="改過的說明", parameters={})]
    assert fingerprint(scenario, SYSTEM, other_tools) != base

    # The scenario's `expect` text reaches the model as trigger materiality,
    # so editing the scenario must invalidate the recording.
    scenario.write_text('{"events": [{"t": "07:50", "expect": "新的說明"}]}', encoding="utf-8")
    assert fingerprint(scenario, SYSTEM, TOOLS) != base


def test_the_shipped_cassette_matches_the_current_questions(settings):
    """Guards the demo: a stale cassette silently degrades every step."""
    from app.agent.prompts import SYSTEM_PROMPT
    from app.agent.tools import declarations

    path = settings.cassette_path
    assert path.exists(), "demo 錄音檔不在"
    stored = json.loads(path.read_text(encoding="utf-8")).get("fingerprint")
    expected = fingerprint(settings.scenario_path, SYSTEM_PROMPT, declarations())
    assert stored == expected, (
        "錄音檔與目前的 prompt／工具／情境不符，請執行 "
        "scripts/record_cassette.py 重錄"
    )

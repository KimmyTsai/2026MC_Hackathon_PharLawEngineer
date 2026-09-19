"""Record the demo's model turns so the presentation costs no quota.

The free tier allows 20 generate_content requests per day PER MODEL
(`GenerateRequestsPerDayPerProjectPerModel-FreeTier`, observed 2026-09-19), and
one agent run costs 2-6 of them. Re-record after changing prompts, tool
declarations, tool output shapes, or the scenario — the cassette key is a hash of
exactly those things, so a stale cassette simply misses and falls back.

    # uses GEMINI_FLASH_MODEL from .env
    .venv/Scripts/python.exe scripts/record_cassette.py

    # spend a different model's daily quota instead
    .venv/Scripts/python.exe scripts/record_cassette.py --model gemini-3.1-flash-lite

    # only the moments you need, to stay inside the quota
    .venv/Scripts/python.exe scripts/record_cassette.py --events 3
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.agent.llm import build_llm  # noqa: E402
from app.config import Settings  # noqa: E402
from app.main import build_state  # noqa: E402


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=None, help="模型 ID（每個模型有獨立的每日額度）")
    parser.add_argument("--scenario", default="demo_wed")
    parser.add_argument("--events", type=int, default=0, help="只錄前 N 個事件，0 表示全部")
    parser.add_argument("--retries", type=int, default=3, help="遇到每分鐘額度時的重試次數")
    args = parser.parse_args()

    overrides = {
        "scenario": args.scenario,
        "cassette": args.scenario,
        "agent_mode": "cache",
        # The free tier also limits requests per minute (5 RPM on some models),
        # so recording waits the server's own retryDelay instead of giving up.
        "quota_retries": args.retries,
    }
    if args.model:
        overrides["gemini_flash_model"] = args.model
        overrides["gemini_pro_model"] = None
    settings = Settings(**overrides)

    state = build_state(settings)
    state.llm = build_llm(settings)
    if not state.llm.available:
        print("沒有可用的模型，先設定 GEMINI_API_KEY", file=sys.stderr)
        return 1

    print(f"錄音模型 {state.llm.model_id} → {settings.cassette_path}")
    state.apply_due_events()
    state.run_agent()

    events = state.scenario.events
    if args.events:
        events = events[: args.events]

    for event in events:
        if event.at <= state.clock.now():
            continue
        state.clock.advance_to(event.at)
        triggers = state.apply_due_events()
        run = state.run_agent(triggers[-1] if triggers else None)
        selected = run.plan.selected.mode.value if run.plan else "無可行方案"
        status = "退化" if run.fell_back else f"{run.steps} 步"
        print(f"  {event.at:%H:%M} {event.type:22s} {status:8s} → {selected}")
        if run.fell_back:
            print(f"    停止錄音：{run.fallback_reason}", file=sys.stderr)
            break

    cassette = getattr(state.llm, "_entries", {})
    print(f"錄音檔共 {len(cassette)} 步，命中 {state.llm.hits} 次，呼叫 {state.llm.misses} 次")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

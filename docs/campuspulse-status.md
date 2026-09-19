# CampusPulse status

## Current stage
- Stage: 1 / M1 — deterministic planning vertical slice
- State: complete
- Goal: one repeatable scenario proves `initial plan → material trigger → changed plan`, computed entirely by deterministic code with no network and no model.

## Evidence
- Command: `.venv/Scripts/python.exe -m pytest -q` → **92 passed** in 0.49s (clock, scenario, graph, facility overrides, router, planner, API, SSE).
- Command: `.venv/Scripts/python.exe -m uvicorn app.main:app --port 3000` → the arc below, driven only by `POST /replay/advance`:

  | sim time | events | plan |
  | --- | --- | --- |
  | 07:50 | day_start | 低地板公車，08:35 出發 → 09:17 抵達，西側電梯 |
  | 08:06 | 電梯保養公告 | 改走**東側電梯**，出發提前到 08:33 |
  | 08:17 | 雨 15mm＋車輛 0 台＋積水 advisory | 步行與 YouBike 被安全性排除，出發提前到 **08:21**（步行段時間加倍） |
  | 08:23 | 公車到站更新 | 出發 08:28，抵達 09:17 |
  | 08:40 | 仍未出門 | `status: infeasible`，誠實回報「沒有任何可行方案」 |

- `GET /graph` → `blocked_ids: ["CSIE_W_ELEV"]` after the notice; the graph file itself is untouched.
- Demo path: `http://127.0.0.1:3000/` → 重設回放 → 前進 5 分鐘 ×11。計畫面板顯示選中方案、每段時間、使用的電梯、風險註記、被排除的方案與判斷依據。

## Decisions and assumptions
- **Scope is the merged reading of the two specs** (user's call, 2026-09-19). The seam is `MobilityProfile` → `ELIGIBLE_MODES`: `crutches` compares 低地板公車／公車／步行, `default` compares YouBike／公車／步行, `wheelchair` compares 低地板公車／復康巴士／步行. Every option ends with an accessible walking leg from the graph. See `docs/adr/0001-stack-and-merged-scope.md`.
- Stack follows `CLAUDE.md` (FastAPI single service, vanilla JS, JSON data files); method follows `references/` (stage gates, provider adapters, `SourceMode` labels, decision records).
- **Ranking**: hard safety rules are disqualifications inside the candidate builders (積水、階梯、陡坡、無車、非低地板), so they never reach the score. What is left is ranked by `total_seconds + 180s × 風險註記數`. Ranking on `conservative_eta` would be meaningless — every option's departure is derived backwards from the same required arrival, so they all land within a minute of each other.
- **The scenario was re-timed, and the reason matters**: at 0.6 m/s a crutches user needs ~34 min dry and ~48 min in the rain for this 2.2 km commute plus 438 m campus walk. A 09:00 class therefore requires leaving before 08:00, which leaves the 08:15 disruption nothing to change. The Wednesday presentation is now **09:30**, and the fixture's bus ride was corrected from 14 min to 8 min (2.1 km at 14 km/h was wrong). `product-brief.md`'s 09:00/08:35 figures were written for the `default` YouBike story and do not transfer to an accessibility profile unchanged.
- Notices become facility overrides via each fixture's `expected_override` block, logged as `tool="fixture_notice_extraction"`. M2 replaces this with Gemini extraction from the mail body; the block then becomes the manual fallback that Stage 2 requires.
- `data/campus_graph.json` is a **draft**: topology is hand-built to exercise the planner (a stairs shortcut vs a covered corridor, two elevators, one steep segment, a 2.2 km sidewalk), coordinates are approximate. `draft: true` is surfaced in `/health`, `/graph` and the UI.
- Fixtures use a synthetic student (`student@example.edu`) and a synthetic address. No real personal data.
- Live providers return an `unavailable` signal instead of silently falling back to fixtures.
- `/events?limit=N` exists for tests only — see the note at the top of `tests/test_events.py`.

## Bugs found and fixed in this stage
- **Injected events at the current instant were dropped forever.** The scheduled window is `(processed, until]`, so an event stamped "now" — exactly the on-stage photo-report path — was never perceived. `AgentState.inject_event` now perceives a due event immediately and leaves future-dated ones to the clock. Regression tests: `test_an_event_injected_at_the_current_instant_is_perceived`, `test_an_event_injected_in_the_future_waits_for_the_clock`.
- **A stale ETA flag could outrank 37 extra minutes of walking.** Counting risk flags lexicographically made one stale bus reading beat a much shorter journey for a crutches user. Fixed by the score above.

## Blockers
- None for M2.

## Open questions for the team (from CLAUDE.md 13)
- Team size, contest hours, and who owns which branch.
- Whether pre-contest data preparation and on-site photography are allowed.
- Which campus and which 5–8 buildings; who corrects the draft graph coordinates.
- Whether live mode is needed at all, or the demo stays fully on replay.
- **New**: is the 09:30 class time acceptable for the demo script, or should the commute distance shrink instead?

## Next gate
- M2 / Stage 1 完成條件的後半：Gemini function-calling 迴圈取代 `state.replan()` 的直接呼叫。Agent 決定「要不要重新規劃」，工具回傳仍是這一版的確定性計算；公告分類改由 Flash 從信件內文抽取（`expected_override` 降級為 fallback）；每一步的思考摘要與工具呼叫寫入 agent log 並經 SSE 推給前端。驗收：回放注入電梯保養公告後，log 可見模型選了哪個工具、拿到什麼結果、為什麼改計畫。

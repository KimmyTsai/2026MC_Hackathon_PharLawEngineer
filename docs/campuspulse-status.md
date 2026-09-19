# CampusPulse status

## Current stage
- Stage: M2 — Gemini function-calling agent loop
- State: complete
- Goal: the model decides whether to re-plan and which tools to call; every number still comes from deterministic code; the whole loop is visible in the agent log.

## Evidence
- Command: `.venv/Scripts/python.exe -m pytest -q` → **139 passed** (clock, scenario, graph, facility overrides, router, planner, orchestrator, cassette, API, SSE). No test touches the network: `tests/conftest.py::never_call_the_model` patches `build_llm`, and the agent tests script the model.
- Live run against `gemini-3.1-flash-lite`, three consecutive events, no fallback:

  | sim time | tools the model chose, in order | result |
  | --- | --- | --- |
  | 07:50 | `locate_room` → `plan_route` → `compare_travel_options` → `commit_plan` | 低地板公車，08:37 出發 |
  | 08:06 | `check_facility_notices` → `update_facility` → `compare_travel_options` → `commit_plan` | 改走東側電梯，08:33 出發 |
  | 08:17 | `compare_travel_options` → `commit_plan` | 08:21 出發（步行與 YouBike 被安全性排除） |

  The model read `mail_003` itself, resolved 「資訊系館西側電梯」 to `CSIE_W_ELEV`, and wrote the override. Its own words at 08:17: 「由於大學路出現積水且有短時強降雨，步行路線已不安全…我已將出發時間調整為 08:21。」
- Cassette replay: **14 hits / 0 misses**, byte-identical plans, zero API requests.
- Demo path: `重設回放` → `下一個事件 ▶` walks the scenario. The Agent badge names the model, or says 確定性規劃 with the reason when the model is unavailable.

## Gemini findings that shaped the design (all measured 2026-09-19)
| finding | consequence |
| --- | --- |
`GenerateRequestsPerDayPerProjectPerModel-FreeTier` = **20/day per model** | One agent run costs 2–6 requests, so ~4–8 runs a day. The demo replays a cassette instead of calling. |
`GenerateRequestsPerMinutePerProjectPerModel-FreeTier` = **5/min** on some models | `scripts/record_cassette.py` waits out the server's own `retryDelay`; a web request never waits. |
`gemini-3.1-pro-preview` → hard 429 | No Pro quota on the free tier. CLAUDE.md 7's "Pro decides the re-route" is not available; Flash does everything. `GEMINI_PRO_MODEL` stays configurable for a billed key. |
`gemini-3.8-flash`, `gemini-3.6-flash` → intermittent 503 "high demand" | Not safe to demo on. `gemini-3-flash-preview` measured 1.70s median and never failed. |
`gemini-2.5-flash` → 404 "no longer available to new users" | Do not put 2.5 model IDs in config. |
**Gemini 3 requires `thought_signature` back verbatim** | Rebuilding a `functionCall` part with `Part.from_function_call` drops it and the next turn is 400 INVALID_ARGUMENT. The adapter replays the provider's own `Content`, and the cassette stores it (base64 via `model_dump(mode="json")`, verified to round-trip). |

## Decisions and assumptions
- **Scope is the merged reading of the two specs** (user's call, 2026-09-19): `MobilityProfile` → `ELIGIBLE_MODES` decides which travel modes are candidates; every option ends with an accessible walking leg. See `docs/adr/0001-stack-and-merged-scope.md`.
- **The model chooses; code computes and validates.** `compare_travel_options` and `plan_route` return deterministic numbers. `commit_plan` refuses an infeasible option and tells the model why. If the model picks a feasible option that is not the ranked best, that is honoured and the divergence is recorded in the decision. If it never commits, the deterministic best is used and an assumption says so.
- **`send_email` is not a tool.** The authorization boundary is structural, not a prompt instruction; a test asserts the tool is absent. Outward actions land in `pending_confirmations` (M5).
- **`update_facility` validates the target against the graph**, so a hallucinated node id cannot enter state; `resolve_facility_hint` maps free text to a node in code, and returns null on ambiguity rather than guessing.
- Notice reading is Gemini Flash with a JSON schema; the fixture's `expected_override` is the manual fallback Stage 2 requires. Each notice reports `read_by: gemini | fixture_fallback`.
- Confidence below 0.7 marks a facility 「不確定」 and never reroutes — low-confidence photo reports ask instead of acting.
- The scenario was re-timed for physics (09:30 class) and the fixture bus ride corrected; see the M1 section below.
- `data/campus_graph.json` is still a **draft** (approximate coordinates), surfaced as `draft: true`.
- **The cassette is keyed on the exact question, simulated time included.** Use `下一個事件 ▶` / `POST /replay/next` in the demo so the times line up. `前進 5 分鐘` will miss and degrade to deterministic planning — correct, but not the presentation path. Re-record after changing prompts, tool declarations, tool output shapes, or the scenario.

## Bugs found and fixed
- **M2: replayed turns broke live continuation.** A cached tool turn had no `thought_signature`, so the next live call in the same run got 400. The cassette now stores the signed turn, and cache mode treats an unsigned tool turn as a miss so it is re-recorded complete.
- **M1: injected events at the current instant were dropped forever.** The scheduled window is `(processed, until]`, so an event stamped "now" — the on-stage photo-report path — was never perceived.
- **M1: a stale ETA flag could outrank 37 extra minutes of walking.** Risk flags were a lexicographic veto; they are now a 180s-per-flag time penalty, with hard safety rules handled as disqualifications instead.

## Blockers
- **The demo cassette only covers the first three moments** (07:45 start, 07:50 day_start, 08:05 notice, 08:15 weather), because today's free quota is spent across four models. Re-record the rest with `scripts/record_cassette.py --model <fresh model>` when quota resets. Later events still work — they degrade to deterministic planning and say so.

## Open questions for the team
- Team size, contest hours, and who owns which branch (CLAUDE.md 13).
- Whether pre-contest data preparation and on-site photography are allowed.
- Which campus and which 5–8 buildings; who corrects the draft graph coordinates.
- Is the 09:30 class time acceptable for the demo script, or should the commute distance shrink instead?
- **Is anyone willing to enable billing on the Gemini project?** 20 requests/day/model makes live rehearsal impossible, and it rules out Pro entirely.

## Previous stages
- **M1 / Stage 1 — deterministic planning.** 92 tests. `plan_route` (profile speeds, stairs/steep/rain rules, 60s per elevator, closed-facility exclusion) plus candidate comparison across eligible modes. Arc verified over HTTP: 07:50 初次計畫 → 08:06 電梯公告改走東側 → 08:17 雨+淹水+無車提前出發 → 08:40 誠實回報無可行方案. The scenario was re-timed because at 0.6 m/s a crutches user needs ~34 min dry and ~48 min in rain for this 2.2 km commute plus 438 m campus walk, so a 09:00 class leaves the 08:15 disruption nothing to change; the presentation moved to 09:30 and the fixture's 2.1 km bus ride was corrected from 14 min to 8 min.
- **M0 / Stage 0 — baseline shell.** 44 tests. FastAPI single service, SimClock (paused by default, never rewinds), provider interfaces with fixture implementations and honest `unavailable` live stubs, facility override layer, SSE, frontend shell with provider-mode badges.

## Next gate
- M3: Leaflet map showing the route, stopped facilities in red and reported points in orange, with the route visibly changing between plans; plus the decision timeline the rubric in `demo-and-acceptance.md` asks for. No new model calls, so no quota cost.
- Then M5 (confirmation-gated email) before M4, because the authorization boundary is the rubric's must-pass item and `pending_confirmations` is still empty.

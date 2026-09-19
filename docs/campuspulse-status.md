# CampusPulse status

## Current stage
- Stage: M5 — confirmation-gated email
- State: complete
- Goal: the agent can prepare an outward-facing action but cannot perform one. `demo-and-acceptance.md` lists this as a must-pass item.

## Basemap: Google Map Tiles (2026-09-19)
- The key in `MAPS_API_KEY.txt` now reaches **Map Tiles API, Maps Static API and Geocoding**; Places is still blocked (not needed).
- `MAP_PROVIDER=google` serves tiles through `/map/tiles/{z}/{x}/{y}.png`. Verified: `createSession` → token, tile returns a real 2520-byte PNG, second request is served from memory in 17 ms, and neither `/map/config` nor the page contains `key=`. Leaflet's attribution in the browser reads 「地圖資料 ©2026 Google」.
- Leaflet stays; only the tile source changed, so every vector layer, the campus/journey focus and the fallback behaviour are untouched. Five consecutive tile errors switch the page back to OSM and say so.
- **Not done, deliberately**: pointing Leaflet at `mt0.google.com/vt/...`. That is the popular recipe and it violates the Maps Terms of Service, which permit tile access only through the Maps APIs.
- Tiles are cached in memory only (400 tiles, LRU). Persistent on-disk caching is a Terms question this code cannot settle, and OSM already covers the offline case.

## Evidence (M5)
- `send_email` is **absent from the toolbox**; the model can only call `draft_email`, which creates a `ProposedAction` and returns `sent: false`. A test asserts the absence, so the boundary cannot be softened by editing a prompt.
- Walking the whole scenario: at 08:25 the agent warns that the window is closing (still feasible, no draft); at 08:35 it drafts. The body carries computed facts — 「現在出發最快也要 09:26 才會到，比計算機組織需要抵達的 09:18 晚約 9 分鐘」 — not model guesses.
- `GET /outbox` returns 0 until `POST /confirm/{id}` is called; confirming writes exactly one file; confirming again returns `duplicate: true` and still one file; cancelling sends nothing and a later approval is refused with 409; an expired draft cannot be sent.
- Whether the student is late is deterministic time arithmetic, so the dialog appears even when the model is unavailable.
- Confirmation dialog in the UI with 寄出／修改／取消; an edited body is what reaches the outbox.
- 20 tests in `tests/test_actions.py` plus 9 HTTP-level tests; 172 in total.
- Verified in the browser end to end: the dialog opens by itself at 08:35, 寄出 writes one file to the outbox, and the panel then reads 「已寄出・已寫入 outbox」.
- The whole scenario now replays from the cassette with **zero fallback and zero API requests** — 48 recorded turns on `gemini-3.5-flash-lite`, including the model's own `draft_email` call at 08:35.

## Evidence (M3)
- Command: `.venv/Scripts/python.exe -m pytest -q` → **143 passed**.
- Verified in a real browser, not by inspection: the map renders the campus graph over OSM tiles, the route draws through 資訊系館東側電梯 after the notice, the timeline marks processed events, and leg descriptions read 「校外租屋處（合成地點）→ 住處附近公車站」.
- Leaflet is **vendored** (`web/vendor/`), and a test asserts the page loads nothing over the network: venue wifi must not be able to break the demo. Tiles are the only network dependency and the vector layer stays readable without them.
- The map defaults to the **campus leg**, with a 全程 toggle. Fitting the whole 2.2 km commute shrinks the accessibility story — which elevator, which ramp — to a few pixels.
- Demo entry point is the **重設並開始** button (`POST /replay/start`), then 下一個事件 ▶.

## Evidence (M2)
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

## Bugs found and fixed in M5
- **The 寄出 button silently did nothing.** The dialog relied on `<form method="dialog">` and `returnValue`; the implicit submit never fired the request, so the click closed the dialog and sent nothing. Found by clicking it in a real browser and checking the outbox, not by reading the code. The three buttons now have explicit handlers, and Escape leaves the draft pending rather than deciding for the student.
- **Windows cannot store the idempotency key as a filename.** `late-notice:cmt_01:20260923.json` raised `OSError: Invalid argument` at the moment of sending — the demo's final step. The mailbox now derives a safe filename (sanitised slug plus a hash of the key, so distinct keys stay distinct) and keeps the readable key inside the record.
- **Tests were writing into `data/outbox/` and polluting each other.** Sending is idempotent per commitment per simulated day, so one test's message made the next test see a non-empty outbox. `OUTBOX_PATH` now redirects it, and an autouse fixture points every test at a temp directory.

## Bugs found and fixed in M3 (all found by driving the real browser)
- **Refresh storm.** One `/replay/next` publishes a dozen SSE frames and each triggered a full refresh plus a map redraw and an animated `fitBounds`, queueing animations until the page stalled. Refreshes are now coalesced, and the map only re-fits when the route or focus actually changed. Verified from the server log: three clicks now produce exactly three `/state` and three `/graph`.
- **重設 broke every cassette hit.** The recording starts with a plan made at the scenario's opening time; `/replay/reset` skips that run, so every later question differed and every recorded turn missed. The button now calls `/replay/start`.
- **A stale cassette was only detectable as a run of silent misses.** Editing the scenario's `expect` text changed `trigger.materiality`, which reaches the model, which changed every key. The cassette now stores a **fingerprint** of prompt + tool declarations + scenario; the badge says 「錄音檔已過期，需重錄」 and `test_the_shipped_cassette_matches_the_current_questions` fails the build.

## Bugs found and fixed
- **M2: replayed turns broke live continuation.** A cached tool turn had no `thought_signature`, so the next live call in the same run got 400. The cassette now stores the signed turn, and cache mode treats an unsigned tool turn as a miss so it is re-recorded complete.
- **M1: injected events at the current instant were dropped forever.** The scheduled window is `(processed, until]`, so an event stamped "now" — the on-stage photo-report path — was never perceived.
- **M1: a stale ETA flag could outrank 37 extra minutes of walking.** Risk flags were a lexicographic veto; they are now a 180s-per-flag time penalty, with hard safety rules handled as disqualifications instead.

## Blockers
- **The demo cassette covers 07:45 start, 07:50 day_start and 08:05 notice.** 08:15 onwards degrades to deterministic planning, labelled in the log. Today's 20-per-day quota is spent on five models (`gemini-3-flash-preview`, `3.1-flash-lite`, `3.5-flash`, `3.6-flash`, `3.7-flash`). Re-record with `scripts/record_cassette.py --model <fresh model>` when quota resets.
- **The second API key does not work yet.** It belongs to GCP project `631356509762` and returns `403 PERMISSION_DENIED / API_KEY_SERVICE_BLOCKED`: the key has API restrictions that exclude the Generative Language API. Fix in Cloud Console → APIs & Services → Credentials → that key → API restrictions, and confirm the API is enabled on the project. The code already supports several keys in one file, selected with `GEMINI_KEY_INDEX`; because the free quota is **per project**, a working second key doubles the daily budget.

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
- **M4**: material-change thresholds, debounce and cooldown, scheduled rechecks. Today every event re-plans; `demo-and-acceptance.md` asks that oscillating signals must not spam notifications or actions. Deterministic, so no quota cost.
- Then **M6** (timetable image ingestion) and **M7** (photo reports), both of which need Gemini vision and therefore quota.
- **M9 live mode stays optional.** The demo is deliberately replay-only.

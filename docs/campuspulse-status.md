# CampusPulse status

## Current stage
- Stage: 0 / M0 — baseline and runnable shell
- State: complete
- Goal: a new developer can start the app and run the checks with no credentials; every provider is behind an interface with a fixture implementation and an honest mode badge.

## Evidence
- Command: `uv pip install --python .venv/Scripts/python.exe -r requirements.txt` → 1 extra dep needed on Windows (`tzdata`); added to requirements.
- Command: `.venv/Scripts/python.exe -m pytest -q` → **44 passed** (clock, scenario timeline, graph loader, facility overrides, API, SSE).
- Command: `.venv/Scripts/python.exe -m uvicorn app.main:app --port 8123` → `/health` 200, `/` 200, `/graph` 200, `/state` 200, `/static/style.css` 200.
- `/health` reports `mode=replay`, `sim_time=2026-09-23T07:45:00+08:00`, graph `22 nodes / 28 edges / 3 rooms` (`draft: true`), 2 commitments for the scenario weekday, and all eight provider badges as `fixture`.
- SSE verified against a live server: `curl -N /events` returns `data: {"type": "hello", ...}` immediately.
- Demo path: open `http://127.0.0.1:3000/`, read the badges and the 07:45 sim clock, press 前進 5 分鐘 and watch the signal values change at the scenario's event times.

## Decisions and assumptions
- **Scope is the merged reading of the two specs** (user's call, 2026-09-19). `CLAUDE.md` describes accessible walking routes inside campus; `references/product-brief.md` describes a YouBike↔bus city commute. The seam is `MobilityProfile`: it decides which travel modes are candidates at all (`ELIGIBLE_MODES` in `app/models.py`), so `crutches` compares low-floor bus / walk while `default` compares YouBike / bus, and **every** option ends with an accessible on-campus walking leg from the graph.
- Stack follows `CLAUDE.md` (FastAPI single service, vanilla JS + Leaflet, JSON data files), not the greenfield monorepo default in `references/architecture-and-contracts.md`, because that document defers to an explicit user choice. See `docs/adr/0001-stack-and-merged-scope.md`.
- Python 3.13 in practice (spec says 3.11). `tzdata` is required because Windows has no system tz database.
- `data/campus_graph.json` is a **draft**: topology is hand-built to exercise the planner (a stairs shortcut vs a covered corridor, two elevators, one steep segment), coordinates are approximate. `draft: true` is surfaced in `/health`, `/graph` and the UI.
- Fixtures carry a synthetic student, `student@example.edu`, and a synthetic off-campus address. No real personal data.
- `SimClock` is paused by default and never rewinds; a reset builds a new clock at the scenario start. `/events?limit=N` exists for tests only — see the note in `tests/test_events.py`.
- Live providers return an `unavailable` signal instead of silently falling back to fixtures.

## Blockers
- None for M1.

## Open questions for the team (from CLAUDE.md 13)
- Team size, contest hours, and who owns which branch.
- Whether pre-contest data preparation and on-site photography are allowed.
- Which campus and which 5–8 buildings; who corrects the draft graph coordinates.
- Whether live mode is needed at all, or the demo stays fully on replay.

## Next gate
- M1 / Stage 1: `plan_route` + the multimodal candidate comparison. Tests must show: crutches never takes stairs; a closed west elevator re-routes to the east elevator; rain prefers covered segments; a steep segment is refused for `wheelchair`; zero bikes makes the YouBike option infeasible rather than merely slower; ETA arithmetic is exact and deterministic.

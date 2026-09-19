# ADR 0001 — Stack, and merging the two product specs

- Date: 2026-09-19
- Status: accepted

## Context

The repository carries two specifications that describe different products under
the same name:

- `CLAUDE.md` — a campus agent for students with limited mobility. Accessible
  walking routes over a `networkx` graph of entrances, elevators, ramps and
  stairs; FastAPI single service; vanilla JS + Leaflet; JSON data files;
  milestones M0–M9.
- `references/` (`SKILL.md`, `product-brief.md`,
  `architecture-and-contracts.md`, `pipeline.md`, `demo-and-acceptance.md`) — a
  city commute agent. YouBike versus Tainan bus, rain, flood and AQI signals;
  greenfield default of a TypeScript/Python monorepo; stages 0–7.

The planner core — the most expensive part to build — differs between them.

## Decision

1. **Scope: merge both, with `MobilityProfile` as the seam.** A profile decides
   which travel modes are candidates at all (`ELIGIBLE_MODES` in
   `app/models.py`):

   | profile | candidate modes |
   | --- | --- |
   | `wheelchair` | walk, low-floor bus, paratransit |
   | `crutches` | walk, low-floor bus, bus |
   | `default` | walk, YouBike, bus |

   Every candidate ends with an accessible on-campus walking leg computed from
   the graph. The same signal set (rain, flood, AQI, bike availability, bus ETA,
   facility status) feeds every profile; only the candidate set and the weights
   change. `default` therefore reproduces the YouBike↔bus story in
   `product-brief.md`, and `crutches`/`wheelchair` reproduce the elevator and
   ramp story in `CLAUDE.md`.

2. **Stack follows `CLAUDE.md`**: Python 3.11+ / FastAPI single service,
   `networkx`, JSON data files, vanilla JS + Leaflet, SSE for live updates,
   `pytest`. `SKILL.md` instructs using the monorepo default only in a
   greenfield repository "unless the user specifies otherwise", and `CLAUDE.md`
   is that specification.

3. **Method follows `references/`**: stage gates with observable evidence,
   typed provider adapters with fixture and live implementations behind one
   contract, `SourceMode` labels (`live` / `fixture` / `stale` / `unavailable`)
   on everything the UI shows, `DecisionRecord` for explainability, deterministic
   code for time arithmetic, authorization, idempotency and safety, and
   `docs/campuspulse-status.md` as the running status file.

4. **Milestone numbering**: CLAUDE.md's M0–M9 is the working list; each entry in
   the status file also names the `pipeline.md` stage it satisfies.

## Consequences

- One service to run and one place to reason about time, which matters for a
  one-day contest.
- The accessibility profile is what makes the multimodal comparison interesting
  rather than a second, parallel feature: a wheelchair user cannot ride a
  YouBike, so low-floor bus availability becomes the safety-critical signal that
  bike availability is for the `default` profile.
- Scope is larger than either document alone. If time runs short, cut from the
  back of the M-list (M9 live mode, M8 on-device Gemma, M7 photo reports), never
  the confirmation boundary on outward-facing actions.
- Python 3.13 is what is installed; `tzdata` is an explicit dependency because
  Windows ships no system tz database.

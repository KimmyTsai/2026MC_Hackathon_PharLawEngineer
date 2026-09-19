# Architecture and normalized contracts

## Greenfield default

When the repository has no chosen architecture, prefer a small monorepo:

- `apps/web`: TypeScript web/PWA interface optimized for mobile use.
- `apps/api`: Python API and monitoring worker.
- `packages/contracts` or generated schemas: shared API contract.
- `fixtures/scenarios`: deterministic input timelines.
- `docs`: status and architecture decisions.

Use the project's existing framework if one exists. Avoid introducing a database, queue, or orchestration framework until a stage requires it. SQLite is sufficient for a single-user PoC; retain repository interfaces so storage can change later.

## Core entities

Keep equivalent typed models even if names differ:

### Commitment

- Stable ID, title, start/end, timezone, destination, importance, required arrival buffer.
- Source and source reference, extraction confidence, and user-confirmed flag.

### Signal

- Provider, kind, observed time, valid-until time, geography, normalized value, source mode, confidence, and error state.

### TravelOption

- Mode, ordered legs, departure window, ETA distribution or conservative ETA, required transfers, availability, safety/risk flags, evidence, and provider modes.

### Plan

- Commitment ID, selected option, alternatives, generated time, assumptions, rationale, next check time, and status.

### Trigger

- Kind, old/new values, materiality reason, observed time, and affected plan.

### ProposedAction

- Type, payload preview, required authorization, idempotency key, expiration, state, and audit timestamps.

### DecisionRecord

- Normalized inputs, rejected options and reasons, selected option, policy/rule versions, model identifier if used, and trace/correlation ID. Redact private content.

## Provider boundaries

Use small interfaces whose live and fixture implementations return the same normalized objects:

- `get_weather(area, at)`
- `get_bus_eta(stop_or_route, at)`
- `get_bike_status(station_or_area, at)`
- `get_road_events(corridor, at)`
- `get_flood_sensors(area, at)`
- `get_air_quality(area, at)`
- `estimate_route(origin, destination, mode, depart_at)`
- `read_course_messages(window)`
- `preview_calendar_change(change)` / `execute_calendar_change(approved_action)`
- `preview_email(message)` / `send_email(approved_action)`

These are conceptual contracts, not required function names. Preserve typed errors such as `unavailable`, `unauthorized`, `rate_limited`, `invalid_response`, and `stale`.

## Planner invariants

- Use a conservative arrival estimate. A plan is feasible only when that estimate satisfies the event-specific arrival buffer.
- Increase the buffer or reliability weight for exams, presentations, labs, and professor meetings.
- Safety restrictions outrank convenience. A flood warning can disqualify an otherwise faster bike or walking route.
- Penalize stale and low-confidence evidence; surface when missing evidence could change the choice.
- Do not replan for tiny fluctuations. Define material thresholds and use hysteresis/cooldowns to prevent oscillation.
- If no option is feasible, say so and propose the least harmful next action; do not fabricate an on-time route.
- Identical inputs and policy versions should produce stable decisions unless deliberate stochastic behavior is recorded and justified.

## Model boundary

Use models for perception and interpretation: image extraction, message classification, ambiguity resolution, summaries, and natural-language rationale. Validate model output against schemas.

Use deterministic application code for authorization, time arithmetic, freshness, hard safety rules, idempotency, state transitions, and external writes. Do not let free-form model text directly invoke a side effect.

## Observability

Expose a user-readable timeline while keeping internal logs structured. At minimum record:

- What changed.
- Which plan assumption was invalidated.
- Which alternatives were reconsidered.
- Why the selected plan won.
- What action was proposed or executed.
- When the agent will check again.

Every signal shown in a decision needs a timestamp and source mode. Redact secrets and minimize private content.

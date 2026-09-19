# Delivery pipeline

Use the first incomplete stage unless the user requests a specific one. A stage is complete only when its gate is demonstrated, not when files merely exist.

## Stage 0 — Baseline and runnable shell

Deliver:

- Reproducible local setup, `.env.example`, lint/type/test commands, and a minimal web or mobile-friendly surface.
- Health endpoint and a visible provider-mode badge (`fixture`, `live`, `stale`, or `unavailable`).
- `docs/campuspulse-status.md` and an architecture decision record for any newly chosen stack.

Gate: a new developer can start the app from documented commands, load the shell, and run the baseline checks without private credentials.

## Stage 1 — Deterministic agent-loop vertical slice

Deliver the canonical demo entirely with typed fixture data:

- A sample commitment and initial YouBike plan.
- A time-controllable scenario runner.
- A disruption event that changes rain, bike availability, and flood risk.
- A re-planning decision that selects the bus and explains why.
- An email action preview that cannot send.

Gate: one repeatable automated scenario proves `initial plan → material trigger → changed plan → proposed action`, with tests for the transition and no network dependency.

## Stage 2 — Timetable ingestion and correction

Deliver:

- Timetable image upload.
- Structured extraction with confidence per field.
- Review/edit screen before commitments are stored.
- Duplicate detection and timezone-aware recurrence.
- A manual-entry fallback when model extraction is unavailable.

Gate: fixture images cover at least clean, low-confidence, and malformed cases; stored commitments exactly match the user's reviewed values.

## Stage 3 — Provider adapters

Implement one provider at a time behind the normalized interfaces in `architecture-and-contracts.md`. Prefer the provider that most improves the canonical demo.

Suggested order:

1. Weather/rain.
2. YouBike.
3. Tainan bus ETA.
4. Flood signals.
5. AQI.
6. Route-time provider.

Each adapter needs timeout handling, timestamp/freshness rules, normalized errors, a fixture implementation, and contract tests. Cache according to source cadence; do not disguise stale values as current.

Gate: live mode can be enabled per provider without changing planner code, and loss of one noncritical provider degrades transparently rather than crashing the loop.

## Stage 4 — Planner and monitoring service

Deliver:

- Commitment criticality and configurable arrival buffers.
- Candidate comparison using conservative ETA, availability, safety, and freshness.
- Material-change thresholds and debounce/cooldown behavior.
- Scheduled rechecks plus event-triggered rechecks.
- Decision records suitable for the UI and debugging.

Gate: tests cover ordinary class, presentation/exam, stale data, no feasible option, oscillating signals, and a recovered plan. Repeated identical input must not spam notifications or actions.

## Stage 5 — Google integrations and authorization

Deliver only after the core loop is stable:

- Read-only Gmail course-message classification with minimal scopes.
- Calendar create/update preview and explicit execution.
- Email draft preview and explicit execution.
- Idempotency keys, audit records, and test-account instructions.

Gate: dry-run mode is the default; mocked tests cover all paths; a real test-account walkthrough succeeds only after an explicit user confirmation and does not duplicate events or messages.

## Stage 6 — Demo hardening

Deliver:

- One-click fixture reset and seeded scenario.
- A visible timeline of perceptions, decisions, actions, and reflection.
- Clear live/fixture labels and a fallback path if an API is unavailable.
- Mobile-width layout, loading/error states, and accessible controls.
- A 3–5 minute scripted presentation with expected screen states.

Gate: follow the rubric in `demo-and-acceptance.md`; run the complete demo twice from a clean reset without manual database repair.

## Stage 7 — Optional expansion

Only after Stage 6: other campuses, classmate coordination using simulated accounts, on-device/offline support, idle-time suggestions, earthquake mode, and production operations.

## Status file template

Create `docs/campuspulse-status.md` with this structure and keep entries factual:

```markdown
# CampusPulse status

## Current stage
- Stage: 0
- State: in progress
- Goal: ...

## Evidence
- Command: `...`
- Result: ...
- Demo path: ...

## Decisions and assumptions
- ...

## Blockers
- None.

## Next gate
- ...
```

Do not mark a stage complete without evidence. Never place secrets, tokens, private message bodies, or precise personal locations in this file.

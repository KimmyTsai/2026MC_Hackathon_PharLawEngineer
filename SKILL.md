---
name: campuspulse-pipeline
description: "Build, continue, or review the CampusPulse NCKU proof of concept: a proactive campus commute agent that understands schedules, monitors live conditions, replans travel, and safely proposes external actions. Use for CampusPulse architecture, implementation, integration, testing, and competition-demo work; do not use for unrelated navigation apps."
---

# CampusPulse Pipeline

Turn the CampusPulse concept into a working, demonstrable vertical slice. Optimize for a reliable competition demo, explicit evidence, and incremental delivery—not a broad but hollow platform.

## Start by grounding the repository

1. Read repository instructions such as `AGENTS.md`, then inspect the tree, README, package manifests, tests, environment examples, and `git status`.
2. Preserve the existing stack and conventions. In a greenfield repository, use the defaults in [architecture-and-contracts.md](references/architecture-and-contracts.md) unless the user specifies otherwise.
3. Read [product-brief.md](references/product-brief.md) before changing product behavior or scope.
4. Read [pipeline.md](references/pipeline.md) to identify the first incomplete stage and its gate. Do not rebuild a stage already supported by working code and tests.
5. Read [demo-and-acceptance.md](references/demo-and-acceptance.md) when preparing, testing, or judging the demo.

If the repository lacks `docs/campuspulse-status.md`, create it from the status template in `pipeline.md`. Keep it concise and update it after each completed stage.

## Execute one coherent milestone at a time

- Select the earliest incomplete stage, or the specific stage requested by the user.
- State the milestone, observable acceptance criteria, and any assumptions before editing.
- Implement the smallest end-to-end path that satisfies the gate. Prefer one working vertical slice over multiple disconnected modules.
- Keep external data providers behind typed adapters. Every live integration must have a deterministic fixture or simulator using the same normalized contract.
- Run the narrowest relevant tests first, then broader checks. Fix failures caused by the change; report unrelated pre-existing failures separately.
- Update `docs/campuspulse-status.md` with evidence: commands run, tests passed, demo path, remaining blockers, and the next stage.
- Stop after a useful milestone unless the user explicitly asks for the full pipeline in one run. When continuing automatically, re-check the gate before entering the next stage.

## Preserve the agent behavior

The implemented loop must remain observable:

1. **Perceive:** normalize schedules, messages, location, weather, transit, bike, flood, and air-quality signals.
2. **Plan:** compare feasible travel options using arrival confidence, event importance, data freshness, and safety risk.
3. **Act:** update local recommendations immediately; treat messages and calendar writes as proposed actions until authorized.
4. **Reflect:** observe time, state, and user feedback; replan only when a material trigger invalidates the current plan.

Keep planning decisions explainable. Return the chosen option, rejected alternatives, decisive signals, freshness timestamps, confidence, and the next re-evaluation time. The model may interpret unstructured inputs, but deterministic code must enforce deadlines, confirmation policy, idempotency, and safety constraints.

## Safety and privacy invariants

- Never send email, modify a real calendar, contact classmates, or share location without the user's explicit authorization for that action or an already configured approval policy.
- Default to dry-run previews for external writes. Show recipient, content, and intended change before requesting approval.
- Never expose API keys, OAuth tokens, raw private email bodies, or precise location in logs, fixtures, screenshots, or committed files.
- Use synthetic identities and locations in demos unless the user explicitly supplies safe test data.
- Treat earthquake and flood features as assistance, not authoritative emergency guidance. Surface official sources and uncertainty; do not claim guaranteed safety.
- Do not silently substitute simulated data for live data. Mark every provider and signal as `live`, `fixture`, `stale`, or `unavailable`.

## Definition of done

A milestone is complete only when its gate passes with observable evidence. A polished UI alone is not an agent, and a planner without a state-changing disruption is not a complete CampusPulse demo. The final PoC must demonstrate the initial plan, a simulated or live disruption, autonomous re-planning, a changed recommendation, and a confirmation-gated external action.

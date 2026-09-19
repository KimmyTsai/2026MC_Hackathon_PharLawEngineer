# Demo and acceptance rubric

## Required competition demo

The shortest convincing path is:

1. Reset the fixture scenario.
2. Upload or select a timetable image and review the extracted Wednesday 09:00 NCKU class.
3. Show the stored commitment and its importance.
4. Advance to 07:50 and show the initial YouBike plan, departure time, ETA, evidence timestamps, and next check.
5. Advance to 08:15; inject rain, bike shortage, and flood-risk events.
6. Show the system detect a material change without a new destination query.
7. Show the bus plan replacing the YouBike plan and the explanation for rejected alternatives.
8. Advance the departure-state fixture; show the late-risk warning and an email preview.
9. Demonstrate that the email cannot be sent until explicit confirmation.
10. End on the event timeline showing perception, planning, action proposal, and reflection.

## Must-pass behavioral checks

| Area | Acceptance evidence |
| --- | --- |
| Context | Destination and deadline come from the reviewed commitment. |
| Continuity | Monitoring continues after the initial recommendation. |
| Replanning | A material event changes the selected option or clearly explains why it does not. |
| Explainability | Decision shows decisive signals, timestamps, source modes, and rejected alternatives. |
| Safety | Flood risk can block an unsafe route; uncertainty is surfaced. |
| Authorization | External writes default to preview and require approval. |
| Reliability | Fixture reset reproduces the same transition. |
| Degradation | Missing live data is labeled and does not masquerade as a valid signal. |
| UX | Mobile-width flow remains readable and has useful loading/error states. |
| Agent loop | Timeline visibly includes perception, planning, proposed action, and reflection. |

## Tests worth prioritizing

- Initial plan is feasible under baseline fixtures.
- Bike availability dropping to zero invalidates the bike option.
- Flood risk disqualifies exposed walking/bike legs.
- Heavy rain alone does not always force a switch if the alternative would miss an exam.
- Stale signals reduce confidence and are labeled.
- Oscillating bike counts do not create notification spam.
- Re-running the same approved action does not duplicate an event or email.
- An unapproved proposed action has no external side effect.
- Malformed timetable extraction reaches the correction flow.
- No feasible plan produces an honest late-risk state.

## Presentation quality

Prefer a single decision timeline over a wall of dashboards. Judges should understand within seconds:

- Where the student needs to be and why it matters.
- What the current plan is.
- What changed in the world.
- Why the agent changed its mind.
- What it wants to do next and whether approval is required.

Keep live integrations optional during judging. A deterministic demo is the source of truth; live provider badges can demonstrate extensibility without making the presentation fragile.

## Failure conditions

Do not call the PoC complete if any of these remain true:

- The route changes only after the user manually asks again.
- The disruption is visual but never reaches planner state.
- The UI claims live data while using fixtures.
- The model invents transport times without provider evidence.
- Email or calendar writes occur without an approval boundary.
- The demo cannot be reset and reproduced.

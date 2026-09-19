# CampusPulse product brief

## Product promise

CampusPulse is a proactive campus itinerary agent for Taiwanese university students. It understands class schedules and course messages, monitors changing urban conditions, and revises departure time, transport mode, and follow-up actions before the student is late.

The NCKU proof of concept is the initial scope. It should feel different from a route-search app because it knows the student's commitment, continues observing after the first route is produced, and can act—with confirmation—when the plan fails.

## Primary demo story

Use this story as the canonical vertical slice:

1. A student uploads a timetable image containing a Wednesday 09:00 class at NCKU's Cheng Kung campus.
2. CampusPulse extracts the course, time, room, campus, and importance, then creates a local commitment. Calendar sync may be previewed or executed with approval.
3. At 07:50 the system reads location and transport signals. It recommends leaving at 08:35 and taking YouBike, with an ETA of 08:52.
4. At 08:15 the scenario changes: heavy rain is forecast, the nearest YouBike station drops to one or zero bikes, and the route gains a flood warning.
5. The agent detects that the plan is no longer acceptable and switches to a bus, recommending departure at 08:22 and arrival at 08:48.
6. If the student still has not left, the agent warns that the on-time window is closing and offers a preview of an email to the teaching assistant. Sending requires confirmation.

The demo must show both state transitions and why the decision changed.

## NCKU PoC scope

Build these capabilities first:

- NCKU campuses and major buildings.
- Timetable image ingestion and manual correction.
- Tainan bus and YouBike status.
- Rain/weather, AQI, and flood-risk signals.
- A persistent commitment and travel-plan view.
- Deterministic disruption simulation.
- Proactive re-planning and clear notifications.
- Gmail course-message reading and Google Calendar synchronization only after the core loop works; use preview/dry-run behavior by default.

Defer nationwide campus support, real classmate ride matching, computer-vision damage inspection, and production emergency response until after the PoC gate.

## Model roles

Treat model names as configurable capabilities rather than hard-coded dependencies:

- A low-latency multimodal model handles timetable extraction, email classification, signal summaries, and notification drafting.
- A stronger reasoning model compares plans and chooses tool calls when deterministic rules alone are insufficient.
- An optional on-device model supports offline schedule access, coarse departure-state detection, private local reminders, and cached safety guidance.

Do not call multiple models merely to claim model diversity. Each call needs a measurable reason such as latency, privacy, offline availability, or planning quality.

## What makes it agentic

- It derives the destination and deadline from existing context rather than requiring a fresh destination query.
- It keeps monitoring after the first recommendation.
- It interprets the importance of exams, presentations, labs, and meetings.
- It changes the plan when observed conditions invalidate assumptions.
- It can prepare or execute downstream actions under an explicit authorization policy.
- It checks the result and schedules the next observation.

## Non-goals

- Reimplementing a full map-routing engine.
- Claiming safety or on-time guarantees.
- Treating an LLM-generated answer as live transport evidence.
- Collecting other students' locations for the PoC.
- Building a dashboard whose cards never affect a decision.

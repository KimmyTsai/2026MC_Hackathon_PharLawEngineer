"""FastAPI entrypoint.

Endpoints implemented at M0: /health, /state, /graph, /events (SSE), and the
replay controls. /schedule, /report and /confirm answer 501 with the milestone
that will implement them, so the API surface is visible but never pretends.
"""

from __future__ import annotations

import asyncio
import json
from contextlib import asynccontextmanager
from datetime import datetime
from typing import Any

from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import FileResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel

from app.agent.state import AgentState, EventBus
from app.clock import SimClock
from app.config import RunMode, Settings, get_settings
from app.graph.loader import load_campus_graph
from app.graph.status import FacilityStatusStore
from app.sources.registry import ProviderRegistry
from app.sources.scenario import ScenarioEvent, load_scenario

HEARTBEAT_SECONDS = 15.0


def build_state(settings: Settings, bus: EventBus | None = None) -> AgentState:
    scenario = load_scenario(settings.scenario_path)
    clock = SimClock(scenario.start_at, speed=settings.replay_speed)
    graph = load_campus_graph(settings.graph_path, settings.rooms_path)
    facilities = FacilityStatusStore()
    providers = ProviderRegistry(settings, scenario)
    # Keep the bus across scenario swaps so connected SSE clients stay attached.
    return AgentState(settings, scenario, clock, graph, facilities, providers, bus or EventBus())


@asynccontextmanager
async def lifespan(app: FastAPI):
    settings = get_settings()
    app.state.settings = settings
    app.state.agent = build_state(settings)
    yield


app = FastAPI(title="CampusPulse", version="0.1.0", lifespan=lifespan)


def agent(request: Request) -> AgentState:
    return request.app.state.agent


# --------------------------------------------------------------------------- #
# read endpoints
# --------------------------------------------------------------------------- #
@app.get("/health")
def health(request: Request) -> dict[str, Any]:
    state = agent(request)
    now = state.clock.now()
    return {
        "status": "ok",
        "version": app.version,
        "mode": state.settings.mode.value,
        "scenario": state.scenario.name,
        "sim_time": now.isoformat(),
        "clock": {"kind": state.clock.mode, "running": state.clock.running,
                  "speed": state.clock.speed},
        "graph": {
            "draft": state.graph.draft,
            "nodes": len(state.graph.nodes),
            "edges": len(state.graph.edges),
            "rooms": len(state.graph.rooms),
        },
        "commitments": len(state.commitments),
        "provider_modes": state.providers.modes(now),
        "sse_subscribers": state.bus.subscriber_count,
    }


@app.get("/state")
def read_state(request: Request) -> dict[str, Any]:
    return agent(request).snapshot()


@app.get("/graph")
def read_graph(request: Request) -> dict[str, Any]:
    state = agent(request)
    now = state.clock.now()
    payload = state.graph.to_dict()
    payload["overrides"] = [o.model_dump(mode="json") for o in state.facilities.active(now)]
    payload["blocked_ids"] = sorted(state.facilities.blocked_ids(now))
    payload["unconfirmed_ids"] = sorted(state.facilities.unconfirmed_ids(now))
    payload["as_of"] = now.isoformat()
    return payload


@app.get("/events")
async def events(request: Request, limit: int | None = None) -> StreamingResponse:
    """SSE stream of agent log, plan, trigger and clock events.

    `limit` closes the stream after that many data frames. The browser never
    passes it; tests do, because an endless generator cannot be cancelled from
    an in-process ASGI client.
    """
    state = agent(request)
    queue = state.bus.subscribe()

    async def stream():
        sent = 0
        try:
            hello = {"type": "hello", "data": {"now": state.clock.now().isoformat(),
                                               "mode": state.settings.mode.value}}
            yield f"data: {json.dumps(hello, ensure_ascii=False)}\n\n"
            sent += 1
            while limit is None or sent < limit:
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(queue.get(), timeout=HEARTBEAT_SECONDS)
                except asyncio.TimeoutError:
                    yield ": heartbeat\n\n"
                    continue
                yield f"data: {json.dumps(event, ensure_ascii=False)}\n\n"
                sent += 1
        finally:
            state.bus.unsubscribe(queue)

    return StreamingResponse(
        stream(),
        media_type="text/event-stream",
        headers={"Cache-Control": "no-cache", "X-Accel-Buffering": "no"},
    )


# --------------------------------------------------------------------------- #
# replay controls
# --------------------------------------------------------------------------- #
class ReplayStartRequest(BaseModel):
    scenario: str | None = None
    speed: float | None = None
    autostart: bool = False


@app.post("/replay/start")
def replay_start(body: ReplayStartRequest, request: Request) -> dict[str, Any]:
    settings: Settings = request.app.state.settings
    if settings.mode is not RunMode.replay:
        raise HTTPException(409, "replay controls require MODE=replay")

    if body.scenario and body.scenario != settings.scenario:
        candidate = settings.data_dir / "scenarios" / f"{body.scenario}.json"
        if not candidate.exists():
            raise HTTPException(404, f"scenario {body.scenario!r} not found")
        settings = settings.model_copy(update={"scenario": body.scenario})
        request.app.state.settings = settings
        request.app.state.agent = build_state(settings, bus=request.app.state.agent.bus)

    state = agent(request)
    state.reset()
    if body.speed:
        state.clock.set_speed(body.speed)
    if body.autostart:
        state.clock.start()
    state.log("perceive", f"回放情境 {state.scenario.name} 已重設至 "
                          f"{state.clock.now():%H:%M}")
    return {"scenario": state.scenario.name, "now": state.clock.now().isoformat(),
            "speed": state.clock.speed, "running": state.clock.running,
            "events": len(state.scenario.events)}


class AdvanceRequest(BaseModel):
    seconds: float | None = None
    to: str | None = None


@app.post("/replay/advance")
def replay_advance(body: AdvanceRequest, request: Request) -> dict[str, Any]:
    state = agent(request)
    if body.to:
        target = datetime.fromisoformat(body.to)
        if target.tzinfo is None:
            target = target.replace(tzinfo=state.scenario.start_at.tzinfo)
        try:
            state.clock.advance_to(target)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    elif body.seconds is not None:
        try:
            state.clock.advance_by(body.seconds)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    else:
        raise HTTPException(400, "provide seconds or to")
    now = state.clock.now()
    state.bus.publish({"type": "clock", "data": {"now": now.isoformat()}})
    return {"now": now.isoformat()}


class InjectRequest(BaseModel):
    type: str
    payload: dict[str, Any] = {}
    at: str | None = None


@app.post("/replay/inject")
def replay_inject(body: InjectRequest, request: Request) -> dict[str, Any]:
    state = agent(request)
    at = state.clock.now()
    if body.at:
        parsed = datetime.fromisoformat(body.at)
        at = parsed if parsed.tzinfo else parsed.replace(tzinfo=at.tzinfo)
    event = ScenarioEvent(at=at, type=body.type, payload=body.payload, expect="live injection")
    state.scenario.inject(event)
    state.log("perceive", f"現場注入事件 {event.type} @ {at:%H:%M}", tool="replay_inject",
              tool_args={"type": event.type, "payload": event.payload})
    return {"injected": event.type, "at": at.isoformat()}


@app.post("/replay/reset")
def replay_reset(request: Request) -> dict[str, Any]:
    state = agent(request)
    state.reset()
    return {"reset": True, "now": state.clock.now().isoformat()}


# --------------------------------------------------------------------------- #
# not implemented yet — each names the milestone that will deliver it
# --------------------------------------------------------------------------- #
@app.post("/schedule")
def upload_schedule() -> None:
    raise HTTPException(501, "timetable image ingestion lands in M6 / Stage 2")


@app.post("/report")
def submit_report() -> None:
    raise HTTPException(501, "photo reporting lands in M7")


@app.post("/confirm/{action_id}")
def confirm_action(action_id: str) -> None:
    raise HTTPException(501, "confirmation flow lands in M5 / Stage 5")


# --------------------------------------------------------------------------- #
# static frontend
# --------------------------------------------------------------------------- #
_settings = get_settings()
if _settings.web_dir.exists():
    app.mount("/static", StaticFiles(directory=_settings.web_dir), name="static")

    @app.get("/")
    def index() -> FileResponse:
        return FileResponse(_settings.web_dir / "index.html")

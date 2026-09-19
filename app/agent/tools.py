"""Tool implementations and their declarations.

Two rules shape this file:

- The model may read and choose; it may not compute. Every duration, distance
  and ETA comes from `app/graph/router.py` or `app/agent/planner.py`.
- `send_email` is absent on purpose. Outward-facing actions only ever reach
  `pending_confirmations`; `/confirm/{id}` executes them (M5).

Arguments arrive from the model, so every handler validates them and returns a
readable error instead of raising.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable

from app.agent.planner import PlanningInputs, build_options, option_score
from app.agent.prompts import NOTICE_EXTRACTION_PROMPT, NOTICE_SCHEMA
from app.graph.loader import GraphDataError
from app.graph.router import NoRouteError, Router, conditions_from_signals
from app.agent.llm import LLM, ToolDeclaration
from app.models import (
    Commitment,
    FacilityStatus,
    Provenance,
    SignalKind,
    TravelOption,
)

MIN_ACTIONABLE_CONFIDENCE = 0.7


@dataclass
class ToolContext:
    """Everything a tool may touch. `state` is an AgentState (duck-typed to keep
    the import one-way)."""

    state: Any
    commitment: Commitment | None
    llm: LLM
    options: list[TravelOption] = field(default_factory=list)
    committed_option: TravelOption | None = None
    reminders: list[dict[str, Any]] = field(default_factory=list)

    @property
    def now(self) -> datetime:
        return self.state.clock.now()

    def planning_inputs(self) -> PlanningInputs:
        return PlanningInputs(
            graph=self.state.graph,
            facilities=self.state.facilities,
            profile=self.state.scenario.user.profile,
            origin=self.state.scenario.user.home_node,
            signals=self.state.providers.fetch_all(self.now),
            now=self.now,
        )


def _error(message: str, **extra: Any) -> dict[str, Any]:
    return {"ok": False, "error": message, **extra}


# --------------------------------------------------------------------------- #
# read-only tools
# --------------------------------------------------------------------------- #
def locate_room(ctx: ToolContext, room_id: str = "") -> dict[str, Any]:
    try:
        room = ctx.state.graph.room(room_id)
    except GraphDataError:
        return _error(f"沒有 {room_id} 這間教室", known_rooms=sorted(ctx.state.graph.rooms))
    statuses = ctx.state.facilities.status_many(room.elevators + room.entrances, ctx.now)
    return {
        "ok": True,
        "room": room.room,
        "building": room.building,
        "floor": room.floor,
        "arrival_node": room.node,
        "entrances": [
            {"id": e, "step_free": ctx.state.graph.node(e).step_free,
             "status": statuses[e].status.value}
            for e in room.entrances
        ],
        "elevators": [
            {"id": e, "status": statuses[e].status.value,
             "reason": statuses[e].reason, "source": statuses[e].source.value if statuses[e].source else None}
            for e in room.elevators
        ],
        "source": room.source.value,
        "updated_at": room.updated_at,
    }


def plan_route(ctx: ToolContext, from_node: str = "", room_id: str = "") -> dict[str, Any]:
    """Walking route only. The profile comes from state, never from the model."""
    inputs = ctx.planning_inputs()
    conditions = conditions_from_signals(inputs.signals)
    router = Router(inputs.graph, inputs.facilities, inputs.profile, conditions, ctx.now)
    try:
        route = router.route_to_room(from_node or inputs.origin, room_id)
    except GraphDataError as exc:
        return _error(str(exc))
    except NoRouteError as exc:
        return _error(exc.reason, origin=exc.origin, destination=exc.destination)
    return {
        "ok": True,
        "nodes": route.nodes,
        "distance_m": round(route.distance_m),
        "walking_minutes": round(route.seconds / 60, 1),
        "covered_ratio": round(route.covered_ratio, 2),
        "uses_facilities": route.uses_facilities,
        "notes": route.notes,
        "conditions": {"raining": conditions.raining, "flood_level": conditions.flood_level},
    }


def compare_travel_options(ctx: ToolContext) -> dict[str, Any]:
    """Score every eligible mode. This is the only place plans are ranked."""
    if ctx.commitment is None:
        return _error("今日沒有後續行程，沒有可比較的方案")
    inputs = ctx.planning_inputs()
    ctx.options = build_options(inputs, ctx.commitment)
    return {
        "ok": True,
        "latest_arrival": ctx.commitment.latest_arrival.isoformat(),
        "options": [
            {
                "id": option.id,
                "mode": option.mode.value,
                "feasible": option.feasible,
                "disqualified_reason": option.disqualified_reason,
                "depart_at": option.depart_at.strftime("%H:%M") if option.feasible else None,
                "conservative_eta": (
                    option.conservative_eta.strftime("%H:%M") if option.feasible else None
                ),
                "total_minutes": round(option.total_seconds / 60, 1),
                "score_minutes": round(option_score(option) / 60, 1),
                "risk_flags": option.risk_flags,
                "legs": [
                    {"mode": leg.mode.value, "minutes": round(leg.seconds / 60, 1),
                     "from": leg.from_node, "to": leg.to_node, "notes": leg.notes}
                    for leg in option.legs
                ],
            }
            for option in ctx.options
        ],
        "ranked_best": next(
            (o.id for o in sorted(
                [x for x in ctx.options if x.feasible], key=option_score
            )), None
        ),
    }


def get_facility_status(ctx: ToolContext, ids: list[str] | None = None) -> dict[str, Any]:
    if not ids:
        return _error("請提供要查詢的設施 id")
    states = ctx.state.facilities.status_many(list(ids), ctx.now)
    return {
        "ok": True,
        "as_of": ctx.now.isoformat(),
        "facilities": [
            {
                "id": target,
                "name": (
                    ctx.state.graph.nodes[target].name if target in ctx.state.graph.nodes else None
                ),
                "status": state.status.value,
                "reason": state.reason,
                "source": state.source.value if state.source else None,
                "source_ref": state.source_ref,
                "updated_at": state.updated_at.isoformat() if state.updated_at else None,
                "confidence": state.confidence,
                "actionable": state.confidence >= MIN_ACTIONABLE_CONFIDENCE,
            }
            for target, state in states.items()
        ],
    }


def get_rain_forecast(ctx: ToolContext) -> dict[str, Any]:
    signals = ctx.state.providers.fetch_all(ctx.now)
    out: dict[str, Any] = {"ok": True, "as_of": ctx.now.isoformat()}
    for kind in (SignalKind.rain, SignalKind.flood, SignalKind.air_quality):
        signal = signals.get(kind)
        if signal is None:
            out[kind.value] = {"available": False}
        elif signal.error:
            out[kind.value] = {"available": False, "error": signal.error}
        else:
            out[kind.value] = {
                "available": True,
                "value": signal.value,
                "observed_at": signal.observed_at.strftime("%H:%M"),
                "freshness": signal.freshness(ctx.now).value,
            }
    return out


# --------------------------------------------------------------------------- #
# notices: the model reads the mail, code resolves it to a node
# --------------------------------------------------------------------------- #
def resolve_facility_hint(graph, hint: str) -> tuple[str | None, str]:
    """Map a free-text facility name onto a node id. Deterministic on purpose:
    the model must not invent node codes."""
    if not hint:
        return None, "沒有設施描述"
    cleaned = hint.strip()
    if cleaned in graph.nodes or cleaned in graph.edges:
        # An id either exists in the graph or it does not, so accepting one is
        # safe; update_facility validates it again before writing.
        return cleaned, "id 直接相符"
    for node in graph.nodes.values():
        if node.name == cleaned:
            return node.id, "名稱完全相符"
    matches = [
        node for node in graph.nodes.values()
        if node.name and (node.name in cleaned or cleaned in node.name)
    ]
    if len(matches) == 1:
        return matches[0].id, f"以「{matches[0].name}」比對"
    if len(matches) > 1:
        return None, "比對到多個設施：" + "、".join(n.name for n in matches)
    return None, f"圖資中找不到「{cleaned}」"


def check_facility_notices(ctx: ToolContext, since: str = "") -> dict[str, Any]:
    """Read unread notices and extract which facility they affect.

    Gemini Flash does the reading; `resolve_facility_hint` does the mapping; the
    fixture's `expected_override` block is the fallback when the model is
    unavailable, as Stage 2 requires a manual path.
    """
    mailbox = ctx.state.providers.mailbox
    try:
        messages = mailbox.read_messages()
    except Exception as exc:  # noqa: BLE001
        return _error(f"讀不到信箱：{type(exc).__name__}")

    delivered = set(ctx.state.scenario.notices_until(ctx.now))
    results = []
    for message in messages:
        if message.get("id") not in delivered:
            continue
        body = f"主旨：{message.get('subject', '')}\n\n{message.get('body', '')}"
        extracted = ctx.llm.extract(NOTICE_EXTRACTION_PROMPT, body, NOTICE_SCHEMA)
        source = "gemini"
        if extracted is None:
            fallback = message.get("expected_override")
            source = "fixture_fallback"
            extracted = (
                {
                    "affects_facility": True,
                    "facility_hint": fallback["target_id"],
                    "status": fallback["status"],
                    "reason": fallback["reason"],
                    "valid_from": fallback["valid_from"],
                    "valid_to": fallback.get("valid_to"),
                    "confidence": 1.0,
                }
                if fallback
                else {"affects_facility": False, "confidence": 1.0}
            )

        entry: dict[str, Any] = {
            "mail_id": message.get("id"),
            "subject": message.get("subject"),
            "read_by": source,
            "affects_facility": bool(extracted.get("affects_facility")),
            "confidence": float(extracted.get("confidence", 0.0) or 0.0),
        }
        if entry["affects_facility"]:
            hint = str(extracted.get("facility_hint", ""))
            node_id, how = resolve_facility_hint(ctx.state.graph, hint)
            entry.update(
                {
                    "facility_hint": hint,
                    "resolved_target_id": node_id,
                    "resolution": how,
                    "status": extracted.get("status", "unknown"),
                    "reason": extracted.get("reason", ""),
                    "valid_from": extracted.get("valid_from"),
                    "valid_to": extracted.get("valid_to") or None,
                }
            )
        results.append(entry)
    return {"ok": True, "notices": results, "count": len(results)}


def update_facility(
    ctx: ToolContext,
    target_id: str = "",
    status: str = "",
    reason: str = "",
    source_ref: str = "",
    confidence: float = 1.0,
    valid_from: str = "",
    valid_to: str = "",
) -> dict[str, Any]:
    if target_id not in ctx.state.graph.nodes and target_id not in ctx.state.graph.edges:
        return _error(f"{target_id} 不是圖資中的節點或路段，不寫入覆寫")
    try:
        facility_status = FacilityStatus(status)
    except ValueError:
        return _error(f"status 必須是 {[s.value for s in FacilityStatus]}")

    def parse(value: str, default: datetime) -> datetime:
        if not value:
            return default
        try:
            parsed = datetime.fromisoformat(value)
        except ValueError:
            return default
        return parsed if parsed.tzinfo else parsed.replace(tzinfo=ctx.now.tzinfo)

    confidence = max(0.0, min(1.0, float(confidence)))
    override = ctx.state.facilities.add_override(
        target_id=target_id,
        status=facility_status,
        reason=reason or "未說明",
        source=Provenance.notice if source_ref.startswith("mail") else Provenance.user_report,
        source_ref=source_ref or None,
        confidence=confidence,
        valid_from=parse(valid_from, ctx.now),
        valid_to=parse(valid_to, None) if valid_to else None,
        recorded_at=ctx.now,
    )
    return {
        "ok": True,
        "target_id": override.target_id,
        "status": override.status.value,
        "confidence": override.confidence,
        "actionable": override.confidence >= MIN_ACTIONABLE_CONFIDENCE,
        "note": (
            "信心不足，規劃時只會標示不確定，不會改道"
            if override.confidence < MIN_ACTIONABLE_CONFIDENCE
            else "已納入路線規劃"
        ),
    }


def set_departure_reminder(ctx: ToolContext, at: str = "", message: str = "") -> dict[str, Any]:
    when = None
    if at:
        try:
            parsed = datetime.fromisoformat(at)
            when = parsed if parsed.tzinfo else parsed.replace(tzinfo=ctx.now.tzinfo)
        except ValueError:
            when = None
    if when is None and ctx.committed_option is not None:
        when = ctx.committed_option.depart_at
    if when is None:
        return _error("需要提醒時間，或先 commit_plan")
    reminder = ctx.state.providers.calendar.set_reminder(when, message or "出發提醒")
    ctx.reminders.append(reminder)
    return {"ok": True, "at": when.strftime("%H:%M"), "message": reminder["message"]}


def commit_plan(ctx: ToolContext, option_id: str = "", why: str = "") -> dict[str, Any]:
    """Select an option. Code validates feasibility — the model cannot override it."""
    if not ctx.options:
        return _error("請先呼叫 compare_travel_options")
    chosen = next((o for o in ctx.options if o.id == option_id), None)
    if chosen is None:
        return _error(
            f"沒有 {option_id} 這個方案", available=[o.id for o in ctx.options]
        )
    if not chosen.feasible:
        return _error(
            f"{option_id} 不可行：{chosen.disqualified_reason}。請改選可行方案",
            feasible=[o.id for o in ctx.options if o.feasible],
        )
    ctx.committed_option = chosen
    return {
        "ok": True,
        "committed": chosen.id,
        "mode": chosen.mode.value,
        "depart_at": chosen.depart_at.strftime("%H:%M"),
        "conservative_eta": chosen.conservative_eta.strftime("%H:%M"),
        "why": why,
    }


# --------------------------------------------------------------------------- #
# registry
# --------------------------------------------------------------------------- #
Handler = Callable[..., dict[str, Any]]

TOOLS: dict[str, tuple[ToolDeclaration, Handler]] = {
    "locate_room": (
        ToolDeclaration(
            name="locate_room",
            description="查教室在哪一棟哪一樓、可用入口與電梯及其目前狀態。",
            parameters={
                "type": "object",
                "properties": {"room_id": {"type": "string", "description": "教室代碼，例如 CSIE-4263"}},
                "required": ["room_id"],
            },
        ),
        locate_room,
    ),
    "plan_route": (
        ToolDeclaration(
            name="plan_route",
            description=(
                "計算校園內無障礙步行路線與所需時間。路線條件由系統帶入，你不需要也不能指定。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "from_node": {"type": "string", "description": "起點節點 id，省略則用使用者目前位置"},
                    "room_id": {"type": "string", "description": "目的教室代碼"},
                },
                "required": ["room_id"],
            },
        ),
        plan_route,
    ),
    "compare_travel_options": (
        ToolDeclaration(
            name="compare_travel_options",
            description=(
                "比較所有適用的交通方式，回傳每個方案的出發時間、抵達時間、"
                "是否可行與被排除的原因。要換方案前必須先呼叫這個。"
            ),
            parameters={"type": "object", "properties": {}},
        ),
        compare_travel_options,
    ),
    "get_facility_status": (
        ToolDeclaration(
            name="get_facility_status",
            description="查一組設施（電梯、斜坡、入口、路段）目前的狀態與來源。",
            parameters={
                "type": "object",
                "properties": {
                    "ids": {"type": "array", "items": {"type": "string"},
                            "description": "設施 id 清單"}
                },
                "required": ["ids"],
            },
        ),
        get_facility_status,
    ),
    "get_rain_forecast": (
        ToolDeclaration(
            name="get_rain_forecast",
            description="取得降雨、積水與空氣品質訊號，含觀測時間與新鮮度。",
            parameters={"type": "object", "properties": {}},
        ),
        get_rain_forecast,
    ),
    "check_facility_notices": (
        ToolDeclaration(
            name="check_facility_notices",
            description=(
                "讀取已收到的校內公告，判斷哪一封影響哪個設施。"
                "回傳的 resolved_target_id 若為 null 表示無法對應到圖資，不要自己編代碼。"
            ),
            parameters={
                "type": "object",
                "properties": {"since": {"type": "string", "description": "ISO 時間，可省略"}},
            },
        ),
        check_facility_notices,
    ),
    "update_facility": (
        ToolDeclaration(
            name="update_facility",
            description=(
                "寫入設施狀態覆寫。target_id 必須是圖資中存在的節點或路段 id。"
                "信心低於 0.7 時只會標示不確定，不會讓路線改道。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "target_id": {"type": "string"},
                    "status": {"type": "string", "description": "open / closed / degraded / unknown"},
                    "reason": {"type": "string"},
                    "source_ref": {"type": "string", "description": "來源，例如 mail_003"},
                    "confidence": {"type": "number"},
                    "valid_from": {"type": "string"},
                    "valid_to": {"type": "string"},
                },
                "required": ["target_id", "status", "reason"],
            },
        ),
        update_facility,
    ),
    "set_departure_reminder": (
        ToolDeclaration(
            name="set_departure_reminder",
            description="設定出發提醒。省略時間則用已選定方案的出發時間。",
            parameters={
                "type": "object",
                "properties": {
                    "at": {"type": "string", "description": "ISO 時間，可省略"},
                    "message": {"type": "string"},
                },
            },
        ),
        set_departure_reminder,
    ),
    "commit_plan": (
        ToolDeclaration(
            name="commit_plan",
            description=(
                "送出你選定的方案。必須先 compare_travel_options。"
                "選到不可行的方案會被程式駁回，請改選。"
            ),
            parameters={
                "type": "object",
                "properties": {
                    "option_id": {"type": "string"},
                    "why": {"type": "string", "description": "一句話說明為什麼選這個"},
                },
                "required": ["option_id", "why"],
            },
        ),
        commit_plan,
    ),
}


def declarations() -> list[ToolDeclaration]:
    return [declaration for declaration, _ in TOOLS.values()]


def call(ctx: ToolContext, name: str, args: dict[str, Any]) -> dict[str, Any]:
    entry = TOOLS.get(name)
    if entry is None:
        return _error(f"沒有 {name} 這個工具", available=sorted(TOOLS))
    _, handler = entry
    try:
        return handler(ctx, **args)
    except TypeError as exc:
        return _error(f"參數不正確：{exc}")
    except Exception as exc:  # noqa: BLE001 - a tool failure is data, not a crash
        return _error(f"{type(exc).__name__}: {exc}")

"""課表視覺化頁面的後端。

這層做的是「串接」：先問課表下一堂是什麼，再用教室代碼查大樓，最後組導航連結。
這其實就是 README 規劃中 Skill 層要做的事，等 SkillToolset 上線後可以搬過去；
在那之前先放這裡，Tool 層維持各自獨立、互不呼叫。

啟動：
    ./.venv/bin/python -m web.server        # http://localhost:8080
"""

from __future__ import annotations

from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

import uvicorn
from fastapi import FastAPI
from fastapi.responses import FileResponse, JSONResponse

from api import load_settings
from commute_agent.tools.class_schedule import PROJECT_ROOT, find_classes, load_courses
from commute_agent.tools.ncku_room import lookup_room
from commute_agent.skills.accessible_route import PROFILE_LABELS, plan_accessible_route
from commute_agent.skills.parking_plan import plan_parking
from commute_agent.skills.trip_plan import estimate_trip
from commute_agent.tools.route_link import TRAVEL_MODE_LABELS, build_route_link

WEB_DIR = Path(__file__).resolve().parent

app = FastAPI(title="NCKU Smart Commute")


def _resolve_building(entry: dict) -> dict:
    """用教室代碼查出真正的大樓，查不到就退回課表上的原始寫法。

    課表寫的大樓名稱不一定正確（例如「資訊系館」實際可能是 B501 或 B502），
    所以一律以 lookup_room 的 building_name 為準，並標記是否做過修正。
    """
    enriched = {**entry, "building_name": "", "floor": "", "lookup_status": "skipped",
                "corrected": False}
    query = entry.get("room_query")
    if not query:
        enriched["lookup_status"] = "no_room_code"
        return enriched

    result = lookup_room(query)
    enriched["lookup_status"] = result["status"]
    exact = [c for c in result.get("candidates", []) if c["exact_match"]]
    chosen = exact[0] if exact else (result.get("candidates") or [None])[0]
    if chosen:
        enriched["building_name"] = chosen["building_name"]
        enriched["floor"] = chosen["floor"]
        # 課表寫「資訊系館」但實際是「B501 資訊工程系館」，值得提醒使用者
        enriched["corrected"] = chosen["building_name"] not in entry["location"]
    return enriched


def _with_route(entry: dict | None, origin: str, travel_mode: str) -> dict | None:
    if entry is None:
        return None
    enriched = _resolve_building(entry)
    destination = enriched["building_name"] or enriched["location"]
    enriched["route_link"] = build_route_link(destination, origin=origin or None,
                                              travel_mode=travel_mode)
    enriched["origin"] = origin
    enriched["travel_mode"] = travel_mode
    return enriched


def _parking_for(entry: dict | None, vehicle_type: str) -> dict | None:
    """騎車或開車時才需要停車建議；目的地查不到大樓就不猜。"""
    if entry is None or not entry.get("building_name"):
        return None
    plan = plan_parking(entry["building_name"], vehicle_type)
    if plan.get("recommended"):
        plan["recommended"]["route_link"] = build_route_link(
            plan["recommended"]["name"], travel_mode="driving")
    return plan


def _accessible_route(entry: dict | None, profile: str, raining: bool) -> dict | None:
    """校內最後一段：哪個入口進得去、哪台電梯能用。

    Google Maps 只能帶到大樓門口。行動狀態選「一般」時不查——那段路對一般
    人沒有決策價值，查了只是多一個雜訊區塊。
    """
    if entry is None or profile == "default":
        return None
    building = entry.get("building_name") or entry.get("location") or ""
    if not building:
        return None
    return plan_accessible_route(
        destination_building=building,
        floor=entry.get("floor", ""),
        profile=profile,
        raining=raining,
    )


@app.get("/api/state")
def state(mode: str = "walking", vehicle: str = "機車",
          origin: str | None = None, profile: str = "default",
          raining: bool = False) -> JSONResponse:
    if mode not in TRAVEL_MODE_LABELS:
        return JSONResponse({"error": f"不支援的交通模式：{mode}"}, status_code=400)
    if profile not in PROFILE_LABELS:
        return JSONResponse({"error": f"不支援的行動狀態：{profile}"}, status_code=400)

    settings = load_settings()
    now = datetime.now(ZoneInfo(settings.timezone))
    path = Path(settings.class_schedule_path)
    if not path.is_absolute():
        path = PROJECT_ROOT / path

    if not path.is_file():
        return JSONResponse({"error": f"找不到課表檔案：{path}"}, status_code=404)

    # 使用者在頁面上填了出發地就用它，沒填才退回 .env 的預設地址
    start = (origin or "").strip() or settings.default_origin

    courses = load_courses(path)
    current, upcoming = find_classes(courses, now)
    next_class = _with_route(upcoming, start, mode)

    trip = None
    if next_class:
        destination = next_class["building_name"] or next_class["location"]
        trip = estimate_trip(start, destination, mode) if start else None

    return JSONResponse({
        "now": now.isoformat(timespec="seconds"),
        "origin": start,
        "default_origin": settings.default_origin,
        "using_default_origin": start == settings.default_origin,
        "travel_mode": mode,
        "travel_mode_label": TRAVEL_MODE_LABELS[mode],
        "vehicle_type": vehicle,
        "modes": TRAVEL_MODE_LABELS,
        "profile": profile,
        "profile_label": PROFILE_LABELS[profile],
        "profiles": PROFILE_LABELS,
        "raining": raining,
        "accessible_route": _accessible_route(next_class, profile, raining),
        "current_class": _with_route(current, start, mode),
        "next_class": next_class,
        "trip": trip,
        # 只有騎車開車才需要停車位，步行與大眾運輸不查，省掉七次連線
        "parking": _parking_for(next_class, vehicle) if mode == "driving" else None,
        "courses": courses,
    })


@app.get("/")
def index() -> FileResponse:
    return FileResponse(WEB_DIR / "index.html")


if __name__ == "__main__":
    uvicorn.run(app, host="127.0.0.1", port=8080)

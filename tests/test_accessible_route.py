"""校內無障礙路線。

每一條斷言都是一條規則，不是「程式目前跑出什麼」的快照。規則出自
CampusPulse 的 CLAUDE.md 5.3：各 profile 的速度、哪些路段禁行、
下雨怎麼加權、電梯每次 60 秒。
"""

from __future__ import annotations

import pytest

from commute_agent.skills.accessible_route import (
    ELEVATOR_SECONDS,
    PROFILE_SPEED_MPS,
    plan_accessible_route,
)
from commute_agent.tools.accessible_map import (
    FACILITY_STATUS,
    get_accessible_facilities,
    load_campus_map,
    report_facility_closed,
)

CSIE = "B501 資訊工程系館"


@pytest.fixture(autouse=True)
def clean_status():
    """設施停用狀態是模組層級的，每個測試都要從乾淨的開始。"""
    FACILITY_STATUS.reset()
    yield
    FACILITY_STATUS.reset()


def route(profile="default", floor="4F", **kwargs):
    return plan_accessible_route(CSIE, floor=floor, profile=profile, **kwargs)


def edge_ids(result):
    return [step["edge_id"] for step in result["steps"]]


# --- 硬安全規則 -------------------------------------------------------------
def test_一般使用者可以走階梯捷徑():
    result = route("default")
    campus = load_campus_map()
    assert any(campus.edges[e].stairs for e in edge_ids(result))


@pytest.mark.parametrize("profile", ["crutches", "wheelchair"])
def test_拐杖與輪椅永遠不走階梯(profile):
    result = route(profile)
    campus = load_campus_map()
    assert result["status"] == "ok"
    assert result["avoids_stairs"] is True
    for edge_id in edge_ids(result):
        assert campus.edges[edge_id].stairs is False, edge_id
    # 改走無障礙斜坡
    assert any("斜坡" in step["to"] for step in result["steps"])


def test_輪椅不走陡坡而一般使用者可以():
    campus = load_campus_map()
    steep = [e for e in campus.edges.values() if e.slope == "steep"]
    assert steep, "圖資裡應該要有陡坡才測得到這條規則"

    from commute_agent.skills.accessible_route import Conditions, _edge_seconds

    dry = Conditions()
    assert _edge_seconds(steep[0], "wheelchair", dry, set()) is None
    assert _edge_seconds(steep[0], "default", dry, set()) is not None


def test_下雨時拐杖也不走陡坡():
    from commute_agent.skills.accessible_route import Conditions, _edge_seconds

    campus = load_campus_map()
    steep = next(e for e in campus.edges.values() if e.slope == "steep")
    assert _edge_seconds(steep, "crutches", Conditions(), set()) is not None
    assert _edge_seconds(steep, "crutches", Conditions(raining=True), set()) is None


# --- 天氣 -------------------------------------------------------------------
def test_下雨會改走有頂蓋的路即使比較遠():
    dry = route("crutches")
    wet = route("crutches", raining=True)
    assert wet["covered_ratio"] > dry["covered_ratio"]
    assert wet["distance_m"] > dry["distance_m"]
    assert any("下雨" in note for note in wet["notes"])


def test_有頂蓋路段不受下雨影響():
    from commute_agent.skills.accessible_route import Conditions, _edge_seconds

    campus = load_campus_map()
    covered = next(e for e in campus.edges.values() if e.covered and not e.stairs)
    assert _edge_seconds(covered, "crutches", Conditions(raining=True), set()) == pytest.approx(
        _edge_seconds(covered, "crutches", Conditions(), set())
    )


# --- 設施停用 ---------------------------------------------------------------
def test_電梯停用時改走另一台並說明原因():
    before = route("crutches")
    assert before["uses_elevators"] == ["資訊系館西側電梯"]

    FACILITY_STATUS.close("CSIE_W_ELEV", "電梯年度保養")
    after = route("crutches")

    assert after["uses_elevators"] == ["資訊系館東側電梯"]
    assert after["minutes"] > before["minutes"]
    assert any("保養" in note for note in after["notes"])


def test_兩台電梯都停用就誠實說走不到():
    FACILITY_STATUS.close("CSIE_W_ELEV", "保養")
    FACILITY_STATUS.close("CSIE_E_ELEV", "維修")
    result = route("crutches")

    assert result["status"] == "no_route"
    assert "沒有可行路線" in result["reason"]
    assert "資訊系館西側電梯" in result["blocked_facilities"]


def test_低信心的回報只標示不確定不會改道():
    recorded = report_facility_closed("RAMP_07", "照片看起來被機車擋住", confidence=0.4)
    assert recorded["actionable"] is False

    result = route("crutches")
    assert result["status"] == "ok"
    assert any("斜坡" in step["to"] for step in result["steps"]), "低信心不該讓路線改道"
    assert any("未確認" in note for note in result["notes"])


def test_高信心的回報會讓路線改道():
    report_facility_closed("CSIE_W_ELEV", "現場確認停電", confidence=1.0)
    result = route("crutches")
    assert result["uses_elevators"] == ["資訊系館東側電梯"]


def test_回報不存在的設施不會寫進狀態():
    result = report_facility_closed("這個設施不存在", "亂填的")
    assert result["status"] == "unknown_facility"
    assert FACILITY_STATUS.active(__import__("datetime").datetime.now()) == []


# --- 算術 -------------------------------------------------------------------
def test_步行時間等於距離除以該profile的速度():
    from commute_agent.skills.accessible_route import Conditions, _edge_seconds

    campus = load_campus_map()
    edge = next(e for e in campus.edges.values()
                if not e.stairs and e.slope == "flat" and not e.covered and e.mode == "walk")
    for profile, speed in PROFILE_SPEED_MPS.items():
        assert _edge_seconds(edge, profile, Conditions(), set()) == pytest.approx(
            edge.length_m / speed
        )


def test_每次搭電梯固定加六十秒():
    result = route("crutches")
    steps = result["steps"]
    elevator_step = next(s for s in steps if any("電梯" in m for m in s["marks"]))
    campus = load_campus_map()
    walking = campus.edges[elevator_step["edge_id"]].length_m / PROFILE_SPEED_MPS["crutches"]
    assert elevator_step["minutes"] == pytest.approx((walking + ELEVATOR_SECONDS) / 60, abs=0.05)


def test_速度越慢的profile花的時間越多():
    quick = route("default")["minutes"]
    chair = route("wheelchair")["minutes"]
    crutch = route("crutches")["minutes"]
    assert quick < chair < crutch


def test_同樣的輸入給同樣的結果():
    assert route("crutches") == route("crutches")


# --- 誠實回報 ---------------------------------------------------------------
def test_沒有圖資的大樓明確回報而不是猜():
    result = plan_accessible_route("雲平大樓", floor="3F", profile="wheelchair")
    assert result["status"] == "no_map"
    assert result["covered_buildings"]
    assert "資訊工程系館" in result["covered_buildings"]


def test_大樓名稱用關鍵詞比對成大GIS的寫法():
    """lookup_room 回的是「B501 資訊工程系館」，課表可能只寫「資訊系館」。"""
    for name in ("B501 資訊工程系館", "資訊系館", "資訊工程學系"):
        assert plan_accessible_route(name, floor="4F", profile="crutches")["status"] == "ok"


def test_圖資沒畫的樓層改走到電梯再搭上去():
    """圖資不必每層都畫節點——電梯自己知道到得了哪幾層。"""
    result = route("crutches", floor="2F")
    assert result["status"] == "ok"
    assert result["floor"] == "2F"
    assert result["uses_elevators"], "應該要搭電梯上去"
    assert any("2F" in note and "電梯" in note for note in result["notes"])
    assert any("搭電梯至 2F" in "".join(step["marks"]) for step in result["steps"])


def test_電梯到不了的樓層要說路線只到入口而且不標樓層():
    """不能標著 99F 卻把人放在門口。"""
    result = route("crutches", floor="99F")
    assert result["status"] == "ok"
    assert result["floor"] == "", "到不了就不要宣稱到得了"
    assert result["uses_elevators"] == []
    assert any("只到大樓入口" in note for note in result["notes"])


def test_沒指定樓層就走到入口():
    result = route("crutches", floor="")
    assert result["status"] == "ok"
    assert result["floor"] == ""


def test_路線每一步都說得出理由():
    result = route("crutches", raining=True)
    for step in result["steps"]:
        assert step["from"] and step["to"]
        assert step["metres"] >= 0 and step["minutes"] >= 0
    assert result["notes"], "至少要說明下雨與使用了哪台電梯"


def test_設施查詢回傳來源與更新時間():
    facilities = get_accessible_facilities(CSIE)
    assert facilities["status"] == "ok"
    assert len(facilities["elevators"]) == 2
    for item in facilities["entrances"] + facilities["elevators"]:
        assert item["source"], "每個設施都要說得出資料來源"
        assert item["updated_at"]


def test_設施查詢對沒有圖資的大樓也誠實():
    assert get_accessible_facilities("雲平大樓")["status"] == "no_map"

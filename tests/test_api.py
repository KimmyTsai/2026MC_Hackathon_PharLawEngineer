from __future__ import annotations

import re
from contextlib import contextmanager


def test_health_reports_mode_and_graph_size(client):
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["mode"] == "replay"
    assert body["scenario"] == "demo_wed"
    assert body["sim_time"].startswith("2026-09-23T07:45")
    assert body["graph"]["nodes"] > 10
    assert body["graph"]["draft"] is True


def test_provider_badges_never_claim_live_in_replay(client):
    modes = client.get("/health").json()["provider_modes"]
    assert set(modes) >= {"rain", "flood", "bike_availability", "bus_eta", "mailbox"}
    assert "live" not in set(modes.values())


def test_state_exposes_commitments_and_signal_freshness(client):
    body = client.get("/state").json()
    assert body["profile"] == "crutches"
    assert body["next_commitment"]["room"] == "CSIE-4263"
    assert body["next_commitment"]["importance"] == "presentation"
    assert body["signals"]["rain"]["freshness"] in {"fixture", "stale"}
    assert body["plans"] == {}  # reading state never plans as a side effect


def test_graph_endpoint_carries_overrides_and_alias(client):
    body = client.get("/graph").json()
    assert body["blocked_ids"] == []
    assert "from" in body["edges"][0]
    assert any(r["room"] == "CSIE-4263" for r in body["rooms"])


def test_advance_moves_sim_time_and_refuses_rewind(client):
    client.post("/replay/reset")
    after = client.post("/replay/advance", json={"seconds": 1800}).json()
    assert after["now"].startswith("2026-09-23T08:15")

    rewind = client.post("/replay/advance", json={"to": "2026-09-23T07:50:00+08:00"})
    assert rewind.status_code == 400

    assert client.post("/replay/advance", json={}).status_code == 400


def test_advanced_clock_changes_what_the_agent_perceives(client):
    client.post("/replay/reset")
    before = client.get("/state").json()["signals"]["bike_availability"]["value"]["bikes"]
    client.post("/replay/advance", json={"to": "2026-09-23T08:20:00+08:00"})
    after = client.get("/state").json()["signals"]["bike_availability"]["value"]["bikes"]
    assert before == 7
    assert after == 0


def test_reset_returns_to_the_scenario_start(client):
    client.post("/replay/advance", json={"seconds": 3600})
    body = client.post("/replay/reset").json()
    assert body["now"].startswith("2026-09-23T07:45")
    assert client.get("/state").json()["agent_log"] == []


def test_inject_appends_an_event_and_logs_it(client):
    client.post("/replay/reset")
    body = client.post("/replay/inject", json={"type": "report_submitted", "payload": {"node_hint": "RAMP_07"}}).json()
    assert body["injected"] == "report_submitted"
    log = client.get("/state").json()["agent_log"]
    assert any("report_submitted" in entry["summary"] for entry in log)


def test_unbuilt_endpoints_answer_501_with_their_milestone(client):
    for path in ("/schedule", "/report"):
        response = client.post(path)
        assert response.status_code == 501
        assert "M" in response.json()["detail"]


def test_confirming_an_unknown_action_is_refused(client):
    response = client.post("/confirm/act_nonexistent", json={"approve": True})
    assert response.status_code == 409
    assert "沒有" in response.json()["detail"]


def test_frontend_shell_is_served(client):
    body = client.get("/").text
    assert "CampusPulse" in body
    assert client.get("/static/app.js").status_code == 200


# --------------------------------------------------------------------------- #
# M1: planning over HTTP
# --------------------------------------------------------------------------- #
def test_plan_endpoint_returns_a_plan_and_its_decision(client):
    client.post("/replay/reset")
    body = client.post("/plan").json()
    plan, decision = body["plan"], body["decision"]
    assert plan["selected"]["mode"] in {"walk", "bus", "bus_lowfloor"}
    assert plan["status"] == "active"
    assert plan["selected"]["depart_at"] < plan["selected"]["conservative_eta"]
    assert decision["selected_option"] == plan["selected"]["id"]
    assert decision["rejected"] and all(r["reason"] for r in decision["rejected"])
    assert decision["model_id"] is None


def test_starting_a_replay_produces_the_initial_plan(client):
    body = client.post("/replay/start", json={}).json()
    assert body["now"].startswith("2026-09-23T07:45")
    state = client.get("/state").json()
    assert state["plans"], "starting the replay should leave a plan on the state"


def test_the_notice_closes_the_west_elevator_and_reroutes(client):
    client.post("/replay/start", json={})
    before = client.post("/replay/advance", json={"to": "2026-09-23T08:00:00+08:00"}).json()["plan"]
    after = client.post("/replay/advance", json={"to": "2026-09-23T08:06:00+08:00"}).json()["plan"]

    assert client.get("/graph").json()["blocked_ids"] == ["CSIE_W_ELEV"]
    used_before = [f for leg in before["selected"]["legs"] for f in leg["uses_facilities"]]
    used_after = [f for leg in after["selected"]["legs"] for f in leg["uses_facilities"]]
    assert used_before == ["CSIE_W_ELEV"]
    assert used_after == ["CSIE_E_ELEV"]
    assert after["selected"]["depart_at"] < before["selected"]["depart_at"]


def test_the_weather_and_bike_events_move_the_departure_earlier(client):
    client.post("/replay/start", json={})
    before = client.post("/replay/advance", json={"to": "2026-09-23T08:10:00+08:00"}).json()["plan"]
    after = client.post("/replay/advance", json={"to": "2026-09-23T08:17:00+08:00"}).json()["plan"]
    assert after["selected"]["depart_at"] < before["selected"]["depart_at"]

    walk = next(o for o in after["alternatives"] if o["mode"] == "walk")
    assert walk["feasible"] is False
    assert "積水" in walk["disqualified_reason"]


def test_missing_the_departure_window_reports_no_feasible_plan(client):
    client.post("/replay/start", json={})
    body = client.post("/replay/advance", json={"to": "2026-09-23T08:40:00+08:00"}).json()
    assert body["plan"]["status"] == "infeasible"
    decision = client.get("/state").json()["latest_decision"]
    assert decision["selected_option"] is None
    assert "沒有任何可行方案" in decision["rationale"]


def test_an_unrelated_notice_changes_nothing(client):
    client.post("/replay/start", json={})
    client.post("/replay/inject", json={"type": "notice_received", "payload": {"mail_id": "mail_001"}})
    client.post("/replay/advance", json={"seconds": 60})
    assert client.get("/graph").json()["blocked_ids"] == []
    log = client.get("/state").json()["agent_log"]
    assert any("與設施無關" in entry["summary"] for entry in log)


def test_reset_replays_the_same_transition(client):
    def run() -> tuple[str, str]:
        client.post("/replay/start", json={})
        first = client.post("/replay/advance", json={"to": "2026-09-23T08:00:00+08:00"}).json()
        second = client.post("/replay/advance", json={"to": "2026-09-23T08:20:00+08:00"}).json()
        return first["plan"]["selected"]["depart_at"], second["plan"]["selected"]["depart_at"]

    assert run() == run()


def test_an_event_injected_at_the_current_instant_is_perceived(client):
    """Regression: the scheduled window is (processed, until], so an event
    stamped 'now' used to be skipped forever — the on-stage photo report path."""
    client.post("/replay/start", json={})
    client.post("/replay/advance", json={"to": "2026-09-23T08:10:00+08:00"})
    before = len(client.get("/state").json()["triggers"])

    body = client.post(
        "/replay/inject",
        json={"type": "notice_received", "payload": {"mail_id": "mail_002"}},
    ).json()
    assert body["perceived"] is True

    state = client.get("/state").json()
    assert len(state["triggers"]) == before + 1
    assert "e_009" in client.get("/graph").json()["blocked_ids"]


def test_an_event_injected_in_the_future_waits_for_the_clock(client):
    client.post("/replay/start", json={})
    body = client.post(
        "/replay/inject",
        json={"type": "notice_received", "payload": {"mail_id": "mail_002"},
              "at": "2026-09-23T08:30:00+08:00"},
    ).json()
    assert body["perceived"] is False
    assert client.get("/graph").json()["blocked_ids"] == []

    client.post("/replay/advance", json={"to": "2026-09-23T08:31:00+08:00"})
    assert "e_009" in client.get("/graph").json()["blocked_ids"]


# --------------------------------------------------------------------------- #
# M2: the agent over HTTP (with the model stubbed out — see conftest)
# --------------------------------------------------------------------------- #
def test_health_says_whether_the_agent_can_reach_a_model(client):
    agent = client.get("/health").json()["agent"]
    assert agent["available"] is False  # conftest forbids network calls
    assert "測試環境" in agent["reason"]
    assert agent["replaying"] is False
    assert client.get("/state").json()["agent"] == agent


def test_plan_reports_how_the_agent_ran(client):
    client.post("/replay/reset")
    body = client.post("/plan").json()
    assert body["agent"]["fell_back"] is True
    assert body["agent"]["tool_calls"] == []
    assert body["plan"] is not None  # no model, still a plan


def test_next_jumps_to_the_next_event(client):
    client.post("/replay/start", json={})
    first = client.post("/replay/next").json()
    assert first["now"].startswith("2026-09-23T07:50")
    assert first["event"]["type"] == "day_start"

    second = client.post("/replay/next").json()
    assert second["now"].startswith("2026-09-23T08:05")
    assert second["event"]["type"] == "notice_received"
    assert client.get("/graph").json()["blocked_ids"] == ["CSIE_W_ELEV"]


def test_next_walks_the_whole_scenario_then_stops(client):
    client.post("/replay/start", json={})
    seen = []
    for _ in range(20):
        body = client.post("/replay/next").json()
        if body["done"]:
            break
        seen.append(body["event"]["type"])
    assert seen[0] == "day_start"
    assert "flood_warning" in seen
    assert seen[-1] == "user_not_departed"
    assert client.post("/replay/next").json()["done"] is True


def test_the_agent_log_records_which_path_decided(client):
    client.post("/replay/start", json={})
    client.post("/replay/next")
    log = client.get("/state").json()["agent_log"]
    assert any(entry["model_id"] is None for entry in log)  # deterministic path
    assert all(entry["phase"] in {"perceive", "plan", "act", "reflect"} for entry in log)


# --------------------------------------------------------------------------- #
# M3: what the map and timeline need from the API
# --------------------------------------------------------------------------- #
def test_state_carries_the_scenario_timeline(client):
    client.post("/replay/start", json={})
    events = client.get("/state").json()["scenario_events"]
    assert len(events) >= 8
    assert events == sorted(events, key=lambda e: e["at"])
    assert all(e["expect"] for e in events)
    assert events[0]["type"] == "day_start"

    client.post("/replay/next")
    after = client.get("/state").json()["scenario_events"]
    assert after[0]["processed"] is True
    assert after[-1]["processed"] is False


def test_every_graph_node_can_be_drawn(client):
    graph = client.get("/graph").json()
    for node in graph["nodes"]:
        assert -90 <= node["lat"] <= 90 and -180 <= node["lng"] <= 180
        assert node["name"] and node["type"]
    ids = {n["id"] for n in graph["nodes"]}
    for edge in graph["edges"]:
        assert edge["from"] in ids and edge["to"] in ids


def test_route_legs_reference_drawable_nodes(client):
    client.post("/replay/start", json={})
    client.post("/replay/next")
    state = client.get("/state").json()
    ids = {n["id"] for n in client.get("/graph").json()["nodes"]}
    plan = state["plans"][state["next_commitment"]["id"]]
    for leg in plan["selected"]["legs"]:
        assert len(leg["nodes"]) >= 2
        assert all(node in ids for node in leg["nodes"])
        assert all(f in ids for f in leg["uses_facilities"])


def test_the_frontend_assets_are_served_including_vendored_leaflet(client):
    for path in ("/", "/static/app.js", "/static/map.js", "/static/style.css",
                 "/static/vendor/leaflet.js", "/static/vendor/leaflet.css"):
        assert client.get(path).status_code == 200, path
    page = client.get("/").text
    assert "/static/vendor/leaflet.js" in page
    # Nothing the page needs to render may come from the network: venue wifi.
    assert not re.search(r'(?:src|href)="https?://', page)


def test_the_report_button_path_marks_a_point_for_the_map(client):
    """現場回報 injects an event; a low-confidence report must show as
    unconfirmed rather than silently rerouting."""
    client.post("/replay/start", json={})
    client.post("/replay/next")
    body = client.post(
        "/replay/inject",
        json={"type": "report_submitted",
              "payload": {"node_hint": "RAMP_07", "confidence_hint": "low"}},
    ).json()
    assert body["perceived"] is True
    graph = client.get("/graph").json()
    assert "RAMP_07" not in graph["blocked_ids"]


def test_health_warns_about_a_maps_key_in_the_gemini_key_list(client, settings):
    """A standard AIza key cannot call the Gemini API whatever its restrictions
    say, so it must be caught here rather than at request time."""
    body = client.get("/health").json()
    assert body["warnings"] == []  # the repo's own keys are the right type

    from app.config import Settings

    mixed = Settings(gemini_api_keys=["AQ.Ab8_real", "AIzaSyStandardKey"])
    warnings = mixed.gemini_key_warnings()
    assert len(warnings) == 1
    assert "MAPS_API_KEY.txt" in warnings[0]


# --------------------------------------------------------------------------- #
# M5: the confirmation boundary over HTTP
# --------------------------------------------------------------------------- #
def walk_to_the_draft(client):
    client.post("/replay/start", json={})
    for _ in range(12):
        if client.post("/replay/next").json().get("done"):
            break
    pending = client.get("/state").json()["pending_confirmations"]
    assert pending, "走完情境後應該要有一封待確認的遲到通知"
    return pending[0]


def test_the_scenario_ends_with_one_unsent_draft(client):
    action = walk_to_the_draft(client)
    assert action["type"] == "send_email"
    assert action["state"] == "awaiting_confirmation"
    assert action["requires_authorization"] is True
    assert client.get("/outbox").json()["count"] == 0


def test_the_draft_names_the_class_and_how_late(client):
    action = walk_to_the_draft(client)
    assert "計算機組織" in action["preview"]["subject"]
    assert "CSIE-4263" in action["preview"]["body"]
    assert action["preview"]["minutes_late"] > 0


def test_nothing_is_sent_until_confirm_is_called(client):
    walk_to_the_draft(client)
    # Every other endpoint has now run; the outbox must still be empty.
    assert client.get("/outbox").json()["count"] == 0
    log = client.get("/state").json()["agent_log"]
    assert any("等待你確認" in e["summary"] for e in log)
    assert not any(e["tool"] == "send_email" for e in log)


def test_confirming_sends_once_and_only_once(client):
    action = walk_to_the_draft(client)
    first = client.post(f"/confirm/{action['id']}", json={"approve": True}).json()
    assert first["result"]["sent"] is True
    assert first["result"]["duplicate"] is False
    assert first["action"]["state"] == "executed"
    assert client.get("/outbox").json()["count"] == 1

    second = client.post(f"/confirm/{action['id']}", json={"approve": True}).json()
    assert second["result"]["duplicate"] is True
    assert client.get("/outbox").json()["count"] == 1


def test_cancelling_sends_nothing(client):
    action = walk_to_the_draft(client)
    body = client.post(f"/confirm/{action['id']}", json={"approve": False}).json()
    assert body["action"]["state"] == "rejected"
    assert body["result"]["sent"] is False
    assert client.get("/outbox").json()["count"] == 0

    retry = client.post(f"/confirm/{action['id']}", json={"approve": True})
    assert retry.status_code == 409


def test_an_edited_draft_is_what_reaches_the_outbox(client):
    action = walk_to_the_draft(client)
    client.post(f"/confirm/{action['id']}", json={"approve": True, "body": "我會晚到，抱歉。"})
    messages = client.get("/outbox").json()["messages"]
    assert len(messages) == 1
    assert messages[0]["body"] == "我會晚到，抱歉。"


def test_the_confirmation_dialog_is_in_the_page(client):
    page = client.get("/").text
    assert 'id="confirm-dialog"' in page
    for label in ("寄出", "修改", "取消"):
        assert label in page


# --------------------------------------------------------------------------- #
# basemap: Google tiles are proxied so the key never reaches the browser
# --------------------------------------------------------------------------- #
def test_map_config_defaults_to_openstreetmap(client):
    config = client.get("/map/config").json()
    assert config["provider"] == "osm"
    assert config["tile_url"].startswith("https://tile.openstreetmap.org/")


def test_map_config_never_leaks_the_api_key(client, settings):
    body = client.get("/map/config").text + client.get("/").text
    assert "key=" not in body
    if settings.maps_api_key:
        assert settings.maps_api_key not in body


def test_the_tile_proxy_is_off_unless_google_is_selected(client):
    response = client.get("/map/tiles/16/54737/28468.png")
    assert response.status_code == 503
    assert "未啟用" in response.json()["detail"]


class _StubProxy:
    """Stands in for GoogleTileProxy so no test touches Google."""

    def __init__(self, error: Exception | None = None) -> None:
        self.error = error

    def attribution(self, *_args):
        return "地圖資料 ©Google"

    def tile(self, z, x, y):
        if self.error:
            raise self.error
        return bytes([0x89]) + b"PNG stub"


@contextmanager
def google_basemap(client, proxy):
    """Swap the app into Google-tile mode for one test, then put it back."""
    app = client.app
    old_settings, old_tiles = app.state.settings, getattr(app.state, "tiles", None)
    app.state.settings = old_settings.model_copy(update={"map_provider": "google"})
    app.state.tiles = proxy
    try:
        yield
    finally:
        app.state.settings = old_settings
        app.state.tiles = old_tiles


def test_google_config_hides_the_key_and_offers_a_fallback(client):
    """With the proxy installed the page gets a relative tile URL, never a
    Google URL with a key in it."""
    with google_basemap(client, _StubProxy()):
        config = client.get("/map/config").json()
        assert config["provider"] == "google"
        assert config["tile_url"] == "/map/tiles/{z}/{x}/{y}.png"
        assert "Google" in config["attribution"]
        assert config["fallback"]["provider"] == "osm"
        assert "key" not in config["tile_url"]

        tile = client.get("/map/tiles/16/54737/28468.png")
        assert tile.status_code == 200
        assert tile.headers["content-type"] == "image/png"


def test_a_tile_failure_reports_502_so_the_page_can_fall_back(client):
    from app.sources.maps import MapTilesError

    with google_basemap(client, _StubProxy(MapTilesError("createSession 403"))):
        response = client.get("/map/tiles/16/54737/28468.png")
        assert response.status_code == 502
        assert "403" in response.json()["detail"]

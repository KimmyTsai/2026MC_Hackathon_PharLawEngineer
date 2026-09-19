from __future__ import annotations


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
    for path in ("/schedule", "/report", "/confirm/abc"):
        response = client.post(path)
        assert response.status_code == 501
        assert "M" in response.json()["detail"]


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

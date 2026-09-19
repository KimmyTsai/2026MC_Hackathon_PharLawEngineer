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
    assert body["plans"] == {}  # planner lands in M1


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

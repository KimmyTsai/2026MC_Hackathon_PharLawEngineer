from __future__ import annotations

import pytest

from app.config import get_settings
from app.graph.loader import load_campus_graph
from app.main import build_state
from app.sources.scenario import load_scenario


@pytest.fixture(autouse=True)
def isolated_outbox(tmp_path, monkeypatch):
    """No test may write into the repo's data/outbox.

    Sending is idempotent per commitment per simulated day, so tests sharing one
    directory also see each other's messages.
    """
    monkeypatch.setenv("OUTBOX_PATH", str(tmp_path / "outbox"))
    get_settings.cache_clear()
    yield
    get_settings.cache_clear()


@pytest.fixture(autouse=True)
def never_call_the_model(monkeypatch):
    """No test may reach the Gemini API.

    A key in .env would otherwise make every /replay/advance a paid network
    call. Tests that exercise the agent loop pass their own scripted LLM into
    AgentState instead (see tests/test_orchestrator.py).
    """
    from app.agent.llm import NullLLM

    monkeypatch.setattr(
        "app.agent.state.build_llm", lambda settings: NullLLM("測試環境不呼叫模型")
    )


@pytest.fixture
def settings():
    return get_settings()


@pytest.fixture
def scenario(settings):
    return load_scenario(settings.scenario_path)


@pytest.fixture
def graph(settings):
    return load_campus_graph(settings.graph_path, settings.rooms_path)


@pytest.fixture
def state(settings):
    return build_state(settings)


@pytest.fixture
def client():
    from fastapi.testclient import TestClient

    from app.main import app

    with TestClient(app) as test_client:
        yield test_client

from __future__ import annotations

import pytest

from app.config import get_settings
from app.graph.loader import load_campus_graph
from app.main import build_state
from app.sources.scenario import load_scenario


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

from __future__ import annotations

import pytest

from app.graph.loader import CampusGraph, GraphDataError
from app.models import GraphEdge, GraphNode, NodeType, RoomRecord


def test_graph_loads_with_expected_shape(graph):
    assert graph.draft is True
    assert "CSIE_W_ELEV" in graph.nodes
    assert graph.node("CSIE_W_ELEV").floors == [1, 2, 3, 4, 5]
    assert graph.node("CSIE_S_ENT").step_free is False


def test_parallel_edges_are_both_kept(graph):
    """The stairs shortcut and the covered corridor are different choices."""
    keys = set(graph.nx[("JCT_02")]["JCT_03"].keys())
    assert {"e_009", "e_018"} <= keys
    assert graph.edge("e_009").stairs is True
    assert graph.edge("e_018").covered is True and graph.edge("e_018").stairs is False


def test_room_lookup_points_at_a_real_node(graph):
    room = graph.room("CSIE-4263")
    assert room.floor == 4
    assert room.node in graph.nodes
    assert set(room.elevators) == {"CSIE_W_ELEV", "CSIE_E_ELEV"}


def test_elevators_in_building(graph):
    ids = {n.id for n in graph.elevators_in("CSIE")}
    assert ids == {"CSIE_W_ELEV", "CSIE_E_ELEV"}


def test_unknown_lookups_raise(graph):
    with pytest.raises(GraphDataError):
        graph.node("NOPE")
    with pytest.raises(GraphDataError):
        graph.room("NOPE-0000")


def test_dangling_edge_is_rejected():
    node = GraphNode(id="A", name="a", type=NodeType.junction, lat=0, lng=0, updated_at="2026-09-19")
    edge = GraphEdge.model_validate(
        {"id": "e", "from": "A", "to": "MISSING", "length_m": 1, "updated_at": "2026-09-19"}
    )
    with pytest.raises(GraphDataError, match="unknown nodes"):
        CampusGraph([node], [edge], [])


def test_room_pointing_at_missing_node_is_rejected():
    node = GraphNode(id="A", name="a", type=NodeType.junction, lat=0, lng=0, updated_at="2026-09-19")
    room = RoomRecord(
        room="X-1", building="X", floor=1, node="GHOST", entrances=["A"], updated_at="2026-09-19"
    )
    with pytest.raises(GraphDataError, match="unknown nodes"):
        CampusGraph([node], [], [room])


def test_to_dict_uses_the_json_edge_alias(graph):
    payload = graph.to_dict()
    assert "from" in payload["edges"][0]
    assert "from_" not in payload["edges"][0]

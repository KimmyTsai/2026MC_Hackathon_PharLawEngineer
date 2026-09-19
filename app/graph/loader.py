"""Load the accessible-campus graph into networkx.

A MultiGraph, because two nodes can be joined by genuinely different paths
(a short stairway and a longer covered corridor) and the planner must be able
to choose between them.
"""

from __future__ import annotations

import json
from pathlib import Path

import networkx as nx

from app.models import CampusGraphFile, GraphEdge, GraphNode, RoomRecord


class GraphDataError(ValueError):
    pass


class CampusGraph:
    def __init__(
        self,
        nodes: list[GraphNode],
        edges: list[GraphEdge],
        rooms: list[RoomRecord],
        draft: bool = True,
    ) -> None:
        self.draft = draft
        self.nodes: dict[str, GraphNode] = {n.id: n for n in nodes}
        self.edges: dict[str, GraphEdge] = {e.id: e for e in edges}
        self.rooms: dict[str, RoomRecord] = {r.room: r for r in rooms}

        missing = [
            (e.id, endpoint)
            for e in edges
            for endpoint in (e.from_, e.to)
            if endpoint not in self.nodes
        ]
        if missing:
            raise GraphDataError(f"edges reference unknown nodes: {missing}")

        bad_rooms = [
            (r.room, r.node) for r in rooms if r.node not in self.nodes
        ] + [
            (r.room, entrance)
            for r in rooms
            for entrance in r.entrances
            if entrance not in self.nodes
        ]
        if bad_rooms:
            raise GraphDataError(f"rooms reference unknown nodes: {bad_rooms}")

        self.nx = nx.MultiGraph()
        for node in nodes:
            self.nx.add_node(node.id, data=node)
        for edge in edges:
            self.nx.add_edge(edge.from_, edge.to, key=edge.id, data=edge)

    # -- lookups -------------------------------------------------------------
    def node(self, node_id: str) -> GraphNode:
        try:
            return self.nodes[node_id]
        except KeyError as exc:
            raise GraphDataError(f"unknown node {node_id!r}") from exc

    def edge(self, edge_id: str) -> GraphEdge:
        try:
            return self.edges[edge_id]
        except KeyError as exc:
            raise GraphDataError(f"unknown edge {edge_id!r}") from exc

    def room(self, room_id: str) -> RoomRecord:
        try:
            return self.rooms[room_id]
        except KeyError as exc:
            raise GraphDataError(f"unknown room {room_id!r}") from exc

    def elevators_in(self, building: str) -> list[GraphNode]:
        return [
            n
            for n in self.nodes.values()
            if n.building == building and n.type.value == "elevator"
        ]

    def to_dict(self) -> dict:
        """Shape the frontend consumes for the map."""
        return {
            "draft": self.draft,
            "nodes": [n.model_dump(mode="json") for n in self.nodes.values()],
            "edges": [e.model_dump(mode="json", by_alias=True) for e in self.edges.values()],
            "rooms": [r.model_dump(mode="json") for r in self.rooms.values()],
        }


def load_campus_graph(graph_path: Path, rooms_path: Path) -> CampusGraph:
    graph_raw = json.loads(graph_path.read_text(encoding="utf-8"))
    parsed = CampusGraphFile.model_validate(graph_raw)

    rooms_raw = json.loads(rooms_path.read_text(encoding="utf-8"))
    rooms = [RoomRecord.model_validate(r) for r in rooms_raw.get("rooms", [])]

    return CampusGraph(parsed.nodes, parsed.edges, rooms, draft=parsed.draft)

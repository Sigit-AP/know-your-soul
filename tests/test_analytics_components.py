"""Tests for ``analytics.components`` (ATM wave A, plan F-A2).

Pins the connectivity contract: weak/strong components, articulation points,
bridges, k-core numbers and the "knowledge island" review candidates — all
pure functions of the read model with canonical (sorted) iteration order.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.components import analyze_components

_IDS = ["a", "b", "c", "d", "e", "f"]
_DOMAINS = [f"P{i}" for i in range(1, 8)]
_RELATIONS = ["depends_on", "supports", "contradicts", "parent_of", "supersedes"]


def _node(node_id: str, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain, "kind": "leaf"}


def _link(source: str, target: str, relation: str = "depends_on") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links), "hyperedges": []}


# ------------------------------------------------------------------ examples


def test_empty_graph() -> None:
    result = analyze_components(_graph([], []))
    assert result["kind"] == "analytics.components"
    assert result["weak_components"] == []
    assert result["strong_components"] == []
    assert result["articulation_points"] == []
    assert result["bridges"] == []
    assert result["k_core"] == {"max_core": 0, "nodes": []}
    assert result["knowledge_islands"] == []


def test_path_components_articulation_and_bridges() -> None:
    result = analyze_components(_graph([_node("a"), _node("b"), _node("c")], [_link("a", "b"), _link("b", "c")]))
    assert result["weak_components"] == [{"component_id": 0, "size": 3, "nodes": ["a", "b", "c"]}]
    # No directed cycle: every strong component is a singleton.
    assert all(not comp["cyclic"] for comp in result["strong_components"])
    assert len(result["strong_components"]) == 3
    assert result["articulation_points"] == ["b"]
    assert result["bridges"] == [{"source": "a", "target": "b"}, {"source": "b", "target": "c"}]
    assert result["k_core"]["max_core"] == 1


def test_directed_cycle_is_one_cyclic_strong_component() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("d")],
        [_link("a", "b"), _link("b", "c"), _link("c", "a"), _link("c", "d")],
    )
    result = analyze_components(graph)
    cyclic = [comp for comp in result["strong_components"] if comp["cyclic"]]
    assert cyclic == [{"component_id": 0, "cyclic": True, "nodes": ["a", "b", "c"], "size": 3}]
    # 'd' is reachable from the cycle but cannot reach back.
    singletons = [comp for comp in result["strong_components"] if not comp["cyclic"]]
    assert singletons == [{"component_id": 1, "cyclic": False, "nodes": ["d"], "size": 1}]
    # Removing 'c' disconnects 'd' from the triangle (undirected view).
    assert result["articulation_points"] == ["c"]
    assert result["bridges"] == [{"source": "c", "target": "d"}]


def test_triangle_has_core_two() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("d")],
        [_link("a", "b"), _link("b", "c"), _link("c", "a"), _link("a", "d")],
    )
    result = analyze_components(graph)
    cores = {entry["node_id"]: entry["core"] for entry in result["k_core"]["nodes"]}
    assert cores == {"a": 2, "b": 2, "c": 2, "d": 1}
    assert result["k_core"]["max_core"] == 2
    # Sorted by (-core, node_id).
    assert [entry["node_id"] for entry in result["k_core"]["nodes"]] == ["a", "b", "c", "d"]


def test_disconnected_nodes_form_separate_weak_components() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("x"), _node("y")],
        [_link("a", "b"), _link("x", "y")],
    )
    result = analyze_components(graph, evidence_counts={"a": 1, "b": 1, "x": 1, "y": 1})
    assert [comp["nodes"] for comp in result["weak_components"]] == [["a", "b"], ["x", "y"]]
    assert [comp["component_id"] for comp in result["weak_components"]] == [0, 1]


def test_knowledge_islands_require_no_evidence_and_no_links() -> None:
    graph = _graph(
        [_node("linked", "P2"), _node("peer", "P2"), _node("orphan", "P5"), _node("evidenced", "P3")],
        [_link("linked", "peer")],
    )
    result = analyze_components(graph, evidence_counts={"evidenced": 2, "orphan": 0})
    assert result["knowledge_islands"] == [
        {"domain": "P5", "node_id": "orphan", "reason": "no_evidence_and_no_valid_relations"}
    ]


def test_knowledge_islands_default_to_zero_evidence() -> None:
    graph = _graph([_node("a"), _node("b")], [_link("a", "b")])
    result = analyze_components(graph)
    assert result["knowledge_islands"] == []
    lone = analyze_components(_graph([_node("a"), _node("b")], []))
    assert [item["node_id"] for item in lone["knowledge_islands"]] == ["a", "b"]


def test_invalid_evidence_counts_raise() -> None:
    graph = _graph([_node("a")], [])
    with pytest.raises(ValueError):
        analyze_components(graph, evidence_counts={"a": -1})
    with pytest.raises(ValueError):
        analyze_components(graph, evidence_counts={"a": True})


def test_dropped_links_are_counted() -> None:
    graph = _graph(
        [_node("a"), _node("b")],
        [_link("a", "b"), _link("a", "b"), _link("a", "a"), _link("a", "ghost")],
    )
    result = analyze_components(graph)
    assert result["link_count"] == 1
    assert result["dropped_links"] == 3


def test_summary_counts() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("solo")],
        [_link("a", "b"), _link("b", "c"), _link("c", "a")],
    )
    result = analyze_components(graph)
    assert result["summary"] == {
        "weak_components": 2,
        "strong_components": 2,
        "cyclic_strong_components": 1,
        "articulation_points": 0,
        "bridges": 0,
        "knowledge_islands": 1,
        "max_core": 2,
    }


def test_repeated_runs_are_identical() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("d")],
        [_link("a", "b"), _link("b", "a"), _link("c", "d")],
    )
    assert analyze_components(graph) == analyze_components(graph)


# ------------------------------------------------------------------ property


@st.composite
def _random_graphs(draw: st.DrawFn) -> dict[str, Any]:
    ids = draw(st.lists(st.sampled_from(_IDS), unique=True, max_size=6))
    nodes = [{"id": node_id, "domain": draw(st.sampled_from(_DOMAINS)), "kind": "leaf"} for node_id in ids]
    links = []
    if ids:
        links = draw(
            st.lists(
                st.tuples(st.sampled_from(ids), st.sampled_from(ids), st.sampled_from(_RELATIONS)).map(
                    lambda t: {"source": t[0], "target": t[1], "relation": t[2]}
                ),
                max_size=10,
            )
        )
    return {"directed": True, "nodes": nodes, "links": links, "hyperedges": []}


@given(data=st.data())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_input_permutation_produces_identical_output(data: st.DataObject) -> None:
    graph = data.draw(_random_graphs())
    evidence = {node["id"]: data.draw(st.integers(min_value=0, max_value=3)) for node in graph["nodes"]}
    baseline = analyze_components(graph, evidence_counts=evidence)
    node_perm = data.draw(st.permutations(list(range(len(graph["nodes"]))))) if graph["nodes"] else []
    link_perm = data.draw(st.permutations(list(range(len(graph["links"]))))) if graph["links"] else []
    shuffled = {
        "directed": True,
        "nodes": [graph["nodes"][i] for i in node_perm],
        "links": [graph["links"][i] for i in link_perm],
        "hyperedges": [],
    }
    assert analyze_components(shuffled, evidence_counts=evidence) == baseline


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_structural_invariants(graph: dict[str, Any]) -> None:
    result = analyze_components(graph)
    node_ids = {node["id"] for node in graph["nodes"]}
    # Weak components partition the node set.
    weak_members = [node for comp in result["weak_components"] for node in comp["nodes"]]
    assert sorted(weak_members) == sorted(node_ids)
    # Strong components also partition the node set.
    strong_members = [node for comp in result["strong_components"] for node in comp["nodes"]]
    assert sorted(strong_members) == sorted(node_ids)
    # Bridges are normalized (source < target) and reference real nodes.
    for bridge in result["bridges"]:
        assert bridge["source"] < bridge["target"]
        assert bridge["source"] in node_ids and bridge["target"] in node_ids
    # Every articulation point removal strictly grows the weak-component count.
    base = len(result["weak_components"])
    for point in result["articulation_points"]:
        remaining = [node for node in graph["nodes"] if node["id"] != point]
        links = [link for link in graph["links"] if link["source"] != point and link["target"] != point]
        reduced = analyze_components({"directed": True, "nodes": remaining, "links": links, "hyperedges": []})
        assert len(reduced["weak_components"]) > base
    # Core numbers never exceed the node's collapsed undirected degree.
    for entry in result["k_core"]["nodes"]:
        assert 0 <= entry["core"] <= max(0, len(node_ids) - 1)

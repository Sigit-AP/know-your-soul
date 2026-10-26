"""Tests for ``analytics.cycles`` (ATM wave A, plan F-A4).

Pins bounded cycle enumeration over the knowledge relations
(``depends_on``/``contradicts``/``supersedes``): canonical rotation dedup,
length bound, stop-early guards, and contradiction flagging — every finding
is a review candidate for circular reasoning in the knowledge graph.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.cycles import find_cycles

_IDS = ["a", "b", "c", "d", "e", "f"]
_RELATIONS = ["depends_on", "contradicts", "supersedes", "supports", "parent_of"]


def _node(node_id: str, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain, "kind": "leaf"}


def _link(source: str, target: str, relation: str = "depends_on") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links), "hyperedges": []}


# ------------------------------------------------------------------ examples


def test_empty_graph_has_no_cycles() -> None:
    result = find_cycles(_graph([], []))
    assert result["kind"] == "analytics.cycles"
    assert result["cycles"] == []
    assert result["summary"] == {"cycles_found": 0, "contradiction_cycles": 0, "truncated": False}
    assert result["guards"] == []


def test_acyclic_graph_has_no_cycles() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b"), _link("b", "c"), _link("a", "c")],
    )
    assert find_cycles(graph)["cycles"] == []


def test_two_cycle_is_found_once() -> None:
    graph = _graph([_node("a"), _node("b")], [_link("a", "b"), _link("b", "a")])
    result = find_cycles(graph)
    assert result["cycles"] == [
        {"has_contradiction": False, "length": 2, "nodes": ["a", "b"], "relations": ["depends_on"]}
    ]
    assert result["summary"]["cycles_found"] == 1


def test_triangle_cycle_rotation_is_canonical() -> None:
    # The same cycle reachable via different link orderings must appear once,
    # rotated to start at the lexicographically smallest node.
    links = [_link("c", "a"), _link("b", "c"), _link("a", "b")]
    result = find_cycles(_graph([_node("c"), _node("a"), _node("b")], links))
    assert result["cycles"] == [
        {"has_contradiction": False, "length": 3, "nodes": ["a", "b", "c"], "relations": ["depends_on"]}
    ]


def test_contradiction_cycle_is_flagged() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b", "depends_on"), _link("b", "c", "contradicts"), _link("c", "a", "depends_on")],
    )
    result = find_cycles(graph)
    assert result["cycles"] == [
        {
            "has_contradiction": True,
            "length": 3,
            "nodes": ["a", "b", "c"],
            "relations": ["contradicts", "depends_on"],
        }
    ]
    assert result["summary"]["contradiction_cycles"] == 1


def test_parallel_relations_on_one_pair_do_not_duplicate_cycles() -> None:
    graph = _graph(
        [_node("a"), _node("b")],
        [_link("a", "b", "depends_on"), _link("a", "b", "supersedes"), _link("b", "a")],
    )
    result = find_cycles(graph)
    assert result["summary"]["cycles_found"] == 1
    assert result["cycles"][0]["relations"] == ["depends_on", "supersedes"]


def test_unlisted_relations_are_not_enumerated() -> None:
    graph = _graph(
        [_node("a"), _node("b")],
        [_link("a", "b", "supports"), _link("b", "a", "supports")],
    )
    assert find_cycles(graph)["cycles"] == []
    assert find_cycles(graph, relations=("supports",))["summary"]["cycles_found"] == 1


def test_length_bound_excludes_longer_cycles() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b"), _link("b", "c"), _link("c", "a")],
    )
    assert find_cycles(graph, max_length=2)["cycles"] == []
    assert find_cycles(graph, max_length=3)["summary"]["cycles_found"] == 1


def test_max_cycles_guard_stops_early() -> None:
    # Complete digraph on 4 nodes has many cycles; the guard must truncate in
    # canonical order and say so.
    ids = ["a", "b", "c", "d"]
    links = [_link(u, v) for u in ids for v in ids if u != v]
    result = find_cycles(_graph([_node(i) for i in ids], links), max_cycles=3)
    assert result["summary"]["cycles_found"] == 3
    assert result["summary"]["truncated"] is True
    assert "max_cycles" in result["guards"]
    lengths = [cycle["length"] for cycle in result["cycles"]]
    assert lengths == sorted(lengths)


def test_max_expansions_guard_stops_early() -> None:
    ids = ["a", "b", "c", "d"]
    links = [_link(u, v) for u in ids for v in ids if u != v]
    result = find_cycles(_graph([_node(i) for i in ids], links), max_expansions=2)
    assert result["summary"]["truncated"] is True
    assert "max_expansions" in result["guards"]


def test_cycles_are_sorted_by_length_then_nodes() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("d")],
        [
            _link("a", "b"),
            _link("b", "a"),
            _link("a", "c"),
            _link("c", "d"),
            _link("d", "a"),
        ],
    )
    result = find_cycles(graph)
    assert [cycle["nodes"] for cycle in result["cycles"]] == [["a", "b"], ["a", "c", "d"]]


def test_invalid_parameters_raise() -> None:
    graph = _graph([_node("a")], [])
    with pytest.raises(ValueError):
        find_cycles(graph, relations=())
    with pytest.raises(ValueError):
        find_cycles(graph, max_length=1)
    with pytest.raises(ValueError):
        find_cycles(graph, max_cycles=0)
    with pytest.raises(ValueError):
        find_cycles(graph, max_expansions=0)


def test_repeated_runs_are_identical() -> None:
    ids = ["a", "b", "c", "d"]
    links = [_link(u, v) for u in ids for v in ids if u != v]
    graph = _graph([_node(i) for i in ids], links)
    assert find_cycles(graph, max_cycles=10) == find_cycles(graph, max_cycles=10)


# ------------------------------------------------------------------ property


@st.composite
def _random_graphs(draw: st.DrawFn) -> dict[str, Any]:
    ids = draw(st.lists(st.sampled_from(_IDS), unique=True, max_size=5))
    nodes = [{"id": node_id, "domain": "P1", "kind": "leaf"} for node_id in ids]
    links = []
    if ids:
        links = draw(
            st.lists(
                st.tuples(st.sampled_from(ids), st.sampled_from(ids), st.sampled_from(_RELATIONS)).map(
                    lambda t: {"source": t[0], "target": t[1], "relation": t[2]}
                ),
                max_size=8,
            )
        )
    return {"directed": True, "nodes": nodes, "links": links, "hyperedges": []}


@given(data=st.data())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_input_permutation_produces_identical_output(data: st.DataObject) -> None:
    graph = data.draw(_random_graphs())
    baseline = find_cycles(graph, max_cycles=32)
    node_perm = data.draw(st.permutations(list(range(len(graph["nodes"]))))) if graph["nodes"] else []
    link_perm = data.draw(st.permutations(list(range(len(graph["links"]))))) if graph["links"] else []
    shuffled = {
        "directed": True,
        "nodes": [graph["nodes"][i] for i in node_perm],
        "links": [graph["links"][i] for i in link_perm],
        "hyperedges": [],
    }
    assert find_cycles(shuffled, max_cycles=32) == baseline


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_reported_cycles_are_valid_simple_cycles(graph: dict[str, Any]) -> None:
    result = find_cycles(graph, max_cycles=64)
    links = {
        (link["source"], link["target"]): link["relation"]
        for link in graph["links"]
        if link["source"] != link["target"]
    }
    seen: set[tuple[str, ...]] = set()
    for cycle in result["cycles"]:
        nodes = cycle["nodes"]
        # Canonical rotation: the smallest node id leads.
        assert nodes[0] == min(nodes)
        # Simple: no repeated nodes.
        assert len(set(nodes)) == len(nodes)
        # Every consecutive pair (wrapping) is a real directed edge.
        for index, source in enumerate(nodes):
            target = nodes[(index + 1) % len(nodes)]
            assert (source, target) in links
        # Dedup: no cycle reported twice.
        key = tuple(nodes)
        assert key not in seen
        seen.add(key)
        assert cycle["length"] == len(nodes)
        assert cycle["has_contradiction"] == ("contradicts" in cycle["relations"])
    assert result["summary"]["cycles_found"] == len(result["cycles"])

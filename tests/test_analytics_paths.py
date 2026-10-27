"""Tests for ``analytics.paths`` — deterministic Yen k-shortest paths (SUB-74).

Pins:

* hand-computed path sets and canonical tie-breaking (equal-cost paths sort
  lexicographically by node sequence, so input permutation cannot matter);
* every returned path is simple, edge-valid, starts at the source and ends
  at the target, and costs are non-decreasing;
* the expansion cap fails softly with an honest guard, while unknown
  endpoints fail closed with the typed ``PathQueryError``;
* full determinism across input permutations (unit + hypothesis).
"""

from __future__ import annotations

import random
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.algorithms import AlgorithmError
from know_your_soul.analytics.paths import PathQueryError, find_k_shortest_paths


def _node(node_id: str, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain, "kind": "leaf"}


def _link(source: str, target: str, relation: str = "depends_on") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links), "hyperedges": []}


def _simple(node_ids: list[str], arcs: list[tuple[str, str]]) -> dict[str, Any]:
    return _graph([_node(node_id) for node_id in node_ids], [_link(s, t) for s, t in arcs])


def _diamond() -> dict[str, Any]:
    return _simple(["s", "a", "b", "t"], [("s", "t"), ("s", "a"), ("a", "t"), ("s", "b"), ("b", "t")])


def _permuted(graph: dict[str, Any], seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    nodes = list(graph["nodes"])
    links = list(graph["links"])
    rng.shuffle(nodes)
    rng.shuffle(links)
    return {"directed": graph["directed"], "nodes": nodes, "links": links, "hyperedges": []}


def _nodes_of(result: dict[str, Any]) -> list[list[str]]:
    return [entry["nodes"] for entry in result["paths"]]


# -------------------------------------------------------------- examples


def test_empty_graph_unknown_endpoints() -> None:
    with pytest.raises(PathQueryError):
        find_k_shortest_paths(_graph([], []), source="s", target="t")


def test_direct_edge_only() -> None:
    result = find_k_shortest_paths(_simple(["s", "t"], [("s", "t")]), source="s", target="t")
    assert result["kind"] == "analytics.paths.k_shortest"
    assert result["schema_version"] == 1
    assert result["paths"] == [{"rank": 0, "cost": 1, "nodes": ["s", "t"]}]
    assert result["path_count"] == 1
    assert result["guards"] == []


def test_no_path_returns_empty() -> None:
    graph = _simple(["s", "m", "t"], [("s", "m")])
    result = find_k_shortest_paths(graph, source="s", target="t")
    assert result["paths"] == []
    assert result["path_count"] == 0
    assert result["guards"] == []


def test_diamond_k_three_canonical_order() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=3)
    assert _nodes_of(result) == [["s", "t"], ["s", "a", "t"], ["s", "b", "t"]]
    assert [entry["cost"] for entry in result["paths"]] == [1, 2, 2]
    assert [entry["rank"] for entry in result["paths"]] == [0, 1, 2]


def test_k_larger_than_available_returns_all() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=10)
    assert result["path_count"] == 3


def test_k_one_returns_shortest_only() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=1)
    assert _nodes_of(result) == [["s", "t"]]


def test_direction_respected() -> None:
    graph = _simple(["s", "t"], [("t", "s")])
    result = find_k_shortest_paths(graph, source="s", target="t")
    assert result["paths"] == []


def test_two_edge_relations_collapse_to_one_path() -> None:
    graph = _graph(
        [_node("s"), _node("t")],
        [_link("s", "t", "depends_on"), _link("s", "t", "supports")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=5)
    assert _nodes_of(result) == [["s", "t"]]
    assert result["dropped_links"] == 0  # parallel relations are kept as links


def test_layered_graph_path_set() -> None:
    # s -> x -> t and s -> y -> t plus the longer s -> x -> y -> t.
    graph = _simple(["s", "x", "y", "t"], [("s", "x"), ("x", "t"), ("s", "y"), ("y", "t"), ("x", "y")])
    result = find_k_shortest_paths(graph, source="s", target="t", k=4)
    assert _nodes_of(result) == [["s", "x", "t"], ["s", "y", "t"], ["s", "x", "y", "t"]]


def test_paths_are_simple_on_cyclic_graph() -> None:
    graph = _simple(
        ["s", "a", "b", "t"],
        [("s", "a"), ("a", "b"), ("b", "a"), ("b", "t"), ("a", "t"), ("s", "b")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=6)
    assert result["path_count"] >= 2
    for entry in result["paths"]:
        assert len(set(entry["nodes"])) == len(entry["nodes"])


def test_self_loop_never_enter_path() -> None:
    graph = _graph([_node("s"), _node("t")], [_link("s", "s"), _link("s", "t")])
    result = find_k_shortest_paths(graph, source="s", target="t")
    assert _nodes_of(result) == [["s", "t"]]


def test_intermediate_node_paths_query() -> None:
    graph = _simple(["s", "a", "t"], [("s", "a"), ("a", "t"), ("s", "t")])
    result = find_k_shortest_paths(graph, source="a", target="t", k=3)
    assert _nodes_of(result) == [["a", "t"]]


# ---------------------------------------------------------------- errors


def test_unknown_source_fails_closed() -> None:
    with pytest.raises(PathQueryError, match="source"):
        find_k_shortest_paths(_diamond(), source="ghost", target="t")


def test_unknown_target_fails_closed() -> None:
    with pytest.raises(PathQueryError, match="target"):
        find_k_shortest_paths(_diamond(), source="s", target="ghost")


def test_same_source_target_rejected() -> None:
    with pytest.raises(PathQueryError, match="differ"):
        find_k_shortest_paths(_diamond(), source="s", target="s")


def test_path_error_is_algorithm_error() -> None:
    assert issubclass(PathQueryError, AlgorithmError)
    assert issubclass(PathQueryError, ValueError)


def test_rejects_bad_k() -> None:
    with pytest.raises(ValueError, match="k"):
        find_k_shortest_paths(_diamond(), source="s", target="t", k=0)


def test_rejects_bad_max_expansions() -> None:
    with pytest.raises(ValueError, match="max_expansions"):
        find_k_shortest_paths(_diamond(), source="s", target="t", max_expansions=0)


# ---------------------------------------------------------------- guards


def test_expansion_cap_guard() -> None:
    graph = _simple(
        ["s", "a", "b", "c", "t"],
        [("s", "a"), ("a", "t"), ("s", "b"), ("b", "t"), ("s", "t"), ("a", "b"), ("b", "a")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=5, max_expansions=2)
    assert "max_expansions_reached" in result["guards"]
    assert result["path_count"] <= 5


def test_expansion_cap_does_not_hide_found_paths() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=3, max_expansions=4)
    assert result["path_count"] >= 1
    assert result["paths"][0]["nodes"] == ["s", "t"]


def test_parameters_echoed() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=2, max_expansions=100)
    assert result["parameters"] == {"source": "s", "target": "t", "k": 2, "max_expansions": 100}


# ------------------------------------------------------------- invariants


def test_does_not_mutate_input() -> None:
    graph = _diamond()
    snapshot = repr(graph)
    find_k_shortest_paths(graph, source="s", target="t", k=3)
    assert repr(graph) == snapshot


def test_deterministic_under_permutation() -> None:
    baseline = find_k_shortest_paths(_diamond(), source="s", target="t", k=3)
    for seed in range(6):
        assert find_k_shortest_paths(_permuted(_diamond(), seed), source="s", target="t", k=3) == baseline


def test_result_shape_consistent_with_analytics_modules() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t")
    assert result["schema_version"] == 1
    assert result["kind"].startswith("analytics.")
    assert result["node_count"] == 4
    assert result["link_count"] == 5
    assert result["dropped_links"] == 0


@st.composite
def _dag_graphs(draw: Any) -> dict[str, Any]:
    """A DAG over ids drawn from a pool: arcs always go from smaller to
    larger ids, so the graph is acyclic and path counts stay finite."""
    node_ids = draw(st.lists(st.sampled_from(list("abcdef")), min_size=2, max_size=6, unique=True))
    arcs: list[dict[str, Any]] = []
    for source in node_ids:
        for target in node_ids:
            if source < target and draw(st.booleans()):
                arcs.append(_link(source, target))
    return _graph([_node(node_id) for node_id in node_ids], arcs)


@given(graph=_dag_graphs(), seed=st.integers(min_value=0, max_value=999))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_property_paths_valid_when_found(graph: dict[str, Any], seed: int) -> None:
    node_ids = sorted(node["id"] for node in graph["nodes"])
    source, target = node_ids[0], node_ids[-1]
    result = find_k_shortest_paths(graph, source=source, target=target, k=5)
    edges = {(link["source"], link["target"]) for link in graph["links"]}
    costs = []
    for entry in result["paths"]:
        nodes = entry["nodes"]
        assert nodes[0] == source and nodes[-1] == target
        assert len(set(nodes)) == len(nodes)  # simple
        for u, v in zip(nodes, nodes[1:], strict=False):
            assert (u, v) in edges
        assert entry["cost"] == len(nodes) - 1
        costs.append(entry["cost"])
    assert costs == sorted(costs)  # non-decreasing
    assert len(result["paths"]) == len({tuple(entry["nodes"]) for entry in result["paths"]})


@given(graph=_dag_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_property_deterministic_under_permutation(graph: dict[str, Any]) -> None:
    node_ids = sorted(node["id"] for node in graph["nodes"])
    baseline = find_k_shortest_paths(graph, source=node_ids[0], target=node_ids[-1], k=4)
    assert find_k_shortest_paths(_permuted(graph, 41), source=node_ids[0], target=node_ids[-1], k=4) == baseline


@given(graph=_dag_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_property_first_path_is_a_shortest_path(graph: dict[str, Any]) -> None:
    """The rank-0 path cost equals the BFS distance from source to target."""
    node_ids = sorted(node["id"] for node in graph["nodes"])
    source, target = node_ids[0], node_ids[-1]
    result = find_k_shortest_paths(graph, source=source, target=target, k=3)
    # Independent BFS oracle.
    adjacency: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for link in graph["links"]:
        adjacency[link["source"]].append(link["target"])
    dist = {source: 0}
    frontier = [source]
    while frontier:
        current = frontier.pop(0)
        for nxt in adjacency[current]:
            if nxt not in dist:
                dist[nxt] = dist[current] + 1
                frontier.append(nxt)
    if target in dist:
        assert result["paths"], "BFS reached the target but no path was returned"
        assert result["paths"][0]["cost"] == dist[target]
    else:
        assert result["paths"] == []


# ------------------------------------------------------------ second wave


def test_shared_prefix_second_path() -> None:
    # Both paths share the root s-a; Yen must ban the used edge (s, a) /
    # (a, b) appropriately and still find the detour through c.
    graph = _simple(["s", "a", "b", "c", "t"], [("s", "a"), ("a", "b"), ("b", "t"), ("a", "c"), ("c", "t")])
    result = find_k_shortest_paths(graph, source="s", target="t", k=2)
    assert _nodes_of(result) == [["s", "a", "b", "t"], ["s", "a", "c", "t"]]


def test_spur_from_later_position() -> None:
    graph = _simple(
        ["s", "a", "b", "t"],
        [("s", "a"), ("a", "b"), ("b", "t"), ("s", "b"), ("a", "t")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=3)
    assert _nodes_of(result) == [["s", "a", "t"], ["s", "b", "t"], ["s", "a", "b", "t"]]


def test_exact_k_when_more_exist() -> None:
    graph = _simple(
        ["s", "a", "b", "c", "t"],
        [("s", "t"), ("s", "a"), ("a", "t"), ("s", "b"), ("b", "t"), ("s", "c"), ("c", "t")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=2)
    assert _nodes_of(result) == [["s", "t"], ["s", "a", "t"]]
    assert result["path_count"] == 2


def test_long_detour_found_last() -> None:
    graph = _simple(
        ["s", "a", "b", "t"],
        [("s", "t"), ("s", "a"), ("a", "b"), ("b", "t")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=3)
    assert _nodes_of(result) == [["s", "t"], ["s", "a", "b", "t"]]


def test_source_with_only_incoming_edges() -> None:
    graph = _simple(["s", "a", "t"], [("a", "s"), ("a", "t")])
    result = find_k_shortest_paths(graph, source="s", target="t")
    assert result["paths"] == []


def test_path_error_message_contains_node_id() -> None:
    with pytest.raises(PathQueryError, match="ghost"):
        find_k_shortest_paths(_diamond(), source="ghost", target="t")


def test_dropped_links_reported() -> None:
    graph = _graph(
        [_node("s"), _node("t")],
        [_link("s", "t"), _link("s", "t"), _link("s", "s"), _link("s", "ghost")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t")
    assert result["dropped_links"] == 3
    assert result["link_count"] == 1
    assert _nodes_of(result) == [["s", "t"]]


def test_guards_empty_on_normal_run() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=3)
    assert result["guards"] == []


def test_bottleneck_graph_single_route() -> None:
    # Two lobes joined by a single bridge edge: exactly one s-t path exists
    # no matter how large k is.
    graph = _simple(
        ["s", "a", "m", "b", "t"],
        [("s", "a"), ("a", "s"), ("a", "m"), ("m", "b"), ("b", "t"), ("t", "b")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=9)
    assert _nodes_of(result) == [["s", "a", "m", "b", "t"]]


def test_k_default_is_bounded() -> None:
    from know_your_soul.analytics.paths import DEFAULT_K, DEFAULT_MAX_EXPANSIONS

    assert DEFAULT_K >= 1
    assert DEFAULT_MAX_EXPANSIONS >= 1
    result = find_k_shortest_paths(_diamond(), source="s", target="t")
    assert result["parameters"]["k"] == DEFAULT_K


@given(graph=_dag_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_property_path_count_never_exceeds_k(graph: dict[str, Any]) -> None:
    node_ids = sorted(node["id"] for node in graph["nodes"])
    result = find_k_shortest_paths(graph, source=node_ids[0], target=node_ids[-1], k=3)
    assert result["path_count"] <= 3
    assert result["path_count"] == len(result["paths"])


@given(graph=_dag_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_property_ranks_are_dense_from_zero(graph: dict[str, Any]) -> None:
    node_ids = sorted(node["id"] for node in graph["nodes"])
    result = find_k_shortest_paths(graph, source=node_ids[0], target=node_ids[-1], k=4)
    assert [entry["rank"] for entry in result["paths"]] == list(range(result["path_count"]))


def test_wide_fanout_lexicographic_order() -> None:
    graph = _simple(
        ["s", "a", "b", "c", "d", "t"],
        [("s", "a"), ("s", "b"), ("s", "c"), ("s", "d"), ("a", "t"), ("b", "t"), ("c", "t"), ("d", "t")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=4)
    assert _nodes_of(result) == [["s", "a", "t"], ["s", "b", "t"], ["s", "c", "t"], ["s", "d", "t"]]


def test_parameters_echoed_even_without_path() -> None:
    graph = _simple(["s", "t"], [])
    result = find_k_shortest_paths(graph, source="s", target="t", k=3, max_expansions=50)
    assert result["path_count"] == 0
    assert result["parameters"] == {"source": "s", "target": "t", "k": 3, "max_expansions": 50}


def test_budget_one_may_find_nothing_with_guard() -> None:
    result = find_k_shortest_paths(_diamond(), source="s", target="t", k=3, max_expansions=1)
    assert "max_expansions_reached" in result["guards"]
    assert result["path_count"] == 0


def test_deep_detour_chain() -> None:
    # s->a->t is shortest; s->b->c->t is the only detour.
    graph = _simple(
        ["s", "a", "b", "c", "t"],
        [("s", "a"), ("a", "t"), ("s", "b"), ("b", "c"), ("c", "t")],
    )
    result = find_k_shortest_paths(graph, source="s", target="t", k=5)
    assert _nodes_of(result) == [["s", "a", "t"], ["s", "b", "c", "t"]]
    assert [entry["cost"] for entry in result["paths"]] == [2, 3]

"""Tests for ``analytics.algorithms`` (ATM mega-wave 2, SUB-74).

Pins the public contract of the deterministic graph algorithm suite:

* every score is a 12-decimal fixed-point string (spec section 9);
* every ranking orders by ``(-score, node_id)`` so ties never depend on the
  input ordering of nodes or links;
* known-value examples are hand-computed (HITS on a path, eigenvector on an
  edge/triangle, Katz on a path, Louvain modularity on two triangles, …);
* mathematical invariants hold under hypothesis-generated graphs
  (clustering coefficients in [0, 1], HITS authority mass sums to 1,
  eigenvector L2 norm is 1, assortativity in [-1, 1], triad census sums to
  C(n, 3), Louvain partitions are exact covers);
* large inputs fail closed with the typed ``GraphCapExceededError``.
"""

from __future__ import annotations

import random
import re
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.algorithms import (
    DEFAULT_KATZ_ALPHA,
    GraphCapExceededError,
    compute_assortativity,
    compute_clustering,
    compute_eigenvector,
    compute_hits,
    compute_katz,
    compute_louvain,
    compute_reachability,
    count_motifs,
)

_SCORE_RE = re.compile(r"^-?\d+\.\d{12}$")

_TRIAD_CLASSES = [
    "003",
    "012",
    "102",
    "021D",
    "021U",
    "021C",
    "111D",
    "111U",
    "030T",
    "030C",
    "201",
    "120D",
    "120U",
    "120C",
    "210",
    "300",
]


def _node(node_id: str, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain, "kind": "leaf"}


def _link(source: str, target: str, relation: str = "depends_on") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links), "hyperedges": []}


def _simple(node_ids: list[str], arcs: list[tuple[str, str]]) -> dict[str, Any]:
    return _graph([_node(node_id) for node_id in node_ids], [_link(s, t) for s, t in arcs])


def _path_abc() -> dict[str, Any]:
    return _simple(["a", "b", "c"], [("a", "b"), ("b", "c")])


def _ranked(block: list[dict[str, str]]) -> dict[str, Decimal]:
    return {entry["node_id"]: Decimal(entry["score"]) for entry in block}


def _assert_quantized_strings(payload: Any) -> None:
    """Every dict value stored under a score-like key is a 12-decimal string."""
    score_keys = {"score", "coefficient", "modularity", "density", "ratio", "transitivity", "average_coefficient"}
    if isinstance(payload, dict):
        for key, value in payload.items():
            if key in score_keys:
                assert isinstance(value, str), f"{key}={value!r} not a string"
                assert _SCORE_RE.match(value), f"{key}={value!r} not 12-decimal fixed-point"
            else:
                _assert_quantized_strings(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_quantized_strings(item)


def _permuted(graph: dict[str, Any], seed: int) -> dict[str, Any]:
    rng = random.Random(seed)
    nodes = list(graph["nodes"])
    links = list(graph["links"])
    rng.shuffle(nodes)
    rng.shuffle(links)
    return {"directed": graph["directed"], "nodes": nodes, "links": links, "hyperedges": []}


@st.composite
def _random_graphs(draw: Any) -> dict[str, Any]:
    node_ids = draw(st.lists(st.sampled_from(list("abcdefgh")), min_size=0, max_size=8, unique=True))
    links: list[dict[str, Any]] = []
    if node_ids:
        arcs = draw(
            st.lists(
                st.tuples(st.sampled_from(node_ids), st.sampled_from(node_ids)),
                min_size=0,
                max_size=12,
            )
        )
        links = [_link(source, target) for source, target in arcs if source != target]
    return _graph([_node(node_id) for node_id in node_ids], links)


# ==================================================================== HITS


def test_hits_empty_graph() -> None:
    result = compute_hits(_graph([], []))
    assert result["kind"] == "analytics.algorithms.hits"
    assert result["schema_version"] == 1
    assert result["node_count"] == 0
    assert result["hubs"] == []
    assert result["authorities"] == []
    assert result["converged"] is True
    assert result["iterations_used"] == 0


def test_hits_no_edges_all_zero() -> None:
    result = compute_hits(_simple(["a", "b"], []))
    assert _ranked(result["hubs"]) == {"a": Decimal(0), "b": Decimal(0)}
    assert _ranked(result["authorities"]) == {"a": Decimal(0), "b": Decimal(0)}
    assert result["converged"] is True


def test_hits_path_hand_computed() -> None:
    # a->b->c: hubs settle at (1/2, 1/2, 0), authorities at (0, 1/2, 1/2).
    result = compute_hits(_path_abc())
    assert result["converged"] is True
    assert _ranked(result["hubs"]) == {"a": Decimal("0.5"), "b": Decimal("0.5"), "c": Decimal(0)}
    assert _ranked(result["authorities"]) == {"a": Decimal(0), "b": Decimal("0.5"), "c": Decimal("0.5")}


def test_hits_star_in_arrows() -> None:
    graph = _simple(["a", "b", "c", "h"], [("a", "h"), ("b", "h"), ("c", "h")])
    result = compute_hits(graph)
    authorities = _ranked(result["authorities"])
    hubs = _ranked(result["hubs"])
    assert authorities["h"] == Decimal(1)
    assert authorities["a"] == authorities["b"] == authorities["c"] == Decimal(0)
    third = Decimal(1) / Decimal(3)
    for leaf in ("a", "b", "c"):
        assert abs(hubs[leaf] - third) < Decimal("1e-11")
    assert hubs["h"] == Decimal(0)


def test_hits_mutual_pair_symmetric() -> None:
    result = compute_hits(_simple(["a", "b"], [("a", "b"), ("b", "a")]))
    assert _ranked(result["hubs"]) == {"a": Decimal("0.5"), "b": Decimal("0.5")}
    assert _ranked(result["authorities"]) == {"a": Decimal("0.5"), "b": Decimal("0.5")}


def test_hits_ranking_tie_break_canonical() -> None:
    result = compute_hits(_path_abc())
    hub_ids = [entry["node_id"] for entry in result["hubs"]]
    assert hub_ids == ["a", "b", "c"]


def test_hits_top_caps_rankings() -> None:
    result = compute_hits(_path_abc(), top=1)
    assert len(result["hubs"]) == 1
    assert len(result["authorities"]) == 1
    assert result["authorities"][0]["node_id"] == "b"


def test_hits_iteration_cap_marks_not_converged() -> None:
    # A single iteration can never certify convergence below epsilon on a
    # graph whose scores actually move.
    result = compute_hits(_path_abc(), max_iterations=1, epsilon="1e-30")
    assert result["converged"] is False
    assert result["iterations_used"] == 1


def test_hits_loose_epsilon_converges_early() -> None:
    result = compute_hits(_path_abc(), epsilon="1000")
    assert result["converged"] is True
    assert result["iterations_used"] == 1


def test_hits_parameters_echoed() -> None:
    result = compute_hits(_path_abc(), max_iterations=7, epsilon="1e-9", top=2)
    assert result["parameters"] == {"max_iterations": 7, "epsilon": "1E-9", "top": 2}


@pytest.mark.parametrize("field", ["max_iterations"], ids=["iterations"])
def test_hits_rejects_bad_iteration_count(field: str) -> None:
    with pytest.raises(ValueError, match=field):
        compute_hits(_path_abc(), max_iterations=0)


def test_hits_rejects_bad_epsilon() -> None:
    with pytest.raises(ValueError, match="epsilon"):
        compute_hits(_path_abc(), epsilon="0")
    with pytest.raises(ValueError, match="epsilon"):
        compute_hits(_path_abc(), epsilon="-1")


def test_hits_rejects_bad_top() -> None:
    with pytest.raises(ValueError, match="top"):
        compute_hits(_path_abc(), top=0)


def test_hits_scores_quantized() -> None:
    _assert_quantized_strings(compute_hits(_path_abc()))


def test_hits_deterministic_under_permutation() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e"],
        [("a", "b"), ("b", "c"), ("c", "d"), ("a", "e"), ("e", "d"), ("d", "b")],
    )
    baseline = compute_hits(graph)
    for seed in range(5):
        assert compute_hits(_permuted(graph, seed)) == baseline


def test_hits_does_not_mutate_input() -> None:
    graph = _path_abc()
    snapshot = repr(graph)
    compute_hits(graph)
    assert repr(graph) == snapshot


# ============================================================ eigenvector


def test_eigenvector_empty_graph() -> None:
    result = compute_eigenvector(_graph([], []))
    assert result["kind"] == "analytics.algorithms.eigenvector"
    assert result["eigenvector"] == []
    assert result["converged"] is True


def test_eigenvector_isolated_node_zero() -> None:
    result = compute_eigenvector(_simple(["solo"], []))
    assert _ranked(result["eigenvector"]) == {"solo": Decimal(0)}


def test_eigenvector_single_edge_hand_computed() -> None:
    result = compute_eigenvector(_simple(["a", "b"], [("a", "b")]))
    assert result["converged"] is True
    half_sqrt2 = Decimal(1) / Decimal(2).sqrt()
    for score in _ranked(result["eigenvector"]).values():
        assert abs(score - half_sqrt2) < Decimal("1e-11")


def test_eigenvector_triangle_uniform() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")])
    result = compute_eigenvector(graph)
    assert result["converged"] is True
    third_sqrt = Decimal(1) / Decimal(3).sqrt()
    for score in _ranked(result["eigenvector"]).values():
        assert abs(score - third_sqrt) < Decimal("1e-11")


def test_eigenvector_bipartite_path_oscillates() -> None:
    # The undirected path a-b-c is bipartite with eigenvalues +/-sqrt(2);
    # the uniform start overlaps the negative eigenvector, so power
    # iteration oscillates forever and the module must say so.
    result = compute_eigenvector(_path_abc())
    assert result["converged"] is False
    assert result["iterations_used"] == result["parameters"]["max_iterations"]


def test_eigenvector_nonbipartite_center_dominant() -> None:
    # Triangle a-b-c plus pendant d at b: connected and non-bipartite, so
    # the iteration converges and the degree-3 node b dominates.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("a", "c"), ("b", "d")])
    result = compute_eigenvector(graph)
    assert result["converged"] is True
    scores = _ranked(result["eigenvector"])
    assert scores["b"] > scores["a"]
    assert scores["a"] == scores["c"]
    assert scores["d"] < scores["b"]


def test_eigenvector_two_disjoint_edges() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("c", "d")])
    result = compute_eigenvector(graph)
    assert result["converged"] is True
    for score in _ranked(result["eigenvector"]).values():
        assert score == Decimal("0.5")


def test_eigenvector_star_does_not_converge_honestly() -> None:
    # Star K1,2: the uniform start has equal overlap with the +/-sqrt(2)
    # eigenvectors, so power iteration oscillates forever. The module must
    # report this honestly instead of pretending convergence.
    graph = _simple(["a", "b", "h"], [("h", "a"), ("h", "b")])
    result = compute_eigenvector(graph, max_iterations=16)
    assert result["converged"] is False
    assert result["iterations_used"] == 16


def test_eigenvector_disconnected_dominant_component_wins() -> None:
    # Triangle (spectral radius 2) + disjoint edge (spectral radius 1): the
    # edge component's mass decays to zero.
    graph = _simple(["a", "b", "c", "x", "y"], [("a", "b"), ("b", "c"), ("a", "c"), ("x", "y")])
    result = compute_eigenvector(graph)
    assert result["converged"] is True
    scores = _ranked(result["eigenvector"])
    third_sqrt = Decimal(1) / Decimal(3).sqrt()
    for node_id in ("a", "b", "c"):
        assert abs(scores[node_id] - third_sqrt) < Decimal("1e-11")
    assert scores["x"] == Decimal(0)
    assert scores["y"] == Decimal(0)


def test_eigenvector_direction_ignored() -> None:
    forward = compute_eigenvector(_simple(["a", "b", "c"], [("a", "b"), ("b", "c")]))
    backward = compute_eigenvector(_simple(["a", "b", "c"], [("b", "a"), ("c", "b")]))
    assert forward["eigenvector"] == backward["eigenvector"]


def test_eigenvector_top_cap() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("a", "c"), ("b", "d")])
    result = compute_eigenvector(graph, top=1)
    assert len(result["eigenvector"]) == 1
    assert result["eigenvector"][0]["node_id"] == "b"


def test_eigenvector_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="max_iterations"):
        compute_eigenvector(_path_abc(), max_iterations=0)
    with pytest.raises(ValueError, match="epsilon"):
        compute_eigenvector(_path_abc(), epsilon="0")


def test_eigenvector_scores_quantized() -> None:
    _assert_quantized_strings(compute_eigenvector(_path_abc()))


def test_eigenvector_deterministic_under_permutation() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")])
    baseline = compute_eigenvector(graph)
    for seed in range(5):
        assert compute_eigenvector(_permuted(graph, seed)) == baseline


# ================================================================= Katz


def test_katz_empty_graph() -> None:
    result = compute_katz(_graph([], []))
    assert result["kind"] == "analytics.algorithms.katz"
    assert result["katz"] == []
    assert result["converged"] is True


def test_katz_path_hand_computed() -> None:
    # alpha=1/2, beta=1 on a->b->c (in-direction prestige):
    # k=1 term (0, 1/2, 1/2); k=2 term (0, 0, 1/4); k=3 term zero -> done.
    result = compute_katz(_path_abc(), alpha="0.5", beta="1")
    assert result["converged"] is True
    assert _ranked(result["katz"]) == {
        "a": Decimal(0),
        "b": Decimal("0.5"),
        "c": Decimal("0.75"),
    }


def test_katz_star_in_arrows() -> None:
    graph = _simple(["a", "b", "c"], [("b", "a"), ("c", "a")])
    result = compute_katz(graph, alpha="0.5", beta="1")
    scores = _ranked(result["katz"])
    assert scores["a"] == Decimal(1)
    assert scores["b"] == scores["c"] == Decimal(0)


def test_katz_no_edges_all_zero() -> None:
    result = compute_katz(_simple(["a", "b"], []))
    assert _ranked(result["katz"]) == {"a": Decimal(0), "b": Decimal(0)}
    assert result["converged"] is True


def test_katz_beta_scales_linearly() -> None:
    one = compute_katz(_path_abc(), alpha="0.5", beta="1")
    two = compute_katz(_path_abc(), alpha="0.5", beta="2")
    for node_id, score in _ranked(one["katz"]).items():
        assert _ranked(two["katz"])[node_id] == score * 2


def test_katz_divergent_alpha_truncated_with_guard() -> None:
    # alpha=10 on a cycle never decays: the iteration cap must fire and the
    # guard must say so honestly.
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    result = compute_katz(graph, alpha="10", max_iterations=8)
    assert result["converged"] is False
    assert result["iterations_used"] == 8
    assert "katz_truncated" in result["guards"]


def test_katz_default_alpha_documented() -> None:
    result = compute_katz(_path_abc())
    assert result["parameters"]["alpha"] == str(Decimal(DEFAULT_KATZ_ALPHA))


def test_katz_rejects_bad_params() -> None:
    with pytest.raises(ValueError, match="alpha"):
        compute_katz(_path_abc(), alpha="0")
    with pytest.raises(ValueError, match="alpha"):
        compute_katz(_path_abc(), alpha="-0.5")
    with pytest.raises(ValueError, match="beta"):
        compute_katz(_path_abc(), beta="-1")
    with pytest.raises(ValueError, match="max_iterations"):
        compute_katz(_path_abc(), max_iterations=0)


def test_katz_respects_direction() -> None:
    forward = compute_katz(_path_abc(), alpha="0.5")
    backward = compute_katz(_simple(["a", "b", "c"], [("c", "b"), ("b", "a")]), alpha="0.5")
    assert _ranked(forward["katz"])["c"] == _ranked(backward["katz"])["a"]
    assert _ranked(forward["katz"])["a"] == _ranked(backward["katz"])["c"]


def test_katz_scores_quantized() -> None:
    _assert_quantized_strings(compute_katz(_path_abc()))


def test_katz_deterministic_under_permutation() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("a", "c"), ("d", "a")])
    baseline = compute_katz(graph, alpha="0.1")
    for seed in range(5):
        assert compute_katz(_permuted(graph, seed), alpha="0.1") == baseline


# ============================================================ clustering


def test_clustering_empty_graph() -> None:
    result = compute_clustering(_graph([], []))
    assert result["kind"] == "analytics.algorithms.clustering"
    assert result["per_node"] == []
    assert result["global"] == {
        "node_count": 0,
        "average_coefficient": "0.000000000000",
        "transitivity": "0.000000000000",
        "triangles": 0,
        "connected_triples": 0,
    }


def test_clustering_triangle_all_one() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")])
    result = compute_clustering(graph)
    for entry in result["per_node"]:
        assert entry["coefficient"] == "1.000000000000"
        assert entry["triangles"] == 1
    assert result["global"]["average_coefficient"] == "1.000000000000"
    assert result["global"]["transitivity"] == "1.000000000000"
    assert result["global"]["triangles"] == 1
    assert result["global"]["connected_triples"] == 3


def test_clustering_path_all_zero() -> None:
    result = compute_clustering(_path_abc())
    for entry in result["per_node"]:
        assert entry["coefficient"] == "0.000000000000"
    assert result["global"]["transitivity"] == "0.000000000000"
    assert result["global"]["connected_triples"] == 1


def test_clustering_diamond_hand_computed() -> None:
    # a connected to b, c, d; plus edge b-c.
    # C_a = 2*1/(3*2) = 1/3, C_b = C_c = 1, C_d = 0; average 7/12.
    # sum of triangles = 3, triples = 3 + 1 + 1 + 0 = 5 -> transitivity 3/5.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("a", "d"), ("b", "c")])
    result = compute_clustering(graph)
    per_node = {entry["node_id"]: entry for entry in result["per_node"]}
    assert per_node["a"]["coefficient"] == "0.333333333333"
    assert per_node["b"]["coefficient"] == "1.000000000000"
    assert per_node["c"]["coefficient"] == "1.000000000000"
    assert per_node["d"]["coefficient"] == "0.000000000000"
    assert per_node["a"]["degree"] == 3
    assert per_node["d"]["triangles"] == 0
    assert result["global"]["average_coefficient"] == "0.583333333333"
    assert result["global"]["transitivity"] == "0.600000000000"
    assert result["global"]["triangles"] == 1
    assert result["global"]["connected_triples"] == 5


def test_clustering_k4_all_one() -> None:
    nodes = ["a", "b", "c", "d"]
    arcs = [(u, v) for i, u in enumerate(nodes) for v in nodes[i + 1 :]]
    result = compute_clustering(_simple(nodes, arcs))
    assert result["global"]["triangles"] == 4
    assert result["global"]["transitivity"] == "1.000000000000"
    for entry in result["per_node"]:
        assert entry["triangles"] == 3


def test_clustering_parallel_relations_count_once() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b", "depends_on"), _link("a", "b", "supports"), _link("b", "c"), _link("a", "c")],
    )
    result = compute_clustering(graph)
    assert result["global"]["triangles"] == 1
    assert {entry["node_id"]: entry["degree"] for entry in result["per_node"]} == {"a": 2, "b": 2, "c": 2}


def test_clustering_direction_ignored() -> None:
    forward = compute_clustering(_simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")]))
    mixed = compute_clustering(_simple(["a", "b", "c"], [("b", "a"), ("b", "c"), ("c", "a")]))
    assert forward["global"] == mixed["global"]


def test_clustering_per_node_sorted_by_id() -> None:
    graph = _simple(["d", "c", "b", "a"], [("a", "b"), ("a", "c")])
    result = compute_clustering(graph)
    assert [entry["node_id"] for entry in result["per_node"]] == ["a", "b", "c", "d"]


def test_clustering_scores_quantized() -> None:
    _assert_quantized_strings(compute_clustering(_path_abc()))


def test_clustering_deterministic_under_permutation() -> None:
    graph = _simple(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("a", "c"), ("c", "d"), ("d", "e")])
    baseline = compute_clustering(graph)
    for seed in range(5):
        assert compute_clustering(_permuted(graph, seed)) == baseline


# =============================================================== Louvain


def test_louvain_empty_graph() -> None:
    result = compute_louvain(_graph([], []))
    assert result["kind"] == "analytics.algorithms.louvain"
    assert result["communities"] == []
    assert result["community_count"] == 0
    assert result["modularity"] == "0.000000000000"
    assert result["converged"] is True


def test_louvain_no_edges_all_singletons() -> None:
    result = compute_louvain(_simple(["a", "b", "c"], []))
    assert result["community_count"] == 3
    assert all(community["size"] == 1 for community in result["communities"])
    assert result["modularity"] == "0.000000000000"
    assert result["converged"] is True


def test_louvain_single_triangle_one_community() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")])
    result = compute_louvain(graph)
    assert result["community_count"] == 1
    assert result["communities"][0]["members"] == ["a", "b", "c"]
    # One community spanning the whole graph always has modularity zero.
    assert result["modularity"] == "0.000000000000"
    assert result["converged"] is True


def test_louvain_two_triangles_with_bridge() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
    )
    result = compute_louvain(graph)
    assert result["community_count"] == 2
    memberships = sorted(tuple(community["members"]) for community in result["communities"])
    assert memberships == [("a", "b", "c"), ("d", "e", "f")]
    # Q = 2 * (3/7 - (7/14)^2) = 5/14.
    assert result["modularity"] == "0.357142857143"
    assert result["converged"] is True
    assert result["levels_used"] == 2


def test_louvain_two_disconnected_triangles() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f")],
    )
    result = compute_louvain(graph)
    assert result["community_count"] == 2
    assert result["modularity"] == "0.500000000000"


def test_louvain_path_four_nodes() -> None:
    # Optimal split of the 4-path is {a,b} | {c,d} with Q = 1/6.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    result = compute_louvain(graph)
    memberships = sorted(tuple(community["members"]) for community in result["communities"])
    assert memberships == [("a", "b"), ("c", "d")]
    assert result["modularity"] == "0.166666666667"


def test_louvain_community_ids_canonical() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
    )
    result = compute_louvain(graph)
    sizes = [community["size"] for community in result["communities"]]
    assert sizes == sorted(sizes, reverse=True)
    assert [community["id"] for community in result["communities"]] == list(range(len(sizes)))
    for community in result["communities"]:
        assert community["members"] == sorted(community["members"])


def test_louvain_levels_recorded() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
    )
    result = compute_louvain(graph)
    assert result["levels"][0]["level"] == 0
    assert result["levels"][0]["community_count"] == 2
    assert result["levels"][-1]["modularity"] == result["modularity"]


def test_louvain_level_cap_guard() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
    )
    result = compute_louvain(graph, max_levels=1)
    assert result["levels_used"] == 1
    assert result["converged"] is False
    assert "louvain_max_levels_reached" in result["guards"]


def test_louvain_node_cap_fails_closed() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    with pytest.raises(GraphCapExceededError) as excinfo:
        compute_louvain(graph, max_nodes=3)
    error = excinfo.value
    assert error.algorithm == "louvain"
    assert error.cap == 3
    assert error.actual == 4
    assert "louvain" in str(error)


def test_louvain_cap_error_is_value_error() -> None:
    assert issubclass(GraphCapExceededError, ValueError)


def test_louvain_rejects_bad_params() -> None:
    graph = _path_abc()
    with pytest.raises(ValueError, match="max_passes"):
        compute_louvain(graph, max_passes=0)
    with pytest.raises(ValueError, match="max_levels"):
        compute_louvain(graph, max_levels=0)
    with pytest.raises(ValueError, match="max_nodes"):
        compute_louvain(graph, max_nodes=0)


def test_louvain_scores_quantized() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
    )
    _assert_quantized_strings(compute_louvain(graph))


def test_louvain_deterministic_under_permutation() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f", "g"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d"), ("f", "g")],
    )
    baseline = compute_louvain(graph)
    for seed in range(6):
        assert compute_louvain(_permuted(graph, seed)) == baseline


# ========================================================== reachability


def test_reachability_empty_graph() -> None:
    result = compute_reachability(_graph([], []))
    assert result["kind"] == "analytics.algorithms.reachability"
    assert result["per_node"] == []
    assert result["summary"] == {
        "pairs": 0,
        "density": "0.000000000000",
        "diameter": 0,
        "unreachable_pairs": 0,
    }


def test_reachability_path_hand_computed() -> None:
    result = compute_reachability(_path_abc())
    per_node = {entry["node_id"]: entry for entry in result["per_node"]}
    assert per_node["a"] == {"node_id": "a", "reachable": 2, "farthest": 2, "ratio": "1.000000000000"}
    assert per_node["b"] == {"node_id": "b", "reachable": 1, "farthest": 1, "ratio": "0.500000000000"}
    assert per_node["c"] == {"node_id": "c", "reachable": 0, "farthest": 0, "ratio": "0.000000000000"}
    assert result["summary"] == {
        "pairs": 3,
        "density": "0.500000000000",
        "diameter": 2,
        "unreachable_pairs": 3,
    }


def test_reachability_cycle_all_reachable() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    result = compute_reachability(graph)
    for entry in result["per_node"]:
        assert entry["reachable"] == 2
        assert entry["ratio"] == "1.000000000000"
    assert result["summary"]["density"] == "1.000000000000"
    assert result["summary"]["diameter"] == 2


def test_reachability_disconnected_pairs_counted() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("c", "d")])
    result = compute_reachability(graph)
    assert result["summary"]["pairs"] == 2
    assert result["summary"]["unreachable_pairs"] == 10
    assert result["summary"]["diameter"] == 1


def test_reachability_direction_matters() -> None:
    forward = compute_reachability(_path_abc())
    backward = compute_reachability(_simple(["a", "b", "c"], [("c", "b"), ("b", "a")]))
    assert {e["node_id"]: e["reachable"] for e in forward["per_node"]} == {"a": 2, "b": 1, "c": 0}
    assert {e["node_id"]: e["reachable"] for e in backward["per_node"]} == {"a": 0, "b": 1, "c": 2}


def test_reachability_per_node_ranking_order() -> None:
    result = compute_reachability(_path_abc())
    assert [entry["node_id"] for entry in result["per_node"]] == ["a", "b", "c"]


def test_reachability_single_node() -> None:
    result = compute_reachability(_simple(["solo"], []))
    assert result["per_node"] == [{"node_id": "solo", "reachable": 0, "farthest": 0, "ratio": "0.000000000000"}]
    assert result["summary"]["density"] == "0.000000000000"


def test_reachability_cap_fails_closed() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b")])
    with pytest.raises(GraphCapExceededError) as excinfo:
        compute_reachability(graph, max_nodes=3)
    assert excinfo.value.algorithm == "reachability"
    assert excinfo.value.cap == 3
    assert excinfo.value.actual == 4


def test_reachability_rejects_bad_cap() -> None:
    with pytest.raises(ValueError, match="max_nodes"):
        compute_reachability(_path_abc(), max_nodes=0)


def test_reachability_scores_quantized() -> None:
    _assert_quantized_strings(compute_reachability(_path_abc()))


def test_reachability_deterministic_under_permutation() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("d", "a"), ("c", "d")])
    baseline = compute_reachability(graph)
    for seed in range(5):
        assert compute_reachability(_permuted(graph, seed)) == baseline


# ================================================================ motifs


def test_motifs_empty_graph() -> None:
    result = count_motifs(_graph([], []))
    assert result["kind"] == "analytics.algorithms.motifs"
    assert result["triangles"]["total"] == 0
    assert result["triad_total"] == 0
    assert result["triad_census"] == {name: 0 for name in _TRIAD_CLASSES}


def test_motifs_two_nodes_one_triad_slot() -> None:
    result = count_motifs(_simple(["a", "b"], [("a", "b")]))
    assert result["triad_total"] == 0  # C(2, 3) == 0
    assert result["triangles"]["total"] == 0


def test_motifs_triangle_counting() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")])
    result = count_motifs(graph)
    assert result["triangles"]["total"] == 1
    per_node = {entry["node_id"]: entry["triangles"] for entry in result["triangles"]["per_node"]}
    assert per_node == {"a": 1, "b": 1, "c": 1}


def test_motifs_k4_triangle_counting() -> None:
    nodes = ["a", "b", "c", "d"]
    arcs = [(u, v) for i, u in enumerate(nodes) for v in nodes[i + 1 :]]
    result = count_motifs(_simple(nodes, arcs))
    assert result["triangles"]["total"] == 4
    for entry in result["triangles"]["per_node"]:
        assert entry["triangles"] == 3


def test_motifs_triangle_sum_invariant() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("c", "d"), ("d", "e"), ("c", "e")],
    )
    result = count_motifs(graph)
    total = sum(entry["triangles"] for entry in result["triangles"]["per_node"])
    assert total == 3 * result["triangles"]["total"]


def test_motifs_per_node_ranking_order() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("c", "d"), ("d", "e"), ("c", "e")],
    )
    result = count_motifs(graph)
    ranking = [(entry["triangles"], entry["node_id"]) for entry in result["triangles"]["per_node"]]
    assert ranking == sorted(ranking, key=lambda item: (-item[0], item[1]))


_TRIAD_EXAMPLES = [
    ("003", []),
    ("012", [("a", "b")]),
    ("102", [("a", "b"), ("b", "a")]),
    ("021D", [("a", "b"), ("a", "c")]),
    ("021U", [("b", "a"), ("c", "a")]),
    ("021C", [("a", "b"), ("b", "c")]),
    ("111D", [("a", "b"), ("b", "a"), ("c", "a")]),
    ("111U", [("a", "b"), ("b", "a"), ("a", "c")]),
    ("030T", [("a", "b"), ("a", "c"), ("b", "c")]),
    ("030C", [("a", "b"), ("b", "c"), ("c", "a")]),
    ("201", [("a", "b"), ("b", "a"), ("a", "c"), ("c", "a")]),
    ("120D", [("a", "b"), ("b", "a"), ("c", "a"), ("c", "b")]),
    ("120U", [("a", "b"), ("b", "a"), ("a", "c"), ("b", "c")]),
    ("120C", [("a", "b"), ("b", "a"), ("a", "c"), ("c", "b")]),
    ("210", [("a", "b"), ("b", "a"), ("a", "c"), ("c", "a"), ("b", "c")]),
    ("300", [("a", "b"), ("b", "a"), ("a", "c"), ("c", "a"), ("b", "c"), ("c", "b")]),
]


@pytest.mark.parametrize("expected,arcs", _TRIAD_EXAMPLES, ids=[name for name, _ in _TRIAD_EXAMPLES])
def test_motifs_triad_class_canonical_example(expected: str, arcs: list[tuple[str, str]]) -> None:
    result = count_motifs(_simple(["a", "b", "c"], arcs))
    assert result["triad_census"][expected] == 1
    assert result["triad_total"] == 1
    for name in _TRIAD_CLASSES:
        if name != expected:
            assert result["triad_census"][name] == 0


def test_motifs_triad_labels_are_relabeling_invariant() -> None:
    # The same 120C triad under a different node naming must classify equal.
    first = count_motifs(_simple(["a", "b", "c"], [("a", "b"), ("b", "a"), ("a", "c"), ("c", "b")]))
    second = count_motifs(_simple(["x", "y", "z"], [("x", "y"), ("y", "x"), ("x", "z"), ("z", "y")]))
    assert first["triad_census"] == second["triad_census"]
    assert first["triad_census"]["120C"] == 1


def test_motifs_complete_digraph_all_300() -> None:
    nodes = ["a", "b", "c", "d"]
    arcs = [(u, v) for u in nodes for v in nodes if u != v]
    result = count_motifs(_simple(nodes, arcs))
    assert result["triad_census"]["300"] == 4
    assert result["triad_total"] == 4


def test_motifs_cap_fails_closed() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b")])
    with pytest.raises(GraphCapExceededError) as excinfo:
        count_motifs(graph, max_nodes=3)
    assert excinfo.value.algorithm == "motifs"
    assert excinfo.value.cap == 3
    assert excinfo.value.actual == 4


def test_motifs_rejects_bad_cap() -> None:
    with pytest.raises(ValueError, match="max_nodes"):
        count_motifs(_path_abc(), max_nodes=0)


def test_motifs_deterministic_under_permutation() -> None:
    graph = _simple(
        ["a", "b", "c", "d"],
        [("a", "b"), ("b", "a"), ("a", "c"), ("c", "b"), ("b", "d"), ("d", "a")],
    )
    baseline = count_motifs(graph)
    for seed in range(5):
        assert count_motifs(_permuted(graph, seed)) == baseline


def test_motifs_triangles_agree_with_clustering() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("c", "d"), ("d", "e"), ("c", "e")],
    )
    assert count_motifs(graph)["triangles"]["total"] == compute_clustering(graph)["global"]["triangles"]


# ========================================================== assortativity


def test_assortativity_empty_graph() -> None:
    result = compute_assortativity(_graph([], []))
    assert result["kind"] == "analytics.algorithms.assortativity"
    assert result["defined"] is False
    assert result["score"] == "0.000000000000"
    assert "assortativity_undefined" in result["guards"]


def test_assortativity_star_minus_one() -> None:
    graph = _simple(["h", "a", "b"], [("h", "a"), ("h", "b")])
    result = compute_assortativity(graph)
    assert result["defined"] is True
    assert result["score"] == "-1.000000000000"


def test_assortativity_two_edge_path_minus_one() -> None:
    assert compute_assortativity(_path_abc())["score"] == "-1.000000000000"


def test_assortativity_three_edge_path_minus_half() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    assert compute_assortativity(graph)["score"] == "-0.500000000000"


def test_assortativity_regular_graph_undefined() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("a", "c")])
    result = compute_assortativity(graph)
    assert result["defined"] is False
    assert result["score"] == "0.000000000000"
    assert "assortativity_undefined" in result["guards"]


def test_assortativity_perfectly_assortative_mix() -> None:
    # A disjoint edge (deg 1 - deg 1) plus a triangle (deg 2 - deg 2): every
    # edge joins equal degrees, so r = 1 exactly.
    graph = _simple(
        ["a", "b", "x", "y", "z"],
        [("a", "b"), ("x", "y"), ("y", "z"), ("x", "z")],
    )
    result = compute_assortativity(graph)
    assert result["defined"] is True
    assert result["score"] == "1.000000000000"


def test_assortativity_counts_unique_pairs() -> None:
    graph = _simple(["h", "a", "b"], [("h", "a"), ("h", "b")])
    assert compute_assortativity(graph)["edge_pair_count"] == 2


def test_assortativity_parallel_relations_count_once() -> None:
    plain = compute_assortativity(_simple(["h", "a", "b"], [("h", "a"), ("h", "b")]))
    doubled = compute_assortativity(
        _graph(
            [_node("h"), _node("a"), _node("b")],
            [_link("h", "a", "depends_on"), _link("h", "a", "supports"), _link("h", "b")],
        )
    )
    assert plain["score"] == doubled["score"]
    assert doubled["edge_pair_count"] == 2


def test_assortativity_scores_quantized() -> None:
    _assert_quantized_strings(compute_assortativity(_path_abc()))


def test_assortativity_deterministic_under_permutation() -> None:
    graph = _simple(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("c", "d"), ("d", "e"), ("a", "e")])
    baseline = compute_assortativity(graph)
    for seed in range(5):
        assert compute_assortativity(_permuted(graph, seed)) == baseline


# ===================================================== property: invariant


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_clustering_coefficients_in_unit_interval(graph: dict[str, Any]) -> None:
    result = compute_clustering(graph)
    for entry in result["per_node"]:
        coefficient = Decimal(entry["coefficient"])
        assert Decimal(0) <= coefficient <= Decimal(1)
    average = Decimal(result["global"]["average_coefficient"])
    transitivity = Decimal(result["global"]["transitivity"])
    assert Decimal(0) <= average <= Decimal(1)
    assert Decimal(0) <= transitivity <= Decimal(1)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_clustering_deterministic(graph: dict[str, Any]) -> None:
    assert compute_clustering(graph) == compute_clustering(_permuted(graph, 7))


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_hits_authority_mass_is_one(graph: dict[str, Any]) -> None:
    result = compute_hits(graph)
    if result["link_count"] == 0:
        return
    total = sum(Decimal(entry["score"]) for entry in result["authorities"])
    assert abs(total - Decimal(1)) < Decimal("1e-9")
    hub_total = sum(Decimal(entry["score"]) for entry in result["hubs"])
    assert abs(hub_total - Decimal(1)) < Decimal("1e-9")


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_hits_deterministic(graph: dict[str, Any]) -> None:
    assert compute_hits(graph) == compute_hits(_permuted(graph, 11))


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_eigenvector_unit_norm_when_converged(graph: dict[str, Any]) -> None:
    result = compute_eigenvector(graph)
    if not result["converged"] or result["link_count"] == 0:
        return
    norm_sq = sum(Decimal(entry["score"]) ** 2 for entry in result["eigenvector"])
    assert abs(norm_sq - Decimal(1)) < Decimal("1e-9")


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_eigenvector_deterministic(graph: dict[str, Any]) -> None:
    assert compute_eigenvector(graph) == compute_eigenvector(_permuted(graph, 13))


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_katz_deterministic(graph: dict[str, Any]) -> None:
    assert compute_katz(graph, alpha="0.05") == compute_katz(_permuted(graph, 17), alpha="0.05")


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_katz_nonnegative(graph: dict[str, Any]) -> None:
    result = compute_katz(graph, alpha="0.05")
    for entry in result["katz"]:
        assert Decimal(entry["score"]) >= Decimal(0)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=20, deadline=None)
def test_property_louvain_partition_is_exact_cover(graph: dict[str, Any]) -> None:
    result = compute_louvain(graph)
    members = [member for community in result["communities"] for member in community["members"]]
    expected = sorted(node["id"] for node in graph["nodes"])
    assert sorted(members) == expected


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_louvain_modularity_in_bounds(graph: dict[str, Any]) -> None:
    result = compute_louvain(graph)
    modularity = Decimal(result["modularity"])
    assert Decimal("-0.5") <= modularity <= Decimal(1)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_louvain_deterministic(graph: dict[str, Any]) -> None:
    assert compute_louvain(graph) == compute_louvain(_permuted(graph, 19))


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_reachability_pair_accounting(graph: dict[str, Any]) -> None:
    result = compute_reachability(graph)
    n = result["node_count"]
    summary = result["summary"]
    assert summary["pairs"] + summary["unreachable_pairs"] == n * (n - 1)
    density = Decimal(summary["density"])
    assert Decimal(0) <= density <= Decimal(1)
    for entry in result["per_node"]:
        assert entry["reachable"] <= max(0, n - 1)
        assert entry["farthest"] <= max(0, n - 1)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_reachability_deterministic(graph: dict[str, Any]) -> None:
    assert compute_reachability(graph) == compute_reachability(_permuted(graph, 23))


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_triad_census_sums_to_binomial(graph: dict[str, Any]) -> None:
    result = count_motifs(graph)
    n = result["node_count"]
    assert result["triad_total"] == n * (n - 1) * (n - 2) // 6
    assert sum(result["triad_census"].values()) == result["triad_total"]
    triangles = sum(entry["triangles"] for entry in result["triangles"]["per_node"])
    assert triangles == 3 * result["triangles"]["total"]


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_motifs_deterministic(graph: dict[str, Any]) -> None:
    assert count_motifs(graph) == count_motifs(_permuted(graph, 29))


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_assortativity_in_range_when_defined(graph: dict[str, Any]) -> None:
    result = compute_assortativity(graph)
    score = Decimal(result["score"])
    if result["defined"]:
        assert Decimal(-1) <= score <= Decimal(1)
    else:
        assert score == Decimal(0)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_assortativity_deterministic(graph: dict[str, Any]) -> None:
    assert compute_assortativity(graph) == compute_assortativity(_permuted(graph, 31))


# ==================================================== api consistency pins


def test_all_functions_share_schema_shape() -> None:
    graph = _path_abc()
    results = [
        compute_hits(graph),
        compute_eigenvector(graph),
        compute_katz(graph),
        compute_clustering(graph),
        compute_louvain(graph),
        compute_reachability(graph),
        count_motifs(graph),
        compute_assortativity(graph),
    ]
    for result in results:
        assert result["schema_version"] == 1
        assert result["kind"].startswith("analytics.")
        assert result["node_count"] == 3
        assert result["link_count"] == 2
        assert result["dropped_links"] == 0


def test_all_functions_report_dropped_links() -> None:
    graph = _graph(
        [_node("a"), _node("b")],
        [
            _link("a", "b"),
            _link("a", "b"),  # duplicate
            _link("a", "a"),  # self-loop
            _link("a", "ghost"),  # unknown endpoint
            {"source": "a"},  # malformed row
        ],
    )
    for result in (compute_hits(graph), compute_clustering(graph), compute_assortativity(graph)):
        assert result["dropped_links"] == 4
        assert result["link_count"] == 1


def test_functions_accept_minimal_graph_shapes() -> None:
    # Missing keys / non-list members degrade gracefully, like the wave-A
    # modules: unknown rows are dropped, never crash.
    for function in (compute_hits, compute_eigenvector, compute_katz, compute_clustering, compute_assortativity):
        result = function({})
        assert result["node_count"] == 0


def test_cap_error_message_mentions_algorithm_and_cap() -> None:
    graph = _simple(["a", "b", "c", "d"], [])
    with pytest.raises(GraphCapExceededError, match="reachability"):
        compute_reachability(graph, max_nodes=2)


# ======================================================== second wave: more


def test_hits_path_converges_in_two_iterations() -> None:
    result = compute_hits(_path_abc())
    assert result["iterations_used"] == 2


def test_hits_complete_digraph_uniform() -> None:
    nodes = ["a", "b", "c", "d"]
    arcs = [(u, v) for u in nodes for v in nodes if u != v]
    result = compute_hits(_simple(nodes, arcs))
    assert result["converged"] is True
    for entry in result["hubs"] + result["authorities"]:
        assert entry["score"] == "0.250000000000"


def test_hits_chain_of_four_uniform_thirds() -> None:
    # Every internal node has in/out degree exactly 1, so mass spreads
    # uniformly: hubs a=b=c, authorities b=c=d, endpoints zero on one side.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    result = compute_hits(graph)
    hubs = _ranked(result["hubs"])
    authorities = _ranked(result["authorities"])
    third = Decimal(1) / Decimal(3)
    for node_id in ("a", "b", "c"):
        assert abs(hubs[node_id] - third) < Decimal("1e-11")
    assert hubs["d"] == Decimal(0)
    for node_id in ("b", "c", "d"):
        assert abs(authorities[node_id] - third) < Decimal("1e-11")
    assert authorities["a"] == Decimal(0)


def test_hits_top_none_returns_all_nodes() -> None:
    result = compute_hits(_path_abc(), top=None)
    assert len(result["hubs"]) == 3
    assert len(result["authorities"]) == 3


def test_hits_guards_field_absent_not_empty() -> None:
    # HITS has no soft guards; convergence state is the honest signal.
    result = compute_hits(_path_abc())
    assert "guards" not in result


def test_eigenvector_k4_uniform() -> None:
    nodes = ["a", "b", "c", "d"]
    arcs = [(u, v) for i, u in enumerate(nodes) for v in nodes[i + 1 :]]
    result = compute_eigenvector(_simple(nodes, arcs))
    assert result["converged"] is True
    for entry in result["eigenvector"]:
        assert entry["score"] == "0.500000000000"


def test_eigenvector_two_triangles_uniform() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f")],
    )
    result = compute_eigenvector(graph)
    assert result["converged"] is True
    sixth_sqrt = Decimal(1) / Decimal(6).sqrt()
    for entry in result["eigenvector"]:
        assert abs(Decimal(entry["score"]) - sixth_sqrt) < Decimal("1e-11")


def test_eigenvector_single_edge_converges_in_two_iterations() -> None:
    result = compute_eigenvector(_simple(["a", "b"], [("a", "b")]))
    assert result["iterations_used"] == 2


def test_eigenvector_iterations_used_bounded_by_cap() -> None:
    result = compute_eigenvector(_path_abc(), max_iterations=9)
    assert result["iterations_used"] == 9


def test_katz_chain_of_four_hand_computed() -> None:
    # alpha=1/2: b = 1/2, c = 1/2 + 1/4, d = 1/2 + 1/4 + 1/8.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    result = compute_katz(graph, alpha="0.5", beta="1")
    assert result["converged"] is True
    assert _ranked(result["katz"]) == {
        "a": Decimal(0),
        "b": Decimal("0.5"),
        "c": Decimal("0.75"),
        "d": Decimal("0.875"),
    }


def test_katz_path_converges_in_three_steps() -> None:
    result = compute_katz(_path_abc(), alpha="0.5", beta="1")
    assert result["iterations_used"] == 3


def test_katz_converged_run_has_no_guards() -> None:
    result = compute_katz(_path_abc(), alpha="0.5")
    assert result["guards"] == []


def test_katz_alpha_scales_attenuation() -> None:
    weak = compute_katz(_path_abc(), alpha="0.1")
    strong = compute_katz(_path_abc(), alpha="0.5")
    assert _ranked(weak["katz"])["c"] < _ranked(strong["katz"])["c"]


def test_clustering_bowtie_hand_computed() -> None:
    # Two triangles sharing only the center node.
    graph = _simple(
        ["c", "a", "b", "d", "e"],
        [("c", "a"), ("c", "b"), ("a", "b"), ("c", "d"), ("c", "e"), ("d", "e")],
    )
    result = compute_clustering(graph)
    per_node = {entry["node_id"]: entry for entry in result["per_node"]}
    assert per_node["c"]["coefficient"] == "0.333333333333"
    for wing in ("a", "b", "d", "e"):
        assert per_node[wing]["coefficient"] == "1.000000000000"
    assert result["global"]["triangles"] == 2
    assert result["global"]["connected_triples"] == 10
    assert result["global"]["transitivity"] == "0.600000000000"


def test_clustering_disconnected_mix() -> None:
    graph = _simple(["a", "b", "c", "x", "y"], [("a", "b"), ("b", "c"), ("a", "c"), ("x", "y")])
    result = compute_clustering(graph)
    assert result["global"]["average_coefficient"] == "0.600000000000"
    assert result["global"]["transitivity"] == "1.000000000000"
    assert result["global"]["triangles"] == 1


def test_clustering_square_zero() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")])
    result = compute_clustering(graph)
    assert result["global"]["triangles"] == 0
    assert result["global"]["average_coefficient"] == "0.000000000000"


def test_clustering_square_with_diagonal() -> None:
    graph = _simple(
        ["a", "b", "c", "d"],
        [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d"), ("b", "d")],
    )
    result = compute_clustering(graph)
    assert result["global"]["triangles"] == 2
    per_node = {entry["node_id"]: entry for entry in result["per_node"]}
    assert per_node["b"]["coefficient"] == "0.666666666667"
    assert per_node["a"]["coefficient"] == "1.000000000000"


def test_louvain_single_edge_one_community() -> None:
    result = compute_louvain(_simple(["a", "b"], [("a", "b")]))
    assert result["community_count"] == 1
    assert result["modularity"] == "0.000000000000"
    assert result["converged"] is True


def test_louvain_star_single_community() -> None:
    graph = _simple(["h", "a", "b", "d"], [("h", "a"), ("h", "b"), ("h", "d")])
    result = compute_louvain(graph)
    assert result["community_count"] == 1
    assert result["modularity"] == "0.000000000000"


def test_louvain_square_splits_in_two() -> None:
    # C4: the split {a,b}|{c,d} has Q = 2*(1/4 - (4/8)^2) = 0, still strictly
    # better than singletons (-1/4); Louvain's greedy pass finds it.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d"), ("a", "d")])
    result = compute_louvain(graph)
    memberships = sorted(tuple(community["members"]) for community in result["communities"])
    assert memberships == [("a", "b"), ("c", "d")]
    assert result["modularity"] == "0.000000000000"


def test_louvain_triangle_plus_edge() -> None:
    graph = _simple(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e")])
    result = compute_louvain(graph)
    memberships = sorted(tuple(community["members"]) for community in result["communities"])
    assert memberships == [("a", "b", "c"), ("d", "e")]
    assert result["modularity"] == "0.375000000000"


def test_louvain_converged_run_has_no_guards() -> None:
    result = compute_louvain(_path_abc())
    assert result["guards"] == []
    assert result["converged"] is True


def test_louvain_pass_cap_guard_and_valid_partition() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f"), ("c", "d")],
    )
    result = compute_louvain(graph, max_passes=1)
    assert "louvain_local_pass_cap" in result["guards"]
    members = [member for community in result["communities"] for member in community["members"]]
    assert sorted(members) == ["a", "b", "c", "d", "e", "f"]


def test_louvain_levels_entries_quantized() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    result = compute_louvain(graph)
    for level in result["levels"]:
        assert _SCORE_RE.match(level["modularity"])


def test_reachability_chain_of_four() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("b", "c"), ("c", "d")])
    result = compute_reachability(graph)
    per_node = {entry["node_id"]: entry for entry in result["per_node"]}
    assert per_node["a"]["reachable"] == 3
    assert per_node["a"]["farthest"] == 3
    assert per_node["a"]["ratio"] == "1.000000000000"
    assert result["summary"]["pairs"] == 6
    assert result["summary"]["density"] == "0.500000000000"
    assert result["summary"]["diameter"] == 3


def test_reachability_complete_digraph_density_one() -> None:
    nodes = ["a", "b", "c"]
    arcs = [(u, v) for u in nodes for v in nodes if u != v]
    result = compute_reachability(_simple(nodes, arcs))
    assert result["summary"]["density"] == "1.000000000000"
    assert result["summary"]["diameter"] == 1
    assert result["summary"]["unreachable_pairs"] == 0


def test_reachability_out_star() -> None:
    graph = _simple(["h", "a", "b"], [("h", "a"), ("h", "b")])
    result = compute_reachability(graph)
    per_node = {entry["node_id"]: entry for entry in result["per_node"]}
    assert per_node["h"]["reachable"] == 2
    assert per_node["a"]["reachable"] == 0
    assert result["summary"]["density"] == "0.333333333333"


def test_motifs_two_disjoint_cycles_census() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("c", "a"), ("d", "e"), ("e", "f"), ("f", "d")],
    )
    result = count_motifs(graph)
    assert result["triad_census"]["030C"] == 2
    assert result["triad_census"]["012"] == 18
    assert result["triad_total"] == 20


def test_motifs_triangle_with_pendant_census() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("a", "c"), ("b", "c"), ("a", "d")])
    result = count_motifs(graph)
    assert result["triad_census"]["030T"] == 1
    assert result["triad_census"]["021D"] == 2
    assert result["triad_census"]["012"] == 1
    assert result["triad_total"] == 4


def test_motifs_bowtie_triangles() -> None:
    graph = _simple(
        ["c", "a", "b", "d", "e"],
        [("c", "a"), ("c", "b"), ("a", "b"), ("c", "d"), ("c", "e"), ("d", "e")],
    )
    result = count_motifs(graph)
    assert result["triangles"]["total"] == 2
    per_node = {entry["node_id"]: entry["triangles"] for entry in result["triangles"]["per_node"]}
    assert per_node["c"] == 2
    for wing in ("a", "b", "d", "e"):
        assert per_node[wing] == 1


def test_assortativity_two_hubs_with_leaves() -> None:
    graph = _simple(
        ["h1", "h2", "a1", "a2", "b1", "b2"],
        [("h1", "h2"), ("h1", "a1"), ("h1", "a2"), ("h2", "b1"), ("h2", "b2")],
    )
    result = compute_assortativity(graph)
    assert result["defined"] is True
    assert result["score"] == "-0.666666666667"


def test_assortativity_single_edge_undefined() -> None:
    result = compute_assortativity(_simple(["a", "b"], [("a", "b")]))
    assert result["defined"] is False
    assert result["edge_pair_count"] == 1


def test_assortativity_direction_ignored() -> None:
    forward = compute_assortativity(_simple(["h", "a", "b"], [("h", "a"), ("h", "b")]))
    backward = compute_assortativity(_simple(["h", "a", "b"], [("a", "h"), ("b", "h")]))
    assert forward["score"] == backward["score"]


_TRIAD_SECOND_EXAMPLES = [
    ("003", ["x", "y", "z"], []),
    ("012", ["x", "y", "z"], [("z", "x")]),
    ("102", ["x", "y", "z"], [("y", "z"), ("z", "y")]),
    ("021D", ["x", "y", "z"], [("z", "x"), ("z", "y")]),
    ("021U", ["x", "y", "z"], [("x", "z"), ("y", "z")]),
    ("021C", ["x", "y", "z"], [("z", "y"), ("y", "x")]),
    ("111D", ["x", "y", "z"], [("x", "z"), ("z", "x"), ("y", "z")]),
    ("111U", ["x", "y", "z"], [("x", "z"), ("z", "x"), ("x", "y")]),
    ("030T", ["x", "y", "z"], [("z", "y"), ("z", "x"), ("y", "x")]),
    ("030C", ["x", "y", "z"], [("x", "z"), ("z", "y"), ("y", "x")]),
    ("201", ["x", "y", "z"], [("x", "y"), ("y", "x"), ("y", "z"), ("z", "y")]),
    ("120D", ["x", "y", "z"], [("x", "y"), ("y", "x"), ("z", "x"), ("z", "y")]),
    ("120U", ["x", "y", "z"], [("x", "y"), ("y", "x"), ("x", "z"), ("y", "z")]),
    ("120C", ["x", "y", "z"], [("x", "y"), ("y", "x"), ("z", "x"), ("y", "z")]),
    ("210", ["x", "y", "z"], [("x", "y"), ("y", "x"), ("y", "z"), ("z", "y"), ("x", "z")]),
    ("300", ["x", "y", "z"], [("x", "y"), ("y", "x"), ("x", "z"), ("z", "x"), ("y", "z"), ("z", "y")]),
]


@pytest.mark.parametrize(
    "expected,node_ids,arcs", _TRIAD_SECOND_EXAMPLES, ids=[name for name, _, _ in _TRIAD_SECOND_EXAMPLES]
)
def test_motifs_triad_class_second_naming(expected: str, node_ids: list[str], arcs: list[tuple[str, str]]) -> None:
    result = count_motifs(_simple(node_ids, arcs))
    assert result["triad_census"][expected] == 1
    assert result["triad_total"] == 1


# --------------------------------------------- per-function contract battery


def _rich_graph() -> dict[str, Any]:
    return _graph(
        [_node(node_id) for node_id in ["a", "b", "c", "d", "e", "f"]],
        [
            _link("a", "b"),
            _link("b", "c"),
            _link("a", "c"),
            _link("c", "d"),
            _link("d", "e"),
            _link("e", "f"),
            _link("d", "f"),
            _link("f", "a", "supports"),
        ],
    )


_ALL_ALGORITHMS = [
    ("hits", compute_hits),
    ("eigenvector", compute_eigenvector),
    ("katz", compute_katz),
    ("clustering", compute_clustering),
    ("louvain", compute_louvain),
    ("reachability", compute_reachability),
    ("motifs", count_motifs),
    ("assortativity", compute_assortativity),
]


@pytest.mark.parametrize("name,function", _ALL_ALGORITHMS, ids=[name for name, _ in _ALL_ALGORITHMS])
def test_contract_kind_and_schema(name: str, function: Any) -> None:
    result = function(_rich_graph())
    assert result["kind"] == f"analytics.algorithms.{name}"
    assert result["schema_version"] == 1
    assert result["node_count"] == 6
    assert result["link_count"] == 8


@pytest.mark.parametrize("name,function", _ALL_ALGORITHMS, ids=[name for name, _ in _ALL_ALGORITHMS])
def test_contract_scores_quantized(name: str, function: Any) -> None:
    _assert_quantized_strings(function(_rich_graph()))


@pytest.mark.parametrize("name,function", _ALL_ALGORITHMS, ids=[name for name, _ in _ALL_ALGORITHMS])
def test_contract_deterministic_under_permutations(name: str, function: Any) -> None:
    graph = _rich_graph()
    baseline = function(graph)
    for seed in (1, 2, 3):
        assert function(_permuted(graph, seed)) == baseline


@pytest.mark.parametrize("name,function", _ALL_ALGORITHMS, ids=[name for name, _ in _ALL_ALGORITHMS])
def test_contract_does_not_mutate_input(name: str, function: Any) -> None:
    graph = _rich_graph()
    snapshot = repr(graph)
    function(graph)
    assert repr(graph) == snapshot


# ------------------------------------------------------- more invariants


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_hits_scores_in_unit_interval(graph: dict[str, Any]) -> None:
    result = compute_hits(graph)
    for entry in result["hubs"] + result["authorities"]:
        assert Decimal(0) <= Decimal(entry["score"]) <= Decimal(1)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_eigenvector_scores_in_unit_interval(graph: dict[str, Any]) -> None:
    result = compute_eigenvector(graph)
    for entry in result["eigenvector"]:
        assert Decimal(0) <= Decimal(entry["score"]) <= Decimal(1)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_louvain_community_count_bounded(graph: dict[str, Any]) -> None:
    result = compute_louvain(graph)
    assert 0 <= result["community_count"] <= result["node_count"]
    assert result["levels_used"] <= result["parameters"]["max_levels"]
    if result["converged"]:
        assert "louvain_max_levels_reached" not in result["guards"]


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_reachability_farthest_consistent(graph: dict[str, Any]) -> None:
    result = compute_reachability(graph)
    for entry in result["per_node"]:
        assert (entry["reachable"] > 0) == (entry["farthest"] > 0)
        assert entry["farthest"] >= 1 or entry["reachable"] == 0


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_triad_support_triangles_match_counter(graph: dict[str, Any]) -> None:
    # A triad's underlying undirected support is a triangle exactly when all
    # three dyads carry arcs: classes 030T, 030C, 120*, 210, 300.
    result = count_motifs(graph)
    census = result["triad_census"]
    support_triangles = (
        census["030T"]
        + census["030C"]
        + census["120D"]
        + census["120U"]
        + census["120C"]
        + census["210"]
        + census["300"]
    )
    assert support_triangles == result["triangles"]["total"]


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_assortativity_defined_implies_edges(graph: dict[str, Any]) -> None:
    result = compute_assortativity(graph)
    if result["defined"]:
        assert result["edge_pair_count"] >= 1
    if result["edge_pair_count"] == 0:
        assert not result["defined"]


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=20, deadline=None)
def test_property_katz_dag_always_converges(graph: dict[str, Any]) -> None:
    # On a DAG walks terminate, so the truncated expansion always converges
    # regardless of alpha.
    node_ids = sorted(node["id"] for node in graph["nodes"])
    order = {node_id: index for index, node_id in enumerate(node_ids)}
    dag_links = [link for link in graph["links"] if order.get(link["source"], 0) < order.get(link["target"], 0)]
    dag = _graph(list(graph["nodes"]), dag_links)
    result = compute_katz(dag, alpha="2", max_iterations=len(node_ids) + 1)
    assert result["converged"] is True
    assert result["guards"] == []


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_hits_top_never_exceeds_graph(graph: dict[str, Any]) -> None:
    result = compute_hits(graph, top=3)
    assert len(result["hubs"]) <= min(3, result["node_count"])


# --------------------------------------------------------- third wave: top-up


def test_hits_two_disjoint_edges_uniform() -> None:
    # Each hub pair shares one unit of mass per vector: hubs a == c == 1/2,
    # authorities b == d == 1/2, the rest zero.
    graph = _simple(["a", "b", "c", "d"], [("a", "b"), ("c", "d")])
    result = compute_hits(graph)
    assert result["converged"] is True
    assert _ranked(result["hubs"]) == {
        "a": Decimal("0.5"),
        "c": Decimal("0.5"),
        "b": Decimal(0),
        "d": Decimal(0),
    }
    assert _ranked(result["authorities"]) == {
        "b": Decimal("0.5"),
        "d": Decimal("0.5"),
        "a": Decimal(0),
        "c": Decimal(0),
    }


def test_hits_epsilon_accepts_decimal_and_int() -> None:
    assert compute_hits(_path_abc(), epsilon=Decimal("0.5"))["converged"] is True
    result = compute_hits(_path_abc(), epsilon=1)
    assert result["parameters"]["epsilon"] == "1"


def test_hits_top_beyond_node_count_returns_all() -> None:
    result = compute_hits(_path_abc(), top=99)
    assert len(result["hubs"]) == 3


def test_eigenvector_parameters_echoed() -> None:
    result = compute_eigenvector(_path_abc(), max_iterations=7, epsilon="1e-9", top=2)
    assert result["parameters"] == {"max_iterations": 7, "epsilon": "1E-9", "top": 2}


def test_eigenvector_self_loop_only_scores_zero() -> None:
    graph = _graph([_node("a")], [_link("a", "a")])
    result = compute_eigenvector(graph)
    assert _ranked(result["eigenvector"]) == {"a": Decimal(0)}


def test_katz_parameters_echoed() -> None:
    result = compute_katz(_path_abc(), alpha="0.2", beta="3", max_iterations=5, top=1)
    assert result["parameters"] == {
        "alpha": "0.2",
        "beta": "3",
        "max_iterations": 5,
        "epsilon": "1E-15",
        "top": 1,
    }


def test_katz_beta_zero_all_zero_converged() -> None:
    result = compute_katz(_path_abc(), beta="0")
    assert _ranked(result["katz"]) == {"a": Decimal(0), "b": Decimal(0), "c": Decimal(0)}
    assert result["converged"] is True


def test_katz_accepts_decimal_alpha() -> None:
    result = compute_katz(_path_abc(), alpha=Decimal("0.5"))
    assert _ranked(result["katz"])["c"] == Decimal("0.75")


def test_clustering_single_edge_all_zero() -> None:
    result = compute_clustering(_simple(["a", "b"], [("a", "b")]))
    assert result["global"]["connected_triples"] == 0
    assert result["global"]["transitivity"] == "0.000000000000"


def test_clustering_two_disjoint_triangles() -> None:
    graph = _simple(
        ["a", "b", "c", "d", "e", "f"],
        [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e"), ("e", "f"), ("d", "f")],
    )
    result = compute_clustering(graph)
    assert result["global"]["average_coefficient"] == "1.000000000000"
    assert result["global"]["triangles"] == 2
    assert result["global"]["connected_triples"] == 6


def test_louvain_empty_graph_levels_empty() -> None:
    result = compute_louvain(_graph([], []))
    assert result["levels"] == []
    assert result["levels_used"] == 0


def test_louvain_parameters_echoed() -> None:
    result = compute_louvain(_path_abc(), max_passes=3, max_levels=2, max_nodes=10)
    assert result["parameters"] == {"max_passes": 3, "max_levels": 2, "max_nodes": 10}


def test_louvain_largest_community_gets_id_zero() -> None:
    graph = _simple(["a", "b", "c", "d", "e"], [("a", "b"), ("b", "c"), ("a", "c"), ("d", "e")])
    result = compute_louvain(graph)
    assert result["communities"][0]["members"] == ["a", "b", "c"]
    assert result["communities"][1]["members"] == ["d", "e"]


def test_louvain_tolerates_dropped_link_rows() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b"), _link("b", "c"), _link("a", "c"), _link("a", "ghost"), {"broken": True}],
    )
    result = compute_louvain(graph)
    assert result["dropped_links"] == 2
    assert result["community_count"] == 1


def test_reachability_parameters_echoed() -> None:
    result = compute_reachability(_path_abc(), max_nodes=99)
    assert result["parameters"] == {"max_nodes": 99}


def test_reachability_mutual_pair_density_one() -> None:
    result = compute_reachability(_simple(["a", "b"], [("a", "b"), ("b", "a")]))
    assert result["summary"]["density"] == "1.000000000000"
    assert result["summary"]["diameter"] == 1


def test_motifs_parameters_echoed() -> None:
    result = count_motifs(_path_abc(), max_nodes=99)
    assert result["parameters"] == {"max_nodes": 99}


def test_motifs_single_node_zero_triads() -> None:
    result = count_motifs(_simple(["solo"], []))
    assert result["triad_total"] == 0
    assert all(count == 0 for count in result["triad_census"].values())


def test_motifs_census_keys_exactly_the_sixteen_classes() -> None:
    result = count_motifs(_rich_graph())
    assert list(result["triad_census"]) == list(_TRIAD_CLASSES)


def test_assortativity_defined_run_has_no_guards() -> None:
    result = compute_assortativity(_path_abc())
    assert result["guards"] == []


def test_hits_cycle_uniform_and_converged() -> None:
    graph = _simple(["a", "b", "c"], [("a", "b"), ("b", "c"), ("c", "a")])
    result = compute_hits(graph)
    assert result["converged"] is True
    for entry in result["hubs"] + result["authorities"]:
        assert entry["score"] == "0.333333333333"


def test_eigenvector_top_none_returns_all() -> None:
    result = compute_eigenvector(_path_abc(), top=None)
    assert len(result["eigenvector"]) == 3


def test_louvain_single_isolated_node_one_singleton() -> None:
    result = compute_louvain(_simple(["solo"], []))
    assert result["community_count"] == 1
    assert result["communities"] == [{"id": 0, "size": 1, "members": ["solo"]}]


# ------------------------------------------------------------ per-domain


def _two_domain_graph() -> dict[str, Any]:
    return _graph(
        [_node("a"), _node("b"), _node("x", "P2"), _node("y", "P2"), _node("z", "P2")],
        [_link("a", "b"), _link("x", "y"), _link("y", "z")],
    )


def test_hits_domains_block_all_primary_domains() -> None:
    result = compute_hits(_two_domain_graph())
    assert set(result["domains"]) == {f"P{i}" for i in range(1, 8)}
    p1 = result["domains"]["P1"]
    assert p1["node_count"] == 2
    assert _ranked(p1["hubs"]) == {"a": Decimal(1), "b": Decimal(0)}
    assert _ranked(p1["authorities"]) == {"b": Decimal(1), "a": Decimal(0)}
    p2 = result["domains"]["P2"]
    assert _ranked(p2["authorities"]) == {"x": Decimal(0), "y": Decimal("0.5"), "z": Decimal("0.5")}
    empty = result["domains"]["P7"]
    assert empty["node_count"] == 0
    assert empty["hubs"] == []
    assert empty["converged"] is True


def test_eigenvector_domains_block_hand_computed() -> None:
    result = compute_eigenvector(_two_domain_graph())
    assert set(result["domains"]) == {f"P{i}" for i in range(1, 8)}
    p1 = result["domains"]["P1"]
    half_sqrt2 = Decimal(1) / Decimal(2).sqrt()
    for entry in p1["eigenvector"]:
        assert abs(Decimal(entry["score"]) - half_sqrt2) < Decimal("1e-11")
    assert result["domains"]["P5"]["eigenvector"] == []


def test_katz_domains_block_hand_computed() -> None:
    result = compute_katz(_two_domain_graph(), alpha="0.5", beta="1")
    p2 = result["domains"]["P2"]
    assert _ranked(p2["katz"]) == {"x": Decimal(0), "y": Decimal("0.5"), "z": Decimal("0.75")}
    p1 = result["domains"]["P1"]
    assert _ranked(p1["katz"]) == {"a": Decimal(0), "b": Decimal("0.5")}


def test_domains_blocks_share_global_parameters() -> None:
    result = compute_katz(_two_domain_graph(), alpha="0.5", beta="1", top=1)
    for domain in result["domains"].values():
        assert len(domain["katz"]) <= 1


def test_domains_blocks_deterministic_under_permutation() -> None:
    graph = _two_domain_graph()
    baseline = compute_hits(graph)
    for seed in (5, 6):
        assert compute_hits(_permuted(graph, seed))["domains"] == baseline["domains"]


def test_cap_error_string_mentions_cap_and_actual() -> None:
    graph = _simple(["a", "b", "c", "d"], [("a", "b")])
    with pytest.raises(GraphCapExceededError) as excinfo:
        count_motifs(graph, max_nodes=2)
    message = str(excinfo.value)
    assert "2" in message and "4" in message


def test_triad_classes_constant_is_complete() -> None:
    from know_your_soul.analytics.algorithms import TRIAD_CLASSES

    assert len(TRIAD_CLASSES) == 16
    assert len(set(TRIAD_CLASSES)) == 16


@pytest.mark.parametrize(
    "constant_name",
    [
        "DEFAULT_HITS_ITERATIONS",
        "DEFAULT_EIGENVECTOR_ITERATIONS",
        "DEFAULT_KATZ_ITERATIONS",
        "DEFAULT_LOUVAIN_MAX_PASSES",
        "DEFAULT_LOUVAIN_MAX_LEVELS",
        "DEFAULT_LOUVAIN_MAX_NODES",
        "DEFAULT_REACHABILITY_MAX_NODES",
        "DEFAULT_MOTIF_MAX_NODES",
    ],
)
def test_default_constants_are_positive_ints(constant_name: str) -> None:
    import know_your_soul.analytics.algorithms as algorithms

    value = getattr(algorithms, constant_name)
    assert isinstance(value, int) and value >= 1


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_hits_reversed_graph_swaps_vectors(graph: dict[str, Any]) -> None:
    reversed_graph = _graph(
        list(graph["nodes"]),
        [{"source": link["target"], "target": link["source"], "relation": link["relation"]} for link in graph["links"]],
    )
    forward = compute_hits(graph)
    backward = compute_hits(reversed_graph)
    assert forward["authorities"] == backward["hubs"]
    assert forward["hubs"] == backward["authorities"]


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_louvain_modularity_nondecreasing_across_levels(graph: dict[str, Any]) -> None:
    result = compute_louvain(graph)
    modularity = [Decimal(level["modularity"]) for level in result["levels"]]
    for previous, current in zip(modularity, modularity[1:], strict=False):
        assert current >= modularity[0] - Decimal("1e-12")
        assert current >= previous - Decimal("1e-12")


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_clustering_coefficient_matches_triangle_count(graph: dict[str, Any]) -> None:
    result = compute_clustering(graph)
    for entry in result["per_node"]:
        degree = entry["degree"]
        coefficient = Decimal(entry["coefficient"])
        if degree >= 2:
            expected = Decimal(2 * entry["triangles"]) / Decimal(degree * (degree - 1))
            assert abs(coefficient - expected) < Decimal("1e-11")
        else:
            assert coefficient == Decimal(0)


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_property_reachability_ratio_matches_counts(graph: dict[str, Any]) -> None:
    result = compute_reachability(graph)
    n = result["node_count"]
    for entry in result["per_node"]:
        expected = Decimal(entry["reachable"]) / Decimal(n - 1) if n > 1 else Decimal(0)
        assert abs(Decimal(entry["ratio"]) - expected) < Decimal("1e-11")

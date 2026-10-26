"""Tests for ``analytics.centrality`` (ATM wave A, plan F-A1).

Pins the public contract of the deterministic centrality suite:

* every score is a 12-decimal fixed-point string (spec section 9);
* rankings are sorted by ``(-score, node_id)`` — ties never depend on the
  input ordering of nodes or links;
* the betweenness sampler is seeded and guarded (``k = min(samples, n)``),
  so repeated runs and permuted inputs are byte-identical;
* the suite runs globally and per primary domain (P1..P7).
"""

from __future__ import annotations

import re
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.centrality import compute_centrality

_SCORE_RE = re.compile(r"^\d+\.\d{12}$")

_IDS = ["a", "b", "c", "d", "e", "f"]
_DOMAINS = [f"P{i}" for i in range(1, 8)]
_RELATIONS = ["depends_on", "supports", "contradicts", "parent_of", "supersedes"]


def _node(node_id: str, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain, "kind": "leaf"}


def _link(source: str, target: str, relation: str = "depends_on") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _graph(nodes: list[dict[str, Any]], links: list[dict[str, Any]]) -> dict[str, Any]:
    return {"directed": True, "nodes": list(nodes), "links": list(links), "hyperedges": []}


def _path_abc() -> dict[str, Any]:
    return _graph([_node("a"), _node("b"), _node("c")], [_link("a", "b"), _link("b", "c")])


def _scores(block: dict[str, Any], metric: str) -> dict[str, Decimal]:
    return {entry["node_id"]: Decimal(entry["score"]) for entry in block[metric]}


def _assert_all_scores_quantized(payload: Any) -> None:
    if isinstance(payload, dict):
        for value in payload.values():
            _assert_all_scores_quantized(value)
    elif isinstance(payload, list):
        for item in payload:
            _assert_all_scores_quantized(item)


# ------------------------------------------------------------------ examples


def test_empty_graph() -> None:
    result = compute_centrality(_graph([], []))
    assert result["kind"] == "analytics.centrality"
    assert result["node_count"] == 0
    assert result["link_count"] == 0
    for metric in ("degree", "betweenness", "harmonic", "closeness", "pagerank"):
        assert result["global"][metric] == []
    assert set(result["domains"]) == {f"P{i}" for i in range(1, 8)}


def test_single_node_scores_zero() -> None:
    result = compute_centrality(_graph([_node("solo")], []))
    for metric in ("degree", "betweenness", "harmonic", "closeness"):
        assert _scores(result["global"], metric) == {"solo": Decimal(0)}
    assert _scores(result["global"], "pagerank") == {"solo": Decimal(1)}


def test_path_degree_exact() -> None:
    result = compute_centrality(_path_abc())
    assert _scores(result["global"], "degree") == {
        "a": Decimal("0.5"),
        "b": Decimal("1"),
        "c": Decimal("0.5"),
    }


def test_path_harmonic_exact() -> None:
    result = compute_centrality(_path_abc())
    assert _scores(result["global"], "harmonic") == {
        "a": Decimal("0.75"),
        "b": Decimal("0.5"),
        "c": Decimal("0"),
    }


def test_path_closeness_exact() -> None:
    result = compute_centrality(_path_abc())
    scores = _scores(result["global"], "closeness")
    assert scores["a"] == Decimal("0.666666666667")
    assert scores["b"] == Decimal("0.5")
    assert scores["c"] == Decimal(0)


def test_path_betweenness_exact_when_unsampled() -> None:
    # k = min(samples, n) = 3 == n, so no sampling guard fires and the
    # directed path gives the middle node exactly 1/((n-1)(n-2)) = 1/2.
    result = compute_centrality(_path_abc(), betweenness_samples=10)
    assert "betweenness_sampled" not in result["guards"]
    assert _scores(result["global"], "betweenness") == {
        "a": Decimal(0),
        "b": Decimal("0.5"),
        "c": Decimal(0),
    }


def test_triangle_pagerank_is_uniform() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b"), _link("b", "c"), _link("c", "a")],
    )
    result = compute_centrality(graph)
    assert _scores(result["global"], "pagerank") == {
        "a": Decimal("0.333333333333"),
        "b": Decimal("0.333333333333"),
        "c": Decimal("0.333333333333"),
    }


def test_path_pagerank_order_and_mass() -> None:
    result = compute_centrality(_path_abc())
    scores = _scores(result["global"], "pagerank")
    assert scores["c"] > scores["b"] > scores["a"]
    assert abs(sum(scores.values()) - Decimal(1)) < Decimal("1e-9")


def test_ranking_tie_breaks_on_node_id() -> None:
    # Two sinks with identical scores: 'aa' must precede 'zz' everywhere.
    graph = _graph(
        [_node("zz"), _node("aa"), _node("m")],
        [_link("m", "zz"), _link("m", "aa")],
    )
    result = compute_centrality(graph)
    ids = [entry["node_id"] for entry in result["global"]["degree"]]
    assert ids == ["m", "aa", "zz"]


def test_scores_are_12_decimal_strings() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("d")],
        [_link("a", "b"), _link("b", "c"), _link("c", "a"), _link("c", "d")],
    )
    result = compute_centrality(graph, betweenness_samples=2)
    for block in (result["global"], *result["domains"].values()):
        for metric in ("degree", "betweenness", "harmonic", "closeness", "pagerank"):
            for entry in block[metric]:
                assert _SCORE_RE.match(entry["score"]), entry
    _assert_all_scores_quantized(result["global"])


def test_per_domain_blocks_are_induced_subgraphs() -> None:
    graph = _graph(
        [_node("a", "P1"), _node("b", "P1"), _node("c", "P2"), _node("d", "P3")],
        [_link("a", "b"), _link("b", "c"), _link("c", "d")],
    )
    result = compute_centrality(graph)
    p1_ids = [entry["node_id"] for entry in result["domains"]["P1"]["degree"]]
    assert p1_ids == ["a", "b"]
    assert result["domains"]["P2"]["node_count"] == 1
    assert result["domains"]["P7"]["node_count"] == 0
    assert result["domains"]["P7"]["pagerank"] == []


def test_betweenness_sampling_guard_and_determinism() -> None:
    nodes = [_node(f"n{i:02d}") for i in range(12)]
    links = [_link(f"n{i:02d}", f"n{i + 1:02d}") for i in range(11)]
    graph = _graph(nodes, links)
    first = compute_centrality(graph, betweenness_samples=3)
    second = compute_centrality(graph, betweenness_samples=3)
    assert "betweenness_sampled" in first["guards"]
    assert first == second


def test_top_limits_ranking_length() -> None:
    result = compute_centrality(_path_abc(), top=2)
    assert len(result["global"]["degree"]) == 2
    assert [entry["node_id"] for entry in result["global"]["degree"]] == ["b", "a"]


def test_dropped_links_are_counted() -> None:
    graph = _graph(
        [_node("a"), _node("b")],
        [
            _link("a", "b"),
            _link("a", "b"),  # duplicate
            _link("a", "ghost"),  # unknown endpoint
            _link("a", "a"),  # self loop
            {"source": "a"},  # malformed
        ],
    )
    result = compute_centrality(graph)
    assert result["link_count"] == 1
    assert result["dropped_links"] == 4


def test_invalid_parameters_raise() -> None:
    graph = _path_abc()
    with pytest.raises(ValueError):
        compute_centrality(graph, betweenness_samples=0)
    with pytest.raises(ValueError):
        compute_centrality(graph, pagerank_iterations=0)
    with pytest.raises(ValueError):
        compute_centrality(graph, closeness_cutoff=0)
    with pytest.raises(ValueError):
        compute_centrality(graph, top=0)


def test_repeated_runs_are_identical() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c"), _node("d"), _node("e")],
        [_link("a", "b"), _link("b", "c"), _link("c", "d"), _link("d", "e"), _link("e", "a")],
    )
    assert compute_centrality(graph, betweenness_samples=2) == compute_centrality(graph, betweenness_samples=2)


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
    baseline = compute_centrality(graph, betweenness_samples=4)
    node_perm = data.draw(st.permutations(list(range(len(graph["nodes"]))))) if graph["nodes"] else []
    link_perm = data.draw(st.permutations(list(range(len(graph["links"]))))) if graph["links"] else []
    shuffled = {
        "directed": True,
        "nodes": [graph["nodes"][i] for i in node_perm],
        "links": [graph["links"][i] for i in link_perm],
        "hyperedges": [],
    }
    assert compute_centrality(shuffled, betweenness_samples=4) == baseline


@given(graph=_random_graphs())
@settings(derandomize=True, max_examples=30, deadline=None)
def test_rankings_are_strictly_ordered(graph: dict[str, Any]) -> None:
    result = compute_centrality(graph, betweenness_samples=4)
    for metric in ("degree", "betweenness", "harmonic", "closeness", "pagerank"):
        keys = [(-Decimal(entry["score"]), entry["node_id"]) for entry in result["global"][metric]]
        assert keys == sorted(keys)
        for entry in result["global"][metric]:
            assert _SCORE_RE.match(entry["score"])

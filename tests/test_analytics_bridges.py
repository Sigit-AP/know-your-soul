"""Tests for ``know_your_soul.analytics.bridges`` (ATM F-A5).

Pins the "surprising connection" contract for knowledge graphs: candidate
edges cross primary domains and/or communities, the surprise score is our own
deterministic composition (community distance + relation rarity + evidence
novelty), and output ordering is canonical with explicit tie-breaks.
"""

from __future__ import annotations

from decimal import Decimal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.bridges import (
    DEFAULT_TOP_N,
    find_bridges,
)
from know_your_soul.domain import canonical_json

# ------------------------------------------------------------------ helpers


def _node(node_id: str, *, domain: str = "P1", version: int = 1) -> dict[str, object]:
    return {"id": node_id, "domain": domain, "version": version, "label": node_id}


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, object]:
    return {"source": source, "target": target, "relation": relation, "confidence": "EXTRACTED"}


def _canonical(value: object) -> str:
    return canonical_json(value)


# --------------------------------------------------------------- candidates


class TestCandidates:
    def test_cross_domain_edge_is_a_bridge(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P7")]
        links = [_link("a", "b")]
        result = find_bridges(nodes, links)
        assert len(result["bridges"]) == 1
        bridge = result["bridges"][0]
        assert bridge["source"] == "a"
        assert bridge["target"] == "b"
        assert bridge["kind"] == "cross_domain"

    def test_intra_domain_intra_community_edges_excluded(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("b", "c")]
        result = find_bridges(nodes, links)
        assert result["bridges"] == []

    def test_cross_community_same_domain(self) -> None:
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b")]
        result = find_bridges(nodes, links, membership={"a": 0, "b": 1})
        assert result["bridges"][0]["kind"] == "cross_community"

    def test_cross_domain_and_community(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P7")]
        links = [_link("a", "b")]
        result = find_bridges(nodes, links, membership={"a": 0, "b": 1})
        assert result["bridges"][0]["kind"] == "cross_domain_and_community"

    def test_self_loop_never_a_bridge(self) -> None:
        nodes = [_node("a", domain="P1")]
        links = [_link("a", "a")]
        result = find_bridges(nodes, links)
        assert result["bridges"] == []

    def test_edge_referencing_unknown_node_skipped(self) -> None:
        nodes = [_node("a", domain="P1")]
        links = [_link("a", "ghost")]
        result = find_bridges(nodes, links)
        assert result["bridges"] == []

    def test_empty_graph(self) -> None:
        result = find_bridges([], [])
        assert result["bridges"] == []
        assert result["counts"]["candidates"] == 0


# ------------------------------------------------------------------- score


class TestSurpriseScore:
    def test_score_is_quantized_string(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P2")]
        links = [_link("a", "b")]
        bridge = find_bridges(nodes, links)["bridges"][0]
        score = bridge["surprise_score"]
        assert isinstance(score, str)
        assert "." in score and len(score.split(".")[1]) == 12
        assert Decimal("0") <= Decimal(score) <= Decimal("1")

    def test_unreachable_communities_outscore_adjacent(self) -> None:
        # Community distance is measured with the candidate edge's own
        # instance removed. b-e is the ONLY connection between communities 0
        # and 2 (unreachable -> distance -1), while 0 and 1 stay directly
        # connected via a-d when b-c is ignored (distance 1).
        nodes = [
            _node("a", domain="P1"),
            _node("b", domain="P1"),
            _node("c", domain="P2"),
            _node("d", domain="P2"),
            _node("e", domain="P3"),
        ]
        links = [
            _link("a", "b"),
            _link("c", "d"),
            _link("b", "c"),  # bridge 0-1, alternative path exists via a-d
            _link("a", "d"),  # bridge 0-1, alternative path exists via b-c
            _link("b", "e"),  # bridge 0-2, only connection
        ]
        membership = {"a": 0, "b": 0, "c": 1, "d": 1, "e": 2}
        result = find_bridges(nodes, links, membership=membership)
        by_target = {(b["source"], b["target"]): b for b in result["bridges"]}
        adjacent = by_target[("b", "c")]
        unreachable = by_target[("b", "e")]
        assert unreachable["components"]["community_distance"] == -1
        assert adjacent["components"]["community_distance"] == 1
        assert Decimal(unreachable["surprise_score"]) > Decimal(adjacent["surprise_score"])

    def test_rarer_relation_scores_higher(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P2"), _node("c", domain="P3"), _node("d", domain="P4")]
        links = [
            _link("a", "b", relation="supports"),
            _link("c", "d", relation="contradicts"),
            _link("a", "a", relation="supports"),  # self-loop, still counted for rarity base
            _link("b", "b", relation="supports"),
        ]
        result = find_bridges(nodes, links)
        by_rel = {b["relation"]: b for b in result["bridges"]}
        assert Decimal(by_rel["contradicts"]["components"]["relation_rarity"]) > Decimal(
            by_rel["supports"]["components"]["relation_rarity"]
        )
        assert Decimal(by_rel["contradicts"]["surprise_score"]) > Decimal(by_rel["supports"]["surprise_score"])

    def test_newer_endpoint_raises_novelty(self) -> None:
        nodes = [
            _node("old1", domain="P1", version=9),
            _node("old2", domain="P2", version=9),
            _node("new1", domain="P3", version=1),
        ]
        links = [_link("old1", "old2"), _link("old1", "new1")]
        result = find_bridges(nodes, links)
        by_target = {b["target"]: b for b in result["bridges"]}
        assert Decimal(by_target["new1"]["components"]["evidence_novelty"]) > Decimal(
            by_target["old2"]["components"]["evidence_novelty"]
        )

    def test_score_deterministic_under_input_permutation(self) -> None:
        nodes = [
            _node("a", domain="P1"),
            _node("b", domain="P2"),
            _node("c", domain="P3"),
            _node("d", domain="P1", version=3),
        ]
        links = [
            _link("a", "b"),
            _link("a", "c", relation="contradicts"),
            _link("b", "d"),
            _link("c", "d", relation="derived_from"),
        ]
        membership = {"a": 0, "b": 1, "c": 2, "d": 0}
        base = _canonical(find_bridges(nodes, links, membership=membership))
        permuted = _canonical(find_bridges(list(reversed(nodes)), list(reversed(links)), membership=membership))
        assert permuted == base


# ----------------------------------------------------------------- ordering


class TestOrdering:
    def _graph(self) -> tuple[list[dict[str, object]], list[dict[str, object]]]:
        nodes = [
            _node("a", domain="P1"),
            _node("b", domain="P2"),
            _node("c", domain="P3"),
            _node("e", domain="P4"),
        ]
        links = [
            _link("a", "b", relation="supports"),
            _link("a", "c", relation="contradicts"),
            _link("a", "e", relation="learned_from"),
        ]
        return nodes, links

    def test_sorted_by_score_desc_then_canonical_key(self) -> None:
        nodes, links = self._graph()
        result = find_bridges(nodes, links)
        keys = [(-Decimal(b["surprise_score"]), b["source"], b["target"], b["relation"]) for b in result["bridges"]]
        assert keys == sorted(keys)

    def test_top_n_bounds_output(self) -> None:
        nodes, links = self._graph()
        result = find_bridges(nodes, links, top_n=2)
        assert len(result["bridges"]) == 2
        assert result["counts"]["candidates"] == 3
        assert result["counts"]["emitted"] == 2

    def test_default_top_n(self) -> None:
        nodes, links = self._graph()
        result = find_bridges(nodes, links)
        assert result["top_n"] == DEFAULT_TOP_N

    def test_counts_by_kind(self) -> None:
        nodes = [
            _node("a", domain="P1"),
            _node("b", domain="P2"),
            _node("c", domain="P1"),
        ]
        links = [_link("a", "b"), _link("a", "c")]
        membership = {"a": 0, "b": 1, "c": 2}
        result = find_bridges(nodes, links, membership=membership)
        assert result["counts"]["by_kind"] == {"cross_community": 1, "cross_domain_and_community": 1}


# ---------------------------------------------------------------- property


@settings(derandomize=True, max_examples=50)
@given(
    n=st.integers(min_value=0, max_value=7),
    edges=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=6),
            st.integers(min_value=0, max_value=6),
            st.sampled_from(["supports", "contradicts", "derived_from"]),
        ),
        max_size=12,
    ),
    permutation=st.permutations(range(7)),
)
def test_find_bridges_permutation_invariant(
    n: int, edges: list[tuple[int, int, str]], permutation: tuple[int, ...]
) -> None:
    ids = [f"n{i}" for i in range(n)]
    domains = ["P1", "P2", "P3", "P4", "P5", "P6", "P7"]
    nodes = [_node(i, domain=domains[int(i[1]) % 7]) for i in ids]
    links = [_link(ids[a], ids[b], relation=r) for a, b, r in edges if a < n and b < n]
    membership = {i: int(i[1]) % 3 for i in ids}
    base = _canonical(find_bridges(nodes, links, membership=membership))
    # True bijections over the indexes derived from the drawn permutation —
    # filtering by size would silently drop elements (see communities tests).
    node_order = sorted(range(n), key=lambda i: (permutation[i % len(permutation)], i))
    link_order = sorted(range(len(links)), key=lambda i: (permutation[i % len(permutation)], i))
    permuted_nodes = [nodes[p] for p in node_order]
    permuted_links = [links[p] for p in link_order]
    assert _canonical(find_bridges(permuted_nodes, permuted_links, membership=membership)) == base


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

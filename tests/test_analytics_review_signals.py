"""Tests for ``know_your_soul.analytics.review_signals`` (ATM F-A6).

Pins the review-signal catalog: low-confidence edges, isolated nodes,
low-cohesion communities, stale-evidence nodes (superseded source versions),
and nodes without provenance; plus the round-robin quota diversification and
the ``low_confidence`` hint flag. Output items carry reason codes and
source+version references so a future ``service.review`` consumer can adopt
them without reshaping (this module is pure — it never touches the store).
"""

from __future__ import annotations

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.review_signals import (
    CATALOG_ORDER,
    collect_review_signals,
)
from know_your_soul.domain import canonical_json

# ------------------------------------------------------------------ helpers


def _node(
    node_id: str,
    *,
    version: int = 1,
    source_version_id: str = "sv-1",
    provenance: list[str] | None = None,
) -> dict[str, object]:
    return {
        "id": node_id,
        "label": node_id,
        "domain": "P1",
        "version": version,
        "source_version_id": source_version_id,
        "provenance": ["origin.md"] if provenance is None else provenance,
    }


def _link(source: str, target: str, *, confidence: str = "EXTRACTED") -> dict[str, object]:
    return {"source": source, "target": target, "relation": "supports", "confidence": confidence}


def _community(cid: int, members: list[str], cohesion: str) -> dict[str, object]:
    return {"id": cid, "members": sorted(members), "cohesion": cohesion, "size": len(members)}


def _canonical(value: object) -> str:
    return canonical_json(value)


# ------------------------------------------------------------------ signals


class TestSignalCatalog:
    def test_catalog_order_fixed(self) -> None:
        assert CATALOG_ORDER == (
            "low_confidence_edge",
            "isolated_node",
            "low_cohesion_community",
            "stale_evidence",
            "missing_provenance",
        )

    def test_low_confidence_edge_fires_on_ambiguous(self) -> None:
        nodes = [_node("a", version=2), _node("b", version=3)]
        links = [_link("a", "b", confidence="AMBIGUOUS")]
        result = collect_review_signals(nodes, links)
        assert result["signal_counts"]["low_confidence_edge"] == 1
        item = result["items"][0]
        assert item["signal"] == "low_confidence_edge"
        assert item["reason_codes"] == ["LOW_CONFIDENCE_EDGE"]
        assert item["source_node_ids"] == ["a", "b"]
        assert item["source_node_versions"] == [2, 3]
        assert item["low_confidence"] is False

    def test_extracted_edge_does_not_fire(self) -> None:
        nodes = [_node("a"), _node("b")]
        result = collect_review_signals(nodes, [_link("a", "b")])
        assert result["signal_counts"]["low_confidence_edge"] == 0

    def test_isolated_node_fires(self) -> None:
        nodes = [_node("linked-a"), _node("linked-b"), _node("orphan", version=4)]
        links = [_link("linked-a", "linked-b")]
        result = collect_review_signals(nodes, links)
        assert result["signal_counts"]["isolated_node"] == 1
        item = result["items"][0]
        assert item["signal"] == "isolated_node"
        assert item["reason_codes"] == ["ISOLATED_NODE"]
        assert item["source_node_ids"] == ["orphan"]
        assert item["source_node_versions"] == [4]
        assert item["low_confidence"] is False

    def test_self_loop_only_node_is_isolated(self) -> None:
        nodes = [_node("solo")]
        links = [_link("solo", "solo")]
        result = collect_review_signals(nodes, links)
        assert result["signal_counts"]["isolated_node"] == 1

    def test_low_cohesion_community_fires_below_threshold(self) -> None:
        nodes = [_node(c) for c in "abc"]
        links = [_link("a", "b"), _link("b", "c")]
        communities = [_community(0, ["a", "b", "c"], "0.100000000000")]
        result = collect_review_signals(nodes, links, communities=communities)
        assert result["signal_counts"]["low_cohesion_community"] == 1
        item = result["items"][0]
        assert item["signal"] == "low_cohesion_community"
        assert item["reason_codes"] == ["LOW_COHESION_COMMUNITY"]
        assert item["source_node_ids"] == ["a", "b", "c"]
        assert item["source_node_versions"] == [1, 1, 1]
        assert item["low_confidence"] is True
        assert item["detail"]["cohesion"] == "0.100000000000"

    def test_cohesion_at_or_above_threshold_quiet(self) -> None:
        nodes = [_node(c) for c in "abc"]
        links = [_link("a", "b"), _link("b", "c")]
        communities = [_community(0, ["a", "b", "c"], "0.150000000000")]
        result = collect_review_signals(nodes, links, communities=communities)
        assert result["signal_counts"]["low_cohesion_community"] == 0

    def test_tiny_communities_not_flagged(self) -> None:
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b")]
        communities = [_community(0, ["a", "b"], "0.000000000000")]
        result = collect_review_signals(nodes, links, communities=communities)
        assert result["signal_counts"]["low_cohesion_community"] == 0

    def test_stale_evidence_requires_current_versions(self) -> None:
        nodes = [_node("a", source_version_id="sv-old"), _node("b", source_version_id="sv-new")]
        links = [_link("a", "b")]
        # Without the registry view the signal stays silent (fail-closed).
        silent = collect_review_signals(nodes, links)
        assert silent["signal_counts"]["stale_evidence"] == 0
        result = collect_review_signals(nodes, links, current_source_versions={"sv-new"})
        assert result["signal_counts"]["stale_evidence"] == 1
        item = result["items"][0]
        assert item["signal"] == "stale_evidence"
        assert item["reason_codes"] == ["STALE_EVIDENCE"]
        assert item["source_node_ids"] == ["a"]
        assert item["low_confidence"] is True
        assert item["detail"]["source_version_id"] == "sv-old"

    def test_missing_provenance_fires(self) -> None:
        nodes = [_node("a", provenance=[]), _node("b")]
        links = [_link("a", "b")]
        result = collect_review_signals(nodes, links)
        assert result["signal_counts"]["missing_provenance"] == 1
        item = result["items"][0]
        assert item["signal"] == "missing_provenance"
        assert item["reason_codes"] == ["MISSING_PROVENANCE"]
        assert item["source_node_ids"] == ["a"]
        assert item["low_confidence"] is True

    def test_empty_graph_no_signals(self) -> None:
        result = collect_review_signals([], [])
        assert all(count == 0 for count in result["signal_counts"].values())
        assert result["items"] == []


# ------------------------------------------------------------- round-robin


class TestRoundRobin:
    def test_quota_interleaves_signal_types(self) -> None:
        nodes = [_node(f"iso{i}") for i in range(4)] + [_node("e1a"), _node("e1b"), _node("e2a"), _node("e2b")]
        links = [_link("e1a", "e1b", confidence="AMBIGUOUS"), _link("e2a", "e2b", confidence="AMBIGUOUS")]
        result = collect_review_signals(nodes, links, quota=4)
        assert result["signal_counts"]["isolated_node"] == 4
        assert result["signal_counts"]["low_confidence_edge"] == 2
        signals = [item["signal"] for item in result["items"]]
        # Round-robin in catalog order: edge, node, edge, node.
        assert signals == ["low_confidence_edge", "isolated_node", "low_confidence_edge", "isolated_node"]
        assert result["emitted_counts"] == {"low_confidence_edge": 2, "isolated_node": 2}

    def test_exhausted_type_yields_to_others(self) -> None:
        nodes = [_node(f"iso{i}") for i in range(5)] + [_node("a"), _node("b")]
        links = [_link("a", "b", confidence="AMBIGUOUS")]
        result = collect_review_signals(nodes, links, quota=4)
        signals = [item["signal"] for item in result["items"]]
        assert signals == ["low_confidence_edge", "isolated_node", "isolated_node", "isolated_node"]

    def test_zero_quota_emits_nothing_but_counts(self) -> None:
        nodes = [_node("orphan")]
        result = collect_review_signals(nodes, [], quota=0)
        assert result["items"] == []
        assert result["signal_counts"]["isolated_node"] == 1
        assert result["emitted_counts"] == {}

    def test_signal_counts_cover_whole_catalog(self) -> None:
        result = collect_review_signals([_node("a")], [])
        assert tuple(result["signal_counts"].keys()) == CATALOG_ORDER


# ------------------------------------------------------------- determinism


class TestDeterminism:
    def _fixture(self) -> tuple[list[dict[str, object]], list[dict[str, object]], list[dict[str, object]]]:
        nodes = [
            _node("n1", version=2, provenance=[]),
            _node("n2", source_version_id="sv-old"),
            _node("n3"),
            _node("n4"),
            _node("orphan"),
        ]
        links = [
            _link("n1", "n3", confidence="AMBIGUOUS"),
            _link("n2", "n3"),
            _link("n3", "n4"),
        ]
        communities = [_community(0, ["n1", "n2", "n3"], "0.050000000000")]
        return nodes, links, communities

    def test_canonical_output_stable(self) -> None:
        nodes, links, communities = self._fixture()
        first = collect_review_signals(nodes, links, communities=communities, current_source_versions={"sv-1"})
        second = collect_review_signals(nodes, links, communities=communities, current_source_versions={"sv-1"})
        assert _canonical(first) == _canonical(second)

    def test_permutation_invariant(self) -> None:
        nodes, links, communities = self._fixture()
        base = _canonical(collect_review_signals(nodes, links, communities=communities))
        permuted = _canonical(
            collect_review_signals(list(reversed(nodes)), list(reversed(links)), communities=communities)
        )
        assert permuted == base


@settings(derandomize=True, max_examples=40)
@given(
    n=st.integers(min_value=0, max_value=6),
    edges=st.lists(
        st.tuples(
            st.integers(min_value=0, max_value=5),
            st.integers(min_value=0, max_value=5),
            st.sampled_from(["EXTRACTED", "INFERRED", "AMBIGUOUS"]),
        ),
        max_size=10,
    ),
    quota=st.integers(min_value=0, max_value=10),
    permutation=st.permutations(range(6)),
)
def test_collect_permutation_invariant(
    n: int, edges: list[tuple[int, int, str]], quota: int, permutation: tuple[int, ...]
) -> None:
    ids = [f"n{i}" for i in range(n)]
    nodes = [_node(i) for i in ids]
    links = [_link(ids[a], ids[b], confidence=c) for a, b, c in edges if a < n and b < n]
    base = _canonical(collect_review_signals(nodes, links, quota=quota))
    # True bijections over the indexes derived from the drawn permutation —
    # filtering by size would silently drop elements (see communities tests).
    node_order = sorted(range(n), key=lambda i: (permutation[i % len(permutation)], i))
    link_order = sorted(range(len(links)), key=lambda i: (permutation[i % len(permutation)], i))
    permuted_nodes = [nodes[p] for p in node_order]
    permuted_links = [links[p] for p in link_order]
    assert _canonical(collect_review_signals(permuted_nodes, permuted_links, quota=quota)) == base


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(pytest.main([__file__, "-q"]))

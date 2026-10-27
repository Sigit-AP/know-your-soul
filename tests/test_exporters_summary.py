"""Tests for ``know_your_soul.exporters.summary`` (ATM wave-2 formats).

Pins the canonical ``analytics/graph-summary.json`` contract: node/edge/
self-loop/isolated counts, a degree histogram (total degree over the
deduplicated edge set, self-loop contributes 2), min/max/mean degree as
quantized 12-decimal strings, per-domain breakdown (nodes, internal edges,
boundary in/out), per-relation counts — canonical JSON, byte-exact
round-trip with structural invariants, explicit fail-closed cap.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportRegistry,
    ExportRoundTripError,
    produce_exports,
)
from know_your_soul.exporters.summary import (
    SUMMARY_MAX_BYTES,
    build_summary,
    produce_summary,
    register_summary,
    roundtrip_summary,
)

# ------------------------------------------------------------------ helpers


def _node(node_id: str, *, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain}


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_top_level_shape(self) -> None:
        built = build_summary(_model([]))
        assert built["artifact"] == "graph-summary"
        assert built["schema_version"] == 1
        assert built["counts"] == {
            "nodes": 0,
            "edges": 0,
            "self_loops": 0,
            "isolated_nodes": 0,
            "dangling_edges": 0,
            "duplicate_edges": 0,
        }
        assert built["degree"] == {
            "histogram": {},
            "min_total": 0,
            "max_total": 0,
            "mean_total": "0.000000000000",
        }
        assert built["domains"] == {}
        assert built["relations"] == {}

    def test_counts(self) -> None:
        links = [_link("a", "b"), _link("a", "a"), _link("a", "ghost"), _link("a", "b")]
        built = build_summary(_model([_node("a"), _node("b"), _node("c")], links))
        assert built["counts"] == {
            "nodes": 3,
            "edges": 2,
            "self_loops": 1,
            "isolated_nodes": 1,
            "dangling_edges": 1,
            "duplicate_edges": 1,
        }

    def test_degree_histogram_self_loop_contributes_two(self) -> None:
        links = [_link("a", "a")]
        built = build_summary(_model([_node("a"), _node("b")], links))
        assert built["degree"]["histogram"] == {"0": 1, "2": 1}

    def test_degree_stats_quantized(self) -> None:
        links = [_link("a", "b"), _link("a", "c")]
        built = build_summary(_model([_node("a"), _node("b"), _node("c")], links))
        # degrees: a=2, b=1, c=1 → min 1, max 2, mean 4/3
        assert built["degree"]["min_total"] == 1
        assert built["degree"]["max_total"] == 2
        assert built["degree"]["mean_total"] == "1.333333333333"

    def test_domain_breakdown(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P1"), _node("c", domain="P2")]
        links = [_link("a", "b"), _link("a", "c"), _link("c", "a", relation="depends_on")]
        built = build_summary(_model(nodes, links))
        assert built["domains"] == {
            "P1": {"nodes": 2, "internal_edges": 1, "boundary_in": 1, "boundary_out": 1},
            "P2": {"nodes": 1, "internal_edges": 0, "boundary_in": 1, "boundary_out": 1},
        }

    def test_relation_counts(self) -> None:
        links = [_link("a", "b"), _link("a", "c", relation="depends_on"), _link("b", "c")]
        built = build_summary(_model([_node("a"), _node("b"), _node("c")], links))
        assert built["relations"] == {"depends_on": 1, "supports": 2}

    def test_unknown_domain_bucket(self) -> None:
        built = build_summary(_model([_node("a", domain=""), _node("b", domain="PX")]))
        assert set(built["domains"]) == {"PX", "unknown"}

    def test_cross_domain_edges_counted_once(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P2")]
        built = build_summary(_model(nodes, [_link("a", "b"), _link("b", "a", relation="depends_on")]))
        assert built["counts"]["edges"] == 2
        assert built["domains"]["P1"] == {"nodes": 1, "internal_edges": 0, "boundary_in": 1, "boundary_out": 1}
        assert built["domains"]["P2"] == {"nodes": 1, "internal_edges": 0, "boundary_in": 1, "boundary_out": 1}

    def test_bidi_stripped_from_domain(self) -> None:
        built = build_summary(_model([_node("a", domain="P\u202e1")]))
        assert set(built["domains"]) == {"P1"}

    def test_produced_bytes_are_canonical_json_with_newline(self) -> None:
        data = produce_summary(_model([_node("a")]))
        assert data.endswith(b"\n")
        assert (
            data
            == json.dumps(json.loads(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        )


# --------------------------------------------------------------- determinism


class TestDeterminism:
    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a", domain="P2"), _node("b", domain="P1"), _node("c", domain="P1")]
        links = [_link("a", "b"), _link("b", "c", relation="depends_on")]
        assert produce_summary(_model(nodes, links)) == produce_summary(
            _model(list(reversed(nodes)), list(reversed(links)))
        )

    def test_malformed_rows_counted_conservatively(self) -> None:
        nodes: list[Any] = [{"id": "a"}, "junk", {"id": "b"}]
        links: list[Any] = [{"source": "a", "target": "b", "relation": "supports"}, "junk"]
        built = build_summary(_model(nodes, links))
        assert built["counts"]["nodes"] == 2
        assert built["counts"]["edges"] == 1


# ------------------------------------------------------------------------ cap


class TestCaps:
    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="graph-summary"):
            produce_summary(_model([_node("a" * 100)]), max_bytes=64)

    def test_default_cap_is_explicit(self) -> None:
        assert SUMMARY_MAX_BYTES > 0

    def test_invalid_cap_rejected(self) -> None:
        with pytest.raises(ExportError):
            produce_summary(_model([]), max_bytes=0)


# ---------------------------------------------------------------- round-trip


def _canonical(payload: dict[str, Any]) -> bytes:
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"


class TestRoundTrip:
    def test_valid_artifact_passes(self) -> None:
        data = produce_summary(_model([_node("a"), _node("b")], [_link("a", "b")]))
        roundtrip_summary(data)

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_summary(b"\xff\xfe{")

    def test_non_object_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="object"):
            roundtrip_summary(b"[1,2,3]\n")

    def test_non_canonical_key_order_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="canonical"):
            roundtrip_summary(b'{"zz":1,"artifact":"graph-summary"}\n')

    def test_histogram_sum_mismatch_fails(self) -> None:
        payload = json.loads(produce_summary(_model([_node("a"), _node("b")], [_link("a", "b")])))
        payload["counts"]["nodes"] = 5
        with pytest.raises(ExportRoundTripError, match="histogram"):
            roundtrip_summary(_canonical(payload))

    def test_degree_edge_sum_mismatch_fails(self) -> None:
        payload = json.loads(produce_summary(_model([_node("a"), _node("b")], [_link("a", "b")])))
        payload["counts"]["edges"] = 3
        with pytest.raises(ExportRoundTripError, match="degree"):
            roundtrip_summary(_canonical(payload))

    def test_domain_node_sum_mismatch_fails(self) -> None:
        payload = json.loads(produce_summary(_model([_node("a")])))
        payload["domains"]["P1"]["nodes"] = 9
        with pytest.raises(ExportRoundTripError, match="domain"):
            roundtrip_summary(_canonical(payload))

    def test_boundary_symmetry_mismatch_fails(self) -> None:
        nodes = [_node("a", domain="P1"), _node("b", domain="P2")]
        payload = json.loads(produce_summary(_model(nodes, [_link("a", "b")])))
        payload["domains"]["P2"]["boundary_in"] = 7
        with pytest.raises(ExportRoundTripError, match="boundary"):
            roundtrip_summary(_canonical(payload))

    def test_relation_sum_mismatch_fails(self) -> None:
        payload = json.loads(produce_summary(_model([_node("a"), _node("b")], [_link("a", "b")])))
        payload["relations"]["supports"] = 4
        with pytest.raises(ExportRoundTripError, match="relation"):
            roundtrip_summary(_canonical(payload))

    def test_non_quantized_mean_fails(self) -> None:
        payload = json.loads(produce_summary(_model([_node("a"), _node("b")], [_link("a", "b")])))
        payload["degree"]["mean_total"] = "1.5"
        with pytest.raises(ExportRoundTripError, match="quantized|12"):
            roundtrip_summary(_canonical(payload))


# ----------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_summary_adds_analytics_entry(self) -> None:
        registry = ExportRegistry()
        register_summary(registry)
        assert "analytics/graph-summary.json" in registry.names(kind="analytics")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_summary(registry)
        artifacts = produce_exports(registry, _model([_node("a")]))
        assert b'"artifact":"graph-summary"' in artifacts["analytics/graph-summary.json"]


# --------------------------------------------------------------- properties


_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"),
    min_size=1,
    max_size=10,
)


@settings(derandomize=True, max_examples=25)
@given(
    ids=st.lists(_IDENTIFIER, min_size=0, max_size=8, unique=True),
    edges=st.lists(st.integers(0, 15), min_size=0, max_size=8),
    domain_shift=st.integers(1, 3),
)
def test_property_deterministic_roundtrip_and_invariants(ids: list[str], edges: list[int], domain_shift: int) -> None:
    nodes = [_node(node_id, domain=f"P{(index % 3) + domain_shift}") for index, node_id in enumerate(ids)]
    links = [_link(ids[i % len(ids)], ids[(i * 2 + 1) % len(ids)]) for i in edges if ids]
    first = produce_summary(_model(nodes, links))
    shuffled = produce_summary(_model(list(reversed(nodes)), list(reversed(links))))
    assert first == shuffled
    roundtrip_summary(first)
    built = json.loads(first)
    histogram = built["degree"]["histogram"]
    assert sum(histogram.values()) == built["counts"]["nodes"]
    assert sum(int(degree) * count for degree, count in histogram.items()) == 2 * built["counts"]["edges"]
    assert sum(domain["nodes"] for domain in built["domains"].values()) == built["counts"]["nodes"]

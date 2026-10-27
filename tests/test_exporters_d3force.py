"""Tests for ``know_your_soul.exporters.d3force`` (ATM wave-2 formats).

Pins the canonical D3-force node-link JSON contract: ``nodes`` carry an
``index`` matching their sorted position and a ``group`` per domain;
``links`` reference nodes by those integer indices and are sorted by
(source, target, relation); labels sanitized through
``security.sanitize_text``; canonical-JSON byte-exact round-trip; explicit
fail-closed caps.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.d3force import (
    D3FORCE_LABEL_MAX_CHARS,
    D3FORCE_MAX_BYTES,
    D3FORCE_MAX_EDGES,
    D3FORCE_MAX_NODES,
    build_d3force,
    produce_d3force,
    register_d3force,
    roundtrip_d3force,
)
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportRegistry,
    ExportRoundTripError,
    produce_exports,
)

# ------------------------------------------------------------------ helpers


def _node(
    node_id: str,
    *,
    domain: str = "P1",
    label: str | None = None,
    kind: str = "primary",
) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "domain": domain, "kind": kind}
    if label is not None:
        node["label"] = label
    return node


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_top_level_shape(self) -> None:
        built = build_d3force(_model([]))
        assert built["artifact"] == "d3-force"
        assert built["schema_version"] == 1
        assert built["nodes"] == []
        assert built["links"] == []

    def test_node_fields_and_index(self) -> None:
        built = build_d3force(_model([_node("b"), _node("a", label="Alpha", domain="P3")]))
        assert built["nodes"] == [
            {"id": "a", "index": 0, "label": "Alpha", "domain": "P3", "group": "P3"},
            {"id": "b", "index": 1, "label": "b", "domain": "P1", "group": "P1"},
        ]

    def test_links_reference_indices(self) -> None:
        built = build_d3force(_model([_node("a"), _node("b")], [_link("b", "a", relation="depends_on")]))
        assert built["links"] == [{"index": 0, "source": 1, "target": 0, "relation": "depends_on"}]

    def test_links_sorted_with_sequential_link_index(self) -> None:
        links = [_link("b", "c"), _link("a", "c"), _link("a", "b", relation="depends_on")]
        built = build_d3force(_model([_node("a"), _node("b"), _node("c")], links))
        keys = [(link["source"], link["target"], link["relation"]) for link in built["links"]]
        assert keys == [(0, 1, "depends_on"), (0, 2, "supports"), (1, 2, "supports")]
        assert [link["index"] for link in built["links"]] == [0, 1, 2]

    def test_produced_bytes_are_canonical_json_with_newline(self) -> None:
        data = produce_d3force(_model([_node("a")]))
        assert data.endswith(b"\n")
        assert (
            data
            == json.dumps(json.loads(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        )


# ------------------------------------------------------------------ ordering


class TestOrdering:
    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("b", "c", relation="depends_on")]
        assert produce_d3force(_model(nodes, links)) == produce_d3force(
            _model(list(reversed(nodes)), list(reversed(links)))
        )

    def test_edges_deduplicated(self) -> None:
        links = [_link("a", "b"), _link("a", "b")]
        built = build_d3force(_model([_node("a"), _node("b")], links))
        assert len(built["links"]) == 1

    def test_edge_with_unknown_endpoint_dropped(self) -> None:
        built = build_d3force(_model([_node("a"), _node("b")], [_link("a", "ghost"), _link("a", "b")]))
        assert len(built["links"]) == 1

    def test_self_loop_kept(self) -> None:
        built = build_d3force(_model([_node("a")], [_link("a", "a")]))
        assert built["links"] == [{"index": 0, "source": 0, "target": 0, "relation": "supports"}]

    def test_malformed_rows_dropped(self) -> None:
        nodes: list[Any] = [{"id": "a"}, "junk", {"id": ""}, {"id": "b"}, 42]
        links: list[Any] = [{"source": "a", "target": "b", "relation": "supports"}, "junk", {"source": "a"}]
        built = build_d3force(_model(nodes, links))
        assert len(built["nodes"]) == 2
        assert len(built["links"]) == 1

    def test_duplicate_node_id_first_wins(self) -> None:
        built = build_d3force(_model([_node("a", label="First"), _node("a", label="Second")]))
        assert len(built["nodes"]) == 1
        assert built["nodes"][0]["label"] == "First"


# --------------------------------------------------------------- sanitization


class TestSanitization:
    def test_bidi_and_zero_width_stripped(self) -> None:
        built = build_d3force(_model([_node("a", label="A\u202eB\u200bC")]))
        assert built["nodes"][0]["label"] == "ABC"

    def test_whitespace_controls_collapse_in_label(self) -> None:
        built = build_d3force(_model([_node("a", label="line1\n\tline2")]))
        assert built["nodes"][0]["label"] == "line1 line2"

    def test_label_char_cap(self) -> None:
        built = build_d3force(_model([_node("a", label="x" * 500)]))
        assert built["nodes"][0]["label"] == "x" * D3FORCE_LABEL_MAX_CHARS

    def test_node_id_with_control_char_dropped(self) -> None:
        built = build_d3force(_model([_node("a\x07b"), _node("c")]))
        assert [node["id"] for node in built["nodes"]] == ["c"]


# ------------------------------------------------------------------------ cap


class TestCaps:
    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="d3-force"):
            produce_d3force(_model([_node("a" * 100)]), max_bytes=64)

    def test_node_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="node"):
            produce_d3force(_model([_node("a"), _node("b")]), max_nodes=1)

    def test_edge_cap_breach_fails_closed_typed(self) -> None:
        links = [_link("a", "b"), _link("b", "a")]
        with pytest.raises(ExportCapExceededError, match="edge"):
            produce_d3force(_model([_node("a"), _node("b")], links), max_edges=1)

    def test_default_caps_are_explicit(self) -> None:
        assert D3FORCE_MAX_BYTES > 0
        assert D3FORCE_MAX_NODES > 0
        assert D3FORCE_MAX_EDGES > 0
        assert D3FORCE_LABEL_MAX_CHARS > 0

    def test_invalid_caps_rejected(self) -> None:
        with pytest.raises(ExportError):
            produce_d3force(_model([]), max_bytes=0)


# ---------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_valid_artifact_passes(self) -> None:
        roundtrip_d3force(produce_d3force(_model([_node("a"), _node("b")], [_link("a", "b")])))

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_d3force(b"\xff\xfe{")

    def test_non_object_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="object"):
            roundtrip_d3force(b"[1,2,3]\n")

    def test_non_canonical_key_order_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="canonical"):
            roundtrip_d3force(b'{"zz":1,"artifact":"d3-force"}\n')

    def test_index_mismatch_fails(self) -> None:
        data = produce_d3force(_model([_node("a"), _node("b")])).replace(b'"index":1', b'"index":7', 1)
        with pytest.raises(ExportRoundTripError, match="index"):
            roundtrip_d3force(data)

    def test_link_out_of_range_fails(self) -> None:
        data = produce_d3force(_model([_node("a"), _node("b")], [_link("a", "b")])).replace(
            b'"target":1', b'"target":9', 1
        )
        with pytest.raises(ExportRoundTripError, match="range"):
            roundtrip_d3force(data)

    def test_unsorted_links_fail(self) -> None:
        links = [_link("a", "b"), _link("a", "b", relation="depends_on")]
        data = produce_d3force(_model([_node("a"), _node("b")], links))
        parsed = json.loads(data)
        parsed["links"] = [parsed["links"][1], parsed["links"][0]]
        parsed["links"][0]["index"] = 0
        parsed["links"][1]["index"] = 1
        data = json.dumps(parsed, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_d3force(data)

    def test_unsanitized_label_fails(self) -> None:
        payload = {
            "artifact": "d3-force",
            "schema_version": 1,
            "nodes": [{"id": "a", "index": 0, "label": "a\x07b", "domain": "P1", "group": "P1"}],
            "links": [],
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with pytest.raises(ExportRoundTripError, match="sanit"):
            roundtrip_d3force(data)


# ----------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_d3force_adds_format_entry(self) -> None:
        registry = ExportRegistry()
        register_d3force(registry)
        assert "exports/d3-force.json" in registry.names(kind="format")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_d3force(registry)
        artifacts = produce_exports(registry, _model([_node("a")]))
        assert b'"artifact":"d3-force"' in artifacts["exports/d3-force.json"]


# --------------------------------------------------------------- properties


_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"),
    min_size=1,
    max_size=10,
)
_LABEL = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30)


@settings(derandomize=True, max_examples=25)
@given(
    nodes=st.lists(st.tuples(_IDENTIFIER, _LABEL), min_size=0, max_size=8, unique_by=lambda pair: pair[0]),
    edges=st.lists(st.integers(0, 7), min_size=0, max_size=6),
)
def test_property_byte_deterministic_and_roundtrips(nodes: list[tuple[str, str]], edges: list[int]) -> None:
    node_dicts = [_node(node_id, label=label) for node_id, label in nodes]
    ids = [node_id for node_id, _ in nodes]
    links = [_link(ids[i % len(ids)], ids[(i + 1) % len(ids)]) for i in edges if ids]
    first = produce_d3force(_model(node_dicts, links))
    shuffled = produce_d3force(_model(list(reversed(node_dicts)), list(reversed(links))))
    assert first == shuffled
    roundtrip_d3force(first)
    parsed = json.loads(first)
    assert [node["index"] for node in parsed["nodes"]] == list(range(len(parsed["nodes"])))

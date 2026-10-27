"""Tests for ``know_your_soul.exporters.cytoscape`` (ATM wave-2 formats).

Pins the canonical Cytoscape.js elements JSON contract: ``elements.nodes`` /
``elements.edges`` with ``data`` records, nodes sorted by id, edges sorted by
(source, target, relation) with sequential synthetic ids, labels sanitized
through ``security.sanitize_text``, canonical-JSON byte-exact round-trip,
and explicit fail-closed caps.
"""

from __future__ import annotations

import json
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.cytoscape import (
    CYTOSCAPE_LABEL_MAX_CHARS,
    CYTOSCAPE_MAX_BYTES,
    CYTOSCAPE_MAX_EDGES,
    CYTOSCAPE_MAX_NODES,
    build_cytoscape,
    produce_cytoscape,
    register_cytoscape,
    roundtrip_cytoscape,
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
    version: int = 1,
) -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "domain": domain, "kind": kind, "version": version}
    if label is not None:
        node["label"] = label
    return node


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


def _elements(model: dict[str, Any]) -> dict[str, Any]:
    return build_cytoscape(model)["elements"]


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_top_level_shape(self) -> None:
        built = build_cytoscape(_model([]))
        assert built["artifact"] == "cytoscape"
        assert built["schema_version"] == 1
        assert built["elements"] == {"nodes": [], "edges": []}

    def test_node_data_fields(self) -> None:
        elements = _elements(_model([_node("a", label="Alpha", domain="P3", kind="root", version=4)]))
        assert elements["nodes"] == [
            {"data": {"id": "a", "label": "Alpha", "domain": "P3", "kind": "root", "version": 4}}
        ]

    def test_edge_data_fields_sequential_ids(self) -> None:
        links = [_link("a", "b", relation="depends_on"), _link("a", "c")]
        elements = _elements(_model([_node("a"), _node("b"), _node("c")], links))
        assert elements["edges"] == [
            {"data": {"id": "e000000", "source": "a", "target": "b", "relation": "depends_on"}},
            {"data": {"id": "e000001", "source": "a", "target": "c", "relation": "supports"}},
        ]

    def test_label_defaults_to_node_id(self) -> None:
        elements = _elements(_model([_node("xyz")]))
        assert elements["nodes"][0]["data"]["label"] == "xyz"

    def test_produced_bytes_are_canonical_json_with_newline(self) -> None:
        data = produce_cytoscape(_model([_node("a")]))
        assert data.endswith(b"\n")
        assert (
            data
            == json.dumps(json.loads(data), sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        )


# ------------------------------------------------------------------ ordering


class TestOrdering:
    def test_nodes_sorted_by_id(self) -> None:
        elements = _elements(_model([_node("zeta"), _node("alpha"), _node("mid")]))
        assert [entry["data"]["id"] for entry in elements["nodes"]] == ["alpha", "mid", "zeta"]

    def test_edges_sorted_and_deduplicated(self) -> None:
        links = [
            _link("b", "c"),
            _link("a", "c"),
            _link("a", "b", relation="depends_on"),
            _link("a", "b"),
            _link("a", "b"),
        ]
        elements = _elements(_model([_node("a"), _node("b"), _node("c")], links))
        keys = [(e["data"]["source"], e["data"]["target"], e["data"]["relation"]) for e in elements["edges"]]
        assert keys == [
            ("a", "b", "depends_on"),
            ("a", "b", "supports"),
            ("a", "c", "supports"),
            ("b", "c", "supports"),
        ]

    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("b", "c", relation="depends_on")]
        assert produce_cytoscape(_model(nodes, links)) == produce_cytoscape(
            _model(list(reversed(nodes)), list(reversed(links)))
        )

    def test_edge_with_unknown_endpoint_dropped(self) -> None:
        elements = _elements(_model([_node("a"), _node("b")], [_link("a", "ghost"), _link("a", "b")]))
        assert len(elements["edges"]) == 1

    def test_self_loop_kept(self) -> None:
        elements = _elements(_model([_node("a")], [_link("a", "a")]))
        assert elements["edges"][0]["data"]["source"] == "a"
        assert elements["edges"][0]["data"]["target"] == "a"

    def test_malformed_rows_dropped(self) -> None:
        nodes: list[Any] = [{"id": "a"}, "junk", {"id": ""}, {"id": "b"}, 42]
        links: list[Any] = [{"source": "a", "target": "b", "relation": "supports"}, "junk", {"source": "a"}]
        elements = _elements(_model(nodes, links))
        assert len(elements["nodes"]) == 2
        assert len(elements["edges"]) == 1

    def test_non_integer_version_defaults_to_zero(self) -> None:
        elements = _elements(_model([_node("a", version="seven")]))  # type: ignore[arg-type]
        assert elements["nodes"][0]["data"]["version"] == 0

    def test_duplicate_node_id_first_wins(self) -> None:
        elements = _elements(_model([_node("a", label="First"), _node("a", label="Second")]))
        assert len(elements["nodes"]) == 1
        assert elements["nodes"][0]["data"]["label"] == "First"


# --------------------------------------------------------------- sanitization


class TestSanitization:
    def test_bidi_and_zero_width_stripped(self) -> None:
        elements = _elements(_model([_node("a", label="A\u202eB\u200bC")]))
        assert elements["nodes"][0]["data"]["label"] == "ABC"

    def test_whitespace_controls_collapse_in_label(self) -> None:
        elements = _elements(_model([_node("a", label="line1\n\tline2")]))
        assert elements["nodes"][0]["data"]["label"] == "line1 line2"

    def test_label_char_cap(self) -> None:
        elements = _elements(_model([_node("a", label="x" * 500)]))
        assert elements["nodes"][0]["data"]["label"] == "x" * CYTOSCAPE_LABEL_MAX_CHARS

    def test_node_id_with_control_char_dropped(self) -> None:
        elements = _elements(_model([_node("a\x07b"), _node("c")]))
        assert [entry["data"]["id"] for entry in elements["nodes"]] == ["c"]


# ------------------------------------------------------------------------ cap


class TestCaps:
    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="cytoscape"):
            produce_cytoscape(_model([_node("a" * 100)]), max_bytes=64)

    def test_node_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="node"):
            produce_cytoscape(_model([_node("a"), _node("b")]), max_nodes=1)

    def test_edge_cap_breach_fails_closed_typed(self) -> None:
        links = [_link("a", "b"), _link("b", "a")]
        with pytest.raises(ExportCapExceededError, match="edge"):
            produce_cytoscape(_model([_node("a"), _node("b")], links), max_edges=1)

    def test_default_caps_are_explicit(self) -> None:
        assert CYTOSCAPE_MAX_BYTES > 0
        assert CYTOSCAPE_MAX_NODES > 0
        assert CYTOSCAPE_MAX_EDGES > 0
        assert CYTOSCAPE_LABEL_MAX_CHARS > 0

    def test_invalid_caps_rejected(self) -> None:
        with pytest.raises(ExportError):
            produce_cytoscape(_model([]), max_bytes=0)


# ---------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_valid_artifact_passes(self) -> None:
        roundtrip_cytoscape(produce_cytoscape(_model([_node("a"), _node("b")], [_link("a", "b")])))

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_cytoscape(b"\xff\xfe{")

    def test_non_object_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="object"):
            roundtrip_cytoscape(b"[1,2,3]\n")

    def test_non_canonical_key_order_fails(self) -> None:
        data = produce_cytoscape(_model([_node("a")]))
        parsed = json.loads(data)
        scrambled = json.dumps(parsed, sort_keys=False, separators=(",", ":")).encode() + b"\n"
        if scrambled == data:  # single-key payloads can be accidentally canonical
            scrambled = b'{"zz":1,"artifact":"cytoscape"}\n'
        with pytest.raises(ExportRoundTripError, match="canonical"):
            roundtrip_cytoscape(scrambled)

    def test_missing_elements_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="elements"):
            roundtrip_cytoscape(b'{"artifact":"cytoscape","schema_version":1}\n')

    def test_duplicate_node_id_fails(self) -> None:
        payload = {
            "artifact": "cytoscape",
            "schema_version": 1,
            "elements": {
                "nodes": [{"data": {"id": "a", "label": "a"}}, {"data": {"id": "a", "label": "a"}}],
                "edges": [],
            },
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with pytest.raises(ExportRoundTripError, match="duplicate|order"):
            roundtrip_cytoscape(data)

    def test_edge_to_unknown_node_fails(self) -> None:
        payload = {
            "artifact": "cytoscape",
            "schema_version": 1,
            "elements": {
                "nodes": [{"data": {"id": "a", "label": "a"}}],
                "edges": [{"data": {"id": "e000000", "source": "a", "target": "ghost", "relation": "supports"}}],
            },
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with pytest.raises(ExportRoundTripError, match="unknown|endpoint"):
            roundtrip_cytoscape(data)

    def test_edge_id_sequence_break_fails(self) -> None:
        data = produce_cytoscape(_model([_node("a"), _node("b")], [_link("a", "b")])).replace(
            b'"e000000"', b'"e000009"', 1
        )
        with pytest.raises(ExportRoundTripError, match="edge id"):
            roundtrip_cytoscape(data)

    def test_unsanitized_label_fails(self) -> None:
        payload = {
            "artifact": "cytoscape",
            "schema_version": 1,
            "elements": {"nodes": [{"data": {"id": "a", "label": "a\x07b"}}], "edges": []},
        }
        data = json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode() + b"\n"
        with pytest.raises(ExportRoundTripError, match="sanit"):
            roundtrip_cytoscape(data)


# ----------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_cytoscape_adds_format_entry(self) -> None:
        registry = ExportRegistry()
        register_cytoscape(registry)
        assert "exports/cytoscape.json" in registry.names(kind="format")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_cytoscape(registry)
        artifacts = produce_exports(registry, _model([_node("a")]))
        assert b'"artifact":"cytoscape"' in artifacts["exports/cytoscape.json"]


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
    first = produce_cytoscape(_model(node_dicts, links))
    shuffled = produce_cytoscape(_model(list(reversed(node_dicts)), list(reversed(links))))
    assert first == shuffled
    roundtrip_cytoscape(first)
    assert json.loads(first)["artifact"] == "cytoscape"

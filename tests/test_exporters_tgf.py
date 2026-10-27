"""Tests for ``know_your_soul.exporters.tgf`` (ATM wave-2 formats).

Pins the canonical Trivial Graph Format contract: node section with
sequential integer ids (1..N, assigned in sorted node-id order) and
sanitized labels, a lone ``#`` separator, edge section with integer
endpoint pairs plus relation, sorted and deduplicated, LF endings, explicit
fail-closed caps, and a parse-back round-trip validator.
"""

from __future__ import annotations

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
from know_your_soul.exporters.tgf import (
    TGF_LABEL_MAX_CHARS,
    TGF_MAX_BYTES,
    TGF_MAX_EDGES,
    TGF_MAX_NODES,
    produce_tgf,
    register_tgf,
    render_tgf,
    roundtrip_tgf,
)

# ------------------------------------------------------------------ helpers


def _node(node_id: str, *, label: str | None = None, domain: str = "P1") -> dict[str, Any]:
    node: dict[str, Any] = {"id": node_id, "domain": domain}
    if label is not None:
        node["label"] = label
    return node


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_empty_model(self) -> None:
        assert render_tgf(_model([])) == "#\n"

    def test_nodes_get_sequential_ids_in_sorted_order(self) -> None:
        text = render_tgf(_model([_node("zeta"), _node("alpha"), _node("mid")]))
        node_section = text.split("#\n")[0]
        assert node_section == "1 alpha\n2 mid\n3 zeta\n"

    def test_label_written_after_id(self) -> None:
        text = render_tgf(_model([_node("a", label="Alpha node")]))
        assert text.splitlines()[0] == "1 Alpha node"

    def test_separator_line(self) -> None:
        text = render_tgf(_model([_node("a")]))
        assert "\n#\n" in text

    def test_edges_reference_integer_ids(self) -> None:
        text = render_tgf(_model([_node("a"), _node("b")], [_link("b", "a", relation="depends_on")]))
        assert text.splitlines()[-1] == "2 1 depends_on"

    def test_edges_sorted_and_deduplicated(self) -> None:
        links = [
            _link("b", "c"),
            _link("a", "c"),
            _link("a", "b", relation="depends_on"),
            _link("a", "b"),
            _link("a", "b"),
        ]
        text = render_tgf(_model([_node("a"), _node("b"), _node("c")], links))
        edge_section = text.split("#\n")[1]
        assert edge_section == "1 2 depends_on\n1 2 supports\n1 3 supports\n2 3 supports\n"

    def test_self_loop_kept(self) -> None:
        text = render_tgf(_model([_node("a")], [_link("a", "a")]))
        assert text.splitlines()[-1] == "1 1 supports"

    def test_malformed_rows_dropped(self) -> None:
        nodes: list[Any] = [{"id": "a"}, "junk", {"id": ""}, {"id": "b"}, 42]
        links: list[Any] = [{"source": "a", "target": "b", "relation": "supports"}, "junk", {"source": "a"}]
        text = render_tgf(_model(nodes, links))
        assert text == "1 a\n2 b\n#\n1 2 supports\n"

    def test_relation_label_defaults_when_empty(self) -> None:
        text = render_tgf(_model([_node("a"), _node("b")], [_link("a", "b", relation="   ")]))
        assert text.splitlines()[-1] == "1 2 related"


# --------------------------------------------------------------- sanitization


class TestSanitization:
    def test_bidi_and_zero_width_stripped(self) -> None:
        text = render_tgf(_model([_node("a", label="A\u202eB\u200bC")]))
        assert text.splitlines()[0] == "1 ABC"

    def test_whitespace_controls_collapse_in_label(self) -> None:
        text = render_tgf(_model([_node("a", label="line1\n\tline2")]))
        assert text.splitlines()[0] == "1 line1 line2"

    def test_label_char_cap(self) -> None:
        text = render_tgf(_model([_node("a", label="x" * 500)]))
        assert text.splitlines()[0] == "1 " + "x" * TGF_LABEL_MAX_CHARS

    def test_relation_whitespace_collapses(self) -> None:
        text = render_tgf(_model([_node("a"), _node("b")], [_link("a", "b", relation="rel\nname")]))
        assert text.splitlines()[-1] == "1 2 rel name"

    def test_empty_label_falls_back_to_node_id(self) -> None:
        text = render_tgf(_model([_node("a", label="   ")]))
        assert text.splitlines()[0] == "1 a"

    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a", label="A"), _node("b", label="B")]
        links = [_link("a", "b"), _link("b", "a", relation="depends_on")]
        assert produce_tgf(_model(nodes, links)) == produce_tgf(_model(list(reversed(nodes)), list(reversed(links))))


# ------------------------------------------------------------------------ cap


class TestCaps:
    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="graph.tgf"):
            produce_tgf(_model([_node("a" * 100)]), max_bytes=16)

    def test_node_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="node"):
            produce_tgf(_model([_node("a"), _node("b")]), max_nodes=1)

    def test_edge_cap_breach_fails_closed_typed(self) -> None:
        links = [_link("a", "b"), _link("b", "a")]
        with pytest.raises(ExportCapExceededError, match="edge"):
            produce_tgf(_model([_node("a"), _node("b")], links), max_edges=1)

    def test_default_caps_are_explicit(self) -> None:
        assert TGF_MAX_BYTES > 0
        assert TGF_MAX_NODES > 0
        assert TGF_MAX_EDGES > 0
        assert TGF_LABEL_MAX_CHARS > 0

    def test_invalid_params_rejected(self) -> None:
        with pytest.raises(ExportError):
            produce_tgf(_model([]), max_bytes=0)
        with pytest.raises(ExportError):
            render_tgf(_model([]), label_max_chars=0)


# ---------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_valid_artifact_passes(self) -> None:
        data = produce_tgf(_model([_node("a"), _node("b")], [_link("a", "b")]))
        roundtrip_tgf(data)

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_tgf(b"\xff\xfe1 a")

    def test_missing_separator_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="separator"):
            roundtrip_tgf(b"1 a\n")

    def test_non_sequential_node_ids_fail(self) -> None:
        with pytest.raises(ExportRoundTripError, match="sequential"):
            roundtrip_tgf(b"1 a\n3 c\n#\n")

    def test_node_line_after_separator_fails(self) -> None:
        # A "1 b" line in the edge section would be an edge with missing target.
        with pytest.raises(ExportRoundTripError):
            roundtrip_tgf(b"1 a\n#\n1 b\n")

    def test_edge_to_unknown_node_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="unknown|endpoint"):
            roundtrip_tgf(b"1 a\n#\n1 9 supports\n")

    def test_unsorted_edges_fail(self) -> None:
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_tgf(b"1 a\n2 b\n#\n2 1 supports\n1 2 supports\n")

    def test_duplicate_edges_fail(self) -> None:
        with pytest.raises(ExportRoundTripError, match="order|duplicate"):
            roundtrip_tgf(b"1 a\n2 b\n#\n1 2 supports\n1 2 supports\n")

    def test_crlf_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="CR|control"):
            roundtrip_tgf(b"1 a\r\n#\r\n")

    def test_unsanitized_label_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="sanit|control"):
            roundtrip_tgf(b"1 a\x07b\n#\n")


# ----------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_tgf_adds_format_entry(self) -> None:
        registry = ExportRegistry()
        register_tgf(registry)
        assert "exports/graph.tgf" in registry.names(kind="format")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_tgf(registry)
        artifacts = produce_exports(registry, _model([_node("a")]))
        assert artifacts["exports/graph.tgf"] == b"1 a\n#\n"


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
    first = produce_tgf(_model(node_dicts, links))
    shuffled = produce_tgf(_model(list(reversed(node_dicts)), list(reversed(links))))
    assert first == shuffled
    roundtrip_tgf(first)
    node_section, _, _edge_section = first.decode().partition("#\n")
    node_lines = [line for line in node_section.split("\n") if line]
    assert [int(line.split(" ", 1)[0]) for line in node_lines] == list(range(1, len(node_lines) + 1))

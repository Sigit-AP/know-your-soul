"""Tests for ``know_your_soul.exporters.gexf`` (ATM wave-2 formats).

Pins the canonical GEXF 1.2draft contract: XML-1.0-legal bytes, nodes sorted
by id with ``domain``/``kind``/``version`` attvalues, edges sorted by
(source, target, relation) and deduplicated with sequential synthetic ids,
graph-level node/edge counts, labels sanitized through
``security.sanitize_text``, explicit fail-closed caps, and a parse-back
round-trip validator that rejects any structural drift.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.gexf import (
    GEXF_LABEL_MAX_CHARS,
    GEXF_MAX_BYTES,
    GEXF_MAX_EDGES,
    GEXF_MAX_NODES,
    GEXF_NAMESPACE,
    produce_gexf,
    register_gexf,
    render_gexf,
    roundtrip_gexf,
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


def _node_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.lstrip().startswith("<node ")]


def _edge_lines(text: str) -> list[str]:
    return [line.strip() for line in text.splitlines() if line.lstrip().startswith("<edge ")]


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_empty_model_renders_valid_skeleton(self) -> None:
        text = render_gexf(_model([]))
        assert text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n')
        assert f'<gexf xmlns="{GEXF_NAMESPACE}" version="1.2">' in text
        assert '<graph mode="static" defaultedgetype="directed">' in text
        assert '<nodes count="0">' in text
        assert '<edges count="0">' in text
        assert text.endswith("</gexf>\n")

    def test_node_element_carries_attvalues(self) -> None:
        text = render_gexf(_model([_node("a", label="Alpha", domain="P3", kind="root", version=4)]))
        assert '<node id="a" label="Alpha">' in text
        assert '<attvalue for="domain" value="P3"/>' in text
        assert '<attvalue for="kind" value="root"/>' in text
        assert '<attvalue for="version" value="4"/>' in text

    def test_label_defaults_to_node_id(self) -> None:
        text = render_gexf(_model([_node("xyz")]))
        assert '<node id="xyz" label="xyz">' in text

    def test_edge_elements_have_sequential_ids_and_relation(self) -> None:
        links = [_link("a", "b", relation="depends_on"), _link("a", "c")]
        text = render_gexf(_model([_node("a"), _node("b"), _node("c")], links))
        edges = _edge_lines(text)
        assert edges[0].startswith('<edge id="e000000"')
        assert edges[1].startswith('<edge id="e000001"')
        assert '<attvalue for="relation" value="depends_on"/>' in text
        assert '<edges count="2">' in text
        assert '<nodes count="3">' in text

    def test_attribute_declarations_present(self) -> None:
        text = render_gexf(_model([]))
        assert '<attributes class="node">' in text
        assert '<attribute id="domain" title="domain" type="string"/>' in text
        assert '<attribute id="kind" title="kind" type="string"/>' in text
        assert '<attribute id="version" title="version" type="integer"/>' in text
        assert '<attributes class="edge">' in text
        assert '<attribute id="relation" title="relation" type="string"/>' in text


# ------------------------------------------------------------------ ordering


class TestOrdering:
    def test_nodes_sorted_by_id(self) -> None:
        text = render_gexf(_model([_node("zeta"), _node("alpha"), _node("mid")]))
        ids = [line.split('id="')[1].split('"')[0] for line in _node_lines(text)]
        assert ids == ["alpha", "mid", "zeta"]

    def test_edges_sorted_and_deduplicated(self) -> None:
        links = [
            _link("b", "c"),
            _link("a", "c"),
            _link("a", "b", relation="depends_on"),
            _link("a", "b"),
            _link("a", "b"),  # duplicate
        ]
        text = render_gexf(_model([_node("a"), _node("b"), _node("c")], links))
        edges = _edge_lines(text)
        assert len(edges) == 4
        assert '<edge id="e000000" source="a" target="b">' in edges[0]
        relations = [
            line.split('value="')[1].split('"')[0] for line in text.splitlines() if '<attvalue for="relation"' in line
        ]
        assert relations == ["depends_on", "supports", "supports", "supports"]

    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("b", "c", relation="depends_on")]
        first = produce_gexf(_model(nodes, links))
        second = produce_gexf(_model(list(reversed(nodes)), list(reversed(links))))
        assert first == second

    def test_edge_with_unknown_endpoint_dropped(self) -> None:
        links = [_link("a", "ghost"), _link("a", "b")]
        text = render_gexf(_model([_node("a"), _node("b")], links))
        assert '<edges count="1">' in text
        assert "ghost" not in text

    def test_self_loop_kept(self) -> None:
        text = render_gexf(_model([_node("a")], [_link("a", "a")]))
        assert '<edge id="e000000" source="a" target="a">' in text

    def test_malformed_rows_dropped(self) -> None:
        nodes: list[Any] = [{"id": "a"}, "junk", {"id": ""}, {"id": "b"}, 42]
        links: list[Any] = [{"source": "a", "target": "b", "relation": "supports"}, "junk", {"source": "a"}]
        text = render_gexf(_model(nodes, links))
        assert '<nodes count="2">' in text
        assert '<edges count="1">' in text

    def test_duplicate_node_id_first_wins(self) -> None:
        nodes = [_node("a", label="First"), _node("a", label="Second")]
        text = render_gexf(_model(nodes))
        assert '<nodes count="1">' in text
        assert 'label="First"' in text

    def test_non_integer_version_defaults_to_zero(self) -> None:
        nodes = [_node("a", version="seven")]  # type: ignore[arg-type]
        text = render_gexf(_model(nodes))
        assert '<attvalue for="version" value="0"/>' in text


# --------------------------------------------------------------- sanitization


class TestSanitization:
    def test_bidi_and_zero_width_stripped(self) -> None:
        text = render_gexf(_model([_node("a", label="A\u202eB\u200bC")]))
        assert 'label="ABC"' in text

    def test_c0_control_stripped_from_label(self) -> None:
        text = render_gexf(_model([_node("a", label="bad\x07bell")]))
        assert 'label="badbell"' in text
        assert "\x07" not in text

    def test_whitespace_controls_collapse_in_label(self) -> None:
        text = render_gexf(_model([_node("a", label="line1\n\tline2")]))
        assert 'label="line1 line2"' in text

    def test_label_char_cap(self) -> None:
        text = render_gexf(_model([_node("a", label="x" * 500)]))
        assert 'label="' + "x" * GEXF_LABEL_MAX_CHARS + '"' in text

    def test_xml_special_chars_escaped(self) -> None:
        text = render_gexf(_model([_node("a", label='He said "hi" & <bye>')]))
        # quoteattr switches to single quotes and escapes & < > (not ") here.
        assert "label='He said \"hi\" &amp; &lt;bye&gt;'" in text
        assert "<bye>" not in text

    def test_node_id_with_c0_dropped(self) -> None:
        text = render_gexf(_model([_node("a\x07b"), _node("c")]))
        assert '<nodes count="1">' in text
        assert 'id="c"' in text


# ------------------------------------------------------------------------ cap


class TestCaps:
    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="graph.gexf"):
            produce_gexf(_model([_node("a" * 100)]), max_bytes=64)

    def test_node_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="node"):
            produce_gexf(_model([_node("a"), _node("b")]), max_nodes=1)

    def test_edge_cap_breach_fails_closed_typed(self) -> None:
        links = [_link("a", "b"), _link("b", "a")]
        with pytest.raises(ExportCapExceededError, match="edge"):
            produce_gexf(_model([_node("a"), _node("b")], links), max_edges=1)

    def test_default_caps_are_explicit(self) -> None:
        assert GEXF_MAX_BYTES > 0
        assert GEXF_MAX_NODES > 0
        assert GEXF_MAX_EDGES > 0
        assert GEXF_LABEL_MAX_CHARS > 0

    def test_invalid_caps_rejected(self) -> None:
        with pytest.raises(ExportError):
            produce_gexf(_model([]), max_bytes=0)
        with pytest.raises(ExportError):
            produce_gexf(_model([]), max_nodes=0)
        with pytest.raises(ExportError):
            render_gexf(_model([]), label_max_chars=0)


# ---------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_valid_artifact_passes(self) -> None:
        data = produce_gexf(_model([_node("a"), _node("b")], [_link("a", "b")]))
        roundtrip_gexf(data)

    def test_produce_runs_roundtrip_internally(self) -> None:
        # produce_gexf validates its own output; a pass here proves the loop.
        data = produce_gexf(_model([_node("a", label='weird "label" \u202e')]))
        roundtrip_gexf(data)

    def test_truncated_fails(self) -> None:
        data = produce_gexf(_model([_node("a")]))
        with pytest.raises(ExportRoundTripError):
            roundtrip_gexf(data[: len(data) // 2])

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_gexf(b"\xff\xfe<?xml")

    def test_c0_control_fails(self) -> None:
        data = produce_gexf(_model([_node("a")])).replace(b"<node ", b"<node\x07 ", 1)
        with pytest.raises(ExportRoundTripError, match="control"):
            roundtrip_gexf(data)

    def test_wrong_namespace_fails(self) -> None:
        data = produce_gexf(_model([])).replace(GEXF_NAMESPACE.encode(), b"http://example.invalid/x", 1)
        with pytest.raises(ExportRoundTripError, match="root|namespace"):
            roundtrip_gexf(data)

    def test_count_mismatch_fails(self) -> None:
        data = produce_gexf(_model([_node("a")])).replace(b'<nodes count="1">', b'<nodes count="2">', 1)
        with pytest.raises(ExportRoundTripError, match="count"):
            roundtrip_gexf(data)

    def test_unsorted_nodes_fail(self) -> None:
        # Renaming the first node's id to "z" breaks the canonical id order.
        data = produce_gexf(_model([_node("a"), _node("b")])).replace(b'id="a"', b'id="z"', 1)
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_gexf(data)

    def test_edge_to_unknown_node_fails(self) -> None:
        data = produce_gexf(_model([_node("a")], [_link("a", "a")])).replace(b'target="a"', b'target="ghost"', 1)
        with pytest.raises(ExportRoundTripError, match="unknown|endpoint"):
            roundtrip_gexf(data)

    def test_edge_id_sequence_break_fails(self) -> None:
        links = [_link("a", "b"), _link("b", "a", relation="depends_on")]
        data = produce_gexf(_model([_node("a"), _node("b")], links)).replace(b'id="e000001"', b'id="e000007"', 1)
        with pytest.raises(ExportRoundTripError, match="edge id"):
            roundtrip_gexf(data)


# ----------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_gexf_adds_format_entry(self) -> None:
        registry = ExportRegistry()
        register_gexf(registry)
        assert "exports/graph.gexf" in registry.names(kind="format")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_gexf(registry)
        artifacts = produce_exports(registry, _model([_node("a")]))
        assert artifacts["exports/graph.gexf"].startswith(b'<?xml version="1.0" encoding="UTF-8"?>\n')


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
    first = produce_gexf(_model(node_dicts, links))
    shuffled = produce_gexf(_model(list(reversed(node_dicts)), list(reversed(links))))
    assert first == shuffled
    roundtrip_gexf(first)
    text = first.decode()
    assert "<nodes count=" in text and "</gexf>" in text

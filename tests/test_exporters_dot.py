"""Tests for ``know_your_soul.exporters.dot`` (ATM F-B1).

Pins the canonical Graphviz DOT contract: nodes sorted by id, edges sorted by
(source, target, relation) and deduplicated, labels sanitized through
``security.sanitize_text`` (bidi/zero-width stripped, whitespace controls
collapsed, quotes escaped), one deterministic fill color per primary domain
from our own palette, an explicit fail-closed byte cap, and a parse-back
round-trip validator that rejects any structural drift.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.dot import (
    DEFAULT_DOT_MAX_BYTES,
    DOMAIN_COLORS,
    FALLBACK_COLOR,
    produce_dot,
    register_dot,
    render_dot,
    roundtrip_dot,
)
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
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
    status: str = "active",
    version: int = 1,
    level: int = 2,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "domain": domain,
        "kind": kind,
        "lifecycle_status": status,
        "version": version,
        "professional_level": level,
        "scores": {
            "p": "0.100000000000",
            "m": "0.200000000000",
            "e": "0.300000000000",
            "uncertainty": "0.400000000000",
            "confidence": "0.500000000000",
        },
        "confidence": "0.500000000000",
    }
    if label is not None:
        node["label"] = label
    return node


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation, "confidence": "EXTRACTED"}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


def _node_lines(dot: str) -> list[str]:
    return [line for line in dot.splitlines() if line.startswith('  "') and "->" not in line]


def _edge_lines(dot: str) -> list[str]:
    return [line for line in dot.splitlines() if "->" in line]


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_empty_model_renders_valid_skeleton(self) -> None:
        dot = render_dot(_model([]))
        assert dot.startswith("digraph soul_knowledge {\n")
        assert dot.endswith("}\n")
        assert 'graph [rankdir="LR"];' in dot
        assert _node_lines(dot) == []
        assert _edge_lines(dot) == []

    def test_nodes_sorted_by_id(self) -> None:
        dot = render_dot(_model([_node("zeta"), _node("alpha"), _node("mid")]))
        ids = [re.search(r'^  "([^"]+)"', line).group(1) for line in _node_lines(dot)]  # type: ignore[union-attr]
        assert ids == ["alpha", "mid", "zeta"]

    def test_edges_sorted_and_deduplicated(self) -> None:
        links = [
            _link("b", "c", relation="supports"),
            _link("a", "c", relation="supports"),
            _link("a", "b", relation="supports"),
            _link("a", "b", relation="depends_on"),
            _link("a", "b", relation="supports"),  # duplicate
        ]
        dot = render_dot(_model([_node("a"), _node("b"), _node("c")], links))
        rendered = _edge_lines(dot)
        assert rendered == [
            '  "a" -> "b" [label="depends_on"];',
            '  "a" -> "b" [label="supports"];',
            '  "a" -> "c" [label="supports"];',
            '  "b" -> "c" [label="supports"];',
        ]

    def test_edge_with_unknown_endpoint_skipped(self) -> None:
        dot = render_dot(_model([_node("a")], [_link("a", "ghost"), _link("ghost", "a")]))
        assert _edge_lines(dot) == []

    def test_self_loop_is_kept(self) -> None:
        dot = render_dot(_model([_node("a")], [_link("a", "a")]))
        assert _edge_lines(dot) == ['  "a" -> "a" [label="supports"];']

    def test_label_falls_back_to_id(self) -> None:
        dot = render_dot(_model([_node("plain")]))
        assert '"plain" [label="plain"' in dot


# ------------------------------------------------------------ sanitization


class TestSanitization:
    def test_bidi_and_zero_width_stripped_from_label(self) -> None:
        dot = render_dot(_model([_node("n", label="ab​c‏d")]))
        assert '[label="abcd"' in dot

    def test_whitespace_controls_collapse_to_space(self) -> None:
        dot = render_dot(_model([_node("n", label="one\ntwo\tthree\rfour")]))
        assert '[label="one two three four"' in dot

    def test_quotes_and_backslashes_escaped(self) -> None:
        dot = render_dot(_model([_node("n", label='say "hi" \\ end')]))
        assert r'[label="say \"hi\" \\ end"' in dot

    def test_label_is_capped(self) -> None:
        dot = render_dot(_model([_node("n", label="x" * 500)]), label_max_chars=10)
        assert '[label="xxxxxxxxxx"' in dot
        assert "x" * 11 not in dot


# ------------------------------------------------------------------- colors


class TestColors:
    def test_palette_covers_all_seven_domains_with_distinct_hex(self) -> None:
        assert set(DOMAIN_COLORS) == {"P1", "P2", "P3", "P4", "P5", "P6", "P7"}
        assert len(set(DOMAIN_COLORS.values())) == 7
        for color in DOMAIN_COLORS.values():
            assert re.fullmatch(r"#[0-9A-F]{6}", color)

    def test_node_fillcolor_matches_domain_palette(self) -> None:
        dot = render_dot(_model([_node("a", domain="P3"), _node("b", domain="P5")]))
        assert f'"a" [label="a", fillcolor="{DOMAIN_COLORS["P3"]}"]' in dot
        assert f'"b" [label="b", fillcolor="{DOMAIN_COLORS["P5"]}"]' in dot

    def test_unknown_domain_gets_fallback_color(self) -> None:
        dot = render_dot(_model([_node("x", domain="P9")]))
        assert f'fillcolor="{FALLBACK_COLOR}"' in dot


# -------------------------------------------------------------- determinism


class TestDeterminism:
    def test_byte_identical_across_input_orderings(self) -> None:
        nodes = [_node("c", domain="P7"), _node("a", domain="P1"), _node("b", domain="P4")]
        links = [_link("b", "c"), _link("a", "b"), _link("a", "c")]
        forward = produce_dot(_model(nodes, links))
        backward = produce_dot(_model(list(reversed(nodes)), list(reversed(links))))
        assert forward == backward
        assert forward == produce_dot(_model(nodes, links))

    def test_output_is_utf8_with_trailing_newline(self) -> None:
        data = produce_dot(_model([_node("a")]))
        assert data.decode("utf-8").endswith("}\n")


# --------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_produced_output_passes_roundtrip(self) -> None:
        data = produce_dot(_model([_node("a"), _node("b", label='x "y"')], [_link("a", "b")]))
        roundtrip_dot(data)  # must not raise

    def test_roundtrip_rejects_non_dot_payload(self) -> None:
        with pytest.raises(ExportRoundTripError):
            roundtrip_dot(b"this is not dot")

    def test_roundtrip_rejects_unsorted_nodes(self) -> None:
        bad = (
            'digraph soul_knowledge {\n  graph [rankdir="LR"];\n  node [shape="box", style="filled"];\n'
            f'  "b" [label="b", fillcolor="{DOMAIN_COLORS["P1"]}"];\n'
            f'  "a" [label="a", fillcolor="{DOMAIN_COLORS["P1"]}"];\n}}\n'
        ).encode()
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_dot(bad)

    def test_roundtrip_rejects_color_outside_palette(self) -> None:
        bad = (
            b'digraph soul_knowledge {\n  graph [rankdir="LR"];\n  node [shape="box", style="filled"];\n'
            b'  "a" [label="a", fillcolor="#FFFFFF"];\n}\n'
        )
        with pytest.raises(ExportRoundTripError, match="palette"):
            roundtrip_dot(bad)

    def test_roundtrip_rejects_truncated_graph(self) -> None:
        data = produce_dot(_model([_node("a")]))
        with pytest.raises(ExportRoundTripError):
            roundtrip_dot(data[: data.index(b"}")])


# ---------------------------------------------------------------------- cap


class TestCap:
    def test_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="graph.dot"):
            produce_dot(_model([_node("a" * 100)]), max_bytes=32)

    def test_default_cap_is_explicit(self) -> None:
        assert DEFAULT_DOT_MAX_BYTES > 0


# -------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_dot_adds_format_entry(self) -> None:
        registry = ExportRegistry()
        register_dot(registry)
        assert "exports/graph.dot" in registry.names(kind="format")

    def test_produced_through_orchestrator_is_capped_and_validated(self) -> None:
        registry = ExportRegistry()
        register_dot(registry, max_bytes=64)
        with pytest.raises(ExportCapExceededError):
            produce_exports(registry, _model([_node("node")]))

    def test_orchestrator_roundtrip_uses_module_validator(self) -> None:
        registry = ExportRegistry()
        register_dot(registry)
        artifacts = produce_exports(registry, _model([_node("a")], [_link("a", "a")]))
        assert artifacts["exports/graph.dot"] == produce_dot(_model([_node("a")], [_link("a", "a")]))


# --------------------------------------------------------------- properties


_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=12,
)
_LABEL = st.text(
    alphabet=st.characters(blacklist_categories=("Cs",)),
    max_size=40,
)


@settings(derandomize=True, max_examples=25)
@given(
    nodes=st.lists(st.tuples(_IDENTIFIER, _LABEL), min_size=0, max_size=8, unique_by=lambda pair: pair[0]),
    extra_links=st.lists(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=10),
)
def test_property_byte_deterministic_under_permutation(
    nodes: list[tuple[str, str]], extra_links: list[tuple[int, int]]
) -> None:
    node_dicts = [
        _node(node_id, label=label, domain=f"P{(index % 7) + 1}") for index, (node_id, label) in enumerate(nodes)
    ]
    ids = [node_id for node_id, _ in nodes]
    links = [
        _link(ids[source % len(ids)], ids[target % len(ids)])
        for source, target in extra_links
        if ids and source % len(ids) != target % len(ids)
    ]
    first = produce_dot(_model(node_dicts, links))
    shuffled = produce_dot(_model(list(reversed(node_dicts)), list(reversed(links))))
    assert first == shuffled
    roundtrip_dot(first)

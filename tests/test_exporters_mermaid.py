"""Tests for ``know_your_soul.exporters.mermaid`` (ATM F-B2).

Pins the Mermaid flowchart contract: one subgraph per primary domain P1-P7
(sorted, with Indonesian titles), Mermaid-safe node ids derived
deterministically (with collision suffixes), sanitized labels with proper
``#quot;`` escaping, edges only between shown nodes, and an explicit, marked
elision (never silent) when the graph exceeds the node cap — allocated
round-robin across domains so no domain starves.
"""

from __future__ import annotations

import re
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.mermaid import (
    DEFAULT_MERMAID_MAX_BYTES,
    DEFAULT_MERMAID_MAX_NODES,
    produce_mermaid,
    register_mermaid,
    render_mermaid,
    roundtrip_mermaid,
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
    version: int = 1,
) -> dict[str, Any]:
    node: dict[str, Any] = {
        "id": node_id,
        "domain": domain,
        "kind": "primary",
        "lifecycle_status": "active",
        "version": version,
        "professional_level": 1,
        "scores": {
            "p": "0.000000000000",
            "m": "0.000000000000",
            "e": "0.000000000000",
            "uncertainty": "1.000000000000",
            "confidence": "0.000000000000",
        },
    }
    if label is not None:
        node["label"] = label
    return node


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation, "confidence": "EXTRACTED"}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


# ------------------------------------------------------------------ structure


class TestStructure:
    def test_empty_model_renders_header_only(self) -> None:
        text = render_mermaid(_model([]))
        assert text == "flowchart LR\n"

    def test_subgraph_per_domain_sorted(self) -> None:
        text = render_mermaid(_model([_node("b", domain="P7"), _node("a", domain="P1"), _node("c", domain="P3")]))
        subgraphs = re.findall(r"^  subgraph (dom_\w+)\[", text, flags=re.MULTILINE)
        assert subgraphs == ["dom_P1", "dom_P3", "dom_P7"]

    def test_subgraph_titles_use_domain_titles(self) -> None:
        text = render_mermaid(_model([_node("a", domain="P1"), _node("b", domain="P7")]))
        assert 'subgraph dom_P1["P1 · Pembelajaran"]' in text
        assert 'subgraph dom_P7["P7 · Level Profesional"]' in text

    def test_nodes_sorted_within_subgraph(self) -> None:
        text = render_mermaid(_model([_node("z"), _node("a"), _node("m")]))
        defs = re.findall(r"^    (n_\w+)\[", text, flags=re.MULTILINE)
        assert defs == ["n_a", "n_m", "n_z"]

    def test_edges_sorted_and_between_shown_nodes_only(self) -> None:
        links = [_link("b", "c"), _link("a", "c"), _link("a", "ghost"), _link("a", "b", relation="depends_on")]
        text = render_mermaid(_model([_node("a"), _node("b"), _node("c")], links))
        edges = [line.strip() for line in text.splitlines() if "-->" in line]
        assert edges == ["n_a -->|depends_on| n_b", "n_a -->|supports| n_c", "n_b -->|supports| n_c"]

    def test_label_falls_back_to_id(self) -> None:
        text = render_mermaid(_model([_node("plain")]))
        assert 'n_plain["plain"]' in text


# ------------------------------------------------------------ id sanitation


class TestIdSanitization:
    def test_unsafe_characters_become_underscores(self) -> None:
        text = render_mermaid(_model([_node("a-b.c/d")]))
        assert "n_a_b_c_d[" in text

    def test_id_starting_with_digit_is_prefixed(self) -> None:
        text = render_mermaid(_model([_node("9lives")]))
        assert "n_9lives[" in text

    def test_colliding_sanitized_ids_get_deterministic_suffixes(self) -> None:
        # "a-b" and "a/b" both sanitize to "a_b"; the suffix order must follow
        # canonical node-id order so permuted input renders identically.
        text = render_mermaid(_model([_node("a-b"), _node("a/b")]))
        assert "n_a_b[" in text
        assert "n_a_b_2[" in text
        again = render_mermaid(_model([_node("a/b"), _node("a-b")]))
        assert again == text


# --------------------------------------------------------- label sanitation


class TestLabelSanitization:
    def test_bidi_and_zero_width_stripped(self) -> None:
        text = render_mermaid(_model([_node("n", label="ab​c‏d")]))
        assert 'n_n["abcd"]' in text

    def test_whitespace_controls_collapse(self) -> None:
        text = render_mermaid(_model([_node("n", label="one\ntwo\tthree")]))
        assert 'n_n["one two three"]' in text

    def test_quotes_escaped_as_mermaid_entity(self) -> None:
        text = render_mermaid(_model([_node("n", label='say "hi"')]))
        assert "say #quot;hi#quot;" in text
        assert '["say "hi""]' not in text

    def test_label_capped(self) -> None:
        text = render_mermaid(_model([_node("n", label="x" * 500)]), label_max_chars=10)
        assert 'n_n["xxxxxxxxxx"]' in text
        assert 'n_n["xxxxxxxxxxx"]' not in text


# ------------------------------------------------------------------ elision


class TestElision:
    def test_no_elision_marker_when_under_cap(self) -> None:
        text = render_mermaid(_model([_node("a"), _node("b")]), max_nodes=2)
        assert "elided" not in text

    def test_elision_is_explicit_and_counted(self) -> None:
        nodes = [_node(f"n{i}") for i in range(5)]
        links = [_link("n0", "n3"), _link("n1", "n4"), _link("n0", "n1")]
        text = render_mermaid(_model(nodes, links), max_nodes=3)
        assert "%% elided: 2 nodes (cap 3);" in text
        assert 'ELIDED["2 nodes not shown (cap 3)"]' in text
        # Edges touching elided nodes are dropped and counted.
        assert "edges dropped" in text
        assert "n3" not in [line for line in text.splitlines() if "-->" in line].__str__()

    def test_elision_allocates_round_robin_across_domains(self) -> None:
        nodes = [_node(f"a{i}", domain="P1") for i in range(4)] + [_node(f"b{i}", domain="P2") for i in range(4)]
        text = render_mermaid(_model(nodes), max_nodes=4)
        shown = set(re.findall(r"^    n_(\w+)\[", text, flags=re.MULTILINE))
        # Round-robin: two from each domain, not four from P1 and zero from P2.
        assert shown == {"a0", "a1", "b0", "b1"}

    def test_elision_marker_reflects_exact_counts(self) -> None:
        nodes = [_node(f"n{i}") for i in range(10)]
        text = render_mermaid(_model(nodes), max_nodes=1)
        assert "%% elided: 9 nodes (cap 1);" in text

    def test_elided_graph_still_roundtrips(self) -> None:
        nodes = [_node(f"n{i}") for i in range(6)]
        data = produce_mermaid(_model(nodes), max_nodes=2)
        roundtrip_mermaid(data)  # must not raise


# -------------------------------------------------------------- determinism


class TestDeterminism:
    def test_byte_identical_across_input_orderings(self) -> None:
        nodes = [_node("c", domain="P7"), _node("a", domain="P1"), _node("b", domain="P4")]
        links = [_link("b", "c"), _link("a", "b")]
        forward = produce_mermaid(_model(nodes, links))
        backward = produce_mermaid(_model(list(reversed(nodes)), list(reversed(links))))
        assert forward == backward

    def test_deterministic_under_elision_too(self) -> None:
        nodes = [_node(f"x{i}", domain=f"P{(i % 3) + 1}") for i in range(9)]
        forward = produce_mermaid(_model(nodes), max_nodes=4)
        backward = produce_mermaid(_model(list(reversed(nodes))), max_nodes=4)
        assert forward == backward


# --------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_produced_output_passes_roundtrip(self) -> None:
        data = produce_mermaid(_model([_node("a"), _node("b", label='x "y"')], [_link("a", "b")]))
        roundtrip_mermaid(data)

    def test_roundtrip_rejects_non_mermaid_payload(self) -> None:
        with pytest.raises(ExportRoundTripError):
            roundtrip_mermaid(b"graph TD\n A --> B\n")

    def test_roundtrip_rejects_edge_to_undeclared_node(self) -> None:
        bad = (
            'flowchart LR\n  subgraph dom_P1["P1 \u00b7 Pembelajaran"]\n    n_a["a"]\n  end\n'
            "  n_a -->|supports| n_ghost\n"
        ).encode()
        with pytest.raises(ExportRoundTripError, match="undeclared"):
            roundtrip_mermaid(bad)

    def test_roundtrip_rejects_unsorted_subgraphs(self) -> None:
        bad = (
            "flowchart LR\n"
            '  subgraph dom_P7["P7 · Level Profesional"]\n    n_b["b"]\n  end\n'
            '  subgraph dom_P1["P1 · Pembelajaran"]\n    n_a["a"]\n  end\n'
        ).encode()
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_mermaid(bad)


# ---------------------------------------------------------------------- cap


class TestCap:
    def test_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="graph.mmd"):
            produce_mermaid(_model([_node("a" * 100)]), max_bytes=16)

    def test_default_caps_are_explicit(self) -> None:
        assert DEFAULT_MERMAID_MAX_BYTES > 0
        assert DEFAULT_MERMAID_MAX_NODES > 0


# -------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_mermaid_adds_format_entry(self) -> None:
        registry = ExportRegistry()
        register_mermaid(registry)
        assert "exports/graph.mmd" in registry.names(kind="format")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_mermaid(registry)
        artifacts = produce_exports(registry, _model([_node("a")]))
        assert artifacts["exports/graph.mmd"].startswith(b"flowchart LR\n")


# --------------------------------------------------------------- properties


_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"),
    min_size=1,
    max_size=10,
)
_LABEL = st.text(alphabet=st.characters(blacklist_categories=("Cs",)), max_size=30)


@settings(derandomize=True, max_examples=25)
@given(
    nodes=st.lists(
        st.tuples(_IDENTIFIER, _LABEL, st.integers(1, 7)), min_size=0, max_size=8, unique_by=lambda triple: triple[0]
    ),
    max_nodes=st.integers(1, 12),
)
def test_property_byte_deterministic_and_roundtrips(nodes: list[tuple[str, str, int]], max_nodes: int) -> None:
    node_dicts = [_node(node_id, label=label, domain=f"P{domain}") for node_id, label, domain in nodes]
    first = produce_mermaid(_model(node_dicts), max_nodes=max_nodes)
    shuffled = produce_mermaid(_model(list(reversed(node_dicts))), max_nodes=max_nodes)
    assert first == shuffled
    roundtrip_mermaid(first)
    # Elision honesty: declared elided count + shown count == total count.
    text = first.decode()
    shown = len(re.findall(r"^    n_\w+\[", text, flags=re.MULTILINE))
    marker = re.search(r"%% elided: (\d+) nodes", text)
    elided = int(marker.group(1)) if marker else 0
    assert shown + elided == len(nodes)

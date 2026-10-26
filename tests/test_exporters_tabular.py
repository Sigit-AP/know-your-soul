"""Tests for ``know_your_soul.exporters.tabular`` (ATM F-B3).

Pins the RFC 4180 CSV contract for ``nodes.csv`` / ``edges.csv``: exact column
sets (no secret-shaped free-text fields — labels, provenance, source paths and
context tags never leave the read model here), CRLF line endings with correct
quote doubling, rows in canonical order, in+out degree computed from the link
set, P/M/E/U/confidence carried as their quantized 12-decimal strings, and
explicit fail-closed byte caps per file.
"""

from __future__ import annotations

import csv
import io
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportRegistry,
    ExportRoundTripError,
    produce_exports,
)
from know_your_soul.exporters.tabular import (
    EDGE_COLUMNS,
    NODE_COLUMNS,
    NODES_CSV_MAX_BYTES,
    produce_edges_csv,
    produce_nodes_csv,
    register_tabular,
    render_edges_csv,
    render_nodes_csv,
    roundtrip_edges_csv,
    roundtrip_nodes_csv,
)

# ------------------------------------------------------------------ helpers


def _node(
    node_id: str,
    *,
    domain: str = "P1",
    kind: str = "primary",
    status: str = "active",
    version: int = 1,
    level: int = 2,
    p: str = "0.100000000000",
    m: str = "0.200000000000",
    e: str = "0.300000000000",
    u: str = "0.400000000000",
    confidence: str = "0.500000000000",
    community_sig: str = "sig001",
    label: str = "free text that must never reach the CSV",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "domain": domain,
        "kind": kind,
        "lifecycle_status": status,
        "version": version,
        "professional_level": level,
        "label": label,
        "source_file": "/secret/path.md",
        "provenance": ["vault://secret"],
        "context_tags": ["private"],
        "community_sig": community_sig,
        "scores": {"p": p, "m": m, "e": e, "uncertainty": u, "confidence": confidence},
        "confidence": confidence,
    }


def _link(source: str, target: str, *, relation: str = "supports", confidence: str = "EXTRACTED") -> dict[str, Any]:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "source_file": "/secret/path.md",
    }


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


def _rows(data: bytes) -> list[list[str]]:
    return list(csv.reader(io.StringIO(data.decode("utf-8"))))


# ------------------------------------------------------------------- columns


class TestColumns:
    def test_node_columns_are_exact_and_pinned(self) -> None:
        assert NODE_COLUMNS == (
            "id",
            "domain",
            "kind",
            "status",
            "level",
            "p",
            "m",
            "e",
            "u",
            "confidence",
            "version",
            "degree",
            "community_sig",
        )

    def test_edge_columns_are_exact_and_pinned(self) -> None:
        assert EDGE_COLUMNS == ("source", "target", "relation", "confidence")

    def test_no_secret_or_free_text_fields_in_nodes_csv(self) -> None:
        forbidden = {"label", "source_file", "provenance", "context_tags", "summary"}
        assert forbidden.isdisjoint(NODE_COLUMNS)
        text = render_nodes_csv(_model([_node("a")]))
        assert "free text" not in text
        assert "/secret/path.md" not in text
        assert "vault://secret" not in text

    def test_no_secret_fields_in_edges_csv(self) -> None:
        text = render_edges_csv(_model([_node("a"), _node("b")], [_link("a", "b")]))
        assert "/secret/path.md" not in text

    def test_header_row_is_first(self) -> None:
        rows = _rows(produce_nodes_csv(_model([_node("a")])))
        assert rows[0] == list(NODE_COLUMNS)
        edge_rows = _rows(produce_edges_csv(_model([], [])))
        assert edge_rows[0] == list(EDGE_COLUMNS)


# -------------------------------------------------------------------- values


class TestValues:
    def test_node_row_carries_scores_status_level_version(self) -> None:
        rows = _rows(produce_nodes_csv(_model([_node("a", p="0.900000000000", level=4, status="maintain", version=7)])))
        row = dict(zip(NODE_COLUMNS, rows[1], strict=True))
        assert row == {
            "id": "a",
            "domain": "P1",
            "kind": "primary",
            "status": "maintain",
            "level": "4",
            "p": "0.900000000000",
            "m": "0.200000000000",
            "e": "0.300000000000",
            "u": "0.400000000000",
            "confidence": "0.500000000000",
            "version": "7",
            "degree": "0",
            "community_sig": "sig001",
        }

    def test_missing_scores_fall_back_to_zero_vector(self) -> None:
        node = {"id": "bare", "domain": "P2"}
        rows = _rows(produce_nodes_csv(_model([node])))
        row = dict(zip(NODE_COLUMNS, rows[1], strict=True))
        assert row["p"] == "0.000000000000"
        assert row["u"] == "1.000000000000"
        assert row["level"] == "1"
        assert row["community_sig"] == ""

    def test_degree_counts_in_plus_out_over_links(self) -> None:
        links = [_link("a", "b"), _link("c", "a"), _link("a", "a")]  # self-loop: 1 out + 1 in
        rows = _rows(produce_nodes_csv(_model([_node("a"), _node("b"), _node("c")], links)))
        degree = {row[0]: row[NODE_COLUMNS.index("degree")] for row in rows[1:]}
        assert degree == {"a": "4", "b": "1", "c": "1"}

    def test_links_with_unknown_endpoints_do_not_inflate_degree(self) -> None:
        rows = _rows(produce_nodes_csv(_model([_node("a")], [_link("a", "ghost")])))
        assert dict(zip(NODE_COLUMNS, rows[1], strict=True))["degree"] == "0"

    def test_edge_rows_sorted_and_deduplicated(self) -> None:
        links = [
            _link("b", "a"),
            _link("a", "b", relation="supports"),
            _link("a", "b", relation="depends_on"),
            _link("a", "b", relation="supports"),  # duplicate
        ]
        rows = _rows(produce_edges_csv(_model([_node("a"), _node("b")], links)))
        assert rows[1:] == [
            ["a", "b", "depends_on", "EXTRACTED"],
            ["a", "b", "supports", "EXTRACTED"],
            ["b", "a", "supports", "EXTRACTED"],
        ]

    def test_edge_rows_skip_unknown_endpoints(self) -> None:
        rows = _rows(produce_edges_csv(_model([_node("a")], [_link("a", "ghost"), _link("ghost", "a")])))
        assert rows == [list(EDGE_COLUMNS)]


# ------------------------------------------------------------------ RFC 4180


class TestRfc4180:
    def test_crlf_line_endings_no_bare_lf(self) -> None:
        data = produce_nodes_csv(_model([_node("a"), _node("b")]))
        assert data.endswith(b"\r\n")
        body = data.decode("utf-8")
        assert "\n" in body
        for index in range(len(body)):
            if body[index] == "\n":
                assert body[index - 1] == "\r", "bare LF found"
        assert body.count("\r\n") == 3  # header + 2 rows

    def test_fields_with_comma_quote_crlf_are_quoted_and_doubled(self) -> None:
        node = _node('we,ird "q"\r\nid', community_sig='sig,"x"')
        data = produce_nodes_csv(_model([node]))
        rows = _rows(data)
        assert rows[1][0] == 'we,ird "q"\r\nid'  # parse-back recovers the exact value
        assert rows[1][NODE_COLUMNS.index("community_sig")] == 'sig,"x"'
        # Raw bytes show doubled quotes per RFC 4180 section 2 rule 7.
        assert b'""' in data

    def test_empty_graph_is_header_only(self) -> None:
        assert produce_nodes_csv(_model([])) == (",".join(NODE_COLUMNS) + "\r\n").encode()
        assert produce_edges_csv(_model([])) == (",".join(EDGE_COLUMNS) + "\r\n").encode()


# -------------------------------------------------------------- determinism


class TestDeterminism:
    def test_byte_identical_across_input_orderings(self) -> None:
        nodes = [_node("c", domain="P7"), _node("a", domain="P1"), _node("b", domain="P4")]
        links = [_link("b", "c"), _link("a", "b"), _link("a", "c")]
        forward_nodes = produce_nodes_csv(_model(nodes, links))
        backward_nodes = produce_nodes_csv(_model(list(reversed(nodes)), list(reversed(links))))
        assert forward_nodes == backward_nodes
        assert produce_edges_csv(_model(nodes, links)) == produce_edges_csv(
            _model(list(reversed(nodes)), list(reversed(links)))
        )

    def test_node_rows_sorted_by_id(self) -> None:
        rows = _rows(produce_nodes_csv(_model([_node("z"), _node("a"), _node("m")])))
        assert [row[0] for row in rows[1:]] == ["a", "m", "z"]


# --------------------------------------------------------------- round-trip


class TestRoundTrip:
    def test_produced_outputs_pass_roundtrip(self) -> None:
        model = _model([_node("a"), _node("b")], [_link("a", "b")])
        roundtrip_nodes_csv(produce_nodes_csv(model))
        roundtrip_edges_csv(produce_edges_csv(model))

    def test_roundtrip_rejects_wrong_header(self) -> None:
        with pytest.raises(ExportRoundTripError, match="header"):
            roundtrip_nodes_csv(b"id,domain\r\na,P1\r\n")

    def test_roundtrip_rejects_bare_lf(self) -> None:
        with pytest.raises(ExportRoundTripError, match="CRLF"):
            roundtrip_nodes_csv((",".join(NODE_COLUMNS) + "\n").encode())

    def test_roundtrip_rejects_ragged_rows(self) -> None:
        bad = (",".join(NODE_COLUMNS) + "\r\n" + "a,P1\r\n").encode()
        with pytest.raises(ExportRoundTripError, match="columns"):
            roundtrip_nodes_csv(bad)

    def test_roundtrip_rejects_unsorted_rows(self) -> None:
        scores = "0.100000000000,0.200000000000,0.300000000000,0.400000000000,0.500000000000"
        row_a = f"a,P1,primary,active,2,{scores},1,0,sig"
        row_b = f"b,P1,primary,active,2,{scores},1,0,sig"
        bad = (",".join(NODE_COLUMNS) + "\r\n" + row_b + "\r\n" + row_a + "\r\n").encode()
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_nodes_csv(bad)

    def test_roundtrip_rejects_non_utf8(self) -> None:
        with pytest.raises(ExportRoundTripError):
            roundtrip_edges_csv(b"\xff\xfe\x00")


# ---------------------------------------------------------------------- cap


class TestCap:
    def test_nodes_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="nodes.csv"):
            produce_nodes_csv(_model([_node("a" * 100)]), max_bytes=32)

    def test_edges_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="edges.csv"):
            produce_edges_csv(_model([_node("a"), _node("b")], [_link("a", "b")]), max_bytes=16)

    def test_default_caps_are_explicit(self) -> None:
        assert NODES_CSV_MAX_BYTES > 0


# -------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_tabular_adds_two_format_entries(self) -> None:
        registry = ExportRegistry()
        register_tabular(registry)
        assert "exports/nodes.csv" in registry.names(kind="format")
        assert "exports/edges.csv" in registry.names(kind="format")

    def test_orchestrator_produces_both_files_capped_and_validated(self) -> None:
        registry = ExportRegistry()
        register_tabular(registry)
        model = _model([_node("a"), _node("b")], [_link("a", "b")])
        artifacts = produce_exports(registry, model)
        assert artifacts["exports/nodes.csv"] == produce_nodes_csv(model)
        assert artifacts["exports/edges.csv"] == produce_edges_csv(model)


# --------------------------------------------------------------- properties


_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_"),
    min_size=1,
    max_size=10,
)


@settings(derandomize=True, max_examples=25)
@given(
    nodes=st.lists(
        st.tuples(_IDENTIFIER, st.integers(1, 7), st.integers(1, 5)),
        min_size=0,
        max_size=8,
        unique_by=lambda triple: triple[0],
    ),
    links=st.lists(st.tuples(st.integers(0, 7), st.integers(0, 7)), max_size=10),
)
def test_property_byte_deterministic_and_roundtrips(
    nodes: list[tuple[str, int, int]], links: list[tuple[int, int]]
) -> None:
    node_dicts = [_node(node_id, domain=f"P{domain}", level=level) for node_id, domain, level in nodes]
    ids = [node_id for node_id, _, _ in nodes]
    link_dicts = [_link(ids[s % len(ids)], ids[t % len(ids)]) for s, t in links if ids]
    model = _model(node_dicts, link_dicts)
    shuffled = _model(list(reversed(node_dicts)), list(reversed(link_dicts)))
    first_nodes, first_edges = produce_nodes_csv(model), produce_edges_csv(model)
    assert first_nodes == produce_nodes_csv(shuffled)
    assert first_edges == produce_edges_csv(shuffled)
    roundtrip_nodes_csv(first_nodes)
    roundtrip_edges_csv(first_edges)

"""Tests for ``know_your_soul.exporters.adjacency`` (ATM wave-2 formats).

Two artifacts over one canonical extraction:

* ``exports/adjacency.csv`` — a directed adjacency matrix as RFC 4180 CSV:
  header row ``id,<sorted ids>``, one row per node in the same order, cells
  ``"1"``/``"0"`` (a self-loop marks the diagonal). Explicit node cap (the
  matrix grows quadratically) and byte cap, both fail-closed typed.
* ``exports/edges.txt`` — a TAB-separated edge list with a three-line
  ``#``-header (format marker + node/edge counts), sorted unique lines,
  whitespace controls collapsed out of every field.
"""

from __future__ import annotations

from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.exporters.adjacency import (
    ADJACENCY_CSV_MAX_BYTES,
    ADJACENCY_MAX_NODES,
    EDGE_LIST_MAX_BYTES,
    EDGE_LIST_MAX_EDGES,
    produce_adjacency_csv,
    produce_edge_list_txt,
    register_adjacency,
    render_adjacency_csv,
    render_edge_list_txt,
    roundtrip_adjacency_csv,
    roundtrip_edge_list_txt,
)
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportRegistry,
    ExportRoundTripError,
    produce_exports,
)

# ------------------------------------------------------------------ helpers


def _node(node_id: str, *, domain: str = "P1") -> dict[str, Any]:
    return {"id": node_id, "domain": domain}


def _link(source: str, target: str, *, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


def _model(nodes: list[dict[str, Any]], links: list[dict[str, Any]] | None = None) -> dict[str, Any]:
    return {"directed": True, "multigraph": False, "graph": {}, "nodes": nodes, "links": links or []}


# ---------------------------------------------------------------- matrix csv


class TestAdjacencyCsv:
    def test_empty_model(self) -> None:
        assert render_adjacency_csv(_model([])) == "id\r\n"

    def test_header_and_rows_sorted_by_id(self) -> None:
        text = render_adjacency_csv(_model([_node("z"), _node("a"), _node("m")]))
        lines = text.split("\r\n")
        assert lines[0] == "id,a,m,z"
        assert [line.split(",")[0] for line in lines[1:] if line] == ["a", "m", "z"]

    def test_directed_cells(self) -> None:
        links = [_link("a", "b")]
        text = render_adjacency_csv(_model([_node("a"), _node("b")], links))
        lines = text.split("\r\n")
        assert lines[1] == "a,0,1"
        assert lines[2] == "b,0,0"

    def test_self_loop_marks_diagonal(self) -> None:
        text = render_adjacency_csv(_model([_node("a")], [_link("a", "a")]))
        assert text == "id,a\r\na,1\r\n"

    def test_duplicate_edges_still_single_mark(self) -> None:
        links = [_link("a", "b"), _link("a", "b"), _link("a", "b", relation="depends_on")]
        text = render_adjacency_csv(_model([_node("a"), _node("b")], links))
        assert text.split("\r\n")[1] == "a,0,1"

    def test_unknown_endpoint_dropped(self) -> None:
        text = render_adjacency_csv(_model([_node("a")], [_link("a", "ghost")]))
        assert text == "id,a\r\na,0\r\n"

    def test_node_id_with_comma_is_quoted(self) -> None:
        text = render_adjacency_csv(_model([_node("a,1"), _node("b")]))
        assert text.split("\r\n")[0] == 'id,"a,1",b'

    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a"), _node("b"), _node("c")]
        links = [_link("a", "b"), _link("c", "a")]
        assert produce_adjacency_csv(_model(nodes, links)) == produce_adjacency_csv(
            _model(list(reversed(nodes)), list(reversed(links)))
        )

    def test_node_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="node"):
            produce_adjacency_csv(_model([_node("a"), _node("b")]), max_nodes=1)

    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="adjacency.csv"):
            produce_adjacency_csv(_model([_node("a" * 100)]), max_bytes=16)

    def test_default_caps_are_explicit(self) -> None:
        assert ADJACENCY_CSV_MAX_BYTES > 0
        assert ADJACENCY_MAX_NODES > 0

    def test_invalid_params_rejected(self) -> None:
        with pytest.raises(ExportError):
            produce_adjacency_csv(_model([]), max_bytes=0)
        with pytest.raises(ExportError):
            produce_adjacency_csv(_model([]), max_nodes=0)


class TestAdjacencyCsvRoundTrip:
    def test_valid_passes(self) -> None:
        data = produce_adjacency_csv(_model([_node("a"), _node("b")], [_link("a", "b")]))
        roundtrip_adjacency_csv(data)

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_adjacency_csv(b"\xff\xfe,")

    def test_bare_lf_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="CRLF"):
            roundtrip_adjacency_csv(b"id,a\na,0\n")

    def test_bad_header_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="header"):
            roundtrip_adjacency_csv(b"node,a\r\na,0\r\n")

    def test_unsorted_ids_fail(self) -> None:
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_adjacency_csv(b"id,b,a\r\nb,0,0\r\na,0,0\r\n")

    def test_non_binary_cell_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="cell"):
            roundtrip_adjacency_csv(b"id,a\r\na,2\r\n")

    def test_ragged_row_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="column|width"):
            roundtrip_adjacency_csv(b"id,a,b\r\na,0\r\nb,0,0\r\n")

    def test_row_id_not_in_header_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="row"):
            roundtrip_adjacency_csv(b"id,a\r\nb,0\r\n")


# --------------------------------------------------------------- edge list


class TestEdgeListTxt:
    def test_empty_model(self) -> None:
        text = render_edge_list_txt(_model([_node("a")]))
        assert text == "# know-your-soul edge-list v1\n# nodes: 1\n# edges: 0\n"

    def test_node_count_includes_isolated_nodes(self) -> None:
        text = render_edge_list_txt(_model([_node("a"), _node("b"), _node("c")], [_link("a", "b")]))
        assert "# nodes: 3\n# edges: 1\n" in text

    def test_self_loop_listed(self) -> None:
        text = render_edge_list_txt(_model([_node("a")], [_link("a", "a")]))
        assert text.split("\n")[3] == "a\ta\tsupports"

    def test_lines_sorted_and_tab_separated(self) -> None:
        links = [_link("b", "c"), _link("a", "c"), _link("a", "b", relation="depends_on"), _link("a", "b")]
        text = render_edge_list_txt(_model([_node("a"), _node("b"), _node("c")], links))
        body = text.split("\n")[3:]
        assert body == ["a\tb\tdepends_on", "a\tb\tsupports", "a\tc\tsupports", "b\tc\tsupports", ""]

    def test_counts_in_header(self) -> None:
        links = [_link("a", "b"), _link("a", "b")]
        text = render_edge_list_txt(_model([_node("a"), _node("b")], links))
        assert "# nodes: 2\n# edges: 1\n" in text

    def test_unknown_endpoint_dropped(self) -> None:
        text = render_edge_list_txt(_model([_node("a")], [_link("a", "ghost")]))
        assert "# edges: 0\n" in text

    def test_whitespace_controls_collapse_out_of_fields(self) -> None:
        links = [_link("a", "b", relation="rel\twith\ttabs")]
        text = render_edge_list_txt(_model([_node("a"), _node("b")], links))
        assert "a\tb\trel with tabs\n" in text

    def test_bidi_stripped_from_relation(self) -> None:
        links = [_link("a", "b", relation="sup\u202eports")]
        text = render_edge_list_txt(_model([_node("a"), _node("b")], links))
        assert "a\tb\tsupports\n" in text

    def test_permuted_input_is_byte_identical(self) -> None:
        nodes = [_node("a"), _node("b")]
        links = [_link("a", "b"), _link("b", "a")]
        assert produce_edge_list_txt(_model(nodes, links)) == produce_edge_list_txt(
            _model(list(reversed(nodes)), list(reversed(links)))
        )

    def test_edge_cap_breach_fails_closed_typed(self) -> None:
        links = [_link("a", "b"), _link("b", "a")]
        with pytest.raises(ExportCapExceededError, match="edge"):
            produce_edge_list_txt(_model([_node("a"), _node("b")], links), max_edges=1)

    def test_byte_cap_breach_fails_closed_typed(self) -> None:
        with pytest.raises(ExportCapExceededError, match="edges.txt"):
            produce_edge_list_txt(_model([_node("a"), _node("b")], [_link("a", "b")]), max_bytes=32)

    def test_default_caps_are_explicit(self) -> None:
        assert EDGE_LIST_MAX_BYTES > 0
        assert EDGE_LIST_MAX_EDGES > 0


class TestEdgeListRoundTrip:
    def test_valid_passes(self) -> None:
        data = produce_edge_list_txt(_model([_node("a"), _node("b")], [_link("a", "b")]))
        roundtrip_edge_list_txt(data)

    def test_non_utf8_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="UTF-8"):
            roundtrip_edge_list_txt(b"\xff\xfe#")

    def test_missing_header_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="header"):
            roundtrip_edge_list_txt(b"a\tb\tsupports\n")

    def test_count_mismatch_fails(self) -> None:
        data = produce_edge_list_txt(_model([_node("a"), _node("b")], [_link("a", "b")])).replace(
            b"# edges: 1", b"# edges: 2", 1
        )
        with pytest.raises(ExportRoundTripError, match="count"):
            roundtrip_edge_list_txt(data)

    def test_unsorted_lines_fail(self) -> None:
        with pytest.raises(ExportRoundTripError, match="order"):
            roundtrip_edge_list_txt(
                b"# know-your-soul edge-list v1\n# nodes: 2\n# edges: 2\nb\ta\tsupports\na\tb\tsupports\n"
            )

    def test_wrong_field_count_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="field"):
            roundtrip_edge_list_txt(b"# know-your-soul edge-list v1\n# nodes: 2\n# edges: 1\na\tb\n")

    def test_crlf_fails(self) -> None:
        with pytest.raises(ExportRoundTripError, match="LF"):
            roundtrip_edge_list_txt(b"# know-your-soul edge-list v1\r\n# nodes: 0\r\n# edges: 0\r\n")


# ----------------------------------------------------------- registry seam


class TestRegistryIntegration:
    def test_register_adjacency_adds_both_entries(self) -> None:
        registry = ExportRegistry()
        register_adjacency(registry)
        assert "exports/adjacency.csv" in registry.names(kind="format")
        assert "exports/edges.txt" in registry.names(kind="format")

    def test_orchestrator_enforces_cap_and_roundtrip(self) -> None:
        registry = ExportRegistry()
        register_adjacency(registry)
        artifacts = produce_exports(registry, _model([_node("a"), _node("b")], [_link("a", "b")]))
        assert artifacts["exports/adjacency.csv"].startswith(b"id,a,b\r\n")
        assert artifacts["exports/edges.txt"].endswith(b"a\tb\tsupports\n")


# --------------------------------------------------------------- properties


_IDENTIFIER = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-_./"),
    min_size=1,
    max_size=10,
)


@settings(derandomize=True, max_examples=25)
@given(
    ids=st.lists(_IDENTIFIER, min_size=0, max_size=8, unique=True),
    edges=st.lists(st.integers(0, 7), min_size=0, max_size=6),
)
def test_property_matrix_and_list_deterministic_and_roundtrip(ids: list[str], edges: list[int]) -> None:
    nodes = [_node(node_id) for node_id in ids]
    links = [_link(ids[i % len(ids)], ids[(i + 1) % len(ids)]) for i in edges if ids]
    matrix_a = produce_adjacency_csv(_model(nodes, links))
    matrix_b = produce_adjacency_csv(_model(list(reversed(nodes)), list(reversed(links))))
    assert matrix_a == matrix_b
    roundtrip_adjacency_csv(matrix_a)
    listed_a = produce_edge_list_txt(_model(nodes, links))
    listed_b = produce_edge_list_txt(_model(list(reversed(nodes)), list(reversed(links))))
    assert listed_a == listed_b
    roundtrip_edge_list_txt(listed_a)
    # Consistency invariant: number of 1-cells == number of unique (s,t) pairs.
    ones = sum(line.count(",1") for line in matrix_a.decode().split("\r\n")[1:-1])
    pairs = {(link["source"], link["target"]) for link in links}
    assert ones == len(pairs)

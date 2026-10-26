"""Tests for ``know_your_soul.report.report_v2`` (ATM plan F-C3).

The report is a canonical structured dict plus a deterministic markdown
render. Every count flows through the single ``is_real_node`` predicate so
the report can never disagree with itself (the Graphify ``thin+shown``
double-definition bug, pinned here as explicit invariants).
"""

from __future__ import annotations

import pytest

from know_your_soul.domain import canonical_json
from know_your_soul.report.report_v2 import (
    CONFIDENCE_BUCKETS,
    ReportIntegrityError,
    build_graph_report,
    is_real_node,
    render_markdown,
)

_ZERO = "0.000000000000"


def _node(
    node_id: str,
    *,
    label: str | None = None,
    domain: str = "P1",
    kind: str = "leaf",
    p: str = "0.100000000000",
    m: str = "0.200000000000",
    e: str = "0.300000000000",
    uncertainty: str = "0.400000000000",
    confidence: str = "0.500000000000",
    provenance: list[str] | None = None,
    source_version_id: str = "sv-1",
    community_sig: str | None = None,
) -> dict:
    node = {
        "id": node_id,
        "label": node_id if label is None else label,
        "file_type": "document",
        "source_file": "docs/a.md",
        "domain": domain,
        "kind": kind,
        "lifecycle_status": "active",
        "version": 1,
        "source_version_id": source_version_id,
        "provenance": ["docs/a.md"] if provenance is None else provenance,
        "context_tags": [],
        "scores": {"p": p, "m": m, "e": e, "uncertainty": uncertainty, "confidence": confidence},
        "confidence": confidence,
        "professional_level": 1,
    }
    if community_sig is not None:
        node["community_sig"] = community_sig
    return node


def _link(source: str, target: str, *, relation: str = "supports", confidence: str = "EXTRACTED") -> dict:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": confidence,
        "confidence_score": 1.0,
        "weight": 1.0,
        "source_file": "docs/a.md",
    }


def _graph(nodes: list[dict], links: list[dict] | None = None) -> dict:
    return {
        "directed": True,
        "multigraph": False,
        "graph": {"schema_version": 1, "generator": "know-your-soul", "partitioner": "test"},
        "nodes": nodes,
        "links": links or [],
        "hyperedges": [],
    }


def _manifest(*, watermark: int = 7, node_count: int = 0, edge_count: int = 0) -> dict:
    return {
        "manifest_version": 1,
        "projection_version": "gen-1",
        "content_hash": "ab" * 32,
        "source_knowledge_high_watermark": watermark,
        "schema_version": 1,
        "partitioner": "test",
        "artifacts": {},
        "graph_content_hash": "cd" * 32,
        "node_count": node_count,
        "edge_count": edge_count,
        "multigraph": False,
    }


# ------------------------------------------------------------ single predicate


def test_is_real_node_rejects_non_dict_and_blank_identity() -> None:
    assert not is_real_node("not-a-node")
    assert not is_real_node({"label": "x"})  # missing id
    assert not is_real_node({"id": "", "label": "x"})
    assert not is_real_node({"id": "x", "label": "   "})
    assert not is_real_node({"id": "x", "label": 3})
    assert is_real_node({"id": "x", "label": "x"})


def test_every_count_uses_the_single_real_node_predicate() -> None:
    graph = _graph(
        [
            _node("a", community_sig="c1"),
            _node("b", community_sig="c1"),
            _node("c", community_sig="c2"),
            _node("junk", label="  ", community_sig="c2"),
        ],
        [
            _link("a", "b"),
            _link("a", "junk", confidence="AMBIGUOUS"),  # endpoint not real: excluded everywhere
        ],
    )
    report = build_graph_report(graph, _manifest(node_count=4, edge_count=2))
    real = 3
    assert report["counts"]["nodes_total"] == 4
    assert report["counts"]["nodes_real"] == real
    communities = report["communities"]
    # thin + shown equals the number of real communities (no third bucket).
    assert communities["thin"] + communities["shown"] == communities["total"]
    # and the node-level split accounts for every real node exactly once.
    assert communities["thin_nodes"] + communities["shown_nodes"] + communities["unassigned_nodes"] == real
    assert communities["thin"] == 1 and communities["shown"] == 1
    # the edge to a non-real node is not counted anywhere.
    assert report["counts"]["edges_real"] == 1
    assert sum(report["confidence_distribution"]["edges"].values()) == 1


def test_confidence_distribution_edges_and_node_buckets() -> None:
    graph = _graph(
        [
            _node("a", confidence="0.900000000000"),
            _node("b", confidence="0.500000000000"),
            _node("c", confidence="0.100000000000"),
        ],
        [_link("a", "b"), _link("b", "c", confidence="INFERRED"), _link("a", "c", confidence="AMBIGUOUS")],
    )
    report = build_graph_report(graph, _manifest())
    assert report["confidence_distribution"]["edges"] == {"EXTRACTED": 1, "INFERRED": 1, "AMBIGUOUS": 1}
    buckets = report["confidence_distribution"]["node_buckets"]
    assert set(buckets) == {name for name, _floor in CONFIDENCE_BUCKETS}
    assert buckets == {"high": 1, "medium": 1, "low": 1}


# ------------------------------------------------------------- domain summary


def test_per_domain_pme_u_means_are_quantized_strings() -> None:
    graph = _graph(
        [
            _node(
                "a",
                domain="P1",
                p="0.100000000000",
                m="0.200000000000",
                e="0.300000000000",
                uncertainty="0.400000000000",
            ),
            _node(
                "b",
                domain="P1",
                p="0.200000000000",
                m="0.400000000000",
                e="0.600000000000",
                uncertainty="0.800000000000",
            ),
            _node("c", domain="P2", p="0.500000000000"),
        ]
    )
    report = build_graph_report(graph, _manifest())
    domains = report["domains"]
    assert [entry["domain"] for entry in domains] == ["P1", "P2"]
    p1 = domains[0]
    assert p1["nodes"] == 2
    assert p1["mean"] == {
        "p": "0.150000000000",
        "m": "0.300000000000",
        "e": "0.450000000000",
        "uncertainty": "0.600000000000",
    }
    assert domains[1]["mean"]["p"] == "0.500000000000"
    assert domains[1]["mean"]["m"] == "0.200000000000"


def test_domain_mean_half_even_rounding_is_deterministic() -> None:
    # (1 + 2) / 2 at the 12th decimal: 0.0000000000015 -> HALF_EVEN -> ...2
    graph = _graph(
        [
            _node("a", p="0.000000000001"),
            _node("b", p="0.000000000002"),
        ]
    )
    report = build_graph_report(graph, _manifest())
    assert report["domains"][0]["mean"]["p"] == "0.000000000002"


# ------------------------------------------------------------------- staleness


def test_staleness_compares_manifest_to_store_watermark() -> None:
    graph = _graph([_node("a")])
    fresh = build_graph_report(graph, _manifest(watermark=7), store_watermark=7)
    assert fresh["staleness"] == {
        "known": True,
        "manifest_watermark": 7,
        "store_watermark": 7,
        "lag": 0,
        "stale": False,
    }
    stale = build_graph_report(graph, _manifest(watermark=5), store_watermark=7)
    assert stale["staleness"]["stale"] is True
    assert stale["staleness"]["lag"] == 2
    ahead = build_graph_report(graph, _manifest(watermark=9), store_watermark=7)
    assert ahead["staleness"]["stale"] is True
    assert ahead["staleness"]["lag"] == -2


def test_staleness_unknown_without_store_watermark() -> None:
    report = build_graph_report(_graph([_node("a")]), _manifest())
    assert report["staleness"] == {"known": False}


# -------------------------------------------------------------- review signals


def test_top_review_signals_sorted_capped_and_loosely_coupled() -> None:
    signals = [
        {"code": "low_confidence", "severity": "info", "subject": "n3"},
        {"code": "isolated_node", "severity": "warning", "subject": "n1"},
        {"code": "low_confidence", "severity": "info", "subject": "n2"},
        {"code": "dangling", "severity": "critical", "subject": "n9"},
    ]
    report = build_graph_report(
        _graph([_node("a")]),
        _manifest(),
        review_signals=signals,
        top_review_signals=2,
    )
    top = report["review_signals"]
    assert top["total"] == 4
    assert top["shown"] == 2
    assert top["omitted"] == 2
    # severity first (critical before warning before info), then code, then subject.
    assert [(s["severity"], s["code"], s["subject"]) for s in top["top"]] == [
        ("critical", "dangling", "n9"),
        ("warning", "isolated_node", "n1"),
    ]


def test_review_signals_default_empty() -> None:
    report = build_graph_report(_graph([_node("a")]), _manifest())
    assert report["review_signals"] == {"total": 0, "shown": 0, "omitted": 0, "top": []}


# --------------------------------------------------------------- determinism


def test_report_is_byte_stable_and_input_order_independent() -> None:
    nodes = [_node("b"), _node("a", domain="P2"), _node("c", community_sig="c1")]
    links = [_link("b", "a"), _link("a", "c", confidence="INFERRED")]
    first = build_graph_report(_graph(nodes, links), _manifest(), store_watermark=3)
    shuffled = build_graph_report(
        _graph(list(reversed(nodes)), list(reversed(links))),
        _manifest(),
        store_watermark=3,
    )
    assert canonical_json(first) == canonical_json(shuffled)
    assert render_markdown(first) == render_markdown(shuffled)


# --------------------------------------------------------------- markdown


def test_render_markdown_sections_and_content() -> None:
    graph = _graph([_node("a", community_sig="c1"), _node("b", community_sig="c1")], [_link("a", "b")])
    report = build_graph_report(graph, _manifest(watermark=1), store_watermark=2)
    text = render_markdown(report)
    assert text.startswith("# SOUL Knowledge Graph Report (v2)")
    for heading in (
        "## Counts",
        "## Confidence distribution",
        "## Domains",
        "## Communities",
        "## Staleness",
        "## Top review signals",
    ):
        assert heading in text
    assert "stale" in text
    assert text.endswith("\n")


def test_render_rejects_a_report_that_is_not_ours() -> None:
    with pytest.raises(ReportIntegrityError):
        render_markdown({"schema_version": 999})


# ------------------------------------------------- no absolute paths in output


def test_absolute_paths_in_output_fail_closed() -> None:
    # Free text enters the report through review-signal subjects; an absolute
    # checkout path shape there must fail closed, never render.
    graph = _graph([_node("a")])
    with pytest.raises(ReportIntegrityError):
        build_graph_report(
            graph,
            _manifest(),
            review_signals=[{"code": "leak", "severity": "info", "subject": "see /etc/passwd/file"}],
        )


def test_no_absolute_paths_in_markdown_for_clean_input() -> None:
    graph = _graph([_node("a", label="relative docs/a.md only")])
    report = build_graph_report(graph, _manifest())
    assert "/root/" not in render_markdown(report)

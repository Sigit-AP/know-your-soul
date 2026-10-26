"""Tests for ``know_your_soul.analytics.query_trace`` (ATM plan F-C5).

A retrieval explanation trace is a pure function over query input and the
service's own outputs: terms with IDF, ranked candidates, quota cuts,
traversal path, rejection reasons. Nothing about the service changes; the
trace is a canonical dict ready to be hashed as an artifact, and aggregates
yield matcher-quality metrics (no-match / ambiguity rates).
"""

from __future__ import annotations

from decimal import Decimal

from know_your_soul.analytics.query_trace import aggregate_traces, build_query_trace
from know_your_soul.domain import canonical_json
from know_your_soul.retrieval import LexicalIndex, query_terms


def _index() -> LexicalIndex:
    return LexicalIndex.from_entries(
        [
            {"node_id": "a", "fields": {"summary": ["bayes", "prior"]}},
            {"node_id": "b", "fields": {"summary": ["bayes", "posterior"]}},
            {"node_id": "c", "fields": {"summary": ["fishing"]}},
        ]
    )


# ------------------------------------------------------------------- trace build


def test_trace_terms_carry_quantized_idf() -> None:
    index = _index()
    trace = build_query_trace(purpose="bayes prior", route_status="ranked", root_node_id="b", index=index)
    assert trace["schema_version"] == 1
    terms = trace["terms"]
    assert [t["term"] for t in terms] == query_terms("bayes prior")
    for entry in terms:
        expected = format(index.idf(entry["term"]), ".12f")
        assert entry["idf"] == expected
        assert entry["document_frequency"] == index.document_frequency(entry["term"])


def test_trace_without_index_lists_terms_without_idf() -> None:
    trace = build_query_trace(purpose="bayes", route_status="no_match")
    # normalize_term folds the trailing "s": the indexed term is "baye".
    assert trace["terms"] == [{"term": "baye", "idf": None, "document_frequency": None}]


def test_trace_records_routing_candidates_quota_traversal_rejections() -> None:
    trace = build_query_trace(
        purpose="bayes",
        context_tags=["stats"],
        domain=None,
        route_status="ambiguous",
        route_candidates=["P1", "P2"],
        index=_index(),
        candidates=[
            {"node_id": "b", "score": Decimal("2.5"), "field_rank": Decimal("1")},
            {"node_id": "a", "score": Decimal("2.5"), "field_rank": Decimal("1")},
        ],
        quota={"max_results": 1, "offered": 2, "cut": ["a"]},
        traversal={"nodes": ["b", "d"], "edges": ["b -[supports]-> d"], "cross_edges": [], "guards_hit": ["max_depth"]},
        rejections=[{"node_id": "c", "reason": "low_confidence"}],
        result_node_ids=["b", "d"],
    )
    assert trace["query"] == {"purpose": "bayes", "context_tags": ["stats"], "domain": None}
    assert trace["routing"] == {"status": "ambiguous", "root_node_id": None, "candidates": ["P1", "P2"]}
    # candidate ties break on ascending node id; scores are 12dp strings.
    assert [(c["node_id"], c["score"]) for c in trace["candidates"]] == [
        ("a", "2.500000000000"),
        ("b", "2.500000000000"),
    ]
    assert trace["quota"] == {"max_results": 1, "offered": 2, "cut": ["a"], "cut_count": 1}
    assert trace["traversal"]["guards_hit"] == ["max_depth"]
    assert trace["rejections"] == [{"node_id": "c", "reason": "low_confidence"}]
    assert trace["result_node_ids"] == ["b", "d"]
    # canonical: JSON-round-trips with sorted keys and no Decimal leakage.
    assert isinstance(canonical_json(trace), str)


def test_candidates_accept_score_strings_and_sort_by_score_then_id() -> None:
    trace = build_query_trace(
        purpose="x",
        route_status="ranked",
        root_node_id="a",
        candidates=[
            {"node_id": "z", "score": "1.000000000000"},
            {"node_id": "a", "score": "2.000000000000"},
        ],
    )
    assert [c["node_id"] for c in trace["candidates"]] == ["a", "z"]
    assert trace["candidates"][0]["field_rank"] == "0.000000000000"


def test_rejections_sorted_and_quota_defaults() -> None:
    trace = build_query_trace(
        purpose="x",
        route_status="ranked",
        root_node_id="a",
        rejections=[{"node_id": "z", "reason": "r"}, {"node_id": "a", "reason": "r2"}],
    )
    assert [r["node_id"] for r in trace["rejections"]] == ["a", "z"]
    assert trace["quota"] == {"max_results": None, "offered": 0, "cut": [], "cut_count": 0}


def test_trace_is_input_order_independent() -> None:
    first = build_query_trace(
        purpose="bayes",
        route_status="ranked",
        root_node_id="a",
        candidates=[{"node_id": "a", "score": "1.000000000000"}, {"node_id": "b", "score": "2.000000000000"}],
        rejections=[{"node_id": "b", "reason": "x"}, {"node_id": "a", "reason": "y"}],
    )
    second = build_query_trace(
        purpose="bayes",
        route_status="ranked",
        root_node_id="a",
        candidates=[{"node_id": "b", "score": "2.000000000000"}, {"node_id": "a", "score": "1.000000000000"}],
        rejections=[{"node_id": "a", "reason": "y"}, {"node_id": "b", "reason": "x"}],
    )
    assert canonical_json(first) == canonical_json(second)


# ------------------------------------------------------------------- aggregates


def test_aggregate_empty_is_zeroed() -> None:
    summary = aggregate_traces([])
    assert summary["total"] == 0
    assert summary["no_match_rate"] == "0.000000000000"
    assert summary["ambiguous_rate"] == "0.000000000000"
    assert summary["mean_terms"] == "0.000000000000"
    assert summary["mean_candidates"] == "0.000000000000"
    assert summary["by_status"] == {}
    assert summary["rejection_reasons"] == {}


def test_aggregate_matcher_quality_metrics() -> None:
    traces = [
        build_query_trace(
            purpose="bayes prior",
            route_status="ranked",
            root_node_id="a",
            candidates=[{"node_id": "a", "score": "1.000000000000"}],
        ),
        build_query_trace(purpose="zzzz", route_status="no_match"),
        build_query_trace(purpose="dup", route_status="ambiguous", route_candidates=["P1", "P2"]),
        build_query_trace(
            purpose="gone", route_status="no_match", rejections=[{"node_id": "q", "reason": "low_confidence"}]
        ),
    ]
    summary = aggregate_traces(traces)
    assert summary["total"] == 4
    assert summary["by_status"] == {"ambiguous": 1, "no_match": 2, "ranked": 1}
    assert summary["no_match_rate"] == "0.500000000000"
    assert summary["ambiguous_rate"] == "0.250000000000"
    # terms: 2 + 1 + 1 + 1 = 5 over 4 traces.
    assert summary["mean_terms"] == "1.250000000000"
    # candidates: 1 + 0 + 0 + 0 over 4.
    assert summary["mean_candidates"] == "0.250000000000"
    assert summary["rejection_reasons"] == {"low_confidence": 1}


def test_aggregate_is_input_order_independent() -> None:
    traces = [
        build_query_trace(purpose="a", route_status="ranked", root_node_id="x"),
        build_query_trace(purpose="b c", route_status="no_match"),
    ]
    assert canonical_json(aggregate_traces(traces)) == canonical_json(aggregate_traces(list(reversed(traces))))

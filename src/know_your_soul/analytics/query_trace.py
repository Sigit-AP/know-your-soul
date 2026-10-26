"""Retrieval explanation traces (ATM plan F-C5).

Pure functions over query input and the service's own outputs: nothing here
touches ``service.py`` and the frozen query-response contract is unchanged.
:func:`build_query_trace` assembles a canonical, artifact-ready dict — the
terms used with their IDF, ranked candidates, quota cuts, the traversal
path and rejection reasons — and :func:`aggregate_traces` reduces a set of
traces into matcher-quality metrics (no-match and ambiguity rates).
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal, localcontext
from typing import Any

from know_your_soul.domain import quantize_score
from know_your_soul.retrieval import LexicalIndex, query_terms

__all__ = [
    "TRACE_SCHEMA_VERSION",
    "aggregate_traces",
    "build_query_trace",
]

TRACE_SCHEMA_VERSION = 1


def _q12(value: Decimal) -> str:
    """The 12-decimal fixed-point rendering used for every traced number."""
    return format(quantize_score(value), "f")


def _mean_12dp(total: int, count: int) -> str:
    if count == 0:
        return "0.000000000000"
    with localcontext() as context:
        context.prec = 34
        return _q12(Decimal(total) / Decimal(count))


def _terms(purpose: str, context_tags: Iterable[str]) -> list[str]:
    terms = query_terms(purpose)
    for tag in context_tags:
        terms.extend(query_terms(str(tag)))
    return list(dict.fromkeys(terms))


def build_query_trace(
    *,
    purpose: str,
    route_status: str,
    context_tags: Iterable[str] = (),
    domain: str | None = None,
    root_node_id: str | None = None,
    route_candidates: Iterable[str] = (),
    index: LexicalIndex | None = None,
    candidates: Iterable[Mapping[str, Any]] = (),
    quota: Mapping[str, Any] | None = None,
    traversal: Mapping[str, Any] | None = None,
    rejections: Iterable[Mapping[str, Any]] = (),
    result_node_ids: Iterable[str] = (),
) -> dict[str, Any]:
    """Assemble the canonical explanation trace for one query invocation.

    ``index`` is the :class:`~know_your_soul.retrieval.LexicalIndex` the
    query ran against; without it terms are recorded without IDF. Scores may
    be ``Decimal`` or string; every number is rendered as a 12-decimal
    string so the trace is JSON-canonical and hashable as an artifact.
    """
    tags = [str(tag) for tag in context_tags]
    terms = _terms(purpose, tags)
    term_entries: list[dict[str, Any]] = []
    for term in terms:
        if index is None:
            term_entries.append({"term": term, "idf": None, "document_frequency": None})
        else:
            term_entries.append(
                {
                    "term": term,
                    "idf": _q12(index.idf(term)),
                    "document_frequency": index.document_frequency(term),
                }
            )

    normalized_candidates = [
        {
            "node_id": str(candidate.get("node_id")),
            "score": _q12(Decimal(str(candidate.get("score", "0")))),
            "field_rank": _q12(Decimal(str(candidate.get("field_rank", "0")))),
        }
        for candidate in candidates
    ]
    normalized_candidates.sort(
        key=lambda item: (-Decimal(item["score"]), -Decimal(item["field_rank"]), item["node_id"])
    )

    if quota is None:
        quota_section: dict[str, Any] = {"max_results": None, "offered": 0, "cut": [], "cut_count": 0}
    else:
        cut = sorted(str(node_id) for node_id in quota.get("cut", ()))
        quota_section = {
            "max_results": quota.get("max_results"),
            "offered": int(quota.get("offered", 0)),
            "cut": cut,
            "cut_count": len(cut),
        }

    if traversal is None:
        traversal_section: dict[str, Any] = {"nodes": [], "edges": [], "cross_edges": [], "guards_hit": []}
    else:
        traversal_section = {
            "nodes": [str(node_id) for node_id in traversal.get("nodes", ())],
            "edges": [str(edge) for edge in traversal.get("edges", ())],
            "cross_edges": [str(edge) for edge in traversal.get("cross_edges", ())],
            "guards_hit": sorted(str(guard) for guard in traversal.get("guards_hit", ())),
        }

    rejection_entries = sorted(
        ({"node_id": str(item.get("node_id")), "reason": str(item.get("reason"))} for item in rejections),
        key=lambda item: (item["node_id"], item["reason"]),
    )

    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "query": {"purpose": str(purpose), "context_tags": tags, "domain": domain},
        "terms": term_entries,
        "routing": {
            "status": str(route_status),
            "root_node_id": root_node_id,
            "candidates": sorted(str(candidate) for candidate in route_candidates),
        },
        "candidates": normalized_candidates,
        "quota": quota_section,
        "traversal": traversal_section,
        "rejections": rejection_entries,
        "result_node_ids": [str(node_id) for node_id in result_node_ids],
    }


def aggregate_traces(traces: Iterable[Mapping[str, Any]]) -> dict[str, Any]:
    """Matcher-quality metrics over a set of traces (deterministic)."""
    collected = list(traces)
    total = len(collected)
    by_status: dict[str, int] = {}
    rejection_reasons: dict[str, int] = {}
    term_total = 0
    candidate_total = 0
    for trace in collected:
        status = str(trace.get("routing", {}).get("status", "unknown"))
        by_status[status] = by_status.get(status, 0) + 1
        term_total += len(trace.get("terms", ()))
        candidate_total += len(trace.get("candidates", ()))
        for rejection in trace.get("rejections", ()):
            reason = str(rejection.get("reason"))
            rejection_reasons[reason] = rejection_reasons.get(reason, 0) + 1
    return {
        "schema_version": TRACE_SCHEMA_VERSION,
        "total": total,
        "by_status": dict(sorted(by_status.items())),
        "no_match_rate": _mean_12dp(by_status.get("no_match", 0), total),
        "ambiguous_rate": _mean_12dp(by_status.get("ambiguous", 0), total),
        "mean_terms": _mean_12dp(term_total, total),
        "mean_candidates": _mean_12dp(candidate_total, total),
        "rejection_reasons": dict(sorted(rejection_reasons.items())),
    }

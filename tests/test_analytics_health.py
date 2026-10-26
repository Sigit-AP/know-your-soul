"""Tests for ``know_your_soul.analytics.health`` (ATM plan F-C4).

Audit-style corpus health: deterministic findings with severity and evidence,
a quantized health score, and fail-closed semantics on critical findings.
Junk classification reuses the read-model validator's own junk reasons.
"""

from __future__ import annotations

from know_your_soul.analytics.health import SEVERITY_DEDUCTION, analyze_corpus_health
from know_your_soul.domain import canonical_json


def _node(
    node_id: str,
    *,
    label: str | None = None,
    file_type: str = "document",
    provenance: list[str] | None = None,
    source_version_id: str = "sv-1",
    community_sig: str | None = None,
) -> dict:
    node = {
        "id": node_id,
        "label": node_id if label is None else label,
        "file_type": file_type,
        "source_file": "docs/a.md",
        "domain": "P1",
        "kind": "leaf",
        "lifecycle_status": "active",
        "version": 1,
        "source_version_id": source_version_id,
        "provenance": ["docs/a.md"] if provenance is None else provenance,
        "context_tags": [],
        "scores": {
            "p": "0.100000000000",
            "m": "0.200000000000",
            "e": "0.300000000000",
            "uncertainty": "0.400000000000",
            "confidence": "0.500000000000",
        },
        "confidence": "0.500000000000",
        "professional_level": 1,
    }
    if community_sig is not None:
        node["community_sig"] = community_sig
    return node


def _link(source: str, target: str, *, relation: str = "supports") -> dict:
    return {
        "source": source,
        "target": target,
        "relation": relation,
        "confidence": "EXTRACTED",
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


def _codes(report: dict) -> list[str]:
    return [finding["code"] for finding in report["findings"]]


# ------------------------------------------------------------------ clean graph


def test_clean_corpus_is_ok_with_perfect_score() -> None:
    graph = _graph([_node("a", community_sig="c1"), _node("b", community_sig="c1")], [_link("a", "b")])
    report = analyze_corpus_health(graph)
    assert report["ok"] is True
    assert report["score"] == "1.000000000000"
    assert report["findings"] == []
    assert report["counts"] == {"nodes": 2, "links": 1, "communities": 1}


# ------------------------------------------------------------------ junk nodes


def test_junk_node_is_critical_and_uses_validator_reasons() -> None:
    graph = _graph([_node("a"), _node("bad", label="")])
    report = analyze_corpus_health(graph)
    assert report["ok"] is False
    junk = [f for f in report["findings"] if f["code"] == "junk_node"]
    assert len(junk) == 1
    assert junk[0]["severity"] == "critical"
    assert junk[0]["evidence"] == ["bad"]
    assert "blank label" in junk[0]["message"]


def test_unsanitized_concept_tier_is_junk() -> None:
    graph = _graph([_node("a", file_type="concept")])
    report = analyze_corpus_health(graph)
    assert report["ok"] is False
    assert "junk_node" in _codes(report)


# ----------------------------------------------------------------- dangling arcs


def test_dangling_arc_is_critical() -> None:
    graph = _graph([_node("a")], [_link("a", "ghost")])
    report = analyze_corpus_health(graph)
    assert report["ok"] is False
    finding = next(f for f in report["findings"] if f["code"] == "dangling_arc")
    assert finding["severity"] == "critical"
    assert finding["evidence"] == ["a -[supports]-> ghost"]


# --------------------------------------------------------------- duplicate labels


def test_duplicate_labels_are_a_warning_with_all_holders() -> None:
    graph = _graph([_node("a", label="Bayes"), _node("b", label="bayes "), _node("c", label="other")])
    report = analyze_corpus_health(graph)
    assert report["ok"] is True
    finding = next(f for f in report["findings"] if f["code"] == "duplicate_label")
    assert finding["severity"] == "warning"
    assert finding["evidence"] == ["a", "b"]


# ---------------------------------------------------- evidence without provenance


def test_empty_provenance_is_a_warning() -> None:
    graph = _graph([_node("a", provenance=[])])
    report = analyze_corpus_health(graph)
    finding = next(f for f in report["findings"] if f["code"] == "evidence_without_provenance")
    assert finding["severity"] == "warning"
    assert finding["evidence"] == ["a"]


# --------------------------------------------- contradictions without resolution


def test_contradiction_without_supersedes_is_flagged() -> None:
    graph = _graph([_node("a"), _node("b")], [_link("a", "b", relation="contradicts")])
    report = analyze_corpus_health(graph)
    finding = next(f for f in report["findings"] if f["code"] == "contradiction_without_resolution")
    assert finding["severity"] == "warning"
    assert finding["evidence"] == ["a -[contradicts]-> b"]


def test_contradiction_resolved_by_supersedes_is_not_flagged() -> None:
    graph = _graph(
        [_node("a"), _node("b"), _node("c")],
        [_link("a", "b", relation="contradicts"), _link("c", "a", relation="supersedes")],
    )
    report = analyze_corpus_health(graph)
    assert "contradiction_without_resolution" not in _codes(report)


# --------------------------------------------------------------- empty communities


def test_empty_community_from_explicit_mapping() -> None:
    graph = _graph([_node("a", community_sig="c1"), _node("b", community_sig="c1")])
    report = analyze_corpus_health(graph, communities={"c1": ["a", "b"], "c2": []})
    finding = next(f for f in report["findings"] if f["code"] == "empty_community")
    assert finding["severity"] == "warning"
    assert finding["evidence"] == ["c2"]
    assert report["counts"]["communities"] == 2


def test_community_of_only_junk_nodes_is_empty() -> None:
    graph = _graph([_node("a", community_sig="c1"), _node("bad", label="", community_sig="c2")])
    report = analyze_corpus_health(graph)
    assert "empty_community" in _codes(report)


# ------------------------------------------------------------ score and capping


def test_score_deductions_are_deterministic() -> None:
    assert SEVERITY_DEDUCTION == {"critical": "0.250000000000", "warning": "0.050000000000", "info": "0.010000000000"}
    # one critical (junk node) + one warning (real node with empty provenance)
    graph = _graph([_node("ok", provenance=[]), _node("bad", label="")])
    report = analyze_corpus_health(graph)
    assert report["ok"] is False
    assert report["score"] == "0.700000000000"


def test_score_floors_at_zero() -> None:
    nodes = [_node(f"n{i}") for i in range(6)]
    links = [_link(f"n{i}", "ghost", relation="supports") for i in range(6)]
    report = analyze_corpus_health(_graph(nodes, links))
    assert report["score"] == "0.000000000000"
    assert report["ok"] is False


def test_evidence_is_capped_with_omitted_count() -> None:
    nodes = [_node(f"n{i}", label="dup") for i in range(8)]
    report = analyze_corpus_health(_graph(nodes), max_evidence=3)
    finding = next(f for f in report["findings"] if f["code"] == "duplicate_label")
    assert finding["evidence"] == ["n0", "n1", "n2"]
    assert finding["omitted"] == 5


# ------------------------------------------------------------------ determinism


def test_report_is_byte_stable_and_input_order_independent() -> None:
    graph = _graph(
        [_node("b", provenance=[]), _node("a", label="dup"), _node("c", label="dup")],
        [_link("a", "ghost"), _link("b", "c", relation="contradicts")],
    )
    first = analyze_corpus_health(graph)
    shuffled = analyze_corpus_health(_graph(list(reversed(graph["nodes"])), list(reversed(graph["links"]))))
    assert canonical_json(first) == canonical_json(shuffled)


def test_findings_sorted_by_severity_then_code() -> None:
    graph = _graph(
        [_node("a", label="dup"), _node("b", label="dup"), _node("bad", label="")],
        [_link("a", "ghost")],
    )
    report = analyze_corpus_health(graph)
    severities = [f["severity"] for f in report["findings"]]
    assert severities == sorted(severities, key={"critical": 0, "warning": 1, "info": 2}.__getitem__)

"""Corpus health audit over the published read model (ATM plan F-C4).

A pure function over ``graph.json``: deterministic findings in audit style
(``severity`` + ``evidence``), a quantized health score, and fail-closed
semantics — any ``critical`` finding flips ``ok`` to ``False``.

Junk-node classification reuses the read-model validator's own junk
classifier, so the audit can never drift narrower than the publish gate.
Findings: junk nodes, dangling arcs, duplicate labels, evidence without
provenance, contradictions without resolution, and empty communities.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from know_your_soul.domain import quantize_score
from know_your_soul.read_model_validator import ReadModelValidator

__all__ = [
    "HEALTH_SCHEMA_VERSION",
    "SEVERITY_DEDUCTION",
    "SEVERITY_RANK",
    "analyze_corpus_health",
]

HEALTH_SCHEMA_VERSION = 1

#: Score deduction per finding, by severity (12-decimal fixed-point strings).
SEVERITY_DEDUCTION: dict[str, str] = {
    "critical": "0.250000000000",
    "warning": "0.050000000000",
    "info": "0.010000000000",
}

SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

_DEFAULT_MAX_EVIDENCE = 5

_validator = ReadModelValidator()


def _junk_reasons(node: Any) -> list[str]:
    """The validator's own junk-node reasons; a non-mapping node is junk."""
    if not isinstance(node, Mapping):
        return ["junk node: non-mapping entry"]
    return list(_validator._junk_node_reasons(node))


def _node_id(node: Any) -> str:
    if isinstance(node, Mapping) and isinstance(node.get("id"), str) and node["id"]:
        return str(node["id"])
    return "<unknown>"


def _arc(link: Mapping[str, Any]) -> str:
    return f"{link.get('source')} -[{link.get('relation')}]-> {link.get('target')}"


def _finding(code: str, severity: str, message: str, evidence: Iterable[str], max_evidence: int) -> dict[str, Any]:
    shown = sorted(str(item) for item in evidence)
    omitted = max(0, len(shown) - max_evidence)
    finding: dict[str, Any] = {
        "code": code,
        "severity": severity,
        "message": message,
        "evidence": shown[:max_evidence],
    }
    if omitted:
        finding["omitted"] = omitted
    return finding


def analyze_corpus_health(
    graph: Mapping[str, Any],
    *,
    communities: Mapping[str, Iterable[str]] | None = None,
    max_evidence: int = _DEFAULT_MAX_EVIDENCE,
) -> dict[str, Any]:
    """Audit one published generation and return a canonical health dict.

    ``graph`` is the parsed ``graph.json``. ``communities`` optionally maps
    a community signature to its declared member ids (for example from
    ``signatures.json``); when omitted, communities are derived from the
    nodes' ``community_sig`` fields.
    """
    if max_evidence < 1:
        raise ValueError("max_evidence must be positive")
    raw_nodes: Any = graph.get("nodes")
    raw_links: Any = graph.get("links")
    nodes: list[Any] = list(raw_nodes) if isinstance(raw_nodes, list) else []
    links: list[Any] = list(raw_links) if isinstance(raw_links, list) else []

    findings: list[dict[str, Any]] = []

    # ------------------------------------------------------------------ junk
    real_ids: set[str] = set()
    real_node_ids_by_sig: dict[str, set[str]] = {}
    for node in nodes:
        reasons = _junk_reasons(node)
        node_id = _node_id(node)
        if reasons:
            findings.append(_finding("junk_node", "critical", "; ".join(sorted(reasons)), [node_id], max_evidence))
            continue
        real_ids.add(node_id)
        if isinstance(node, Mapping):
            sig = node.get("community_sig")
            if isinstance(sig, str) and sig:
                real_node_ids_by_sig.setdefault(sig, set()).add(node_id)

    # ----------------------------------------------------------- dangling arcs
    typed_links = [link for link in links if isinstance(link, Mapping)]
    for link in typed_links:
        source, target = link.get("source"), link.get("target")
        if (
            not isinstance(source, str)
            or not isinstance(target, str)
            or source not in real_ids
            or target not in real_ids
        ):
            findings.append(
                _finding(
                    "dangling_arc",
                    "critical",
                    "arc endpoint does not resolve to a real node",
                    [_arc(link)],
                    max_evidence,
                )
            )

    # ---------------------------------------------------------- duplicate labels
    by_label: dict[str, list[str]] = {}
    for node in nodes:
        if not isinstance(node, Mapping) or _node_id(node) not in real_ids:
            continue
        label = node.get("label")
        if isinstance(label, str):
            by_label.setdefault(" ".join(label.casefold().split()), []).append(_node_id(node))
    for label in sorted(by_label):
        holders = sorted(by_label[label])
        if len(holders) > 1:
            findings.append(
                _finding(
                    "duplicate_label",
                    "warning",
                    f"label {label!r} is shared by {len(holders)} real nodes",
                    holders,
                    max_evidence,
                )
            )

    # ------------------------------------------- evidence without provenance
    for node in nodes:
        if not isinstance(node, Mapping) or _node_id(node) not in real_ids:
            continue
        provenance = node.get("provenance")
        if not isinstance(provenance, list) or not provenance:
            findings.append(
                _finding(
                    "evidence_without_provenance",
                    "warning",
                    "real node carries an empty provenance list",
                    [_node_id(node)],
                    max_evidence,
                )
            )

    # ------------------------------ contradictions without resolution
    superseded_targets = {
        str(link["target"])
        for link in typed_links
        if link.get("relation") == "supersedes" and isinstance(link.get("target"), str)
    }
    for link in typed_links:
        if link.get("relation") != "contradicts":
            continue
        source, target = link.get("source"), link.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            continue  # already reported as dangling
        if source not in superseded_targets and target not in superseded_targets:
            findings.append(
                _finding(
                    "contradiction_without_resolution",
                    "warning",
                    "no supersedes arc resolves either endpoint",
                    [_arc(link)],
                    max_evidence,
                )
            )

    # ----------------------------------------------------------- communities
    if communities is not None:
        declared = {str(sig): [str(member) for member in members] for sig, members in communities.items()}
        community_count = len(declared)
        empty = [sig for sig in sorted(declared) if not any(member in real_ids for member in declared[sig])]
    else:
        declared = {}
        sigs = set(real_node_ids_by_sig)
        for node in nodes:
            if isinstance(node, Mapping):
                sig = node.get("community_sig")
                if isinstance(sig, str) and sig:
                    sigs.add(sig)
        community_count = len(sigs)
        empty = [sig for sig in sorted(sigs) if not real_node_ids_by_sig.get(sig)]
    for sig in empty:
        findings.append(
            _finding(
                "empty_community",
                "warning",
                f"community {sig} has no real member nodes",
                [sig],
                max_evidence,
            )
        )

    findings.sort(
        key=lambda finding: (
            SEVERITY_RANK.get(str(finding["severity"]), 3),
            str(finding["code"]),
            finding["evidence"],
        )
    )

    # -------------------------------------------------------------------- score
    score = Decimal(1)
    for finding in findings:
        score -= Decimal(SEVERITY_DEDUCTION.get(str(finding["severity"]), "0.010000000000"))
    score = max(score, Decimal(0))

    return {
        "schema_version": HEALTH_SCHEMA_VERSION,
        "ok": not any(finding["severity"] == "critical" for finding in findings),
        "score": format(quantize_score(score), "f"),
        "counts": {"nodes": len(nodes), "links": len(links), "communities": community_count},
        "findings_by_severity": {
            severity: sum(1 for finding in findings if finding["severity"] == severity)
            for severity in ("critical", "warning", "info")
        },
        "findings": findings,
    }

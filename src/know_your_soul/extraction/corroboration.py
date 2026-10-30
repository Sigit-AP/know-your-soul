"""The deterministic corroboration engine (contract v4, SUB-106).

Multi-agent corroboration over one ``source_sha256`` group, exactly as the
contract writes it: node keys ``(kind, name)`` agree on equal value digests
and contradict on different ones; edge endpoint pairs ``(source, target)``
agree on equal relation and contradict on different relation; a submission
corroborates another when the actors differ, the overlap ratio is ≥ 0.5 and
the pairwise contradiction count is zero. ``recompute_group`` is a pure
function of the ordered submissions — same rows in, same verdicts out.
"""

from __future__ import annotations

import hashlib
from dataclasses import dataclass
from decimal import Decimal

from know_your_soul.domain import canonical_json, quantize_score
from know_your_soul.extraction.schema import ExtractionPayload, Submission
from know_your_soul.extraction.tier import effective_confidence

__all__ = [
    "OVERLAP_THRESHOLD",
    "GroupVerdict",
    "PairwiseResult",
    "compare_payloads",
    "recompute_group",
]

#: The overlap ratio at or above which two submissions corroborate (contract).
OVERLAP_THRESHOLD = Decimal("0.5")


def _fixed(value: Decimal) -> str:
    """The 12-decimal fixed-point rendering (never scientific notation)."""
    return format(quantize_score(value), "f")


@dataclass(frozen=True)
class PairwiseResult:
    """The deterministic comparison of two payloads."""

    shared: int
    agreements: int
    contradictions: int
    overlap_ratio: Decimal


@dataclass(frozen=True)
class GroupVerdict:
    """The recomputed group verdict for one submission (contract v4)."""

    status: str  # "pending_review" | "corroborated" | "contradiction_flagged"
    corroborators: int
    contradictions: int
    overlap_ratio: str  # the best corroborating ratio, 12-decimal string
    confidence_effective: str  # 12-decimal string


def _node_value_digests(payload: ExtractionPayload) -> dict[tuple[str, str], str]:
    digests: dict[tuple[str, str], str] = {}
    for node in payload.nodes:
        canonical = canonical_json(node.canonical())
        digests[node.key] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()
    return digests


def _edge_index(payload: ExtractionPayload) -> dict[tuple[tuple[str, str], tuple[str, str]], str]:
    return {(edge.source, edge.target): edge.relation for edge in payload.edges}


def compare_payloads(a: ExtractionPayload, b: ExtractionPayload) -> PairwiseResult:
    """Compare two payloads on shared node keys and shared edge endpoint pairs."""
    a_nodes, b_nodes = _node_value_digests(a), _node_value_digests(b)
    a_edges, b_edges = _edge_index(a), _edge_index(b)
    shared = 0
    agreements = 0
    contradictions = 0
    for key in sorted(set(a_nodes) & set(b_nodes)):
        shared += 1
        if a_nodes[key] == b_nodes[key]:
            agreements += 1
        else:
            contradictions += 1
    for pair in sorted(set(a_edges) & set(b_edges)):
        shared += 1
        if a_edges[pair] == b_edges[pair]:
            agreements += 1
        else:
            contradictions += 1
    ratio = Decimal(agreements) / Decimal(shared) if shared else Decimal(0)
    return PairwiseResult(
        shared=shared,
        agreements=agreements,
        contradictions=contradictions,
        overlap_ratio=quantize_score(ratio),
    )


def _corroborates(result: PairwiseResult) -> bool:
    return result.contradictions == 0 and result.shared > 0 and result.overlap_ratio >= OVERLAP_THRESHOLD


def recompute_group(rows: list[tuple[Submission, str]]) -> dict[str, GroupVerdict]:
    """Recompute every verdict of one ``source_sha256`` group, deterministically.

    *rows* are ``(submission, actor)`` pairs in submission order. The result
    is keyed by idempotency key; statuses are derived for the pre-decision
    lifecycle (the queue preserves ``applied``/``rejected`` over these).
    """
    verdicts: dict[str, GroupVerdict] = {}
    for index, (submission, actor) in enumerate(rows):
        corroborating_actors: set[str] = set()
        contradictions = 0
        best_ratio = Decimal(0)
        for other_index, (other, other_actor) in enumerate(rows):
            if other_index == index or other_actor == actor:
                continue  # an actor never corroborates or contradicts itself
            result = compare_payloads(submission.payload, other.payload)
            if result.contradictions:
                contradictions += result.contradictions
            elif _corroborates(result):
                corroborating_actors.add(other_actor)
                best_ratio = max(best_ratio, result.overlap_ratio)
        flagged = contradictions > 0
        if flagged:
            status = "contradiction_flagged"
        elif corroborating_actors:
            status = "corroborated"
        else:
            status = "pending_review"
        confidence = effective_confidence(
            submission.confidence, corroborators=len(corroborating_actors), flagged=flagged
        )
        verdicts[submission.idem_key(actor)] = GroupVerdict(
            status=status,
            corroborators=len(corroborating_actors),
            contradictions=contradictions,
            overlap_ratio=_fixed(best_ratio),
            confidence_effective=_fixed(confidence),
        )
    return verdicts

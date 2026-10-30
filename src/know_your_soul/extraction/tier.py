"""The ``agent-extracted`` truth tier (contract v4, SUB-106).

The tier registry is written, not implicit: agent-extracted knowledge starts
under a written intake cap, corroboration moves it only up to the tier cap,
and nothing on this path promotes beyond the cap — promotion rides the
existing operator-gated evidence operations. All arithmetic is quantized
Decimal (12 places, ROUND_HALF_EVEN), so two runs of the same inputs are
byte-identical.
"""

from __future__ import annotations

import hashlib
from decimal import Decimal
from typing import Any

from know_your_soul.domain import Node, NodeKind, PrimaryDomain, ScoreVector, quantize_score
from know_your_soul.extraction.schema import ProposedNode, Submission

__all__ = [
    "AGENT_EXTRACTED_TIER",
    "CONTRADICTION_PENALTY",
    "CORROBORATION_STEP",
    "INTAKE_CAP",
    "TIER_CAP",
    "applied_node_id",
    "certainty_block",
    "effective_confidence",
    "stamp_node",
]

AGENT_EXTRACTED_TIER = "agent-extracted"

#: The most a self-report counts for at intake (contract v4, truth tier).
INTAKE_CAP = Decimal("0.35")
#: The most an agent-extracted node may ever carry through this path.
TIER_CAP = Decimal("0.65")
#: Effective-confidence gain per distinct corroborating actor.
CORROBORATION_STEP = Decimal("0.10")
#: Effective-confidence loss when a contradiction flags the submission.
CONTRADICTION_PENALTY = Decimal("0.20")


def effective_confidence(reported: Decimal, *, corroborators: int, flagged: bool) -> Decimal:
    """The deterministic effective confidence for one submission.

    ``min(self_report, intake_cap) + 0.10 × corroborators − 0.20 if flagged``,
    clamped to ``[0, tier_cap]`` and quantized to 12 places.
    """
    base = min(reported, INTAKE_CAP)
    adjusted = base + CORROBORATION_STEP * corroborators
    if flagged:
        adjusted -= CONTRADICTION_PENALTY
    return quantize_score(max(Decimal(0), min(adjusted, TIER_CAP)))


def certainty_block(
    submission: Submission,
    *,
    receipt_id: str,
    corroborators: int,
) -> dict[str, Any]:
    """The provenance/tier block every applied extraction node carries."""
    return {
        "tier": AGENT_EXTRACTED_TIER,
        "intake_cap": str(INTAKE_CAP),
        "tier_cap": str(TIER_CAP),
        "source_sha256": submission.source_sha256,
        "receipt_id": receipt_id,
        "method": submission.method.canonical(),
        "corroborators": corroborators,
    }


def applied_node_id(source_sha256: str, kind: str, name: str) -> str:
    """The deterministic graph node id for one proposed node key."""
    preimage = f"agent-extraction:{source_sha256}:{kind}:{name}"
    return "agent:" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:32]


def stamp_node(
    submission: Submission,
    node: ProposedNode,
    *,
    receipt_id: str,
    confidence_effective: Decimal,
    corroborators: int = 0,
) -> Node:
    """Build the canonical-graph node for one approved proposed node.

    Deterministic: ``created_at``/``updated_at`` are the method's
    ``extracted_at`` so a retried approve produces the identical write (the
    store's idempotency replay keys on the event payload).
    """
    confidence = quantize_score(min(max(confidence_effective, Decimal(0)), TIER_CAP))
    scores = ScoreVector(
        p=Decimal(0),
        m=Decimal(0),
        e=Decimal(0),
        uncertainty=quantize_score(Decimal(1) - confidence),
        confidence=confidence,
        professional_level=1,
    )
    payload: dict[str, Any] = {
        "certainty": certainty_block(submission, receipt_id=receipt_id, corroborators=corroborators),
        "extraction": {
            "receipt_id": receipt_id,
            "payload_sha256": submission.payload_sha256,
            "span": {"line": node.span[0], "col": node.span[1]} if node.span is not None else None,
        },
    }
    return Node(
        node_id=applied_node_id(submission.source_sha256, node.kind, node.name),
        kind=NodeKind(node.kind),
        domain=PrimaryDomain(node.domain),
        payload=payload,
        summary=node.summary,
        provenance=(
            f"sha256:{submission.source_sha256}",
            f"extraction:{receipt_id}",
            f"agent:{submission.method.agent}",
        ),
        context_tags=(AGENT_EXTRACTED_TIER,),
        created_at=submission.method.extracted_at,
        updated_at=submission.method.extracted_at,
        scores=scores,
        source_version_id=f"extraction:{receipt_id}",
    )

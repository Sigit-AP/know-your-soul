"""Contract-v4 external-agent extraction intake (SUB-106).

The public surface of ``docs/contracts/extraction-contract-v4.md``: the
``knowledge.submit_extraction`` verb (proposal-only, hash-bound provenance,
locked idempotency, deterministic corroboration, the ``agent-extracted``
truth tier) and the operator review surface. Nothing in this package writes
the canonical graph except through the existing operator-gated write path,
and nothing here calls a model, a provider or a network.
"""

from __future__ import annotations

from know_your_soul.extraction.corroboration import (
    OVERLAP_THRESHOLD,
    GroupVerdict,
    PairwiseResult,
    compare_payloads,
    recompute_group,
)
from know_your_soul.extraction.engine import ExtractionEngine
from know_your_soul.extraction.errors import (
    ConfidenceNotDecimalError,
    ConfidenceOutOfRangeError,
    ExtractionDuplicateError,
    ExtractionError,
    ExtractionNotFoundError,
    ExtractionQueueFullError,
    ExtractionStateError,
    MethodSchemaError,
    PayloadSchemaError,
    PayloadTooLargeError,
    SourceHashMismatchError,
    SourceNotFoundError,
    SourcePathOutsideRootError,
)
from know_your_soul.extraction.queue import QUEUE_CAP, STATUS_RANK, ExtractionQueue, QueueRow, sidecar_path
from know_your_soul.extraction.schema import (
    ExtractionPayload,
    MethodDescriptor,
    ProposedEdge,
    ProposedNode,
    Submission,
    sanitize_field,
    sha256_file,
    submission_from_canonical,
    validate_submission,
)
from know_your_soul.extraction.tier import (
    AGENT_EXTRACTED_TIER,
    CONTRADICTION_PENALTY,
    CORROBORATION_STEP,
    INTAKE_CAP,
    TIER_CAP,
    applied_node_id,
    certainty_block,
    effective_confidence,
    stamp_node,
)

__all__ = [
    "AGENT_EXTRACTED_TIER",
    "CONTRADICTION_PENALTY",
    "CORROBORATION_STEP",
    "INTAKE_CAP",
    "OVERLAP_THRESHOLD",
    "QUEUE_CAP",
    "STATUS_RANK",
    "TIER_CAP",
    "ConfidenceNotDecimalError",
    "ConfidenceOutOfRangeError",
    "ExtractionDuplicateError",
    "ExtractionEngine",
    "ExtractionError",
    "ExtractionNotFoundError",
    "ExtractionPayload",
    "ExtractionQueue",
    "ExtractionQueueFullError",
    "ExtractionStateError",
    "GroupVerdict",
    "MethodDescriptor",
    "MethodSchemaError",
    "PairwiseResult",
    "PayloadSchemaError",
    "PayloadTooLargeError",
    "ProposedEdge",
    "ProposedNode",
    "QueueRow",
    "SourceHashMismatchError",
    "SourceNotFoundError",
    "SourcePathOutsideRootError",
    "Submission",
    "applied_node_id",
    "certainty_block",
    "compare_payloads",
    "effective_confidence",
    "recompute_group",
    "sanitize_field",
    "sha256_file",
    "sidecar_path",
    "stamp_node",
    "submission_from_canonical",
    "validate_submission",
]

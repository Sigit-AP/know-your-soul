"""Fixed service contracts: requests, responses, errors and the access policy.

Task 4 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7, 8 and 13.1/13.2 and the frozen contracts in
``docs/contracts/`` and ``docs/mathematics/token-budget-contract.md``.

Everything in this module is a frozen, ``extra="forbid"`` model or a typed
error: callers can never smuggle unknown fields into a request, and every
failure a caller can cause is a typed :class:`KnowledgeError` with a stable
``code`` — never an unclassified exception.
"""

from __future__ import annotations

import hashlib
import json
from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from pathlib import Path
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field, model_validator

from soul_knowledge.domain import RelationKind, canonical_json, quantize_score
from soul_knowledge.improvement import ApprovalRecord, DiffOperation, ImprovementProposal

__all__ = [
    "ACCESS_ONLY_TOKEN_CAP",
    "ACCESS_POLICY_PATH",
    "ACCESS_POLICY_SHA256",
    "DEFAULT_MAX_TOKEN_BUDGET",
    "MINIMUM_TOKEN_BUDGET",
    "AccessDecision",
    "AccessPolicy",
    "AmbiguousDomain",
    "ApplyImprovementRequest",
    "ApprovalRequiredError",
    "BudgetExceedsMax",
    "BudgetTooSmall",
    "DueReviewView",
    "EvaluateRequest",
    "EvaluateResponse",
    "ImprovementNotFound",
    "ImprovementResponse",
    "InvalidProposal",
    "KnowledgeError",
    "LifecycleError",
    "MetadataExceedsBudget",
    "MixedSourceVersionsError",
    "NodeNotFound",
    "NodeResponse",
    "NodeView",
    "OperationNotImplemented",
    "OperatorDecisionRequiredError",
    "PolicyArtifactError",
    "PrimaryRootMissing",
    "PromotionVerdict",
    "ProposalResponse",
    "ProposeImprovementRequest",
    "QueryRequest",
    "QueryResponse",
    "RecommendationBlock",
    "RecommendationItem",
    "RegressionGateError",
    "ResultNode",
    "ReviewDecisionView",
    "ReviewRequest",
    "ReviewResponse",
    "RollbackRequest",
    "RollbackResponse",
    "StaleProjectionUnavailable",
    "SubtreeEdge",
    "SubtreeResponse",
    "TraversalMode",
    "UnknownDomain",
    "UnknownRelearnError",
    "attempt_promotion",
    "estimated_tokens_of_payload",
    "score_text",
]

# Token-budget contract 1.0.0 (docs/mathematics/token-budget-contract.md).
MINIMUM_TOKEN_BUDGET = 128
ACCESS_ONLY_TOKEN_CAP = 160
DEFAULT_MAX_TOKEN_BUDGET = 8192

ACCESS_POLICY_PATH = Path(__file__).resolve().parent / "access_policy.json"
ACCESS_POLICY_SHA256 = "300e094d5094a68ca8b883e19004782f9c4e7ea8a4497adc3b292beaf7a764ad"


def estimated_tokens_of_payload(payload: Any) -> int:
    """estimated_tokens = ceil(utf8_byte_length / 4) of the canonical payload."""
    return -(-len(canonical_json(payload).encode("utf-8")) // 4)


def score_text(value: object) -> str:
    """A 12-decimal fixed-point score string (spec section 9 serialization)."""
    return format(quantize_score(Decimal(str(value))), "f")


# --------------------------------------------------------------------- errors


class KnowledgeError(RuntimeError):
    """A typed service failure with a stable machine-readable ``code``.

    ``audit_event_id`` is attached once the invocation's single final audit
    event has been appended, so even failures identify their audit record
    (spec section 7.7). ``retryable`` marks the stale-projection unavailable
    error (spec section 7.6).
    """

    code = "KNOWLEDGE_ERROR"
    retryable = False

    def __init__(self, message: str, *, audit_event_id: str | None = None, **details: Any) -> None:
        super().__init__(message)
        self.audit_event_id = audit_event_id
        self.details = details


class BudgetTooSmall(KnowledgeError):
    code = "BUDGET_TOO_SMALL"


class BudgetExceedsMax(KnowledgeError):
    code = "BUDGET_EXCEEDS_MAX"


class MetadataExceedsBudget(KnowledgeError):
    code = "METADATA_EXCEEDS_BUDGET"


class AmbiguousDomain(KnowledgeError):
    code = "AMBIGUOUS_DOMAIN"

    def __init__(self, message: str, *, candidates: tuple[Any, ...] = (), **kwargs: Any) -> None:
        super().__init__(message, **kwargs)
        self.candidates = candidates


class UnknownDomain(KnowledgeError):
    code = "UNKNOWN_DOMAIN"


class PrimaryRootMissing(KnowledgeError):
    code = "PRIMARY_ROOT_MISSING"


class NodeNotFound(KnowledgeError):
    code = "NODE_NOT_FOUND"


class StaleProjectionUnavailable(KnowledgeError):
    code = "STALE_PROJECTION"
    retryable = True


class OperationNotImplemented(KnowledgeError):
    code = "NOT_IMPLEMENTED"


class PolicyArtifactError(KnowledgeError):
    code = "POLICY_HASH_MISMATCH"


class LifecycleError(KnowledgeError):
    """A store-backed lifecycle failure that has no more specific contract."""

    code = "LIFECYCLE_ERROR"


class InvalidProposal(LifecycleError):
    code = "PROPOSAL_INVALID"


class ApprovalRequiredError(LifecycleError):
    code = "APPROVAL_REQUIRED"


class RegressionGateError(LifecycleError):
    code = "REGRESSION_GATE_FAILED"


class ImprovementNotFound(LifecycleError):
    code = "IMPROVEMENT_NOT_FOUND"


class UnknownRelearnError(LifecycleError):
    code = "UNKNOWN_RELEARN"


class OperatorDecisionRequiredError(LifecycleError):
    code = "OPERATOR_DECISION_REQUIRED"


class MixedSourceVersionsError(LifecycleError):
    code = "MIXED_SOURCE_VERSIONS"


# ------------------------------------------------------------------- requests


class TraversalMode(StrEnum):
    """Traversal order (spec section 13.1: mode is ``{bfs,dfs}``)."""

    BFS = "bfs"
    DFS = "dfs"


class QueryRequest(BaseModel):
    """The fixed ``knowledge.query`` request contract (spec section 8).

    Strict mode rejects coercions (string booleans, numeric strings); depth is
    bounded to ``[1,6]`` and the relation allowlist to the closed relation
    vocabulary (spec section 13.1).
    """

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    purpose: str = Field(min_length=1, max_length=512)
    domain: str | None = None
    context_tags: tuple[str, ...] = ()
    token_budget: int = Field(ge=0)
    access_only: bool = False
    depth: int = Field(default=2, ge=1, le=6)
    mode: TraversalMode = TraversalMode.BFS
    relation_allowlist: tuple[RelationKind, ...] | None = None
    max_nodes: int = Field(default=64, ge=1, le=1024)
    max_edges: int = Field(default=256, ge=1, le=4096)


class EvaluateRequest(BaseModel):
    """The fixed ``knowledge.evaluate`` request contract (scored in Task 5)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    node_id: str = Field(min_length=1)
    context_tags: tuple[str, ...] = ()
    at: datetime | None = None
    include_descendants: bool = True


# -------------------------------------------------------------- response parts


class ResultNode(BaseModel):
    """One knowledge node inside a response; summaries are sanitized."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    summary: str
    domain: str
    kind: str
    version: int
    confidence: str
    p: str


class RecommendationItem(BaseModel):
    """One contracted recommendation item (recommendation contract 1.0.0).

    Carries structured references only — source node IDs and versions,
    evidence IDs, learned class, reason codes and expiry — never raw SOUL or
    corpus text. The one-observation promotion rule is fail-closed at the
    model boundary.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    action_id: str = Field(min_length=1)
    label: str = Field(min_length=1, max_length=200)
    source_node_ids: tuple[str, ...] = Field(min_length=1)
    source_node_versions: tuple[int, ...]
    evidence_ids: tuple[str, ...] = ()
    learned_class: str = Field(min_length=1)
    reason_codes: tuple[str, ...] = Field(min_length=1)
    expiry: datetime
    promoted: bool = False

    @model_validator(mode="after")
    def _contract(self) -> RecommendationItem:
        if len(self.source_node_ids) != len(self.source_node_versions):
            raise ValueError("source_node_versions must have the same length as source_node_ids")
        if self.promoted and len(self.evidence_ids) < 2:
            raise ValueError("ONE_OBSERVATION_NO_PROMOTION")
        return self


class PromotionVerdict(BaseModel):
    """The deterministic verdict of a promotion attempt."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    accepted: bool
    reason: str | None = None


def attempt_promotion(item: RecommendationItem) -> PromotionVerdict:
    """One observation can never promote an action or a node (contract 1.0.0)."""
    if len(item.evidence_ids) < 2:
        return PromotionVerdict(accepted=False, reason="ONE_OBSERVATION_NO_PROMOTION")
    return PromotionVerdict(accepted=True)


class RecommendationBlock(BaseModel):
    """Exactly the three frozen lists; no other list keys are permitted."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    best_actions: tuple[RecommendationItem, ...] = ()
    recommendations: tuple[RecommendationItem, ...] = ()
    unnecessary_actions: tuple[RecommendationItem, ...] = ()

    @classmethod
    def empty(cls) -> RecommendationBlock:
        return cls()


class NodeView(BaseModel):
    """The full single-node read view (projection-sourced)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    summary: str
    domain: str
    kind: str
    version: int
    lifecycle_status: str
    source_version_id: str
    provenance: tuple[str, ...] = ()
    context_tags: tuple[str, ...] = ()
    confidence: str
    p: str


class SubtreeEdge(BaseModel):
    """One typed edge inside a subtree response (parallel relations survive)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str
    target: str
    relation: str


# ------------------------------------------------------------------ responses


class _ResponseMeta(BaseModel):
    """The audit/projection metadata every read response carries (spec 7.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    audit_event_id: str
    projection_version: str
    required_knowledge_watermark: int
    source_knowledge_high_watermark: int
    stale: bool


class QueryResponse(_ResponseMeta):
    """The fixed ``knowledge.query`` response contract (spec section 8).

    ``canonical_payload`` is the exact object the frozen estimator applies to:
    sorted keys, compact separators, UTF-8, including provenance, audit and
    projection metadata and the truncation flag. ``access_only`` responses
    carry the reduced zero-knowledge-payload shape (access policy 1.0.0).
    """

    truncated: bool
    routing: str
    candidates: tuple[str, ...] = ()
    confidence: str
    node_versions: dict[str, int]
    provenance: tuple[str, ...]
    results: tuple[ResultNode, ...]
    recommendations: RecommendationBlock
    access_only: bool = False
    knowledge_payload_tokens: int = 0
    estimated_tokens: int = 0

    def canonical_payload(self) -> dict[str, Any]:
        if self.access_only:
            return {
                "access_only": True,
                "audit_event_id": self.audit_event_id,
                "estimated_tokens": self.estimated_tokens,
                "knowledge_payload_tokens": 0,
                "projection_version": self.projection_version,
                "required_knowledge_watermark": self.required_knowledge_watermark,
                "results": [],
                "source_knowledge_high_watermark": self.source_knowledge_high_watermark,
                "stale": self.stale,
                "truncated": self.truncated,
            }
        return {
            "access_only": False,
            "audit_event_id": self.audit_event_id,
            "candidates": list(self.candidates),
            "confidence": self.confidence,
            "estimated_tokens": self.estimated_tokens,
            "knowledge_payload_tokens": self.knowledge_payload_tokens,
            "node_versions": dict(self.node_versions),
            "projection_version": self.projection_version,
            "provenance": list(self.provenance),
            "recommendations": json.loads(canonical_json(self.recommendations.model_dump(mode="json"))),
            "required_knowledge_watermark": self.required_knowledge_watermark,
            "results": [item.model_dump(mode="json") for item in self.results],
            "routing": self.routing,
            "source_knowledge_high_watermark": self.source_knowledge_high_watermark,
            "stale": self.stale,
            "truncated": self.truncated,
        }


class NodeResponse(_ResponseMeta):
    """The ``knowledge.get_node`` response."""

    node: NodeView
    estimated_tokens: int = 0

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "audit_event_id": self.audit_event_id,
            "estimated_tokens": self.estimated_tokens,
            "node": self.node.model_dump(mode="json"),
            "projection_version": self.projection_version,
            "required_knowledge_watermark": self.required_knowledge_watermark,
            "source_knowledge_high_watermark": self.source_knowledge_high_watermark,
            "stale": self.stale,
        }


class SubtreeResponse(_ResponseMeta):
    """The ``knowledge.get_subtree`` response with traversal guards shown."""

    node_id: str
    nodes: tuple[ResultNode, ...]
    edges: tuple[SubtreeEdge, ...]
    cross_edges: tuple[SubtreeEdge, ...] = ()
    guards_hit: tuple[str, ...] = ()
    truncated: bool = False
    knowledge_payload_tokens: int = 0
    estimated_tokens: int = 0

    def canonical_payload(self) -> dict[str, Any]:
        return {
            "audit_event_id": self.audit_event_id,
            "cross_edges": [edge.model_dump(mode="json") for edge in self.cross_edges],
            "edges": [edge.model_dump(mode="json") for edge in self.edges],
            "estimated_tokens": self.estimated_tokens,
            "guards_hit": list(self.guards_hit),
            "knowledge_payload_tokens": self.knowledge_payload_tokens,
            "node_id": self.node_id,
            "nodes": [node.model_dump(mode="json") for node in self.nodes],
            "projection_version": self.projection_version,
            "required_knowledge_watermark": self.required_knowledge_watermark,
            "source_knowledge_high_watermark": self.source_knowledge_high_watermark,
            "stale": self.stale,
            "truncated": self.truncated,
        }


class EvaluateResponse(_ResponseMeta):
    """The fixed ``knowledge.evaluate`` response contract (implemented in Task 5).

    Carries P/M/E, per-dimension uncertainty, the professional level,
    evidence-weight explanations, the selected M bin, promotion eligibility
    and the audit/projection metadata (plan Task 5 interface).
    """

    node_id: str
    p: str
    m: str
    e: str
    u_p: str
    u_m: str
    u_e: str
    overall_uncertainty: str
    professional_level: int = Field(ge=1, le=5)
    selected_m_bin: str | None
    m_bin_evidence_count: int | None
    promotable: bool
    g: str
    explanations: dict[str, Any] = Field(default_factory=dict)


# --------------------------------------------------------------- access policy


class AccessDecision(StrEnum):
    """The three conditional-access outcomes (access policy 1.0.0)."""

    NO_CALL = "NO_CALL"
    ACCESS_ONLY = "ACCESS_ONLY"
    QUERY = "QUERY"


_DECISION_BY_VERDICT = {
    "never_call": AccessDecision.NO_CALL,
    "may_call_access_only": AccessDecision.ACCESS_ONLY,
    "call": AccessDecision.QUERY,
}


class AccessPolicy:
    """The hash-pinned local conditional-access policy artifact.

    Deciding whether to call the system never costs a model call or a graph
    call: the policy is a frozen local JSON artifact whose SHA-256 is pinned
    in code; a tampered artifact fails closed at load time. L1 surfaces never
    call the system; ambiguous surfaces may call ``knowledge.query`` with
    ``access_only=true`` capped at 160 estimated tokens with zero knowledge
    payload; required surfaces call normally.
    """

    def __init__(self, *, path: Path | None = None) -> None:
        artifact = path if path is not None else ACCESS_POLICY_PATH
        raw = artifact.read_bytes()
        digest = hashlib.sha256(raw).hexdigest()
        if digest != ACCESS_POLICY_SHA256:
            raise PolicyArtifactError(
                f"access policy artifact hash {digest} does not match the pin {ACCESS_POLICY_SHA256}"
            )
        data = json.loads(raw.decode("utf-8"))
        self._surface_decisions: dict[str, str] = dict(data["surface_decisions"])
        self._unknown_surface: str = str(data["unknown_surface"])
        self._access_only_cap: int = int(data["access_only"]["max_estimated_tokens"])

    @property
    def access_only_max_estimated_tokens(self) -> int:
        return self._access_only_cap

    def decide(self, task_surface: str | None, context: Any = None) -> AccessDecision:
        """Map a task surface to a call decision; pure local lookup.

        ``context`` is accepted for the caller's audit trail but never
        consulted: the decision is a hash-pinned table lookup, so it can never
        cost a model call or a graph call. An unknown surface is treated as
        ambiguous (the cheap, zero-payload path), never as a free call.
        """
        verdict = self._surface_decisions.get(str(task_surface), self._unknown_surface)
        return _DECISION_BY_VERDICT[verdict]


# ------------------------------------------------- lifecycle tool contracts
#
# The remaining four public tools (review, propose_improvement,
# apply_improvement, rollback) share the same envelope as every other read:
# audit id, projection version, both watermarks and staleness. Requests are
# ``extra="forbid"`` so a CLI/API/MCP adapter can never smuggle fields in.


class ReviewRequest(BaseModel):
    """The ``knowledge.review`` request: one node, plus an optional due scan."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    node_id: str = Field(min_length=1)
    at: datetime | None = None
    include_due: bool = False


class ReviewDecisionView(BaseModel):
    """The deterministic review-policy verdict, serialized for the caller."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    current_status: str
    desired_status: str
    target_status: str
    transitioned: bool
    qualifying_for_promotion: bool
    reasons: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    policy_version: str


class DueReviewView(BaseModel):
    """One candidate emitted by the due-review scheduler (no model call)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    version: int
    domain: str
    reasons: tuple[str, ...] = ()
    last_review_at: datetime | None = None
    target_status: str | None = None


class ReviewResponse(_ResponseMeta):
    """The ``knowledge.review`` response: decision, versions and recommendations."""

    node_id: str
    decision: ReviewDecisionView
    persisted: bool
    from_version: int
    to_version: int
    relearn_pending: bool
    due: tuple[DueReviewView, ...] = ()
    recommendations: RecommendationBlock


class ProposeImprovementRequest(BaseModel):
    """The ``knowledge.propose_improvement`` request: one structured diff."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    node_id: str = Field(min_length=1)
    diff: tuple[DiffOperation, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    base_version: int | None = Field(default=None, ge=1)
    at: datetime | None = None


class ProposalResponse(_ResponseMeta):
    """The persisted proposal plus its audit/projection metadata.

    The optional ``domain_event_id``/``audit_event_seq``/``node_version``/
    ``knowledge_seq`` fields are the spec 7.9 mutation-receipt fields (the
    ``WriteReceipt`` pattern, ``events.py``); they are populated when the
    caller hands the domain result through, and stay ``None`` on zero-weight
    or unwired paths.
    """

    proposal: ImprovementProposal
    idempotent_replay: bool = False
    domain_event_id: str | None = None
    audit_event_seq: int | None = None
    node_version: int | None = None
    knowledge_seq: int | None = None


class ApplyImprovementRequest(BaseModel):
    """The ``knowledge.apply_improvement`` request (approval for high impact)."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    proposal_id: str = Field(min_length=1)
    approval: ApprovalRecord | None = None
    at: datetime | None = None


class ImprovementResponse(_ResponseMeta):
    """The outcome of an apply or an improvement rollback (spec 7.9 field set)."""

    proposal_id: str
    node_id: str
    from_version: int
    to_version: int
    status: str
    content_sha256: str
    idempotent_replay: bool
    domain_event_id: str | None = None
    audit_event_seq: int | None = None
    node_version: int | None = None
    knowledge_seq: int | None = None


class RollbackRequest(BaseModel):
    """The ``knowledge.rollback`` request: exactly one rollback target."""

    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)

    node_id: str | None = Field(default=None, min_length=1)
    to_version: int | None = Field(default=None, ge=1)
    relearn_id: str | None = Field(default=None, min_length=1)
    at: datetime | None = None

    @model_validator(mode="after")
    def _exactly_one_target(self) -> RollbackRequest:
        improvement = self.node_id is not None
        relearn = self.relearn_id is not None
        if improvement == relearn:
            raise ValueError("rollback requires exactly one of node_id or relearn_id")
        if relearn and self.to_version is not None:
            raise ValueError("to_version only applies to an improvement rollback")
        return self


class RollbackResponse(_ResponseMeta):
    """The outcome of an improvement rollback or a relearn rollback.

    The optional ``domain_event_id``/``audit_event_seq``/``node_version``/
    ``knowledge_seq`` fields are the spec 7.9 mutation-receipt fields; a
    relearn rollback restores bytes instead of appending a domain event, so
    they stay ``None`` there.
    """

    kind: Literal["improvement", "relearn"]
    node_id: str | None = None
    relearn_id: str | None = None
    from_version: int | None = None
    to_version: int | None = None
    status: str
    content_sha256: str | None = None
    restored_db_sha256: str | None = None
    verified: bool | None = None
    idempotent_replay: bool | None = None
    domain_event_id: str | None = None
    audit_event_seq: int | None = None
    node_version: int | None = None
    knowledge_seq: int | None = None

"""Deterministic review state machine for the improvement lifecycle.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 10.

The policy is a pure function over a :class:`ReviewSignal`: given the same
signal and the same frozen :class:`ReviewPolicy` it always returns the same
:class:`ReviewDecision`. Oscillation is prevented structurally:

* **Cooldown** — a minimum interval between status transitions; safety
  demotions (``needs_improvement``, ``deprecate``, ``merge_or_deduplicate``)
  are never delayed by it.
* **Hysteresis** — entering ``ready_to_promote`` requires ``P >= 0.75`` while
  keeping it only requires ``P >= 0.70``; entering ``needs_improvement``
  requires ``P < 0.30`` while leaving it requires ``P >= 0.35``.
* **One event cannot promote** — ``ready_to_promote`` additionally needs at
  least ``min_evidence_for_promotion`` evidence items and
  ``min_qualifying_reviews`` consecutive qualifying reviews, so a single
  observation or review can never promote a node.
* **Relearn gate** — while ``RELEARN_PENDING`` no promotion is emitted (frozen
  relearn contract: no operator decision means no change).

The engine persists a transition as a new immutable node revision
(``node.upsert``, knowledge-affecting) and a non-transition review as a
zero-weight ``admin`` record; both flow through ``EventStore.commit`` so every
review invocation receives exactly one final audit event. Review history is
read back from the event log, which makes the consecutive-qualifying rule and
the cooldown fully deterministic and replayable.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Annotated

from pydantic import BaseModel, ConfigDict, Field

from know_your_soul.domain import (
    Event,
    EventKind,
    LifecycleStatus,
    Node,
    RelationKind,
    StrictDecimal,
    canonical_json,
)
from know_your_soul.events import Invocation, WriteReceipt
from know_your_soul.source_registry import RELEARN_PENDING, SourceRegistry
from know_your_soul.store import EventStore, StoreError

__all__ = [
    "DEFAULT_REVIEW_POLICY",
    "ReviewDecision",
    "ReviewEngine",
    "ReviewPolicy",
    "ReviewReport",
    "ReviewSignal",
    "UnknownNode",
    "decide",
]


class UnknownNode(StoreError):
    """A review was requested for a node that does not exist."""


CooldownDelta = Annotated[timedelta, Field()]

_SAFETY_DEMOTIONS: frozenset[LifecycleStatus] = frozenset(
    {LifecycleStatus.NEEDS_IMPROVEMENT, LifecycleStatus.DEPRECATE, LifecycleStatus.MERGE_OR_DEDUPLICATE}
)


class ReviewPolicy(BaseModel):
    """Frozen, versioned transition thresholds (spec section 10).

    The promotion thresholds mirror the frozen scoring contract (P gate 0.70,
    ``U_P <= 0.50``); the hysteresis margins and cooldown are review-layer
    oscillation guards and never weaken the scoring gate itself.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "review-policy-1.0.0"
    promotion_p_threshold: StrictDecimal = Decimal("0.70")
    promotion_hysteresis: StrictDecimal = Decimal("0.05")
    promotion_uncertainty_max: StrictDecimal = Decimal("0.50")
    improvement_p_threshold: StrictDecimal = Decimal("0.30")
    improvement_hysteresis: StrictDecimal = Decimal("0.05")
    observe_uncertainty_threshold: StrictDecimal = Decimal("0.70")
    min_evidence_for_promotion: int = Field(default=2, ge=1)
    min_qualifying_reviews: int = Field(default=2, ge=1)
    cooldown: timedelta = timedelta(hours=24)


DEFAULT_REVIEW_POLICY = ReviewPolicy()


class ReviewSignal(BaseModel):
    """Everything the deterministic policy is allowed to consider."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    current_status: LifecycleStatus
    p: StrictDecimal
    m: StrictDecimal
    e: StrictDecimal
    uncertainty: StrictDecimal
    evidence_count: int = Field(ge=0)
    qualifying_review_streak: int = Field(ge=0)
    last_transition_at: datetime | None
    at: datetime
    relearn_pending: bool = False
    has_duplicates: bool = False
    contradiction_tie: bool = False
    retired: bool = False
    just_rolled_back: bool = False


class ReviewDecision(BaseModel):
    """The outcome of one deterministic policy evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    current_status: LifecycleStatus
    desired_status: LifecycleStatus
    target_status: LifecycleStatus
    transitioned: bool
    qualifying_for_promotion: bool
    reasons: tuple[str, ...] = ()
    blocked_by: tuple[str, ...] = ()
    policy_version: str


def decide(signal: ReviewSignal, policy: ReviewPolicy = DEFAULT_REVIEW_POLICY) -> ReviewDecision:
    """Map one signal to a target status; pure and deterministic."""
    reasons: list[str] = []
    blocked: list[str] = []
    current = signal.current_status

    if signal.retired:
        desired, reasons = LifecycleStatus.DEPRECATE, ["retired-node"]
    elif signal.has_duplicates:
        desired, reasons = LifecycleStatus.MERGE_OR_DEDUPLICATE, ["duplicate-relations-present"]
    elif signal.contradiction_tie:
        # Frozen signed recency rule: a tie is left needs_improvement (spec 13.3).
        desired, reasons = LifecycleStatus.NEEDS_IMPROVEMENT, ["contradiction-tie-unresolved"]
    elif signal.just_rolled_back:
        desired, reasons = LifecycleStatus.ROLLBACK, ["improvement-rolled-back"]
    else:
        desired, reasons = _score_driven_status(signal, policy)

    qualifying = _qualifies_for_promotion(signal, policy)
    if desired is LifecycleStatus.READY_TO_PROMOTE:
        if signal.relearn_pending:
            blocked.append("relearn-pending")
        if signal.evidence_count < policy.min_evidence_for_promotion:
            blocked.append("insufficient-evidence")
        if signal.qualifying_review_streak + 1 < policy.min_qualifying_reviews:
            blocked.append("one-event-cannot-promote")

    transitioned = desired is not current and not blocked
    if transitioned and desired not in _SAFETY_DEMOTIONS and signal.last_transition_at is not None:
        if signal.at - signal.last_transition_at < policy.cooldown:
            blocked.append("cooldown")
            transitioned = False

    return ReviewDecision(
        node_id=signal.node_id,
        current_status=current,
        desired_status=desired,
        target_status=desired if transitioned else current,
        transitioned=transitioned,
        qualifying_for_promotion=qualifying,
        reasons=tuple(reasons),
        blocked_by=tuple(blocked),
        policy_version=policy.policy_version,
    )


def _score_driven_status(signal: ReviewSignal, policy: ReviewPolicy) -> tuple[LifecycleStatus, list[str]]:
    current = signal.current_status
    improvement_ceiling = policy.improvement_p_threshold
    if current is LifecycleStatus.NEEDS_IMPROVEMENT:
        improvement_ceiling = policy.improvement_p_threshold + policy.improvement_hysteresis
    if signal.p < improvement_ceiling:
        return LifecycleStatus.NEEDS_IMPROVEMENT, ["p-below-improvement-threshold"]
    if signal.evidence_count == 0 or signal.uncertainty > policy.observe_uncertainty_threshold:
        return LifecycleStatus.OBSERVE, ["insufficient-evidence-or-high-uncertainty"]
    promotion_floor = policy.promotion_p_threshold
    if current is not LifecycleStatus.READY_TO_PROMOTE:
        promotion_floor = policy.promotion_p_threshold + policy.promotion_hysteresis
    if signal.p >= promotion_floor and signal.uncertainty <= policy.promotion_uncertainty_max:
        return LifecycleStatus.READY_TO_PROMOTE, ["promotion-criteria-met"]
    if current is LifecycleStatus.READY_TO_PROMOTE:
        return LifecycleStatus.MAINTAIN, ["promotion-criteria-lost"]
    return LifecycleStatus.MAINTAIN, ["within-tolerance"]


def _qualifies_for_promotion(signal: ReviewSignal, policy: ReviewPolicy) -> bool:
    return (
        signal.p >= policy.promotion_p_threshold + policy.promotion_hysteresis
        and signal.uncertainty <= policy.promotion_uncertainty_max
        and signal.evidence_count >= policy.min_evidence_for_promotion
    )


class ReviewReport(BaseModel):
    """The persisted outcome of one ``knowledge.review`` evaluation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    decision: ReviewDecision
    persisted: bool
    from_version: int
    to_version: int
    audit_event_id: str
    audit_event_seq: int
    relearn_pending: bool = False


# Scope hook supplied by the relearn engine when wired by the coordinator:
# returns (compatible source version ids, quarantined evidence ids).
EvidenceScope = Callable[[], tuple[frozenset[str], frozenset[str]]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class ReviewEngine:
    """Store-backed review: reads evidence/history, applies the pure policy."""

    def __init__(
        self,
        store: EventStore,
        *,
        policy: ReviewPolicy = DEFAULT_REVIEW_POLICY,
        registry: SourceRegistry | None = None,
        evidence_scope: EvidenceScope | None = None,
    ) -> None:
        self._store = store
        self._policy = policy
        self._registry = registry
        self._evidence_scope = evidence_scope

    # ------------------------------------------------------------------ read

    def _review_history(self, node_id: str) -> list[dict[str, object]]:
        records: list[dict[str, object]] = []
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_log WHERE kind = 'domain' AND node_id = ? ORDER BY event_seq",
                (node_id,),
            ).fetchall()
        for row in rows:
            review = (json.loads(str(row["payload_json"])).get("payload") or {}).get("review")
            if isinstance(review, dict):
                records.append(review)
        return records

    def _last_rollback_at(self, node_id: str) -> datetime | None:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT payload_json FROM event_log "
                "WHERE kind = 'domain' AND node_id = ? AND event_type = 'node.upsert' ORDER BY event_seq",
                (node_id,),
            ).fetchall()
        latest: datetime | None = None
        for row in rows:
            data = json.loads(str(row["payload_json"]))
            if isinstance((data.get("payload") or {}).get("improvement_rollback"), dict):
                latest = datetime.fromisoformat(str(data["occurred_at"]))
        return latest

    def signal_for(self, node_id: str, *, at: datetime) -> ReviewSignal:
        node = self._store.current_node(node_id)
        if node is None:
            raise UnknownNode(f"cannot review unknown node {node_id!r}")
        history = self._review_history(node_id)
        streak = 0
        for record in reversed(history):
            if record.get("qualifying"):
                streak += 1
            else:
                break
        transition_times = [
            datetime.fromisoformat(str(record["at"])) for record in history if record.get("transitioned")
        ]
        last_transition_at = max(transition_times) if transition_times else None
        last_review_at = max((datetime.fromisoformat(str(record["at"])) for record in history), default=None)
        last_rollback_at = self._last_rollback_at(node_id)
        just_rolled_back = last_rollback_at is not None and (
            last_review_at is None or last_rollback_at > last_review_at
        )
        evidence_count = self._scorable_evidence_count(node)
        relearn_pending = self._registry is not None and self._registry.relearn_state() == RELEARN_PENDING
        has_duplicates = any(
            relation.kind is RelationKind.DUPLICATES for relation in self._store.relations_from(node_id)
        )
        return ReviewSignal(
            node_id=node_id,
            current_status=node.lifecycle_status,
            p=node.scores.p,
            m=node.scores.m,
            e=node.scores.e,
            uncertainty=node.scores.uncertainty,
            evidence_count=evidence_count,
            qualifying_review_streak=streak,
            last_transition_at=last_transition_at,
            at=at,
            relearn_pending=relearn_pending,
            has_duplicates=has_duplicates,
            retired=node.lifecycle_status is LifecycleStatus.SUPERSEDED,
            just_rolled_back=just_rolled_back,
        )

    def _scorable_evidence_count(self, node: Node) -> int:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT evidence_id, source_version_id FROM evidence WHERE node_id = ?", (node.node_id,)
            ).fetchall()
        if self._evidence_scope is None:
            return len(rows)
        compatible, quarantined = self._evidence_scope()
        return sum(
            1
            for row in rows
            if str(row["source_version_id"]) in compatible and str(row["evidence_id"]) not in quarantined
        )

    def assess(self, node_id: str, *, at: datetime | None = None) -> ReviewDecision:
        """Evaluate the policy without persisting anything."""
        return decide(self.signal_for(node_id, at=at or _utcnow()), self._policy)

    # ----------------------------------------------------------------- write

    def review(
        self,
        node_id: str,
        *,
        at: datetime | None = None,
        actor: str = "operator",
        idempotency_key: str | None = None,
    ) -> ReviewReport:
        """Evaluate one node and persist the outcome as an immutable record."""
        at = at or _utcnow()
        node = self._store.current_node(node_id)
        if node is None:
            raise UnknownNode(f"cannot review unknown node {node_id!r}")
        decision = decide(self.signal_for(node_id, at=at), self._policy)
        record = {
            "at": at.isoformat(),
            "node_id": node_id,
            "current_status": decision.current_status.value,
            "target_status": decision.target_status.value,
            "transitioned": decision.transitioned,
            "qualifying": decision.qualifying_for_promotion,
            "reasons": list(decision.reasons),
            "blocked_by": list(decision.blocked_by),
            "policy_version": decision.policy_version,
        }
        if decision.transitioned:
            updated = node.model_copy(update={"lifecycle_status": decision.target_status, "updated_at": at})
            event = Event(
                kind=EventKind.NODE_UPSERT,
                node_id=node_id,
                domain=node.domain,
                node=updated,
                source_version_id=node.source_version_id,
                occurred_at=at,
                payload={"review": record},
            )
        else:
            # A non-transition review carries zero evidence weight: it records
            # the decision for the streak/cooldown history without moving the
            # knowledge watermark or bumping the node version.
            event = Event(
                kind=EventKind.ADMIN,
                node_id=node_id,
                domain=node.domain,
                occurred_at=at,
                knowledge_affecting=False,
                payload={"review": record},
            )
        request_hash = hashlib.sha256(canonical_json({"node_id": node_id, "at": at.isoformat()}).encode()).hexdigest()
        receipt: WriteReceipt = self._store.commit(
            event,
            invocation=Invocation.create(
                "knowledge.review", request_hash, actor=actor, idempotency_key=idempotency_key
            ),
        )
        return ReviewReport(
            node_id=node_id,
            decision=decision,
            persisted=True,
            from_version=node.version,
            to_version=receipt.node_version,
            audit_event_id=receipt.audit_event_id,
            audit_event_seq=receipt.audit_event_seq,
            relearn_pending=self._registry is not None and self._registry.relearn_state() == RELEARN_PENDING,
        )

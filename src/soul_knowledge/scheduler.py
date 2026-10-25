"""Due-review scheduler: deterministic metadata scans, never model calls.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 8: "A scheduler scans due evaluation metadata, not the agent. It
emits candidates through ``knowledge.review``; it never calls a model and
never silently rewrites knowledge."

``scan_due`` is strictly read-only: it never writes to the store and never
mutates node state. Each candidate optionally carries the review decision the
engine *would* emit, so the caller can route it through ``knowledge.review``.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from decimal import Decimal

from pydantic import BaseModel, ConfigDict, Field

from soul_knowledge.domain import LifecycleStatus, Node, PrimaryDomain, StrictDecimal
from soul_knowledge.review import ReviewDecision, ReviewEngine
from soul_knowledge.store import EventStore

__all__ = ["DueReviewScheduler", "ReviewCandidate", "SchedulePolicy"]


class SchedulePolicy(BaseModel):
    """Frozen scheduling thresholds; metadata only, no scoring changes."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    policy_version: str = "schedule-policy-1.0.0"
    review_interval: timedelta = timedelta(days=7)
    dormancy: timedelta = timedelta(days=30)
    uncertainty_threshold: StrictDecimal = Decimal("0.70")
    reviewable_statuses: tuple[LifecycleStatus, ...] = (
        LifecycleStatus.ACTIVE,
        LifecycleStatus.MAINTAIN,
        LifecycleStatus.OBSERVE,
        LifecycleStatus.NEEDS_IMPROVEMENT,
        LifecycleStatus.READY_TO_PROMOTE,
        LifecycleStatus.ROLLBACK,
    )


class ReviewCandidate(BaseModel):
    """One due node plus the decision ``knowledge.review`` would emit."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str
    version: int = Field(ge=1)
    domain: PrimaryDomain
    reasons: tuple[str, ...]
    last_review_at: datetime | None
    decision: ReviewDecision | None = None


def _utcnow() -> datetime:
    return datetime.now(UTC)


class DueReviewScheduler:
    """Scans node metadata and review history for due reviews."""

    def __init__(
        self,
        store: EventStore,
        engine: ReviewEngine,
        *,
        policy: SchedulePolicy | None = None,
    ) -> None:
        self._store = store
        self._engine = engine
        self._policy = policy or SchedulePolicy()

    def _latest_nodes(self) -> list[Node]:
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT nr.node_id, nr.payload_json FROM node_revision nr "
                "JOIN (SELECT node_id, MAX(version) AS version FROM node_revision GROUP BY node_id) latest "
                "ON nr.node_id = latest.node_id AND nr.version = latest.version"
            ).fetchall()
        return [Node.model_validate(json.loads(str(row["payload_json"]))) for row in rows]

    def _last_review_times(self) -> dict[str, datetime]:
        last: dict[str, datetime] = {}
        with self._store.read() as conn:
            rows = conn.execute(
                "SELECT node_id, payload_json FROM event_log WHERE kind = 'domain' AND node_id IS NOT NULL "
                "ORDER BY event_seq"
            ).fetchall()
        for row in rows:
            review = (json.loads(str(row["payload_json"])).get("payload") or {}).get("review")
            if isinstance(review, dict):
                last[str(row["node_id"])] = datetime.fromisoformat(str(review["at"]))
        return last

    def scan_due(self, *, at: datetime | None = None, attach_decisions: bool = True) -> list[ReviewCandidate]:
        """Return the due nodes in deterministic ``node_id`` order. Read-only."""
        at = at or _utcnow()
        last_reviews = self._last_review_times()
        candidates: list[ReviewCandidate] = []
        for node in sorted(self._latest_nodes(), key=lambda item: item.node_id):
            if node.lifecycle_status not in self._policy.reviewable_statuses:
                continue
            reasons: list[str] = []
            last_review_at = last_reviews.get(node.node_id)
            if last_review_at is None:
                reasons.append("never-reviewed")
            elif at - last_review_at >= self._policy.review_interval:
                reasons.append("review-overdue")
            last_activity = node.last_used_at or node.updated_at
            if at - last_activity >= self._policy.dormancy:
                reasons.append("dormant")
            if node.scores.uncertainty > self._policy.uncertainty_threshold:
                reasons.append("high-uncertainty")
            if not reasons:
                continue
            decision = self._engine.assess(node.node_id, at=at) if attach_decisions else None
            candidates.append(
                ReviewCandidate(
                    node_id=node.node_id,
                    version=node.version,
                    domain=node.domain,
                    reasons=tuple(reasons),
                    last_review_at=last_review_at,
                    decision=decision,
                )
            )
        return candidates

"""Audit and mutation receipts, invocation contracts and chain reports.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7 and 13.3.

These are the *value objects* the store returns. They never carry provider or
model configuration: a mutation response exposes the original domain fields, the
new per-invocation audit identifier, the final audit ``event_seq``, the active
projection version and the source knowledge high-water mark (spec section 7.9).
"""

from __future__ import annotations

from collections.abc import Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from soul_knowledge.domain import new_id

__all__ = [
    "AuditReceipt",
    "ChainReport",
    "DomainReceipt",
    "Invocation",
    "InvocationJournal",
    "InvocationOutcome",
    "WriteReceipt",
]


class DomainReceipt(BaseModel):
    """The immutable result of one domain mutation.

    On an idempotent replay the original ``domain_event_id``, ``event_seq``,
    ``node_version`` and ``knowledge_seq`` are preserved and the knowledge
    watermark never moves (spec section 7, "Idempotent retry").
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain_event_id: str
    event_seq: int
    node_version: int
    knowledge_seq: int | None
    idempotent_replay: bool


class AuditReceipt(BaseModel):
    """The single final audit record of one valid invocation."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str
    audit_event_id: str
    event_seq: int
    status: Literal["success", "failure"]
    outcome: Mapping[str, Any] = Field(default_factory=dict)


class WriteReceipt(BaseModel):
    """A mutation response binding the domain result to its audit and projection."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    domain_event_id: str
    event_seq: int
    node_version: int
    knowledge_seq: int | None
    idempotent_replay: bool
    audit_event_id: str
    audit_event_seq: int
    projection_version: str | None
    source_knowledge_high_watermark: int


class ChainReport(BaseModel):
    """The result of re-deriving the event hash chain."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    ok: bool
    checked: int
    first_bad_seq: int | None = None


class Invocation(BaseModel):
    """The durable identity of one tool invocation, allocated before validation.

    ``audit_event_id`` is allocated here and identifies the single final audit
    event written on completion (spec section 7.1/7.7).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str
    audit_event_id: str
    operation: str
    request_hash: str
    actor: str = "operator"
    idempotency_key: str | None = None
    started_at: datetime = Field(default_factory=lambda: datetime.now(UTC))

    @classmethod
    def create(
        cls,
        operation: str,
        request_hash: str,
        actor: str = "operator",
        *,
        idempotency_key: str | None = None,
        started_at: datetime | None = None,
    ) -> Invocation:
        return cls(
            invocation_id=new_id(),
            audit_event_id=new_id(),
            operation=operation,
            request_hash=request_hash,
            actor=actor,
            idempotency_key=idempotency_key,
            started_at=started_at or datetime.now(UTC),
        )


class InvocationOutcome(BaseModel):
    """The recorded outcome of an invocation (success or failure).

    ``linked_domain_event_id`` links an idempotent retry, a crash-recovery pass
    or a validation failure to the domain result it refers to (spec section 7).
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    status: Literal["success", "failure"]
    result_references: Mapping[str, Any] = Field(default_factory=dict)
    error: str | None = None
    projection_outcome: str | None = None
    duration_ms: int = 0
    correlation_id: str | None = None
    linked_domain_event_id: str | None = None
    recovered: bool = False


class InvocationJournal(BaseModel):
    """Operational recovery state for one invocation; not an audit event."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    invocation_id: str
    audit_event_id: str
    operation: str
    request_hash: str
    actor: str
    idempotency_key: str | None
    started_at: datetime
    status: Literal["in_progress", "complete"]

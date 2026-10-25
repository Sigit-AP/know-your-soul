"""Durable invocation journals and exactly-once final audit completion.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 7.

Flow (spec 7.1, 7.7, 7.8):

1. ``begin`` writes a durable journal row with the pre-allocated
   ``audit_event_id``. The journal is operational recovery state; it is *not* an
   append-only audit event.
2. ``complete`` appends exactly one final audit event (success or failure) and
   marks the journal complete, in one transaction. It is idempotent, so calling
   it twice can never write a second audit event.
3. ``recover_incomplete`` finds journals left ``in_progress`` by a crash,
   repairs the projection once, reconciles the domain record by idempotency key,
   and completes each journal exactly once — never duplicating a domain or audit
   event.
"""

from __future__ import annotations

import json
from collections.abc import Callable
from datetime import UTC, datetime
from typing import Any, Literal

from soul_knowledge.events import AuditReceipt, Invocation, InvocationJournal, InvocationOutcome
from soul_knowledge.store import EventStore, StoreError

__all__ = ["AuditLog", "UnknownInvocation"]


class UnknownInvocation(StoreError):
    """``complete`` was called for an invocation that has no journal."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


class AuditLog:
    """The write path for invocation journals and final audit events."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def begin(self, invocation: Invocation) -> InvocationJournal:
        """Create the durable journal for *invocation*."""
        with self._store.transaction() as conn:
            self._store.insert_journal(conn, invocation)
        return InvocationJournal(
            invocation_id=invocation.invocation_id,
            audit_event_id=invocation.audit_event_id,
            operation=invocation.operation,
            request_hash=invocation.request_hash,
            actor=invocation.actor,
            idempotency_key=invocation.idempotency_key,
            started_at=invocation.started_at,
            status="in_progress",
        )

    def complete(self, invocation_id: str, outcome: InvocationOutcome) -> AuditReceipt:
        """Append exactly one final audit event for *invocation_id*."""
        payload = outcome.model_dump(mode="json")
        with self._store.transaction() as conn:
            row = self._store.fetch_journal(conn, invocation_id)
            if row is None:
                raise UnknownInvocation(f"no journal for invocation {invocation_id!r}")
            audit_event_id = str(row["audit_event_id"])
            if str(row["status"]) == "complete":
                stored = json.loads(str(row["outcome_json"]))
                stored_status: Literal["success", "failure"] = (
                    "success" if stored.get("status") == "success" else "failure"
                )
                return AuditReceipt(
                    invocation_id=invocation_id,
                    audit_event_id=audit_event_id,
                    event_seq=int(row["audit_event_seq"]),
                    status=stored_status,
                    outcome=stored,
                )
            event = self._store.insert_event(
                conn,
                event_id=audit_event_id,
                kind="audit",
                event_type=str(row["operation"]),
                knowledge_affecting=False,
                actor=str(row["actor"]),
                occurred_at=_now_iso(),
                payload=payload,
            )
            self._store.complete_journal(conn, invocation_id, outcome=payload, audit_event_seq=event.event_seq)
            return AuditReceipt(
                invocation_id=invocation_id,
                audit_event_id=audit_event_id,
                event_seq=event.event_seq,
                status=outcome.status,
                outcome=payload,
            )

    def journal_outcome(self, invocation_id: str) -> dict[str, Any] | None:
        """The stored outcome payload of a completed journal, or ``None``."""
        raw = self._store.journal_outcome_json(invocation_id)
        return None if raw is None else json.loads(raw)

    def pending_invocations(self) -> list[InvocationJournal]:
        """Journals still ``in_progress`` (a crash happened before completion)."""
        journals: list[InvocationJournal] = []
        for row in self._store.incomplete_journals():
            journals.append(
                InvocationJournal(
                    invocation_id=str(row["invocation_id"]),
                    audit_event_id=str(row["audit_event_id"]),
                    operation=str(row["operation"]),
                    request_hash=str(row["request_hash"]),
                    actor=str(row["actor"]),
                    idempotency_key=row["idempotency_key"],
                    started_at=datetime.fromisoformat(str(row["started_at"])),
                    status="in_progress",
                )
            )
        return journals

    def recover_incomplete(
        self,
        *,
        projection_repair: Callable[[], None] | None = None,
    ) -> list[AuditReceipt]:
        """Reconcile crashed invocations, appending exactly one audit event each."""
        rows = self._store.incomplete_journals()
        if not rows:
            return []

        projection_error: str | None = None
        if projection_repair is not None:
            try:
                projection_repair()
            except Exception as error:  # noqa: BLE001 - any repair failure is recorded, never hidden
                projection_error = f"PROJECTION_REPAIR_FAILED: {error}"

        receipts: list[AuditReceipt] = []
        for row in rows:
            invocation_id = str(row["invocation_id"])
            idempotency_key = row["idempotency_key"]
            linked_domain_event_id: str | None = None
            if idempotency_key is not None:
                domain = self._store.domain_event_by_idempotency(str(idempotency_key))
                if domain is not None:
                    linked_domain_event_id = str(domain["event_id"])

            if projection_error is not None:
                outcome = InvocationOutcome(
                    status="failure",
                    error=projection_error,
                    projection_outcome="failed",
                    recovered=True,
                    linked_domain_event_id=linked_domain_event_id,
                )
            elif linked_domain_event_id is not None:
                outcome = InvocationOutcome(
                    status="success",
                    projection_outcome="repaired",
                    recovered=True,
                    linked_domain_event_id=linked_domain_event_id,
                )
            else:
                outcome = InvocationOutcome(
                    status="failure",
                    error="INTERRUPTED_BEFORE_COMMIT",
                    recovered=True,
                )
            receipts.append(self.complete(invocation_id, outcome))
        return receipts

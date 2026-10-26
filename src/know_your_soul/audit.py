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
   repairs the projection once, reconciles the domain record by the effective
   idempotency key (the caller's key, falling back to the invocation id), and
   completes each journal exactly once — never duplicating a domain or audit
   event.

Liveness contract (spec 7.8, finding I1): ``EventStore.commit`` refreshes the
journal heartbeat around the domain write, and ``recover_incomplete`` only
closes journals whose last heartbeat (or ``started_at``) is older than
``stale_after_seconds`` (default 300 s). Recovery must run *before* a process
accepts traffic, so a live invocation in another process is never audited as
``INTERRUPTED_BEFORE_COMMIT``.
"""

from __future__ import annotations

import json
import sqlite3
import time
from collections.abc import Callable, Mapping
from datetime import UTC, datetime
from typing import Any, Literal

from know_your_soul.domain import storage_payload
from know_your_soul.events import AuditReceipt, Invocation, InvocationJournal, InvocationOutcome
from know_your_soul.store import EventStore, StoreError

__all__ = ["AuditLog", "UnknownInvocation", "failure_outcome", "success_outcome"]


class UnknownInvocation(StoreError):
    """``complete`` was called for an invocation that has no journal."""


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def success_outcome(
    invocation: Invocation,
    started: float,
    *,
    result_references: Mapping[str, Any] | None = None,
    projection_outcome: str = "appended",
    linked_domain_event_id: str | None = None,
) -> InvocationOutcome:
    """The spec 7.7 success outcome for one mutation-path final audit.

    Every final audit carries outcome/result references, the projection
    outcome, the measured duration and the correlation ID (the invocation id,
    mirroring the read-path ``KnowledgeService._finish_success``). *started* is
    a ``time.monotonic()`` reading taken when the invocation began.
    """
    return InvocationOutcome(
        status="success",
        result_references=result_references or {},
        projection_outcome=projection_outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        correlation_id=invocation.invocation_id,
        linked_domain_event_id=linked_domain_event_id,
    )


def failure_outcome(
    invocation: Invocation,
    started: float,
    error: BaseException,
    *,
    projection_outcome: str = "skipped",
    linked_domain_event_id: str | None = None,
) -> InvocationOutcome:
    """The spec 7.7 failure outcome: typed error plus the same chain fields."""
    return InvocationOutcome(
        status="failure",
        error=f"{type(error).__name__}: {error}",
        projection_outcome=projection_outcome,
        duration_ms=int((time.monotonic() - started) * 1000),
        correlation_id=invocation.invocation_id,
        linked_domain_event_id=linked_domain_event_id,
    )


class AuditLog:
    """The write path for invocation journals and final audit events."""

    def __init__(self, store: EventStore) -> None:
        self._store = store

    def begin(self, invocation: Invocation) -> InvocationJournal:
        """Create the durable journal for *invocation*."""
        try:
            with self._store.transaction() as conn:
                self._store.insert_journal(conn, invocation)
        except sqlite3.IntegrityError as error:
            raise StoreError(f"invocation {invocation.invocation_id!r} already has a journal") from error
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
        payload = outcome.model_dump(mode="python")
        stored_payload = storage_payload(payload)
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
                outcome=stored_payload,
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
        stale_after_seconds: float = 300.0,
    ) -> list[AuditReceipt]:
        """Reconcile crashed invocations, appending exactly one audit event each.

        Only journals whose last heartbeat (or ``started_at``) is older than
        ``stale_after_seconds`` are closed; a fresh journal belongs to a live
        invocation and is left alone. Run recovery before accepting traffic.
        """
        rows = self._store.incomplete_journals(stale_after_seconds)
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
            # The effective key is the one the domain write actually used: the
            # caller's idempotency key, falling back to the invocation id.
            effective_key = row["effective_idempotency_key"] or row["idempotency_key"] or invocation_id
            linked_domain_event_id: str | None = None
            domain = self._store.domain_event_by_idempotency(str(effective_key))
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

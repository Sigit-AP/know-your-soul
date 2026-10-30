"""The contract-v4 extraction engine (SUB-106).

Orchestrates the ``knowledge.submit_extraction`` verb and the operator review
surface over the durable queue:

* ``submit`` — validate (closed schema, sanitization, caps), bind provenance
  (``source_path`` re-hash against the fixed extraction root), dedupe on the
  locked idempotency key, recompute the corroboration group, persist. Exactly
  one final audit event per invocation, success and typed failure alike,
  carrying hashes and counts — never the submitted content.
* ``review`` — the read-only queue listing / per-submission diff.
* ``approve`` — the operator-gated apply: nodes ride the existing audited
  ``knowledge.log_event`` write path (deterministic ids and timestamps, so a
  retried approve replays instead of double-applying), edges ride audited
  ``relation.add`` commits, then the row is marked ``applied`` and exactly
  one ``knowledge.extraction_approve`` decision audit is journaled.
* ``reject`` — marks the row ``rejected`` with the operator's note and one
  ``knowledge.extraction_reject`` audit event. Writes nothing to the graph.

Crash discipline: the queue row is committed before the final audit; a crash
between them leaves an ``in_progress`` journal that the store's standard
``recover_incomplete`` closes, and an idempotent resubmission returns the
same receipt. An approve interrupted mid-apply is retried safely: every
graph write carries a deterministic idempotency key.
"""

from __future__ import annotations

import hashlib
import os
import sqlite3
import time
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any

from know_your_soul.audit import AuditLog, failure_outcome, success_outcome
from know_your_soul.domain import Event, EventKind, PrimaryDomain, Relation, RelationKind, canonical_json
from know_your_soul.events import Invocation
from know_your_soul.extraction.corroboration import compare_payloads, recompute_group
from know_your_soul.extraction.errors import (
    ExtractionDuplicateError,
    ExtractionError,
    ExtractionNotFoundError,
    ExtractionStateError,
    SourceHashMismatchError,
    SourceNotFoundError,
    SourcePathOutsideRootError,
)
from know_your_soul.extraction.queue import ExtractionQueue, QueueRow
from know_your_soul.extraction.schema import Submission, sha256_file, submission_from_canonical, validate_submission
from know_your_soul.extraction.tier import applied_node_id, stamp_node
from know_your_soul.security import PathEscapeError, resolve_within_root
from know_your_soul.store import EventStore

if TYPE_CHECKING:
    from know_your_soul.service import KnowledgeService

__all__ = ["ExtractionEngine"]

_SUBMIT_OPERATION = "knowledge.submit_extraction"
_APPROVE_OPERATION = "knowledge.extraction_approve"
_REJECT_OPERATION = "knowledge.extraction_reject"
_EDGE_OPERATION = "knowledge.extraction_approve_edge"

_TEXT_EXCERPT_CHARS = 512


def _request_hash(arguments: Any) -> str:
    try:
        canonical = canonical_json(arguments) if isinstance(arguments, (dict, list)) else "null"
    except TypeError, ValueError:  # pragma: no cover - defensive
        canonical = "null"
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _receipt_for(row: QueueRow, *, duplicate: bool) -> dict[str, Any]:
    receipt = dict(row.receipt)
    receipt.update(
        {
            "receipt_id": row.receipt_id,
            "status": row.status,
            "duplicate": duplicate,
            "source_sha256": row.source_sha256,
            "payload_sha256": row.payload_sha256,
            "confidence_reported": row.confidence_reported,
            "confidence_effective": row.confidence_effective,
            "corroboration": {
                "corroborators": row.corroborators,
                "contradictions": row.contradictions,
                "overlap_ratio": row.overlap_ratio,
            },
            "nodes": len(row.payload["nodes"]),
            "edges": len(row.payload["edges"]),
        }
    )
    receipt.pop("audit_event_id", None)
    return receipt


class ExtractionEngine:
    """The submit/review/approve/reject orchestration over the queue."""

    def __init__(self, store: EventStore, audit: AuditLog, *, queue: ExtractionQueue | None = None) -> None:
        self._store = store
        self._audit = audit
        self._queue = queue if queue is not None else ExtractionQueue.for_store(store)

    @property
    def queue(self) -> ExtractionQueue:
        return self._queue

    # ------------------------------------------------------------- submit

    def submit(
        self,
        arguments: Any,
        *,
        actor: str,
        extraction_root: str | Path | None = None,
    ) -> dict[str, Any]:
        """Run one ``knowledge.submit_extraction`` invocation (contract v4)."""
        started = time.monotonic()
        invocation = Invocation.create(_SUBMIT_OPERATION, request_hash=_request_hash(arguments), actor=actor)
        self._audit.begin(invocation)
        try:
            submission = validate_submission(arguments)
            self._verify_source_binding(submission, extraction_root)
            receipt, duplicate = self._insert_or_replay(submission, actor)
        except Exception as error:
            audit_receipt = self._audit.complete(invocation.invocation_id, failure_outcome(invocation, started, error))
            if isinstance(error, ExtractionError):
                error.audit_event_id = audit_receipt.audit_event_id
            raise
        outcome = success_outcome(
            invocation,
            started,
            result_references={
                "receipt_id": receipt["receipt_id"],
                "source_sha256": receipt["source_sha256"],
                "payload_sha256": receipt["payload_sha256"],
                "nodes": receipt["nodes"],
                "edges": receipt["edges"],
                "duplicate": duplicate,
            },
            projection_outcome="skipped",
        )
        audit_receipt = self._audit.complete(invocation.invocation_id, outcome)
        return {**receipt, "audit_event_id": audit_receipt.audit_event_id}

    def _verify_source_binding(self, submission: Submission, extraction_root: str | Path | None) -> None:
        """Re-hash the bound file; the claim must match ``source_sha256``."""
        if submission.source_path is None:
            return
        if extraction_root is None:
            raise SourceNotFoundError("source_path requires a startup-fixed extraction root")
        try:
            resolved = resolve_within_root(extraction_root, submission.source_path)
        except PathEscapeError as error:
            raise SourcePathOutsideRootError(
                f"source_path {submission.source_path!r} escapes the extraction root"
            ) from error
        if not resolved.is_file() or not os.access(resolved, os.R_OK):
            raise SourceNotFoundError(f"source_path {submission.source_path!r} names no readable regular file")
        actual = sha256_file(resolved)
        if actual != submission.source_sha256:
            raise SourceHashMismatchError(
                f"source_path {submission.source_path!r} hashes to {actual}, not the claimed {submission.source_sha256}"
            )

    def _insert_or_replay(self, submission: Submission, actor: str) -> tuple[dict[str, Any], bool]:
        """Insert the row and recompute the group; replays return stored receipts."""
        idem_key = submission.idem_key(actor)
        existing = self._queue.get_by_idem_key(idem_key)
        if existing is not None:
            if existing.payload_sha256 != submission.payload_sha256:
                raise ExtractionDuplicateError(
                    f"the idempotency key (source, actor, method) is already bound to payload "
                    f"{existing.payload_sha256}; refusing to silently replace it"
                )
            return _receipt_for(existing, duplicate=True), True
        row = QueueRow.from_submission(submission, actor=actor, receipt={})
        try:
            self._queue.insert(row)
        except sqlite3.IntegrityError:
            # A concurrent identical submission won the insert race; replay it.
            raced = self._queue.get_by_idem_key(idem_key)
            if raced is not None and raced.payload_sha256 == submission.payload_sha256:
                return _receipt_for(raced, duplicate=True), True
            raise
        self._recompute(submission.source_sha256)
        stored = self._queue.get(row.receipt_id)
        assert stored is not None  # the row was inserted in this invocation
        receipt = _receipt_for(stored, duplicate=False)
        self._queue.store_receipt(stored.receipt_id, receipt)
        return receipt, False

    def _recompute(self, source_sha256: str) -> None:
        """Recompute the whole source group from the stored rows (pure)."""
        rows = self._queue.group(source_sha256)
        pairs = [(_row_submission(row), row.actor) for row in rows]
        verdicts = recompute_group(pairs)
        self._queue.update_verdicts(verdicts)

    # ------------------------------------------------------------- review

    def review(self, *, status: str | None = None, receipt_id: str | None = None) -> dict[str, Any]:
        """The read-only review listing or one submission's proposed diff."""
        if receipt_id is not None:
            row = self._queue.get(receipt_id)
            if row is None:
                raise ExtractionNotFoundError(f"no extraction carries receipt id {receipt_id!r}")
            return self._review_detail(row)
        rows = self._queue.list(status=status)
        return {
            "count": len(rows),
            "queue_size": self._queue.count(),
            "rows": [self._review_row(row) for row in rows],
        }

    @staticmethod
    def _review_row(row: QueueRow) -> dict[str, Any]:
        return {
            "receipt_id": row.receipt_id,
            "status": row.status,
            "actor": row.actor,
            "source_sha256": row.source_sha256,
            "source_path": row.source_path,
            "nodes": len(row.payload["nodes"]),
            "edges": len(row.payload["edges"]),
            "confidence_reported": row.confidence_reported,
            "confidence_effective": row.confidence_effective,
            "corroboration": {
                "corroborators": row.corroborators,
                "contradictions": row.contradictions,
                "overlap_ratio": row.overlap_ratio,
            },
            "agent": row.method.get("agent", ""),
            "decided_by": row.decided_by,
            "decision_note": row.decision_note,
        }

    def _review_detail(self, row: QueueRow) -> dict[str, Any]:
        peers = []
        for other in self._queue.group(row.source_sha256):
            if other.receipt_id == row.receipt_id:
                continue
            result = compare_payloads(_row_submission(row).payload, _row_submission(other).payload)
            relation = "contradicts" if result.contradictions else ("corroborates" if result.shared else "independent")
            peers.append(
                {
                    "receipt_id": other.receipt_id,
                    "actor": other.actor,
                    "relation": relation,
                    "shared": result.shared,
                    "agreements": result.agreements,
                    "contradictions": result.contradictions,
                    "overlap_ratio": format(result.overlap_ratio, "f"),
                }
            )
        peers.sort(key=lambda peer: peer["receipt_id"])
        return {
            **self._review_row(row),
            "diff": {
                "text_excerpt": str(row.payload["text"])[:_TEXT_EXCERPT_CHARS],
                "nodes": list(row.payload["nodes"]),
                "edges": list(row.payload["edges"]),
            },
            "method": dict(row.method),
            "peers": peers,
        }

    # ------------------------------------------------------------ decisions

    def approve(
        self,
        receipt_id: str,
        *,
        operator: str,
        service: KnowledgeService,
        note: str = "",
    ) -> dict[str, Any]:
        """Apply one queued extraction to the canonical graph (operator-gated)."""
        started = time.monotonic()
        invocation = Invocation.create(
            _APPROVE_OPERATION,
            request_hash=_request_hash({"receipt_id": receipt_id, "note": note}),
            actor=operator,
        )
        self._audit.begin(invocation)
        try:
            row = self._decidable_row(receipt_id)
            submission = _row_submission(row)
            applied_nodes, applied_edges = self._apply(row, submission, operator=operator, service=service)
            self._queue.mark_decided(receipt_id, "applied", decided_by=operator, note=note or None)
        except Exception as error:
            audit_receipt = self._audit.complete(invocation.invocation_id, failure_outcome(invocation, started, error))
            if isinstance(error, ExtractionError):
                error.audit_event_id = audit_receipt.audit_event_id
            raise
        audit_receipt = self._audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references={
                    "receipt_id": receipt_id,
                    "applied_nodes": list(applied_nodes),
                    "applied_edges": applied_edges,
                },
                projection_outcome="skipped",
            ),
        )
        return {
            "receipt_id": receipt_id,
            "status": "applied",
            "applied_nodes": list(applied_nodes),
            "applied_edges": applied_edges,
            "audit_event_id": audit_receipt.audit_event_id,
        }

    def _decidable_row(self, receipt_id: str) -> QueueRow:
        row = self._queue.get(receipt_id)
        if row is None:
            raise ExtractionNotFoundError(f"no extraction carries receipt id {receipt_id!r}")
        if row.status in {"applied", "rejected"}:
            raise ExtractionStateError(f"extraction {receipt_id!r} is already {row.status}")
        return row

    def _apply(
        self, row: QueueRow, submission: Submission, *, operator: str, service: KnowledgeService
    ) -> tuple[list[str], int]:
        """Write nodes then edges through the existing audited write path.

        Nodes ride ``service.log_event`` (audited ``knowledge.log_event``,
        publish-capacity guard included); edges ride audited
        ``relation.add`` commits. Every write carries a deterministic
        idempotency key and the submission's ``extracted_at`` timestamps, so
        a retried approve replays instead of double-applying.
        """
        confidence = Decimal(row.confidence_effective)
        nodes = sorted(submission.payload.nodes, key=lambda node: node.key)
        domains = {node.key: node.domain for node in nodes}
        applied_ids: list[str] = []
        for node in nodes:
            stamped = stamp_node(
                submission,
                node,
                receipt_id=row.receipt_id,
                confidence_effective=confidence,
                corroborators=row.corroborators,
            )
            service.log_event(
                stamped,
                actor=operator,
                idempotency_key=f"extraction:{row.receipt_id}:node:{node.key[0]}:{node.key[1]}",
            )
            applied_ids.append(stamped.node_id)
        edges = sorted(submission.payload.edges, key=lambda edge: (edge.relation, edge.source, edge.target))
        for index, edge in enumerate(edges):
            source_id = applied_node_id(submission.source_sha256, *edge.source)
            target_id = applied_node_id(submission.source_sha256, *edge.target)
            event = Event(
                kind=EventKind.RELATION_ADD,
                node_id=source_id,
                domain=PrimaryDomain(domains[edge.source]),
                relation=Relation(kind=RelationKind(edge.relation), source_node_id=source_id, target_node_id=target_id),
                source_version_id=f"extraction:{row.receipt_id}",
                actor=operator,
                occurred_at=submission.method.extracted_at,
            )
            edge_invocation = Invocation.create(
                _EDGE_OPERATION,
                request_hash=_request_hash(event.model_dump(mode="json")),
                actor=operator,
                idempotency_key=f"extraction:{row.receipt_id}:edge:{index}",
            )
            self._store.commit(event, invocation=edge_invocation)
        return applied_ids, len(edges)

    def reject(self, receipt_id: str, *, operator: str, note: str = "") -> dict[str, Any]:
        """Mark one queued extraction rejected (operator-gated; no graph write)."""
        started = time.monotonic()
        invocation = Invocation.create(
            _REJECT_OPERATION,
            request_hash=_request_hash({"receipt_id": receipt_id, "note": note}),
            actor=operator,
        )
        self._audit.begin(invocation)
        try:
            self._decidable_row(receipt_id)
            self._queue.mark_decided(receipt_id, "rejected", decided_by=operator, note=note or None)
        except Exception as error:
            audit_receipt = self._audit.complete(invocation.invocation_id, failure_outcome(invocation, started, error))
            if isinstance(error, ExtractionError):
                error.audit_event_id = audit_receipt.audit_event_id
            raise
        audit_receipt = self._audit.complete(
            invocation.invocation_id,
            success_outcome(
                invocation,
                started,
                result_references={"receipt_id": receipt_id},
                projection_outcome="skipped",
            ),
        )
        return {
            "receipt_id": receipt_id,
            "status": "rejected",
            "audit_event_id": audit_receipt.audit_event_id,
        }


def _row_submission(row: QueueRow) -> Submission:
    """Rebuild the validated submission from its stored canonical form."""
    return submission_from_canonical(
        {
            "payload": row.payload,
            "method": row.method,
            "confidence": row.confidence_reported,
            "source_sha256": row.source_sha256,
            "source_path": row.source_path,
        }
    )

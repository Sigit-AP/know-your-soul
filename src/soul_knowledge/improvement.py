"""Structured improvement proposals with approval, regression gate and rollback.

Task 6 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec section 10.

Flow: ``propose`` -> validate -> approval (high impact) -> regression gate ->
``apply_improvement`` -> measure -> ``retain_improvement`` or
``rollback_improvement``. A proposal is an explicit structured diff pinned to an
expected node version; apply re-checks that version through the store's
optimistic concurrency, so a stale proposal fails closed. Every apply is a new
immutable revision — history is never rewritten — and rollback restores the
exact pre-apply content (byte-identical canonical form) as a further revision
carrying the ``rollback`` lifecycle status.

Scores are never writable through a proposal diff: only evidence
(``log_event``/``record_outcome``/``evaluate``) may move P/M/E. Automatic
semantic rewriting is out of scope by design.

Every entry point runs the full journal + final-audit flow: refused proposals,
approval failures, gate failures and version conflicts still receive exactly
one final audit event.
"""

from __future__ import annotations

import hashlib
import json
import time
from collections.abc import Callable, Sequence
from datetime import UTC, datetime
from enum import StrEnum
from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field

from soul_knowledge.audit import AuditLog, failure_outcome, success_outcome
from soul_knowledge.domain import (
    Event,
    EventKind,
    LifecycleStatus,
    Node,
    Relation,
    RelationKind,
    canonical_json,
    new_id,
    storage_payload,
)
from soul_knowledge.events import DomainReceipt, Invocation
from soul_knowledge.store import EventStore, StoreError

__all__ = [
    "ApprovalRecord",
    "ApprovalRequired",
    "DiffOp",
    "DiffOperation",
    "ImprovementProposal",
    "ImprovementReceipt",
    "MergeReceipt",
    "ProposalRecord",
    "ProposalValidationError",
    "RegressionGate",
    "RegressionGateFailure",
    "UnknownImprovement",
    "UnknownNode",
    "apply_improvement",
    "classify_impact",
    "content_fingerprint",
    "default_regression_gate",
    "get_proposal",
    "merge_nodes",
    "propose",
    "retain_improvement",
    "rollback_improvement",
]


class UnknownNode(StoreError):
    """A proposal or mutation referenced a node that does not exist."""


class ProposalValidationError(StoreError):
    """A proposal diff is malformed, stale, or targets protected fields."""


class ApprovalRequired(StoreError):
    """A high-impact change has no matching operator approval record."""


class RegressionGateFailure(StoreError):
    """The regression gate rejected the proposal before any write."""


class UnknownImprovement(StoreError):
    """A rollback was requested but no applied improvement is on record."""


# Scores, provenance and identity are never writable through a proposal diff:
# only log_event/record_outcome/evaluate move P/M/E (frozen relearn contract).
PROTECTED_PAYLOAD_KEYS = frozenset(
    {"scores", "professional_level", "node_id", "version", "source_version_id", "domain", "kind"}
)

_HIGH_IMPACT_STATUSES = frozenset(
    {LifecycleStatus.DEPRECATE.value, LifecycleStatus.SUPERSEDED.value, LifecycleStatus.ROLLBACK.value}
)

_CONTENT_FIELDS = (
    "parent_id",
    "kind",
    "domain",
    "payload",
    "summary",
    "provenance",
    "context_tags",
    "scope",
    "created_at",
    "evidence_ids",
    "plus",
    "minus",
    "scores",
    "relations",
    "source_version_id",
)


class DiffOp(StrEnum):
    """Closed allowlist of diff operations a proposal may contain."""

    SET_SUMMARY = "set_summary"
    SET_SCOPE = "set_scope"
    SET_PAYLOAD_KEY = "set_payload_key"
    REMOVE_PAYLOAD_KEY = "remove_payload_key"
    ADD_PLUS = "add_plus"
    REMOVE_PLUS = "remove_plus"
    ADD_MINUS = "add_minus"
    REMOVE_MINUS = "remove_minus"
    ADD_CONTEXT_TAG = "add_context_tag"
    REMOVE_CONTEXT_TAG = "remove_context_tag"
    ADD_RELATION = "add_relation"
    SET_STATUS = "set_status"


class DiffOperation(BaseModel):
    """One typed operation inside a structured diff."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    op: DiffOp
    key: str = ""
    value: Any = None


class ImprovementProposal(BaseModel):
    """An explicit structured diff pinned to an expected node version."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    base_version: int = Field(ge=1)
    diff: tuple[DiffOperation, ...] = Field(min_length=1)
    rationale: str = Field(min_length=1)
    impact: Literal["low", "high"]
    actor: str = "operator"
    created_at: datetime


class ApprovalRecord(BaseModel):
    """The operator decision a high-impact apply must present."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    approval_id: str = Field(default_factory=new_id)
    proposal_id: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    decision: Literal["approved", "rejected"]
    decided_at: datetime
    note: str = ""


class ProposalRecord(BaseModel):
    """The persisted proposal plus its audit identity and domain result (spec 7.9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal: ImprovementProposal
    audit_event_id: str
    audit_event_seq: int
    domain_event_id: str | None = None
    node_version: int | None = None
    # A proposal is a zero-knowledge-weight admin event: knowledge_seq stays None.
    knowledge_seq: int | None = None


class ImprovementReceipt(BaseModel):
    """The audited outcome of an apply/retain/rollback (spec 7.9 field set)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    proposal_id: str
    node_id: str
    from_version: int
    to_version: int
    status: Literal["applied", "retained", "rolled_back"]
    content_sha256: str
    audit_event_id: str
    audit_event_seq: int
    idempotent_replay: bool = False
    domain_event_id: str | None = None
    node_version: int | None = None
    knowledge_seq: int | None = None


class MergeReceipt(BaseModel):
    """The audited outcome of merging one node into another."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    merge_id: str
    source_node_id: str
    target_node_id: str
    status: Literal["merged"] = "merged"
    valid_from: datetime
    audit_event_id: str
    audit_event_seq: int
    domain_event_id: str | None = None


RegressionGate = Callable[[Node, Node], Sequence[str]]


def _utcnow() -> datetime:
    return datetime.now(UTC)


def classify_impact(diff: Sequence[DiffOperation]) -> Literal["low", "high"]:
    """Relations, removals and terminal status changes are high impact."""
    for operation in diff:
        if operation.op in (DiffOp.ADD_RELATION, DiffOp.REMOVE_PAYLOAD_KEY):
            return "high"
        if operation.op is DiffOp.SET_STATUS and str(operation.value) in _HIGH_IMPACT_STATUSES:
            return "high"
    return "low"


def content_fingerprint(node: Node) -> str:
    """SHA-256 over the canonical content fields (excludes version/updated/status).

    Two nodes with equal content — for example a revision and its rollback —
    have equal fingerprints even though their versions and lifecycle statuses
    differ.
    """
    dump = node.model_dump(mode="python")
    content = {field: dump[field] for field in _CONTENT_FIELDS}
    return hashlib.sha256(canonical_json(storage_payload(content)).encode("utf-8")).hexdigest()


def default_regression_gate(before: Node, after: Node) -> tuple[str, ...]:
    """Refuse identity drift: domain, kind and source version never change."""
    failures: list[str] = []
    if after.domain != before.domain:
        failures.append("domain-changed")
    if after.kind != before.kind:
        failures.append("kind-changed")
    if after.source_version_id != before.source_version_id:
        failures.append("source-version-changed")
    return tuple(failures)


def validate_proposal(store: EventStore, proposal: ImprovementProposal, *, check_version: bool = True) -> None:
    """Fail closed on unknown nodes, stale versions and protected writes.

    ``check_version`` is used at proposal time; at apply time the store's
    ``expected_version`` optimistic-concurrency check owns staleness and
    raises :class:`~soul_knowledge.store.VersionConflict`.
    """
    node = store.current_node(proposal.node_id)
    if node is None:
        raise UnknownNode(f"proposal targets unknown node {proposal.node_id!r}")
    if check_version and proposal.base_version != node.version:
        raise ProposalValidationError(
            f"proposal base_version {proposal.base_version} is stale; node {proposal.node_id!r} is at {node.version}"
        )
    for operation in proposal.diff:
        if operation.op in (DiffOp.SET_PAYLOAD_KEY, DiffOp.REMOVE_PAYLOAD_KEY):
            if not operation.key:
                raise ProposalValidationError(f"{operation.op.value} requires a payload key")
            if operation.key in PROTECTED_PAYLOAD_KEYS:
                raise ProposalValidationError(f"proposals may never write protected field {operation.key!r}")
        if operation.op is DiffOp.SET_STATUS:
            try:
                LifecycleStatus(str(operation.value))
            except ValueError as error:
                raise ProposalValidationError(f"unknown lifecycle status {operation.value!r}") from error
        if operation.op is DiffOp.ADD_RELATION:
            value = operation.value if isinstance(operation.value, dict) else {}
            try:
                RelationKind(str(value.get("kind")))
            except ValueError as error:
                raise ProposalValidationError(f"unknown relation kind {value.get('kind')!r}") from error
            if not value.get("target_node_id"):
                raise ProposalValidationError("add_relation requires target_node_id")


def _proposal_id(node_id: str, base_version: int, diff: Sequence[DiffOperation], rationale: str) -> str:
    """Deterministic proposal identity, so an idempotent retry reuses it."""
    payload = {
        "node_id": node_id,
        "base_version": base_version,
        "diff": [operation.model_dump(mode="json") for operation in diff],
        "rationale": rationale,
    }
    return "prop_" + hashlib.sha256(canonical_json(storage_payload(payload)).encode("utf-8")).hexdigest()[:32]


def propose(
    store: EventStore,
    *,
    node_id: str,
    diff: Sequence[DiffOperation],
    rationale: str,
    base_version: int | None = None,
    actor: str = "operator",
    at: datetime | None = None,
    idempotency_key: str | None = None,
) -> ProposalRecord:
    """Validate and persist a structured diff proposal (zero knowledge weight)."""
    at = at or _utcnow()
    node = store.current_node(node_id)
    if node is None:
        raise UnknownNode(f"cannot propose for unknown node {node_id!r}")
    base = base_version if base_version is not None else node.version
    proposal = ImprovementProposal(
        proposal_id=_proposal_id(node_id, base, diff, rationale),
        node_id=node_id,
        base_version=base,
        diff=tuple(diff),
        rationale=rationale,
        impact=classify_impact(diff),
        actor=actor,
        created_at=at,
    )
    validate_proposal(store, proposal)
    event = Event(
        kind=EventKind.ADMIN,
        node_id=node_id,
        domain=node.domain,
        occurred_at=at,
        knowledge_affecting=False,
        payload={"improvement_proposal": proposal.model_dump(mode="json")},
    )
    request_hash = hashlib.sha256(canonical_json({"propose": proposal.proposal_id}).encode()).hexdigest()
    receipt = store.commit(
        event,
        invocation=Invocation.create(
            "knowledge.propose_improvement", request_hash, actor=actor, idempotency_key=idempotency_key
        ),
    )
    return ProposalRecord(
        proposal=proposal,
        audit_event_id=receipt.audit_event_id,
        audit_event_seq=receipt.audit_event_seq,
        domain_event_id=receipt.domain_event_id,
        node_version=receipt.node_version,
        knowledge_seq=receipt.knowledge_seq,
    )


def get_proposal(store: EventStore, proposal_id: str) -> ImprovementProposal | None:
    """Read back a persisted proposal from the immutable event log."""
    with store.read() as conn:
        rows = conn.execute("SELECT payload_json FROM event_log WHERE kind = 'domain' ORDER BY event_seq").fetchall()
    for row in rows:
        data = (json.loads(str(row["payload_json"])).get("payload") or {}).get("improvement_proposal")
        if isinstance(data, dict) and data.get("proposal_id") == proposal_id:
            return ImprovementProposal.model_validate(data)
    return None


def _apply_diff(node: Node, diff: Sequence[DiffOperation], at: datetime) -> Node:
    summary, scope = node.summary, node.scope
    payload = dict(node.payload)
    plus, minus = list(node.plus), list(node.minus)
    tags = list(node.context_tags)
    relations = list(node.relations)
    status = node.lifecycle_status
    for operation in diff:
        if operation.op is DiffOp.SET_SUMMARY:
            summary = str(operation.value)
        elif operation.op is DiffOp.SET_SCOPE:
            scope = str(operation.value)
        elif operation.op is DiffOp.SET_PAYLOAD_KEY:
            payload[operation.key] = operation.value
        elif operation.op is DiffOp.REMOVE_PAYLOAD_KEY:
            payload.pop(operation.key, None)
        elif operation.op is DiffOp.ADD_PLUS:
            if str(operation.value) not in plus:
                plus.append(str(operation.value))
        elif operation.op is DiffOp.REMOVE_PLUS:
            plus = [item for item in plus if item != operation.value]
        elif operation.op is DiffOp.ADD_MINUS:
            if str(operation.value) not in minus:
                minus.append(str(operation.value))
        elif operation.op is DiffOp.REMOVE_MINUS:
            minus = [item for item in minus if item != operation.value]
        elif operation.op is DiffOp.ADD_CONTEXT_TAG:
            if str(operation.value) not in tags:
                tags.append(str(operation.value))
        elif operation.op is DiffOp.REMOVE_CONTEXT_TAG:
            tags = [item for item in tags if item != operation.value]
        elif operation.op is DiffOp.SET_STATUS:
            status = LifecycleStatus(str(operation.value))
        elif operation.op is DiffOp.ADD_RELATION:
            value = operation.value
            relations.append(
                Relation(
                    kind=RelationKind(str(value["kind"])),
                    source_node_id=node.node_id,
                    target_node_id=str(value["target_node_id"]),
                    valid_from=at,
                )
            )
    return node.model_copy(
        update={
            "summary": summary,
            "scope": scope,
            "payload": payload,
            "plus": tuple(plus),
            "minus": tuple(minus),
            "context_tags": tuple(tags),
            "relations": tuple(relations),
            "lifecycle_status": status,
            "updated_at": at,
        }
    )


class _Audited:
    """The commit flow with pre-write policy checks inside the audit boundary."""

    def __init__(self, store: EventStore, operation: str, request_hash: str, actor: str, idempotency_key: str | None):
        self._store = store
        self._audit = AuditLog(store)
        self.invocation = Invocation.create(operation, request_hash, actor=actor, idempotency_key=idempotency_key)
        self._started = time.monotonic()
        self._audit.begin(self.invocation)

    def run[T](
        self,
        write: Callable[[], T],
        *,
        linked_domain_event_id: Callable[[T], str | None] | None = None,
    ) -> tuple[T, str, int]:
        try:
            result = write()
        except Exception as error:
            self._audit.complete(
                self.invocation.invocation_id,
                failure_outcome(self.invocation, self._started, error),
            )
            raise
        link = linked_domain_event_id(result) if linked_domain_event_id is not None else None
        final = self._audit.complete(
            self.invocation.invocation_id,
            success_outcome(
                self.invocation,
                self._started,
                result_references={"operation": self.invocation.operation},
                linked_domain_event_id=link,
            ),
        )
        return result, final.audit_event_id, final.event_seq


def apply_improvement(
    store: EventStore,
    proposal: ImprovementProposal,
    *,
    approval: ApprovalRecord | None = None,
    gate: RegressionGate | None = None,
    at: datetime | None = None,
    actor: str = "operator",
    idempotency_key: str | None = None,
) -> ImprovementReceipt:
    """Apply a proposal: approval and regression gate run before any write."""
    at = at or _utcnow()
    audited = _Audited(
        store,
        "knowledge.apply_improvement",
        hashlib.sha256(canonical_json({"apply": proposal.proposal_id}).encode()).hexdigest(),
        actor,
        idempotency_key,
    )

    def write() -> tuple[Node, int, DomainReceipt]:
        validate_proposal(store, proposal, check_version=False)
        node = store.current_node(proposal.node_id)
        assert node is not None  # validate_proposal guarantees it
        if proposal.impact == "high" and (
            approval is None or approval.proposal_id != proposal.proposal_id or approval.decision != "approved"
        ):
            raise ApprovalRequired(
                f"high-impact proposal {proposal.proposal_id!r} requires an approved operator record"
            )
        after = _apply_diff(node, proposal.diff, at)
        failures = tuple((gate or default_regression_gate)(node, after))
        if failures:
            raise RegressionGateFailure(f"proposal {proposal.proposal_id!r} failed the regression gate: {failures}")
        event = Event(
            kind=EventKind.NODE_UPSERT,
            node_id=proposal.node_id,
            domain=node.domain,
            node=after,
            source_version_id=node.source_version_id,
            occurred_at=at,
            payload={
                "improvement_apply": {
                    "proposal_id": proposal.proposal_id,
                    "from_version": node.version,
                    "impact": proposal.impact,
                    "approval_id": approval.approval_id if approval else None,
                }
            },
        )
        domain = store.append_domain(
            event,
            idempotency_key=idempotency_key or audited.invocation.invocation_id,
            expected_version=proposal.base_version,
        )
        for relation in after.relations[len(node.relations) :]:
            store.append_domain(
                Event(
                    kind=EventKind.RELATION_ADD,
                    node_id=proposal.node_id,
                    domain=node.domain,
                    relation=relation,
                    occurred_at=at,
                    payload={"improvement_apply": {"proposal_id": proposal.proposal_id}},
                ),
                idempotency_key=(
                    f"{idempotency_key or audited.invocation.invocation_id}:relation:"
                    f"{relation.target_node_id}:{relation.kind.value}"
                ),
            )
        return after, node.version, domain

    (after, from_version, domain), audit_event_id, audit_event_seq = audited.run(
        write, linked_domain_event_id=lambda result: result[2].domain_event_id
    )
    return ImprovementReceipt(
        proposal_id=proposal.proposal_id,
        node_id=proposal.node_id,
        from_version=from_version,
        to_version=store.node_version(proposal.node_id),
        status="applied",
        content_sha256=content_fingerprint(after),
        audit_event_id=audit_event_id,
        audit_event_seq=audit_event_seq,
        idempotent_replay=domain.idempotent_replay,
        domain_event_id=domain.domain_event_id,
        node_version=domain.node_version,
        knowledge_seq=domain.knowledge_seq,
    )


def retain_improvement(
    store: EventStore,
    proposal: ImprovementProposal,
    *,
    actor: str = "operator",
    at: datetime | None = None,
    idempotency_key: str | None = None,
) -> ImprovementReceipt:
    """Mark an applied improvement retained after measurement (zero weight)."""
    at = at or _utcnow()
    node = store.current_node(proposal.node_id)
    if node is None:
        raise UnknownNode(f"cannot retain improvement for unknown node {proposal.node_id!r}")
    event = Event(
        kind=EventKind.ADMIN,
        node_id=proposal.node_id,
        domain=node.domain,
        occurred_at=at,
        knowledge_affecting=False,
        payload={"improvement_retain": {"proposal_id": proposal.proposal_id}},
    )
    receipt = store.commit(
        event,
        invocation=Invocation.create(
            "knowledge.retain_improvement",
            hashlib.sha256(canonical_json({"retain": proposal.proposal_id}).encode()).hexdigest(),
            actor=actor,
            idempotency_key=idempotency_key,
        ),
    )
    return ImprovementReceipt(
        proposal_id=proposal.proposal_id,
        node_id=proposal.node_id,
        from_version=node.version,
        to_version=node.version,
        status="retained",
        content_sha256=content_fingerprint(node),
        audit_event_id=receipt.audit_event_id,
        audit_event_seq=receipt.audit_event_seq,
        idempotent_replay=receipt.idempotent_replay,
        domain_event_id=receipt.domain_event_id,
        node_version=receipt.node_version,
        knowledge_seq=receipt.knowledge_seq,
    )


def merge_nodes(
    store: EventStore,
    *,
    source_node_id: str,
    target_node_id: str,
    approval: ApprovalRecord | None,
    at: datetime | None = None,
    actor: str = "operator",
    idempotency_key: str | None = None,
) -> MergeReceipt:
    """Merge *source* into *target*: supersession with retention, never deletion."""
    at = at or _utcnow()
    merge_id = f"merge:{source_node_id}:{target_node_id}"
    if approval is None or approval.proposal_id != merge_id or approval.decision != "approved":
        raise ApprovalRequired(f"merge {merge_id!r} is always high impact and requires approval")
    source = store.current_node(source_node_id)
    target = store.current_node(target_node_id)
    if source is None:
        raise UnknownNode(f"merge source {source_node_id!r} does not exist")
    if target is None:
        raise UnknownNode(f"merge target {target_node_id!r} does not exist")
    audited = _Audited(
        store,
        "knowledge.merge",
        hashlib.sha256(canonical_json({"merge": merge_id}).encode()).hexdigest(),
        actor,
        idempotency_key,
    )
    key = idempotency_key or audited.invocation.invocation_id

    def write() -> DomainReceipt:
        store.append_domain(
            Event(
                kind=EventKind.RELATION_ADD,
                node_id=source_node_id,
                domain=source.domain,
                relation=Relation(
                    kind=RelationKind.SUPERSEDES,
                    source_node_id=source_node_id,
                    target_node_id=target_node_id,
                    valid_from=at,
                ),
                occurred_at=at,
                payload={"improvement_merge": {"merge_id": merge_id}},
            ),
            idempotency_key=f"{key}:relation",
        )
        return store.append_domain(
            Event(
                kind=EventKind.NODE_RETIRE,
                node_id=source_node_id,
                domain=source.domain,
                node=source,
                occurred_at=at,
                payload={"improvement_merge": {"merge_id": merge_id}},
            ),
            idempotency_key=f"{key}:retire",
        )

    domain, audit_event_id, audit_event_seq = audited.run(write, linked_domain_event_id=lambda d: d.domain_event_id)
    return MergeReceipt(
        merge_id=merge_id,
        source_node_id=source_node_id,
        target_node_id=target_node_id,
        valid_from=at,
        audit_event_id=audit_event_id,
        audit_event_seq=audit_event_seq,
        domain_event_id=domain.domain_event_id,
    )


def _revision_at(store: EventStore, node_id: str, version: int) -> Node | None:
    with store.read() as conn:
        row = conn.execute(
            "SELECT payload_json FROM node_revision WHERE node_id = ? AND version = ?", (node_id, version)
        ).fetchone()
    return None if row is None else Node.model_validate(json.loads(str(row["payload_json"])))


def _last_apply_from_version(store: EventStore, node_id: str) -> int | None:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT payload_json FROM event_log WHERE kind = 'domain' AND node_id = ? ORDER BY event_seq DESC",
            (node_id,),
        ).fetchall()
    for row in rows:
        data = (json.loads(str(row["payload_json"])).get("payload") or {}).get("improvement_apply")
        if isinstance(data, dict) and data.get("from_version") is not None:
            return int(data["from_version"])
    return None


def rollback_improvement(
    store: EventStore,
    *,
    node_id: str,
    to_version: int | None = None,
    actor: str = "operator",
    at: datetime | None = None,
    idempotency_key: str | None = None,
) -> ImprovementReceipt:
    """Restore the exact pre-apply content as a new revision marked ``rollback``.

    ``to_version`` defaults to the version recorded by the most recent applied
    improvement for the node. Content fields (payload, summary, plus/minus,
    tags, scope, provenance, relations, scores) are restored byte-identically;
    only the monotonic version, ``updated_at`` and the ``rollback`` lifecycle
    status differ.
    """
    at = at or _utcnow()
    node = store.current_node(node_id)
    if node is None:
        raise UnknownNode(f"cannot roll back unknown node {node_id!r}")
    if to_version is None:
        to_version = _last_apply_from_version(store, node_id)
        if to_version is None:
            raise UnknownImprovement(f"node {node_id!r} has no applied improvement to roll back")
    restored_from = _revision_at(store, node_id, to_version)
    if restored_from is None:
        raise UnknownImprovement(f"node {node_id!r} has no revision {to_version}")
    restored = restored_from.model_copy(update={"updated_at": at, "lifecycle_status": LifecycleStatus.ROLLBACK})
    event = Event(
        kind=EventKind.NODE_UPSERT,
        node_id=node_id,
        domain=node.domain,
        node=restored,
        source_version_id=node.source_version_id,
        occurred_at=at,
        payload={
            "improvement_rollback": {
                "node_id": node_id,
                "to_version": to_version,
                "from_version": node.version,
            }
        },
    )
    receipt = store.commit(
        event,
        invocation=Invocation.create(
            "knowledge.rollback",
            hashlib.sha256(canonical_json({"rollback": node_id, "to_version": to_version}).encode()).hexdigest(),
            actor=actor,
            idempotency_key=idempotency_key,
        ),
    )
    return ImprovementReceipt(
        proposal_id=f"rollback:{node_id}:{to_version}",
        node_id=node_id,
        from_version=node.version,
        to_version=receipt.node_version,
        status="rolled_back",
        content_sha256=content_fingerprint(restored),
        audit_event_id=receipt.audit_event_id,
        audit_event_seq=receipt.audit_event_seq,
        idempotent_replay=receipt.idempotent_replay,
        domain_event_id=receipt.domain_event_id,
        node_version=receipt.node_version,
        knowledge_seq=receipt.knowledge_seq,
    )

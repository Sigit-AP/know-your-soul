"""Persistent local JSON API and shared operation dispatch (Task 7).

Task 7 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7 (invocation/audit flow), 8 and 13.1.

This module is a *thin adapter*: it validates transport payloads, forwards
them to the fourteen-operation :class:`~know_your_soul.service.KnowledgeService`
(the frozen ten v1 operations plus the contract-v2 G1 retrieval verbs
``path`` / ``explain`` / ``affected`` / ``god_nodes`` — operator ruling
2026-10-26, addendum ``docs/contracts/retrieval-verbs-v2.md``)
(or the operational commands: projection rebuild, backup, restore, health and
the operator-gated relearn lifecycle ``relearn_scan`` / ``relearn_status`` /
``relearn_preview`` / ``relearn_apply`` — final-audit F-4; the knowledge
operations are unchanged by the relearn surface) and serializes the result.
No business logic lives here.

Audit discipline (spec 7.7):

* The service journals every invocation it receives — successes and typed
  failures — so the adapter never journals a second time on the service path.
* Input rejected *by the adapter* (malformed JSON, unknown fields, wrong
  shapes) never reaches the service, so the adapter itself journals exactly
  one completion audit record (``status=failure``) before rejecting. Every
  valid invocation therefore has exactly one final audit event.

Boundaries: the Starlette app enforces the Host allowlist and the
loopback/Unix-socket policy from :mod:`know_your_soul.security`, caps request
bodies (streaming, under one total body deadline), requires ``application/json`` on
POST routes (CSRF drive-by rejection), and exposes only versioned ``/v1``
routes. ``serve`` binds a Unix socket by default — created owner-only
(``0o600``) via :func:`_bind_unix_socket`; loopback HTTP is opt-in and wider
binds require an API key. When an API key is configured it gates *every*
client, loopback and unix-socket peers included (fail-closed; the CLI never
talks to the socket, it dispatches in-process, so local CLI flows are
unaffected). ``ops/backup`` and ``ops/restore`` paths are confined to the
stack's ``ops_root`` (default: the store's directory) via
:func:`know_your_soul.security.resolve_within_root`.
"""

from __future__ import annotations

import asyncio
import errno
import hashlib
import hmac
import json
import logging
import os
import platform
import socket
import stat
import threading
import time
from collections.abc import AsyncIterator, Callable, Mapping, Sequence
from contextlib import asynccontextmanager
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal, NoReturn

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from know_your_soul.audit import AuditLog
from know_your_soul.backup import BackupError, backup_store, restore_store
from know_your_soul.contracts import (
    DEFAULT_MAX_TOKEN_BUDGET,
    MINIMUM_TOKEN_BUDGET,
    ApplyImprovementRequest,
    BudgetExceedsMax,
    BudgetTooSmall,
    EvaluateRequest,
    KnowledgeError,
    MetadataExceedsBudget,
    ProposeImprovementRequest,
    QueryRequest,
    ReviewRequest,
    RollbackRequest,
    TraversalMode,
    estimated_tokens_of_payload,
)
from know_your_soul.domain import Evidence, Node, RelationKind, canonical_json, new_id
from know_your_soul.events import Invocation, InvocationOutcome
from know_your_soul.ingest.pipeline import (
    ExtractorUnavailableError,
    IngestionBudget,
    IngestionError,
    IngestionPipeline,
    PathNotFoundError,
    PathNotReadableError,
    PathOutsideRootError,
)
from know_your_soul.observability import HealthReport, check_readiness
from know_your_soul.projection import GraphProjector, ProjectionError
from know_your_soul.relearn import RelearnApproval, RelearnOffer
from know_your_soul.security import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    BindPolicyError,
    HostNotAllowedError,
    PathEscapeError,
    RequestView,
    UnauthorizedError,
    assert_bind_allowed,
    authorize_request,
    redact_text,
    resolve_within_root,
    sanitize_text,
)
from know_your_soul.service import KnowledgeService
from know_your_soul.source_registry import SourceKind
from know_your_soul.source_watcher import SourceChange, SourceWatcher
from know_your_soul.store import EventStore, StoreError
from know_your_soul.update_pipeline import SourceWatchLoop, UpdatePipelineError

__all__ = [
    "API_VERSION",
    "DEFAULT_HOST_ALLOWLIST",
    "INGEST_VERBS",
    "KNOWLEDGE_OPERATIONS",
    "OPS_OPERATIONS",
    "AdapterValidationError",
    "BoundaryDenialRecorder",
    "BoundaryMiddleware",
    "LocalStack",
    "UnknownOperation",
    "build_stack",
    "create_app",
    "dispatch",
    "dispatch_text",
    "error_envelope",
    "run_ingest_verb",
    "serve",
    "status_for_error",
    "to_jsonable",
]

logger = logging.getLogger(__name__)

API_VERSION = "v1"
DEFAULT_HOST_ALLOWLIST: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
KNOWLEDGE_OPERATIONS: tuple[str, ...] = (
    # The frozen ten v1 operations (spec section 8).
    "query",
    "get_node",
    "get_subtree",
    "log_event",
    "record_outcome",
    "evaluate",
    "review",
    "propose_improvement",
    "apply_improvement",
    "rollback",
    # Contract v2 (operator ruling 2026-10-26, progress.md; addendum
    # docs/contracts/retrieval-verbs-v2.md): the four G1 retrieval verbs.
    "path",
    "explain",
    "affected",
    "god_nodes",
)
OPS_OPERATIONS: tuple[str, ...] = (
    "rebuild",
    "backup",
    "restore",
    "health",
    # Final-audit F-4 (adapter half): the operator-gated relearn lifecycle
    # rides the ops route — the knowledge operations stay untouched by it.
    "relearn_scan",
    "relearn_status",
    "relearn_preview",
    "relearn_apply",
)

_MAX_ERROR_MESSAGE = 512


# --------------------------------------------------------------------- errors


class AdapterValidationError(KnowledgeError):
    """Input rejected at the adapter boundary (already journaled)."""

    code = "VALIDATION_ERROR"


class UnknownOperation(KnowledgeError):
    """The requested operation name is not part of the fixed surface."""

    code = "NOT_FOUND"


class PayloadTooLarge(KnowledgeError):
    """The request body exceeded the configured byte cap."""

    code = "PAYLOAD_TOO_LARGE"


class RequestBodyTimeout(KnowledgeError):
    """The request body was not fully received within the total body deadline."""

    code = "REQUEST_TIMEOUT"


class UnsupportedMediaType(KnowledgeError):
    """A POST route was called with a non-JSON Content-Type (CSRF guard)."""

    code = "UNSUPPORTED_MEDIA_TYPE"


class ClientDisconnected(KnowledgeError):
    """The client vanished before its request body finished arriving.

    Wave-2 F9(b): a dropped connection is routine network noise, so it maps
    to a typed 400 instead of an uncaught ``starlette.requests.ClientDisconnect``
    traceback per disconnect. No invocation is journaled — the request never
    existed as far as the service is concerned (spec 7.7).
    """

    code = "CLIENT_DISCONNECTED"


# ---------------------------------------------------------------------- stack


@dataclass
class LocalStack:
    """The store + projector + service + audit log behind one local API.

    ``ops_root`` is the fixed containment root for ``ops/backup`` and
    ``ops/restore`` paths (wave-2 F1): every caller-supplied path is resolved
    through :func:`know_your_soul.security.resolve_within_root` against it,
    failing closed on escapes. ``None`` refuses backup/restore outright.
    """

    store: EventStore
    projector: GraphProjector
    service: KnowledgeService
    audit: AuditLog
    ops_root: Path | None = None

    def close(self) -> None:
        self.store.close()


def build_stack(store_path: str | Path, output_root: str | Path, *, ops_root: str | Path | None = None) -> LocalStack:
    """Open (or create) a local store and bind the service to it.

    Crash recovery runs before the stack accepts traffic: journals left
    ``in_progress`` by a dead process are reconciled exactly once (spec 7.8).
    *ops_root* confines backup/restore paths; it defaults to the store's
    directory and must be an existing directory (fail closed).
    """
    store = EventStore(store_path)
    projector = GraphProjector(store, output_root=output_root)
    audit = AuditLog(store)
    root_candidate = Path(ops_root) if ops_root is not None else Path(store_path).resolve().parent
    resolved_ops_root = resolve_within_root(root_candidate, ".")

    def _repair_projection() -> None:
        projector.repair_if_stale()

    audit.recover_incomplete(projection_repair=_repair_projection)
    return LocalStack(
        store=store,
        projector=projector,
        service=KnowledgeService(store, projector),
        audit=audit,
        ops_root=resolved_ops_root,
    )


# ------------------------------------------------------------ payload schemas


class _Payload(BaseModel):
    # Strict like the contracts.py request models (arch review F-2): the wire
    # adapters must reject coercions — numeric strings, bools-as-ints — exactly
    # as the MCP adapter's schema layer does. Parity is guarded by
    # tests/test_adapter_validation_parity.py.
    model_config = ConfigDict(frozen=True, extra="forbid", strict=True)


class _EmptyPayload(_Payload):
    pass


class _GetNodePayload(_Payload):
    node_id: str = Field(min_length=1)


class _GetSubtreePayload(_Payload):
    # Bounds and enums mirror QueryRequest and the MCP schema (F-2): without
    # them the adapter accepted depth=0/99 or mode="random" and the traversal
    # raised an untyped ValueError deep in the service.
    node_id: str = Field(min_length=1)
    depth: int = Field(default=2, ge=1, le=6)
    mode: TraversalMode = TraversalMode.BFS
    relation_allowlist: tuple[RelationKind, ...] | None = None
    max_nodes: int = Field(default=64, ge=1, le=1024)
    max_edges: int = Field(default=256, ge=1, le=4096)
    token_budget: int = 1024


# Contract-v2 retrieval verb payloads (addendum retrieval-verbs-v2.md).


class _PathPayload(_Payload):
    from_id: str = Field(min_length=1)
    to_id: str = Field(min_length=1)
    token_budget: int = 1024


class _ExplainPayload(_Payload):
    node_id: str = Field(min_length=1)
    token_budget: int = 1024


class _AffectedPayload(_Payload):
    node_id: str = Field(min_length=1)
    depth: int = Field(default=2, ge=1, le=6)
    max_nodes: int = Field(default=256, ge=1, le=1024)
    token_budget: int = 1024


class _GodNodesPayload(_Payload):
    limit: int = Field(default=10, ge=1, le=64)
    token_budget: int = 1024


class _RebuildPayload(_Payload):
    upto_knowledge_seq: int | None = None


class _BackupPayload(_Payload):
    dest_path: str = Field(min_length=1)


class _RestorePayload(_Payload):
    backup_path: str = Field(min_length=1)
    dest_path: str = Field(min_length=1)
    overwrite: bool = False


class _RelearnScanPayload(_Payload):
    path: str = Field(min_length=1)
    kind: SourceKind = SourceKind.SOUL_CORPUS


class _RelearnPreviewPayload(_Payload):
    mode: Literal["RESET", "ACCUMULATE"]


class _RelearnApprovalPayload(_Payload):
    offer_id: str = Field(min_length=1)
    approved_by: str = Field(min_length=1)
    decided_at: datetime | None = None


class _RelearnApplyPayload(_Payload):
    mode: Literal["RESET", "ACCUMULATE"]
    approval: _RelearnApprovalPayload


# ------------------------------------------------------------------ dispatch


@dataclass(frozen=True)
class _OpSpec:
    parse: Callable[[Any], Any]
    call: Callable[[LocalStack, Any, str, str | None], Any]


def _model_parse(model: type[BaseModel]) -> Callable[[Any], Any]:
    def parse(payload: Any) -> Any:
        # Raw JSON text is validated in JSON mode so strict models accept JSON
        # arrays for tuple fields; already-decoded mappings take python mode.
        if isinstance(payload, (bytes, str)):
            return model.model_validate_json(payload)
        if not isinstance(payload, Mapping):
            raise AdapterValidationError(f"payload must be a JSON object, got {type(payload).__name__}")
        return model.model_validate(payload)

    return parse


def _call_query(stack: LocalStack, request: QueryRequest, actor: str, idem: str | None) -> Any:
    return stack.service.query(request, actor=actor)


def _call_get_node(stack: LocalStack, request: _GetNodePayload, actor: str, idem: str | None) -> Any:
    return stack.service.get_node(request.node_id, actor=actor)


def _call_get_subtree(stack: LocalStack, request: _GetSubtreePayload, actor: str, idem: str | None) -> Any:
    return stack.service.get_subtree(
        request.node_id,
        depth=request.depth,
        mode=request.mode,
        relation_allowlist=frozenset(request.relation_allowlist) if request.relation_allowlist else None,
        max_nodes=request.max_nodes,
        max_edges=request.max_edges,
        token_budget=request.token_budget,
        actor=actor,
    )


def _call_log_event(stack: LocalStack, request: Node, actor: str, idem: str | None) -> Any:
    return stack.service.log_event(request, actor=actor, idempotency_key=idem)


def _call_record_outcome(stack: LocalStack, request: Evidence, actor: str, idem: str | None) -> Any:
    return stack.service.record_outcome(request, actor=actor, idempotency_key=idem)


def _call_evaluate(stack: LocalStack, request: EvaluateRequest, actor: str, idem: str | None) -> Any:
    return stack.service.evaluate(request, actor=actor)


def _call_review(stack: LocalStack, request: ReviewRequest, actor: str, idem: str | None) -> Any:
    return stack.service.review(request, actor=actor, idempotency_key=idem)


def _call_propose(stack: LocalStack, request: ProposeImprovementRequest, actor: str, idem: str | None) -> Any:
    return stack.service.propose_improvement(request, actor=actor, idempotency_key=idem)


def _call_apply(stack: LocalStack, request: ApplyImprovementRequest, actor: str, idem: str | None) -> Any:
    return stack.service.apply_improvement(request, actor=actor, idempotency_key=idem)


def _call_rollback(stack: LocalStack, request: RollbackRequest, actor: str, idem: str | None) -> Any:
    return stack.service.rollback(request, actor=actor, idempotency_key=idem)


def _call_path(stack: LocalStack, request: _PathPayload, actor: str, idem: str | None) -> Any:
    return stack.service.path(request.from_id, request.to_id, token_budget=request.token_budget, actor=actor)


def _call_explain(stack: LocalStack, request: _ExplainPayload, actor: str, idem: str | None) -> Any:
    return stack.service.explain(request.node_id, token_budget=request.token_budget, actor=actor)


def _call_affected(stack: LocalStack, request: _AffectedPayload, actor: str, idem: str | None) -> Any:
    return stack.service.affected(
        request.node_id,
        depth=request.depth,
        max_nodes=request.max_nodes,
        token_budget=request.token_budget,
        actor=actor,
    )


def _call_god_nodes(stack: LocalStack, request: _GodNodesPayload, actor: str, idem: str | None) -> Any:
    return stack.service.god_nodes(limit=request.limit, token_budget=request.token_budget, actor=actor)


def _call_rebuild(stack: LocalStack, request: _RebuildPayload, actor: str, idem: str | None) -> Any:
    return stack.projector.rebuild(upto_knowledge_seq=request.upto_knowledge_seq)


def _reject_hardlinked_ops_file(resolved: Path, candidate: str) -> None:
    """Refuse multi-linked regular files inside the ops root (wave-3 W3-7).

    Path-based containment is structurally blind to hardlinks: a link inside
    the root to an outside inode resolves "inside" while naming the outside
    inode. ``st_nlink > 1`` on a regular file means another name shares the
    inode — possibly outside the root — so the path is refused. Missing
    paths (backup destinations yet to be written) pass; directories are
    exempt (their link count includes subdirectory entries).
    """
    try:
        info = resolved.stat()
    except FileNotFoundError:
        return
    except OSError as error:
        raise PathEscapeError(f"path {candidate!r} cannot be inspected: {error}") from error
    if stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
        raise PathEscapeError(
            f"path {candidate!r} is a multi-linked file (nlink={info.st_nlink}); "
            "containment cannot see the other inode names, so it is refused"
        )


def _resolve_ops_path(stack: LocalStack, candidate: str) -> Path:
    """Confine an operator-supplied backup/restore path to the stack's ops root.

    The surfaced message names only the caller-supplied candidate — never the
    server's absolute root (wave-2 re-review observation): the typed code
    (``PATH_ESCAPE``) is unchanged, the full detail stays in ``__cause__``
    for server-side diagnostics.
    """
    if stack.ops_root is None:
        raise PathEscapeError("no ops root is configured; backup/restore paths are refused")
    try:
        resolved = resolve_within_root(stack.ops_root, candidate)
    except PathEscapeError as error:
        raise PathEscapeError(f"path {candidate!r} is outside the permitted operations root") from error
    _reject_hardlinked_ops_file(resolved, candidate)
    return resolved


def _recheck_ops_path(stack: LocalStack, candidate: str, pre_op: Path) -> None:
    """Re-resolve *candidate* after an operation; require the identical path.

    Closes the resolve-then-use TOCTOU window (wave-3 W3-7) as far as is
    deterministically possible: if a parent directory inside the ops root was
    swapped for a symlink between resolution and use, the post-operation
    resolution either escapes the root (:func:`_resolve_ops_path` raises) or
    names a different path — both fail closed. The surfaced message names
    only the caller-supplied candidate, never a server-side absolute path.
    """
    resolved = _resolve_ops_path(stack, candidate)
    if resolved != pre_op:
        raise PathEscapeError(f"path {candidate!r} moved during the operation; refusing to trust the result")


def _call_backup(stack: LocalStack, request: _BackupPayload, actor: str, idem: str | None) -> Any:
    dest = _resolve_ops_path(stack, request.dest_path)
    receipt = backup_store(stack.store, dest)
    _recheck_ops_path(stack, request.dest_path, dest)
    return receipt


def _call_restore(stack: LocalStack, request: _RestorePayload, actor: str, idem: str | None) -> Any:
    backup_path = _resolve_ops_path(stack, request.backup_path)
    dest = _resolve_ops_path(stack, request.dest_path)
    receipt = restore_store(backup_path, dest, overwrite=request.overwrite)
    _recheck_ops_path(stack, request.backup_path, backup_path)
    _recheck_ops_path(stack, request.dest_path, dest)
    return receipt


def _call_health(stack: LocalStack, request: _EmptyPayload, actor: str, idem: str | None) -> Any:
    return check_readiness(stack.store, projector=stack.projector)


def _call_relearn_scan(stack: LocalStack, request: _RelearnScanPayload, actor: str, idem: str | None) -> Any:
    """Scan a source path; emit (and return) a RelearnOffer iff it changed.

    Final-audit F-4: this is the operator-reachable half of the spec-13.3
    trigger, riding the service's public ``offer_relearn`` (audited). The
    engine never selects a mode — an offer only sets ``RELEARN_PENDING`` and
    waits for ``relearn_apply``. Unlike backup/restore, *path* is a source to
    hash (read-only), so it is not confined to ``ops_root``.
    """
    offer = stack.service.offer_relearn(Path(request.path), request.kind, actor=actor)
    status = stack.service.relearn_status(actor=actor)
    return {"offer": offer, "relearn_state": status.state}


def _call_relearn_status(stack: LocalStack, request: _EmptyPayload, actor: str, idem: str | None) -> Any:
    """The relearn gate: state, the pending offer (with its id), the baseline."""
    return stack.service.relearn_status(actor=actor)


def _call_relearn_preview(stack: LocalStack, request: _RelearnPreviewPayload, actor: str, idem: str | None) -> Any:
    return stack.service.preview_relearn(request.mode, actor=actor)


def _call_relearn_apply(stack: LocalStack, request: _RelearnApplyPayload, actor: str, idem: str | None) -> Any:
    approval = RelearnApproval(
        offer_id=request.approval.offer_id,
        mode=request.mode,
        approved_by=request.approval.approved_by,
        decided_at=request.approval.decided_at or datetime.now(UTC),
    )
    return stack.service.apply_relearn(request.mode, approval, actor=actor, idempotency_key=idem)


_OPERATIONS: dict[str, _OpSpec] = {
    "query": _OpSpec(_model_parse(QueryRequest), _call_query),
    "get_node": _OpSpec(_model_parse(_GetNodePayload), _call_get_node),
    "get_subtree": _OpSpec(_model_parse(_GetSubtreePayload), _call_get_subtree),
    "log_event": _OpSpec(_model_parse(Node), _call_log_event),
    "record_outcome": _OpSpec(_model_parse(Evidence), _call_record_outcome),
    "evaluate": _OpSpec(_model_parse(EvaluateRequest), _call_evaluate),
    "review": _OpSpec(_model_parse(ReviewRequest), _call_review),
    "propose_improvement": _OpSpec(_model_parse(ProposeImprovementRequest), _call_propose),
    "apply_improvement": _OpSpec(_model_parse(ApplyImprovementRequest), _call_apply),
    "rollback": _OpSpec(_model_parse(RollbackRequest), _call_rollback),
    # Contract v2 retrieval verbs (addendum retrieval-verbs-v2.md).
    "path": _OpSpec(_model_parse(_PathPayload), _call_path),
    "explain": _OpSpec(_model_parse(_ExplainPayload), _call_explain),
    "affected": _OpSpec(_model_parse(_AffectedPayload), _call_affected),
    "god_nodes": _OpSpec(_model_parse(_GodNodesPayload), _call_god_nodes),
    "rebuild": _OpSpec(_model_parse(_RebuildPayload), _call_rebuild),
    "backup": _OpSpec(_model_parse(_BackupPayload), _call_backup),
    "restore": _OpSpec(_model_parse(_RestorePayload), _call_restore),
    "health": _OpSpec(_model_parse(_EmptyPayload), _call_health),
    "relearn_scan": _OpSpec(_model_parse(_RelearnScanPayload), _call_relearn_scan),
    "relearn_status": _OpSpec(_model_parse(_EmptyPayload), _call_relearn_status),
    "relearn_preview": _OpSpec(_model_parse(_RelearnPreviewPayload), _call_relearn_preview),
    "relearn_apply": _OpSpec(_model_parse(_RelearnApplyPayload), _call_relearn_apply),
}


# ------------------------------------------------- contract-v3 ingestion verbs
#
# The frozen adapter mapping of ``docs/contracts/ingestion-contract-v3.md``
# (audit-final-v020 F2): ``ingest_file`` / ``ingest_directory`` are exposed on
# all three surfaces (CLI ``ingest-file``/``ingest-directory``, HTTP
# ``POST /v1/knowledge/ingest/file|directory``, MCP
# ``knowledge_ingest_file``/``knowledge_ingest_directory``) and every surface
# runs the same :class:`IngestionPipeline` — the adapter layer adds only the
# contract input validation, the path discipline against the startup-fixed
# ingestion root, and the output budget shaping. The fourteen frozen
# operations are untouched: the ingestion verbs never join
# ``KNOWLEDGE_OPERATIONS``/``OPS_OPERATIONS``/``_OPERATIONS``.
#
# Audit discipline: the pipeline journals exactly one final audit event per
# invocation (``knowledge.ingest_file`` / ``knowledge.ingest_directory``) for
# every invocation it runs, successes and typed failures alike. Input the
# adapter rejects *before* the pipeline runs (closed-key/type/range failures,
# budget bounds, path discipline) is journaled by the adapter itself — one
# failure event under the same operation name, at parity with the other
# adapters (spec 7.7). A response that exceeds ``token_budget`` fails typed
# with ``METADATA_EXCEEDS_BUDGET`` carrying the pipeline's audit id, so the
# one-event rule holds there too.

INGEST_VERBS: tuple[str, ...] = ("ingest_file", "ingest_directory")

_INGEST_ALLOWED_KEYS: dict[str, frozenset[str]] = {
    "ingest_file": frozenset({"path", "token_budget"}),
    "ingest_directory": frozenset({"path", "recursive", "max_files", "token_budget"}),
}

# HTTP statuses for the pipeline's typed failure codes (the contract fixes
# the codes; statuses follow the existing envelope conventions).
_INGEST_STATUS_BY_CODE: dict[str, int] = {
    "PATH_OUTSIDE_ROOT": 400,
    "PATH_NOT_FOUND": 404,
    "PATH_NOT_READABLE": 400,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "FILE_TOO_LARGE": 413,
    "INGESTION_BUDGET_EXCEEDED": 400,
    "EXTRACTOR_UNAVAILABLE": 501,
    "RESOLVER_UNAVAILABLE": 501,
}


def _journal_ingest_rejection(audit: AuditLog, verb: str, arguments: Any, code: str, actor: str, started: float) -> str:
    """Exactly one failure audit event for an adapter-rejected ingest call."""
    try:
        canonical = canonical_json(arguments) if isinstance(arguments, (dict, list)) else "null"
    except TypeError, ValueError:  # pragma: no cover - defensive
        canonical = "null"
    invocation = Invocation.create(
        f"knowledge.{verb}",
        request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
        actor=actor,
    )
    audit.begin(invocation)
    receipt = audit.complete(
        invocation.invocation_id,
        InvocationOutcome(
            status="failure",
            error=code,
            duration_ms=int((time.monotonic() - started) * 1000),
            correlation_id=invocation.invocation_id,
        ),
    )
    return receipt.audit_event_id


def _ingest_reject(
    error: KnowledgeError, *, audit: AuditLog, verb: str, arguments: Any, actor: str, started: float
) -> NoReturn:
    """Journal the adapter rejection and raise *error* carrying the audit id."""
    error.audit_event_id = _journal_ingest_rejection(audit, verb, arguments, error.code, actor, started)
    raise error


def _reject_ingest_pipeline_style(
    error: IngestionError, *, audit: AuditLog, verb: str, arguments: Any, actor: str, started: float
) -> NoReturn:
    """Journal one adapter-side rejection for a contract path-discipline error.

    ``IngestionError`` carries no ``audit_event_id`` attribute; the id is
    attached here so the MCP/HTTP surfaces can surface it (and prove the
    single-audit rule) without a second journal entry.
    """
    error.audit_event_id = _journal_ingest_rejection(audit, verb, arguments, error.code, actor, started)  # type: ignore[attr-defined]
    raise error


def run_ingest_verb(
    store: EventStore,
    audit: AuditLog,
    verb: str,
    arguments: Mapping[str, Any],
    *,
    actor: str,
    ingestion_root: str | Path,
    max_token_budget: int = DEFAULT_MAX_TOKEN_BUDGET,
) -> dict[str, Any]:
    """Run one contract-v3 ingestion verb through the existing pipeline.

    *arguments* is the decoded payload (``path``, ``token_budget=1024``, and
    for ``ingest_directory`` also ``recursive=true`` / ``max_files=1024``).
    The ingestion root is fixed at startup by the calling surface (CLI: the
    process working directory; HTTP: the stack's ops root; MCP: the startup
    graph root). Raises the contract's typed errors — everything is journaled
    exactly once, by the adapter (pre-pipeline rejections) or by the pipeline
    itself (everything after).
    """
    started = time.monotonic()
    if verb not in INGEST_VERBS:
        raise ValueError(f"unknown ingestion verb {verb!r}")  # adapter-internal misuse, never wired
    if not isinstance(arguments, Mapping):
        _ingest_reject(
            AdapterValidationError("payload must be a JSON object"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    unknown = sorted(str(key) for key in arguments if key not in _INGEST_ALLOWED_KEYS[verb])
    if unknown:
        _ingest_reject(
            AdapterValidationError(f"unknown properties rejected: {', '.join(unknown)}"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    path = arguments.get("path")
    if type(path) is not str or not path:
        _ingest_reject(
            AdapterValidationError("property 'path' is required and must be a non-empty string"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    token_budget = arguments.get("token_budget", 1024)
    if isinstance(token_budget, bool) or not isinstance(token_budget, int):
        _ingest_reject(
            AdapterValidationError("property 'token_budget' must be an integer"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    # The frozen token-budget semantics (contract v3, shared guarantees).
    if token_budget < MINIMUM_TOKEN_BUDGET:
        _ingest_reject(
            BudgetTooSmall(f"token_budget {token_budget} is below the {MINIMUM_TOKEN_BUDGET}-token minimum"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    if token_budget > max_token_budget:
        _ingest_reject(
            BudgetExceedsMax(f"token_budget {token_budget} exceeds the configured maximum {max_token_budget}"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    recursive = True
    max_files = 1024
    if verb == "ingest_directory":
        recursive = arguments.get("recursive", True)
        if type(recursive) is not bool:
            _ingest_reject(
                AdapterValidationError("property 'recursive' must be a boolean"),
                audit=audit,
                verb=verb,
                arguments=arguments,
                actor=actor,
                started=started,
            )
        max_files = arguments.get("max_files", 1024)
        if isinstance(max_files, bool) or not isinstance(max_files, int) or not 1 <= max_files <= 65536:
            _ingest_reject(
                AdapterValidationError("property 'max_files' must be an integer in [1, 65536]"),
                audit=audit,
                verb=verb,
                arguments=arguments,
                actor=actor,
                started=started,
            )
    # Path discipline (contract v3): resolve against the startup-fixed
    # ingestion root; escapes, missing paths and unreadable paths are typed.
    try:
        resolved = resolve_within_root(ingestion_root, path)
    except PathEscapeError:
        _reject_ingest_pipeline_style(
            PathOutsideRootError(f"path {path!r} escapes the ingestion root"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    if not resolved.exists():
        _reject_ingest_pipeline_style(
            PathNotFoundError(f"input path {path!r} does not exist"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    if verb == "ingest_file" and not resolved.is_file():
        _reject_ingest_pipeline_style(
            PathNotFoundError(f"input path {path!r} is not a regular file"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    if verb == "ingest_directory" and not resolved.is_dir():
        _reject_ingest_pipeline_style(
            PathNotFoundError(f"input path {path!r} is not a directory"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    if not os.access(resolved, os.R_OK):
        _reject_ingest_pipeline_style(
            PathNotReadableError(f"input path {path!r} is not readable"),
            audit=audit,
            verb=verb,
            arguments=arguments,
            actor=actor,
            started=started,
        )
    pipeline = IngestionPipeline(store, budget=IngestionBudget(max_files=max_files), actor=actor)
    report = pipeline.ingest_path(resolved, incremental=True, recursive=recursive, resolve=True)
    payload = report.to_dict()
    # The contract's ``input.path`` is relative to the startup-fixed ingestion
    # root; the pipeline reports against its own derived root (the parent of
    # a file input). Re-anchor the envelope — never the per-file entries,
    # which the contract defines relative to the walked directory.
    root_path = Path(ingestion_root).resolve()
    payload["root"] = str(root_path)
    payload["input"] = {**payload["input"], "path": resolved.relative_to(root_path).as_posix()}
    if verb == "ingest_file":
        # The file proposal is atomic: it never truncates (contract v3).
        if estimated_tokens_of_payload(payload) > token_budget:
            raise MetadataExceedsBudget(
                f"the ingest_file proposal exceeds the {token_budget}-token budget",
                audit_event_id=report.audit_event_id,
            )
        return payload
    # The directory payload truncates the ``files`` list tail-first under
    # budget pressure; the envelope (input/stats/guards_hit) never truncates.
    # The ``truncated`` flag is part of the returned payload, so the budget
    # check must measure with the flag's space reserved (``false`` is one
    # byte longer than ``true`` — conservative): adding the key only after
    # the loop converged let the final payload overshoot the budget by the
    # flag's own ~19 bytes (surfaced on macOS CI, where tmp roots are longer
    # and the numbers land exactly on the boundary).
    files = list(payload["files"])
    truncated = False
    probed = {**payload, "truncated": False}
    while files and estimated_tokens_of_payload(probed) > token_budget:
        files.pop()
        truncated = True
        probed = {**probed, "files": files}
    if truncated:
        payload = {**payload, "files": files, "truncated": True}
    return payload


def _ingest_error_envelope(error: IngestionError | ExtractorUnavailableError) -> dict[str, Any]:
    """The standard envelope for the pipeline's typed failures."""
    return {
        "error": {
            "code": error.code,
            "message": redact_text(str(error))[:_MAX_ERROR_MESSAGE],
            "retryable": False,
            "audit_event_id": getattr(error, "audit_event_id", None),
        }
    }


def _handle_ingest_sync(stack: LocalStack, verb: str, body: bytes, actor: str) -> dict[str, Any]:
    """Decode, validate and run one ingestion verb (contract v3)."""
    try:
        payload: Any = json.loads(body.decode("utf-8")) if body.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        audit_event_id = _journal_ingest_rejection(stack.audit, verb, {}, "VALIDATION_ERROR", actor, time.monotonic())
        raise AdapterValidationError(
            f"request body is not valid JSON: {error}", audit_event_id=audit_event_id
        ) from error
    ingestion_root = stack.ops_root if stack.ops_root is not None else Path(stack.store.db_path).resolve().parent
    return run_ingest_verb(stack.store, stack.audit, verb, payload, actor=actor, ingestion_root=ingestion_root)


def _validation_summary(error: ValidationError, *, max_errors: int = 3) -> str:
    """Field/type summary of a pydantic failure — never raw input values.

    The MCP adapter keeps the same value-free rendering; the two validators'
    accept/reject parity is guarded by tests/test_adapter_validation_parity.py
    (arch review F-2 — a contract test, not a mirror comment, holds the lock).
    Pydantic's own ``str()`` rendering embeds ``input_value=...``, which would
    echo attacker-supplied values back onto the wire; the ``loc`` + ``msg``
    pair carries the diagnostic without the value.
    """
    entries = error.errors()
    parts: list[str] = []
    for entry in entries[:max_errors]:
        location = ".".join(str(part) for part in entry.get("loc", ()))
        message = str(entry.get("msg", "invalid value"))
        parts.append(f"{location}: {message}" if location else message)
    if not parts:
        return "invalid structured payload"
    summary = "; ".join(parts)
    remaining = len(entries) - len(parts)
    return f"{summary} (+{remaining} more)" if remaining > 0 else summary


def _journal_validation_failure(
    stack: LocalStack, operation: str, raw: bytes, error: BaseException, actor: str, *, message: str | None = None
) -> str:
    """Append the one completion audit record for adapter-rejected input."""
    invocation = Invocation.create(
        f"knowledge.{operation}",
        request_hash=hashlib.sha256(raw).hexdigest(),
        actor=actor,
    )
    stack.audit.begin(invocation)
    message = (message if message is not None else redact_text(str(error)))[:_MAX_ERROR_MESSAGE]
    receipt = stack.audit.complete(
        invocation.invocation_id,
        InvocationOutcome(
            status="failure",
            error=f"VALIDATION_ERROR: {message}",
            correlation_id=invocation.invocation_id,
        ),
    )
    return receipt.audit_event_id


def dispatch(
    stack: LocalStack,
    operation: str,
    payload: Any,
    *,
    actor: str = "operator",
    idempotency_key: str | None = None,
    raw: bytes = b"",
) -> Any:
    """Validate *payload* and invoke the operation on the shared service."""
    spec = _OPERATIONS.get(operation)
    if spec is None:
        raise UnknownOperation(f"unknown operation {operation!r}")
    try:
        request = spec.parse(raw if raw else payload)
    except AdapterValidationError as error:
        error.audit_event_id = _journal_validation_failure(stack, operation, raw, error, actor)
        raise
    except (ValidationError, ValueError, TypeError) as error:
        # Wave-2 F8: pydantic errors surface a value-free loc/msg summary; the
        # same summary is journaled so wire and audit agree. ValueError and
        # TypeError carry no structured form, so they keep the redacted text.
        if isinstance(error, ValidationError):
            message = redact_text(_validation_summary(error))[:_MAX_ERROR_MESSAGE]
        else:
            message = redact_text(str(error))[:_MAX_ERROR_MESSAGE]
        audit_event_id = _journal_validation_failure(stack, operation, raw, error, actor, message=message)
        raise AdapterValidationError(message, audit_event_id=audit_event_id) from error
    return spec.call(stack, request, actor, idempotency_key)


def dispatch_text(
    stack: LocalStack,
    operation: str,
    text: str | bytes,
    *,
    actor: str = "operator",
    idempotency_key: str | None = None,
) -> Any:
    """JSON-decode *text* and dispatch; decode failures are journaled too."""
    raw = text.encode("utf-8") if isinstance(text, str) else text
    try:
        payload: Any = json.loads(raw.decode("utf-8")) if raw.strip() else {}
    except (json.JSONDecodeError, UnicodeDecodeError) as error:
        audit_event_id = _journal_validation_failure(stack, operation, raw, error, actor)
        raise AdapterValidationError(
            f"request body is not valid JSON: {error}", audit_event_id=audit_event_id
        ) from error
    return dispatch(stack, operation, payload, actor=actor, idempotency_key=idempotency_key, raw=raw)


# -------------------------------------------------------------- serialization


def to_jsonable(value: Any) -> Any:
    if isinstance(value, BaseModel):
        return value.model_dump(mode="json")
    if is_dataclass(value) and not isinstance(value, type):
        return to_jsonable(asdict(value))
    if isinstance(value, Mapping):
        return {str(key): to_jsonable(item) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [to_jsonable(item) for item in value]
    return value


_STATUS_BY_CODE: dict[str, int] = {
    "VALIDATION_ERROR": 400,
    "NOT_FOUND": 404,
    "NODE_NOT_FOUND": 404,
    "PRIMARY_ROOT_MISSING": 404,
    "UNKNOWN_DOMAIN": 404,
    "AMBIGUOUS_DOMAIN": 409,
    "BUDGET_TOO_SMALL": 400,
    "BUDGET_EXCEEDS_MAX": 400,
    "METADATA_EXCEEDS_BUDGET": 400,
    "STALE_PROJECTION": 503,
    "NOT_IMPLEMENTED": 501,
    "POLICY_HASH_MISMATCH": 500,
    "PAYLOAD_TOO_LARGE": 413,
    "REQUEST_TIMEOUT": 408,
    "UNSUPPORTED_MEDIA_TYPE": 415,
    "CLIENT_DISCONNECTED": 400,
    # Service-level relearn gate refusals (final-audit F-4).
    "OPERATOR_DECISION_REQUIRED": 409,
    "UNKNOWN_RELEARN": 404,
}

_STATUS_BY_TYPE: dict[str, int] = {
    "VersionConflict": 409,
    "ApprovalRequired": 409,
    "UnknownNode": 404,
    "UnknownImprovement": 404,
    "RegressionGateFailure": 422,
    "ProposalValidationError": 400,
    "BackupError": 400,
    # Relearn contract failures (StoreError subclass; code = type name).
    "NoPendingRelearn": 409,
    "OperatorDecisionRequired": 409,
    "UnknownRelearn": 404,
    # ProjectionError family: typed whether or not the core declares it a
    # StoreError — the wire result is identical either way (final-audit F-5).
    "ProjectorBusy": 503,
    "StalePublicationError": 409,
    "ShrinkGuardError": 422,
    "ProjectionValidationError": 422,
    "OversizedArtifactError": 507,
}

# Transient-by-contract types: the caller may retry as-is.
_RETRYABLE_TYPES: frozenset[str] = frozenset({"ProjectorBusy", "StalePublicationError"})

# Final-audit F-5: the BackupError/ProjectionError family collapses into the
# INTERNAL envelope when it does not ride the StoreError hierarchy — with the
# actionable message sanitized off the wire and a status/body code mismatch.
# This table is the adapter-side guarantee: the wire code is the type name
# (the StoreError pattern), the status and retryable flag are honest, and the
# (redacted) message survives. When the core declares these as StoreError,
# the StoreError branches produce the identical result via _STATUS_BY_TYPE.
_OPS_ERROR_MAP: dict[str, tuple[str, int, bool]] = {
    "BackupError": ("BackupError", 400, False),
    "ProjectorBusy": ("ProjectorBusy", 503, True),
    "StalePublicationError": ("StalePublicationError", 409, True),
    "ShrinkGuardError": ("ShrinkGuardError", 422, False),
    "ProjectionValidationError": ("ProjectionValidationError", 422, False),
    "OversizedArtifactError": ("OversizedArtifactError", 507, False),
}


def _ops_error_mapping(error: BaseException) -> tuple[str, int, bool] | None:
    if not isinstance(error, (BackupError, ProjectionError)):
        return None
    return _OPS_ERROR_MAP.get(type(error).__name__)


def status_for_error(error: BaseException) -> int:
    """Map a failure to its HTTP status; unknown failures are 500."""
    if isinstance(error, KnowledgeError):
        return _STATUS_BY_CODE.get(error.code, 400)
    if isinstance(error, PathEscapeError):
        return 400
    if isinstance(error, (HostNotAllowedError, UnauthorizedError)):
        return 403 if isinstance(error, HostNotAllowedError) else 401
    if isinstance(error, StoreError):
        return _STATUS_BY_TYPE.get(type(error).__name__, 422)
    mapped = _ops_error_mapping(error)
    if mapped is not None:
        return mapped[1]
    return _STATUS_BY_TYPE.get(type(error).__name__, 500)


def error_envelope(error: BaseException) -> dict[str, Any]:
    """The consistent error envelope for every surfaced failure."""
    if isinstance(error, KnowledgeError):
        code = error.code
        retryable = error.retryable
        audit_event_id = error.audit_event_id
    elif isinstance(error, BindPolicyError):
        code, retryable, audit_event_id = "BIND_POLICY", False, None
    elif isinstance(error, HostNotAllowedError):
        code, retryable, audit_event_id = "HOST_NOT_ALLOWED", False, None
    elif isinstance(error, UnauthorizedError):
        code, retryable, audit_event_id = "UNAUTHORIZED", False, None
    elif isinstance(error, PathEscapeError):
        code, retryable, audit_event_id = "PATH_ESCAPE", False, None
    elif isinstance(error, StoreError):
        code = type(error).__name__
        retryable = code in _RETRYABLE_TYPES
        audit_event_id = None
    elif (mapped := _ops_error_mapping(error)) is not None:
        code, _, retryable = mapped
        audit_event_id = None
    else:
        code, retryable, audit_event_id = "INTERNAL", False, None
    message = redact_text(str(error))[:_MAX_ERROR_MESSAGE]
    return {
        "error": {
            "code": code,
            "message": message,
            "retryable": retryable,
            "audit_event_id": audit_event_id,
        }
    }


def _json_response(payload: Any, *, status: int = 200) -> Response:
    return Response(content=canonical_json(payload), status_code=status, media_type="application/json")


# --------------------------------------------------------------- http surface


def _now_iso() -> str:
    return datetime.now(UTC).isoformat()


def _record_security_denial(
    store: EventStore,
    scope: Mapping[str, Any],
    error: BaseException,
    *,
    denial_count: int = 1,
    window_seconds: float | None = None,
) -> None:
    """Append one tamper-evident audit event for a boundary security denial.

    Wave-2 F9(a): a 401/403 rejection is not an invocation (spec 7.7), so it
    never opens an invocation journal — but it is security-relevant, so it
    appends a standalone ``security.boundary_denial`` event to the store's
    hash-chained event log through the existing public ``insert_event`` API.
    The payload carries reason, peer, host, method and path — never any
    presented key material. ``denial_count`` accounts for the denials this
    single event covers (final-audit S-5 aggregation); it is 1 when the
    recorder writes every denial individually.
    """
    reason = "host_not_allowed" if isinstance(error, HostNotAllowedError) else "unauthorized"
    client = scope.get("client")
    headers = {name.lower(): value for name, value in scope.get("headers", [])}
    raw_host = headers.get(b"host")
    host = sanitize_text(raw_host.decode("latin-1"), max_length=253) if raw_host is not None else None
    payload: dict[str, Any] = {
        "reason": reason,
        "remote_addr": str(client[0]) if client else "local",
        "host": host,
        "method": str(scope.get("method", "")),
        "path": str(scope.get("path", "")),
        "denial_count": denial_count,
        "window_seconds": window_seconds,
    }
    with store.transaction() as conn:
        store.insert_event(
            conn,
            event_id=new_id(),
            kind="audit",
            event_type="security.boundary_denial",
            knowledge_affecting=False,
            actor="boundary",
            occurred_at=_now_iso(),
            payload=payload,
        )


class BoundaryDenialRecorder:
    """Windowed aggregation for boundary-denial audit writes (final-audit S-5).

    An unauthenticated wrong-key storm used to append one ~683-byte
    hash-chained event per request, letting any remote keyless caller grow
    the store without bound and bury the audit signal. The recorder writes
    the first occurrence of each distinct denial — keyed by
    ``(reason, remote_addr, message)``, so a missing-key rejection and a
    wrong-key rejection from the same peer are both audited individually
    (the wave-2 F9(a) contract) — then suppresses *identical repeats*
    inside ``window_seconds`` and folds their count into the next event
    after the window. The trail stays complete (every denial is accounted by
    exactly one event's ``denial_count``) but a homogeneous storm is bounded
    to one event per window. The key map itself is capped; beyond the cap an
    unseen key is written immediately (fidelity over aggregation), so the
    map can never be grown without bound either.
    """

    _MAX_TRACKED_KEYS = 4096

    def __init__(
        self,
        store: EventStore,
        *,
        window_seconds: float = 60.0,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._store = store
        self._window = float(window_seconds)
        self._clock = clock
        self._lock = threading.Lock()
        self._last_recorded: dict[tuple[str, str, str], float] = {}
        self._suppressed: dict[tuple[str, str, str], int] = {}

    def record(self, scope: Mapping[str, Any], error: BaseException) -> None:
        """The ``denial_recorder`` hook for :class:`BoundaryMiddleware`."""
        reason = "host_not_allowed" if isinstance(error, HostNotAllowedError) else "unauthorized"
        client = scope.get("client")
        remote_addr = str(client[0]) if client else "local"
        key = (reason, remote_addr, str(error)[:256])
        with self._lock:
            now = self._clock()
            last = self._last_recorded.get(key)
            if last is not None and now - last < self._window:
                self._suppressed[key] = self._suppressed.get(key, 0) + 1
                return
            count = self._suppressed.pop(key, 0) + 1
            if len(self._last_recorded) < self._MAX_TRACKED_KEYS or key in self._last_recorded:
                self._last_recorded[key] = now
        _record_security_denial(self._store, scope, error, denial_count=count, window_seconds=self._window)


class BoundaryMiddleware:
    """Pure-ASGI boundary: Host allowlist, API-key policy, streaming body cap.

    A configured API key gates *every* client — loopback and unix-socket
    peers included (wave-2 F1, fail-closed against browser drive-by CSRF).
    Bodies are streamed in with a hard byte cap (cut at cap+1, never fully
    buffered first — wave-2 F4) and one total body deadline (slow-loris
    guard: a sub-timeout dribble cannot pin the connection — wave-2 F4
    residual); the buffered body is then replayed to the inner app.

    ``denial_recorder`` (wave-2 F9(a)) is invoked once per 401/403 boundary
    rejection so security denials leave a tamper-evident audit trail; a
    recorder failure is logged and never weakens or delays the denial.
    """

    def __init__(
        self,
        app: Any,
        *,
        api_key: str | None,
        host_allowlist: Sequence[str],
        max_body_bytes: int,
        body_timeout_seconds: float = 10.0,
        denial_recorder: Callable[[Mapping[str, Any], BaseException], None] | None = None,
    ) -> None:
        # Wave-3 W3-9: a non-positive body deadline makes wait_for() lose
        # every race, wedging *all* requests that carry a body with 408s.
        # Refuse the misconfiguration at startup (fail-closed) instead of
        # serving a dead endpoint. ``not x > 0`` also rejects NaN.
        if not body_timeout_seconds > 0:
            raise ValueError(
                f"body_timeout_seconds must be positive, got {body_timeout_seconds!r}; "
                "0 or negative wedges every request that carries a body"
            )
        self._app = app
        self._api_key = api_key
        self._host_allowlist = tuple(host_allowlist)
        self._max_body_bytes = max_body_bytes
        self._body_timeout_seconds = body_timeout_seconds
        self._denial_recorder = denial_recorder

    def _authorize_local(self, presented_key: str | None) -> None:
        """Gate local peers with the configured key (constant-time compare)."""
        if self._api_key is None:
            return
        if presented_key is None:
            raise UnauthorizedError("a configured API key is required, including from loopback/unix clients")
        if not hmac.compare_digest(
            hashlib.sha256(presented_key.encode("utf-8")).digest(),
            hashlib.sha256(self._api_key.encode("utf-8")).digest(),
        ):
            raise UnauthorizedError("the presented API key is invalid")

    async def __call__(self, scope: dict[str, Any], receive: Any, send: Any) -> None:
        if scope["type"] != "http":
            await self._app(scope, receive, send)
            return
        headers = {name.lower(): value for name, value in scope.get("headers", [])}
        host_header = headers.get(b"host")
        host = host_header.decode("latin-1") if host_header is not None else None
        presented_raw = headers.get(b"x-api-key")
        presented_key = presented_raw.decode("latin-1") if presented_raw is not None else None
        client = scope.get("client")
        # A unix-socket peer carries no address; it is a local (loopback) peer.
        remote_addr = str(client[0]) if client else "127.0.0.1"
        try:
            authorize_request(
                RequestView(remote_addr=remote_addr, host=host, api_key=presented_key),
                api_key=self._api_key,
                host_allowlist=self._host_allowlist,
            )
            self._authorize_local(presented_key)
        except (HostNotAllowedError, UnauthorizedError) as error:
            if self._denial_recorder is not None:
                try:
                    self._denial_recorder(scope, error)
                except Exception:  # noqa: BLE001 - an audit failure never weakens the denial
                    logger.warning("security-denial audit write failed for %s", type(error).__name__)
            response = _json_response(error_envelope(error), status=status_for_error(error))
            await response(scope, receive, send)
            return
        content_length = headers.get(b"content-length")
        declared: int | None = None
        if content_length is not None:
            try:
                declared = int(content_length.decode("latin-1"))
            except ValueError:
                declared = self._max_body_bytes + 1
            if declared > self._max_body_bytes:
                response = _json_response(
                    error_envelope(PayloadTooLarge("declared Content-Length exceeds the request size cap")),
                    status=413,
                )
                await response(scope, receive, send)
                return
        has_body = (declared is not None and declared > 0) or b"transfer-encoding" in headers
        if not has_body:
            await self._app(scope, receive, send)
            return
        # Stream the body in: cut at cap+1 (never buffer the whole attack)
        # and bound the whole body by one total deadline — a per-read timeout
        # alone lets a sub-timeout dribble pin the connection indefinitely
        # (wave-2 F4 residual).
        chunks: list[bytes] = []
        total = 0
        loop = asyncio.get_running_loop()
        deadline = loop.time() + self._body_timeout_seconds
        while True:
            try:
                message = await asyncio.wait_for(receive(), timeout=max(deadline - loop.time(), 0.0))
            except TimeoutError:
                response = _json_response(
                    error_envelope(
                        RequestBodyTimeout("request body was not fully received within the total body deadline")
                    ),
                    status=408,
                )
                await response(scope, receive, send)
                return
            if message["type"] == "http.disconnect":
                return  # the client is gone; nothing to answer
            if message["type"] != "http.request":
                continue
            total += len(message.get("body", b""))
            if total > self._max_body_bytes:
                response = _json_response(
                    error_envelope(PayloadTooLarge("request body exceeds the size cap")),
                    status=413,
                )
                await response(scope, receive, send)
                return
            chunks.append(message.get("body", b""))
            if not message.get("more_body"):
                break
        buffered = b"".join(chunks)
        delivered = False

        async def replay_receive() -> dict[str, Any]:
            nonlocal delivered
            if not delivered:
                delivered = True
                return {"type": "http.request", "body": buffered, "more_body": False}
            return await receive()

        await self._app(scope, replay_receive, send)


def _build_watch_loop(
    stack: LocalStack,
    watch_root: str | Path,
    *,
    interval_seconds: float,
    cooldown_seconds: float,
    state_path: str | Path | None,
) -> SourceWatchLoop:
    """Wire the serve-time doc-change watcher to the gated relearn path (F-1).

    Spec 13.5: the doc-change watcher drives the deterministic update
    pipeline. The watcher only *detects*: every change batch rides the
    service's audited ``offer_relearn`` — the same gated path as
    ``ops/relearn_scan`` — so a detected change publishes the deterministic
    offer plus ``RELEARN_PENDING`` and nothing more. No auto-apply, no
    baseline flip (final-audit F-4 semantics); the operator's
    ``relearn_preview``/``relearn_apply`` decision stays mandatory.
    """
    root = Path(watch_root).resolve()
    if not root.is_dir():
        raise UpdatePipelineError(f"watch root {str(root)!r} is not an existing directory")
    if state_path is not None:
        resolved_state = Path(state_path).resolve()
    elif stack.store.db_path != ":memory:":
        resolved_state = Path(stack.store.db_path).resolve().parent / "source-watch.json"
    else:
        raise UpdatePipelineError("watching with an in-memory store requires an explicit watch_state_path")
    # The registry's SOUL_CORPUS scan hashes every regular file under the
    # root, so the watcher mirrors it (patterns=None) and its snapshot must
    # live *outside* the root — a state file inside would perturb the very
    # fingerprint detection depends on and mint spurious offers.
    try:
        resolve_within_root(root, resolved_state)
    except PathEscapeError:
        pass  # outside the watched root: required
    else:
        raise UpdatePipelineError(
            f"watch state path {str(resolved_state)!r} lies inside the watched root {str(root)!r}; "
            "state writes would perturb the corpus fingerprint"
        )

    def _gate(changes: list[SourceChange]) -> RelearnOffer | None:
        return stack.service.offer_relearn(root, SourceKind.SOUL_CORPUS, actor="source-watcher")

    return SourceWatchLoop(
        SourceWatcher(root, state_path=resolved_state, patterns=None),
        _gate,
        interval_seconds=interval_seconds,
        cooldown_seconds=cooldown_seconds,
    )


def create_app(
    stack: LocalStack,
    *,
    api_key: str | None = None,
    host_allowlist: Sequence[str] = DEFAULT_HOST_ALLOWLIST,
    max_body_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    body_timeout_seconds: float = 10.0,
    watch_root: str | Path | None = None,
    watch_interval_seconds: float = 30.0,
    watch_cooldown_seconds: float = 300.0,
    watch_state_path: str | Path | None = None,
) -> Any:
    """Build the versioned ``/v1`` ASGI app over *stack*.

    With ``watch_root`` the app runs the spec-13.5 doc-change watcher for its
    own lifespan (arch review F-1): polling starts at server startup and stops
    at shutdown; a detected corpus change publishes the gated relearn offer
    (``RELEARN_PENDING``) with no auto-apply — see :func:`_build_watch_loop`.
    """
    watch_loop: SourceWatchLoop | None = None
    if watch_root is not None:
        watch_loop = _build_watch_loop(
            stack,
            watch_root,
            interval_seconds=watch_interval_seconds,
            cooldown_seconds=watch_cooldown_seconds,
            state_path=watch_state_path,
        )

    @asynccontextmanager
    async def _lifespan(app: Starlette) -> AsyncIterator[None]:
        if watch_loop is not None:
            watch_loop.start()
        try:
            yield
        finally:
            if watch_loop is not None:
                watch_loop.stop()

    async def health(request: Request) -> Response:
        report = await run_in_threadpool(_call_health, stack, _EmptyPayload(), "operator", None)
        return _json_response(to_jsonable(report), status=200 if report.ok else 503)

    async def knowledge(request: Request) -> Response:
        operation = request.path_params["operation"]
        if operation not in KNOWLEDGE_OPERATIONS:
            return _json_response(error_envelope(UnknownOperation(f"unknown operation {operation!r}")), status=404)
        return await _handle(stack, operation, request, max_body_bytes)

    async def ops(request: Request) -> Response:
        operation = request.path_params["operation"]
        if operation not in OPS_OPERATIONS:
            return _json_response(error_envelope(UnknownOperation(f"unknown operation {operation!r}")), status=404)
        return await _handle(stack, operation, request, max_body_bytes)

    # Contract v3 ingestion verbs (addendum ingestion-contract-v3.md; audit
    # F2): two fixed routes, the same boundary middleware and JSON/CSRF
    # discipline as every other POST route.
    async def ingest_file(request: Request) -> Response:
        return await _handle_ingest(stack, "ingest_file", request, max_body_bytes)

    async def ingest_directory(request: Request) -> Response:
        return await _handle_ingest(stack, "ingest_directory", request, max_body_bytes)

    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/knowledge/ingest/file", ingest_file, methods=["POST"]),
        Route("/v1/knowledge/ingest/directory", ingest_directory, methods=["POST"]),
        Route("/v1/knowledge/{operation}", knowledge, methods=["POST"]),
        Route("/v1/ops/{operation}", ops, methods=["POST"]),
    ]
    app = Starlette(routes=routes, lifespan=_lifespan if watch_loop is not None else None)
    denial_recorder = BoundaryDenialRecorder(stack.store)
    return BoundaryMiddleware(
        app,
        api_key=api_key,
        host_allowlist=host_allowlist,
        max_body_bytes=max_body_bytes,
        body_timeout_seconds=body_timeout_seconds,
        denial_recorder=denial_recorder.record,
    )


async def _handle(stack: LocalStack, operation: str, request: Request, max_body_bytes: int) -> Response:
    # CSRF guard (wave-2 F1): cross-origin forms cannot set application/json,
    # so requiring it fails browser drive-bys closed before any dispatch.
    content_type = request.headers.get("content-type")
    media_type = content_type.split(";", 1)[0].strip().lower() if content_type else ""
    if media_type != "application/json":
        return _json_response(
            error_envelope(UnsupportedMediaType("POST routes require Content-Type: application/json")),
            status=415,
        )
    try:
        body = await request.body()
    except ClientDisconnect:
        # Wave-2 F9(b): a dropped connection is one operator log line, never
        # a per-disconnect traceback out of the handler.
        logger.info("client disconnected while reading the request body for %s", operation)
        return _json_response(
            error_envelope(ClientDisconnected("the client disconnected before the request body arrived")),
            status=400,
        )
    if len(body) > max_body_bytes:
        return _json_response(error_envelope(PayloadTooLarge("request body exceeds the size cap")), status=413)
    actor = request.headers.get("x-actor", "operator")
    idempotency_key = request.headers.get("idempotency-key")
    try:
        result = await run_in_threadpool(
            dispatch_text, stack, operation, body, actor=actor, idempotency_key=idempotency_key
        )
    except Exception as error:  # noqa: BLE001 - every failure uses the envelope
        envelope = error_envelope(error)
        if envelope["error"]["code"] == "INTERNAL":
            # Wave-2 F5: raw exception text (paths, OS errors) goes to the
            # server log; the wire message is fixed.
            logger.exception("unhandled internal error serving %s", operation)
            envelope["error"]["message"] = "internal error"
        return _json_response(envelope, status=status_for_error(error))
    status = 200
    if operation == "health" and isinstance(result, HealthReport):
        # Final-audit F-8: POST /v1/ops/health must agree with GET /v1/health —
        # monitoring wired to the wrong verb must not see green when degraded.
        status = 200 if result.ok else 503
    return _json_response(to_jsonable(result), status=status)


async def _handle_ingest(stack: LocalStack, verb: str, request: Request, max_body_bytes: int) -> Response:
    """The contract-v3 ingestion routes: same body/CSRF discipline as _handle."""
    content_type = request.headers.get("content-type")
    media_type = content_type.split(";", 1)[0].strip().lower() if content_type else ""
    if media_type != "application/json":
        return _json_response(
            error_envelope(UnsupportedMediaType("POST routes require Content-Type: application/json")),
            status=415,
        )
    try:
        body = await request.body()
    except ClientDisconnect:
        logger.info("client disconnected while reading the request body for knowledge.%s", verb)
        return _json_response(
            error_envelope(ClientDisconnected("the client disconnected before the request body arrived")),
            status=400,
        )
    if len(body) > max_body_bytes:
        return _json_response(error_envelope(PayloadTooLarge("request body exceeds the size cap")), status=413)
    actor = request.headers.get("x-actor", "operator")
    try:
        result = await run_in_threadpool(_handle_ingest_sync, stack, verb, body, actor)
    except (IngestionError, ExtractorUnavailableError) as error:
        # The pipeline journaled the invocation's single audit event itself.
        return _json_response(_ingest_error_envelope(error), status=_INGEST_STATUS_BY_CODE.get(error.code, 400))
    except Exception as error:  # noqa: BLE001 - every failure uses the envelope
        envelope = error_envelope(error)
        if envelope["error"]["code"] == "INTERNAL":
            logger.exception("unhandled internal error serving knowledge.%s", verb)
            envelope["error"]["message"] = "internal error"
        return _json_response(envelope, status=status_for_error(error))
    return _json_response(to_jsonable(result), status=200)


# --------------------------------------------------------------------- serve


def _unlink_stale_socket(path: Path) -> None:
    """Remove *path* iff it is a stale socket; fail closed on anything else.

    Final-audit S-3: a leftover socket file is stale exactly when nothing
    answers a connect (``ECONNREFUSED``/``ENOENT``) — a bound-but-dead socket
    from a previous run. A live listener (or a socket that cannot even be
    probed) and any non-socket — a regular file, a symlink, whatever an
    accident or an attacker dropped at the path — is never unlinked; the
    bind refuses with :class:`BindPolicyError` instead.
    """
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return
    if not stat.S_ISSOCK(info.st_mode):
        raise BindPolicyError(
            f"refusing to bind {str(path)!r}: the path exists and is not a socket "
            f"(mode {stat.S_IFMT(info.st_mode):05o}); remove it manually if it is truly stale"
        )
    probe = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        probe.settimeout(1.0)
        probe.connect(str(path))
    except OSError as error:
        if error.errno not in (errno.ECONNREFUSED, errno.ENOENT):
            raise BindPolicyError(
                f"refusing to bind {str(path)!r}: cannot verify the existing socket is stale ({error})"
            ) from error
        path.unlink()
    else:
        raise BindPolicyError(f"refusing to bind {str(path)!r}: a live listener answers on the existing socket")
    finally:
        probe.close()


def _unix_socket_path_limit() -> int:
    """Usable ``sun_path`` capacity in bytes (NUL terminator excluded).

    107 on Linux (108-byte ``sun_path``), 103 on macOS/the BSDs (104-byte
    ``sun_path``). Beyond the cap ``bind`` fails with a bare
    ``OSError("AF_UNIX path too long")`` — checked up front so the caller
    gets a typed, actionable error instead.
    """
    return 103 if platform.system() == "Darwin" else 107


def _bind_unix_socket(socket_path: str | Path, *, backlog: int = 2048) -> socket.socket:
    """Bind a unix socket, created owner-only (``0o600``) atomically, restartable.

    The socket inode is born ``0o600``: a temporary ``0o177`` umask around
    ``bind`` makes the permission atomic — literally race-free, with no
    bind→chmod window and no path-based chmod for a swapped symlink to ride
    (final-audit S-4; Linux ``fchmod`` on a socket fd is a silent no-op, so
    the umask is the only honest option). A stale socket file from a
    previous run is unlinked first — after verifying it is a socket and no
    listener answers (:func:`_unlink_stale_socket`, final-audit S-3) — so
    restarts never die on ``EADDRINUSE``. Paths beyond the platform's
    AF_UNIX cap are rejected typed (the default deep-tmp paths on macOS
    runners exceed its 103-byte limit where Linux's 107 still fits).
    """
    path = Path(socket_path)
    limit = _unix_socket_path_limit()
    if len(os.fsencode(path)) > limit:
        raise BindPolicyError(
            f"unix socket path {str(path)!r} is {len(os.fsencode(path))} bytes, beyond this platform's "
            f"AF_UNIX cap of {limit} bytes — pass a shorter --socket path (or use --http)"
        )
    _unlink_stale_socket(path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    previous_umask = os.umask(0o177)
    try:
        sock.bind(str(path))
        sock.listen(backlog)
    except BaseException:
        sock.close()
        raise
    finally:
        os.umask(previous_umask)
    return sock


def serve(
    stack: LocalStack,
    *,
    socket_path: str | Path | None = None,
    http: str | None = None,
    api_key: str | None = None,
    host_allowlist: Sequence[str] = DEFAULT_HOST_ALLOWLIST,
    max_body_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    timeout_keep_alive: int = 5,
    body_timeout_seconds: float = 10.0,
    watch_root: str | Path | None = None,
    watch_interval_seconds: float = 30.0,
    watch_cooldown_seconds: float = 300.0,
    watch_state_path: str | Path | None = None,
) -> None:
    """Serve the API: Unix socket by default, opt-in loopback HTTP.

    Anything beyond loopback fails closed through
    :func:`know_your_soul.security.assert_bind_allowed` unless an API key is
    configured. When a key is configured it gates every client, loopback and
    unix-socket peers included (wave-2 F1). The unix socket is created
    owner-only (``0o600``) via :func:`_bind_unix_socket` (wave-2 F2).
    Uvicorn owns SIGINT/SIGTERM graceful shutdown; the store is closed by the
    caller when ``serve`` returns. With ``watch_root`` the spec-13.5
    doc-change watcher runs for the server's lifespan — detect-only, gated
    offers, never an auto-apply (:func:`create_app`, arch review F-1).
    """
    if (socket_path is None) == (http is None):
        raise BindPolicyError("exactly one of socket_path or http is required")
    bind = f"unix:{socket_path}" if socket_path is not None else str(http)
    assert_bind_allowed(bind, api_key=api_key)
    app = create_app(
        stack,
        api_key=api_key,
        host_allowlist=host_allowlist,
        max_body_bytes=max_body_bytes,
        body_timeout_seconds=body_timeout_seconds,
        watch_root=watch_root,
        watch_interval_seconds=watch_interval_seconds,
        watch_cooldown_seconds=watch_cooldown_seconds,
        watch_state_path=watch_state_path,
    )
    listener: socket.socket | None = None
    if socket_path is not None:
        listener = _bind_unix_socket(socket_path)
        config = uvicorn.Config(app, fd=listener.fileno(), timeout_keep_alive=timeout_keep_alive)
    else:
        host, _, port_text = str(http).rpartition(":")
        config = uvicorn.Config(app, host=host, port=int(port_text), timeout_keep_alive=timeout_keep_alive)
    try:
        uvicorn.Server(config).run()
    finally:
        if listener is not None and socket_path is not None:
            listener.close()
            # S-3: a clean shutdown removes the socket file so the next start
            # never meets it; _bind_unix_socket covers the crash case anyway.
            # If the path no longer names a stale socket (swapped, replaced
            # by a live listener) the unlink refuses — cleanup never raises.
            try:
                _unlink_stale_socket(Path(socket_path))
            except BindPolicyError:
                logger.warning("leaving socket path %r in place: it no longer names our stale socket", socket_path)

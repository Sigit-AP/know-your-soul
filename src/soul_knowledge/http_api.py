"""Persistent local JSON API and shared operation dispatch (Task 7).

Task 7 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 7 (invocation/audit flow), 8 and 13.1.

This module is a *thin adapter*: it validates transport payloads, forwards
them to the ten-operation :class:`~soul_knowledge.service.KnowledgeService`
(or the operational commands: projection rebuild, backup, restore, health)
and serializes the result. No business logic lives here.

Audit discipline (spec 7.7):

* The service journals every invocation it receives — successes and typed
  failures — so the adapter never journals a second time on the service path.
* Input rejected *by the adapter* (malformed JSON, unknown fields, wrong
  shapes) never reaches the service, so the adapter itself journals exactly
  one completion audit record (``status=failure``) before rejecting. Every
  valid invocation therefore has exactly one final audit event.

Boundaries: the Starlette app enforces the Host allowlist and the
loopback/Unix-socket policy from :mod:`soul_knowledge.security`, caps request
bodies (streaming, under one total body deadline), requires ``application/json`` on
POST routes (CSRF drive-by rejection), and exposes only versioned ``/v1``
routes. ``serve`` binds a Unix socket by default — created owner-only
(``0o600``) via :func:`_bind_unix_socket`; loopback HTTP is opt-in and wider
binds require an API key. When an API key is configured it gates *every*
client, loopback and unix-socket peers included (fail-closed; the CLI never
talks to the socket, it dispatches in-process, so local CLI flows are
unaffected). ``ops/backup`` and ``ops/restore`` paths are confined to the
stack's ``ops_root`` (default: the store's directory) via
:func:`soul_knowledge.security.resolve_within_root`.
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import json
import logging
import os
import socket
from collections.abc import Callable, Mapping, Sequence
from dataclasses import asdict, dataclass, is_dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import uvicorn
from pydantic import BaseModel, ConfigDict, Field, ValidationError
from starlette.applications import Starlette
from starlette.concurrency import run_in_threadpool
from starlette.requests import ClientDisconnect, Request
from starlette.responses import Response
from starlette.routing import Route

from soul_knowledge.audit import AuditLog
from soul_knowledge.backup import backup_store, restore_store
from soul_knowledge.contracts import (
    ApplyImprovementRequest,
    EvaluateRequest,
    KnowledgeError,
    ProposeImprovementRequest,
    QueryRequest,
    ReviewRequest,
    RollbackRequest,
)
from soul_knowledge.domain import Evidence, Node, canonical_json, new_id
from soul_knowledge.events import Invocation, InvocationOutcome
from soul_knowledge.observability import check_readiness
from soul_knowledge.projection import GraphProjector
from soul_knowledge.security import (
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
from soul_knowledge.service import KnowledgeService
from soul_knowledge.store import EventStore, StoreError

__all__ = [
    "API_VERSION",
    "DEFAULT_HOST_ALLOWLIST",
    "KNOWLEDGE_OPERATIONS",
    "OPS_OPERATIONS",
    "AdapterValidationError",
    "BoundaryMiddleware",
    "LocalStack",
    "UnknownOperation",
    "build_stack",
    "create_app",
    "dispatch",
    "dispatch_text",
    "error_envelope",
    "serve",
    "status_for_error",
    "to_jsonable",
]

logger = logging.getLogger(__name__)

API_VERSION = "v1"
DEFAULT_HOST_ALLOWLIST: tuple[str, ...] = ("localhost", "127.0.0.1", "::1")
KNOWLEDGE_OPERATIONS: tuple[str, ...] = (
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
)
OPS_OPERATIONS: tuple[str, ...] = ("rebuild", "backup", "restore", "health")

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
    through :func:`soul_knowledge.security.resolve_within_root` against it,
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
    model_config = ConfigDict(frozen=True, extra="forbid")


class _EmptyPayload(_Payload):
    pass


class _GetNodePayload(_Payload):
    node_id: str = Field(min_length=1)


class _GetSubtreePayload(_Payload):
    node_id: str = Field(min_length=1)
    depth: int = 2
    mode: str = "bfs"
    relation_allowlist: tuple[str, ...] | None = None
    max_nodes: int = 64
    max_edges: int = 256
    token_budget: int = 1024


class _RebuildPayload(_Payload):
    upto_knowledge_seq: int | None = None


class _BackupPayload(_Payload):
    dest_path: str = Field(min_length=1)


class _RestorePayload(_Payload):
    backup_path: str = Field(min_length=1)
    dest_path: str = Field(min_length=1)
    overwrite: bool = False


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


def _call_rebuild(stack: LocalStack, request: _RebuildPayload, actor: str, idem: str | None) -> Any:
    return stack.projector.rebuild(upto_knowledge_seq=request.upto_knowledge_seq)


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
        return resolve_within_root(stack.ops_root, candidate)
    except PathEscapeError as error:
        raise PathEscapeError(f"path {candidate!r} is outside the permitted operations root") from error


def _call_backup(stack: LocalStack, request: _BackupPayload, actor: str, idem: str | None) -> Any:
    return backup_store(stack.store, _resolve_ops_path(stack, request.dest_path))


def _call_restore(stack: LocalStack, request: _RestorePayload, actor: str, idem: str | None) -> Any:
    return restore_store(
        _resolve_ops_path(stack, request.backup_path),
        _resolve_ops_path(stack, request.dest_path),
        overwrite=request.overwrite,
    )


def _call_health(stack: LocalStack, request: _EmptyPayload, actor: str, idem: str | None) -> Any:
    return check_readiness(stack.store, projector=stack.projector)


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
    "rebuild": _OpSpec(_model_parse(_RebuildPayload), _call_rebuild),
    "backup": _OpSpec(_model_parse(_BackupPayload), _call_backup),
    "restore": _OpSpec(_model_parse(_RestorePayload), _call_restore),
    "health": _OpSpec(_model_parse(_EmptyPayload), _call_health),
}


def _validation_summary(error: ValidationError, *, max_errors: int = 3) -> str:
    """Field/type summary of a pydantic failure — never raw input values.

    Mirrors the MCP adapter's ``_validation_summary`` (wave-2 F8): pydantic's
    own ``str()`` rendering embeds ``input_value=...``, which would echo
    attacker-supplied values back onto the wire; the ``loc`` + ``msg`` pair
    carries the diagnostic without the value.
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
}

_STATUS_BY_TYPE: dict[str, int] = {
    "VersionConflict": 409,
    "ApprovalRequired": 409,
    "UnknownNode": 404,
    "UnknownImprovement": 404,
    "RegressionGateFailure": 422,
    "ProposalValidationError": 400,
    "BackupError": 400,
}


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
    elif isinstance(error, PayloadTooLarge):
        code, retryable, audit_event_id = "PAYLOAD_TOO_LARGE", False, None
    elif isinstance(error, PathEscapeError):
        code, retryable, audit_event_id = "PATH_ESCAPE", False, None
    elif isinstance(error, StoreError):
        code, retryable, audit_event_id = type(error).__name__, False, None
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


def _record_security_denial(stack: LocalStack, scope: Mapping[str, Any], error: BaseException) -> None:
    """Append one tamper-evident audit event for a boundary security denial.

    Wave-2 F9(a): a 401/403 rejection is not an invocation (spec 7.7), so it
    never opens an invocation journal — but it is security-relevant, so it
    appends a standalone ``security.boundary_denial`` event to the store's
    hash-chained event log through the existing public ``insert_event`` API.
    The payload carries reason, peer, host, method and path — never any
    presented key material.
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
    }
    with stack.store.transaction() as conn:
        stack.store.insert_event(
            conn,
            event_id=new_id(),
            kind="audit",
            event_type="security.boundary_denial",
            knowledge_affecting=False,
            actor="boundary",
            occurred_at=_now_iso(),
            payload=payload,
        )


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


def create_app(
    stack: LocalStack,
    *,
    api_key: str | None = None,
    host_allowlist: Sequence[str] = DEFAULT_HOST_ALLOWLIST,
    max_body_bytes: int = DEFAULT_MAX_PAYLOAD_BYTES,
    body_timeout_seconds: float = 10.0,
) -> Any:
    """Build the versioned ``/v1`` ASGI app over *stack*."""

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

    routes = [
        Route("/v1/health", health, methods=["GET"]),
        Route("/v1/knowledge/{operation}", knowledge, methods=["POST"]),
        Route("/v1/ops/{operation}", ops, methods=["POST"]),
    ]
    app = Starlette(routes=routes)
    return BoundaryMiddleware(
        app,
        api_key=api_key,
        host_allowlist=host_allowlist,
        max_body_bytes=max_body_bytes,
        body_timeout_seconds=body_timeout_seconds,
        denial_recorder=lambda scope, error: _record_security_denial(stack, scope, error),
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
    return _json_response(to_jsonable(result))


# --------------------------------------------------------------------- serve


def _bind_unix_socket(socket_path: str | Path, *, backlog: int = 2048) -> socket.socket:
    """Bind a unix socket with owner-only (``0o600``) permissions, race-free.

    Uvicorn's own ``uds=`` path chmods the socket ``0o666`` after binding
    (wave-2 F2); binding it ourselves, chmodding ``0o600`` immediately and
    handing uvicorn the file descriptor closes the world-writable window
    regardless of the ambient umask.
    """
    path = Path(socket_path)
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    try:
        sock.bind(str(path))
        os.chmod(path, 0o600)
        sock.listen(backlog)
    except BaseException:
        sock.close()
        raise
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
) -> None:
    """Serve the API: Unix socket by default, opt-in loopback HTTP.

    Anything beyond loopback fails closed through
    :func:`soul_knowledge.security.assert_bind_allowed` unless an API key is
    configured. When a key is configured it gates every client, loopback and
    unix-socket peers included (wave-2 F1). The unix socket is created
    owner-only (``0o600``) via :func:`_bind_unix_socket` (wave-2 F2).
    Uvicorn owns SIGINT/SIGTERM graceful shutdown; the store is closed by the
    caller when ``serve`` returns.
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
        if listener is not None:
            listener.close()

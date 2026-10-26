"""The deterministic MCP adapter: fourteen knowledge operations, tools+resources only.

Task 8 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
the binding adaptation contract is ``docs/research/hermes-mcp-contract.md``
section 3.1 and the deep-research adaptations X1/X2/X6/X8
(``docs/research/graphify-mcp-security-deep.md``). The surface is the frozen
ten v1 operations plus the contract-v2 G1 retrieval verbs
(``knowledge_path`` / ``knowledge_explain`` / ``knowledge_affected`` /
``knowledge_god_nodes``) — operator ruling 2026-10-26 (progress.md), addendum
``docs/contracts/retrieval-verbs-v2.md``.

Hard rules enforced here:

* One startup allowlisted graph root. No tool schema carries ``project_path``
  (or any path), so a caller can never select a graph — the Graphify F1
  arbitrary-root/symlink disclosure shape is structurally absent.
* Strict validation before dispatch: unknown properties, missing required
  keys, wrong types (including string booleans and numeric strings), enums,
  ``depth`` in ``[1,6]`` and ``token_budget`` in ``[128, max]`` are rejected
  with :class:`ToolError`, surfaced as ``isError: true`` results carrying a
  typed error envelope with an audit event id — never a successful result
  with raw exception text.
* Every invocation — adapter-rejected or service-handled — produces exactly
  one final audit event, at parity with the CLI (spec section 7).
* Capabilities are exactly ``tools`` + ``resources``: no sampling, prompts,
  logging, completions or elicitation handler is registered, so the SDK
  advertises nothing else and forbidden methods answer ``-32601``.
* Resources are sanitized (control/bidi/zero-width stripping plus secret
  redaction), canonical-JSON, size-capped projections — never raw Graphify
  reports (the F4 shape). There are no PR/``gh`` tools (F3).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import os
import re
import sys
import time
from collections.abc import Mapping
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Literal

from mcp import types
from mcp.server.lowlevel import Server
from mcp.shared.exceptions import MCPError
from pydantic import ValidationError

from know_your_soul import runtime_guard
from know_your_soul.audit import AuditLog
from know_your_soul.contracts import (
    DEFAULT_MAX_TOKEN_BUDGET,
    MINIMUM_TOKEN_BUDGET,
    EvaluateRequest,
    KnowledgeError,
    QueryRequest,
    TraversalMode,
)
from know_your_soul.domain import (
    Evidence,
    Node,
    RelationKind,
    canonical_json,
    storage_payload,
)
from know_your_soul.events import Invocation, InvocationOutcome
from know_your_soul.http_api import DEFAULT_HOST_ALLOWLIST, BoundaryDenialRecorder, BoundaryMiddleware
from know_your_soul.improvement import ApprovalRecord, DiffOperation
from know_your_soul.projection import GraphProjector, ProjectionError
from know_your_soul.security import (
    DEFAULT_MAX_PAYLOAD_BYTES,
    assert_bind_allowed,
    redact_value,
    resolve_within_root,
    sanitize_text,
)
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore, StoreError

__all__ = [
    "DEFAULT_RESOURCE_MAX_BYTES",
    "EXPECTED_TOOLS",
    "ToolError",
    "build_server",
    "build_service",
    "main",
]

DEFAULT_RESOURCE_MAX_BYTES = 65_536
SERVER_NAME = "know-your-soul"

# The spec operations (contract v2: the frozen ten plus the four G1 retrieval
# verbs — operator ruling 2026-10-26, addendum
# ``docs/contracts/retrieval-verbs-v2.md``), with ``.`` rendered as ``_`` so
# every tool name matches the MCP ``^[a-zA-Z0-9_-]{1,64}$`` constraint; Hermes
# registers them as ``mcp__know-your-soul__<name>``.
_TOOL_TO_OPERATION: dict[str, str] = {
    "knowledge_query": "knowledge.query",
    "knowledge_get_node": "knowledge.get_node",
    "knowledge_get_subtree": "knowledge.get_subtree",
    "knowledge_log_event": "knowledge.log_event",
    "knowledge_record_outcome": "knowledge.record_outcome",
    "knowledge_evaluate": "knowledge.evaluate",
    "knowledge_review": "knowledge.review",
    "knowledge_propose_improvement": "knowledge.propose_improvement",
    "knowledge_apply_improvement": "knowledge.apply_improvement",
    "knowledge_rollback": "knowledge.rollback",
    "knowledge_path": "knowledge.path",
    "knowledge_explain": "knowledge.explain",
    "knowledge_affected": "knowledge.affected",
    "knowledge_god_nodes": "knowledge.god_nodes",
}
EXPECTED_TOOLS: tuple[str, ...] = tuple(sorted(_TOOL_TO_OPERATION))

_RELATION_VALUES = sorted(relation.value for relation in RelationKind)
_DIFF_OPS = [
    "set_summary",
    "set_scope",
    "set_payload_key",
    "remove_payload_key",
    "add_plus",
    "remove_plus",
    "add_minus",
    "remove_minus",
    "add_context_tag",
    "remove_context_tag",
    "add_relation",
    "set_status",
]
_RESOURCE_URIS = (
    "soul://projection/manifest",
    "soul://projection/stats",
    "soul://projection/nodes",
    "soul://relearn/status",
)


class ToolError(RuntimeError):
    """A fail-closed tool failure surfaced as an ``isError: true`` result."""

    def __init__(self, code: str, message: str, *, audit_event_id: str | None = None) -> None:
        super().__init__(message)
        self.code = code
        self.audit_event_id = audit_event_id


# --------------------------------------------------------------------- schemas


def _tool_schemas(max_token_budget: int) -> dict[str, dict[str, Any]]:
    """Strict JSON schemas for the fourteen tools; every one is closed to unknown keys."""
    budget = {"type": "integer", "minimum": MINIMUM_TOKEN_BUDGET, "maximum": max_token_budget}
    depth = {"type": "integer", "minimum": 1, "maximum": 6}
    mode = {"enum": ["bfs", "dfs"]}
    tags = {"type": "array", "items": {"type": "string", "maxLength": 256}}
    relations = {"type": ["array", "null"], "items": {"enum": _RELATION_VALUES}}
    idempotency = {"type": ["string", "null"], "minLength": 1, "maxLength": 256}
    node_id = {"type": ["string", "null"], "minLength": 1, "maxLength": 256}
    required_node_id = {"type": "string", "minLength": 1, "maxLength": 256}
    return {
        "knowledge_query": {
            "type": "object",
            "additionalProperties": False,
            "required": ["purpose", "token_budget"],
            "properties": {
                "purpose": {"type": "string", "minLength": 1, "maxLength": 512},
                "domain": {"type": ["string", "null"], "maxLength": 64},
                "context_tags": tags,
                "token_budget": budget,
                "access_only": {"type": "boolean"},
                "depth": depth,
                "mode": mode,
                "relation_allowlist": relations,
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1024},
                "max_edges": {"type": "integer", "minimum": 1, "maximum": 4096},
            },
        },
        "knowledge_get_node": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id"],
            "properties": {"node_id": required_node_id},
        },
        "knowledge_get_subtree": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id"],
            "properties": {
                "node_id": required_node_id,
                "depth": depth,
                "mode": mode,
                "relation_allowlist": relations,
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1024},
                "max_edges": {"type": "integer", "minimum": 1, "maximum": 4096},
                "token_budget": budget,
            },
        },
        "knowledge_log_event": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node"],
            "properties": {
                "node": {"type": "object"},
                "idempotency_key": idempotency,
                "expected_version": {"type": ["integer", "null"], "minimum": 1},
            },
        },
        "knowledge_record_outcome": {
            "type": "object",
            "additionalProperties": False,
            "required": ["evidence"],
            "properties": {"evidence": {"type": "object"}, "idempotency_key": idempotency},
        },
        "knowledge_evaluate": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id"],
            "properties": {
                "node_id": required_node_id,
                "context_tags": tags,
                "at": {"type": ["string", "null"], "maxLength": 64},
                "include_descendants": {"type": "boolean"},
            },
        },
        "knowledge_review": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id"],
            "properties": {"node_id": required_node_id, "idempotency_key": idempotency},
        },
        "knowledge_propose_improvement": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id", "diff", "rationale"],
            "properties": {
                "node_id": required_node_id,
                "diff": {
                    "type": "array",
                    "minItems": 1,
                    "items": {
                        "type": "object",
                        "additionalProperties": False,
                        "required": ["op"],
                        "properties": {
                            "op": {"enum": _DIFF_OPS},
                            "key": {"type": "string", "maxLength": 256},
                            "value": True,
                        },
                    },
                },
                "rationale": {"type": "string", "minLength": 1, "maxLength": 1024},
                "base_version": {"type": ["integer", "null"], "minimum": 1},
                "idempotency_key": idempotency,
            },
        },
        "knowledge_apply_improvement": {
            "type": "object",
            "additionalProperties": False,
            "required": ["proposal_id"],
            "properties": {
                "proposal_id": {"type": "string", "minLength": 1, "maxLength": 256},
                "approval": {
                    "type": ["object", "null"],
                    "additionalProperties": False,
                    "required": ["approved_by", "decision"],
                    "properties": {
                        "approved_by": {"type": "string", "minLength": 1, "maxLength": 256},
                        "decision": {"enum": ["approved", "rejected"]},
                        "decided_at": {"type": ["string", "null"], "maxLength": 64},
                        "note": {"type": "string", "maxLength": 1024},
                    },
                },
                "idempotency_key": idempotency,
            },
        },
        "knowledge_rollback": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {
                "node_id": node_id,
                "to_version": {"type": ["integer", "null"], "minimum": 1},
                "relearn_id": {"type": ["string", "null"], "minLength": 1, "maxLength": 256},
                "idempotency_key": idempotency,
            },
        },
        # Contract-v2 G1 retrieval verbs (addendum retrieval-verbs-v2.md).
        "knowledge_path": {
            "type": "object",
            "additionalProperties": False,
            "required": ["from_id", "to_id"],
            "properties": {
                "from_id": required_node_id,
                "to_id": required_node_id,
                "token_budget": budget,
            },
        },
        "knowledge_explain": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id"],
            "properties": {"node_id": required_node_id, "token_budget": budget},
        },
        "knowledge_affected": {
            "type": "object",
            "additionalProperties": False,
            "required": ["node_id"],
            "properties": {
                "node_id": required_node_id,
                "depth": depth,
                "max_nodes": {"type": "integer", "minimum": 1, "maximum": 1024},
                "token_budget": budget,
            },
        },
        "knowledge_god_nodes": {
            "type": "object",
            "additionalProperties": False,
            "required": [],
            "properties": {
                "limit": {"type": "integer", "minimum": 1, "maximum": 64},
                "token_budget": budget,
            },
        },
    }


_DESCRIPTIONS: dict[str, str] = {
    "knowledge_query": "Retrieve a scoped, token-bounded primary-node context from the knowledge projection.",
    "knowledge_get_node": "Read one knowledge node from the active projection.",
    "knowledge_get_subtree": "Read a bounded subtree around one node from the active projection.",
    "knowledge_log_event": "Record structured content as a versioned node upsert.",
    "knowledge_record_outcome": "Append one outcome evidence item to an existing node.",
    "knowledge_evaluate": "Score one node deterministically (P/M/E with uncertainty).",
    "knowledge_review": "Run the deterministic lifecycle review for one node.",
    "knowledge_propose_improvement": "Persist a structured diff proposal pinned to a node version.",
    "knowledge_apply_improvement": "Apply a persisted proposal after approval and regression gates.",
    "knowledge_rollback": "Roll a node back to a prior version, or roll back a relearn cycle.",
    "knowledge_path": "Find the deterministic shortest directed path between two nodes (no_path is a typed result).",
    "knowledge_explain": "Explain one node's typed relational neighborhood (in/out links, per-relation counts).",
    "knowledge_affected": "List the nodes that depend on this one (bounded reverse walk, typed).",
    "knowledge_god_nodes": "List the top hub nodes by measured graph degree (deterministic, bounded).",
}


# ------------------------------------------------------------------ validation


def _invalid(property_name: str, reason: str) -> ToolError:
    return ToolError("INVALID_ARGUMENTS", f"property {property_name!r}: {reason}")


def _type_matches(value: Any, expected: str) -> bool:
    if expected == "string":
        return type(value) is str
    if expected == "integer":
        return type(value) is int  # a bool is not an integer here
    if expected == "boolean":
        return type(value) is bool
    if expected == "array":
        return type(value) is list
    if expected == "object":
        return type(value) is dict
    if expected == "null":
        return value is None
    return False  # pragma: no cover - the schemas above use only these types


def _check_property(name: str, value: Any, spec: Any) -> None:
    if spec is True or not isinstance(spec, Mapping):
        return
    expected = spec.get("type")
    if expected is not None:
        allowed = expected if isinstance(expected, list) else [expected]
        if not any(_type_matches(value, candidate) for candidate in allowed):
            raise _invalid(name, f"must be of type {'/'.join(str(item) for item in allowed)}")
    # Arch review F-2: the enum check precedes the null early-return — a null
    # must never bypass a closed vocabulary (enum-only specs like ``mode``
    # carry no null in their value set, so null fails here; nullable specs
    # pass the type check above and skip the rest below as before).
    if "enum" in spec and value not in spec["enum"]:
        raise _invalid(name, f"must be one of {spec['enum']}")
    if value is None:
        return
    if type(value) is int:
        if "minimum" in spec and value < spec["minimum"]:
            raise _invalid(name, f"must be >= {spec['minimum']}")
        if "maximum" in spec and value > spec["maximum"]:
            raise _invalid(name, f"must be <= {spec['maximum']}")
    if type(value) is str:
        if "minLength" in spec and len(value) < spec["minLength"]:
            raise _invalid(name, "must not be empty")
        if "maxLength" in spec and len(value) > spec["maxLength"]:
            raise _invalid(name, f"must be at most {spec['maxLength']} characters")
        if "pattern" in spec and not re.fullmatch(str(spec["pattern"]), value):
            raise _invalid(name, "must be a decimal string (never a binary float)")
    if type(value) is list:
        if "minItems" in spec and len(value) < spec["minItems"]:
            raise _invalid(name, f"must contain at least {spec['minItems']} item(s)")
        item_spec = spec.get("items")
        if item_spec is not None:
            for index, item in enumerate(value):
                _check_property(f"{name}[{index}]", item, item_spec)
    if type(value) is dict and "properties" in spec:
        _validate_object(name, spec, value)


def _validate_object(context: str, schema: Mapping[str, Any], value: Mapping[str, Any]) -> None:
    properties = schema.get("properties", {})
    unknown = sorted(str(key) for key in value if key not in properties)
    if unknown:
        raise ToolError("INVALID_ARGUMENTS", f"property {context!r}: unknown properties: {', '.join(unknown)}")
    missing = [key for key in schema.get("required", []) if key not in value]
    if missing:
        raise ToolError("INVALID_ARGUMENTS", f"property {context!r}: missing required properties: {', '.join(missing)}")
    for key, item in value.items():
        _check_property(f"{context}.{key}", item, properties[str(key)])


def _validate_arguments(tool: str, schema: Mapping[str, Any], arguments: Any) -> dict[str, Any]:
    if arguments is None:
        arguments = {}
    if type(arguments) is not dict:
        raise ToolError("INVALID_ARGUMENTS", f"tool {tool}: arguments must be a JSON object")
    properties = schema.get("properties", {})
    unknown = sorted(str(key) for key in arguments if key not in properties)
    if unknown:
        raise ToolError(
            "INVALID_ARGUMENTS",
            f"tool {tool}: unknown properties rejected: {', '.join(unknown)}"
            " (the graph root is fixed at startup; no path may be supplied)",
        )
    missing = [key for key in schema.get("required", []) if key not in arguments]
    if missing:
        raise ToolError("INVALID_ARGUMENTS", f"tool {tool}: missing required properties: {', '.join(missing)}")
    for key, value in arguments.items():
        _check_property(str(key), value, properties[str(key)])
    return dict(arguments)


def _validation_summary(error: ValidationError) -> str:
    """One line, no raw input values, no traceback."""
    errors = error.errors()
    if not errors:
        return "invalid structured payload"
    first = errors[0]
    location = ".".join(str(part) for part in first.get("loc", ()))
    message = str(first.get("msg", "invalid value"))
    return f"{location}: {message}" if location else message


# -------------------------------------------------------------------- adapter


def _model_payload(model: Any) -> dict[str, Any]:
    """Canonical JSON-safe payload of a pydantic response model.

    ``storage_payload`` renders Decimals as 12-decimal quantized strings and
    datetimes as ISO strings — the store serialization boundary (spec 9).
    """
    return json.loads(canonical_json(storage_payload(model.model_dump(mode="python"))))  # type: ignore[no-any-return]


def _error_text(code: str, message: str, audit_event_id: str | None) -> str:
    error: dict[str, Any] = {"code": code, "message": sanitize_text(message, max_length=512)}
    if audit_event_id is not None:
        error["audit_event_id"] = audit_event_id
    return canonical_json({"error": error})


class _Adapter:
    """Dispatch, validation, audit and resources for one fixed graph root."""

    def __init__(
        self,
        service: KnowledgeService,
        *,
        graph_root: Path,
        audit_log: AuditLog,
        actor: str,
        max_token_budget: int,
        resource_max_bytes: int,
    ) -> None:
        self._service = service
        self._root = graph_root
        self._audit = audit_log
        self._actor = actor
        self._schemas = _tool_schemas(max_token_budget)
        self._resource_max_bytes = resource_max_bytes

    # ------------------------------------------------------------------ audit

    def _audit_rejection(self, operation: str, arguments: Any, code: str, started: float) -> str:
        """Exactly one final audit event for an adapter-rejected invocation.

        CLI parity: a request that never reaches the service is still a
        completed invocation with a typed failure (spec section 7.7).
        """
        try:
            canonical = canonical_json(arguments) if isinstance(arguments, (dict, list)) else "null"
        except TypeError, ValueError:  # pragma: no cover - defensive
            canonical = "null"
        invocation = Invocation.create(
            operation,
            request_hash=hashlib.sha256(canonical.encode("utf-8")).hexdigest(),
            actor=self._actor,
        )
        self._audit.begin(invocation)
        receipt = self._audit.complete(
            invocation.invocation_id,
            InvocationOutcome(
                status="failure",
                error=code,
                duration_ms=int((time.monotonic() - started) * 1000),
                correlation_id=invocation.invocation_id,
            ),
        )
        return receipt.audit_event_id

    # ------------------------------------------------------------------ tools

    def list_tools(self) -> types.ListToolsResult:
        return types.ListToolsResult(
            tools=[
                types.Tool(
                    name=name,
                    description=_DESCRIPTIONS[name],
                    input_schema=self._schemas[name],
                )
                for name in EXPECTED_TOOLS
            ]
        )

    async def call_tool(self, name: str, arguments: Any) -> types.CallToolResult:
        started = time.monotonic()
        operation = _TOOL_TO_OPERATION.get(name)
        if operation is None:
            audit_event_id = self._audit_rejection("mcp.unknown_tool", {"tool": name}, "UNKNOWN_TOOL", started)
            return self._error_result("UNKNOWN_TOOL", f"unknown tool {name!r}", audit_event_id)
        try:
            validated = _validate_arguments(name, self._schemas[name], arguments)
        except ToolError as error:
            audit_event_id = self._audit_rejection(operation, arguments, error.code, started)
            return self._error_result(error.code, str(error), audit_event_id)
        try:
            payload = self._dispatch(name, validated)
        except ToolError as error:
            audit_event_id = error.audit_event_id
            if audit_event_id is None:
                audit_event_id = self._audit_rejection(operation, arguments, error.code, started)
            return self._error_result(error.code, str(error), audit_event_id)
        except KnowledgeError as error:
            # The service already wrote the single final failure audit event.
            return self._error_result(error.code, str(error), error.audit_event_id)
        except StoreError as error:
            # ``store.commit`` writes the failure audit before re-raising.
            return self._error_result(type(error).__name__, str(error), None)
        except ProjectionError as error:
            # Final-audit F-5: projection contract violations (busy, shrink
            # guard, oversized artifact) are typed, never INTERNAL_ERROR.
            return self._error_result(type(error).__name__, str(error), None)
        except Exception:  # noqa: BLE001 - never leak raw exception text
            return self._error_result("INTERNAL_ERROR", "the operation failed with an internal error", None)
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=canonical_json(payload))],
            is_error=False,
        )

    @staticmethod
    def _error_result(code: str, message: str, audit_event_id: str | None) -> types.CallToolResult:
        return types.CallToolResult(
            content=[types.TextContent(type="text", text=_error_text(code, message, audit_event_id))],
            is_error=True,
        )

    # --------------------------------------------------------------- dispatch

    def _dispatch(self, name: str, args: dict[str, Any]) -> dict[str, Any]:
        handler = getattr(self, f"_op_{name.removeprefix('knowledge_')}")
        return handler(args)  # type: ignore[no-any-return]

    def _op_query(self, args: dict[str, Any]) -> dict[str, Any]:
        allowlist = args.get("relation_allowlist")
        request = QueryRequest(
            purpose=args["purpose"],
            domain=args.get("domain"),
            context_tags=tuple(args.get("context_tags", ())),
            token_budget=args["token_budget"],
            access_only=args.get("access_only", False),
            depth=args.get("depth", 2),
            mode=TraversalMode(args.get("mode", "bfs")),
            relation_allowlist=tuple(RelationKind(item) for item in allowlist) if allowlist is not None else None,
            max_nodes=args.get("max_nodes", 64),
            max_edges=args.get("max_edges", 256),
        )
        return self._service.query(request, actor=self._actor).canonical_payload()

    def _op_get_node(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._service.get_node(args["node_id"], actor=self._actor).canonical_payload()

    def _op_get_subtree(self, args: dict[str, Any]) -> dict[str, Any]:
        allowlist = args.get("relation_allowlist")
        return self._service.get_subtree(
            args["node_id"],
            depth=args.get("depth", 2),
            mode=args.get("mode", "bfs"),
            relation_allowlist=frozenset(allowlist) if allowlist is not None else None,
            max_nodes=args.get("max_nodes", 64),
            max_edges=args.get("max_edges", 256),
            token_budget=args.get("token_budget", 1024),
            actor=self._actor,
        ).canonical_payload()

    def _op_log_event(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            node = Node(**args["node"])
        except ValidationError as error:
            raise ToolError("INVALID_ARGUMENTS", f"property 'node': {_validation_summary(error)}") from None
        receipt = self._service.log_event(
            node,
            actor=self._actor,
            idempotency_key=args.get("idempotency_key"),
            expected_version=args.get("expected_version"),
        )
        return _model_payload(receipt)

    def _op_record_outcome(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            evidence = Evidence(**args["evidence"])
        except ValidationError as error:
            raise ToolError("INVALID_ARGUMENTS", f"property 'evidence': {_validation_summary(error)}") from None
        receipt = self._service.record_outcome(
            evidence,
            actor=self._actor,
            idempotency_key=args.get("idempotency_key"),
        )
        return _model_payload(receipt)

    def _op_evaluate(self, args: dict[str, Any]) -> dict[str, Any]:
        at = args.get("at")
        parsed_at: datetime | None = None
        if at is not None:
            try:
                parsed_at = datetime.fromisoformat(at)
            except ValueError:
                raise ToolError("INVALID_ARGUMENTS", "property 'at': must be an ISO-8601 datetime string") from None
        request = EvaluateRequest(
            node_id=args["node_id"],
            context_tags=tuple(args.get("context_tags", ())),
            at=parsed_at,
            include_descendants=args.get("include_descendants", True),
        )
        return _model_payload(self._service.evaluate(request, actor=self._actor))

    # The four Task 6 operations call the wired service methods per the
    # task-6-report.md handoff; their service signatures are owned by the
    # Task 4-6 wiring commit.

    def _op_review(self, args: dict[str, Any]) -> dict[str, Any]:
        report = self._service.review(
            args["node_id"],
            actor=self._actor,
            idempotency_key=args.get("idempotency_key"),
        )
        return _model_payload(report)

    def _op_propose_improvement(self, args: dict[str, Any]) -> dict[str, Any]:
        try:
            diff = [DiffOperation(**item) for item in args["diff"]]
        except ValidationError as error:
            raise ToolError("INVALID_ARGUMENTS", f"property 'diff': {_validation_summary(error)}") from None
        record = self._service.propose_improvement(
            node_id=args["node_id"],
            diff=diff,
            rationale=args["rationale"],
            base_version=args.get("base_version"),
            actor=self._actor,
            idempotency_key=args.get("idempotency_key"),
        )
        return _model_payload(record)

    def _op_apply_improvement(self, args: dict[str, Any]) -> dict[str, Any]:
        approval_arg = args.get("approval")
        approval: ApprovalRecord | None = None
        if approval_arg is not None:
            decided_at = approval_arg.get("decided_at")
            try:
                approval = ApprovalRecord(
                    proposal_id=args["proposal_id"],
                    approved_by=approval_arg["approved_by"],
                    decision=approval_arg["decision"],
                    decided_at=datetime.fromisoformat(decided_at) if decided_at else datetime.now(UTC),
                    note=approval_arg.get("note", ""),
                )
            except ValidationError, ValueError:
                raise ToolError("INVALID_ARGUMENTS", "property 'approval': invalid approval record") from None
        receipt = self._service.apply_improvement(
            proposal_id=args["proposal_id"],
            approval=approval,
            actor=self._actor,
            idempotency_key=args.get("idempotency_key"),
        )
        return _model_payload(receipt)

    def _op_rollback(self, args: dict[str, Any]) -> dict[str, Any]:
        # Arch review F-2: the cross-field rule mirrors RollbackRequest's
        # model validator — exactly one target, and to_version only with a
        # node target — at the adapter, like every other MCP validation.
        node_id = args.get("node_id")
        relearn_id = args.get("relearn_id")
        if (node_id is None) == (relearn_id is None):
            raise ToolError(
                "INVALID_ARGUMENTS",
                "knowledge_rollback requires exactly one of 'node_id' (improvement) or 'relearn_id' (relearn rollback)",
            )
        if relearn_id is not None and args.get("to_version") is not None:
            raise ToolError(
                "INVALID_ARGUMENTS",
                "property 'to_version': only applies to an improvement rollback (with 'node_id')",
            )
        receipt = self._service.rollback(
            node_id=node_id,
            to_version=args.get("to_version"),
            relearn_id=relearn_id,
            actor=self._actor,
            idempotency_key=args.get("idempotency_key"),
        )
        return _model_payload(receipt)

    # The four contract-v2 G1 retrieval verbs (addendum
    # docs/contracts/retrieval-verbs-v2.md): read-only, audited, canonical
    # payload parity with the CLI/API.

    def _op_path(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._service.path(
            args["from_id"],
            args["to_id"],
            token_budget=args.get("token_budget", 1024),
            actor=self._actor,
        ).canonical_payload()

    def _op_explain(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._service.explain(
            args["node_id"],
            token_budget=args.get("token_budget", 1024),
            actor=self._actor,
        ).canonical_payload()

    def _op_affected(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._service.affected(
            args["node_id"],
            depth=args.get("depth", 2),
            max_nodes=args.get("max_nodes", 256),
            token_budget=args.get("token_budget", 1024),
            actor=self._actor,
        ).canonical_payload()

    def _op_god_nodes(self, args: dict[str, Any]) -> dict[str, Any]:
        return self._service.god_nodes(
            limit=args.get("limit", 10),
            token_budget=args.get("token_budget", 1024),
            actor=self._actor,
        ).canonical_payload()

    # --------------------------------------------------------------- resources

    def list_resources(self) -> types.ListResourcesResult:
        return types.ListResourcesResult(
            resources=[
                types.Resource(uri=uri, name=name, description=description, mime_type="application/json")
                for uri, name, description in (
                    ("soul://projection/manifest", "projection-manifest", "Sanitized active projection manifest."),
                    ("soul://projection/stats", "projection-stats", "Sanitized node/edge counts and watermarks."),
                    ("soul://projection/nodes", "projection-nodes", "Sanitized, size-capped node summaries."),
                    (
                        "soul://relearn/status",
                        "relearn-status",
                        "Relearn gate: pending offer plus read-only RESET/ACCUMULATE preview counts.",
                    ),
                )
            ]
        )

    def _relearn_status_payload(self) -> dict[str, Any]:
        """The pending relearn offer and both mode previews, read-only (F-4).

        Final-audit F-4: the operator decision needs the ``offer_id`` and the
        per-mode consequences visible; MCP exposes exactly that, read-only —
        the approval itself is an operator act on the CLI/HTTP ops surface,
        never an eleventh tool (spec 13.1/13.2). Evidence id lists stay on
        the ops surface; this resource carries counts only, so it stays well
        under the size cap. Status and previews ride the service's public
        relearn gate (each an audited read invocation).
        """
        status = self._service.relearn_status(actor=self._actor)
        payload: dict[str, Any] = {
            "relearn_state": status.state,
            "pending_offer": status.pending_offer.model_dump(mode="json") if status.pending_offer else None,
            "active_source_version_id": status.active_source_version_id,
        }
        if status.pending_offer is None:
            return payload
        modes: tuple[Literal["RESET", "ACCUMULATE"], ...] = ("RESET", "ACCUMULATE")
        previews: dict[str, Any] = {}
        for mode in modes:
            preview = self._service.preview_relearn(mode, actor=self._actor)
            previews[mode] = {
                "evidence_carried": preview.evidence_carried,
                "evidence_quarantined": preview.evidence_quarantined,
                "nodes_reused": preview.nodes_reused,
                "scores_after": preview.scores_after,
                "promotion_blocked": preview.promotion_blocked,
            }
        payload["previews"] = previews
        return payload

    def _active_manifest(self) -> dict[str, Any]:
        manifest = self._service._projector.active_manifest()  # noqa: SLF001 - same-package wiring
        if manifest is None:
            raise MCPError(types.INVALID_PARAMS, "no active projection")
        return dict(manifest)

    def _nodes_payload(self, manifest: Mapping[str, Any], nodes: list[dict[str, Any]]) -> dict[str, Any]:
        entries = [
            {
                "node_id": sanitize_text(str(node["id"]), max_length=256),
                "summary": sanitize_text(str(node.get("label") or node["id"]), max_length=256),
                "domain": sanitize_text(str(node.get("domain", "")), max_length=64),
                "kind": sanitize_text(str(node.get("kind", "")), max_length=64),
            }
            for node in sorted(nodes, key=lambda item: str(item.get("id", "")))
            if isinstance(node.get("id"), str)
        ]
        total = len(entries)
        while True:
            payload = {
                "graph_content_hash": manifest["graph_content_hash"],
                "projection_version": manifest["projection_version"],
                "returned": len(entries),
                "total": total,
                "truncated": len(entries) < total,
                "nodes": entries,
            }
            if len(canonical_json(payload).encode("utf-8")) <= self._resource_max_bytes or not entries:
                return payload
            entries = entries[:-1]

    def _resource_payload(self, uri: str) -> dict[str, Any]:
        if uri == "soul://relearn/status":
            return self._relearn_status_payload()
        manifest = self._active_manifest()
        if uri == "soul://projection/manifest":
            return manifest
        generation = self._service._projector.active_generation_dir()  # noqa: SLF001 - same-package wiring
        if generation is None:
            raise MCPError(types.INVALID_PARAMS, "no active projection")
        graph_bytes = (generation / "graph.json").read_bytes()
        if hashlib.sha256(graph_bytes).hexdigest() != manifest["graph_content_hash"]:
            raise MCPError(types.INVALID_PARAMS, "stale projection generation")
        graph = json.loads(graph_bytes.decode("utf-8"))
        nodes = [node for node in graph.get("nodes", []) if isinstance(node, dict)]
        if uri == "soul://projection/stats":
            return {
                "graph_content_hash": manifest["graph_content_hash"],
                "projection_version": manifest["projection_version"],
                "source_knowledge_high_watermark": manifest["source_knowledge_high_watermark"],
                "node_count": len(nodes),
                "edge_count": len(graph.get("edges", graph.get("links", [])) or []),
            }
        return self._nodes_payload(manifest, nodes)

    async def read_resource(self, uri: str) -> types.ReadResourceResult:
        if uri not in _RESOURCE_URIS:
            # A fixed resource set: no traversal, no foreign schemes, no paths.
            raise MCPError(types.INVALID_PARAMS, f"unknown resource {uri!r}")
        payload = redact_value(self._resource_payload(uri))
        text = canonical_json(payload)
        size = len(text.encode("utf-8"))
        if size > self._resource_max_bytes:
            raise MCPError(types.INVALID_PARAMS, f"resource {uri!r} exceeds the {self._resource_max_bytes}-byte cap")
        return types.ReadResourceResult(
            contents=[types.TextResourceContents(uri=uri, mime_type="application/json", text=text)]
        )


# -------------------------------------------------------------------- builders


def build_server(
    service: KnowledgeService,
    *,
    graph_root: str | Path,
    audit_log: AuditLog | None = None,
    actor: str = "mcp-client",
    max_token_budget: int = DEFAULT_MAX_TOKEN_BUDGET,
    resource_max_bytes: int = DEFAULT_RESOURCE_MAX_BYTES,
) -> Server:
    """Bind the fourteen operations to one allowlisted graph root (resolved once)."""
    root = resolve_within_root(graph_root, ".")
    adapter = _Adapter(
        service,
        graph_root=root,
        audit_log=audit_log if audit_log is not None else AuditLog(service._store),  # noqa: SLF001
        actor=actor,
        max_token_budget=max_token_budget,
        resource_max_bytes=resource_max_bytes,
    )

    async def _on_list_tools(context: Any, params: Any) -> types.ListToolsResult:
        return adapter.list_tools()

    async def _on_call_tool(context: Any, params: types.CallToolRequestParams) -> types.CallToolResult:
        return await adapter.call_tool(params.name, params.arguments)

    async def _on_list_resources(context: Any, params: Any) -> types.ListResourcesResult:
        return adapter.list_resources()

    async def _on_read_resource(context: Any, params: types.ReadResourceRequestParams) -> types.ReadResourceResult:
        return await adapter.read_resource(str(params.uri))

    try:
        from importlib.metadata import version

        server_version = version("know-your-soul")
    except Exception:  # noqa: BLE001 - the version is informational only
        server_version = "0.0.0"

    server = Server(
        SERVER_NAME,
        version=server_version,
        on_list_tools=_on_list_tools,
        on_call_tool=_on_call_tool,
        on_list_resources=_on_list_resources,
        on_read_resource=_on_read_resource,
    )
    # Fail closed at construction: the registry must never carry a forbidden
    # handler, no matter what the SDK defaults change to.
    runtime_guard.assert_no_sampling_handler(server)
    return server


def build_service(
    *,
    graph_root: str | Path,
    db: str | Path,
    max_token_budget: int = DEFAULT_MAX_TOKEN_BUDGET,
) -> KnowledgeService:
    """Construct the service stack bound to the allowlisted graph root."""
    root = resolve_within_root(graph_root, ".")
    store = EventStore(Path(db))
    projector = GraphProjector(store, output_root=root)
    return KnowledgeService(store, projector, max_token_budget=max_token_budget)


# ------------------------------------------------------------------------ main


async def _serve_stdio(server: Server) -> None:
    from mcp.server.stdio import stdio_server

    async with stdio_server() as (read_stream, write_stream):
        await server.run(read_stream, write_stream, server.create_initialization_options(), True)


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="know-your-soul-mcp",
        description="Deterministic knowledge MCP server (tools+resources only, no sampling).",
    )
    parser.add_argument("--graph-root", required=True, help="The single allowlisted projection root.")
    parser.add_argument("--db", required=True, help="Path to the soul SQLite event store.")
    parser.add_argument("--transport", choices=("stdio", "http"), default="stdio")
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8080)
    key = parser.add_mutually_exclusive_group()
    key.add_argument(
        "--api-key",
        default=None,
        help="API key for the http transport (WARNING: visible in argv; prefer --api-key-env)",
    )
    key.add_argument(
        "--api-key-env",
        default=None,
        metavar="NAME",
        help="read the API key from environment variable NAME (keeps it out of argv)",
    )
    parser.add_argument("--max-token-budget", type=int, default=DEFAULT_MAX_TOKEN_BUDGET)
    args = parser.parse_args(argv)

    try:
        service = build_service(graph_root=args.graph_root, db=args.db, max_token_budget=args.max_token_budget)
        server = build_server(service, graph_root=args.graph_root, max_token_budget=args.max_token_budget)
        # The server refuses to run with provider credentials in its own
        # environment (contract section 3.1 item 4): Hermes never sends them,
        # and a direct launch with a dirty environment fails closed.
        runtime_guard.verify_runtime(mcp_server=server, strict_env=True)
    except Exception as error:  # noqa: BLE001 - any startup guard failure refuses to serve
        print(f"know-your-soul-mcp: startup refused: {type(error).__name__}: {error}", file=sys.stderr)
        return 2

    # The socket guard stays installed for the process lifetime: no code path
    # may open an outbound connection. The import guard is proven at startup
    # by ``verify_runtime`` but not left installed: the pinned Graphify
    # distribution lazily probes optional extras with ``except ImportError``
    # fallbacks, and the guard's deliberate non-ImportError denial must not
    # break those allowed-surface fallbacks.
    runtime_guard.install_socket_guard()

    if args.transport == "http":
        # Wave-2 F6: a named env var keeps the key out of process argv.
        api_key: str | None = args.api_key
        if args.api_key_env is not None:
            api_key = os.environ.get(args.api_key_env) or None
            if api_key is None:
                print(
                    f"know-your-soul-mcp: environment variable {args.api_key_env} is not set or is empty",
                    file=sys.stderr,
                )
                return 2
        try:
            assert_bind_allowed(f"{args.host}:{args.port}", api_key=api_key)
        except Exception as error:  # noqa: BLE001
            print(f"know-your-soul-mcp: bind refused: {error}", file=sys.stderr)
            return 2
        import uvicorn

        # Wave-2 F3: the streamable-HTTP app is wrapped in the same boundary
        # middleware as the Task 7 API — Host allowlist plus per-request API
        # key authentication (a configured key gates loopback too). Final-audit
        # S-6: boundary denials append the same aggregated tamper-evident audit
        # events as the HTTP API, through the service's own store.
        denial_recorder = BoundaryDenialRecorder(service._store)  # noqa: SLF001 - same-package wiring
        app = BoundaryMiddleware(
            server.streamable_http_app(),
            api_key=api_key,
            host_allowlist=DEFAULT_HOST_ALLOWLIST,
            max_body_bytes=DEFAULT_MAX_PAYLOAD_BYTES,
            denial_recorder=denial_recorder.record,
        )
        uvicorn.run(app, host=args.host, port=args.port, log_level="warning")
        return 0

    import anyio

    anyio.run(_serve_stdio, server)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

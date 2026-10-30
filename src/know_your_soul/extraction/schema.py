"""Intake validation for the contract-v4 extraction verb (SUB-106).

A strict, closed-schema validator for ``knowledge.submit_extraction``
arguments: every rejection is a typed :class:`ExtractionError` from the
contract's error vocabulary, every free-text field is sanitized **before**
hashing or storage (control/bidi/zero-width characters stripped, HTML
escaped), and every cap is fail-closed. The validator never mutates the
caller's arguments.

Determinism: the sanitized canonical payload, its sha256, the method
fingerprint, the idempotency key and the receipt id are pure functions of the
arguments — two validations of the same input are byte-identical.
"""

from __future__ import annotations

import hashlib
import html
import re
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal, InvalidOperation
from pathlib import Path
from typing import Any

from know_your_soul.domain import NodeKind, PrimaryDomain, canonical_json
from know_your_soul.extraction.errors import (
    ConfidenceNotDecimalError,
    ConfidenceOutOfRangeError,
    ExtractionError,
    MethodSchemaError,
    PayloadSchemaError,
    PayloadTooLargeError,
)

__all__ = [
    "MAX_EDGES",
    "MAX_EVIDENCE_CHARS",
    "MAX_METHOD_FIELD_CHARS",
    "MAX_NAME_CHARS",
    "MAX_NODES",
    "MAX_SUBMISSION_BYTES",
    "MAX_SUMMARY_CHARS",
    "MAX_TEXT_CHARS",
    "ExtractionPayload",
    "MethodDescriptor",
    "ProposedEdge",
    "ProposedNode",
    "Submission",
    "sanitize_field",
    "sha256_file",
    "validate_submission",
]

MAX_TEXT_CHARS = 262_144
MAX_NODES = 4_096
MAX_EDGES = 8_192
MAX_NAME_CHARS = 512
MAX_SUMMARY_CHARS = 4_096
MAX_EVIDENCE_CHARS = 1_024
MAX_METHOD_FIELD_CHARS = 256
MAX_SUBMISSION_BYTES = 1_048_576
MAX_CONFIDENCE_CHARS = 64

_TOP_LEVEL_KEYS = frozenset({"source_sha256", "source_path", "payload", "method", "confidence"})
_PAYLOAD_KEYS = frozenset({"text", "nodes", "edges"})
_NODE_KEYS = frozenset({"name", "kind", "domain", "summary", "span"})
_SPAN_KEYS = frozenset({"line", "col"})
_EDGE_KEYS = frozenset({"relation", "source", "target", "evidence"})
_ENDPOINT_KEYS = frozenset({"kind", "name"})
_METHOD_KEYS = frozenset({"agent", "extracted_at", "prompt_id", "tool"})

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_CONFIDENCE_RE = re.compile(r"^-?[0-9]{1,2}(\.[0-9]{1,18})?$")
_RELATION_VALUES = frozenset(
    {
        "parent_of",
        "derived_from",
        "supports",
        "contradicts",
        "causes",
        "learned_from",
        "applied_in",
        "supersedes",
        "duplicates",
        "depends_on",
    }
)
_KIND_VALUES = frozenset(kind.value for kind in NodeKind)
_DOMAIN_VALUES = frozenset(domain.value for domain in PrimaryDomain)


def sanitize_field(value: str) -> str:
    """Strip control/bidi/zero-width characters, then HTML-escape.

    The result is inert under rendering (review diffs, webview, applied node
    summaries) and deterministic — the payload hash binds this form.
    """
    cleaned = "".join(char for char in value if unicodedata.category(char) not in {"Cc", "Cf"})
    return html.escape(cleaned, quote=True)


def sha256_file(path: Path) -> str:
    """The sha256 of a file's bytes, read in bounded chunks."""
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for chunk in iter(lambda: handle.read(65536), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _unknown(context: str, value: Any, allowed: frozenset[str], error: type[ExtractionError]) -> None:
    if not isinstance(value, dict):
        raise error(f"{context} must be an object")
    extras = sorted(str(key) for key in value if key not in allowed)
    if extras:
        raise error(f"{context}: unknown properties rejected: {', '.join(extras)}")


def _require(context: str, value: dict[str, Any], keys: tuple[str, ...], error: type[ExtractionError]) -> None:
    missing = [key for key in keys if key not in value]
    if missing:
        raise error(f"{context}: missing required properties: {', '.join(missing)}")


def _str_field(
    context: str,
    value: Any,
    *,
    max_chars: int,
    min_chars: int = 0,
    error: type[ExtractionError],
    cap_error: type[ExtractionError] = PayloadTooLargeError,
) -> str:
    if type(value) is not str:
        raise error(f"{context} must be a string")
    if len(value) < min_chars:
        raise error(f"{context} must not be empty")
    if len(value) > max_chars:
        raise cap_error(f"{context} exceeds the {max_chars}-character cap")
    return value


# ------------------------------------------------------------------ payload


@dataclass(frozen=True)
class ProposedNode:
    """One proposed node record (sanitized)."""

    name: str
    kind: str
    domain: str
    summary: str = ""
    span: tuple[int, int | None] | None = None

    @property
    def key(self) -> tuple[str, str]:
        return (self.kind, self.name)

    def canonical(self) -> dict[str, Any]:
        result: dict[str, Any] = {"domain": self.domain, "kind": self.kind, "name": self.name, "summary": self.summary}
        if self.span is not None:
            result["span"] = {"line": self.span[0], "col": self.span[1]}
        return result


@dataclass(frozen=True)
class ProposedEdge:
    """One proposed typed link between payload node keys (sanitized)."""

    relation: str
    source: tuple[str, str]
    target: tuple[str, str]
    evidence: str = ""

    def canonical(self) -> dict[str, Any]:
        result: dict[str, Any] = {
            "relation": self.relation,
            "source": {"kind": self.source[0], "name": self.source[1]},
            "target": {"kind": self.target[0], "name": self.target[1]},
        }
        if self.evidence:
            result["evidence"] = self.evidence
        return result


@dataclass(frozen=True)
class ExtractionPayload:
    """The sanitized proposed payload: free text plus nodes and edges."""

    text: str
    nodes: tuple[ProposedNode, ...]
    edges: tuple[ProposedEdge, ...]

    def canonical(self) -> dict[str, Any]:
        return {
            "text": self.text,
            "nodes": [node.canonical() for node in self.nodes],
            "edges": [edge.canonical() for edge in self.edges],
        }

    def sha256(self) -> str:
        return hashlib.sha256(canonical_json(self.canonical()).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class MethodDescriptor:
    """How the external agent produced the extraction."""

    agent: str
    extracted_at: datetime
    prompt_id: str = ""
    tool: str = ""

    def fingerprint(self) -> str:
        """sha256 over ``{agent, prompt_id, tool}`` — ``extracted_at`` excluded."""
        canonical = canonical_json({"agent": self.agent, "prompt_id": self.prompt_id, "tool": self.tool})
        return hashlib.sha256(canonical.encode("utf-8")).hexdigest()

    def canonical(self) -> dict[str, Any]:
        return {
            "agent": self.agent,
            "extracted_at": self.extracted_at.isoformat(),
            "prompt_id": self.prompt_id,
            "tool": self.tool,
        }


@dataclass(frozen=True)
class Submission:
    """One validated, sanitized ``submit_extraction`` invocation."""

    source_sha256: str
    source_path: str | None
    payload: ExtractionPayload
    method: MethodDescriptor
    confidence: Decimal

    @property
    def payload_sha256(self) -> str:
        return self.payload.sha256()

    def idem_key(self, actor: str) -> str:
        """The locked idempotency key: ``(source_sha256, actor, method)``."""
        return f"{self.source_sha256}|{actor}|{self.method.fingerprint()}"

    def receipt_id(self, actor: str) -> str:
        """The content-derived receipt id for one (key, payload) pair."""
        preimage = f"{self.idem_key(actor)}|{self.payload_sha256}"
        return "ext-" + hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:32]

    def canonical(self) -> dict[str, Any]:
        return {
            "confidence": str(self.confidence),
            "method": self.method.canonical(),
            "payload": self.payload.canonical(),
            "source_path": self.source_path,
            "source_sha256": self.source_sha256,
        }


# ---------------------------------------------------------------- validation


def _parse_confidence(value: Any) -> Decimal:
    if type(value) is not str:
        raise ConfidenceNotDecimalError("confidence must be a decimal string (never a binary float)")
    if len(value) > MAX_CONFIDENCE_CHARS or _CONFIDENCE_RE.fullmatch(value) is None:
        raise ConfidenceNotDecimalError(f"confidence {value!r} is not a plain decimal string")
    try:
        parsed = Decimal(value)
    except InvalidOperation as error:  # pragma: no cover - the regex already guards this
        raise ConfidenceNotDecimalError(f"confidence {value!r} is not a decimal") from error
    if value.startswith("-") or parsed > 1:
        raise ConfidenceOutOfRangeError(f"confidence {value!r} is outside [0, 1]")
    return parsed


def _parse_endpoint(context: str, value: Any) -> tuple[str, str]:
    _unknown(context, value, _ENDPOINT_KEYS, PayloadSchemaError)
    _require(context, value, ("kind", "name"), PayloadSchemaError)
    kind = _str_field(f"{context}.kind", value["kind"], max_chars=64, min_chars=1, error=PayloadSchemaError)
    name = _str_field(f"{context}.name", value["name"], max_chars=MAX_NAME_CHARS, min_chars=1, error=PayloadSchemaError)
    return (sanitize_field(kind), sanitize_field(name))


def _parse_node(index: int, value: Any) -> ProposedNode:
    context = f"payload.nodes[{index}]"
    _unknown(context, value, _NODE_KEYS, PayloadSchemaError)
    _require(context, value, ("name", "domain"), PayloadSchemaError)
    name = _str_field(f"{context}.name", value["name"], max_chars=MAX_NAME_CHARS, min_chars=1, error=PayloadSchemaError)
    kind = value.get("kind", "leaf")
    if type(kind) is not str or kind not in _KIND_VALUES:
        raise PayloadSchemaError(f"{context}.kind must be one of {sorted(_KIND_VALUES)}")
    domain = value["domain"]
    if type(domain) is not str or domain not in _DOMAIN_VALUES:
        raise PayloadSchemaError(f"{context}.domain must be one of {sorted(_DOMAIN_VALUES)}")
    summary = _str_field(
        f"{context}.summary", value.get("summary", ""), max_chars=MAX_SUMMARY_CHARS, error=PayloadSchemaError
    )
    span: tuple[int, int | None] | None = None
    if value.get("span") is not None:
        raw_span = value["span"]
        _unknown(f"{context}.span", raw_span, _SPAN_KEYS, PayloadSchemaError)
        _require(f"{context}.span", raw_span, ("line",), PayloadSchemaError)
        line = raw_span["line"]
        col = raw_span.get("col")
        if isinstance(line, bool) or not isinstance(line, int) or line < 1:
            raise PayloadSchemaError(f"{context}.span.line must be an integer >= 1")
        if col is not None and (isinstance(col, bool) or not isinstance(col, int) or col < 0):
            raise PayloadSchemaError(f"{context}.span.col must be an integer >= 0")
        span = (line, col)
    return ProposedNode(
        name=sanitize_field(name),
        kind=kind,
        domain=domain,
        summary=sanitize_field(summary),
        span=span,
    )


def _parse_edge(index: int, value: Any, node_keys: frozenset[tuple[str, str]]) -> ProposedEdge:
    context = f"payload.edges[{index}]"
    _unknown(context, value, _EDGE_KEYS, PayloadSchemaError)
    _require(context, value, ("relation", "source", "target"), PayloadSchemaError)
    relation = value["relation"]
    if type(relation) is not str or relation not in _RELATION_VALUES:
        raise PayloadSchemaError(f"{context}.relation must be one of {sorted(_RELATION_VALUES)}")
    source = _parse_endpoint(f"{context}.source", value["source"])
    target = _parse_endpoint(f"{context}.target", value["target"])
    for label, endpoint in (("source", source), ("target", target)):
        if endpoint not in node_keys:
            raise PayloadSchemaError(
                f"{context}.{label} references {endpoint[1]!r} ({endpoint[0]}), which is not a node of this payload"
            )
    evidence = _str_field(
        f"{context}.evidence", value.get("evidence", ""), max_chars=MAX_EVIDENCE_CHARS, error=PayloadSchemaError
    )
    return ProposedEdge(relation=relation, source=source, target=target, evidence=sanitize_field(evidence))


def _parse_payload(value: Any) -> ExtractionPayload:
    _unknown("payload", value, _PAYLOAD_KEYS, PayloadSchemaError)
    _require("payload", value, ("text", "nodes", "edges"), PayloadSchemaError)
    text = _str_field("payload.text", value["text"], max_chars=MAX_TEXT_CHARS, error=PayloadSchemaError)
    nodes_raw = value["nodes"]
    edges_raw = value["edges"]
    if not isinstance(nodes_raw, list):
        raise PayloadSchemaError("payload.nodes must be a list")
    if not isinstance(edges_raw, list):
        raise PayloadSchemaError("payload.edges must be a list")
    if len(nodes_raw) > MAX_NODES:
        raise PayloadTooLargeError(f"payload.nodes exceeds the {MAX_NODES}-item cap")
    if len(edges_raw) > MAX_EDGES:
        raise PayloadTooLargeError(f"payload.edges exceeds the {MAX_EDGES}-item cap")
    nodes = tuple(_parse_node(index, item) for index, item in enumerate(nodes_raw))
    keys: set[tuple[str, str]] = set()
    for node in nodes:
        if node.key in keys:
            raise PayloadSchemaError(
                f"payload.nodes carries a duplicate (kind, name) key: {node.key[1]!r} ({node.key[0]})"
            )
        keys.add(node.key)
    edges = tuple(_parse_edge(index, item, frozenset(keys)) for index, item in enumerate(edges_raw))
    return ExtractionPayload(text=sanitize_field(text), nodes=nodes, edges=edges)


def _parse_method(value: Any) -> MethodDescriptor:
    _unknown("method", value, _METHOD_KEYS, MethodSchemaError)
    _require("method", value, ("agent", "extracted_at"), MethodSchemaError)
    agent = _str_field(
        "method.agent",
        value["agent"],
        max_chars=MAX_METHOD_FIELD_CHARS,
        min_chars=1,
        error=MethodSchemaError,
        cap_error=MethodSchemaError,
    )
    extracted_raw = _str_field(
        "method.extracted_at",
        value["extracted_at"],
        max_chars=64,
        min_chars=1,
        error=MethodSchemaError,
        cap_error=MethodSchemaError,
    )
    try:
        extracted_at = datetime.fromisoformat(extracted_raw)
    except ValueError as error:
        raise MethodSchemaError("method.extracted_at must be an ISO-8601 datetime string") from error
    prompt_id = _str_field(
        "method.prompt_id",
        value.get("prompt_id", ""),
        max_chars=MAX_METHOD_FIELD_CHARS,
        error=MethodSchemaError,
        cap_error=MethodSchemaError,
    )
    tool = _str_field(
        "method.tool",
        value.get("tool", ""),
        max_chars=MAX_METHOD_FIELD_CHARS,
        error=MethodSchemaError,
        cap_error=MethodSchemaError,
    )
    return MethodDescriptor(agent=agent, extracted_at=extracted_at, prompt_id=prompt_id, tool=tool)


def validate_submission(arguments: Any) -> Submission:
    """Validate one ``submit_extraction`` argument object (closed, sanitized).

    Never mutates *arguments*; every rejection is a typed error from the
    contract's vocabulary.
    """
    if not isinstance(arguments, dict):
        raise PayloadSchemaError("submit_extraction arguments must be a JSON object")
    _unknown("submit_extraction", arguments, _TOP_LEVEL_KEYS, PayloadSchemaError)
    _require("submit_extraction", arguments, ("source_sha256", "payload", "method", "confidence"), PayloadSchemaError)
    source_sha256 = arguments["source_sha256"]
    if type(source_sha256) is not str or _SHA256_RE.fullmatch(source_sha256) is None:
        raise PayloadSchemaError("source_sha256 must be 64 lowercase hex characters")
    source_path = arguments.get("source_path")
    if source_path is not None and (type(source_path) is not str or not source_path):
        raise PayloadSchemaError("source_path must be a non-empty string when present")
    payload = _parse_payload(arguments["payload"])
    method = _parse_method(arguments["method"])
    confidence = _parse_confidence(arguments["confidence"])
    submission = Submission(
        source_sha256=source_sha256,
        source_path=source_path,
        payload=payload,
        method=method,
        confidence=confidence,
    )
    encoded = canonical_json(submission.canonical()).encode("utf-8")
    if len(encoded) > MAX_SUBMISSION_BYTES:
        raise PayloadTooLargeError(
            f"the canonical submission is {len(encoded)} bytes, over the {MAX_SUBMISSION_BYTES}-byte cap"
        )
    return submission


def submission_from_canonical(document: Mapping[str, Any]) -> Submission:
    """Rebuild a Submission from its stored canonical form (queue round-trip).

    The stored form is already sanitized, so this parses without re-escaping:
    fields are trusted as the validator's own output.
    """
    payload_raw = document["payload"]
    method_raw = document["method"]
    nodes = tuple(
        ProposedNode(
            name=str(node["name"]),
            kind=str(node["kind"]),
            domain=str(node["domain"]),
            summary=str(node.get("summary", "")),
            span=(
                (int(node["span"]["line"]), None if node["span"].get("col") is None else int(node["span"]["col"]))
                if node.get("span")
                else None
            ),
        )
        for node in payload_raw["nodes"]
    )
    edges = tuple(
        ProposedEdge(
            relation=str(edge["relation"]),
            source=(str(edge["source"]["kind"]), str(edge["source"]["name"])),
            target=(str(edge["target"]["kind"]), str(edge["target"]["name"])),
            evidence=str(edge.get("evidence", "")),
        )
        for edge in payload_raw["edges"]
    )
    method = MethodDescriptor(
        agent=str(method_raw["agent"]),
        extracted_at=datetime.fromisoformat(str(method_raw["extracted_at"])),
        prompt_id=str(method_raw.get("prompt_id", "")),
        tool=str(method_raw.get("tool", "")),
    )
    return Submission(
        source_sha256=str(document["source_sha256"]),
        source_path=document.get("source_path"),
        payload=ExtractionPayload(text=str(payload_raw["text"]), nodes=nodes, edges=edges),
        method=method,
        confidence=Decimal(str(document["confidence"])),
    )

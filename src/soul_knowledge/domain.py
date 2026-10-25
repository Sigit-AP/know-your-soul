"""Versioned domain schema: seven primary domains, nodes, evidence and events.

Task 2 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``;
spec sections 6 and 9.

Design rules:

* Decimals only. Every scoring field is a :class:`decimal.Decimal`; a binary
  ``float`` is refused at validation time so no IEEE-754 rounding can enter the
  stored knowledge (spec section 9, "no binary-float conversion").
* Provenance is mandatory. Every node and every evidence item carries a
  ``source_version_id``; there is no default, so an import that forgets it fails
  closed instead of persisting unversioned knowledge (spec section 13.3).
* Relations are a closed allowlist. An unknown relation kind is a validation
  error, not a new edge type (spec section 6).
* Models are frozen and forbid extra fields, so a caller cannot smuggle a
  provider/model setting into a knowledge record.
"""

from __future__ import annotations

import json
import uuid
from collections.abc import Mapping
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal, InvalidOperation
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

__all__ = [
    "DECIMAL_PRECISION",
    "PRIMARY_DOMAINS",
    "SCORE_QUANTUM",
    "Event",
    "EventKind",
    "Evidence",
    "LifecycleStatus",
    "Node",
    "NodeKind",
    "PrimaryDomain",
    "Relation",
    "RelationKind",
    "ScoreVector",
    "canonical_json",
    "quantize_score",
]

DECIMAL_PRECISION: int = 34
SCORE_QUANTUM: Decimal = Decimal("1E-12")
ZERO = Decimal(0)
ONE = Decimal(1)
MINUS_ONE = Decimal(-1)
TEN = Decimal(10)


def _to_decimal(value: object) -> Decimal:
    """Interpret *value* as a Decimal without ever going through a binary float."""
    if isinstance(value, bool):
        raise ValueError("a boolean is not a decimal score")
    if isinstance(value, float):
        raise ValueError("binary floats are not permitted in knowledge records; pass a decimal string")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except InvalidOperation as error:  # pragma: no cover - defensive
            raise ValueError(f"{value!r} is not a decimal") from error
    raise ValueError(f"cannot interpret {type(value).__name__} as a Decimal")


StrictDecimal = Annotated[Decimal, BeforeValidator(_to_decimal)]


def quantize_score(value: Decimal) -> Decimal:
    """Quantize a score to 12 decimal places with ``ROUND_HALF_EVEN`` (spec section 9)."""
    return value.quantize(SCORE_QUANTUM, rounding=ROUND_HALF_EVEN)


def canonical_json(value: object) -> str:
    """Deterministic one-line JSON used for hashing and storage."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def new_id() -> str:
    """A fresh opaque identifier (UUID4 hex)."""
    return uuid.uuid4().hex


def _utcnow() -> datetime:
    return datetime.now(UTC)


# ------------------------------------------------------------------ domains


class PrimaryDomain(StrEnum):
    """Exactly seven stable primary domains (spec section 6)."""

    P1 = "P1"
    P2 = "P2"
    P3 = "P3"
    P4 = "P4"
    P5 = "P5"
    P6 = "P6"
    P7 = "P7"

    @property
    def title(self) -> str:
        return {
            PrimaryDomain.P1: "Pembelajaran",
            PrimaryDomain.P2: "Gagasan",
            PrimaryDomain.P3: "Tindakan",
            PrimaryDomain.P4: "Keputusan",
            PrimaryDomain.P5: "Pemikiran/Penalaran",
            PrimaryDomain.P6: "Disiplin",
            PrimaryDomain.P7: "Level Profesional",
        }[self]


PRIMARY_DOMAINS: tuple[PrimaryDomain, ...] = tuple(PrimaryDomain)


class NodeKind(StrEnum):
    """Position in the knowledge hierarchy (spec section 6)."""

    ROOT = "root"
    PRIMARY = "primary"
    CHILD = "child"
    BRANCH = "branch"
    LEAF = "leaf"


class LifecycleStatus(StrEnum):
    """Lifecycle status vocabulary (spec sections 6 and 10)."""

    DRAFT = "draft"
    ACTIVE = "active"
    MAINTAIN = "maintain"
    OBSERVE = "observe"
    NEEDS_IMPROVEMENT = "needs_improvement"
    READY_TO_PROMOTE = "ready_to_promote"
    MERGE_OR_DEDUPLICATE = "merge_or_deduplicate"
    DEPRECATE = "deprecate"
    SUPERSEDED = "superseded"
    ROLLBACK = "rollback"


class RelationKind(StrEnum):
    """Closed allowlist of supported relations (spec section 6)."""

    PARENT_OF = "parent_of"
    DERIVED_FROM = "derived_from"
    SUPPORTS = "supports"
    CONTRADICTS = "contradicts"
    CAUSES = "causes"
    LEARNED_FROM = "learned_from"
    APPLIED_IN = "applied_in"
    SUPERSEDES = "supersedes"
    DUPLICATES = "duplicates"
    DEPENDS_ON = "depends_on"


# -------------------------------------------------------------------- models


class Relation(BaseModel):
    """A directed, typed edge between two nodes with an optional validity window."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    kind: RelationKind
    source_node_id: str = Field(min_length=1)
    target_node_id: str = Field(min_length=1)
    valid_from: datetime | None = None
    valid_to: datetime | None = None


class ScoreVector(BaseModel):
    """P/M/E plus uncertainty, confidence and the professional level.

    ``P``, ``M`` and ``E`` remain separately visible (spec section 9); nothing
    here is a composite score.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    p: StrictDecimal = Field(default=ZERO, ge=ZERO, le=ONE)
    m: StrictDecimal = Field(default=ZERO, ge=ZERO, le=ONE)
    e: StrictDecimal = Field(default=ZERO, ge=ZERO, le=ONE)
    uncertainty: StrictDecimal = Field(default=ONE, ge=ZERO, le=ONE)
    confidence: StrictDecimal = Field(default=ZERO, ge=ZERO, le=ONE)
    professional_level: int = Field(default=1, ge=1, le=5)
    selected_m_bin: str | None = None

    @classmethod
    def zero(cls) -> ScoreVector:
        """The zero-evidence vector: ``P=M=E=0``, uncertainty 1, level 1 (spec section 9)."""
        return cls()

    def quantized(self) -> dict[str, str]:
        """The 12-decimal serialization of the score fields."""
        return {
            "p": str(quantize_score(self.p)),
            "m": str(quantize_score(self.m)),
            "e": str(quantize_score(self.e)),
            "uncertainty": str(quantize_score(self.uncertainty)),
            "confidence": str(quantize_score(self.confidence)),
        }


class Evidence(BaseModel):
    """One scored observation with full provenance (spec section 9)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    evidence_id: str = Field(min_length=1)
    node_id: str = Field(min_length=1)
    source_version_id: str = Field(min_length=1)
    source_uri: str = Field(min_length=1)
    observed_at: datetime
    context_tags: tuple[str, ...] = ()
    independence_key: str = Field(min_length=1)
    outcome: StrictDecimal = Field(ge=MINUS_ONE, le=ONE)
    correctness: StrictDecimal = Field(ge=ZERO, le=ONE)
    reliability: StrictDecimal = Field(ge=ZERO, le=ONE)
    reproducibility: StrictDecimal = Field(ge=ZERO, le=ONE)
    scope_fit: StrictDecimal = Field(ge=ZERO, le=ONE)
    successful_use: StrictDecimal = Field(ge=ZERO, le=ONE)
    task_coverage: StrictDecimal = Field(ge=ZERO, le=ONE)
    quality_at_use: StrictDecimal = Field(ge=ZERO, le=ONE)
    contradiction: StrictDecimal = Field(ge=ZERO, le=ONE)
    utilization_u: StrictDecimal
    latency_ms: int = Field(gt=0)
    compute_ms: int = Field(gt=0)
    output_chars: int = Field(ge=0)

    @field_validator("utilization_u")
    @classmethod
    def _utilization_is_a_bin(cls, value: Decimal) -> Decimal:
        scaled = value * TEN
        if scaled != scaled.to_integral_value() or not (ONE <= scaled <= TEN):
            raise ValueError("utilization_u must be one of 0.1, 0.2, ..., 1.0")
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_timezone_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("observed_at must be timezone-aware UTC")
        return value


class Node(BaseModel):
    """One immutable node revision (spec section 6)."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    parent_id: str | None = None
    kind: NodeKind = NodeKind.LEAF
    domain: PrimaryDomain
    payload: Mapping[str, Any] = Field(default_factory=dict)
    summary: str = ""
    provenance: tuple[str, ...] = ()
    context_tags: tuple[str, ...] = ()
    scope: str = ""
    created_at: datetime = Field(default_factory=_utcnow)
    updated_at: datetime = Field(default_factory=_utcnow)
    last_used_at: datetime | None = None
    evidence_ids: tuple[str, ...] = ()
    plus: tuple[str, ...] = ()
    minus: tuple[str, ...] = ()
    scores: ScoreVector = Field(default_factory=ScoreVector.zero)
    lifecycle_status: LifecycleStatus = LifecycleStatus.ACTIVE
    relations: tuple[Relation, ...] = ()
    version: int = Field(default=1, ge=1)
    source_version_id: str = Field(min_length=1)

    @model_validator(mode="before")
    @classmethod
    def _fold_professional_level(cls, data: object) -> object:
        """Accept ``professional_level`` as sugar for ``scores.professional_level``."""
        if isinstance(data, Mapping) and "professional_level" in data:
            mutable = dict(data)
            level = mutable.pop("professional_level")
            scores = mutable.get("scores")
            if isinstance(scores, ScoreVector):
                mutable["scores"] = scores.model_copy(update={"professional_level": level})
            else:
                merged = dict(scores) if isinstance(scores, Mapping) else {}
                merged["professional_level"] = level
                mutable["scores"] = merged
            return mutable
        return data

    @property
    def professional_level(self) -> int:
        return self.scores.professional_level


class EventKind(StrEnum):
    """Immutable domain facts."""

    NODE_UPSERT = "node.upsert"
    NODE_RETIRE = "node.retire"
    EVIDENCE_APPEND = "evidence.append"
    RELATION_ADD = "relation.add"
    ADMIN = "admin"


class Event(BaseModel):
    """A single immutable domain fact about one node.

    Only knowledge-affecting events receive a ``knowledge_seq``; an event with
    ``knowledge_affecting=False`` (administrative/relearn/backup records) shares
    the global ``event_seq`` but never moves the knowledge watermark.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    event_id: str = Field(default_factory=new_id, min_length=1)
    kind: EventKind
    node_id: str = Field(min_length=1)
    domain: PrimaryDomain
    node: Node | None = None
    evidence: Evidence | None = None
    relation: Relation | None = None
    source_version_id: str | None = None
    actor: str = "operator"
    occurred_at: datetime = Field(default_factory=_utcnow)
    knowledge_affecting: bool = True
    payload: Mapping[str, Any] = Field(default_factory=dict)

    @model_validator(mode="after")
    def _payload_matches_the_kind(self) -> Event:
        if self.kind is EventKind.NODE_UPSERT and self.node is None:
            raise ValueError("a node.upsert event requires a node payload")
        if self.kind is EventKind.EVIDENCE_APPEND and self.evidence is None:
            raise ValueError("an evidence.append event requires an evidence payload")
        if self.kind is EventKind.RELATION_ADD and self.relation is None:
            raise ValueError("a relation.add event requires a relation payload")
        if self.node is not None and self.node.node_id != self.node_id:
            raise ValueError("event node_id must match the embedded node's node_id")
        if self.evidence is not None and self.evidence.node_id != self.node_id:
            raise ValueError("event node_id must match the embedded evidence's node_id")
        return self

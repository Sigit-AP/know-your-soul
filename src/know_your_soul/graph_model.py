"""Universal knowledge graph model v2 (wave W1, spec: MASTER-PLAN "Universal Knowledge Graph").

A pure, deterministic, in-memory domain model. No I/O, no clock dependence in
derived outputs, no model/provider calls. Persistence belongs to the storage
wave (schema v3); this module owns only the shapes and invariants.

Design rules:

* Node kinds are an **open-forward** registry: the frozen :class:`CoreNodeKind`
  enum carries the knowledge hierarchy (``root``/``primary``/``child``/
  ``branch``/``leaf``), the code kinds (``file``/``module``/``class``/
  ``function``/``method``/``variable``/``constant``/``interface``/``type``/
  ``import``) and the document kinds (``document``/``section``/``page``/
  ``paragraph``); future kinds are added with :func:`register_node_kind`
  (append-only, validated, idempotent). Same for edge kinds.
* Node ids follow codegraph: ``kind:sha256(path:kind:name:line)[:col]`` with a
  32-hex-char truncated digest. ``line`` is optional (knowledge nodes carry no
  line); the preimage omits the line segment entirely when absent. File root
  nodes use the codegraph-native ``file:<path>`` form.
* Hierarchy is expressed as ``contains`` **edges**, never columns, rooted at a
  source node (``file``/``document``). Arbitrary depth emerges from the edge
  chain. Invariants are enforced eagerly and fail closed: one ``contains``
  parent per node, no ``contains`` cycles.
* Primary ("ibu") nodes are first-class: many primaries, each with exactly one
  domain and one non-empty purpose. Primaries interlink through ``bridges``
  edges — symmetric (stored canonically), weighted, reason-bearing, and only
  between primaries.
* :func:`propose_primary_splits` and :func:`review_candidates` are
  **proposal-only**: they inspect a subtree and return deterministic,
  reason-bearing proposals; they never mutate the graph.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal
from enum import StrEnum
from typing import Annotated, Any

from pydantic import BaseModel, BeforeValidator, ConfigDict, Field, field_validator, model_validator

from know_your_soul.domain import PrimaryDomain, ScoreVector, canonical_json, quantize_score, storage_payload

__all__ = [
    "CORE_EDGE_KINDS",
    "CORE_NODE_KINDS",
    "SYMMETRIC_EDGE_KINDS",
    "BridgeError",
    "ContainsCycleError",
    "CoreEdgeKind",
    "CoreNodeKind",
    "DuplicateEdgeError",
    "DuplicateNodeError",
    "EdgeKindError",
    "GraphEdge",
    "GraphModelError",
    "GraphNode",
    "MultipleParentsError",
    "NodeIdError",
    "NodeIdParts",
    "NodeKindError",
    "PrimaryError",
    "ReviewCandidate",
    "ReviewPolicy",
    "SplitProposal",
    "SplitThresholds",
    "UniversalGraph",
    "UnknownNodeError",
    "all_edge_kinds",
    "all_node_kinds",
    "file_node_id",
    "is_edge_kind",
    "is_file_node_id",
    "is_node_kind",
    "make_node_id",
    "parse_node_id",
    "propose_primary_splits",
    "register_edge_kind",
    "register_node_kind",
    "review_candidates",
]

GRAPH_MODEL_VERSION: int = 2

_KIND_PATTERN = re.compile(r"[a-z][a-z0-9_]{0,31}")
_HASH_PATTERN = re.compile(r"[0-9a-f]{64}")
_NODE_ID_PATTERN = re.compile(r"^(?P<kind>[a-z][a-z0-9_]{0,31}):(?P<digest>[0-9a-f]{32})(?::(?P<col>[0-9]+))?$")
_FILE_ID_PREFIX = "file:"


# -------------------------------------------------------------------- errors


class GraphModelError(Exception):
    """Base class for every error raised by the graph model."""


class NodeKindError(GraphModelError, ValueError):
    """A node kind is malformed or unregistered."""


class EdgeKindError(GraphModelError, ValueError):
    """An edge kind is malformed or unregistered."""


class NodeIdError(GraphModelError, ValueError):
    """A node id cannot be built or parsed."""


class DuplicateNodeError(GraphModelError):
    """A node id already exists with different content."""


class DuplicateEdgeError(GraphModelError):
    """An edge identity already exists with different content."""


class UnknownNodeError(GraphModelError):
    """A referenced node id is not in the graph."""


class MultipleParentsError(GraphModelError):
    """A ``contains`` edge would give a node a second parent."""


class ContainsCycleError(GraphModelError):
    """A ``contains`` edge would close a cycle."""


class BridgeError(GraphModelError, ValueError):
    """A ``bridges`` edge violates the primary-to-primary rules."""


class PrimaryError(GraphModelError):
    """An operation requires a primary node but got something else."""


# ---------------------------------------------------------------- node kinds


class CoreNodeKind(StrEnum):
    """Frozen core node kinds: knowledge hierarchy + code + document."""

    # knowledge hierarchy (kept from the v1 domain model)
    ROOT = "root"
    PRIMARY = "primary"
    CHILD = "child"
    BRANCH = "branch"
    LEAF = "leaf"
    # code kinds
    FILE = "file"
    MODULE = "module"
    CLASS = "class"
    FUNCTION = "function"
    METHOD = "method"
    VARIABLE = "variable"
    CONSTANT = "constant"
    INTERFACE = "interface"
    TYPE = "type"
    IMPORT = "import"
    # document kinds
    DOCUMENT = "document"
    SECTION = "section"
    PAGE = "page"
    PARAGRAPH = "paragraph"


CORE_NODE_KINDS: tuple[str, ...] = tuple(sorted(kind.value for kind in CoreNodeKind))

_extra_node_kinds: set[str] = set()


def _validate_kind_syntax(kind: str, error_type: type[NodeKindError] | type[EdgeKindError]) -> str:
    if not isinstance(kind, str) or not _KIND_PATTERN.fullmatch(kind):
        raise error_type(f"kind {kind!r} must match {_KIND_PATTERN.pattern}")
    return kind


def register_node_kind(kind: str) -> None:
    """Register a custom node kind (append-only, idempotent).

    Re-registering an existing kind (core or custom) is a no-op so callers
    can safely re-assert their vocabulary on startup.
    """
    _validate_kind_syntax(kind, NodeKindError)
    if kind in CORE_NODE_KINDS:
        return
    _extra_node_kinds.add(kind)


def is_node_kind(value: object) -> bool:
    return isinstance(value, str) and (value in CORE_NODE_KINDS or value in _extra_node_kinds)


def all_node_kinds() -> tuple[str, ...]:
    """Every known node kind, sorted for deterministic iteration."""
    return tuple(sorted(set(CORE_NODE_KINDS) | _extra_node_kinds))


# ---------------------------------------------------------------- edge kinds


class CoreEdgeKind(StrEnum):
    """Frozen core edge kinds: hierarchy, bridges and the codegraph relations."""

    CONTAINS = "contains"
    BRIDGES = "bridges"
    CALLS = "calls"
    IMPORTS = "imports"
    EXPORTS = "exports"
    EXTENDS = "extends"
    IMPLEMENTS = "implements"
    REFERENCES = "references"
    TYPE_OF = "type_of"
    RETURNS = "returns"
    INSTANTIATES = "instantiates"
    OVERRIDES = "overrides"
    DECORATES = "decorates"
    NAVIGATES = "navigates"


CORE_EDGE_KINDS: tuple[str, ...] = tuple(sorted(kind.value for kind in CoreEdgeKind))

#: Edge kinds whose (source, target) direction carries no meaning; the graph
#: stores them canonically with ``source < target``.
SYMMETRIC_EDGE_KINDS: frozenset[str] = frozenset({"bridges"})

_extra_edge_kinds: set[str] = set()


def register_edge_kind(kind: str) -> None:
    """Register a custom edge kind (append-only, idempotent)."""
    _validate_kind_syntax(kind, EdgeKindError)
    if kind in CORE_EDGE_KINDS:
        return
    _extra_edge_kinds.add(kind)


def is_edge_kind(value: object) -> bool:
    return isinstance(value, str) and (value in CORE_EDGE_KINDS or value in _extra_edge_kinds)


def all_edge_kinds() -> tuple[str, ...]:
    return tuple(sorted(set(CORE_EDGE_KINDS) | _extra_edge_kinds))


# -------------------------------------------------------------------- node id


def _check_int(value: object, name: str, minimum: int) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise NodeIdError(f"{name} must be an int, got {type(value).__name__}")
    if value < minimum:
        raise NodeIdError(f"{name} must be >= {minimum}, got {value}")
    return value


def make_node_id(*, path: str, kind: str, name: str, line: int | None = None, col: int | None = None) -> str:
    """Build a stable codegraph-style id: ``kind:sha256(path:kind:name:line)[:col]``.

    The digest preimage joins the segments with ``:``; when ``line`` is
    ``None`` (knowledge nodes) the line segment is omitted entirely. ``col``
    is appended to the id only to disambiguate same-(path, kind, name, line)
    declarations and requires ``line``.
    """
    if not is_node_kind(kind):
        raise NodeKindError(f"unregistered node kind {kind!r}")
    if not isinstance(name, str) or not name:
        raise NodeIdError("name must be a non-empty string")
    if not isinstance(path, str):
        raise NodeIdError("path must be a string")
    if line is not None:
        _check_int(line, "line", 1)
    if col is not None:
        if line is None:
            raise NodeIdError("col requires line")
        _check_int(col, "col", 0)
    segments = [path, kind, name] + ([str(line)] if line is not None else [])
    preimage = ":".join(segments)
    digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:32]
    node_id = f"{kind}:{digest}"
    if col is not None:
        node_id = f"{node_id}:{col}"
    return node_id


def file_node_id(path: str) -> str:
    """The codegraph-native id of a source-file root node: ``file:<path>``."""
    if not isinstance(path, str) or not path:
        raise NodeIdError("file node id requires a non-empty path")
    return f"{_FILE_ID_PREFIX}{path}"


def is_file_node_id(node_id: str) -> bool:
    return node_id.startswith(_FILE_ID_PREFIX) and len(node_id) > len(_FILE_ID_PREFIX)


@dataclass(frozen=True)
class NodeIdParts:
    """The parsed components of a hash-based node id."""

    kind: str
    digest: str
    col: int | None


def parse_node_id(node_id: str) -> NodeIdParts:
    """Parse a hash-based node id; file ids (``file:<path>``) are rejected here."""
    match = _NODE_ID_PATTERN.match(node_id)
    if match is None:
        raise NodeIdError(f"malformed node id {node_id!r}")
    col_text = match.group("col")
    return NodeIdParts(kind=match.group("kind"), digest=match.group("digest"), col=int(col_text) if col_text else None)


# --------------------------------------------------------------------- models


def _strict_decimal(value: object, name: str) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise GraphModelError(f"{name} must be a Decimal/int/str, never a binary float")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        try:
            return Decimal(value)
        except ArithmeticError as error:
            raise GraphModelError(f"{name}: {value!r} is not a decimal") from error
    raise GraphModelError(f"{name}: cannot interpret {type(value).__name__} as a Decimal")


def _to_decimal(value: object) -> Decimal:
    if isinstance(value, bool) or isinstance(value, float):
        raise ValueError("binary floats are not permitted; pass a decimal string")
    if isinstance(value, Decimal):
        return value
    if isinstance(value, int):
        return Decimal(value)
    if isinstance(value, str):
        return Decimal(value)
    raise ValueError(f"cannot interpret {type(value).__name__} as a Decimal")


StrictDecimal = Annotated[Decimal, BeforeValidator(_to_decimal)]


def _utcnow() -> datetime:
    return datetime.now(UTC)


class GraphNode(BaseModel):
    """One node of the universal graph.

    The ``node_id`` must be the deterministic id derived from
    (``path``, ``kind``, ``name``, ``line``, ``col``) — or ``file:<path>`` for
    ``file`` roots — so identity is stable and rename-safe per location.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    node_id: str = Field(min_length=1)
    kind: str
    name: str = Field(min_length=1)
    path: str = ""
    line: int | None = Field(default=None, ge=1)
    col: int | None = Field(default=None, ge=0)
    domain: PrimaryDomain | None = None
    purpose: str = ""
    summary: str = ""
    scores: ScoreVector = Field(default_factory=ScoreVector.zero)
    content_hash: str | None = None
    reviewed_hash: str | None = None
    watermark: int | None = Field(default=None, ge=0)
    updated_at: datetime = Field(default_factory=_utcnow)
    payload: Mapping[str, Any] = Field(default_factory=dict)

    @field_validator("kind")
    @classmethod
    def _kind_is_registered(cls, value: str) -> str:
        if not is_node_kind(value):
            raise NodeKindError(f"unregistered node kind {value!r}")
        return value

    @field_validator("content_hash", "reviewed_hash")
    @classmethod
    def _hash_is_sha256_hex(cls, value: str | None) -> str | None:
        if value is not None and not _HASH_PATTERN.fullmatch(value):
            raise ValueError("hash fields must be 64 lowercase hex chars (sha256)")
        return value

    @field_validator("updated_at")
    @classmethod
    def _updated_at_is_aware(cls, value: datetime) -> datetime:
        if value.tzinfo is None:
            raise ValueError("updated_at must be timezone-aware")
        return value

    @model_validator(mode="after")
    def _id_matches_fields(self) -> GraphNode:
        if self.col is not None and self.line is None:
            raise ValueError("col requires line")
        if self.kind == CoreNodeKind.FILE.value:
            if not self.path:
                raise ValueError("a file node requires a path")
            expected = file_node_id(self.path)
        else:
            expected = make_node_id(path=self.path, kind=self.kind, name=self.name, line=self.line, col=self.col)
        if self.node_id != expected:
            raise ValueError(f"node_id {self.node_id!r} does not match the deterministic id {expected!r}")
        if self.kind == CoreNodeKind.PRIMARY.value:
            if self.domain is None:
                raise ValueError("a primary node requires exactly one domain")
            if not self.purpose:
                raise ValueError("a primary node requires a non-empty purpose (single responsibility)")
        return self


class GraphEdge(BaseModel):
    """A directed, typed edge. ``bridges`` edges carry a weight and a reason."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    source: str = Field(min_length=1)
    target: str = Field(min_length=1)
    kind: str
    weight: StrictDecimal | None = None
    reason: str = ""
    metadata: Mapping[str, Any] = Field(default_factory=dict)
    line: int | None = Field(default=None, ge=1)
    col: int | None = Field(default=None, ge=0)

    @field_validator("kind")
    @classmethod
    def _kind_is_registered(cls, value: str) -> str:
        if not is_edge_kind(value):
            raise EdgeKindError(f"unregistered edge kind {value!r}")
        return value

    @model_validator(mode="after")
    def _bridge_and_position_rules(self) -> GraphEdge:
        if self.col is not None and self.line is None:
            raise ValueError("col requires line")
        if self.weight is not None and not (Decimal(0) <= self.weight <= Decimal(1)):
            raise ValueError("weight must be within [0, 1]")
        if self.kind == CoreEdgeKind.BRIDGES.value:
            if self.weight is None:
                raise ValueError("a bridges edge requires a weight")
            if not self.reason:
                raise ValueError("a bridges edge requires a non-empty reason")
        return self


# ------------------------------------------------------------ graph container


def _edge_key(edge: GraphEdge) -> tuple[str, str, str, int, int]:
    return (
        edge.source,
        edge.target,
        edge.kind,
        edge.line if edge.line is not None else -1,
        edge.col if edge.col is not None else -1,
    )


class UniversalGraph:
    """A deterministic in-memory node/edge container with eager invariant checks."""

    def __init__(self, nodes: Iterable[GraphNode] = (), edges: Iterable[GraphEdge] = ()) -> None:
        self._nodes: dict[str, GraphNode] = {}
        self._edges: dict[tuple[str, str, str, int, int], GraphEdge] = {}
        for node in nodes:
            self.add_node(node)
        for edge in edges:
            self.add_edge(edge)

    # -- nodes -------------------------------------------------------------

    @property
    def node_count(self) -> int:
        return len(self._nodes)

    @property
    def edge_count(self) -> int:
        return len(self._edges)

    def has_node(self, node_id: str) -> bool:
        return node_id in self._nodes

    def get_node(self, node_id: str) -> GraphNode:
        try:
            return self._nodes[node_id]
        except KeyError:
            raise UnknownNodeError(f"unknown node {node_id!r}") from None

    def nodes(self) -> tuple[GraphNode, ...]:
        """All nodes, sorted by id (deterministic)."""
        return tuple(self._nodes[node_id] for node_id in sorted(self._nodes))

    def add_node(self, node: GraphNode) -> None:
        existing = self._nodes.get(node.node_id)
        if existing is not None:
            if existing != node:
                raise DuplicateNodeError(f"node {node.node_id!r} already exists with different content")
            return
        self._nodes[node.node_id] = node

    # -- edges -------------------------------------------------------------

    def edges(self) -> tuple[GraphEdge, ...]:
        """All edges, sorted by (source, target, kind, line, col)."""
        return tuple(self._edges[key] for key in sorted(self._edges))

    def add_edge(self, edge: GraphEdge) -> GraphEdge:
        """Insert an edge, enforcing the ``contains`` forest invariants eagerly."""
        for endpoint in (edge.source, edge.target):
            if endpoint not in self._nodes:
                raise UnknownNodeError(f"edge endpoint {endpoint!r} is not in the graph")
        if edge.kind in SYMMETRIC_EDGE_KINDS and edge.source > edge.target:
            edge = edge.model_copy(update={"source": edge.target, "target": edge.source})
        key = _edge_key(edge)
        existing = self._edges.get(key)
        if existing is not None:
            if existing != edge:
                raise DuplicateEdgeError(f"edge {key!r} already exists with different content")
            return existing
        if edge.kind == CoreEdgeKind.CONTAINS.value:
            self._check_contains(edge)
        elif edge.kind == CoreEdgeKind.BRIDGES.value:
            self._check_bridge(edge)
        self._edges[key] = edge
        return edge

    def add_bridge(
        self,
        source: str,
        target: str,
        *,
        weight: Decimal | int | str,
        reason: str,
        metadata: Mapping[str, Any] | None = None,
    ) -> GraphEdge:
        """Add a symmetric weighted bridge between two primary nodes."""
        edge = GraphEdge(
            source=source,
            target=target,
            kind=CoreEdgeKind.BRIDGES.value,
            weight=_strict_decimal(weight, "weight"),
            reason=reason,
            metadata={} if metadata is None else dict(metadata),
        )
        return self.add_edge(edge)

    def _check_contains(self, edge: GraphEdge) -> None:
        if edge.source == edge.target:
            raise ContainsCycleError(f"contains self-loop on {edge.source!r}")
        parent = self.contains_parent(edge.target)
        if parent is not None:
            raise MultipleParentsError(
                f"node {edge.target!r} already has contains parent {parent!r}; cannot add {edge.source!r}"
            )
        # walk up from the proposed source: if target is an ancestor, this closes a cycle
        seen = {edge.target}
        cursor: str | None = edge.source
        while cursor is not None:
            if cursor in seen:
                raise ContainsCycleError(f"contains edge {edge.source!r} -> {edge.target!r} would close a cycle")
            seen.add(cursor)
            cursor = self.contains_parent(cursor)

    def _check_bridge(self, edge: GraphEdge) -> None:
        if edge.source == edge.target:
            raise BridgeError("a bridges edge cannot be a self-loop")
        for endpoint in (edge.source, edge.target):
            if self._nodes[endpoint].kind != CoreNodeKind.PRIMARY.value:
                raise BridgeError(f"bridges edges connect primary nodes only; {endpoint!r} is not primary")

    # -- hierarchy queries -------------------------------------------------

    def contains_parent(self, node_id: str) -> str | None:
        for edge in self._edges.values():
            if edge.kind == CoreEdgeKind.CONTAINS.value and edge.target == node_id:
                return edge.source
        return None

    def contains_children(self, node_id: str) -> tuple[str, ...]:
        children = [
            edge.target
            for edge in self._edges.values()
            if edge.kind == CoreEdgeKind.CONTAINS.value and edge.source == node_id
        ]
        return tuple(sorted(children))

    def contains_root(self, node_id: str) -> str:
        """The root of the ``contains`` tree holding ``node_id`` (itself if parentless)."""
        self.get_node(node_id)
        cursor = node_id
        while True:
            parent = self.contains_parent(cursor)
            if parent is None:
                return cursor
            cursor = parent

    def depth(self, node_id: str) -> int:
        """Number of ``contains`` hops from ``node_id`` up to its tree root."""
        self.get_node(node_id)
        depth = 0
        cursor = node_id
        while True:
            parent = self.contains_parent(cursor)
            if parent is None:
                return depth
            depth += 1
            cursor = parent

    def subtree_ids(self, root_id: str) -> tuple[str, ...]:
        """BFS over ``contains`` from ``root_id`` (inclusive), deterministic order."""
        self.get_node(root_id)
        order: list[str] = []
        queue = [root_id]
        seen = {root_id}
        while queue:
            current = queue.pop(0)
            order.append(current)
            for child in self.contains_children(current):
                if child not in seen:
                    seen.add(child)
                    queue.append(child)
        return tuple(order)

    # -- primaries / bridges -----------------------------------------------

    def primaries(self) -> tuple[str, ...]:
        """Ids of all primary nodes, sorted."""
        return tuple(sorted(nid for nid, node in self._nodes.items() if node.kind == CoreNodeKind.PRIMARY.value))

    def bridges_of(self, node_id: str) -> tuple[GraphEdge, ...]:
        """All ``bridges`` edges touching ``node_id``, sorted by the other endpoint."""
        self.get_node(node_id)
        found = [
            edge
            for edge in self._edges.values()
            if edge.kind == CoreEdgeKind.BRIDGES.value and node_id in (edge.source, edge.target)
        ]
        return tuple(sorted(found, key=lambda e: (e.target if e.source == node_id else e.source, e.source, e.target)))

    # -- integrity ---------------------------------------------------------

    def validate(self) -> None:
        """Re-check the global invariants; raises on the first violation.

        Invariants are enforced eagerly at insertion, so a passing ``validate``
        is a proof, not a repair step.
        """
        parents: dict[str, str] = {}
        for edge in self._edges.values():
            for endpoint in (edge.source, edge.target):
                if endpoint not in self._nodes:
                    raise UnknownNodeError(f"edge endpoint {endpoint!r} is not in the graph")
            if edge.kind != CoreEdgeKind.CONTAINS.value:
                continue
            if edge.target in parents:
                raise MultipleParentsError(f"node {edge.target!r} has multiple contains parents")
            parents[edge.target] = edge.source
        for start in parents:
            seen: set[str] = set()
            cursor: str | None = start
            while cursor is not None:
                if cursor in seen:
                    raise ContainsCycleError(f"contains cycle detected at {cursor!r}")
                seen.add(cursor)
                cursor = parents.get(cursor)

    # -- canonical form ------------------------------------------------------

    def canonical_dump(self) -> str:
        """A byte-stable canonical JSON dump (nodes sorted by id, edges sorted by key)."""
        payload = {
            "graph_model_version": GRAPH_MODEL_VERSION,
            "nodes": [storage_payload(node.model_dump(mode="python")) for node in self.nodes()],
            "edges": [storage_payload(edge.model_dump(mode="python")) for edge in self.edges()],
        }
        return canonical_json(payload)


# ------------------------------------------------------------ split proposals


@dataclass(frozen=True)
class SplitThresholds:
    """Deterministic heterogeneity/size thresholds for primary decomposition."""

    max_subtree_nodes: int = 500
    max_distinct_domains: int = 3
    max_distinct_kinds: int = 8

    def __post_init__(self) -> None:
        for name in ("max_subtree_nodes", "max_distinct_domains", "max_distinct_kinds"):
            value = getattr(self, name)
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise GraphModelError(f"{name} must be an int >= 1, got {value!r}")


@dataclass(frozen=True)
class SplitProposal:
    """A proposal-only suggestion to decompose part of a primary's subtree."""

    root_id: str
    suggested_name: str
    domain: PrimaryDomain | None
    member_node_ids: tuple[str, ...]
    reasons: tuple[str, ...]
    metrics: tuple[tuple[str, int], ...] = field(default_factory=tuple)


def propose_primary_splits(
    graph: UniversalGraph, root_id: str, thresholds: SplitThresholds | None = None
) -> tuple[SplitProposal, ...]:
    """Suggest new primaries when a primary's subtree is too large or heterogeneous.

    Deterministic and proposal-only: the graph is never mutated. Triggers
    (reported in sorted ``reasons``):

    * ``subtree_size`` — descendants exceed ``max_subtree_nodes``;
    * ``domain_heterogeneity`` — distinct non-``None`` domains among
      descendants exceed ``max_distinct_domains``;
    * ``kind_heterogeneity`` — distinct kinds among descendants exceed
      ``max_distinct_kinds``.

    Grouping: descendants are grouped by domain; the root's own domain group
    stays put and every other group becomes one proposal. A homogeneous
    oversized subtree is instead split by direct-child subtrees with a
    deterministic first-fit packing (children sorted by (-size, id)).
    """
    thresholds = thresholds if thresholds is not None else SplitThresholds()
    root = graph.get_node(root_id)
    if root.kind != CoreNodeKind.PRIMARY.value:
        raise PrimaryError(f"split proposals require a primary root; {root_id!r} is {root.kind!r}")

    descendants = graph.subtree_ids(root_id)[1:]
    domains = sorted({node.domain.value for nid in descendants if (node := graph.get_node(nid)).domain is not None})
    kinds = sorted({graph.get_node(nid).kind for nid in descendants})
    metrics = (
        ("distinct_domains", len(domains)),
        ("distinct_kinds", len(kinds)),
        ("subtree_size", len(descendants)),
    )
    reasons = tuple(
        name
        for name, triggered in (
            ("domain_heterogeneity", len(domains) > thresholds.max_distinct_domains),
            ("kind_heterogeneity", len(kinds) > thresholds.max_distinct_kinds),
            ("subtree_size", len(descendants) > thresholds.max_subtree_nodes),
        )
        if triggered
    )
    if not reasons:
        return ()

    groups: dict[str, list[str]] = {}
    for nid in descendants:
        domain = graph.get_node(nid).domain
        groups.setdefault(domain.value if domain else "", []).append(nid)
    keep_key = root.domain.value if root.domain is not None else ""

    proposals: list[SplitProposal] = []
    if len(groups) > 1:
        for key in sorted(groups):
            if key == keep_key:
                continue
            members = tuple(sorted(groups[key]))
            proposals.append(
                SplitProposal(
                    root_id=root_id,
                    suggested_name=f"{root.name}:{key or 'undomain'}",
                    domain=PrimaryDomain(key) if key else None,
                    member_node_ids=members,
                    reasons=reasons,
                    metrics=metrics,
                )
            )
        return tuple(proposals)

    # single group: size-driven split over direct-child subtrees
    children = graph.contains_children(root_id)
    sized = sorted(
        ((len(graph.subtree_ids(child)), child) for child in children),
        key=lambda item: (-item[0], item[1]),
    )
    bins: list[list[str]] = []
    bin_sizes: list[int] = []
    for size, child in sized:
        placed = False
        for index in range(len(bins)):
            if bin_sizes[index] + size <= thresholds.max_subtree_nodes:
                bins[index].append(child)
                bin_sizes[index] += size
                placed = True
                break
        if not placed:
            bins.append([child])
            bin_sizes.append(size)
    for index, members_children in enumerate(bins, start=1):
        members = tuple(sorted({nid for child in members_children for nid in graph.subtree_ids(child)}))
        proposals.append(
            SplitProposal(
                root_id=root_id,
                suggested_name=f"{root.name}:part-{index}",
                domain=None,
                member_node_ids=members,
                reasons=reasons,
                metrics=metrics,
            )
        )
    return tuple(proposals)


# ----------------------------------------------------------- review candidates


@dataclass(frozen=True)
class ReviewPolicy:
    """Deterministic review-signal policy.

    * ``stale_watermark``: a node is ``stale`` when its watermark is ``None``
      (never reviewed) or below this value;
    * ``min_score``: a node is ``low_score`` when ``min(P, M, E)`` is below it;
    * ``check_drift``: a node is ``content_drift`` when both hashes are set
      and the content hash differs from the reviewed hash.
    """

    stale_watermark: int | None = None
    min_score: Decimal = Decimal("0.2")
    check_drift: bool = True

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_score", _strict_decimal(self.min_score, "min_score"))
        if not (Decimal(0) <= self.min_score <= Decimal(1)):
            raise GraphModelError(f"min_score must be within [0, 1], got {self.min_score}")
        if self.stale_watermark is not None and (
            isinstance(self.stale_watermark, bool)
            or not isinstance(self.stale_watermark, int)
            or self.stale_watermark < 0
        ):
            raise GraphModelError(f"stale_watermark must be an int >= 0, got {self.stale_watermark!r}")


@dataclass(frozen=True)
class ReviewCandidate:
    """A proposal-only review signal for one node, with machine-readable reasons."""

    node_id: str
    reasons: tuple[str, ...]
    details: tuple[tuple[str, str], ...] = field(default_factory=tuple)


def review_candidates(
    graph: UniversalGraph, root_id: str, policy: ReviewPolicy | None = None
) -> tuple[ReviewCandidate, ...]:
    """Compute review candidates for the subtree under ``root_id`` (root excluded).

    Proposal-only and deterministic: candidates are sorted by node id and
    reasons follow the fixed order ``content_drift`` < ``low_score`` <
    ``stale``.
    """
    policy = policy if policy is not None else ReviewPolicy()
    graph.get_node(root_id)
    candidates: list[ReviewCandidate] = []
    for nid in graph.subtree_ids(root_id)[1:]:
        node = graph.get_node(nid)
        reasons: list[str] = []
        details: list[tuple[str, str]] = []
        if (
            policy.check_drift
            and node.content_hash is not None
            and node.reviewed_hash is not None
            and node.content_hash != node.reviewed_hash
        ):
            reasons.append("content_drift")
            details.append(("content_hash", node.content_hash))
            details.append(("reviewed_hash", node.reviewed_hash))
        min_pme = min(node.scores.p, node.scores.m, node.scores.e)
        if min_pme < policy.min_score:
            reasons.append("low_score")
            details.append(("min_pme", format(quantize_score(min_pme), "f")))
            details.append(("min_score", format(quantize_score(policy.min_score), "f")))
        if policy.stale_watermark is not None and (node.watermark is None or node.watermark < policy.stale_watermark):
            reasons.append("stale")
            details.append(("stale_watermark", str(policy.stale_watermark)))
            details.append(("watermark", str(node.watermark) if node.watermark is not None else "none"))
        if reasons:
            candidates.append(
                ReviewCandidate(node_id=nid, reasons=tuple(sorted(reasons)), details=tuple(sorted(details)))
            )
    return tuple(sorted(candidates, key=lambda candidate: candidate.node_id))

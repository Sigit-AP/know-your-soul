"""Structured query depth over the published read model (ATM F-E1/F-E2).

A pure analytics module on top of ``graph.json`` — the OPS/ART channel for
query depth. The frozen ``QueryRequest``/``QueryResponse`` contract and
``service.py`` are untouched: everything here is a deterministic function
over read-model mappings, returning canonical JSON-ready dicts.

Capabilities:

* **Filters** — strict, fail-closed structured predicates over node
  metadata: primary domain, node kind, lifecycle status, professional level
  range, and P/M/E/uncertainty/confidence score ranges. Unknown keys and
  malformed values raise :class:`QueryToolsError`; nothing is guessed.
* **Sorting** — multi-key ordering with explicit directions over an
  allowlist of fields. The ascending node id is always the final
  tie-breaker, so the result never depends on input order. Nodes missing a
  sortable value order after present values regardless of direction.
* **Pagination** — stable cursor paging where the cursor is the last node
  id of the previous page. A cursor that does not belong to the ordered set
  fails closed instead of silently restarting the scan.
* **Aggregation** — ``count``/``min``/``max``/``mean`` of the P/M/E family
  per group (domain, kind, lifecycle status, professional level), plus a
  frozen ten-bin histogram per dimension. Means are computed in Decimal
  precision 34 and rendered as 12-decimal fixed-point strings, exactly like
  every other score surface. Aggregations are always computed over the
  *full* filtered set, never over a truncated page.
* **Faceting** — per-field value breakdowns with deterministic
  ``(-count, value)`` ordering.
* **Traversal** — ``subtree_nodes`` walks the published link set from one
  root over an allowlist of relation kinds (default: the full closed
  relation vocabulary), breadth-first with sorted neighbor order, so the
  result is independent of link ordering in the artifact.
* **Search** — advanced retrieval on top of
  :class:`know_your_soul.retrieval.LexicalIndex`: rank lexically, then
  apply the structured filter to the hits, preserving the rank order. The
  index itself is never modified.
* **Near-duplicate detection (F-E2)** — proposal-only candidate pairs via
  normalized-summary word shingles and *exact* Jaccard similarity, blocked
  per primary domain. The output is a review list with rationale — never an
  auto-merge. The frozen ``merge_or_deduplicate`` lifecycle and the audited
  improvement channel own any actual merge; this module only proposes.

Determinism: no clock, no randomness, no I/O, no binary floats. Every
score-like value entering from the caller is a string, int, or Decimal —
binary floats are rejected at validation so results cannot drift across
platforms.

origin: idea of "filter the graph before traversal" from Graphify
``serve.py`` context filters, and of shingle-based duplicate detection from
Graphify ``dedup.py`` (Broder 1997 is standard literature). Implementation
original: structured metadata filters, exact blocked Jaccard, Decimal
scores, and proposal-only policy are know-your-soul concepts — Graphify
auto-merges; this module never does.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal, localcontext
from fractions import Fraction
from functools import cmp_to_key
from typing import Any, cast

from know_your_soul.domain import (
    LifecycleStatus,
    NodeKind,
    PrimaryDomain,
    RelationKind,
    quantize_score,
)
from know_your_soul.retrieval import LexicalIndex, query_terms, tokenize

__all__ = [
    "AGGREGATE_DIMENSIONS",
    "DEFAULT_JACCARD_THRESHOLD",
    "DEFAULT_PAGE_LIMIT",
    "DEFAULT_SHINGLE_SIZE",
    "FACET_FIELDS",
    "FILTER_KEYS",
    "GROUP_BY_FIELDS",
    "HISTOGRAM_BIN_COUNT",
    "MAX_PAGE_LIMIT",
    "NEAR_DUPLICATE_SCHEMA_VERSION",
    "QUERY_SCHEMA_VERSION",
    "RELATION_VALUES",
    "SEARCH_SCHEMA_VERSION",
    "SORTABLE_FIELDS",
    "QueryToolsError",
    "aggregate_nodes",
    "facet_nodes",
    "filter_nodes",
    "find_near_duplicates",
    "normalize_summary",
    "paginate_nodes",
    "query_nodes",
    "search_nodes",
    "shingle_tokens",
    "sort_nodes",
    "subtree_nodes",
    "validate_filter",
    "validate_sort",
]

QUERY_SCHEMA_VERSION = 1
SEARCH_SCHEMA_VERSION = 1
NEAR_DUPLICATE_SCHEMA_VERSION = 1

#: Frozen near-duplicate threshold (F-E2): pairs at or above this exact
#: Jaccard similarity are proposed for operator review.
DEFAULT_JACCARD_THRESHOLD = Decimal("0.8")

#: Frozen shingle width for near-duplicate detection (F-E2).
DEFAULT_SHINGLE_SIZE = 3

DEFAULT_PAGE_LIMIT = 50
MAX_PAGE_LIMIT = 1000

#: Score dimensions supported by filters, sorting and aggregation. The
#: first three are the P/M/E learning axes; uncertainty and confidence ride
#: along because the read model publishes them next to P/M/E.
AGGREGATE_DIMENSIONS: tuple[str, ...] = ("p", "m", "e", "uncertainty", "confidence")

#: Fields a facet breakdown may be requested for.
FACET_FIELDS: tuple[str, ...] = ("domain", "kind", "lifecycle_status", "professional_level")

#: Fields an aggregation may group by.
GROUP_BY_FIELDS: tuple[str, ...] = ("domain", "kind", "lifecycle_status", "professional_level")

#: Fields usable as sort keys. ``id`` is always the final tie-breaker.
SORTABLE_FIELDS: tuple[str, ...] = (
    "id",
    "label",
    "domain",
    "kind",
    "lifecycle_status",
    "professional_level",
    "version",
    "p",
    "m",
    "e",
    "uncertainty",
    "confidence",
)

#: Strict allowlist of filter-spec keys; anything else fails closed.
FILTER_KEYS: frozenset[str] = frozenset(
    {
        "domains",
        "kinds",
        "lifecycle_statuses",
        "level_min",
        "level_max",
        "score_ranges",
    }
)

_DOMAIN_VALUES = frozenset(member.value for member in PrimaryDomain)
_KIND_VALUES = frozenset(member.value for member in NodeKind)
_STATUS_VALUES = frozenset(member.value for member in LifecycleStatus)

#: The closed relation vocabulary (spec section 6); traversal allowlists
#: are validated against it, never free-form.
RELATION_VALUES: frozenset[str] = frozenset(member.value for member in RelationKind)

#: Frozen histogram width for score aggregations: ten equal bins covering
#: ``[0.0, 0.1) ... [0.9, 1.0]`` (the exact score 1 lands in the last bin).
HISTOGRAM_BIN_COUNT = 10

_LEVEL_MIN = 1
_LEVEL_MAX = 5

_ZERO = Decimal(0)
_ONE = Decimal(1)


class QueryToolsError(ValueError):
    """A query spec, filter, sort key, cursor, or read-model value is invalid.

    Every failure is closed: the module never guesses a domain, never
    coerces a binary float, never restarts a cursor, and never merges a
    duplicate candidate.
    """


# --------------------------------------------------------------------------
# read-model access helpers
# --------------------------------------------------------------------------


def _iter_nodes(graph_or_nodes: Mapping[str, Any] | Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Extract mapping nodes from a parsed ``graph.json`` or a node iterable.

    Non-mapping entries are ignored (the read-model validator owns the hard
    failure for those; analytics degrade gracefully over partial inputs).
    """
    raw: Any
    if isinstance(graph_or_nodes, Mapping):
        # ``cast`` pins the mapping branch: pyright widens isinstance
        # narrowing over the ``Mapping | Iterable[Mapping]`` union into
        # ``Mapping[str, Any] | Mapping[Mapping[str, Any], Unknown]``.
        raw = cast(Mapping[str, Any], graph_or_nodes).get("nodes")
    else:
        raw = graph_or_nodes
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        return []
    return [entry for entry in raw if isinstance(entry, Mapping)]


def _node_id(node: Mapping[str, Any]) -> str | None:
    """The node id, or ``None`` when the node carries no usable id."""
    value = node.get("id")
    if isinstance(value, str) and value:
        return value
    return None


def _node_label(node: Mapping[str, Any]) -> str:
    """The published label (enriched summary); falls back to the id."""
    label = node.get("label")
    if isinstance(label, str) and label:
        return label
    node_id = _node_id(node)
    return node_id if node_id is not None else ""


def _node_domain(node: Mapping[str, Any]) -> str | None:
    value = node.get("domain")
    if isinstance(value, str) and value in _DOMAIN_VALUES:
        return value
    return None


def _node_kind(node: Mapping[str, Any]) -> str | None:
    value = node.get("kind")
    if isinstance(value, str) and value in _KIND_VALUES:
        return value
    return None


def _node_status(node: Mapping[str, Any]) -> str | None:
    value = node.get("lifecycle_status")
    if isinstance(value, str) and value in _STATUS_VALUES:
        return value
    return None


def _node_level(node: Mapping[str, Any]) -> int | None:
    """The professional level, or ``None`` when absent or out of range."""
    value = node.get("professional_level")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    if _LEVEL_MIN <= value <= _LEVEL_MAX:
        return value
    return None


def _node_version(node: Mapping[str, Any]) -> int | None:
    value = node.get("version")
    if isinstance(value, bool) or not isinstance(value, int):
        return None
    return value if value >= 1 else None


def _node_score(node: Mapping[str, Any], dimension: str) -> Decimal | None:
    """One published score as Decimal, or ``None`` when malformed.

    The read model serializes scores as 12-decimal fixed-point strings.
    Anything else (missing key, non-string, unparseable) is ``None`` — the
    caller decides whether that excludes the node (filters) or just skips
    the value (aggregation).
    """
    scores = node.get("scores")
    if not isinstance(scores, Mapping):
        return None
    value = scores.get(dimension)
    if isinstance(value, Decimal):
        candidate = value
    elif isinstance(value, str):
        try:
            candidate = Decimal(value)
        except ArithmeticError:
            return None
    else:
        return None
    if not candidate.is_finite() or candidate < _ZERO or candidate > _ONE:
        return None
    return candidate


# --------------------------------------------------------------------------
# filter spec validation
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _Filter:
    """The normalized, fully validated filter spec."""

    domains: frozenset[str] | None = None
    kinds: frozenset[str] | None = None
    lifecycle_statuses: frozenset[str] | None = None
    level_min: int | None = None
    level_max: int | None = None
    score_ranges: tuple[tuple[str, Decimal | None, Decimal | None], ...] = ()


def _reject_unknown_keys(spec: Mapping[str, Any], allowed: frozenset[str], where: str) -> None:
    unknown = sorted(str(key) for key in spec if key not in allowed)
    if unknown:
        raise QueryToolsError(f"unknown {where} key(s): {', '.join(unknown)}")


def _validate_value_set(spec: Mapping[str, Any], key: str, allowed: frozenset[str]) -> frozenset[str] | None:
    raw = spec.get(key)
    if raw is None:
        return None
    if isinstance(raw, (str, bytes)) or not isinstance(raw, Iterable):
        raise QueryToolsError(f"filter {key!r} must be a list of strings")
    values: set[str] = set()
    for item in raw:
        if not isinstance(item, str) or not item:
            raise QueryToolsError(f"filter {key!r} entries must be non-empty strings")
        if item not in allowed:
            raise QueryToolsError(f"filter {key!r} value {item!r} is not in the closed vocabulary")
        values.add(item)
    if not values:
        raise QueryToolsError(f"filter {key!r} must not be an empty list")
    return frozenset(values)


def _validate_level_bound(spec: Mapping[str, Any], key: str) -> int | None:
    raw = spec.get(key)
    if raw is None:
        return None
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise QueryToolsError(f"filter {key!r} must be an integer in [1, 5]")
    if raw < _LEVEL_MIN or raw > _LEVEL_MAX:
        raise QueryToolsError(f"filter {key!r} must be in [1, 5], got {raw}")
    return raw


def _validate_score_bound(value: Any, where: str) -> Decimal:
    """Parse one range bound; binary floats are rejected, never coerced."""
    if isinstance(value, bool):
        raise QueryToolsError(f"score range bound {where} must be a string, int or Decimal")
    if isinstance(value, Decimal):
        bound = value
    elif isinstance(value, int):
        bound = Decimal(value)
    elif isinstance(value, str):
        try:
            bound = Decimal(value)
        except ArithmeticError:
            raise QueryToolsError(f"score range bound {where} is not a decimal: {value!r}") from None
    else:
        raise QueryToolsError(
            f"score range bound {where} must be a string, int or Decimal (binary floats are rejected)"
        )
    if not bound.is_finite():
        raise QueryToolsError(f"score range bound {where} must be finite")
    if bound < _ZERO or bound > _ONE:
        raise QueryToolsError(f"score range bound {where} must be in [0, 1], got {bound}")
    return bound


def _validate_score_ranges(spec: Mapping[str, Any]) -> tuple[tuple[str, Decimal | None, Decimal | None], ...]:
    raw = spec.get("score_ranges")
    if raw is None:
        return ()
    if not isinstance(raw, Mapping):
        raise QueryToolsError("filter 'score_ranges' must be a mapping of dimension to {min, max}")
    _reject_unknown_keys(raw, frozenset(AGGREGATE_DIMENSIONS), "score_ranges dimension")
    ranges: list[tuple[str, Decimal | None, Decimal | None]] = []
    for dimension in AGGREGATE_DIMENSIONS:
        if dimension not in raw:
            continue
        entry = raw[dimension]
        if not isinstance(entry, Mapping):
            raise QueryToolsError(f"score range for {dimension!r} must be a mapping with 'min'/'max'")
        _reject_unknown_keys(entry, frozenset({"min", "max"}), f"score range {dimension!r}")
        low = entry.get("min")
        high = entry.get("max")
        low_dec = _validate_score_bound(low, f"{dimension}.min") if low is not None else None
        high_dec = _validate_score_bound(high, f"{dimension}.max") if high is not None else None
        if low_dec is None and high_dec is None:
            raise QueryToolsError(f"score range for {dimension!r} sets neither 'min' nor 'max'")
        if low_dec is not None and high_dec is not None and low_dec > high_dec:
            raise QueryToolsError(f"score range for {dimension!r} has min > max")
        ranges.append((dimension, low_dec, high_dec))
    return tuple(ranges)


def validate_filter(spec: Mapping[str, Any] | None) -> _Filter:
    """Validate and normalize a filter spec; fail closed on anything unknown.

    ``None`` and the empty mapping both mean "match every node" — the
    additive-only default from F-E1. A non-mapping spec never matches by
    accident: it raises.
    """
    if spec is None:
        return _Filter()
    if not isinstance(spec, Mapping):
        raise QueryToolsError("filter spec must be a mapping")
    _reject_unknown_keys(spec, FILTER_KEYS, "filter")
    domains = _validate_value_set(spec, "domains", _DOMAIN_VALUES)
    kinds = _validate_value_set(spec, "kinds", _KIND_VALUES)
    statuses = _validate_value_set(spec, "lifecycle_statuses", _STATUS_VALUES)
    level_min = _validate_level_bound(spec, "level_min")
    level_max = _validate_level_bound(spec, "level_max")
    if level_min is not None and level_max is not None and level_min > level_max:
        raise QueryToolsError("filter level_min must not exceed level_max")
    score_ranges = _validate_score_ranges(spec)
    return _Filter(
        domains=domains,
        kinds=kinds,
        lifecycle_statuses=statuses,
        level_min=level_min,
        level_max=level_max,
        score_ranges=score_ranges,
    )


def _matches(node: Mapping[str, Any], spec: _Filter) -> bool:
    """Whether one read-model node satisfies the validated filter.

    A node with a missing or malformed value never satisfies a predicate
    over that value — absence is not a wildcard.
    """
    if spec.domains is not None and _node_domain(node) not in spec.domains:
        return False
    if spec.kinds is not None and _node_kind(node) not in spec.kinds:
        return False
    if spec.lifecycle_statuses is not None and _node_status(node) not in spec.lifecycle_statuses:
        return False
    if spec.level_min is not None or spec.level_max is not None:
        level = _node_level(node)
        if level is None:
            return False
        if spec.level_min is not None and level < spec.level_min:
            return False
        if spec.level_max is not None and level > spec.level_max:
            return False
    for dimension, low, high in spec.score_ranges:
        value = _node_score(node, dimension)
        if value is None:
            return False
        if low is not None and value < low:
            return False
        if high is not None and value > high:
            return False
    return True


def filter_nodes(
    graph_or_nodes: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    spec: Mapping[str, Any] | None = None,
) -> list[Mapping[str, Any]]:
    """Filter read-model nodes by a strict spec; canonical id-ascending order.

    The output order is ``sorted(node id)`` regardless of input order, so
    filtering alone is already a deterministic projection. Nodes without a
    usable id never pass — a filter result must be addressable by cursor.
    """
    parsed = validate_filter(spec)
    matched = [node for node in _iter_nodes(graph_or_nodes) if _node_id(node) is not None and _matches(node, parsed)]
    matched.sort(key=lambda node: str(node["id"]))
    return matched


# --------------------------------------------------------------------------
# sorting
# --------------------------------------------------------------------------


@dataclass(frozen=True)
class _SortKey:
    field: str
    descending: bool = False


def validate_sort(spec: Sequence[Any] | None) -> tuple[_SortKey, ...]:
    """Validate a sort spec: a list of ``{"field": ..., "direction": ...}``.

    Directions are ``"asc"`` (default) or ``"desc"``. A plain string entry
    is accepted as sugar for ``{"field": entry}``; a leading ``-`` means
    descending. The ascending node id is appended as the final tie-breaker
    unless the caller already sorted by id — the result never depends on
    input order either way.
    """
    if spec is None:
        return ()
    if isinstance(spec, (str, bytes)) or not isinstance(spec, Sequence):
        raise QueryToolsError("sort spec must be a list of sort keys")
    keys: list[_SortKey] = []
    for entry in spec:
        if isinstance(entry, str):
            field = entry[1:] if entry.startswith("-") else entry
            descending = entry.startswith("-")
        elif isinstance(entry, Mapping):
            _reject_unknown_keys(entry, frozenset({"field", "direction"}), "sort key")
            raw_field = entry.get("field")
            if not isinstance(raw_field, str) or not raw_field:
                raise QueryToolsError("sort key 'field' must be a non-empty string")
            field = raw_field
            raw_direction = entry.get("direction", "asc")
            if raw_direction not in ("asc", "desc"):
                raise QueryToolsError(f"sort direction must be 'asc' or 'desc', got {raw_direction!r}")
            descending = raw_direction == "desc"
        else:
            raise QueryToolsError("sort keys must be strings or {field, direction} mappings")
        if field not in SORTABLE_FIELDS:
            raise QueryToolsError(f"sort field {field!r} is not sortable; allowed: {', '.join(SORTABLE_FIELDS)}")
        keys.append(_SortKey(field=field, descending=descending))
    if not keys:
        return ()
    if all(key.field != "id" for key in keys):
        keys.append(_SortKey(field="id", descending=False))
    return tuple(keys)


def _sort_value(node: Mapping[str, Any], field: str) -> tuple[int, Any]:
    """The comparable value for one field; missing sorts last (rank 1).

    Strings compare as strings, levels/versions as ints, scores as Decimal.
    A missing or malformed value ranks after every present value regardless
    of direction, so direction never inverts the "known before unknown"
    rule.
    """
    if field == "id":
        node_id = _node_id(node)
        return (0, node_id) if node_id is not None else (1, "")
    if field == "label":
        label = node.get("label")
        return (0, label) if isinstance(label, str) and label else (1, "")
    if field == "domain":
        value = _node_domain(node)
        return (0, value) if value is not None else (1, "")
    if field == "kind":
        value = _node_kind(node)
        return (0, value) if value is not None else (1, "")
    if field == "lifecycle_status":
        value = _node_status(node)
        return (0, value) if value is not None else (1, "")
    if field == "professional_level":
        value = _node_level(node)
        return (0, value) if value is not None else (1, 0)
    if field == "version":
        value = _node_version(node)
        return (0, value) if value is not None else (1, 0)
    value = _node_score(node, field)
    return (0, value) if value is not None else (1, _ZERO)


def _compare_nodes(left: Mapping[str, Any], right: Mapping[str, Any], keys: tuple[_SortKey, ...]) -> int:
    """Three-way comparison over the validated keys; deterministic total order."""
    for key in keys:
        left_rank, left_value = _sort_value(left, key.field)
        right_rank, right_value = _sort_value(right, key.field)
        if left_rank != right_rank:
            # Missing values always sort after present ones.
            return -1 if left_rank < right_rank else 1
        if left_value == right_value:
            continue
        if key.descending:
            return -1 if left_value > right_value else 1
        return -1 if left_value < right_value else 1
    # Absolute backstop: identical on every key — order by id so the sort
    # is total even for duplicate-valued entries.
    left_id = _node_id(left) or ""
    right_id = _node_id(right) or ""
    if left_id == right_id:
        return 0
    return -1 if left_id < right_id else 1


def sort_nodes(
    nodes: Iterable[Mapping[str, Any]],
    spec: Sequence[Any] | None = None,
) -> list[Mapping[str, Any]]:
    """Sort nodes by the validated multi-key spec; default is id ascending."""
    parsed = validate_sort(spec)
    materialized = [node for node in nodes if isinstance(node, Mapping)]
    if not parsed:
        materialized.sort(key=lambda node: _node_id(node) or "")
        return materialized
    materialized.sort(key=cmp_to_key(lambda left, right: _compare_nodes(left, right, parsed)))
    return materialized


# --------------------------------------------------------------------------
# pagination
# --------------------------------------------------------------------------


def _validate_limit(limit: Any) -> int:
    if limit is None:
        return DEFAULT_PAGE_LIMIT
    if isinstance(limit, bool) or not isinstance(limit, int):
        raise QueryToolsError("page limit must be an integer")
    if limit < 1:
        raise QueryToolsError("page limit must be positive")
    if limit > MAX_PAGE_LIMIT:
        raise QueryToolsError(f"page limit must not exceed {MAX_PAGE_LIMIT}")
    return limit


def paginate_nodes(
    sorted_nodes: Sequence[Mapping[str, Any]],
    *,
    cursor: str | None = None,
    limit: int | None = None,
) -> dict[str, Any]:
    """One stable page over an already-ordered node list.

    The cursor is the node id of the last item of the previous page; the
    next page starts strictly after it. A cursor that is not part of the
    ordered set fails closed — silently restarting would mix pages from
    different result sets. ``next_cursor`` is ``None`` on the last page.
    """
    page_limit = _validate_limit(limit)
    indexed: list[tuple[str, Mapping[str, Any]]] = []
    for node in sorted_nodes:
        node_id = _node_id(node)
        if node_id is None:
            continue
        indexed.append((node_id, node))
    start = 0
    if cursor is not None:
        if not isinstance(cursor, str) or not cursor:
            raise QueryToolsError("cursor must be a non-empty node id string")
        positions = {node_id: position for position, (node_id, _) in enumerate(indexed)}
        if cursor not in positions:
            raise QueryToolsError(f"cursor {cursor!r} is not part of this ordered result set")
        start = positions[cursor] + 1
    window = indexed[start : start + page_limit]
    has_more = start + page_limit < len(indexed)
    items = [node for _, node in window]
    return {
        "items": items,
        "count": len(items),
        "cursor": cursor,
        "next_cursor": window[-1][0] if has_more and window else None,
        "has_more": has_more,
        "total": len(indexed),
    }


# --------------------------------------------------------------------------
# aggregation and faceting
# --------------------------------------------------------------------------


def _validate_group_field(field: Any, allowed: tuple[str, ...], what: str) -> str:
    if not isinstance(field, str) or field not in allowed:
        raise QueryToolsError(f"{what} must be one of: {', '.join(allowed)}")
    return field


def _group_value(node: Mapping[str, Any], field: str) -> str:
    """The group key for one node; missing values group under ``<missing>``."""
    if field == "domain":
        return _node_domain(node) or "<missing>"
    if field == "kind":
        return _node_kind(node) or "<missing>"
    if field == "lifecycle_status":
        return _node_status(node) or "<missing>"
    level = _node_level(node)
    return str(level) if level is not None else "<missing>"


def _mean_decimal(values: Sequence[Decimal]) -> Decimal:
    """The exact mean in Decimal precision 34, quantized for output."""
    with localcontext() as context:
        context.prec = 34
        total = Decimal(0)
        for value in values:
            total += value
        return quantize_score(total / Decimal(len(values)))


def _histogram(values: Sequence[Decimal]) -> list[int]:
    """The frozen ten-bin histogram over scores in ``[0, 1]``.

    Bin ``i`` covers ``[i/10, (i+1)/10)``; the exact score 1 lands in the
    last bin. Boundaries are computed in Decimal arithmetic — no binary
    float ever touches a score.
    """
    bins = [0] * HISTOGRAM_BIN_COUNT
    for value in values:
        index = int(value * HISTOGRAM_BIN_COUNT)
        bins[min(index, HISTOGRAM_BIN_COUNT - 1)] += 1
    return bins


def aggregate_nodes(
    graph_or_nodes: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    group_by: str = "domain",
) -> dict[str, Any]:
    """Count/min/max/mean of the score family per group.

    Groups order by key ascending. ``missing`` counts nodes whose score for
    that dimension was absent or malformed — those nodes are excluded from
    that dimension's min/max/mean/histogram but still count in ``count``. A
    group where every node misses a dimension reports null min/max/mean and
    an all-zero histogram. Each dimension also carries the frozen ten-bin
    histogram (see :data:`HISTOGRAM_BIN_COUNT`).
    """
    field = _validate_group_field(group_by, GROUP_BY_FIELDS, "group_by")
    nodes = _iter_nodes(graph_or_nodes)
    buckets: dict[str, list[Mapping[str, Any]]] = {}
    for node in nodes:
        buckets.setdefault(_group_value(node, field), []).append(node)
    groups: list[dict[str, Any]] = []
    for key in sorted(buckets):
        members = buckets[key]
        scores: dict[str, Any] = {}
        for dimension in AGGREGATE_DIMENSIONS:
            values: list[Decimal] = []
            missing = 0
            for node in members:
                value = _node_score(node, dimension)
                if value is None:
                    missing += 1
                else:
                    values.append(value)
            if values:
                scores[dimension] = {
                    "min": format(quantize_score(min(values)), "f"),
                    "max": format(quantize_score(max(values)), "f"),
                    "mean": format(_mean_decimal(values), "f"),
                    "missing": missing,
                    "histogram": _histogram(values),
                }
            else:
                scores[dimension] = {
                    "min": None,
                    "max": None,
                    "mean": None,
                    "missing": missing,
                    "histogram": [0] * HISTOGRAM_BIN_COUNT,
                }
        groups.append({"key": key, "count": len(members), "scores": scores})
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "group_by": field,
        "total": len(nodes),
        "group_count": len(groups),
        "groups": groups,
    }


def facet_nodes(
    graph_or_nodes: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    fields: Sequence[str] | None = None,
) -> dict[str, Any]:
    """Per-field value breakdowns over the node set.

    Each facet orders by ``(-count, value)`` — the most frequent value
    first, ties by ascending value. Requesting no fields facets all of
    :data:`FACET_FIELDS`.
    """
    if fields is None:
        selected = FACET_FIELDS
    else:
        if isinstance(fields, (str, bytes)):
            raise QueryToolsError("fields must be a list of facet field names")
        selected = tuple(_validate_group_field(field, FACET_FIELDS, "facet field") for field in fields)
    nodes = _iter_nodes(graph_or_nodes)
    facets: dict[str, Any] = {}
    for field in selected:
        counts: dict[str, int] = {}
        for node in nodes:
            key = _group_value(node, field)
            counts[key] = counts.get(key, 0) + 1
        facets[field] = [
            {"value": value, "count": counts[value]} for value in sorted(counts, key=lambda item: (-counts[item], item))
        ]
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "total": len(nodes),
        "facets": facets,
    }


# --------------------------------------------------------------------------
# lexical search with structured filters
# --------------------------------------------------------------------------


def search_nodes(
    graph_or_nodes: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    query: str,
    *,
    node_filter: Mapping[str, Any] | None = None,
    limit: int | None = None,
    index: LexicalIndex | None = None,
) -> dict[str, Any]:
    """Rank lexically, then apply the structured filter to the hits.

    The filter never re-ranks: hit order stays the index's canonical
    ``(-score, -field_rank, node_id)``; filtered-out hits are simply
    removed. An empty query (no terms after stopword removal) matches
    nothing rather than everything — lexical search is a positive-signal
    surface. Scores are rendered as 12-decimal fixed-point strings.
    """
    nodes = _iter_nodes(graph_or_nodes)
    terms = query_terms(query) if isinstance(query, str) else []
    parsed = validate_filter(node_filter)
    lexical = index if index is not None else LexicalIndex.from_graph_nodes(nodes)
    by_id = {str(node["id"]): node for node in nodes if _node_id(node) is not None}
    hits: list[dict[str, Any]] = []
    examined = 0
    if terms:
        for hit in lexical.rank(terms):
            examined += 1
            node = by_id.get(hit.node_id)
            if node is None:
                # An externally supplied index may cover ids outside this
                # node set; those hits are out of scope, not an error.
                continue
            if not _matches(node, parsed):
                continue
            hits.append(
                {
                    "node_id": hit.node_id,
                    "score": format(quantize_score(hit.score), "f"),
                    "field_rank": format(quantize_score(hit.field_rank), "f"),
                }
            )
    truncated = False
    if limit is not None:
        page_limit = _validate_limit(limit)
        if len(hits) > page_limit:
            hits = hits[:page_limit]
            truncated = True
    return {
        "schema_version": SEARCH_SCHEMA_VERSION,
        "query_terms": terms,
        "total": len(hits),
        "examined": examined,
        "truncated": truncated,
        "hits": hits,
    }


# --------------------------------------------------------------------------
# near-duplicate detection (proposal-only)
# --------------------------------------------------------------------------


def normalize_summary(text: str) -> tuple[str, ...]:
    """Casefolded word tokens of a summary; the comparison normal form.

    Stopword-free normalized query terms are preferred (they are the same
    normal form the lexical index ranks with); a summary made entirely of
    stopwords falls back to raw tokens so it still has an identity instead
    of vanishing from duplicate detection.
    """
    terms = tuple(query_terms(text))
    if terms:
        return terms
    return tuple(tokenize(text))


def shingle_tokens(tokens: Sequence[str], shingle_size: int) -> frozenset[tuple[str, ...]]:
    """The set of contiguous word ``shingle_size``-grams over the tokens.

    A document shorter than the shingle size contributes one shingle: the
    whole token sequence. Two short documents therefore compare equal only
    when their normalized text is identical — a conservative fail-closed
    reading of "similar".
    """
    if shingle_size < 1:
        raise QueryToolsError("shingle_size must be positive")
    if not tokens:
        return frozenset()
    if len(tokens) < shingle_size:
        return frozenset({tuple(tokens)})
    return frozenset(tuple(tokens[index : index + shingle_size]) for index in range(len(tokens) - shingle_size + 1))


def _exact_key(tokens: Sequence[str]) -> str:
    """The content hash of the normalized text; identical text ⇒ identical key."""
    return hashlib.sha256(" ".join(tokens).encode("utf-8")).hexdigest()


def _jaccard(left: frozenset[tuple[str, ...]], right: frozenset[tuple[str, ...]]) -> Fraction:
    """Exact Jaccard similarity as a rational — no float, no rounding."""
    union = left | right
    if not union:
        return Fraction(0, 1)
    return Fraction(len(left & right), len(union))


def _render_score(value: Fraction) -> str:
    """The candidate score as a 12-decimal fixed-point string."""
    with localcontext() as context:
        context.prec = 34
        return format(quantize_score(Decimal(value.numerator) / Decimal(value.denominator)), "f")


def _parse_threshold(threshold: Any) -> Fraction:
    bound = _validate_score_bound(threshold, "threshold")
    return Fraction(bound)


def find_near_duplicates(
    graph_or_nodes: Mapping[str, Any] | Iterable[Mapping[str, Any]],
    *,
    threshold: Any = DEFAULT_JACCARD_THRESHOLD,
    shingle_size: int = DEFAULT_SHINGLE_SIZE,
    max_candidates: int | None = None,
) -> dict[str, Any]:
    """Propose near-duplicate node pairs for operator review — never merge.

    Detection is summary-similarity: normalize each node's published label
    (casefold, stopword-free tokens), build word shingles, and compute
    *exact* Jaccard similarity within per-domain blocks. Pairs at or above
    the frozen threshold become candidates, ordered by
    ``(-similarity, node_a, node_b)``. Nodes whose normalized texts are
    identical are flagged ``exact`` with similarity 1.

    The output carries ``"disposition": "proposal_only"`` and
    ``"auto_merge": false`` as data, not just documentation: a downstream
    consumer that ignored the policy would still trip over the explicit
    refusal fields. Actual merges belong to the frozen
    ``merge_or_deduplicate`` lifecycle via the audited improvement channel.

    Blocking is per primary domain: cross-domain duplicates are not
    proposed, because a node pair in different primary domains is a
    taxonomy question, not a hygiene one.
    """
    cutoff = _parse_threshold(threshold)
    if isinstance(shingle_size, bool) or not isinstance(shingle_size, int) or shingle_size < 1:
        raise QueryToolsError("shingle_size must be a positive integer")
    if max_candidates is not None:
        if isinstance(max_candidates, bool) or not isinstance(max_candidates, int) or max_candidates < 1:
            raise QueryToolsError("max_candidates must be a positive integer")

    nodes = _iter_nodes(graph_or_nodes)
    # Canonical document order: ascending id, so pair enumeration and every
    # downstream sort are input-order independent.
    documents: list[tuple[str, str, tuple[str, ...], frozenset[tuple[str, ...]], str]] = []
    skipped_empty = 0
    for node in sorted(nodes, key=lambda item: _node_id(item) or ""):
        node_id = _node_id(node)
        if node_id is None:
            skipped_empty += 1
            continue
        tokens = normalize_summary(_node_label(node))
        if not tokens:
            skipped_empty += 1
            continue
        domain = _node_domain(node) or "<none>"
        documents.append((node_id, domain, tokens, shingle_tokens(tokens, shingle_size), _exact_key(tokens)))

    blocks: dict[str, list[int]] = {}
    for position, (_, domain, _, _, _) in enumerate(documents):
        blocks.setdefault(domain, []).append(position)

    candidates: list[tuple[Fraction, str, str, str, bool]] = []
    pairs_compared = 0
    for domain in sorted(blocks):
        members = blocks[domain]
        for left_offset in range(len(members)):
            for right_offset in range(left_offset + 1, len(members)):
                left = documents[members[left_offset]]
                right = documents[members[right_offset]]
                pairs_compared += 1
                exact = left[4] == right[4]
                similarity = Fraction(1, 1) if exact else _jaccard(left[3], right[3])
                if similarity >= cutoff:
                    candidates.append((similarity, left[0], right[0], domain, exact))

    candidates.sort(key=lambda item: (-item[0], item[1], item[2]))
    truncated = False
    if max_candidates is not None and len(candidates) > max_candidates:
        candidates = candidates[:max_candidates]
        truncated = True

    with localcontext() as context:
        context.prec = 34
        cutoff_rendered = format(quantize_score(Decimal(cutoff.numerator) / Decimal(cutoff.denominator)), "f")

    entries: list[dict[str, Any]] = []
    for similarity, node_a, node_b, domain, exact in candidates:
        rendered = _render_score(similarity)
        if exact:
            rationale = f"normalized summaries are identical (content hash match, {shingle_size}-token shingles)"
        else:
            rationale = f"jaccard {rendered} over {shingle_size}-token shingles meets threshold {cutoff_rendered}"
        entries.append(
            {
                "node_a": node_a,
                "node_b": node_b,
                "domain": domain,
                "score": rendered,
                "exact": exact,
                "rationale": rationale,
            }
        )

    return {
        "schema_version": NEAR_DUPLICATE_SCHEMA_VERSION,
        "disposition": "proposal_only",
        "auto_merge": False,
        "threshold": cutoff_rendered,
        "shingle_size": shingle_size,
        "candidate_count": len(entries),
        "candidates": entries,
        "truncated": truncated,
        "stats": {
            "nodes_considered": len(documents),
            "nodes_skipped": skipped_empty,
            "pairs_compared": pairs_compared,
            "blocks": {domain: len(blocks[domain]) for domain in sorted(blocks)},
        },
    }


# --------------------------------------------------------------------------
# traversal over the published link set
# --------------------------------------------------------------------------


def _validate_relations(relations: Any) -> tuple[str, ...]:
    """The traversal relation allowlist, sorted; default is the full vocabulary."""
    if relations is None:
        return tuple(sorted(RELATION_VALUES))
    if isinstance(relations, (str, bytes)) or not isinstance(relations, Iterable):
        raise QueryToolsError("relations must be a list of relation kinds")
    values: set[str] = set()
    for item in relations:
        if not isinstance(item, str) or item not in RELATION_VALUES:
            raise QueryToolsError(f"relation {item!r} is not in the closed relation vocabulary")
        values.add(item)
    if not values:
        raise QueryToolsError("relations must not be an empty list")
    return tuple(sorted(values))


def _iter_links(graph: Mapping[str, Any]) -> list[tuple[str, str, str]]:
    """The ``(source, target, relation)`` triples of the published link set."""
    raw = graph.get("links")
    if not isinstance(raw, list):
        return []
    triples: list[tuple[str, str, str]] = []
    for entry in raw:
        if not isinstance(entry, Mapping):
            continue
        source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
        if (
            isinstance(source, str)
            and source
            and isinstance(target, str)
            and target
            and isinstance(relation, str)
            and relation in RELATION_VALUES
        ):
            triples.append((source, target, relation))
    return triples


def subtree_nodes(
    graph: Mapping[str, Any],
    root_id: str,
    *,
    relations: Sequence[str] | None = None,
    max_depth: int | None = None,
) -> dict[str, Any]:
    """Breadth-first walk of the link set from ``root_id``, relation-allowlisted.

    Only outgoing edges whose relation is in the allowlist are followed.
    Neighbors are visited in ascending target-id order, so the walk order is
    a pure function of the graph content, not of the artifact's link order.
    Cycles terminate: each node is visited once, at its shallowest depth,
    and the edge that first reached it is the one reported.

    A root id absent from the node set fails closed — walking from a
    guessed root is how analysis quietly answers the wrong question.
    ``max_depth`` 0 returns just the root.
    """
    if not isinstance(root_id, str) or not root_id:
        raise QueryToolsError("root_id must be a non-empty string")
    allowed = _validate_relations(relations)
    if max_depth is not None:
        if isinstance(max_depth, bool) or not isinstance(max_depth, int) or max_depth < 0:
            raise QueryToolsError("max_depth must be a non-negative integer")
    nodes = _iter_nodes(graph)
    known = {node_id for node in nodes if (node_id := _node_id(node)) is not None}
    if root_id not in known:
        raise QueryToolsError(f"root {root_id!r} is not a node of this read model")

    allowed_set = set(allowed)
    # Build the adjacency once, canonically: per source, neighbors sorted by
    # (target id, relation). Duplicate triples collapse — following the same
    # edge twice changes nothing.
    adjacency: dict[str, list[tuple[str, str]]] = {}
    for source, target, relation in _iter_links(graph):
        if relation not in allowed_set or target not in known:
            continue
        adjacency.setdefault(source, []).append((target, relation))
    for source in adjacency:
        adjacency[source] = sorted(set(adjacency[source]))

    visited: dict[str, tuple[int, str | None]] = {root_id: (0, None)}
    queue: list[str] = [root_id]
    while queue:
        current = queue.pop(0)
        depth = visited[current][0]
        if max_depth is not None and depth >= max_depth:
            continue
        for target, relation in adjacency.get(current, []):
            if target in visited:
                continue
            visited[target] = (depth + 1, relation)
            queue.append(target)

    walk = [{"id": node_id, "depth": visited[node_id][0], "via": visited[node_id][1]} for node_id in visited]
    return {
        "schema_version": QUERY_SCHEMA_VERSION,
        "root": root_id,
        "relations": list(allowed),
        "max_depth": max_depth,
        "count": len(walk),
        "nodes": walk,
        "edges_considered": sum(len(neighbors) for neighbors in adjacency.values()),
    }


# --------------------------------------------------------------------------
# composition: the one-call structured query
# --------------------------------------------------------------------------

_QUERY_KEYS = frozenset({"filter", "sort", "page", "group_by", "facets"})
_PAGE_KEYS = frozenset({"cursor", "limit"})


def query_nodes(
    graph: Mapping[str, Any],
    spec: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Compose filter → sort → page (+ aggregation/facets) in one call.

    The spec is a strict mapping with keys ``filter``, ``sort``, ``page``,
    ``group_by`` and ``facets`` — all optional. Aggregations and facets are
    computed over the *full* filtered set, never over the truncated page,
    so a paginated consumer still sees corpus-true counts. The page carries
    the matched node dicts themselves in canonical order.

    ``total_nodes`` counts every mapping node in the read model;
    ``total_filtered`` counts the set after filtering — the difference is
    the filter's selectivity, exposed for review tooling.
    """
    if spec is None:
        spec = {}
    if not isinstance(spec, Mapping):
        raise QueryToolsError("query spec must be a mapping")
    _reject_unknown_keys(spec, _QUERY_KEYS, "query")

    nodes = _iter_nodes(graph)
    filtered = filter_nodes(nodes, spec.get("filter"))

    sort_spec = spec.get("sort")
    if sort_spec is None:
        ordered = filtered  # filter_nodes already returns id-ascending
    else:
        ordered = sort_nodes(filtered, sort_spec)

    raw_page = spec.get("page")
    if raw_page is None:
        page_cursor: str | None = None
        page_limit: int | None = None
    else:
        if not isinstance(raw_page, Mapping):
            raise QueryToolsError("query 'page' must be a mapping with 'cursor'/'limit'")
        _reject_unknown_keys(raw_page, _PAGE_KEYS, "page")
        page_cursor = raw_page.get("cursor")
        page_limit = raw_page.get("limit")
    page = paginate_nodes(ordered, cursor=page_cursor, limit=page_limit)

    result: dict[str, Any] = {
        "schema_version": QUERY_SCHEMA_VERSION,
        "total_nodes": len(nodes),
        "total_filtered": len(filtered),
        "page": page,
    }
    if "group_by" in spec:
        result["aggregation"] = aggregate_nodes(filtered, group_by=spec["group_by"])
    if "facets" in spec:
        raw_facets = spec["facets"]
        if raw_facets is not None and not isinstance(raw_facets, (list, tuple)):
            raise QueryToolsError("query 'facets' must be a list of facet field names")
        result["facets"] = facet_nodes(filtered, fields=raw_facets)
    return result

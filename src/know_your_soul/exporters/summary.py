"""Canonical graph-summary statistics exporter (ATM wave-2 formats).

``analytics/graph-summary.json``: a deterministic statistical projection of
the read model — node/edge counts (including dropped dangling and
duplicate edges, so nothing is silently hidden), a total-degree histogram
(over the deduplicated edge set; a self-loop contributes 2, matching the
``nodes.csv`` degree column), min/max/mean degree with the mean as a
quantized 12-decimal string (spec section 9), a per-domain breakdown
(nodes, internal edges, boundary in/out) and per-relation edge counts.

Contract:

* Deterministic, byte-identical output: canonical JSON (sorted keys,
  compact separators, UTF-8 passthrough) with a single trailing newline;
  no wall-clock, no RNG.
* Domain and relation names pass through ``security.sanitize_text`` with
  whitespace controls collapsed; an empty/absent domain buckets under
  ``"unknown"``.
* Explicit fail-closed byte cap via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  :func:`roundtrip_summary` re-serializes to prove canonical form and
  asserts the structural invariants (histogram sums to the node count,
  degree mass equals twice the edge count, per-domain nodes sum to the
  node count, boundary in/out symmetry, relation counts sum to the edge
  count, quantized mean shape).

No AI, no network, stdlib only.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from decimal import Decimal
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.scoring import serialize_score
from know_your_soul.security import sanitize_text

__all__ = [
    "SUMMARY_MAX_BYTES",
    "build_summary",
    "produce_summary",
    "register_summary",
    "roundtrip_summary",
]

SUMMARY_MAX_BYTES = 256 * 1024

_ARTIFACT_NAME = "analytics/graph-summary.json"
_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")
_QUANTIZED_RE = re.compile(r"^\d+\.\d{12}$")
_INT_KEY_RE = re.compile(r"^\d+$")


# --------------------------------------------------------------- primitives


def _clean_name(raw: Any, fallback: str) -> str:
    """Sanitize a domain/relation name; collapse whitespace controls."""
    if raw in (None, ""):
        return fallback
    text = sanitize_text(str(raw)[:129])[:128]
    return _WHITESPACE_RUN_RE.sub(" ", text).strip() or fallback


def _extract(
    read_model: Mapping[str, Any],
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]], int, int]:
    """Canonical view: sorted (id, domain) pairs, sorted unique edges, drop counts.

    Returns the dangling-edge count (unknown endpoints) and duplicate-edge
    count separately so the summary never hides what was excluded.
    """
    if not isinstance(read_model, Mapping):
        raise ExportError(f"read model must be a mapping, got {type(read_model).__name__}")
    nodes: dict[str, str] = {}
    raw_nodes = read_model.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                continue
            node_id = entry.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in nodes:
                continue
            nodes[node_id] = _clean_name(entry.get("domain"), "unknown")
    edges: set[tuple[str, str, str]] = set()
    dangling = 0
    duplicates = 0
    raw_links = read_model.get("links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                dangling += 1
                continue
            source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
            if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)):
                dangling += 1
                continue
            if source not in nodes or target not in nodes:
                dangling += 1
                continue
            key = (source, target, _clean_name(relation, "unknown"))
            if key in edges:
                duplicates += 1
            else:
                edges.add(key)
    return sorted(nodes.items()), sorted(edges), dangling, duplicates


# ------------------------------------------------------------------ producer


def build_summary(read_model: Mapping[str, Any]) -> dict[str, Any]:
    """Build the canonical graph-summary mapping."""
    nodes, edges, dangling, duplicates = _extract(read_model)
    domain_of = dict(nodes)

    in_degree = {node_id: 0 for node_id, _domain in nodes}
    out_degree = {node_id: 0 for node_id, _domain in nodes}
    for source, target, _relation in edges:
        out_degree[source] += 1
        in_degree[target] += 1

    histogram: dict[str, int] = {}
    for node_id, _domain in nodes:
        total = in_degree[node_id] + out_degree[node_id]
        histogram[str(total)] = histogram.get(str(total), 0) + 1
    degrees = [in_degree[node_id] + out_degree[node_id] for node_id, _domain in nodes]
    if degrees:
        mean = serialize_score(Decimal(sum(degrees)) / Decimal(len(degrees)))
        min_total, max_total = min(degrees), max(degrees)
    else:
        mean, min_total, max_total = "0.000000000000", 0, 0

    domains: dict[str, dict[str, int]] = {}
    for _node_id, domain in nodes:
        bucket = domains.setdefault(domain, {"nodes": 0, "internal_edges": 0, "boundary_in": 0, "boundary_out": 0})
        bucket["nodes"] += 1
    for source, target, _relation in edges:
        source_domain, target_domain = domain_of[source], domain_of[target]
        if source_domain == target_domain:
            domains[source_domain]["internal_edges"] += 1
        else:
            domains[source_domain]["boundary_out"] += 1
            domains[target_domain]["boundary_in"] += 1

    relations: dict[str, int] = {}
    for _source, _target, relation in edges:
        relations[relation] = relations.get(relation, 0) + 1

    return {
        "artifact": "graph-summary",
        "schema_version": 1,
        "counts": {
            "nodes": len(nodes),
            "edges": len(edges),
            "self_loops": sum(1 for source, target, _relation in edges if source == target),
            "isolated_nodes": histogram.get("0", 0),
            "dangling_edges": dangling,
            "duplicate_edges": duplicates,
        },
        "degree": {
            "histogram": histogram,
            "min_total": min_total,
            "max_total": max_total,
            "mean_total": mean,
        },
        "domains": domains,
        "relations": relations,
    }


def produce_summary(read_model: Mapping[str, Any], *, max_bytes: int = SUMMARY_MAX_BYTES) -> bytes:
    """UTF-8 encoded canonical summary JSON, fail-closed on the byte cap."""
    if max_bytes < 1:
        raise ExportError(f"max_bytes must be positive, got {max_bytes!r}")
    data = (canonical_json(build_summary(read_model)) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_ARTIFACT_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_summary(data)
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_summary(data: bytes) -> None:
    """Parse *data* back and assert the canonical shape plus invariants."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is not UTF-8: {error}") from error
    try:
        parsed = json.loads(text)
    except json.JSONDecodeError as error:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} does not parse as JSON: {error}") from error
    if not isinstance(parsed, dict):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} must be a JSON object, got {type(parsed).__name__}")
    if (canonical_json(parsed) + "\n").encode("utf-8") != data:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is not in canonical-JSON form")
    if parsed.get("artifact") != "graph-summary" or parsed.get("schema_version") != 1:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a non-canonical artifact marker")

    counts = parsed.get("counts")
    degree = parsed.get("degree")
    domains = parsed.get("domains")
    relations = parsed.get("relations")
    if not all(isinstance(section, dict) for section in (counts, degree, domains, relations)):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} lacks its canonical sections")
    assert isinstance(counts, dict) and isinstance(degree, dict)  # for the type checker
    assert isinstance(domains, dict) and isinstance(relations, dict)

    count_keys = {"nodes", "edges", "self_loops", "isolated_nodes", "dangling_edges", "duplicate_edges"}
    if set(counts) != count_keys or not all(isinstance(value, int) and value >= 0 for value in counts.values()):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a non-canonical counts object")

    histogram = degree.get("histogram")
    if not isinstance(histogram, dict):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} lacks a degree histogram")
    for key, value in histogram.items():
        if _INT_KEY_RE.fullmatch(key) is None or not isinstance(value, int) or value < 1:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a malformed degree histogram entry")
    if sum(histogram.values()) != counts["nodes"]:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} histogram does not sum to the node count")
    if sum(int(key) * value for key, value in histogram.items()) != 2 * counts["edges"]:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} degree mass disagrees with the edge count")
    if histogram.get("0", 0) != counts["isolated_nodes"]:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} isolated count disagrees with the histogram")
    if not isinstance(degree.get("min_total"), int) or not isinstance(degree.get("max_total"), int):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} degree bounds must be integers")
    mean = degree.get("mean_total")
    if not isinstance(mean, str) or _QUANTIZED_RE.fullmatch(mean) is None:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} mean_total is not a quantized 12-decimal string")

    node_sum = 0
    boundary_in = 0
    boundary_out = 0
    internal_sum = 0
    for domain, bucket in domains.items():
        if not isinstance(domain, str) or not isinstance(bucket, dict):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a malformed domain bucket")
        if set(bucket) != {"nodes", "internal_edges", "boundary_in", "boundary_out"} or not all(
            isinstance(value, int) and value >= 0 for value in bucket.values()
        ):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} domain {domain!r} has a non-canonical bucket")
        node_sum += bucket["nodes"]
        boundary_in += bucket["boundary_in"]
        boundary_out += bucket["boundary_out"]
        internal_sum += bucket["internal_edges"]
    if node_sum != counts["nodes"]:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} domain node counts do not sum to the node count")
    if boundary_in != boundary_out:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} boundary in/out counts are not symmetric")
    if internal_sum + boundary_out != counts["edges"]:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} domain edge counts do not sum to the edge count")

    if not all(isinstance(name, str) and isinstance(value, int) and value >= 1 for name, value in relations.items()):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a malformed relation count")
    if sum(relations.values()) != counts["edges"]:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} relation counts do not sum to the edge count")


# ------------------------------------------------------------------ registry


def register_summary(
    registry: ExportRegistry,
    *,
    artifact_name: str = _ARTIFACT_NAME,
    max_bytes: int = SUMMARY_MAX_BYTES,
) -> None:
    """Register the graph-summary artifact on *registry* (analytics kind)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_summary(model, max_bytes=max_bytes)

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="analytics",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_summary,
        )
    )

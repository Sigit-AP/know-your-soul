"""Canonical Cytoscape.js elements JSON exporter (ATM wave-2 formats).

Clean-room implementation of the public Cytoscape.js elements convention:
``elements.nodes`` / ``elements.edges`` arrays whose entries carry a single
``data`` record; nodes expose ``id``/``label``/``domain``/``kind``/
``version``, edges expose a sequential synthetic ``id`` plus
``source``/``target``/``relation``.

Contract:

* Deterministic, byte-identical output: nodes sorted by id, edges sorted
  by ``(source, target, relation)`` and deduplicated, sequential edge ids
  (``e000000``…), canonical JSON (sorted keys, compact separators, UTF-8
  passthrough) with a single trailing newline — no wall-clock, no RNG.
* Labels pass through ``security.sanitize_text`` (bidi/zero-width/control
  stripping), surviving whitespace controls collapse to single spaces,
  and the result is char-capped.
* A node whose id sanitizes (control/format characters) is malformed and
  dropped; edges with unknown endpoints are dropped; self-loops are kept.
* Explicit fail-closed caps (bytes, nodes, edges) via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  :func:`roundtrip_cytoscape` parses the bytes back, re-serializes to
  prove canonical form, and asserts the structural invariants.

No AI, no network, stdlib only.
"""

from __future__ import annotations

import json
import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.security import sanitize_text

__all__ = [
    "CYTOSCAPE_LABEL_MAX_CHARS",
    "CYTOSCAPE_MAX_BYTES",
    "CYTOSCAPE_MAX_EDGES",
    "CYTOSCAPE_MAX_NODES",
    "build_cytoscape",
    "produce_cytoscape",
    "register_cytoscape",
    "roundtrip_cytoscape",
]

CYTOSCAPE_MAX_BYTES = 4 * 1024 * 1024
CYTOSCAPE_MAX_NODES = 4096
CYTOSCAPE_MAX_EDGES = 16384
CYTOSCAPE_LABEL_MAX_CHARS = 256

_ARTIFACT_NAME = "exports/cytoscape.json"
_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")
_EDGE_ID_RE = re.compile(r"^e\d{6}$")


# --------------------------------------------------------------- primitives


def _clean_label(raw: Any, fallback: str, *, max_chars: int) -> str:
    """Sanitize, collapse whitespace controls, cap chars."""
    if raw in (None, ""):
        return fallback[:max_chars]
    text = sanitize_text(str(raw)[: max_chars + 1])
    collapsed = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    return collapsed[:max_chars] if collapsed else fallback[:max_chars]


def _clean_id(raw: Any) -> str | None:
    """A node id must survive sanitization unchanged; otherwise it is dropped."""
    if not isinstance(raw, str) or not raw:
        return None
    return raw if sanitize_text(raw) == raw else None


def _extract(
    read_model: Mapping[str, Any], *, label_max_chars: int
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Canonical view: sorted node dicts and sorted unique edges.

    Malformed rows, duplicate ids and edges with unknown endpoints are
    dropped; self-loops are kept.
    """
    if not isinstance(read_model, Mapping):
        raise ExportError(f"read model must be a mapping, got {type(read_model).__name__}")
    nodes: dict[str, dict[str, Any]] = {}
    raw_nodes = read_model.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                continue
            node_id = _clean_id(entry.get("id"))
            if node_id is None or node_id in nodes:
                continue
            version = entry.get("version")
            nodes[node_id] = {
                "id": node_id,
                "label": _clean_label(entry.get("label"), node_id, max_chars=label_max_chars),
                "domain": _clean_label(entry.get("domain"), "", max_chars=64),
                "kind": _clean_label(entry.get("kind"), "", max_chars=64),
                "version": version
                if isinstance(version, int) and not isinstance(version, bool) and version >= 0
                else 0,
            }
    seen: set[tuple[str, str, str]] = set()
    raw_links = read_model.get("links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                continue
            source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
            if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)):
                continue
            if source not in nodes or target not in nodes:
                continue
            seen.add((source, target, _clean_label(relation, "related", max_chars=128)))
    return [nodes[node_id] for node_id in sorted(nodes)], sorted(seen)


def _check_caps(node_count: int, edge_count: int, *, max_nodes: int, max_edges: int) -> None:
    if max_nodes < 1 or max_edges < 1:
        raise ExportError(f"max_nodes/max_edges must be positive, got {max_nodes!r}/{max_edges!r}")
    if node_count > max_nodes:
        raise ExportCapExceededError(
            f"graph has {node_count} nodes, over the {max_nodes}-node cap for {_ARTIFACT_NAME}"
        )
    if edge_count > max_edges:
        raise ExportCapExceededError(
            f"graph has {edge_count} edges, over the {max_edges}-edge cap for {_ARTIFACT_NAME}"
        )


# ------------------------------------------------------------------ producer


def build_cytoscape(
    read_model: Mapping[str, Any], *, label_max_chars: int = CYTOSCAPE_LABEL_MAX_CHARS
) -> dict[str, Any]:
    """Build the canonical Cytoscape.js elements mapping."""
    if label_max_chars < 1:
        raise ExportError(f"label_max_chars must be positive, got {label_max_chars!r}")
    nodes, edges = _extract(read_model, label_max_chars=label_max_chars)
    return {
        "artifact": "cytoscape",
        "schema_version": 1,
        "elements": {
            "nodes": [
                {
                    "data": {
                        "id": node["id"],
                        "label": node["label"],
                        "domain": node["domain"],
                        "kind": node["kind"],
                        "version": node["version"],
                    }
                }
                for node in nodes
            ],
            "edges": [
                {
                    "data": {
                        "id": f"e{index:06d}",
                        "source": source,
                        "target": target,
                        "relation": relation,
                    }
                }
                for index, (source, target, relation) in enumerate(edges)
            ],
        },
    }


def produce_cytoscape(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = CYTOSCAPE_MAX_BYTES,
    max_nodes: int = CYTOSCAPE_MAX_NODES,
    max_edges: int = CYTOSCAPE_MAX_EDGES,
    label_max_chars: int = CYTOSCAPE_LABEL_MAX_CHARS,
) -> bytes:
    """UTF-8 encoded canonical Cytoscape.js JSON, fail-closed on every cap."""
    if max_bytes < 1:
        raise ExportError(f"max_bytes must be positive, got {max_bytes!r}")
    nodes, edges = _extract(read_model, label_max_chars=label_max_chars)
    _check_caps(len(nodes), len(edges), max_nodes=max_nodes, max_edges=max_edges)
    data = (canonical_json(build_cytoscape(read_model, label_max_chars=label_max_chars)) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_ARTIFACT_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_cytoscape(data)
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_cytoscape(data: bytes) -> None:
    """Parse *data* back and assert the exact canonical Cytoscape.js shape."""
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
    if parsed.get("artifact") != "cytoscape" or parsed.get("schema_version") != 1:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a non-canonical artifact marker")
    elements = parsed.get("elements")
    if not isinstance(elements, dict) or set(elements) != {"nodes", "edges"}:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} lacks the canonical elements object")
    raw_nodes, raw_edges = elements["nodes"], elements["edges"]
    if not isinstance(raw_nodes, list) or not isinstance(raw_edges, list):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} elements must be arrays")

    node_ids: list[str] = []
    for entry in raw_nodes:
        record = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(record, dict) or not isinstance(record.get("id"), str) or not record["id"]:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a node without a data.id")
        label = record.get("label")
        if not isinstance(label, str) or any(unicodedata.category(char) in ("Cc", "Cf") for char in label):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries an unsanitized label")
        node_ids.append(record["id"])
    if node_ids != sorted(node_ids) or len(set(node_ids)) != len(node_ids):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} nodes are out of canonical order or duplicate")

    known = set(node_ids)
    edge_keys: list[tuple[str, str, str]] = []
    for position, entry in enumerate(raw_edges):
        record = entry.get("data") if isinstance(entry, dict) else None
        if not isinstance(record, dict):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a malformed edge")
        edge_id = record.get("id")
        if not isinstance(edge_id, str) or _EDGE_ID_RE.fullmatch(edge_id) is None or int(edge_id[1:]) != position:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge id {edge_id!r} is out of sequence")
        source, target, relation = record.get("source"), record.get("target"), record.get("relation")
        if source not in known or target not in known:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge {edge_id!r} has an unknown endpoint")
        if not isinstance(relation, str) or not relation:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge {edge_id!r} lacks a relation")
        edge_keys.append((source, target, relation))
    if edge_keys != sorted(edge_keys) or len(set(edge_keys)) != len(edge_keys):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edges are out of canonical order or duplicate")


# ------------------------------------------------------------------ registry


def register_cytoscape(
    registry: ExportRegistry,
    *,
    artifact_name: str = _ARTIFACT_NAME,
    max_bytes: int = CYTOSCAPE_MAX_BYTES,
    max_nodes: int = CYTOSCAPE_MAX_NODES,
    max_edges: int = CYTOSCAPE_MAX_EDGES,
    label_max_chars: int = CYTOSCAPE_LABEL_MAX_CHARS,
) -> None:
    """Register the Cytoscape.js format on *registry* (wave-2 seam into F-B8)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_cytoscape(
            model, max_bytes=max_bytes, max_nodes=max_nodes, max_edges=max_edges, label_max_chars=label_max_chars
        )

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="format",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_cytoscape,
        )
    )

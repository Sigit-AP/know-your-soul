"""Canonical D3-force node-link JSON exporter (ATM wave-2 formats).

Clean-room implementation of the public D3-force simulation data
convention: a ``nodes`` array where each entry carries an integer
``index`` equal to its position, and a ``links`` array whose
``source``/``target`` reference nodes *by that index*. Nodes also carry
``group`` (the primary domain, the field D3 colour scales conventionally
key on) alongside ``id``/``label``/``domain``.

Contract:

* Deterministic, byte-identical output: nodes sorted by id (index equals
  sorted position), links sorted by ``(source, target, relation)`` over
  the *string* node ids and deduplicated, sequential link ``index``
  values, canonical JSON with a single trailing newline — no wall-clock,
  no RNG.
* Labels pass through ``security.sanitize_text`` (bidi/zero-width/control
  stripping), surviving whitespace controls collapse to single spaces,
  and the result is char-capped.
* A node whose id sanitizes (control/format characters) is malformed and
  dropped; edges with unknown endpoints are dropped; self-loops are kept.
* Explicit fail-closed caps (bytes, nodes, edges) via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  :func:`roundtrip_d3force` parses the bytes back, re-serializes to prove
  canonical form, and asserts the index invariants (index == position,
  link endpoints in range, links resolvable to sorted id pairs).

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
    "D3FORCE_LABEL_MAX_CHARS",
    "D3FORCE_MAX_BYTES",
    "D3FORCE_MAX_EDGES",
    "D3FORCE_MAX_NODES",
    "build_d3force",
    "produce_d3force",
    "register_d3force",
    "roundtrip_d3force",
]

D3FORCE_MAX_BYTES = 4 * 1024 * 1024
D3FORCE_MAX_NODES = 4096
D3FORCE_MAX_EDGES = 16384
D3FORCE_LABEL_MAX_CHARS = 256

_ARTIFACT_NAME = "exports/d3-force.json"
_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")


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
    """Canonical view: sorted node dicts and sorted unique edges."""
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
            nodes[node_id] = {
                "id": node_id,
                "label": _clean_label(entry.get("label"), node_id, max_chars=label_max_chars),
                "domain": _clean_label(entry.get("domain"), "", max_chars=64),
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


# ------------------------------------------------------------------ producer


def build_d3force(read_model: Mapping[str, Any], *, label_max_chars: int = D3FORCE_LABEL_MAX_CHARS) -> dict[str, Any]:
    """Build the canonical D3-force node-link mapping."""
    if label_max_chars < 1:
        raise ExportError(f"label_max_chars must be positive, got {label_max_chars!r}")
    nodes, edges = _extract(read_model, label_max_chars=label_max_chars)
    position = {node["id"]: index for index, node in enumerate(nodes)}
    return {
        "artifact": "d3-force",
        "schema_version": 1,
        "nodes": [
            {
                "id": node["id"],
                "index": index,
                "label": node["label"],
                "domain": node["domain"],
                "group": node["domain"],
            }
            for index, node in enumerate(nodes)
        ],
        "links": [
            {
                "index": index,
                "source": position[source],
                "target": position[target],
                "relation": relation,
            }
            for index, (source, target, relation) in enumerate(edges)
        ],
    }


def produce_d3force(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = D3FORCE_MAX_BYTES,
    max_nodes: int = D3FORCE_MAX_NODES,
    max_edges: int = D3FORCE_MAX_EDGES,
    label_max_chars: int = D3FORCE_LABEL_MAX_CHARS,
) -> bytes:
    """UTF-8 encoded canonical D3-force JSON, fail-closed on every cap."""
    if max_bytes < 1:
        raise ExportError(f"max_bytes must be positive, got {max_bytes!r}")
    if max_nodes < 1 or max_edges < 1:
        raise ExportError(f"max_nodes/max_edges must be positive, got {max_nodes!r}/{max_edges!r}")
    nodes, edges = _extract(read_model, label_max_chars=label_max_chars)
    if len(nodes) > max_nodes:
        raise ExportCapExceededError(
            f"graph has {len(nodes)} nodes, over the {max_nodes}-node cap for {_ARTIFACT_NAME}"
        )
    if len(edges) > max_edges:
        raise ExportCapExceededError(
            f"graph has {len(edges)} edges, over the {max_edges}-edge cap for {_ARTIFACT_NAME}"
        )
    data = (canonical_json(build_d3force(read_model, label_max_chars=label_max_chars)) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_ARTIFACT_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_d3force(data)
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_d3force(data: bytes) -> None:
    """Parse *data* back and assert the exact canonical D3-force shape."""
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
    if parsed.get("artifact") != "d3-force" or parsed.get("schema_version") != 1:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a non-canonical artifact marker")
    nodes, links = parsed.get("nodes"), parsed.get("links")
    if not isinstance(nodes, list) or not isinstance(links, list):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} nodes/links must be arrays")

    node_ids: list[str] = []
    for position, node in enumerate(nodes):
        if not isinstance(node, dict) or not isinstance(node.get("id"), str) or not node["id"]:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a node without an id")
        if node.get("index") != position:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} node index must equal its sorted position")
        label = node.get("label")
        if not isinstance(label, str) or any(unicodedata.category(char) in ("Cc", "Cf") for char in label):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries an unsanitized label")
        if node.get("group") != node.get("domain"):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} node group must equal its domain")
        node_ids.append(node["id"])
    if node_ids != sorted(node_ids) or len(set(node_ids)) != len(node_ids):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} nodes are out of canonical order or duplicate")

    link_keys: list[tuple[str, str, str]] = []
    for position, link in enumerate(links):
        if not isinstance(link, dict) or link.get("index") != position:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} link index {position} is out of sequence")
        source, target = link.get("source"), link.get("target")
        if (
            not isinstance(source, int)
            or not isinstance(target, int)
            or isinstance(source, bool)
            or isinstance(target, bool)
        ):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} link endpoints must be integer indices")
        if not 0 <= source < len(node_ids) or not 0 <= target < len(node_ids):
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} link endpoint out of range")
        relation = link.get("relation")
        if not isinstance(relation, str) or not relation:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} link lacks a relation")
        link_keys.append((node_ids[source], node_ids[target], relation))
    if link_keys != sorted(link_keys) or len(set(link_keys)) != len(link_keys):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} links are out of canonical order or duplicate")


# ------------------------------------------------------------------ registry


def register_d3force(
    registry: ExportRegistry,
    *,
    artifact_name: str = _ARTIFACT_NAME,
    max_bytes: int = D3FORCE_MAX_BYTES,
    max_nodes: int = D3FORCE_MAX_NODES,
    max_edges: int = D3FORCE_MAX_EDGES,
    label_max_chars: int = D3FORCE_LABEL_MAX_CHARS,
) -> None:
    """Register the D3-force format on *registry* (wave-2 seam into F-B8)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_d3force(
            model, max_bytes=max_bytes, max_nodes=max_nodes, max_edges=max_edges, label_max_chars=label_max_chars
        )

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="format",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_d3force,
        )
    )

"""Canonical Trivial Graph Format (TGF) exporter (ATM wave-2 formats).

Clean-room implementation of the public Trivial Graph Format convention: a
node section (one ``<id> <label>`` line per node), a lone ``#`` separator
line, then an edge section (one ``<source> <target> <label>`` line per
edge). TGF node ids are integers, so this exporter assigns ``1..N`` in
sorted node-id order — the mapping is fully determined by the content.

Contract:

* Deterministic, byte-identical output: nodes sorted by id, edges sorted
  by ``(source, target, relation)`` and deduplicated, LF line endings, a
  single trailing newline — no wall-clock, no RNG.
* Labels and relations pass through ``security.sanitize_text``
  (bidi/zero-width/control stripping), surviving whitespace controls
  collapse to single spaces (a CR/LF can never smuggle a fake record),
  and the result is char-capped. An empty sanitized label falls back to
  the node id.
* Explicit fail-closed caps (bytes, nodes, edges) via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  :func:`roundtrip_tgf` parses the bytes back and asserts the exact
  canonical structure (sequential ids, sections, ordering, endpoints).

No AI, no network, stdlib only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.security import sanitize_text

__all__ = [
    "TGF_LABEL_MAX_CHARS",
    "TGF_MAX_BYTES",
    "TGF_MAX_EDGES",
    "TGF_MAX_NODES",
    "produce_tgf",
    "register_tgf",
    "render_tgf",
    "roundtrip_tgf",
]

TGF_MAX_BYTES = 2 * 1024 * 1024
TGF_MAX_NODES = 8192
TGF_MAX_EDGES = 32768
TGF_LABEL_MAX_CHARS = 80

_ARTIFACT_NAME = "exports/graph.tgf"
_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")
_NODE_LINE_RE = re.compile(r"^(\d+)(?: (.*))?$")
_EDGE_LINE_RE = re.compile(r"^(\d+) (\d+)(?: (.*))?$")


# --------------------------------------------------------------- primitives


def _clean_label(raw: Any, fallback: str, *, max_chars: int) -> str:
    """Sanitize, collapse whitespace controls, cap chars."""
    if raw in (None, ""):
        return fallback[:max_chars]
    text = sanitize_text(str(raw)[: max_chars + 1])
    collapsed = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    return collapsed[:max_chars] if collapsed else fallback[:max_chars]


def _extract(
    read_model: Mapping[str, Any], *, label_max_chars: int
) -> tuple[list[tuple[str, str]], list[tuple[str, str, str]]]:
    """Canonical view: sorted ``(node_id, label)`` pairs and sorted unique edges.

    Malformed rows, duplicate ids and edges with unknown endpoints are
    dropped; self-loops are kept.
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
            nodes[node_id] = _clean_label(entry.get("label"), node_id, max_chars=label_max_chars)
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
    return [(node_id, nodes[node_id]) for node_id in sorted(nodes)], sorted(seen)


# ------------------------------------------------------------------ producer


def render_tgf(read_model: Mapping[str, Any], *, label_max_chars: int = TGF_LABEL_MAX_CHARS) -> str:
    """Render the read model as canonical TGF text."""
    if label_max_chars < 1:
        raise ExportError(f"label_max_chars must be positive, got {label_max_chars!r}")
    nodes, edges = _extract(read_model, label_max_chars=label_max_chars)
    number = {node_id: index + 1 for index, (node_id, _label) in enumerate(nodes)}
    lines = [f"{number[node_id]} {label}" for node_id, label in nodes]
    lines.append("#")
    lines.extend(f"{number[source]} {number[target]} {relation}" for source, target, relation in edges)
    return "\n".join(lines) + "\n"


def produce_tgf(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = TGF_MAX_BYTES,
    max_nodes: int = TGF_MAX_NODES,
    max_edges: int = TGF_MAX_EDGES,
    label_max_chars: int = TGF_LABEL_MAX_CHARS,
) -> bytes:
    """UTF-8 encoded canonical TGF, fail-closed on every explicit cap."""
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
    data = render_tgf(read_model, label_max_chars=label_max_chars).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_ARTIFACT_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_tgf(data)
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_tgf(data: bytes) -> None:
    """Parse *data* back and assert the exact canonical TGF structure."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is not UTF-8: {error}") from error
    if not text.endswith("\n"):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is truncated")
    lines = text[:-1].split("\n")
    for line in lines:
        for char in line:
            if unicodedata.category(char) in ("Cc", "Cf"):
                raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries an unsanitized control/format character")
    if "#" not in lines:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is missing its '#' separator")
    if lines.count("#") != 1:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries more than one separator")
    split = lines.index("#")
    node_lines, edge_lines = lines[:split], lines[split + 1 :]

    for expected, line in enumerate(node_lines, start=1):
        match = _NODE_LINE_RE.match(line)
        if match is None:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a malformed node line: {line!r}")
        if int(match.group(1)) != expected:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} node ids are not sequential from 1")
    node_count = len(node_lines)

    edge_keys: list[tuple[int, int, str]] = []
    for line in edge_lines:
        match = _EDGE_LINE_RE.match(line)
        if match is None:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a malformed edge line: {line!r}")
        source, target = int(match.group(1)), int(match.group(2))
        if not 1 <= source <= node_count or not 1 <= target <= node_count:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge references an unknown endpoint")
        relation = match.group(3) or ""
        if not relation:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge lacks a relation label")
        edge_keys.append((source, target, relation))
    if edge_keys != sorted(edge_keys):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edges are out of canonical order")
    if len(set(edge_keys)) != len(edge_keys):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries duplicate edges")


# ------------------------------------------------------------------ registry


def register_tgf(
    registry: ExportRegistry,
    *,
    artifact_name: str = _ARTIFACT_NAME,
    max_bytes: int = TGF_MAX_BYTES,
    max_nodes: int = TGF_MAX_NODES,
    max_edges: int = TGF_MAX_EDGES,
    label_max_chars: int = TGF_LABEL_MAX_CHARS,
) -> None:
    """Register the TGF format on *registry* (the wave-2 seam into F-B8)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_tgf(
            model, max_bytes=max_bytes, max_nodes=max_nodes, max_edges=max_edges, label_max_chars=label_max_chars
        )

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="format",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_tgf,
        )
    )

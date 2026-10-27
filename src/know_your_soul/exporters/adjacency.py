"""Canonical adjacency matrix CSV + edge-list TXT exporters (ATM wave-2).

Two text artifacts over one canonical extraction of the read model:

* :func:`render_adjacency_csv` — a directed adjacency matrix as RFC 4180
  CSV (stdlib ``csv`` writer, CRLF terminators, minimal quoting): the
  header row is ``id,<sorted node ids>`` and each following row is
  ``<node id>`` plus ``"1"``/``"0"`` cells in the same id order; a cell is
  ``1`` when at least one deduplicated edge ``row -> column`` exists, so a
  self-loop marks the diagonal.
* :func:`render_edge_list_txt` — a TAB-separated edge list with a
  three-line header (format marker + node/edge counts): one
  ``source<TAB>target<TAB>relation`` line per deduplicated edge, sorted,
  LF-terminated.

Contract:

* Deterministic, byte-identical output; no wall-clock, no RNG.
* Text hygiene: every field passes ``security.sanitize_text`` and
  whitespace controls collapse to single spaces (TAB/CR/LF can never
  smuggle extra fields or records).
* Explicit fail-closed caps via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`:
  the matrix additionally caps the node count (quadratic growth), the
  edge list caps the edge count, both cap artifact bytes.
* Round-trip validators parse the bytes back and assert header, ordering,
  cell alphabet (``0``/``1``), counts and row width.

No AI, no network, stdlib only.
"""

from __future__ import annotations

import csv
import io
import re
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
    "ADJACENCY_CSV_MAX_BYTES",
    "ADJACENCY_MAX_NODES",
    "EDGE_LIST_MAX_BYTES",
    "EDGE_LIST_MAX_EDGES",
    "produce_adjacency_csv",
    "produce_edge_list_txt",
    "register_adjacency",
    "render_adjacency_csv",
    "render_edge_list_txt",
    "roundtrip_adjacency_csv",
    "roundtrip_edge_list_txt",
]

ADJACENCY_CSV_MAX_BYTES = 16 * 1024 * 1024
ADJACENCY_MAX_NODES = 2048  # the matrix is quadratic in the node count
EDGE_LIST_MAX_BYTES = 4 * 1024 * 1024
EDGE_LIST_MAX_EDGES = 65536

_MATRIX_NAME = "exports/adjacency.csv"
_LIST_NAME = "exports/edges.txt"
_LIST_HEADER = "# know-your-soul edge-list v1"

_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")


# --------------------------------------------------------------- primitives


def _clean_field(raw: Any, *, max_chars: int = 1024) -> str:
    """Sanitize and collapse whitespace controls (fields may contain spaces)."""
    text = sanitize_text(str(raw)[: max_chars + 1])[:max_chars]
    return _WHITESPACE_RUN_RE.sub(" ", text).strip()


def _extract(read_model: Mapping[str, Any]) -> tuple[list[str], list[tuple[str, str, str]]]:
    """Canonical view: sorted node ids and sorted unique edges.

    Malformed rows, duplicate ids, non-string fields and edges with
    unknown endpoints are dropped; self-loops are kept. Fields are
    sanitized so no field can carry a TAB/CR/LF into the text formats.
    """
    if not isinstance(read_model, Mapping):
        raise ExportError(f"read model must be a mapping, got {type(read_model).__name__}")
    node_ids: set[str] = set()
    raw_nodes = read_model.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                continue
            raw_id = entry.get("id")
            if not isinstance(raw_id, str) or not raw_id:
                continue
            node_id = _clean_field(raw_id)
            if node_id:
                node_ids.add(node_id)
    seen: set[tuple[str, str, str]] = set()
    raw_links = read_model.get("links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                continue
            source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
            if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)):
                continue
            source, target, relation = _clean_field(source), _clean_field(target), _clean_field(relation)
            if not source or not target or not relation:
                continue
            if source in node_ids and target in node_ids:
                seen.add((source, target, relation))
    return sorted(node_ids), sorted(seen)


# ------------------------------------------------------------- matrix csv


def render_adjacency_csv(read_model: Mapping[str, Any]) -> str:
    """Render the canonical adjacency-matrix CSV text (CRLF endings)."""
    node_ids, edges = _extract(read_model)
    adjacency: set[tuple[str, str]] = {(source, target) for source, target, _relation in edges}
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")  # RFC 4180 record terminator
    writer.writerow(["id", *node_ids])
    for source in node_ids:
        writer.writerow([source, *["1" if (source, target) in adjacency else "0" for target in node_ids]])
    return buffer.getvalue()


def produce_adjacency_csv(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = ADJACENCY_CSV_MAX_BYTES,
    max_nodes: int = ADJACENCY_MAX_NODES,
) -> bytes:
    """UTF-8 encoded adjacency matrix, fail-closed on the explicit caps."""
    if max_bytes < 1:
        raise ExportError(f"max_bytes must be positive, got {max_bytes!r}")
    if max_nodes < 1:
        raise ExportError(f"max_nodes must be positive, got {max_nodes!r}")
    node_ids, _edges = _extract(read_model)
    if len(node_ids) > max_nodes:
        raise ExportCapExceededError(
            f"graph has {len(node_ids)} nodes, over the {max_nodes}-node cap for {_MATRIX_NAME}"
        )
    data = render_adjacency_csv(read_model).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_MATRIX_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_adjacency_csv(data)
    return data


def roundtrip_adjacency_csv(data: bytes) -> None:
    """Parse the matrix back and assert the exact canonical structure."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact {_MATRIX_NAME} is not UTF-8: {error}") from error
    if not text.endswith("\r\n"):
        raise ExportRoundTripError(f"artifact {_MATRIX_NAME} is truncated or missing its final CRLF")
    for index in range(1, len(text)):
        if text[index] == "\n" and text[index - 1] != "\r":
            raise ExportRoundTripError(f"artifact {_MATRIX_NAME} carries a bare LF; RFC 4180 requires CRLF")
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as error:
        raise ExportRoundTripError(f"artifact {_MATRIX_NAME} does not parse as RFC 4180 CSV: {error}") from error
    if not rows or not rows[0] or rows[0][0] != "id":
        raise ExportRoundTripError(f"artifact {_MATRIX_NAME} carries a non-canonical header")
    header_ids = rows[0][1:]
    if header_ids != sorted(header_ids) or len(set(header_ids)) != len(header_ids):
        raise ExportRoundTripError(f"artifact {_MATRIX_NAME} header ids are out of canonical order")
    for row in rows[1:]:
        if len(row) != len(rows[0]):
            raise ExportRoundTripError(f"artifact {_MATRIX_NAME} carries a row with the wrong column width")
        if row[0] not in set(header_ids):
            raise ExportRoundTripError(f"artifact {_MATRIX_NAME} row id {row[0]!r} is not in the header")
        for cell in row[1:]:
            if cell not in ("0", "1"):
                raise ExportRoundTripError(f"artifact {_MATRIX_NAME} carries a non-binary cell {cell!r}")
    row_ids = [row[0] for row in rows[1:]]
    if row_ids != header_ids:
        raise ExportRoundTripError(f"artifact {_MATRIX_NAME} row order must match the header order")


# --------------------------------------------------------------- edge list


def render_edge_list_txt(read_model: Mapping[str, Any]) -> str:
    """Render the canonical TAB-separated edge-list text (LF endings)."""
    node_ids, edges = _extract(read_model)
    lines = [_LIST_HEADER, f"# nodes: {len(node_ids)}", f"# edges: {len(edges)}"]
    lines.extend(f"{source}\t{target}\t{relation}" for source, target, relation in edges)
    return "\n".join(lines) + "\n"


def produce_edge_list_txt(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = EDGE_LIST_MAX_BYTES,
    max_edges: int = EDGE_LIST_MAX_EDGES,
) -> bytes:
    """UTF-8 encoded edge list, fail-closed on the explicit caps."""
    if max_bytes < 1:
        raise ExportError(f"max_bytes must be positive, got {max_bytes!r}")
    if max_edges < 1:
        raise ExportError(f"max_edges must be positive, got {max_edges!r}")
    _node_ids, edges = _extract(read_model)
    if len(edges) > max_edges:
        raise ExportCapExceededError(f"graph has {len(edges)} edges, over the {max_edges}-edge cap for {_LIST_NAME}")
    data = render_edge_list_txt(read_model).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_LIST_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_edge_list_txt(data)
    return data


def roundtrip_edge_list_txt(data: bytes) -> None:
    """Parse the edge list back and assert the exact canonical structure."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact {_LIST_NAME} is not UTF-8: {error}") from error
    if not text.endswith("\n"):
        raise ExportRoundTripError(f"artifact {_LIST_NAME} is truncated")
    if "\r" in text:
        raise ExportRoundTripError(f"artifact {_LIST_NAME} carries CR characters; the canonical form is LF-only")
    lines = text[:-1].split("\n")
    if len(lines) < 3 or lines[0] != _LIST_HEADER:
        raise ExportRoundTripError(f"artifact {_LIST_NAME} has a non-canonical header")
    counts: dict[str, int] = {}
    for line in lines[1:3]:
        parts = line.split(": ")
        if len(parts) != 2 or parts[0] not in ("# nodes", "# edges"):
            raise ExportRoundTripError(f"artifact {_LIST_NAME} has a non-canonical header count line")
        try:
            counts[parts[0]] = int(parts[1])
        except ValueError as error:
            raise ExportRoundTripError(f"artifact {_LIST_NAME} carries a non-integer count") from error
    body = lines[3:]
    keys: list[tuple[str, str, str]] = []
    for line in body:
        fields = line.split("\t")
        if len(fields) != 3 or not all(fields):
            raise ExportRoundTripError(f"artifact {_LIST_NAME} carries a line without exactly 3 fields")
        keys.append((fields[0], fields[1], fields[2]))
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ExportRoundTripError(f"artifact {_LIST_NAME} lines are out of canonical order or duplicate")
    if counts["# edges"] != len(keys):
        raise ExportRoundTripError(
            f"artifact {_LIST_NAME} count mismatch: declared {counts['# edges']}, actual {len(keys)}"
        )
    endpoints = {endpoint for key in keys for endpoint in key[:2]}
    if counts["# nodes"] < len(endpoints):
        raise ExportRoundTripError(f"artifact {_LIST_NAME} node count is below the referenced endpoint count")


# ------------------------------------------------------------------ registry


def register_adjacency(
    registry: ExportRegistry,
    *,
    matrix_name: str = _MATRIX_NAME,
    list_name: str = _LIST_NAME,
    matrix_max_bytes: int = ADJACENCY_CSV_MAX_BYTES,
    matrix_max_nodes: int = ADJACENCY_MAX_NODES,
    list_max_bytes: int = EDGE_LIST_MAX_BYTES,
    list_max_edges: int = EDGE_LIST_MAX_EDGES,
) -> None:
    """Register both adjacency formats on *registry* (wave-2 seam into F-B8)."""
    registry.register(
        ExportFormat(
            name=matrix_name,
            version=1,
            kind="format",
            max_bytes=matrix_max_bytes,
            producer=lambda model: produce_adjacency_csv(model, max_bytes=matrix_max_bytes, max_nodes=matrix_max_nodes),
            roundtrip=roundtrip_adjacency_csv,
        )
    )
    registry.register(
        ExportFormat(
            name=list_name,
            version=1,
            kind="format",
            max_bytes=list_max_bytes,
            producer=lambda model: produce_edge_list_txt(model, max_bytes=list_max_bytes, max_edges=list_max_edges),
            roundtrip=roundtrip_edge_list_txt,
        )
    )

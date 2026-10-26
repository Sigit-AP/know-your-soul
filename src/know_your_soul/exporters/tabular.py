"""Canonical RFC 4180 tabular exporter (ATM F-B3): ``nodes.csv`` + ``edges.csv``.

Clean-room implementation of the public CSV specification (RFC 4180): fields
containing commas, quotes or CR/LF are quoted, embedded quotes are doubled,
and every record ends with CRLF. Serialization goes through the stdlib
``csv`` module with a pinned ``\\r\\n`` line terminator; nothing here is
hand-rolled escaping.

Contract:

* Deterministic, byte-identical output: node rows sorted by id, edge rows
  sorted by ``(source, target, relation)`` and deduplicated, fixed column
  sets, no wall-clock, no RNG, no BOM.
* **No secret or free-text fields** (plan F-B3): labels, summaries, source
  paths, provenance and context tags never leave the read model here. The
  column sets below are the complete, pinned surface — structural/score
  fields only.
* Scores ride as their quantized 12-decimal strings (spec section 9), never
  as floats; ``degree`` is in+out degree over the deduplicated edge set (a
  self-loop contributes 2), and edges with unknown endpoints are excluded
  from both the file and the degree counts.
* Explicit fail-closed byte caps per file via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  the round-trip validators parse the bytes back and assert header, CRLF
  endings, row width, canonical order and field shapes.

No AI, no network, stdlib only.
"""

from __future__ import annotations

import csv
import io
import re
from collections.abc import Mapping
from decimal import Decimal, InvalidOperation
from typing import Any

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.scoring import serialize_score

__all__ = [
    "EDGE_COLUMNS",
    "EDGES_CSV_MAX_BYTES",
    "NODE_COLUMNS",
    "NODES_CSV_MAX_BYTES",
    "produce_edges_csv",
    "produce_nodes_csv",
    "register_tabular",
    "render_edges_csv",
    "render_nodes_csv",
    "roundtrip_edges_csv",
    "roundtrip_nodes_csv",
]

# The pinned, complete column surfaces (plan F-B3: id, domain, level, P/M/E,
# U, version, status, degree — plus kind and community signature). No
# free-text column exists, so no secret-shaped field can leak by construction.
NODE_COLUMNS: tuple[str, ...] = (
    "id",
    "domain",
    "kind",
    "status",
    "level",
    "p",
    "m",
    "e",
    "u",
    "confidence",
    "version",
    "degree",
    "community_sig",
)
EDGE_COLUMNS: tuple[str, ...] = ("source", "target", "relation", "confidence")

NODES_CSV_MAX_BYTES = 4 * 1024 * 1024
EDGES_CSV_MAX_BYTES = 4 * 1024 * 1024

_ZERO = "0.000000000000"
_ONE = "1.000000000000"

_SCORE_FIELD_RE = re.compile(r"^\d+\.\d{12}$")
_INT_FIELD_RE = re.compile(r"^\d+$")


# --------------------------------------------------------------- primitives


def _nodes(read_model: Mapping[str, Any]) -> list[dict[str, Any]]:
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
    nodes.sort(key=lambda node: str(node.get("id")))
    return nodes


def _edges(read_model: Mapping[str, Any], known: set[str]) -> list[tuple[str, str, str, str]]:
    """Unique sorted ``(source, target, relation, confidence)`` with known endpoints."""
    raw_links = read_model.get("links")
    links = [link for link in raw_links if isinstance(link, dict)] if isinstance(raw_links, list) else []
    return sorted(
        {
            (
                str(link.get("source")),
                str(link.get("target")),
                str(link.get("relation")),
                str(link.get("confidence") or ""),
            )
            for link in links
            if str(link.get("source")) in known and str(link.get("target")) in known
        }
    )


def _degrees(edges: list[tuple[str, str, str, str]]) -> dict[str, int]:
    """In+out degree per node id over the deduplicated edge set."""
    degree: dict[str, int] = {}
    for source, target, _relation, _confidence in edges:
        degree[source] = degree.get(source, 0) + 1
        degree[target] = degree.get(target, 0) + 1
    return degree


def _score(node: dict[str, Any], field: str, default: str) -> str:
    scores = node.get("scores")
    if isinstance(scores, dict):
        value = scores.get({"u": "uncertainty"}.get(field, field))
        if value not in (None, ""):
            # The read model renders exact zero as ``0E-12``; the CSV contract
            # is the fixed-point 12-decimal serialization (spec section 9).
            try:
                return serialize_score(Decimal(str(value)))
            except InvalidOperation:
                return str(value)  # non-numeric scores fail the round-trip
    return default


def _as_int(value: Any, default: int) -> int:
    return value if isinstance(value, int) and not isinstance(value, bool) else default


def _node_confidence(node: dict[str, Any]) -> str:
    """The numeric confidence column: ``scores.confidence`` first.

    The read model also carries a top-level confidence *label* (EXTRACTED /
    INFERRED / AMBIGUOUS); a label is not a score, so the numeric column
    falls back to it only when it actually parses as a number.
    """
    scores = node.get("scores")
    if isinstance(scores, dict):
        value = scores.get("confidence")
        if value not in (None, ""):
            try:
                return serialize_score(Decimal(str(value)))
            except InvalidOperation:
                pass
    raw = node.get("confidence")
    if raw not in (None, ""):
        try:
            return serialize_score(Decimal(str(raw)))
        except InvalidOperation:
            return str(raw)  # non-numeric confidence fails the round-trip
    return _ZERO


def _write_csv(columns: tuple[str, ...], rows: list[list[str]]) -> str:
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\r\n")  # RFC 4180 record terminator
    writer.writerow(list(columns))
    writer.writerows(rows)
    return buffer.getvalue()


# ----------------------------------------------------------------- producers


def render_nodes_csv(read_model: Mapping[str, Any]) -> str:
    """Render the canonical ``nodes.csv`` text (CRLF endings)."""
    nodes = _nodes(read_model)
    known = {str(node.get("id")) for node in nodes}
    degree = _degrees(_edges(read_model, known))
    rows: list[list[str]] = []
    for node in nodes:
        node_id = str(node.get("id"))
        rows.append(
            [
                node_id,
                str(node.get("domain") or ""),
                str(node.get("kind") or ""),
                str(node.get("lifecycle_status") or ""),
                str(_as_int(node.get("professional_level"), 1)),
                _score(node, "p", _ZERO),
                _score(node, "m", _ZERO),
                _score(node, "e", _ZERO),
                _score(node, "u", _ONE),
                _node_confidence(node),
                str(_as_int(node.get("version"), 0)),
                str(degree.get(node_id, 0)),
                str(node.get("community_sig") or ""),
            ]
        )
    return _write_csv(NODE_COLUMNS, rows)


def render_edges_csv(read_model: Mapping[str, Any]) -> str:
    """Render the canonical ``edges.csv`` text (CRLF endings)."""
    nodes = _nodes(read_model)
    known = {str(node.get("id")) for node in nodes}
    rows = [
        [source, target, relation, confidence] for source, target, relation, confidence in _edges(read_model, known)
    ]
    return _write_csv(EDGE_COLUMNS, rows)


def produce_nodes_csv(read_model: Mapping[str, Any], *, max_bytes: int = NODES_CSV_MAX_BYTES) -> bytes:
    """UTF-8 encoded ``nodes.csv``, fail-closed on the explicit byte cap."""
    data = render_nodes_csv(read_model).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact exports/nodes.csv is {len(data)} bytes, over its {max_bytes}-byte cap")
    return data


def produce_edges_csv(read_model: Mapping[str, Any], *, max_bytes: int = EDGES_CSV_MAX_BYTES) -> bytes:
    """UTF-8 encoded ``edges.csv``, fail-closed on the explicit byte cap."""
    data = render_edges_csv(read_model).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact exports/edges.csv is {len(data)} bytes, over its {max_bytes}-byte cap")
    return data


# ---------------------------------------------------------------- round-trip


def _roundtrip_csv(
    artifact: str,
    data: bytes,
    columns: tuple[str, ...],
    *,
    check_row: Any,
) -> list[list[str]]:
    """Shared parse-back: UTF-8, CRLF-only, exact header, exact row width."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact {artifact} is not UTF-8: {error}") from error
    if not text.endswith("\r\n"):
        raise ExportRoundTripError(f"artifact {artifact} is truncated or missing its final CRLF")
    for index in range(1, len(text)):
        if text[index] == "\n" and text[index - 1] != "\r":
            raise ExportRoundTripError(f"artifact {artifact} carries a bare LF; RFC 4180 requires CRLF")
    try:
        rows = list(csv.reader(io.StringIO(text)))
    except csv.Error as error:
        raise ExportRoundTripError(f"artifact {artifact} does not parse as RFC 4180 CSV: {error}") from error
    if not rows or rows[0] != list(columns):
        raise ExportRoundTripError(f"artifact {artifact} carries a non-canonical header")
    for row in rows[1:]:
        if len(row) != len(columns):
            raise ExportRoundTripError(f"artifact {artifact} carries a row with the wrong number of columns")
        check_row(row)
    keys = [tuple(row) for row in rows[1:]]
    if keys != sorted(keys) or len(set(keys)) != len(keys):
        raise ExportRoundTripError(f"artifact {artifact} rows are out of canonical order")
    return rows


def roundtrip_nodes_csv(data: bytes) -> None:
    """Parse ``nodes.csv`` bytes back and assert the exact canonical structure."""

    def _check(row: list[str]) -> None:
        for field in (row[5], row[6], row[7], row[8], row[9]):  # p, m, e, u, confidence
            if _SCORE_FIELD_RE.fullmatch(field) is None:
                raise ExportRoundTripError("artifact exports/nodes.csv carries a non-quantized score field")
        for field in (row[4], row[10], row[11]):  # level, version, degree
            if _INT_FIELD_RE.fullmatch(field) is None:
                raise ExportRoundTripError("artifact exports/nodes.csv carries a non-integer numeric field")

    rows = _roundtrip_csv("exports/nodes.csv", data, NODE_COLUMNS, check_row=_check)
    ids = [row[0] for row in rows[1:]]
    if ids != sorted(ids):
        raise ExportRoundTripError("artifact exports/nodes.csv rows are out of canonical order")


def roundtrip_edges_csv(data: bytes) -> None:
    """Parse ``edges.csv`` bytes back and assert the exact canonical structure."""

    def _check(row: list[str]) -> None:
        if not row[0] or not row[1] or not row[2]:
            raise ExportRoundTripError("artifact exports/edges.csv carries an empty endpoint or relation")

    _roundtrip_csv("exports/edges.csv", data, EDGE_COLUMNS, check_row=_check)


# ------------------------------------------------------------------ registry


def register_tabular(
    registry: ExportRegistry,
    *,
    nodes_name: str = "exports/nodes.csv",
    edges_name: str = "exports/edges.csv",
    nodes_max_bytes: int = NODES_CSV_MAX_BYTES,
    edges_max_bytes: int = EDGES_CSV_MAX_BYTES,
) -> None:
    """Register both CSV formats on *registry* (the wave-B3 seam into F-B8)."""
    registry.register(
        ExportFormat(
            name=nodes_name,
            version=1,
            kind="format",
            max_bytes=nodes_max_bytes,
            producer=lambda model: produce_nodes_csv(model, max_bytes=nodes_max_bytes),
            roundtrip=roundtrip_nodes_csv,
        )
    )
    registry.register(
        ExportFormat(
            name=edges_name,
            version=1,
            kind="format",
            max_bytes=edges_max_bytes,
            producer=lambda model: produce_edges_csv(model, max_bytes=edges_max_bytes),
            roundtrip=roundtrip_edges_csv,
        )
    )

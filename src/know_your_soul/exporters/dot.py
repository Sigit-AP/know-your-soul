"""Canonical Graphviz DOT exporter (ATM F-B1).

Clean-room implementation of the public Graphviz DOT language: the only
borrowed *idea* is "serialize the node-link read model as a sorted graph
text" (atm-feature-plan F-B1); every byte is written by this module.

Contract:

* Deterministic, byte-identical output: nodes sorted by id, edges sorted by
  ``(source, target, relation)`` and deduplicated, no wall-clock, no RNG.
* Labels pass through ``security.sanitize_text`` (bidi/zero-width/control
  stripping), surviving whitespace controls collapse to single spaces, and
  the result is char-capped before DOT escaping (``\\`` and ``"`` escaped).
* One fill color per primary domain P1-P7 from **our own palette**
  (:data:`DOMAIN_COLORS` — chosen for this project, not taken from any
  upstream color list); unknown domains get :data:`FALLBACK_COLOR`.
* Fail-closed cap: :func:`produce_dot` raises
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`
  when the encoded output exceeds the explicit byte budget.
* :func:`roundtrip_dot` parses the bytes back and asserts the exact
  canonical structure (header, ordering, palette membership, escaping), so a
  defective artifact fails closed before publication.

No AI, no network, stdlib only.
"""

from __future__ import annotations

import re
import unicodedata
from collections.abc import Mapping
from typing import Any

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.security import sanitize_text

__all__ = [
    "DEFAULT_DOT_MAX_BYTES",
    "DEFAULT_DOT_LABEL_MAX_CHARS",
    "DOMAIN_COLORS",
    "FALLBACK_COLOR",
    "produce_dot",
    "register_dot",
    "render_dot",
    "roundtrip_dot",
]

# Explicit per-format caps (plan section 5, point 5): a breach is a typed,
# fail-closed error — never a silent truncation.
DEFAULT_DOT_MAX_BYTES = 2 * 1024 * 1024
DEFAULT_DOT_LABEL_MAX_CHARS = 80

# Our own per-domain palette (chosen for Know Your Soul; deliberately not any
# upstream color list — see atm-feature-plan section 2, the palette row).
DOMAIN_COLORS: dict[str, str] = {
    "P1": "#1B6CA8",  # Pembelajaran — deep blue
    "P2": "#2E8B57",  # Gagasan — sea green
    "P3": "#E8A33D",  # Tindakan — amber
    "P4": "#C0392B",  # Keputusan — brick red
    "P5": "#7D5BA6",  # Pemikiran/Penalaran — violet
    "P6": "#5D6D7E",  # Disiplin — slate
    "P7": "#B03A5B",  # Level Profesional — raspberry
}
FALLBACK_COLOR = "#808080"

_GRAPH_NAME = "soul_knowledge"

_NODE_LINE_RE = re.compile(r'^  "((?:[^"\\]|\\.)*)" \[label="((?:[^"\\]|\\.)*)", fillcolor="(#[0-9A-F]{6})"\];$')
_EDGE_LINE_RE = re.compile(r'^  "((?:[^"\\]|\\.)*)" -> "((?:[^"\\]|\\.)*)" \[label="((?:[^"\\]|\\.)*)"\];$')

_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")


# --------------------------------------------------------------- primitives


def _extract(read_model: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Canonical view of the read model: sorted node dicts + sorted unique edges.

    Defensive against malformed rows (non-dict entries are dropped); edges
    referencing unknown endpoints and duplicate ``(source, target, relation)``
    triples are excluded so the output is a faithful simple digraph.
    """
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
    nodes.sort(key=lambda node: str(node.get("id")))
    known = {str(node.get("id")) for node in nodes}
    raw_links = read_model.get("links")
    links = [link for link in raw_links if isinstance(link, dict)] if isinstance(raw_links, list) else []
    edges = sorted(
        {
            (str(link.get("source")), str(link.get("target")), str(link.get("relation")))
            for link in links
            if str(link.get("source")) in known and str(link.get("target")) in known
        }
    )
    return nodes, edges


def _clean_label(raw: Any, fallback: str, *, max_chars: int) -> str:
    """Sanitize, collapse whitespace controls, cap chars — before escaping."""
    text = sanitize_text(str(raw)) if raw not in (None, "") else fallback
    collapsed = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    return collapsed[:max_chars] if collapsed else fallback[:max_chars]


def _dot_escape(text: str) -> str:
    """Escape a string for a DOT double-quoted context."""
    return text.replace("\\", "\\\\").replace('"', '\\"')


def _dot_unescape(text: str) -> str:
    """Inverse of :func:`_dot_escape` for round-trip verification."""
    out: list[str] = []
    index = 0
    while index < len(text):
        char = text[index]
        if char == "\\" and index + 1 < len(text):
            out.append(text[index + 1])
            index += 2
        else:
            out.append(char)
            index += 1
    return "".join(out)


# ------------------------------------------------------------------ producer


def render_dot(read_model: Mapping[str, Any], *, label_max_chars: int = DEFAULT_DOT_LABEL_MAX_CHARS) -> str:
    """Render the read model as canonical DOT text (nodes, then edges)."""
    if label_max_chars < 1:
        raise ValueError("label_max_chars must be positive")
    nodes, edges = _extract(read_model)
    lines = [
        f"digraph {_GRAPH_NAME} {{",
        '  graph [rankdir="LR"];',
        '  node [shape="box", style="filled"];',
    ]
    for node in nodes:
        node_id = str(node.get("id"))
        label = _clean_label(node.get("label"), node_id, max_chars=label_max_chars)
        color = DOMAIN_COLORS.get(str(node.get("domain")), FALLBACK_COLOR)
        lines.append(f'  "{_dot_escape(node_id)}" [label="{_dot_escape(label)}", fillcolor="{color}"];')
    for source, target, relation in edges:
        lines.append(f'  "{_dot_escape(source)}" -> "{_dot_escape(target)}" [label="{_dot_escape(relation)}"];')
    lines.append("}")
    return "\n".join(lines) + "\n"


def produce_dot(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_DOT_MAX_BYTES,
    label_max_chars: int = DEFAULT_DOT_LABEL_MAX_CHARS,
) -> bytes:
    """UTF-8 encoded canonical DOT, fail-closed on the explicit byte cap."""
    data = render_dot(read_model, label_max_chars=label_max_chars).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact exports/graph.dot is {len(data)} bytes, over its {max_bytes}-byte cap")
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_dot(data: bytes) -> None:
    """Parse *data* back and assert the exact canonical DOT structure.

    Any drift — unknown line shapes, unsorted nodes or edges, a color outside
    the palette, surviving control characters in a label, a missing footer —
    is a typed round-trip failure.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact exports/graph.dot is not UTF-8: {error}") from error
    lines = text.split("\n")
    if len(lines) < 5 or lines[-1] != "" or lines[-2] != "}":
        raise ExportRoundTripError("artifact exports/graph.dot is truncated or missing its closing brace")
    if lines[0] != f"digraph {_GRAPH_NAME} {{":
        raise ExportRoundTripError("artifact exports/graph.dot has a non-canonical header")
    if lines[1] != '  graph [rankdir="LR"];' or lines[2] != '  node [shape="box", style="filled"];':
        raise ExportRoundTripError("artifact exports/graph.dot has non-canonical graph defaults")
    palette = set(DOMAIN_COLORS.values()) | {FALLBACK_COLOR}
    node_ids: list[str] = []
    edge_keys: list[tuple[str, str, str]] = []
    for line in lines[3:-2]:
        node_match = _NODE_LINE_RE.match(line)
        if node_match is not None:
            if edge_keys:
                raise ExportRoundTripError("artifact exports/graph.dot interleaves node and edge lines")
            node_id, raw_label, color = node_match.groups()
            if color not in palette:
                raise ExportRoundTripError(f"artifact exports/graph.dot uses color {color} outside the palette")
            label = _dot_unescape(node_match.group(2))
            if any(unicodedata.category(char) in ("Cc", "Cf") for char in label):
                raise ExportRoundTripError("artifact exports/graph.dot carries an unsanitized label")
            node_ids.append(_dot_unescape(node_id))
            continue
        edge_match = _EDGE_LINE_RE.match(line)
        if edge_match is not None:
            source, target, relation = (_dot_unescape(group) for group in edge_match.groups())
            edge_keys.append((source, target, relation))
            continue
        raise ExportRoundTripError(f"artifact exports/graph.dot carries an unrecognized line: {line!r}")
    if node_ids != sorted(node_ids) or len(set(node_ids)) != len(node_ids):
        raise ExportRoundTripError("artifact exports/graph.dot nodes are out of canonical order")
    if edge_keys != sorted(edge_keys) or len(set(edge_keys)) != len(edge_keys):
        raise ExportRoundTripError("artifact exports/graph.dot edges are out of canonical order")
    known = set(node_ids)
    for source, target, _relation in edge_keys:
        if source not in known or target not in known:
            raise ExportRoundTripError("artifact exports/graph.dot has an edge to an undeclared node")


# ------------------------------------------------------------------ registry


def register_dot(
    registry: ExportRegistry,
    *,
    artifact_name: str = "exports/graph.dot",
    max_bytes: int = DEFAULT_DOT_MAX_BYTES,
    label_max_chars: int = DEFAULT_DOT_LABEL_MAX_CHARS,
) -> None:
    """Register the DOT format on *registry* (the wave-B1 seam into F-B8)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_dot(model, max_bytes=max_bytes, label_max_chars=label_max_chars)

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="format",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_dot,
        )
    )

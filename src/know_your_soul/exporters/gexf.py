"""Canonical GEXF exporter (ATM wave-2 formats).

Clean-room implementation of the public GEXF 1.2draft specification (the
Gephi exchange stream format): a ``gexf`` root in the
``http://www.gexf.net/1.2draft`` namespace, one static directed ``graph``,
declared node attributes (``domain``/``kind``/``version``) and one edge
attribute (``relation``), and ``nodes``/``edges`` sections with explicit
``count`` attributes so the round-trip validator can prove integrity on
parse-back.

Contract:

* Deterministic, byte-identical output: nodes sorted by id, edges sorted by
  ``(source, target, relation)`` and deduplicated, sequential synthetic
  edge ids, no wall-clock, no RNG, no ``<meta>`` block (a creator/date
  would break determinism).
* XML 1.0 legal: characters outside the XML 1.0 character range (C0
  controls, lone surrogates, non-characters) are stripped before escaping;
  labels pass through ``security.sanitize_text`` (bidi/zero-width
  stripping), whitespace controls collapse to single spaces, and the
  result is char-capped.
* A node whose id is not XML-1.0-clean is dropped (ids are
  system-generated; an id that mutates under cleaning is malformed).
* Explicit fail-closed caps (bytes, nodes, edges) via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  :func:`roundtrip_gexf` parses the bytes back and asserts the exact
  canonical structure.

No AI, no network, stdlib only.
"""

from __future__ import annotations

import re
import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any
from xml.sax.saxutils import quoteattr

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.security import sanitize_text

__all__ = [
    "GEXF_LABEL_MAX_CHARS",
    "GEXF_MAX_BYTES",
    "GEXF_MAX_EDGES",
    "GEXF_MAX_NODES",
    "GEXF_NAMESPACE",
    "produce_gexf",
    "register_gexf",
    "render_gexf",
    "roundtrip_gexf",
]

GEXF_NAMESPACE = "http://www.gexf.net/1.2draft"
GEXF_VERSION = "1.2"

# Explicit per-format caps (fail-closed, typed — never silent truncation).
GEXF_MAX_BYTES = 2 * 1024 * 1024
GEXF_MAX_NODES = 4096
GEXF_MAX_EDGES = 16384
GEXF_LABEL_MAX_CHARS = 256

_ARTIFACT_NAME = "exports/graph.gexf"
_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")
_EDGE_ID_RE = re.compile(r"^e\d{6}$")


# --------------------------------------------------------------- primitives


def _xml10_clean(text: str) -> str:
    """Drop every character that is not legal in XML 1.0.

    Legal scalar values are #x9, #xA, #xD, #x20-#xD7FF, #xE000-#xFFFD and
    the astral planes; lone surrogates and the U+FFFE/U+FFFF
    non-characters are dropped so no downstream XML parser can choke.
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char in "\t\n\r" or (0x20 <= code <= 0xD7FF) or (0xE000 <= code <= 0xFFFD) or code >= 0x10000:
            out.append(char)
    return "".join(out)


def _clean_label(raw: Any, fallback: str, *, max_chars: int) -> str:
    """Sanitize, XML-clean, collapse whitespace controls, cap chars."""
    if raw in (None, ""):
        return fallback[:max_chars]
    text = sanitize_text(_xml10_clean(str(raw)[: max_chars + 1]))
    collapsed = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    return collapsed[:max_chars] if collapsed else fallback[:max_chars]


def _clean_id(raw: Any) -> str | None:
    """A node id must survive XML cleaning unchanged; otherwise it is dropped."""
    if not isinstance(raw, str) or not raw:
        return None
    cleaned = sanitize_text(_xml10_clean(raw))
    return cleaned if cleaned == raw else None


def _extract(read_model: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Canonical view: sorted node dicts and sorted unique edges.

    Malformed rows, duplicate ids and edges with unknown endpoints are
    dropped; self-loops are kept (GEXF supports them).
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
                "label": _clean_label(entry.get("label"), node_id, max_chars=GEXF_LABEL_MAX_CHARS),
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


# ------------------------------------------------------------------ producer


def render_gexf(read_model: Mapping[str, Any], *, label_max_chars: int = GEXF_LABEL_MAX_CHARS) -> str:
    """Render the read model as canonical GEXF 1.2draft text."""
    if label_max_chars < 1:
        raise ExportError(f"label_max_chars must be positive, got {label_max_chars!r}")
    nodes, edges = _extract(read_model)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<gexf xmlns="{GEXF_NAMESPACE}" version="{GEXF_VERSION}">',
        '  <graph mode="static" defaultedgetype="directed">',
        '    <attributes class="node">',
        '      <attribute id="domain" title="domain" type="string"/>',
        '      <attribute id="kind" title="kind" type="string"/>',
        '      <attribute id="version" title="version" type="integer"/>',
        "    </attributes>",
        '    <attributes class="edge">',
        '      <attribute id="relation" title="relation" type="string"/>',
        "    </attributes>",
        f'    <nodes count="{len(nodes)}">',
    ]
    for node in nodes:
        label = _clean_label(node["label"], node["id"], max_chars=label_max_chars)
        lines.append(f"      <node id={quoteattr(node['id'])} label={quoteattr(label)}>")
        lines.append("        <attvalues>")
        lines.append(f'          <attvalue for="domain" value={quoteattr(node["domain"])}/>')
        lines.append(f'          <attvalue for="kind" value={quoteattr(node["kind"])}/>')
        lines.append(f'          <attvalue for="version" value="{node["version"]}"/>')
        lines.append("        </attvalues>")
        lines.append("      </node>")
    lines.append("    </nodes>")
    lines.append(f'    <edges count="{len(edges)}">')
    for index, (source, target, relation) in enumerate(edges):
        lines.append(f'      <edge id="e{index:06d}" source={quoteattr(source)} target={quoteattr(target)}>')
        lines.append("        <attvalues>")
        lines.append(f'          <attvalue for="relation" value={quoteattr(relation)}/>')
        lines.append("        </attvalues>")
        lines.append("      </edge>")
    lines.append("    </edges>")
    lines.append("  </graph>")
    lines.append("</gexf>")
    return "\n".join(lines) + "\n"


def produce_gexf(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = GEXF_MAX_BYTES,
    max_nodes: int = GEXF_MAX_NODES,
    max_edges: int = GEXF_MAX_EDGES,
    label_max_chars: int = GEXF_LABEL_MAX_CHARS,
) -> bytes:
    """UTF-8 encoded canonical GEXF, fail-closed on every explicit cap."""
    if max_bytes < 1:
        raise ExportError(f"max_bytes must be positive, got {max_bytes!r}")
    if max_nodes < 1:
        raise ExportError(f"max_nodes must be positive, got {max_nodes!r}")
    if max_edges < 1:
        raise ExportError(f"max_edges must be positive, got {max_edges!r}")
    nodes, edges = _extract(read_model)
    if len(nodes) > max_nodes:
        raise ExportCapExceededError(
            f"graph has {len(nodes)} nodes, over the {max_nodes}-node cap for {_ARTIFACT_NAME}"
        )
    if len(edges) > max_edges:
        raise ExportCapExceededError(
            f"graph has {len(edges)} edges, over the {max_edges}-edge cap for {_ARTIFACT_NAME}"
        )
    data = render_gexf(read_model, label_max_chars=label_max_chars).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact {_ARTIFACT_NAME} is {len(data)} bytes, over its {max_bytes}-byte cap")
    roundtrip_gexf(data)
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_gexf(data: bytes) -> None:
    """Parse *data* back and assert the exact canonical GEXF structure.

    Any drift — wrong namespace, count attributes disagreeing with the
    actual elements, unsorted nodes or edges, an edge id out of sequence,
    an endpoint not declared as a node, surviving control characters — is
    a typed round-trip failure.
    """
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is not UTF-8: {error}") from error
    if not text.startswith('<?xml version="1.0" encoding="UTF-8"?>\n'):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} has a non-canonical XML declaration")
    for char in text:
        code = ord(char)
        if code < 0x20 and char not in "\t\n\r":
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries control char U+{code:04X}")
    try:
        # Input is our own generated artifact: size-capped, C0-stripped, no DTD
        # ever emitted by the producer; ElementTree does not resolve external entities.
        root = ET.fromstring(text)  # nosec B314 - round-trip of self-produced capped artifact
    except ET.ParseError as error:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} does not parse as XML: {error}") from error
    if root.tag != f"{{{GEXF_NAMESPACE}}}gexf" or root.get("version") != GEXF_VERSION:
        raise ExportRoundTripError(
            f"artifact {_ARTIFACT_NAME} root namespace/version is not canonical GEXF {GEXF_VERSION}"
        )
    graph = root.find(f"{{{GEXF_NAMESPACE}}}graph")
    if graph is None or graph.get("mode") != "static" or graph.get("defaultedgetype") != "directed":
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} has a non-canonical graph element")

    nodes_el = graph.find(f"{{{GEXF_NAMESPACE}}}nodes")
    edges_el = graph.find(f"{{{GEXF_NAMESPACE}}}edges")
    if nodes_el is None or edges_el is None:
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} is missing its nodes/edges sections")
    node_ids: list[str] = []
    for node in nodes_el.findall(f"{{{GEXF_NAMESPACE}}}node"):
        node_id = node.get("id")
        if not node_id or node.get("label") is None:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} carries a node without id/label")
        node_ids.append(node_id)
        attvalues = node.find(f"{{{GEXF_NAMESPACE}}}attvalues")
        if attvalues is None:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} node {node_id!r} lacks attvalues")
        fields = {att.get("for"): att.get("value") for att in attvalues.findall(f"{{{GEXF_NAMESPACE}}}attvalue")}
        if set(fields) != {"domain", "kind", "version"}:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} node {node_id!r} carries non-canonical attvalues")
    if node_ids != sorted(node_ids) or len(set(node_ids)) != len(node_ids):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} nodes are out of canonical order")

    known = set(node_ids)
    edge_keys: list[tuple[str, str, str]] = []
    for position, edge in enumerate(edges_el.findall(f"{{{GEXF_NAMESPACE}}}edge")):
        edge_id = edge.get("id") or ""
        if _EDGE_ID_RE.fullmatch(edge_id) is None or int(edge_id[1:]) != position:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge id {edge_id!r} is out of sequence")
        source, target = edge.get("source"), edge.get("target")
        if source not in known or target not in known:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge {edge_id!r} has an unknown endpoint")
        attvalues = edge.find(f"{{{GEXF_NAMESPACE}}}attvalues")
        relation = None
        if attvalues is not None:
            for att in attvalues.findall(f"{{{GEXF_NAMESPACE}}}attvalue"):
                if att.get("for") == "relation":
                    relation = att.get("value")
        if relation is None:
            raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edge {edge_id!r} lacks a relation attvalue")
        edge_keys.append((source or "", target or "", relation))
    if edge_keys != sorted(edge_keys) or len(set(edge_keys)) != len(edge_keys):
        raise ExportRoundTripError(f"artifact {_ARTIFACT_NAME} edges are out of canonical order")

    for section, actual in ((nodes_el, len(node_ids)), (edges_el, len(edge_keys))):
        try:
            declared = int(section.get("count") or "-1")
        except ValueError:
            declared = -1
        if declared != actual:
            raise ExportRoundTripError(
                f"artifact {_ARTIFACT_NAME} count mismatch: declared {declared}, actual {actual}"
            )


# ------------------------------------------------------------------ registry


def register_gexf(
    registry: ExportRegistry,
    *,
    artifact_name: str = _ARTIFACT_NAME,
    max_bytes: int = GEXF_MAX_BYTES,
    max_nodes: int = GEXF_MAX_NODES,
    max_edges: int = GEXF_MAX_EDGES,
    label_max_chars: int = GEXF_LABEL_MAX_CHARS,
) -> None:
    """Register the GEXF format on *registry* (the wave-2 seam into F-B8)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_gexf(
            model, max_bytes=max_bytes, max_nodes=max_nodes, max_edges=max_edges, label_max_chars=label_max_chars
        )

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="format",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_gexf,
        )
    )

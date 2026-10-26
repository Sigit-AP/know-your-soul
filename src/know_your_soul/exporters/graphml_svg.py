"""GraphML and static SVG exporters (ATM wave B, plan F-B4).

Two interchange artifacts over the generation read model (``graph.json``
shape: ``nodes``/``links``):

* :func:`to_graphml` — GraphML per the public format specification:
  XML 1.0 legal (C0 control characters stripped — one poisoned label must
  never abort a whole export), the canonical graphdrawing namespace,
  deterministic node/edge ordering, and graph-level node/edge counts so the
  round-trip validator can prove structural integrity by parsing the bytes
  back.
* :func:`to_svg` — a static, script-free SVG 1.1 render: edges as lines,
  nodes as circles colored per domain (our own light-document palette),
  bounded labels with a *marked* elision (``…`` with the full text preserved
  in a hover ``<title>``), and a sorted domain legend. Positions come from
  the deterministic :mod:`know_your_soul.viz.layout` engine (F-B7).

Both producers are pure functions of the read model with explicit,
fail-closed caps (node/edge counts, label length, artifact bytes) and run
their own round-trip validator before returning, so a defective artifact is
a typed error at production time rather than a corrupt file downstream.
Clean-room: format structure follows the public GraphML/SVG
specifications; no template strings or palettes are taken from any other
project.
"""

from __future__ import annotations

import xml.etree.ElementTree as ET
from collections.abc import Mapping
from typing import Any
from xml.sax.saxutils import escape, quoteattr

from know_your_soul.security import sanitize_text
from know_your_soul.viz.layout import DEFAULT_ITERATIONS, DEFAULT_SEED, LayoutError, layout

__all__ = [
    "GRAPHML_LABEL_MAX_CHARS",
    "GRAPHML_MAX_BYTES",
    "GRAPHML_NAMESPACE",
    "MAX_GRAPH_EDGES",
    "MAX_GRAPH_NODES",
    "MAX_SVG_LABEL_CHARS",
    "SVG_MAX_BYTES",
    "SVG_NAMESPACE",
    "GraphExportCapExceededError",
    "GraphExportError",
    "GraphExportRoundTripError",
    "domain_palette",
    "to_graphml",
    "to_svg",
    "validate_graphml_bytes",
    "validate_svg_bytes",
]

GRAPHML_NAMESPACE = "http://graphml.graphdrawing.org/xmlns"
SVG_NAMESPACE = "http://www.w3.org/2000/svg"

# Explicit per-artifact caps (fail-closed, typed — never silent truncation).
GRAPHML_MAX_BYTES = 256 * 1024
SVG_MAX_BYTES = 256 * 1024
MAX_GRAPH_NODES = 4096
MAX_GRAPH_EDGES = 16384

# Label bounds: GraphML keeps a generous data label, the SVG renders a short
# one; both elide with a visible marker rather than truncating silently.
GRAPHML_LABEL_MAX_CHARS = 512
MAX_SVG_LABEL_CHARS = 48

# Legend geometry: at most this many rows, the rest folds into a "+N more"
# line so the panel itself stays bounded.
_MAX_LEGEND_ROWS = 12

_ELISION = "…"

# Our own light-document palette (chosen for this project; dark-theme asset
# colors in tools/generate_visual_assets.py are a separate surface). Unknown
# domains cycle deterministically through the fallback ramp in sorted order.
_DOMAIN_PALETTE: dict[str, str] = {
    "P1": "#0969da",
    "P2": "#8250df",
    "P3": "#cf222e",
    "P4": "#bc4c00",
    "P5": "#bf3989",
    "P6": "#1a7f37",
    "P7": "#9a6700",
}
_FALLBACK_PALETTE: tuple[str, ...] = ("#0550ae", "#6f42c1", "#b35900", "#116329", "#a40e26", "#7d4e00")

_SVG_BG = "#ffffff"
_SVG_PANEL = "#f6f8fa"
_SVG_BORDER = "#d0d7de"
_SVG_TEXT = "#1f2328"
_SVG_MUTED = "#57606a"
_SVG_EDGE = "#8c959f"


class GraphExportError(RuntimeError):
    """Base class for graph-export contract violations."""


class GraphExportCapExceededError(GraphExportError):
    """A graph or artifact exceeded an explicit fail-closed cap."""


class GraphExportRoundTripError(GraphExportError):
    """A produced artifact failed its own parse-back structural validation."""


# ------------------------------------------------------------- sanitization


def _xml10_clean(text: str) -> str:
    """Strip every character that is not legal in XML 1.0 (C0 controls et al.).

    Legal scalar values are #x9, #xA, #xD, #x20-#xD7FF, #xE000-#xFFFD and
    the astral planes; lone surrogates and the U+FFFE/U+FFFF non-characters
    are dropped so the serializer and any downstream parser can never choke.
    """
    out: list[str] = []
    for char in text:
        code = ord(char)
        if char in "\t\n\r" or (0x20 <= code <= 0xD7FF) or (0xE000 <= code <= 0xFFFD) or code >= 0x10000:
            out.append(char)
    return "".join(out)


def _clean_field(value: Any, *, max_chars: int) -> str:
    """Project-side text hygiene: bounded length, sanitized, XML-1.0 legal."""
    if not isinstance(value, str):
        return ""
    raw = _xml10_clean(value[: max_chars + 1])
    return sanitize_text(raw, max_length=max_chars + 1)[:max_chars]


def _elide(text: str, max_chars: int) -> str:
    """Bound a label with a *marked* elision — truncation is always visible."""
    if len(text) <= max_chars:
        return text
    return text[: max(1, max_chars - 1)] + _ELISION


# ------------------------------------------------------------- normalization


def _normalize(
    read_model: Mapping[str, Any],
    *,
    max_nodes: int,
    max_edges: int,
) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Canonical graph parts: sorted node dicts and sorted unique typed links.

    Malformed rows, unknown endpoints, self-loops and duplicates are dropped;
    caps are enforced as typed errors. The result depends only on content.
    """
    if not isinstance(read_model, Mapping):
        raise GraphExportError(f"read model must be a mapping, got {type(read_model).__name__}")
    nodes: dict[str, dict[str, Any]] = {}
    raw_nodes = read_model.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                continue
            node_id = entry.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in nodes:
                continue
            if _clean_field(node_id, max_chars=256) != node_id:
                # Ids are system-generated; an id that sanitizes is malformed.
                continue
            version = entry.get("version")
            clean_version = (
                version if isinstance(version, int) and not isinstance(version, bool) and version >= 0 else 0
            )
            nodes[node_id] = {
                "id": _clean_field(node_id, max_chars=256),
                "label": _clean_field(entry.get("label") or node_id, max_chars=GRAPHML_LABEL_MAX_CHARS),
                "domain": _clean_field(entry.get("domain"), max_chars=64),
                "kind": _clean_field(entry.get("kind"), max_chars=64),
                "version": clean_version,
            }
    if len(nodes) > max_nodes:
        raise GraphExportCapExceededError(f"graph has {len(nodes)} nodes, over the {max_nodes}-node export cap")
    seen: set[tuple[str, str, str]] = set()
    links: list[tuple[str, str, str]] = []
    raw_links = read_model.get("links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                continue
            source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
            if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)):
                continue
            if source not in nodes or target not in nodes or source == target:
                continue
            key = (source, _clean_field(relation, max_chars=128), target)
            if key in seen:
                continue
            seen.add(key)
            links.append(key)
    if len(links) > max_edges:
        raise GraphExportCapExceededError(f"graph has {len(links)} edges, over the {max_edges}-edge export cap")
    links.sort()
    return [nodes[node_id] for node_id in sorted(nodes)], links


def _check_params(width: float, height: float, max_bytes: int) -> None:
    if not isinstance(width, (int, float)) or isinstance(width, bool) or not float(width) > 0.0:
        raise GraphExportError(f"width must be a positive number, got {width!r}")
    if not isinstance(height, (int, float)) or isinstance(height, bool) or not float(height) > 0.0:
        raise GraphExportError(f"height must be a positive number, got {height!r}")
    if max_bytes < 1:
        raise GraphExportError(f"max_bytes must be positive, got {max_bytes!r}")


def domain_palette() -> dict[str, str]:
    """A copy of the domain → color mapping (own palette, sorted keys)."""
    return dict(sorted(_DOMAIN_PALETTE.items()))


def _domain_color(domain: str, present_domains: tuple[str, ...]) -> str:
    if domain in _DOMAIN_PALETTE:
        return _DOMAIN_PALETTE[domain]
    index = present_domains.index(domain) if domain in present_domains else 0
    return _FALLBACK_PALETTE[index % len(_FALLBACK_PALETTE)]


# ------------------------------------------------------------------ GraphML


def to_graphml(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = GRAPHML_MAX_BYTES,
    max_nodes: int = MAX_GRAPH_NODES,
    max_edges: int = MAX_GRAPH_EDGES,
) -> bytes:
    """Serialize the read model as deterministic, XML-1.0-legal GraphML.

    Nodes are emitted in sorted id order with ``label``/``domain``/``kind``/
    ``version`` data; edges in sorted ``(source, relation, target)`` order
    with stable synthetic ids. The graph element carries node/edge counts so
    :func:`validate_graphml_bytes` can prove integrity on parse-back. The
    producer validates its own output before returning (fail closed).
    """
    if max_bytes < 1:
        raise GraphExportError(f"max_bytes must be positive, got {max_bytes!r}")
    nodes, links = _normalize(read_model, max_nodes=max_nodes, max_edges=max_edges)
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        f'<graphml xmlns="{GRAPHML_NAMESPACE}">',
        '  <key id="d_label" for="node" attr.name="label" attr.type="string"/>',
        '  <key id="d_domain" for="node" attr.name="domain" attr.type="string"/>',
        '  <key id="d_kind" for="node" attr.name="kind" attr.type="string"/>',
        '  <key id="d_version" for="node" attr.name="version" attr.type="int"/>',
        '  <key id="d_relation" for="edge" attr.name="relation" attr.type="string"/>',
        '  <key id="d_node_count" for="graph" attr.name="node_count" attr.type="int"/>',
        '  <key id="d_edge_count" for="graph" attr.name="edge_count" attr.type="int"/>',
        '  <graph id="know-your-soul" edgedefault="directed">',
        f'    <data key="d_node_count">{len(nodes)}</data>',
        f'    <data key="d_edge_count">{len(links)}</data>',
    ]
    for node in nodes:
        lines.append(f"    <node id={quoteattr(node['id'])}>")
        lines.append(f'      <data key="d_label">{escape(node["label"])}</data>')
        lines.append(f'      <data key="d_domain">{escape(node["domain"])}</data>')
        lines.append(f'      <data key="d_kind">{escape(node["kind"])}</data>')
        lines.append(f'      <data key="d_version">{node["version"]}</data>')
        lines.append("    </node>")
    for index, (source, relation, target) in enumerate(links):
        lines.append(f'    <edge id="e{index:06d}" source={quoteattr(source)} target={quoteattr(target)}>')
        lines.append(f'      <data key="d_relation">{escape(relation)}</data>')
        lines.append("    </edge>")
    lines.append("  </graph>")
    lines.append("</graphml>")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise GraphExportCapExceededError(f"GraphML artifact is {len(data)} bytes, over its {max_bytes}-byte cap")
    validate_graphml_bytes(data)
    return data


def validate_graphml_bytes(data: bytes) -> None:
    """Parse-back structural validation for GraphML artifacts (fail closed)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GraphExportRoundTripError(f"GraphML artifact is not UTF-8: {error}") from error
    for char in text:
        code = ord(char)
        if code < 0x20 and char not in "\t\n\r":
            raise GraphExportRoundTripError(f"GraphML artifact carries C0 control char U+{code:04X}")
    try:
        # nosec B314 — input is our own generated artifact: size-capped, C0-stripped,
        # no DTD/doctype is ever emitted by the producer, and ElementTree does not
        # resolve external entities. Round-trip validation only parses known-good
        # self-produced text.
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise GraphExportRoundTripError(f"GraphML artifact does not parse as XML: {error}") from error
    if root.tag != f"{{{GRAPHML_NAMESPACE}}}graphml":
        raise GraphExportRoundTripError(f"GraphML root must be {{{GRAPHML_NAMESPACE}}}graphml, got {root.tag!r}")
    graph = root.find(f"{{{GRAPHML_NAMESPACE}}}graph")
    if graph is None:
        raise GraphExportRoundTripError("GraphML artifact has no graph element")
    node_ids: set[str] = set()
    for node in graph.findall(f"{{{GRAPHML_NAMESPACE}}}node"):
        node_id = node.get("id")
        if not node_id:
            raise GraphExportRoundTripError("GraphML node without an id attribute")
        if node_id in node_ids:
            raise GraphExportRoundTripError(f"GraphML duplicate node id {node_id!r}")
        node_ids.add(node_id)
    edges = graph.findall(f"{{{GRAPHML_NAMESPACE}}}edge")
    for edge in edges:
        for endpoint in ("source", "target"):
            value = edge.get(endpoint)
            if value not in node_ids:
                raise GraphExportRoundTripError(f"GraphML edge {edge.get('id')!r} has unknown {endpoint} {value!r}")
    declared: dict[str, int] = {}
    for datum in graph.findall(f"{{{GRAPHML_NAMESPACE}}}data"):
        key = datum.get("key")
        if key in {"d_node_count", "d_edge_count"}:
            try:
                declared[key] = int(datum.text or "")
            except ValueError as error:
                raise GraphExportRoundTripError(f"GraphML graph count {key} is not an int") from error
    if declared.get("d_node_count") != len(node_ids) or declared.get("d_edge_count") != len(edges):
        raise GraphExportRoundTripError(
            f"GraphML declared counts {declared} disagree with actual ({len(node_ids)} nodes, {len(edges)} edges)"
        )


# ---------------------------------------------------------------------- SVG


def _legend_lines(domains: tuple[str, ...], counts: Mapping[str, int], width: int) -> list[str]:
    """A bounded, sorted domain legend panel pinned to the top-right corner."""
    shown = domains[:_MAX_LEGEND_ROWS]
    row_height = 18
    panel_h = 14 + row_height * (len(shown) + (1 if len(domains) > len(shown) else 0)) + 6
    panel_w = 132
    x0 = width - panel_w - 12
    lines = [
        f'<rect x="{x0}" y="12" width="{panel_w}" height="{panel_h}" rx="6" '
        f'fill="{_SVG_PANEL}" stroke="{_SVG_BORDER}" stroke-width="1"/>'
    ]
    for row, domain in enumerate(shown):
        y = 12 + 14 + row * row_height
        color = _domain_color(domain, domains)
        lines.append(f'<circle cx="{x0 + 14}" cy="{y - 4}" r="5" fill="{color}"/>')
        lines.append(
            f'<text x="{x0 + 26}" y="{y}" font-size="11" fill="{_SVG_TEXT}">'
            f"{escape(domain)} &#183; {counts[domain]}</text>"
        )
    if len(domains) > len(shown):
        y = 12 + 14 + len(shown) * row_height
        lines.append(
            f'<text x="{x0 + 26}" y="{y}" font-size="11" fill="{_SVG_MUTED}">'
            f"+{len(domains) - len(shown)} more{_ELISION}</text>"
        )
    return lines


def to_svg(
    read_model: Mapping[str, Any],
    *,
    seed: int = DEFAULT_SEED,
    iterations: int = DEFAULT_ITERATIONS,
    width: int = 1200,
    height: int = 800,
    max_bytes: int = SVG_MAX_BYTES,
    max_nodes: int = MAX_GRAPH_NODES,
    max_edges: int = MAX_GRAPH_EDGES,
) -> bytes:
    """Render the read model as a static, script-free SVG 1.1 document.

    Edges are straight lines, nodes are circles colored per domain with a
    bounded label below (elision always marked, full text in ``<title>``),
    and a sorted legend counts nodes per domain. Positions come from the
    deterministic layout engine, so identical inputs render byte-identical.
    """
    _check_params(float(width), float(height), max_bytes)
    bad_dims = (
        isinstance(width, bool) or not isinstance(width, int) or isinstance(height, bool) or not isinstance(height, int)
    )
    if bad_dims:
        raise GraphExportError(f"width/height must be integers, got {width!r}x{height!r}")
    nodes, links = _normalize(read_model, max_nodes=max_nodes, max_edges=max_edges)
    try:
        placed = layout(read_model, seed=seed, iterations=iterations, width=float(width), height=float(height))
    except LayoutError as error:
        raise GraphExportError(f"layout rejected the render request: {error}") from error
    positions = placed.positions
    present_domains = tuple(sorted({node["domain"] for node in nodes}))
    counts: dict[str, int] = {domain: 0 for domain in present_domains}
    for node in nodes:
        counts[node["domain"]] += 1
    radius = {node["id"]: (9 if node["kind"] in {"root", "domain"} else 6) for node in nodes}
    lines = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        "<!-- Deterministic static render; positions from know_your_soul.viz.layout. -->",
        f'<svg xmlns="{SVG_NAMESPACE}" width="{width}" height="{height}" '
        f'viewBox="0 0 {width} {height}" font-family="ui-sans-serif, system-ui, sans-serif">',
        f'<rect width="{width}" height="{height}" fill="{_SVG_BG}"/>',
    ]
    # Edges first so node circles always paint over the lines.
    for source, _relation, target in links:
        x1, y1 = positions[source]
        x2, y2 = positions[target]
        lines.append(
            f'<line x1="{x1:.1f}" y1="{y1:.1f}" x2="{x2:.1f}" y2="{y2:.1f}" '
            f'stroke="{_SVG_EDGE}" stroke-width="1" opacity="0.7"/>'
        )
    for node in nodes:
        node_id = node["id"]
        x, y = positions[node_id]
        r = radius[node_id]
        color = _domain_color(node["domain"], present_domains)
        lines.append(
            f'<circle cx="{x:.1f}" cy="{y:.1f}" r="{r}" fill="{color}" stroke="{_SVG_BG}" stroke-width="1.5"/>'
        )
    for node in nodes:
        node_id = node["id"]
        x, y = positions[node_id]
        r = radius[node_id]
        full_label = node["label"]
        short = _elide(full_label, MAX_SVG_LABEL_CHARS)
        lines.append(
            f'<text x="{x:.1f}" y="{y + r + 12:.1f}" font-size="10" fill="{_SVG_MUTED}" text-anchor="middle">'
            f"{escape(short)}<title>{escape(full_label)}</title></text>"
        )
    if present_domains:
        lines.extend(_legend_lines(present_domains, counts, width))
    lines.append("</svg>")
    data = ("\n".join(lines) + "\n").encode("utf-8")
    if len(data) > max_bytes:
        raise GraphExportCapExceededError(f"SVG artifact is {len(data)} bytes, over its {max_bytes}-byte cap")
    validate_svg_bytes(data)
    return data


def validate_svg_bytes(data: bytes) -> None:
    """Parse-back structural validation for SVG artifacts (fail closed)."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise GraphExportRoundTripError(f"SVG artifact is not UTF-8: {error}") from error
    for char in text:
        code = ord(char)
        if code < 0x20 and char not in "\t\n\r":
            raise GraphExportRoundTripError(f"SVG artifact carries C0 control char U+{code:04X}")
    try:
        # nosec B314 — see the GraphML validator above: self-produced, size-capped,
        # C0-stripped artifact text only; ElementTree never resolves external entities.
        root = ET.fromstring(text)
    except ET.ParseError as error:
        raise GraphExportRoundTripError(f"SVG artifact does not parse as XML: {error}") from error
    if root.tag != f"{{{SVG_NAMESPACE}}}svg":
        raise GraphExportRoundTripError(f"SVG root must be {{{SVG_NAMESPACE}}}svg, got {root.tag!r}")

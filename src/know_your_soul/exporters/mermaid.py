"""Canonical Mermaid flowchart exporter (ATM F-B2).

Clean-room implementation of the public Mermaid ``flowchart`` syntax: one
subgraph per primary domain P1-P7, Mermaid-safe node ids, sanitized labels,
and — critically — **explicit, marked elision** when the graph exceeds the
node cap (plan F-B2: elision is declared, never silent).

Contract:

* Deterministic, byte-identical output: subgraphs sorted by domain, nodes
  sorted by their (deterministic) Mermaid id inside each subgraph, edges
  sorted by their rendered line.
* Node-id mapping is deterministic: unsafe characters become ``_``, ids are
  prefixed ``n_``, and sanitization collisions receive suffixes (``_2``,
  ``_3``…) assigned in canonical node-id order — permuted input renders
  identically.
* Labels pass through ``security.sanitize_text``, whitespace controls
  collapse to single spaces, the result is char-capped, and ``"`` becomes
  the Mermaid entity ``#quot;`` inside the quoted ``["..."]`` form.
* Elision budget is allocated **round-robin across domains** (sorted domain
  order, sorted ids within a domain) so a single large domain can never
  starve the others; elided nodes and the edges touching them are counted in
  a ``%% elided:`` comment plus a visible ``ELIDED`` node.
* Fail-closed byte cap via
  :class:`~know_your_soul.exporters.orchestrator.ExportCapExceededError`;
  :func:`roundtrip_mermaid` parses the bytes back and asserts the exact
  canonical structure.

No AI, no network, stdlib only.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from know_your_soul.domain import PrimaryDomain
from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
)
from know_your_soul.security import sanitize_text

__all__ = [
    "DEFAULT_MERMAID_LABEL_MAX_CHARS",
    "DEFAULT_MERMAID_MAX_BYTES",
    "DEFAULT_MERMAID_MAX_NODES",
    "produce_mermaid",
    "register_mermaid",
    "render_mermaid",
    "roundtrip_mermaid",
]

DEFAULT_MERMAID_MAX_BYTES = 1024 * 1024
DEFAULT_MERMAID_MAX_NODES = 200
DEFAULT_MERMAID_LABEL_MAX_CHARS = 60

_MAX_ID_CHARS = 48

_SUBGRAPH_RE = re.compile(r'^  subgraph (dom_\w+)\["(.*)"\]$')
_NODE_RE = re.compile(r'^    (\w+)\["(.*)"\]$')
_EDGE_RE = re.compile(r"^  (\w+) -->\|([^|]*)\| (\w+)$")
_ELIDED_COMMENT_RE = re.compile(r"^  %% elided: (\d+) nodes \(cap (\d+)\); (\d+) edges dropped$")
_ELIDED_NODE_RE = re.compile(r'^  ELIDED\["(\d+) nodes not shown \(cap (\d+)\)"\]$')

_WHITESPACE_RUN_RE = re.compile(r"[\t\r\n]+")
_UNSAFE_ID_CHAR_RE = re.compile(r"[^A-Za-z0-9_]")
_UNSAFE_RELATION_CHAR_RE = re.compile(r"[^A-Za-z0-9_]")


# --------------------------------------------------------------- primitives


def _extract(read_model: Mapping[str, Any]) -> tuple[list[dict[str, Any]], list[tuple[str, str, str]]]:
    """Canonical view: nodes sorted by id; unique sorted edges with known endpoints."""
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
    text = sanitize_text(str(raw)) if raw not in (None, "") else fallback
    collapsed = _WHITESPACE_RUN_RE.sub(" ", text).strip()
    return collapsed[:max_chars] if collapsed else fallback[:max_chars]


def _domain_title(domain: str) -> str:
    """``P1 · Pembelajaran`` for known domains; the sanitized raw value otherwise."""
    try:
        title = PrimaryDomain(domain).title
    except ValueError:
        return _WHITESPACE_RUN_RE.sub(" ", sanitize_text(domain)).strip() or "UNKNOWN"
    return f"{domain} · {title}"


def _mermaid_ids(node_ids: list[str]) -> dict[str, str]:
    """Deterministic node-id → Mermaid-id mapping with collision suffixes.

    *node_ids* must be in canonical (sorted) order; the first occurrence of a
    sanitized base keeps it, later collisions get ``_2``, ``_3``… so permuted
    input yields the identical mapping.
    """
    mapping: dict[str, str] = {}
    used: set[str] = set()
    for node_id in node_ids:
        base = _UNSAFE_ID_CHAR_RE.sub("_", node_id) or "x"
        candidate = f"n_{base}"[:_MAX_ID_CHARS]
        suffix = 1
        while candidate in used:
            suffix += 1
            candidate = f"{f'n_{base}'[: _MAX_ID_CHARS - len(str(suffix)) - 1]}_{suffix}"
        used.add(candidate)
        mapping[node_id] = candidate
    return mapping


def _select_shown(nodes: list[dict[str, Any]], max_nodes: int) -> list[str]:
    """Round-robin node selection across domains (sorted domain, sorted ids).

    Every domain gets a share of the budget before any domain gets a second
    node, so elision never silently erases a whole domain.
    """
    queues: dict[str, list[str]] = {}
    for node in nodes:
        queues.setdefault(str(node.get("domain")), []).append(str(node.get("id")))
    domains = sorted(queues)
    shown: list[str] = []
    while len(shown) < max_nodes and any(queues[domain] for domain in domains):
        for domain in domains:
            if queues[domain] and len(shown) < max_nodes:
                shown.append(queues[domain].pop(0))
    return shown


# ------------------------------------------------------------------ producer


def render_mermaid(
    read_model: Mapping[str, Any],
    *,
    max_nodes: int = DEFAULT_MERMAID_MAX_NODES,
    label_max_chars: int = DEFAULT_MERMAID_LABEL_MAX_CHARS,
) -> str:
    """Render the read model as a canonical Mermaid flowchart."""
    if max_nodes < 1:
        raise ValueError("max_nodes must be positive")
    if label_max_chars < 1:
        raise ValueError("label_max_chars must be positive")
    nodes, edges = _extract(read_model)
    shown_ids = set(_select_shown(nodes, max_nodes))
    elided = len(nodes) - len(shown_ids)
    id_map = _mermaid_ids([str(node.get("id")) for node in nodes])

    kept_edges = [edge for edge in edges if edge[0] in shown_ids and edge[1] in shown_ids]
    dropped_edges = len(edges) - len(kept_edges)

    lines = ["flowchart LR"]
    if elided:
        lines.append(f"  %% elided: {elided} nodes (cap {max_nodes}); {dropped_edges} edges dropped")
        lines.append(f'  ELIDED["{elided} nodes not shown (cap {max_nodes})"]')
    by_domain: dict[str, list[dict[str, Any]]] = {}
    for node in nodes:
        node_id = str(node.get("id"))
        if node_id in shown_ids:
            by_domain.setdefault(str(node.get("domain")), []).append(node)
    for domain in sorted(by_domain):
        lines.append(f'  subgraph dom_{_UNSAFE_ID_CHAR_RE.sub("_", domain)}["{_domain_title(domain)}"]')
        for node in sorted(by_domain[domain], key=lambda item: id_map[str(item.get("id"))]):
            node_id = str(node.get("id"))
            label = _clean_label(node.get("label"), node_id, max_chars=label_max_chars).replace('"', "#quot;")
            lines.append(f'    {id_map[node_id]}["{label}"]')
        lines.append("  end")
    rendered_edges = [
        f"  {id_map[source]} -->|{_UNSAFE_RELATION_CHAR_RE.sub('_', relation)}| {id_map[target]}"
        for source, target, relation in kept_edges
    ]
    lines.extend(sorted(rendered_edges))
    return "\n".join(lines) + "\n"


def produce_mermaid(
    read_model: Mapping[str, Any],
    *,
    max_bytes: int = DEFAULT_MERMAID_MAX_BYTES,
    max_nodes: int = DEFAULT_MERMAID_MAX_NODES,
    label_max_chars: int = DEFAULT_MERMAID_LABEL_MAX_CHARS,
) -> bytes:
    """UTF-8 encoded canonical Mermaid, fail-closed on the explicit byte cap."""
    data = render_mermaid(read_model, max_nodes=max_nodes, label_max_chars=label_max_chars).encode("utf-8")
    if len(data) > max_bytes:
        raise ExportCapExceededError(f"artifact exports/graph.mmd is {len(data)} bytes, over its {max_bytes}-byte cap")
    return data


# ---------------------------------------------------------------- round-trip


def roundtrip_mermaid(data: bytes) -> None:
    """Parse *data* back and assert the exact canonical Mermaid structure."""
    try:
        text = data.decode("utf-8")
    except UnicodeDecodeError as error:
        raise ExportRoundTripError(f"artifact exports/graph.mmd is not UTF-8: {error}") from error
    if not text.endswith("\n"):
        raise ExportRoundTripError("artifact exports/graph.mmd is truncated")
    lines = text[:-1].split("\n")
    if not lines or lines[0] != "flowchart LR":
        raise ExportRoundTripError("artifact exports/graph.mmd has a non-canonical header")

    declared: set[str] = set()
    subgraphs: list[str] = []
    edge_lines: list[str] = []
    elided_comment: tuple[int, int, int] | None = None
    elided_node: tuple[int, int] | None = None
    in_subgraph = False
    subgraph_nodes: list[str] = []
    for line in lines[1:]:
        if in_subgraph:
            node_match = _NODE_RE.match(line)
            if line == "  end":
                if subgraph_nodes != sorted(subgraph_nodes):
                    raise ExportRoundTripError("artifact exports/graph.mmd nodes are out of canonical order")
                in_subgraph = False
                subgraph_nodes = []
            elif node_match is not None:
                declared.add(node_match.group(1))
                subgraph_nodes.append(node_match.group(1))
            else:
                raise ExportRoundTripError(f"artifact exports/graph.mmd carries an unrecognized line: {line!r}")
            continue
        subgraph_match = _SUBGRAPH_RE.match(line)
        if subgraph_match is not None:
            if in_subgraph:
                raise ExportRoundTripError("artifact exports/graph.mmd nests subgraphs")
            subgraphs.append(subgraph_match.group(1))
            in_subgraph = True
            subgraph_nodes = []
            continue
        edge_match = _EDGE_RE.match(line)
        if edge_match is not None:
            edge_lines.append(line)
            continue
        comment_match = _ELIDED_COMMENT_RE.match(line)
        if comment_match is not None:
            elided_comment = (int(comment_match.group(1)), int(comment_match.group(2)), int(comment_match.group(3)))
            continue
        elided_match = _ELIDED_NODE_RE.match(line)
        if elided_match is not None:
            elided_node = (int(elided_match.group(1)), int(elided_match.group(2)))
            continue
        raise ExportRoundTripError(f"artifact exports/graph.mmd carries an unrecognized line: {line!r}")
    if in_subgraph:
        raise ExportRoundTripError("artifact exports/graph.mmd has an unterminated subgraph")
    if subgraphs != sorted(subgraphs):
        raise ExportRoundTripError("artifact exports/graph.mmd subgraphs are out of canonical order")
    if edge_lines != sorted(edge_lines):
        raise ExportRoundTripError("artifact exports/graph.mmd edges are out of canonical order")
    for line in edge_lines:
        match = _EDGE_RE.match(line)
        assert match is not None  # already validated above
        for endpoint in (match.group(1), match.group(3)):
            if endpoint not in declared:
                raise ExportRoundTripError(f"artifact exports/graph.mmd has an edge to an undeclared node {endpoint!r}")
    if (elided_comment is None) != (elided_node is None):
        raise ExportRoundTripError("artifact exports/graph.mmd has a partial elision marker")
    if elided_comment is not None and elided_node is not None:
        if elided_comment[0] != elided_node[0] or elided_comment[1] != elided_node[1]:
            raise ExportRoundTripError("artifact exports/graph.mmd elision markers disagree")
        if elided_comment[0] < 1 or elided_comment[1] < 1:
            raise ExportRoundTripError("artifact exports/graph.mmd elision counts must be positive")


# ------------------------------------------------------------------ registry


def register_mermaid(
    registry: ExportRegistry,
    *,
    artifact_name: str = "exports/graph.mmd",
    max_bytes: int = DEFAULT_MERMAID_MAX_BYTES,
    max_nodes: int = DEFAULT_MERMAID_MAX_NODES,
    label_max_chars: int = DEFAULT_MERMAID_LABEL_MAX_CHARS,
) -> None:
    """Register the Mermaid format on *registry* (the wave-B2 seam into F-B8)."""

    def _producer(model: Mapping[str, Any]) -> bytes:
        return produce_mermaid(model, max_bytes=max_bytes, max_nodes=max_nodes, label_max_chars=label_max_chars)

    registry.register(
        ExportFormat(
            name=artifact_name,
            version=1,
            kind="format",
            max_bytes=max_bytes,
            producer=_producer,
            roundtrip=roundtrip_mermaid,
        )
    )

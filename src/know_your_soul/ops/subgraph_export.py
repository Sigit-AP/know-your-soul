"""Bounded per-domain / per-node subgraph export (ATM F-D3).

Produces an ego-net (``node_id`` + ``depth``) or a single-domain slice
(``domain``) of the generation read model as two canonical artifacts — a
canonical-JSON node-link document and a Mermaid flowchart rendered through
:mod:`know_your_soul.exporters.mermaid` — plus sha256 manifest entries, so
the integrator can register them without this module touching the
orchestrator or the CLI.

Contract:

* **Bounded**: selection never exceeds ``max_nodes``; truncation is declared
  in the JSON payload (``truncated.nodes_omitted`` / ``edges_omitted``),
  never silent. Ego-net budgets are spent nearest-first (BFS layer, then
  canonical node id).
* **Deterministic**: permuted read models yield byte-identical artifacts;
  node and edge output order is canonical.
* **Fail-closed**: malformed requests, unknown seeds/domains, and byte-cap
  breaches raise typed errors before any artifact is claimed.
* Read-only over the read model; no store access, no AI, no network.
"""

from __future__ import annotations

import hashlib
import re
from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from know_your_soul.domain import canonical_json
from know_your_soul.exporters.mermaid import DEFAULT_MERMAID_LABEL_MAX_CHARS, render_mermaid

__all__ = [
    "DEFAULT_SUBGRAPH_DEPTH",
    "DEFAULT_SUBGRAPH_MAX_BYTES",
    "DEFAULT_SUBGRAPH_MAX_NODES",
    "SubgraphCapExceededError",
    "SubgraphExport",
    "SubgraphExportError",
    "SubgraphRequest",
    "SubgraphResult",
    "SubgraphSelectionError",
    "UnknownSubgraphNodeError",
    "build_subgraph",
    "export_subgraph",
    "subgraph_manifest_entries",
]

DEFAULT_SUBGRAPH_DEPTH = 1
DEFAULT_SUBGRAPH_MAX_NODES = 64
DEFAULT_SUBGRAPH_MAX_BYTES = 256 * 1024

_MAX_SLUG_CHARS = 64
_UNSAFE_SLUG_CHAR_RE = re.compile(r"[^A-Za-z0-9._-]+")


class SubgraphExportError(RuntimeError):
    """Base class for subgraph-export contract violations."""


class SubgraphSelectionError(SubgraphExportError):
    """The request itself is malformed (selector, depth, bounds, domain)."""


class UnknownSubgraphNodeError(SubgraphSelectionError):
    """The requested ego-net seed does not exist in the read model."""


class SubgraphCapExceededError(SubgraphExportError):
    """A produced artifact exceeded the explicit byte cap."""


@dataclass(frozen=True)
class SubgraphRequest:
    """One subgraph selection: exactly one of *domain* / *node_id*."""

    domain: str | None = None
    node_id: str | None = None
    depth: int = DEFAULT_SUBGRAPH_DEPTH
    max_nodes: int = DEFAULT_SUBGRAPH_MAX_NODES
    label_max_chars: int = DEFAULT_MERMAID_LABEL_MAX_CHARS

    def __post_init__(self) -> None:
        if (self.domain is None) == (self.node_id is None):
            raise SubgraphSelectionError("exactly one selector is required: domain XOR node_id")
        if self.node_id is not None and self.depth < 0:
            raise SubgraphSelectionError(f"depth must be >= 0, got {self.depth}")
        if self.max_nodes < 1:
            raise SubgraphSelectionError(f"max_nodes must be positive, got {self.max_nodes}")
        if self.label_max_chars < 1:
            raise SubgraphSelectionError(f"label_max_chars must be positive, got {self.label_max_chars}")


@dataclass(frozen=True)
class SubgraphResult:
    """The selected subgraph as a canonical read model plus truncation stats."""

    read_model: Mapping[str, Any]
    nodes_omitted: int
    edges_omitted: int


@dataclass(frozen=True)
class SubgraphExport:
    """Produced artifacts (name → bytes) plus the selection's stats."""

    slug: str
    artifacts: dict[str, bytes]
    node_count: int
    edge_count: int
    nodes_omitted: int
    edges_omitted: int


# ------------------------------------------------------------------ selection


def _extract(read_model: Mapping[str, Any]) -> tuple[dict[str, dict[str, Any]], list[tuple[str, str, str]]]:
    raw_nodes = read_model.get("nodes")
    nodes = [node for node in raw_nodes if isinstance(node, dict)] if isinstance(raw_nodes, list) else []
    by_id = {str(node.get("id")): node for node in nodes}
    raw_links = read_model.get("links")
    links = [link for link in raw_links if isinstance(link, dict)] if isinstance(raw_links, list) else []
    edges = sorted(
        {
            (str(link.get("source")), str(link.get("target")), str(link.get("relation")))
            for link in links
            if str(link.get("source")) in by_id and str(link.get("target")) in by_id
        }
    )
    return by_id, edges


def _adjacency(edges: list[tuple[str, str, str]]) -> dict[str, tuple[str, ...]]:
    """Undirected adjacency with canonically sorted neighbour lists."""
    neighbours: dict[str, set[str]] = {}
    for source, target, _relation in edges:
        neighbours.setdefault(source, set()).add(target)
        neighbours.setdefault(target, set()).add(source)
    return {node_id: tuple(sorted(peers)) for node_id, peers in neighbours.items()}


def _ego_layers(seed: str, adjacency: Mapping[str, tuple[str, ...]], depth: int) -> dict[str, int]:
    """BFS distances from the seed, layer by layer, in canonical order."""
    distance = {seed: 0}
    frontier = (seed,)
    for level in range(1, depth + 1):
        next_layer: list[str] = []
        for node_id in sorted(frontier):
            for peer in adjacency.get(node_id, ()):
                if peer not in distance:
                    distance[peer] = level
                    next_layer.append(peer)
        frontier = tuple(next_layer)
        if not frontier:
            break
    return distance


def build_subgraph(read_model: Mapping[str, Any], request: SubgraphRequest) -> SubgraphResult:
    """Select the bounded subgraph for *request*; deterministic, fail-closed."""
    by_id, edges = _extract(read_model)

    if request.domain is not None:
        domains = sorted({str(node.get("domain")) for node in by_id.values()})
        if request.domain not in domains:
            raise SubgraphSelectionError(
                f"unknown domain {request.domain!r}; present domains: {', '.join(domains) or '(none)'}"
            )
        full = {node_id for node_id, node in by_id.items() if str(node.get("domain")) == request.domain}
        # Domain slices spend the budget in canonical id order.
        selected = set(sorted(full)[: request.max_nodes])
    else:
        assert request.node_id is not None  # enforced by SubgraphRequest
        if request.node_id not in by_id:
            raise UnknownSubgraphNodeError(
                f"node {request.node_id!r} does not exist in the read model "
                f"({len(by_id)} nodes); check the id from graph.json"
            )
        distance = _ego_layers(request.node_id, _adjacency(edges), request.depth)
        full = set(distance)
        # Budget: nearest first (BFS layer), canonical id inside a layer.
        ranked = sorted(full, key=lambda node_id: (distance[node_id], node_id))
        selected = set(ranked[: request.max_nodes])

    nodes_omitted = len(full) - len(selected)
    full_edges = [edge for edge in edges if edge[0] in full and edge[1] in full]
    kept_edges = [edge for edge in full_edges if edge[0] in selected and edge[1] in selected]
    edges_omitted = len(full_edges) - len(kept_edges)

    nodes = [by_id[node_id] for node_id in sorted(selected)]
    links = [{"source": source, "target": target, "relation": relation} for source, target, relation in kept_edges]
    return SubgraphResult(
        read_model={"nodes": nodes, "links": links},
        nodes_omitted=nodes_omitted,
        edges_omitted=edges_omitted,
    )


# ------------------------------------------------------------------ artifacts


def _slug(request: SubgraphRequest) -> str:
    raw = f"domain-{request.domain}" if request.domain is not None else f"node-{request.node_id}"
    return _UNSAFE_SLUG_CHAR_RE.sub("_", raw)[:_MAX_SLUG_CHARS]


def _json_payload(result: SubgraphResult, request: SubgraphRequest) -> dict[str, Any]:
    selector: dict[str, Any] = (
        {"domain": request.domain}
        if request.domain is not None
        else {"depth": request.depth, "node_id": request.node_id}
    )
    return {
        "artifact": "subgraph",
        "schema_version": 1,
        "selector": selector,
        "bounds": {"max_nodes": request.max_nodes},
        "truncated": {"nodes_omitted": result.nodes_omitted, "edges_omitted": result.edges_omitted},
        "graph": result.read_model,
    }


def export_subgraph(
    read_model: Mapping[str, Any],
    request: SubgraphRequest,
    *,
    max_bytes: int = DEFAULT_SUBGRAPH_MAX_BYTES,
) -> SubgraphExport:
    """Produce the canonical JSON + Mermaid artifacts for *request*."""
    if max_bytes < 1:
        raise SubgraphSelectionError(f"max_bytes must be positive, got {max_bytes}")
    result = build_subgraph(read_model, request)
    slug = _slug(request)
    json_bytes = (canonical_json(_json_payload(result, request)) + "\n").encode("utf-8")
    mermaid_bytes = render_mermaid(
        result.read_model,
        max_nodes=request.max_nodes,
        label_max_chars=request.label_max_chars,
    ).encode("utf-8")
    artifacts = {f"subgraphs/{slug}.json": json_bytes, f"subgraphs/{slug}.mmd": mermaid_bytes}
    for name, data in sorted(artifacts.items()):
        if len(data) > max_bytes:
            raise SubgraphCapExceededError(f"artifact {name} is {len(data)} bytes, over its {max_bytes}-byte cap")
    node_count = len(result.read_model["nodes"])
    edge_count = len(result.read_model["links"])
    return SubgraphExport(
        slug=slug,
        artifacts=artifacts,
        node_count=node_count,
        edge_count=edge_count,
        nodes_omitted=result.nodes_omitted,
        edges_omitted=result.edges_omitted,
    )


def subgraph_manifest_entries(export: SubgraphExport) -> dict[str, dict[str, Any]]:
    """Manifest-ready entries (sha256/size/version/kind) for the artifacts."""
    return {
        name: {
            "sha256": hashlib.sha256(data).hexdigest(),
            "size": len(data),
            "version": 1,
            "kind": "format",
        }
        for name, data in sorted(export.artifacts.items())
    }

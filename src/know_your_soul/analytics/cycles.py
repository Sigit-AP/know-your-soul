"""Bounded cycle enumeration over knowledge relations (plan F-A4).

Origin: idea from Graphify ``analyze.py`` ``find_import_cycles`` (L720-834 —
length bound, stop-early guard, canonical rotation dedup), re-targeted from
code imports to the knowledge relations ``depends_on`` / ``contradicts`` /
``supersedes``; implementation original.

Enumeration is anchored: each simple cycle is discovered exactly once from
its lexicographically smallest node, with neighbours visited in sorted order
and depth bounded by ``max_length``. Two explicit cost guards stop the
search deterministically (``max_cycles`` findings, ``max_expansions``
neighbour examinations) and surface themselves in ``guards``. Every finding
lists the cycle's relations; cycles carrying a ``contradicts`` edge are
flagged as circular-contradiction review candidates.
"""

from __future__ import annotations

from collections.abc import Mapping
from typing import Any

from know_your_soul.analytics.centrality import _prepare_graph

DEFAULT_CYCLE_RELATIONS: tuple[str, ...] = ("depends_on", "contradicts", "supersedes")
DEFAULT_MAX_LENGTH = 6
DEFAULT_MAX_CYCLES = 256
DEFAULT_MAX_EXPANSIONS = 65536


def _canonical_rotation(cycle: tuple[str, ...]) -> tuple[str, ...]:
    """Rotate a cycle so its lexicographically smallest node leads."""
    pivot = cycle.index(min(cycle))
    return cycle[pivot:] + cycle[:pivot]


# ------------------------------------------------------------------ public API


def find_cycles(
    graph: Mapping[str, Any],
    *,
    relations: tuple[str, ...] = DEFAULT_CYCLE_RELATIONS,
    max_length: int = DEFAULT_MAX_LENGTH,
    max_cycles: int = DEFAULT_MAX_CYCLES,
    max_expansions: int = DEFAULT_MAX_EXPANSIONS,
) -> dict[str, Any]:
    """Enumerate bounded simple cycles over the given knowledge relations.

    *graph* is the published read-model mapping (``graph.json`` shape). Only
    links whose relation is in *relations* participate. The result is a pure
    function of the graph content and the explicit parameters; truncation is
    deterministic because anchors and neighbours are visited in sorted order.
    """
    if not relations or any(not isinstance(relation, str) or not relation for relation in relations):
        raise ValueError("relations must be a non-empty tuple of relation names")
    if max_length < 2:
        raise ValueError("max_length must be >= 2")
    if max_cycles < 1:
        raise ValueError("max_cycles must be >= 1")
    if max_expansions < 1:
        raise ValueError("max_expansions must be >= 1")

    node_ids, _domains, links, dropped = _prepare_graph(graph)
    allowed = frozenset(relations)
    neighbours: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    edge_relations: dict[tuple[str, str], set[str]] = {}
    considered = 0
    for source, relation, target in links:
        if relation not in allowed:
            continue
        considered += 1
        neighbours[source].add(target)
        edge_relations.setdefault((source, target), set()).add(relation)
    adjacency = {node_id: tuple(sorted(targets)) for node_id, targets in neighbours.items()}
    position = {node_id: index for index, node_id in enumerate(node_ids)}

    found: list[tuple[str, ...]] = []
    seen_cycles: set[tuple[str, ...]] = set()
    guards: list[str] = []
    expansions = 0
    exhausted = False

    for anchor in node_ids:
        if exhausted:
            break
        if len(found) >= max_cycles:
            if "max_cycles" not in guards:
                guards.append("max_cycles")
            break
        path = [anchor]
        on_path = {anchor}
        frames = [iter(adjacency[anchor])]
        while frames:
            if len(found) >= max_cycles:
                if "max_cycles" not in guards:
                    guards.append("max_cycles")
                exhausted = True
                break
            descended = False
            for nxt in frames[-1]:
                expansions += 1
                if expansions > max_expansions:
                    if "max_expansions" not in guards:
                        guards.append("max_expansions")
                    exhausted = True
                    break
                if nxt == anchor and len(path) >= 2:
                    cycle = _canonical_rotation(tuple(path))
                    if cycle not in seen_cycles:
                        seen_cycles.add(cycle)
                        found.append(cycle)
                    continue
                if position[nxt] > position[anchor] and nxt not in on_path and len(path) < max_length:
                    path.append(nxt)
                    on_path.add(nxt)
                    frames.append(iter(adjacency[nxt]))
                    descended = True
                    break
            if exhausted:
                break
            if not descended:
                frames.pop()
                if frames:
                    on_path.discard(path.pop())

    entries = []
    for cycle in found:
        cycle_relations: set[str] = set()
        for index, source in enumerate(cycle):
            target = cycle[(index + 1) % len(cycle)]
            cycle_relations |= edge_relations.get((source, target), set())
        relation_list = sorted(cycle_relations)
        entries.append(
            {
                "nodes": list(cycle),
                "length": len(cycle),
                "relations": relation_list,
                "has_contradiction": "contradicts" in relation_list,
            }
        )
    entries.sort(key=lambda entry: (entry["length"], entry["nodes"]))
    guards.sort()
    return {
        "schema_version": 1,
        "kind": "analytics.cycles",
        "parameters": {
            "relations": sorted(allowed),
            "max_length": max_length,
            "max_cycles": max_cycles,
            "max_expansions": max_expansions,
        },
        "node_count": len(node_ids),
        "link_count": len(links),
        "links_considered": considered,
        "dropped_links": dropped,
        "guards": guards,
        "cycles": entries,
        "summary": {
            "cycles_found": len(entries),
            "contradiction_cycles": sum(1 for entry in entries if entry["has_contradiction"]),
            "truncated": bool(guards),
        },
    }

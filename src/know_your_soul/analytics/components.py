"""Deterministic connectivity analysis over the read model (plan F-A2).

Origin: idea from Graphify ``analyze.py`` isolated-node signal (L568-583)
and standard connectivity algorithms (ordered BFS, iterative Tarjan,
DFS low-link articulation/bridges, Batagelj-Zaversnik k-core);
implementation original.

Input is the published ``graph.json`` mapping plus an optional evidence-count
index (``node_id -> int``) supplied by the caller from the store; output is a
canonical dict. Components are listed largest-first with ``node_id``
tie-breaks; bridges are normalized to ``source < target``; the k-core pass
pops ``(degree, node_id)`` from a heap, so every step is a pure function of
the graph content.

"Knowledge islands" are orphan nodes — zero incident valid links *and* zero
evidence — surfaced as review candidates, never auto-acted upon.
"""

from __future__ import annotations

import heapq
from collections import deque
from collections.abc import Mapping
from typing import Any

from know_your_soul.analytics.centrality import _directed_adjacency, _prepare_graph, _undirected_adjacency

ISLAND_REASON = "no_evidence_and_no_valid_relations"


# ------------------------------------------------------------------ components


def _weak_components(node_ids: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]) -> list[list[str]]:
    seen: set[str] = set()
    components: list[tuple[str, ...]] = []
    for seed in node_ids:
        if seed in seen:
            continue
        seen.add(seed)
        members: list[str] = []
        frontier: deque[str] = deque([seed])
        while frontier:
            current = frontier.popleft()
            members.append(current)
            for nxt in adjacency[current]:
                if nxt not in seen:
                    seen.add(nxt)
                    frontier.append(nxt)
        components.append(tuple(sorted(members)))
    components.sort(key=lambda comp: (-len(comp), comp[0]))
    return [list(comp) for comp in components]


def _strong_components(node_ids: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]) -> list[list[str]]:
    """Iterative Tarjan over the sorted directed adjacency."""
    index: dict[str, int] = {}
    low: dict[str, int] = {}
    on_stack: set[str] = set()
    stack: list[str] = []
    components: list[tuple[str, ...]] = []
    counter = 0
    for root in node_ids:
        if root in index:
            continue
        index[root] = low[root] = counter
        counter += 1
        stack.append(root)
        on_stack.add(root)
        call: list[tuple[str, int]] = [(root, 0)]
        while call:
            node, child_index = call[-1]
            neighbours = adjacency[node]
            if child_index < len(neighbours):
                call[-1] = (node, child_index + 1)
                nxt = neighbours[child_index]
                if nxt not in index:
                    index[nxt] = low[nxt] = counter
                    counter += 1
                    stack.append(nxt)
                    on_stack.add(nxt)
                    call.append((nxt, 0))
                elif nxt in on_stack:
                    low[node] = min(low[node], index[nxt])
            else:
                call.pop()
                if call:
                    parent = call[-1][0]
                    low[parent] = min(low[parent], low[node])
                if low[node] == index[node]:
                    members: list[str] = []
                    while True:
                        member = stack.pop()
                        on_stack.discard(member)
                        members.append(member)
                        if member == node:
                            break
                    components.append(tuple(sorted(members)))
    components.sort(key=lambda comp: (-len(comp), comp[0]))
    return [list(comp) for comp in components]


def _articulation_and_bridges(
    node_ids: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]
) -> tuple[list[str], list[tuple[str, str]]]:
    """Iterative low-link DFS on the collapsed undirected view."""
    disc: dict[str, int] = {}
    low: dict[str, int] = {}
    parent: dict[str, str | None] = {}
    articulation: set[str] = set()
    bridges: set[tuple[str, str]] = set()
    clock = 0
    for root in node_ids:
        if root in disc:
            continue
        disc[root] = low[root] = clock
        clock += 1
        parent[root] = None
        root_children = 0
        call: list[tuple[str, int]] = [(root, 0)]
        while call:
            node, child_index = call[-1]
            neighbours = adjacency[node]
            if child_index < len(neighbours):
                call[-1] = (node, child_index + 1)
                nxt = neighbours[child_index]
                if nxt not in disc:
                    parent[nxt] = node
                    disc[nxt] = low[nxt] = clock
                    clock += 1
                    if node == root:
                        root_children += 1
                    call.append((nxt, 0))
                elif nxt != parent[node]:
                    low[node] = min(low[node], disc[nxt])
            else:
                call.pop()
                if call:
                    parent_id = call[-1][0]
                    low[parent_id] = min(low[parent_id], low[node])
                    if parent[parent_id] is not None and low[node] >= disc[parent_id]:
                        articulation.add(parent_id)
                    if low[node] > disc[parent_id]:
                        bridges.add((min(parent_id, node), max(parent_id, node)))
        if root_children > 1:
            articulation.add(root)
    return sorted(articulation), sorted(bridges)


def _k_core(node_ids: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]) -> dict[str, int]:
    """Core numbers via deterministic min-degree removal (heap, id tie-break)."""
    degree = {node_id: len(adjacency[node_id]) for node_id in node_ids}
    heap = [(degree[node_id], node_id) for node_id in node_ids]
    heapq.heapify(heap)
    removed: set[str] = set()
    core: dict[str, int] = {}
    while heap:
        current_degree, node = heapq.heappop(heap)
        if node in removed or current_degree != degree[node]:
            continue
        removed.add(node)
        core[node] = current_degree
        for peer in adjacency[node]:
            if peer not in removed and degree[peer] > current_degree:
                degree[peer] -= 1
                heapq.heappush(heap, (degree[peer], peer))
    return core


# ------------------------------------------------------------------ public API


def analyze_components(
    graph: Mapping[str, Any],
    *,
    evidence_counts: Mapping[str, int] | None = None,
) -> dict[str, Any]:
    """Analyze connectivity and surface orphan "knowledge island" candidates.

    *graph* is the published read-model mapping (``graph.json`` shape).
    *evidence_counts* maps node ids to their evidence count in the store;
    nodes absent from the mapping count as zero evidence. A node is an
    island when it has zero incident valid links **and** zero evidence.
    """
    counts: dict[str, int] = {}
    if evidence_counts is not None:
        if not isinstance(evidence_counts, Mapping):
            raise ValueError("evidence_counts must be a mapping of node_id -> int")
        for key, value in evidence_counts.items():
            if not isinstance(key, str) or isinstance(value, bool) or not isinstance(value, int) or value < 0:
                raise ValueError("evidence_counts must map node ids to non-negative ints")
            counts[key] = value
    node_ids, domains, links, dropped = _prepare_graph(graph)
    undirected = _undirected_adjacency(node_ids, links)
    directed = _directed_adjacency(node_ids, links)

    weak = _weak_components(node_ids, undirected)
    strong = _strong_components(node_ids, directed)
    articulation, bridges = _articulation_and_bridges(node_ids, undirected)
    core = _k_core(node_ids, undirected)

    incident: set[str] = set()
    for source, _relation, target in links:
        incident.add(source)
        incident.add(target)
    islands = [
        {"node_id": node_id, "domain": domains.get(node_id, ""), "reason": ISLAND_REASON}
        for node_id in node_ids
        if node_id not in incident and counts.get(node_id, 0) == 0
    ]
    islands.sort(key=lambda item: (item["domain"], item["node_id"]))

    max_core = max(core.values(), default=0)
    core_entries = sorted(core.items(), key=lambda item: (-item[1], item[0]))
    return {
        "schema_version": 1,
        "kind": "analytics.components",
        "node_count": len(node_ids),
        "link_count": len(links),
        "dropped_links": dropped,
        "weak_components": [
            {"component_id": index, "size": len(members), "nodes": members} for index, members in enumerate(weak)
        ],
        "strong_components": [
            {"component_id": index, "size": len(members), "nodes": members, "cyclic": len(members) > 1}
            for index, members in enumerate(strong)
        ],
        "articulation_points": articulation,
        "bridges": [{"source": source, "target": target} for source, target in bridges],
        "k_core": {
            "max_core": max_core,
            "nodes": [{"node_id": node_id, "core": value} for node_id, value in core_entries],
        },
        "knowledge_islands": islands,
        "summary": {
            "weak_components": len(weak),
            "strong_components": len(strong),
            "cyclic_strong_components": sum(1 for members in strong if len(members) > 1),
            "articulation_points": len(articulation),
            "bridges": len(bridges),
            "knowledge_islands": len(islands),
            "max_core": max_core,
        },
    }

"""Deterministic bounded k-shortest paths (Yen) over the read model.

Companion to ``analytics.algorithms`` (mega-wave 2, SUB-74): same input
contract (the published ``graph.json`` mapping), same output conventions
(``schema_version: 1``, canonical ordering, honest guards).

``find_k_shortest_paths`` runs Yen's algorithm on the collapsed directed
view with unit hop costs:

* the inner shortest-path search is a uniform-cost search whose heap is
  keyed by ``(cost, path_tuple)``, so among equal-cost candidates the
  lexicographically smallest node sequence always wins — the result never
  depends on input ordering;
* only simple paths are produced (a node never repeats within a path);
* work is bounded twice: by ``k`` and by a global expansion budget
  (``max_expansions`` heap pops); exhausting the budget adds the
  ``max_expansions_reached`` guard and keeps every path already found;
* unknown endpoints or ``source == target`` fail closed with the typed
  :class:`PathQueryError` before any search happens.

origin: classical algorithm (Yen 1971); implementation original.
"""

from __future__ import annotations

import heapq
from collections.abc import Mapping
from typing import Any

from know_your_soul.analytics.algorithms import SCHEMA_VERSION, AlgorithmError
from know_your_soul.analytics.centrality import _directed_adjacency, _prepare_graph

__all__ = [
    "DEFAULT_K",
    "DEFAULT_MAX_EXPANSIONS",
    "PathQueryError",
    "find_k_shortest_paths",
]

DEFAULT_K = 8
DEFAULT_MAX_EXPANSIONS = 65536


class PathQueryError(AlgorithmError):
    """The path query itself is invalid (unknown endpoint, source == target)."""


class _ExpansionBudget:
    """Shared pop counter so the cap is global across all spur searches."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.used = 0
        self.exhausted = False

    def spend(self) -> bool:
        """Account one heap pop; returns False (and latches) past the cap."""
        if self.used >= self.limit:
            self.exhausted = True
            return False
        self.used += 1
        return True


def _shortest_path(
    adjacency: Mapping[str, tuple[str, ...]],
    start: str,
    target: str,
    banned_nodes: frozenset[str],
    banned_edges: frozenset[tuple[str, str]],
    budget: _ExpansionBudget,
) -> tuple[str, ...] | None:
    """Lexicographically smallest minimum-hop path under the bans.

    Uniform-cost search over ``(cost, path)`` heap keys: the first time the
    target pops, its path is optimal and tie-broken canonically.
    """
    if start in banned_nodes:
        return None
    heap: list[tuple[int, tuple[str, ...]]] = [(0, (start,))]
    while heap:
        cost, path = heapq.heappop(heap)
        if not budget.spend():
            return None
        node = path[-1]
        if node == target:
            return path
        for neighbour in adjacency[node]:
            if neighbour in path or neighbour in banned_nodes:
                continue
            if (node, neighbour) in banned_edges:
                continue
            heapq.heappush(heap, (cost + 1, (*path, neighbour)))
    return None


def find_k_shortest_paths(
    graph: Mapping[str, Any],
    *,
    source: str,
    target: str,
    k: int = DEFAULT_K,
    max_expansions: int = DEFAULT_MAX_EXPANSIONS,
) -> dict[str, Any]:
    """Yen's k-shortest simple paths on the collapsed directed view.

    Returns at most ``k`` paths ordered by ``(cost, nodes)`` with explicit
    ranks. Fewer than ``k`` paths simply means no more exist (or the
    expansion budget fired, which the guard reports).
    """
    if k < 1:
        raise ValueError("k must be >= 1")
    if max_expansions < 1:
        raise ValueError("max_expansions must be >= 1")
    node_ids, _domains, links, dropped = _prepare_graph(graph)
    adjacency = _directed_adjacency(node_ids, links)
    if source not in adjacency:
        raise PathQueryError(f"unknown source node: {source!r}")
    if target not in adjacency:
        raise PathQueryError(f"unknown target node: {target!r}")
    if source == target:
        raise PathQueryError("source and target must differ")

    budget = _ExpansionBudget(max_expansions)
    paths: list[tuple[str, ...]] = []
    candidates: list[tuple[int, tuple[str, ...]]] = []
    seen: set[tuple[str, ...]] = set()

    first = _shortest_path(adjacency, source, target, frozenset(), frozenset(), budget)
    if first is not None:
        paths.append(first)
        seen.add(first)

    while paths and len(paths) < k and not budget.exhausted:
        previous = paths[-1]
        for index in range(len(previous) - 1):
            spur_node = previous[index]
            root = previous[: index + 1]
            banned_edges = frozenset(
                (path[index], path[index + 1]) for path in paths if len(path) > index and path[: index + 1] == root
            )
            banned_nodes = frozenset(root[:-1])
            spur = _shortest_path(adjacency, spur_node, target, banned_nodes, banned_edges, budget)
            if spur is None:
                continue
            total = (*root[:-1], *spur)
            if total not in seen:
                seen.add(total)
                heapq.heappush(candidates, (len(total) - 1, total))
        if not candidates:
            break
        _cost, best = heapq.heappop(candidates)
        paths.append(best)

    guards = ["max_expansions_reached"] if budget.exhausted else []
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": "analytics.paths.k_shortest",
        "parameters": {"source": source, "target": target, "k": k, "max_expansions": max_expansions},
        "node_count": len(node_ids),
        "link_count": len(links),
        "dropped_links": dropped,
        "path_count": len(paths),
        "paths": [{"rank": rank, "cost": len(path) - 1, "nodes": list(path)} for rank, path in enumerate(paths)],
        "guards": guards,
    }

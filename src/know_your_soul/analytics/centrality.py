"""Deterministic centrality suite over the read model (plan F-A1).

Origin: idea from Graphify ``analyze.py`` (degree ranking L112, sampled
betweenness with a fixed seed and ``k = min(k, n)`` guard L513-522);
implementation original.

Input is the published ``graph.json`` mapping (``nodes``/``links``), never a
live store; output is a canonical dict whose scores are 12-decimal
fixed-point strings (spec section 9), ready to embed in a generation
artifact. All arithmetic is :class:`~decimal.Decimal` at precision 34; the
betweenness sampler is a locally seeded RNG over the *sorted* node list, so
permuting the input never changes the output.

Metrics (all on the collapsed directed view — parallel typed relations on one
node pair count once):

* ``degree`` — weak degree (distinct neighbours, direction ignored),
  normalized by ``n - 1``;
* ``betweenness`` — Brandes accumulation from ``k = min(samples, n)``
  fixed-seed source samples, scaled by ``n/k`` and normalized by
  ``(n-1)(n-2)``; the guard ``betweenness_sampled`` marks ``k < n``;
* ``harmonic`` — sum of ``1/d`` over forward-reachable nodes, normalized by
  ``n - 1`` (unreachable nodes contribute zero, so no cutoff is needed);
* ``closeness`` — Wasserman-Faust style: ``(r/(n-1)) * (r/sum_d)`` over
  nodes reachable within ``closeness_cutoff`` hops;
* ``pagerank`` — fixed-iteration power method (damping 0.85, dangling mass
  redistributed uniformly), iterated in sorted node order.
"""

from __future__ import annotations

import random
from collections import deque
from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Any

from know_your_soul.domain import DECIMAL_PRECISION, PrimaryDomain
from know_your_soul.scoring import serialize_score

_BETWEENNESS_SEED = 0x4B5953  # fixed seed ("KYS"); never derived from input order
_PAGERANK_DAMPING = "0.85"

DEFAULT_BETWEENNESS_SAMPLES = 32
DEFAULT_PAGERANK_ITERATIONS = 64
DEFAULT_CLOSENESS_CUTOFF = 6


# ------------------------------------------------------------------ read model


# A normalized link: ``(source, relation, target)``.
LinkTuple = tuple[str, str, str]
# The canonical graph parts: sorted node ids, id->domain map, sorted unique
# links, and the count of rejected raw link rows.
PreparedGraph = tuple[tuple[str, ...], dict[str, str], tuple[LinkTuple, ...], int]


def _prepare_graph(graph: Mapping[str, Any]) -> PreparedGraph:
    """Normalize the read model into canonical parts.

    Returns ``(node_ids, domains, links, dropped)`` where ``node_ids`` is the
    sorted unique id tuple, ``domains`` maps id to its domain string,
    ``links`` is the sorted unique ``(source, relation, target)`` tuple list,
    and ``dropped`` counts rejected link rows (malformed, unknown endpoint,
    self-loop or duplicate). The result is a pure function of the graph
    *content*: input ordering is irrelevant.
    """
    domains: dict[str, str] = {}
    raw_nodes = graph.get("nodes", [])
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if not isinstance(entry, Mapping):
                continue
            node_id = entry.get("id")
            if not isinstance(node_id, str) or not node_id or node_id in domains:
                continue
            domain = entry.get("domain")
            domains[node_id] = domain if isinstance(domain, str) else ""
    raw_links = graph.get("links", [])
    seen: set[tuple[str, str, str]] = set()
    links: list[tuple[str, str, str]] = []
    dropped = 0
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                dropped += 1
                continue
            source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
            if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)):
                dropped += 1
                continue
            if source not in domains or target not in domains or source == target:
                dropped += 1
                continue
            key = (source, relation, target)
            if key in seen:
                dropped += 1
                continue
            seen.add(key)
            links.append(key)
    links.sort()
    return tuple(sorted(domains)), domains, tuple(links), dropped


def _directed_adjacency(node_ids: tuple[str, ...], links: tuple[LinkTuple, ...]) -> dict[str, tuple[str, ...]]:
    neighbours: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for source, _relation, target in links:
        neighbours[source].add(target)
    return {node_id: tuple(sorted(targets)) for node_id, targets in neighbours.items()}


def _undirected_adjacency(node_ids: tuple[str, ...], links: tuple[LinkTuple, ...]) -> dict[str, tuple[str, ...]]:
    neighbours: dict[str, set[str]] = {node_id: set() for node_id in node_ids}
    for source, _relation, target in links:
        neighbours[source].add(target)
        neighbours[target].add(source)
    return {node_id: tuple(sorted(peers)) for node_id, peers in neighbours.items()}


def _bfs_distances(source: str, adjacency: Mapping[str, tuple[str, ...]]) -> dict[str, int]:
    """Unweighted forward distances from *source* over the sorted adjacency."""
    dist = {source: 0}
    frontier: deque[str] = deque([source])
    while frontier:
        current = frontier.popleft()
        for nxt in adjacency[current]:
            if nxt not in dist:
                dist[nxt] = dist[current] + 1
                frontier.append(nxt)
    return dist


# ------------------------------------------------------------------ metrics


def _degree_scores(node_ids: tuple[str, ...], links: tuple[tuple[str, str, str], ...]) -> dict[str, Decimal]:
    n = len(node_ids)
    adjacency = _undirected_adjacency(node_ids, links)
    if n < 2:
        return {node_id: Decimal(0) for node_id in node_ids}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        norm = Decimal(n - 1)
        return {node_id: Decimal(len(adjacency[node_id])) / norm for node_id in node_ids}


def _betweenness_scores(
    node_ids: tuple[str, ...],
    adjacency: Mapping[str, tuple[str, ...]],
    samples: int,
) -> tuple[dict[str, Decimal], bool]:
    """Sampled Brandes betweenness; returns ``(scores, sampled)``."""
    n = len(node_ids)
    if n < 3:
        return {node_id: Decimal(0) for node_id in node_ids}, False
    k = min(samples, n)
    rng = random.Random(_BETWEENNESS_SEED)
    seeds = rng.sample(list(node_ids), k)
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        centrality = {node_id: Decimal(0) for node_id in node_ids}
        for seed in seeds:
            stack: list[str] = []
            predecessors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
            sigma = {node_id: 0 for node_id in node_ids}
            sigma[seed] = 1
            dist = {seed: 0}
            queue: deque[str] = deque([seed])
            while queue:
                current = queue.popleft()
                stack.append(current)
                for nxt in adjacency[current]:
                    if nxt not in dist:
                        dist[nxt] = dist[current] + 1
                        queue.append(nxt)
                    if dist[nxt] == dist[current] + 1:
                        sigma[nxt] += sigma[current]
                        predecessors[nxt].append(current)
            delta = {node_id: Decimal(0) for node_id in node_ids}
            while stack:
                node = stack.pop()
                if sigma[node]:
                    share = Decimal(1) + delta[node]
                    for predecessor in predecessors[node]:
                        delta[predecessor] += Decimal(sigma[predecessor]) / Decimal(sigma[node]) * share
                if node != seed:
                    centrality[node] += delta[node]
        scale = Decimal(n) / Decimal(k)
        norm = Decimal((n - 1) * (n - 2))
        return {node_id: centrality[node_id] * scale / norm for node_id in node_ids}, k < n


def _reachability_scores(
    node_ids: tuple[str, ...],
    adjacency: Mapping[str, tuple[str, ...]],
    cutoff: int,
) -> tuple[dict[str, Decimal], dict[str, Decimal]]:
    """Harmonic (unbounded) and bounded closeness, both normalized."""
    n = len(node_ids)
    harmonic = {node_id: Decimal(0) for node_id in node_ids}
    closeness = {node_id: Decimal(0) for node_id in node_ids}
    if n < 2:
        return harmonic, closeness
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        norm = Decimal(n - 1)
        for node_id in node_ids:
            dist = _bfs_distances(node_id, adjacency)
            total = Decimal(0)
            within = 0
            within_sum = 0
            for other, hops in dist.items():
                if other == node_id:
                    continue
                total += Decimal(1) / Decimal(hops)
                if hops <= cutoff:
                    within += 1
                    within_sum += hops
            harmonic[node_id] = total / norm
            if within_sum:
                reach = Decimal(within) / norm
                closeness[node_id] = reach * (Decimal(within) / Decimal(within_sum))
    return harmonic, closeness


def _pagerank_scores(
    node_ids: tuple[str, ...],
    links: tuple[LinkTuple, ...],
    adjacency: Mapping[str, tuple[str, ...]],
    iterations: int,
) -> dict[str, Decimal]:
    n = len(node_ids)
    if n == 0:
        return {}
    predecessors: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    seen_pairs: set[tuple[str, str]] = set()
    for source, _relation, target in links:
        if (source, target) not in seen_pairs:
            seen_pairs.add((source, target))
            predecessors[target].append(source)
    for preds in predecessors.values():
        preds.sort()
    out_degree = {node_id: len(adjacency[node_id]) for node_id in node_ids}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        damping = Decimal(_PAGERANK_DAMPING)
        base = (Decimal(1) - damping) / Decimal(n)
        rank = {node_id: Decimal(1) / Decimal(n) for node_id in node_ids}
        for _ in range(iterations):
            dangling = sum((rank[node_id] for node_id in node_ids if out_degree[node_id] == 0), Decimal(0))
            dangling_share = dangling / Decimal(n)
            nxt: dict[str, Decimal] = {}
            for node_id in node_ids:
                inflow = sum(
                    (rank[source] / Decimal(out_degree[source]) for source in predecessors[node_id]),
                    Decimal(0),
                )
                nxt[node_id] = base + damping * (inflow + dangling_share)
            rank = nxt
    return rank


# ------------------------------------------------------------------ ranking


def _ranking(scores: Mapping[str, Decimal], top: int | None) -> list[dict[str, str]]:
    """Canonical ranking: ``(-score, node_id)`` order, 12-decimal strings."""
    ordered = sorted(scores.items(), key=lambda item: (-item[1], item[0]))
    if top is not None:
        ordered = ordered[:top]
    return [{"node_id": node_id, "score": serialize_score(value)} for node_id, value in ordered]


def _suite(
    node_ids: tuple[str, ...],
    links: tuple[LinkTuple, ...],
    *,
    betweenness_samples: int,
    pagerank_iterations: int,
    closeness_cutoff: int,
    top: int | None,
) -> tuple[list[str], dict[str, list[dict[str, str]]]]:
    adjacency = _directed_adjacency(node_ids, links)
    betweenness, sampled = _betweenness_scores(node_ids, adjacency, betweenness_samples)
    harmonic, closeness = _reachability_scores(node_ids, adjacency, closeness_cutoff)
    guards = ["betweenness_sampled"] if sampled else []
    rankings = {
        "degree": _ranking(_degree_scores(node_ids, links), top),
        "betweenness": _ranking(betweenness, top),
        "harmonic": _ranking(harmonic, top),
        "closeness": _ranking(closeness, top),
        "pagerank": _ranking(_pagerank_scores(node_ids, links, adjacency, pagerank_iterations), top),
    }
    return guards, rankings


# ------------------------------------------------------------------ public API


def compute_centrality(
    graph: Mapping[str, Any],
    *,
    betweenness_samples: int = DEFAULT_BETWEENNESS_SAMPLES,
    pagerank_iterations: int = DEFAULT_PAGERANK_ITERATIONS,
    closeness_cutoff: int = DEFAULT_CLOSENESS_CUTOFF,
    top: int | None = None,
) -> dict[str, Any]:
    """Run the centrality suite globally and per primary domain (P1..P7).

    *graph* is the published read-model mapping (``graph.json`` shape). The
    per-domain blocks are the same five rankings computed on the induced
    subgraph of each domain. ``top`` caps every ranking's length (``None``
    keeps all nodes). The result is a pure function of the graph content and
    the explicit parameters — never of input ordering, wall time, or global
    RNG state.
    """
    if betweenness_samples < 1:
        raise ValueError("betweenness_samples must be >= 1")
    if pagerank_iterations < 1:
        raise ValueError("pagerank_iterations must be >= 1")
    if closeness_cutoff < 1:
        raise ValueError("closeness_cutoff must be >= 1")
    if top is not None and top < 1:
        raise ValueError("top must be >= 1 when given")
    node_ids, domains, links, dropped = _prepare_graph(graph)
    params = {
        "betweenness_samples": betweenness_samples,
        "pagerank_iterations": pagerank_iterations,
        "closeness_cutoff": closeness_cutoff,
        "top": top,
    }
    guards, rankings = _suite(
        node_ids,
        links,
        betweenness_samples=betweenness_samples,
        pagerank_iterations=pagerank_iterations,
        closeness_cutoff=closeness_cutoff,
        top=top,
    )
    domain_blocks: dict[str, Any] = {}
    for domain in PrimaryDomain:
        members = tuple(node_id for node_id in node_ids if domains.get(node_id) == domain.value)
        member_set = set(members)
        sub_links = tuple(link for link in links if link[0] in member_set and link[2] in member_set)
        sub_guards, sub_rankings = _suite(
            members,
            sub_links,
            betweenness_samples=betweenness_samples,
            pagerank_iterations=pagerank_iterations,
            closeness_cutoff=closeness_cutoff,
            top=top,
        )
        domain_blocks[domain.value] = {"node_count": len(members), "guards": sub_guards, **sub_rankings}
    return {
        "schema_version": 1,
        "kind": "analytics.centrality",
        "parameters": params,
        "node_count": len(node_ids),
        "link_count": len(links),
        "dropped_links": dropped,
        "guards": guards,
        "global": rankings,
        "domains": domain_blocks,
    }

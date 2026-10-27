"""Deterministic graph algorithm suite over the read model (mega-wave 2, SUB-74).

Extends the wave-A analytics (``centrality.py``, ``components.py``,
``communities.py``) with the classical structure algorithms a knowledge
graph actually benefits from:

* ``compute_hits`` — HITS hubs/authorities, fixed-iteration power method on
  the collapsed directed view, L1-normalized in sorted node order;
* ``compute_eigenvector`` — eigenvector centrality, L2-normalized power
  iteration on the collapsed undirected view (convergence is reported
  honestly: bipartite-shaped graphs make power iteration oscillate and the
  module says ``converged: false`` instead of pretending);
* ``compute_katz`` — Katz prestige, truncated path expansion
  ``beta * sum_k alpha^k * (A^T)^k * 1`` with an explicit iteration cap and
  a ``katz_truncated`` guard when the cap fires;
* ``compute_clustering`` — per-node local clustering coefficient plus the
  global average and transitivity on the collapsed undirected view;
* ``compute_louvain`` — deterministic Louvain community detection: greedy
  local-moving passes in canonical node order (strictly positive modularity
  gain, ties break to the smallest community key) followed by graph
  aggregation, level by level, with no randomness anywhere;
* ``compute_reachability`` — all-pairs forward reachability *summary* (per
  node reachable set size/farthest hop, pair accounting, density, diameter);
* ``count_motifs`` — triangle counting (per node and total) and the full
  16-class directed triad census (MAN labels);
* ``compute_assortativity`` — degree assortativity (Pearson over the unique
  undirected pairs, each edge counted in both orientations).

k-core deliberately lives in ``components.py`` (wave A); this suite
composes with it instead of duplicating it — same input contract, same
output conventions.

Conventions shared with the other analytics modules: input is the published
``graph.json`` mapping, output is a canonical artifact-ready dict with
``schema_version: 1``; every score is a 12-decimal fixed-point string via
``scoring.serialize_score``; internal arithmetic is ``Decimal`` at
precision 34 inside ``localcontext``; iteration only ever walks sorted
lists, so results are pure functions of graph *content* — never of input
ordering, wall time, or ambient RNG state. Oversized inputs fail closed
with the typed :class:`GraphCapExceededError` before any work happens.

origin: classical algorithms from the literature (Kleinberg HITS;
Bonacich power iteration; Katz 1953; Watts-Strogatz clustering; Blondel et
al. Louvain local moving; Wasserman-Faust triad census; Newman
assortativity). Graphify implements none of these itself. Implementation
original.
"""

from __future__ import annotations

from collections.abc import Mapping
from decimal import Decimal, InvalidOperation, localcontext
from typing import Any

from know_your_soul.analytics.centrality import (
    _bfs_distances,
    _directed_adjacency,
    _prepare_graph,
    _ranking,
    _undirected_adjacency,
)
from know_your_soul.domain import DECIMAL_PRECISION, PrimaryDomain
from know_your_soul.scoring import serialize_score

__all__ = [
    "DEFAULT_EIGENVECTOR_ITERATIONS",
    "DEFAULT_EPSILON",
    "DEFAULT_HITS_ITERATIONS",
    "DEFAULT_KATZ_ALPHA",
    "DEFAULT_KATZ_BETA",
    "DEFAULT_KATZ_ITERATIONS",
    "DEFAULT_LOUVAIN_MAX_LEVELS",
    "DEFAULT_LOUVAIN_MAX_NODES",
    "DEFAULT_LOUVAIN_MAX_PASSES",
    "DEFAULT_MOTIF_MAX_NODES",
    "DEFAULT_REACHABILITY_MAX_NODES",
    "SCHEMA_VERSION",
    "TRIAD_CLASSES",
    "AlgorithmError",
    "GraphCapExceededError",
    "compute_assortativity",
    "compute_clustering",
    "compute_eigenvector",
    "compute_hits",
    "compute_katz",
    "compute_louvain",
    "compute_reachability",
    "count_motifs",
]

SCHEMA_VERSION = 1

DEFAULT_HITS_ITERATIONS = 64
DEFAULT_EIGENVECTOR_ITERATIONS = 128
DEFAULT_KATZ_ITERATIONS = 32
DEFAULT_KATZ_ALPHA = "0.05"
DEFAULT_KATZ_BETA = "1"
DEFAULT_EPSILON = "1e-15"
DEFAULT_LOUVAIN_MAX_PASSES = 32
DEFAULT_LOUVAIN_MAX_LEVELS = 16
DEFAULT_LOUVAIN_MAX_NODES = 2048
DEFAULT_REACHABILITY_MAX_NODES = 1024
DEFAULT_MOTIF_MAX_NODES = 256

# The 16 triad isomorphism classes in MAN (mutual-asymmetric-null) order.
TRIAD_CLASSES = (
    "003",
    "012",
    "102",
    "021D",
    "021U",
    "021C",
    "111D",
    "111U",
    "030T",
    "030C",
    "201",
    "120D",
    "120U",
    "120C",
    "210",
    "300",
)

_ZERO = Decimal(0)
_ONE = Decimal(1)


# ------------------------------------------------------------------- errors


class AlgorithmError(ValueError):
    """Base class for typed algorithm-suite input failures."""


class GraphCapExceededError(AlgorithmError):
    """The input graph exceeds an algorithm's explicit node cap.

    Carries the algorithm name, the configured cap, and the actual node
    count so callers can react programmatically; the failing algorithm has
    done no work and no partial result exists.
    """

    def __init__(self, algorithm: str, cap: int, actual: int) -> None:
        self.algorithm = algorithm
        self.cap = cap
        self.actual = actual
        super().__init__(f"{algorithm}: input has {actual} nodes; the cap is {cap}")


# ----------------------------------------------------------------- helpers


def _to_decimal(name: str, value: Any) -> Decimal:
    """Parse an explicit numeric parameter into a finite ``Decimal``."""
    if isinstance(value, bool):
        raise ValueError(f"{name} must be a decimal, not a bool")
    try:
        result = value if isinstance(value, Decimal) else Decimal(str(value))
    except (InvalidOperation, ValueError) as exc:
        raise ValueError(f"{name} must be a decimal: {value!r}") from exc
    if not result.is_finite():
        raise ValueError(f"{name} must be finite")
    return result


def _check_node_cap(algorithm: str, node_count: int, cap: int) -> None:
    if cap < 1:
        raise ValueError(f"{algorithm}: max_nodes must be >= 1")
    if node_count > cap:
        raise GraphCapExceededError(algorithm, cap, node_count)


def _validate_epsilon(epsilon: Any) -> Decimal:
    eps = _to_decimal("epsilon", epsilon)
    if eps <= _ZERO:
        raise ValueError("epsilon must be > 0")
    return eps


def _validate_top(top: int | None) -> None:
    if top is not None and top < 1:
        raise ValueError("top must be >= 1 when given")


def _predecessors(node_ids: tuple[str, ...], adjacency: Mapping[str, tuple[str, ...]]) -> dict[str, list[str]]:
    """Inbound neighbour lists over the collapsed directed view, sorted."""
    preds: dict[str, list[str]] = {node_id: [] for node_id in node_ids}
    for source in node_ids:
        for target in adjacency[source]:
            preds[target].append(source)
    for sources in preds.values():
        sources.sort()
    return preds


def _base_result(kind: str, node_count: int, link_count: int, dropped: int) -> dict[str, Any]:
    return {
        "schema_version": SCHEMA_VERSION,
        "kind": kind,
        "node_count": node_count,
        "link_count": link_count,
        "dropped_links": dropped,
    }


def _domain_subgraphs(
    node_ids: tuple[str, ...],
    domains: Mapping[str, str],
    links: tuple[tuple[str, str, str], ...],
) -> dict[str, tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]]:
    """Induced subgraph per primary domain (P1..P7), in canonical order.

    Mirrors the per-domain blocks of ``analytics.centrality`` so the whole
    analytics family shares one output convention.
    """
    blocks: dict[str, tuple[tuple[str, ...], tuple[tuple[str, str, str], ...]]] = {}
    for domain in PrimaryDomain:
        members = tuple(node_id for node_id in node_ids if domains.get(node_id) == domain.value)
        member_set = set(members)
        sub_links = tuple(link for link in links if link[0] in member_set and link[2] in member_set)
        blocks[domain.value] = (members, sub_links)
    return blocks


# -------------------------------------------------------------------- HITS


def _hits_vectors(
    node_ids: tuple[str, ...],
    links: tuple[tuple[str, str, str], ...],
    max_iterations: int,
    eps: Decimal,
) -> tuple[dict[str, Decimal], dict[str, Decimal], int, bool]:
    """One HITS run; returns ``(hubs, authorities, iterations, converged)``."""
    adjacency = _directed_adjacency(node_ids, links)
    preds = _predecessors(node_ids, adjacency)
    hubs = {node_id: _ZERO for node_id in node_ids}
    auth = {node_id: _ZERO for node_id in node_ids}
    converged = True
    iterations_used = 0
    if node_ids and links:
        hubs = {node_id: _ONE for node_id in node_ids}
        auth = {node_id: _ONE for node_id in node_ids}
        converged = False
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            for iteration in range(max_iterations):
                iterations_used = iteration + 1
                raw_auth = {node_id: sum((hubs[source] for source in preds[node_id]), _ZERO) for node_id in node_ids}
                auth_mass = sum(raw_auth.values(), _ZERO)
                new_auth = (
                    {node_id: value / auth_mass for node_id, value in raw_auth.items()}
                    if auth_mass
                    else dict.fromkeys(node_ids, _ZERO)
                )
                raw_hub = {node_id: sum((auth[target] for target in adjacency[node_id]), _ZERO) for node_id in node_ids}
                hub_mass = sum(raw_hub.values(), _ZERO)
                new_hub = (
                    {node_id: value / hub_mass for node_id, value in raw_hub.items()}
                    if hub_mass
                    else dict.fromkeys(node_ids, _ZERO)
                )
                diff = _ZERO
                for node_id in node_ids:
                    diff = max(diff, abs(new_auth[node_id] - auth[node_id]), abs(new_hub[node_id] - hubs[node_id]))
                hubs, auth = new_hub, new_auth
                if diff < eps:
                    converged = True
                    break
    return hubs, auth, iterations_used, converged


def compute_hits(
    graph: Mapping[str, Any],
    *,
    max_iterations: int = DEFAULT_HITS_ITERATIONS,
    epsilon: Any = DEFAULT_EPSILON,
    top: int | None = None,
) -> dict[str, Any]:
    """HITS hubs and authorities on the collapsed directed view.

    Both vectors start at 1 per node; each iteration recomputes authorities
    from the *previous* hubs and hubs from the *previous* authorities
    (simultaneous update), L1-normalizing both, so a converged vector sums
    to 1 and reversing every edge swaps hubs and authorities exactly at
    every iteration. Convergence means the largest per-node change fell
    below ``epsilon``; otherwise the iteration cap is reported honestly via
    ``converged: false``. The suite runs globally and per primary domain
    (P1..P7) on the induced subgraphs, like ``analytics.centrality``.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    eps = _validate_epsilon(epsilon)
    _validate_top(top)
    node_ids, domains, links, dropped = _prepare_graph(graph)
    hubs, auth, iterations_used, converged = _hits_vectors(node_ids, links, max_iterations, eps)

    result = _base_result("analytics.algorithms.hits", len(node_ids), len(links), dropped)
    result["parameters"] = {"max_iterations": max_iterations, "epsilon": str(eps), "top": top}
    result["iterations_used"] = iterations_used
    result["converged"] = converged
    result["hubs"] = _ranking(hubs, top)
    result["authorities"] = _ranking(auth, top)
    domain_blocks: dict[str, Any] = {}
    for domain, (members, sub_links) in _domain_subgraphs(node_ids, domains, links).items():
        sub_hubs, sub_auth, sub_iterations, sub_converged = _hits_vectors(members, sub_links, max_iterations, eps)
        domain_blocks[domain] = {
            "node_count": len(members),
            "iterations_used": sub_iterations,
            "converged": sub_converged,
            "hubs": _ranking(sub_hubs, top),
            "authorities": _ranking(sub_auth, top),
        }
    result["domains"] = domain_blocks
    return result


# ------------------------------------------------------------ eigenvector


def _eigenvector_scores(
    node_ids: tuple[str, ...],
    links: tuple[tuple[str, str, str], ...],
    max_iterations: int,
    eps: Decimal,
) -> tuple[dict[str, Decimal], int, bool]:
    """One power-iteration run; returns ``(scores, iterations, converged)``."""
    adjacency = _undirected_adjacency(node_ids, links)
    scores = {node_id: _ZERO for node_id in node_ids}
    converged = True
    iterations_used = 0
    if node_ids and links:
        vector = {node_id: _ONE for node_id in node_ids}
        converged = False
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            for iteration in range(max_iterations):
                iterations_used = iteration + 1
                raw = {node_id: sum((vector[peer] for peer in adjacency[node_id]), _ZERO) for node_id in node_ids}
                norm = sum((value * value for value in raw.values()), _ZERO).sqrt()
                if not norm:
                    break
                new_vector = {node_id: raw[node_id] / norm for node_id in node_ids}
                diff = _ZERO
                for node_id in node_ids:
                    diff = max(diff, abs(new_vector[node_id] - vector[node_id]))
                vector = new_vector
                if diff < eps:
                    converged = True
                    break
            scores = vector
    return scores, iterations_used, converged


def compute_eigenvector(
    graph: Mapping[str, Any],
    *,
    max_iterations: int = DEFAULT_EIGENVECTOR_ITERATIONS,
    epsilon: Any = DEFAULT_EPSILON,
    top: int | None = None,
) -> dict[str, Any]:
    """Eigenvector centrality: L2-normalized power iteration, undirected.

    Starts from the uniform vector and repeatedly applies the adjacency
    sum, normalizing by the L2 norm (``Decimal.sqrt`` is correctly rounded,
    hence deterministic). On convergence the scores have unit L2 norm.
    Graphs whose uniform start has nonzero overlap with a negative-radius
    eigenvector (e.g. stars) oscillate forever; the module reports
    ``converged: false`` rather than hiding it. Runs globally and per
    primary domain (P1..P7), like ``analytics.centrality``.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    eps = _validate_epsilon(epsilon)
    _validate_top(top)
    node_ids, domains, links, dropped = _prepare_graph(graph)
    scores, iterations_used, converged = _eigenvector_scores(node_ids, links, max_iterations, eps)

    result = _base_result("analytics.algorithms.eigenvector", len(node_ids), len(links), dropped)
    result["parameters"] = {"max_iterations": max_iterations, "epsilon": str(eps), "top": top}
    result["iterations_used"] = iterations_used
    result["converged"] = converged
    result["eigenvector"] = _ranking(scores, top)
    domain_blocks: dict[str, Any] = {}
    for domain, (members, sub_links) in _domain_subgraphs(node_ids, domains, links).items():
        sub_scores, sub_iterations, sub_converged = _eigenvector_scores(members, sub_links, max_iterations, eps)
        domain_blocks[domain] = {
            "node_count": len(members),
            "iterations_used": sub_iterations,
            "converged": sub_converged,
            "eigenvector": _ranking(sub_scores, top),
        }
    result["domains"] = domain_blocks
    return result


# -------------------------------------------------------------------- Katz


def _katz_scores(
    node_ids: tuple[str, ...],
    links: tuple[tuple[str, str, str], ...],
    alpha: Decimal,
    beta: Decimal,
    max_iterations: int,
    eps: Decimal,
) -> tuple[dict[str, Decimal], int, bool]:
    """One truncated Katz expansion; returns ``(scores, iterations, converged)``."""
    adjacency = _directed_adjacency(node_ids, links)
    preds = _predecessors(node_ids, adjacency)
    scores = {node_id: _ZERO for node_id in node_ids}
    converged = True
    iterations_used = 0
    if node_ids and links and beta:
        walks = {node_id: _ONE for node_id in node_ids}
        factor = _ONE
        converged = False
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            for step in range(1, max_iterations + 1):
                iterations_used = step
                walks = {node_id: sum((walks[source] for source in preds[node_id]), _ZERO) for node_id in node_ids}
                factor *= alpha
                peak = _ZERO
                for node_id in node_ids:
                    term = beta * factor * walks[node_id]
                    scores[node_id] += term
                    peak = max(peak, term)
                if peak < eps:
                    converged = True
                    break
    return scores, iterations_used, converged


def compute_katz(
    graph: Mapping[str, Any],
    *,
    alpha: Any = DEFAULT_KATZ_ALPHA,
    beta: Any = DEFAULT_KATZ_BETA,
    max_iterations: int = DEFAULT_KATZ_ITERATIONS,
    epsilon: Any = DEFAULT_EPSILON,
    top: int | None = None,
) -> dict[str, Any]:
    """Katz prestige: ``beta * sum_k alpha^k (A^T)^k 1``, truncated.

    Walks the *incoming* direction (being pointed at is what confers
    prestige). The expansion stops when the largest per-node term falls
    below ``epsilon``; if the iteration cap fires first the result carries
    the ``katz_truncated`` guard and ``converged: false``. Choosing an
    ``alpha`` beyond the spectral radius is the caller's responsibility —
    the guard makes that visible instead of returning silent garbage. Runs
    globally and per primary domain (P1..P7), like ``analytics.centrality``.
    """
    alpha_d = _to_decimal("alpha", alpha)
    beta_d = _to_decimal("beta", beta)
    if alpha_d <= _ZERO:
        raise ValueError("alpha must be > 0")
    if beta_d < _ZERO:
        raise ValueError("beta must be >= 0")
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")
    eps = _validate_epsilon(epsilon)
    _validate_top(top)
    node_ids, domains, links, dropped = _prepare_graph(graph)
    scores, iterations_used, converged = _katz_scores(node_ids, links, alpha_d, beta_d, max_iterations, eps)

    result = _base_result("analytics.algorithms.katz", len(node_ids), len(links), dropped)
    result["parameters"] = {
        "alpha": str(alpha_d),
        "beta": str(beta_d),
        "max_iterations": max_iterations,
        "epsilon": str(eps),
        "top": top,
    }
    result["iterations_used"] = iterations_used
    result["converged"] = converged
    result["guards"] = [] if converged else ["katz_truncated"]
    result["katz"] = _ranking(scores, top)
    domain_blocks: dict[str, Any] = {}
    for domain, (members, sub_links) in _domain_subgraphs(node_ids, domains, links).items():
        sub_scores, sub_iterations, sub_converged = _katz_scores(
            members, sub_links, alpha_d, beta_d, max_iterations, eps
        )
        domain_blocks[domain] = {
            "node_count": len(members),
            "iterations_used": sub_iterations,
            "converged": sub_converged,
            "guards": [] if sub_converged else ["katz_truncated"],
            "katz": _ranking(sub_scores, top),
        }
    result["domains"] = domain_blocks
    return result


# -------------------------------------------------------------- clustering


def compute_clustering(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Per-node local clustering coefficient plus global average/transitivity.

    On the collapsed undirected view, ``C_v = 2 * T_v / (d_v * (d_v - 1))``
    where ``T_v`` counts edges among neighbours of ``v``; nodes with degree
    below 2 score zero. Transitivity is ``sum(T_v) / sum(C(d_v, 2))`` over
    the whole graph (zero when the graph has no connected triple).
    """
    node_ids, _domains, links, dropped = _prepare_graph(graph)
    adjacency = _undirected_adjacency(node_ids, links)
    neighbour_sets = {node_id: set(adjacency[node_id]) for node_id in node_ids}

    per_node: list[dict[str, Any]] = []
    sum_triangles = 0
    triples = 0
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        sum_coefficients = _ZERO
        for node_id in node_ids:
            neighbours = adjacency[node_id]
            degree = len(neighbours)
            shared = sum(len(neighbour_sets[peer] & neighbour_sets[node_id]) for peer in neighbours)
            triangles = shared // 2
            coefficient = Decimal(2 * triangles) / Decimal(degree * (degree - 1)) if degree >= 2 else _ZERO
            sum_coefficients += coefficient
            sum_triangles += triangles
            triples += degree * (degree - 1) // 2
            per_node.append(
                {
                    "node_id": node_id,
                    "degree": degree,
                    "triangles": triangles,
                    "coefficient": serialize_score(coefficient),
                }
            )
        n = len(node_ids)
        average = sum_coefficients / Decimal(n) if n else _ZERO
        transitivity = Decimal(sum_triangles) / Decimal(triples) if triples else _ZERO

    result = _base_result("analytics.algorithms.clustering", len(node_ids), len(links), dropped)
    result["per_node"] = per_node
    result["global"] = {
        "node_count": len(node_ids),
        "average_coefficient": serialize_score(average),
        "transitivity": serialize_score(transitivity),
        "triangles": sum_triangles // 3,
        "connected_triples": triples,
    }
    return result


# ------------------------------------------------------------------ Louvain


def _louvain_local_moving(
    nodes: list[str],
    adjacency: dict[str, dict[str, int]],
    self_loop: dict[str, int],
    degrees: dict[str, int],
    m2: Decimal,
    max_passes: int,
) -> tuple[dict[str, str], bool, bool]:
    """Greedy modularity local moving in canonical node order.

    Communities are keyed by their smallest member key. A node moves only on
    a strictly positive modularity gain numerator
    ``k_i_in * m2 - sigma_tot * k_i``; ties resolve to the smallest
    community key because the tally is walked in sorted order with a strict
    ``>`` comparison. Returns ``(assignment, moved_any, pass_cap_hit)``.
    """
    community = {node: node for node in nodes}
    sigma_tot = {node: degrees[node] for node in nodes}
    moved_any = False
    for _pass in range(max_passes):
        moved = False
        for node in nodes:
            if degrees[node] == 0:
                continue
            current = community[node]
            tally: dict[str, int] = {}
            for neighbour in sorted(adjacency[node]):
                if neighbour == node:
                    continue
                neighbour_community = community[neighbour]
                tally[neighbour_community] = tally.get(neighbour_community, 0) + adjacency[node][neighbour]
            sigma_tot[current] -= degrees[node]
            best = current
            best_gain = _ZERO
            for candidate in sorted(tally):
                gain = Decimal(tally[candidate]) * m2 - Decimal(sigma_tot[candidate]) * degrees[node]
                if gain > best_gain:
                    best = candidate
                    best_gain = gain
            community[node] = best
            sigma_tot[best] += degrees[node]
            if best != current:
                moved = True
        moved_any = moved_any or moved
        if not moved:
            return community, moved_any, False
    return community, moved_any, True


def _louvain_modularity(
    pair_weights: Mapping[tuple[str, str], int],
    degrees: Mapping[str, int],
    partition: list[tuple[str, ...]],
) -> Decimal:
    """Modularity of *partition* measured on the original level-0 graph."""
    total_weight = sum(pair_weights.values())
    if total_weight == 0:
        return _ZERO
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        m = Decimal(total_weight)
        m2 = Decimal(2 * total_weight)
        quality = _ZERO
        for members in partition:
            member_set = set(members)
            internal = sum(weight for (u, v), weight in pair_weights.items() if u in member_set and v in member_set)
            sigma = sum(degrees[member] for member in members)
            quality += Decimal(internal) / m - (Decimal(sigma) / m2) ** 2
    return quality


def compute_louvain(
    graph: Mapping[str, Any],
    *,
    max_passes: int = DEFAULT_LOUVAIN_MAX_PASSES,
    max_levels: int = DEFAULT_LOUVAIN_MAX_LEVELS,
    max_nodes: int = DEFAULT_LOUVAIN_MAX_NODES,
) -> dict[str, Any]:
    """Deterministic Louvain community detection.

    Alternates greedy local-moving passes (canonical node order, strictly
    positive gain, smallest-key tie-breaks) with graph aggregation where
    each community becomes one super-node keyed by its smallest member and
    internal weight folds into a self-loop. Stops when a level moves nothing
    or aggregation can no longer shrink the graph; hitting ``max_levels``
    is reported via the ``louvain_max_levels_reached`` guard, and an
    exhausted local-moving pass budget adds ``louvain_local_pass_cap``.
    Modularity is always recomputed on the original graph from the current
    partition, so reported values are exact.
    """
    if max_passes < 1:
        raise ValueError("max_passes must be >= 1")
    if max_levels < 1:
        raise ValueError("max_levels must be >= 1")
    node_ids, _domains, links, dropped = _prepare_graph(graph)
    _check_node_cap("louvain", len(node_ids), max_nodes)

    pair_weights: dict[tuple[str, str], int] = {}
    for source, _relation, target in links:
        pair = (source, target) if source < target else (target, source)
        pair_weights[pair] = 1  # collapsed view: parallel relations count once
    base_degrees = {node_id: 0 for node_id in node_ids}
    for (u, v), weight in pair_weights.items():
        base_degrees[u] += weight
        base_degrees[v] += weight

    result = _base_result("analytics.algorithms.louvain", len(node_ids), len(links), dropped)
    result["parameters"] = {"max_passes": max_passes, "max_levels": max_levels, "max_nodes": max_nodes}

    if not node_ids or not pair_weights:
        communities = [{"id": index, "size": 1, "members": [node_id]} for index, node_id in enumerate(node_ids)]
        result.update(
            {
                "community_count": len(communities),
                "communities": communities,
                "modularity": serialize_score(_ZERO),
                "levels": [],
                "levels_used": 0,
                "converged": True,
                "guards": [],
            }
        )
        return result

    current_nodes = list(node_ids)
    current_adjacency: dict[str, dict[str, int]] = {node_id: {} for node_id in node_ids}
    for (u, v), weight in pair_weights.items():
        current_adjacency[u][v] = current_adjacency[u].get(v, 0) + weight
        current_adjacency[v][u] = current_adjacency[v].get(u, 0) + weight
    current_loops: dict[str, int] = {node_id: 0 for node_id in node_ids}
    members_of: dict[str, tuple[str, ...]] = {node_id: (node_id,) for node_id in node_ids}

    partition: list[tuple[str, ...]] = [(node_id,) for node_id in node_ids]
    levels: list[dict[str, Any]] = []
    guards: list[str] = []
    converged = False
    pass_cap_hit = False
    levels_used = 0

    for level in range(max_levels):
        levels_used += 1
        degrees = {node: sum(current_adjacency[node].values()) + current_loops[node] for node in current_nodes}
        m2 = Decimal(sum(degrees.values()))
        assignment, moved_any, cap_hit = _louvain_local_moving(
            current_nodes, current_adjacency, current_loops, degrees, m2, max_passes
        )
        pass_cap_hit = pass_cap_hit or cap_hit

        groups: dict[str, list[str]] = {}
        for node in current_nodes:
            groups.setdefault(assignment[node], []).append(node)
        ordered_groups = [groups[key] for key in sorted(groups)]

        partition = [tuple(sorted(member for node in group for member in members_of[node])) for group in ordered_groups]
        levels.append(
            {
                "level": level,
                "community_count": len(ordered_groups),
                "modularity": serialize_score(_louvain_modularity(pair_weights, base_degrees, partition)),
            }
        )

        if not moved_any or len(ordered_groups) == len(current_nodes) or len(ordered_groups) == 1:
            converged = True
            break

        # Aggregation: each community becomes a super-node keyed by its
        # smallest member; internal weight folds into a self-loop that
        # carries twice the internal pair weight (the loop below adds each
        # internal pair once per endpoint), so degree sums are preserved.
        super_of = {key: group[0] for key, group in zip(sorted(groups), ordered_groups, strict=True)}
        next_nodes = sorted(group[0] for group in ordered_groups)
        next_adjacency: dict[str, dict[str, int]] = {node: {} for node in next_nodes}
        next_loops: dict[str, int] = {node: 0 for node in next_nodes}
        for node in current_nodes:
            super_node = super_of[assignment[node]]
            next_loops[super_node] += current_loops[node]
            for neighbour, weight in current_adjacency[node].items():
                neighbour_super = super_of[assignment[neighbour]]
                if neighbour_super == super_node:
                    next_loops[super_node] += weight
                elif node < neighbour:
                    next_adjacency[super_node][neighbour_super] = (
                        next_adjacency[super_node].get(neighbour_super, 0) + weight
                    )
                    next_adjacency[neighbour_super][super_node] = (
                        next_adjacency[neighbour_super].get(super_node, 0) + weight
                    )
        next_members: dict[str, tuple[str, ...]] = {}
        for group in ordered_groups:
            next_members[group[0]] = tuple(sorted(member for node in group for member in members_of[node]))
        current_nodes = next_nodes
        current_adjacency = {node: next_adjacency[node] for node in current_nodes}
        current_loops = {node: next_loops[node] for node in current_nodes}
        members_of = next_members

    if not converged:
        guards.append("louvain_max_levels_reached")
    if pass_cap_hit:
        guards.append("louvain_local_pass_cap")

    ordered_partition = sorted(partition, key=lambda members: (-len(members), members[0]))
    communities = [
        {"id": index, "size": len(members), "members": list(members)} for index, members in enumerate(ordered_partition)
    ]
    result.update(
        {
            "community_count": len(communities),
            "communities": communities,
            "modularity": serialize_score(_louvain_modularity(pair_weights, base_degrees, partition)),
            "levels": levels,
            "levels_used": levels_used,
            "converged": converged,
            "guards": guards,
        }
    )
    return result


# ------------------------------------------------------------ reachability


def compute_reachability(
    graph: Mapping[str, Any],
    *,
    max_nodes: int = DEFAULT_REACHABILITY_MAX_NODES,
) -> dict[str, Any]:
    """All-pairs forward reachability summary (BFS per node, directed).

    Per node: reachable set size, farthest hop count, and the ratio to
    ``n - 1``. The summary accounts every ordered pair exactly once
    (``pairs + unreachable_pairs == n * (n - 1)``), reports the density and
    the finite diameter. All-pairs work is quadratic in the worst case, so
    inputs beyond ``max_nodes`` fail closed with :class:`GraphCapExceededError`.
    """
    node_ids, _domains, links, dropped = _prepare_graph(graph)
    _check_node_cap("reachability", len(node_ids), max_nodes)
    adjacency = _directed_adjacency(node_ids, links)

    n = len(node_ids)
    per_node: list[dict[str, Any]] = []
    pairs = 0
    diameter = 0
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        norm = Decimal(n - 1) if n > 1 else _ZERO
        for node_id in node_ids:
            distances = _bfs_distances(node_id, adjacency)
            reachable = len(distances) - 1
            farthest = max(distances.values(), default=0)
            pairs += reachable
            diameter = max(diameter, farthest)
            ratio = Decimal(reachable) / norm if norm else _ZERO
            per_node.append(
                {
                    "node_id": node_id,
                    "reachable": reachable,
                    "farthest": farthest,
                    "ratio": serialize_score(ratio),
                }
            )
        density = Decimal(pairs) / Decimal(n * (n - 1)) if n > 1 else _ZERO
    per_node.sort(key=lambda entry: (-entry["reachable"], entry["node_id"]))

    result = _base_result("analytics.algorithms.reachability", n, len(links), dropped)
    result["parameters"] = {"max_nodes": max_nodes}
    result["per_node"] = per_node
    result["summary"] = {
        "pairs": pairs,
        "density": serialize_score(density),
        "diameter": diameter,
        "unreachable_pairs": n * (n - 1) - pairs,
    }
    return result


# ------------------------------------------------------------------ motifs


def _triad_class(edges: set[tuple[str, str]], x: str, y: str, z: str) -> str:
    """Classify the triad on nodes ``x < y < z`` into its MAN class label.

    ``m``/``a``/``n`` count mutual, asymmetric and null dyads; the D/U/C
    suffixes follow the classical convention: D = arcs descend from a common
    source, U = arcs converge upward to a common target, C = cyclic or
    transitive-chain arrangement. In/out degrees below count only the
    asymmetric arcs of the triad.
    """
    out_degree = {x: 0, y: 0, z: 0}
    in_degree = {x: 0, y: 0, z: 0}
    mutual = 0
    asymmetric = 0
    mutual_members: set[str] = set()
    for u, v in ((x, y), (x, z), (y, z)):
        forward = (u, v) in edges
        backward = (v, u) in edges
        if forward and backward:
            mutual += 1
            mutual_members.update((u, v))
        elif forward or backward:
            asymmetric += 1
            source, target = (u, v) if forward else (v, u)
            out_degree[source] += 1
            in_degree[target] += 1
    key = (mutual, asymmetric)
    if key == (0, 0):
        return "003"
    if key == (0, 1):
        return "012"
    if key == (1, 0):
        return "102"
    if key == (0, 2):
        if any(value == 2 for value in out_degree.values()):
            return "021D"
        if any(value == 2 for value in in_degree.values()):
            return "021U"
        return "021C"
    if key == (1, 1):
        odd = next(node for node in (x, y, z) if node not in mutual_members)
        return "111D" if out_degree[odd] else "111U"
    if key == (0, 3):
        return "030C" if all(value == 1 for value in out_degree.values()) else "030T"
    if key == (2, 0):
        return "201"
    if key == (1, 2):
        odd = next(node for node in (x, y, z) if node not in mutual_members)
        if in_degree[odd] == 2:
            return "120U"
        if out_degree[odd] == 2:
            return "120D"
        return "120C"
    if key == (2, 1):
        return "210"
    return "300"


def count_motifs(
    graph: Mapping[str, Any],
    *,
    max_nodes: int = DEFAULT_MOTIF_MAX_NODES,
) -> dict[str, Any]:
    """Triangle counting plus the full 16-class directed triad census.

    Triangles are counted on the collapsed undirected view (per node and in
    total; ``sum(per_node) == 3 * total`` always). The census classifies
    every one of the ``C(n, 3)`` node triples by its directed arc pattern
    into the MAN labels; the census keys always sum to ``triad_total``.
    Enumeration is cubic, so inputs beyond ``max_nodes`` fail closed.
    """
    node_ids, _domains, links, dropped = _prepare_graph(graph)
    _check_node_cap("motifs", len(node_ids), max_nodes)
    directed = _directed_adjacency(node_ids, links)
    edges = {(source, target) for source in node_ids for target in directed[source]}
    undirected = _undirected_adjacency(node_ids, links)
    neighbour_sets = {node_id: set(undirected[node_id]) for node_id in node_ids}

    per_node_triangles: dict[str, int] = {}
    for node_id in node_ids:
        neighbours = undirected[node_id]
        shared = sum(len(neighbour_sets[peer] & neighbour_sets[node_id]) for peer in neighbours)
        per_node_triangles[node_id] = shared // 2
    triangle_total = sum(per_node_triangles.values()) // 3

    census = {name: 0 for name in TRIAD_CLASSES}
    n = len(node_ids)
    for i in range(n):
        for j in range(i + 1, n):
            for k in range(j + 1, n):
                census[_triad_class(edges, node_ids[i], node_ids[j], node_ids[k])] += 1

    ranked = sorted(per_node_triangles.items(), key=lambda item: (-item[1], item[0]))
    result = _base_result("analytics.algorithms.motifs", n, len(links), dropped)
    result["parameters"] = {"max_nodes": max_nodes}
    result["triangles"] = {
        "total": triangle_total,
        "per_node": [{"node_id": node_id, "triangles": count} for node_id, count in ranked],
    }
    result["triad_census"] = census
    result["triad_total"] = n * (n - 1) * (n - 2) // 6
    return result


# ------------------------------------------------------------ assortativity


def compute_assortativity(graph: Mapping[str, Any]) -> dict[str, Any]:
    """Degree assortativity: Pearson correlation of endpoint degrees.

    Computed over the unique undirected pairs with each edge counted in both
    orientations (the classical symmetric form). When the variance of either
    endpoint-degree distribution is zero — regular graphs, or no edges —
    the coefficient is undefined: the result reports ``defined: false``, a
    zero score, and the ``assortativity_undefined`` guard.
    """
    node_ids, _domains, links, dropped = _prepare_graph(graph)
    adjacency = _undirected_adjacency(node_ids, links)
    degrees = {node_id: len(adjacency[node_id]) for node_id in node_ids}
    pairs: set[tuple[str, str]] = set()
    for source, _relation, target in links:
        pairs.add((source, target) if source < target else (target, source))

    guards: list[str] = []
    score = _ZERO
    defined = False
    if pairs:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            m = Decimal(2 * len(pairs))
            sum_x = _ZERO
            sum_y = _ZERO
            sum_xy = _ZERO
            sum_xx = _ZERO
            sum_yy = _ZERO
            for u, v in sorted(pairs):
                for x_value, y_value in ((degrees[u], degrees[v]), (degrees[v], degrees[u])):
                    x = Decimal(x_value)
                    y = Decimal(y_value)
                    sum_x += x
                    sum_y += y
                    sum_xy += x * y
                    sum_xx += x * x
                    sum_yy += y * y
            numerator = m * sum_xy - sum_x * sum_y
            var_x = m * sum_xx - sum_x * sum_x
            var_y = m * sum_yy - sum_y * sum_y
            if var_x > _ZERO and var_y > _ZERO:
                value = numerator / (var_x * var_y).sqrt()
                score = max(Decimal(-1), min(_ONE, value))
                defined = True
            else:
                guards.append("assortativity_undefined")
    else:
        guards.append("assortativity_undefined")

    result = _base_result("analytics.algorithms.assortativity", len(node_ids), len(links), dropped)
    result["score"] = serialize_score(score)
    result["defined"] = defined
    result["edge_pair_count"] = len(pairs)
    result["guards"] = guards
    return result

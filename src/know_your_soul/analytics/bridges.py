"""Surprising-connection detection for the knowledge graph (ATM F-A5).

A *bridge* is a link whose endpoints sit in different primary domains and/or
different communities — a candidate "surprising connection" between areas of
knowledge that the structure says are otherwise separate.

The surprise score is our own deterministic composition of three components,
each rendered as a 12-decimal fixed-point string:

* **community distance** — BFS hop count between the endpoints' communities
  in the community-adjacency graph with the candidate edge itself removed
  (an edge that is the *only* connection between two communities is the most
  surprising). ``-1`` (unreachable) contributes the maximum component ``1``;
  otherwise the component is ``d / (d + 1)``.
* **relation rarity** — ``(E - count(relation)) / E`` over the whole link
  set (self-loops included in ``E``): rare relation types are more
  surprising than the everyday ``supports``.
* **evidence novelty** — ``1 / min(version(source), version(target))``: a
  bridge touching a freshly-versioned node is newer evidence than one
  between twice-revised veterans.

The composite is ``(3*distance + 2*rarity + 1*novelty) / 6``. Ordering is
``(-surprise, source, target, relation)`` — fully canonical, independent of
input ordering and ``PYTHONHASHSEED``.

When no ``membership`` mapping is given, the community dimension falls back
to the primary domain (so only cross-domain bridges are found and no
cross-community kind is reported).

This module is pure: no store, no clock, no ambient state.

origin: idea from Graphify ``analyze.py`` (cross-community/cross-source
"surprising connections" as a feature class); score formula and
implementation original.
"""

from __future__ import annotations

from collections import deque
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from know_your_soul.domain import quantize_score

__all__ = ["DEFAULT_TOP_N", "SCHEMA_VERSION", "find_bridges"]

SCHEMA_VERSION = 1
DEFAULT_TOP_N = 25

_DISTANCE_WEIGHT = Decimal(3)
_RARITY_WEIGHT = Decimal(2)
_NOVELTY_WEIGHT = Decimal(1)
_TOTAL_WEIGHT = _DISTANCE_WEIGHT + _RARITY_WEIGHT + _NOVELTY_WEIGHT


def _community_distance(
    adjacency: Mapping[Any, list[Any]], start: Any, goal: Any, forbidden_edge: tuple[Any, Any]
) -> int:
    """BFS hops from ``start`` to ``goal`` with the candidate's own edge instance removed.

    The adjacency lists carry one entry per inter-community link (multiset),
    so removing the candidate only removes ONE connection between the two
    communities — parallel links between the same pair still count. Returns
    ``-1`` when the communities are unreachable without the candidate.
    """
    if start == goal:
        return 0
    visited = {start}
    queue: deque[tuple[Any, int]] = deque([(start, 0)])
    forbidden_used = False
    while queue:
        current, depth = queue.popleft()
        for neighbour in adjacency.get(current, []):
            if not forbidden_used and {current, neighbour} == {forbidden_edge[0], forbidden_edge[1]}:
                forbidden_used = True
                continue
            if neighbour == goal:
                return depth + 1
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, depth + 1))
    return -1


def find_bridges(
    nodes: Iterable[Mapping[str, Any]],
    links: Iterable[Mapping[str, Any]],
    *,
    membership: Mapping[str, Any] | None = None,
    top_n: int = DEFAULT_TOP_N,
) -> dict[str, Any]:
    """Rank cross-domain/cross-community links by deterministic surprise.

    ``nodes`` follow the ``graph.json`` node shape (``id``, ``domain``,
    optional ``version``); ``links`` follow the link shape (``source``,
    ``target``, ``relation``, optional ``confidence``). ``membership`` maps
    node id -> community id (e.g. from ``analytics.communities``); without it
    the primary domain stands in for the community.
    """
    if top_n < 0:
        raise ValueError("top_n must be >= 0")

    node_list = list(nodes)
    link_list = list(links)
    node_ids = {str(node["id"]) for node in node_list}
    domains = {str(node["id"]): str(node.get("domain", "")) for node in node_list}
    versions = {str(node["id"]): int(node.get("version", 1)) for node in node_list}

    def community_of(node_id: str) -> Any:
        if membership is not None:
            return membership.get(node_id, ("__unmapped__", node_id))
        return domains.get(node_id, "")

    total_links = len(link_list)
    relation_counts: dict[str, int] = {}
    for link in link_list:
        relation = str(link["relation"])
        relation_counts[relation] = relation_counts.get(relation, 0) + 1

    # Community adjacency over the whole link set (self-loops excluded); one
    # entry per link so parallel connections between the same community pair
    # are distinguishable when the candidate edge is removed.
    community_adjacency: dict[Any, list[Any]] = {}
    for link in link_list:
        source = str(link["source"])
        target = str(link["target"])
        if source == target or source not in node_ids or target not in node_ids:
            continue
        source_community = community_of(source)
        target_community = community_of(target)
        if source_community == target_community:
            continue
        community_adjacency.setdefault(source_community, []).append(target_community)
        community_adjacency.setdefault(target_community, []).append(source_community)
    sorted_adjacency = {community: sorted(neighbours, key=str) for community, neighbours in community_adjacency.items()}

    bridges: list[dict[str, Any]] = []
    by_kind: dict[str, int] = {}
    for link in link_list:
        source = str(link["source"])
        target = str(link["target"])
        if source == target or source not in node_ids or target not in node_ids:
            continue
        cross_domain = domains[source] != domains[target]
        source_community = community_of(source)
        target_community = community_of(target)
        cross_community = membership is not None and source_community != target_community
        if not cross_domain and not cross_community:
            continue

        if cross_domain and cross_community:
            kind = "cross_domain_and_community"
        elif cross_domain:
            kind = "cross_domain"
        else:
            kind = "cross_community"

        distance = _community_distance(
            sorted_adjacency, source_community, target_community, (source_community, target_community)
        )
        if distance < 0:
            distance_component = Decimal(1)
        else:
            distance_component = Decimal(distance) / Decimal(distance + 1)

        relation = str(link["relation"])
        rarity = Decimal(total_links - relation_counts[relation]) / Decimal(total_links) if total_links else Decimal(0)
        novelty = Decimal(1) / Decimal(max(1, min(versions[source], versions[target])))

        surprise = (
            _DISTANCE_WEIGHT * distance_component + _RARITY_WEIGHT * rarity + _NOVELTY_WEIGHT * novelty
        ) / _TOTAL_WEIGHT

        bridges.append(
            {
                "source": source,
                "target": target,
                "relation": relation,
                "confidence": str(link.get("confidence", "EXTRACTED")),
                "kind": kind,
                "surprise_score": str(quantize_score(surprise)),
                "components": {
                    "community_distance": distance,
                    "distance_component": str(quantize_score(distance_component)),
                    "relation_rarity": str(quantize_score(rarity)),
                    "evidence_novelty": str(quantize_score(novelty)),
                },
            }
        )
        by_kind[kind] = by_kind.get(kind, 0) + 1

    bridges.sort(
        key=lambda bridge: (
            -Decimal(bridge["surprise_score"]),
            bridge["source"],
            bridge["target"],
            bridge["relation"],
        )
    )
    emitted = bridges[:top_n]

    return {
        "schema_version": SCHEMA_VERSION,
        "top_n": top_n,
        "bridges": emitted,
        "counts": {
            "candidates": len(bridges),
            "emitted": len(emitted),
            "by_kind": dict(sorted(by_kind.items())),
        },
    }

"""Deterministic community detection for the knowledge graph (ATM F-A3).

Pure-Python label propagation over the ``graph.json`` read model:

* **Canonical order everywhere.** Nodes are processed in sorted-id order,
  neighbour tallies are walked in sorted-label order, and ties break toward
  the lexicographically smallest label. The algorithm iterates only sorted
  lists — never sets — so the result is independent of ``PYTHONHASHSEED``
  and of the caller's input ordering.
* **Iteration cap.** Propagation runs at most ``max_iterations`` rounds;
  whether the cap was hit is reported honestly (``converged``) instead of
  being hidden.
* **Stable community ids.** Communities are ordered by ``(-size, smallest
  member id)`` and numbered ``0..n-1``; id 0 is always the largest community.
  Ids are internal — publication happens through the membership signature.
* **Deterministic labels.** Each community is labelled by its highest-degree
  member (undirected degree over the whole graph, self-loops discarded),
  breaking ties by smallest node id. The label text is the hub node's
  ``label`` field, falling back to its id.
* **Membership signature.** ``sha256`` of the newline-joined sorted member
  ids, truncated to 16 hex characters — a stable fingerprint for diffs.
* **Cohesion.** ``2 * internal_pairs / (n * (n - 1))`` over unique undirected
  pairs with self-loops discarded, rendered as a 12-decimal fixed-point
  string. Singletons are vacuously cohesive (``1.000000000000``).
* **Cross-generation remap.** Greedy one-to-one matching by member overlap
  (largest overlap first, ties by id pair), plus ``label_still_applies``:
  a label is reusable only when the shared members form a strict majority of
  BOTH the previous and the current membership (or the signatures match
  exactly).

This module is a pure function of its inputs: it never reads the store,
never writes artifacts, and never consults the clock or ambient state.

origin: idea from Graphify ``cluster.py`` (label propagation contract,
membership signatures, cohesion, remap semantics); implementation original.
"""

from __future__ import annotations

import hashlib
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from know_your_soul.domain import quantize_score

__all__ = [
    "DEFAULT_MAX_ITERATIONS",
    "SCHEMA_VERSION",
    "detect_communities",
    "label_still_applies",
    "membership_signature",
    "remap_communities",
]

SCHEMA_VERSION = 1
DEFAULT_MAX_ITERATIONS = 64

_ZERO = Decimal(0)
_ONE = Decimal(1)


def membership_signature(members: Iterable[str]) -> str:
    """16-hex-char fingerprint of a member set; independent of input order."""
    ordered = "\n".join(sorted(str(member) for member in members))
    return hashlib.sha256(ordered.encode("utf-8")).hexdigest()[:16]


def label_still_applies(previous_members: Iterable[str], current_members: Iterable[str]) -> bool:
    """Whether a previous community's label may be reused for a successor.

    True when the memberships are identical, or when the overlap is a strict
    majority of BOTH sides (each side keeps more than half of itself).
    """
    previous = set(previous_members)
    current = set(current_members)
    if not previous or not current:
        return False
    if previous == current:
        return True
    overlap = len(previous & current)
    return overlap * 2 > len(previous) and overlap * 2 > len(current)


def _undirected_degrees(node_ids: list[str], links: Iterable[Mapping[str, Any]]) -> dict[str, int]:
    degrees = {node_id: 0 for node_id in node_ids}
    known = set(node_ids)
    seen_pairs: set[tuple[str, str]] = set()
    for link in links:
        source = str(link["source"])
        target = str(link["target"])
        if source == target or source not in known or target not in known:
            continue
        pair = (source, target) if source < target else (target, source)
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        degrees[source] += 1
        degrees[target] += 1
    return degrees


def _weighted_adjacency(node_ids: list[str], links: Iterable[Mapping[str, Any]]) -> dict[str, dict[str, float]]:
    adjacency: dict[str, dict[str, float]] = {node_id: {} for node_id in node_ids}
    known = set(node_ids)
    for link in links:
        source = str(link["source"])
        target = str(link["target"])
        if source == target or source not in known or target not in known:
            continue
        weight = float(link.get("weight", 1.0))
        adjacency[source][target] = adjacency[source].get(target, 0.0) + weight
        adjacency[target][source] = adjacency[target].get(source, 0.0) + weight
    return adjacency


def _propagate(
    node_ids: list[str], adjacency: dict[str, dict[str, float]], max_iterations: int
) -> tuple[dict[str, str], int, bool]:
    """Canonical-order asynchronous label propagation.

    Each round walks nodes in sorted-id order and immediately applies the
    majority neighbour label (weight-sum; ties break to the smallest label).
    Stops when a full round changes nothing or the iteration cap is reached.
    """
    labels = {node_id: node_id for node_id in node_ids}
    iterations_used = 0
    converged = False
    for _round in range(max_iterations):
        iterations_used += 1
        changed = False
        for node_id in node_ids:  # already sorted by the caller
            tally: dict[str, float] = {}
            for neighbour_id in sorted(adjacency[node_id]):
                neighbour_label = labels[neighbour_id]
                tally[neighbour_label] = tally.get(neighbour_label, 0.0) + adjacency[node_id][neighbour_id]
            if not tally:
                continue
            best_label = min(tally)  # tie-break: smallest label
            best_weight = tally[best_label]
            for label in sorted(tally):
                if tally[label] > best_weight:
                    best_label = label
                    best_weight = tally[label]
            if best_label != labels[node_id]:
                labels[node_id] = best_label
                changed = True
        if not changed:
            converged = True
            break
    return labels, iterations_used, converged


def _cohesion(members: list[str], links: Iterable[Mapping[str, Any]]) -> str:
    size = len(members)
    if size < 2:
        return str(quantize_score(_ONE))
    member_set = set(members)
    internal_pairs: set[tuple[str, str]] = set()
    for link in links:
        source = str(link["source"])
        target = str(link["target"])
        if source == target or source not in member_set or target not in member_set:
            continue
        internal_pairs.add((source, target) if source < target else (target, source))
    numerator = Decimal(2 * len(internal_pairs))
    denominator = Decimal(size * (size - 1))
    return str(quantize_score(numerator / denominator))


def detect_communities(
    nodes: Iterable[Mapping[str, Any]],
    links: Iterable[Mapping[str, Any]],
    *,
    max_iterations: int = DEFAULT_MAX_ITERATIONS,
) -> dict[str, Any]:
    """Partition the graph into communities; output is canonical and artifact-ready.

    ``nodes``/``links`` follow the ``graph.json`` shapes (node ``id``,
    optional ``label``; link ``source``/``target``/optional ``weight``).
    The result sorts identically regardless of input ordering.
    """
    if max_iterations < 1:
        raise ValueError("max_iterations must be >= 1")

    node_list = list(nodes)
    link_list = list(links)
    node_ids = sorted(str(node["id"]) for node in node_list)
    labels_by_id = {str(node["id"]): node.get("label") for node in node_list}

    adjacency = _weighted_adjacency(node_ids, link_list)
    labels, iterations_used, converged = _propagate(node_ids, adjacency, max_iterations)
    degrees = _undirected_degrees(node_ids, link_list)

    groups: dict[str, list[str]] = {}
    for node_id in node_ids:
        groups.setdefault(labels[node_id], []).append(node_id)

    ordered_groups = sorted(groups.values(), key=lambda members: (-len(members), members[0]))

    communities: list[dict[str, Any]] = []
    for community_id, members in enumerate(ordered_groups):
        hub = min(members, key=lambda member: (-degrees[member], member))
        raw_label = labels_by_id.get(hub)
        label = raw_label if isinstance(raw_label, str) and raw_label else hub
        communities.append(
            {
                "id": community_id,
                "size": len(members),
                "label": label,
                "label_node_id": hub,
                "membership_sig": membership_signature(members),
                "cohesion": _cohesion(members, link_list),
                "members": list(members),
            }
        )

    return {
        "schema_version": SCHEMA_VERSION,
        "algorithm": "canonical-label-propagation",
        "max_iterations": max_iterations,
        "iterations_used": iterations_used,
        "converged": converged,
        "community_count": len(communities),
        "communities": communities,
    }


def remap_communities(
    previous: Iterable[Mapping[str, Any]],
    current: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Greedily map previous community ids onto current ones by member overlap.

    Pairs are considered in ``(-overlap, previous_id, current_id)`` order and
    matched one-to-one. Every mapped pair reports whether the previous label
    still applies (exact membership or strict two-way majority overlap).
    """
    previous_list = sorted(previous, key=lambda community: int(community["id"]))
    current_list = sorted(current, key=lambda community: int(community["id"]))
    previous_members = {int(c["id"]): sorted(str(m) for m in c["members"]) for c in previous_list}
    current_members = {int(c["id"]): sorted(str(m) for m in c["members"]) for c in current_list}

    pairs: list[tuple[int, int, int]] = []
    for previous_id in sorted(previous_members):
        previous_set = set(previous_members[previous_id])
        for current_id in sorted(current_members):
            overlap = len(previous_set & set(current_members[current_id]))
            if overlap > 0:
                pairs.append((-overlap, previous_id, current_id))
    pairs.sort()

    matched_previous: set[int] = set()
    matched_current: set[int] = set()
    mapping: list[dict[str, Any]] = []
    for negative_overlap, previous_id, current_id in pairs:
        if previous_id in matched_previous or current_id in matched_current:
            continue
        matched_previous.add(previous_id)
        matched_current.add(current_id)
        prev_members = previous_members[previous_id]
        curr_members = current_members[current_id]
        mapping.append(
            {
                "previous_id": previous_id,
                "current_id": current_id,
                "overlap": -negative_overlap,
                "previous_size": len(prev_members),
                "current_size": len(curr_members),
                "label_still_applies": label_still_applies(prev_members, curr_members),
                "previous_sig": membership_signature(prev_members),
                "current_sig": membership_signature(curr_members),
            }
        )
    mapping.sort(key=lambda entry: (entry["previous_id"], entry["current_id"]))

    return {
        "schema_version": SCHEMA_VERSION,
        "mapping": mapping,
        "unmatched_previous": sorted(set(previous_members) - matched_previous),
        "unmatched_current": sorted(set(current_members) - matched_current),
    }

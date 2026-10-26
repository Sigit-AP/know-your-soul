"""Cross-generation diff over two published read models (ATM F-C1).

A pure function over two parsed ``graph.json`` generations. The diff answers
"what changed since the previous generation" for audit and trust:

* **Nodes** — added / removed by id; changed nodes report label moves,
  ``node_version`` transitions, and per-dimension P/M/E deltas in 12-decimal
  fixed-point (``delta = current - previous``, signed).
* **Edges** — keyed by the canonical triple ``(source, target, relation)``.
  An edge that disappears while its exact mirror (same relation, swapped
  endpoints) appears is reported once under ``edges_reversed`` and is **not**
  double counted as an add plus a remove. Reversal matching is one-to-one in
  canonical order. Every key in the symmetric difference is accounted exactly
  once: ``|removed| + |reversed| == |previous-only|`` and likewise for adds.
* **Communities** — when the caller supplies community mappings they are used
  verbatim; otherwise communities are derived from the nodes'
  ``community_sig`` fields. Cross-generation pairing delegates to
  :func:`know_your_soul.analytics.communities.remap_communities` (greedy
  member-overlap matching). Per-node moves are listed separately.
* **Determinism** — the output sorts identically regardless of input
  ordering; no clock, no randomness, no I/O.

``render_diff_markdown`` renders the artifact-facing ``DIFF.md`` and fails
closed on a foreign schema or an absolute-path shape in the rendered text.

origin: idea from Graphify ``analyze.py`` (graph diff, reversed-edge
handling); implementation original — canonical keys, Decimal score deltas,
node versions, and community remap are know-your-soul concepts.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from know_your_soul.analytics.communities import remap_communities
from know_your_soul.domain import canonical_json, quantize_score

__all__ = [
    "DIFF_SCHEMA_VERSION",
    "GenerationDiffError",
    "diff_generations",
    "render_diff_markdown",
]

DIFF_SCHEMA_VERSION = 1

_DIMENSIONS = ("p", "m", "e")

# Same absolute-path shape as the report guard: a diff artifact must never
# leak a host path through a label or an identifier.
_ABS_PATH_RE = re.compile(r"(?<![\w@:+/.-])/(?:[\w@.+-]+/){2,}[\w@.+-]+")


class GenerationDiffError(RuntimeError):
    """The diff dict or its rendering violates the artifact contract."""


# --------------------------------------------------------------- extraction


def _node_map(graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    """Index ``graph['nodes']`` by id; non-mapping entries are ignored."""
    raw = graph.get("nodes")
    result: dict[str, Mapping[str, Any]] = {}
    if isinstance(raw, list):
        for entry in raw:
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"]:
                result[str(entry["id"])] = entry
    return result


def _edge_keys(graph: Mapping[str, Any]) -> set[tuple[str, str, str]]:
    raw = graph.get("links")
    keys: set[tuple[str, str, str]] = set()
    if isinstance(raw, list):
        for entry in raw:
            if not isinstance(entry, Mapping):
                continue
            source, target = entry.get("source"), entry.get("target")
            relation = entry.get("relation", "supports")
            if isinstance(source, str) and isinstance(target, str) and isinstance(relation, str):
                keys.add((source, target, relation))
    return keys


def _edge_dict(key: tuple[str, str, str]) -> dict[str, str]:
    return {"source": key[0], "target": key[1], "relation": key[2]}


def _score(node: Mapping[str, Any], dimension: str) -> Decimal:
    scores = node.get("scores")
    raw: Any = scores.get(dimension) if isinstance(scores, Mapping) else None
    if raw is None:
        return Decimal(0)
    return Decimal(str(raw))


def _sig(node: Mapping[str, Any]) -> str | None:
    value = node.get("community_sig")
    return value if isinstance(value, str) and value else None


# -------------------------------------------------------------------- nodes


def _diff_node(node_id: str, previous: Mapping[str, Any], current: Mapping[str, Any]) -> dict[str, Any] | None:
    changes: list[str] = []
    entry: dict[str, Any] = {"node_id": node_id}

    version_from = previous.get("version")
    version_to = current.get("version")
    entry["version_from"] = version_from
    entry["version_to"] = version_to
    if version_from != version_to:
        changes.append("version")

    label_from = previous.get("label")
    label_to = current.get("label")
    if label_from != label_to:
        entry["label_from"] = label_from
        entry["label_to"] = label_to
        changes.append("label")

    status_from = previous.get("lifecycle_status")
    status_to = current.get("lifecycle_status")
    if status_from != status_to:
        entry["lifecycle_from"] = status_from
        entry["lifecycle_to"] = status_to
        changes.append("lifecycle_status")

    score_changes: dict[str, dict[str, str]] = {}
    for dimension in _DIMENSIONS:
        before = _score(previous, dimension)
        after = _score(current, dimension)
        if before != after:
            score_changes[dimension] = {
                "from": format(quantize_score(before), "f"),
                "to": format(quantize_score(after), "f"),
                "delta": format(quantize_score(after - before), "f"),
            }
    if score_changes:
        entry["scores"] = score_changes
        changes.append("scores")

    if not changes:
        return None
    entry["changes"] = sorted(changes)
    return entry


# -------------------------------------------------------------------- edges


def _diff_edges(
    previous_keys: set[tuple[str, str, str]], current_keys: set[tuple[str, str, str]]
) -> tuple[list[dict[str, str]], list[dict[str, str]], list[dict[str, Any]]]:
    added = set(current_keys) - set(previous_keys)
    removed = set(previous_keys) - set(current_keys)

    # Pair each removed key with its exact mirror in the added set, one-to-one,
    # in canonical order — a reversal is reported once and leaves both the
    # added and the removed sets.
    reversed_edges: list[dict[str, Any]] = []
    for key in sorted(removed):
        mirror = (key[1], key[0], key[2])
        if mirror in added:
            added.discard(mirror)
            removed.discard(key)
            reversed_edges.append({"from": _edge_dict(key), "to": _edge_dict(mirror)})

    return (
        [_edge_dict(key) for key in sorted(added)],
        [_edge_dict(key) for key in sorted(removed)],
        reversed_edges,
    )


# --------------------------------------------------------------- communities


def _derived_communities(nodes: Mapping[str, Mapping[str, Any]]) -> dict[str, list[str]]:
    """Group node ids by ``community_sig``; members sorted, sigs sorted."""
    groups: dict[str, list[str]] = {}
    for node_id in sorted(nodes):
        sig = _sig(nodes[node_id])
        if sig is not None:
            groups.setdefault(sig, []).append(node_id)
    return {sig: groups[sig] for sig in sorted(groups)}


def _diff_communities(
    previous_nodes: Mapping[str, Mapping[str, Any]],
    current_nodes: Mapping[str, Mapping[str, Any]],
    *,
    previous_communities: Iterable[Mapping[str, Any]] | None,
    current_communities: Iterable[Mapping[str, Any]] | None,
) -> dict[str, Any]:
    if previous_communities is not None and current_communities is not None:
        source = "provided"
        remap = remap_communities(previous_communities, current_communities)
        mapping = [
            {
                "previous_id": entry["previous_id"],
                "current_id": entry["current_id"],
                "overlap": entry["overlap"],
                "label_still_applies": entry["label_still_applies"],
            }
            for entry in remap["mapping"]
        ]
        return {
            "source": source,
            "mapping": mapping,
            "unmatched_previous": list(remap["unmatched_previous"]),
            "unmatched_current": list(remap["unmatched_current"]),
        }

    previous_groups = _derived_communities(previous_nodes)
    current_groups = _derived_communities(current_nodes)
    if not previous_groups and not current_groups:
        return {"source": "none", "mapping": [], "unmatched_previous": [], "unmatched_current": []}

    # Sigs stand in for community identity; remap needs integer ids, so number
    # the sorted sigs and translate back afterwards.
    previous_sigs = sorted(previous_groups)
    current_sigs = sorted(current_groups)
    previous_input = [{"id": index, "members": previous_groups[sig]} for index, sig in enumerate(previous_sigs)]
    current_input = [{"id": index, "members": current_groups[sig]} for index, sig in enumerate(current_sigs)]
    remap = remap_communities(previous_input, current_input)
    mapping = [
        {
            "previous_sig": previous_sigs[entry["previous_id"]],
            "current_sig": current_sigs[entry["current_id"]],
            "overlap": entry["overlap"],
            "previous_size": entry["previous_size"],
            "current_size": entry["current_size"],
            "label_still_applies": entry["label_still_applies"],
        }
        for entry in remap["mapping"]
    ]
    mapping.sort(key=lambda entry: (entry["previous_sig"], entry["current_sig"]))
    return {
        "source": "derived",
        "mapping": mapping,
        "unmatched_previous": [previous_sigs[index] for index in remap["unmatched_previous"]],
        "unmatched_current": [current_sigs[index] for index in remap["unmatched_current"]],
    }


def _community_moves(
    previous_nodes: Mapping[str, Mapping[str, Any]], current_nodes: Mapping[str, Mapping[str, Any]]
) -> list[dict[str, Any]]:
    moves: list[dict[str, Any]] = []
    for node_id in sorted(set(previous_nodes) & set(current_nodes)):
        before = _sig(previous_nodes[node_id])
        after = _sig(current_nodes[node_id])
        if before != after:
            moves.append({"node_id": node_id, "from": before, "to": after})
    return moves


# -------------------------------------------------------------------- driver


def diff_generations(
    previous: Mapping[str, Any],
    current: Mapping[str, Any],
    *,
    previous_communities: Iterable[Mapping[str, Any]] | None = None,
    current_communities: Iterable[Mapping[str, Any]] | None = None,
) -> dict[str, Any]:
    """Diff two parsed ``graph.json`` generations into a canonical dict.

    Community inputs, when given, follow the
    :func:`~know_your_soul.analytics.communities.remap_communities` shape
    (``{"id": int, "members": [...]}``); both sides must be supplied together.
    """
    if (previous_communities is None) != (current_communities is None):
        raise GenerationDiffError("previous_communities and current_communities must be supplied together")

    previous_nodes = _node_map(previous)
    current_nodes = _node_map(current)
    previous_edges = _edge_keys(previous)
    current_edges = _edge_keys(current)

    nodes_added = sorted(set(current_nodes) - set(previous_nodes))
    nodes_removed = sorted(set(previous_nodes) - set(current_nodes))
    nodes_changed = [
        entry
        for node_id in sorted(set(previous_nodes) & set(current_nodes))
        if (entry := _diff_node(node_id, previous_nodes[node_id], current_nodes[node_id])) is not None
    ]

    edges_added, edges_removed, edges_reversed = _diff_edges(previous_edges, current_edges)
    community_moves = _community_moves(previous_nodes, current_nodes)
    communities = _diff_communities(
        previous_nodes,
        current_nodes,
        previous_communities=previous_communities,
        current_communities=current_communities,
    )

    summary = {
        "nodes_before": len(previous_nodes),
        "nodes_after": len(current_nodes),
        "edges_before": len(previous_edges),
        "edges_after": len(current_edges),
        "nodes_added": len(nodes_added),
        "nodes_removed": len(nodes_removed),
        "nodes_changed": len(nodes_changed),
        "edges_added": len(edges_added),
        "edges_removed": len(edges_removed),
        "edges_reversed": len(edges_reversed),
        "community_moves": len(community_moves),
    }

    diff: dict[str, Any] = {
        "schema_version": DIFF_SCHEMA_VERSION,
        "nodes_added": nodes_added,
        "nodes_removed": nodes_removed,
        "nodes_changed": nodes_changed,
        "edges_added": edges_added,
        "edges_removed": edges_removed,
        "edges_reversed": edges_reversed,
        "community_moves": community_moves,
        "communities": communities,
        "summary": summary,
    }
    _guard_no_absolute_paths(canonical_json(diff))
    return diff


# -------------------------------------------------------------------- render


def _guard_no_absolute_paths(text: str) -> None:
    match = _ABS_PATH_RE.search(text)
    if match is not None:
        raise GenerationDiffError(f"diff output contains an absolute path shape: {match.group(0)!r}")


def render_diff_markdown(diff: Mapping[str, Any]) -> str:
    """Deterministic ``DIFF.md`` rendering of a :func:`diff_generations` dict."""
    if diff.get("schema_version") != DIFF_SCHEMA_VERSION:
        raise GenerationDiffError(f"not a v1 diff dict (schema_version={diff.get('schema_version')!r})")

    summary = diff["summary"]
    lines = [
        "# DIFF — knowledge graph generations",
        "",
        "## Summary",
        "",
        f"- nodes: {summary['nodes_before']} -> {summary['nodes_after']}"
        f" (+{summary['nodes_added']} / -{summary['nodes_removed']} / ~{summary['nodes_changed']})",
        f"- edges: {summary['edges_before']} -> {summary['edges_after']}"
        f" (+{summary['edges_added']} / -{summary['edges_removed']} / reversed {summary['edges_reversed']})",
        f"- community moves: {summary['community_moves']}",
        "",
        "## Nodes added",
        "",
    ]
    lines += [f"- {node_id}" for node_id in diff["nodes_added"]] or ["- (none)"]
    lines += ["", "## Nodes removed", ""]
    lines += [f"- {node_id}" for node_id in diff["nodes_removed"]] or ["- (none)"]

    lines += ["", "## Nodes changed", ""]
    changed = diff["nodes_changed"]
    if changed:
        lines += ["| node | version | changes | detail |", "|---|---|---|---|"]
        for entry in changed:
            details: list[str] = []
            if "label_from" in entry:
                details.append(f"label: {entry['label_from']} -> {entry['label_to']}")
            for dimension in _DIMENSIONS:
                if dimension in entry.get("scores", {}):
                    move = entry["scores"][dimension]
                    details.append(f"{dimension}: {move['from']} -> {move['to']} ({move['delta']})")
            lines.append(
                f"| {entry['node_id']} | {entry['version_from']} -> {entry['version_to']} "
                f"| {', '.join(entry['changes'])} | {'; '.join(details)} |"
            )
    else:
        lines.append("- (none)")

    lines += ["", "## Edges reversed", ""]
    reversed_edges = diff["edges_reversed"]
    if reversed_edges:
        for entry in reversed_edges:
            lines.append(
                f"- {entry['from']['source']} -[{entry['from']['relation']}]-> {entry['from']['target']}"
                f"  reversed to  {entry['to']['source']} -[{entry['to']['relation']}]-> {entry['to']['target']}"
            )
    else:
        lines.append("- (none)")

    lines += ["", "## Edges added", ""]
    lines += [f"- {edge['source']} -[{edge['relation']}]-> {edge['target']}" for edge in diff["edges_added"]] or [
        "- (none)"
    ]
    lines += ["", "## Edges removed", ""]
    lines += [f"- {edge['source']} -[{edge['relation']}]-> {edge['target']}" for edge in diff["edges_removed"]] or [
        "- (none)"
    ]

    lines += ["", "## Community moves", ""]
    moves = diff["community_moves"]
    if moves:
        lines += [f"- {move['node_id']}: {move['from']} -> {move['to']}" for move in moves]
    else:
        lines.append("- (none)")

    communities = diff["communities"]
    lines += ["", "## Communities", "", f"source: {communities['source']}", ""]
    if communities["mapping"]:
        lines += ["| previous | current | overlap | label still applies |", "|---|---|---|---|"]
        for entry in communities["mapping"]:
            previous_label = entry.get("previous_sig", entry.get("previous_id"))
            current_label = entry.get("current_sig", entry.get("current_id"))
            lines.append(
                f"| {previous_label} | {current_label} | {entry['overlap']} | {entry['label_still_applies']} |"
            )
    else:
        lines.append("- (no matched communities)")

    text = "\n".join(lines) + "\n"
    _guard_no_absolute_paths(text)
    return text

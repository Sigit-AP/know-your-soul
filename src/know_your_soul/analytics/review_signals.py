"""Deterministic review-signal catalog for the knowledge graph (ATM F-A6).

Scans the ``graph.json`` read model (plus optional community detection
output and the current source-version registry view) and produces review
*candidate* items — structured findings a future ``service.review`` consumer
can adopt without reshaping. This module is pure: it never reads the store,
never writes, and never changes any existing review behaviour.

Signal catalog (fixed order — the round-robin walks it cyclically):

1. ``low_confidence_edge`` — a link published with ``AMBIGUOUS`` confidence.
   A primary finding (``low_confidence=False``): the edge itself declares
   low confidence.
2. ``isolated_node`` — a node with no incident link (self-loops do not count
   as connectivity). Primary finding.
3. ``low_cohesion_community`` — a community of at least 3 members whose
   cohesion falls below ``cohesion_threshold`` (default ``0.15``). Hint-level
   (``low_confidence=True``): loose grouping can be legitimate.
4. ``stale_evidence`` — a node whose ``source_version_id`` is absent from the
   caller-supplied current source-version set. The signal stays silent
   (fail-closed) when no registry view is provided. Hint-level.
5. ``missing_provenance`` — a node with an empty provenance list. Hint-level.

Every item carries ``reason_codes`` plus ``source_node_ids`` /
``source_node_versions`` references, per the frozen recommendation contract
(docs/contracts/recommendation-contract.md). Items are diversified by
round-robin across signal types in catalog order so one noisy signal cannot
swallow the whole quota.

origin: idea from Graphify ``analyze.py`` (signal-catalog + round-robin
quota diversification + confidence flagging as a feature class); signal
definitions and implementation original.
"""

from __future__ import annotations

from collections.abc import Iterable, Mapping
from decimal import Decimal
from typing import Any

from know_your_soul.domain import canonical_json

__all__ = [
    "CATALOG_ORDER",
    "DEFAULT_COHESION_THRESHOLD",
    "DEFAULT_QUOTA",
    "SCHEMA_VERSION",
    "collect_review_signals",
]

SCHEMA_VERSION = 1
DEFAULT_QUOTA = 20
DEFAULT_COHESION_THRESHOLD = Decimal("0.15")
MIN_COHESION_COMMUNITY_SIZE = 3

CATALOG_ORDER: tuple[str, ...] = (
    "low_confidence_edge",
    "isolated_node",
    "low_cohesion_community",
    "stale_evidence",
    "missing_provenance",
)

_REASON_BY_SIGNAL: dict[str, str] = {
    "low_confidence_edge": "LOW_CONFIDENCE_EDGE",
    "isolated_node": "ISOLATED_NODE",
    "low_cohesion_community": "LOW_COHESION_COMMUNITY",
    "stale_evidence": "STALE_EVIDENCE",
    "missing_provenance": "MISSING_PROVENANCE",
}

# Primary findings assert a concrete defect; hint-level signals flag heuristic
# smells that may have legitimate explanations.
_LOW_CONFIDENCE_BY_SIGNAL: dict[str, bool] = {
    "low_confidence_edge": False,
    "isolated_node": False,
    "low_cohesion_community": True,
    "stale_evidence": True,
    "missing_provenance": True,
}


def _item(signal: str, node_ids: list[str], versions: list[int], detail: dict[str, Any]) -> dict[str, Any]:
    return {
        "signal": signal,
        "reason_codes": [_REASON_BY_SIGNAL[signal]],
        "source_node_ids": node_ids,
        "source_node_versions": versions,
        "low_confidence": _LOW_CONFIDENCE_BY_SIGNAL[signal],
        "detail": detail,
    }


def collect_review_signals(
    nodes: Iterable[Mapping[str, Any]],
    links: Iterable[Mapping[str, Any]],
    *,
    communities: Iterable[Mapping[str, Any]] | None = None,
    current_source_versions: Iterable[str] | None = None,
    cohesion_threshold: Decimal = DEFAULT_COHESION_THRESHOLD,
    quota: int = DEFAULT_QUOTA,
) -> dict[str, Any]:
    """Collect review candidates from the read model; output is canonical.

    ``nodes``/``links`` follow the ``graph.json`` shapes. ``communities``
    accepts the ``communities`` list from
    :func:`know_your_soul.analytics.communities.detect_communities`.
    ``current_source_versions`` is the set of source versions that are still
    current; omitting it disables the stale-evidence signal entirely.
    """
    if quota < 0:
        raise ValueError("quota must be >= 0")

    node_list = sorted(nodes, key=lambda node: str(node["id"]))
    link_list = sorted(
        links,
        key=lambda link: (str(link["source"]), str(link["target"]), str(link.get("relation", ""))),
    )
    versions = {str(node["id"]): int(node.get("version", 1)) for node in node_list}

    def version_of(node_id: str) -> int:
        return versions.get(node_id, 1)

    buckets: dict[str, list[dict[str, Any]]] = {signal: [] for signal in CATALOG_ORDER}

    # 1. low-confidence edges
    for link in link_list:
        if str(link.get("confidence", "EXTRACTED")) == "AMBIGUOUS":
            source = str(link["source"])
            target = str(link["target"])
            buckets["low_confidence_edge"].append(
                _item(
                    "low_confidence_edge",
                    [source, target],
                    [version_of(source), version_of(target)],
                    {
                        "source": source,
                        "target": target,
                        "relation": str(link.get("relation", "")),
                        "confidence": "AMBIGUOUS",
                    },
                )
            )

    # 2. isolated nodes (self-loops are not connectivity)
    connected: set[str] = set()
    for link in link_list:
        source = str(link["source"])
        target = str(link["target"])
        if source == target:
            continue
        connected.add(source)
        connected.add(target)
    for node in node_list:
        node_id = str(node["id"])
        if node_id not in connected:
            buckets["isolated_node"].append(
                _item(
                    "isolated_node",
                    [node_id],
                    [version_of(node_id)],
                    {"domain": str(node.get("domain", "")), "degree": 0},
                )
            )

    # 3. low-cohesion communities
    if communities is not None:
        for community in sorted(communities, key=lambda entry: int(entry["id"])):
            members = sorted(str(member) for member in community["members"])
            cohesion = Decimal(str(community["cohesion"]))
            if len(members) >= MIN_COHESION_COMMUNITY_SIZE and cohesion < cohesion_threshold:
                buckets["low_cohesion_community"].append(
                    _item(
                        "low_cohesion_community",
                        members,
                        [version_of(member) for member in members],
                        {
                            "community_id": int(community["id"]),
                            "membership_sig": str(community.get("membership_sig", "")),
                            "cohesion": str(community["cohesion"]),
                            "threshold": str(cohesion_threshold),
                            "size": len(members),
                        },
                    )
                )

    # 4. stale evidence (fail-closed without a registry view)
    if current_source_versions is not None:
        current_versions = set(str(version) for version in current_source_versions)
        for node in node_list:
            node_id = str(node["id"])
            source_version_id = str(node.get("source_version_id", ""))
            if source_version_id not in current_versions:
                buckets["stale_evidence"].append(
                    _item(
                        "stale_evidence",
                        [node_id],
                        [version_of(node_id)],
                        {"source_version_id": source_version_id},
                    )
                )

    # 5. missing provenance
    for node in node_list:
        provenance = node.get("provenance", [])
        if not provenance:
            node_id = str(node["id"])
            buckets["missing_provenance"].append(
                _item(
                    "missing_provenance",
                    [node_id],
                    [version_of(node_id)],
                    {"provenance": []},
                )
            )

    for signal in CATALOG_ORDER:
        buckets[signal].sort(key=lambda item: (list(item["source_node_ids"]), canonical_json(item["detail"])))

    signal_counts = {signal: len(buckets[signal]) for signal in CATALOG_ORDER}

    # Round-robin diversification: walk the catalog cyclically, taking one
    # item per non-exhausted signal, so no single signal type swallows quota.
    positions = {signal: 0 for signal in CATALOG_ORDER}
    emitted: list[dict[str, Any]] = []
    emitted_counts: dict[str, int] = {}
    while len(emitted) < quota:
        progressed = False
        for signal in CATALOG_ORDER:
            if len(emitted) >= quota:
                break
            if positions[signal] < len(buckets[signal]):
                emitted.append(buckets[signal][positions[signal]])
                positions[signal] += 1
                emitted_counts[signal] = emitted_counts.get(signal, 0) + 1
                progressed = True
        if not progressed:
            break

    return {
        "schema_version": SCHEMA_VERSION,
        "quota": quota,
        "signal_counts": signal_counts,
        "emitted_counts": emitted_counts,
        "items": emitted,
    }

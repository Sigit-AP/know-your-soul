"""Composite graph-analysis report (W8 SUB-107) — beats Graphify's GRAPH_REPORT.

Three mechanisms, each measurably stronger than the Graphify counterpart
(``graphify/analyze.py``); the proving tests live in
``tests/test_report_graph_report.py`` and the mechanism-by-mechanism
comparison in ``docs/recovery/w8-graph-report.md``:

* **God nodes — composite ranking, not degree-only.** Graphify ranks by
  raw degree (``god_nodes`` L112-148), which over-rewards leaf-collecting
  hubs. Here every node is scored on three axes — undirected degree,
  PageRank, and bridge (betweenness) centrality, each max-normalized to
  ``[0, 1]`` — and ranked by the equally-weighted composite
  ``(degree + pagerank + betweenness) / 3``. Every entry also carries
  ``rank_by_degree_only`` so the report itself documents where the
  composite overturns Graphify's ordering.
* **Surprising connections — composite score + structured explanation +
  confidence.** Graphify emits a flat ``why`` string. Here each bridge
  (from :func:`know_your_soul.analytics.bridges.find_bridges`: community
  distance + relation rarity + evidence novelty) is enriched with a
  stable, ordered list of reason codes (``CROSS_DOMAIN``,
  ``CROSS_COMMUNITY``, ``SOLE_COMMUNITY_LINK``, ``NO_ALTERNATIVE_PATH``,
  ``UNEXPECTED_PATH``, ``DEGREE_ANOMALY``, ``RARE_RELATION``,
  ``LOW_CONFIDENCE``), the alternative-path hop count with the candidate
  link removed, and a confidence bucket (``high``/``medium``/``low``,
  the same thresholds as ``report_v2``).
* **Suggested questions — template per structural pattern, each carrying
  evidence node ids and an executable query spec.** Graphify generates
  4-7 prose questions with no machine-checkable payload. Here every
  question is one of five deterministic templates (``hub_dependency``,
  ``bridge_path``, ``isolated_community``, ``isolated_node``,
  ``ambiguous_relationship``), carries the evidence node ids, and embeds
  a ``query`` that :func:`answer_question` executes deterministically
  against the same read model — so "answerable by retrieval" is a tested
  property, not a hope. Types are interleaved round-robin before the cap
  so no single signal crowds out the rest.
* **Token benchmark at build time.** Graphify prints token savings after
  ``build``. Here the benchmark is computed from the same estimator the
  retrieval path uses (:func:`know_your_soul.retrieval.estimated_tokens`)
  and embedded in both the canonical JSON and the markdown; the CLI also
  prints it to stderr. ``report_json_tokens`` is measured over the report
  *without* the benchmark section itself (self-reference would be
  circular).

Everything is a pure function of the published ``graph.json`` read model:
no store, no clock, no RNG beyond the fixed-seed centrality sampler.
Scores are 12-decimal fixed-point strings; ordering is fully canonical.
"""

from __future__ import annotations

import re
from collections import deque
from collections.abc import Mapping
from decimal import Decimal, localcontext
from typing import Any

from know_your_soul.analytics.bridges import find_bridges
from know_your_soul.analytics.centrality import (
    DEFAULT_BETWEENNESS_SAMPLES,
    DEFAULT_PAGERANK_ITERATIONS,
    _betweenness_scores,
    _degree_scores,
    _directed_adjacency,
    _pagerank_scores,
    _prepare_graph,
    _undirected_adjacency,
)
from know_your_soul.analytics.paths import PathQueryError, find_k_shortest_paths
from know_your_soul.domain import DECIMAL_PRECISION, canonical_json, quantize_score
from know_your_soul.report.report_v2 import CONFIDENCE_BUCKETS
from know_your_soul.retrieval import estimated_tokens

__all__ = [
    "DEFAULT_TOP_GOD",
    "DEFAULT_TOP_QUESTIONS",
    "DEFAULT_TOP_SURPRISING",
    "GRAPH_REPORT_KIND",
    "GRAPH_REPORT_SCHEMA_VERSION",
    "GraphReportError",
    "QuestionError",
    "answer_question",
    "build_graph_analysis",
    "render_graph_analysis_markdown",
]

GRAPH_REPORT_SCHEMA_VERSION = 1
GRAPH_REPORT_KIND = "report.graph_analysis"

DEFAULT_TOP_GOD = 10
DEFAULT_TOP_SURPRISING = 10
DEFAULT_TOP_QUESTIONS = 8

#: Composite god-node weights (degree, pagerank, betweenness), equal thirds.
GOD_NODE_WEIGHTS = {"degree": 1, "pagerank": 1, "betweenness": 1}

#: Peripheral endpoint degree at or below this, other endpoint at or above
#: ``DEGREE_ANOMALY_HUB_DEGREE`` -> ``DEGREE_ANOMALY`` explanation.
DEGREE_ANOMALY_MAX_PERIPHERAL = 2
DEGREE_ANOMALY_HUB_DEGREE = 6

#: Alternative path (candidate link removed) at least this many hops ->
#: ``UNEXPECTED_PATH`` explanation.
UNEXPECTED_PATH_MIN_HOPS = 3

#: Relation rarity at or above this share -> ``RARE_RELATION`` explanation.
RARE_RELATION_THRESHOLD = Decimal("0.9")

#: Stable explanation-code ordering (report + markdown + tests pin it).
_EXPLANATION_ORDER = (
    "CROSS_DOMAIN",
    "CROSS_COMMUNITY",
    "SOLE_COMMUNITY_LINK",
    "NO_ALTERNATIVE_PATH",
    "UNEXPECTED_PATH",
    "DEGREE_ANOMALY",
    "RARE_RELATION",
    "LOW_CONFIDENCE",
)

#: Question-type interleave order (round-robin before the top cap).
_QUESTION_TYPE_ORDER = (
    "hub_dependency",
    "bridge_path",
    "isolated_community",
    "isolated_node",
    "ambiguous_relationship",
)

#: Candidate caps per question type (before interleave).
_HUB_QUESTION_CANDIDATES = 5
_BRIDGE_QUESTION_CANDIDATES = 3
_COMMUNITY_QUESTION_CANDIDATES = 2
_ISOLATED_QUESTION_CANDIDATES = 3
_AMBIGUOUS_QUESTION_CANDIDATES = 3

# Same absolute-path shape guard as report_v2: output must never carry one.
_ABS_PATH_RE = re.compile(r"(?<![\w@:+/.-])/(?:[\w@.+-]+/){2,}[\w@.+-]+")


class GraphReportError(RuntimeError):
    """The graph report would carry content it must never publish (fail closed)."""


class QuestionError(GraphReportError):
    """A suggested-question query is invalid (unknown kind, unknown node)."""


# ------------------------------------------------------------------ internals


def _q12(value: Decimal) -> str:
    return format(quantize_score(value), "f")


def _node_index(graph: Mapping[str, Any]) -> dict[str, Mapping[str, Any]]:
    index: dict[str, Mapping[str, Any]] = {}
    raw_nodes = graph.get("nodes")
    if isinstance(raw_nodes, list):
        for entry in raw_nodes:
            if isinstance(entry, Mapping) and isinstance(entry.get("id"), str) and entry["id"]:
                index.setdefault(entry["id"], entry)
    return index


def _label(index: Mapping[str, Mapping[str, Any]], node_id: str) -> str:
    label = index.get(node_id, {}).get("label")
    return label if isinstance(label, str) and label.strip() else node_id


def _filtered_links(graph: Mapping[str, Any], node_ids: set[str]) -> list[dict[str, Any]]:
    """Valid, deduplicated links preserving ``confidence`` (canonical order)."""
    seen: set[tuple[str, str, str]] = set()
    links: list[dict[str, Any]] = []
    raw_links = graph.get("links")
    if isinstance(raw_links, list):
        for entry in raw_links:
            if not isinstance(entry, Mapping):
                continue
            source, target, relation = entry.get("source"), entry.get("target"), entry.get("relation")
            if not (isinstance(source, str) and isinstance(target, str) and isinstance(relation, str)):
                continue
            if source not in node_ids or target not in node_ids or source == target:
                continue
            key = (source, relation, target)
            if key in seen:
                continue
            seen.add(key)
            links.append(
                {
                    "source": source,
                    "target": target,
                    "relation": relation,
                    "confidence": str(entry.get("confidence", "EXTRACTED")),
                }
            )
    links.sort(key=lambda link: (link["source"], link["target"], link["relation"]))
    return links


def _membership(index: Mapping[str, Mapping[str, Any]]) -> dict[str, str] | None:
    """node id -> community_sig, or None when no node carries a signature."""
    membership = {
        node_id: str(entry["community_sig"])
        for node_id, entry in index.items()
        if isinstance(entry.get("community_sig"), str) and entry["community_sig"]
    }
    return membership or None


def _raw_degrees(node_ids: tuple[str, ...], links: list[dict[str, Any]]) -> dict[str, int]:
    degrees = {node_id: 0 for node_id in node_ids}
    seen_pairs: set[tuple[str, str]] = set()
    for link in links:
        pair = (link["source"], link["target"]) if link["source"] < link["target"] else (link["target"], link["source"])
        if pair in seen_pairs:
            continue
        seen_pairs.add(pair)
        degrees[link["source"]] += 1
        degrees[link["target"]] += 1
    return degrees


def _alternative_path_hops(adjacency: Mapping[str, tuple[str, ...]], source: str, target: str) -> int:
    """Undirected hops from ``source`` to ``target`` with the direct pair removed; -1 unreachable."""
    visited = {source}
    queue: deque[tuple[str, int]] = deque([(source, 0)])
    while queue:
        current, depth = queue.popleft()
        for neighbour in adjacency.get(current, ()):
            if {current, neighbour} == {source, target}:
                continue
            if neighbour == target:
                return depth + 1
            if neighbour not in visited:
                visited.add(neighbour)
                queue.append((neighbour, depth + 1))
    return -1


def _max_normalized(scores: Mapping[str, Decimal]) -> dict[str, Decimal]:
    # Degenerate graphs (n < 2) carry no relative signal: all zeros.
    if len(scores) < 2:
        return {node_id: Decimal(0) for node_id in scores}
    peak = max(scores.values())
    if peak <= 0:
        return {node_id: Decimal(0) for node_id in scores}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        return {node_id: scores[node_id] / peak for node_id in scores}


# ------------------------------------------------------------------ god nodes


def _god_nodes(
    node_ids: tuple[str, ...],
    links: list[dict[str, Any]],
    index: Mapping[str, Mapping[str, Any]],
    top_god: int,
) -> list[dict[str, Any]]:
    link_tuples = tuple((link["source"], link["relation"], link["target"]) for link in links)
    adjacency = _directed_adjacency(node_ids, link_tuples)
    degree = _max_normalized(_degree_scores(node_ids, link_tuples))
    pagerank = _max_normalized(_pagerank_scores(node_ids, link_tuples, adjacency, DEFAULT_PAGERANK_ITERATIONS))
    betweenness, _sampled = _betweenness_scores(node_ids, adjacency, DEFAULT_BETWEENNESS_SAMPLES)
    betweenness = _max_normalized(betweenness)
    raw_degree = _raw_degrees(node_ids, links)
    incoming = {node_id: 0 for node_id in node_ids}
    outgoing = {node_id: 0 for node_id in node_ids}
    for link in links:
        outgoing[link["source"]] += 1
        incoming[link["target"]] += 1

    weight_total = sum(GOD_NODE_WEIGHTS.values())
    composite: dict[str, Decimal] = {}
    with localcontext() as context:
        context.prec = DECIMAL_PRECISION
        for node_id in node_ids:
            weighted = (
                GOD_NODE_WEIGHTS["degree"] * degree[node_id]
                + GOD_NODE_WEIGHTS["pagerank"] * pagerank[node_id]
                + GOD_NODE_WEIGHTS["betweenness"] * betweenness[node_id]
            )
            composite[node_id] = weighted / Decimal(weight_total)

    degree_only_order = sorted(node_ids, key=lambda node_id: (-raw_degree[node_id], node_id))
    degree_only_rank = {node_id: rank for rank, node_id in enumerate(degree_only_order, start=1)}

    ordered = sorted(node_ids, key=lambda node_id: (-composite[node_id], node_id))[:top_god]
    return [
        {
            "rank": rank,
            "node_id": node_id,
            "label": _label(index, node_id),
            "domain": str(index.get(node_id, {}).get("domain", "")),
            "degree": raw_degree[node_id],
            "incoming": incoming[node_id],
            "outgoing": outgoing[node_id],
            "scores": {
                "degree": _q12(degree[node_id]),
                "pagerank": _q12(pagerank[node_id]),
                "betweenness": _q12(betweenness[node_id]),
                "composite": _q12(composite[node_id]),
            },
            "rank_by_degree_only": degree_only_rank[node_id],
        }
        for rank, node_id in enumerate(ordered, start=1)
    ]


# ---------------------------------------------------- surprising connections


def _confidence_bucket(surprise_score: str) -> str:
    score = Decimal(surprise_score)
    for name, floor in CONFIDENCE_BUCKETS:
        if score >= floor:
            return name
    return CONFIDENCE_BUCKETS[-1][0]


def _explanations(
    bridge: Mapping[str, Any],
    *,
    source_domain: str,
    target_domain: str,
    cross_community: bool,
    alternative_hops: int,
    source_degree: int,
    target_degree: int,
) -> list[dict[str, str]]:
    explanations: list[dict[str, str]] = []
    if source_domain != target_domain:
        explanations.append(
            {
                "code": "CROSS_DOMAIN",
                "detail": f"bridges domains {source_domain} and {target_domain}",
            }
        )
    if cross_community:
        explanations.append({"code": "CROSS_COMMUNITY", "detail": "endpoints sit in different communities"})
    distance = int(bridge["components"]["community_distance"])
    if cross_community and distance < 0:
        explanations.append(
            {
                "code": "SOLE_COMMUNITY_LINK",
                "detail": "the only link between the two communities; removing it disconnects them",
            }
        )
    if alternative_hops < 0:
        explanations.append(
            {
                "code": "NO_ALTERNATIVE_PATH",
                "detail": "no alternative path between the endpoints with this link removed",
            }
        )
    elif alternative_hops >= UNEXPECTED_PATH_MIN_HOPS:
        explanations.append(
            {
                "code": "UNEXPECTED_PATH",
                "detail": f"shortest alternative route is {alternative_hops} hops — the direct link is unexpected",
            }
        )
    low, high = min(source_degree, target_degree), max(source_degree, target_degree)
    if low <= DEGREE_ANOMALY_MAX_PERIPHERAL and high >= DEGREE_ANOMALY_HUB_DEGREE:
        ordered_pair = (
            (source_degree, target_degree) if source_degree <= target_degree else (target_degree, source_degree)
        )
        peripheral, hub = ordered_pair
        explanations.append(
            {
                "code": "DEGREE_ANOMALY",
                "detail": f"peripheral endpoint (degree {peripheral}) reaches a hub (degree {hub})",
            }
        )
    rarity = Decimal(str(bridge["components"]["relation_rarity"]))
    if rarity >= RARE_RELATION_THRESHOLD:
        explanations.append(
            {
                "code": "RARE_RELATION",
                "detail": f"relation `{bridge['relation']}` is rare in this graph (rarity {rarity})",
            }
        )
    if str(bridge["confidence"]) in ("AMBIGUOUS", "INFERRED"):
        explanations.append(
            {
                "code": "LOW_CONFIDENCE",
                "detail": f"edge confidence is {bridge['confidence']} — not explicitly stated in the source",
            }
        )
    rank = {code: position for position, code in enumerate(_EXPLANATION_ORDER)}
    explanations.sort(key=lambda explanation: rank[explanation["code"]])
    return explanations


def _surprising_connections(
    graph: Mapping[str, Any],
    node_ids: tuple[str, ...],
    links: list[dict[str, Any]],
    index: Mapping[str, Mapping[str, Any]],
    membership: Mapping[str, str] | None,
    top_surprising: int,
) -> list[dict[str, Any]]:
    nodes = [index[node_id] for node_id in node_ids]
    found = find_bridges(nodes, links, membership=membership, top_n=top_surprising)
    link_tuples = tuple((link["source"], link["relation"], link["target"]) for link in links)
    undirected = _undirected_adjacency(node_ids, link_tuples)
    raw_degree = _raw_degrees(node_ids, links)

    enriched: list[dict[str, Any]] = []
    for rank, bridge in enumerate(found["bridges"], start=1):
        source, target = bridge["source"], bridge["target"]
        alternative_hops = _alternative_path_hops(undirected, source, target)
        cross_community = (
            membership is not None
            and membership.get(source) is not None
            and membership.get(source) != membership.get(target)
        )
        explanations = _explanations(
            bridge,
            source_domain=str(index.get(source, {}).get("domain", "")),
            target_domain=str(index.get(target, {}).get("domain", "")),
            cross_community=cross_community,
            alternative_hops=alternative_hops,
            source_degree=raw_degree[source],
            target_degree=raw_degree[target],
        )
        enriched.append(
            {
                "rank": rank,
                "source": source,
                "target": target,
                "relation": bridge["relation"],
                "kind": bridge["kind"],
                "surprise_score": bridge["surprise_score"],
                "confidence": _confidence_bucket(bridge["surprise_score"]),
                "components": bridge["components"],
                "explanations": explanations,
                "evidence_node_ids": [source, target],
                "alternative_path_hops": alternative_hops,
            }
        )
    _ = graph  # signature symmetry with the other section builders
    return enriched


# -------------------------------------------------------- suggested questions


def _hub_questions(god_nodes: list[dict[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for entry in god_nodes[:_HUB_QUESTION_CANDIDATES]:
        if entry["incoming"] < 1:
            continue
        label = entry["label"]
        questions.append(
            {
                "type": "hub_dependency",
                "question": f"What depends on `{label}`?",
                "why": (
                    f"composite god-node rank {entry['rank']} (score {entry['scores']['composite']}); "
                    f"{entry['incoming']} incoming link(s)"
                ),
                "evidence_node_ids": [entry["node_id"]],
                "query": {"kind": "dependents", "node": entry["node_id"]},
            }
        )
    return questions


def _bridge_questions(surprising: list[dict[str, Any]], index: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    for entry in surprising[:_BRIDGE_QUESTION_CANDIDATES]:
        source_label = _label(index, entry["source"])
        target_label = _label(index, entry["target"])
        lead = entry["explanations"][0]["detail"] if entry["explanations"] else "crosses structural boundaries"
        questions.append(
            {
                "type": "bridge_path",
                "question": f"How is `{source_label}` connected to `{target_label}`?",
                "why": f"surprise {entry['surprise_score']}: {lead}",
                "evidence_node_ids": [entry["source"], entry["target"]],
                "query": {"kind": "path", "source": entry["source"], "target": entry["target"]},
            }
        )
    return questions


def _community_questions(
    node_ids: tuple[str, ...],
    links: list[dict[str, Any]],
    index: Mapping[str, Mapping[str, Any]],
    membership: Mapping[str, str] | None,
) -> list[dict[str, Any]]:
    if membership is None:
        return []
    members_by_sig: dict[str, list[str]] = {}
    for node_id in node_ids:
        sig = membership.get(node_id)
        if sig is not None:
            members_by_sig.setdefault(sig, []).append(node_id)
    boundary_sigs: set[str] = set()
    for link in links:
        sig_source, sig_target = membership.get(link["source"]), membership.get(link["target"])
        if sig_source is not None and sig_target is not None and sig_source != sig_target:
            boundary_sigs.add(sig_source)
            boundary_sigs.add(sig_target)
    raw_degree = _raw_degrees(node_ids, links)
    questions: list[dict[str, Any]] = []
    isolated = [sig for sig in members_by_sig if sig not in boundary_sigs and len(members_by_sig[sig]) < len(node_ids)]
    for sig in sorted(isolated, key=lambda s: (-len(members_by_sig[s]), s))[:_COMMUNITY_QUESTION_CANDIDATES]:
        members = sorted(members_by_sig[sig])
        hub = min(members, key=lambda node_id: (-raw_degree[node_id], node_id))
        hub_label = _label(index, hub)
        question_text = (
            f"Why is the group around `{hub_label}` ({len(members)} node(s)) separate from the rest of the graph?"
        )
        questions.append(
            {
                "type": "isolated_community",
                "question": question_text,
                "why": f"community {sig} has 0 links crossing its boundary",
                "evidence_node_ids": members,
                "query": {"kind": "community_boundary", "members": members},
            }
        )
    return questions


def _isolated_node_questions(
    node_ids: tuple[str, ...],
    links: list[dict[str, Any]],
    index: Mapping[str, Mapping[str, Any]],
) -> list[dict[str, Any]]:
    raw_degree = _raw_degrees(node_ids, links)
    questions: list[dict[str, Any]] = []
    for node_id in sorted(node_ids):
        if raw_degree[node_id] > 1:
            continue
        label = _label(index, node_id)
        questions.append(
            {
                "type": "isolated_node",
                "question": f"What connects `{label}` to the rest of the system?",
                "why": f"degree {raw_degree[node_id]} <= 1 — weakly connected",
                "evidence_node_ids": [node_id],
                "query": {"kind": "neighborhood", "node": node_id},
            }
        )
        if len(questions) >= _ISOLATED_QUESTION_CANDIDATES:
            break
    return questions


def _ambiguous_questions(links: list[dict[str, Any]], index: Mapping[str, Mapping[str, Any]]) -> list[dict[str, Any]]:
    questions: list[dict[str, Any]] = []
    ambiguous = sorted(
        (link for link in links if link["confidence"] == "AMBIGUOUS"),
        key=lambda link: (link["source"], link["target"], link["relation"]),
    )
    for link in ambiguous[:_AMBIGUOUS_QUESTION_CANDIDATES]:
        source_label = _label(index, link["source"])
        target_label = _label(index, link["target"])
        questions.append(
            {
                "type": "ambiguous_relationship",
                "question": f"What is the exact relationship between `{source_label}` and `{target_label}`?",
                "why": f"edge tagged AMBIGUOUS (relation `{link['relation']}`) — confidence is low",
                "evidence_node_ids": [link["source"], link["target"]],
                "query": {"kind": "edge_evidence", "source": link["source"], "target": link["target"]},
            }
        )
    return questions


def _suggested_questions(
    node_ids: tuple[str, ...],
    links: list[dict[str, Any]],
    index: Mapping[str, Mapping[str, Any]],
    membership: Mapping[str, str] | None,
    god_nodes: list[dict[str, Any]],
    surprising: list[dict[str, Any]],
    top_questions: int,
) -> list[dict[str, Any]]:
    by_type: dict[str, list[dict[str, Any]]] = {
        "hub_dependency": _hub_questions(god_nodes),
        "bridge_path": _bridge_questions(surprising, index),
        "isolated_community": _community_questions(node_ids, links, index, membership),
        "isolated_node": _isolated_node_questions(node_ids, links, index),
        "ambiguous_relationship": _ambiguous_questions(links, index),
    }
    pools = [by_type[name] for name in _QUESTION_TYPE_ORDER]
    if not any(pools):
        return [
            {
                "rank": 1,
                "type": "no_signal",
                "question": None,
                "why": (
                    "not enough structural signal: no god node with incoming links, no bridge, "
                    "no isolated community or node, no AMBIGUOUS edge"
                ),
                "evidence_node_ids": [],
                "query": None,
            }
        ]
    interleaved: list[dict[str, Any]] = []
    depth = max(len(pool) for pool in pools)
    for level in range(depth):
        for pool in pools:
            if level < len(pool):
                interleaved.append(pool[level])
    return [{"rank": rank, **question} for rank, question in enumerate(interleaved[:top_questions], start=1)]


# ------------------------------------------------------------- token benchmark


def _token_benchmark(graph: Mapping[str, Any], report: dict[str, Any]) -> dict[str, Any]:
    """Token benchmark with a deterministic fixed point for self-reference.

    The markdown carries the benchmark's own numbers, so the markdown token
    count is computed by iterating render → measure → inject until the
    values stop changing (digit-width convergence; bounded at 8 rounds).
    ``report_json_tokens`` is measured over the report *without* the
    benchmark section — a JSON measure of itself would be circular.
    """
    graph_tokens = estimated_tokens(graph)
    json_tokens = estimated_tokens(report)

    def _build(markdown_tokens: int) -> dict[str, Any]:
        with localcontext() as context:
            context.prec = DECIMAL_PRECISION
            if graph_tokens > 0 and markdown_tokens > 0:
                savings = Decimal(1) - Decimal(markdown_tokens) / Decimal(graph_tokens)
                compression = Decimal(graph_tokens) / Decimal(markdown_tokens)
            else:
                savings = Decimal(0)
                compression = Decimal(1)
        return {
            "estimator": "ceil(utf8_bytes / 4) over the canonical payload (retrieval.estimated_tokens)",
            "graph_tokens": graph_tokens,
            "report_json_tokens": json_tokens,
            "report_markdown_tokens": markdown_tokens,
            "savings_ratio": _q12(savings),
            "compression_ratio": _q12(compression),
        }

    benchmark = _build(0)
    for _ in range(8):
        rendered = render_graph_analysis_markdown({**report, "token_benchmark": benchmark})
        nxt = _build(-(-len(rendered.encode("utf-8")) // 4))
        if nxt == benchmark:
            break
        benchmark = nxt
    return benchmark


# ------------------------------------------------------------------ public API


def build_graph_analysis(
    graph: Mapping[str, Any],
    *,
    top_god: int = DEFAULT_TOP_GOD,
    top_surprising: int = DEFAULT_TOP_SURPRISING,
    top_questions: int = DEFAULT_TOP_QUESTIONS,
) -> dict[str, Any]:
    """Build the canonical composite graph-analysis report over one read model.

    ``graph`` is the parsed ``graph.json`` mapping (``nodes``/``links``).
    The result is JSON-ready (no ``Decimal``, no timestamps, no absolute
    paths) and a pure function of the graph content plus the explicit
    parameters.
    """
    for name, value in (("top_god", top_god), ("top_surprising", top_surprising), ("top_questions", top_questions)):
        if value < 1:
            raise ValueError(f"{name} must be >= 1")
    node_ids, _domains, link_tuples, dropped = _prepare_graph(graph)
    node_id_set = set(node_ids)
    links = _filtered_links(graph, node_id_set)
    index = _node_index(graph)
    membership = _membership(index)

    god_nodes = _god_nodes(node_ids, links, index, top_god)
    surprising = _surprising_connections(graph, node_ids, links, index, membership, top_surprising)
    questions = _suggested_questions(node_ids, links, index, membership, god_nodes, surprising, top_questions)

    communities = len({sig for sig in (membership or {}).values()})
    report: dict[str, Any] = {
        "schema_version": GRAPH_REPORT_SCHEMA_VERSION,
        "kind": GRAPH_REPORT_KIND,
        "parameters": {"top_god": top_god, "top_surprising": top_surprising, "top_questions": top_questions},
        "counts": {
            "nodes": len(node_ids),
            "links": len(link_tuples),
            "communities": communities,
            "dropped_links": dropped,
        },
        "god_nodes": god_nodes,
        "surprising_connections": surprising,
        "suggested_questions": questions,
    }
    report["token_benchmark"] = _token_benchmark(graph, report)
    _guard_output(report)
    return report


def _guard_output(report: Mapping[str, Any]) -> None:
    match = _ABS_PATH_RE.search(canonical_json(report))
    if match is not None:
        raise GraphReportError(f"report output contains an absolute path shape: {match.group(0)!r}")


def render_graph_analysis_markdown(report: Mapping[str, Any]) -> str:
    """Deterministic markdown rendering of a :func:`build_graph_analysis` dict."""
    if report.get("schema_version") != GRAPH_REPORT_SCHEMA_VERSION or report.get("kind") != GRAPH_REPORT_KIND:
        raise GraphReportError(
            f"not a {GRAPH_REPORT_KIND} dict "
            f"(schema_version={report.get('schema_version')!r}, kind={report.get('kind')!r})"
        )
    counts = report["counts"]
    lines = [
        "# SOUL Graph Analysis Report",
        "",
        f"nodes: {counts['nodes']} | links: {counts['links']} | communities: {counts['communities']} | "
        f"dropped links: {counts['dropped_links']}",
        "",
        "## God nodes",
        "",
        "composite = (degree + pagerank + betweenness) / 3, each max-normalized; `degree-only #` is the "
        "Graphify-style raw-degree rank",
        "",
        "| rank | node | domain | degree | pagerank | betweenness | composite | degree-only # |",
        "|---|---|---|---|---|---|---|---|",
    ]
    god_nodes = report["god_nodes"]
    if god_nodes:
        for entry in god_nodes:
            scores = entry["scores"]
            lines.append(
                f"| {entry['rank']} | `{entry['label']}` | {entry['domain']} | {entry['degree']} | "
                f"{scores['pagerank']} | {scores['betweenness']} | {scores['composite']} | "
                f"{entry['rank_by_degree_only']} |"
            )
    else:
        lines.append("| - | none | - | - | - | - | - | - |")
    lines += ["", "## Surprising connections", ""]
    surprising = report["surprising_connections"]
    if surprising:
        for entry in surprising:
            lines.append(
                f"{entry['rank']}. `{entry['source']}` —{entry['relation']}→ `{entry['target']}` "
                f"(surprise {entry['surprise_score']}, confidence {entry['confidence']}, {entry['kind']})"
            )
            for explanation in entry["explanations"]:
                lines.append(f"   - {explanation['code']}: {explanation['detail']}")
            hops = entry["alternative_path_hops"]
            lines.append(f"   - alternative path: {'none' if hops < 0 else f'{hops} hops'}")
    else:
        lines.append("- none")
    lines += ["", "## Suggested questions", ""]
    questions = report["suggested_questions"]
    if questions and questions[0]["type"] == "no_signal":
        lines.append(f"- no signal: {questions[0]['why']}")
    elif questions:
        for question in questions:
            lines.append(f"{question['rank']}. [{question['type']}] {question['question']}")
            lines.append(f"   - why: {question['why']}")
            lines.append(f"   - evidence: {', '.join(question['evidence_node_ids'])}")
            lines.append(f"   - query: {question['query']['kind']} {canonical_json(question['query'])}")
    else:
        lines.append("- none")
    lines += ["", "## Token benchmark", ""]
    benchmark = report.get("token_benchmark")
    if benchmark is None:
        lines.append("- computed at build time; re-run build_graph_analysis to populate")
    else:
        lines += [
            f"- raw graph.json: ~{benchmark['graph_tokens']} tokens",
            f"- this report: ~{benchmark['report_markdown_tokens']} tokens markdown / "
            f"~{benchmark['report_json_tokens']} tokens json",
            f"- savings: {benchmark['savings_ratio']} ({benchmark['compression_ratio']}x smaller)",
            f"- estimator: {benchmark['estimator']}",
        ]
    text = "\n".join(lines) + "\n"
    match = _ABS_PATH_RE.search(text)
    if match is not None:
        raise GraphReportError(f"rendered report contains an absolute path shape: {match.group(0)!r}")
    return text


def answer_question(graph: Mapping[str, Any], question: Mapping[str, Any]) -> dict[str, Any]:
    """Execute a suggested question's ``query`` against the read model.

    Deterministic and total: every query kind the generator emits resolves
    here. Invalid queries (unknown kind, unknown node, malformed payload)
    raise :class:`QuestionError`.
    """
    query = question.get("query") if isinstance(question, Mapping) else None
    if not isinstance(query, Mapping):
        raise QuestionError("question carries no executable query")
    kind = query.get("kind")
    node_ids, _domains, link_tuples, _dropped = _prepare_graph(graph)
    node_id_set = set(node_ids)

    if kind == "dependents":
        node = query.get("node")
        if not isinstance(node, str) or node not in node_id_set:
            raise QuestionError(f"unknown node: {node!r}")
        dependents = sorted({source for source, _relation, target in link_tuples if target == node})
        return {
            "kind": "dependents",
            "node": node,
            "dependents": dependents,
            "count": len(dependents),
            "answerable": True,
        }
    if kind == "path":
        source, target = query.get("source"), query.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise QuestionError("path query needs source and target node ids")
        try:
            found = find_k_shortest_paths(graph, source=source, target=target, k=1)
        except PathQueryError as error:
            raise QuestionError(str(error)) from error
        if found["paths"]:
            best = found["paths"][0]
            return {
                "kind": "path",
                "source": source,
                "target": target,
                "reachable": True,
                "path": best["nodes"],
                "cost": best["cost"],
                "answerable": True,
            }
        return {
            "kind": "path",
            "source": source,
            "target": target,
            "reachable": False,
            "path": None,
            "cost": None,
            "answerable": True,
        }
    if kind == "community_boundary":
        members = query.get("members")
        if not isinstance(members, list) or not all(isinstance(member, str) for member in members):
            raise QuestionError("community_boundary query needs a members list")
        member_set = set(members)
        boundary: list[dict[str, Any]] = []
        for source, relation, target in link_tuples:
            inside_source, inside_target = source in member_set, target in member_set
            if inside_source == inside_target:
                continue
            boundary.append(
                {
                    "source": source,
                    "relation": relation,
                    "target": target,
                    "direction": "outgoing" if inside_source else "incoming",
                }
            )
        return {
            "kind": "community_boundary",
            "members": sorted(member_set),
            "boundary_links": boundary,
            "count": len(boundary),
            "isolated": not boundary,
            "answerable": True,
        }
    if kind == "neighborhood":
        node = query.get("node")
        if not isinstance(node, str) or node not in node_id_set:
            raise QuestionError(f"unknown node: {node!r}")
        neighbors = sorted(
            {target for source, _relation, target in link_tuples if source == node}
            | {source for source, _relation, target in link_tuples if target == node}
        )
        return {
            "kind": "neighborhood",
            "node": node,
            "neighbors": neighbors,
            "degree": len(neighbors),
            "answerable": True,
        }
    if kind == "edge_evidence":
        source, target = query.get("source"), query.get("target")
        if not isinstance(source, str) or not isinstance(target, str):
            raise QuestionError("edge_evidence query needs source and target node ids")
        evidence: list[dict[str, Any]] = []
        for link_source, relation, link_target in link_tuples:
            if (link_source, link_target) == (source, target):
                evidence.append(
                    {"source": link_source, "relation": relation, "target": link_target, "direction": "forward"}
                )
            elif (link_source, link_target) == (target, source):
                evidence.append(
                    {"source": link_source, "relation": relation, "target": link_target, "direction": "reverse"}
                )
        return {
            "kind": "edge_evidence",
            "source": source,
            "target": target,
            "links": evidence,
            "count": len(evidence),
            "answerable": True,
        }
    raise QuestionError(f"unknown query kind: {kind!r}")

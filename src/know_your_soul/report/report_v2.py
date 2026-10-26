"""Structured GRAPH_REPORT v2 (ATM plan F-C3).

The rich graph report as *data*: :func:`build_graph_report` returns a
canonical, JSON-ready dict (no ``Decimal``, no timestamps, no absolute
paths) and :func:`render_markdown` renders that dict deterministically.

Honesty discipline (the Graphify ``report.py`` lesson): every count in the
report flows through the single :func:`is_real_node` predicate, so
``thin + shown`` can never silently undercount the corpus. All numbers are
derived from the published read model (``graph.json``/manifest); the store
watermark is read-only input supplied by the caller. Review signals are
accepted as plain mappings (loose coupling — this module never imports the
health or review-signals producers).
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from decimal import Decimal, localcontext
from typing import Any

from know_your_soul.domain import canonical_json, quantize_score

__all__ = [
    "CONFIDENCE_BUCKETS",
    "DEFAULT_MIN_COMMUNITY_SIZE",
    "REPORT_SCHEMA_VERSION",
    "ReportIntegrityError",
    "build_graph_report",
    "is_real_node",
    "render_markdown",
]

REPORT_SCHEMA_VERSION = 1

#: Default minimum real-node count for a community to be "shown".
DEFAULT_MIN_COMMUNITY_SIZE = 2

#: Node-confidence buckets as ``(name, inclusive floor)``, highest first.
CONFIDENCE_BUCKETS: tuple[tuple[str, Decimal], ...] = (
    ("high", Decimal("0.7")),
    ("medium", Decimal("0.4")),
    ("low", Decimal("0")),
)

_EDGE_CONFIDENCE_LABELS = ("EXTRACTED", "INFERRED", "AMBIGUOUS")

_SEVERITY_RANK = {"critical": 0, "warning": 1, "info": 2}

# Same absolute-path shape as the read-model validator: an absolute POSIX
# path of 3+ segments that is not part of a URI. Output must never carry one.
_ABS_PATH_RE = re.compile(r"(?<![\w@:+/.-])/(?:[\w@.+-]+/){2,}[\w@.+-]+")


class ReportIntegrityError(RuntimeError):
    """The report would carry content it must never publish (fail closed)."""


def is_real_node(node: Any) -> bool:
    """THE single "real node" predicate for every count in this report.

    A real node is a mapping with a non-empty string ``id`` and a non-blank
    string ``label``. Nothing else is consulted anywhere in this module, so
    totals, per-domain summaries, confidence distributions and the
    thin/shown community split can never drift onto two definitions.
    """
    if not isinstance(node, Mapping):
        return False
    node_id = node.get("id")
    label = node.get("label")
    return isinstance(node_id, str) and bool(node_id) and isinstance(label, str) and bool(label.strip())


def _q12(value: Decimal) -> str:
    """The 12-decimal fixed-point rendering used for every reported score."""
    return format(quantize_score(value), "f")


def _mean_12dp(values: list[Decimal]) -> str:
    with localcontext() as context:
        context.prec = 34
        total = sum(values, Decimal(0))
        return _q12(total / Decimal(len(values)))


def _decimal_field(node: Mapping[str, Any], *path: str) -> Decimal:
    current: Any = node
    for key in path:
        if not isinstance(current, Mapping):
            return Decimal(0)
        current = current.get(key)
    try:
        return Decimal(str(current))
    except ArithmeticError:
        return Decimal(0)


def _bucket(confidence: Decimal) -> str:
    for name, floor in CONFIDENCE_BUCKETS:
        if confidence >= floor:
            return name
    return CONFIDENCE_BUCKETS[-1][0]


def _normalize_signal(signal: Mapping[str, Any]) -> dict[str, str]:
    return {
        "code": str(signal.get("code", "unknown")),
        "severity": str(signal.get("severity", "info")),
        "subject": str(signal.get("subject", "")),
    }


def build_graph_report(
    graph: Mapping[str, Any],
    manifest: Mapping[str, Any],
    *,
    store_watermark: int | None = None,
    review_signals: Iterable[Mapping[str, Any]] = (),
    min_community_size: int = DEFAULT_MIN_COMMUNITY_SIZE,
    top_review_signals: int = 10,
) -> dict[str, Any]:
    """Build the canonical structured report over one published generation.

    ``graph`` is the parsed ``graph.json``; ``manifest`` the generation
    manifest; ``store_watermark`` is the store's knowledge high watermark
    read by the caller (read-only) — ``None`` marks staleness as unknown.
    ``review_signals`` are plain mappings (``code``/``severity``/``subject``)
    supplied by the caller; this module never imports their producers.
    """
    if min_community_size < 1:
        raise ValueError("min_community_size must be positive")
    raw_nodes: Any = graph.get("nodes")
    raw_links: Any = graph.get("links")
    nodes: list[Any] = list(raw_nodes) if isinstance(raw_nodes, list) else []
    links: list[Any] = list(raw_links) if isinstance(raw_links, list) else []

    real_nodes = [node for node in nodes if is_real_node(node)]
    real_ids = {str(node["id"]) for node in real_nodes}
    real_edges = [
        link
        for link in links
        if isinstance(link, Mapping)
        and isinstance(link.get("source"), str)
        and isinstance(link.get("target"), str)
        and link["source"] in real_ids
        and link["target"] in real_ids
    ]

    # ------------------------------------------------- confidence distribution
    edge_confidence = {label: 0 for label in _EDGE_CONFIDENCE_LABELS}
    for link in real_edges:
        label = str(link.get("confidence"))
        if label in edge_confidence:
            edge_confidence[label] += 1
    node_buckets = {name: 0 for name, _floor in CONFIDENCE_BUCKETS}
    for node in real_nodes:
        node_buckets[_bucket(_decimal_field(node, "confidence"))] += 1

    # --------------------------------------------------------- domain summary
    by_domain: dict[str, list[Mapping[str, Any]]] = {}
    for node in real_nodes:
        domain = node.get("domain")
        by_domain.setdefault(str(domain) if isinstance(domain, str) else "?", []).append(node)
    domains: list[dict[str, Any]] = []
    for domain in sorted(by_domain):
        members = by_domain[domain]
        domains.append(
            {
                "domain": domain,
                "nodes": len(members),
                "mean": {
                    "p": _mean_12dp([_decimal_field(node, "scores", "p") for node in members]),
                    "m": _mean_12dp([_decimal_field(node, "scores", "m") for node in members]),
                    "e": _mean_12dp([_decimal_field(node, "scores", "e") for node in members]),
                    "uncertainty": _mean_12dp([_decimal_field(node, "scores", "uncertainty") for node in members]),
                },
            }
        )

    # ------------------------------------------- communities (thin vs shown)
    community_members: dict[str, int] = {}
    unassigned = 0
    for node in real_nodes:
        sig = node.get("community_sig")
        if isinstance(sig, str) and sig:
            community_members[sig] = community_members.get(sig, 0) + 1
        else:
            unassigned += 1
    shown = sum(1 for size in community_members.values() if size >= min_community_size)
    thin = len(community_members) - shown
    communities = {
        "total": len(community_members),
        "shown": shown,
        "thin": thin,
        "shown_nodes": sum(size for size in community_members.values() if size >= min_community_size),
        "thin_nodes": sum(size for size in community_members.values() if size < min_community_size),
        "unassigned_nodes": unassigned,
        "min_community_size": min_community_size,
    }

    # ---------------------------------------------------------------- staleness
    manifest_watermark = int(manifest["source_knowledge_high_watermark"])
    if store_watermark is None:
        staleness: dict[str, Any] = {"known": False}
    else:
        lag = int(store_watermark) - manifest_watermark
        staleness = {
            "known": True,
            "manifest_watermark": manifest_watermark,
            "store_watermark": int(store_watermark),
            "lag": lag,
            "stale": lag != 0,
        }

    # --------------------------------------------------------- review signals
    normalized = sorted(
        (_normalize_signal(signal) for signal in review_signals),
        key=lambda signal: (_SEVERITY_RANK.get(signal["severity"], 3), signal["code"], signal["subject"]),
    )
    top = normalized[:top_review_signals]
    signals_section = {
        "total": len(normalized),
        "shown": len(top),
        "omitted": len(normalized) - len(top),
        "top": top,
    }

    report: dict[str, Any] = {
        "schema_version": REPORT_SCHEMA_VERSION,
        "predicate": "is_real_node",
        "counts": {
            "nodes_total": len(nodes),
            "nodes_real": len(real_nodes),
            "edges_total": len(links),
            "edges_real": len(real_edges),
            "domains": len(domains),
            "communities": len(community_members),
        },
        "confidence_distribution": {"edges": edge_confidence, "node_buckets": node_buckets},
        "domains": domains,
        "communities": communities,
        "staleness": staleness,
        "review_signals": signals_section,
    }
    _guard_output(report)
    return report


def _guard_output(report: Mapping[str, Any]) -> None:
    """Fail closed if the report would carry an absolute-path shape."""
    match = _ABS_PATH_RE.search(canonical_json(report))
    if match is not None:
        raise ReportIntegrityError(f"report output contains an absolute path shape: {match.group(0)!r}")


def render_markdown(report: Mapping[str, Any]) -> str:
    """Deterministic markdown rendering of a :func:`build_graph_report` dict."""
    if report.get("schema_version") != REPORT_SCHEMA_VERSION:
        raise ReportIntegrityError(f"not a v2 report dict (schema_version={report.get('schema_version')!r})")
    counts = report["counts"]
    staleness = report["staleness"]
    lines = [
        "# SOUL Knowledge Graph Report (v2)",
        "",
        "All counts flow through the single `is_real_node` predicate.",
        "",
        "## Counts",
        "",
        f"- nodes: {counts['nodes_real']} real / {counts['nodes_total']} total",
        f"- edges: {counts['edges_real']} real / {counts['edges_total']} total",
        f"- domains: {counts['domains']}",
        f"- communities: {counts['communities']}",
        "",
        "## Confidence distribution",
        "",
        "edges: " + " ".join(f"{label}={count}" for label, count in report["confidence_distribution"]["edges"].items()),
        "node confidence: "
        + " ".join(f"{name}={count}" for name, count in report["confidence_distribution"]["node_buckets"].items()),
        "",
        "## Domains",
        "",
        "| domain | nodes | P | M | E | U |",
        "|---|---|---|---|---|---|",
    ]
    for entry in report["domains"]:
        mean = entry["mean"]
        row = (
            f"| {entry['domain']} | {entry['nodes']} | {mean['p']} | {mean['m']} "
            f"| {mean['e']} | {mean['uncertainty']} |"
        )
        lines.append(row)
    communities = report["communities"]
    lines += [
        "",
        "## Communities",
        "",
        f"- total: {communities['total']} ({communities['shown']} shown, {communities['thin']} thin)",
        f"- nodes: {communities['shown_nodes']} shown, {communities['thin_nodes']} thin, "
        f"{communities['unassigned_nodes']} unassigned",
        f"- min community size: {communities['min_community_size']}",
        "",
        "## Staleness",
        "",
    ]
    if staleness.get("known"):
        status = "stale" if staleness["stale"] else "fresh"
        lines.append(
            f"- status: {status} (manifest watermark {staleness['manifest_watermark']}, "
            f"store watermark {staleness['store_watermark']}, lag {staleness['lag']})"
        )
    else:
        lines.append("- status: unknown (no store watermark supplied)")
    signals = report["review_signals"]
    lines += ["", "## Top review signals", ""]
    if signals["top"]:
        for signal in signals["top"]:
            lines.append(f"- [{signal['severity']}] {signal['code']}: {signal['subject']}")
        if signals["omitted"]:
            lines.append(f"- ... {signals['omitted']} more signal(s) omitted")
    else:
        lines.append("- none")
    text = "\n".join(lines) + "\n"
    match = _ABS_PATH_RE.search(text)
    if match is not None:
        raise ReportIntegrityError(f"rendered report contains an absolute path shape: {match.group(0)!r}")
    return text

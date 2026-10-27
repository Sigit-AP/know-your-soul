"""The built-in ATM export registry (wave integration, SUB-70; mega-wave 2, SUB-80).

Wires every ATM wave A/B/C and mega-wave 2 module into the F-B8 orchestrator
as capped, round-trip-validated, manifest-ready artifacts:

* analytics — centrality, components, cycles, communities, bridges,
  review-signals, health, report-v2 (+ markdown), generation-diff (+ DIFF.md),
  timeline (+ TIMELINE.md), and the baseline domain-rollup; plus the
  mega-wave 2 suite: hits, eigenvector, katz, clustering, louvain,
  reachability, motifs, assortativity, a deterministic k-shortest-paths
  probe, the graph summary, domain score aggregates, and the proposal-only
  near-duplicate candidates;
* formats — node-index, DOT, Mermaid, tabular CSV (nodes + edges), GraphML,
  SVG, the self-contained HTML viewer, the book (fixed page set: ``index.md``
  + one page per primary domain P1..P7), and the mega-wave 2 formats: GEXF,
  Cytoscape.js elements JSON, D3-force node-link JSON, adjacency matrix CSV +
  edge list, and TGF.

Context-dependent modules never open the store themselves. The caller builds
an :class:`ExportContext` once — :func:`gather_context` does it with strictly
read-only SQL (the timeline record adapter follows the pattern documented in
``docs/recovery/atm-wave-c2.md``) — and every producer closes over that
immutable snapshot, so production stays deterministic and side-effect free.

``generation-diff`` needs two generations; when the corpus has only one (or
retention collected the rest) the artifact reports ``no_previous_generation``
as a valid, honest result instead of failing.
"""

from __future__ import annotations

import json
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from know_your_soul.analytics import (
    algorithms,
    bridges,
    centrality,
    communities,
    components,
    cycles,
    generation_diff,
    health,
    paths,
    query_tools,
    review_signals,
    timeline,
)
from know_your_soul.domain import PrimaryDomain
from know_your_soul.exporters import (
    adjacency,
    cytoscape,
    d3force,
    dot,
    gexf,
    graphml_svg,
    html_view,
    mermaid,
    summary,
    tabular,
    tgf,
)
from know_your_soul.exporters import book as book_exporter
from know_your_soul.exporters.orchestrator import (
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
    default_registry,
)
from know_your_soul.report import report_v2
from know_your_soul.store import EventStore
from know_your_soul.viz.layout import layout

__all__ = [
    "ANALYTICS_ALIASES",
    "BOOK_PAGES",
    "FORMAT_ALIASES",
    "ExportContext",
    "build_registry",
    "gather_context",
    "resolve_selection",
]

# Read-only store → timeline records adapter (pattern documented in
# docs/recovery/atm-wave-c2.md §"Risiko / catatan untuk integrasi"). The
# payload is parsed here so the timeline module stays store-agnostic.
_TIMELINE_RECORDS_SQL = (
    "SELECT event_seq, event_type, node_id, domain, occurred_at, recorded_at, payload_json "
    "FROM event_log WHERE kind = 'domain' ORDER BY event_seq"
)
_EVIDENCE_COUNTS_SQL = "SELECT node_id, COUNT(*) AS n FROM evidence GROUP BY node_id"
_CURRENT_SOURCE_VERSIONS_SQL = "SELECT source_version_id FROM source_version WHERE source_state = 'CURRENT'"

# Per-artifact caps (explicit, fail-closed — never a silent truncation).
_JSON_SMALL = 2 * 1024 * 1024
_JSON_LARGE = 4 * 1024 * 1024
_TIMELINE_JSON_MAX = 8 * 1024 * 1024
_MARKDOWN_MAX = 1 * 1024 * 1024

_DIFF_HEADER = "# DIFF — knowledge graph generations"
_NO_PREVIOUS_STATUS = "no_previous_generation"
_TIMELINE_HEADER = "# TIMELINE — P/M/E learning series"
_REPORT_HEADER = "# SOUL Knowledge Graph Report (v2)"

#: The book's fixed page set: the index plus one page per primary domain.
#: Fixed names keep the registry (and its fingerprint) context-independent;
#: community pages stay available through ``exporters.book`` directly.
BOOK_PAGES: tuple[str, ...] = ("book/index.md",) + tuple(f"book/{domain.value}.md" for domain in sorted(PrimaryDomain))

#: Short CLI aliases for the format artifacts (``know-your-soul export``).
FORMAT_ALIASES: dict[str, tuple[str, ...]] = {
    "node-index": ("exports/node-index.json",),
    "dot": ("exports/graph.dot",),
    "mermaid": ("exports/graph.mmd",),
    "tabular": ("exports/nodes.csv", "exports/edges.csv"),
    "graphml": ("exports/graph.graphml",),
    "svg": ("exports/graph.svg",),
    "html": ("exports/graph.html",),
    "book": BOOK_PAGES,
    # Mega-wave 2 formats (SUB-75, wired by SUB-80).
    "gexf": ("exports/graph.gexf",),
    "cytoscape": ("exports/cytoscape.json",),
    "d3force": ("exports/d3-force.json",),
    "adjacency": ("exports/adjacency.csv", "exports/edges.txt"),
    "tgf": ("exports/graph.tgf",),
}

#: Short CLI aliases for the analytics artifacts (``know-your-soul analytics``).
ANALYTICS_ALIASES: dict[str, tuple[str, ...]] = {
    "domain-rollup": ("analytics/domain-rollup.json",),
    "centrality": ("analytics/centrality.json",),
    "components": ("analytics/components.json",),
    "cycles": ("analytics/cycles.json",),
    "communities": ("analytics/communities.json",),
    "bridges": ("analytics/bridges.json",),
    "review-signals": ("analytics/review-signals.json",),
    "health": ("analytics/health.json",),
    "report-v2": ("analytics/report-v2.json", "analytics/graph-report-v2.md"),
    "generation-diff": ("analytics/generation-diff.json", "analytics/DIFF.md"),
    "timeline": ("analytics/timeline.json", "analytics/TIMELINE.md"),
    # Mega-wave 2 analytics (SUB-74/SUB-75/SUB-76, wired by SUB-80).
    "graph-summary": ("analytics/graph-summary.json",),
    "hits": ("analytics/hits.json",),
    "eigenvector": ("analytics/eigenvector.json",),
    "katz": ("analytics/katz.json",),
    "clustering": ("analytics/clustering.json",),
    "louvain": ("analytics/louvain.json",),
    "reachability": ("analytics/reachability.json",),
    "motifs": ("analytics/motifs.json",),
    "assortativity": ("analytics/assortativity.json",),
    "paths": ("analytics/paths.json",),
    "aggregates": ("analytics/aggregates.json",),
    "near-duplicates": ("analytics/near-duplicates.json",),
}


@dataclass(frozen=True)
class ExportContext:
    """Immutable store/generation snapshot for context-dependent producers.

    Every field defaults to an empty/unknown value so a context-free registry
    still produces honest artifacts (an empty timeline, a diff that reports
    ``no_previous_generation``, a report whose staleness is ``unknown``).
    """

    timeline_records: tuple[Mapping[str, Any], ...] = ()
    previous_graph: Mapping[str, Any] | None = None
    evidence_counts: Mapping[str, int] = field(default_factory=dict)
    # ``None`` = unknown (the stale-evidence signal stays off); a set —
    # possibly empty — is a real read of the store's CURRENT source versions.
    current_source_versions: frozenset[str] | None = None
    store_watermark: int | None = None
    manifest: Mapping[str, Any] | None = None


def gather_context(store: EventStore, *, generation_dir: Path) -> ExportContext:
    """Read the store and generation directory into an :class:`ExportContext`.

    Strictly read-only: plain ``SELECT`` statements inside the store's read
    lock plus file reads under the generation directory. The previous
    generation (for the diff) is the deterministically newest *other*
    generation — highest knowledge watermark, content-hash name as the
    tie-break — or ``None`` when only one generation exists on disk.
    """
    with store.read() as conn:
        records = tuple(
            {
                "event_seq": int(row["event_seq"]),
                "event_type": str(row["event_type"]),
                "node_id": row["node_id"] if row["node_id"] is None else str(row["node_id"]),
                "domain": row["domain"] if row["domain"] is None else str(row["domain"]),
                "occurred_at": str(row["occurred_at"]),
                "recorded_at": str(row["recorded_at"]),
                "payload": json.loads(str(row["payload_json"])),
            }
            for row in conn.execute(_TIMELINE_RECORDS_SQL).fetchall()
        )
        evidence_counts = {str(row["node_id"]): int(row["n"]) for row in conn.execute(_EVIDENCE_COUNTS_SQL).fetchall()}
        current_source_versions = frozenset(
            str(row["source_version_id"]) for row in conn.execute(_CURRENT_SOURCE_VERSIONS_SQL).fetchall()
        )
    watermark = store.knowledge_high_watermark()

    try:
        manifest_raw = json.loads((generation_dir / "manifest.json").read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as error:
        raise ExportError(f"active generation manifest is unreadable: {generation_dir}: {error}") from error
    if not isinstance(manifest_raw, dict):
        raise ExportError(f"active generation manifest is not an object: {generation_dir}")
    manifest: dict[str, Any] = manifest_raw

    previous_graph: Mapping[str, Any] | None = None
    candidates: list[tuple[int, str]] = []
    generations_root = generation_dir.parent
    for candidate in generations_root.iterdir():
        if not candidate.is_dir() or candidate.name == generation_dir.name:
            continue
        try:
            other_manifest = json.loads((candidate / "manifest.json").read_text(encoding="utf-8"))
            candidate_watermark = int(other_manifest["source_knowledge_high_watermark"])
        except OSError, json.JSONDecodeError, KeyError, TypeError, ValueError:
            continue  # a torn/foreign generation directory is not a diff base
        candidates.append((candidate_watermark, candidate.name))
    if candidates:
        _watermark, previous_name = max(candidates)
        try:
            previous_raw = json.loads((generations_root / previous_name / "graph.json").read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as error:
            raise ExportError(f"previous generation graph.json is unreadable: {previous_name}: {error}") from error
        if isinstance(previous_raw, dict):
            previous_graph = previous_raw

    return ExportContext(
        timeline_records=records,
        previous_graph=previous_graph,
        evidence_counts=evidence_counts,
        current_source_versions=current_source_versions,
        store_watermark=watermark,
        manifest=manifest,
    )


def resolve_selection(selection: str, *, kind: str, registry: ExportRegistry) -> tuple[str, ...]:
    """Resolve a comma-separated CLI selection into registered artifact names.

    Each part is ``all`` (every artifact of the command's kind), a short
    alias from :data:`FORMAT_ALIASES`/:data:`ANALYTICS_ALIASES`, or a full
    artifact name. Anything else is a ``ValueError`` (a CLI usage error).
    """
    parts = [part.strip() for part in selection.split(",")]
    if not parts or any(not part for part in parts):
        raise ValueError(f"expected a comma-separated list of names or aliases, got {selection!r}")
    aliases = {**FORMAT_ALIASES, **ANALYTICS_ALIASES}
    registered = set(registry.names())
    names: list[str] = []
    for part in parts:
        if part == "all":
            names.extend(registry.names(kind=kind))
        elif part in aliases:
            names.extend(aliases[part])
        elif part in registered:
            names.append(part)
        else:
            raise ValueError(
                f"unknown export selection {part!r}; aliases: {', '.join(sorted(aliases))}, all; "
                f"artifacts: {', '.join(registry.names())}"
            )
    return tuple(sorted(dict.fromkeys(names)))


# ------------------------------------------------------------------ producers


def _nodes(read_model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = read_model.get("nodes")
    return [node for node in raw if isinstance(node, Mapping)] if isinstance(raw, list) else []


def _links(read_model: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    raw = read_model.get("links")
    return [link for link in raw if isinstance(link, Mapping)] if isinstance(raw, list) else []


def _detect_communities(read_model: Mapping[str, Any]) -> dict[str, Any]:
    return communities.detect_communities(_nodes(read_model), _links(read_model))


def _bridges(read_model: Mapping[str, Any]) -> Mapping[str, Any]:
    detected = _detect_communities(read_model)
    membership = {member: entry["id"] for entry in detected["communities"] for member in entry["members"]}
    return bridges.find_bridges(_nodes(read_model), _links(read_model), membership=membership)


def _review_signals(context: ExportContext, read_model: Mapping[str, Any]) -> dict[str, Any]:
    detected = _detect_communities(read_model)
    return review_signals.collect_review_signals(
        _nodes(read_model),
        _links(read_model),
        communities=detected["communities"],
        current_source_versions=context.current_source_versions,
    )


def _report(context: ExportContext, read_model: Mapping[str, Any]) -> dict[str, Any]:
    signals = _review_signals(context, read_model)
    mapped = [
        {
            "code": str(item["signal"]),
            "severity": "info" if item["low_confidence"] else "warning",
            "subject": str(item["source_node_ids"][0]) if item["source_node_ids"] else str(item["signal"]),
        }
        for item in signals["items"]
    ]
    # Without a manifest (context-free registry) the watermark is reported as
    # 0 and staleness stays ``unknown`` — never a fabricated value.
    manifest = context.manifest if context.manifest is not None else {"source_knowledge_high_watermark": 0}
    return report_v2.build_graph_report(
        read_model,
        manifest,
        store_watermark=context.store_watermark,
        review_signals=mapped,
    )


def _diff(context: ExportContext, read_model: Mapping[str, Any]) -> dict[str, Any]:
    if context.previous_graph is None:
        return {
            "schema_version": 1,
            "artifact": "generation-diff",
            "status": _NO_PREVIOUS_STATUS,
            "summary": {
                "nodes_before": 0,
                "nodes_after": len(_nodes(read_model)),
                "edges_before": 0,
                "edges_after": len(_links(read_model)),
            },
        }
    diff = generation_diff.diff_generations(context.previous_graph, read_model)
    return {"status": "diff", **diff}


def _diff_markdown(context: ExportContext, read_model: Mapping[str, Any]) -> str:
    diff = _diff(context, read_model)
    if diff.get("status") == _NO_PREVIOUS_STATUS:
        summary = diff["summary"]
        lines = [
            _DIFF_HEADER,
            "",
            "status: no previous generation — this is the first generation on disk (or older",
            "generations were collected by retention), so there is nothing to diff against.",
            "",
            f"- nodes: {summary['nodes_after']}",
            f"- edges: {summary['edges_after']}",
            "",
        ]
        return "\n".join(lines)
    return generation_diff.render_diff_markdown(diff)


def _timeline(context: ExportContext) -> dict[str, Any]:
    return timeline.build_timeline(context.timeline_records)


def _paths_probe(read_model: Mapping[str, Any]) -> dict[str, Any]:
    """A deterministic Yen k-shortest-paths probe over the read model.

    ``find_k_shortest_paths`` needs explicit endpoints; a context-free
    artifact picks them deterministically — the lexicographically smallest
    and largest node ids of the published graph. A graph with fewer than two
    addressable nodes reports ``insufficient_nodes`` honestly instead of
    raising (the probe is a report, not a query).
    """
    node_ids = sorted(
        str(node["id"]) for node in _nodes(read_model) if isinstance(node.get("id"), str) and node.get("id")
    )
    if len(node_ids) < 2:
        return {
            "schema_version": 1,
            "kind": "analytics.paths.k_shortest",
            "status": "insufficient_nodes",
            "parameters": {
                "source": node_ids[0] if node_ids else None,
                "target": None,
                "k": paths.DEFAULT_K,
                "max_expansions": paths.DEFAULT_MAX_EXPANSIONS,
            },
            "node_count": len(node_ids),
            "path_count": 0,
            "paths": [],
            "guards": [],
        }
    return paths.find_k_shortest_paths(read_model, source=node_ids[0], target=node_ids[-1])


def _html_producer(read_model: Mapping[str, Any]) -> bytes:
    """The HTML viewer positioned by the deterministic layout engine (F-B7)."""
    positions = layout(read_model, width=1000.0, height=1000.0).positions
    rendered = html_view.render_html_view(read_model, positions=positions, max_bytes=None)
    return rendered.encode("utf-8")


def _book_pages(context: ExportContext, read_model: Mapping[str, Any]) -> dict[str, str]:
    pages = book_exporter.build_book(
        read_model,
        evidence_counts=context.evidence_counts,
        include_domains=[domain.value for domain in PrimaryDomain],
    )
    book_exporter.validate_book(pages)
    return pages


def _book_page_producer(context: ExportContext, page: str):  # noqa: ANN202 - factory keeps the page binding explicit
    def _produce(read_model: Mapping[str, Any]) -> bytes:
        pages = _book_pages(context, read_model)
        try:
            return pages[page].encode("utf-8")
        except KeyError:
            raise ExportError(f"book build did not produce the registered page {page!r}") from None

    return _produce


# --------------------------------------------------------------- round-trips


def _markdown_roundtrip(name: str, header: str, required: tuple[str, ...], alternatives: tuple[str, ...] = ()):
    def _roundtrip(data: bytes) -> None:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExportRoundTripError(f"artifact {name} is not UTF-8: {error}") from error
        if not text.startswith(header):
            raise ExportRoundTripError(f"artifact {name} does not start with {header!r}")
        missing = [marker for marker in required if marker not in text]
        if missing:
            raise ExportRoundTripError(f"artifact {name} is missing markers: {missing}")
        if alternatives and not any(marker in text for marker in alternatives):
            raise ExportRoundTripError(f"artifact {name} carries none of the expected markers: {list(alternatives)}")

    return _roundtrip


def _book_page_roundtrip(page: str):  # noqa: ANN202 - factory keeps the page binding explicit
    def _roundtrip(data: bytes) -> None:
        try:
            text = data.decode("utf-8")
        except UnicodeDecodeError as error:
            raise ExportRoundTripError(f"book page {page} is not UTF-8: {error}") from error
        if page == "book/index.md":
            if "<!-- know-your-soul-book v1 -->" not in text:
                raise ExportRoundTripError(f"book page {page} is missing its schema marker")
        elif not text.startswith("# Domain ") or "[Index](index.md)" not in text:
            raise ExportRoundTripError(f"book page {page} lacks the domain header or the index link")

    return _roundtrip


# ------------------------------------------------------------------- registry


def build_registry(context: ExportContext | None = None) -> ExportRegistry:
    """The full built-in registry: every ATM format and analytics artifact.

    The registered name set (and therefore the registry fingerprint) is
    independent of *context*; the context only shapes artifact content.
    """
    ctx = context if context is not None else ExportContext()
    registry = ExportRegistry()

    # Baseline seam artifacts from the orchestrator's minimal registry.
    baseline = default_registry()
    for name in baseline.names():
        registry.register(baseline.get(name))

    # ---------------------------------------------------------- formats
    dot.register_dot(registry)
    mermaid.register_mermaid(registry)
    tabular.register_tabular(registry)
    registry.register(
        ExportFormat(
            name="exports/graph.graphml",
            version=1,
            kind="format",
            max_bytes=graphml_svg.GRAPHML_MAX_BYTES,
            producer=graphml_svg.to_graphml,
            roundtrip=graphml_svg.validate_graphml_bytes,
        )
    )
    registry.register(
        ExportFormat(
            name="exports/graph.svg",
            version=1,
            kind="format",
            max_bytes=graphml_svg.SVG_MAX_BYTES,
            producer=graphml_svg.to_svg,
            roundtrip=graphml_svg.validate_svg_bytes,
        )
    )
    html_spec = html_view.html_view_format()
    registry.register(
        ExportFormat(
            name=html_spec.name,
            version=html_spec.version,
            kind=html_spec.kind,
            max_bytes=html_spec.max_bytes,
            producer=_html_producer,
            roundtrip=html_spec.roundtrip,
        )
    )
    for page in BOOK_PAGES:
        registry.register(
            ExportFormat(
                name=page,
                version=1,
                kind="format",
                max_bytes=book_exporter.MAX_BOOK_PAGE_BYTES,
                producer=_book_page_producer(ctx, page),
                roundtrip=_book_page_roundtrip(page),
            )
        )

    # Mega-wave 2 formats (SUB-75): self-registering with their own caps and
    # round-trips — gexf, cytoscape, d3force, adjacency (matrix + edge list),
    # tgf.
    gexf.register_gexf(registry)
    cytoscape.register_cytoscape(registry)
    d3force.register_d3force(registry)
    adjacency.register_adjacency(registry)
    tgf.register_tgf(registry)

    # ---------------------------------------------------------- analytics
    registry.register_json(
        "analytics/centrality.json", centrality.compute_centrality, kind="analytics", max_bytes=_JSON_LARGE
    )
    registry.register_json(
        "analytics/components.json",
        lambda read_model: components.analyze_components(read_model, evidence_counts=ctx.evidence_counts),
        kind="analytics",
        max_bytes=_JSON_SMALL,
    )
    registry.register_json("analytics/cycles.json", cycles.find_cycles, kind="analytics", max_bytes=_JSON_SMALL)
    registry.register_json("analytics/communities.json", _detect_communities, kind="analytics", max_bytes=_JSON_LARGE)
    registry.register_json("analytics/bridges.json", _bridges, kind="analytics", max_bytes=_JSON_SMALL)
    registry.register_json(
        "analytics/review-signals.json",
        lambda read_model: _review_signals(ctx, read_model),
        kind="analytics",
        max_bytes=_JSON_SMALL,
    )
    registry.register_json(
        "analytics/health.json",
        lambda read_model: health.analyze_corpus_health(
            read_model,
            communities={
                entry["membership_sig"]: entry["members"] for entry in _detect_communities(read_model)["communities"]
            },
        ),
        kind="analytics",
        max_bytes=_JSON_SMALL,
    )
    registry.register_json(
        "analytics/report-v2.json",
        lambda read_model: _report(ctx, read_model),
        kind="analytics",
        max_bytes=_JSON_SMALL,
    )
    registry.register(
        ExportFormat(
            name="analytics/graph-report-v2.md",
            version=1,
            kind="analytics",
            max_bytes=_MARKDOWN_MAX,
            producer=lambda read_model: (report_v2.render_markdown(_report(ctx, read_model)) + "\n").encode("utf-8"),
            roundtrip=_markdown_roundtrip("analytics/graph-report-v2.md", _REPORT_HEADER, ("## Counts",)),
        )
    )
    registry.register_json(
        "analytics/generation-diff.json",
        lambda read_model: _diff(ctx, read_model),
        kind="analytics",
        max_bytes=_JSON_LARGE,
    )
    registry.register(
        ExportFormat(
            name="analytics/DIFF.md",
            version=1,
            kind="analytics",
            max_bytes=_MARKDOWN_MAX,
            producer=lambda read_model: (_diff_markdown(ctx, read_model)).encode("utf-8"),
            roundtrip=_markdown_roundtrip(
                "analytics/DIFF.md",
                _DIFF_HEADER,
                (),
                alternatives=("## Summary", "no previous generation"),
            ),
        )
    )
    registry.register_json(
        "analytics/timeline.json",
        lambda read_model: _timeline(ctx),
        kind="analytics",
        max_bytes=_TIMELINE_JSON_MAX,
    )
    registry.register(
        ExportFormat(
            name="analytics/TIMELINE.md",
            version=1,
            kind="analytics",
            max_bytes=2 * _MARKDOWN_MAX,
            producer=lambda read_model: (timeline.render_timeline_markdown(_timeline(ctx)) + "\n").encode("utf-8"),
            roundtrip=_markdown_roundtrip("analytics/TIMELINE.md", _TIMELINE_HEADER, ("scoring contract:",)),
        )
    )

    # Mega-wave 2 analytics (SUB-74/SUB-75/SUB-76). All producers are pure
    # functions of the read model; caps are explicit and fail-closed.
    summary.register_summary(registry)  # analytics/graph-summary.json
    registry.register_json("analytics/hits.json", algorithms.compute_hits, kind="analytics", max_bytes=_JSON_LARGE)
    registry.register_json(
        "analytics/eigenvector.json", algorithms.compute_eigenvector, kind="analytics", max_bytes=_JSON_LARGE
    )
    registry.register_json("analytics/katz.json", algorithms.compute_katz, kind="analytics", max_bytes=_JSON_LARGE)
    registry.register_json(
        "analytics/clustering.json", algorithms.compute_clustering, kind="analytics", max_bytes=_JSON_SMALL
    )
    registry.register_json(
        "analytics/louvain.json", algorithms.compute_louvain, kind="analytics", max_bytes=_JSON_LARGE
    )
    registry.register_json(
        "analytics/reachability.json", algorithms.compute_reachability, kind="analytics", max_bytes=_JSON_LARGE
    )
    registry.register_json("analytics/motifs.json", algorithms.count_motifs, kind="analytics", max_bytes=_JSON_LARGE)
    registry.register_json(
        "analytics/assortativity.json", algorithms.compute_assortativity, kind="analytics", max_bytes=_JSON_SMALL
    )
    registry.register_json("analytics/paths.json", _paths_probe, kind="analytics", max_bytes=_JSON_SMALL)
    registry.register_json(
        "analytics/aggregates.json",
        lambda read_model: query_tools.aggregate_nodes(read_model, group_by="domain"),
        kind="analytics",
        max_bytes=_JSON_SMALL,
    )
    registry.register_json(
        "analytics/near-duplicates.json",
        query_tools.find_near_duplicates,
        kind="analytics",
        max_bytes=_JSON_LARGE,
    )
    return registry

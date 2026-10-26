"""Integration tests for the built-in ATM export registry (SUB-70).

The built-in registry (``exporters/builtin.py``) wires every ATM wave A/B/C
module — analytics (centrality, components, cycles, communities, bridges,
review-signals, health, report-v2, generation-diff, timeline) and exporters
(dot, mermaid, tabular, graphml, svg, html-view, book) — into the F-B8
orchestrator as capped, round-trip-validated artifacts. Context-dependent
modules (timeline, generation-diff, components, review-signals, report-v2,
book) read a store-derived :class:`ExportContext` that is gathered read-only.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest

from know_your_soul.cli import main
from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from know_your_soul.exporters import builtin
from know_your_soul.exporters.builtin import (
    ANALYTICS_ALIASES,
    FORMAT_ALIASES,
    ExportContext,
    build_registry,
    gather_context,
    resolve_selection,
)
from know_your_soul.exporters.orchestrator import manifest_entries, produce_exports
from know_your_soul.projection import GraphProjector
from know_your_soul.report import report_v2
from know_your_soul.store import EventStore

# --------------------------------------------------------------- expectations

EXPECTED_FORMAT_ARTIFACTS = {
    "exports/node-index.json",
    "exports/graph.dot",
    "exports/graph.mmd",
    "exports/nodes.csv",
    "exports/edges.csv",
    "exports/graph.graphml",
    "exports/graph.svg",
    "exports/graph.html",
    "book/index.md",
    "book/P1.md",
    "book/P2.md",
    "book/P3.md",
    "book/P4.md",
    "book/P5.md",
    "book/P6.md",
    "book/P7.md",
}

EXPECTED_ANALYTICS_ARTIFACTS = {
    "analytics/domain-rollup.json",
    "analytics/centrality.json",
    "analytics/components.json",
    "analytics/cycles.json",
    "analytics/communities.json",
    "analytics/bridges.json",
    "analytics/review-signals.json",
    "analytics/health.json",
    "analytics/report-v2.json",
    "analytics/graph-report-v2.md",
    "analytics/generation-diff.json",
    "analytics/DIFF.md",
    "analytics/timeline.json",
    "analytics/TIMELINE.md",
}


# ------------------------------------------------------------------- fixtures


def _node(node_id: str, domain: PrimaryDomain, *, kind: NodeKind = NodeKind.LEAF) -> Node:
    return Node(
        node_id=node_id,
        kind=kind,
        domain=domain,
        summary=f"summary of {node_id}",
        provenance=("soul://tests",),
        source_version_id="sv-test",
    )


def _evidence(evidence_id: str, node_id: str) -> Evidence:
    return Evidence(
        evidence_id=evidence_id,
        node_id=node_id,
        source_version_id="sv-test",
        source_uri="soul://tests/evidence",
        observed_at=datetime(2026, 1, 1, tzinfo=UTC),
        independence_key=f"ind-{evidence_id}",
        outcome=Decimal("1"),
        correctness=Decimal("1"),
        reliability=Decimal("1"),
        reproducibility=Decimal("1"),
        scope_fit=Decimal("1"),
        successful_use=Decimal("1"),
        task_coverage=Decimal("1"),
        quality_at_use=Decimal("1"),
        contradiction=Decimal("0"),
        utilization_u=Decimal("0.5"),
        latency_ms=5,
        compute_ms=5,
        output_chars=42,
    )


def _seed_store(db_path: Path) -> EventStore:
    store = EventStore(db_path)
    upserts = [
        ("KROOT", PrimaryDomain.P1, NodeKind.ROOT),
        ("N-P1", PrimaryDomain.P1, NodeKind.LEAF),
        ("N-P2", PrimaryDomain.P2, NodeKind.LEAF),
        ("N-P5", PrimaryDomain.P5, NodeKind.LEAF),
    ]
    for node_id, domain, kind in upserts:
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=node_id,
                domain=domain,
                node=_node(node_id, domain, kind=kind),
                source_version_id="sv-test",
            ),
            idempotency_key=f"test:upsert:{node_id}",
        )
    # one real relation: N-P1 supports N-P2 (cross-domain bridge material)
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="N-P1",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.SUPPORTS, source_node_id="N-P1", target_node_id="N-P2"),
        ),
        idempotency_key="test:relation:supports:p1-p2",
    )
    store.append_domain(
        Event(
            kind=EventKind.EVIDENCE_APPEND,
            node_id="N-P1",
            domain=PrimaryDomain.P1,
            evidence=_evidence("ev-1", "N-P1"),
            source_version_id="sv-test",
        ),
        idempotency_key="test:evidence:1",
    )
    store.append_domain(
        Event(
            kind=EventKind.EVIDENCE_APPEND,
            node_id="N-P2",
            domain=PrimaryDomain.P2,
            evidence=_evidence("ev-2", "N-P2"),
            source_version_id="sv-test",
        ),
        idempotency_key="test:evidence:2",
    )
    return store


def _rebuilt(tmp_path: Path) -> tuple[Path, Path]:
    db_path = tmp_path / "soul.sqlite3"
    out_path = tmp_path / "out"
    store = _seed_store(db_path)
    GraphProjector(store, output_root=out_path).rebuild()
    store.close()
    return db_path, out_path


def _generation_dir(out_path: Path) -> Path:
    return out_path / "generations" / (out_path / "CURRENT").read_text(encoding="utf-8").strip()


def _read_model(out_path: Path) -> dict:
    return json.loads((_generation_dir(out_path) / "graph.json").read_text(encoding="utf-8"))


def _gather(db_path: Path, out_path: Path) -> builtin.ExportContext:
    store = EventStore(db_path)
    try:
        return gather_context(store, generation_dir=_generation_dir(out_path))
    finally:
        store.close()


# ---------------------------------------------------------- registry contract


def test_builtin_registry_covers_every_atm_format_and_analytics() -> None:
    registry = build_registry()
    assert set(registry.names(kind="format")) == EXPECTED_FORMAT_ARTIFACTS
    assert set(registry.names(kind="analytics")) == EXPECTED_ANALYTICS_ARTIFACTS
    for name in registry.names():
        spec = registry.get(name)
        assert spec.max_bytes >= 1
        assert spec.version >= 1


def test_registry_names_and_fingerprint_are_context_independent() -> None:
    bare = build_registry()
    ctx = ExportContext(
        timeline_records=({"event_seq": 1},),  # type: ignore[typeddict-item]
        evidence_counts={"N-P1": 3},
        current_source_versions=frozenset({"sv-test"}),
        store_watermark=7,
        manifest={"source_knowledge_high_watermark": 7},
    )
    with_ctx = build_registry(ctx)
    assert bare.names() == with_ctx.names()
    assert bare.fingerprint() == with_ctx.fingerprint()


def test_aliases_cover_the_whole_registry() -> None:
    aliased = set().union(*FORMAT_ALIASES.values()) | set().union(*ANALYTICS_ALIASES.values())
    registry = build_registry()
    assert aliased == set(registry.names())
    assert resolve_selection("all", kind="format", registry=registry) == registry.names(kind="format")
    assert resolve_selection("all", kind="analytics", registry=registry) == registry.names(kind="analytics")
    assert resolve_selection("dot", kind="format", registry=registry) == ("exports/graph.dot",)
    assert resolve_selection("book", kind="format", registry=registry) == tuple(
        sorted(EXPECTED_FORMAT_ARTIFACTS & set(FORMAT_ALIASES["book"]))
    )
    assert resolve_selection("exports/graph.dot", kind="format", registry=registry) == ("exports/graph.dot",)
    with pytest.raises(ValueError):
        resolve_selection("nope", kind="format", registry=registry)


# ------------------------------------------------------- end-to-end artifacts


def test_produce_all_artifacts_from_a_real_generation(tmp_path: Path) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    context = _gather(db_path, out_path)
    registry = build_registry(context)
    read_model = _read_model(out_path)
    artifacts = produce_exports(registry, read_model)
    assert set(artifacts) == set(registry.names())
    assert len(artifacts) == len(EXPECTED_FORMAT_ARTIFACTS | EXPECTED_ANALYTICS_ARTIFACTS)
    for name, data in artifacts.items():
        assert isinstance(data, bytes) and data, name
        spec = registry.get(name)
        assert len(data) <= spec.max_bytes, name
    entries = manifest_entries(registry, artifacts)
    assert set(entries) == set(artifacts)
    # byte-deterministic: a second production over the same inputs is identical
    again = produce_exports(build_registry(context), read_model)
    assert again == artifacts


def test_generation_diff_without_previous_generation_is_a_valid_result(tmp_path: Path) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    context = _gather(db_path, out_path)
    assert context.previous_graph is None  # exactly one generation exists
    artifacts = produce_exports(
        build_registry(context), _read_model(out_path), select=("analytics/generation-diff.json", "analytics/DIFF.md")
    )
    diff = json.loads(artifacts["analytics/generation-diff.json"])
    assert diff["status"] == "no_previous_generation"
    assert diff["summary"]["nodes_after"] == len(_read_model(out_path)["nodes"])
    markdown = artifacts["analytics/DIFF.md"].decode("utf-8")
    assert "no previous generation" in markdown


def test_generation_diff_with_two_generations_diffs_them(tmp_path: Path) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    store = EventStore(db_path)
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="N-P3",
            domain=PrimaryDomain.P3,
            node=_node("N-P3", PrimaryDomain.P3),
            source_version_id="sv-test",
        ),
        idempotency_key="test:upsert:N-P3",
    )
    GraphProjector(store, output_root=out_path).rebuild()
    context = gather_context(store, generation_dir=_generation_dir(out_path))
    store.close()
    assert context.previous_graph is not None
    artifacts = produce_exports(
        build_registry(context), _read_model(out_path), select=("analytics/generation-diff.json",)
    )
    diff = json.loads(artifacts["analytics/generation-diff.json"])
    assert diff["status"] == "diff"
    assert "N-P3" in diff["nodes_added"]
    assert diff["summary"]["nodes_added"] == 1


def test_timeline_is_built_from_the_store_event_log(tmp_path: Path) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    store = EventStore(db_path)
    try:
        context = gather_context(store, generation_dir=_generation_dir(out_path))
        domain_events = store.domain_event_count()
    finally:
        store.close()
    assert context.timeline_records, "expected the read-only store adapter to yield domain records"
    artifacts = produce_exports(
        build_registry(context), _read_model(out_path), select=("analytics/timeline.json", "analytics/TIMELINE.md")
    )
    timeline = json.loads(artifacts["analytics/timeline.json"])
    assert timeline["events_processed"] == domain_events
    by_node = {entry["node_id"]: entry for entry in timeline["nodes"]}
    assert by_node["N-P1"]["evidence_count"] == 1
    assert by_node["N-P1"]["points"], "evidence.append must emit a scored point"
    markdown = artifacts["analytics/TIMELINE.md"].decode("utf-8")
    assert markdown.startswith("# TIMELINE — P/M/E learning series")


def test_analytics_contents_are_real(tmp_path: Path) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    context = _gather(db_path, out_path)
    artifacts = produce_exports(
        build_registry(context), _read_model(out_path), select=build_registry().names(kind="analytics")
    )
    centrality = json.loads(artifacts["analytics/centrality.json"])
    assert centrality["kind"] == "analytics.centrality"
    communities = json.loads(artifacts["analytics/communities.json"])
    assert communities["community_count"] >= 1
    bridges = json.loads(artifacts["analytics/bridges.json"])
    assert bridges["schema_version"] == 1
    assert bridges["counts"]["candidates"] >= 1  # the N-P1 -> N-P2 cross-domain link
    health = json.loads(artifacts["analytics/health.json"])
    assert health["ok"] is True
    report = json.loads(artifacts["analytics/report-v2.json"])
    assert report["schema_version"] == report_v2.REPORT_SCHEMA_VERSION
    assert report["staleness"]["known"] is True
    assert report["staleness"]["stale"] is False
    signals = json.loads(artifacts["analytics/review-signals.json"])
    assert set(signals["signal_counts"]) >= {"low_confidence_edge", "isolated_node"}
    md = artifacts["analytics/graph-report-v2.md"].decode("utf-8")
    assert md.startswith("# SOUL Knowledge Graph Report (v2)")


def test_book_pages_validate_as_a_set(tmp_path: Path) -> None:
    from know_your_soul.exporters.book import validate_book

    db_path, out_path = _rebuilt(tmp_path)
    context = _gather(db_path, out_path)
    page_names = tuple(sorted(name for name in EXPECTED_FORMAT_ARTIFACTS if name.startswith("book/")))
    artifacts = produce_exports(build_registry(context), _read_model(out_path), select=page_names)
    assert set(artifacts) == set(page_names)  # fixed 8 pages even though only 3 domains hold nodes
    validate_book({name: data.decode("utf-8") for name, data in artifacts.items()})


# -------------------------------------------------------------------- CLI e2e


def _invoke(argv: list[str], capsys: pytest.CaptureFixture[str]) -> tuple[int, dict | None, dict | None]:
    code = main(argv)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    err = json.loads(captured.err) if captured.err.strip().startswith("{") else None
    return code, out, err


def test_cli_export_all_and_analytics_all(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    base = ["--store", str(db_path), "--output-root", str(out_path)]

    code, out, err = _invoke(base + ["export", "--formats", "all"], capsys)
    assert code == 0, err
    assert out is not None
    assert {entry["name"] for entry in out["artifacts"]} == EXPECTED_FORMAT_ARTIFACTS  # type: ignore[index]
    out_dir = Path(str(out["output_dir"]))
    for entry in out["artifacts"]:  # type: ignore[union-attr]
        assert (out_dir / entry["name"]).is_file()

    code, out, err = _invoke(base + ["analytics", "--modules", "all"], capsys)
    assert code == 0, err
    assert out is not None
    assert {entry["name"] for entry in out["artifacts"]} == EXPECTED_ANALYTICS_ARTIFACTS  # type: ignore[index]


def test_cli_export_accepts_short_aliases(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    base = ["--store", str(db_path), "--output-root", str(out_path)]
    code, out, err = _invoke(base + ["export", "--formats", "dot,book"], capsys)
    assert code == 0, err
    assert out is not None
    names = {entry["name"] for entry in out["artifacts"]}  # type: ignore[index]
    assert names == {"exports/graph.dot"} | {
        f"book/{page}.md" for page in ("index", "P1", "P2", "P3", "P4", "P5", "P6", "P7")
    }

    code, out, err = _invoke(base + ["analytics", "--modules", "timeline"], capsys)
    assert code == 0, err
    assert out is not None
    assert {entry["name"] for entry in out["artifacts"]} == {"analytics/timeline.json", "analytics/TIMELINE.md"}  # type: ignore[index]


def test_cli_rejects_unknown_alias(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    code, out, err = _invoke(
        ["--store", str(db_path), "--output-root", str(out_path), "export", "--formats", "nope"], capsys
    )
    assert code == 2
    assert err is not None and err["error"]["code"] == "CLI_USAGE"  # type: ignore[index]


def test_cli_export_is_byte_deterministic_across_invocations(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    db_path, out_path = _rebuilt(tmp_path)
    base = ["--store", str(db_path), "--output-root", str(out_path)]
    code, first, _ = _invoke(base + ["export", "--formats", "all"], capsys)
    assert code == 0
    code, second, _ = _invoke(base + ["export", "--formats", "all"], capsys)
    assert code == 0
    strip = lambda entries: [{k: v for k, v in entry.items() if k != "written"} for entry in entries]  # noqa: E731
    assert strip(first["artifacts"]) == strip(second["artifacts"])  # type: ignore[index]
    assert all(entry["written"] is False for entry in second["artifacts"])  # type: ignore[index]

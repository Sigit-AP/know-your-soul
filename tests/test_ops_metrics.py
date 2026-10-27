"""Tests for ``know_your_soul.ops.metrics`` (ATM wave-2 OPS).

Pins the operational metrics contract: store counts, audit-derived latency
summaries, artifact sizes from the active generation, the publish-capacity
position, and a deterministic weighted health score — collected read-only
(never a write to the store), rendered as a canonical dict and as markdown.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, cast

import pytest

from know_your_soul.domain import canonical_json
from know_your_soul.ops.metrics import (
    HEALTH_COMPONENT_WEIGHTS,
    METRICS_SCHEMA_VERSION,
    collect_ops_metrics,
    latency_summary,
    render_markdown,
)
from know_your_soul.projection import ARTIFACT_MAX_BYTES, GraphProjector
from know_your_soul.store import PUBLISH_CAPACITY_RATIO, EventStore

# ------------------------------------------------------------------ helpers


def _store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "metrics.sqlite3")


def _insert_journal(
    store: EventStore,
    invocation_id: str,
    operation: str,
    duration_ms: Any,
    *,
    status: str = "complete",
    outcome: dict[str, Any] | None = None,
) -> None:
    if outcome is None:
        outcome = {"status": "success", "duration_ms": duration_ms}
    store.execute_raw(
        "INSERT INTO invocation_journal (invocation_id, audit_event_id, operation, request_hash, actor, "
        "idempotency_key, started_at, status, completed_at, outcome_json) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        (
            invocation_id,
            f"audit-{invocation_id}",
            operation,
            "request-hash",
            "tester",
            None,
            "2026-01-01T00:00:00+00:00",
            status,
            "2026-01-01T00:00:01+00:00" if status == "complete" else None,
            json.dumps(outcome) if outcome is not None else None,
        ),
    )


def _projected(tmp_path: Path) -> tuple[EventStore, Path]:
    store = EventStore(tmp_path / "with-projection.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "proj")
    projector.rebuild()
    return store, tmp_path / "proj"


# ---------------------------------------------------------- latency_summary


def test_latency_summary_empty() -> None:
    summary = latency_summary(())
    assert summary == {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None}


def test_latency_summary_single_value() -> None:
    summary = latency_summary((7,))
    assert summary == {"count": 1, "min": 7, "max": 7, "mean": 7, "p50": 7, "p95": 7}


def test_latency_summary_mean_rounds_half_up() -> None:
    assert latency_summary((1, 2))["mean"] == 2  # 1.5 rounds up
    assert latency_summary((1, 1, 2))["mean"] == 1  # 1.33 rounds down
    assert latency_summary((0, 3))["mean"] == 2  # 1.5 rounds up


def test_latency_summary_percentiles_are_nearest_rank() -> None:
    values = tuple(range(1, 101))  # 1..100
    summary = latency_summary(values)
    assert summary["p50"] == 50
    assert summary["p95"] == 95
    assert summary["min"] == 1
    assert summary["max"] == 100


def test_latency_summary_unsorted_input_is_sorted() -> None:
    assert latency_summary((9, 1, 5)) == latency_summary((1, 5, 9))


def test_latency_summary_rejects_negative() -> None:
    with pytest.raises(ValueError):
        latency_summary((-1,))


def test_latency_summary_rejects_bool() -> None:
    with pytest.raises(ValueError):
        latency_summary((True,))


def test_latency_summary_rejects_float() -> None:
    with pytest.raises(ValueError):
        latency_summary(cast(Any, (1.5,)))


def test_latency_summary_deterministic() -> None:
    assert latency_summary((3, 1, 2)) == latency_summary((2, 3, 1))


# --------------------------------------------------------------- counts


def test_counts_on_empty_store(tmp_path: Path) -> None:
    store = _store(tmp_path)
    metrics = collect_ops_metrics(store)
    counts = metrics["counts"]
    assert counts["events_total"] == 0
    assert counts["domain_events"] == 0
    assert counts["audit_events"] == 0
    assert counts["evidence_items"] == 0
    assert counts["source_versions"] == 0
    assert counts["journals_in_progress"] == 0
    assert counts["journals_stale"] == 0
    assert counts["journals_complete"] == 0
    assert counts["knowledge_high_watermark"] == 0
    assert counts["event_high_watermark"] == 0


def test_counts_include_journals(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 5)
    _insert_journal(store, "inv-2", "knowledge.query", 9)
    _insert_journal(store, "inv-3", "knowledge.get_node", 3, status="in_progress")
    metrics = collect_ops_metrics(store)
    assert metrics["counts"]["journals_complete"] == 2
    assert metrics["counts"]["journals_in_progress"] == 1


def test_stale_journals_counted_with_threshold(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-old", "knowledge.query", 1, status="in_progress")
    metrics = collect_ops_metrics(store, stale_after_seconds=0.0)
    assert metrics["counts"]["journals_stale"] == 1
    fresh = collect_ops_metrics(store, stale_after_seconds=1e12)
    assert fresh["counts"]["journals_stale"] == 0


# --------------------------------------------------------------- latency


def test_latency_grouped_by_operation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 10)
    _insert_journal(store, "inv-2", "knowledge.query", 20)
    _insert_journal(store, "inv-3", "knowledge.get_node", 4)
    metrics = collect_ops_metrics(store)
    latency = metrics["latency_ms"]
    assert set(latency) == {"knowledge.get_node", "knowledge.query"}
    assert latency["knowledge.query"] == {"count": 2, "min": 10, "max": 20, "mean": 15, "p50": 10, "p95": 20}
    assert latency["knowledge.get_node"]["count"] == 1


def test_latency_excludes_in_progress(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 10, status="in_progress")
    metrics = collect_ops_metrics(store)
    assert metrics["latency_ms"] == {}


def test_latency_skips_malformed_outcomes_and_counts_them(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 7)
    _insert_journal(store, "inv-2", "knowledge.query", None, outcome={"status": "success"})
    _insert_journal(store, "inv-3", "knowledge.query", None, outcome={"duration_ms": -4})
    _insert_journal(store, "inv-4", "knowledge.query", None, outcome={"duration_ms": "soon"})
    _insert_journal(store, "inv-5", "knowledge.query", None, outcome={"duration_ms": 1.5})
    metrics = collect_ops_metrics(store)
    assert metrics["latency_ms"]["knowledge.query"] == {"count": 1, "min": 7, "max": 7, "mean": 7, "p50": 7, "p95": 7}
    assert metrics["counts"]["latency_rows_excluded"] == 4


def test_latency_tolerates_non_json_outcome(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.execute_raw(
        "INSERT INTO invocation_journal (invocation_id, audit_event_id, operation, request_hash, actor, "
        "idempotency_key, started_at, status, completed_at, outcome_json) "
        "VALUES ('inv-x', 'audit-inv-x', 'knowledge.query', 'h', 'tester', NULL, "
        "'2026-01-01T00:00:00+00:00', 'complete', '2026-01-01T00:00:01+00:00', 'not json')",
    )
    metrics = collect_ops_metrics(store)
    assert metrics["latency_ms"] == {}
    assert metrics["counts"]["latency_rows_excluded"] == 1


def test_latency_zero_duration_is_valid(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 0)
    assert collect_ops_metrics(store)["latency_ms"]["knowledge.query"]["count"] == 1


# -------------------------------------------------------- publish capacity


def test_publish_capacity_structure(tmp_path: Path) -> None:
    store = _store(tmp_path)
    capacity = collect_ops_metrics(store)["publish_capacity"]
    assert capacity["cap_bytes"] == ARTIFACT_MAX_BYTES
    assert capacity["guard_bytes"] == int(ARTIFACT_MAX_BYTES * PUBLISH_CAPACITY_RATIO)
    assert capacity["estimated_bytes"] == store.estimate_publish_bytes()
    assert capacity["headroom_bytes"] == capacity["guard_bytes"] - capacity["estimated_bytes"]
    assert capacity["ok"] is True
    assert 0 <= capacity["utilization_permille"] <= 1000


def test_publish_capacity_utilization_is_deterministic(tmp_path: Path) -> None:
    store = _store(tmp_path)
    first = collect_ops_metrics(store)["publish_capacity"]["utilization_permille"]
    second = collect_ops_metrics(store)["publish_capacity"]["utilization_permille"]
    assert first == second


# ------------------------------------------------------------- projection


def test_projection_is_none_without_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    assert collect_ops_metrics(store)["projection"] is None


def test_projection_metrics_from_real_generation(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    metrics = collect_ops_metrics(store, projection_root=root)
    projection = metrics["projection"]
    assert projection is not None
    assert projection["watermark"] == store.knowledge_high_watermark()
    assert projection["watermark_matches_store"] is True
    assert projection["node_count"] == 0
    assert projection["edge_count"] == 0
    artifacts = projection["artifacts"]
    assert artifacts["graph.json"] > 0
    assert artifacts["manifest.json"] > 0
    assert projection["total_bytes"] == sum(artifacts.values())
    assert projection["generation"]  # non-empty name


def test_projection_artifact_sizes_match_disk(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    projection = collect_ops_metrics(store, projection_root=root)["projection"]
    generation = root / "generations" / projection["generation"]
    for name, size in projection["artifacts"].items():
        assert (generation / name).stat().st_size == size


def test_projection_missing_current_reports_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    projection = collect_ops_metrics(store, projection_root=root)["projection"]
    assert projection is not None
    assert projection["present"] is False


def test_projection_watermark_mismatch_detected(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    manifest_path = generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_knowledge_high_watermark"] = 99
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    projection = collect_ops_metrics(store, projection_root=root)["projection"]
    assert projection["watermark"] == 99
    assert projection["watermark_matches_store"] is False


# ----------------------------------------------------------- health score


def test_health_weights_sum_to_hundred() -> None:
    assert sum(HEALTH_COMPONENT_WEIGHTS.values()) == 100


def test_health_perfect_on_fresh_projected_store(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    health = collect_ops_metrics(store, projection_root=root)["health"]
    assert health["score"] == 100
    assert health["max_score"] == 100
    assert all(component["ok"] for component in health["components"].values())


def test_health_without_projection_root_excludes_projection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    health = collect_ops_metrics(store)["health"]
    projection_weight = sum(HEALTH_COMPONENT_WEIGHTS[k] for k in ("projection_present", "projection_watermark"))
    assert health["max_score"] == 100 - projection_weight
    assert "projection_present" not in health["components"]
    assert health["score"] == health["max_score"]


def test_health_drops_points_for_missing_projection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    health = collect_ops_metrics(store, projection_root=root)["health"]
    assert health["components"]["projection_present"]["ok"] is False
    assert health["components"]["projection_watermark"]["ok"] is False
    projection_weight = sum(HEALTH_COMPONENT_WEIGHTS[k] for k in ("projection_present", "projection_watermark"))
    assert health["score"] == 100 - projection_weight


def test_health_score_is_deterministic(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    first = collect_ops_metrics(store, projection_root=root)["health"]
    assert first == collect_ops_metrics(store, projection_root=root)["health"]


# -------------------------------------------------------------- rendering


def test_render_markdown_contains_sections(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 12)
    text = render_markdown(collect_ops_metrics(store, projection_root=root))
    assert "# Operational Metrics" in text
    assert "## Counts" in text
    assert "## Latency" in text
    assert "## Publish Capacity" in text
    assert "## Health" in text
    assert "## Projection" in text
    assert "knowledge.query" in text
    assert "100/100" in text


def test_render_markdown_without_projection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    text = render_markdown(collect_ops_metrics(store))
    assert "## Projection" not in text


def test_render_markdown_is_deterministic(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 12)
    assert render_markdown(collect_ops_metrics(store, projection_root=root)) == render_markdown(
        collect_ops_metrics(store, projection_root=root)
    )


# -------------------------------------------------------------- canonicity


def test_metrics_are_canonical_json_serializable_without_floats(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 12)
    metrics = collect_ops_metrics(store, projection_root=root)

    def _walk(value: Any) -> None:
        if isinstance(value, float):
            raise AssertionError(f"float leaked into metrics: {value}")
        if isinstance(value, dict):
            for key, item in value.items():
                assert isinstance(key, str)
                _walk(item)
        elif isinstance(value, (list, tuple)):
            for item in value:
                _walk(item)

    _walk(metrics)
    text = canonical_json(metrics)
    assert json.loads(text) == metrics
    assert metrics["schema_version"] == METRICS_SCHEMA_VERSION


def test_metrics_do_not_write_to_store(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    before = (store.event_count(), store.audit_event_count(), store.event_high_watermark())
    collect_ops_metrics(store, projection_root=root)
    after = (store.event_count(), store.audit_event_count(), store.event_high_watermark())
    assert before == after


def test_collection_is_repeatable_byte_identical(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 12)
    _insert_journal(store, "inv-2", "knowledge.query", 30)
    assert canonical_json(collect_ops_metrics(store, projection_root=root)) == canonical_json(
        collect_ops_metrics(store, projection_root=root)
    )


# ------------------------------------------------------------------ hardening


def test_counts_with_real_events(tmp_path: Path) -> None:
    store = _store(tmp_path)
    with store.transaction() as conn:
        store.insert_event(
            conn,
            event_id="e" * 64,
            kind="audit",
            event_type="audit.test",
            knowledge_affecting=False,
            actor="tester",
            occurred_at="2026-01-01T00:00:00+00:00",
            payload={"probe": True},
        )
    metrics = collect_ops_metrics(store)
    assert metrics["counts"]["events_total"] == 1
    assert metrics["counts"]["audit_events"] == 1
    assert metrics["counts"]["event_high_watermark"] == 1
    assert store.verify_chain().ok


def test_projection_invalid_pointer_name_reports_absent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    (root / "generations").mkdir(parents=True)
    (root / "CURRENT").write_text("bogus\n", encoding="utf-8")
    assert collect_ops_metrics(store, projection_root=root)["projection"] == {"present": False}


def test_projection_unreadable_manifest_reports_absent(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    (generation_dir / "manifest.json").write_text("{ nope", encoding="utf-8")
    assert collect_ops_metrics(store, projection_root=root)["projection"] == {"present": False}


def test_health_chain_failure_drops_score(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _BrokenChain:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def verify_chain(self) -> Any:
            from know_your_soul.store import ChainReport

            return ChainReport(ok=False, checked=1, first_bad_seq=1)

    metrics = collect_ops_metrics(cast(EventStore, _BrokenChain(store)))
    health = metrics["health"]
    assert health["components"]["hash_chain"]["ok"] is False
    assert health["score"] == health["max_score"] - HEALTH_COMPONENT_WEIGHTS["hash_chain"]


def test_health_migrations_drift_drops_score(tmp_path: Path) -> None:
    store = _store(tmp_path)
    empty_migrations = tmp_path / "empty-migrations"
    empty_migrations.mkdir()
    health = collect_ops_metrics(store, migrations_dir=empty_migrations)["health"]
    assert health["components"]["migrations"]["ok"] is False


def test_render_markdown_empty_latency_text(tmp_path: Path) -> None:
    store = _store(tmp_path)
    text = render_markdown(collect_ops_metrics(store))
    assert "No completed invocations recorded yet." in text


def test_render_markdown_orders_operations(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 10)
    _insert_journal(store, "inv-2", "knowledge.get_node", 4)
    text = render_markdown(collect_ops_metrics(store))
    assert text.index("knowledge.get_node") < text.index("knowledge.query")


def test_counts_latency_excluded_zero_by_default(tmp_path: Path) -> None:
    store = _store(tmp_path)
    _insert_journal(store, "inv-1", "knowledge.query", 5)
    assert collect_ops_metrics(store)["counts"]["latency_rows_excluded"] == 0


def test_projection_pointer_names_missing_generation(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    (root / "generations").mkdir(parents=True)
    (root / "CURRENT").write_text("a" * 64 + "\n", encoding="utf-8")
    assert collect_ops_metrics(store, projection_root=root)["projection"] == {"present": False}


def test_projection_manifest_non_object_reports_absent(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    (generation_dir / "manifest.json").write_text("[1]", encoding="utf-8")
    assert collect_ops_metrics(store, projection_root=root)["projection"] == {"present": False}


def test_health_store_select_exception_is_contained(tmp_path: Path) -> None:
    import contextlib

    store = _store(tmp_path)

    class _ConnProxy:
        def __init__(self, conn: Any) -> None:
            self._conn = conn

        def execute(self, sql: str, parameters: Any = ()) -> Any:
            if sql == "SELECT 1":
                raise RuntimeError("simulated select failure")
            return self._conn.execute(sql, parameters)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._conn, name)

    class _SelectBrokenStore:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        @contextlib.contextmanager
        def read(self) -> Any:
            with self._inner.read() as conn:
                yield _ConnProxy(conn)

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    metrics = collect_ops_metrics(cast(EventStore, _SelectBrokenStore(store)))
    assert metrics["health"]["components"]["store_select"]["ok"] is False


def test_health_chain_and_migration_exceptions_are_contained(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _FlakyStore:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        def verify_chain(self) -> Any:
            raise RuntimeError("simulated chain failure")

        def applied_migrations(self) -> Any:
            raise RuntimeError("simulated migration failure")

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    health = collect_ops_metrics(cast(EventStore, _FlakyStore(store)))["health"]
    assert health["components"]["hash_chain"]["ok"] is False
    assert health["components"]["migrations"]["ok"] is False


def test_render_markdown_absent_projection(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    text = render_markdown(collect_ops_metrics(store, projection_root=root))
    assert "No active projection." in text

"""Schema v3 (Universal Knowledge Graph) migration and query-helper tests.

W1 of ``.superpowers/sdd/2026-10-27-universal-knowledge-graph/MASTER-PLAN.md``;
DDL patterns adopted from codegraph (``RESEARCH-STORAGE.md``): a versioned
``schema_versions`` table, expression index on ``lower(name)``, partial indexes
on unresolved-ref status, and a unique edge-identity index folding NULL
line/col via ``IFNULL(..., -1)``.

Every test drives real SQLite state through the public store API. The
v2-fixture tests build a *genuine* pre-v3 store (same migrations 0001+0002,
same public write API) and then reopen it so the real migration path runs —
no hand-crafted DDL stand-ins.
"""

from __future__ import annotations

import hashlib
import itertools
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

import know_your_soul.store as store_module
from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from know_your_soul.store import (
    EventStore,
    MigrationError,
    SchemaStateError,
    SchemaVersionTooNew,
    UnresolvedRefError,
)

# Hypothesis reuses a function-scoped tmp_path across generated examples, so
# every example gets its own database file under it.
_EXAMPLE_COUNTER = itertools.count()


def _example_db(tmp_path: Path) -> Path:
    return tmp_path / f"soul-{next(_EXAMPLE_COUNTER)}.sqlite3"


NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
SEEN = "2026-10-26T00:00:00+00:00"

V3_TABLES = ("schema_versions", "files", "symbols", "unresolved_refs", "graph_edges")


# ------------------------------------------------------------------ builders


def build_node(node_id: str, **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": node_id},
        "summary": f"node {node_id}",
        "provenance": ("soul/01-identity-compliance.md",),
        "context_tags": ("identity",),
        "scope": "global",
        "created_at": NOW,
        "updated_at": NOW,
        "professional_level": 1,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def node_event(node: Node) -> Event:
    return Event.model_validate(
        {
            "kind": EventKind.NODE_UPSERT,
            "node_id": node.node_id,
            "domain": node.domain,
            "node": node,
            "source_version_id": node.source_version_id,
            "occurred_at": NOW,
        }
    )


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def dump_all_tables(store: EventStore) -> dict[str, list[tuple[object, ...]]]:
    """Full ordered dump of every table — the byte-level migration witness."""
    with store.read() as conn:
        names = [
            str(row["name"])
            for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'table' ORDER BY name")
        ]
        return {
            name: [tuple(row) for row in conn.execute(f"SELECT * FROM {name} ORDER BY rowid")]  # noqa: S608
            for name in names
        }


def dump_v3_tables(store: EventStore) -> dict[str, list[tuple[object, ...]]]:
    # schema_versions carries wall-clock applied_at by design; byte determinism
    # is asserted over the data tables only.
    full = dump_all_tables(store)
    return {name: full[name] for name in ("files", "symbols", "unresolved_refs", "graph_edges")}


def open_v2_store(path: Path, monkeypatch: pytest.MonkeyPatch) -> EventStore:
    """Open a store capped at schema v2 — a genuine pre-v3 database."""
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 2)
    return EventStore(path)


# ------------------------------------------------------------- migration DDL


def test_v3_migration_creates_the_new_tables(store: EventStore) -> None:
    tables = store.table_names()
    for name in V3_TABLES:
        assert name in tables, name


def test_v3_migration_records_schema_versions(store: EventStore) -> None:
    with store.read() as conn:
        rows = conn.execute("SELECT version, description FROM schema_versions ORDER BY version").fetchall()
    assert [int(row["version"]) for row in rows] == [1, 2, 3]
    assert all(str(row["description"]) for row in rows)
    # The file-based ledger is untouched and still the authority for v1/v2.
    assert store.applied_migrations() == [1, 2]


def test_v3_migration_is_idempotent_across_reopens(tmp_path: Path) -> None:
    path = tmp_path / "soul.sqlite3"
    first = EventStore(path)
    first.upsert_file(
        path="src/a.py",
        sha256="aa",
        size=10,
        mtime=1,
        language="python",
        generated_hint=False,
        extraction_version=1,
    )
    first.close()
    second = EventStore(path)
    try:
        with second.read() as conn:
            versions = [int(r["version"]) for r in conn.execute("SELECT version FROM schema_versions")]
            assert sorted(versions) == [1, 2, 3]
            assert conn.execute("SELECT COUNT(*) AS n FROM files").fetchone()["n"] == 1
        assert second.get_file("src/a.py") is not None
    finally:
        second.close()


def test_v3_expected_indexes_exist(store: EventStore) -> None:
    with store.read() as conn:
        indexes = {str(row["name"]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type = 'index'")}
    for name in (
        "idx_symbols_lower_name",
        "idx_symbols_path",
        "idx_unresolved_pending",
        "idx_unresolved_failed_tail",
        "idx_unresolved_source",
        "idx_graph_edges_identity",
        "idx_graph_edges_source_kind",
        "idx_graph_edges_target_kind",
        "idx_files_language",
        "idx_files_generated",
    ):
        assert name in indexes, name


def test_migration_fails_closed_on_newer_schema(tmp_path: Path) -> None:
    path = tmp_path / "soul.sqlite3"
    EventStore(path).close()
    raw = sqlite3.connect(path)
    raw.execute("INSERT INTO schema_versions (version, applied_at, description) VALUES (99, 'x', 'future')")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaVersionTooNew):
        EventStore(path)


def test_migration_fails_closed_when_tables_exist_without_version_record(tmp_path: Path) -> None:
    path = tmp_path / "soul.sqlite3"
    EventStore(path).close()
    raw = sqlite3.connect(path)
    raw.execute("DELETE FROM schema_versions WHERE version = 3")
    raw.commit()
    raw.close()
    with pytest.raises(SchemaStateError):
        EventStore(path)


def test_migration_is_transactional_and_rolls_back_on_failure(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "soul.sqlite3"
    legacy = open_v2_store(path, monkeypatch)
    legacy.append_domain(node_event(build_node("n-1")), idempotency_key="k-1")
    before = dump_all_tables(legacy)
    legacy.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 3)

    broken = tuple(store_module._SCHEMA_V3_STATEMENTS) + ("CREATE TABLE broken (",)
    monkeypatch.setattr(store_module, "_SCHEMA_V3_STATEMENTS", broken)
    with pytest.raises(MigrationError):
        EventStore(path)

    monkeypatch.undo()
    # The failed attempt left no trace: no v3 tables, no schema_versions row,
    # and every pre-v3 byte is intact. A clean retry succeeds.
    recovered = EventStore(path)
    try:
        after = dump_all_tables(recovered)
        for name in V3_TABLES:
            assert name in after, name
        for name, rows in before.items():
            assert after[name] == rows, name
        assert recovered.verify_chain().ok
    finally:
        recovered.close()


# ------------------------------------------------------- v2 -> v3 no-loss


def _populate_v2_store(store: EventStore, node_count: int) -> None:
    for index in range(node_count):
        store.append_domain(node_event(build_node(f"n-{index}")), idempotency_key=f"key-{index}")


def test_real_v2_store_migrates_without_losing_a_single_byte(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    path = tmp_path / "soul.sqlite3"
    legacy = open_v2_store(path, monkeypatch)
    _populate_v2_store(legacy, 5)
    assert legacy.verify_chain().ok
    before = dump_all_tables(legacy)
    assert set(before).isdisjoint(V3_TABLES)
    legacy.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 3)

    migrated = EventStore(path)
    try:
        after = dump_all_tables(migrated)
        assert set(after) == set(before) | set(V3_TABLES)
        for name, rows in before.items():
            assert after[name] == rows, f"table {name} changed during migration"
        assert migrated.verify_chain().ok
        # Post-migration the store is fully writable.
        migrated.append_domain(node_event(build_node("n-post")), idempotency_key="key-post")
        assert migrated.verify_chain().ok
    finally:
        migrated.close()


@given(node_count=st.integers(min_value=0, max_value=8))
@settings(max_examples=12, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_v2_migration_preserves_every_row(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, node_count: int
) -> None:
    path = _example_db(tmp_path)
    legacy = open_v2_store(path, monkeypatch)
    _populate_v2_store(legacy, node_count)
    before = dump_all_tables(legacy)
    legacy.close()
    monkeypatch.setattr(store_module, "SCHEMA_VERSION", 3)

    migrated = EventStore(path)
    try:
        after = dump_all_tables(migrated)
        for name, rows in before.items():
            assert after[name] == rows, name
    finally:
        migrated.close()


# --------------------------------------------------------------------- files


def _file(store: EventStore, path: str, sha256: str = "aa", size: int = 10, mtime: int = 1) -> None:
    store.upsert_file(
        path=path,
        sha256=sha256,
        size=size,
        mtime=mtime,
        language="python",
        generated_hint=False,
        extraction_version=1,
    )


def test_file_upsert_insert_then_update(store: EventStore) -> None:
    _file(store, "src/a.py", sha256="aa", size=10, mtime=1)
    row = store.get_file("src/a.py")
    assert row is not None
    assert row.sha256 == "aa" and row.size == 10 and row.mtime == 1
    _file(store, "src/a.py", sha256="bb", size=12, mtime=2)
    row = store.get_file("src/a.py")
    assert row is not None
    assert (row.sha256, row.size, row.mtime) == ("bb", 12, 2)
    assert store.get_file("src/missing.py") is None


def test_files_listing_is_deterministic(store: EventStore) -> None:
    for path in ("src/c.py", "src/a.py", "src/b.py"):
        _file(store, path)
    assert [row.path for row in store.list_files()] == ["src/a.py", "src/b.py", "src/c.py"]


# ------------------------------------------------------------------- symbols


def test_symbol_roundtrip_and_case_insensitive_lookup(store: EventStore) -> None:
    store.upsert_symbol(
        node_id="function:abc", name="CalculateTotal", kind="function", path="src/math.py", line=10, col=4
    )
    exact = store.symbols_named("CalculateTotal")
    assert [s.node_id for s in exact] == ["function:abc"]
    insensitive = store.symbols_named("calculatetotal", case_insensitive=True)
    assert [s.node_id for s in insensitive] == ["function:abc"]
    assert store.symbols_named("calculatetotal") == []
    by_node = store.symbol_by_node("function:abc")
    assert by_node is not None and (by_node.line, by_node.col) == (10, 4)
    assert store.symbol_by_node("function:missing") is None


def test_symbols_in_path_ordered_by_line(store: EventStore) -> None:
    store.upsert_symbol(node_id="function:b", name="b", kind="function", path="src/a.py", line=20, col=-1)
    store.upsert_symbol(node_id="function:a", name="a", kind="function", path="src/a.py", line=10, col=-1)
    assert [s.name for s in store.symbols_in_path("src/a.py")] == ["a", "b"]


def test_case_insensitive_lookup_uses_the_expression_index(store: EventStore) -> None:
    store.upsert_symbol(node_id="function:abc", name="Foo", kind="function", path="a.py", line=1, col=-1)
    with store.read() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM symbols WHERE lower(name) = lower(?)", ("foo",)
        ).fetchall()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "idx_symbols_lower_name" in detail, detail


# --------------------------------------------------------------- graph edges


def test_graph_edge_identity_dedups_with_null_line(store: EventStore) -> None:
    store.add_graph_edge(source="file:a", target="class:b", kind="contains")
    store.add_graph_edge(source="file:a", target="class:b", kind="contains")
    store.add_graph_edge(source="file:a", target="class:b", kind="contains", line=-1, col=-1)
    # NULL line/col folds to -1 (codegraph IFNULL identity): still one edge.
    store.add_graph_edge(source="file:a", target="class:b", kind="contains", line=5, col=1)
    edges = store.outgoing_edges("file:a")
    assert len(edges) == 2


def test_adjacency_order_is_deterministic_and_kind_filtered(store: EventStore) -> None:
    store.add_graph_edge(source="f", target="t2", kind="contains", line=3)
    store.add_graph_edge(source="f", target="t1", kind="contains", line=2)
    store.add_graph_edge(source="f", target="t0", kind="calls", line=1)
    store.add_graph_edge(source="other", target="f", kind="contains")
    outgoing = store.outgoing_edges("f")
    assert [(e.kind, e.target) for e in outgoing] == [("calls", "t0"), ("contains", "t1"), ("contains", "t2")]
    assert [e.target for e in store.outgoing_edges("f", kinds=("contains",))] == ["t1", "t2"]
    assert [e.source for e in store.incoming_edges("f")] == ["other"]
    assert store.outgoing_edges("missing") == []


def test_subtree_bfs_is_depth_bounded_and_cycle_safe(store: EventStore) -> None:
    edges = [
        ("a", "b", "contains"),
        ("a", "c", "contains"),
        ("b", "d", "contains"),
        ("d", "e", "contains"),
        ("e", "a", "contains"),  # cycle back to the root
        ("a", "x", "calls"),  # non-contains edge must not be followed
    ]
    store.add_graph_edges(
        [
            {"source": s, "target": t, "kind": k, "line": None, "col": None, "metadata": None, "provenance": None}
            for s, t, k in edges
        ]
    )
    assert store.subtree("a", max_depth=0) == [("a", 0)]
    assert store.subtree("a", max_depth=1) == [("a", 0), ("b", 1), ("c", 1)]
    assert store.subtree("a", max_depth=2) == [("a", 0), ("b", 1), ("c", 1), ("d", 2)]
    assert store.subtree("a", max_depth=99) == [("a", 0), ("b", 1), ("c", 1), ("d", 2), ("e", 3)]


def test_subtree_follows_requested_kinds(store: EventStore) -> None:
    store.add_graph_edges(
        [
            {
                "source": "a",
                "target": "b",
                "kind": "contains",
                "line": None,
                "col": None,
                "metadata": None,
                "provenance": None,
            },
            {
                "source": "a",
                "target": "c",
                "kind": "bridges",
                "line": None,
                "col": None,
                "metadata": None,
                "provenance": None,
            },
            {
                "source": "c",
                "target": "d",
                "kind": "bridges",
                "line": None,
                "col": None,
                "metadata": None,
                "provenance": None,
            },
        ]
    )
    assert store.subtree("a", max_depth=5, kinds=("bridges",)) == [("a", 0), ("c", 1), ("d", 2)]


# ----------------------------------------------------------- unresolved refs


def test_unresolved_ref_full_lifecycle(store: EventStore) -> None:
    ref_id = store.insert_unresolved_ref(
        source_node_id="function:a", target_name="pkg.mod.CalculateTotal", kind="calls", first_seen=SEEN
    )
    pending = store.pending_unresolved_refs()
    assert [r.ref_id for r in pending] == [ref_id]
    ref = pending[0]
    assert ref.status == "pending"
    assert ref.name_tail == "CalculateTotal"
    assert ref.attempts == 0

    failed = store.record_resolution_attempt(ref_id, outcome="failed")
    assert failed.status == "failed" and failed.attempts == 1
    assert store.pending_unresolved_refs() == []
    assert [r.ref_id for r in store.failed_unresolved_refs(name_tail="CalculateTotal")] == [ref_id]

    retried = store.retry_failed_ref(ref_id)
    assert retried.status == "pending" and retried.attempts == 1

    resolved = store.record_resolution_attempt(ref_id, outcome="resolved")
    assert resolved.status == "resolved" and resolved.attempts == 2
    assert store.pending_unresolved_refs() == []


def test_unresolved_ref_explicit_name_tail_is_kept(store: EventStore) -> None:
    ref_id = store.insert_unresolved_ref(
        source_node_id="function:a",
        target_name="weird",
        kind="imports",
        name_tail="module:pkg/mod",
        first_seen=SEEN,
    )
    ref = store.unresolved_ref(ref_id)
    assert ref is not None and ref.name_tail == "module:pkg/mod"


def test_unresolved_ref_fail_closed_errors(store: EventStore) -> None:
    with pytest.raises(UnresolvedRefError):
        store.record_resolution_attempt(999, outcome="resolved")
    with pytest.raises(UnresolvedRefError):
        store.retry_failed_ref(999)
    ref_id = store.insert_unresolved_ref(source_node_id="function:a", target_name="x.y", kind="calls", first_seen=SEEN)
    with pytest.raises(UnresolvedRefError):
        store.retry_failed_ref(ref_id)  # pending, not failed
    store.record_resolution_attempt(ref_id, outcome="resolved")
    with pytest.raises(UnresolvedRefError):
        store.record_resolution_attempt(ref_id, outcome="failed")  # resolved is terminal


def test_pending_refs_query_uses_the_partial_index(store: EventStore) -> None:
    store.insert_unresolved_ref(source_node_id="a", target_name="x", kind="calls", first_seen=SEEN)
    with store.read() as conn:
        plan = conn.execute(
            "EXPLAIN QUERY PLAN SELECT * FROM unresolved_refs WHERE status = 'pending' ORDER BY first_seen, ref_id"
        ).fetchall()
    detail = " ".join(str(row["detail"]) for row in plan)
    assert "idx_unresolved_pending" in detail, detail


# Lone surrogates (category Cs) are not valid UTF-8 — real source text never
# carries them and the store refuses them fail-closed, so they are excluded.
@given(
    name=st.text(
        alphabet=st.characters(blacklist_characters="\x00", blacklist_categories=("Cs",)), min_size=1, max_size=40
    )
)
@settings(max_examples=50, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_name_tail_is_always_the_last_dot_segment(tmp_path: Path, name: str) -> None:
    instance = EventStore(_example_db(tmp_path))
    try:
        ref_id = instance.insert_unresolved_ref(source_node_id="a", target_name=name, kind="calls", first_seen=SEEN)
        ref = instance.unresolved_ref(ref_id)
        assert ref is not None
        assert ref.name_tail == name.rsplit(".", 1)[-1]
        assert ref.attempts == 0 and ref.status == "pending"
    finally:
        instance.close()


def test_unresolved_ref_rejects_non_utf8_names(store: EventStore) -> None:
    with pytest.raises(UnresolvedRefError):
        store.insert_unresolved_ref(source_node_id="a", target_name="\ud800", kind="calls", first_seen=SEEN)


@given(steps=st.lists(st.booleans(), min_size=1, max_size=6))
@settings(max_examples=20, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
def test_property_attempts_increment_monotonically(tmp_path: Path, steps: list[bool]) -> None:
    """attempts only ever grows by one per resolution attempt; retries never move it."""
    instance = EventStore(_example_db(tmp_path))
    try:
        ref_id = instance.insert_unresolved_ref(source_node_id="a", target_name="m.n", kind="calls", first_seen=SEEN)
        expected_attempts = 0
        status = "pending"
        for _step in steps:
            if status == "pending":
                ref = instance.record_resolution_attempt(ref_id, outcome="failed")
                expected_attempts += 1
                status = "failed"
            else:
                ref = instance.retry_failed_ref(ref_id)
                status = "pending"
            assert ref.attempts == expected_attempts
            assert ref.status == status
    finally:
        instance.close()


# ------------------------------------------------------------- determinism


def _exercise_v3(store: EventStore) -> None:
    _file(store, "src/a.py", sha256=hashlib.sha256(b"a").hexdigest(), size=1, mtime=10)
    _file(store, "src/b.py", sha256=hashlib.sha256(b"b").hexdigest(), size=2, mtime=20)
    store.upsert_symbol(node_id="function:1", name="alpha", kind="function", path="src/a.py", line=1, col=0)
    store.upsert_symbol(node_id="function:2", name="beta", kind="function", path="src/b.py", line=5, col=2)
    store.add_graph_edge(source="src/a.py", target="function:1", kind="contains", line=1, col=0)
    store.add_graph_edge(source="src/b.py", target="function:2", kind="contains", line=5, col=2)
    ref_id = store.insert_unresolved_ref(
        source_node_id="function:1", target_name="mod.beta", kind="calls", first_seen=SEEN
    )
    store.record_resolution_attempt(ref_id, outcome="failed")


def test_v3_writes_are_byte_deterministic_across_stores(tmp_path: Path) -> None:
    first = EventStore(tmp_path / "one.sqlite3")
    second = EventStore(tmp_path / "two.sqlite3")
    try:
        _exercise_v3(first)
        _exercise_v3(second)
        assert dump_v3_tables(first) == dump_v3_tables(second)
    finally:
        first.close()
        second.close()

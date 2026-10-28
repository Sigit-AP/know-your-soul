"""Property tests for the two-phase reference resolver (wave W2).

Invariants under test, over randomized-but-seeded (``derandomize=True``)
scenarios:

1. **Determinism across insert order** — shuffling the ref-insertion order
   changes ref_ids but never the resolved state: the multiset of
   ``(source_node_id, target_name, kind, status, attempts)`` and the multiset
   of ``(source, target, kind)`` edges are identical.
2. **Idempotency** — a second ``resolve_pending`` run changes nothing
   (byte-level dump of ``unresolved_refs`` + ``graph_edges``).
3. **Referential invariant** — every resolved ref produced an edge whose
   source and target both exist (symbol node or tracked file node).
4. **Report composition** — ``processed == resolved + failed`` and
   ``pending_remaining`` matches the store's queue length.
5. **Cross-instance determinism** — two independently built identical stores
   yield identical reports and identical dumps.
6. **Retry accounting** — resurrect + re-resolve increments ``attempts`` by
   exactly one per recorded attempt; resolved refs stay terminal.
"""

from __future__ import annotations

import hashlib
import itertools
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from know_your_soul.resolve import ReferenceResolver
from know_your_soul.store import EventStore

SEEN = "2026-10-26T00:00:00+00:00"

FILES: tuple[tuple[str, str], ...] = (
    ("pkg/__init__.py", "python"),
    ("pkg/mod.py", "python"),
    ("pkg/main.py", "python"),
    ("src/a.ts", "typescript"),
    ("src/util.ts", "typescript"),
)

SYMBOL_NAMES = ("alpha", "beta", "gamma", "mod.alpha", "Class.beta", "Class::gamma")

REF_TARGETS = (
    "alpha",
    "beta",
    "mod.alpha",
    "pkg.mod.alpha",
    "./util",
    "./ghost",
    "lodash",
    "unknown.thing",
)

REF_KINDS = ("calls", "imports", "function_ref")

Scenario = tuple[list[tuple[str, str, str, int]], list[tuple[str, str, str]], tuple[int, ...]]

_EXAMPLE_COUNTER = itertools.count()


def _example_db(tmp_path: Path) -> Path:
    return tmp_path / f"soul-{next(_EXAMPLE_COUNTER)}.sqlite3"


@st.composite
def scenarios(draw: st.DrawFn) -> Scenario:
    """(symbols, refs, insertion_order)."""
    n_symbols = draw(st.integers(min_value=1, max_value=5))
    symbols: list[tuple[str, str, str, int]] = []
    used: set[tuple[str, int]] = set()
    for index in range(n_symbols):
        path = draw(st.sampled_from([path for path, _ in FILES]))
        name = draw(st.sampled_from(SYMBOL_NAMES))
        line = draw(st.integers(min_value=1, max_value=40))
        if (path, line) in used:
            continue
        used.add((path, line))
        symbols.append((f"function:s{index}", name, path, line))
    assume_symbols = symbols or [("function:s0", "alpha", "pkg/mod.py", 1)]
    sources = [node_id for node_id, _, _, _ in assume_symbols] + ["file:src/a.ts", "file:pkg/mod.py"]
    n_refs = draw(st.integers(min_value=0, max_value=8))
    refs: list[tuple[str, str, str]] = []
    for _ in range(n_refs):
        refs.append(
            (
                draw(st.sampled_from(sources)),
                draw(st.sampled_from(REF_TARGETS)),
                draw(st.sampled_from(REF_KINDS)),
            )
        )
    order = tuple(draw(st.permutations(list(range(len(refs)))))) if refs else ()
    return assume_symbols, refs, order


def build_store(db_path: Path, scenario: Scenario) -> EventStore:
    symbols, refs, order = scenario
    store = EventStore(db_path)
    for path, language in FILES:
        store.upsert_file(
            path=path,
            sha256=hashlib.sha256(path.encode()).hexdigest(),
            size=10,
            mtime=100,
            language=language,
            generated_hint=False,
            extraction_version=1,
        )
    for node_id, name, path, line in symbols:
        store.upsert_symbol(node_id=node_id, name=name, kind="function", path=path, line=line)
    for ref_index in order:
        source, target, kind = refs[ref_index]
        store.insert_unresolved_ref(source_node_id=source, target_name=target, kind=kind, first_seen=SEEN)
    return store


def ref_multiset(store: EventStore) -> list[tuple[object, ...]]:
    with store.read() as conn:
        rows = conn.execute(
            "SELECT source_node_id, target_name, name_tail, kind, status, attempts FROM unresolved_refs"
        ).fetchall()
    return sorted(tuple(row) for row in rows)


def edge_multiset(store: EventStore) -> list[tuple[object, ...]]:
    with store.read() as conn:
        rows = conn.execute("SELECT source, target, kind FROM graph_edges").fetchall()
    return sorted(tuple(row) for row in rows)


def full_dump(store: EventStore) -> tuple[list[tuple[object, ...]], list[tuple[object, ...]]]:
    with store.read() as conn:
        refs = [tuple(row) for row in conn.execute("SELECT * FROM unresolved_refs ORDER BY ref_id")]
        edges = [tuple(row) for row in conn.execute("SELECT * FROM graph_edges ORDER BY edge_id")]
    return refs, edges


def known_node_ids(store: EventStore) -> set[str]:
    with store.read() as conn:
        symbol_ids = {str(row["node_id"]) for row in conn.execute("SELECT node_id FROM symbols")}
        file_ids = {f"file:{row['path']}" for row in conn.execute("SELECT path FROM files")}
    return symbol_ids | file_ids


SETTINGS = settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,  # each example gets its own db file via _example_db
    ],
)


class TestDeterminismAcrossInsertOrder:
    @given(scenario=scenarios())
    @SETTINGS
    def test_shuffled_inserts_same_outcome(self, tmp_path: Path, scenario: Scenario) -> None:
        symbols, refs, order = scenario
        reversed_order = tuple(reversed(order))
        store_a = build_store(_example_db(tmp_path), (symbols, refs, order))
        store_b = build_store(_example_db(tmp_path), (symbols, refs, reversed_order))
        try:
            ReferenceResolver().resolve_pending(store_a)
            ReferenceResolver().resolve_pending(store_b)
            assert ref_multiset(store_a) == ref_multiset(store_b)
            assert edge_multiset(store_a) == edge_multiset(store_b)
        finally:
            store_a.close()
            store_b.close()


class TestIdempotency:
    @given(scenario=scenarios())
    @SETTINGS
    def test_second_run_changes_nothing(self, tmp_path: Path, scenario: Scenario) -> None:
        store = build_store(_example_db(tmp_path), scenario)
        try:
            ReferenceResolver().resolve_pending(store)
            snapshot = full_dump(store)
            ReferenceResolver().resolve_pending(store)
            assert full_dump(store) == snapshot
        finally:
            store.close()


class TestReferentialInvariant:
    @given(scenario=scenarios())
    @SETTINGS
    def test_resolved_refs_point_at_existing_nodes(self, tmp_path: Path, scenario: Scenario) -> None:
        store = build_store(_example_db(tmp_path), scenario)
        try:
            report = ReferenceResolver().resolve_pending(store)
            known = known_node_ids(store)
            with store.read() as conn:
                edges = [tuple(row) for row in conn.execute("SELECT source, target, kind FROM graph_edges")]
            # every emitted edge connects two known nodes
            for source, target, _kind in edges:
                assert source in known
                assert target in known
            # every resolved ref is backed by a matching edge (dedup may fold
            # duplicate edges, so assert the (source, target, kind) triple)
            kind_map = {"calls": "calls", "imports": "imports", "function_ref": "calls"}
            edge_set = set(edges)
            for outcome in report.outcomes:
                if outcome.status != "resolved":
                    continue
                row = store.unresolved_ref(outcome.ref_id)
                assert row is not None
                assert outcome.target_node_id is not None
                assert (row.source_node_id, outcome.target_node_id, kind_map[row.kind]) in edge_set
        finally:
            store.close()


class TestReportComposition:
    @given(scenario=scenarios())
    @SETTINGS
    def test_counts_add_up(self, tmp_path: Path, scenario: Scenario) -> None:
        store = build_store(_example_db(tmp_path), scenario)
        try:
            total_refs = len(scenario[1])
            report = ReferenceResolver().resolve_pending(store)
            assert report.processed == report.resolved + report.failed
            assert report.processed == total_refs
            assert report.pending_remaining == 0
            assert report.edges_inserted <= report.resolved
        finally:
            store.close()

    @given(scenario=scenarios(), limit=st.integers(min_value=1, max_value=4))
    @SETTINGS
    def test_batch_limit_never_exceeded(
        self,
        tmp_path: Path,
        scenario: Scenario,
        limit: int,
    ) -> None:
        store = build_store(_example_db(tmp_path), scenario)
        try:
            report = ReferenceResolver().resolve_pending(store, limit=limit)
            assert report.processed <= limit
            assert report.processed + report.pending_remaining == len(scenario[1])
        finally:
            store.close()


class TestCrossInstanceDeterminism:
    @given(scenario=scenarios())
    @SETTINGS
    def test_identical_stores_identical_reports(self, tmp_path: Path, scenario: Scenario) -> None:
        store_a = build_store(_example_db(tmp_path), scenario)
        store_b = build_store(_example_db(tmp_path), scenario)
        try:
            report_a = ReferenceResolver().resolve_pending(store_a)
            report_b = ReferenceResolver().resolve_pending(store_b)
            assert report_a == report_b
            assert full_dump(store_a) == full_dump(store_b)
        finally:
            store_a.close()
            store_b.close()


class TestRetryAccounting:
    @given(scenario=scenarios())
    @SETTINGS
    def test_resurrect_reresolve_increments_attempts(self, tmp_path: Path, scenario: Scenario) -> None:
        store = build_store(_example_db(tmp_path), scenario)
        try:
            resolver = ReferenceResolver()
            first = resolver.resolve_pending(store)
            failed_before = {outcome.ref_id for outcome in first.outcomes if outcome.status == "failed"}
            moved = resolver.resurrect_failed(store)
            assert set(moved) == failed_before
            second = resolver.resolve_pending(store)
            for outcome in second.outcomes:
                row = store.unresolved_ref(outcome.ref_id)
                assert row is not None
                assert row.attempts == 2  # exactly one attempt per resolve pass
                assert outcome.status == "failed"  # store unchanged → same outcome
            # resolved refs stayed terminal and untouched
            resolved_before = {outcome.ref_id for outcome in first.outcomes if outcome.status == "resolved"}
            for ref_id in resolved_before:
                row = store.unresolved_ref(ref_id)
                assert row is not None and row.status == "resolved" and row.attempts == 1
        finally:
            store.close()


class TestTailDerivedInvariant:
    @given(
        target=st.text(
            alphabet=st.characters(whitelist_categories=("Ll", "Lu"), whitelist_characters="._"),
            min_size=1,
            max_size=30,
        )
    )
    @SETTINGS
    def test_store_derived_tail_matches_last_dot_segment(self, tmp_path: Path, target: str) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            ref_id = store.insert_unresolved_ref(
                source_node_id="function:s", target_name=target, kind="calls", first_seen=SEEN
            )
            row = store.unresolved_ref(ref_id)
            assert row is not None
            assert row.name_tail == target.rsplit(".", 1)[-1]
        finally:
            store.close()

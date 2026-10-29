"""Unit + integration tests for the bounded synthesis passes (wave W7, SUB-103).

``resolve/synthesis.py`` re-implements the *applicable* subset of codegraph's
45 ``SYNTH_PASSES`` (RESEARCH-COVERAGE-AUDIT §4.1) on top of the universal
graph store — rewritten, not ported. Four passes, all high-precision /
low-recall, all fail-closed on ambiguity:

* ``method_class_containment`` — codegraph's ``goMethodContains`` fixed synth
  step generalized: orphan ``method`` symbols gain a ``contains`` parent.
* ``reexport_chains`` — import A from B; B re-exports from C → A references C.
* ``type_hint_edges`` — parked ``type_hint`` refs resolve to type-kind symbols
  (``class``/``interface``/``type``) and mint ``type_of`` edges.
* ``callback_edges`` — named callbacks registered by a function
  (``function_ref`` edges) are linked to that registrar's callers (dynamic
  dispatch channel, capped like codegraph's ``EVENT_FANOUT_CAP`` /
  ``MAX_CALLBACKS_PER_CHANNEL``).

Every minted edge carries ``provenance = "synthesis:<pass>"`` — never the
``ingest:*`` parse stamp and never the ``resolver:*`` stamps — and synthesis
never creates a second edge where any same-(source,target,kind) edge exists.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from know_your_soul.domain import canonical_json
from know_your_soul.resolve.name_matcher import NameMatchReport
from know_your_soul.resolve.resolver import PROVENANCE, BatchReport, ReferenceResolver
from know_your_soul.resolve.synthesis import (
    PASS_CALLBACK_EDGES,
    PASS_METHOD_CLASS_CONTAINMENT,
    PASS_REEXPORT_CHAINS,
    PASS_TYPE_HINT_EDGES,
    SYNTH_PASS_ORDER,
    SYNTHESIS_VERSION,
    PipelineReport,
    SynthesisConfig,
    SynthesisError,
    SynthesisReport,
    Synthesizer,
    pass_provenance,
    resolve_and_synthesize,
    run_synthesis,
)
from know_your_soul.store import EventStore

SEEN = "2026-10-28T00:00:00+00:00"
PARSE = "ingest:0123456789abcdef"


# ------------------------------------------------------------------ helpers


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def add_file(store: EventStore, path: str, language: str = "python", *, generated: bool = False) -> None:
    store.upsert_file(
        path=path,
        sha256=hashlib.sha256(path.encode()).hexdigest(),
        size=10,
        mtime=100,
        language=language,
        generated_hint=generated,
        extraction_version=1,
    )


def add_symbol(
    store: EventStore,
    node_id: str,
    name: str,
    path: str,
    *,
    kind: str = "function",
    line: int = 1,
    col: int = -1,
) -> None:
    store.upsert_symbol(node_id=node_id, name=name, kind=kind, path=path, line=line, col=col)


def add_parse_edge(store: EventStore, source: str, target: str, kind: str = "contains") -> None:
    store.add_graph_edge(source, target, kind, provenance=PARSE)


def edges(store: EventStore) -> list[tuple[object, ...]]:
    with store.read() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT source, target, kind, line, col, metadata, provenance FROM graph_edges ORDER BY edge_id"
            )
        ]


def edges_of_kind(store: EventStore, kind: str) -> list[tuple[object, ...]]:
    return [row for row in edges(store) if row[2] == kind]


def metadata_of(row: tuple[object, ...]) -> dict[str, object]:
    assert isinstance(row[5], str)
    return json.loads(row[5])


def add_ref(store: EventStore, source: str, target: str, kind: str) -> int:
    return store.insert_unresolved_ref(source_node_id=source, target_name=target, kind=kind, first_seen=SEEN)


def pass_report(report: SynthesisReport, name: str):
    matches = [entry for entry in report.passes if entry.name == name]
    assert len(matches) == 1
    return matches[0]


# ------------------------------------------------------------------ constants


class TestConstants:
    def test_version(self) -> None:
        assert isinstance(SYNTHESIS_VERSION, int) and SYNTHESIS_VERSION >= 1

    def test_pass_order_is_fixed_tuple(self) -> None:
        assert SYNTH_PASS_ORDER == (
            PASS_METHOD_CLASS_CONTAINMENT,
            PASS_REEXPORT_CHAINS,
            PASS_TYPE_HINT_EDGES,
            PASS_CALLBACK_EDGES,
        )

    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            (PASS_CALLBACK_EDGES, "synthesis:callback_edges"),
            (PASS_METHOD_CLASS_CONTAINMENT, "synthesis:method_class_containment"),
            (PASS_REEXPORT_CHAINS, "synthesis:reexport_chains"),
            (PASS_TYPE_HINT_EDGES, "synthesis:type_hint_edges"),
        ],
    )
    def test_pass_provenance(self, name: str, expected: str) -> None:
        assert pass_provenance(name) == expected

    def test_pass_provenance_rejects_unknown(self) -> None:
        with pytest.raises(SynthesisError):
            pass_provenance("nope")


# -------------------------------------------------------------------- config


class TestSynthesisConfig:
    def test_defaults_enable_all_passes(self) -> None:
        config = SynthesisConfig()
        for name in SYNTH_PASS_ORDER:
            assert config.is_enabled(name)

    def test_explicit_subset(self) -> None:
        config = SynthesisConfig(enabled_passes=frozenset({PASS_REEXPORT_CHAINS}))
        assert config.is_enabled(PASS_REEXPORT_CHAINS)
        assert not config.is_enabled(PASS_CALLBACK_EDGES)

    def test_unknown_pass_rejected(self) -> None:
        with pytest.raises(SynthesisError):
            SynthesisConfig(enabled_passes=frozenset({"not_a_pass"}))

    @pytest.mark.parametrize(
        "field",
        ["max_callbacks_per_channel", "max_dispatchers_per_channel", "reexport_max_depth", "ancestor_cap"],
    )
    @pytest.mark.parametrize("value", [0, -1, True, "6"])
    def test_caps_validated(self, field: str, value: object) -> None:
        with pytest.raises(SynthesisError):
            SynthesisConfig(**{field: value})  # type: ignore[arg-type]

    def test_none_enabled_means_all(self) -> None:
        assert SynthesisConfig(enabled_passes=None).is_enabled(PASS_CALLBACK_EDGES)


# ----------------------------------------------- pass: method_class_containment


class TestMethodClassContainment:
    def test_qualified_method_gains_class_parent(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:G", "Greeter", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "method:G.greet", "Greeter.greet", "pkg/mod.py", kind="method", line=5)
        report = run_synthesis(store)
        entry = pass_report(report, PASS_METHOD_CLASS_CONTAINMENT)
        assert entry.emitted == 1 and entry.inserted == 1
        rows = edges_of_kind(store, "contains")
        assert len(rows) == 1
        source, target = rows[0][0], rows[0][1]
        assert (source, target) == ("class:G", "method:G.greet")
        assert rows[0][6] == "synthesis:method_class_containment"
        meta = metadata_of(rows[0])
        assert meta["synthesized_by"] == PASS_METHOD_CLASS_CONTAINMENT
        assert meta["rule"] == "qualified_prefix"
        assert meta["confidence"] == "9/10"

    def test_double_colon_qualified(self, store: EventStore) -> None:
        add_file(store, "src/mod.rs", "rust")
        add_symbol(store, "class:S", "Stack", "src/mod.rs", kind="class", line=1)
        add_symbol(store, "method:S.push", "Stack::push", "src/mod.rs", kind="method", line=9)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 1

    def test_nearest_preceding_class_heuristic(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "class:B", "Beta", "pkg/mod.py", kind="class", line=20)
        add_symbol(store, "method:run", "run", "pkg/mod.py", kind="method", line=30)
        run_synthesis(store)
        rows = edges_of_kind(store, "contains")
        assert (rows[0][0], rows[0][1]) == ("class:B", "method:run")
        assert metadata_of(rows[0])["rule"] == "nearest_preceding_class"
        assert metadata_of(rows[0])["confidence"] == "7/10"

    def test_class_after_method_is_not_a_parent(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "method:run", "run", "pkg/mod.py", kind="method", line=3)
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=10)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0
        assert edges_of_kind(store, "contains") == []

    def test_method_with_parse_parent_untouched(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "class:B", "Beta", "pkg/mod.py", kind="class", line=20)
        add_symbol(store, "method:m", "run", "pkg/mod.py", kind="method", line=30)
        add_parse_edge(store, "class:A", "method:m")
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0
        rows = edges_of_kind(store, "contains")
        assert len(rows) == 1 and rows[0][0] == "class:A" and rows[0][6] == PARSE

    def test_ambiguous_qualified_class_skipped(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:G1", "Greeter", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "class:G2", "Greeter", "pkg/mod.py", kind="class", line=40)
        add_symbol(store, "method:G.greet", "Greeter.greet", "pkg/mod.py", kind="method", line=50)
        report = run_synthesis(store)
        entry = pass_report(report, PASS_METHOD_CLASS_CONTAINMENT)
        assert entry.inserted == 0
        assert entry.skipped_uncertain == 1

    def test_qualified_without_class_never_falls_through(self, store: EventStore) -> None:
        # A qualified name asserts an owner; when the class is absent the
        # method stays orphan (no nearest-class guess).
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "method:G.greet", "Greeter.greet", "pkg/mod.py", kind="method", line=50)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0

    def test_same_line_classes_are_uncertain(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=10, col=0)
        add_symbol(store, "class:B", "Beta", "pkg/mod.py", kind="class", line=10, col=20)
        add_symbol(store, "method:m", "run", "pkg/mod.py", kind="method", line=30)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0

    def test_cycle_guard(self, store: EventStore) -> None:
        # C is (transitively) contained by the orphan method: adding C→M would
        # close a contains cycle, so the pass must skip.
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:C", "Inner", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "function:holder", "holder", "pkg/mod.py", kind="function", line=2)
        add_symbol(store, "method:M", "run", "pkg/mod.py", kind="method", line=5)
        add_parse_edge(store, "method:M", "function:holder")
        add_parse_edge(store, "function:holder", "class:C")
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0

    def test_generated_files_skipped_by_default(self, store: EventStore) -> None:
        add_file(store, "pkg/gen.py", generated=True)
        add_symbol(store, "class:A", "Alpha", "pkg/gen.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/gen.py", kind="method", line=5)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0

    def test_generated_files_opt_in(self, store: EventStore) -> None:
        add_file(store, "pkg/gen.py", generated=True)
        add_symbol(store, "class:A", "Alpha", "pkg/gen.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/gen.py", kind="method", line=5)
        report = run_synthesis(store, config=SynthesisConfig(skip_generated=False))
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 1

    def test_functions_never_gain_class_parents(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "function:f", "run", "pkg/mod.py", kind="function", line=5)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).candidates == 0

    def test_cross_path_never_linked(self, store: EventStore) -> None:
        add_file(store, "pkg/a.py")
        add_file(store, "pkg/b.py")
        add_symbol(store, "class:A", "Alpha", "pkg/a.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/b.py", kind="method", line=5)
        report = run_synthesis(store)
        assert pass_report(report, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0

    def test_idempotent_second_run(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/mod.py", kind="method", line=5)
        first = run_synthesis(store)
        assert pass_report(first, PASS_METHOD_CLASS_CONTAINMENT).inserted == 1
        second = run_synthesis(store)
        assert pass_report(second, PASS_METHOD_CLASS_CONTAINMENT).inserted == 0
        assert pass_report(second, PASS_METHOD_CLASS_CONTAINMENT).skipped_existing == 1
        assert len(edges_of_kind(store, "contains")) == 1

    def test_disabled_by_config(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/mod.py", kind="method", line=5)
        report = run_synthesis(store, config=SynthesisConfig(enabled_passes=frozenset()))
        assert report.inserted == 0
        assert edges(store) == []


# -------------------------------------------------------- pass: reexport_chains


class TestReexportChains:
    def _chain_fixture(self, store: EventStore) -> None:
        # a.py: `from b import widget` → resolved to B's import node.
        # b.py: `from c import widget` → resolved to the real class in c.py.
        add_file(store, "pkg/a.py")
        add_file(store, "pkg/b.py")
        add_file(store, "pkg/c.py")
        add_symbol(store, "import:a", "widget", "pkg/a.py", kind="import", line=1)
        add_symbol(store, "import:b", "widget", "pkg/b.py", kind="import", line=1)
        add_symbol(store, "class:W", "Widget", "pkg/c.py", kind="class", line=3)
        store.add_graph_edge("import:a", "import:b", "imports", provenance=PROVENANCE)
        store.add_graph_edge("import:b", "class:W", "imports", provenance=PROVENANCE)

    def test_two_hop_chain(self, store: EventStore) -> None:
        self._chain_fixture(store)
        report = run_synthesis(store)
        entry = pass_report(report, PASS_REEXPORT_CHAINS)
        assert entry.inserted == 1
        rows = edges_of_kind(store, "references")
        assert len(rows) == 1
        assert (rows[0][0], rows[0][1]) == ("import:a", "class:W")
        assert rows[0][6] == "synthesis:reexport_chains"
        meta = metadata_of(rows[0])
        assert meta["synthesized_by"] == PASS_REEXPORT_CHAINS
        assert meta["via"] == ["import:b"]
        assert meta["confidence"] == "4/5"

    def test_three_hop_chain(self, store: EventStore) -> None:
        self._chain_fixture(store)
        # extend: import:b now points at import:b2, which points at class:W.
        add_symbol(store, "import:b2", "widget", "pkg/b.py", kind="import", line=2)
        with store.transaction() as conn:
            conn.execute("DELETE FROM graph_edges WHERE source = 'import:b'")
        store.add_graph_edge("import:b", "import:b2", "imports", provenance=PROVENANCE)
        store.add_graph_edge("import:b2", "class:W", "imports", provenance=PROVENANCE)
        run_synthesis(store)
        rows = edges_of_kind(store, "references")
        # Both suffix chains resolve honestly: a→…→W (via b, b2) and b→…→W.
        assert len(rows) == 2
        by_source = {row[0]: row for row in rows}
        assert metadata_of(by_source["import:a"])["via"] == ["import:b", "import:b2"]
        assert metadata_of(by_source["import:b"])["via"] == ["import:b2"]

    def test_direct_import_is_not_a_chain(self, store: EventStore) -> None:
        add_file(store, "pkg/a.py")
        add_file(store, "pkg/c.py")
        add_symbol(store, "import:a", "widget", "pkg/a.py", kind="import", line=1)
        add_symbol(store, "class:W", "Widget", "pkg/c.py", kind="class", line=3)
        store.add_graph_edge("import:a", "class:W", "imports", provenance=PROVENANCE)
        report = run_synthesis(store)
        assert pass_report(report, PASS_REEXPORT_CHAINS).inserted == 0
        assert edges_of_kind(store, "references") == []

    def test_ambiguous_hop_skipped(self, store: EventStore) -> None:
        self._chain_fixture(store)
        add_symbol(store, "class:W2", "Widget", "pkg/c.py", kind="class", line=9)
        store.add_graph_edge("import:b", "class:W2", "imports", provenance=PROVENANCE)
        report = run_synthesis(store)
        entry = pass_report(report, PASS_REEXPORT_CHAINS)
        assert entry.inserted == 0
        assert entry.skipped_uncertain == 1

    def test_dangling_reexport_skipped(self, store: EventStore) -> None:
        add_file(store, "pkg/a.py")
        add_file(store, "pkg/b.py")
        add_symbol(store, "import:a", "widget", "pkg/a.py", kind="import", line=1)
        add_symbol(store, "import:b", "widget", "pkg/b.py", kind="import", line=1)
        store.add_graph_edge("import:a", "import:b", "imports", provenance=PROVENANCE)
        report = run_synthesis(store)
        assert pass_report(report, PASS_REEXPORT_CHAINS).inserted == 0

    def test_cycle_never_emits(self, store: EventStore) -> None:
        add_file(store, "pkg/a.py")
        add_file(store, "pkg/b.py")
        add_symbol(store, "import:a", "widget", "pkg/a.py", kind="import", line=1)
        add_symbol(store, "import:b", "widget", "pkg/b.py", kind="import", line=1)
        store.add_graph_edge("import:a", "import:b", "imports", provenance=PROVENANCE)
        store.add_graph_edge("import:b", "import:a", "imports", provenance=PROVENANCE)
        report = run_synthesis(store)
        assert pass_report(report, PASS_REEXPORT_CHAINS).inserted == 0

    def test_depth_cap(self, store: EventStore) -> None:
        add_file(store, "pkg/a.py")
        add_symbol(store, "import:0", "w", "pkg/a.py", kind="import", line=1)
        depth = 4
        for index in range(1, depth + 2):
            add_symbol(store, f"import:{index}", "w", "pkg/a.py", kind="import", line=index + 1)
            store.add_graph_edge(f"import:{index - 1}", f"import:{index}", "imports", provenance=PROVENANCE)
        add_symbol(store, "class:W", "Widget", "pkg/a.py", kind="class", line=99)
        store.add_graph_edge(f"import:{depth + 1}", "class:W", "imports", provenance=PROVENANCE)
        shallow = run_synthesis(store, config=SynthesisConfig(reexport_max_depth=2))
        entry = pass_report(shallow, PASS_REEXPORT_CHAINS)
        # Chains longer than the cap from their own start are skipped…
        rows = edges_of_kind(store, "references")
        sources = {row[0] for row in rows}
        assert "import:0" not in sources
        assert "import:1" not in sources
        assert "import:2" not in sources
        assert entry.skipped_uncertain >= 3
        # …while suffix chains that fit the cap resolve honestly.
        assert {"import:3", "import:4"} <= sources

    def test_depth_cap_allows_shorter(self, store: EventStore) -> None:
        self._chain_fixture(store)
        report = run_synthesis(store, config=SynthesisConfig(reexport_max_depth=1))
        assert pass_report(report, PASS_REEXPORT_CHAINS).inserted == 1

    def test_existing_references_edge_not_duplicated(self, store: EventStore) -> None:
        self._chain_fixture(store)
        store.add_graph_edge("import:a", "class:W", "references", provenance=PARSE)
        report = run_synthesis(store)
        entry = pass_report(report, PASS_REEXPORT_CHAINS)
        assert entry.inserted == 0
        assert entry.skipped_existing == 1
        assert len(edges_of_kind(store, "references")) == 1

    def test_idempotent(self, store: EventStore) -> None:
        self._chain_fixture(store)
        run_synthesis(store)
        second = run_synthesis(store)
        assert pass_report(second, PASS_REEXPORT_CHAINS).inserted == 0
        assert len(edges_of_kind(store, "references")) == 1

    def test_import_edge_from_synthesis_is_not_retraversed_as_parse(self, store: EventStore) -> None:
        # The synthesized references edge must not feed back into the chain
        # walker (it only follows `imports` edges).
        self._chain_fixture(store)
        run_synthesis(store)
        report = run_synthesis(store)
        assert report.inserted == 0


# -------------------------------------------------------- pass: type_hint_edges


class TestTypeHintEdges:
    def test_pending_type_hint_resolves(self, store: EventStore) -> None:
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U", "User", "pkg/models.py", kind="class", line=3)
        ref_id = add_ref(store, "function:main", "User", "type_hint")
        report = run_synthesis(store)
        entry = pass_report(report, PASS_TYPE_HINT_EDGES)
        assert entry.inserted == 1
        rows = edges_of_kind(store, "type_of")
        assert len(rows) == 1
        assert (rows[0][0], rows[0][1]) == ("function:main", "class:U")
        assert rows[0][6] == "synthesis:type_hint_edges"
        meta = metadata_of(rows[0])
        assert meta["ref_id"] == ref_id
        assert meta["confidence"] == "9/10"
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "resolved"

    @pytest.mark.parametrize("kind", ["class", "interface", "type"])
    def test_type_like_kinds_resolve(self, store: EventStore, kind: str) -> None:
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, f"{kind}:T", "Thing", "pkg/models.py", kind=kind, line=3)
        add_ref(store, "function:main", "Thing", "type_hint")
        report = run_synthesis(store)
        assert pass_report(report, PASS_TYPE_HINT_EDGES).inserted == 1

    def test_function_target_never_resolves(self, store: EventStore) -> None:
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "function:helper", "helper", "pkg/main.py")
        ref_id = add_ref(store, "function:main", "helper", "type_hint")
        report = run_synthesis(store)
        assert pass_report(report, PASS_TYPE_HINT_EDGES).inserted == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "pending"

    def test_ambiguous_type_left_pending(self, store: EventStore) -> None:
        add_file(store, "pkg/a.py")
        add_file(store, "pkg/b.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U1", "User", "pkg/a.py", kind="class", line=1)
        add_symbol(store, "class:U2", "User", "pkg/b.py", kind="class", line=1)
        ref_id = add_ref(store, "function:main", "User", "type_hint")
        report = run_synthesis(store)
        entry = pass_report(report, PASS_TYPE_HINT_EDGES)
        assert entry.inserted == 0
        assert entry.skipped_uncertain == 1
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "pending"

    def test_unknown_name_left_pending(self, store: EventStore) -> None:
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        ref_id = add_ref(store, "function:main", "Ghost", "type_hint")
        report = run_synthesis(store)
        assert pass_report(report, PASS_TYPE_HINT_EDGES).inserted == 0
        assert store.unresolved_ref(ref_id).status == "pending"  # type: ignore[union-attr]

    def test_failed_unknown_ref_kind_is_retried(self, store: EventStore) -> None:
        # The primary cascade fails type_hint refs as unknown_ref_kind; the
        # synthesis pass claims exactly those.
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U", "User", "pkg/models.py", kind="class", line=3)
        ref_id = add_ref(store, "function:main", "User", "type_hint")
        ReferenceResolver().resolve_pending(store)
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "failed" and row.failure_reason == "unknown_ref_kind"
        report = run_synthesis(store)
        assert pass_report(report, PASS_TYPE_HINT_EDGES).inserted == 1
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "resolved"

    def test_failed_for_other_reasons_untouched(self, store: EventStore) -> None:
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U", "User", "pkg/models.py", kind="class", line=3)
        ref_id = add_ref(store, "function:main", "User", "type_hint")
        ReferenceResolver().resolve_pending(store)
        with store.transaction() as conn:
            conn.execute("UPDATE unresolved_refs SET failure_reason = 'ambiguous_exact:2' WHERE ref_id = ?", (ref_id,))
        report = run_synthesis(store)
        assert pass_report(report, PASS_TYPE_HINT_EDGES).inserted == 0

    def test_existing_type_of_edge_not_duplicated(self, store: EventStore) -> None:
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U", "User", "pkg/models.py", kind="class", line=3)
        add_ref(store, "function:main", "User", "type_hint")
        store.add_graph_edge("function:main", "class:U", "type_of", provenance=PARSE)
        report = run_synthesis(store)
        assert pass_report(report, PASS_TYPE_HINT_EDGES).inserted == 0
        # ...but the ref is still resolved (the edge exists, the answer is known).
        assert len(edges_of_kind(store, "type_of")) == 1

    def test_idempotent(self, store: EventStore) -> None:
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U", "User", "pkg/models.py", kind="class", line=3)
        add_ref(store, "function:main", "User", "type_hint")
        run_synthesis(store)
        second = run_synthesis(store)
        assert pass_report(second, PASS_TYPE_HINT_EDGES).inserted == 0
        assert len(edges_of_kind(store, "type_of")) == 1

    def test_disabled_leaves_refs_pending(self, store: EventStore) -> None:
        add_file(store, "pkg/models.py")
        add_file(store, "pkg/main.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "class:U", "User", "pkg/models.py", kind="class", line=3)
        ref_id = add_ref(store, "function:main", "User", "type_hint")
        run_synthesis(store, config=SynthesisConfig(enabled_passes=frozenset({PASS_CALLBACK_EDGES})))
        assert store.unresolved_ref(ref_id).status == "pending"  # type: ignore[union-attr]


# --------------------------------------------------------- pass: callback_edges


class TestCallbackEdges:
    def _registered(self, store: EventStore) -> None:
        """dispatcher D calls registrar G; G received callback F by reference."""
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:D", "boot", "pkg/bus.py", line=1)
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        add_symbol(store, "function:F", "handle_update", "pkg/bus.py", line=20)
        store.add_graph_edge("function:D", "function:G", "calls", provenance=PROVENANCE)
        ref_id = add_ref(store, "function:G", "handle_update", "function_ref")
        ReferenceResolver().resolve_pending(store)
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "resolved"

    def test_dispatch_edge_minted(self, store: EventStore) -> None:
        self._registered(store)
        report = run_synthesis(store)
        entry = pass_report(report, PASS_CALLBACK_EDGES)
        assert entry.inserted == 1
        rows = [row for row in edges_of_kind(store, "calls") if row[6] == "synthesis:callback_edges"]
        assert len(rows) == 1
        assert (rows[0][0], rows[0][1]) == ("function:D", "function:F")
        meta = metadata_of(rows[0])
        assert meta["synthesized_by"] == PASS_CALLBACK_EDGES
        assert meta["channel"] == "registered_callback"
        assert meta["registrar"] == "function:G"
        assert meta["confidence"] == "2/5"

    def test_parse_call_graph_untouched(self, store: EventStore) -> None:
        self._registered(store)
        before = {tuple(row) for row in edges(store)}
        run_synthesis(store)
        after = {tuple(row) for row in edges(store)}
        assert before <= after  # synthesis only ever adds

    def test_existing_edge_not_duplicated(self, store: EventStore) -> None:
        self._registered(store)
        store.add_graph_edge("function:D", "function:F", "calls", provenance=PROVENANCE)
        report = run_synthesis(store)
        assert pass_report(report, PASS_CALLBACK_EDGES).inserted == 0

    def test_dispatcher_cap_skips_channel(self, store: EventStore) -> None:
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        add_symbol(store, "function:F", "handle_update", "pkg/bus.py", line=20)
        for index in range(3):
            add_symbol(store, f"function:D{index}", f"boot_{index}", "pkg/bus.py", line=30 + index)
            store.add_graph_edge(f"function:D{index}", "function:G", "calls", provenance=PROVENANCE)
        add_ref(store, "function:G", "handle_update", "function_ref")
        ReferenceResolver().resolve_pending(store)
        config = SynthesisConfig(max_dispatchers_per_channel=2)
        report = run_synthesis(store, config=config)
        entry = pass_report(report, PASS_CALLBACK_EDGES)
        assert entry.inserted == 0
        assert entry.skipped_uncertain == 1

    def test_callback_cap_skips_channel(self, store: EventStore) -> None:
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:D", "boot", "pkg/bus.py", line=1)
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        for index in range(3):
            add_symbol(store, f"function:F{index}", f"handler_{index}", "pkg/bus.py", line=20 + index)
            add_ref(store, "function:G", f"handler_{index}", "function_ref")
        store.add_graph_edge("function:D", "function:G", "calls", provenance=PROVENANCE)
        ReferenceResolver().resolve_pending(store)
        config = SynthesisConfig(max_callbacks_per_channel=2)
        report = run_synthesis(store, config=config)
        assert pass_report(report, PASS_CALLBACK_EDGES).inserted == 0

    def test_file_node_dispatchers_skipped(self, store: EventStore) -> None:
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        add_symbol(store, "function:F", "handle_update", "pkg/bus.py", line=20)
        store.add_graph_edge("file:pkg/bus.py", "function:G", "calls", provenance=PROVENANCE)
        add_ref(store, "function:G", "handle_update", "function_ref")
        ReferenceResolver().resolve_pending(store)
        report = run_synthesis(store)
        assert pass_report(report, PASS_CALLBACK_EDGES).inserted == 0

    def test_non_callable_registrar_skipped(self, store: EventStore) -> None:
        add_file(store, "pkg/bus.py")
        add_symbol(store, "variable:V", "registry", "pkg/bus.py", kind="variable", line=5)
        add_symbol(store, "function:F", "handle_update", "pkg/bus.py", line=20)
        add_symbol(store, "function:D", "boot", "pkg/bus.py", line=1)
        store.add_graph_edge("function:D", "variable:V", "calls", provenance=PROVENANCE)
        ref_id = add_ref(store, "variable:V", "handle_update", "function_ref")
        ReferenceResolver().resolve_pending(store)
        assert store.unresolved_ref(ref_id).status == "resolved"  # type: ignore[union-attr]
        report = run_synthesis(store)
        assert pass_report(report, PASS_CALLBACK_EDGES).inserted == 0

    def test_self_edges_never_minted(self, store: EventStore) -> None:
        # D == F: the caller is itself the registered callback.
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:DF", "boot", "pkg/bus.py", line=1)
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        store.add_graph_edge("function:DF", "function:G", "calls", provenance=PROVENANCE)
        add_ref(store, "function:G", "boot", "function_ref")
        ReferenceResolver().resolve_pending(store)
        report = run_synthesis(store)
        assert pass_report(report, PASS_CALLBACK_EDGES).inserted == 0

    def test_unresolved_function_ref_contributes_nothing(self, store: EventStore) -> None:
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:D", "boot", "pkg/bus.py", line=1)
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        store.add_graph_edge("function:D", "function:G", "calls", provenance=PROVENANCE)
        add_ref(store, "function:G", "ghost_handler", "function_ref")
        ReferenceResolver().resolve_pending(store)
        report = run_synthesis(store)
        assert pass_report(report, PASS_CALLBACK_EDGES).inserted == 0

    def test_idempotent(self, store: EventStore) -> None:
        self._registered(store)
        run_synthesis(store)
        second = run_synthesis(store)
        assert pass_report(second, PASS_CALLBACK_EDGES).inserted == 0
        synth = [row for row in edges(store) if row[6] == "synthesis:callback_edges"]
        assert len(synth) == 1


# ------------------------------------------------------------- global rules


class TestGlobalRules:
    def _mixed_fixture(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/mod.py", kind="method", line=5)
        add_symbol(store, "function:D", "boot", "pkg/mod.py", line=8)
        add_symbol(store, "function:G", "on_update", "pkg/mod.py", line=12)
        add_symbol(store, "function:F", "handle_update", "pkg/mod.py", line=20)
        add_symbol(store, "import:a", "widget", "pkg/mod.py", kind="import", line=2)
        add_symbol(store, "import:b", "widget", "pkg/mod.py", kind="import", line=3)
        add_symbol(store, "class:W", "Widget", "pkg/mod.py", kind="class", line=30)
        add_parse_edge(store, "file:pkg/mod.py", "class:A")
        store.add_graph_edge("function:D", "function:G", "calls", provenance=PROVENANCE)
        store.add_graph_edge("import:a", "import:b", "imports", provenance=PROVENANCE)
        store.add_graph_edge("import:b", "class:W", "imports", provenance=PROVENANCE)
        add_ref(store, "function:G", "handle_update", "function_ref")
        add_ref(store, "function:D", "Widget", "type_hint")
        ReferenceResolver().resolve_pending(store)

    def test_never_duplicates_existing_identity(self, store: EventStore) -> None:
        self._mixed_fixture(store)
        pre = {(row[0], row[1], row[2]) for row in edges(store)}
        run_synthesis(store)
        post = [tuple(row) for row in edges(store)]
        post_keys = [(row[0], row[1], row[2]) for row in post]
        assert len(post_keys) == len(set(post_keys)), "synthesis created a duplicate (source,target,kind)"
        assert pre <= set(post_keys)

    def test_parse_edges_byte_unchanged(self, store: EventStore) -> None:
        self._mixed_fixture(store)
        parse_before = [row for row in edges(store) if row[6] == PARSE]
        run_synthesis(store)
        parse_after = [row for row in edges(store) if row[6] == PARSE]
        assert parse_before == parse_after

    def test_all_synthesis_provenance_prefixed(self, store: EventStore) -> None:
        self._mixed_fixture(store)
        run_synthesis(store)
        for row in edges(store):
            provenance = row[6]
            assert provenance in (PARSE, PROVENANCE) or str(provenance).startswith("synthesis:")

    def test_report_aggregates(self, store: EventStore) -> None:
        self._mixed_fixture(store)
        report = run_synthesis(store)
        assert isinstance(report, SynthesisReport)
        assert report.inserted == sum(entry.inserted for entry in report.passes)
        assert report.emitted == sum(entry.emitted for entry in report.passes)
        assert [entry.name for entry in report.passes] == list(SYNTH_PASS_ORDER)

    def test_disabled_passes_still_reported_with_zero(self, store: EventStore) -> None:
        self._mixed_fixture(store)
        report = run_synthesis(store, config=SynthesisConfig(enabled_passes=frozenset({PASS_REEXPORT_CHAINS})))
        entry = pass_report(report, PASS_CALLBACK_EDGES)
        assert entry.inserted == 0 and entry.candidates == 0

    def test_empty_store_is_noop(self, store: EventStore) -> None:
        report = run_synthesis(store)
        assert report.inserted == 0
        assert report.emitted == 0

    def test_determinism_across_identical_stores(self, store: EventStore, tmp_path: Path) -> None:
        other = EventStore(tmp_path / "other.sqlite3")
        try:
            self._mixed_fixture(store)
            self._mixed_fixture(other)
            report_a = run_synthesis(store)
            report_b = run_synthesis(other)
            assert report_a == report_b
            assert edges(store) == edges(other)
        finally:
            other.close()

    def test_synthesizer_class_matches_function(self, store: EventStore) -> None:
        self._mixed_fixture(store)
        via_class = Synthesizer().run(store)
        assert isinstance(via_class, SynthesisReport)
        via_function = run_synthesis(store)
        assert via_function.inserted == 0  # idempotent after the class run

    def test_config_reuse(self, store: EventStore) -> None:
        config = SynthesisConfig(enabled_passes=frozenset({PASS_REEXPORT_CHAINS}))
        synthesizer = Synthesizer(config)
        assert synthesizer.config is config


# ------------------------------------------------------- pipeline integration


class TestResolveAndSynthesize:
    def test_full_pipeline(self, store: EventStore) -> None:
        add_file(store, "pkg/main.py")
        add_file(store, "pkg/lib.py")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "function:helper", "helper", "pkg/lib.py")
        add_symbol(store, "class:A", "Alpha", "pkg/main.py", kind="class", line=2)
        add_symbol(store, "method:m", "run", "pkg/main.py", kind="method", line=9)
        add_ref(store, "function:main", "helper", "calls")
        report = resolve_and_synthesize(store)
        assert isinstance(report, PipelineReport)
        assert isinstance(report.resolver, BatchReport)
        assert isinstance(report.name_match, NameMatchReport)
        assert isinstance(report.synthesis, SynthesisReport)
        assert report.resolver.resolved == 1
        kinds = {row[2] for row in edges(store)}
        assert "calls" in kinds and "contains" in kinds

    def test_pipeline_order_resolver_then_synthesis(self, store: EventStore) -> None:
        # The function_ref the callback pass needs only exists after the
        # primary resolver ran — proving ordering inside the composed entry.
        add_file(store, "pkg/bus.py")
        add_symbol(store, "function:D", "boot", "pkg/bus.py", line=1)
        add_symbol(store, "function:G", "on_update", "pkg/bus.py", line=5)
        add_symbol(store, "function:F", "handle_update", "pkg/bus.py", line=20)
        store.add_graph_edge("function:D", "function:G", "calls", provenance=PROVENANCE)
        add_ref(store, "function:G", "handle_update", "function_ref")
        report = resolve_and_synthesize(store)
        assert report.resolver.resolved == 1
        synth = [row for row in edges(store) if row[6] == "synthesis:callback_edges"]
        assert len(synth) == 1

    def test_pipeline_runs_name_match_tier(self, store: EventStore) -> None:
        add_file(store, "pkg/main.py", "python")
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:main", "main", "pkg/main.py")
        add_symbol(store, "function:t", "parse_http_response", "pkg/http.py")
        add_ref(store, "function:main", "parseHTTPResponse", "calls")
        report = resolve_and_synthesize(store)
        assert report.resolver.resolved == 0
        assert report.name_match.resolved == 1

    def test_pipeline_metadata_canonical(self, store: EventStore) -> None:
        add_file(store, "pkg/mod.py")
        add_symbol(store, "class:A", "Alpha", "pkg/mod.py", kind="class", line=1)
        add_symbol(store, "method:m", "run", "pkg/mod.py", kind="method", line=5)
        resolve_and_synthesize(store)
        rows = edges_of_kind(store, "contains")
        assert rows[0][5] == canonical_json(json.loads(str(rows[0][5])))

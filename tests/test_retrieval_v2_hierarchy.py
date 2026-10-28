"""Unit tests for retrieval v2 call hierarchy (wave W4).

Depth-bounded callers/callees traversal over ``calls``/``imports``/
``references`` edges with canonical output (RESEARCH-QUERY §1C).
"""

from __future__ import annotations

from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from _retrieval_v2_helpers import add_edge, add_symbol
from know_your_soul.retrieval_v2 import (
    DEFAULT_EDGE_KINDS,
    CallHierarchy,
    HierarchyEntry,
    HierarchyError,
    HierarchyReport,
    callees,
    callers,
)
from know_your_soul.store import EventStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def hierarchy() -> CallHierarchy:
    return CallHierarchy()


def build_chain(store: EventStore) -> dict[str, str]:
    """a -> b -> c (calls), d -> b (imports), b -> c (references dup at other line)."""
    ids = {
        "a": add_symbol(store, "alpha", "function", "src/a.py", 1),
        "b": add_symbol(store, "beta", "function", "src/b.py", 1),
        "c": add_symbol(store, "gamma", "function", "src/c.py", 1),
        "d": add_symbol(store, "delta", "module", "src/d.py", 1),
    }
    add_edge(store, ids["a"], ids["b"], "calls", line=10)
    add_edge(store, ids["b"], ids["c"], "calls", line=20)
    add_edge(store, ids["d"], ids["b"], "imports", line=5)
    return ids


class TestDefaults:
    def test_default_edge_kinds(self) -> None:
        assert DEFAULT_EDGE_KINDS == ("calls", "imports", "references")

    def test_depth_must_be_positive(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        with pytest.raises(HierarchyError):
            hierarchy.callers(store, "alpha", depth=0)

    def test_depth_capped(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        with pytest.raises(HierarchyError):
            hierarchy.callers(store, "alpha", depth=99)

    def test_limit_validated(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        with pytest.raises(HierarchyError):
            hierarchy.callees(store, "alpha", limit=0)

    def test_unknown_kind_rejected(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        with pytest.raises(HierarchyError):
            hierarchy.callers(store, "alpha", kinds=("calls", "bogus kind!"))


class TestCallers:
    def test_direct_callers(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        ids = build_chain(store)
        report = hierarchy.callers(store, "beta")
        assert isinstance(report, HierarchyReport)
        assert len(report.definitions) == 1
        result = report.definitions[0]
        assert result.root.node_id == ids["b"]
        entry_names = {entry.name for entry in result.entries}
        assert entry_names == {"alpha", "delta"}

    def test_callers_respect_depth(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        shallow = hierarchy.callers(store, "gamma", depth=1)
        deep = hierarchy.callers(store, "gamma", depth=2)
        assert {e.name for e in shallow.definitions[0].entries} == {"beta"}
        assert {e.name for e in deep.definitions[0].entries} == {"beta", "alpha", "delta"}

    def test_callees(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callees(store, "alpha")
        assert {e.name for e in report.definitions[0].entries} == {"beta"}

    def test_callees_depth_two(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callees(store, "alpha", depth=2)
        assert {e.name for e in report.definitions[0].entries} == {"beta", "gamma"}

    def test_entries_carry_edge_metadata(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callers(store, "beta")
        by_name = {e.name: e for e in report.definitions[0].entries}
        assert by_name["alpha"].via_edge_kind == "calls"
        assert by_name["alpha"].via_line == 10
        assert by_name["delta"].via_edge_kind == "imports"

    def test_entry_depths(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callers(store, "gamma", depth=3)
        depths = {e.name: e.depth for e in report.definitions[0].entries}
        assert depths["beta"] == 1
        assert depths["alpha"] == 2
        assert depths["delta"] == 2

    def test_contains_edges_excluded(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        a = add_symbol(store, "alpha", "function", "a.py", 1)
        b = add_symbol(store, "beta", "function", "b.py", 1)
        add_edge(store, a, b, "contains")
        report = hierarchy.callers(store, "beta")
        assert report.definitions[0].entries == ()

    def test_unknown_symbol_gives_empty_report(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        report = hierarchy.callers(store, "nonexistent")
        assert report.definitions == ()
        assert "nonexistent" in report.render()

    def test_cycle_safe(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        a = add_symbol(store, "alpha", "function", "a.py", 1)
        b = add_symbol(store, "beta", "function", "b.py", 1)
        add_edge(store, a, b, "calls")
        add_edge(store, b, a, "calls")
        report = hierarchy.callers(store, "alpha", depth=5)
        assert {e.name for e in report.definitions[0].entries} == {"beta"}

    def test_limit_truncates_and_flags(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        target = add_symbol(store, "popular", "function", "t.py", 1)
        for index in range(10):
            caller = add_symbol(store, f"caller{index:02d}", "function", f"c{index:02d}.py", 1)
            add_edge(store, caller, target, "calls")
        report = hierarchy.callers(store, "popular", limit=3)
        result = report.definitions[0]
        assert len(result.entries) == 3
        assert result.truncated is True

    def test_references_edges_traversed(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        doc = add_symbol(store, "guide", "section", "docs/g.md", 1)
        target = add_symbol(store, "helper", "function", "h.py", 1)
        add_edge(store, doc, target, "references")
        report = hierarchy.callers(store, "helper")
        assert {e.name for e in report.definitions[0].entries} == {"guide"}

    def test_kind_filter_narrows(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        ids = build_chain(store)
        report = hierarchy.callers(store, "beta", kinds=("imports",))
        entries = report.definitions[0].entries
        assert {e.name for e in entries} == {"delta"}
        assert ids["d"] == entries[0].node_id


class TestResolution:
    def test_lookup_by_node_id(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        ids = build_chain(store)
        report = hierarchy.callers(store, ids["b"])
        assert report.definitions[0].root.node_id == ids["b"]

    def test_case_insensitive_name(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callers(store, "BETA")
        assert report.definitions[0].root.name == "beta"

    def test_multiple_definitions_grouped(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        one = add_symbol(store, "dup", "function", "a.py", 1)
        two = add_symbol(store, "dup", "function", "b.py", 1)
        caller = add_symbol(store, "caller", "function", "c.py", 1)
        add_edge(store, caller, two, "calls")
        report = hierarchy.callers(store, "dup")
        assert len(report.definitions) == 2
        keys = [(r.root.path, r.root.line, r.root.node_id) for r in report.definitions]
        assert keys == sorted(keys)  # canonical definition order
        by_root = {result.root.node_id: result for result in report.definitions}
        assert by_root[one].entries == ()
        assert {e.name for e in by_root[two].entries} == {"caller"}

    def test_file_narrows_definitions(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "dup", "function", "a.py", 1)
        add_symbol(store, "dup", "function", "b.py", 1)
        report = hierarchy.callers(store, "dup", file="b.py")
        assert len(report.definitions) == 1
        assert report.definitions[0].root.path == "b.py"

    def test_file_no_match_gives_empty(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "dup", "function", "a.py", 1)
        report = hierarchy.callers(store, "dup", file="zzz.py")
        assert report.definitions == ()


class TestCanonicalOutput:
    def test_entries_sorted_canonically(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callers(store, "gamma", depth=2)
        entries = report.definitions[0].entries
        keys = [(e.depth, e.path, e.line, e.node_id) for e in entries]
        assert keys == sorted(keys)

    def test_render_is_deterministic(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        first = hierarchy.callers(store, "gamma", depth=2).render()
        second = hierarchy.callers(store, "gamma", depth=2).render()
        assert first == second

    def test_render_contains_header_and_entries(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        text = hierarchy.callers(store, "beta").render()
        assert "Callers of beta (function)" in text
        assert "src/b.py:1" in text
        assert "via calls" in text
        assert "alpha" in text

    def test_render_callees_header(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        text = hierarchy.callees(store, "beta").render()
        assert "Callees of beta (function)" in text

    def test_render_empty_entries(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        add_symbol(store, "lonely", "function", "a.py", 1)
        text = hierarchy.callers(store, "lonely").render()
        assert "(none)" in text

    def test_render_truncation_marker(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        target = add_symbol(store, "popular", "function", "t.py", 1)
        for index in range(5):
            caller = add_symbol(store, f"caller{index}", "function", f"c{index}.py", 1)
            add_edge(store, caller, target, "calls")
        text = hierarchy.callers(store, "popular", limit=2).render()
        assert "Showing 2 of 5" in text

    def test_report_frozen(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        report = hierarchy.callers(store, "beta")
        with pytest.raises(FrozenInstanceError):
            report.direction = "callees"  # type: ignore[misc]

    def test_entry_type(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        build_chain(store)
        entry = hierarchy.callers(store, "beta").definitions[0].entries[0]
        assert isinstance(entry, HierarchyEntry)

    def test_unknown_endpoint_described_as_node(self, store: EventStore, hierarchy: CallHierarchy) -> None:
        # edge to a node id with no symbols row (e.g. external module file node)
        a = add_symbol(store, "alpha", "function", "a.py", 1)
        add_edge(store, a, "file:ext/mod.py", "imports")
        report = hierarchy.callees(store, "alpha")
        entry = report.definitions[0].entries[0]
        assert entry.node_id == "file:ext/mod.py"
        assert entry.name == "file:ext/mod.py"
        assert entry.kind == "node"

    def test_module_level_helpers(self, store: EventStore) -> None:
        build_chain(store)
        assert callers(store, "beta").definitions[0].entries != ()
        assert callees(store, "beta").definitions[0].entries != ()

"""Resolver addendum tests (wave W4, temuan SUB-90).

W3 document ingestion parks markdown/plaintext link refs with
``kind="references"``; the W2 resolver rejected them as
``unknown_ref_kind``. The addendum maps ref kind ``references`` → edge
kind ``references`` (CORE edge kind, graph_model.CoreEdgeKind.REFERENCES)
— a minimal, additive change to ``REF_KIND_TO_EDGE_KIND``.
"""

from __future__ import annotations

import json
from collections.abc import Iterator
from pathlib import Path

import pytest

from _retrieval_v2_helpers import add_file, add_ref, add_symbol
from know_your_soul.resolve import REF_KIND_TO_EDGE_KIND, ReferenceResolver, resolve_pending
from know_your_soul.store import EventStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


@pytest.fixture
def doc_section(store: EventStore) -> str:
    add_file(store, "docs/guide.md", "markdown")
    return add_symbol(store, "intro", "section", "docs/guide.md", 1)


class TestMapping:
    def test_references_kind_is_mapped(self) -> None:
        assert REF_KIND_TO_EDGE_KIND["references"] == "references"

    def test_existing_mappings_unchanged(self) -> None:
        # the addendum is additive only — W2 mappings must not move
        assert REF_KIND_TO_EDGE_KIND["calls"] == "calls"
        assert REF_KIND_TO_EDGE_KIND["imports"] == "imports"
        assert REF_KIND_TO_EDGE_KIND["function_ref"] == "calls"

    def test_mapping_has_exactly_four_entries(self) -> None:
        assert set(REF_KIND_TO_EDGE_KIND) == {"calls", "imports", "function_ref", "references"}


class TestResolution:
    def test_references_ref_resolves_to_references_edge(self, store: EventStore, doc_section: str) -> None:
        target = add_symbol(store, "helper", "function", "src/h.py", 3)
        add_ref(store, doc_section, "helper", "references")
        report = resolve_pending(store)
        assert report.resolved == 1
        assert report.failed == 0
        edges = store.outgoing_edges(doc_section, kinds=("references",))
        assert len(edges) == 1
        assert edges[0].target == target
        assert edges[0].kind == "references"

    def test_references_edge_carries_ref_provenance(self, store: EventStore, doc_section: str) -> None:
        add_symbol(store, "helper", "function", "src/h.py", 3)
        ref_id = add_ref(store, doc_section, "helper", "references")
        resolve_pending(store)
        edge = store.outgoing_edges(doc_section, kinds=("references",))[0]
        assert edge.provenance is not None and edge.provenance.startswith("resolver:two-phase:")
        metadata = json.loads(edge.metadata or "{}")
        assert metadata["ref_id"] == ref_id
        assert metadata["strategy"] == "exact"

    def test_unknown_target_fails_with_typed_reason(self, store: EventStore, doc_section: str) -> None:
        # before the addendum this failed as unknown_ref_kind; now the ref
        # flows through the normal cascade and fails as no_resolution
        add_ref(store, doc_section, "ghost_symbol", "references")
        report = resolve_pending(store)
        assert report.failed == 1
        assert report.outcomes[0].reason == "no_resolution"

    def test_ambiguous_references_fail_closed(self, store: EventStore, doc_section: str) -> None:
        add_symbol(store, "helper", "function", "src/a.py", 1)
        add_symbol(store, "helper", "function", "src/b.py", 1)
        add_ref(store, doc_section, "helper", "references")
        report = resolve_pending(store)
        assert report.failed == 1
        assert report.outcomes[0].reason is not None
        assert report.outcomes[0].reason.startswith("ambiguous_exact:")

    def test_mixed_batch_with_w2_kinds(self, store: EventStore, doc_section: str) -> None:
        caller = add_symbol(store, "caller", "function", "src/c.py", 1)
        target = add_symbol(store, "callee", "function", "src/t.py", 1)
        add_ref(store, caller, "callee", "calls")
        add_ref(store, doc_section, "callee", "references")
        report = resolve_pending(store)
        assert report.resolved == 2
        assert store.outgoing_edges(caller, kinds=("calls",))[0].target == target
        assert store.outgoing_edges(doc_section, kinds=("references",))[0].target == target

    def test_name_tail_resolution_for_references(self, store: EventStore, doc_section: str) -> None:
        target = add_symbol(store, "mod.helper", "function", "src/h.py", 3)
        add_ref(store, doc_section, "deep.qual.helper", "references", name_tail="helper")
        report = resolve_pending(store)
        assert report.resolved == 1
        assert store.outgoing_edges(doc_section, kinds=("references",))[0].target == target

    def test_resolver_instance_path(self, store: EventStore, doc_section: str) -> None:
        add_symbol(store, "helper", "function", "src/h.py", 3)
        add_ref(store, doc_section, "helper", "references")
        resolver = ReferenceResolver()
        report = resolver.resolve_pending(store)
        assert report.resolved == 1

    def test_resolved_references_ref_is_terminal(self, store: EventStore, doc_section: str) -> None:
        add_symbol(store, "helper", "function", "src/h.py", 3)
        ref_id = add_ref(store, doc_section, "helper", "references")
        resolve_pending(store)
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "resolved"
        # replay is idempotent: edge identity dedups, ref stays resolved
        report = resolve_pending(store)
        assert report.processed == 0
        assert len(store.outgoing_edges(doc_section, kinds=("references",))) == 1

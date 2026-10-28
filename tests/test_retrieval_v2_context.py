"""Unit tests for retrieval v2 context assembly (wave W4).

Token budgeting bertingkat: proportional allocation by score, skeletoni-
zation when a node's share cannot afford its full form, provenance +
node id on every entry, hard invariant ``total_tokens <= budget``
(RESEARCH-QUERY §5).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from _retrieval_v2_helpers import add_edge, add_symbol
from know_your_soul.retrieval_v2 import (
    SKELETON_MARKER,
    AssembledContext,
    ContextConfig,
    ContextEntry,
    ContextError,
    ContextItem,
    assemble_context,
    estimate_tokens,
)
from know_your_soul.store import EventStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def item(
    node_id: str, name: str, kind: str = "function", path: str = "a.py", line: int = 1, score: float = 1.0
) -> ContextItem:
    return ContextItem(node_id=node_id, name=name, kind=kind, path=path, line=line, col=0, score=score)


class TestConfigValidation:
    def test_budget_must_be_int(self) -> None:
        with pytest.raises(ContextError):
            ContextConfig(budget_tokens="100")  # type: ignore[arg-type]

    def test_budget_below_frame_rejected(self) -> None:
        with pytest.raises(ContextError):
            ContextConfig(budget_tokens=1)

    def test_minimal_budget_accepted(self) -> None:
        assert ContextConfig(budget_tokens=16).budget_tokens == 16

    def test_chars_per_token_validated(self) -> None:
        with pytest.raises(ContextError):
            ContextConfig(budget_tokens=100, chars_per_token=0)

    def test_max_share_fraction_validated(self) -> None:
        with pytest.raises(ContextError):
            ContextConfig(budget_tokens=100, max_share_fraction=0.0)
        with pytest.raises(ContextError):
            ContextConfig(budget_tokens=100, max_share_fraction=1.5)

    def test_defaults(self) -> None:
        config = ContextConfig(budget_tokens=100)
        assert config.chars_per_token == 4


class TestBudgetInvariant:
    def test_total_never_exceeds_budget(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=float(i + 1)) for i in range(10)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=64))
        assert context.total_tokens <= 64

    def test_tight_budget_skeletonizes(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}") for i in range(6)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=48))
        assert context.total_tokens <= 48
        assert any(entry.skeleton for entry in context.entries)

    def test_tiny_budget_omits_everything(self, store: EventStore) -> None:
        context = assemble_context(store, [item("function:n1", "name1")], ContextConfig(budget_tokens=16))
        assert context.entries == ()
        assert context.omitted == ("function:n1",)
        assert context.total_tokens <= 16

    def test_empty_items(self, store: EventStore) -> None:
        context = assemble_context(store, [], ContextConfig(budget_tokens=100))
        assert context.entries == ()
        assert context.omitted == ()
        assert context.total_tokens <= 100

    def test_rendered_text_within_budget(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=2.0) for i in range(5)]
        budget = 200
        context = assemble_context(store, items, ContextConfig(budget_tokens=budget))
        assert estimate_tokens(context.render()) <= budget


class TestProportionalAllocation:
    def test_higher_score_gets_full_form_first(self, store: EventStore) -> None:
        low = item("function:low", "lowscore", score=1.0)
        high = item("function:n2", "highscore", score=100.0)
        context = assemble_context(store, [low, high], ContextConfig(budget_tokens=100))
        by_id = {entry.node_id: entry for entry in context.entries}
        assert by_id["function:n2"].skeleton is False
        assert by_id["function:low"].skeleton is True

    def test_equal_scores_share_evenly(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=1.0) for i in range(2)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=200))
        assert all(not entry.skeleton for entry in context.entries)

    def test_zero_scores_treated_equally(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=0.0) for i in range(2)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=200))
        assert len(context.entries) == 2

    def test_negative_scores_clamped_to_zero(self, store: EventStore) -> None:
        items = [item("function:a", "aaa", score=-5.0), item("function:b", "bbb", score=0.0)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=200))
        assert len(context.entries) == 2

    def test_max_share_caps_dominant_item(self, store: EventStore) -> None:
        # a dominant item must not eat the whole budget: the small peer still
        # gets its skeleton when the budget allows one
        items = [item("function:big", "bigname", score=1000.0), item("function:tiny", "tinyname", score=0.001)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=60))
        ids = {entry.node_id for entry in context.entries}
        assert "function:tiny" in ids


class TestProvenance:
    def test_every_entry_has_node_id_and_location(self, store: EventStore) -> None:
        items = [item("function:n1", "alpha", path="src/a.py", line=7)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=200))
        entry = context.entries[0]
        assert entry.node_id == "function:n1"
        assert "src/a.py:7" in entry.text
        assert "function:n1" in entry.text

    def test_skeleton_keeps_provenance(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=1.0) for i in range(6)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=48))
        for entry in context.entries:
            assert entry.node_id in entry.text
            if entry.skeleton:
                assert SKELETON_MARKER in entry.text

    def test_full_entry_shows_edge_summary(self, store: EventStore) -> None:
        a = add_symbol(store, "alpha", "function", "a.py", 1)
        b = add_symbol(store, "beta", "function", "b.py", 1)
        add_edge(store, a, b, "calls")
        target = ContextItem(node_id=a, name="alpha", kind="function", path="a.py", line=1, col=0, score=10.0)
        context = assemble_context(store, [target], ContextConfig(budget_tokens=200))
        assert "calls" in context.entries[0].text

    def test_omitted_listed_in_order(self, store: EventStore) -> None:
        items = [item(f"function:n{i:02d}", f"name{i:02d}", score=1.0) for i in range(30)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=32))
        assert context.omitted == tuple(sorted(context.omitted))


class TestDeterminism:
    def test_same_input_same_output(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=float(i)) for i in range(8)]
        config = ContextConfig(budget_tokens=80)
        first = assemble_context(store, items, config)
        second = assemble_context(store, items, config)
        assert first == second
        assert first.render() == second.render()

    def test_input_order_does_not_change_output(self, store: EventStore) -> None:
        items = [item(f"function:n{i}", f"name{i}", score=float(i + 1)) for i in range(8)]
        config = ContextConfig(budget_tokens=80)
        forward = assemble_context(store, items, config)
        backward = assemble_context(store, list(reversed(items)), config)
        assert forward == backward

    def test_duplicate_node_ids_deduped(self, store: EventStore) -> None:
        duplicate = item("function:n1", "alpha", score=1.0)
        winner = item("function:n1", "alpha", score=5.0)
        context = assemble_context(store, [duplicate, winner], ContextConfig(budget_tokens=200))
        assert len(context.entries) == 1
        assert context.entries[0].score == 5.0

    def test_result_types(self, store: EventStore) -> None:
        context = assemble_context(store, [item("function:n1", "alpha")], ContextConfig(budget_tokens=100))
        assert isinstance(context, AssembledContext)
        assert isinstance(context.entries[0], ContextEntry)

    def test_render_header_reports_budget(self, store: EventStore) -> None:
        context = assemble_context(store, [item("function:n1", "alpha")], ContextConfig(budget_tokens=100))
        assert "budget" in context.render().lower()

    def test_render_lists_omitted_count(self, store: EventStore) -> None:
        items = [item(f"function:n{i:02d}", f"name{i:02d}") for i in range(30)]
        context = assemble_context(store, items, ContextConfig(budget_tokens=32))
        if context.omitted:
            assert "omitted" in context.render()

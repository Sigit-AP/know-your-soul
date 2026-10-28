"""Property tests for retrieval v2 (wave W4).

Invariants under randomized-but-seeded (``derandomize=True``) inputs:

* determinism — identical store + query ⇒ identical results, twice;
* budget — assembled context never exceeds its token budget;
* order-stability — output is invariant under input ordering;
* bounded Levenshtein — symmetry, bounds, and -1-above-cap contract;
* parser — never raises on arbitrary text, output shape stable.

Every example gets its own database file via ``_example_db`` (the
``tmp_path`` fixture is function-scoped and not reset between examples).
"""

from __future__ import annotations

import itertools
import random
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from _retrieval_v2_helpers import add_file, add_symbol
from know_your_soul.retrieval_v2 import (
    ContextConfig,
    ContextItem,
    SymbolLookup,
    assemble_context,
    bounded_levenshtein,
    ensure_fts_index,
    estimate_tokens,
    parse_query,
)
from know_your_soul.store import EventStore

_EXAMPLE_COUNTER = itertools.count()


def _example_db(tmp_path: Path) -> Path:
    return tmp_path / f"soul-{next(_EXAMPLE_COUNTER)}.sqlite3"


SETTINGS = settings(
    max_examples=25,
    deadline=None,
    derandomize=True,
    suppress_health_check=[
        HealthCheck.too_slow,
        HealthCheck.data_too_large,
        HealthCheck.function_scoped_fixture,  # each example gets its own db via _example_db
    ],
)

_NAMES = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_"),
    min_size=1,
    max_size=12,
)
_KINDS = st.sampled_from(["function", "class", "method", "variable", "section", "paragraph", "module"])
_PATHS = st.sampled_from(["src/a.py", "src/b.py", "lib/c.go", "docs/g.md", "tests/test_x.py"])


def _seed_symbols(store: EventStore, rows: list[tuple[str, str, str, int]]) -> None:
    for path in sorted({row[2] for row in rows}):
        add_file(store, path, "python" if path.endswith(".py") else "markdown")
    for name, kind, path, line in rows:
        add_symbol(store, name, kind, path, line)


class TestLookupDeterminism:
    @given(rows=st.lists(st.tuples(_NAMES, _KINDS, _PATHS, st.integers(1, 200)), max_size=12), term=_NAMES)
    @SETTINGS
    def test_search_is_deterministic(self, tmp_path: Path, rows: list[tuple[str, str, str, int]], term: str) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            _seed_symbols(store, rows)
            lookup = SymbolLookup()
            first = lookup.search(store, term)
            second = lookup.search(store, term)
        finally:
            store.close()
        assert first == second

    @given(rows=st.lists(st.tuples(_NAMES, _KINDS, _PATHS, st.integers(1, 200)), max_size=12), term=_NAMES)
    @SETTINGS
    def test_results_have_canonical_order(
        self, tmp_path: Path, rows: list[tuple[str, str, str, int]], term: str
    ) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            _seed_symbols(store, rows)
            results = SymbolLookup().search(store, term)
        finally:
            store.close()
        keys = [(-r.score, r.path, r.line, r.col, r.node_id) for r in results]
        assert keys == sorted(keys)

    @given(rows=st.lists(st.tuples(_NAMES, _KINDS, _PATHS, st.integers(1, 200)), max_size=12), term=_NAMES)
    @SETTINGS
    def test_results_never_exceed_limit(self, tmp_path: Path, rows: list[tuple[str, str, str, int]], term: str) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            _seed_symbols(store, rows)
            results = SymbolLookup().search(store, term, limit=7)
        finally:
            store.close()
        assert len(results) <= 7


def _context_items(items: list[tuple[str, str, str, int, float]]) -> list[ContextItem]:
    return [
        ContextItem(
            node_id=f"{kind}:id{index}",
            name=name,
            kind=kind,
            path=path,
            line=line,
            col=0,
            score=score,
        )
        for index, (name, kind, path, line, score) in enumerate(items)
    ]


_ITEMS = st.lists(
    st.tuples(_NAMES, _KINDS, _PATHS, st.integers(1, 200), st.floats(0.0, 1e4, allow_nan=False)),
    max_size=15,
)


class TestContextBudgetInvariant:
    @given(items=_ITEMS, budget=st.integers(16, 400))
    @SETTINGS
    def test_total_tokens_never_exceeds_budget(
        self,
        tmp_path: Path,
        items: list[tuple[str, str, str, int, float]],
        budget: int,
    ) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            context = assemble_context(store, _context_items(items), ContextConfig(budget_tokens=budget))
        finally:
            store.close()
        assert context.total_tokens <= budget
        assert estimate_tokens(context.render()) <= budget
        # every emitted entry carries provenance
        for entry in context.entries:
            assert entry.node_id in entry.text
            assert f"{entry.path}:{entry.line}" in entry.text

    @given(items=_ITEMS, budget=st.integers(16, 400), seed=st.integers(0, 10_000))
    @SETTINGS
    def test_output_stable_under_input_permutation(
        self,
        tmp_path: Path,
        items: list[tuple[str, str, str, int, float]],
        budget: int,
        seed: int,
    ) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            context_items = _context_items(items)
            shuffled = list(context_items)
            random.Random(seed).shuffle(shuffled)
            config = ContextConfig(budget_tokens=budget)
            forward = assemble_context(store, context_items, config)
            backward = assemble_context(store, shuffled, config)
        finally:
            store.close()
        assert forward == backward


class TestLevenshteinProperties:
    @given(a=st.text(max_size=12), b=st.text(max_size=12))
    @SETTINGS
    def test_distance_is_symmetric(self, a: str, b: str) -> None:
        assert bounded_levenshtein(a, b, 12) == bounded_levenshtein(b, a, 12)

    @given(a=st.text(max_size=12), b=st.text(max_size=12))
    @SETTINGS
    def test_distance_lower_bounded_by_length_gap(self, a: str, b: str) -> None:
        distance = bounded_levenshtein(a, b, 12)
        assert distance >= abs(len(a) - len(b))

    @given(a=st.text(max_size=12), b=st.text(max_size=12), cap=st.integers(0, 6))
    @SETTINGS
    def test_cap_contract(self, a: str, b: str, cap: int) -> None:
        distance = bounded_levenshtein(a, b, cap)
        assert distance == -1 or 0 <= distance <= cap

    @given(a=st.text(max_size=12))
    @SETTINGS
    def test_identity_is_zero(self, a: str) -> None:
        assert bounded_levenshtein(a, a, 2) == 0


class TestParserProperties:
    @given(text=st.text(max_size=100))
    @SETTINGS
    def test_parser_never_raises(self, text: str) -> None:
        parsed = parse_query(text)
        assert parsed.text == " ".join(parsed.terms)

    @given(text=st.text(max_size=40))
    @SETTINGS
    def test_parser_is_deterministic(self, text: str) -> None:
        assert parse_query(text) == parse_query(text)


class TestFtsProperties:
    @given(rows=st.lists(st.tuples(_NAMES, _KINDS, _PATHS, st.integers(1, 200)), max_size=10))
    @SETTINGS
    def test_ensure_is_idempotent(self, tmp_path: Path, rows: list[tuple[str, str, str, int]]) -> None:
        store = EventStore(_example_db(tmp_path))
        try:
            _seed_symbols(store, rows)
            assert ensure_fts_index(store) is True
            assert ensure_fts_index(store) is False
        finally:
            store.close()

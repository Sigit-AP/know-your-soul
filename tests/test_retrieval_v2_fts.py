"""Unit tests for the retrieval v2 FTS5 index manager (wave W4).

The FTS5 table is module-owned (``retrieval_v2_symbols_fts``) as an
external-content view over the frozen v3 ``symbols`` table — the store
schema itself is not touched (RESEARCH-QUERY §2.1 channel 1).
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from _retrieval_v2_helpers import add_file, add_symbol
from know_your_soul.retrieval_v2 import (
    FTS_TABLE,
    META_TABLE,
    build_fts_query,
    drop_fts_index,
    ensure_fts_index,
    fts_available,
    rebuild_fts_index,
)
from know_your_soul.store import EventStore


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def _table_names(store: EventStore) -> set[str]:
    with store.read() as conn:
        rows = conn.execute("SELECT name FROM sqlite_master WHERE type IN ('table', 'view')").fetchall()
    return {str(row[0]) for row in rows}


class TestAvailability:
    def test_fts5_is_available(self, store: EventStore) -> None:
        with store.read() as conn:
            assert fts_available(conn) is True


class TestLifecycle:
    def test_ensure_creates_table_and_meta(self, store: EventStore) -> None:
        assert ensure_fts_index(store) is True
        names = _table_names(store)
        assert FTS_TABLE in names
        assert META_TABLE in names

    def test_ensure_is_idempotent_on_unchanged_symbols(self, store: EventStore) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        assert ensure_fts_index(store) is True
        assert ensure_fts_index(store) is False

    def test_ensure_rebuilds_after_insert(self, store: EventStore) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        ensure_fts_index(store)
        add_symbol(store, "beta", "function", "b.py", 2)
        assert ensure_fts_index(store) is True

    def test_ensure_rebuilds_after_update(self, store: EventStore) -> None:
        nid = add_symbol(store, "alpha", "function", "a.py", 1)
        ensure_fts_index(store)
        store.upsert_symbol(node_id=nid, name="renamed", kind="function", path="a.py", line=1, col=0)
        assert ensure_fts_index(store) is True

    def test_rebuild_returns_row_count(self, store: EventStore) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        add_symbol(store, "beta", "class", "b.py", 2)
        assert rebuild_fts_index(store) == 2

    def test_rebuild_on_empty_symbols(self, store: EventStore) -> None:
        assert rebuild_fts_index(store) == 0

    def test_drop_removes_table_and_meta(self, store: EventStore) -> None:
        ensure_fts_index(store)
        drop_fts_index(store)
        names = _table_names(store)
        assert FTS_TABLE not in names
        assert META_TABLE not in names

    def test_drop_is_idempotent(self, store: EventStore) -> None:
        drop_fts_index(store)
        drop_fts_index(store)

    def test_index_survives_reopen(self, tmp_path: Path) -> None:
        path = tmp_path / "soul.sqlite3"
        first = EventStore(path)
        add_symbol(first, "alpha", "function", "a.py", 1)
        ensure_fts_index(first)
        first.close()
        second = EventStore(path)
        try:
            assert ensure_fts_index(second) is False
        finally:
            second.close()


class TestMatch:
    def test_name_token_match(self, store: EventStore) -> None:
        add_symbol(store, "authenticate", "function", "a.py", 1)
        add_symbol(store, "unrelated", "function", "b.py", 1)
        ensure_fts_index(store)
        with store.read() as conn:
            rows = conn.execute(
                f"SELECT node_id FROM {FTS_TABLE} CROSS JOIN symbols s ON s.rowid = {FTS_TABLE}.rowid "
                f"WHERE {FTS_TABLE} MATCH ?",
                (build_fts_query(("authenticate",)),),
            ).fetchall()
        assert len(rows) == 1

    def test_prefix_match(self, store: EventStore) -> None:
        add_symbol(store, "authentication", "function", "a.py", 1)
        ensure_fts_index(store)
        query = build_fts_query(("auth",))
        with store.read() as conn:
            rows = conn.execute(f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?", (query,)).fetchall()
        assert len(rows) == 1

    def test_path_column_match(self, store: EventStore) -> None:
        add_file(store, "src/api/handler.py", "python")
        add_symbol(store, "handle", "function", "src/api/handler.py", 1)
        ensure_fts_index(store)
        with store.read() as conn:
            rows = conn.execute(
                f"SELECT rowid FROM {FTS_TABLE} WHERE {FTS_TABLE} MATCH ?",
                (build_fts_query(("handler",)),),
            ).fetchall()
        assert len(rows) == 1

    def test_bm25_name_hit_outranks_path_hit(self, store: EventStore) -> None:
        add_symbol(store, "alpha", "function", "plain.py", 1)
        add_symbol(store, "omega", "function", "alpha/deep.py", 1)
        ensure_fts_index(store)
        with store.read() as conn:
            rows = conn.execute(
                f"SELECT s.name, bm25({FTS_TABLE}, 20.0, 5.0, 1.0) AS rank "
                f"FROM {FTS_TABLE} CROSS JOIN symbols s ON s.rowid = {FTS_TABLE}.rowid "
                f"WHERE {FTS_TABLE} MATCH ? ORDER BY rank",
                (build_fts_query(("alpha",)),),
            ).fetchall()
        assert [str(row[0]) for row in rows][0] == "alpha"


class TestBuildFtsQuery:
    def test_single_token_becomes_prefix_query(self) -> None:
        assert build_fts_query(("auth",)) == '"auth"*'

    def test_tokens_joined_with_or(self) -> None:
        assert build_fts_query(("alpha", "beta")) == '"alpha"* OR "beta"*'

    def test_operator_words_dropped(self) -> None:
        assert build_fts_query(("AND", "or", "NOT", "NEAR", "keep")) == '"keep"*'

    def test_special_chars_stripped(self) -> None:
        assert build_fts_query(('au"th', "x*y", "a(b)", "c^d")) == '"auth"* OR "xy"* OR "ab"* OR "cd"*'

    def test_colons_replaced(self) -> None:
        assert build_fts_query(("pkg::mod",)) == '"pkg"* OR "mod"*'

    def test_duplicate_tokens_deduped(self) -> None:
        assert build_fts_query(("a", "a", "b")) == '"a"* OR "b"*'

    def test_empty_terms_give_empty_query(self) -> None:
        assert build_fts_query(()) == ""

    def test_only_operators_give_empty_query(self) -> None:
        assert build_fts_query(("AND", "OR")) == ""

    def test_deterministic(self) -> None:
        assert build_fts_query(("x", "y")) == build_fts_query(("x", "y"))

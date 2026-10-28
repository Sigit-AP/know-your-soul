"""FTS5 index management for retrieval v2 (wave W4).

The full-text index is a module-owned virtual table
(``retrieval_v2_symbols_fts``) — an external-content FTS5 view over the
frozen v3 ``symbols`` table (columns ``name``, ``kind``, ``path``). The
store schema is not touched; the index lives next to it and can be
dropped/rebuilt at any time (RESEARCH-QUERY §2.1 channel 1, MASTER-PLAN
pillar 4).

Freshness is fingerprint-based: ``ensure_fts_index`` rebuilds only when
the ``symbols`` fingerprint (row count + total column bytes) changed.
Callers that need guaranteed freshness after a length-preserving update
call :func:`rebuild_fts_index` explicitly (the ingestion pipeline does).
"""

from __future__ import annotations

import re
import sqlite3

from know_your_soul.store import EventStore

__all__ = [
    "FTS_TABLE",
    "META_TABLE",
    "FtsUnavailableError",
    "build_fts_query",
    "drop_fts_index",
    "ensure_fts_index",
    "fts_available",
    "rebuild_fts_index",
]

FTS_TABLE = "retrieval_v2_symbols_fts"
META_TABLE = "retrieval_v2_meta"

# characters stripped from FTS tokens (codegraph searchNodesFTS sanitiser)
_STRIP_RE = re.compile(r"['\"*():^]")
_FTS_OPERATORS = frozenset({"AND", "OR", "NOT", "NEAR"})


class FtsUnavailableError(RuntimeError):
    """Raised when the SQLite build lacks FTS5 (fail-closed)."""


def fts_available(conn: sqlite3.Connection) -> bool:
    """Probe FTS5 support with a throwaway temp virtual table."""
    try:
        conn.execute("CREATE VIRTUAL TABLE temp._retrieval_v2_fts_probe USING fts5(x)")
        conn.execute("DROP TABLE temp._retrieval_v2_fts_probe")
    except sqlite3.OperationalError:
        return False
    return True


def build_fts_query(terms: tuple[str, ...]) -> str:
    """Build the FTS5 MATCH expression: sanitised prefix tokens joined by OR.

    Mirrors codegraph: ``::`` becomes a space, ``['"*():^]`` are stripped,
    FTS operator words are dropped, and every surviving token becomes a
    quoted prefix query ``"token"*``. Empty input → empty string.
    """
    tokens: list[str] = []
    seen: set[str] = set()
    for term in terms:
        cleaned = _STRIP_RE.sub("", term.replace("::", " "))
        for token in cleaned.split():
            if not token or token.upper() in _FTS_OPERATORS or token in seen:
                continue
            seen.add(token)
            tokens.append(token)
    return " OR ".join(f'"{token}"*' for token in tokens)


def _fingerprint(conn: sqlite3.Connection) -> str:
    row = conn.execute(
        "SELECT count(*), IFNULL(sum(length(node_id) + length(name) + length(kind) + length(path)"
        " + abs(line) + abs(col)), 0) FROM symbols"
    ).fetchone()
    return f"{int(row[0])}:{int(row[1])}"


def _ensure_schema(conn: sqlite3.Connection) -> None:
    if not fts_available(conn):
        raise FtsUnavailableError("this SQLite build lacks FTS5; retrieval v2 search is unavailable")
    conn.execute(
        f"CREATE VIRTUAL TABLE IF NOT EXISTS {FTS_TABLE} "
        f"USING fts5(name, kind, path, content='symbols', content_rowid='rowid')"
    )
    conn.execute(f"CREATE TABLE IF NOT EXISTS {META_TABLE} (key TEXT PRIMARY KEY, value TEXT NOT NULL)")


def rebuild_fts_index(store: EventStore) -> int:
    """Drop and re-index every symbol row; returns the indexed row count."""
    with store.transaction() as conn:
        _ensure_schema(conn)
        # external-content tables reject raw DELETE; the FTS5 'rebuild'
        # command atomically clears the index and repopulates from symbols
        conn.execute(f"INSERT INTO {FTS_TABLE}({FTS_TABLE}) VALUES('rebuild')")  # nosec B608 -- module-owned constant table name
        count = int(conn.execute("SELECT count(*) FROM symbols").fetchone()[0])
        conn.execute(
            f"INSERT INTO {META_TABLE}(key, value) VALUES('fingerprint', ?) "  # nosec B608 -- constant table name; value bound
            f"ON CONFLICT(key) DO UPDATE SET value = excluded.value",
            (_fingerprint(conn),),
        )
    return count


def ensure_fts_index(store: EventStore) -> bool:
    """Create the index when missing; rebuild when the fingerprint drifted.

    Returns True when a (re)build happened, False when the index was
    already current.
    """
    with store.transaction() as conn:
        _ensure_schema(conn)
        row = conn.execute(
            f"SELECT value FROM {META_TABLE} WHERE key = 'fingerprint'"  # nosec B608 -- constant table name
        ).fetchone()
        current = _fingerprint(conn)
        if row is not None and str(row[0]) == current:
            return False
    rebuild_fts_index(store)
    return True


def drop_fts_index(store: EventStore) -> None:
    """Remove the index and its metadata; idempotent."""
    with store.transaction() as conn:
        conn.execute(f"DROP TABLE IF EXISTS {FTS_TABLE}")
        conn.execute(f"DROP TABLE IF EXISTS {META_TABLE}")

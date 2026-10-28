"""Symbol lookup for retrieval v2 (wave W4).

Channel cascade from codegraph ``QueryBuilder.searchNodes``
(RESEARCH-QUERY §2.1), adapted to the frozen v3 schema:

1. **FTS5 + BM25** over ``(name, kind, path)`` with column weights
   (name 20, kind 5, path 1), fetching ``max(limit*factor, floor)`` rows
   to leave room for rescoring.
2. **Exact-name supplement** — ``lower(name) = lower(?)`` hits are tagged
   ``exact`` and injected with the current maximum score.
3. **LIKE substring** fallback (only when FTS found nothing): exact 1.0,
   prefix 0.9, contains 0.8, path-contains 0.5, +0.05 per extra term hit.
4. **Bounded fuzzy** fallback (only when FTS and LIKE are empty):
   Damerau-Levenshtein with early exit, score ``0.4 / (1 + distance)`` —
   strictly below every LIKE score so tiers never mix.

Multi-signal rescoring (kind bonus + name match + path relevance) applies
to the FTS channel; ``kind:``/``lang:``/``path:``/``name:`` filters are
hard gates applied last (codegraph §2.1 step 5). Final ordering is total:
``(-score, path, line, col, node_id)``.
"""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from typing import Literal

from know_your_soul.retrieval_v2.fts import FTS_TABLE, build_fts_query, ensure_fts_index
from know_your_soul.retrieval_v2.query import ParsedQuery, parse_query
from know_your_soul.retrieval_v2.scoring import (
    bounded_levenshtein,
    fuzzy_max_distance,
    kind_bonus,
    name_match_bonus,
    path_relevance,
)
from know_your_soul.store import EventStore, SymbolRow

__all__ = ["LookupConfig", "LookupError", "ScoredSymbol", "SymbolLookup"]

LIMIT_MIN = 1
LIMIT_MAX = 100


class LookupError(ValueError):
    """Invalid lookup configuration or argument (fail-closed)."""


def _validate_limit(value: int, label: str) -> None:
    if isinstance(value, bool) or not isinstance(value, int) or not (LIMIT_MIN <= value <= LIMIT_MAX):
        raise LookupError(f"{label} must be an int in [{LIMIT_MIN}, {LIMIT_MAX}], got {value!r}")


@dataclass(frozen=True)
class LookupConfig:
    """Tuning knobs for :class:`SymbolLookup` (all deterministic)."""

    limit: int = 10
    fts_fetch_factor: int = 5
    fts_fetch_floor: int = 100
    enable_fuzzy: bool = True
    fuzzy_scan_cap: int = 5000
    bm25_name: float = 20.0
    bm25_kind: float = 5.0
    bm25_path: float = 1.0

    def __post_init__(self) -> None:
        _validate_limit(self.limit, "limit")
        for label, value in (
            ("fts_fetch_factor", self.fts_fetch_factor),
            ("fts_fetch_floor", self.fts_fetch_floor),
            ("fuzzy_scan_cap", self.fuzzy_scan_cap),
        ):
            if isinstance(value, bool) or not isinstance(value, int) or value < 1:
                raise LookupError(f"{label} must be an int >= 1, got {value!r}")
        for label, value in (
            ("bm25_name", self.bm25_name),
            ("bm25_kind", self.bm25_kind),
            ("bm25_path", self.bm25_path),
        ):
            if not isinstance(value, (int, float)) or value < 0:
                raise LookupError(f"{label} must be a number >= 0, got {value!r}")


@dataclass(frozen=True)
class ScoredSymbol:
    """One lookup hit with its score, source channel and provenance."""

    node_id: str
    name: str
    kind: str
    path: str
    line: int
    col: int
    score: float
    source: Literal["fts", "exact", "like", "fuzzy", "filter"]


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _like_score(name: str, path: str, term: str) -> float:
    lowered_name, lowered_term = name.lower(), term.lower()
    if lowered_name == lowered_term:
        return 1.0
    if lowered_name.startswith(lowered_term):
        return 0.9
    if lowered_term in lowered_name:
        return 0.8
    if lowered_term in path.lower():
        return 0.5
    return 0.0


class SymbolLookup:
    """Tiered symbol search over the v3 ``symbols`` table."""

    def __init__(self, config: LookupConfig | None = None) -> None:
        self._config = config if config is not None else LookupConfig()

    @property
    def config(self) -> LookupConfig:
        return self._config

    def search(
        self, store: EventStore, query: str | ParsedQuery, *, limit: int | None = None
    ) -> tuple[ScoredSymbol, ...]:
        """Search symbols; returns at most ``limit`` hits in canonical order."""
        effective_limit = self._config.limit if limit is None else limit
        _validate_limit(effective_limit, "limit")
        parsed = parse_query(query) if isinstance(query, str) else query
        language_map: dict[str, str] | None = None
        if parsed.languages:
            language_map = self._language_map(store)

        candidates: dict[str, ScoredSymbol] = {}
        if parsed.terms:
            candidates = self._fts_channel(store, parsed, effective_limit)
            if not candidates:
                candidates = self._like_channel(store, parsed)
            if not candidates and self._config.enable_fuzzy:
                candidates = self._fuzzy_channel(store, parsed)
        else:
            for symbol in self._all_symbols(store):
                candidates[symbol.node_id] = ScoredSymbol(
                    node_id=symbol.node_id,
                    name=symbol.name,
                    kind=symbol.kind,
                    path=symbol.path,
                    line=symbol.line,
                    col=symbol.col,
                    score=0.0,
                    source="filter",
                )

        gated = [hit for hit in candidates.values() if self._passes_filters(hit, parsed, language_map)]
        gated.sort(key=lambda hit: (-hit.score, hit.path, hit.line, hit.col, hit.node_id))
        return tuple(gated[:effective_limit])

    # ------------------------------------------------------------ channels

    def _fts_channel(self, store: EventStore, parsed: ParsedQuery, limit: int) -> dict[str, ScoredSymbol]:
        ensure_fts_index(store)
        fts_query = build_fts_query(parsed.terms)
        candidates: dict[str, ScoredSymbol] = {}
        if fts_query:
            fetch = max(limit * self._config.fts_fetch_factor, self._config.fts_fetch_floor)
            sql = (
                f"SELECT s.node_id, s.name, s.kind, s.path, s.line, s.col, "
                f"bm25({FTS_TABLE}, {float(self._config.bm25_name)}, {float(self._config.bm25_kind)}, "  # nosec B608
                f"{float(self._config.bm25_path)}) AS rank "  # nosec B608
                # FTS5 quirk: MATCH must name the table, not an alias, and
                # CROSS JOIN keeps the FTS table left-most (codegraph shape)
                f"FROM {FTS_TABLE} CROSS JOIN symbols AS s ON s.rowid = {FTS_TABLE}.rowid "
                f"WHERE {FTS_TABLE} MATCH ? ORDER BY rank LIMIT ?"
            )
            try:
                with store.read() as conn:
                    rows = conn.execute(sql, (fts_query, fetch)).fetchall()
            except sqlite3.OperationalError:
                rows = []  # sanitised query should never fail; never crash a search
            for row in rows:
                base = -float(row["rank"])  # bm25 is negative; smaller is better
                score = (
                    base
                    + kind_bonus(str(row["kind"]))
                    + name_match_bonus(str(row["name"]), parsed.terms)
                    + path_relevance(str(row["path"]), parsed.terms)
                )
                candidates[str(row["node_id"])] = ScoredSymbol(
                    node_id=str(row["node_id"]),
                    name=str(row["name"]),
                    kind=str(row["kind"]),
                    path=str(row["path"]),
                    line=int(row["line"]),
                    col=int(row["col"]),
                    score=score,
                    source="fts",
                )
        # exact-name supplement (codegraph tier 4): tagged 'exact', injected
        # with the current maximum score so an exact name never loses to a
        # fuzzy BM25 neighbour
        if len(parsed.terms) == 1:
            term = parsed.terms[0]
            supplement = [
                symbol
                for symbol in store.symbols_named(term, case_insensitive=True)
                if symbol.name.lower() == term.lower()
            ]
            if supplement:
                top = max((hit.score for hit in candidates.values()), default=0.0)
                for symbol in supplement:
                    existing = candidates.get(symbol.node_id)
                    candidates[symbol.node_id] = ScoredSymbol(
                        node_id=symbol.node_id,
                        name=symbol.name,
                        kind=symbol.kind,
                        path=symbol.path,
                        line=symbol.line,
                        col=symbol.col,
                        score=max(top, existing.score) if existing is not None else top,
                        source="exact",
                    )
        return candidates

    def _like_channel(self, store: EventStore, parsed: ParsedQuery) -> dict[str, ScoredSymbol]:
        clauses: list[str] = []
        params: list[str] = []
        for term in parsed.terms:
            escaped = _escape_like(term)
            clauses.append("(name LIKE ? ESCAPE '\\' OR path LIKE ? ESCAPE '\\')")
            params.extend((f"%{escaped}%", f"%{escaped}%"))
        if not clauses:
            return {}
        sql = f"SELECT * FROM symbols WHERE {' OR '.join(clauses)} ORDER BY path, line, col, node_id"  # nosec B608 -- constant fragments; terms are bound params
        with store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        candidates: dict[str, ScoredSymbol] = {}
        for row in rows:
            scores = [
                score for term in parsed.terms if (score := _like_score(str(row["name"]), str(row["path"]), term)) > 0.0
            ]
            if not scores:
                continue
            candidates[str(row["node_id"])] = ScoredSymbol(
                node_id=str(row["node_id"]),
                name=str(row["name"]),
                kind=str(row["kind"]),
                path=str(row["path"]),
                line=int(row["line"]),
                col=int(row["col"]),
                score=max(scores) + 0.05 * (len(scores) - 1),
                source="like",
            )
        return candidates

    def _fuzzy_channel(self, store: EventStore, parsed: ParsedQuery) -> dict[str, ScoredSymbol]:
        terms = [term for term in parsed.terms if fuzzy_max_distance(len(term)) > 0]
        if not terms:
            return {}
        candidates: dict[str, ScoredSymbol] = {}
        for symbol in self._all_symbols(store, limit=self._config.fuzzy_scan_cap):
            best: float = 0.0
            for term in terms:
                distance = bounded_levenshtein(term.lower(), symbol.name.lower(), fuzzy_max_distance(len(term)))
                if distance >= 0:
                    best = max(best, 0.4 / (1.0 + distance))
            if best > 0.0:
                candidates[symbol.node_id] = ScoredSymbol(
                    node_id=symbol.node_id,
                    name=symbol.name,
                    kind=symbol.kind,
                    path=symbol.path,
                    line=symbol.line,
                    col=symbol.col,
                    score=best,
                    source="fuzzy",
                )
        return candidates

    # ------------------------------------------------------------ helpers

    @staticmethod
    def _all_symbols(store: EventStore, limit: int | None = None) -> list[SymbolRow]:
        sql = "SELECT * FROM symbols ORDER BY path, line, col, node_id"
        params: tuple[int, ...] = ()
        if limit is not None:
            sql += " LIMIT ?"
            params = (int(limit),)
        with store.read() as conn:
            rows = conn.execute(sql, params).fetchall()
        return [
            SymbolRow(
                node_id=str(row["node_id"]),
                name=str(row["name"]),
                kind=str(row["kind"]),
                path=str(row["path"]),
                line=int(row["line"]),
                col=int(row["col"]),
            )
            for row in rows
        ]

    @staticmethod
    def _language_map(store: EventStore) -> dict[str, str]:
        with store.read() as conn:
            rows = conn.execute("SELECT path, language FROM files").fetchall()
        return {str(row["path"]): str(row["language"]) for row in rows}

    @staticmethod
    def _passes_filters(hit: ScoredSymbol, parsed: ParsedQuery, language_map: dict[str, str] | None) -> bool:
        if parsed.kinds and hit.kind not in parsed.kinds:
            return False
        if parsed.path_filters and not all(f in hit.path for f in parsed.path_filters):
            return False
        if parsed.name_filters and not all(f.lower() in hit.name.lower() for f in parsed.name_filters):
            return False
        if parsed.languages:
            assert language_map is not None
            if language_map.get(hit.path) not in parsed.languages:
                return False
        return True

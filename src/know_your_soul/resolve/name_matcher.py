"""Structure-tolerant name matching — the post-primary resolver tier (wave W7).

Adapts the *pattern* of codegraph's ``name-matcher.ts`` (28K LOC,
RESEARCH-COVERAGE-AUDIT §4.3) and its segment-vocabulary search channel
(RESEARCH-QUERY §2.1) to this project's fail-closed resolver cascade —
rewritten, not ported. What survives of the original design:

* identifiers are split into a **segment vocabulary** (camelCase humps,
  ``snake_case`` words, acronym runs, digit boundaries, ``::``/``.``
  separators) so ``parseHTTPResponse`` and ``parse_http_response`` share one
  structure;
* matching is **multi-signal rescoring**: segment overlap (Jaccard +
  query-containment) plus weighted prefix/substring bonuses;
* a **ubiquity ceiling** (codegraph ``AMBIGUOUS_NAME_CEILING = 500``) refuses
  names that match the world, keeping the tier O(bounded) and honest;
* **language-family gating** (codegraph ``gateLanguageMatch``): candidates
  must live in the same language family as the source file.

What is deliberately dropped: fuzzy Levenshtein scoring, receiver-type
inference, per-language scope tables — none of which the schema-v4 store can
ground truthfully.

Fail-closed contract
--------------------
The tier runs *after* the primary cascade (exact/name_tail/import_path) and
only ever retries refs whose persisted failure reason is exactly
``no_resolution``. Ambiguous primary verdicts (``ambiguous_*``) are terminal.
A match requires one candidate scoring at least ``min_score`` with a
``min_margin`` gap to the runner-up; otherwise the ref stays failed,
untouched, so the pass is idempotent by construction. All scores are exact
:class:`fractions.Fraction` values — binary floats never enter the pipeline.

Batch discipline mirrors :class:`~know_your_soul.resolve.resolver.ReferenceResolver`:
one pure read phase, one ``add_graph_edges`` transaction, then one
``record_resolution_attempt`` per hit (``retry_failed_ref`` first, because the
store requires attempts to start from ``pending``). A crash between the write
phases replays cleanly: edge identity dedups and the ref is simply retried.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Iterable
from dataclasses import dataclass
from fractions import Fraction
from pathlib import PurePosixPath
from typing import Literal

from know_your_soul.domain import canonical_json
from know_your_soul.graph_model import is_file_node_id
from know_your_soul.resolve.resolver import REF_KIND_TO_EDGE_KIND
from know_your_soul.store import EventStore, SymbolRow, UnresolvedRefRow

__all__ = [
    "NAME_MATCHER_VERSION",
    "PROVENANCE_NAME_MATCH",
    "NameMatch",
    "NameMatchConfig",
    "NameMatchError",
    "NameMatchOutcome",
    "NameMatchReport",
    "NameMatchResolver",
    "match_name",
    "score_name_pair",
    "split_identifier",
]

NAME_MATCHER_VERSION: int = 1

#: Provenance stamp on every name-match-minted edge — distinct from both the
#: parse stamp (``ingest:*``) and the primary resolver stamp, so audit can
#: always tell *which* tier answered.
PROVENANCE_NAME_MATCH: str = f"resolver:name-match:v{NAME_MATCHER_VERSION}"

#: The only primary failure reason this tier retries (fail-closed).
_RETRYABLE_REASON: str = "no_resolution"

#: Extension → language fallback, mirroring the resolver's table (private to
#: it, so duplicated here deliberately — same values, same purpose).
_EXTENSION_LANGUAGES: dict[str, str] = {
    ".py": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascriptreact",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".tsx": "typescriptreact",
}

_JS_FAMILY: frozenset[str] = frozenset({"javascript", "javascriptreact", "typescript", "typescriptreact"})

#: codegraph LANGUAGE_FAMILY: JS and TS may see each other; everything else
#: matches within its own language only.
_LANGUAGE_FAMILIES: dict[str, frozenset[str]] = {
    "python": frozenset({"python"}),
    "javascript": _JS_FAMILY,
    "javascriptreact": _JS_FAMILY,
    "typescript": _JS_FAMILY,
    "typescriptreact": _JS_FAMILY,
}


class NameMatchError(ValueError):
    """Invalid matcher configuration or input (fail-closed)."""


# ------------------------------------------------------------- segment vocab


def _case_splits(chunk: str) -> Iterable[str]:
    """Split one alphanumeric chunk on case/digit boundaries.

    Boundaries: lower/digit → upper (``parseHTTP``), acronym tail
    (``HTTPResponse`` → ``HTTP`` + ``Response``), and letter ↔ digit.
    """
    start = 0
    length = len(chunk)
    for index in range(1, length):
        previous = chunk[index - 1]
        current = chunk[index]
        following = chunk[index + 1] if index + 1 < length else ""
        boundary = (
            (previous.islower() and current.isupper())
            or (previous.isalpha() != current.isalpha())
            or (previous.isupper() and current.isupper() and following.islower())
        )
        if boundary:
            yield chunk[start:index]
            start = index
    yield chunk[start:]


def split_identifier(name: str) -> tuple[str, ...]:
    """Split an identifier into its lowercase segment vocabulary.

    Separators (``_``, ``.``, ``::``, ``-``, ``/`` …) split first, then each
    chunk splits on camelCase/acronym/digit boundaries. Empty segments are
    dropped. Deterministic and total over all strings.
    """
    if not isinstance(name, str):
        raise NameMatchError(f"name must be a str, got {type(name).__name__}")
    segments: list[str] = []
    chunk: list[str] = []
    chunks: list[str] = []
    for character in name:
        if character.isalnum():
            chunk.append(character)
        elif chunk:
            chunks.append("".join(chunk))
            chunk = []
    if chunk:
        chunks.append("".join(chunk))
    for piece in chunks:
        for segment in _case_splits(piece):
            lowered = segment.lower()
            if lowered:
                segments.append(lowered)
    return tuple(segments)


# ------------------------------------------------------------------- scoring


def _check_fraction(value: object, label: str) -> Fraction:
    if isinstance(value, bool) or isinstance(value, float):
        raise NameMatchError(f"{label} must be a Fraction/int/str, never a binary float")
    try:
        result = Fraction(value)  # type: ignore[arg-type]
    except (TypeError, ValueError, ZeroDivisionError) as error:
        raise NameMatchError(f"{label} is not a rational value: {value!r}") from error
    if not (Fraction(0) <= result <= Fraction(1)):
        raise NameMatchError(f"{label} must be within [0, 1], got {result}")
    return result


def _check_int(value: object, label: str) -> int:
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NameMatchError(f"{label} must be an int >= 1, got {value!r}")
    return value


@dataclass(frozen=True)
class NameMatchConfig:
    """Tuning knobs for the matcher (all deterministic, all validated)."""

    min_score: Fraction = Fraction(3, 4)
    min_margin: Fraction = Fraction(1, 20)
    candidate_cap: int = 500  # codegraph AMBIGUOUS_NAME_CEILING
    max_query_segments: int = 8
    prefix_bonus: Fraction = Fraction(1, 10)
    substring_bonus: Fraction = Fraction(1, 20)

    def __post_init__(self) -> None:
        object.__setattr__(self, "min_score", _check_fraction(self.min_score, "min_score"))
        object.__setattr__(self, "min_margin", _check_fraction(self.min_margin, "min_margin"))
        object.__setattr__(self, "prefix_bonus", _check_fraction(self.prefix_bonus, "prefix_bonus"))
        object.__setattr__(self, "substring_bonus", _check_fraction(self.substring_bonus, "substring_bonus"))
        _check_int(self.candidate_cap, "candidate_cap")
        _check_int(self.max_query_segments, "max_query_segments")


def score_name_pair(query: str, candidate: str, *, config: NameMatchConfig | None = None) -> Fraction:
    """Score one (query, candidate) pair in ``[0, 1]`` as an exact fraction.

    Signals (weights fixed by convention, mirrors codegraph's multi-signal
    rescoring in spirit):

    * exact case-insensitive equality → 1;
    * segment overlap: ``(jaccard + 2 * query-containment) / 3`` — a query
      whose segments are all present in the candidate is strong evidence;
    * ``prefix_bonus`` when the candidate starts with the query text, else
      ``substring_bonus`` when the query text occurs inside the candidate.
    """
    config = config if config is not None else NameMatchConfig()
    if not query or not candidate:
        return Fraction(0)
    query_lower = query.lower()
    candidate_lower = candidate.lower()
    if query_lower == candidate_lower:
        return Fraction(1)
    query_segments = frozenset(split_identifier(query))
    candidate_segments = frozenset(split_identifier(candidate))
    if not query_segments or not candidate_segments:
        return Fraction(0)
    overlap = len(query_segments & candidate_segments)
    if overlap == 0:
        return Fraction(0)
    jaccard = Fraction(overlap, len(query_segments | candidate_segments))
    containment = Fraction(overlap, len(query_segments))
    base = (jaccard + 2 * containment) / 3
    if candidate_lower.startswith(query_lower):
        bonus = config.prefix_bonus
    elif query_lower in candidate_lower:
        bonus = config.substring_bonus
    else:
        bonus = Fraction(0)
    return min(Fraction(1), base + bonus)


@dataclass(frozen=True)
class NameMatch:
    """The deterministic winner of one matching round."""

    node_id: str
    name: str
    score: Fraction
    margin: Fraction  # score minus the runner-up's score (full score when alone)


def match_name(
    query: str,
    candidates: Iterable[tuple[str, str]],
    *,
    config: NameMatchConfig | None = None,
) -> NameMatch | None:
    """Pick the unique decisive candidate for ``query`` or return ``None``.

    ``candidates`` is an iterable of ``(node_id, name)``. Fail-closed: no
    candidate above ``min_score`` → ``None``; a runner-up within
    ``min_margin`` of the top → ``None`` (ambiguity never guesses).
    """
    config = config if config is not None else NameMatchConfig()
    scored: dict[str, tuple[str, Fraction]] = {}
    for node_id, name in candidates:
        if node_id in scored:
            continue
        score = score_name_pair(query, name, config=config)
        if score >= config.min_score:
            scored[node_id] = (name, score)
    if not scored:
        return None
    ordered = sorted(scored.items(), key=lambda item: (-item[1][1], item[0]))
    top_id, (top_name, top_score) = ordered[0]
    runner_up = ordered[1][1][1] if len(ordered) > 1 else Fraction(0)
    margin = top_score - runner_up
    if len(ordered) > 1 and margin < config.min_margin:
        return None
    return NameMatch(node_id=top_id, name=top_name, score=top_score, margin=margin)


# ------------------------------------------------------------ store-bound tier


def _escape_like(value: str) -> str:
    return value.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")


def _symbol_row(row: sqlite3.Row) -> SymbolRow:
    return SymbolRow(
        node_id=str(row["node_id"]),
        name=str(row["name"]),
        kind=str(row["kind"]),
        path=str(row["path"]),
        line=int(row["line"]),
        col=int(row["col"]),
    )


def _ref_row(row: sqlite3.Row) -> UnresolvedRefRow:
    reason = row["failure_reason"]
    return UnresolvedRefRow(
        ref_id=int(row["ref_id"]),
        source_node_id=str(row["source_node_id"]),
        target_name=str(row["target_name"]),
        name_tail=str(row["name_tail"]),
        kind=str(row["kind"]),
        status=str(row["status"]),
        first_seen=str(row["first_seen"]),
        attempts=int(row["attempts"]),
        failure_reason=None if reason is None else str(reason),
    )


@dataclass(frozen=True)
class NameMatchOutcome:
    """The deterministic verdict for one retried ref."""

    ref_id: int
    status: Literal["resolved", "unmatched"]
    target_node_id: str | None
    score: Fraction | None
    reason: str | None  # report-only for unmatched; the store row is untouched


@dataclass(frozen=True)
class NameMatchReport:
    """Per-pass counts; ``outcomes`` is sorted by ``ref_id``."""

    processed: int
    resolved: int
    unmatched: int
    edges_inserted: int
    outcomes: tuple[NameMatchOutcome, ...]


def _validate_limit(value: int | None) -> None:
    if value is None:
        return
    if isinstance(value, bool) or not isinstance(value, int) or value < 1:
        raise NameMatchError(f"limit must be None or an int >= 1, got {value!r}")


class NameMatchResolver:
    """Retries ``no_resolution`` failures with structure-tolerant matching."""

    def __init__(self, config: NameMatchConfig | None = None) -> None:
        self._config = config if config is not None else NameMatchConfig()

    @property
    def config(self) -> NameMatchConfig:
        return self._config

    def resolve_unmatched(self, store: EventStore, *, limit: int | None = None) -> NameMatchReport:
        """One bounded pass over refs the primary cascade failed ``no_resolution``.

        Refs the matcher cannot resolve stay failed with their original
        reason — the store is only ever mutated on a decisive hit, which is
        what makes the pass idempotent.
        """
        _validate_limit(limit)
        refs = self._retryable_refs(store, limit)
        decisions: list[tuple[UnresolvedRefRow, NameMatch | None, str | None]] = []
        for ref in refs:
            match, reason = self._match_ref(store, ref)
            decisions.append((ref, match, reason))
        edge_payloads: list[dict[str, object]] = []
        for ref, match, _reason in decisions:
            if match is None:
                continue
            edge_payloads.append(
                {
                    "source": ref.source_node_id,
                    "target": match.node_id,
                    "kind": REF_KIND_TO_EDGE_KIND[ref.kind],
                    "metadata": canonical_json(
                        {
                            "ref_id": ref.ref_id,
                            "ref_name": ref.target_name,
                            "strategy": "name_match",
                            "score": f"{match.score.numerator}/{match.score.denominator}",
                        }
                    ),
                    "provenance": PROVENANCE_NAME_MATCH,
                }
            )
        edges_inserted = store.add_graph_edges(edge_payloads) if edge_payloads else 0
        outcomes: list[NameMatchOutcome] = []
        for ref, match, reason in decisions:
            if match is None:
                outcomes.append(
                    NameMatchOutcome(
                        ref_id=ref.ref_id,
                        status="unmatched",
                        target_node_id=None,
                        score=None,
                        reason=reason,
                    )
                )
                continue
            # The store requires attempts to start from pending.
            store.retry_failed_ref(ref.ref_id)
            store.record_resolution_attempt(ref.ref_id, outcome="resolved")
            outcomes.append(
                NameMatchOutcome(
                    ref_id=ref.ref_id,
                    status="resolved",
                    target_node_id=match.node_id,
                    score=match.score,
                    reason=None,
                )
            )
        outcomes.sort(key=lambda outcome: outcome.ref_id)
        resolved = sum(1 for outcome in outcomes if outcome.status == "resolved")
        return NameMatchReport(
            processed=len(outcomes),
            resolved=resolved,
            unmatched=len(outcomes) - resolved,
            edges_inserted=edges_inserted,
            outcomes=tuple(outcomes),
        )

    # ---------------------------------------------------------- internals

    def _retryable_refs(self, store: EventStore, limit: int | None) -> list[UnresolvedRefRow]:
        sql = "SELECT * FROM unresolved_refs WHERE status = 'failed' AND failure_reason = ? ORDER BY first_seen, ref_id"
        parameters: tuple[object, ...] = (_RETRYABLE_REASON,)
        if limit is not None:
            sql += " LIMIT ?"
            parameters = (_RETRYABLE_REASON, limit)
        with store.read() as conn:
            rows = conn.execute(sql, parameters).fetchall()
        return [_ref_row(row) for row in rows]

    def _source_language(self, store: EventStore, source_node_id: str) -> str | None:
        path: str | None = None
        symbol = store.symbol_by_node(source_node_id)
        if symbol is not None:
            path = symbol.path
        elif is_file_node_id(source_node_id):
            candidate = source_node_id[len("file:") :]
            if store.get_file(candidate) is not None:
                path = candidate
        if path is None:
            return None
        file_row = store.get_file(path)
        if file_row is not None and file_row.language != "unknown":
            return file_row.language
        return _EXTENSION_LANGUAGES.get(PurePosixPath(path).suffix)

    def _match_ref(self, store: EventStore, ref: UnresolvedRefRow) -> tuple[NameMatch | None, str | None]:
        """(match, report-only reason when unmatched). Pure reads only."""
        if ref.kind not in REF_KIND_TO_EDGE_KIND:
            return None, "unknown_ref_kind"
        language = self._source_language(store, ref.source_node_id)
        if language is None:
            return None, "unknown_source"
        family = _LANGUAGE_FAMILIES.get(language)
        if family is None:
            return None, "unknown_language"
        candidates, capped = self._candidates(store, ref, family)
        if capped:
            return None, "candidate_cap_exceeded"
        if not candidates:
            return None, "no_candidate"
        # Score the written form; for qualified targets also score the tail
        # and keep the stronger verdict (both deterministic).
        queries = [ref.target_name]
        if ref.name_tail and ref.name_tail != ref.target_name:
            queries.append(ref.name_tail)
        best: NameMatch | None = None
        for query in queries:
            match = match_name(query, ((c.node_id, c.name) for c in candidates), config=self._config)
            if match is not None and (best is None or match.score > best.score):
                best = match
        if best is None:
            return None, "below_threshold_or_ambiguous"
        return best, None

    def _candidates(
        self, store: EventStore, ref: UnresolvedRefRow, family: frozenset[str]
    ) -> tuple[list[SymbolRow], bool]:
        """Symbols sharing >=1 query segment, language-gated, capped fail-closed."""
        segments = list(dict.fromkeys(split_identifier(ref.name_tail or ref.target_name)))
        segments = segments[: self._config.max_query_segments]
        found: dict[str, SymbolRow] = {}
        capped = False
        cap = self._config.candidate_cap
        with store.read() as conn:
            # Case-insensitive exact names are always candidates.
            for row in conn.execute(
                "SELECT s.* FROM symbols s JOIN files f ON f.path = s.path "
                "WHERE lower(s.name) = lower(?) AND f.language IN ({}) "
                "ORDER BY s.path, s.line, s.col, s.node_id".format(", ".join("?" for _ in family)),
                (ref.name_tail or ref.target_name, *sorted(family)),
            ).fetchall():
                symbol = _symbol_row(row)
                found.setdefault(symbol.node_id, symbol)
            for segment in segments:
                if len(found) >= cap:
                    capped = True
                    break
                rows = conn.execute(
                    "SELECT s.* FROM symbols s JOIN files f ON f.path = s.path "
                    "WHERE lower(s.name) LIKE ? ESCAPE '\\' AND f.language IN ({}) "
                    "ORDER BY s.path, s.line, s.col, s.node_id LIMIT ?".format(", ".join("?" for _ in family)),
                    (f"%{_escape_like(segment.lower())}%", *sorted(family), cap + 1 - len(found)),
                ).fetchall()
                if len(rows) > cap - len(found):
                    capped = True
                for row in rows:
                    symbol = _symbol_row(row)
                    if len(found) >= cap:
                        capped = True
                        break
                    found.setdefault(symbol.node_id, symbol)
                if capped:
                    break
        return sorted(found.values(), key=lambda s: (s.path, s.line, s.col, s.node_id)), capped

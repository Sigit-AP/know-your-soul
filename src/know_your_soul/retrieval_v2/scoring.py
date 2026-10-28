"""Scoring helpers for retrieval v2 (wave W4).

Constants and shapes follow codegraph ``src/search/query-utils.ts``
(RESEARCH-QUERY §2.1): nameMatchBonus (exact 80 / exact-token 60 /
prefix 10+30*ratio / all-terms 15 / substring 10), kindBonus
(function/method 10, interface/type 9, class 8, …) and
scorePathRelevance (+10 filename word, +5 directory word, +3 substring,
−15 test path unless the query mentions tests).

Everything is a pure function — no store access, no clocks, no randomness.
"""

from __future__ import annotations

import re

__all__ = [
    "CHARS_PER_TOKEN",
    "KIND_BONUSES",
    "bounded_levenshtein",
    "estimate_tokens",
    "fuzzy_max_distance",
    "is_test_path",
    "kind_bonus",
    "name_match_bonus",
    "path_relevance",
    "split_identifier",
]

#: Deterministic token estimate: one token per four characters, rounded up
#: (codegraph uses the same 4-chars-per-token rule of thumb for budgeting).
CHARS_PER_TOKEN: int = 4

#: kindBonus table (codegraph query-utils.ts:483), mapped onto the frozen
#: v3 node kinds (graph_model.CoreNodeKind).
KIND_BONUSES: dict[str, float] = {
    "function": 10.0,
    "method": 10.0,
    "interface": 9.0,
    "type": 9.0,
    "type_alias": 9.0,
    "class": 8.0,
    "section": 6.0,
    "variable": 5.0,
    "constant": 5.0,
    "document": 4.0,
    "module": 3.0,
    "page": 2.0,
    "paragraph": 2.0,
    "import": 1.0,
    "file": 0.0,
}

_NON_ALNUM_RE = re.compile(r"[^0-9A-Za-z]+")
# camelCase / PascalCase / HTTPRequest boundaries
_CAMEL_RE = re.compile(r"(?<=[a-z0-9])(?=[A-Z])|(?<=[A-Z])(?=[A-Z][a-z])")

_TEST_SEGMENTS: frozenset[str] = frozenset({"test", "tests", "testing", "__tests__", "spec", "specs"})
_TEST_WORDS: frozenset[str] = frozenset({"test", "tests", "testing", "spec"})


def estimate_tokens(text: str) -> int:
    """Deterministic token estimate: ``ceil(len(text) / CHARS_PER_TOKEN)``."""
    if not text:
        return 0
    return (len(text) + CHARS_PER_TOKEN - 1) // CHARS_PER_TOKEN


def split_identifier(value: str) -> list[str]:
    """Split an identifier/path fragment into lowercase words (snake, kebab,
    dot and camel boundaries)."""
    words: list[str] = []
    for piece in _NON_ALNUM_RE.split(value):
        if not piece:
            continue
        words.extend(word.lower() for word in _CAMEL_RE.split(piece) if word)
    return words


def kind_bonus(kind: str) -> float:
    """Static per-kind ranking bonus; unknown kinds score zero."""
    return KIND_BONUSES.get(kind, 0.0)


def name_match_bonus(name: str, terms: tuple[str, ...]) -> float:
    """Name-similarity bonus; the best applicable rule wins.

    Single-term queries use the full codegraph ladder (exact 80, exact
    identifier-token 60, prefix ``10 + 30 * ratio``, substring 10).
    Multi-term queries score exact/exact-token per term and an extra 15
    when *every* term hits the name (codegraph multi-term rule).
    """
    meaningful = [term.lower() for term in terms if term]
    if not meaningful or not name:
        return 0.0
    lowered = name.lower()
    tokens = set(split_identifier(name))
    best = 0.0
    hits = 0
    for term in meaningful:
        if lowered == term:
            best = max(best, 80.0)
            hits += 1
        elif term in tokens:
            best = max(best, 60.0)
            hits += 1
        elif term in lowered:
            hits += 1  # substring hit: counts toward the all-terms bonus only
    if len(meaningful) >= 2:
        if hits == len(meaningful):
            best = max(best, 15.0)
        return best
    term = meaningful[0]
    if best == 0.0:
        if lowered.startswith(term):
            best = 10.0 + 30.0 * len(term) / len(lowered)
        elif term in lowered:
            best = 10.0
    return best


def is_test_path(path: str) -> bool:
    """Word-boundary test-file predicate (``contest.py`` is not a test)."""
    parts = [part for part in path.replace("\\", "/").split("/") if part]
    for directory in parts[:-1]:
        if directory.lower() in _TEST_SEGMENTS:
            return True
    if parts:
        filename = parts[-1].lower()
        stem = filename.rsplit(".", 1)[0] if "." in filename else filename
        if set(split_identifier(stem)) & _TEST_WORDS:
            return True
    return False


def path_relevance(path: str, terms: tuple[str, ...]) -> float:
    """Path-similarity bonus: +10 filename word, +5 directory word, +3
    substring; −15 for test paths unless the query is about tests."""
    lowered_terms = {term.lower() for term in terms if term}
    if not lowered_terms:
        return 0.0
    parts = [part for part in path.split("/") if part]
    filename = parts[-1] if parts else path
    stem = filename.rsplit(".", 1)[0] if "." in filename else filename
    file_words = set(split_identifier(stem))
    dir_words: set[str] = set()
    for directory in parts[:-1]:
        dir_words.update(split_identifier(directory))
    lowered_path = path.lower()
    score = 0.0
    for term in sorted(lowered_terms):
        if term in file_words:
            score += 10.0
        elif term in dir_words:
            score += 5.0
        elif term in lowered_path:
            score += 3.0
    if is_test_path(path) and not (lowered_terms & _TEST_WORDS):
        score -= 15.0
    return score


def fuzzy_max_distance(term_length: int) -> int:
    """Edit-distance ceiling per term length (codegraph fuzzy gate: terms
    shorter than 3 characters are never fuzzy-matched)."""
    if term_length < 3:
        return 0
    if term_length < 6:
        return 1
    return 2


def bounded_levenshtein(a: str, b: str, max_dist: int) -> int:
    """Damerau-Levenshtein distance with early exit (codegraph
    ``boundedEditDistance``).

    Returns the exact distance when it is ``<= max_dist`` and ``-1``
    otherwise. Rows are capped at ``max_dist + 1``; capping never corrupts
    an exact result because every cell on a minimal path is ``<=`` the true
    distance.
    """
    if isinstance(max_dist, bool) or not isinstance(max_dist, int) or max_dist < 0:
        raise ValueError(f"max_dist must be an int >= 0, got {max_dist!r}")
    if a == b:
        return 0
    len_a, len_b = len(a), len(b)
    if abs(len_a - len_b) > max_dist:
        return -1
    cap = max_dist + 1
    prev2: list[int] | None = None
    prev = list(range(len_b + 1))
    for i in range(1, len_a + 1):
        cur = [i] + [0] * len_b
        row_min = cur[0]
        for j in range(1, len_b + 1):
            cost = 0 if a[i - 1] == b[j - 1] else 1
            value = min(prev[j] + 1, cur[j - 1] + 1, prev[j - 1] + cost)
            if prev2 is not None and a[i - 1] == b[j - 2] and a[i - 2] == b[j - 1]:
                value = min(value, prev2[j - 2] + 1)
            if value > max_dist:
                value = cap
            cur[j] = value
            if value < row_min:
                row_min = value
        if row_min > max_dist:
            return -1
        prev2, prev = prev, cur
    result = prev[len_b]
    return result if result <= max_dist else -1

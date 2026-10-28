"""Unit tests for retrieval v2 symbol lookup (wave W4).

Channel cascade from codegraph ``QueryBuilder.searchNodes``
(RESEARCH-QUERY §2.1): FTS5 BM25 (column-weighted) + exact-name supplement
→ LIKE substring → bounded fuzzy. Multi-signal rescoring on the FTS
channel; ``kind:/lang:/path:/name:`` filters are hard gates applied last.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from _retrieval_v2_helpers import add_file, add_symbol
from know_your_soul.retrieval_v2 import (
    LookupConfig,
    LookupError,
    ScoredSymbol,
    SymbolLookup,
    parse_query,
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
def lookup() -> SymbolLookup:
    return SymbolLookup()


def names(results: tuple[ScoredSymbol, ...]) -> list[str]:
    return [result.name for result in results]


class TestConfigValidation:
    def test_default_config(self) -> None:
        config = LookupConfig()
        assert config.limit == 10
        assert config.enable_fuzzy is True

    def test_limit_zero_rejected(self) -> None:
        with pytest.raises(LookupError):
            LookupConfig(limit=0)

    def test_limit_above_max_rejected(self) -> None:
        with pytest.raises(LookupError):
            LookupConfig(limit=101)

    def test_limit_at_max_accepted(self) -> None:
        assert LookupConfig(limit=100).limit == 100

    def test_bool_limit_rejected(self) -> None:
        with pytest.raises(LookupError):
            LookupConfig(limit=True)  # type: ignore[arg-type]

    def test_negative_fetch_factor_rejected(self) -> None:
        with pytest.raises(LookupError):
            LookupConfig(fts_fetch_factor=0)

    def test_search_limit_override_validated(self, store: EventStore, lookup: SymbolLookup) -> None:
        with pytest.raises(LookupError):
            lookup.search(store, "x", limit=0)

    def test_search_limit_override(self, store: EventStore, lookup: SymbolLookup) -> None:
        for index in range(5):
            add_symbol(store, f"alpha{index}", "function", "a.py", index + 1)
        results = lookup.search(store, "alpha", limit=3)
        assert len(results) == 3


class TestFtsChannel:
    def test_exact_term_found_via_fts(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "authenticate", "function", "a.py", 1)
        add_symbol(store, "unrelated", "function", "b.py", 1)
        results = lookup.search(store, "authenticate")
        assert names(results) == ["authenticate"]
        assert results[0].source == "exact"

    def test_prefix_match_via_fts(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "authentication", "function", "a.py", 1)
        results = lookup.search(store, "xxxauthentication")  # distance 3: beyond fuzzy bound
        assert results == ()
        results = lookup.search(store, "authentic")
        assert names(results) == ["authentication"]

    def test_no_match_returns_empty(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        assert lookup.search(store, "zzzzzzzz") == ()

    def test_results_carry_provenance(self, store: EventStore, lookup: SymbolLookup) -> None:
        nid = add_symbol(store, "authenticate", "function", "src/auth.py", 7, 3)
        results = lookup.search(store, "authenticate")
        assert results[0].node_id == nid
        assert results[0].path == "src/auth.py"
        assert results[0].line == 7
        assert results[0].col == 3
        assert results[0].kind == "function"

    def test_function_outranks_paragraph(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "cache", "paragraph", "doc.md", 1)
        add_symbol(store, "cache", "function", "a.py", 1)
        results = lookup.search(store, "cache")
        assert results[0].kind == "function"

    def test_exact_name_beats_prefix_name(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "auth", "function", "a.py", 1)
        add_symbol(store, "authentication", "function", "b.py", 1)
        results = lookup.search(store, "auth")
        assert results[0].name == "auth"

    def test_path_relevance_lifts_matching_file(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "handle", "function", "src/auth.py", 1)
        add_symbol(store, "handle", "function", "src/other.py", 1)
        results = lookup.search(store, "handle auth")
        assert results[0].path == "src/auth.py"

    def test_test_file_penalized(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "helper", "function", "src/helper.py", 1)
        add_symbol(store, "helper", "function", "tests/test_helper.py", 1)
        results = lookup.search(store, "helper")
        assert results[0].path == "src/helper.py"

    def test_deterministic_ordering_ties_broken_canonically(self, store: EventStore, lookup: SymbolLookup) -> None:
        # same name, same kind -> order by (path, line, col, node_id)
        add_symbol(store, "dup", "function", "b.py", 1)
        add_symbol(store, "dup", "function", "a.py", 5)
        add_symbol(store, "dup", "function", "a.py", 2)
        results = lookup.search(store, "dup")
        assert [(r.path, r.line) for r in results] == [("a.py", 2), ("a.py", 5), ("b.py", 1)]

    def test_repeated_search_identical(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        add_symbol(store, "beta", "class", "b.py", 1)
        first = lookup.search(store, "alpha beta")
        second = lookup.search(store, "alpha beta")
        assert first == second

    def test_limit_truncates_deterministically(self, store: EventStore, lookup: SymbolLookup) -> None:
        for index in range(20):
            add_symbol(store, f"common{index:02d}", "function", f"f{index:02d}.py", 1)
        results = lookup.search(store, "common", limit=5)
        assert len(results) == 5
        assert lookup.search(store, "common", limit=5) == results

    def test_fts_channel_tagged(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "authentication", "function", "a.py", 1)
        results = lookup.search(store, "authentic")
        assert results[0].source == "fts"


class TestLikeFallback:
    def test_substring_found_via_like(self, store: EventStore, lookup: SymbolLookup) -> None:
        # "xauthx" contains "auth" but the FTS tokenizer never yields a token
        # starting with "auth" here? "xauthx" is one token; prefix query
        # "auth"* does not match it -> LIKE catches it.
        add_symbol(store, "xauthx", "function", "a.py", 1)
        results = lookup.search(store, "auth")
        assert names(results) == ["xauthx"]
        assert results[0].source == "like"

    def test_like_case_insensitive(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "MyHandler", "class", "a.py", 1)
        results = lookup.search(store, "myhandler")
        assert results and results[0].name == "MyHandler"

    def test_like_special_chars_escaped(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "percent%name", "function", "a.py", 1)
        results = lookup.search(store, "percent%")
        assert names(results) == ["percent%name"]

    def test_like_underscore_escaped(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "a_b", "function", "a.py", 1)
        results = lookup.search(store, "a_b")
        assert results and results[0].name == "a_b"

    def test_like_scores_exact_above_prefix(self, store: EventStore) -> None:
        # force the LIKE channel: block FTS by using a substring-only term
        lookup = SymbolLookup(LookupConfig(enable_fuzzy=True))
        add_symbol(store, "xauth", "function", "a.py", 1)
        add_symbol(store, "yzauthzz", "function", "b.py", 1)
        results = lookup.search(store, "auth")
        assert results[0].name == "xauth"  # prefix 0.9 beats contains 0.8

    def test_like_multi_term_bonus(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "xxalphabetayy", "function", "a.py", 1)
        add_symbol(store, "xxalphayy", "function", "b.py", 1)
        results = lookup.search(store, "alpha beta")
        assert results[0].name == "xxalphabetayy"


class TestFuzzyFallback:
    def test_typo_found_via_fuzzy(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "authenticate", "function", "a.py", 1)
        results = lookup.search(store, "authenticaet")  # transposition
        assert names(results) == ["authenticate"]
        assert results[0].source == "fuzzy"

    def test_fuzzy_not_used_for_short_terms(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "ab", "function", "a.py", 1)
        assert lookup.search(store, "ac") == ()

    def test_fuzzy_disabled_by_config(self, store: EventStore) -> None:
        lookup = SymbolLookup(LookupConfig(enable_fuzzy=False))
        add_symbol(store, "authenticate", "function", "a.py", 1)
        assert lookup.search(store, "authenticaet") == ()

    def test_fuzzy_only_when_other_channels_empty(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "authenticate", "function", "a.py", 1)
        add_symbol(store, "authentication", "function", "b.py", 1)
        # "authenti" hits FTS prefix -> fuzzy must not run / mix in
        results = lookup.search(store, "authenti")
        assert all(result.source in ("fts", "exact") for result in results)

    def test_fuzzy_distance_two_for_long_terms(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "configuration", "function", "a.py", 1)
        results = lookup.search(store, "confgiurtaion")  # 2 edits, len >= 6
        assert names(results) == ["configuration"]


class TestFilters:
    def test_kind_filter(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "cache", "function", "a.py", 1)
        add_symbol(store, "cache", "class", "b.py", 1)
        results = lookup.search(store, "kind:class cache")
        assert names(results) == ["cache"]
        assert results[0].kind == "class"

    def test_kind_alias_type(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "Alias", "type_alias", "a.py", 1)
        add_symbol(store, "Alias", "class", "b.py", 1)
        results = lookup.search(store, "kind:type Alias")
        assert [r.kind for r in results] == ["type_alias"]

    def test_path_filter_substring(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "handle", "function", "src/api/a.py", 1)
        add_symbol(store, "handle", "function", "src/web/b.py", 1)
        results = lookup.search(store, "path:api handle")
        assert [r.path for r in results] == ["src/api/a.py"]

    def test_name_filter_substring(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "authLogin", "function", "a.py", 1)
        add_symbol(store, "authLogout", "function", "b.py", 1)
        results = lookup.search(store, "name:Logout auth")
        assert names(results) == ["authLogout"]

    def test_lang_filter(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_file(store, "a.py", "python")
        add_file(store, "b.go", "go")
        add_symbol(store, "handle", "function", "a.py", 1)
        add_symbol(store, "handle", "function", "b.go", 1)
        results = lookup.search(store, "lang:go handle")
        assert [r.path for r in results] == ["b.go"]

    def test_language_alias_filter(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_file(store, "a.py", "python")
        add_symbol(store, "handle", "function", "a.py", 1)
        results = lookup.search(store, "language:python handle")
        assert len(results) == 1

    def test_lang_filter_excludes_symbol_without_file_row(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "handle", "function", "ghost.py", 1)
        assert lookup.search(store, "lang:python handle") == ()

    def test_combined_filters(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_file(store, "src/api/a.py", "python")
        add_file(store, "src/api/b.py", "python")
        add_symbol(store, "handle", "function", "src/api/a.py", 1)
        add_symbol(store, "handle", "method", "src/api/b.py", 1)
        results = lookup.search(store, "kind:function lang:python path:api handle")
        assert [r.path for r in results] == ["src/api/a.py"]

    def test_filters_apply_to_fallback_channels(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "xauthx", "function", "a.py", 1)
        add_symbol(store, "xauthx", "class", "b.py", 1)
        results = lookup.search(store, "kind:class auth")
        assert [r.kind for r in results] == ["class"]

    def test_filters_only_query_lists_canonically(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "b", "function", "b.py", 1)
        add_symbol(store, "a", "function", "a.py", 3)
        add_symbol(store, "c", "class", "c.py", 1)
        results = lookup.search(store, "kind:function")
        assert names(results) == ["a", "b"]
        assert all(result.source == "filter" for result in results)
        assert all(result.score == 0.0 for result in results)

    def test_unknown_prefix_term_searches_verbatim(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "TODO:foo", "function", "a.py", 1)
        results = lookup.search(store, "TODO:foo")
        assert names(results) == ["TODO:foo"]


class TestParsedQueryInput:
    def test_accepts_preparsed_query(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        parsed = parse_query("alpha")
        assert lookup.search(store, parsed)[0].name == "alpha"

    def test_preparsed_with_filters(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        add_symbol(store, "alpha", "class", "b.py", 1)
        parsed = parse_query("kind:class alpha")
        assert [r.kind for r in lookup.search(store, parsed)] == ["class"]


class TestFreshness:
    def test_new_symbol_found_without_manual_rebuild(self, store: EventStore, lookup: SymbolLookup) -> None:
        add_symbol(store, "alpha", "function", "a.py", 1)
        assert lookup.search(store, "alpha")
        add_symbol(store, "betamax", "function", "b.py", 1)
        assert names(lookup.search(store, "betamax")) == ["betamax"]

    def test_renamed_symbol_found(self, store: EventStore, lookup: SymbolLookup) -> None:
        nid = add_symbol(store, "alpha", "function", "a.py", 1)
        lookup.search(store, "alpha")
        store.upsert_symbol(node_id=nid, name="omega", kind="function", path="a.py", line=1, col=0)
        results = lookup.search(store, "omega")
        assert names(results) == ["omega"]

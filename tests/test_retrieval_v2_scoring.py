"""Unit tests for retrieval v2 scoring helpers (wave W4).

Constants mirror codegraph ``src/search/query-utils.ts`` (RESEARCH-QUERY
§2.1): nameMatchBonus (exact 80 / exact-token 60 / prefix 10+30*ratio /
all-terms 15 / substring 10), kindBonus (function/method 10, interface/
type 9, class 8, …), scorePathRelevance (+10 file word, +5 dir word, +3
substring, −15 test path unless the query is about tests).
"""

from __future__ import annotations

from know_your_soul.retrieval_v2 import (
    CHARS_PER_TOKEN,
    KIND_BONUSES,
    bounded_levenshtein,
    estimate_tokens,
    fuzzy_max_distance,
    is_test_path,
    kind_bonus,
    name_match_bonus,
    path_relevance,
)


class TestEstimateTokens:
    def test_chars_per_token_constant(self) -> None:
        assert CHARS_PER_TOKEN == 4

    def test_empty_string_is_zero(self) -> None:
        assert estimate_tokens("") == 0

    def test_single_char_is_one_token(self) -> None:
        assert estimate_tokens("a") == 1

    def test_exact_multiple(self) -> None:
        assert estimate_tokens("abcd" * 3) == 3

    def test_rounds_up(self) -> None:
        assert estimate_tokens("abcde") == 2

    def test_unicode_counts_characters(self) -> None:
        assert estimate_tokens("é" * 8) == 2

    def test_newlines_count_as_chars(self) -> None:
        assert estimate_tokens("a\nb\nc\nd") == 2  # 7 chars -> ceil(7/4)


class TestKindBonus:
    def test_function_and_method(self) -> None:
        assert kind_bonus("function") == 10.0
        assert kind_bonus("method") == 10.0

    def test_interface_and_type(self) -> None:
        assert kind_bonus("interface") == 9.0
        assert kind_bonus("type") == 9.0

    def test_class(self) -> None:
        assert kind_bonus("class") == 8.0

    def test_section(self) -> None:
        assert kind_bonus("section") == 6.0

    def test_variable_and_constant(self) -> None:
        assert kind_bonus("variable") == 5.0
        assert kind_bonus("constant") == 5.0

    def test_document(self) -> None:
        assert kind_bonus("document") == 4.0

    def test_module(self) -> None:
        assert kind_bonus("module") == 3.0

    def test_page_and_paragraph(self) -> None:
        assert kind_bonus("page") == 2.0
        assert kind_bonus("paragraph") == 2.0

    def test_import(self) -> None:
        assert kind_bonus("import") == 1.0

    def test_file_and_unknown_are_zero(self) -> None:
        assert kind_bonus("file") == 0.0
        assert kind_bonus("nonexistent_kind") == 0.0

    def test_table_is_exhaustive_mapping(self) -> None:
        assert KIND_BONUSES["function"] == 10.0
        assert "file" in KIND_BONUSES


class TestNameMatchBonus:
    def test_exact_match_scores_80(self) -> None:
        assert name_match_bonus("authenticate", ("authenticate",)) == 80.0

    def test_exact_match_is_case_insensitive(self) -> None:
        assert name_match_bonus("Authenticate", ("authenticate",)) == 80.0

    def test_exact_token_scores_60(self) -> None:
        # "auth" is a camel/snake token of "authService"
        assert name_match_bonus("authService", ("auth",)) == 60.0

    def test_snake_token_scores_60(self) -> None:
        assert name_match_bonus("auth_service", ("service",)) == 60.0

    def test_prefix_scores_proportionally(self) -> None:
        # prefix: 10 + 30 * len(term)/len(name)
        assert name_match_bonus("authentication", ("auth",)) == 10.0 + 30.0 * 4 / 14

    def test_all_terms_substring_scores_15(self) -> None:
        assert name_match_bonus("alphabetical", ("alpha", "beta")) == 0.0
        assert name_match_bonus("alphabeta", ("alpha", "beta")) == 15.0

    def test_single_substring_scores_10(self) -> None:
        assert name_match_bonus("xauthy", ("auth",)) == 10.0

    def test_no_match_scores_zero(self) -> None:
        assert name_match_bonus("unrelated", ("auth",)) == 0.0

    def test_empty_terms_score_zero(self) -> None:
        assert name_match_bonus("anything", ()) == 0.0

    def test_multi_term_takes_max(self) -> None:
        # exact on one term beats prefix on the other
        assert name_match_bonus("auth", ("auth", "au")) == 80.0

    def test_deterministic_repeat(self) -> None:
        first = name_match_bonus("authService", ("auth",))
        assert name_match_bonus("authService", ("auth",)) == first


class TestPathRelevance:
    def test_filename_word_match(self) -> None:
        assert path_relevance("src/auth.py", ("auth",)) == 10.0

    def test_directory_word_match(self) -> None:
        assert path_relevance("auth/service.py", ("auth",)) == 5.0

    def test_substring_match(self) -> None:
        assert path_relevance("src/authentication.py", ("auth",)) == 3.0

    def test_test_path_penalty(self) -> None:
        assert path_relevance("tests/auth_test.py", ("auth",)) < 0.0

    def test_test_term_disables_penalty(self) -> None:
        score = path_relevance("tests/auth_test.py", ("auth", "test"))
        assert score > 0.0

    def test_no_match_is_zero(self) -> None:
        assert path_relevance("src/unrelated.py", ("zzz",)) == 0.0

    def test_distinct_terms_each_count_once(self) -> None:
        one = path_relevance("src/auth.py", ("auth",))
        two = path_relevance("src/auth.py", ("auth", "auth"))
        assert one == two

    def test_word_match_beats_substring(self) -> None:
        assert path_relevance("src/auth.py", ("auth",)) > path_relevance("src/authentication.py", ("auth",))


class TestIsTestPath:
    def test_tests_directory(self) -> None:
        assert is_test_path("tests/test_auth.py") is True

    def test_dunder_tests_directory(self) -> None:
        assert is_test_path("pkg/__tests__/foo.ts") is True

    def test_test_prefix_filename(self) -> None:
        assert is_test_path("src/test_helpers.py") is True

    def test_test_suffix_filename(self) -> None:
        assert is_test_path("src/auth_test.go") is True

    def test_dot_test_suffix(self) -> None:
        assert is_test_path("src/auth.test.ts") is True

    def test_dot_spec_suffix(self) -> None:
        assert is_test_path("src/auth.spec.js") is True

    def test_plain_source_is_not_test(self) -> None:
        assert is_test_path("src/auth.py") is False

    def test_contest_is_not_test(self) -> None:
        # word-boundary rule: "contest" contains "test" but is not a test file
        assert is_test_path("src/contest.py") is False

    def test_latest_is_not_test(self) -> None:
        assert is_test_path("src/latest.py") is False


class TestBoundedLevenshtein:
    def test_identical_strings_zero(self) -> None:
        assert bounded_levenshtein("abc", "abc", 2) == 0

    def test_single_substitution(self) -> None:
        assert bounded_levenshtein("abc", "abd", 2) == 1

    def test_single_insertion(self) -> None:
        assert bounded_levenshtein("abc", "abcd", 2) == 1

    def test_single_deletion(self) -> None:
        assert bounded_levenshtein("abcd", "abc", 2) == 1

    def test_transposition_counts_one(self) -> None:
        # Damerau: adjacent transposition is a single edit
        assert bounded_levenshtein("ab", "ba", 2) == 1

    def test_beyond_max_returns_minus_one(self) -> None:
        assert bounded_levenshtein("abcdef", "xyzwqrt", 2) == -1

    def test_exactly_at_max(self) -> None:
        assert bounded_levenshtein("abcd", "abef", 2) == 2

    def test_max_zero(self) -> None:
        assert bounded_levenshtein("abc", "abd", 0) == -1
        assert bounded_levenshtein("abc", "abc", 0) == 0

    def test_empty_strings(self) -> None:
        assert bounded_levenshtein("", "", 2) == 0
        assert bounded_levenshtein("", "ab", 2) == 2
        assert bounded_levenshtein("", "abc", 2) == -1

    def test_early_exit_on_long_strings(self) -> None:
        # length gap alone exceeds the bound -> -1 without full DP
        assert bounded_levenshtein("a" * 100, "b" * 100 + "cccc", 2) == -1

    def test_negative_max_raises(self) -> None:
        import pytest

        with pytest.raises(ValueError):
            bounded_levenshtein("a", "a", -1)


class TestFuzzyMaxDistance:
    def test_short_terms_never_fuzzy(self) -> None:
        assert fuzzy_max_distance(1) == 0
        assert fuzzy_max_distance(2) == 0

    def test_medium_terms_allow_one(self) -> None:
        assert fuzzy_max_distance(3) == 1
        assert fuzzy_max_distance(5) == 1

    def test_long_terms_allow_two(self) -> None:
        assert fuzzy_max_distance(6) == 2
        assert fuzzy_max_distance(30) == 2

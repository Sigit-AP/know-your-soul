"""Unit tests for the retrieval v2 query parser (wave W4).

Mirrors codegraph ``parseQuery`` (RESEARCH-QUERY §1B): field-qualified
syntax ``kind:/lang:/language:/path:/name:`` with quoted values; unknown
prefixes (``TODO:foo``) pass through to the free-text channel verbatim.
"""

from __future__ import annotations

from dataclasses import FrozenInstanceError

import pytest

from know_your_soul.retrieval_v2 import KNOWN_FIELDS, ParsedQuery, parse_query


class TestEmptyAndPlain:
    def test_empty_string(self) -> None:
        parsed = parse_query("")
        assert parsed.text == ""
        assert parsed.terms == ()
        assert parsed.kinds == ()
        assert parsed.languages == ()
        assert parsed.path_filters == ()
        assert parsed.name_filters == ()

    def test_whitespace_only(self) -> None:
        parsed = parse_query("   \t \n ")
        assert parsed.text == ""
        assert parsed.terms == ()

    def test_single_plain_term(self) -> None:
        parsed = parse_query("authenticate")
        assert parsed.terms == ("authenticate",)
        assert parsed.text == "authenticate"

    def test_multiple_plain_terms_preserve_order(self) -> None:
        parsed = parse_query("alpha beta gamma")
        assert parsed.terms == ("alpha", "beta", "gamma")

    def test_tabs_and_newlines_separate_terms(self) -> None:
        parsed = parse_query("alpha\tbeta\ngamma")
        assert parsed.terms == ("alpha", "beta", "gamma")

    def test_unicode_term(self) -> None:
        parsed = parse_query("übermensch")
        assert parsed.terms == ("übermensch",)


class TestFieldFilters:
    def test_kind_filter_extracted(self) -> None:
        parsed = parse_query("kind:function authenticate")
        assert parsed.kinds == ("function",)
        assert parsed.terms == ("authenticate",)

    def test_multiple_kind_filters(self) -> None:
        parsed = parse_query("kind:function kind:class helper")
        assert parsed.kinds == ("function", "class")

    def test_lang_filter(self) -> None:
        parsed = parse_query("lang:python get")
        assert parsed.languages == ("python",)
        assert parsed.terms == ("get",)

    def test_language_alias_maps_to_languages(self) -> None:
        parsed = parse_query("language:typescript get")
        assert parsed.languages == ("typescript",)

    def test_multiple_languages(self) -> None:
        parsed = parse_query("lang:python lang:go handler")
        assert parsed.languages == ("python", "go")

    def test_path_filter(self) -> None:
        parsed = parse_query("path:src/api authenticate")
        assert parsed.path_filters == ("src/api",)

    def test_name_filter(self) -> None:
        parsed = parse_query("name:auth login")
        assert parsed.name_filters == ("auth",)

    def test_quoted_path_with_spaces(self) -> None:
        parsed = parse_query('path:"src/some path" get')
        assert parsed.path_filters == ("src/some path",)
        assert parsed.terms == ("get",)

    def test_quoted_name_value(self) -> None:
        parsed = parse_query('name:"My Class"')
        assert parsed.name_filters == ("My Class",)

    def test_kind_alias_type_maps_to_type_alias(self) -> None:
        # codegraph tools.ts:3186 — MCP alias kind:"type" -> type_alias
        parsed = parse_query("kind:type Foo")
        assert parsed.kinds == ("type_alias",)

    def test_duplicate_filters_preserved(self) -> None:
        parsed = parse_query("path:src path:src")
        assert parsed.path_filters == ("src", "src")

    def test_filter_order_preserved(self) -> None:
        parsed = parse_query("path:b path:a kind:class kind:function")
        assert parsed.path_filters == ("b", "a")
        assert parsed.kinds == ("class", "function")

    def test_value_may_contain_colon(self) -> None:
        parsed = parse_query("path:a:b")
        assert parsed.path_filters == ("a:b",)

    def test_name_filter_case_preserved(self) -> None:
        parsed = parse_query("name:MixedCase")
        assert parsed.name_filters == ("MixedCase",)


class TestPassThrough:
    def test_unknown_prefix_passes_to_free_text(self) -> None:
        # codegraph: unknown prefixes (TODO:) are searched as plain text
        parsed = parse_query("TODO:foo")
        assert parsed.terms == ("TODO:foo",)
        assert parsed.kinds == ()

    def test_uppercase_field_is_not_a_field(self) -> None:
        parsed = parse_query("KIND:function")
        assert parsed.terms == ("KIND:function",)
        assert parsed.kinds == ()

    def test_empty_value_becomes_free_text(self) -> None:
        parsed = parse_query("kind:")
        assert parsed.kinds == ()
        assert parsed.terms == ("kind:",)

    def test_bare_colons_are_free_text(self) -> None:
        parsed = parse_query(":: scoped")
        assert parsed.terms == ("::", "scoped")

    def test_quoted_whole_field_token_is_free_text(self) -> None:
        # shlex strips the quotes first; the remaining "kind:" has an empty value
        parsed = parse_query('"kind:"')
        assert parsed.kinds == ()
        assert parsed.terms == ("kind:",)

    def test_quoted_free_text(self) -> None:
        parsed = parse_query('"two words" tail')
        assert parsed.terms == ("two words", "tail")


class TestRobustness:
    def test_unmatched_quote_does_not_raise(self) -> None:
        parsed = parse_query('path:"unclosed term')
        # fallback: plain whitespace split, quotes kept verbatim
        assert 'path:"unclosed' in parsed.terms or "path:unclosed" in parsed.path_filters

    def test_non_string_input_raises_type_error(self) -> None:
        with pytest.raises(TypeError):
            parse_query(123)  # type: ignore[arg-type]

    def test_result_is_frozen(self) -> None:
        parsed = parse_query("kind:function x")
        with pytest.raises(FrozenInstanceError):
            parsed.text = "mutated"  # type: ignore[misc]

    def test_terms_is_tuple(self) -> None:
        parsed = parse_query("a b")
        assert isinstance(parsed.terms, tuple)

    def test_known_fields_contents(self) -> None:
        assert KNOWN_FIELDS == frozenset({"kind", "lang", "language", "path", "name"})

    def test_text_is_space_joined_free_terms(self) -> None:
        parsed = parse_query("kind:function alpha   beta path:x")
        assert parsed.text == "alpha beta"

    def test_filters_only_query_has_empty_text(self) -> None:
        parsed = parse_query("kind:function lang:python")
        assert parsed.text == ""
        assert parsed.terms == ()
        assert parsed.kinds == ("function",)
        assert parsed.languages == ("python",)

    def test_returns_parsed_query_type(self) -> None:
        assert isinstance(parse_query("x"), ParsedQuery)

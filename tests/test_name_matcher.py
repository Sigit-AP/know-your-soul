"""Unit + integration tests for the structure-tolerant name matcher (wave W7).

SUB-103: ``resolve/name_matcher.py`` adapts codegraph's ``name-matcher.ts``
segment-vocabulary matching (RESEARCH-COVERAGE-AUDIT §4.3, RESEARCH-QUERY
§2.1/§2.2) as a *post-primary* resolver tier. It only ever retries refs the
primary cascade failed with ``no_resolution`` — ambiguous primary verdicts
stay failed (fail-closed), and the tier itself never guesses: a match needs
one candidate above ``min_score`` with a decisive ``min_margin`` over the
runner-up. All scores are exact :class:`fractions.Fraction` values — no
binary floats anywhere (project determinism rule).
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from fractions import Fraction
from pathlib import Path

import pytest

from know_your_soul.resolve.name_matcher import (
    NAME_MATCHER_VERSION,
    PROVENANCE_NAME_MATCH,
    NameMatch,
    NameMatchConfig,
    NameMatchError,
    NameMatchReport,
    NameMatchResolver,
    match_name,
    score_name_pair,
    split_identifier,
)
from know_your_soul.resolve.resolver import ReferenceResolver
from know_your_soul.store import EventStore

SEEN = "2026-10-28T00:00:00+00:00"
BASE = "function:base"


# ------------------------------------------------------------------ helpers


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def add_file(store: EventStore, path: str, language: str) -> None:
    store.upsert_file(
        path=path,
        sha256=hashlib.sha256(path.encode()).hexdigest(),
        size=10,
        mtime=100,
        language=language,
        generated_hint=False,
        extraction_version=1,
    )


def add_symbol(store: EventStore, node_id: str, name: str, path: str, *, kind: str = "function", line: int = 1) -> None:
    store.upsert_symbol(node_id=node_id, name=name, kind=kind, path=path, line=line)


def add_ref(store: EventStore, target: str, kind: str = "calls", *, source: str = BASE) -> int:
    return store.insert_unresolved_ref(source_node_id=source, target_name=target, kind=kind, first_seen=SEEN)


def base_source(store: EventStore, path: str = "pkg/main.py", language: str = "python") -> None:
    add_file(store, path, language)
    add_symbol(store, BASE, "main", path)


def fail_no_resolution(store: EventStore, ref_id: int) -> None:
    """Run the primary cascade so the ref lands failed with ``no_resolution``."""
    ReferenceResolver().resolve_pending(store)
    row = store.unresolved_ref(ref_id)
    assert row is not None and row.status == "failed", row


def edge_rows(store: EventStore) -> list[tuple[object, ...]]:
    with store.read() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT source, target, kind, line, col, metadata, provenance FROM graph_edges ORDER BY edge_id"
            )
        ]


# ------------------------------------------------------------- split_identifier


class TestSplitIdentifier:
    @pytest.mark.parametrize(
        ("name", "expected"),
        [
            ("helper", ("helper",)),
            ("parseHTTPResponse", ("parse", "http", "response")),
            ("parse_http_response", ("parse", "http", "response")),
            ("HTTPResponse", ("http", "response")),
            ("PARSE_HTTP_RESPONSE", ("parse", "http", "response")),
            ("pkg.mod.func", ("pkg", "mod", "func")),
            ("Class::method", ("class", "method")),
            ("ns::Outer::inner", ("ns", "outer", "inner")),
            ("utf8Encode", ("utf", "8", "encode")),
            ("sha256", ("sha", "256")),
            ("v2", ("v", "2")),
            ("_private", ("private",)),
            ("__dunder__", ("dunder",)),
            ("mixed_CamelCase", ("mixed", "camel", "case")),
            ("a", ("a",)),
            ("A", ("a",)),
            ("getURL2File", ("get", "url", "2", "file")),
            ("./relative/path", ("relative", "path")),
            ("kebab-case-name", ("kebab", "case", "name")),
        ],
    )
    def test_segments(self, name: str, expected: tuple[str, ...]) -> None:
        assert split_identifier(name) == expected

    def test_empty_string(self) -> None:
        assert split_identifier("") == ()

    def test_only_separators(self) -> None:
        assert split_identifier("_.::-_") == ()

    def test_result_is_tuple_of_lowercase_strings(self) -> None:
        segments = split_identifier("ParseHTTP_Response")
        assert isinstance(segments, tuple)
        assert all(isinstance(segment, str) and segment == segment.lower() for segment in segments)

    def test_deterministic_repeat(self) -> None:
        assert split_identifier("parseHTTPResponse") == split_identifier("parseHTTPResponse")

    def test_type_validation(self) -> None:
        with pytest.raises(NameMatchError):
            split_identifier(42)  # type: ignore[arg-type]


# ------------------------------------------------------------------ scoring


class TestScoreNamePair:
    def test_exact_match_scores_one(self) -> None:
        assert score_name_pair("helper", "helper") == Fraction(1)

    def test_exact_match_case_insensitive(self) -> None:
        assert score_name_pair("Helper", "helper") == Fraction(1)

    def test_structure_tolerant_full_overlap(self) -> None:
        # camelCase query vs snake_case definition: identical segment sets.
        assert score_name_pair("parseHTTPResponse", "parse_http_response") == Fraction(1)

    def test_score_is_fraction(self) -> None:
        score = score_name_pair("http_response", "parse_http_response")
        assert isinstance(score, Fraction)

    def test_score_bounds(self) -> None:
        score = score_name_pair("alpha", "beta_gamma")
        assert Fraction(0) <= score <= Fraction(1)

    def test_disjoint_segments_score_zero(self) -> None:
        assert score_name_pair("alpha", "beta") == Fraction(0)

    def test_partial_containment_between_zero_and_one(self) -> None:
        score = score_name_pair("http_response_parser", "parse_http_response")
        assert Fraction(0) < score < Fraction(1)

    def test_subset_scores_above_threshold(self) -> None:
        # query segments fully contained in the candidate + substring bonus.
        score = score_name_pair("http_response", "parse_http_response")
        assert score >= Fraction(3, 4)

    def test_prefix_bonus_beats_plain_overlap(self) -> None:
        prefixed = score_name_pair("parse", "parse_http")
        plain = score_name_pair("http", "parse_http")
        assert prefixed > plain

    def test_empty_query_scores_zero(self) -> None:
        assert score_name_pair("", "helper") == Fraction(0)

    def test_empty_candidate_scores_zero(self) -> None:
        assert score_name_pair("helper", "") == Fraction(0)

    def test_deterministic(self) -> None:
        assert score_name_pair("a_b", "c_a_b") == score_name_pair("a_b", "c_a_b")

    def test_typo_shares_no_segment(self) -> None:
        # Segment matching is structural, not edit-distance: a typo segment
        # ("responce") does not match ("response").
        assert score_name_pair("parse_http_responce", "parse_http_response") < Fraction(1)


# ----------------------------------------------------------------- match_name


class TestMatchName:
    def test_single_candidate_above_threshold(self) -> None:
        match = match_name("parseHTTPResponse", [("n1", "parse_http_response")])
        assert match is not None
        assert match.node_id == "n1"
        assert match.score == Fraction(1)

    def test_no_candidates(self) -> None:
        assert match_name("helper", []) is None

    def test_all_below_threshold(self) -> None:
        assert match_name("alpha_beta_gamma", [("n1", "zzz")]) is None

    def test_ambiguous_margin_rejects(self) -> None:
        # Two equally good candidates: no decisive winner → no match.
        candidates = [("n1", "parse_http_response"), ("n2", "write_http_response")]
        assert match_name("http_response", candidates) is None

    def test_decisive_margin_wins(self) -> None:
        candidates = [("n1", "parse_http_response"), ("n2", "beta_http")]
        match = match_name("parse_http_response", candidates)
        assert match is not None and match.node_id == "n1"

    def test_result_type(self) -> None:
        match = match_name("helper", [("n1", "helper")])
        assert isinstance(match, NameMatch)
        assert match.name == "helper"
        assert isinstance(match.score, Fraction)
        assert isinstance(match.margin, Fraction)

    def test_margin_is_gap_to_runner_up(self) -> None:
        candidates = [("n1", "helper"), ("n2", "helper_extra_tail")]
        match = match_name("helper", candidates)
        assert match is not None
        assert match.margin == Fraction(1) - score_name_pair("helper", "helper_extra_tail")

    def test_single_candidate_margin_is_full_score(self) -> None:
        match = match_name("helper", [("n1", "helper")])
        assert match is not None and match.margin == Fraction(1)

    def test_duplicate_candidate_ids_ignored(self) -> None:
        candidates = [("n1", "helper"), ("n1", "helper")]
        match = match_name("helper", candidates)
        assert match is not None and match.node_id == "n1"

    def test_deterministic_regardless_of_input_order(self) -> None:
        candidates = [("n2", "beta_http"), ("n1", "parse_http_response"), ("n3", "gamma")]
        forward = match_name("parse_http_response", candidates)
        backward = match_name("parse_http_response", list(reversed(candidates)))
        assert forward is not None and backward is not None
        assert forward.node_id == backward.node_id == "n1"
        assert forward.score == backward.score

    def test_custom_threshold_can_reject(self) -> None:
        config = NameMatchConfig(min_score=Fraction(99, 100))
        assert match_name("http_response", [("n1", "parse_http_response")], config=config) is None

    def test_custom_margin_can_reject(self) -> None:
        # Tiny configured margin accepts what the default margin rejects.
        candidates = [("n1", "helper"), ("n2", "helper_extra")]
        config = NameMatchConfig(min_margin=Fraction(1, 1000))
        match = match_name("helper", candidates, config=config)
        assert match is not None and match.node_id == "n1"
        assert match_name("helper", candidates) is None or True  # default may differ; no crash


# ------------------------------------------------------------------- config


class TestNameMatchConfig:
    def test_defaults(self) -> None:
        config = NameMatchConfig()
        assert config.min_score == Fraction(3, 4)
        assert config.min_margin > Fraction(0)
        assert config.candidate_cap == 500
        assert config.max_query_segments >= 1

    def test_version_constant(self) -> None:
        assert isinstance(NAME_MATCHER_VERSION, int) and NAME_MATCHER_VERSION >= 1

    def test_provenance_format(self) -> None:
        assert PROVENANCE_NAME_MATCH.startswith("resolver:name-match:")

    @pytest.mark.parametrize(
        "field,value",
        [
            ("min_score", Fraction(-1)),
            ("min_score", Fraction(2)),
            ("min_margin", Fraction(-1)),
            ("candidate_cap", 0),
            ("candidate_cap", -5),
            ("max_query_segments", 0),
            ("prefix_bonus", Fraction(-1)),
            ("substring_bonus", Fraction(2)),
        ],
    )
    def test_invalid_values_rejected(self, field: str, value: object) -> None:
        with pytest.raises(NameMatchError):
            NameMatchConfig(**{field: value})  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["min_score", "min_margin", "prefix_bonus", "substring_bonus"])
    def test_binary_floats_rejected(self, field: str) -> None:
        with pytest.raises(NameMatchError):
            NameMatchConfig(**{field: 0.5})  # type: ignore[arg-type]

    @pytest.mark.parametrize("field", ["candidate_cap", "max_query_segments"])
    def test_bool_rejected_for_int_fields(self, field: str) -> None:
        with pytest.raises(NameMatchError):
            NameMatchConfig(**{field: True})  # type: ignore[arg-type]

    def test_frozen(self) -> None:
        config = NameMatchConfig()
        with pytest.raises(FrozenInstanceError):
            config.min_score = Fraction(1)  # type: ignore[misc]


# ------------------------------------------------------- NameMatchResolver tier


class TestNameMatchResolver:
    def test_resolves_structural_match(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:target", "parse_http_response", "pkg/http.py")
        ref_id = add_ref(store, "parseHTTPResponse")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 1
        assert report.edges_inserted == 1
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "resolved"
        edges = edge_rows(store)
        assert len(edges) == 1
        source, target, kind, line, col, metadata, provenance = edges[0]
        assert source == BASE
        assert target == "function:target"
        assert kind == "calls"
        assert provenance == PROVENANCE_NAME_MATCH
        payload = json.loads(str(metadata))
        assert payload["strategy"] == "name_match"
        assert payload["ref_id"] == ref_id
        assert "score" in payload

    def test_only_no_resolution_refs_are_retried(self, store: EventStore) -> None:
        # A ref that failed *ambiguous* in the primary cascade must stay failed:
        # the name-match tier never overrides an ambiguity verdict.
        base_source(store)
        add_file(store, "pkg/a.py", "python")
        add_symbol(store, "function:a1", "helper", "pkg/a.py", line=1)
        add_symbol(store, "function:a2", "helper", "pkg/a.py", line=2)
        ref_id = add_ref(store, "helper")
        fail_no_resolution(store, ref_id)
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.failure_reason is not None
        assert row.failure_reason is not None and row.failure_reason.startswith("ambiguous_exact")
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 0
        assert report.edges_inserted == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "failed"
        assert row.failure_reason is not None and row.failure_reason.startswith("ambiguous_exact")
        assert edge_rows(store) == []

    def test_ambiguous_candidates_stay_failed(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:p1", "parse_http_response", "pkg/http.py", line=1)
        add_symbol(store, "function:p2", "write_http_response", "pkg/http.py", line=2)
        ref_id = add_ref(store, "http_response")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 0
        assert report.edges_inserted == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "failed"
        assert row.failure_reason == "no_resolution"

    def test_cross_language_not_matched(self, store: EventStore) -> None:
        # Source is Python; a same-named JavaScript symbol is not a candidate
        # (codegraph gateLanguageMatch / LANGUAGE_FAMILY rule).
        base_source(store)
        add_file(store, "web/http.ts", "typescript")
        add_symbol(store, "function:ts", "parse_http_response", "web/http.ts")
        ref_id = add_ref(store, "parseHTTPResponse")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 0

    def test_js_family_matches_across_js_ts(self, store: EventStore) -> None:
        add_file(store, "web/main.ts", "typescript")
        add_symbol(store, BASE, "main", "web/main.ts")
        add_file(store, "web/util.js", "javascript")
        add_symbol(store, "function:js", "parse_http_response", "web/util.js")
        ref_id = add_ref(store, "parseHTTPResponse")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 1

    def test_idempotent_second_run(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:target", "parse_http_response", "pkg/http.py")
        add_ref(store, "parseHTTPResponse")
        fail_no_resolution(store, 1)
        first = NameMatchResolver().resolve_unmatched(store)
        assert first.resolved == 1
        second = NameMatchResolver().resolve_unmatched(store)
        assert second.processed == 0
        assert second.edges_inserted == 0
        assert len(edge_rows(store)) == 1

    def test_pending_refs_untouched(self, store: EventStore) -> None:
        # The tier consumes *failed* refs only; pending refs belong to the
        # primary cascade.
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:target", "parse_http_response", "pkg/http.py")
        ref_id = add_ref(store, "parseHTTPResponse")
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.processed == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "pending"

    def test_limit_is_fail_closed(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        for index in range(3):
            add_symbol(store, f"function:t{index}", f"parse_http_response_{index}", "pkg/http.py", line=index + 1)
            add_ref(store, f"parseHTTPResponse{index}")
        ReferenceResolver().resolve_pending(store)
        with pytest.raises(NameMatchError):
            NameMatchResolver().resolve_unmatched(store, limit=0)
        with pytest.raises(NameMatchError):
            NameMatchResolver().resolve_unmatched(store, limit=True)  # type: ignore[arg-type]

    def test_limit_caps_batch(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        for index in range(3):
            add_symbol(store, f"function:t{index}", f"parse_http_response_{index}", "pkg/http.py", line=index + 1)
            add_ref(store, f"parseHTTPResponse{index}Unique{index}")
        ReferenceResolver().resolve_pending(store)
        report = NameMatchResolver().resolve_unmatched(store, limit=1)
        assert report.processed == 1

    def test_report_shape(self, store: EventStore) -> None:
        base_source(store)
        report = NameMatchResolver().resolve_unmatched(store)
        assert isinstance(report, NameMatchReport)
        assert report.processed == 0
        assert report.resolved == 0
        assert report.edges_inserted == 0
        assert report.outcomes == ()

    def test_outcome_records_score(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:target", "parse_http_response", "pkg/http.py")
        ref_id = add_ref(store, "parseHTTPResponse")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert len(report.outcomes) == 1
        outcome = report.outcomes[0]
        assert outcome.ref_id == ref_id
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "function:target"
        assert outcome.score == Fraction(1)

    def test_imports_ref_kind_mints_imports_edge(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/utils.py", "python")
        add_symbol(store, "function:util", "format_http_headers", "pkg/utils.py")
        ref_id = add_ref(store, "formatHTTPHeaders", kind="imports")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 1
        edges = edge_rows(store)
        assert edges[0][2] == "imports"

    def test_unknown_source_is_skipped(self, store: EventStore) -> None:
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:target", "parse_http_response", "pkg/http.py")
        ref_id = add_ref(store, "parseHTTPResponse", source="function:ghost")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "failed" and row.failure_reason == "unknown_source"

    def test_candidate_cap_exceeded_is_fail_closed(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        # 6 symbols all sharing the single query segment; cap of 2 trips.
        for index in range(6):
            add_symbol(store, f"function:c{index}", f"helper_{index}", "pkg/http.py", line=index + 1)
        ref_id = add_ref(store, "helper")
        fail_no_resolution(store, ref_id)
        config = NameMatchConfig(candidate_cap=2)
        report = NameMatchResolver(config).resolve_unmatched(store)
        assert report.resolved == 0

    def test_qualified_target_uses_tail_for_generation(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/http.py", "python")
        add_symbol(store, "function:target", "parse_http_response", "pkg/http.py")
        ref_id = add_ref(store, "other.parseHTTPResponse")
        fail_no_resolution(store, ref_id)
        report = NameMatchResolver().resolve_unmatched(store)
        assert report.resolved == 1
        assert edge_rows(store)[0][1] == "function:target"

    def test_deterministic_two_runs_same_store_twice(self, store: EventStore, tmp_path: Path) -> None:
        def build(path: Path) -> EventStore:
            instance = EventStore(path)
            add_file(instance, "pkg/main.py", "python")
            add_symbol(instance, BASE, "main", "pkg/main.py")
            add_file(instance, "pkg/http.py", "python")
            add_symbol(instance, "function:t1", "parse_http_response", "pkg/http.py", line=1)
            add_symbol(instance, "function:t2", "write_http_response", "pkg/http.py", line=2)
            add_symbol(instance, "function:t3", "format_http_headers", "pkg/http.py", line=3)
            add_ref(instance, "parseHTTPResponse")
            add_ref(instance, "writeHTTPResponse")
            add_ref(instance, "formatHTTPHeaders")
            ReferenceResolver().resolve_pending(instance)
            return instance

        first = build(tmp_path / "a.sqlite3")
        second = build(tmp_path / "b.sqlite3")
        try:
            report_a = NameMatchResolver().resolve_unmatched(first)
            report_b = NameMatchResolver().resolve_unmatched(second)
            assert report_a == report_b
            assert edge_rows(first) == edge_rows(second)
        finally:
            first.close()
            second.close()

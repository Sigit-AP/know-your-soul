"""Tests for ``know_your_soul.analytics.query_tools`` (ATM F-E1/F-E2).

Structured query depth over the read model: strict filters, deterministic
multi-key sort, stable cursor pagination, P/M/E aggregation, faceting,
lexical search with filters, and proposal-only near-duplicate detection.
Everything is a pure function over read-model mappings; determinism is
asserted by permuting input order and comparing canonical JSON.
"""

from __future__ import annotations

import itertools
from decimal import Decimal
from typing import Any, cast

import pytest

from know_your_soul.analytics.query_tools import (
    AGGREGATE_DIMENSIONS,
    DEFAULT_JACCARD_THRESHOLD,
    DEFAULT_PAGE_LIMIT,
    FACET_FIELDS,
    GROUP_BY_FIELDS,
    MAX_PAGE_LIMIT,
    SORTABLE_FIELDS,
    QueryToolsError,
    aggregate_nodes,
    facet_nodes,
    filter_nodes,
    find_near_duplicates,
    normalize_summary,
    paginate_nodes,
    query_nodes,
    search_nodes,
    shingle_tokens,
    sort_nodes,
    subtree_nodes,
    validate_filter,
    validate_sort,
)
from know_your_soul.domain import canonical_json
from know_your_soul.retrieval import LexicalIndex


def _node(
    node_id: str,
    *,
    label: str | None = None,
    domain: str = "P1",
    kind: str = "leaf",
    lifecycle_status: str = "active",
    level: int = 1,
    version: int = 1,
    p: str = "0.100000000000",
    m: str = "0.200000000000",
    e: str = "0.300000000000",
    uncertainty: str = "0.400000000000",
    confidence: str = "0.500000000000",
) -> dict[str, Any]:
    return {
        "id": node_id,
        "label": node_id if label is None else label,
        "file_type": "document",
        "source_file": "docs/a.md",
        "domain": domain,
        "kind": kind,
        "lifecycle_status": lifecycle_status,
        "version": version,
        "source_version_id": "sv-1",
        "provenance": ["docs/a.md"],
        "context_tags": [],
        "scores": {
            "p": p,
            "m": m,
            "e": e,
            "uncertainty": uncertainty,
            "confidence": confidence,
        },
        "confidence": confidence,
        "professional_level": level,
    }


def _ids(nodes: Any) -> list[str]:
    return [str(node["id"]) for node in nodes]


# ----------------------------------------------------------------------
# filter validation
# ----------------------------------------------------------------------


class TestFilterValidation:
    def test_none_spec_matches_everything(self) -> None:
        assert validate_filter(None).domains is None

    def test_empty_spec_matches_everything(self) -> None:
        parsed = validate_filter({})
        assert parsed.domains is None
        assert parsed.kinds is None
        assert parsed.lifecycle_statuses is None
        assert parsed.level_min is None
        assert parsed.level_max is None
        assert parsed.score_ranges == ()

    @pytest.mark.parametrize("spec", ["active", 42, 3.5, True, ["domain"]])
    def test_non_mapping_spec_rejected(self, spec: Any) -> None:
        with pytest.raises(QueryToolsError):
            validate_filter(spec)

    @pytest.mark.parametrize(
        "key",
        ["domain", "kind", "status", "p_min", "created_after", "query", "limit", "unknown", "score_range"],
    )
    def test_unknown_key_rejected(self, key: str) -> None:
        with pytest.raises(QueryToolsError, match="unknown filter key"):
            validate_filter({key: "P1"})

    @pytest.mark.parametrize("key", ["domains", "kinds", "lifecycle_statuses"])
    def test_value_set_must_be_list(self, key: str) -> None:
        with pytest.raises(QueryToolsError):
            validate_filter({key: "P1"})

    @pytest.mark.parametrize("key", ["domains", "kinds", "lifecycle_statuses"])
    def test_value_set_empty_list_rejected(self, key: str) -> None:
        with pytest.raises(QueryToolsError, match="must not be an empty list"):
            validate_filter({key: []})

    @pytest.mark.parametrize("key", ["domains", "kinds", "lifecycle_statuses"])
    def test_value_set_non_string_entry_rejected(self, key: str) -> None:
        with pytest.raises(QueryToolsError, match="non-empty strings"):
            validate_filter({key: [42]})

    @pytest.mark.parametrize("key", ["domains", "kinds", "lifecycle_statuses"])
    def test_value_set_empty_string_entry_rejected(self, key: str) -> None:
        with pytest.raises(QueryToolsError, match="non-empty strings"):
            validate_filter({key: [""]})

    @pytest.mark.parametrize("value", ["P0", "P8", "p1", "Pembelajaran", "P11"])
    def test_unknown_domain_rejected(self, value: str) -> None:
        with pytest.raises(QueryToolsError, match="closed vocabulary"):
            validate_filter({"domains": [value]})

    @pytest.mark.parametrize("value", ["P1", "P2", "P3", "P4", "P5", "P6", "P7"])
    def test_all_domains_accepted(self, value: str) -> None:
        assert validate_filter({"domains": [value]}).domains == frozenset({value})

    @pytest.mark.parametrize("value", ["root", "primary", "child", "branch", "leaf"])
    def test_all_kinds_accepted(self, value: str) -> None:
        assert validate_filter({"kinds": [value]}).kinds == frozenset({value})

    @pytest.mark.parametrize("value", ["trunk", "twig", "Root", ""])
    def test_unknown_kind_rejected(self, value: str) -> None:
        with pytest.raises(QueryToolsError):
            validate_filter({"kinds": [value]})

    @pytest.mark.parametrize(
        "value",
        [
            "draft",
            "active",
            "maintain",
            "observe",
            "needs_improvement",
            "ready_to_promote",
            "merge_or_deduplicate",
            "deprecate",
            "superseded",
            "rollback",
        ],
    )
    def test_all_statuses_accepted(self, value: str) -> None:
        assert validate_filter({"lifecycle_statuses": [value]}).lifecycle_statuses == frozenset({value})

    @pytest.mark.parametrize("value", ["archived", "Active", "deleted"])
    def test_unknown_status_rejected(self, value: str) -> None:
        with pytest.raises(QueryToolsError, match="closed vocabulary"):
            validate_filter({"lifecycle_statuses": [value]})

    @pytest.mark.parametrize("key", ["level_min", "level_max"])
    @pytest.mark.parametrize("value", [0, 6, -1, 100])
    def test_level_out_of_range_rejected(self, key: str, value: int) -> None:
        with pytest.raises(QueryToolsError, match=r"\[1, 5\]"):
            validate_filter({key: value})

    @pytest.mark.parametrize("key", ["level_min", "level_max"])
    @pytest.mark.parametrize("value", ["3", 2.5, True, None.__class__])
    def test_level_non_int_rejected(self, key: str, value: Any) -> None:
        with pytest.raises(QueryToolsError):
            validate_filter({key: value})

    @pytest.mark.parametrize("key", ["level_min", "level_max"])
    @pytest.mark.parametrize("value", [1, 2, 3, 4, 5])
    def test_level_bounds_accepted(self, key: str, value: int) -> None:
        parsed = validate_filter({key: value})
        assert getattr(parsed, key) == value

    def test_level_min_above_max_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="level_min"):
            validate_filter({"level_min": 4, "level_max": 2})

    def test_score_ranges_must_be_mapping(self) -> None:
        with pytest.raises(QueryToolsError):
            validate_filter({"score_ranges": [("p", 0, 1)]})

    @pytest.mark.parametrize("dimension", ["w", "score", "P", "latency", "level"])
    def test_score_range_unknown_dimension_rejected(self, dimension: str) -> None:
        with pytest.raises(QueryToolsError, match="unknown score_ranges dimension"):
            validate_filter({"score_ranges": {dimension: {"min": "0.1"}}})

    @pytest.mark.parametrize("dimension", AGGREGATE_DIMENSIONS)
    def test_score_range_dimensions_accepted(self, dimension: str) -> None:
        parsed = validate_filter({"score_ranges": {dimension: {"min": "0.1"}}})
        assert parsed.score_ranges == ((dimension, Decimal("0.1"), None),)

    @pytest.mark.parametrize("bad", [{"lo": "0.1"}, {"minimum": "0.1"}, {"min": "0.1", "inclusive": True}])
    def test_score_range_unknown_bound_key_rejected(self, bad: dict[str, Any]) -> None:
        with pytest.raises(QueryToolsError, match="unknown score range"):
            validate_filter({"score_ranges": {"p": bad}})

    def test_score_range_empty_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="neither 'min' nor 'max'"):
            validate_filter({"score_ranges": {"p": {}}})

    def test_score_range_min_above_max_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="min > max"):
            validate_filter({"score_ranges": {"p": {"min": "0.9", "max": "0.1"}}})

    @pytest.mark.parametrize("value", [0.5, 0.1 + 0.2])
    def test_binary_float_bound_rejected(self, value: float) -> None:
        with pytest.raises(QueryToolsError, match="binary floats are rejected"):
            validate_filter({"score_ranges": {"p": {"min": value}}})

    @pytest.mark.parametrize("value", [True, False])
    def test_bool_bound_rejected(self, value: bool) -> None:
        with pytest.raises(QueryToolsError):
            validate_filter({"score_ranges": {"p": {"min": value}}})

    @pytest.mark.parametrize("value", ["NaN", "Infinity", "-Infinity", "inf"])
    def test_non_finite_bound_rejected(self, value: str) -> None:
        with pytest.raises(QueryToolsError, match="finite"):
            validate_filter({"score_ranges": {"p": {"min": value}}})

    @pytest.mark.parametrize("value", ["-0.1", "1.1", "-1", "2"])
    def test_out_of_unit_interval_bound_rejected(self, value: str) -> None:
        with pytest.raises(QueryToolsError, match=r"\[0, 1\]"):
            validate_filter({"score_ranges": {"p": {"min": value}}})

    @pytest.mark.parametrize("value", ["abc", "", "0.1.2", "0,5"])
    def test_unparseable_bound_rejected(self, value: str) -> None:
        with pytest.raises(QueryToolsError, match="not a decimal"):
            validate_filter({"score_ranges": {"p": {"min": value}}})

    @pytest.mark.parametrize("value", ["0", "0.5", "1", "0.123456789012", 1, 0, Decimal("0.25")])
    def test_valid_bounds_accepted(self, value: Any) -> None:
        parsed = validate_filter({"score_ranges": {"m": {"max": value}}})
        dimension, low, high = parsed.score_ranges[0]
        assert dimension == "m"
        assert low is None
        assert high == Decimal(str(value)) if not isinstance(value, Decimal) else high == value

    def test_multiple_dimensions_preserved_in_canonical_order(self) -> None:
        parsed = validate_filter({"score_ranges": {"e": {"min": "0.1"}, "p": {"max": "0.9"}}})
        assert [dimension for dimension, _, _ in parsed.score_ranges] == ["p", "e"]


# ----------------------------------------------------------------------
# filter matching
# ----------------------------------------------------------------------


class TestFilterMatching:
    def _corpus(self) -> list[dict[str, Any]]:
        return [
            _node("a", domain="P1", kind="leaf", level=1, p="0.10"),
            _node("b", domain="P2", kind="branch", level=2, p="0.50"),
            _node("c", domain="P3", kind="child", level=3, p="0.90", lifecycle_status="draft"),
            _node("d", domain="P1", kind="leaf", level=5, p="0.70", lifecycle_status="maintain"),
        ]

    def test_no_filter_returns_all_sorted_by_id(self) -> None:
        nodes = list(reversed(self._corpus()))
        assert _ids(filter_nodes(nodes)) == ["a", "b", "c", "d"]

    def test_domain_filter(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"domains": ["P1"]})) == ["a", "d"]

    def test_kind_filter(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"kinds": ["leaf"]})) == ["a", "d"]

    def test_status_filter(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"lifecycle_statuses": ["draft", "maintain"]})) == ["c", "d"]

    def test_level_range(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"level_min": 2, "level_max": 3})) == ["b", "c"]

    def test_level_min_only(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"level_min": 3})) == ["c", "d"]

    def test_level_max_only(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"level_max": 1})) == ["a"]

    def test_score_min(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"score_ranges": {"p": {"min": "0.5"}}})) == ["b", "c", "d"]

    def test_score_max(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"score_ranges": {"p": {"max": "0.5"}}})) == ["a", "b"]

    def test_score_range_inclusive_bounds(self) -> None:
        assert _ids(filter_nodes(self._corpus(), {"score_ranges": {"p": {"min": "0.50", "max": "0.90"}}})) == [
            "b",
            "c",
            "d",
        ]

    def test_combined_filters_intersect(self) -> None:
        spec = {"domains": ["P1"], "score_ranges": {"p": {"min": "0.5"}}}
        assert _ids(filter_nodes(self._corpus(), spec)) == ["d"]

    def test_no_match_returns_empty(self) -> None:
        assert filter_nodes(self._corpus(), {"domains": ["P7"]}) == []

    def test_graph_mapping_input(self) -> None:
        graph = {"nodes": self._corpus(), "links": []}
        assert _ids(filter_nodes(graph, {"kinds": ["branch"]})) == ["b"]

    def test_non_mapping_nodes_ignored(self) -> None:
        nodes: list[Any] = self._corpus() + ["junk", 42, None]
        assert _ids(filter_nodes(nodes)) == ["a", "b", "c", "d"]

    def test_node_without_id_never_passes(self) -> None:
        node = _node("x")
        del node["id"]
        assert filter_nodes([node]) == []

    def test_node_with_empty_id_never_passes(self) -> None:
        assert filter_nodes([_node("")]) == []

    def test_missing_score_excluded_when_range_set(self) -> None:
        node = _node("x")
        del node["scores"]
        assert filter_nodes([node], {"score_ranges": {"p": {"min": "0"}}}) == []
        assert _ids(filter_nodes([node])) == ["x"]

    def test_malformed_score_excluded_when_range_set(self) -> None:
        node = _node("x", p="not-a-number")
        assert filter_nodes([node], {"score_ranges": {"p": {"min": "0"}}}) == []

    def test_out_of_range_score_excluded(self) -> None:
        node = _node("x", p="1.5")
        assert filter_nodes([node], {"score_ranges": {"p": {"min": "0"}}}) == []

    def test_missing_level_excluded_when_bound_set(self) -> None:
        node = _node("x")
        del node["professional_level"]
        assert filter_nodes([node], {"level_min": 1}) == []
        assert _ids(filter_nodes([node])) == ["x"]

    def test_bool_level_treated_as_missing(self) -> None:
        node = _node("x")
        node["professional_level"] = True
        assert filter_nodes([node], {"level_min": 1}) == []

    def test_out_of_range_level_treated_as_missing(self) -> None:
        assert filter_nodes([_node("x", level=9)], {"level_min": 1}) == []

    def test_unknown_domain_value_excluded_by_domain_filter(self) -> None:
        node = _node("x", domain="P9")
        assert filter_nodes([node], {"domains": ["P1"]}) == []
        assert _ids(filter_nodes([node])) == ["x"]

    def test_filter_does_not_mutate_spec(self) -> None:
        spec: dict[str, Any] = {"domains": ["P1"], "score_ranges": {"p": {"min": "0.1"}}}
        snapshot = canonical_json(spec)
        filter_nodes(self._corpus(), spec)
        assert canonical_json(spec) == snapshot

    @pytest.mark.parametrize("dimension", AGGREGATE_DIMENSIONS)
    def test_every_dimension_filters(self, dimension: str) -> None:
        overrides: dict[str, Any] = {dimension: "0.95"}
        nodes = [_node("low"), _node("high", **overrides)]
        result = filter_nodes(nodes, {"score_ranges": {dimension: {"min": "0.9"}}})
        assert _ids(result) == ["high"]

    def test_determinism_under_input_permutation(self) -> None:
        corpus = self._corpus()
        spec = {"domains": ["P1", "P2", "P3"], "level_min": 1}
        baseline = canonical_json([_ids(filter_nodes(corpus, spec))])
        for perm in itertools.islice(itertools.permutations(corpus), 12):
            assert canonical_json([_ids(filter_nodes(list(perm), spec))]) == baseline


# ----------------------------------------------------------------------
# sorting
# ----------------------------------------------------------------------


class TestSort:
    def _corpus(self) -> list[dict[str, Any]]:
        return [
            _node("a", domain="P2", level=3, p="0.50", label="zebra"),
            _node("b", domain="P1", level=1, p="0.90", label="apple"),
            _node("c", domain="P1", level=2, p="0.90", label="mango"),
            _node("d", domain="P3", level=1, p="0.10", label="banana"),
        ]

    def test_default_sort_is_id_ascending(self) -> None:
        assert _ids(sort_nodes(list(reversed(self._corpus())))) == ["a", "b", "c", "d"]

    def test_string_sugar(self) -> None:
        assert _ids(sort_nodes(self._corpus(), ["domain"])) == ["b", "c", "a", "d"]

    def test_dash_prefix_descending(self) -> None:
        assert _ids(sort_nodes(self._corpus(), ["-p"])) == ["b", "c", "a", "d"]

    def test_mapping_form(self) -> None:
        spec = [{"field": "domain", "direction": "desc"}]
        assert _ids(sort_nodes(self._corpus(), spec)) == ["d", "a", "b", "c"]

    def test_direction_defaults_to_asc(self) -> None:
        assert _ids(sort_nodes(self._corpus(), [{"field": "professional_level"}])) == ["b", "d", "c", "a"]

    def test_multi_key(self) -> None:
        spec = [{"field": "domain"}, {"field": "p", "direction": "desc"}]
        assert _ids(sort_nodes(self._corpus(), spec)) == ["b", "c", "a", "d"]

    def test_tie_breaks_on_id(self) -> None:
        result = sort_nodes(self._corpus(), [{"field": "p", "direction": "desc"}])
        assert _ids(result) == ["b", "c", "a", "d"]  # b,c tie at 0.90 → id order

    def test_label_sort(self) -> None:
        assert _ids(sort_nodes(self._corpus(), ["label"])) == ["b", "d", "c", "a"]

    def test_version_sort(self) -> None:
        nodes = [_node("a", version=3), _node("b", version=1), _node("c", version=2)]
        assert _ids(sort_nodes(nodes, ["version"])) == ["b", "c", "a"]

    def test_status_sort(self) -> None:
        nodes = [_node("a", lifecycle_status="maintain"), _node("b", lifecycle_status="draft")]
        assert _ids(sort_nodes(nodes, ["lifecycle_status"])) == ["b", "a"]

    def test_kind_sort(self) -> None:
        nodes = [_node("a", kind="leaf"), _node("b", kind="branch")]
        assert _ids(sort_nodes(nodes, ["kind"])) == ["b", "a"]

    @pytest.mark.parametrize("field", ["p", "m", "e", "uncertainty", "confidence"])
    def test_score_field_sorts_numerically_not_lexically(self, field: str) -> None:
        low: dict[str, Any] = {field: "0.10"}
        high: dict[str, Any] = {field: "0.9"}
        nodes = [_node("a", **high), _node("b", **low)]
        assert _ids(sort_nodes(nodes, [field])) == ["b", "a"]

    def test_missing_value_sorts_last_ascending(self) -> None:
        node = _node("z")
        del node["scores"]
        assert _ids(sort_nodes([node, _node("a")], ["p"])) == ["a", "z"]

    def test_missing_value_sorts_last_descending(self) -> None:
        node = _node("z")
        del node["scores"]
        assert _ids(sort_nodes([node, _node("a")], ["-p"])) == ["a", "z"]

    def test_explicit_id_key_not_duplicated(self) -> None:
        parsed = validate_sort(["id"])
        assert len(parsed) == 1

    def test_implicit_id_tiebreak_appended(self) -> None:
        parsed = validate_sort(["p"])
        assert parsed[-1].field == "id"
        assert parsed[-1].descending is False

    def test_none_spec_empty(self) -> None:
        assert validate_sort(None) == ()

    def test_empty_spec_empty(self) -> None:
        assert validate_sort([]) == ()

    @pytest.mark.parametrize("spec", ["p", 42, {"field": "p"}])
    def test_non_list_spec_rejected(self, spec: Any) -> None:
        with pytest.raises(QueryToolsError):
            validate_sort(spec)

    @pytest.mark.parametrize("field", ["summary", "provenance", "source_file", "score", "id_", "P"])
    def test_unsortable_field_rejected(self, field: str) -> None:
        with pytest.raises(QueryToolsError, match="not sortable"):
            validate_sort([field])

    @pytest.mark.parametrize("field", SORTABLE_FIELDS)
    def test_all_sortable_fields_accepted(self, field: str) -> None:
        assert validate_sort([field])[0].field == field

    @pytest.mark.parametrize("direction", ["up", "down", "ASC", "descending", ""])
    def test_bad_direction_rejected(self, direction: str) -> None:
        with pytest.raises(QueryToolsError, match="asc.*desc"):
            validate_sort([{"field": "p", "direction": direction}])

    def test_unknown_sort_key_entry_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="unknown sort key"):
            validate_sort([{"field": "p", "weight": 2}])

    def test_empty_field_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            validate_sort([{"field": ""}])

    def test_dash_only_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            validate_sort(["-"])

    def test_non_mapping_non_string_entry_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            validate_sort([42])

    def test_determinism_under_input_permutation(self) -> None:
        corpus = self._corpus()
        spec = [{"field": "domain"}, {"field": "p", "direction": "desc"}]
        baseline = _ids(sort_nodes(corpus, spec))
        for perm in itertools.islice(itertools.permutations(corpus), 12):
            assert _ids(sort_nodes(list(perm), spec)) == baseline


# ----------------------------------------------------------------------
# pagination
# ----------------------------------------------------------------------


class TestPagination:
    def _nodes(self, count: int = 10) -> list[dict[str, Any]]:
        return [_node(f"n{index:02d}") for index in range(count)]

    def test_default_limit(self) -> None:
        page = paginate_nodes(self._nodes(DEFAULT_PAGE_LIMIT + 5))
        assert page["count"] == DEFAULT_PAGE_LIMIT
        assert page["has_more"] is True

    def test_first_page(self) -> None:
        page = paginate_nodes(self._nodes(5), limit=3)
        assert _ids(page["items"]) == ["n00", "n01", "n02"]
        assert page["has_more"] is True
        assert page["next_cursor"] == "n02"
        assert page["cursor"] is None
        assert page["total"] == 5

    def test_walk_to_end(self) -> None:
        nodes = self._nodes(7)
        seen: list[str] = []
        cursor = None
        for _ in range(10):
            page = paginate_nodes(nodes, cursor=cursor, limit=3)
            seen.extend(_ids(page["items"]))
            if not page["has_more"]:
                assert page["next_cursor"] is None
                break
            cursor = page["next_cursor"]
        assert seen == _ids(nodes)

    def test_exact_multiple_last_page_has_no_cursor(self) -> None:
        nodes = self._nodes(6)
        first = paginate_nodes(nodes, limit=3)
        second = paginate_nodes(nodes, cursor=first["next_cursor"], limit=3)
        assert second["has_more"] is False
        assert second["next_cursor"] is None

    def test_cursor_after_last_id_gives_empty_page(self) -> None:
        page = paginate_nodes(self._nodes(3), cursor="n02", limit=2)
        assert page["items"] == []
        assert page["count"] == 0
        assert page["has_more"] is False

    def test_unknown_cursor_fails_closed(self) -> None:
        with pytest.raises(QueryToolsError, match="not part of this ordered result set"):
            paginate_nodes(self._nodes(3), cursor="nope")

    @pytest.mark.parametrize("cursor", ["", 42, 3.5, ["n00"]])
    def test_malformed_cursor_rejected(self, cursor: Any) -> None:
        with pytest.raises(QueryToolsError, match="cursor"):
            paginate_nodes(self._nodes(3), cursor=cursor)

    @pytest.mark.parametrize("limit", [0, -1])
    def test_non_positive_limit_rejected(self, limit: int) -> None:
        with pytest.raises(QueryToolsError, match="positive"):
            paginate_nodes(self._nodes(3), limit=limit)

    def test_limit_above_max_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match=str(MAX_PAGE_LIMIT)):
            paginate_nodes(self._nodes(3), limit=MAX_PAGE_LIMIT + 1)

    @pytest.mark.parametrize("limit", ["3", 2.5, True])
    def test_non_int_limit_rejected(self, limit: Any) -> None:
        with pytest.raises(QueryToolsError):
            paginate_nodes(self._nodes(3), limit=limit)

    def test_nodes_without_id_skipped(self) -> None:
        node = _node("keep")
        ghost = _node("ghost")
        del ghost["id"]
        page = paginate_nodes([ghost, node], limit=5)
        assert _ids(page["items"]) == ["keep"]
        assert page["total"] == 1

    def test_empty_input(self) -> None:
        page = paginate_nodes([], limit=5)
        assert page["count"] == 0
        assert page["has_more"] is False
        assert page["total"] == 0


# ----------------------------------------------------------------------
# aggregation
# ----------------------------------------------------------------------


class TestAggregation:
    def _corpus(self) -> list[dict[str, Any]]:
        return [
            _node("a", domain="P1", p="0.10", m="0.20", e="0.30"),
            _node("b", domain="P1", p="0.30", m="0.40", e="0.50"),
            _node("c", domain="P2", p="0.90", m="0.90", e="0.90", kind="branch"),
        ]

    def test_group_by_domain(self) -> None:
        result = aggregate_nodes(self._corpus(), group_by="domain")
        assert result["group_by"] == "domain"
        assert result["total"] == 3
        assert result["group_count"] == 2
        groups = {group["key"]: group for group in result["groups"]}
        assert groups["P1"]["count"] == 2
        assert groups["P2"]["count"] == 1

    def test_min_max_mean_exact(self) -> None:
        result = aggregate_nodes(self._corpus(), group_by="domain")
        p1 = next(group for group in result["groups"] if group["key"] == "P1")
        assert p1["scores"]["p"] == {
            "min": "0.100000000000",
            "max": "0.300000000000",
            "mean": "0.200000000000",
            "missing": 0,
            "histogram": [0, 1, 0, 1, 0, 0, 0, 0, 0, 0],
        }

    def test_mean_quantized_half_even(self) -> None:
        nodes = [_node("a", p="0.0000000000001"), _node("b", p="0.0000000000002")]
        # mean 0.00000000000015 → 12dp → 0.000000000000 (ties-to-even on the 13th digit 1.5→ wait: quantized)
        result = aggregate_nodes(nodes, group_by="domain")
        mean = result["groups"][0]["scores"]["p"]["mean"]
        assert Decimal(mean) == Decimal("0.000000000000")

    def test_mean_one_third(self) -> None:
        nodes = [_node("a", p="1"), _node("b", p="0"), _node("c", p="0")]
        result = aggregate_nodes(nodes, group_by="domain")
        assert result["groups"][0]["scores"]["p"]["mean"] == "0.333333333333"

    @pytest.mark.parametrize("field", GROUP_BY_FIELDS)
    def test_every_group_field(self, field: str) -> None:
        result = aggregate_nodes(self._corpus(), group_by=field)
        assert result["group_by"] == field
        assert result["total"] == 3

    @pytest.mark.parametrize("field", ["id", "label", "p", "version", ""])
    def test_bad_group_field_rejected(self, field: str) -> None:
        with pytest.raises(QueryToolsError):
            aggregate_nodes(self._corpus(), group_by=field)

    def test_groups_sorted_by_key(self) -> None:
        nodes = [_node("a", domain="P3"), _node("b", domain="P1"), _node("c", domain="P2")]
        result = aggregate_nodes(nodes, group_by="domain")
        assert [group["key"] for group in result["groups"]] == ["P1", "P2", "P3"]

    def test_missing_scores_counted_not_crashing(self) -> None:
        node = _node("x")
        del node["scores"]["p"]
        result = aggregate_nodes([node, _node("y", p="0.5")], group_by="domain")
        scores = result["groups"][0]["scores"]["p"]
        assert scores["missing"] == 1
        assert scores["min"] == "0.500000000000"
        assert scores["mean"] == "0.500000000000"

    def test_all_missing_dimension_is_null(self) -> None:
        node = _node("x")
        del node["scores"]["e"]
        result = aggregate_nodes([node], group_by="domain")
        assert result["groups"][0]["scores"]["e"] == {
            "min": None,
            "max": None,
            "mean": None,
            "missing": 1,
            "histogram": [0] * 10,
        }

    def test_empty_input(self) -> None:
        result = aggregate_nodes([], group_by="domain")
        assert result["total"] == 0
        assert result["groups"] == []

    def test_missing_group_value_bucket(self) -> None:
        node = _node("x")
        del node["domain"]
        result = aggregate_nodes([node], group_by="domain")
        assert result["groups"][0]["key"] == "<missing>"

    def test_level_groups_render_as_strings(self) -> None:
        result = aggregate_nodes([_node("a", level=2), _node("b", level=2)], group_by="professional_level")
        assert result["groups"][0]["key"] == "2"

    def test_all_five_dimensions_present(self) -> None:
        result = aggregate_nodes([_node("a")], group_by="domain")
        assert set(result["groups"][0]["scores"]) == set(AGGREGATE_DIMENSIONS)

    def test_determinism_under_input_permutation(self) -> None:
        corpus = self._corpus()
        baseline = canonical_json(aggregate_nodes(corpus, group_by="domain"))
        for perm in itertools.islice(itertools.permutations(corpus), 6):
            assert canonical_json(aggregate_nodes(list(perm), group_by="domain")) == baseline


# ----------------------------------------------------------------------
# faceting
# ----------------------------------------------------------------------


class TestFaceting:
    def _corpus(self) -> list[dict[str, Any]]:
        return [
            _node("a", domain="P1", kind="leaf"),
            _node("b", domain="P1", kind="branch"),
            _node("c", domain="P2", kind="leaf"),
        ]

    def test_default_facets_all_fields(self) -> None:
        result = facet_nodes(self._corpus())
        assert set(result["facets"]) == set(FACET_FIELDS)
        assert result["total"] == 3

    def test_single_field(self) -> None:
        result = facet_nodes(self._corpus(), fields=["domain"])
        assert list(result["facets"]) == ["domain"]

    def test_count_desc_then_value_asc(self) -> None:
        result = facet_nodes(self._corpus(), fields=["domain"])
        assert result["facets"]["domain"] == [
            {"value": "P1", "count": 2},
            {"value": "P2", "count": 1},
        ]

    def test_tie_breaks_by_value(self) -> None:
        result = facet_nodes(self._corpus(), fields=["kind"])
        assert result["facets"]["kind"] == [
            {"value": "branch", "count": 1},
            {"value": "leaf", "count": 2},
        ] or result["facets"]["kind"] == [
            {"value": "leaf", "count": 2},
            {"value": "branch", "count": 1},
        ]
        # leaf has count 2 → strictly first; make it exact:
        assert result["facets"]["kind"][0] == {"value": "leaf", "count": 2}

    def test_missing_value_faceted(self) -> None:
        node = _node("x")
        del node["kind"]
        result = facet_nodes([node], fields=["kind"])
        assert result["facets"]["kind"] == [{"value": "<missing>", "count": 1}]

    def test_empty_input(self) -> None:
        result = facet_nodes([], fields=["domain"])
        assert result["facets"]["domain"] == []

    @pytest.mark.parametrize("field", ["id", "label", "p", ""])
    def test_bad_field_rejected(self, field: str) -> None:
        with pytest.raises(QueryToolsError):
            facet_nodes(self._corpus(), fields=[field])

    def test_string_fields_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            facet_nodes(self._corpus(), fields="domain")

    def test_determinism_under_input_permutation(self) -> None:
        corpus = self._corpus()
        baseline = canonical_json(facet_nodes(corpus))
        for perm in itertools.islice(itertools.permutations(corpus), 6):
            assert canonical_json(facet_nodes(list(perm))) == baseline


# ----------------------------------------------------------------------
# lexical search with filters
# ----------------------------------------------------------------------


class TestSearch:
    def _corpus(self) -> list[dict[str, Any]]:
        return [
            _node("alpha_notes", label="calibration of bayesian estimators", domain="P1", p="0.10"),
            _node("beta_notes", label="calibration of physical sensors", domain="P2", p="0.90"),
            _node("gamma_notes", label="unrelated cooking recipe", domain="P1", p="0.90"),
        ]

    def test_basic_ranking(self) -> None:
        result = search_nodes(self._corpus(), "calibration")
        assert result["total"] == 2
        assert {hit["node_id"] for hit in result["hits"]} == {"alpha_notes", "beta_notes"}
        assert result["query_terms"] == ["calibration"]

    def test_filter_applies_to_hits(self) -> None:
        result = search_nodes(self._corpus(), "calibration", node_filter={"domains": ["P2"]})
        assert [hit["node_id"] for hit in result["hits"]] == ["beta_notes"]

    def test_filter_by_score(self) -> None:
        result = search_nodes(self._corpus(), "calibration", node_filter={"score_ranges": {"p": {"min": "0.5"}}})
        assert [hit["node_id"] for hit in result["hits"]] == ["beta_notes"]

    def test_filter_preserves_rank_order(self) -> None:
        plain = search_nodes(self._corpus(), "calibration")
        filtered = search_nodes(self._corpus(), "calibration", node_filter={"domains": ["P1"]})
        kept = [hit["node_id"] for hit in plain["hits"] if hit["node_id"] == "alpha_notes"]
        assert [hit["node_id"] for hit in filtered["hits"]] == kept

    def test_empty_query_matches_nothing(self) -> None:
        result = search_nodes(self._corpus(), "")
        assert result["hits"] == []
        assert result["examined"] == 0
        assert result["query_terms"] == []

    def test_stopword_only_query_matches_nothing(self) -> None:
        result = search_nodes(self._corpus(), "the and of")
        assert result["hits"] == []

    def test_scores_rendered_fixed_point(self) -> None:
        result = search_nodes(self._corpus(), "calibration")
        for hit in result["hits"]:
            Decimal(hit["score"])  # parses
            assert "." in hit["score"]
            assert len(hit["score"].split(".")[1]) == 12

    def test_limit_truncates(self) -> None:
        result = search_nodes(self._corpus(), "calibration", limit=1)
        assert result["total"] == 1
        assert result["truncated"] is True

    def test_no_limit_no_truncation(self) -> None:
        result = search_nodes(self._corpus(), "calibration")
        assert result["truncated"] is False

    @pytest.mark.parametrize("limit", [0, -1, "1"])
    def test_bad_limit_rejected(self, limit: Any) -> None:
        with pytest.raises(QueryToolsError):
            search_nodes(self._corpus(), "calibration", limit=limit)

    def test_bad_filter_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            search_nodes(self._corpus(), "calibration", node_filter={"bogus": 1})

    def test_external_index_used(self) -> None:
        nodes = self._corpus()
        index = LexicalIndex.from_graph_nodes(nodes)
        result = search_nodes(nodes, "calibration", index=index)
        assert result["total"] == 2

    def test_external_index_unknown_ids_skipped(self) -> None:
        nodes = self._corpus()
        extra = nodes + [_node("ghost", label="calibration ghost")]
        index = LexicalIndex.from_graph_nodes(extra)
        result = search_nodes(nodes, "calibration", index=index)
        assert "ghost" not in {hit["node_id"] for hit in result["hits"]}
        assert result["examined"] == 3

    def test_examined_counts_ranked_hits_before_filter(self) -> None:
        result = search_nodes(self._corpus(), "calibration", node_filter={"domains": ["P2"]})
        assert result["examined"] == 2
        assert result["total"] == 1

    def test_graph_mapping_input(self) -> None:
        graph = {"nodes": self._corpus()}
        assert search_nodes(graph, "calibration")["total"] == 2

    def test_determinism_under_input_permutation(self) -> None:
        corpus = self._corpus()
        baseline = canonical_json(search_nodes(corpus, "calibration of"))
        for perm in itertools.islice(itertools.permutations(corpus), 6):
            assert canonical_json(search_nodes(list(perm), "calibration of")) == baseline


# ----------------------------------------------------------------------
# near-duplicate helpers
# ----------------------------------------------------------------------


class TestNormalizeAndShingles:
    def test_normalize_casefolds(self) -> None:
        assert normalize_summary("Alpha BETA") == ("alpha", "beta")

    def test_normalize_strips_stopwords(self) -> None:
        assert "the" not in normalize_summary("the alpha of the beta")

    def test_normalize_folds_plurals(self) -> None:
        assert normalize_summary("calibrations") == normalize_summary("calibration")

    def test_stopword_only_falls_back_to_raw_tokens(self) -> None:
        assert normalize_summary("the and of") != ()

    def test_empty_text_normalizes_empty(self) -> None:
        assert normalize_summary("") == ()

    def test_shingles_basic(self) -> None:
        shingles = shingle_tokens(("a", "b", "c", "d"), 2)
        assert shingles == frozenset({("a", "b"), ("b", "c"), ("c", "d")})

    def test_shorter_than_shingle_size_single_shingle(self) -> None:
        assert shingle_tokens(("a", "b"), 3) == frozenset({("a", "b")})

    def test_empty_tokens_no_shingles(self) -> None:
        assert shingle_tokens((), 3) == frozenset()

    def test_shingle_size_one_is_unigrams(self) -> None:
        assert shingle_tokens(("a", "b"), 1) == frozenset({("a",), ("b",)})

    @pytest.mark.parametrize("size", [0, -1])
    def test_bad_shingle_size_rejected(self, size: int) -> None:
        with pytest.raises(QueryToolsError):
            shingle_tokens(("a",), size)

    def test_duplicate_shingles_collapse(self) -> None:
        assert shingle_tokens(("a", "a", "a"), 2) == frozenset({("a", "a")})


# ----------------------------------------------------------------------
# near-duplicate detection (proposal-only)
# ----------------------------------------------------------------------


def _dup_node(node_id: str, label: str, *, domain: str = "P1") -> dict[str, Any]:
    return _node(node_id, label=label, domain=domain)


class TestNearDuplicates:
    def test_identical_summaries_flagged_exact(self) -> None:
        nodes = [
            _dup_node("a", "calibration of bayesian estimators under drift"),
            _dup_node("b", "calibration of bayesian estimators under drift"),
        ]
        result = find_near_duplicates(nodes)
        assert result["candidate_count"] == 1
        candidate = result["candidates"][0]
        assert candidate["node_a"] == "a"
        assert candidate["node_b"] == "b"
        assert candidate["exact"] is True
        assert candidate["score"] == "1.000000000000"

    def test_case_and_stopword_variants_are_exact(self) -> None:
        nodes = [
            _dup_node("a", "The Calibration of Bayesian Estimators"),
            _dup_node("b", "calibration of the bayesian estimators"),
        ]
        result = find_near_duplicates(nodes)
        assert result["candidate_count"] == 1
        assert result["candidates"][0]["exact"] is True

    def test_partial_overlap_scored_by_jaccard(self) -> None:
        # tokens a: alpha beta gamma delta → shingles(3): abg, bgd
        # tokens b: alpha beta gamma epsilon → shingles(3): abg, bge
        # intersection 1, union 3 → 1/3 ≈ 0.3333
        nodes = [
            _dup_node("a", "alpha beta gamma delta"),
            _dup_node("b", "alpha beta gamma epsilon"),
        ]
        result = find_near_duplicates(nodes, threshold="0.3")
        assert result["candidate_count"] == 1
        candidate = result["candidates"][0]
        assert candidate["exact"] is False
        assert candidate["score"] == "0.333333333333"
        assert "jaccard 0.333333333333" in candidate["rationale"]

    def test_below_threshold_not_proposed(self) -> None:
        nodes = [
            _dup_node("a", "alpha beta gamma delta"),
            _dup_node("b", "alpha beta gamma epsilon"),
        ]
        result = find_near_duplicates(nodes)  # default 0.8; similarity 1/3
        assert result["candidate_count"] == 0
        assert result["candidates"] == []

    def test_at_threshold_is_proposed(self) -> None:
        # Construct similarity exactly 0.8: shingle_size 1,
        # a: {x1..x4, y1} shared 4 of 5, b: {x1..x4, y2} → 4/6 = 2/3. Use
        # shingle_size 1 with 4 shared of union 5: a={a,b,c,d}, b={a,b,c,d,e}? |∩|=4, |∪|=5 → 0.8
        nodes = [
            _dup_node("n1", "aa bb cc dd"),
            _dup_node("n2", "aa bb cc dd ee"),
        ]
        result = find_near_duplicates(nodes, threshold="0.8", shingle_size=1)
        assert result["candidate_count"] == 1
        assert result["candidates"][0]["score"] == "0.800000000000"

    def test_cross_domain_pairs_not_compared(self) -> None:
        nodes = [
            _dup_node("a", "identical summary text here", domain="P1"),
            _dup_node("b", "identical summary text here", domain="P2"),
        ]
        result = find_near_duplicates(nodes)
        assert result["candidate_count"] == 0
        assert result["stats"]["pairs_compared"] == 0
        assert result["stats"]["blocks"] == {"P1": 1, "P2": 1}

    def test_blocking_reduces_pair_count(self) -> None:
        nodes = [
            _dup_node("a", "text one", domain="P1"),
            _dup_node("b", "text two", domain="P1"),
            _dup_node("c", "text three", domain="P2"),
        ]
        result = find_near_duplicates(nodes)
        # P1 block: 1 pair; P2 block: singleton → 0 pairs.
        assert result["stats"]["pairs_compared"] == 1

    def test_proposal_only_fields(self) -> None:
        nodes = [_dup_node("a", "same text here"), _dup_node("b", "same text here")]
        result = find_near_duplicates(nodes)
        assert result["disposition"] == "proposal_only"
        assert result["auto_merge"] is False

    def test_candidates_sorted_by_score_then_ids(self) -> None:
        nodes = [
            _dup_node("a", "alpha beta gamma delta epsilon"),
            _dup_node("b", "alpha beta gamma delta epsilon"),  # exact dup of a
            _dup_node("c", "alpha beta gamma delta zeta"),  # partial overlap
        ]
        result = find_near_duplicates(nodes, threshold="0.5")
        pairs = [(c["node_a"], c["node_b"]) for c in result["candidates"]]
        assert pairs[0] == ("a", "b")  # exact first
        assert ("a", "c") in pairs and ("b", "c") in pairs

    def test_rationale_mentions_threshold(self) -> None:
        nodes = [_dup_node("a", "alpha beta gamma delta"), _dup_node("b", "alpha beta gamma epsilon")]
        result = find_near_duplicates(nodes, threshold="0.3")
        assert "meets threshold 0.300000000000" in result["candidates"][0]["rationale"]

    def test_exact_rationale_mentions_hash(self) -> None:
        nodes = [_dup_node("a", "same summary"), _dup_node("b", "same summary")]
        result = find_near_duplicates(nodes)
        assert "content hash match" in result["candidates"][0]["rationale"]

    def test_empty_labels_skipped(self) -> None:
        node = _dup_node("a", "real summary text")
        ghost = _node("b", label="placeholder")
        ghost["label"] = "!!!"  # tokenizes to nothing → no identity, skipped
        result = find_near_duplicates([node, ghost])
        assert result["stats"]["nodes_considered"] == 1
        assert result["stats"]["nodes_skipped"] == 1

    def test_nodes_without_id_skipped(self) -> None:
        node = _dup_node("a", "some text")
        ghost = _dup_node("b", "some text")
        del ghost["id"]
        result = find_near_duplicates([node, ghost])
        assert result["stats"]["nodes_considered"] == 1

    def test_stats_blocks_sorted(self) -> None:
        nodes = [
            _dup_node("a", "t1", domain="P3"),
            _dup_node("b", "t2", domain="P1"),
            _dup_node("c", "t3", domain="P1"),
        ]
        result = find_near_duplicates(nodes)
        assert list(result["stats"]["blocks"]) == ["P1", "P3"]

    def test_default_threshold_is_frozen_constant(self) -> None:
        nodes = [_dup_node("a", "alpha beta gamma delta"), _dup_node("b", "alpha beta gamma epsilon")]
        result = find_near_duplicates(nodes)
        assert result["threshold"] == "0.800000000000"
        assert result["shingle_size"] == 3
        assert DEFAULT_JACCARD_THRESHOLD == Decimal("0.8")

    def test_max_candidates_truncates_deterministically(self) -> None:
        nodes = [
            _dup_node("a", "same words here"),
            _dup_node("b", "same words here"),
            _dup_node("c", "same words here"),
        ]
        result = find_near_duplicates(nodes, max_candidates=2)
        assert result["candidate_count"] == 2
        assert result["truncated"] is True
        pairs = [(c["node_a"], c["node_b"]) for c in result["candidates"]]
        assert pairs == [("a", "b"), ("a", "c")]

    @pytest.mark.parametrize("value", [0, -1, "2", True])
    def test_bad_max_candidates_rejected(self, value: Any) -> None:
        with pytest.raises(QueryToolsError):
            find_near_duplicates([], max_candidates=value)

    @pytest.mark.parametrize("value", [0, -3, "2", 2.5])
    def test_bad_shingle_size_rejected(self, value: Any) -> None:
        with pytest.raises(QueryToolsError):
            find_near_duplicates([], shingle_size=value)

    @pytest.mark.parametrize("value", ["1.5", "-0.1", 0.9, "abc"])
    def test_bad_threshold_rejected(self, value: Any) -> None:
        with pytest.raises(QueryToolsError):
            find_near_duplicates([], threshold=value)

    def test_empty_corpus(self) -> None:
        result = find_near_duplicates([])
        assert result["candidate_count"] == 0
        assert result["stats"]["nodes_considered"] == 0
        assert result["stats"]["blocks"] == {}

    def test_graph_mapping_input(self) -> None:
        graph = {"nodes": [_dup_node("a", "same text"), _dup_node("b", "same text")]}
        assert find_near_duplicates(graph)["candidate_count"] == 1

    def test_determinism_under_input_permutation(self) -> None:
        nodes = [
            _dup_node("a", "alpha beta gamma delta epsilon"),
            _dup_node("b", "alpha beta gamma delta epsilon"),
            _dup_node("c", "alpha beta gamma delta zeta"),
            _dup_node("d", "completely different words entirely"),
        ]
        baseline = canonical_json(find_near_duplicates(nodes, threshold="0.5"))
        for perm in itertools.permutations(nodes):
            assert canonical_json(find_near_duplicates(list(perm), threshold="0.5")) == baseline

    def test_output_is_json_serializable(self) -> None:
        nodes = [_dup_node("a", "same text"), _dup_node("b", "same text")]
        canonical_json(find_near_duplicates(nodes))


# ----------------------------------------------------------------------
# composed query
# ----------------------------------------------------------------------


class TestQueryNodes:
    def _graph(self) -> dict[str, Any]:
        return {
            "nodes": [
                _node("a", domain="P1", p="0.10", level=1),
                _node("b", domain="P1", p="0.90", level=3),
                _node("c", domain="P2", p="0.50", level=2),
                _node("d", domain="P2", p="0.70", level=5, lifecycle_status="draft"),
            ]
        }

    def test_empty_spec_returns_first_page(self) -> None:
        result = query_nodes(self._graph())
        assert result["total_nodes"] == 4
        assert result["total_filtered"] == 4
        assert result["page"]["count"] == 4
        assert "aggregation" not in result
        assert "facets" not in result

    def test_none_spec(self) -> None:
        assert query_nodes(self._graph(), None)["total_filtered"] == 4

    def test_filter_then_page(self) -> None:
        result = query_nodes(self._graph(), {"filter": {"domains": ["P1"]}, "page": {"limit": 1}})
        assert result["total_filtered"] == 2
        assert _ids(result["page"]["items"]) == ["a"]
        assert result["page"]["has_more"] is True

    def test_cursor_walk_over_filtered_set(self) -> None:
        first = query_nodes(self._graph(), {"filter": {"domains": ["P1"]}, "page": {"limit": 1}})
        second = query_nodes(
            self._graph(),
            {"filter": {"domains": ["P1"]}, "page": {"limit": 1, "cursor": first["page"]["next_cursor"]}},
        )
        assert _ids(second["page"]["items"]) == ["b"]
        assert second["page"]["has_more"] is False

    def test_sort_applies_before_paging(self) -> None:
        result = query_nodes(
            self._graph(),
            {"sort": [{"field": "p", "direction": "desc"}], "page": {"limit": 2}},
        )
        assert _ids(result["page"]["items"]) == ["b", "d"]

    def test_aggregation_over_full_filtered_set_not_page(self) -> None:
        result = query_nodes(
            self._graph(),
            {"filter": {"domains": ["P1"]}, "page": {"limit": 1}, "group_by": "domain"},
        )
        assert result["page"]["count"] == 1
        aggregation = result["aggregation"]
        assert aggregation["total"] == 2
        group = aggregation["groups"][0]
        assert group["count"] == 2
        assert group["scores"]["p"]["mean"] == "0.500000000000"

    def test_facets_over_full_filtered_set(self) -> None:
        result = query_nodes(self._graph(), {"filter": {"domains": ["P2"]}, "page": {"limit": 1}, "facets": ["kind"]})
        assert result["page"]["count"] == 1
        kinds = {entry["value"]: entry["count"] for entry in result["facets"]["facets"]["kind"]}
        assert kinds == {"leaf": 2}

    @pytest.mark.parametrize("key", ["query", "where", "aggregate", "limit", "cursor", "filterr"])
    def test_unknown_query_key_rejected(self, key: str) -> None:
        with pytest.raises(QueryToolsError, match="unknown query key"):
            query_nodes(self._graph(), {key: {}})

    @pytest.mark.parametrize("page", [[], "x", 42])
    def test_non_mapping_page_rejected(self, page: Any) -> None:
        with pytest.raises(QueryToolsError, match="'page' must be a mapping"):
            query_nodes(self._graph(), {"page": page})

    @pytest.mark.parametrize("key", ["offset", "size", "page_size"])
    def test_unknown_page_key_rejected(self, key: str) -> None:
        with pytest.raises(QueryToolsError, match="unknown page key"):
            query_nodes(self._graph(), {"page": {key: 1}})

    def test_non_mapping_facets_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="'facets' must be a list"):
            query_nodes(self._graph(), {"facets": "domain"})

    def test_non_mapping_spec_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            query_nodes(self._graph(), cast(Any, ["filter"]))

    def test_invalid_filter_inside_query_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            query_nodes(self._graph(), {"filter": {"bogus": 1}})

    def test_invalid_sort_inside_query_rejected(self) -> None:
        with pytest.raises(QueryToolsError):
            query_nodes(self._graph(), {"sort": ["bogus"]})

    def test_cursor_from_unfiltered_set_fails_on_filtered_set(self) -> None:
        with pytest.raises(QueryToolsError, match="not part of this ordered result set"):
            query_nodes(self._graph(), {"filter": {"domains": ["P1"]}, "page": {"cursor": "d"}})

    def test_schema_version_present(self) -> None:
        assert query_nodes(self._graph())["schema_version"] == 1

    def test_output_is_json_serializable(self) -> None:
        spec: dict[str, Any] = {
            "filter": {"level_min": 2},
            "sort": ["-p"],
            "page": {"limit": 2},
            "group_by": "kind",
            "facets": ["domain"],
        }
        result = query_nodes(self._graph(), spec)
        canonical_json(result)

    def test_determinism_under_input_permutation(self) -> None:
        graph = self._graph()
        spec: dict[str, Any] = {"sort": [{"field": "domain"}, "-p"], "group_by": "domain", "facets": ["kind"]}
        baseline = canonical_json(query_nodes(graph, spec))
        for perm in itertools.islice(itertools.permutations(graph["nodes"]), 12):
            assert canonical_json(query_nodes({"nodes": list(perm)}, spec)) == baseline


# ----------------------------------------------------------------------
# cross-cutting: frozen contract untouched
# ----------------------------------------------------------------------


class TestContractDistance:
    def test_module_imports_no_service_or_contracts(self) -> None:
        import know_your_soul.analytics.query_tools as module

        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        assert "know_your_soul.service" not in source
        assert "know_your_soul.contracts" not in source

    def test_no_clock_no_randomness(self) -> None:
        import know_your_soul.analytics.query_tools as module

        with open(module.__file__, encoding="utf-8") as handle:
            source = handle.read()
        assert "datetime.now" not in source
        assert "time.time" not in source
        assert "import random" not in source


# ----------------------------------------------------------------------
# histogram bins (frozen)
# ----------------------------------------------------------------------


class TestHistogram:
    @pytest.mark.parametrize(
        ("score", "expected_bin"),
        [
            ("0.0", 0),
            ("0.05", 0),
            ("0.099999999999", 0),
            ("0.1", 1),
            ("0.15", 1),
            ("0.5", 5),
            ("0.9", 9),
            ("0.999999999999", 9),
            ("1.0", 9),
            ("1", 9),
        ],
    )
    def test_bin_assignment(self, score: str, expected_bin: int) -> None:
        result = aggregate_nodes([_node("a", p=score)], group_by="domain")
        histogram = result["groups"][0]["scores"]["p"]["histogram"]
        assert len(histogram) == 10
        assert histogram[expected_bin] == 1
        assert sum(histogram) == 1

    def test_histogram_counts_match_group_count(self) -> None:
        nodes = [_node("a", p="0.05"), _node("b", p="0.55"), _node("c", p="0.95")]
        result = aggregate_nodes(nodes, group_by="domain")
        histogram = result["groups"][0]["scores"]["p"]["histogram"]
        assert histogram == [1, 0, 0, 0, 0, 1, 0, 0, 0, 1]
        assert sum(histogram) == 3

    def test_histogram_excludes_missing(self) -> None:
        node = _node("x")
        del node["scores"]["m"]
        result = aggregate_nodes([node, _node("y")], group_by="domain")
        histogram = result["groups"][0]["scores"]["m"]["histogram"]
        assert sum(histogram) == 1


# ----------------------------------------------------------------------
# traversal over the published link set
# ----------------------------------------------------------------------


def _link(source: str, target: str, relation: str = "supports") -> dict[str, Any]:
    return {"source": source, "target": target, "relation": relation}


class TestSubtree:
    def _graph(self) -> dict[str, Any]:
        return {
            "nodes": [_node(node_id) for node_id in ["r", "a", "b", "c", "d"]],
            "links": [
                _link("r", "b"),
                _link("r", "a"),
                _link("a", "c"),
                _link("b", "c"),
                _link("c", "d"),
            ],
        }

    def test_bfs_order_sorted_neighbors(self) -> None:
        result = subtree_nodes(self._graph(), "r")
        assert [entry["id"] for entry in result["nodes"]] == ["r", "a", "b", "c", "d"]
        assert result["count"] == 5

    def test_depths_recorded(self) -> None:
        result = subtree_nodes(self._graph(), "r")
        depths = {entry["id"]: entry["depth"] for entry in result["nodes"]}
        assert depths == {"r": 0, "a": 1, "b": 1, "c": 2, "d": 3}

    def test_via_records_first_edge(self) -> None:
        result = subtree_nodes(self._graph(), "r")
        via = {entry["id"]: entry["via"] for entry in result["nodes"]}
        assert via["r"] is None
        assert via["a"] == "supports"
        assert via["c"] == "supports"  # reached from "a" first (a < b)

    def test_relation_allowlist_restricts(self) -> None:
        graph = self._graph()
        graph["links"].append(_link("r", "d", "contradicts"))
        only_supports = subtree_nodes(graph, "r", relations=["supports"])
        assert [entry["id"] for entry in only_supports["nodes"]] == ["r", "a", "b", "c", "d"]
        only_contra = subtree_nodes(graph, "r", relations=["contradicts"])
        assert [entry["id"] for entry in only_contra["nodes"]] == ["r", "d"]

    def test_default_relations_cover_full_vocabulary(self) -> None:
        result = subtree_nodes(self._graph(), "r")
        assert sorted(result["relations"]) == result["relations"]
        assert len(result["relations"]) == 10

    def test_max_depth_zero_returns_root_only(self) -> None:
        result = subtree_nodes(self._graph(), "r", max_depth=0)
        assert [entry["id"] for entry in result["nodes"]] == ["r"]

    def test_max_depth_bounds_walk(self) -> None:
        result = subtree_nodes(self._graph(), "r", max_depth=1)
        assert [entry["id"] for entry in result["nodes"]] == ["r", "a", "b"]

    def test_cycles_terminate(self) -> None:
        graph = {
            "nodes": [_node("x"), _node("y")],
            "links": [_link("x", "y"), _link("y", "x")],
        }
        result = subtree_nodes(graph, "x")
        assert [entry["id"] for entry in result["nodes"]] == ["x", "y"]
        assert {entry["id"]: entry["depth"] for entry in result["nodes"]} == {"x": 0, "y": 1}

    def test_unknown_root_fails_closed(self) -> None:
        with pytest.raises(QueryToolsError, match="not a node of this read model"):
            subtree_nodes(self._graph(), "ghost")

    @pytest.mark.parametrize("root", ["", 42, None])
    def test_bad_root_rejected(self, root: Any) -> None:
        with pytest.raises(QueryToolsError, match="root_id"):
            subtree_nodes(self._graph(), root)

    @pytest.mark.parametrize("relation", ["relates_to", "SUPPORTS", "", "parent"])
    def test_unknown_relation_rejected(self, relation: str) -> None:
        with pytest.raises(QueryToolsError, match="closed relation vocabulary"):
            subtree_nodes(self._graph(), "r", relations=[relation])

    def test_empty_relations_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="must not be an empty list"):
            subtree_nodes(self._graph(), "r", relations=[])

    def test_string_relations_rejected(self) -> None:
        with pytest.raises(QueryToolsError, match="must be a list"):
            subtree_nodes(self._graph(), "r", relations="supports")

    @pytest.mark.parametrize("depth", [-1, "1", 1.5, True])
    def test_bad_max_depth_rejected(self, depth: Any) -> None:
        with pytest.raises(QueryToolsError, match="max_depth"):
            subtree_nodes(self._graph(), "r", max_depth=depth)

    def test_edges_to_unknown_targets_ignored(self) -> None:
        graph = {"nodes": [_node("r")], "links": [_link("r", "ghost")]}
        result = subtree_nodes(graph, "r")
        assert result["count"] == 1
        assert result["edges_considered"] == 0

    def test_malformed_links_ignored(self) -> None:
        graph = {
            "nodes": [_node("r"), _node("a")],
            "links": ["junk", {"source": "r"}, _link("r", "a"), _link("r", "a", "made_up")],
        }
        result = subtree_nodes(graph, "r")
        assert result["count"] == 2
        assert result["edges_considered"] == 1

    def test_duplicate_edges_collapse(self) -> None:
        graph = {"nodes": [_node("r"), _node("a")], "links": [_link("r", "a"), _link("r", "a")]}
        result = subtree_nodes(graph, "r")
        assert result["edges_considered"] == 1

    def test_determinism_under_link_permutation(self) -> None:
        graph = self._graph()
        baseline = canonical_json(subtree_nodes(graph, "r"))
        for perm in itertools.permutations(graph["links"]):
            assert canonical_json(subtree_nodes({"nodes": graph["nodes"], "links": list(perm)}, "r")) == baseline

    def test_output_is_json_serializable(self) -> None:
        canonical_json(subtree_nodes(self._graph(), "r"))

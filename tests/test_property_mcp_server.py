"""Property tests for the MCP adapter: schema validation, surface, envelopes.

Wave-2 follow-up to ``docs/recovery/codebase-push-property-tests.md``; pins
the binding adaptation contract (``docs/research/hermes-mcp-contract.md``
section 3.1, deep-research adaptations X1/X2/X6/X8) on arbitrary inputs:

* strict validation rejects unknown properties, missing required keys and
  wrong JSON types (string booleans, numeric strings, bools-as-integers) with
  a typed ``INVALID_ARGUMENTS`` — deterministically and without mutating the
  caller's arguments;
* the tool surface is the frozen fourteen operations plus the two contract-v3
  ingestion verbs, every name matches the MCP tool-name constraint, and no
  schema carries a path-like property except the confined v3 amendment
  (``path`` on exactly the two ingestion schemas) — the Graphify F1
  arbitrary-root shape is otherwise structurally absent;
* the error envelope is canonical JSON, sanitized (no control/bidi/zero-width
  characters), message-capped, and carries an audit id exactly when one
  exists;
* the nodes resource never exceeds its byte cap, truncates from the tail with
  an honest ``truncated`` flag, and stays sorted and deterministic;
* unknown tools and invalid arguments each produce exactly one final audit
  event (CLI parity, spec section 7.7) and ``isError: true`` — never a raw
  exception leak.

All runs are deterministic (hypothesis ``derandomize=True``).
"""

from __future__ import annotations

import json
import re
import tempfile
import unicodedata
from pathlib import Path
from typing import Any

import anyio
import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from mcp import types
from mcp.shared.exceptions import MCPError

from know_your_soul.audit import AuditLog
from know_your_soul.domain import canonical_json
from know_your_soul.mcp_server import (
    _RESOURCE_URIS,
    _TOOL_TO_OPERATION,
    EXPECTED_TOOLS,
    ToolError,
    _Adapter,
    _error_text,
    _tool_schemas,
    _validate_arguments,
)
from know_your_soul.projection import GraphProjector
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore

_TOOL_NAME_RE = re.compile(r"^[a-zA-Z0-9_-]{1,64}$")
_MAX = 1024  # the max_token_budget the test adapter is built with

_JSON_SCALAR = st.one_of(
    st.none(),
    st.booleans(),
    st.integers(min_value=-(10**6), max_value=10**6),
    st.text(max_size=24),
)
_JSON_VALUE = st.recursive(
    _JSON_SCALAR,
    lambda children: st.one_of(
        st.lists(children, max_size=3),
        st.dictionaries(keys=st.text(alphabet="abcd", max_size=6), values=children, max_size=3),
    ),
    max_leaves=8,
)


def _scratch() -> Path:
    return Path(tempfile.mkdtemp(prefix="soul-mcp-"))


def _adapter(**overrides: Any) -> _Adapter:
    tmp = _scratch()
    store = EventStore(tmp / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp / "graph")
    service = KnowledgeService(store, projector)
    kwargs: dict[str, Any] = {
        "graph_root": tmp / "graph",
        "audit_log": AuditLog(store),
        "actor": "test",
        "max_token_budget": _MAX,
        "resource_max_bytes": 4096,
    }
    kwargs.update(overrides)
    return _Adapter(service, **kwargs)


def _valid_arguments(tool: str) -> dict[str, Any]:
    base: dict[str, Any] = {
        "knowledge_query": {"purpose": "why", "token_budget": 256},
        "knowledge_get_node": {"node_id": "n-1"},
        "knowledge_get_subtree": {"node_id": "n-1"},
        "knowledge_log_event": {"node": {"node_id": "n-1"}},
        "knowledge_record_outcome": {"evidence": {"evidence_id": "e-1"}},
        "knowledge_evaluate": {"node_id": "n-1"},
        "knowledge_review": {"node_id": "n-1"},
        "knowledge_propose_improvement": {
            "node_id": "n-1",
            "diff": [{"op": "set_summary", "value": "x"}],
            "rationale": "because",
        },
        "knowledge_apply_improvement": {"proposal_id": "p-1"},
        "knowledge_rollback": {},
        # Contract v2 G1 retrieval verbs (operator ruling 2026-10-26).
        "knowledge_path": {"from_id": "n-1", "to_id": "n-2"},
        "knowledge_explain": {"node_id": "n-1"},
        "knowledge_affected": {"node_id": "n-1"},
        "knowledge_god_nodes": {},
        # Contract v3 ingestion verbs (audit F2) — the confined path amendment.
        "knowledge_ingest_file": {"path": "src/a.py"},
        "knowledge_ingest_directory": {"path": "src"},
    }[tool]
    return dict(base)


# ------------------------------------------------------------ schema surface


def test_tool_surface_is_the_frozen_v2_operations_plus_the_v3_ingestion_verbs() -> None:
    """Contract v2 (operator ruling 2026-10-26; addendum
    docs/contracts/retrieval-verbs-v2.md): the frozen ten spec operations plus
    the four G1 retrieval verbs — fourteen tools, bijectively mapped; contract
    v3 (addendum docs/contracts/ingestion-contract-v3.md, audit F2) adds
    exactly the two ingestion verbs."""
    assert len(EXPECTED_TOOLS) == 16
    assert tuple(sorted(EXPECTED_TOOLS)) == EXPECTED_TOOLS
    assert set(EXPECTED_TOOLS) == set(_TOOL_TO_OPERATION)
    operations = set(_TOOL_TO_OPERATION.values())
    assert len(operations) == 16  # the mapping is bijective
    frozen = {
        "knowledge.query",
        "knowledge.get_node",
        "knowledge.get_subtree",
        "knowledge.log_event",
        "knowledge.record_outcome",
        "knowledge.evaluate",
        "knowledge.review",
        "knowledge.propose_improvement",
        "knowledge.apply_improvement",
        "knowledge.rollback",
        "knowledge.path",
        "knowledge.explain",
        "knowledge.affected",
        "knowledge.god_nodes",
    }
    assert frozen <= operations  # the fourteen are unchanged
    assert operations - frozen == {"knowledge.ingest_file", "knowledge.ingest_directory"}


@given(index=st.integers(min_value=0, max_value=15))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_every_tool_name_matches_the_mcp_constraint(index: int) -> None:
    assert _TOOL_NAME_RE.fullmatch(EXPECTED_TOOLS[index])


def _walk_properties(schema: Any) -> set[str]:
    found: set[str] = set()
    if isinstance(schema, dict):
        properties = schema.get("properties")
        if isinstance(properties, dict):
            found.update(str(key) for key in properties)
            for sub in properties.values():
                found |= _walk_properties(sub)
        items = schema.get("items")
        if items is not None:
            found |= _walk_properties(items)
    return found


def test_no_schema_anywhere_carries_a_path_like_property() -> None:
    """The v2 no-path rule holds except for the confined v3 amendment.

    Contract v3 (docs/contracts/ingestion-contract-v3.md, adapter mapping):
    exactly the two ingestion schemas may carry a path-like property, and it
    is exactly ``path`` — every other tool stays path-free.
    """
    ingestion_tools = {"knowledge_ingest_file", "knowledge_ingest_directory"}
    for tool, schema in _tool_schemas(_MAX).items():
        names = _walk_properties(schema)
        if tool in ingestion_tools:
            assert names & {"project_path", "path", "root", "graph_root", "cwd", "file"} == {
                "path",
            }, tool
        else:
            assert names.isdisjoint({"project_path", "path", "root", "graph_root", "cwd", "file"}), tool


@given(tool=st.sampled_from(EXPECTED_TOOLS))
@settings(derandomize=True, max_examples=12, deadline=None)
def test_every_tool_schema_is_a_closed_object(tool: str) -> None:
    schema = _tool_schemas(_MAX)[tool]
    assert schema["type"] == "object"
    assert schema["additionalProperties"] is False


@given(tool=st.sampled_from(EXPECTED_TOOLS))
@settings(derandomize=True, max_examples=12, deadline=None)
def test_listed_tools_match_the_computed_schemas(tool: str) -> None:
    adapter = _adapter()
    listed = {t.name: t for t in adapter.list_tools().tools}
    assert set(listed) == set(EXPECTED_TOOLS)
    assert listed[tool].input_schema == _tool_schemas(_MAX)[tool]
    assert listed[tool].description


# ---------------------------------------------------------- strict validation


@given(tool=st.sampled_from(EXPECTED_TOOLS))
@settings(derandomize=True, max_examples=12, deadline=None)
def test_valid_arguments_validate_unchanged(tool: str) -> None:
    arguments = _valid_arguments(tool)
    validated = _validate_arguments(tool, _tool_schemas(_MAX)[tool], dict(arguments))
    assert validated == arguments


@given(
    tool=st.sampled_from(EXPECTED_TOOLS),
    extra=st.text(
        alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"), min_size=1, max_size=12
    ),
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_an_unknown_property_is_always_rejected(tool: str, extra: str) -> None:
    schema = _tool_schemas(_MAX)[tool]
    if extra in schema["properties"]:
        return
    arguments = {**_valid_arguments(tool), extra: 1}
    with pytest.raises(ToolError) as caught:
        _validate_arguments(tool, schema, arguments)
    assert caught.value.code == "INVALID_ARGUMENTS"
    assert extra in str(caught.value)


@given(tool=st.sampled_from(EXPECTED_TOOLS), drop=st.data())
@settings(derandomize=True, max_examples=25, deadline=None)
def test_a_missing_required_property_is_always_rejected(tool: str, drop: Any) -> None:
    schema = _tool_schemas(_MAX)[tool]
    required = schema.get("required", [])
    if not required:
        return
    arguments = _valid_arguments(tool)
    missing = drop.draw(st.sampled_from(required), label="dropped")
    del arguments[missing]
    with pytest.raises(ToolError) as caught:
        _validate_arguments(tool, schema, arguments)
    assert caught.value.code == "INVALID_ARGUMENTS"
    assert missing in str(caught.value)


@given(arguments=_JSON_VALUE)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_validation_is_deterministic_on_arbitrary_json(arguments: Any) -> None:
    schema = _tool_schemas(_MAX)["knowledge_query"]
    outcomes: list[str] = []
    for _ in range(2):
        try:
            validated = _validate_arguments("knowledge_query", schema, arguments)
            outcomes.append(canonical_json(validated))
        except ToolError as error:
            outcomes.append(f"{error.code}:{error}")
    assert outcomes[0] == outcomes[1]


@given(arguments=_JSON_VALUE)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_validation_never_mutates_the_callers_arguments(arguments: Any) -> None:
    before = canonical_json(arguments) if isinstance(arguments, (dict, list)) else repr(arguments)
    try:
        _validate_arguments("knowledge_query", _tool_schemas(_MAX)["knowledge_query"], arguments)
    except ToolError:
        pass
    after = canonical_json(arguments) if isinstance(arguments, (dict, list)) else repr(arguments)
    assert before == after


@given(value=_JSON_VALUE)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_a_non_object_argument_body_is_rejected_or_treated_as_empty(value: Any) -> None:
    schema = _tool_schemas(_MAX)["knowledge_get_node"]
    if value is None:
        # None is the SDK's "no arguments" marker: treated as {}.
        with pytest.raises(ToolError, match="missing required"):
            _validate_arguments("knowledge_get_node", schema, value)
    elif type(value) is not dict:
        with pytest.raises(ToolError, match="must be a JSON object"):
            _validate_arguments("knowledge_get_node", schema, value)


@given(value=st.one_of(st.text(min_size=1, max_size=8), st.booleans(), st.floats(allow_nan=False)))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_token_budget_rejects_non_integer_json_types(value: Any) -> None:
    schema = _tool_schemas(_MAX)["knowledge_query"]
    arguments = {"purpose": "why", "token_budget": value}
    # A JSON bool or a numeric string is never an integer budget.
    with pytest.raises(ToolError, match="token_budget"):
        _validate_arguments("knowledge_query", schema, arguments)


@given(budget=st.integers(min_value=-1000, max_value=3000))
@settings(derandomize=True, max_examples=30, deadline=None)
def test_token_budget_bounds_are_exactly_enforced(budget: int) -> None:
    schema = _tool_schemas(_MAX)["knowledge_query"]
    arguments = {"purpose": "why", "token_budget": budget}
    if 128 <= budget <= _MAX:
        assert _validate_arguments("knowledge_query", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="token_budget"):
            _validate_arguments("knowledge_query", schema, arguments)


@given(depth=st.integers(min_value=-10, max_value=20))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_depth_bounds_are_exactly_one_to_six(depth: int) -> None:
    schema = _tool_schemas(_MAX)["knowledge_get_subtree"]
    arguments = {"node_id": "n-1", "depth": depth}
    if 1 <= depth <= 6:
        assert _validate_arguments("knowledge_get_subtree", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="depth"):
            _validate_arguments("knowledge_get_subtree", schema, arguments)


@given(flag=st.one_of(st.text(min_size=1, max_size=8), st.integers(min_value=0, max_value=2)))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_boolean_fields_reject_string_booleans_and_numbers(flag: Any) -> None:
    schema = _tool_schemas(_MAX)["knowledge_query"]
    arguments = {"purpose": "why", "token_budget": 256, "access_only": flag}
    with pytest.raises(ToolError, match="access_only"):
        _validate_arguments("knowledge_query", schema, arguments)


@given(mode=st.text(alphabet="b dfsBDFS", min_size=1, max_size=6))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_mode_enum_is_case_sensitive_and_closed(mode: str) -> None:
    schema = _tool_schemas(_MAX)["knowledge_get_subtree"]
    arguments = {"node_id": "n-1", "mode": mode}
    if mode in ("bfs", "dfs"):
        assert _validate_arguments("knowledge_get_subtree", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="mode"):
            _validate_arguments("knowledge_get_subtree", schema, arguments)


@given(node_id=st.text(max_size=300))
@settings(derandomize=True, max_examples=25, deadline=None)
def test_node_id_length_limits_are_exactly_enforced(node_id: str) -> None:
    schema = _tool_schemas(_MAX)["knowledge_get_node"]
    if 1 <= len(node_id) <= 256:
        assert _validate_arguments("knowledge_get_node", schema, {"node_id": node_id}) == {"node_id": node_id}
    else:
        with pytest.raises(ToolError, match="node_id"):
            _validate_arguments("knowledge_get_node", schema, {"node_id": node_id})


@given(budget=st.integers(min_value=1, max_value=600))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_the_budget_schema_obeys_the_configured_maximum(budget: int) -> None:
    schema = _tool_schemas(256)["knowledge_query"]
    arguments = {"purpose": "why", "token_budget": budget}
    if 128 <= budget <= 256:
        assert _validate_arguments("knowledge_query", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError):
            _validate_arguments("knowledge_query", schema, arguments)


# ----------------------------------------------------------- error envelope


@given(
    code=st.text(
        alphabet=st.characters(whitelist_categories=("Lu",), whitelist_characters="_"), min_size=1, max_size=24
    ),
    message=st.text(max_size=1200),
)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_error_envelope_is_canonical_sanitized_and_capped(code: str, message: str) -> None:
    text = _error_text(code, message, None)
    payload = json.loads(text)
    assert text == canonical_json(payload)  # canonical key order, compact separators
    error = payload["error"]
    assert error["code"] == code
    assert "audit_event_id" not in error
    assert len(error["message"]) <= 512
    for char in error["message"]:
        if char in "\t\n\r":
            continue
        assert unicodedata.category(char) not in {"Cc", "Cf"}


@given(message=st.text(max_size=600))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_error_envelope_carries_an_audit_id_exactly_when_one_exists(message: str) -> None:
    with_id = json.loads(_error_text("X", message, "audit-1"))
    assert with_id["error"]["audit_event_id"] == "audit-1"
    without = json.loads(_error_text("X", message, None))
    assert "audit_event_id" not in without["error"]


@given(code=st.text(alphabet="XYZ_", min_size=1, max_size=12), message=st.text(max_size=300))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_error_envelope_is_deterministic(code: str, message: str) -> None:
    assert _error_text(code, message, "a-1") == _error_text(code, message, "a-1")


# ----------------------------------------------------------- nodes resource


def _nodes(node_count: int, label_size: int) -> list[dict[str, Any]]:
    return [
        {
            "id": f"n-{index:04d}",
            "label": f"label {index} " + "x" * label_size,
            "domain": "P1",
            "kind": "leaf",
        }
        for index in range(node_count)
    ]


@given(
    node_count=st.integers(min_value=0, max_value=40),
    label_size=st.integers(min_value=0, max_value=200),
    cap=st.integers(min_value=200, max_value=4096),
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_nodes_payload_never_exceeds_the_resource_cap(node_count: int, label_size: int, cap: int) -> None:
    adapter = _adapter(resource_max_bytes=cap)
    manifest = {"graph_content_hash": "0" * 64, "projection_version": "gen-x"}
    payload = adapter._nodes_payload(manifest, _nodes(node_count, label_size))  # noqa: SLF001
    assert len(canonical_json(payload).encode("utf-8")) <= cap
    assert payload["total"] == node_count
    assert payload["returned"] <= payload["total"]
    assert payload["truncated"] is (payload["returned"] < payload["total"])
    ids = [node["node_id"] for node in payload["nodes"]]
    assert ids == sorted(ids)


@given(node_count=st.integers(min_value=1, max_value=30), cap=st.integers(min_value=200, max_value=4096))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_nodes_payload_truncates_from_the_tail(node_count: int, cap: int) -> None:
    adapter = _adapter(resource_max_bytes=cap)
    manifest = {"graph_content_hash": "0" * 64, "projection_version": "gen-x"}
    payload = adapter._nodes_payload(manifest, _nodes(node_count, 100))  # noqa: SLF001
    ids = [node["node_id"] for node in payload["nodes"]]
    assert ids == [f"n-{index:04d}" for index in range(len(ids))]


@given(node_count=st.integers(min_value=0, max_value=20))
@settings(derandomize=True, max_examples=10, deadline=None)
def test_nodes_payload_is_deterministic(node_count: int) -> None:
    adapter = _adapter()
    manifest = {"graph_content_hash": "0" * 64, "projection_version": "gen-x"}
    first = adapter._nodes_payload(manifest, _nodes(node_count, 10))  # noqa: SLF001
    second = adapter._nodes_payload(manifest, _nodes(node_count, 10))  # noqa: SLF001
    assert first == second


@given(uri=st.text(alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters=":/.-"), max_size=40))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_an_unknown_resource_uri_fails_closed(uri: str) -> None:
    if uri in _RESOURCE_URIS:
        return
    adapter = _adapter()
    with pytest.raises(MCPError):
        anyio.run(adapter.read_resource, uri)


def test_resource_uris_are_exactly_the_four_sanitized_projections() -> None:
    assert len(_RESOURCE_URIS) == 4
    assert all(uri.startswith("soul://") for uri in _RESOURCE_URIS)
    assert not any(".." in uri or "\\" in uri for uri in _RESOURCE_URIS)
    adapter = _adapter()
    listed = adapter.list_resources()
    assert {str(resource.uri) for resource in listed.resources} == set(_RESOURCE_URIS)
    assert all(resource.mime_type == "application/json" for resource in listed.resources)


# ------------------------------------------------- audit parity on rejection


def test_an_unknown_tool_produces_exactly_one_final_audit_event() -> None:
    adapter = _adapter()
    store = adapter._service._store  # noqa: SLF001
    before = store.audit_event_count()
    result = anyio.run(adapter.call_tool, "knowledge_nope", {})
    assert result.is_error
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert payload["error"]["code"] == "UNKNOWN_TOOL"
    assert payload["error"]["audit_event_id"]
    assert store.audit_event_count() == before + 1


def test_invalid_arguments_produce_exactly_one_final_audit_event() -> None:
    adapter = _adapter()
    store = adapter._service._store  # noqa: SLF001
    before = store.audit_event_count()
    result = anyio.run(adapter.call_tool, "knowledge_query", {"purpose": "why", "project_path": "/etc"})
    assert result.is_error
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"
    assert payload["error"]["audit_event_id"]
    assert store.audit_event_count() == before + 1


def test_an_internal_error_never_leaks_raw_exception_text() -> None:
    adapter = _adapter()
    marker = "sensitive-internal-detail-9f3a"

    def _explode(*args: Any, **kwargs: Any) -> Any:
        raise RuntimeError(marker)

    adapter._service.get_node = _explode  # type: ignore[method-assign]  # noqa: SLF001
    result = anyio.run(adapter.call_tool, "knowledge_get_node", {"node_id": "n-1"})
    assert result.is_error
    text = result.content[0].text  # type: ignore[union-attr]
    payload = json.loads(text)
    assert payload["error"]["code"] == "INTERNAL_ERROR"
    assert marker not in text


# ------------------------------------------------------- deeper schema invariants


def test_required_sets_are_the_pinned_snapshot() -> None:
    """Pinned required sets (contract v2: +4 G1 verbs — operator ruling 2026-10-26)."""
    required = {tool: tuple(schema.get("required", ())) for tool, schema in _tool_schemas(_MAX).items()}
    assert required == {
        "knowledge_query": ("purpose", "token_budget"),
        "knowledge_get_node": ("node_id",),
        "knowledge_get_subtree": ("node_id",),
        "knowledge_log_event": ("node",),
        "knowledge_record_outcome": ("evidence",),
        "knowledge_evaluate": ("node_id",),
        "knowledge_review": ("node_id",),
        "knowledge_propose_improvement": ("node_id", "diff", "rationale"),
        "knowledge_apply_improvement": ("proposal_id",),
        "knowledge_rollback": (),
        "knowledge_path": ("from_id", "to_id"),
        "knowledge_explain": ("node_id",),
        "knowledge_affected": ("node_id",),
        "knowledge_god_nodes": (),
        # Contract v3 ingestion verbs (audit F2).
        "knowledge_ingest_file": ("path",),
        "knowledge_ingest_directory": ("path",),
    }


def test_the_rollback_tool_accepts_an_empty_argument_body() -> None:
    schema = _tool_schemas(_MAX)["knowledge_rollback"]
    assert _validate_arguments("knowledge_rollback", schema, {}) == {}
    assert _validate_arguments("knowledge_rollback", schema, None) == {}


def test_the_rollback_tool_without_a_target_fails_at_dispatch() -> None:
    adapter = _adapter()
    result = anyio.run(adapter.call_tool, "knowledge_rollback", {})
    assert result.is_error
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert payload["error"]["code"] == "INVALID_ARGUMENTS"


@given(
    op=st.text(alphabet=st.characters(whitelist_categories=("Ll",), whitelist_characters="_"), min_size=1, max_size=20)
)
@settings(derandomize=True, max_examples=25, deadline=None)
def test_the_diff_op_enum_is_closed(op: str) -> None:
    from know_your_soul.mcp_server import _DIFF_OPS

    schema = _tool_schemas(_MAX)["knowledge_propose_improvement"]
    arguments = {"node_id": "n-1", "diff": [{"op": op}], "rationale": "r"}
    if op in _DIFF_OPS:
        assert _validate_arguments("knowledge_propose_improvement", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="op"):
            _validate_arguments("knowledge_propose_improvement", schema, arguments)


@given(op=st.sampled_from(["set_summary", "add_plus", "remove_minus"]))
@settings(derandomize=True, max_examples=6, deadline=None)
def test_a_diff_item_with_an_unknown_key_is_rejected(op: str) -> None:
    schema = _tool_schemas(_MAX)["knowledge_propose_improvement"]
    arguments = {"node_id": "n-1", "diff": [{"op": op, "surprise": 1}], "rationale": "r"}
    with pytest.raises(ToolError) as caught:
        _validate_arguments("knowledge_propose_improvement", schema, arguments)
    assert caught.value.code == "INVALID_ARGUMENTS"


@given(version=st.integers(min_value=-10, max_value=10))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_expected_version_has_a_minimum_of_one(version: int) -> None:
    schema = _tool_schemas(_MAX)["knowledge_log_event"]
    arguments = {"node": {"node_id": "n-1"}, "expected_version": version}
    if version >= 1:
        assert _validate_arguments("knowledge_log_event", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="expected_version"):
            _validate_arguments("knowledge_log_event", schema, arguments)


@given(tag=st.text(max_size=300))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_context_tag_items_are_capped_at_256_chars(tag: str) -> None:
    schema = _tool_schemas(_MAX)["knowledge_query"]
    arguments = {"purpose": "why", "token_budget": 256, "context_tags": [tag]}
    if len(tag) <= 256:
        assert _validate_arguments("knowledge_query", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="context_tags"):
            _validate_arguments("knowledge_query", schema, arguments)


@given(key=st.text(max_size=300))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_idempotency_key_is_nullable_but_never_empty_or_overlong(key: str) -> None:
    schema = _tool_schemas(_MAX)["knowledge_review"]
    arguments: dict[str, Any] = {"node_id": "n-1", "idempotency_key": key}
    if 1 <= len(key) <= 256:
        assert _validate_arguments("knowledge_review", schema, arguments) == arguments
    else:
        with pytest.raises(ToolError, match="idempotency_key"):
            _validate_arguments("knowledge_review", schema, arguments)


def test_an_empty_diff_list_is_rejected() -> None:
    schema = _tool_schemas(_MAX)["knowledge_propose_improvement"]
    arguments = {"node_id": "n-1", "diff": [], "rationale": "r"}
    with pytest.raises(ToolError, match="diff"):
        _validate_arguments("knowledge_propose_improvement", schema, arguments)


@given(tool=st.sampled_from(EXPECTED_TOOLS))
@settings(derandomize=True, max_examples=12, deadline=None)
def test_validate_arguments_returns_an_independent_copy(tool: str) -> None:
    arguments = _valid_arguments(tool)
    validated = _validate_arguments(tool, _tool_schemas(_MAX)[tool], arguments)
    validated["injected"] = True
    assert "injected" not in arguments


# --------------------------------------------------------------- dispatch path


def test_get_node_of_an_unknown_node_is_a_typed_error() -> None:
    adapter = _adapter()
    result = anyio.run(adapter.call_tool, "knowledge_get_node", {"node_id": "n-ghost"})
    assert result.is_error
    payload = json.loads(result.content[0].text)  # type: ignore[union-attr]
    assert payload["error"]["code"] == "NODE_NOT_FOUND"
    assert payload["error"]["audit_event_id"]


def test_error_results_are_a_single_text_content() -> None:
    adapter = _adapter()
    result = anyio.run(adapter.call_tool, "knowledge_query", {"purpose": "x", "token_budget": 1})
    assert result.is_error
    assert len(result.content) == 1
    assert result.content[0].type == "text"  # type: ignore[union-attr]


def test_tool_error_carries_its_code_and_optional_audit_id() -> None:
    error = ToolError("SOME_CODE", "a message", audit_event_id="a-1")
    assert error.code == "SOME_CODE"
    assert error.audit_event_id == "a-1"
    assert str(error) == "a message"


def test_reading_a_projection_resource_without_a_projection_fails_closed() -> None:
    adapter = _adapter()
    with pytest.raises(MCPError):
        anyio.run(adapter.read_resource, "soul://projection/manifest")


def test_relearn_status_resource_is_served_without_a_pending_gate() -> None:
    adapter = _adapter()
    result = anyio.run(adapter.read_resource, "soul://relearn/status")
    content = result.contents[0]
    assert isinstance(content, types.TextResourceContents)
    assert content.mime_type == "application/json"
    text = content.text
    payload = json.loads(text)
    assert text == canonical_json(payload)
    assert payload["relearn_state"] is None
    assert payload["pending_offer"] is None
    assert "previews" not in payload

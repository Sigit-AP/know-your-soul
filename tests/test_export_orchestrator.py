"""Contract tests for the deterministic export orchestrator (ATM F-B8).

The registry maps artifact names to format specs (producer + round-trip
validator + explicit byte cap). Batch production is fail-closed: an oversized
or structurally defective artifact raises a typed error before anything is
published, and every produced byte sequence is deterministic for a given read
model — two runs over differently-ordered input mappings must be byte
identical.
"""

from __future__ import annotations

import json
from typing import Any

import pytest

from know_your_soul.exporters.orchestrator import (
    ExportCapExceededError,
    ExportError,
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
    UnknownExportFormatError,
    default_registry,
    manifest_entries,
    produce_exports,
)


def _read_model() -> dict[str, Any]:
    """A minimal node-link read model in the graph.json shape."""
    return {
        "directed": True,
        "multigraph": False,
        "graph": {"schema_version": 1, "generator": "know-your-soul", "partitioner": "test"},
        "nodes": [
            {"id": "b-node", "domain": "P2", "kind": "CHILD", "version": 1, "confidence": "0.900000000000"},
            {"id": "a-node", "domain": "P1", "kind": "PRIMARY", "version": 2, "confidence": "0.500000000000"},
        ],
        "links": [
            {"source": "a-node", "target": "b-node", "relation": "parent_of"},
        ],
        "hyperedges": [],
    }


def _json_producer(payload: dict[str, Any]) -> Any:
    def _produce(read_model: Any) -> dict[str, Any]:
        return payload

    return _produce


def _json_roundtrip(data: bytes) -> None:
    parsed = json.loads(data.decode("utf-8"))
    if not isinstance(parsed, dict):
        raise ExportRoundTripError("top-level must be an object")


# ------------------------------------------------------------------ registry


def test_registry_rejects_duplicate_names() -> None:
    registry = ExportRegistry()
    registry.register_json("a.json", _json_producer({"x": 1}))
    with pytest.raises(ExportError, match="duplicate"):
        registry.register_json("a.json", _json_producer({"x": 2}))


@pytest.mark.parametrize(
    "bad_name",
    ["", "/abs/path.json", "../escape.json", "a/../b.json", "white space.json", "evil\x00.json", "-lead.json"],
)
def test_registry_rejects_unsafe_artifact_names(bad_name: str) -> None:
    registry = ExportRegistry()
    with pytest.raises(ExportError, match="artifact name"):
        registry.register_json(bad_name, _json_producer({}))


@pytest.mark.parametrize("good_name", ["graph.dot", "analytics.json", "exports/node-index.json", "book/P1.md"])
def test_registry_accepts_safe_artifact_names(good_name: str) -> None:
    registry = ExportRegistry()
    registry.register_json(good_name, _json_producer({}))
    assert registry.names() == (good_name,)


def test_registry_rejects_bad_kind_and_caps() -> None:
    registry = ExportRegistry()
    with pytest.raises(ExportError, match="kind"):
        registry.register_json("a.json", _json_producer({}), kind="mystery")
    with pytest.raises(ExportError, match="max_bytes"):
        registry.register_json("b.json", _json_producer({}), max_bytes=0)


def test_register_json_roundtrip_is_byte_exact() -> None:
    registry = ExportRegistry()
    registry.register_json("a.json", _json_producer({"z": [1, 2], "a": "b"}))
    artifacts = produce_exports(registry, _read_model())
    data = artifacts["a.json"]
    parsed = json.loads(data.decode("utf-8"))
    assert parsed == {"z": [1, 2], "a": "b"}
    assert data.endswith(b"\n")


def test_unknown_format_raises_typed_error() -> None:
    registry = ExportRegistry()
    registry.register_json("a.json", _json_producer({}))
    with pytest.raises(UnknownExportFormatError, match="missing.json"):
        produce_exports(registry, _read_model(), select=("missing.json",))


def test_select_limits_production_to_named_formats() -> None:
    registry = ExportRegistry()
    registry.register_json("a.json", _json_producer({"a": 1}), kind="format")
    registry.register_json("b.json", _json_producer({"b": 1}), kind="analytics")
    artifacts = produce_exports(registry, _read_model(), select=("b.json",))
    assert list(artifacts) == ["b.json"]


def test_names_filter_by_kind() -> None:
    registry = ExportRegistry()
    registry.register_json("a.json", _json_producer({}), kind="format")
    registry.register_json("b.json", _json_producer({}), kind="analytics")
    assert registry.names() == ("a.json", "b.json")
    assert registry.names(kind="format") == ("a.json",)
    assert registry.names(kind="analytics") == ("b.json",)
    with pytest.raises(ExportError, match="kind"):
        registry.names(kind="mystery")


# ------------------------------------------------------------------ failures


def test_oversized_artifact_fails_closed_typed() -> None:
    registry = ExportRegistry()
    registry.register_json("big.json", _json_producer({"pad": "x" * 100}), max_bytes=16)
    with pytest.raises(ExportCapExceededError, match="big.json"):
        produce_exports(registry, _read_model())


def test_defective_roundtrip_fails_closed_typed() -> None:
    def bad_producer(read_model: Any) -> bytes:
        return b"this is not json at all"

    registry = ExportRegistry()
    registry.register(
        ExportFormat(
            name="bad.json",
            version=1,
            kind="format",
            max_bytes=1024,
            producer=bad_producer,
            roundtrip=_json_roundtrip,
        )
    )
    with pytest.raises(ExportRoundTripError, match="bad.json"):
        produce_exports(registry, _read_model())


def test_non_bytes_producer_output_fails_closed_typed() -> None:
    def bad_producer(read_model: Any) -> Any:
        return {"not": "bytes"}

    registry = ExportRegistry()
    registry.register(
        ExportFormat(
            name="bad.bin",
            version=1,
            kind="format",
            max_bytes=1024,
            producer=bad_producer,
            roundtrip=_json_roundtrip,
        )
    )
    with pytest.raises(ExportError, match="bytes"):
        produce_exports(registry, _read_model())


# --------------------------------------------------------------- determinism


def test_production_is_byte_identical_across_input_orderings() -> None:
    registry = default_registry()
    forward = produce_exports(registry, _read_model())
    reversed_model = _read_model()
    reversed_model["nodes"] = list(reversed(reversed_model["nodes"]))
    backward = produce_exports(registry, reversed_model)
    assert forward == backward
    again = produce_exports(default_registry(), _read_model())
    assert forward == again


def test_fingerprint_is_stable_and_version_sensitive() -> None:
    first = default_registry().fingerprint()
    assert first == default_registry().fingerprint()
    registry = ExportRegistry()
    registry.register_json("a.json", _json_producer({}), version=1)
    other = ExportRegistry()
    other.register_json("a.json", _json_producer({}), version=2)
    assert registry.fingerprint() != other.fingerprint()


def test_manifest_entries_carry_name_hash_size_version() -> None:
    import hashlib

    registry = default_registry()
    artifacts = produce_exports(registry, _read_model())
    entries = manifest_entries(registry, artifacts)
    assert set(entries) == set(artifacts)
    for name, entry in entries.items():
        assert entry["sha256"] == hashlib.sha256(artifacts[name]).hexdigest()
        assert entry["size"] == len(artifacts[name])
        assert entry["version"] == registry.get(name).version
        assert entry["kind"] == registry.get(name).kind


# ------------------------------------------------------------ default formats


def test_default_registry_has_format_and_analytics_entries() -> None:
    registry = default_registry()
    assert registry.names(kind="format"), "expected at least one built-in format"
    assert registry.names(kind="analytics"), "expected at least one built-in analytics module"


def test_default_registry_produces_real_artifacts_from_read_model() -> None:
    registry = default_registry()
    artifacts = produce_exports(registry, _read_model())
    index = json.loads(artifacts["exports/node-index.json"].decode("utf-8"))
    assert [node["id"] for node in index["nodes"]] == ["a-node", "b-node"]
    rollup = json.loads(artifacts["analytics/domain-rollup.json"].decode("utf-8"))
    assert rollup["domains"]["P1"]["nodes"] == 1
    assert rollup["domains"]["P2"]["nodes"] == 1
    # The only link crosses domains, so no domain has internal edges.
    assert rollup["domains"]["P1"]["internal_edges"] == 0
    assert rollup["domains"]["P2"]["internal_edges"] == 0


def test_schema_view_exposes_caps_and_roundtrips() -> None:
    registry = default_registry()
    schemas = registry.schema_view()
    assert set(schemas) == set(registry.names())
    for name, schema in schemas.items():
        assert schema.max_bytes == registry.get(name).max_bytes
        schema.roundtrip(produce_exports(registry, _read_model(), select=(name,))[name])

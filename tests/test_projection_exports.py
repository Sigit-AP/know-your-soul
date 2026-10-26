"""Integration tests: export artifacts ride the generation cycle (ATM F-B8).

A projector configured with an export registry writes the export artifacts
into the content-addressed generation directory, binds them into the manifest
(name, sha256, size, version, kind) and the content identity, enforces the
publish caps, and passes the registry-driven read-model validator. Without a
registry the generation is byte-identical to the pre-export behaviour.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    canonical_json,
)
from know_your_soul.exporters.orchestrator import (
    ExportFormat,
    ExportRegistry,
    ExportRoundTripError,
    default_registry,
)
from know_your_soul.projection import (
    ARTIFACT_NAMES,
    GraphProjector,
    ProjectionValidationError,
)
from know_your_soul.read_model_validator import ReadModelValidator
from know_your_soul.store import EventStore


def _seeded_store(tmp_path: Path) -> EventStore:
    store = EventStore(tmp_path / "soul.sqlite3")
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="KROOT",
            domain=PrimaryDomain.P1,
            node=Node(
                node_id="KROOT",
                kind=NodeKind.ROOT,
                domain=PrimaryDomain.P1,
                summary="Knowledge Root",
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
            source_version_id="sv-test",
        ),
        idempotency_key="test:upsert:KROOT",
    )
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="P1",
            domain=PrimaryDomain.P1,
            node=Node(
                node_id="P1",
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=PrimaryDomain.P1,
                summary="Discipline",
                provenance=("soul://roots",),
                source_version_id="sv-test",
            ),
            source_version_id="sv-test",
        ),
        idempotency_key="test:upsert:P1",
    )
    return store


def _active_generation(out_path: Path) -> Path:
    return out_path / "generations" / (out_path / "CURRENT").read_text(encoding="utf-8").strip()


def test_generation_includes_export_artifacts_in_manifest(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out_path = tmp_path / "out"
    registry = default_registry()
    projector = GraphProjector(store, output_root=out_path, export_registry=registry)
    receipt = projector.rebuild()
    generation = _active_generation(out_path)

    for name in registry.names():
        path = generation / name
        assert path.is_file(), f"missing export artifact {name}"
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert set(manifest["artifacts"]) == set(ARTIFACT_NAMES) - {"manifest.json"}
    export_entries = manifest["export_artifacts"]
    assert set(export_entries) == set(registry.names())
    for name, entry in export_entries.items():
        data = (generation / name).read_bytes()
        assert entry["sha256"] == hashlib.sha256(data).hexdigest()
        assert entry["size"] == len(data)
        assert entry["version"] == registry.get(name).version
        assert entry["kind"] == registry.get(name).kind
    assert manifest["export_registry_fingerprint"] == registry.fingerprint()
    assert receipt.projection_version == manifest["projection_version"]
    store.close()


def test_export_artifacts_bind_into_content_identity(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    plain = GraphProjector(store, output_root=tmp_path / "plain").rebuild()
    exported = GraphProjector(store, output_root=tmp_path / "out", export_registry=default_registry()).rebuild()
    assert plain.content_hash != exported.content_hash
    store.close()


def test_rebuild_with_registry_is_byte_identical(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out_path = tmp_path / "out"
    projector = GraphProjector(store, output_root=out_path, export_registry=default_registry())
    first = projector.rebuild()
    generation = _active_generation(out_path)
    snapshot = {
        str(path.relative_to(generation)): path.read_bytes() for path in sorted(generation.rglob("*")) if path.is_file()
    }
    second = projector.rebuild()
    assert first.content_hash == second.content_hash
    assert first.generation_dir == second.generation_dir
    for rel, data in snapshot.items():
        assert (generation / rel).read_bytes() == data
    store.close()


def test_without_registry_generation_is_unchanged(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out_path = tmp_path / "out"
    GraphProjector(store, output_root=out_path).rebuild()
    generation = _active_generation(out_path)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert "export_artifacts" not in manifest
    assert "export_registry_fingerprint" not in manifest
    assert sorted(path.name for path in generation.iterdir()) == sorted(ARTIFACT_NAMES)
    store.close()


def test_oversized_export_fails_closed_before_activation(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    registry = ExportRegistry()
    registry.register_json(
        "exports/huge.json",
        lambda read_model: {"pad": "x" * 4096},
        kind="format",
        max_bytes=64,
    )
    projector = GraphProjector(store, output_root=tmp_path / "out", export_registry=registry)
    with pytest.raises(ProjectionValidationError, match="huge.json"):
        projector.rebuild()
    assert not (tmp_path / "out" / "CURRENT").exists()
    store.close()


def test_defective_export_roundtrip_fails_closed(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)

    def bad_producer(read_model: Any) -> bytes:
        return b"not json"

    def strict_roundtrip(data: bytes) -> None:
        json.loads(data.decode("utf-8"))

    registry = ExportRegistry(
        (
            ExportFormat(
                name="exports/bad.json",
                version=1,
                kind="format",
                max_bytes=1024,
                producer=bad_producer,
                roundtrip=strict_roundtrip,
            ),
        )
    )
    projector = GraphProjector(store, output_root=tmp_path / "out", export_registry=registry)
    with pytest.raises(ProjectionValidationError, match="bad.json"):
        projector.rebuild()
    assert not (tmp_path / "out" / "CURRENT").exists()
    store.close()


def test_projector_requires_validator_schema_coverage(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    with pytest.raises(ValueError, match="export schema"):
        GraphProjector(
            store,
            output_root=tmp_path / "out",
            export_registry=default_registry(),
            validator=ReadModelValidator(),
        )
    store.close()


def test_validator_fails_closed_on_unknown_export_schema(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out_path = tmp_path / "out"
    GraphProjector(store, output_root=out_path, export_registry=default_registry()).rebuild()
    generation = _active_generation(out_path)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))

    # A manifest naming an artifact the validator's registry does not know
    # must fail closed — unknown schemas are never trusted.
    tampered = dict(manifest)
    tampered["export_artifacts"] = {
        "exports/mystery.bin": {"sha256": "0" * 64, "size": 1, "version": 1, "kind": "format"}
    }
    (generation / "exports" / "mystery.bin").write_bytes(b"x")
    (generation / "manifest.json").write_bytes((canonical_json(tampered) + "\n").encode("utf-8"))
    report = ReadModelValidator(export_schemas=default_registry().schema_view()).validate(generation, tampered)
    assert not report.ok
    assert any("unknown export artifact schema" in error for error in report.errors)
    store.close()


def test_validator_detects_tampered_export_artifact(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out_path = tmp_path / "out"
    registry = default_registry()
    GraphProjector(store, output_root=out_path, export_registry=registry).rebuild()
    generation = _active_generation(out_path)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))

    victim = registry.names()[0]
    (generation / victim).write_bytes(b"tampered")
    report = ReadModelValidator(export_schemas=registry.schema_view()).validate(generation, manifest)
    assert not report.ok
    assert any(victim in error and ("hash" in error or "size" in error) for error in report.errors)
    store.close()


def test_validator_scans_export_artifacts_for_secrets(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    registry = ExportRegistry()
    registry.register_json(
        "exports/leak.json",
        lambda read_model: {"note": "AKIAIOSFODNN7EXAMPLE"},
        kind="format",
    )
    projector = GraphProjector(store, output_root=tmp_path / "out", export_registry=registry)
    with pytest.raises(ProjectionValidationError, match="secret"):
        projector.rebuild()
    assert not (tmp_path / "out" / "CURRENT").exists()
    store.close()


def test_validator_roundtrip_gate_catches_structural_defect(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    out_path = tmp_path / "out"
    registry = default_registry()
    GraphProjector(store, output_root=out_path, export_registry=registry).rebuild()
    generation = _active_generation(out_path)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))

    # Rewrite an export artifact with valid JSON that breaks the canonical
    # byte-level round trip, and re-point the manifest hash so only the
    # round-trip gate can catch it.
    victim = registry.names()[0]
    defective = json.dumps({"reordered": True, "z": [3, 1, 2]}, indent=2).encode("utf-8")
    (generation / victim).write_bytes(defective)
    manifest["export_artifacts"][victim]["sha256"] = hashlib.sha256(defective).hexdigest()
    manifest["export_artifacts"][victim]["size"] = len(defective)
    (generation / "manifest.json").write_bytes((canonical_json(manifest) + "\n").encode("utf-8"))
    report = ReadModelValidator(export_schemas=registry.schema_view()).validate(generation, manifest)
    assert not report.ok
    assert any(victim in error and "round-trip" in error for error in report.errors)
    store.close()


def test_export_artifact_names_cannot_escape_the_generation(tmp_path: Path) -> None:
    store = _seeded_store(tmp_path)
    registry = ExportRegistry()
    with pytest.raises(Exception, match="artifact name"):
        registry.register_json("../outside.json", lambda read_model: {})
    store.close()


def test_export_roundtrip_error_is_exported() -> None:
    assert issubclass(ExportRoundTripError, Exception)

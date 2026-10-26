"""Contract tests for the deterministic seed importer (Task 3).

Structured imports require an explicit domain mapping; every imported node
carries the hash-identified source version, file-scoped stable ids and a
parent/relation structure the store can integrity-check. Duplicate imports
replay idempotently; directory imports are manifest-anchored and incremental
(matrix row 10).
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path

import pytest

from soul_knowledge.domain import NodeKind, PrimaryDomain, RelationKind
from soul_knowledge.importer import ImportManifestError, SeedImporter, injective_file_id, injective_section_key
from soul_knowledge.source_registry import SourceKind, SourceRegistry
from soul_knowledge.store import EventStore

ALPHA = (
    b"# Alpha Lessons\n"
    b"\n"
    b"## First Lesson\n"
    b"\n"
    b"Always roll the database back before retrying.\n"
    b"\n"
    b"## Second Lesson\n"
    b"\n"
    b"See [[beta]] and [[ghost]] for the counterexample.\n"
)
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"


def _store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "soul.sqlite3")


def _importer(store: EventStore, tmp_path: Path) -> SeedImporter:
    return SeedImporter(store, state_dir=tmp_path / "import-state")


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_import_requires_an_explicit_domain_mapping(tmp_path: Path) -> None:
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    source = _write(tmp_path / "alpha.md", ALPHA)
    with pytest.raises(TypeError):
        importer.import_file(source, source_uri="soul://corpus/alpha.md")  # type: ignore[call-arg]
    with pytest.raises(TypeError):
        importer.import_file(source, domain="P1", source_uri="soul://corpus/alpha.md")  # type: ignore[arg-type]
    receipt = importer.import_file(source, domain=PrimaryDomain.P3, source_uri="soul://corpus/alpha.md")
    assert receipt.domain is PrimaryDomain.P3
    for node_id in receipt.node_ids:
        node = store.current_node(node_id)
        assert node is not None
        assert node.domain is PrimaryDomain.P3


def test_seven_primary_roots_exist_after_import(tmp_path: Path) -> None:
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    importer.import_file(
        _write(tmp_path / "alpha.md", ALPHA), domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md"
    )
    root = store.current_node("KROOT")
    assert root is not None and root.kind is NodeKind.ROOT
    for domain in PrimaryDomain:
        node = store.current_node(domain.value)
        assert node is not None, domain
        assert node.kind is NodeKind.PRIMARY
        assert node.parent_id == "KROOT"
        assert node.summary == domain.title
        relations = store.relations_from("KROOT")
        assert any(r.kind is RelationKind.PARENT_OF and r.target_node_id == domain.value for r in relations)


def test_provenance_and_source_version_are_preserved(tmp_path: Path) -> None:
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    source = _write(tmp_path / "alpha.md", ALPHA)
    receipt = importer.import_file(source, domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md")
    active = SourceRegistry(store).active(SourceKind.OPERATOR_IMPORT)
    assert active is not None
    assert receipt.source_version_id == active.source_version_id
    assert receipt.content_sha256 == hashlib.sha256(ALPHA).hexdigest()
    node = store.current_node(receipt.node_ids[0])
    assert node is not None
    assert node.source_version_id == receipt.source_version_id
    assert "soul://corpus/alpha.md" in node.provenance


def test_duplicate_import_is_idempotent(tmp_path: Path) -> None:
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    source = _write(tmp_path / "alpha.md", ALPHA)
    first = importer.import_file(source, domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md")
    events = store.event_count()
    watermark = store.knowledge_high_watermark()
    second = importer.import_file(source, domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md")
    assert second.idempotent_replay is True
    assert second.events_appended == 0
    assert second.source_version_id == first.source_version_id
    assert second.node_ids == first.node_ids
    assert store.event_count() == events
    assert store.knowledge_high_watermark() == watermark
    assert store.node_version(first.node_ids[0]) == 1


def test_stable_file_scoped_ids_follow_the_relative_name(tmp_path: Path) -> None:
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    one = _write(tmp_path / "one" / "alpha.md", ALPHA)
    two = _write(tmp_path / "two" / "alpha.md", ALPHA)
    first = importer.import_file(one, domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md")
    second = importer.import_file(two, domain=PrimaryDomain.P1, source_uri="soul://mirror/alpha.md")
    assert first.node_ids == second.node_ids
    assert second.idempotent_replay is True


def test_parent_relation_integrity_and_unresolved_links(tmp_path: Path) -> None:
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    alpha = _write(tmp_path / "alpha.md", ALPHA)
    receipt = importer.import_file(alpha, domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md")
    # [[beta]] and [[ghost]] cannot resolve yet; both are reported, neither stored.
    assert receipt.unresolved_links == ("beta", "ghost")
    file_id = injective_file_id("alpha.md")
    root_relations = store.relations_from(PrimaryDomain.P1.value)
    assert any(r.kind is RelationKind.PARENT_OF and r.target_node_id == file_id for r in root_relations)
    file_relations = store.relations_from(file_id)
    sections = {r.target_node_id for r in file_relations if r.kind is RelationKind.PARENT_OF}
    assert f"{file_id}.{injective_section_key('First Lesson')}" in sections
    assert f"{file_id}.{injective_section_key('Second Lesson')}" in sections
    assert not any(r.kind is RelationKind.DEPENDS_ON for r in file_relations)
    # Once the target file exists, a re-import resolves the link deterministically.
    _write(tmp_path / "beta.md", BETA)
    importer.import_file(tmp_path / "beta.md", domain=PrimaryDomain.P1, source_uri="soul://corpus/beta.md")
    again = importer.import_file(alpha, domain=PrimaryDomain.P1, source_uri="soul://corpus/alpha.md")
    assert again.unresolved_links == ("ghost",)
    resolved = store.relations_from(file_id)
    assert any(r.kind is RelationKind.DEPENDS_ON and r.target_node_id == injective_file_id("beta.md") for r in resolved)


def test_manifest_anchored_incremental_import(tmp_path: Path) -> None:
    """Row 10: the import manifest is anchored to the corpus root and content-hashed."""
    store = _store(tmp_path)
    importer = _importer(store, tmp_path)
    corpus = tmp_path / "corpus"
    alpha = _write(corpus / "alpha.md", ALPHA)
    _write(corpus / "beta.md", BETA)
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P2)
    assert len(receipts) == 2
    watermark = store.knowledge_high_watermark()
    assert importer.import_directory(corpus, domain=PrimaryDomain.P2) == []
    assert store.knowledge_high_watermark() == watermark
    # A content change re-imports exactly the changed file.
    alpha.write_bytes(ALPHA + b"\n## Extra\n\nMore.\n")
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P2)
    assert len(receipts) == 1
    assert receipts[0].content_sha256 == hashlib.sha256(ALPHA + b"\n## Extra\n\nMore.\n").hexdigest()
    assert store.knowledge_high_watermark() > watermark
    # An mtime-only touch is not a change (content hash, never stat).
    os.utime(alpha, None)
    assert importer.import_directory(corpus, domain=PrimaryDomain.P2) == []
    # The manifest is anchored: a manifest written for another root fails closed.
    manifest_path = tmp_path / "import-state" / "import_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["root"] == str(corpus.resolve())
    manifest["root"] = str(tmp_path.resolve())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImportManifestError):
        importer.import_directory(corpus, domain=PrimaryDomain.P2)

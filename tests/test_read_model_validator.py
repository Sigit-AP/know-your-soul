"""Contract tests for the product read-model validator (Task 3).

The validator fails closed on anything Graphify's lenient readers accept:
unknown schema versions, unknown top-level keys, bad endpoints, invalid file
types or confidences, junk nodes, absolute checkout paths, secret-shaped
strings and artifact hash mismatches (matrix rows 15, 21, 22, 33, 34, 39).
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

import pytest

from soul_knowledge.domain import Event, EventKind, PrimaryDomain, Relation, RelationKind
from soul_knowledge.fragment_generator import file_scoped_id
from soul_knowledge.graphify_adapter import GraphifyAdapter
from soul_knowledge.importer import SeedImporter
from soul_knowledge.projection import GraphProjector
from soul_knowledge.read_model_validator import ReadModelValidator, ValidationReport
from soul_knowledge.store import EventStore

ALPHA = b"# Alpha Lessons\n\n## First Lesson\n\nAlways roll the database back before retrying.\n"
BETA = b"# Beta\n\n## Counterexample\n\nA rollback that was itself rolled back.\n"


def _generation(tmp_path: Path) -> tuple[EventStore, Path, dict[str, Any]]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=tmp_path / "out")
    receipt = projector.rebuild()
    generation = Path(receipt.generation_dir)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    return store, generation, manifest


def _retamper(generation: Path, manifest: dict[str, Any], mutate) -> dict[str, Any]:
    """Apply *mutate* to graph.json, then honestly re-hash and re-write the manifest."""
    from soul_knowledge.domain import canonical_json

    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    mutate(graph)
    graph_path.write_text(canonical_json(graph), encoding="utf-8")
    tampered = dict(manifest)
    tampered["artifacts"] = dict(manifest["artifacts"])
    tampered["artifacts"]["graph.json"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    (generation / "manifest.json").write_text(canonical_json(tampered) + "\n", encoding="utf-8")
    return tampered


def test_a_fresh_generation_passes(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    report = ReadModelValidator().validate(generation, manifest)
    assert report.ok, report.errors
    assert report.errors == ()
    assert report.node_count > 0


def test_rejects_bad_endpoints_and_unknown_keys(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    validator = ReadModelValidator()

    tampered = _retamper(generation, manifest, lambda graph: graph.update({"surprise_key": {}}))
    report = validator.validate(generation, tampered)
    assert not report.ok
    assert any("surprise_key" in error for error in report.errors)

    _, generation, manifest = _generation(tmp_path / "second")

    def add_dangling(graph: dict) -> None:
        graph["links"].append(
            {
                "source": "P1",
                "target": "missing-node",
                "relation": "parent_of",
                "confidence": "EXTRACTED",
                "confidence_score": 1.0,
                "source_file": "alpha.md",
            }
        )

    tampered = _retamper(generation, manifest, add_dangling)
    report = validator.validate(generation, tampered)
    assert not report.ok
    assert any("missing-node" in error for error in report.errors)


def test_unknown_schema_version_fails_closed(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    tampered = _retamper(generation, manifest, lambda graph: graph["graph"].update({"schema_version": 999}))
    report = ReadModelValidator().validate(generation, tampered)
    assert not report.ok
    assert any("schema" in error.lower() for error in report.errors)


def test_junk_node_classifier(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    validator = ReadModelValidator()

    base_node = {
        "label": "Fine",
        "file_type": "document",
        "source_file": "junk.md",
        "source_version_id": "sv_anything",
    }
    junk_cases = [
        {**base_node, "id": "junk_blank", "label": "   "},
        {**base_node, "id": "evil/x"},
        {**base_node, "id": "junk_rationale", "file_type": "rationale"},
        {"id": "junk_orphan", "label": "No provenance", "file_type": "document", "source_file": "junk.md"},
    ]
    for junk in junk_cases:
        _, generation, manifest = _generation(tmp_path / f"case-{junk['id'].replace('/', '_')}")
        tampered = _retamper(generation, manifest, lambda graph, junk=junk: graph["nodes"].append(junk))
        report = validator.validate(generation, tampered)
        assert not report.ok, junk
        assert any("junk" in error.lower() for error in report.errors), (junk, report.errors)


def test_no_absolute_checkout_paths(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    absolute = str(tmp_path / "corpus" / "alpha.md")

    def poison(graph: dict) -> None:
        graph["nodes"][0]["source_file"] = absolute

    tampered = _retamper(generation, manifest, poison)
    report = ReadModelValidator().validate(generation, tampered)
    assert not report.ok
    assert any("absolute" in error.lower() for error in report.errors)


def test_secret_shapes_fail_closed(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)

    def poison(graph: dict) -> None:
        graph["nodes"][0]["label"] = "leaked sk-" + "a" * 24

    tampered = _retamper(generation, manifest, poison)
    report = ReadModelValidator().validate(generation, tampered)
    assert not report.ok
    assert any("secret" in error.lower() for error in report.errors)


def test_hash_mismatch_fails_closed(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    graph["nodes"][0]["label"] = "Tampered Label"
    from soul_knowledge.domain import canonical_json

    graph_path.write_text(canonical_json(graph), encoding="utf-8")
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok
    assert any("hash" in error.lower() for error in report.errors)


def test_invalid_confidence_and_file_type_fail_closed(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)

    def poison(graph: dict) -> None:
        graph["links"][0]["confidence"] = "GUESSED"
        graph["nodes"][0]["file_type"] = "hologram"

    tampered = _retamper(generation, manifest, poison)
    report = ReadModelValidator().validate(generation, tampered)
    assert not report.ok
    assert any("confidence" in error.lower() for error in report.errors)
    assert any("file_type" in error.lower() for error in report.errors)


def test_missing_artifact_fails_closed(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    (generation / "GRAPH_REPORT.md").unlink()
    report = ReadModelValidator().validate(generation, manifest)
    assert not report.ok


def test_direction_and_confidence_restored(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    assert ReadModelValidator().validate(generation, manifest).ok
    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    assert graph["directed"] is True
    assert all("confidence_score" in link for link in graph["links"])
    loaded = GraphifyAdapter().load_graph(graph_path)
    assert loaded.is_directed()
    assert loaded.number_of_edges() == len(graph["links"])
    assert loaded.has_edge("P1", file_scoped_id("alpha.md"))


def test_multigraph_probe_and_parallel_edges(tmp_path: Path) -> None:
    store, generation, manifest = _generation(tmp_path)
    probe = GraphifyAdapter().multigraph_probe()
    assert probe.ok, probe.error_message()
    assert manifest["multigraph"] is False
    # Two typed relations between the same pair survive as distinct links in the
    # explicitly simple (non-multigraph) knowledge graph.
    alpha = file_scoped_id("alpha.md")
    beta = file_scoped_id("beta.md")
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id=alpha,
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.SUPPORTS, source_node_id=alpha, target_node_id=beta),
        ),
        idempotency_key="extra:supports",
    )
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id=alpha,
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.DEPENDS_ON, source_node_id=alpha, target_node_id=beta),
        ),
        idempotency_key="extra:depends",
    )
    receipt = GraphProjector(store, output_root=tmp_path / "out").rebuild()
    graph = json.loads((Path(receipt.generation_dir) / "graph.json").read_text(encoding="utf-8"))
    assert graph["multigraph"] is False
    pair = sorted(link["relation"] for link in graph["links"] if link["source"] == alpha and link["target"] == beta)
    assert pair == ["depends_on", "supports"]


def test_parallel_link_collapse_is_reported_loudly(tmp_path: Path) -> None:
    """Fix round 1 (F1): the simple-graph collapse of parallel typed links is
    loud — the report names every collapsed pair and its relation kinds while
    validation still passes (the simple-graph decision itself stands)."""
    store, generation, manifest = _generation(tmp_path)
    report = ReadModelValidator().validate(generation, manifest)
    assert report.ok
    assert report.collapsed_links == ()

    alpha = file_scoped_id("alpha.md")
    beta = file_scoped_id("beta.md")
    for kind, key in ((RelationKind.SUPPORTS, "extra:supports"), (RelationKind.DEPENDS_ON, "extra:depends")):
        store.append_domain(
            Event(
                kind=EventKind.RELATION_ADD,
                node_id=alpha,
                domain=PrimaryDomain.P1,
                relation=Relation(kind=kind, source_node_id=alpha, target_node_id=beta),
            ),
            idempotency_key=key,
        )
    receipt = GraphProjector(store, output_root=tmp_path / "out").rebuild()
    generation = Path(receipt.generation_dir)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))

    report = ReadModelValidator().validate(generation, manifest)
    assert report.ok, report.errors  # the collapse is a loud warning, not a gate failure
    collapsed = {(entry.source, entry.target): entry.relations for entry in report.collapsed_links}
    assert collapsed[(alpha, beta)] == ("depends_on", "supports")
    # The pinned loader really does collapse the pair to a single edge.
    loaded = GraphifyAdapter().load_graph(generation / "graph.json")
    assert loaded.number_of_edges() == len(graph["links"]) - 1


def test_validation_report_is_serializable(tmp_path: Path) -> None:
    _, generation, manifest = _generation(tmp_path)
    report = ReadModelValidator().validate(generation, manifest)
    assert isinstance(report, ValidationReport)
    json.dumps(report.model_dump(mode="json"))
    with pytest.raises(Exception):  # noqa: B017 - generation dir must exist
        ReadModelValidator().validate(tmp_path / "nowhere", manifest)

"""Audit wave-1 fix probes (SUB-12): F4 secret shapes, F5 symlink containment, F6 publish sanitization.

Each probe reproduces the live finding from
``.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave1-security.md``
against the committed fix:

* F4 — the read-model validator (and therefore the publish gate) fails closed
  on JWT, ``bearer`` and ``github_pat_`` shapes, unified with
  :mod:`know_your_soul.security`'s pattern set.
* F5 — ``SourceRegistry._scan`` wires ``security.resolve_within_root``: a
  symlink inside a corpus root that points outside fails closed (typed
  ``PathEscapeError``, the file is skipped and named in the error); in-root
  symlinks still scan.
* F6 — published ``graph.json``/``graph.html`` never carry raw control, bidi
  or zero-width characters; ``projection`` sanitizes free text at publish via
  ``security.sanitize_text``.
"""

from __future__ import annotations

import hashlib
import json
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, canonical_json
from know_your_soul.events import Invocation
from know_your_soul.projection import GraphProjector
from know_your_soul.read_model_validator import ReadModelValidator
from know_your_soul.security import PathEscapeError
from know_your_soul.source_registry import ParserVersions, SourceKind, SourceRegistry
from know_your_soul.store import EventStore

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)
PARSER_VERSIONS = ParserVersions(
    parser_version="1",
    normalization_version="1",
    engine_version="1",
    scoring_contract_version="1.0.0",
)

JWT = "eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJVadQssw5c"
BEARER = "bearer abcdef0123456789abcdef0123456789"
GITHUB_PAT = "github_pat_11ABCDEFGHIJ" + "0123456789abcd"

BIDI = "‮"  # U+202E RIGHT-TO-LEFT OVERRIDE
ZERO_WIDTH = "​"  # U+200B ZERO WIDTH SPACE
CONTROL = ""  # C0 control (BEL)


def build_node(node_id: str, **overrides: object) -> Node:
    payload: dict[str, object] = {
        "node_id": node_id,
        "kind": NodeKind.LEAF,
        "domain": PrimaryDomain.P1,
        "source_version_id": "sv-1",
        "payload": {"fact": node_id},
        "summary": f"node {node_id}",
        "provenance": ("soul/01-identity-compliance.md",),
        "created_at": NOW,
        "updated_at": NOW,
        "version": 1,
    }
    payload.update(overrides)
    return Node.model_validate(payload)


def upsert(store: EventStore, node: Node, key: str) -> None:
    event = Event.model_validate(
        {
            "kind": EventKind.NODE_UPSERT,
            "node_id": node.node_id,
            "domain": node.domain,
            "node": node,
            "source_version_id": node.source_version_id,
            "occurred_at": NOW,
        }
    )
    invocation = Invocation.create("knowledge.log_event", request_hash=f"rh:{key}", idempotency_key=key)
    store.commit(event, invocation=invocation)


def published(tmp_path: Path) -> tuple[EventStore, GraphProjector, Path, dict[str, Any]]:
    store = EventStore(tmp_path / "soul.sqlite3")
    upsert(store, build_node("n-safe"), "safe")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    receipt = projector.rebuild()
    generation = Path(receipt.generation_dir)
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    return store, projector, generation, manifest


def retamper(generation: Path, manifest: dict[str, Any], mutate: Any) -> dict[str, Any]:
    """Apply *mutate* to graph.json, then honestly re-hash and re-write the manifest."""
    graph_path = generation / "graph.json"
    graph = json.loads(graph_path.read_text(encoding="utf-8"))
    mutate(graph)
    graph_path.write_text(canonical_json(graph), encoding="utf-8")
    tampered = dict(manifest)
    tampered["artifacts"] = dict(manifest["artifacts"])
    tampered["artifacts"]["graph.json"] = hashlib.sha256(graph_path.read_bytes()).hexdigest()
    (generation / "manifest.json").write_text(canonical_json(tampered) + "\n", encoding="utf-8")
    return tampered


# --------------------------------------------------------------------- F4


def test_f4_validator_fails_closed_on_jwt_bearer_and_github_pat(tmp_path: Path) -> None:
    """Probe P4 closure: the audit wave-1 secret shapes now fail validation."""
    store, _, generation, manifest = published(tmp_path)
    validator = ReadModelValidator()
    assert validator.validate(generation, manifest).ok  # clean baseline
    for secret in (JWT, BEARER, GITHUB_PAT):
        tampered = retamper(
            generation,
            manifest,
            lambda graph, leaked=secret: graph["nodes"][0].update({"label": f"leaked {leaked}"}),
        )
        report = validator.validate(generation, tampered)
        assert not report.ok, f"validator accepted {secret[:12]}…"
        assert any("secret" in error.lower() for error in report.errors)
    store.close()


def test_f4_publish_fails_closed_on_secret_shaped_summary(tmp_path: Path) -> None:
    """A node carrying a JWT/bearer/github_pat never reaches an activated generation.

    Wave-2 F7 semantics: fail closed *per node* — the poisoned node is
    quarantined from the extraction, the rest of the graph still publishes,
    and the whole-generation validator remains as the backstop. The invariant
    is unchanged and stronger: no secret-shaped string ever reaches a read
    surface (previously the whole publish failed, taking all reads down).
    """
    for index, secret in enumerate((JWT, BEARER, GITHUB_PAT)):
        store = EventStore(tmp_path / f"soul-{index}.sqlite3")
        upsert(store, build_node("n-secret", summary=f"token leak: {secret}"), f"leak-{index}")
        projector = GraphProjector(store, output_root=tmp_path / f"out-{index}")
        receipt = projector.rebuild()
        assert receipt.node_count == 0, "the poisoned node must be quarantined, not published"
        manifest = projector.active_manifest()
        assert manifest is not None, "the rest of the graph still activates (F7 blast-radius fix)"
        assert int(manifest["node_count"]) == 0
        generation = projector.active_generation_dir()
        assert generation is not None
        for artifact in generation.iterdir():
            text = artifact.read_text(encoding="utf-8")
            assert secret not in text, f"{artifact.name} leaks the secret"
            assert "n-secret" not in text, f"{artifact.name} still carries the quarantined node"
        rows = store.execute_raw(
            "SELECT payload_json FROM event_log WHERE event_type = 'security.projection_quarantine'"
        ).fetchall()
        assert len(rows) == 1, "the quarantine leaves exactly one typed audit event"
        assert secret not in str(rows[0]["payload_json"])
        store.close()


# --------------------------------------------------------------------- F5


def test_f5_corpus_scan_refuses_symlink_escape(tmp_path: Path) -> None:
    """Probe P5 closure: outside content behind a corpus symlink is never ingested."""
    outside = tmp_path / "outside-secret.txt"
    outside.write_text("outside content that must never be ingested", encoding="utf-8")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.md").write_text("# Real\n\nReal lesson.\n", encoding="utf-8")
    (corpus / "linked.md").symlink_to(outside)
    store = EventStore(tmp_path / "soul.sqlite3")
    registry = SourceRegistry(store)
    with pytest.raises(PathEscapeError) as excinfo:
        registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert "linked.md" in str(excinfo.value)  # the skipped escape is recorded in the error
    assert registry.active() is None  # nothing was recorded
    store.close()


def test_f5_single_file_symlink_escape_refused(tmp_path: Path) -> None:
    outside = tmp_path / "outside.md"
    outside.write_text("outside", encoding="utf-8")
    root = tmp_path / "root"
    root.mkdir()
    link = root / "SOUL.md"
    link.symlink_to(outside)
    store = EventStore(tmp_path / "soul.sqlite3")
    registry = SourceRegistry(store)
    with pytest.raises(PathEscapeError):
        registry.observe(link, SourceKind.SOUL_MD, PARSER_VERSIONS)
    store.close()


def test_f5_in_root_symlink_still_scans(tmp_path: Path) -> None:
    """A symlink whose target stays inside the corpus root remains valid input."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "real.md").write_text("# Real\n\nReal lesson.\n", encoding="utf-8")
    (corpus / "alias.md").symlink_to(corpus / "real.md")
    store = EventStore(tmp_path / "soul.sqlite3")
    registry = SourceRegistry(store)
    version = registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    paths = {entry.path for entry in version.file_manifest}
    assert paths == {"alias.md", "real.md"}
    expected = hashlib.sha256((corpus / "real.md").read_bytes()).hexdigest()
    assert all(entry.sha256 == expected for entry in version.file_manifest)
    store.close()


# --------------------------------------------------------------------- F6


def test_f6_publish_strips_control_bidi_and_zero_width(tmp_path: Path) -> None:
    """Probe closure: raw U+202E / U+200B / C0 controls never reach published artifacts."""
    store = EventStore(tmp_path / "soul.sqlite3")
    dirty = f"payl{BIDI}oad{ZERO_WIDTH} tex{CONTROL}t"
    upsert(store, build_node("n-bidi", summary=dirty), "bidi")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    receipt = projector.rebuild()  # sanitizes instead of failing
    generation = Path(receipt.generation_dir)
    for artifact in ("graph.json", "graph.html", "GRAPH_REPORT.md"):
        raw = (generation / artifact).read_bytes()
        for char in (BIDI, ZERO_WIDTH, CONTROL):
            assert char.encode("utf-8") not in raw, f"{artifact} carries raw {char!r}"
    graph = json.loads((generation / "graph.json").read_text(encoding="utf-8"))
    labels = {node["id"]: node["label"] for node in graph["nodes"]}
    assert labels["n-bidi"] == "payload text"
    manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    assert ReadModelValidator().validate(generation, manifest).ok
    store.close()

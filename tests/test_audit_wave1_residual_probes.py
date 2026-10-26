"""Residual audit wave-1 probes (SUB-19): R1 read-surface redaction, R2 importer bounds.

R1 — defense-in-depth on the Task 4 read surfaces: the publish gate
(read_model_validator) already fails closed on secret shapes, but
``get_node``/``get_subtree``/``query`` must *also* pass content-derived text
(summary, provenance, context tags) through
:func:`know_your_soul.security.redact_text` before leaving the service. A
node carrying secret/PII-shaped text must never surface the raw string;
clean text must pass through byte-identical (token estimator parity).

R2 — every importer file read is contained and bounded: paths resolve through
:func:`know_your_soul.security.resolve_within_root` (symlink/path escapes
fail closed with a typed ``PathEscapeError``) and files larger than
``importer.MAX_IMPORT_FILE_BYTES`` fail closed with a typed
``OversizedPayloadError``. Normal and in-root-symlinked imports still work.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from know_your_soul.contracts import QueryRequest
from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, Relation, RelationKind, ScoreVector
from know_your_soul.importer import SeedImporter
from know_your_soul.importer import __dict__ as _importer_dict
from know_your_soul.projection import GraphProjector
from know_your_soul.security import OversizedPayloadError, PathEscapeError
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore

# The R2 fix must define and document this constant; the fallback keeps the
# probes collectible pre-fix so the R1 RED signal is visible too.
MAX_IMPORT_FILE_BYTES: int = _importer_dict.get("MAX_IMPORT_FILE_BYTES", 1_048_576)

AWS_KEY = "AKIA" + "0123456789ABCDEF"
EMAIL = "admin@example.com"

LESSON = b"# Lessons\n\n## Rollback Drill\n\nAlways roll the database back before retrying.\n"


# --------------------------------------------------------------------- setup


def _upsert(store: EventStore, node: Node) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=f"probe:upsert:{node.node_id}",
    )


def _relate(store: EventStore, source: str, target: str) -> None:
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id=source,
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.PARENT_OF, source_node_id=source, target_node_id=target),
        ),
        idempotency_key=f"probe:rel:{source}:{target}",
    )


def _roots(store: EventStore) -> None:
    _upsert(
        store,
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-probe",
        ),
    )
    for domain in PrimaryDomain:
        _upsert(
            store,
            Node(
                node_id=domain.value,
                parent_id="KROOT",
                kind=NodeKind.PRIMARY,
                domain=domain,
                summary=domain.title,
                provenance=("soul://roots",),
                source_version_id="sv-probe",
            ),
        )


def _service(tmp_path: Path, *nodes: Node) -> KnowledgeService:
    store = EventStore(tmp_path / "soul.sqlite3")
    _roots(store)
    for node in nodes:
        _upsert(store, node)
        _relate(store, node.parent_id or "P1", node.node_id)
    projector = GraphProjector(store, output_root=tmp_path / "out")
    projector.rebuild()
    return KnowledgeService(store, projector)


def _leaky_node() -> Node:
    """A node whose content-derived text fields carry PII shapes.

    PII (email) is not a publish-gate blocker — the gate scans SECRET_PATTERNS
    only — so this node reaches an activated generation, which is exactly the
    gap R1 closes at the read surface. True secret shapes (AWS keys, JWTs)
    never survive the F4 publish gate; they are covered by the unit probe
    against ``_node_view``/``_result_node`` directly.
    """

    return Node(
        node_id="n-leaky",
        parent_id="P1",
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=f"Rollback drill; contact {EMAIL} for the runbook",
        provenance=(f"soul://corpus/lesson.md?owner={EMAIL}",),
        context_tags=(f"owner-{EMAIL}", "rollback"),
        source_version_id="sv-probe",
        scores=ScoreVector.zero(),
    )


def test_r1_result_and_view_helpers_redact_true_secret_shapes() -> None:
    """Unit probe: the read-surface layer itself neutralizes full secret shapes.

    Non-vacuous: the raw dict carries an AWS key, a JWT and a bearer token in
    every content field; the helpers must emit none of them.
    """
    jwt = "eyJhbGciOiJIUzI1NiJ9.eyJzdWIiOiIxMjM0NTY3ODkwIn0.SflKxwRJSMeKKF2QT4fwpMeJf36POk6yJV"
    raw = {
        "id": "n-raw",
        "label": f"key {AWS_KEY} and bearer abcdef0123456789abcdef0123456789",
        "domain": "P1",
        "kind": "child",
        "version": 3,
        "provenance": [f"soul://corpus/x.md#jwt-{jwt}", "soul://plain"],
        "context_tags": [f"tok-{AWS_KEY}", "clean-tag"],
        "lifecycle_status": "active",
        "source_version_id": "sv-raw",
    }
    view = KnowledgeService._node_view(raw)
    result = KnowledgeService._result_node(raw)
    for text in (view.summary, result.summary, *view.provenance, *view.context_tags):
        for secret in (AWS_KEY, jwt, "abcdef0123456789abcdef0123456789"):
            assert secret not in text
    assert "[REDACTED:" in view.summary
    assert "[REDACTED:" in view.provenance[0]
    assert "[REDACTED:" in view.context_tags[0]
    # Clean fields and structure survive untouched.
    assert view.provenance[1] == "soul://plain"
    assert view.context_tags[1] == "clean-tag"
    assert result.node_id == "n-raw" and result.version == 3


# ------------------------------------------------------------------ R1 probes


def test_r1_get_node_redacts_secret_and_pii_shapes(tmp_path: Path) -> None:
    service = _service(tmp_path, _leaky_node())
    view = service.get_node("n-leaky").node
    assert AWS_KEY not in view.summary and EMAIL not in view.summary
    assert "[REDACTED:" in view.summary
    for entry in view.provenance:
        assert EMAIL not in entry
    for tag in view.context_tags:
        assert EMAIL not in tag


def test_r1_get_subtree_redacts_secret_and_pii_shapes(tmp_path: Path) -> None:
    service = _service(tmp_path, _leaky_node())
    response = service.get_subtree("P1", token_budget=4096)
    leaky = [node for node in response.nodes if node.node_id == "n-leaky"]
    assert leaky, "probe vacuous: the leaky node never entered the subtree"
    for node in leaky:
        assert AWS_KEY not in node.summary and EMAIL not in node.summary
        assert "[REDACTED:" in node.summary


def test_r1_query_redacts_results_and_provenance(tmp_path: Path) -> None:
    service = _service(tmp_path, _leaky_node())
    response = service.query(QueryRequest(purpose="rollback drill", domain="P1", token_budget=4096))
    leaky = [node for node in response.results if node.node_id == "n-leaky"]
    assert leaky, "probe vacuous: the leaky node never entered the results"
    for node in leaky:
        assert AWS_KEY not in node.summary and EMAIL not in node.summary
    for entry in response.provenance:
        assert EMAIL not in entry


def test_r1_clean_text_passes_through_unchanged(tmp_path: Path) -> None:
    """Non-vacuous parity: clean input is byte-identical after redaction wiring."""
    clean = Node(
        node_id="n-clean",
        parent_id="P1",
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary="Run the gates before commit",
        provenance=("soul://corpus/lesson.md",),
        context_tags=("gates", "discipline"),
        source_version_id="sv-probe",
        scores=ScoreVector.zero(),
    )
    service = _service(tmp_path, clean)
    view = service.get_node("n-clean").node
    assert view.summary == "Run the gates before commit"
    assert view.provenance == ("soul://corpus/lesson.md",)
    assert view.context_tags == ("gates", "discipline")
    response = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=2048))
    found = [node for node in response.results if node.node_id == "n-clean"]
    assert found and all(node.summary == "Run the gates before commit" for node in found)


# ------------------------------------------------------------------ R2 probes


def test_r2_import_file_refuses_symlink_escape(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside.md", LESSON)
    folder = tmp_path / "inbox"
    folder.mkdir()
    link = folder / "lesson.md"
    link.symlink_to(outside)
    importer = SeedImporter(EventStore(tmp_path / "soul.sqlite3"))
    with pytest.raises(PathEscapeError):
        importer.import_file(link, domain=PrimaryDomain.P1, source_uri="soul://probe/lesson.md")


def test_r2_import_file_refuses_oversized_file(tmp_path: Path) -> None:
    giant = _write(tmp_path / "giant.md", b"# Giant\n\n" + b"x" * (MAX_IMPORT_FILE_BYTES + 1))
    importer = SeedImporter(EventStore(tmp_path / "soul.sqlite3"))
    with pytest.raises(OversizedPayloadError) as excinfo:
        importer.import_file(giant, domain=PrimaryDomain.P1, source_uri="soul://probe/giant.md")
    assert str(MAX_IMPORT_FILE_BYTES) in str(excinfo.value)


def test_r2_import_directory_refuses_symlink_escape(tmp_path: Path) -> None:
    outside = _write(tmp_path / "outside.md", LESSON)
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write(corpus / "real.md", LESSON)
    (corpus / "linked.md").symlink_to(outside)
    importer = SeedImporter(EventStore(tmp_path / "soul.sqlite3"), state_dir=tmp_path / "state")
    with pytest.raises(PathEscapeError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_r2_import_directory_refuses_oversized_file(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    _write(corpus / "giant.md", b"# Giant\n\n" + b"x" * (MAX_IMPORT_FILE_BYTES + 1))
    importer = SeedImporter(EventStore(tmp_path / "soul.sqlite3"), state_dir=tmp_path / "state")
    with pytest.raises(OversizedPayloadError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_r2_normal_and_in_root_symlinked_imports_still_work(tmp_path: Path) -> None:
    """Positive control: containment never rejects content that stays in-root."""
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    real = _write(corpus / "real.md", LESSON)
    (corpus / "alias.md").symlink_to(real)
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "state")
    receipts = importer.import_directory(corpus, domain=PrimaryDomain.P1)
    assert len(receipts) == 2
    assert all(receipt.node_ids for receipt in receipts)
    # Standalone single-file import (default parent containment), fresh content.
    standalone = _write(tmp_path / "standalone" / "solo.md", LESSON)
    single = importer.import_file(standalone, domain=PrimaryDomain.P2, source_uri="soul://probe/solo.md")
    assert single.node_ids


def _write(path: Path, content: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path

"""Contract tests for the atomic Graphify projection (Task 3).

A single serialized projector replays knowledge-affecting events in
``knowledge_seq`` order, derives a Graphify-schema graph deterministically,
publishes a content-addressed generation (graph.json + report + labels +
signatures + analysis + HTML view + manifest), and activates it through one
atomic ``CURRENT`` pointer gated by a compare-and-swap on the knowledge
watermark (spec section 7.4; matrix rows 4-14, 17-20, 23-24, 26-32, 35).
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import subprocess
import sys
import time
from pathlib import Path
from typing import Any

import pytest

from know_your_soul.audit import AuditLog
from know_your_soul.domain import Event, EventKind, Node, PrimaryDomain
from know_your_soul.events import Invocation, InvocationOutcome
from know_your_soul.fragment_generator import (
    FRAGMENT_GENERATOR_VERSION,
    FRAGMENT_PROMPT_FINGERPRINT,
    FragmentGenerator,
    file_scoped_id,
)
from know_your_soul.graphify_adapter import GraphifyAdapter
from know_your_soul.importer import SeedImporter, injective_section_key
from know_your_soul.projection import (
    ARTIFACT_NAMES,
    FragmentCache,
    GraphProjector,
    OversizedArtifactError,
    ProjectionValidationError,
    ProjectorBusy,
    ShrinkGuardError,
    StalePublicationError,
)
from know_your_soul.retrieval import BudgetedTraversal, PrimaryRouter
from know_your_soul.source_registry import SourceRegistry
from know_your_soul.source_watcher import ChangeKind, SourceWatcher
from know_your_soul.store import EventStore

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
GAMMA = b"# Gamma\n\n## Standing Order\n\nNever widen a transaction to cover reads.\n"

SRC = Path(__file__).resolve().parents[1] / "src"


def _populated(tmp_path: Path) -> tuple[EventStore, SeedImporter]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    return store, importer


def _projector(store: EventStore, tmp_path: Path) -> GraphProjector:
    return GraphProjector(store, output_root=tmp_path / "out")


def _active_generation(tmp_path: Path) -> Path:
    pointer = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8").strip()
    return tmp_path / "out" / "generations" / pointer


def _manifest(tmp_path: Path) -> dict:
    return json.loads((_active_generation(tmp_path) / "manifest.json").read_text(encoding="utf-8"))


def _graph_json(tmp_path: Path) -> dict:
    return json.loads((_active_generation(tmp_path) / "graph.json").read_text(encoding="utf-8"))


# ------------------------------------------------------------ publication


def test_rebuild_publishes_a_manifest_gated_generation(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    receipt = _projector(store, tmp_path).rebuild()
    assert receipt.activated is True
    assert receipt.projection_version.startswith("gen-")
    assert receipt.source_knowledge_high_watermark == store.knowledge_high_watermark()
    generation = Path(receipt.generation_dir)
    assert generation.is_dir()
    assert generation.parent.name == "generations"
    assert generation.name == receipt.content_hash
    for name in ARTIFACT_NAMES:
        assert (generation / name).is_file(), name
    assert (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8").strip() == generation.name
    manifest = _manifest(tmp_path)
    assert manifest["projection_version"] == receipt.projection_version
    assert manifest["content_hash"] == receipt.content_hash
    assert manifest["source_knowledge_high_watermark"] == store.knowledge_high_watermark()
    graph_bytes = (generation / "graph.json").read_bytes()
    assert manifest["artifacts"]["graph.json"] == hashlib.sha256(graph_bytes).hexdigest()
    assert manifest["graph_content_hash"] == hashlib.sha256(graph_bytes).hexdigest()
    assert store.projection_version() == receipt.projection_version


def test_write_receipt_carries_the_active_projection_version(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    receipt = _projector(store, tmp_path).rebuild()
    current = store.current_node("P1")
    assert current is not None
    event = Event(kind=EventKind.NODE_UPSERT, node_id="P1", domain=PrimaryDomain.P1, node=current)
    write = store.commit(event, invocation=Invocation.create("knowledge.log_event", request_hash="x"))
    assert write.projection_version == receipt.projection_version
    assert write.source_knowledge_high_watermark == store.knowledge_high_watermark()


# ------------------------------------------------------- cache (rows 4-9)


def test_cache_keyed_on_content_hash_not_stat(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "cache")
    key = cache.key_for("a.md", b"# A\n")
    assert cache.get(key) is None
    cache.put(key, {"nodes": [], "edges": []})
    assert cache.get(key) == {"nodes": [], "edges": []}
    touched = tmp_path / "a.md"
    touched.write_bytes(b"# A\n")
    os.utime(touched, (0, 0))
    assert cache.key_for("a.md", b"# A\n") == key
    assert cache.get(cache.key_for("a.md", b"# B\n")) is None


def test_cache_namespace_pins_generator_version(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "cache")
    assert FRAGMENT_GENERATOR_VERSION in cache.namespace
    key = cache.key_for("a.md", b"x")
    cache.put(key, {"nodes": []})
    bumped = FragmentCache(tmp_path / "cache", namespace="fragment-generator-0.0.0")
    assert bumped.get(key) is None


def test_fragment_fingerprint_is_stable(tmp_path: Path) -> None:
    assert len(FRAGMENT_PROMPT_FINGERPRINT) == 64
    int(FRAGMENT_PROMPT_FINGERPRINT, 16)
    adapter = GraphifyAdapter()
    fragment = adapter.fragment_from_markdown(source_file="a.md", content=b"# A\n")
    again = adapter.fragment_from_markdown(source_file="a.md", content=b"# A\n")
    assert fragment.fingerprint == FRAGMENT_PROMPT_FINGERPRINT
    assert again.fingerprint == fragment.fingerprint


def test_truncated_source_is_not_served_from_cache(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "cache")
    adapter = GraphifyAdapter(generator=FragmentGenerator(max_source_bytes=64), cache=cache)
    big = ALPHA * 100
    first = adapter.fragment_from_markdown(source_file="big.md", content=big)
    assert first.partial is True
    assert cache.entries() == []
    second = adapter.fragment_from_markdown(source_file="big.md", content=big)
    assert second.partial is True
    assert cache.entries() == []


def test_cache_is_portable_across_roots(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "cache-a")
    key = cache.key_for("docs/a.md", b"# A\n")
    cache.put(key, {"nodes": [{"id": "doc_a"}], "edges": []})
    shutil.copytree(tmp_path / "cache-a", tmp_path / "cache-b")
    moved = FragmentCache(tmp_path / "cache-b")
    assert moved.get(key) == {"nodes": [{"id": "doc_a"}], "edges": []}


def test_cache_prune_bounded_by_live_documents(tmp_path: Path) -> None:
    cache = FragmentCache(tmp_path / "cache")
    keys = [cache.key_for(f"{name}.md", name.encode()) for name in ("a", "b", "c")]
    for key in keys:
        cache.put(key, {"nodes": []})
    removed = cache.prune(keys[:1])
    assert removed == 2
    assert cache.entries() == keys[:1]


# -------------------------------------------- watch and lock (rows 11-13)


def test_source_change_triggers_rebuild(tmp_path: Path) -> None:
    store, importer = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    first = projector.rebuild()

    def _update(_changes: list) -> None:
        importer.import_directory(tmp_path / "corpus", domain=PrimaryDomain.P1)
        projector.rebuild()

    watcher = SourceWatcher(tmp_path / "corpus", state_path=tmp_path / "watch-state.json", on_change=_update)
    baseline = watcher.poll()
    assert {change.kind for change in baseline} == {ChangeKind.ADDED}
    (tmp_path / "corpus" / "gamma.md").write_bytes(GAMMA)
    changes = watcher.poll()
    assert [change.kind for change in changes] == [ChangeKind.ADDED]
    assert changes[0].path == "gamma.md"
    active = projector.active_manifest()
    assert active is not None
    assert active["source_knowledge_high_watermark"] == store.knowledge_high_watermark()
    assert active["source_knowledge_high_watermark"] > first.source_knowledge_high_watermark


def test_doc_change_triggers_deterministic_update(tmp_path: Path) -> None:
    """Row 12: a document change runs the deterministic pipeline; no flag-only path."""
    store, importer = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    projector.rebuild()

    def _update(_changes: list) -> None:
        importer.import_directory(tmp_path / "corpus", domain=PrimaryDomain.P1)
        projector.rebuild()

    watcher = SourceWatcher(tmp_path / "corpus", state_path=tmp_path / "watch-state.json", on_change=_update)
    watcher.poll()
    assert not hasattr(watcher, "needs_update")
    (tmp_path / "corpus" / "alpha.md").write_bytes(ALPHA + b"\n## Extra Lesson\n\nMore.\n")
    changes = watcher.poll()
    assert [change.kind for change in changes] == [ChangeKind.CHANGED]
    graph = _graph_json(tmp_path)
    ids = {node["id"] for node in graph["nodes"]}
    assert f"alpha.{injective_section_key('Extra Lesson')}" in ids


def test_single_writer_lock_and_pending_drain(tmp_path: Path) -> None:
    store, importer = _populated(tmp_path)
    first = _projector(store, tmp_path)
    second = _projector(store, tmp_path)
    with first.hold_lock():
        with pytest.raises(ProjectorBusy):
            second.rebuild()
        (tmp_path / "corpus" / "gamma.md").write_bytes(GAMMA)
        importer.import_file(
            tmp_path / "corpus" / "gamma.md", domain=PrimaryDomain.P2, source_uri="soul://corpus/gamma.md"
        )
    receipt = second.rebuild()
    assert receipt.source_knowledge_high_watermark == store.knowledge_high_watermark()
    ids = {node["id"] for node in _graph_json(tmp_path)["nodes"]}
    assert file_scoped_id("gamma.md") in ids


# ------------------------------------- invariance and build gate (rows 14, 17-20)


def _rebuild_hash(root: Path) -> str:
    store = EventStore(root / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=root / "import-state")
    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(ALPHA)
    (corpus / "beta.md").write_bytes(BETA)
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    return GraphProjector(store, output_root=root / "out").rebuild().content_hash


def test_rebuild_invariant_across_roots_and_eols(tmp_path: Path) -> None:
    first = _rebuild_hash(tmp_path / "checkout-one")
    second = _rebuild_hash(tmp_path / "checkout-two" / "nested")
    assert first == second
    generator = FragmentGenerator()
    lf = generator.generate(source_file="alpha.md", content=ALPHA)
    crlf = generator.generate(source_file="alpha.md", content=ALPHA.replace(b"\n", b"\r\n"))
    assert lf.to_dict() == crlf.to_dict()


def test_build_warning_fails_the_gate(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    active = SourceRegistry(store).active()
    assert active is not None
    bad = Node(node_id="bad/id", domain=PrimaryDomain.P1, summary="bad", source_version_id=active.source_version_id)
    store.append_domain(
        Event(kind=EventKind.NODE_UPSERT, node_id="bad/id", domain=PrimaryDomain.P1, node=bad),
        idempotency_key="bad-node",
    )
    projector = _projector(store, tmp_path)
    with pytest.raises(ProjectionValidationError):
        projector.rebuild()
    assert projector.active_manifest() is None
    assert store.projection_version() is None


def test_explicit_provenance_prevents_cross_file_wipes(tmp_path: Path) -> None:
    adapter = GraphifyAdapter()
    alpha = adapter.fragment_from_markdown(source_file="alpha.md", content=ALPHA)
    beta = adapter.fragment_from_markdown(source_file="beta.md", content=BETA)
    merged = adapter.merge_fragments([alpha, beta])
    beta_before = {node["id"] for node in merged["nodes"] if node["source_file"] == "beta.md"}
    regenerated = adapter.fragment_from_markdown(source_file="alpha.md", content=b"# Alpha v2\n\n## Changed\n\nNew.\n")
    assert all(node["source_file"] == "alpha.md" for node in regenerated.nodes)
    remerged = adapter.replace_file_tier(merged, regenerated)
    beta_after = {node["id"] for node in remerged["nodes"] if node["source_file"] == "beta.md"}
    assert beta_after == beta_before
    alpha_labels = [node["label"] for node in remerged["nodes"] if node["source_file"] == "alpha.md"]
    assert "Alpha v2" in alpha_labels


def test_shrink_guard_blocks_silent_loss(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    first = projector.rebuild()
    for node_id in (f"beta.{injective_section_key('Counterexample')}", file_scoped_id("beta.md")):
        store.append_domain(
            Event(kind=EventKind.NODE_RETIRE, node_id=node_id, domain=PrimaryDomain.P1),
            idempotency_key=f"retire:{node_id}",
        )
    with pytest.raises(ShrinkGuardError):
        projector.rebuild()
    receipt = projector.rebuild(allow_shrink=True)
    assert receipt.node_count < first.node_count


def test_dedup_disabled_on_guarded_build(monkeypatch: pytest.MonkeyPatch) -> None:
    import know_your_soul.graphify_adapter as adapter_module

    calls: list[dict[str, Any]] = []
    original = adapter_module.build_from_json

    def spy(extraction: dict, **kwargs: Any) -> Any:
        calls.append(kwargs)
        return original(extraction, **kwargs)

    monkeypatch.setattr(adapter_module, "build_from_json", spy)
    adapter = GraphifyAdapter()
    fragment = adapter.generate_fragment(
        nodes=[
            {"id": "doc_a_one", "label": "Same Label", "file_type": "document", "source_file": "a.md"},
            {"id": "doc_a_two", "label": "Same Label", "file_type": "document", "source_file": "a.md"},
        ],
        edges=[],
    )
    graph = adapter.build_graph(fragment.to_dict())
    assert calls and calls[0].get("dedup") is False
    assert {"doc_a_one", "doc_a_two"} <= set(graph.nodes)


# ------------------------------------ atomic activation (rows 23-24, crash probes)


def test_atomic_activation_is_fsynced_and_manifest_gated(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    receipt = projector.rebuild()
    pointer = tmp_path / "out" / "CURRENT"
    assert pointer.is_file()
    generation = _active_generation(tmp_path)
    assert (generation / "manifest.json").is_file()
    # Torn publication: a pointer naming a generation without a manifest never serves.
    torn = tmp_path / "out" / "generations" / "torn-generation"
    torn.mkdir()
    (torn / "graph.json").write_text("{}", encoding="utf-8")
    pointer.write_text("torn-generation\n", encoding="utf-8")
    repaired = projector.repair_if_stale()
    assert repaired.repaired is True
    assert pointer.read_text(encoding="utf-8").strip() == receipt.content_hash
    manifest = projector.active_manifest()
    assert manifest is not None and manifest["projection_version"] == receipt.projection_version


def test_activation_swap_failure_leaves_previous_pointer_and_repairs(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    store, importer = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    first = projector.rebuild()
    (tmp_path / "corpus" / "gamma.md").write_bytes(GAMMA)
    importer.import_file(tmp_path / "corpus" / "gamma.md", domain=PrimaryDomain.P2, source_uri="soul://corpus/gamma.md")
    real_replace = os.replace

    def crash_on_pointer(src: Any, dst: Any) -> None:
        if str(dst).endswith("CURRENT"):
            raise OSError("simulated crash during pointer swap")
        real_replace(src, dst)

    monkeypatch.setattr(os, "replace", crash_on_pointer)
    with pytest.raises(OSError, match="simulated crash"):
        projector.rebuild()
    # The store CAS committed the new version but the pointer still names the old one.
    pointer_manifest = projector.active_manifest()
    assert pointer_manifest is not None
    assert pointer_manifest["projection_version"] == first.projection_version
    assert store.projection_version() != first.projection_version
    monkeypatch.undo()
    repaired = projector.repair_if_stale()
    assert repaired.repaired is True
    manifest = projector.active_manifest()
    assert manifest is not None and manifest["projection_version"] == store.projection_version()
    assert manifest["source_knowledge_high_watermark"] == store.knowledge_high_watermark()


def test_unchanged_artifact_is_not_rewritten(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    first = projector.rebuild()
    generation = Path(first.generation_dir)
    before = {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in generation.iterdir()}
    pointer = tmp_path / "out" / "CURRENT"
    pointer_before = (pointer.stat().st_ino, pointer.stat().st_mtime_ns)
    time.sleep(0.01)
    second = projector.rebuild()
    assert second.content_hash == first.content_hash
    assert Path(second.generation_dir) == generation
    after = {path.name: (path.stat().st_ino, path.stat().st_mtime_ns) for path in generation.iterdir()}
    assert before == after
    assert (pointer.stat().st_ino, pointer.stat().st_mtime_ns) == pointer_before


# --------------------------------------------- communities (rows 26-31)


def test_pinned_partitioner_recorded_in_manifest(tmp_path: Path) -> None:
    import importlib.util

    store, _ = _populated(tmp_path)
    _projector(store, tmp_path).rebuild()
    manifest = _manifest(tmp_path)
    assert importlib.util.find_spec("graspologic") is None
    assert manifest["partitioner"]["name"] == "networkx-louvain"
    assert manifest["partitioner"]["seed"] == 42
    assert manifest["partitioner"]["resolution"] == 1.0


def test_partition_input_canonical_across_hash_seeds(tmp_path: Path) -> None:
    script = (
        "import json, sys\n"
        f"sys.path.insert(0, {str(SRC)!r})\n"
        "from know_your_soul.graphify_adapter import GraphifyAdapter\n"
        "adapter = GraphifyAdapter()\n"
        "nodes = [{'id': f'n{i}', 'label': f'N{i}', 'file_type': 'document', 'source_file': 'a.md'}"
        " for i in range(8)]\n"
        "edges = [{'source': f'n{i}', 'target': f'n{(i + 1) % 8}', 'relation': 'contains',"
        " 'confidence': 'EXTRACTED', 'source_file': 'a.md'} for i in range(8)]\n"
        "edges += [{'source': 'n0', 'target': 'n4', 'relation': 'contains', 'confidence': 'AMBIGUOUS',"
        " 'source_file': 'a.md'}]\n"
        "fragment = adapter.generate_fragment(nodes=nodes, edges=edges)\n"
        "graph = adapter.build_graph(fragment.to_dict())\n"
        "print(json.dumps({str(k): v for k, v in adapter.partition(graph).items()}, sort_keys=True))\n"
    )
    outputs = []
    for seed in ("0", "1"):
        completed = subprocess.run(
            [sys.executable, "-c", script],
            env={"PATH": os.environ.get("PATH", ""), "PYTHONHASHSEED": seed, "PYTHONPATH": str(SRC)},
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        assert completed.returncode == 0, completed.stderr
        outputs.append(completed.stdout.strip())
    assert outputs[0] == outputs[1]


def test_community_ids_internal_and_signed(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    _projector(store, tmp_path).rebuild()
    graph = _graph_json(tmp_path)
    signatures = json.loads((_active_generation(tmp_path) / "signatures.json").read_text(encoding="utf-8"))
    member_sigs = set()
    for node in graph["nodes"]:
        assert "community" not in node
        sig = node.get("community_sig")
        assert isinstance(sig, str) and len(sig) == 16
        member_sigs.add(sig)
    assert member_sigs <= set(signatures)
    for members in signatures.values():
        assert members == sorted(members)


def test_hub_labels_are_placeholders(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    _projector(store, tmp_path).rebuild()
    labels = json.loads((_active_generation(tmp_path) / "labels.json").read_text(encoding="utf-8"))
    assert labels
    assert set(labels.values()) == {"UNREVIEWED"}


def test_hub_policy_is_recorded(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    _projector(store, tmp_path).rebuild()
    manifest = _manifest(tmp_path)
    assert manifest["hub_policy"] == {"exclude_hubs_percentile": 99, "ambiguous_scale": 0.5}


def test_cohesion_is_not_a_quality_score(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    _projector(store, tmp_path).rebuild()
    analysis = json.loads((_active_generation(tmp_path) / "analysis.json").read_text(encoding="utf-8"))
    diagnostics = analysis["structural_diagnostics"]
    assert "cohesion_by_community_sig" in diagnostics
    assert "never a knowledge-quality" in diagnostics["note"]
    for key in diagnostics:
        assert key not in {"p", "m", "e", "P", "M", "E"}


# --------------------------------------------- report and view (rows 32, 35)


def test_report_bytes_are_date_independent(tmp_path: Path) -> None:
    import re

    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    first = projector.rebuild()
    report = Path(first.generation_dir) / "GRAPH_REPORT.md"
    before = report.read_bytes()
    time.sleep(1.05)
    second = projector.rebuild()
    assert second.content_hash == first.content_hash
    assert report.read_bytes() == before
    assert not re.search(rb"\d{4}-\d{2}-\d{2}", before)


def test_html_view_is_generation_keyed(tmp_path: Path) -> None:
    store, importer = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    first = projector.rebuild()
    html = (Path(first.generation_dir) / "graph.html").read_text(encoding="utf-8")
    assert first.projection_version in html
    (tmp_path / "corpus" / "gamma.md").write_bytes(GAMMA)
    importer.import_file(tmp_path / "corpus" / "gamma.md", domain=PrimaryDomain.P2, source_uri="soul://corpus/gamma.md")
    second = projector.rebuild()
    html2 = (Path(second.generation_dir) / "graph.html").read_text(encoding="utf-8")
    assert second.projection_version in html2
    assert html2 != html


# ------------------------------------ watermark discipline and repair


def test_rejects_older_knowledge_watermarks(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    full = projector.rebuild()
    mark = store.knowledge_high_watermark()
    assert mark > 0
    with pytest.raises(StalePublicationError):
        projector.rebuild(upto_knowledge_seq=mark - 1, allow_shrink=True)
    assert store.projection_version() == full.projection_version
    manifest = projector.active_manifest()
    assert manifest is not None and manifest["projection_version"] == full.projection_version


def test_audit_only_events_leave_the_projection_unchanged(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    projector.rebuild()
    pointer_before = (tmp_path / "out" / "CURRENT").read_bytes()
    watermark = store.knowledge_high_watermark()
    audit = AuditLog(store)
    invocation = Invocation.create("knowledge.query", request_hash="read-only")
    audit.begin(invocation)
    audit.complete(invocation.invocation_id, InvocationOutcome(status="success"))
    store.append_domain(
        Event(
            kind=EventKind.ADMIN,
            node_id="KROOT",
            domain=PrimaryDomain.P1,
            knowledge_affecting=False,
            payload={"note": "operator review"},
        ),
        idempotency_key="admin-note",
    )
    assert store.event_high_watermark() > watermark
    assert store.knowledge_high_watermark() == watermark
    assert (tmp_path / "out" / "CURRENT").read_bytes() == pointer_before
    receipt = projector.repair_if_stale()
    assert receipt.repaired is False
    assert receipt.activated is False
    assert (tmp_path / "out" / "CURRENT").read_bytes() == pointer_before


def test_deterministic_rebuild_from_replay_at_a_fixed_watermark(tmp_path: Path) -> None:
    store, importer = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    mark = store.knowledge_high_watermark()
    first = projector.rebuild(upto_knowledge_seq=mark)
    (tmp_path / "corpus" / "gamma.md").write_bytes(GAMMA)
    importer.import_file(tmp_path / "corpus" / "gamma.md", domain=PrimaryDomain.P2, source_uri="soul://corpus/gamma.md")
    assert store.knowledge_high_watermark() > mark
    second = projector.rebuild(upto_knowledge_seq=mark)
    assert second.content_hash == first.content_hash
    full = projector.rebuild()
    assert full.content_hash != first.content_hash
    assert full.source_knowledge_high_watermark == store.knowledge_high_watermark()


def test_recover_incomplete_runs_the_projection_repair_hook(tmp_path: Path) -> None:
    store, _ = _populated(tmp_path)
    projector = _projector(store, tmp_path)
    projector.rebuild()
    audit = AuditLog(store)
    invocation = Invocation.create("knowledge.log_event", request_hash="crash", idempotency_key="crash-key")
    audit.begin(invocation)
    gamma = Node(
        node_id="gamma",
        parent_id="P2",
        domain=PrimaryDomain.P2,
        summary="Gamma",
        payload={"source_file": "gamma.md"},
        provenance=("soul://corpus/gamma.md",),
        source_version_id=SourceRegistry(store).active().source_version_id,  # type: ignore[union-attr]
    )
    store.append_domain(
        Event(kind=EventKind.NODE_UPSERT, node_id="gamma", domain=PrimaryDomain.P2, node=gamma),
        idempotency_key="crash-key",
    )
    (tmp_path / "out" / "CURRENT").unlink()

    def _repair() -> None:
        projector.repair_if_stale()

    receipts = audit.recover_incomplete(projection_repair=_repair, stale_after_seconds=0)
    assert len(receipts) == 1
    assert receipts[0].status == "success"
    assert receipts[0].outcome.get("projection_outcome") == "repaired"
    manifest = projector.active_manifest()
    assert manifest is not None
    assert manifest["source_knowledge_high_watermark"] == store.knowledge_high_watermark()


# ---------------------------------------------------------------------------
# Opsi A summary enrichment (T10-1) and R2 write-side publish caps
# ---------------------------------------------------------------------------

ENRICH = (
    b"# Guide\n"
    b"\n"
    b"Intro sentence one. Intro sentence two stays corpus-side.\n"
    b"\n"
    b"## Terms\n"
    b"\n"
    b"- **Alpha Term** -- the first definition.\n"
    b"- **Beta Term** -- the second definition.\n"
)


def _enriched(tmp_path: Path, content: bytes = ENRICH) -> tuple[EventStore, GraphProjector]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "guide.md").write_bytes(content)
    importer.import_file(corpus / "guide.md", domain=PrimaryDomain.P1, source_uri="soul://corpus/guide.md")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    return store, projector


def _labels(tmp_path: Path) -> dict[str, str]:
    return {node["id"]: node["label"] for node in _graph_json(tmp_path)["nodes"]}


def test_projection_enriches_file_summary_with_first_sentence_only(tmp_path: Path) -> None:
    store, projector = _enriched(tmp_path)
    projector.rebuild()
    labels = _labels(tmp_path)
    file_id = file_scoped_id("guide.md")
    # Opsi A: heading + the sanitized, capped FIRST sentence; later sentences
    # stay corpus-side (raw-body containment, see test_service.py's marker).
    assert labels[file_id] == "Guide — Intro sentence one."
    assert "stays corpus-side" not in labels[file_id]


def test_projection_enriches_section_summary_with_defined_term_digest(tmp_path: Path) -> None:
    store, projector = _enriched(tmp_path)
    projector.rebuild()
    labels = _labels(tmp_path)
    section_id = f"guide.{injective_section_key('Terms')}"
    # `**Term** — gloss` leads are the section's own mini-headings: a digest
    # of them is label-like knowledge, capped and in source order.
    assert labels[section_id] == "Terms — Alpha Term; Beta Term"


def test_enrichment_sanitizes_control_bidi_redacts_secrets_and_masks_paths(tmp_path: Path) -> None:
    content = (
        "# Secrets\n\nFirst sentence mentions sk-ABCDEFGHIJKLMNOPQRST and /usr/local/bin/tool and xo\u202exb.\n"
    ).encode("utf-8")
    store, projector = _enriched(tmp_path, content)
    projector.rebuild()
    label = _labels(tmp_path)[file_scoped_id("guide.md")]
    assert "sk-ABCDEFGHIJKLMNOPQRST" not in label
    assert "[REDACTED:openai-style key]" in label
    assert "/usr/local/bin/tool" not in label
    assert "[path]" in label
    assert "\u202e" not in label and "\u200b" not in label
    assert label.startswith("Secrets — ")


def test_enrichment_respects_the_summary_cap_on_fragment_boundaries(tmp_path: Path) -> None:
    long_lead = "Lead " + "x" * 300
    content = f"# Capped\n\n- **{long_lead}** -- too long to fit.\n- **Short** -- fits never.\n".encode()
    store, projector = _enriched(tmp_path, content)
    projector.rebuild()
    label = _labels(tmp_path)[file_scoped_id("guide.md")]
    assert len(label) <= 256
    # The oversized first lead ends the fill: no mid-word cut, no partial term.
    assert label == "Capped"


def test_enrichment_degrades_when_source_is_missing(tmp_path: Path) -> None:
    store, projector = _enriched(tmp_path)
    (tmp_path / "corpus" / "guide.md").unlink()
    projector.rebuild()
    labels = _labels(tmp_path)
    assert labels[file_scoped_id("guide.md")] == "Guide"
    assert labels[f"guide.{injective_section_key('Terms')}"] == "Terms"


def test_enrichment_degrades_when_source_drifts_from_its_recorded_hash(tmp_path: Path) -> None:
    store, projector = _enriched(tmp_path)
    (tmp_path / "corpus" / "guide.md").write_bytes(b"# Guide\n\nTampered body, never re-imported.\n")
    projector.rebuild()
    labels = _labels(tmp_path)
    assert labels[file_scoped_id("guide.md")] == "Guide"
    assert "Tampered" not in labels[file_scoped_id("guide.md")]


def test_enriched_rebuild_is_byte_deterministic(tmp_path: Path) -> None:
    store, projector = _enriched(tmp_path)
    first = projector.rebuild()
    second = projector.rebuild()
    assert second.content_hash == first.content_hash
    assert second.projection_version == first.projection_version


def test_enriched_labels_feed_the_lexical_index(tmp_path: Path) -> None:
    """The enriched summary is what the query router/ranker reads (T10-1)."""
    store, projector = _enriched(tmp_path)
    projector.rebuild()
    graph = _graph_json(tmp_path)
    router = PrimaryRouter(graph["nodes"], BudgetedTraversal.from_graph_json(graph).links)
    # "beta" exists only in the source body, never in any heading: it reaches
    # the index solely through the enriched summary.
    outcome = router.route(domain=None, purpose="which node defines the beta term?", context_tags=())
    assert outcome.status == "ranked"
    assert outcome.domain is PrimaryDomain.P1


# --------------------------------------------------------------- R2 caps


def test_oversized_node_summary_fails_typed_before_building(tmp_path: Path) -> None:
    """R2: a 70 KB store summary can no longer wedge rebuilds behind a bare
    security error — the write-side cap raises a typed error naming the node,
    and no generation is published."""
    store, projector = _enriched(tmp_path)
    assert projector.rebuild().activated
    active_before = (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8")
    bloated = Node(
        node_id="bloated",
        parent_id="P1",
        domain=PrimaryDomain.P1,
        summary="x" * 70_000,
        payload={"source_file": "soul://operator"},
        provenance=("soul://operator",),
        source_version_id=SourceRegistry(store).active().source_version_id,  # type: ignore[union-attr]
    )
    store.append_domain(
        Event(kind=EventKind.NODE_UPSERT, node_id="bloated", domain=PrimaryDomain.P1, node=bloated),
        idempotency_key="bloated",
    )
    with pytest.raises(OversizedArtifactError, match="bloated"):
        projector.rebuild()
    # Fail-closed with no torn publication: the previous generation serves.
    assert (tmp_path / "out" / "CURRENT").read_text(encoding="utf-8") == active_before
    assert projector.active_manifest() is not None


def test_artifact_size_cap_is_fail_closed_and_typed(tmp_path: Path) -> None:
    """R2 probe: an artifact over the publish cap is rejected typed, at
    assembly time — before any byte of the generation reaches disk; a normal
    artifact under the cap publishes fine."""
    store, _ = _enriched(tmp_path)
    capped = GraphProjector(store, output_root=tmp_path / "out", max_artifact_bytes=64)
    with pytest.raises(OversizedArtifactError, match="byte"):
        capped.rebuild()
    assert not (tmp_path / "out" / "CURRENT").exists()
    normal = GraphProjector(store, output_root=tmp_path / "out")
    receipt = normal.rebuild()
    assert receipt.activated
    assert normal.active_manifest() is not None


def test_write_if_changed_rechecks_the_cap_at_the_write_point(tmp_path: Path) -> None:
    store, _ = _enriched(tmp_path)
    projector = GraphProjector(store, output_root=tmp_path / "out", max_artifact_bytes=16)
    with pytest.raises(OversizedArtifactError):
        projector._write_if_changed(tmp_path / "out" / "graph.json", b"x" * 32)
    assert not (tmp_path / "out" / "graph.json").exists()

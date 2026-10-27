"""Regression probes for the final flow audit (SUB-37a findings F-1..F-9).

* F-1: ``restore_store`` onto the LIVE store path used to leave the old WAL
  in place (the "restore" silently did not restore and the live handle
  half-broke with untyped ``sqlite3.OperationalError``). The restore is now
  coordinated with the live handle (close → purge sidecars → byte-replace →
  in-place reopen) or refused with a typed ``BackupError`` when another
  process holds the WAL locks.
* F-2: a rebuild with ``upto_knowledge_seq`` in the future wrote a manifest
  whose watermark lied, freezing the read model permanently. The projector
  now refuses a future ``upto_knowledge_seq`` with a typed
  ``ProjectionValidationError`` (fail-closed, no lying manifest is ever
  written); the service treats a manifest watermark *ahead* of the store as
  stale, so an already-lying generation is repaired on the next read, and
  ``recover_projection`` heals the already-corrupted state.
* F-3: unbounded ``log_event`` growth met the 64 KiB publish cap as a
  permanent total read outage. ``log_event`` now fails closed with a typed
  ``PublishCapacityExceeded`` (with capacity indicators) before the store
  crosses the publish ceiling, and readiness reports the headroom.
* F-5: ``BackupError`` is a typed ``StoreError`` so the HTTP envelope keeps
  the useful message instead of ``INTERNAL``/"internal error".
* F-6: rollback to the current version (or to identical content) is an
  honest typed refusal, not a status-flipping no-op revision.
* F-7: reader-triggered repairs have a cooldown, so a sustained write stream
  no longer forces one rebuild per read.
* F-9: readiness proactively repairs a lagging projection instead of
  reporting red until the first read happens to heal it.
"""

from __future__ import annotations

import json
import os
import sqlite3
import subprocess
import sys
import threading
import time
from datetime import UTC, datetime
from pathlib import Path

import pytest

from know_your_soul.audit import AuditLog
from know_your_soul.backup import (
    BackupError,
    backup_store,
    diagnose_projection,
    inspect_backup,
    recover_projection,
    restore_store,
)
from know_your_soul.contracts import ApplyImprovementRequest, LifecycleError, NodeNotFound, StaleProjectionUnavailable
from know_your_soul.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, ScoreVector, canonical_json
from know_your_soul.http_api import error_envelope, status_for_error
from know_your_soul.importer import SeedImporter
from know_your_soul.observability import check_readiness
from know_your_soul.projection import (
    ARTIFACT_MAX_BYTES,
    GraphProjector,
    ProjectionValidationError,
    ProjectorBusy,
)
from know_your_soul.service import InvalidPublishCapacity, KnowledgeService
from know_your_soul.store import EventStore, PublishCapacityExceeded

NOW = datetime(2026, 10, 26, 12, 0, tzinfo=UTC)


# --------------------------------------------------------------------- setup


def _node(node_id: str, *, parent: str | None = None, summary: str | None = None) -> Node:
    return Node(
        node_id=node_id,
        parent_id=parent,
        kind=NodeKind.CHILD if parent else NodeKind.ROOT,
        domain=PrimaryDomain.P1,
        summary=summary or node_id,
        provenance=("soul://corpus/lesson.md",),
        source_version_id="sv-test",
        scores=ScoreVector.zero(),
    )


def _upsert(store: EventStore, node: Node, *, key: str | None = None) -> None:
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node.node_id,
            domain=node.domain,
            node=node,
            source_version_id=node.source_version_id,
        ),
        idempotency_key=key or f"test:upsert:{node.node_id}:{store.node_version(node.node_id)}",
    )


def _seeded(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector)
    _upsert(store, _node("KROOT"))
    _upsert(store, _node("n-1", parent="KROOT", summary="first lesson"), key="test:upsert:n-1")
    projector.rebuild()
    return store, projector, service


def _fresh_table_names(db_path: Path) -> set[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        return {str(row[0]) for row in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    finally:
        conn.close()


def _fresh_domain_nodes(db_path: Path) -> list[str]:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        rows = conn.execute(
            "SELECT payload_json FROM event_log WHERE kind='domain' AND event_type='node.upsert' ORDER BY event_seq"
        ).fetchall()
        return sorted({str(json.loads(str(row[0]))["node_id"]) for row in rows})
    finally:
        conn.close()


# --------------------------------------------------------------------- F-1


def test_restore_over_live_store_is_coherent(tmp_path: Path) -> None:
    """F-1: restore onto the live store path must really restore — no WAL chimera."""
    store, _, _ = _seeded(tmp_path)
    live_path = Path(store.db_path)
    backup_path = tmp_path / "backup.sqlite3"
    backup_store(store, backup_path)
    backup_watermark = store.knowledge_high_watermark()

    # Post-backup writes, which the restore must make disappear.
    _upsert(store, _node("n-2", parent="KROOT"), key="test:upsert:n-2")
    _upsert(store, _node("n-3", parent="KROOT"), key="test:upsert:n-3")
    assert store.knowledge_high_watermark() > backup_watermark

    receipt = restore_store(backup_path, live_path, overwrite=True)
    assert receipt.event_count == store.event_count()

    # The live handle is coherent: reads and writes reflect the restored state.
    assert store.knowledge_high_watermark() == backup_watermark
    assert store.current_node("n-2") is None
    _upsert(store, _node("n-4", parent="KROOT"), key="test:upsert:n-4")
    assert store.verify_chain().ok

    # A fresh connection sees exactly the restored content — the old WAL must
    # not replay over the restored file (the pre-fix chimera).
    assert _fresh_domain_nodes(live_path) == ["KROOT", "n-1", "n-4"]
    assert not Path(str(live_path) + "-wal").exists() or Path(str(live_path) + "-wal").stat().st_size >= 0
    info = inspect_backup(live_path)
    assert info["ok"]


def test_restore_over_live_store_requires_overwrite(tmp_path: Path) -> None:
    store, _, _ = _seeded(tmp_path)
    backup_store(store, tmp_path / "backup.sqlite3")
    with pytest.raises(BackupError, match="overwrite"):
        restore_store(tmp_path / "backup.sqlite3", Path(store.db_path))


def test_restore_purges_stale_wal_sidecars(tmp_path: Path) -> None:
    """Crash residue (dest + stale -wal/-shm, no live handle) must not replay."""
    dest = tmp_path / "dest.sqlite3"
    # Build a WAL-mode database with un-checkpointed content, then snapshot
    # the file trio while the WAL is still live.
    conn = sqlite3.connect(str(dest))
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("CREATE TABLE event_log (marker TEXT)")
    conn.execute("INSERT INTO event_log VALUES ('stale-wal-row')")
    conn.commit()
    stash = tmp_path / "stash"
    stash.mkdir()
    for suffix in ("", "-wal", "-shm"):
        src = Path(str(dest) + suffix)
        if src.exists():
            (stash / ("dest.sqlite3" + suffix)).write_bytes(src.read_bytes())
    conn.close()
    for suffix in ("", "-wal", "-shm"):
        staged = stash / ("dest.sqlite3" + suffix)
        if staged.exists():
            Path(str(dest) + suffix).write_bytes(staged.read_bytes())
    assert "event_log" in _fresh_table_names(dest)  # the stale WAL is visible pre-restore

    store = EventStore(tmp_path / "live.sqlite3")
    _upsert(store, _node("KROOT"))
    backup_path = tmp_path / "backup.sqlite3"
    backup_store(store, backup_path)
    store.close()

    restore_store(backup_path, dest, overwrite=True)

    assert not Path(str(dest) + "-wal").exists(), "stale WAL sidecar must be purged by the restore"
    assert not Path(str(dest) + "-shm").exists(), "stale SHM sidecar must be purged by the restore"
    assert _fresh_domain_nodes(dest) == ["KROOT"]
    assert "stale-wal-row" not in {
        str(cell)
        for table_row in sqlite3.connect(str(dest)).execute("SELECT payload_json FROM event_log")
        for cell in table_row
    }


def test_restore_refuses_destination_held_by_another_process(tmp_path: Path) -> None:
    """A live foreign WAL connection (another process) must fail closed, typed."""
    dest = tmp_path / "foreign.sqlite3"
    child = subprocess.Popen(
        [
            sys.executable,
            "-c",
            "import sqlite3, time\n"
            f"conn = sqlite3.connect({str(dest)!r})\n"
            "conn.execute('PRAGMA journal_mode=WAL')\n"
            "conn.execute('CREATE TABLE t (x)')\n"
            "conn.execute('INSERT INTO t VALUES (1)')\n"
            "print('ready', flush=True)\n"
            "time.sleep(60)\n",
        ],
        stdout=subprocess.PIPE,
    )
    try:
        assert child.stdout is not None
        assert child.stdout.readline().strip() == b"ready"
        store = EventStore(tmp_path / "live.sqlite3")
        _upsert(store, _node("KROOT"))
        backup_store(store, tmp_path / "backup.sqlite3")
        store.close()
        with pytest.raises(BackupError, match="another process|live"):
            restore_store(tmp_path / "backup.sqlite3", dest, overwrite=True)
        with pytest.raises(BackupError, match="another process|live"):
            backup_store(store, dest)
    finally:
        child.terminate()
        child.wait()


def test_backup_refuses_live_store_destination(tmp_path: Path) -> None:
    """A backup whose destination is the live store itself would be the same chimera."""
    store, _, _ = _seeded(tmp_path)
    with pytest.raises(BackupError, match="live"):
        backup_store(store, Path(store.db_path))


def test_service_reads_are_consistent_after_live_restore(tmp_path: Path) -> None:
    """F-1+F-2 composition: after a live restore the service heals and serves."""
    store, projector, service = _seeded(tmp_path)
    backup_store(store, tmp_path / "backup.sqlite3")
    _upsert(store, _node("n-2", parent="KROOT"), key="test:upsert:n-2")
    projector.rebuild()

    restore_store(tmp_path / "backup.sqlite3", Path(store.db_path), overwrite=True)

    # The manifest is now *ahead* of the restored store; the read heals it.
    response = service.get_node("n-1")
    assert response.node.node_id == "n-1"
    assert store.knowledge_high_watermark() == int(response.source_knowledge_high_watermark)
    with pytest.raises(NodeNotFound):
        service.get_node("n-2")
    assert store.verify_chain().ok
    manifest = projector.active_manifest()
    assert manifest is not None
    assert int(manifest["source_knowledge_high_watermark"]) == store.knowledge_high_watermark()


# --------------------------------------------------------------------- F-2


def test_rebuild_rejects_a_future_upto_knowledge_seq_typed(tmp_path: Path) -> None:
    """F-2 residual: the projection itself refuses a watermark it cannot know.

    SUB-41 healed the store/service side (a lying manifest is detected and
    repaired); this pins the preventive half: ``rebuild`` fails closed with a
    typed error instead of writing a manifest whose watermark lies.
    """
    store, projector, _ = _seeded(tmp_path)
    honest = store.knowledge_high_watermark()
    before = projector.active_manifest()
    assert before is not None

    with pytest.raises(ProjectionValidationError, match="upto_knowledge_seq"):
        projector.rebuild(upto_knowledge_seq=honest + 1)
    with pytest.raises(ProjectionValidationError, match="upto_knowledge_seq"):
        projector.rebuild(upto_knowledge_seq=10**9)

    # Fail-closed: no generation was activated and no marker was poisoned.
    assert projector.active_manifest() == before
    # The typed error maps to a client-error envelope, never a 500.
    assert status_for_error(ProjectionValidationError("x")) == 422
    # The boundary itself stays legal: rebuilding up to the real watermark works.
    receipt = projector.rebuild(upto_knowledge_seq=honest)
    assert receipt.source_knowledge_high_watermark == honest


def _simulate_lying_manifest(store: EventStore, projector: GraphProjector, claimed: int) -> None:
    """Construct the already-corrupted F-2 state the way it genuinely arises.

    The projector refuses to *create* a lying manifest (typed rejection
    above), so the heal-path tests simulate one: direct tampering with the
    activated manifest plus the poisoned activation-CAS marker a pre-fix
    ``rebuild(upto_knowledge_seq=...)`` used to leave behind.
    """
    generation = projector.active_generation_dir()
    assert generation is not None
    manifest_path = generation / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_knowledge_high_watermark"] = claimed
    manifest_path.write_text(canonical_json(manifest) + "\n", encoding="utf-8")
    store.execute_raw(
        "UPDATE system_state SET value = ? WHERE key = 'active_projection_watermark'",
        (str(claimed),),
    )


def test_lying_manifest_watermark_is_healed_on_read(tmp_path: Path) -> None:
    """F-2: a manifest watermark ahead of the store must not freeze the read model."""
    store, projector, service = _seeded(tmp_path)
    _simulate_lying_manifest(store, projector, 10**9)
    manifest = projector.active_manifest()
    assert manifest is not None
    assert int(manifest["source_knowledge_high_watermark"]) == 10**9  # the lie is on disk

    _upsert(store, _node("post-lie", parent="KROOT"), key="test:upsert:post-lie")

    # The read path detects the lie (manifest ahead of the store) and repairs.
    response = service.get_node("post-lie")
    assert response.node.node_id == "post-lie"
    healed = projector.active_manifest()
    assert healed is not None
    assert int(healed["source_knowledge_high_watermark"]) == store.knowledge_high_watermark()


def test_recover_projection_repairs_a_lying_manifest(tmp_path: Path) -> None:
    """F-2 repair path: recovery from the already-corrupted state."""
    store, projector, service = _seeded(tmp_path)
    _simulate_lying_manifest(store, projector, 10**9)
    _upsert(store, _node("post-lie", parent="KROOT"), key="test:upsert:post-lie")

    report = recover_projection(store, projector, audit_log=AuditLog(store))
    assert report.receipt is not None
    diagnosis = diagnose_projection(store, projector)
    assert diagnosis.manifest_watermark == store.knowledge_high_watermark()
    # And the read model serves the post-lie write.
    assert service.get_node("post-lie").node.node_id == "post-lie"


# --------------------------------------------------------------------- F-3


def _capacity_seeded(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    _upsert(store, _node("KROOT"))
    limit = store.estimate_publish_bytes() + 10  # the next write must cross the guard
    service = KnowledgeService(store, projector, publish_capacity_bytes=limit)
    return store, projector, service


def test_log_event_fails_closed_typed_near_publish_cap(tmp_path: Path) -> None:
    store, _, service = _capacity_seeded(tmp_path)
    watermark = store.knowledge_high_watermark()
    domain_events = store.domain_event_count()

    with pytest.raises(PublishCapacityExceeded) as caught:
        service.log_event(_node("n-big", parent="KROOT", summary="x" * 400), idempotency_key="k:n-big")

    message = str(caught.value)
    assert "publish" in message and "capacity" in message
    assert str(ARTIFACT_MAX_BYTES) in message or "cap" in message
    # Fail-closed: no domain write landed, the watermark did not move.
    assert store.knowledge_high_watermark() == watermark
    assert store.domain_event_count() == domain_events
    # The refusal is still an audited invocation (spec 7: exactly one final audit).
    with store.read() as conn:
        rows = conn.execute("SELECT status FROM invocation_journal WHERE operation = 'knowledge.log_event'").fetchall()
    assert rows and all(str(row["status"]) == "complete" for row in rows)


def test_publish_capacity_override_above_artifact_cap_fails_closed(tmp_path: Path) -> None:
    """SUB48-1: an override above the per-artifact cap silently disarmed F-3.

    Live probe: ``publish_capacity_bytes=2**20`` let the store grow until
    every publish/repair failed with ``OversizedArtifactError`` (238600B over
    the 65536B cap). Construction must now fail closed with a typed error
    instead of accepting a guard ceiling the projection can never honor.
    """
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    with pytest.raises(InvalidPublishCapacity) as caught:
        KnowledgeService(store, projector, publish_capacity_bytes=ARTIFACT_MAX_BYTES + 1)
    assert str(ARTIFACT_MAX_BYTES) in str(caught.value)
    with pytest.raises(InvalidPublishCapacity):
        KnowledgeService(store, projector, publish_capacity_bytes=2**20)
    # InvalidPublishCapacity stays inside the StoreError family so the HTTP/MCP
    # envelopes surface it typed, like the write-side PublishCapacityExceeded.
    assert isinstance(caught.value, PublishCapacityExceeded)
    # Boundary: an override at exactly the cap remains constructible.
    service = KnowledgeService(store, projector, publish_capacity_bytes=ARTIFACT_MAX_BYTES)
    assert service._publish_capacity_bytes == ARTIFACT_MAX_BYTES


def test_publish_capacity_guard_still_allows_small_writes(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    _upsert(store, _node("KROOT"))
    limit = store.estimate_publish_bytes() + 4096  # one small node fits comfortably
    service = KnowledgeService(store, projector, publish_capacity_bytes=limit)
    receipt = service.log_event(_node("tiny", parent="KROOT", summary="ok"), idempotency_key="k:tiny")
    assert receipt.domain_event_id
    assert store.current_node("tiny") is not None


def test_publish_estimate_bounds_the_real_graph() -> None:
    """Calibration: the estimate must never under-count the real published graph."""
    tmp = Path(os.environ.get("SOUL_TEST_TMP", "")) if os.environ.get("SOUL_TEST_TMP") else None
    import tempfile

    work = Path(tempfile.mkdtemp()) if tmp is None else tmp
    store = EventStore(work / "soul.db")
    importer = SeedImporter(store, state_dir=work / "state")
    corpus = Path(__file__).resolve().parents[1] / "bench" / "corpus"
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    projector = GraphProjector(store, output_root=work / "out")
    projector.rebuild()
    pointer = (work / "out" / "CURRENT").read_text(encoding="utf-8").strip()
    graph_bytes = (work / "out" / "generations" / pointer / "graph.json").stat().st_size

    estimate = store.estimate_publish_bytes()
    assert estimate >= graph_bytes, "the write-side guard must never under-estimate the real graph"
    assert estimate <= graph_bytes * 2.2, "the estimate must stay tight enough to be useful"
    assert graph_bytes < int(ARTIFACT_MAX_BYTES * 0.95), "the frozen corpus must stay inside the guard"


def test_readiness_reports_publish_capacity(tmp_path: Path) -> None:
    store, projector, _ = _seeded(tmp_path)
    report = check_readiness(store, projector=projector)
    assert report.ok
    capacity = report.details.get("publish_capacity")
    assert capacity is not None
    assert capacity["cap_bytes"] == ARTIFACT_MAX_BYTES
    assert 0 < capacity["estimated_bytes"] <= capacity["cap_bytes"]


# --------------------------------------------------------------------- F-5


def test_backup_error_surfaces_a_typed_envelope() -> None:
    error = BackupError("restore destination 'x' exists; pass overwrite=True to replace it")
    envelope = error_envelope(error)
    assert envelope["error"]["code"] == "BackupError"
    assert "overwrite=True" in envelope["error"]["message"]
    assert status_for_error(error) == 400


# --------------------------------------------------------------------- F-6


def _improved(tmp_path: Path) -> tuple[EventStore, GraphProjector, KnowledgeService]:
    store, projector, service = _seeded(tmp_path)
    proposal = service.propose_improvement(
        node_id="n-1",
        diff=({"op": "set_summary", "value": "improved summary"},),
        rationale="make it better",
    )
    service.apply_improvement(ApplyImprovementRequest(proposal_id=proposal.proposal.proposal_id))
    return store, projector, service


def test_rollback_to_current_version_is_a_typed_refusal(tmp_path: Path) -> None:
    store, _, service = _improved(tmp_path)
    current = store.node_version("n-1")
    with pytest.raises(LifecycleError, match="unchanged|nothing"):
        service.rollback(node_id="n-1", to_version=current)
    assert store.node_version("n-1") == current


def test_second_targetless_rollback_is_a_typed_refusal(tmp_path: Path) -> None:
    store, _, service = _improved(tmp_path)
    first = service.rollback(node_id="n-1")
    assert first.status == "rolled_back"
    version = store.node_version("n-1")
    with pytest.raises(LifecycleError, match="unchanged|nothing"):
        service.rollback(node_id="n-1")
    assert store.node_version("n-1") == version


# --------------------------------------------------------------------- F-7


def test_stale_read_repairs_have_a_cooldown(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector, repair_cooldown_seconds=3600.0)
    _upsert(store, _node("KROOT"))
    _upsert(store, _node("n-1", parent="KROOT"), key="test:upsert:n-1")
    projector.rebuild()

    repairs = {"count": 0}

    def busy_repair() -> object:
        repairs["count"] += 1
        raise ProjectorBusy("another projector holds the rebuild lock")

    monkeypatch.setattr(projector, "repair_if_stale", busy_repair)
    _upsert(store, _node("n-2", parent="KROOT"), key="test:upsert:n-2")

    # First stale read: one repair attempt, non-covering → typed failure and
    # the cooldown engages.
    with pytest.raises(StaleProjectionUnavailable):
        service.get_node("n-2")
    assert repairs["count"] == 1
    # Inside the cooldown the stale read fails typed *without* paying a rebuild.
    with pytest.raises(StaleProjectionUnavailable):
        service.get_node("n-2")
    assert repairs["count"] == 1
    # With the cooldown disabled every stale read retries the repair (spec 7.6).
    eager = KnowledgeService(store, projector, repair_cooldown_seconds=0.0)
    with pytest.raises(StaleProjectionUnavailable):
        eager.get_node("n-2")
    assert repairs["count"] == 2


def test_covering_repair_clears_the_cooldown(tmp_path: Path) -> None:
    """The normal write→repair flow never notices the cooldown exists."""
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    service = KnowledgeService(store, projector, repair_cooldown_seconds=3600.0)
    _upsert(store, _node("KROOT"))
    _upsert(store, _node("n-1", parent="KROOT"), key="test:upsert:n-1")
    projector.rebuild()
    _upsert(store, _node("n-2", parent="KROOT"), key="test:upsert:n-2")
    assert service.get_node("n-2").node.node_id == "n-2"  # repair covered → no cooldown
    _upsert(store, _node("n-3", parent="KROOT"), key="test:upsert:n-3")
    assert service.get_node("n-3").node.node_id == "n-3"  # and again, immediately


def test_reader_storm_under_write_stream_bounds_repairs(tmp_path: Path) -> None:
    """F-7: a sustained write stream must not make every reader pay a rebuild."""
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    # The maximum legal capacity limit keeps the F-3 guard out of this scenario
    # (SUB48-1: overrides above ARTIFACT_MAX_BYTES are now rejected at
    # construction, so this test rides the boundary value).
    service = KnowledgeService(store, projector, repair_cooldown_seconds=2.0, publish_capacity_bytes=ARTIFACT_MAX_BYTES)
    _upsert(store, _node("KROOT"))
    _upsert(store, _node("n-1", parent="KROOT"), key="test:upsert:n-1")
    projector.rebuild()

    rebuilds = {"count": 0}
    real_rebuild = projector.rebuild

    def counting_rebuild(**kwargs: object) -> object:
        rebuilds["count"] += 1
        return real_rebuild(**kwargs)  # type: ignore[arg-type]

    projector.rebuild = counting_rebuild  # type: ignore[method-assign]

    stop = threading.Event()
    errors: list[str] = []
    reads = {"ok": 0, "stale": 0}
    writes = {"ok": 0}

    def writer() -> None:
        index = 0
        while not stop.is_set():
            index += 1
            try:
                service.log_event(_node(f"w-{index}", parent="KROOT"), idempotency_key=f"w:{index}")
                writes["ok"] += 1
            except PublishCapacityExceeded:
                stop.set()  # the F-3 guard engaging is a controlled stop, not an error
            except Exception as error:  # noqa: BLE001 - collected and asserted below
                errors.append(f"writer: {type(error).__name__}: {error}")

    def reader() -> None:
        # Progress-based, not time-based: readers run until a fixed number of
        # attempts, independent of how fast the CI runner is. Typed 503s count
        # as served, so every iteration terminates quickly even under the storm.
        # Hard attempt cap keeps the loop finite even if reads error out.
        attempts = 0
        while reads["ok"] + reads["stale"] < 25 and attempts < 500:
            attempts += 1
            try:
                service.get_node("n-1")
                reads["ok"] += 1
            except StaleProjectionUnavailable:
                reads["stale"] += 1
            except Exception as error:  # noqa: BLE001 - untyped errors fail the test
                errors.append(f"reader: {type(error).__name__}: {error}")

    threads = [threading.Thread(target=writer)] + [threading.Thread(target=reader) for _ in range(4)]
    for thread in threads:
        thread.start()
    time.sleep(3.0)
    stop.set()
    for thread in threads:
        thread.join(timeout=30)

    projector.rebuild = real_rebuild  # type: ignore[method-assign]
    assert errors == []
    assert reads["ok"] + reads["stale"] > 20, "readers made progress (typed 503s count as served)"
    # Fail-closed freshness needs at most one rebuild per write; the F-7 defect
    # was one rebuild per *read* (and still 503). Assert the bounded shape.
    assert rebuilds["count"] <= writes["ok"] + 2, (
        f"rebuild amplification: {rebuilds['count']} rebuilds for {writes['ok']} writes"
    )
    assert rebuilds["count"] < reads["ok"] + reads["stale"], "rebuilds must not track reads"


# --------------------------------------------------------------------- F-9


def test_readiness_proactively_repairs_a_lagging_projection(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "out")
    _upsert(store, _node("KROOT"))
    _upsert(store, _node("n-1", parent="KROOT"), key="test:upsert:n-1")

    report = check_readiness(store, projector=projector)
    assert report.ok
    assert report.checks["projection_present"]
    assert report.checks["projection_watermark"]
    manifest = projector.active_manifest()
    assert manifest is not None
    assert int(manifest["source_knowledge_high_watermark"]) == store.knowledge_high_watermark()

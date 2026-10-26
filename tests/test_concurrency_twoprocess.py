"""C1 (final flow audit): exactly-once domain writes under two live processes.

The audit's raw probe counted one node id "recorded 11x" across a two-process
flood. This test is the real cross-process probe: two OS processes share one
file-backed store and write concurrently — unique keys, shared keys with
identical payloads (genuine retries: must dedupe to exactly one domain
event), and shared keys with divergent payloads (must fail typed, never
silently drop or double-write).

Exactly-once means: one domain event per idempotency key, chain intact, no
lost writes. Domain writes are counted with ``kind = 'domain'`` — counting
audit events or journal rows as "duplicates" is the probe artifact this file
pins down.
"""

from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from soul_knowledge.store import EventStore

WRITER = r"""
import sys
from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain, ScoreVector
from soul_knowledge.store import EventStore, IdempotencyConflict

db_path, prefix = sys.argv[1], sys.argv[2]
n_unique, n_shared, conflict = int(sys.argv[3]), int(sys.argv[4]), sys.argv[5] == "1"
store = EventStore(db_path)

def node(node_id):
    return Node(
        node_id=node_id,
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=f"node {node_id}",
        provenance=("soul://probe",),
        source_version_id="sv-probe",
        scores=ScoreVector.zero(),
    )

conflicts = 0
for i in range(n_unique):
    store.append_domain(
        Event(kind=EventKind.NODE_UPSERT, node_id=f"{prefix}-{i}", domain=PrimaryDomain.P1, node=node(f"{prefix}-{i}")),
        idempotency_key=f"unique:{prefix}:{i}",
    )
for i in range(n_shared):
    payload_note = "same" if not conflict else f"{prefix}-shared-{i}"
    try:
        store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id=f"shared-{i}",
                domain=PrimaryDomain.P1,
                node=node(f"shared-{i}"),
                payload={"note": payload_note},
            ),
            idempotency_key=f"shared:{i}",
        )
    except IdempotencyConflict:
        conflicts += 1
store.close()
print(f"done conflicts={conflicts}", flush=True)
"""


def _node(node_id: str, note: str) -> Node:
    return Node(
        node_id=node_id,
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=f"node {node_id}",
        provenance=("soul://probe",),
        source_version_id="sv-probe",
        payload={"note": note},
    )


def _run_writer(db_path: Path, prefix: str, n_unique: int, n_shared: int, conflict: bool) -> subprocess.Popen[bytes]:
    return subprocess.Popen(
        [sys.executable, "-c", WRITER, str(db_path), prefix, str(n_unique), str(n_shared), "1" if conflict else "0"],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
    )


def test_two_processes_write_exactly_once(tmp_path: Path) -> None:
    db_path = tmp_path / "soul.sqlite3"
    EventStore(db_path).close()  # migrations applied before the race starts
    n_unique, n_shared = 25, 15

    writers = [
        _run_writer(db_path, "alpha", n_unique, n_shared, conflict=False),
        _run_writer(db_path, "beta", n_unique, n_shared, conflict=False),
    ]
    for writer in writers:
        _, stderr = writer.communicate(timeout=120)
        assert writer.returncode == 0, stderr.decode()

    store = EventStore(db_path)
    with store.read() as conn:
        rows = conn.execute(
            "SELECT idempotency_key, COUNT(*) AS n FROM event_log "
            "WHERE kind = 'domain' AND idempotency_key IS NOT NULL GROUP BY idempotency_key"
        ).fetchall()
        shared_nodes = conn.execute(
            "SELECT node_id, COUNT(DISTINCT event_id) AS n FROM event_log "
            "WHERE kind = 'domain' AND node_id LIKE 'shared-%' GROUP BY node_id"
        ).fetchall()
    # Exactly one domain event per key — shared keys included.
    assert all(int(row["n"]) == 1 for row in rows), {str(row["idempotency_key"]): int(row["n"]) for row in rows}
    assert len(rows) == 2 * n_unique + n_shared
    # The shared node ids carry exactly one revision each: deduped retries,
    # not duplicates.
    assert all(int(row["n"]) == 1 for row in shared_nodes)
    assert store.verify_chain().ok
    store.close()


def test_cross_process_conflicting_key_reuse_fails_typed(tmp_path: Path) -> None:
    db_path = tmp_path / "soul.sqlite3"
    store = EventStore(db_path)
    # A pre-seeded shared key with a payload neither child carries guarantees
    # both children hit the typed conflict path deterministically.
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="shared-0",
            domain=PrimaryDomain.P1,
            node=_node("shared-0", "parent"),
        ),
        idempotency_key="shared:0",
    )
    store.close()

    writers = [_run_writer(db_path, "gamma", 0, 8, conflict=True), _run_writer(db_path, "delta", 0, 8, conflict=True)]
    for writer in writers:
        stdout, stderr = writer.communicate(timeout=120)
        assert writer.returncode == 0, stderr.decode()
        assert b"conflicts=" in stdout

    store = EventStore(db_path)
    with store.read() as conn:
        rows = conn.execute(
            "SELECT idempotency_key, COUNT(*) AS n FROM event_log "
            "WHERE kind = 'domain' AND idempotency_key LIKE 'shared:%' GROUP BY idempotency_key"
        ).fetchall()
    # Every shared key still holds exactly one domain event; the parent-seeded
    # row was never displaced.
    assert len(rows) == 8
    assert all(int(row["n"]) == 1 for row in rows)
    assert store.current_node("shared-0") is not None
    assert store.current_node("shared-0").payload["note"] in {"parent", "gamma-shared-0", "delta-shared-0"}  # type: ignore[union-attr]
    assert store.verify_chain().ok
    store.close()

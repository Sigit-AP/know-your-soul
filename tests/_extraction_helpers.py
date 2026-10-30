"""Shared fixtures/builders for the contract-v4 extraction tests (SUB-106).

The seed layout mirrors ``tests/_serve_helpers.py``: a file-backed store, a
projector, a service and an audit log, plus a real file under a scratch
extraction root so provenance binding tests hash real bytes.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any

from know_your_soul.audit import AuditLog
from know_your_soul.projection import GraphProjector
from know_your_soul.service import KnowledgeService
from know_your_soul.store import EventStore

SOURCE_BYTES = b"page 1: the graft procedure requires two witnesses.\n"
SOURCE_SHA256 = hashlib.sha256(SOURCE_BYTES).hexdigest()
SOURCE_BYTES_B = b"field notes: the south graft failed twice in march.\n"
SOURCE_SHA256_B = hashlib.sha256(SOURCE_BYTES_B).hexdigest()

EXTRACTED_AT = "2026-10-30T12:00:00+00:00"


def make_payload(
    *,
    text: str = "the graft procedure requires two witnesses",
    nodes: list[dict[str, Any]] | None = None,
    edges: list[dict[str, Any]] | None = None,
) -> dict[str, Any]:
    return {
        "text": text,
        "nodes": nodes
        if nodes is not None
        else [
            {"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "needs two witnesses"},
            {"name": "Witness", "kind": "leaf", "domain": "P3", "summary": "a procedure participant"},
        ],
        "edges": edges
        if edges is not None
        else [
            {
                "relation": "depends_on",
                "source": {"kind": "child", "name": "Graft procedure"},
                "target": {"kind": "leaf", "name": "Witness"},
            }
        ],
    }


def make_method(
    *, agent: str = "claude-opus-4.7", prompt_id: str | None = "scan-v1", tool: str | None = "vision"
) -> dict[str, Any]:
    method: dict[str, Any] = {"agent": agent, "extracted_at": EXTRACTED_AT}
    if prompt_id is not None:
        method["prompt_id"] = prompt_id
    if tool is not None:
        method["tool"] = tool
    return method


def make_args(
    *,
    source_sha256: Any = SOURCE_SHA256,
    source_path: Any = None,
    payload: dict[str, Any] | None = None,
    method: dict[str, Any] | None = None,
    confidence: Any = "0.8",
) -> dict[str, Any]:
    args: dict[str, Any] = {
        "source_sha256": source_sha256,
        "payload": payload if payload is not None else make_payload(),
        "method": method if method is not None else make_method(),
        "confidence": confidence,
    }
    if source_path is not None:
        args["source_path"] = source_path
    return args


def seed_root(tmp_path: Path) -> Path:
    """A scratch extraction root with two real files whose hashes are known."""
    root = tmp_path / "corpus"
    root.mkdir(parents=True, exist_ok=True)
    (root / "scan-a.txt").write_bytes(SOURCE_BYTES)
    (root / "scan-b.txt").write_bytes(SOURCE_BYTES_B)
    return root


def seed_engine(tmp_path: Path, *, actor: str = "tester"):
    """A store + service + audit + engine over a fresh scratch dir."""
    from know_your_soul.extraction import ExtractionEngine, ExtractionQueue

    store = EventStore(tmp_path / "soul.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "graph")
    service = KnowledgeService(store, projector)
    audit = AuditLog(store)
    queue = ExtractionQueue.for_store(store)
    engine = ExtractionEngine(store, audit, queue=queue)
    return engine, service, store, queue, actor


def audit_events(store: EventStore, operation: str) -> list[dict[str, Any]]:
    """Final audit events for one operation (event_type), oldest first.

    Each payload gains ``audit_event_id`` (the row's event_id) so tests can
    cross-reference the id carried on receipts and typed errors.
    """
    with store.read() as conn:
        rows = conn.execute(
            "SELECT event_id, payload_json FROM event_log WHERE kind = 'audit' AND event_type = ? ORDER BY event_seq",
            (operation,),
        ).fetchall()
    return [{**json.loads(str(row["payload_json"])), "audit_event_id": str(row["event_id"])} for row in rows]


def applied_node(store: EventStore, node_id: str):
    return store.current_node(node_id)

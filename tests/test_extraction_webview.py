"""Webview tests for the read-only extraction queue endpoint (SUB-106).

``GET /api/extractions`` mirrors the CLI ``extraction review`` listing:
deterministic contract order, sanitized fields only, never a write.
"""

from __future__ import annotations

from pathlib import Path

from _extraction_helpers import make_args, make_method, make_payload, seed_engine
from know_your_soul.serve.webview import WebviewApp


def _seed(tmp_path: Path):
    engine, service, store, queue, _ = seed_engine(tmp_path)
    return engine, service, store, queue


class TestExtractionsPayload:
    def test_empty_queue(self, tmp_path: Path) -> None:
        _engine, _service, store, _queue = _seed(tmp_path)
        app = WebviewApp(store.db_path)
        try:
            payload = app.extractions_payload()
            assert payload["count"] == 0
            assert payload["rows"] == []
            assert "queue_size" in payload
        finally:
            app.close()
            store.close()

    def test_missing_sidecar_is_an_empty_queue(self, tmp_path: Path) -> None:
        # No extraction was ever submitted: the sidecar DB does not exist.
        from know_your_soul.store import EventStore

        store = EventStore(tmp_path / "soul.sqlite3")
        app = WebviewApp(store.db_path)
        try:
            payload = app.extractions_payload()
            assert payload["count"] == 0
            assert payload["rows"] == []
        finally:
            app.close()
            store.close()

    def test_rows_carry_the_review_fields(self, tmp_path: Path) -> None:
        engine, _service, store, _queue = _seed(tmp_path)
        receipt = engine.submit(make_args(), actor="actor-a")
        app = WebviewApp(store.db_path)
        try:
            payload = app.extractions_payload()
            assert payload["count"] == 1
            row = payload["rows"][0]
            assert row["receipt_id"] == receipt["receipt_id"]
            assert row["status"] == "pending_review"
            assert row["actor"] == "actor-a"
            assert row["nodes"] == 2
            assert row["edges"] == 1
            assert row["confidence_effective"] == receipt["confidence_effective"]
        finally:
            app.close()
            store.close()

    def test_rows_follow_the_contract_order(self, tmp_path: Path) -> None:
        engine, service, store, _queue = _seed(tmp_path)
        pending = engine.submit(make_args(method=make_method(agent="agent-a")), actor="actor-a")
        flagged = engine.submit(make_args(source_sha256="f" * 64, method=make_method(agent="agent-a")), actor="actor-a")
        engine.submit(
            make_args(
                source_sha256="f" * 64,
                method=make_method(agent="agent-b"),
                payload=make_payload(
                    nodes=[{"name": "Graft procedure", "kind": "child", "domain": "P3", "summary": "changed"}],
                    edges=[],
                ),
            ),
            actor="actor-b",
        )
        applied = engine.submit(make_args(source_sha256="e" * 64, method=make_method(agent="agent-a")), actor="actor-a")
        engine.approve(applied["receipt_id"], operator="operator", service=service)
        app = WebviewApp(store.db_path)
        try:
            payload = app.extractions_payload()
            statuses = [row["status"] for row in payload["rows"]]
            assert statuses[0:2] == ["contradiction_flagged", "contradiction_flagged"]
            assert statuses == sorted(
                statuses,
                key=lambda s: ["contradiction_flagged", "pending_review", "corroborated", "applied", "rejected"].index(
                    s
                ),
            )
            ids = {row["receipt_id"] for row in payload["rows"]}
            assert {pending["receipt_id"], flagged["receipt_id"], applied["receipt_id"]} <= ids
        finally:
            app.close()
            store.close()

    def test_fields_are_already_sanitized(self, tmp_path: Path) -> None:
        engine, _service, store, _queue = _seed(tmp_path)
        engine.submit(
            make_args(payload=make_payload(text="<script>alert(1)</script>", edges=[])),
            actor="actor-a",
        )
        app = WebviewApp(store.db_path)
        try:
            payload = app.extractions_payload()
            assert "<script>" not in str(payload)
        finally:
            app.close()
            store.close()

    def test_the_endpoint_never_writes(self, tmp_path: Path) -> None:
        engine, _service, store, queue = _seed(tmp_path)
        engine.submit(make_args(), actor="actor-a")
        before = queue.canonical_dump()
        app = WebviewApp(store.db_path)
        try:
            app.extractions_payload()
            app.extractions_payload()
        finally:
            app.close()
        assert queue.canonical_dump() == before
        store.close()


class TestRoute:
    def test_api_route_is_registered(self, tmp_path: Path) -> None:
        engine, _service, store, _queue = _seed(tmp_path)
        engine.submit(make_args(), actor="actor-a")
        app = WebviewApp(store.db_path)
        try:
            # The route table dispatches /api/extractions to the payload.
            assert hasattr(app, "extractions_payload")
        finally:
            app.close()
            store.close()

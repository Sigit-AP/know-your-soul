"""Write-arbitration and audit-lane tests (SUB-100).

The single-writer queue is the corruption firewall: mutations serialize,
identical in-flight idempotency keys share one execution, completed keys
replay through the store, and a full queue answers typed SERVER_BUSY —
never a crash, never an unbounded queue.
"""

from __future__ import annotations

import threading
import time
from pathlib import Path

import pytest
from _serve_helpers import SocketClient, make_config, tool_error_code, tool_payload

from know_your_soul.serve import rpc
from know_your_soul.serve.superpower import SuperpowerDaemon
from know_your_soul.store import EventStore


@pytest.fixture()
def daemon(tmp_path: Path, **overrides):
    config = make_config(tmp_path, idle_timeout_seconds=120.0, **overrides)
    instance = SuperpowerDaemon(config)
    instance.start()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, config
    instance.shutdown("test-teardown")
    thread.join(timeout=10)


def _node(node_id: str, summary: str = "arbiter node") -> dict:
    return {
        "node_id": node_id,
        "kind": "leaf",
        "domain": "P1",
        "summary": summary,
        "provenance": ["soul://test"],
        "source_version_id": "sv-test",
    }


def _domain_event_count(db: Path) -> int:
    store = EventStore(db)
    try:
        with store.read() as conn:
            return int(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'domain'").fetchone()[0])
    finally:
        store.close()


def _write(client: SocketClient, node_id: str, *, key: str | None, summary: str = "arbiter node") -> dict:
    arguments: dict = {"node": _node(node_id, summary)}
    if key is not None:
        arguments["idempotency_key"] = key
    return client.call_tool("knowledge_log_event", arguments)


class TestIdempotencyDedup:
    def test_same_key_same_payload_replays(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            first = tool_payload(_write(client, "n-dedup", key="k-1"))
            second = tool_payload(_write(client, "n-dedup", key="k-1"))
        assert first["domain_event_id"] == second["domain_event_id"]
        assert second["idempotent_replay"] is True

    def test_same_key_conflicting_payload_is_typed(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            _write(client, "n-conflict", key="k-2", summary="original")
            conflict = _write(client, "n-conflict", key="k-2", summary="different payload")
        assert tool_error_code(conflict) == "IdempotencyConflict"

    def test_concurrent_identical_writes_share_one_execution(self, tmp_path: Path) -> None:
        """Six threads, one key, one payload: exactly one domain event lands."""
        config = make_config(tmp_path, idle_timeout_seconds=120.0, mutation_delay_seconds=0.15)
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            barrier = threading.Barrier(6)
            results: list[dict] = []
            lock = threading.Lock()

            def write() -> None:
                with SocketClient(config.paths.socket) as client:
                    barrier.wait()
                    response = _write(client, "n-shared", key="k-shared")
                with lock:
                    results.append(response)

            writers = [threading.Thread(target=write) for _ in range(6)]
            for writer in writers:
                writer.start()
            for writer in writers:
                writer.join(timeout=30)
            assert len(results) == 6
            payloads = [tool_payload(response) for response in results]
            event_ids = {payload["domain_event_id"] for payload in payloads}
            assert len(event_ids) == 1, f"duplicate executions: {event_ids}"
            # In-flight dedup: all six callers share the single execution's
            # receipt (a store-level replay would flag idempotent_replay).
            assert all(payload["event_seq"] == payloads[0]["event_seq"] for payload in payloads)
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=10)
        # Exactly one domain event for the shared key across the whole store.
        store = EventStore(config.db_path)
        try:
            with store.read() as conn:
                count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM event_log WHERE kind = 'domain' AND idempotency_key = 'k-shared'"
                    ).fetchone()[0]
                )
        finally:
            store.close()
        assert count == 1


class TestBackpressure:
    def test_full_write_queue_is_server_busy_not_crash(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            write_queue_size=2,
            per_actor_write_cap=1,  # each flooder gets exactly one queued slot
            mutation_delay_seconds=0.4,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        busy: list[dict] = []
        succeeded: list[dict] = []
        lock = threading.Lock()
        try:
            barrier = threading.Barrier(12)

            def flood(index: int) -> None:
                with SocketClient(config.paths.socket) as client:
                    barrier.wait()
                    response = client.call_tool(
                        "knowledge_log_event",
                        {"node": _node(f"n-flood-{index}")},
                    )
                with lock:
                    if "error" in response and response["error"]["code"] == rpc.SERVER_BUSY:
                        busy.append(response)
                    else:
                        succeeded.append(response)

            flooders = [threading.Thread(target=flood, args=(index,)) for index in range(12)]
            for flooder in flooders:
                flooder.start()
            for flooder in flooders:
                flooder.join(timeout=60)
            assert busy, "expected at least one typed SERVER_BUSY under flood"
            for response in busy:
                assert response["error"]["data"]["retry_after_ms"] > 0
            # The daemon survived the flood and stays answerable.
            with SocketClient(config.paths.socket) as client:
                assert client.request("ping")["result"] == {}
                status = client.request("daemon.status")["result"]
            assert status["busy_rejections"] >= len(busy)
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=15)

    def test_reads_flow_while_write_queue_is_full(self, tmp_path: Path) -> None:
        """Read lane + audit lane are not throttled by write backpressure."""
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            write_queue_size=1,
            per_actor_write_cap=1,  # each writer actor occupies exactly one slot
            mutation_delay_seconds=0.5,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            with SocketClient(config.paths.socket) as writer_client:
                writer_client.call_tool("knowledge_log_event", {"node": _node("n-slow-1")}, actor="writer-a")
                writer_client.call_tool("knowledge_log_event", {"node": _node("n-slow-2")}, actor="writer-b")
                read_errors: list[BaseException] = []

                def read_loop() -> None:
                    try:
                        with SocketClient(config.paths.socket) as client:
                            for _ in range(5):
                                response = client.call_tool(
                                    "knowledge_query",
                                    {"purpose": "run the gates", "domain": "P1", "token_budget": 512},
                                )
                                assert "result" in response
                    except BaseException as error:  # noqa: BLE001 - asserted below
                        read_errors.append(error)

                started = time.monotonic()
                reader = threading.Thread(target=read_loop)
                reader.start()
                reader.join(timeout=10)
            assert not read_errors, f"reads failed under write pressure: {read_errors}"
            assert time.monotonic() - started < 5.0, "reads were throttled by the write queue"
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=15)


class TestAuditLane:
    def test_every_invocation_is_journaled_once(self, daemon) -> None:
        """Adapter rejections and executions both land exactly one journal."""
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            client.call_tool("knowledge_query", {"purpose": "run the gates", "domain": "P1", "token_budget": 512})
            client.call_tool("knowledge_query", {"purpose": "x"})  # adapter rejection
            client.call_tool("knowledge_log_event", {"node": _node("n-audit")})
        instance_shutdown_and_count(daemon)
        store = EventStore(config.db_path)
        try:
            with store.read() as conn:
                journals = conn.execute(
                    "SELECT operation, status FROM invocation_journal WHERE actor = 'tester'"
                ).fetchall()
        finally:
            store.close()
        assert len(journals) == 3, f"expected 3 journals, got {journals}"
        assert all(status == "complete" for _, status in journals)

    def test_read_ops_journal_through_the_single_writer(self, daemon) -> None:
        """Read-lane invocations append audit events with the hash chain intact."""
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            for _ in range(4):
                client.call_tool("knowledge_query", {"purpose": "run the gates", "domain": "P1", "token_budget": 512})
        instance_shutdown_and_count(daemon)
        store = EventStore(config.db_path)
        try:
            report = store.verify_chain()
        finally:
            store.close()
        assert report.ok, f"chain broken at seq {report.first_bad_seq}"

    def test_actor_is_recorded_per_agent(self, daemon) -> None:
        _, config = daemon
        with SocketClient(config.paths.socket) as client:
            client.call_tool("knowledge_get_node", {"node_id": "n-gates"}, actor="agent-a")
            client.call_tool("knowledge_get_node", {"node_id": "n-gates"}, actor="agent-b")
        instance_shutdown_and_count(daemon)
        store = EventStore(config.db_path)
        try:
            with store.read() as conn:
                actors = {
                    row[0]
                    for row in conn.execute(
                        "SELECT DISTINCT actor FROM invocation_journal WHERE operation = 'knowledge.get_node'"
                    )
                }
        finally:
            store.close()
        assert {"agent-a", "agent-b"} <= actors


def instance_shutdown_and_count(daemon) -> None:
    instance, _ = daemon
    # Drain: give lanes a moment to finish in-flight audit appends.
    time.sleep(0.2)

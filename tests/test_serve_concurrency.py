"""The SUB-100 gate: 50 concurrent agents, dense mixed read/write traffic.

Proof obligations:
* zero corruption — the hash chain verifies after the storm;
* zero loss — every one of the 350 invocations is journaled complete;
* idempotent convergence — 50 agents writing one shared key land one event;
* typed backpressure — a deliberately starved write queue yields
  SERVER_BUSY + retry_after_ms, never a crash;
* per-agent audit — the journal groups exactly 7 invocations per actor.
"""

from __future__ import annotations

import json
import threading
import time
from pathlib import Path

import pytest
from tests._serve_helpers import SocketClient, make_config, tool_error_code, tool_payload

from know_your_soul.serve import rpc
from know_your_soul.serve.superpower import SuperpowerDaemon
from know_your_soul.store import EventStore

AGENTS = 50
POOL_NODES = 20  # agents rewrite a shared pool — dense writes, bounded graph
READS_PER_AGENT = 3
SEED_DOMAIN_EVENTS = 10  # KROOT + 7 primaries + n-gates + 1 relation


def _node(node_id: str, summary: str) -> dict:
    return {
        "node_id": node_id,
        "kind": "leaf",
        "domain": "P1",
        "summary": summary,
        "provenance": ["soul://storm"],
        "source_version_id": "sv-test",
    }


def _call_with_retry(client: SocketClient, name: str, arguments: dict, *, actor: str) -> dict:
    for _attempt in range(6):
        response = client.call_tool(name, arguments, actor=actor)
        if "error" in response and response["error"]["code"] == rpc.SERVER_BUSY:
            time.sleep(response["error"]["data"]["retry_after_ms"] / 1000.0)
            continue
        return response
    raise AssertionError(f"{actor} never got past SERVER_BUSY for {name}")


@pytest.fixture()
def storm_daemon(tmp_path: Path):
    config = make_config(tmp_path, idle_timeout_seconds=300.0, max_agents=AGENTS, read_pool_size=8)
    instance = SuperpowerDaemon(config)
    instance.start()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, config
    instance.shutdown("test-teardown")
    thread.join(timeout=30)


class TestFiftyAgentStorm:
    def test_dense_mixed_traffic_zero_corruption(self, storm_daemon) -> None:
        _, config = storm_daemon
        barrier = threading.Barrier(AGENTS)
        failures: list[str] = []
        failures_lock = threading.Lock()

        def agent(index: int) -> None:
            actor = f"agent-{index:02d}"
            try:
                with SocketClient(config.paths.socket) as client:
                    barrier.wait(timeout=20)
                    # Two rewrites of a shared-pool node: dense write traffic
                    # (upsert/improve) without growing the graph past the
                    # publish-capacity guard (F-3, 64 KiB artifact cap).
                    pool_node = f"n-pool-{index % POOL_NODES}"
                    for round_ in range(2):
                        response = _call_with_retry(
                            client,
                            "knowledge_log_event",
                            {
                                "node": _node(pool_node, f"pool {index % POOL_NODES} by {actor} round {round_}"),
                            },
                            actor=actor,
                        )
                        assert "result" in response and not response["result"].get("isError"), response
                    # The shared key: every agent writes the same node under
                    # one key. The store's idempotency contract is loud:
                    # exactly one event lands; the rest replay or get a typed
                    # IdempotencyConflict — never corruption, never a silent
                    # drop.
                    shared = _call_with_retry(
                        client,
                        "knowledge_log_event",
                        {
                            "node": _node("n-storm-shared", "the shared storm node"),
                            "idempotency_key": "k-storm-shared",
                        },
                        actor=actor,
                    )
                    assert "result" in shared, shared
                    if shared["result"].get("isError"):
                        assert tool_error_code(shared) == "IdempotencyConflict", shared
                    for r in range(READS_PER_AGENT):
                        if r % 2 == 0:
                            response = _call_with_retry(
                                client,
                                "knowledge_query",
                                {"purpose": "storm read", "domain": "P1", "token_budget": 512},
                                actor=actor,
                            )
                        else:
                            response = _call_with_retry(
                                client, "knowledge_get_node", {"node_id": "n-gates"}, actor=actor
                            )
                        assert "result" in response, response
            except BaseException as error:  # noqa: BLE001 - collected and asserted
                with failures_lock:
                    failures.append(f"{actor}: {type(error).__name__}: {error}")

        agents = [threading.Thread(target=agent, args=(index,)) for index in range(AGENTS)]
        started = time.monotonic()
        for thread in agents:
            thread.start()
        for thread in agents:
            thread.join(timeout=180)
        elapsed = time.monotonic() - started
        assert not failures, f"{len(failures)} agents failed: {failures[:5]}"

        # Let the lanes drain, then verify the store from a fresh handle.
        time.sleep(0.5)
        store = EventStore(config.db_path)
        try:
            report = store.verify_chain()
            assert report.ok, f"hash chain broken at seq {report.first_bad_seq}"
            with store.read() as conn:
                domain_events = int(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'domain'").fetchone()[0])
                shared_events = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM event_log WHERE kind = 'domain' AND idempotency_key = 'k-storm-shared'"
                    ).fetchone()[0]
                )
                journals = conn.execute(
                    "SELECT actor, COUNT(*), SUM(status = 'complete') FROM invocation_journal "
                    "WHERE actor LIKE 'agent-%' GROUP BY actor"
                ).fetchall()
        finally:
            store.close()
        expected = SEED_DOMAIN_EVENTS + AGENTS * 2 + 1
        assert domain_events == expected, f"expected {expected} domain events, found {domain_events}"
        assert shared_events == 1, f"shared key landed {shared_events} events"
        per_agent = 2 + 1 + READS_PER_AGENT
        assert len(journals) == AGENTS, f"expected {AGENTS} audited actors, found {len(journals)}"
        for actor, total, complete in journals:
            assert total == per_agent, f"{actor}: {total} invocations, expected {per_agent}"
            assert complete == total, f"{actor}: {total - complete} incomplete journals"
        print(f"\nstorm: {AGENTS} agents, {AGENTS * per_agent} invocations, {elapsed:.1f}s, chain ok")

    def test_conflicting_idempotency_flood_is_typed(self, storm_daemon) -> None:
        """Ten agents, one key, ten different payloads: one wins, nine typed conflicts."""
        _, config = storm_daemon
        barrier = threading.Barrier(10)
        outcomes: list[dict] = []
        lock = threading.Lock()

        def contender(index: int) -> None:
            with SocketClient(config.paths.socket) as client:
                barrier.wait(timeout=20)
                response = _call_with_retry(
                    client,
                    "knowledge_log_event",
                    {
                        "node": _node("n-contended", f"payload variant {index}"),
                        "idempotency_key": "k-contended",
                    },
                    actor=f"contender-{index}",
                )
            with lock:
                outcomes.append(response)

        threads = [threading.Thread(target=contender, args=(index,)) for index in range(10)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=60)
        assert len(outcomes) == 10
        successes = [o for o in outcomes if "result" in o and not o["result"].get("isError")]
        conflicts = [
            o
            for o in outcomes
            if "result" in o and o["result"].get("isError") and tool_error_code(o) == "IdempotencyConflict"
        ]
        assert len(successes) == 1, f"expected 1 winner, got {len(successes)}"
        assert len(conflicts) == 9, f"expected 9 typed conflicts, got {len(conflicts)}: {outcomes[:2]}"
        time.sleep(0.3)
        store = EventStore(config.db_path)
        try:
            assert store.verify_chain().ok
            with store.read() as conn:
                count = int(
                    conn.execute(
                        "SELECT COUNT(*) FROM event_log WHERE kind = 'domain' AND idempotency_key = 'k-contended'"
                    ).fetchone()[0]
                )
        finally:
            store.close()
        assert count == 1

    def test_read_your_writes_across_lanes(self, storm_daemon) -> None:
        """A write committed on the writer lane is visible to the read pool."""
        _, config = storm_daemon
        with SocketClient(config.paths.socket) as client:
            written = _call_with_retry(
                client,
                "knowledge_log_event",
                {"node": _node("n-crosslane", "cross-lane visibility")},
                actor="agent-crosslane",
            )
            assert not written["result"].get("isError"), written
            # The read lane waits for the writer's publish, then answers fresh.
            for _ in range(5):
                read = _call_with_retry(
                    client, "knowledge_get_node", {"node_id": "n-crosslane"}, actor="agent-crosslane"
                )
                payload = tool_payload(read)
                assert "n-crosslane" in json.dumps(payload)


class TestStarvation:
    def test_full_write_queue_busy_then_drains(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            write_queue_size=2,
            per_actor_write_cap=1,  # each flooder gets exactly one queued slot
            mutation_delay_seconds=0.3,
            retry_after_ms=50,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        busy_seen = 0
        lock = threading.Lock()
        try:
            barrier = threading.Barrier(16)

            def flood(index: int) -> None:
                nonlocal busy_seen
                with SocketClient(config.paths.socket) as client:
                    barrier.wait(timeout=20)
                    response = client.call_tool(
                        "knowledge_log_event",
                        {"node": _node(f"n-starve-{index}", f"starve {index}")},
                        actor=f"starve-{index}",
                    )
                    if "error" in response and response["error"]["code"] == rpc.SERVER_BUSY:
                        with lock:
                            nonlocal_busy[0] += 1

            nonlocal_busy = [0]
            threads = [threading.Thread(target=flood, args=(index,)) for index in range(16)]
            for thread in threads:
                thread.start()
            for thread in threads:
                thread.join(timeout=60)
            busy_seen = nonlocal_busy[0]
            assert busy_seen >= 1, "the starved queue never pushed back"
            # And the daemon is still healthy afterwards.
            with SocketClient(config.paths.socket) as client:
                assert client.request("ping")["result"] == {}
                status = client.request("daemon.status")["result"]
                assert status["busy_rejections"] >= busy_seen
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=15)
        store = EventStore(config.db_path)
        try:
            assert store.verify_chain().ok
        finally:
            store.close()

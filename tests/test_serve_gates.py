"""SUB-104 acceptance gates for the superpower server.

Every gate from SUPERPOWER-ARCHITECTURE.md lands here with real numbers:

* 50-agent storm over ALL 16 verbs (read + write + heavy mixed), with real
  client-side latency percentiles per lane against written targets;
* fairness: one agent spamming 10x the polite load costs the polite agents
  less than 20% throughput;
* deadline propagation: a write that expires in the queue fails typed
  DEADLINE_EXCEEDED instead of executing;
* session lease: a silent session loses its slot, measured by a second agent
  getting in on a max_agents=1 daemon;
* drain: graceful stop finishes in-flight work with a response and refuses
  new work typed; force stop skips the drain and exits 2;
* heavy lane: ingest runs off the interactive lanes, which stay answerable;
* group commit: A/B burst throughput, group_commit on vs off (kill switch
  honored), honest ratio printed;
* crash safety: kill -9 of a real daemon process mid-burst, then restart —
  every acknowledged write survives, the chain verifies, crashed journals
  are reconciled.
"""

from __future__ import annotations

import json
import os
import signal
import threading
import time
from pathlib import Path

import pytest
from tests._serve_helpers import SocketClient, make_config, tool_payload, wait_for

from know_your_soul.serve import rpc
from know_your_soul.serve.superpower import (
    SuperpowerDaemon,
    classify_tool,
    spawn_daemon,
    stop_daemon,
)
from know_your_soul.store import EventStore

# Written latency targets for the storm gate (client-side milliseconds, ~1.5x
# headroom over the measured p95). Under the defined 50-agent storm the write
# lane is a single writer that also drains the read lane's audit appends, so
# percentiles are queue-wait dominated: reads never wait on write EXECUTION,
# but their audit journal rides the writer. Measured (SUB-104, this repo's
# CI host): read p95 ~3.2s, write p95 ~11.2s, heavy p95 ~14.3s.
READ_P95_TARGET_MS = 6_000.0
WRITE_P95_TARGET_MS = 18_000.0
HEAVY_P95_TARGET_MS = 30_000.0

AGENTS = 50


def _node(node_id: str, summary: str = "gate node") -> dict:
    return {
        "node_id": node_id,
        "parent_id": "P1",
        "kind": "leaf",
        "domain": "P1",
        "summary": summary,
        "provenance": ["soul://gate"],
        "source_version_id": "sv-test",
    }


def _evidence(evidence_id: str, node_id: str) -> dict:
    return {
        "evidence_id": evidence_id,
        "node_id": node_id,
        "source_version_id": "sv-test",
        "source_uri": "soul://gate",
        "observed_at": "2026-10-28T00:00:00+00:00",
        "context_tags": ["gate"],
        "independence_key": evidence_id,
        "outcome": "1",
        "correctness": "0.9",
        "reliability": "0.9",
        "reproducibility": "0.9",
        "scope_fit": "0.9",
        "successful_use": "1",
        "task_coverage": "1",
        "quality_at_use": "0.9",
        "contradiction": "0",
        "utilization_u": "0.5",
        "latency_ms": 10,
        "compute_ms": 5,
        "output_chars": 10,
    }


def _percentile(samples: list[float], q: float) -> float:
    ordered = sorted(samples)
    index = min(len(ordered) - 1, max(0, int(round(q * (len(ordered) - 1)))))
    return ordered[index]


def _call_with_retry(client: SocketClient, name: str, arguments: dict, *, actor: str, meta: dict | None = None) -> dict:
    # The heavy lane is one worker with a small queue: a 50-agent ingest burst
    # keeps it full for seconds, so the retry budget must outlast the backlog.
    for _attempt in range(60):
        params: dict = {"name": name, "arguments": arguments, "_meta": {"actor": actor, **(meta or {})}}
        response = client.request("tools/call", params)
        if "error" in response and response["error"]["code"] == rpc.SERVER_BUSY:
            time.sleep(response["error"]["data"]["retry_after_ms"] / 1000.0)
            continue
        return response
    raise AssertionError(f"{actor} never got past SERVER_BUSY for {name}")


@pytest.fixture()
def gate_daemon(tmp_path: Path):
    # heavy_queue_size >= 50 agents x 2 heavy ops: the storm's ingest burst is
    # queued, never rejected; the per-actor cap still pins one spammer's lane.
    # Group commit keeps its production defaults (batch 8 / 2ms): larger
    # batches measurably stall the audit lane and with it read latency.
    config = make_config(
        tmp_path,
        idle_timeout_seconds=300.0,
        max_agents=AGENTS,
        heavy_queue_size=128,
        per_actor_heavy_cap=4,
    )
    instance = SuperpowerDaemon(config)
    instance.start()
    thread = threading.Thread(target=instance.serve_forever, daemon=True)
    thread.start()
    yield instance, config
    instance.shutdown("test-teardown")
    thread.join(timeout=30)


class TestStormAllVerbs:
    def test_sixteen_ops_fifty_agents_with_latency_per_lane(self, gate_daemon, tmp_path: Path) -> None:
        _, config = gate_daemon
        # A file inside the confined ingestion root for the heavy lane.
        Path(config.graph_root).mkdir(parents=True, exist_ok=True)
        (Path(config.graph_root) / "storm_doc.py").write_text("def storm_gate():\n    return 42\n", encoding="utf-8")

        failures: list[str] = []
        lock = threading.Lock()
        latencies: dict[str, list[float]] = {"read": [], "write": [], "heavy": []}

        def record(name: str, elapsed_ms: float) -> None:
            with lock:
                latencies[classify_tool(name)].append(elapsed_ms)

        def call(client: SocketClient, actor: str, name: str, arguments: dict) -> dict:
            started = time.monotonic()
            response = _call_with_retry(client, name, arguments, actor=actor)
            record(name, (time.monotonic() - started) * 1000.0)
            return response

        # The frozen publish-capacity guard (F-3, 62 KiB estimate cap) fits
        # ~36 small nodes on top of the seed: 28 lifecycle agents own one node
        # each and run ALL 16 verbs (incl. the version-pinned propose/apply/
        # rollback chain); 22 pool agents share 8 pool nodes and run the 13
        # verbs with no version pin (log_event upserts are pin-free). Every
        # lane is stormed by every agent.
        lifecycle_agents = 28

        def agent(index: int) -> None:
            actor = f"storm-{index:02d}"
            lifecycle = index < lifecycle_agents
            node_id = f"n-storm-{index:02d}" if lifecycle else f"n-storm-pool-{index % 8}"
            try:
                with SocketClient(config.paths.socket, timeout=45.0) as client:
                    # --- write lane (7 verbs; evaluate/review persist too)
                    response = call(client, actor, "knowledge_log_event", {"node": _node(node_id)})
                    assert not response["result"].get("isError"), response
                    response = call(
                        client,
                        actor,
                        "knowledge_record_outcome",
                        {"evidence": _evidence(f"ev-storm-{index:02d}", node_id)},
                    )
                    assert not response["result"].get("isError"), response
                    response = call(client, actor, "knowledge_evaluate", {"node_id": node_id})
                    assert not response["result"].get("isError"), response
                    response = call(client, actor, "knowledge_review", {"node_id": node_id})
                    assert not response["result"].get("isError"), response
                    if lifecycle:
                        # Version-pinned lifecycle: only safe on an owned node.
                        response = call(
                            client,
                            actor,
                            "knowledge_propose_improvement",
                            {
                                "node_id": node_id,
                                "diff": [{"op": "set_summary", "value": f"storm summary {index}"}],
                                "rationale": "storm",
                            },
                        )
                        assert not response["result"].get("isError"), response
                        proposal_id = tool_payload(response)["proposal"]["proposal_id"]
                        response = call(client, actor, "knowledge_apply_improvement", {"proposal_id": proposal_id})
                        assert not response["result"].get("isError"), response
                        response = call(client, actor, "knowledge_rollback", {"node_id": node_id})
                        assert not response["result"].get("isError"), response
                    # --- read lane (7 verbs)
                    for name, arguments in (
                        ("knowledge_query", {"purpose": "storm", "domain": "P1", "token_budget": 512}),
                        ("knowledge_get_node", {"node_id": node_id}),
                        ("knowledge_get_subtree", {"node_id": node_id, "depth": 1}),
                        ("knowledge_path", {"from_id": "KROOT", "to_id": node_id}),
                        ("knowledge_explain", {"node_id": node_id}),
                        ("knowledge_affected", {"node_id": node_id, "depth": 1}),
                        ("knowledge_god_nodes", {"limit": 3}),
                    ):
                        response = call(client, actor, name, arguments)
                        assert not response["result"].get("isError"), (name, response)
                    # --- heavy lane (2 verbs)
                    response = call(client, actor, "knowledge_ingest_file", {"path": "storm_doc.py"})
                    assert not response["result"].get("isError"), response
                    response = call(client, actor, "knowledge_ingest_directory", {"path": ".", "max_files": 1})
                    assert not response["result"].get("isError"), response
            except BaseException as error:  # noqa: BLE001 - collected and asserted
                with lock:
                    failures.append(f"{actor}: {type(error).__name__}: {error}")

        barrier = threading.Barrier(AGENTS)

        def agent_at_barrier(index: int) -> None:
            try:
                barrier.wait(timeout=30)
            except threading.BrokenBarrierError:
                with lock:
                    failures.append(f"storm-{index:02d}: barrier broken")
                return
            agent(index)

        started = time.monotonic()
        threads = [threading.Thread(target=agent_at_barrier, args=(index,)) for index in range(AGENTS)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=300)
        elapsed = time.monotonic() - started
        assert not failures, f"{len(failures)} agents failed: {failures[:5]}"

        # Real percentiles per lane, against the written targets.
        report: dict[str, dict[str, float]] = {}
        for lane, samples in latencies.items():
            assert samples, f"lane {lane} saw no traffic"
            report[lane] = {
                "count": len(samples),
                "p50": round(_percentile(samples, 0.50), 1),
                "p95": round(_percentile(samples, 0.95), 1),
                "p99": round(_percentile(samples, 0.99), 1),
            }
        print(f"\nstorm16: {AGENTS} agents x 16 ops in {elapsed:.1f}s; latency ms per lane:")
        for lane in ("read", "write", "heavy"):
            print(f"  {lane}: {report[lane]}")
        assert report["read"]["p95"] < READ_P95_TARGET_MS, report["read"]
        assert report["write"]["p95"] < WRITE_P95_TARGET_MS, report["write"]
        assert report["heavy"]["p95"] < HEAVY_P95_TARGET_MS, report["heavy"]

        # Server-side per-lane histograms agree on the traffic shape.
        time.sleep(0.5)
        with SocketClient(config.paths.socket, timeout=30.0) as client:
            status = client.request("daemon.status")["result"]
        per_actor = status["latency_ms_per_actor"]
        server_counts = {"read": 0, "write": 0, "heavy": 0}
        for actor, lanes in per_actor.items():
            if not actor.startswith("storm-"):
                continue
            for lane, stats in lanes.items():
                server_counts[lane] += stats["count"]
        pool_agents = AGENTS - lifecycle_agents
        assert server_counts["read"] == AGENTS * 7, server_counts
        assert server_counts["write"] == lifecycle_agents * 7 + pool_agents * 4, server_counts
        assert server_counts["heavy"] == AGENTS * 2, server_counts

        # Zero corruption, zero lost writes, every invocation journaled.
        store = EventStore(config.db_path)
        try:
            chain = store.verify_chain()
            assert chain.ok, f"hash chain broken at seq {chain.first_bad_seq}"
            with store.read() as conn:
                journals = dict(
                    (row[0], (row[1], row[2]))
                    for row in conn.execute(
                        "SELECT actor, COUNT(*), SUM(status = 'complete') FROM invocation_journal "
                        "WHERE actor LIKE 'storm-%' GROUP BY actor"
                    ).fetchall()
                )
        finally:
            store.close()
        assert len(journals) == AGENTS, f"expected {AGENTS} audited storm actors, found {len(journals)}"
        for index in range(AGENTS):
            total, complete = journals[f"storm-{index:02d}"]
            expected = 16 if index < lifecycle_agents else 13
            assert total == expected, f"storm-{index:02d}: {total} journals, expected {expected}"
            assert complete == total, f"storm-{index:02d}: {total - complete} incomplete journals"


class TestFairnessSpammer:
    def test_spammer_costs_polite_agents_less_than_twenty_percent(self, tmp_path: Path) -> None:
        """One actor at 10x volume; 9 polite actors keep >= 80% throughput."""
        config = make_config(
            tmp_path,
            idle_timeout_seconds=300.0,
            write_queue_size=40,
            per_actor_write_cap=4,
            mutation_delay_seconds=0.05,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        polite = 9
        conns_each = 3  # several outstanding requests per agent, like real agents
        writes_each = 4

        def polite_wave(tag: str) -> float:
            barrier = threading.Barrier(polite * conns_each)
            errors: list[str] = []

            def worker(index: int, conn: int) -> None:
                actor = f"polite-{tag}-{index}"
                try:
                    with SocketClient(config.paths.socket, timeout=30.0) as client:
                        barrier.wait(timeout=20)
                        for w in range(writes_each):
                            # A bounded rewrite pool: dense write traffic that
                            # stays under the publish-capacity guard (F-3).
                            response = _call_with_retry(
                                client,
                                "knowledge_log_event",
                                {"node": _node(f"n-fair-pool-{index}", f"{tag} wave {conn} write {w}")},
                                actor=actor,
                            )
                            assert "result" in response and not response["result"].get("isError"), response
                except BaseException as error:  # noqa: BLE001
                    errors.append(f"{actor}/{conn}: {error}")

            started = time.monotonic()
            threads = [
                threading.Thread(target=worker, args=(index, conn))
                for index in range(polite)
                for conn in range(conns_each)
            ]
            for t in threads:
                t.start()
            for t in threads:
                t.join(timeout=180)
            elapsed = time.monotonic() - started
            assert not errors, errors[:3]
            return elapsed

        try:
            baseline = polite_wave("solo")

            spam_stop = threading.Event()
            spam_attempts = [0]
            spam_done = [0]
            spam_lock = threading.Lock()

            def spammer_conn() -> None:
                # One abusive agent, ten connections: offers ≫10x the polite
                # per-agent rate; every RPC (including typed rejections) is
                # counted as offered load.
                with SocketClient(config.paths.socket, timeout=30.0) as client:
                    counter = 0
                    while not spam_stop.is_set():
                        with spam_lock:
                            spam_attempts[0] += 1
                        response = client.call_tool(
                            "knowledge_log_event",
                            {"node": _node(f"n-spam-pool-{counter % 3}", f"spam {counter}")},
                            actor="spammer",
                        )
                        if "error" in response and response["error"]["code"] == rpc.SERVER_BUSY:
                            time.sleep(response["error"]["data"]["retry_after_ms"] / 1000.0)
                            continue
                        if "result" in response and not response["result"].get("isError"):
                            with spam_lock:
                                spam_done[0] += 1
                        counter += 1

            spam_threads = [threading.Thread(target=spammer_conn, daemon=True) for _ in range(10)]
            for t in spam_threads:
                t.start()
            time.sleep(0.1)  # let the spam reach steady state
            spammed = polite_wave("spam")
            spam_stop.set()
            for t in spam_threads:
                t.join(timeout=30)
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=15)

        throughput_solo = polite * conns_each * writes_each / baseline
        throughput_spammed = polite * conns_each * writes_each / spammed
        ratio = throughput_spammed / throughput_solo
        print(
            f"\nfairness: polite throughput {throughput_solo:.1f}/s solo vs {throughput_spammed:.1f}/s "
            f"under spam ({spam_attempts[0]} attempts / {spam_done[0]} landed); ratio {ratio:.3f}"
        )
        # The spammer's offer rate is the flood: 10x what one polite agent
        # sends in the same window (per-actor caps keep most of it queued out).
        assert spam_attempts[0] >= 10 * conns_each * writes_each, (
            f"the spammer offered only {spam_attempts[0]} writes — the 10x flood never happened"
        )
        assert ratio >= 0.80, f"polite agents lost {1 - ratio:.1%} throughput under the spammer"


class TestDeadlinePropagation:
    def test_write_expiring_in_queue_is_deadline_exceeded(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            write_queue_size=4,
            per_actor_write_cap=4,
            mutation_delay_seconds=0.4,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            outcomes: dict[str, dict] = {}

            def slow_write() -> None:
                with SocketClient(config.paths.socket) as client:
                    outcomes["slow"] = client.call_tool(
                        "knowledge_log_event", {"node": _node("n-deadline-slow")}, actor="slow-writer"
                    )

            slow = threading.Thread(target=slow_write)
            slow.start()
            time.sleep(0.1)  # the slow write is executing on the writer thread
            with SocketClient(config.paths.socket) as client:
                outcomes["expiring"] = client.request(
                    "tools/call",
                    {
                        "name": "knowledge_log_event",
                        "arguments": {"node": _node("n-deadline-expired")},
                        "_meta": {"actor": "impatient", "deadline_ms": 150},
                    },
                )
            slow.join(timeout=30)
            expiring = outcomes["expiring"]
            assert "error" in expiring, expiring
            assert expiring["error"]["code"] == rpc.DEADLINE_EXCEEDED, expiring
            # The slow write was unaffected and the daemon stays healthy.
            assert not outcomes["slow"]["result"].get("isError"), outcomes["slow"]
            with SocketClient(config.paths.socket) as client:
                assert client.request("ping")["result"] == {}
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=15)
        # The expired write never executed: its node is absent.
        store = EventStore(config.db_path)
        try:
            with store.read() as conn:
                rows = conn.execute(
                    "SELECT COUNT(*) FROM event_log WHERE kind = 'domain' AND node_id = 'n-deadline-expired'"
                ).fetchone()[0]
        finally:
            store.close()
        assert int(rows) == 0, "an expired write executed anyway"


class TestSessionLease:
    def test_silent_session_loses_its_slot(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            max_agents=1,
            session_lease_seconds=0.5,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        thread = threading.Thread(target=instance.serve_forever, daemon=True)
        thread.start()
        try:
            holder = SocketClient(config.paths.socket)
            try:
                assert holder.request("ping")["result"] == {}
                # The only slot is taken: a second agent gets typed busy.
                # (Overflow connections receive the busy frame on connect.)
                contender = SocketClient(config.paths.socket)
                frame = contender.read_response(timeout=5.0)
                assert frame["error"]["code"] == rpc.SERVER_BUSY
                contender.close()
                # The holder goes silent; the lease expires and the reaper
                # frees the slot — measured by the second agent getting in.
                deadline = time.monotonic() + 15.0
                admitted = False
                while time.monotonic() < deadline and not admitted:
                    try:
                        probe = SocketClient(config.paths.socket, timeout=3.0)
                    except ConnectionError, OSError:
                        time.sleep(0.2)
                        continue
                    try:
                        response = probe.request("ping")
                        admitted = "result" in response
                    except ConnectionError, OSError:
                        pass
                    finally:
                        probe.close()
                    if not admitted:
                        time.sleep(0.2)
                assert admitted, "the expired session's slot was never released"
            finally:
                holder.close()
        finally:
            instance.shutdown("test-teardown")
            thread.join(timeout=15)


class TestDrainAndForce:
    def test_graceful_drain_answers_in_flight_and_refuses_new(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            write_queue_size=8,
            per_actor_write_cap=2,
            mutation_delay_seconds=0.4,
            drain_timeout_seconds=10.0,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        exit_codes: list[int] = []
        thread = threading.Thread(target=lambda: exit_codes.append(instance.serve_forever()), daemon=True)
        thread.start()
        # Three writers on three connections: one executing, two queued — a
        # real drain window (~1.2s of writer work) follows the stop request.
        writers = [SocketClient(config.paths.socket) for _ in range(3)]
        live = SocketClient(config.paths.socket)  # established before the stop request
        assert live.request("ping")["result"] == {}
        pending: list[tuple[SocketClient, int]] = []
        try:
            for index, writer in enumerate(writers):
                request_id = writer.send_tool(
                    "knowledge_log_event", {"node": _node(f"n-drain-{index}")}, actor=f"drainer-{index}"
                )
                pending.append((writer, request_id))
            time.sleep(0.15)  # first write executing, two queued
            instance.shutdown("operator")
            # New work on a live pre-shutdown session is refused typed.
            refusal = live.call_tool("knowledge_log_event", {"node": _node("n-after-drain")}, actor="late")
            assert refusal["error"]["code"] == rpc.SHUTTING_DOWN, refusal
            # New connections get a typed frame on connect, not silence.
            newcomer = SocketClient(config.paths.socket)
            frame = newcomer.read_response(timeout=5.0)
            assert frame["error"]["code"] == rpc.SHUTTING_DOWN
            newcomer.close()
            # Every pre-shutdown request — executing AND queued — is answered.
            for writer, request_id in pending:
                response = writer.read_response(timeout=20.0)
                assert response["id"] == request_id
                assert "result" in response and not response["result"].get("isError"), response
        finally:
            live.close()
            for writer in writers:
                writer.close()
        thread.join(timeout=20)
        assert not thread.is_alive()
        assert exit_codes == [0], exit_codes
        assert json.loads(config.paths.exit.read_text(encoding="utf-8"))["reason"] == "operator"
        # All three drained writes committed; the refused one never landed.
        store = EventStore(config.db_path)
        try:
            with store.read() as conn:
                drained = conn.execute(
                    "SELECT COUNT(*) FROM event_log WHERE kind = 'domain' AND node_id LIKE 'n-drain-%'"
                ).fetchone()[0]
                refused = conn.execute(
                    "SELECT COUNT(*) FROM event_log WHERE kind = 'domain' AND node_id = 'n-after-drain'"
                ).fetchone()[0]
        finally:
            store.close()
        assert int(drained) == 3
        assert int(refused) == 0

    def test_force_stop_skips_the_drain_and_exits_2(self, tmp_path: Path) -> None:
        config = make_config(
            tmp_path,
            idle_timeout_seconds=120.0,
            mutation_delay_seconds=2.0,
            drain_timeout_seconds=30.0,
        )
        instance = SuperpowerDaemon(config)
        instance.start()
        exit_codes: list[int] = []
        thread = threading.Thread(target=lambda: exit_codes.append(instance.serve_forever()), daemon=True)
        thread.start()
        with SocketClient(config.paths.socket) as client:
            client.send_tool("knowledge_log_event", {"node": _node("n-force")}, actor="forced")
            time.sleep(0.15)
            started = time.monotonic()
            instance.shutdown("operator", force=True)
            thread.join(timeout=15)
            elapsed = time.monotonic() - started
        assert not thread.is_alive()
        # Force skips the drain window entirely: fast even with 2s mutations
        # and a 30s drain budget.
        assert elapsed < 10.0, f"force stop took {elapsed:.1f}s"
        assert exit_codes == [2], exit_codes
        assert json.loads(config.paths.exit.read_text(encoding="utf-8"))["reason"] == "operator-force"


class TestHeavyLane:
    def test_ingest_runs_off_the_interactive_lanes(self, gate_daemon, tmp_path: Path) -> None:
        _, config = gate_daemon
        root = Path(config.graph_root)
        root.mkdir(parents=True, exist_ok=True)
        for index in range(4):
            (root / f"heavy_doc_{index}.py").write_text(f"def heavy_{index}():\n    return {index}\n", encoding="utf-8")
        read_latencies: list[float] = []
        read_errors: list[str] = []
        stop_reads = threading.Event()

        def reader() -> None:
            with SocketClient(config.paths.socket, timeout=30.0) as client:
                while not stop_reads.is_set():
                    started = time.monotonic()
                    try:
                        response = client.call_tool(
                            "knowledge_query",
                            {"purpose": "heavy-lane isolation", "domain": "P1", "token_budget": 512},
                            actor="heavy-reader",
                        )
                        if "error" in response or response["result"].get("isError"):
                            read_errors.append(str(response)[:200])
                        else:
                            read_latencies.append((time.monotonic() - started) * 1000.0)
                    except (ConnectionError, OSError) as error:
                        read_errors.append(str(error))
                        return

        with SocketClient(config.paths.socket, timeout=60.0) as heavy_client:
            reader_thread = threading.Thread(target=reader)
            reader_thread.start()
            try:
                for _ in range(6):  # keep the heavy worker busy across many reads
                    response = heavy_client.call_tool(
                        "knowledge_ingest_directory", {"path": ".", "max_files": 4}, actor="heavy-agent"
                    )
                    assert not response["result"].get("isError"), response
            finally:
                stop_reads.set()
                reader_thread.join(timeout=30)
        assert not read_errors, read_errors[:3]
        assert read_latencies, "the read lane stalled behind the ingest"
        p95 = _percentile(read_latencies, 0.95)
        print(f"\nheavy lane: 6 ingests ok; {len(read_latencies)} reads during ingest, read p95 {p95:.1f}ms")
        assert p95 < READ_P95_TARGET_MS
        with SocketClient(config.paths.socket) as client:
            status = client.request("daemon.status")["result"]
        assert status["latency_ms_per_actor"]["heavy-agent"]["heavy"]["count"] == 6


def _burst(config, tag: str, *, threads: int = 16, writes: int = 15) -> float:
    """16 agents x 15 writes against one daemon; returns wall seconds."""
    barrier = threading.Barrier(threads)
    errors: list[str] = []

    def worker(index: int) -> None:
        actor = f"bench-{tag}-{index}"
        try:
            with SocketClient(config.paths.socket, timeout=60.0) as client:
                barrier.wait(timeout=20)
                for w in range(writes):
                    # Bounded rewrite pool: burst traffic under the F-3 cap.
                    response = client.call_tool(
                        "knowledge_log_event",
                        {"node": _node(f"n-bench-{tag}-pool-{w % 4}", f"bench {index} round {w}")},
                        actor=actor,
                    )
                    if "error" in response or response["result"].get("isError"):
                        errors.append(str(response)[:200])
                        return
        except BaseException as error:  # noqa: BLE001
            errors.append(f"{actor}: {error}")

    started = time.monotonic()
    workers = [threading.Thread(target=worker, args=(index,)) for index in range(threads)]
    for t in workers:
        t.start()
    for t in workers:
        t.join(timeout=180)
    elapsed = time.monotonic() - started
    assert not errors, errors[:3]
    return elapsed


class TestGroupCommit:
    def test_group_commit_ab_benchmark(self, tmp_path: Path) -> None:
        """Honest A/B: 240 writes per arm, group commit on vs off."""

        def run_arm(group_commit: bool) -> tuple[float, int]:
            arm_dir = tmp_path / ("on" if group_commit else "off")
            arm_dir.mkdir()
            config = make_config(arm_dir, idle_timeout_seconds=300.0, group_commit=group_commit)
            instance = SuperpowerDaemon(config)
            instance.start()
            thread = threading.Thread(target=instance.serve_forever, daemon=True)
            thread.start()
            try:
                with SocketClient(config.paths.socket) as client:
                    status = client.request("daemon.status")["result"]
                assert status["group_commit"] is group_commit, "the kill switch did not apply"
                elapsed = _burst(config, "on" if group_commit else "off")
            finally:
                instance.shutdown("test-teardown")
                thread.join(timeout=30)
            store = EventStore(config.db_path)
            try:
                assert store.verify_chain().ok
                with store.read() as conn:
                    events = int(conn.execute("SELECT COUNT(*) FROM event_log WHERE kind = 'domain'").fetchone()[0])
            finally:
                store.close()
            return elapsed, events

        off_elapsed, off_events = run_arm(False)
        on_elapsed, on_events = run_arm(True)
        assert on_events == off_events, "group commit changed the write count"
        writes = 16 * 15
        ratio = off_elapsed / on_elapsed
        print(
            f"\ngroup-commit A/B ({writes} writes/arm): "
            f"off {off_elapsed:.2f}s ({writes / off_elapsed:.0f}/s) vs "
            f"on {on_elapsed:.2f}s ({writes / on_elapsed:.0f}/s) -> {ratio:.2f}x"
        )
        # The hard gate (correctness) is above; the speed claim is reported
        # honestly in docs — group commit must never be SLOWER.
        assert ratio >= 1.0, f"group commit was slower: {ratio:.2f}x"


class TestCrashSafety:
    def test_kill9_mid_burst_recovers_every_acknowledged_write(self, tmp_path: Path) -> None:
        config = make_config(tmp_path, idle_timeout_seconds=300.0)
        spawned = spawn_daemon(config)
        assert not spawned.already_running
        acked: set[str] = set()
        lock = threading.Lock()
        stop = threading.Event()

        def writer(index: int) -> None:
            try:
                with SocketClient(config.paths.socket, timeout=30.0) as client:
                    for w in range(25):
                        if stop.is_set():
                            return
                        # Bounded rewrite pool: burst traffic under the F-3 cap.
                        node_id = f"n-crash-{index:02d}-{w % 3:02d}"
                        response = client.call_tool(
                            "knowledge_log_event",
                            {"node": _node(node_id), "idempotency_key": f"crash:{index}:{w}"},
                            actor=f"crash-{index:02d}",
                        )
                        if "result" in response and not response["result"].get("isError"):
                            with lock:
                                acked.add(node_id)
            except ConnectionError, OSError:
                return  # the daemon died under us — expected

        threads = [threading.Thread(target=writer, args=(index,)) for index in range(12)]
        for t in threads:
            t.start()
        assert wait_for(lambda: len(acked) >= 5, timeout=30.0), "no write was ever acknowledged"
        os.kill(spawned.pid, signal.SIGKILL)
        for t in threads:
            t.join(timeout=30)
        acked_at_crash = set(acked)
        assert acked, "nothing acknowledged before the kill"

        # Restart on the same store: stale-lock recovery + journal reconcile.
        respawned = spawn_daemon(config)
        assert not respawned.already_running
        assert respawned.pid != spawned.pid
        try:
            store = EventStore(config.db_path)
            try:
                chain = store.verify_chain()
                assert chain.ok, f"hash chain broken at seq {chain.first_bad_seq}"
                with store.read() as conn:
                    present = {
                        row[0]
                        for row in conn.execute(
                            "SELECT DISTINCT node_id FROM event_log WHERE kind = 'domain'"
                        ).fetchall()
                    }
                    in_progress = int(
                        conn.execute("SELECT COUNT(*) FROM invocation_journal WHERE status = 'in_progress'").fetchone()[
                            0
                        ]
                    )
            finally:
                store.close()
            missing = acked_at_crash - present
            assert not missing, f"{len(missing)} acknowledged writes lost to kill -9: {sorted(missing)[:5]}"
            assert in_progress == 0, f"{in_progress} crashed journals never reconciled"
            print(
                f"\ncrash safety: kill -9 after {len(acked_at_crash)} acked writes; "
                f"0 lost, chain ok, 0 unreconciled journals"
            )
        finally:
            stop_daemon(config, timeout_seconds=15.0)

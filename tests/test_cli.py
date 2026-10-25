"""Contract tests for the ``soul-knowledge`` CLI (Task 7).

The CLI is a thin adapter over the ten-operation ``KnowledgeService`` plus the
operational commands (rebuild, backup, restore, health). Every valid
invocation — including ones the service rejects — produces exactly one final
audit event; input the adapter itself rejects is still journaled with a
completion audit record before the CLI exits non-zero.
"""

from __future__ import annotations

import io
import json
import os
from pathlib import Path

import pytest

from soul_knowledge.cli import main  # noqa: E402  (RED until cli.py lands)
from soul_knowledge.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    Relation,
    RelationKind,
)
from soul_knowledge.projection import GraphProjector
from soul_knowledge.store import EventStore

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
        idempotency_key=f"test:upsert:{node.node_id}",
    )


def _seeded_paths(tmp_path: Path) -> tuple[Path, Path]:
    """A store with roots + one P1 child, projected and active."""
    db_path = tmp_path / "soul.sqlite3"
    out_path = tmp_path / "out"
    store = EventStore(db_path)
    _upsert(
        store,
        Node(
            node_id="KROOT",
            kind=NodeKind.ROOT,
            domain=PrimaryDomain.P1,
            summary="Knowledge Root",
            provenance=("soul://roots",),
            source_version_id="sv-test",
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
                source_version_id="sv-test",
            ),
        )
    _upsert(
        store,
        Node(
            node_id="n-gates",
            parent_id="P1",
            kind=NodeKind.CHILD,
            domain=PrimaryDomain.P1,
            summary="Run the gates before commit",
            context_tags=("gates", "discipline"),
            provenance=("soul://corpus/lesson.md",),
            source_version_id="sv-test",
        ),
    )
    store.append_domain(
        Event(
            kind=EventKind.RELATION_ADD,
            node_id="P1",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.PARENT_OF, source_node_id="P1", target_node_id="n-gates"),
        ),
        idempotency_key="test:rel:P1:n-gates",
    )
    GraphProjector(store, output_root=out_path).rebuild()
    store.close()
    return db_path, out_path


def _base(db_path: Path, out_path: Path) -> list[str]:
    return ["--store", str(db_path), "--output-root", str(out_path)]


def _invoke(
    argv: list[str], capsys: pytest.CaptureFixture[str]
) -> tuple[int, dict[str, object] | None, dict[str, object] | None]:
    """Run the CLI; return (exit_code, stdout_json, stderr_json)."""
    code = main(argv)
    captured = capsys.readouterr()
    out = json.loads(captured.out) if captured.out.strip() else None
    err = json.loads(captured.err) if captured.err.strip().startswith("{") else None
    return code, out, err


def _audit_count(db_path: Path) -> int:
    store = EventStore(db_path)
    try:
        row = store.execute_raw("SELECT COUNT(*) AS n FROM event_log WHERE kind = 'audit'").fetchone()
        return int(row["n"])
    finally:
        store.close()


def _audit_events_for(db_path: Path, audit_event_id: str) -> list[dict[str, object]]:
    store = EventStore(db_path)
    try:
        rows = store.execute_raw(
            "SELECT event_id, payload_json FROM event_log WHERE kind = 'audit' AND event_id = ?",
            (audit_event_id,),
        ).fetchall()
        return [dict(row) for row in rows]
    finally:
        store.close()


# ------------------------------------------------------------ happy-path ops


def test_cli_query_returns_full_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    payload = json.dumps({"purpose": "run the gates", "domain": "P1", "token_budget": 512})
    code, out, err = _invoke(_base(db_path, out_path) + ["query", payload], capsys)
    assert code == 0, err
    assert out is not None
    assert out["routing"] == "direct"
    assert "n-gates" in {node["node_id"] for node in out["results"]}  # type: ignore[index]
    assert out["audit_event_id"]
    assert str(out["projection_version"]).startswith("gen-")
    assert out["required_knowledge_watermark"] == out["source_knowledge_high_watermark"]
    assert out["stale"] is False
    assert out["estimated_tokens"] <= 512  # type: ignore[operator]


def test_cli_get_node_and_get_subtree(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, _ = _invoke(_base(db_path, out_path) + ["get-node", json.dumps({"node_id": "n-gates"})], capsys)
    assert code == 0
    assert out is not None
    assert out["node"]["node_id"] == "n-gates"  # type: ignore[index]
    assert out["node"]["version"] == 1  # type: ignore[index]
    assert out["audit_event_id"]

    code, out, _ = _invoke(
        _base(db_path, out_path) + ["get-subtree", json.dumps({"node_id": "P1", "token_budget": 512})], capsys
    )
    assert code == 0
    assert out is not None
    assert out["node_id"] == "P1"
    assert "n-gates" in {node["node_id"] for node in out["nodes"]}  # type: ignore[index]
    assert out["audit_event_id"]


def test_cli_log_event_and_record_outcome(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    node_payload = {
        "node_id": "n-cli",
        "parent_id": "P1",
        "kind": "child",
        "domain": "P1",
        "summary": "Added through the CLI",
        "provenance": ["soul://corpus/cli.md"],
        "source_version_id": "sv-test",
    }
    code, out, err = _invoke(_base(db_path, out_path) + ["log-event", json.dumps(node_payload := node_payload)], capsys)
    assert code == 0, err
    assert out is not None
    assert out["audit_event_id"]
    assert out["node_version"] == 1
    assert out["knowledge_seq"] is not None

    evidence_payload = {
        "evidence_id": "ev-cli-1",
        "node_id": "n-cli",
        "source_version_id": "sv-test",
        "source_uri": "soul://corpus/cli.md",
        "observed_at": "2026-10-20T00:00:00+00:00",
        "context_tags": ["gates"],
        "independence_key": "ev-cli-1",
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
    code, out, err = _invoke(_base(db_path, out_path) + ["record-outcome", json.dumps(evidence_payload)], capsys)
    assert code == 0, err
    assert out is not None
    assert out["audit_event_id"]
    assert out["audit_event_seq"]


def test_cli_evaluate_returns_score_contract(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["evaluate", json.dumps({"node_id": "n-gates"})], capsys)
    assert code == 0, err
    assert out is not None
    assert out["node_id"] == "n-gates"
    for field in ("p", "m", "e", "u_p", "u_m", "u_e", "overall_uncertainty", "g"):
        assert field in out
    assert 1 <= int(out["professional_level"]) <= 5  # type: ignore[arg-type]
    assert out["audit_event_id"]


def test_cli_review_returns_decision(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["review", json.dumps({"node_id": "n-gates"})], capsys)
    assert code == 0, err
    assert out is not None
    assert out["node_id"] == "n-gates"
    assert "decision" in out
    assert out["audit_event_id"]


def test_cli_propose_apply_rollback_chain(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    propose_payload = {
        "node_id": "n-gates",
        "diff": [{"op": "set_summary", "value": "Run every gate before each commit"}],
        "rationale": "sharpen the summary",
    }
    code, out, err = _invoke(_base(db_path, out_path) + ["propose-improvement", json.dumps(propose_payload)], capsys)
    assert code == 0, err
    assert out is not None
    assert out["audit_event_id"]
    proposal = out["proposal"] if "proposal" in out else out
    proposal_id = proposal["proposal_id"]  # type: ignore[index]
    assert str(proposal_id).startswith("prop_")

    code, out, err = _invoke(
        _base(db_path, out_path) + ["apply-improvement", json.dumps({"proposal_id": proposal_id})], capsys
    )
    assert code == 0, err
    assert out is not None
    assert out["status"] == "applied"
    assert out["from_version"] == 1
    assert out["to_version"] == 2
    assert out["audit_event_id"]

    code, out, err = _invoke(_base(db_path, out_path) + ["rollback", json.dumps({"node_id": "n-gates"})], capsys)
    assert code == 0, err
    assert out is not None
    assert out["status"] == "rolled_back"
    assert out["audit_event_id"]


# ------------------------------------------------------------------- ops cmds


def test_cli_rebuild_backup_restore_health(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["health"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["ok"] is True

    code, out, err = _invoke(_base(db_path, out_path) + ["rebuild"], capsys)
    assert code == 0, err
    assert out is not None
    assert str(out["projection_version"]).startswith("gen-")

    backup_path = tmp_path / "backup.sqlite3"
    code, out, err = _invoke(_base(db_path, out_path) + ["backup", json.dumps({"dest_path": str(backup_path)})], capsys)
    assert code == 0, err
    assert out is not None
    assert backup_path.exists()
    assert out["sha256"]

    restored_path = tmp_path / "restored.sqlite3"
    code, out, err = _invoke(
        _base(db_path, out_path)
        + ["restore", json.dumps({"backup_path": str(backup_path), "dest_path": str(restored_path)})],
        capsys,
    )
    assert code == 0, err
    assert out is not None
    assert restored_path.exists()

    # The restored store serves reads through a fresh CLI invocation.
    code, out, err = _invoke(
        ["--store", str(restored_path), "--output-root", str(out_path), "get-node", json.dumps({"node_id": "n-gates"})],
        capsys,
    )
    assert code == 0, err
    assert out is not None
    assert out["node"]["node_id"] == "n-gates"  # type: ignore[index]


# ----------------------------------------------------------- failure contract


def test_cli_invalid_json_gets_completion_audit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    before = _audit_count(db_path)
    code, out, err = _invoke(_base(db_path, out_path) + ["query", "{not json"], capsys)
    assert code == 2
    assert out is None
    assert err is not None
    envelope = err["error"]  # type: ignore[index]
    assert envelope["code"] == "VALIDATION_ERROR"  # type: ignore[index]
    assert envelope["audit_event_id"]  # type: ignore[index]
    # Exactly one completion audit record was journaled for the rejection.
    assert _audit_count(db_path) == before + 1


def test_cli_unknown_field_gets_validation_audit(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    before = _audit_count(db_path)
    payload = {"purpose": "x", "token_budget": 512, "bogus_field": True}
    code, _, err = _invoke(_base(db_path, out_path) + ["query", json.dumps(payload)], capsys)
    assert code == 2
    assert err is not None
    assert err["error"]["code"] == "VALIDATION_ERROR"  # type: ignore[index]
    assert _audit_count(db_path) == before + 1


def test_cli_typed_error_envelope(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code, _, err = _invoke(_base(db_path, out_path) + ["get-node", json.dumps({"node_id": "n-absent"})], capsys)
    assert code == 1
    assert err is not None
    envelope = err["error"]  # type: ignore[index]
    assert envelope["code"] == "NODE_NOT_FOUND"  # type: ignore[index]
    assert envelope["retryable"] is False  # type: ignore[index]
    assert envelope["audit_event_id"]  # type: ignore[index]


def test_cli_exactly_one_final_audit_per_invocation(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    before = _audit_count(db_path)
    code, out, _ = _invoke(
        _base(db_path, out_path) + ["query", json.dumps({"purpose": "gates", "domain": "P1", "token_budget": 512})],
        capsys,
    )
    assert code == 0
    assert _audit_count(db_path) == before + 1
    assert out is not None
    events = _audit_events_for(db_path, str(out["audit_event_id"]))
    assert len(events) == 1


def test_cli_survives_restart(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """Two fully separate CLI processes (fresh stacks) share the store."""
    db_path, out_path = _seeded_paths(tmp_path)
    for _ in range(2):
        code, out, err = _invoke(
            _base(db_path, out_path)
            + ["query", json.dumps({"purpose": "run the gates", "domain": "P1", "token_budget": 512})],
            capsys,
        )
        assert code == 0, err
        assert out is not None
        assert "n-gates" in {node["node_id"] for node in out["results"]}  # type: ignore[index]


def test_cli_payload_from_stdin(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    monkeypatch.setattr("sys.stdin", io.StringIO(json.dumps({"node_id": "n-gates"})))
    code, out, err = _invoke(_base(db_path, out_path) + ["get-node", "-"], capsys)
    assert code == 0, err
    assert out is not None
    assert out["node"]["node_id"] == "n-gates"  # type: ignore[index]


def test_cli_serve_refuses_wildcard_bind_without_api_key(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    code = main(_base(db_path, out_path) + ["serve", "--http", "0.0.0.0:8787"])
    captured = capsys.readouterr()
    assert code != 0
    envelope = json.loads(captured.err)
    assert envelope["error"]["code"] == "BIND_POLICY"


# -------------------------------------------------- wave-2 F6: argv secrets


def test_cli_serve_api_key_env_avoids_argv(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    """--api-key-env NAME reads the key from the environment, never argv."""
    db_path, out_path = _seeded_paths(tmp_path)
    monkeypatch.setenv("SOUL_TEST_API_KEY", "secret-from-env")
    captured_kwargs: dict[str, object] = {}

    def fake_serve(stack: object, **kwargs: object) -> None:
        captured_kwargs.update(kwargs)

    monkeypatch.setattr("soul_knowledge.cli.serve", fake_serve)
    code = main(
        _base(db_path, out_path) + ["serve", "--socket", str(tmp_path / "s.sock"), "--api-key-env", "SOUL_TEST_API_KEY"]
    )
    assert code == 0
    assert captured_kwargs.get("api_key") == "secret-from-env"


def test_cli_serve_api_key_env_missing_var_fails_closed(
    tmp_path: Path, capsys: pytest.CaptureFixture[str], monkeypatch: pytest.MonkeyPatch
) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    monkeypatch.delenv("SOUL_MISSING_KEY", raising=False)
    code = main(_base(db_path, out_path) + ["serve", "--api-key-env", "SOUL_MISSING_KEY"])
    captured = capsys.readouterr()
    assert code == 2
    assert "SOUL_MISSING_KEY" in captured.err


def test_cli_serve_api_key_and_api_key_env_are_mutually_exclusive(tmp_path: Path) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    with pytest.raises(SystemExit) as caught:
        main(_base(db_path, out_path) + ["serve", "--api-key", "inline", "--api-key-env", "SOUL_TEST_API_KEY"])
    assert caught.value.code == 2


# ------------------------------------------- wave-2 F1: ops path containment


def test_cli_backup_outside_ops_root_fails_closed(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    outside = tmp_path.parent / f"wave2-cli-evil-{os.getpid()}.sqlite3"
    try:
        code, _, err = _invoke(_base(db_path, out_path) + ["backup", json.dumps({"dest_path": str(outside)})], capsys)
        assert code == 1
        assert err is not None
        assert err["error"]["code"] == "PATH_ESCAPE"  # type: ignore[index]
        assert not outside.exists(), "the old arbitrary-path write must fail closed"
    finally:
        outside.unlink(missing_ok=True)


def test_cli_ops_root_option_allows_a_configured_root(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    db_path, out_path = _seeded_paths(tmp_path)
    other_root = tmp_path / "backups"
    other_root.mkdir()
    dest = other_root / "backup.sqlite3"
    code, out, err = _invoke(
        ["--store", str(db_path), "--output-root", str(out_path), "--ops-root", str(other_root)]
        + ["backup", json.dumps({"dest_path": str(dest)})],
        capsys,
    )
    assert code == 0, err
    assert out is not None
    assert dest.exists()
    assert out["sha256"]

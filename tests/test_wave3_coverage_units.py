"""Coverage wave 3 (SUB-51): unit-level pins for uncovered guard/error branches.

Every test targets a branch the existing suite leaves red in
``pytest --cov=soul_knowledge``: typed error paths, boundary guards and
fail-closed sentinels discovered by reading ``src/`` at HEAD 2f5e711. No
trivial getter tests; property-style tests stay deterministic (hypothesis
``derandomize=True`` where used — this file is pure example-based).
"""

from __future__ import annotations

import json
import sqlite3
from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path
from typing import Any

import pytest
from pydantic import TypeAdapter

from soul_knowledge import runtime_guard
from soul_knowledge.audit import AuditLog
from soul_knowledge.contracts import RecommendationItem, attempt_promotion
from soul_knowledge.domain import (
    Event,
    EventKind,
    Node,
    NodeKind,
    PrimaryDomain,
    ScoreVector,
    StrictDecimal,
)
from soul_knowledge.improvement import (
    DiffOp,
    DiffOperation,
    ImprovementProposal,
    ProposalValidationError,
    UnknownImprovement,
    UnknownNode,
    _apply_diff,
    default_regression_gate,
    merge_nodes,
    retain_improvement,
    rollback_improvement,
    validate_proposal,
)
from soul_knowledge.observability import (
    MetricsRegistry,
    StructuredLogger,
    check_health,
    export_query_audit,
)
from soul_knowledge.review import (
    DEFAULT_REVIEW_POLICY,
    LifecycleStatus,
    ReviewSignal,
    decide,
)
from soul_knowledge.scoring import ScoringConfig, ScoringConfigError, _jaccard  # type: ignore[attr-defined]
from soul_knowledge.security import PathEscapeError, host_allowed, resolve_within_root
from soul_knowledge.source_registry import SourceKind, SourceRegistry
from soul_knowledge.source_watcher import SourceWatcher, WatcherStateError
from soul_knowledge.store import EventStore, InvalidRelation, StoreError

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)


def _node(node_id: str = "n-1") -> Node:
    return Node(
        node_id=node_id,
        kind=NodeKind.CHILD,
        domain=PrimaryDomain.P1,
        summary=f"summary for {node_id}",
        source_version_id="sv-test",
    )


# --------------------------------------------------------------------- domain


def test_strict_decimal_accepts_int() -> None:
    assert TypeAdapter(StrictDecimal).validate_python(7) == Decimal(7)


def test_strict_decimal_rejects_uninterpretable_type() -> None:
    with pytest.raises(ValueError, match="cannot interpret"):
        TypeAdapter(StrictDecimal).validate_python(object())


def test_node_folds_professional_level_into_existing_score_vector() -> None:
    node = Node.model_validate(
        {
            "node_id": "n-1",
            "kind": NodeKind.CHILD,
            "domain": PrimaryDomain.P1,
            "summary": "x",
            "source_version_id": "sv-test",
            "scores": ScoreVector.zero(),
            "professional_level": 4,
        }
    )
    assert node.scores.professional_level == 4


def test_event_rejects_evidence_of_a_different_node() -> None:
    evidence = {
        "evidence_id": "e-1",
        "node_id": "n-other",
        "source_version_id": "sv-test",
        "source_uri": "soul://corpus/a.md",
        "observed_at": NOW,
        "independence_key": "grp",
        "outcome": "0.5",
        "correctness": "0.9",
        "reliability": "0.8",
        "reproducibility": "0.7",
        "scope_fit": "0.6",
        "successful_use": "0.5",
        "task_coverage": "0.4",
        "quality_at_use": "0.75",
        "contradiction": "0.1",
        "utilization_u": "0.5",
        "latency_ms": 1000,
        "compute_ms": 100,
        "output_chars": 500,
    }
    with pytest.raises(ValueError, match="embedded evidence"):
        Event(kind=EventKind.EVIDENCE_APPEND, node_id="n-1", domain=PrimaryDomain.P1, evidence=evidence)  # type: ignore[arg-type]


# ------------------------------------------------------------------ contracts


def _recommendation_item(**overrides: object) -> dict[str, object]:
    base: dict[str, object] = {
        "action_id": "act-1",
        "label": "do the thing",
        "source_node_ids": ["n-1", "n-2"],
        "source_node_versions": [1, 1],
        "evidence_ids": ["e-1", "e-2"],
        "learned_class": "gates",
        "reason_codes": ["evidence-threshold"],
        "expiry": "2027-01-01T00:00:00+00:00",
        "promoted": False,
    }
    base.update(overrides)
    return base


def test_recommendation_item_rejects_mismatched_version_list() -> None:
    with pytest.raises(ValueError, match="same length"):
        RecommendationItem.model_validate(_recommendation_item(source_node_versions=[1]))


def test_attempt_promotion_accepts_two_or_more_observations() -> None:
    verdict = attempt_promotion(RecommendationItem.model_validate(_recommendation_item()))
    assert verdict.accepted is True


# -------------------------------------------------------------------- scoring


def _config_toml() -> str:
    return (Path(__file__).resolve().parent.parent / "src" / "soul_knowledge" / "scoring_config.toml").read_text(
        encoding="utf-8"
    )


def _write_config(tmp_path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
    import tomllib

    data = tomllib.loads(_config_toml())
    mutate(data)
    target = tmp_path / "config.toml"
    target.write_text(_toml_dump(data), encoding="utf-8")
    return target


def _toml_dump(data) -> str:  # type: ignore[no-untyped-def]
    lines: list[str] = []
    scalars = {k: v for k, v in data.items() if not isinstance(v, dict) and not isinstance(v, list)}
    for key, value in scalars.items():
        lines.append(f"{key} = {_toml_value(value)}")
    for key, value in data.items():
        if isinstance(value, dict):
            lines.append(f"\n[{key}]")
            for sub_key, sub_value in value.items():
                lines.append(f"{sub_key} = {_toml_value_dump(sub_value)}")
        elif isinstance(value, list):
            lines.append(f"{key} = {_toml_value_dump(value)}")
    return "\n".join(lines) + "\n"


def _toml_value_dump(value: object) -> str:
    if isinstance(value, str):
        return json.dumps(value)
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, (int, float)):
        return repr(value)
    if isinstance(value, list):
        return "[" + ", ".join(_toml_value_dump(item) for item in value) + "]"
    raise AssertionError(f"unsupported value {value!r}")


def _toml_value(value: object) -> str:
    return _toml_value_dump(value)


def test_scoring_config_rejects_non_string_decimal(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["half_life_days"].__setitem__("p", 30))
    with pytest.raises(ScoringConfigError, match="must be a decimal string"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_undecimal_string(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["half_life_days"].__setitem__("p", "not-a-decimal"))
    with pytest.raises(ScoringConfigError, match="is not a decimal"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_bool_where_int_expected(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["m_bin"].__setitem__("min_independence_groups", True))
    with pytest.raises(ScoringConfigError, match="must be an integer"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_wrong_key_set(tmp_path: Path) -> None:
    def mutate(data) -> None:  # type: ignore[no-untyped-def]
        del data["half_life_days"]["p"]

    path = _write_config(tmp_path, mutate)
    with pytest.raises(ScoringConfigError, match="must contain exactly"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_non_positive_half_life(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["half_life_days"].__setitem__("p", "0"))
    with pytest.raises(ScoringConfigError, match="half-lives must be positive"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_p_weights_not_summing_to_one(tmp_path: Path) -> None:
    def mutate(data) -> None:  # type: ignore[no-untyped-def]
        first = sorted(data["p_weights"])[0]
        data["p_weights"][first] = "0.99"

    path = _write_config(tmp_path, mutate)
    with pytest.raises(ScoringConfigError, match="P weights must sum"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_e_cost_weights_not_summing_to_one(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["e_cost"].__setitem__("latency_weight", "0.99"))
    with pytest.raises(ScoringConfigError, match="E cost weights must sum"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_unsorted_level_thresholds(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["level"].__setitem__("thresholds", ["0.9", "0.1", "0.2", "0.3"]))
    with pytest.raises(ScoringConfigError, match="four ascending"):
        ScoringConfig.load(path)


def test_scoring_config_rejects_composite_exponents_not_summing_to_one(tmp_path: Path) -> None:
    path = _write_config(tmp_path, lambda data: data["composite"].__setitem__("p_exponent", "0.99"))
    with pytest.raises(ScoringConfigError, match="composite exponents must sum"):
        ScoringConfig.load(path)


def test_jaccard_of_two_empty_tag_sets_is_one() -> None:
    assert _jaccard(frozenset(), frozenset()) == Decimal(1)


# ------------------------------------------------------------- scoring engine


def _e_evidence(item_overrides: dict[str, object]) -> dict[str, object]:
    base: dict[str, object] = {
        "evidence_id": "e-1",
        "node_id": "n-1",
        "source_version_id": "sv-test",
        "source_uri": "soul://corpus/a.md",
        "observed_at": "2026-10-20T00:00:00+00:00",
        "independence_key": "grp",
        "outcome": "0.5",
        "correctness": "0.9",
        "reliability": "0.8",
        "reproducibility": "0.7",
        "scope_fit": "0.6",
        "successful_use": "0.5",
        "task_coverage": "0.4",
        "quality_at_use": "0.75",
        "contradiction": "0.1",
        "utilization_u": "0.5",
        "latency_ms": 1000,
        "compute_ms": 100,
        "output_chars": 500,
    }
    base.update(item_overrides)
    return base


def test_e_evidence_task_coverage_outside_unit_interval_is_excluded() -> None:
    from soul_knowledge.scoring import _classify

    reason = _classify(_e_evidence({"task_coverage": "1.5"}), datetime(2026, 10, 25, tzinfo=UTC), frozenset())
    assert reason == "other_exclusions"


def test_e_evidence_non_positive_latency_is_excluded() -> None:
    from soul_knowledge.scoring import _classify

    reason = _classify(_e_evidence({"latency_ms": 0}), datetime(2026, 10, 25, tzinfo=UTC), frozenset())
    assert reason == "other_exclusions"


def test_e_evidence_non_positive_compute_is_excluded() -> None:
    from soul_knowledge.scoring import _classify

    reason = _classify(_e_evidence({"compute_ms": 0}), datetime(2026, 10, 25, tzinfo=UTC), frozenset())
    assert reason == "other_exclusions"


def test_e_evidence_negative_output_chars_is_excluded() -> None:
    from soul_knowledge.scoring import _classify

    reason = _classify(_e_evidence({"output_chars": -1}), datetime(2026, 10, 25, tzinfo=UTC), frozenset())
    assert reason == "other_exclusions"


# --------------------------------------------------------------- observability


def test_metrics_counter_refuses_negative_increment() -> None:
    registry = MetricsRegistry()
    with pytest.raises(ValueError, match="counters only move forward"):
        registry.increment("ops", -1)


def test_query_audit_export_with_no_operations_is_empty_string(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        assert export_query_audit(store, operations=()) == ""
    finally:
        store.close()


def test_structured_logger_warning_and_error_levels_record() -> None:
    records: list[dict[str, object]] = []
    logger = StructuredLogger(records.append, component="test")  # type: ignore[arg-type]
    logger.warning("warn-event", detail="w")
    logger.error("error-event", detail="e")
    levels = [record["level"] for record in logger.records()]
    assert levels == ["warning", "error"]
    assert records[0]["event"] == "warn-event"


def test_check_health_reports_closed_store_without_raising(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    store.close()
    report = check_health(store)
    assert report.ok is False
    assert report.checks["store_select"] is False
    assert "store_select" in report.details
    assert report.checks["chain"] is False
    assert "chain" in report.details
    assert report.checks["migrations"] is False
    assert "migrations" in report.details


# ---------------------------------------------------------------------- review


def _review_signal(**overrides: object) -> ReviewSignal:
    base: dict[str, object] = {
        "node_id": "n-1",
        "current_status": LifecycleStatus.ACTIVE,
        "p": "0.5",
        "m": "0.5",
        "e": "0.5",
        "uncertainty": "0.1",
        "evidence_count": 3,
        "qualifying_review_streak": 0,
        "last_transition_at": None,
        "at": NOW,
    }
    base.update(overrides)
    return ReviewSignal.model_validate(base)


def test_ready_to_promote_node_dropping_below_the_promotion_floor_demotes() -> None:
    policy = DEFAULT_REVIEW_POLICY
    signal = _review_signal(
        current_status=LifecycleStatus.READY_TO_PROMOTE,
        p=str(policy.promotion_p_threshold - Decimal("0.05")),
        uncertainty=str(policy.promotion_uncertainty_max),
    )
    decision = decide(signal, policy)
    assert decision.desired_status == LifecycleStatus.MAINTAIN
    assert "promotion-criteria-lost" in decision.reasons


# ----------------------------------------------------------------- improvement


def test_regression_gate_flags_source_version_change() -> None:
    before = _node("n-1")
    after = before.model_copy(update={"source_version_id": "sv-other"})
    assert "source-version-changed" in default_regression_gate(before, after)


def test_validate_proposal_unknown_node_raises(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        proposal = ImprovementProposal(
            proposal_id="p-1",
            node_id="n-ghost",
            base_version=1,
            diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="x"),),
            rationale="because",
            impact="low",
            actor="test",
            created_at=NOW,
        )
        with pytest.raises(UnknownNode, match="unknown node"):
            validate_proposal(store, proposal)
    finally:
        store.close()


def test_validate_proposal_requires_key_for_payload_ops(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        proposal = ImprovementProposal(
            proposal_id="p-1",
            node_id="n-1",
            base_version=1,
            diff=(DiffOperation(op=DiffOp.SET_PAYLOAD_KEY, key="", value="x"),),
            rationale="because",
            impact="low",
            actor="test",
            created_at=NOW,
        )
        # Seed the node so the unknown-node guard does not fire first.
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        with pytest.raises(ProposalValidationError, match="requires a payload key"):
            validate_proposal(store, proposal)
    finally:
        store.close()


def test_validate_proposal_rejects_unknown_relation_kind(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        proposal = ImprovementProposal(
            proposal_id="p-1",
            node_id="n-1",
            base_version=1,
            diff=(DiffOperation(op=DiffOp.ADD_RELATION, value={"kind": "bogus", "target_node_id": "n-2"}),),
            rationale="because",
            impact="low",
            actor="test",
            created_at=NOW,
        )
        with pytest.raises(ProposalValidationError, match="unknown relation kind"):
            validate_proposal(store, proposal)
    finally:
        store.close()


def test_validate_proposal_add_relation_requires_target(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        proposal = ImprovementProposal(
            proposal_id="p-1",
            node_id="n-1",
            base_version=1,
            diff=(DiffOperation(op=DiffOp.ADD_RELATION, value={"kind": "depends_on"}),),
            rationale="because",
            impact="low",
            actor="test",
            created_at=NOW,
        )
        with pytest.raises(ProposalValidationError, match="requires target_node_id"):
            validate_proposal(store, proposal)
    finally:
        store.close()


def test_apply_diff_covers_scope_minus_and_remove_ops() -> None:
    node = _node("n-1").model_copy(
        update={"plus": ("keep", "drop"), "minus": (), "context_tags": ("old",), "payload": {"gone": 1}}
    )
    diff = (
        DiffOperation(op=DiffOp.SET_SCOPE, value="narrow"),
        DiffOperation(op=DiffOp.REMOVE_PAYLOAD_KEY, key="gone"),
        DiffOperation(op=DiffOp.REMOVE_PLUS, value="drop"),
        DiffOperation(op=DiffOp.ADD_MINUS, value="risk"),
        DiffOperation(op=DiffOp.REMOVE_MINUS, value="risk"),
        DiffOperation(op=DiffOp.ADD_MINUS, value="real-risk"),
        DiffOperation(op=DiffOp.REMOVE_CONTEXT_TAG, value="old"),
    )
    updated = _apply_diff(node, diff, NOW)
    assert updated.scope == "narrow"
    assert "gone" not in updated.payload
    assert updated.plus == ("keep",)
    assert updated.minus == ("real-risk",)
    assert updated.context_tags == ()


def test_retain_improvement_unknown_node_raises(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        proposal = ImprovementProposal(
            proposal_id="p-1",
            node_id="n-ghost",
            base_version=1,
            diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="x"),),
            rationale="because",
            impact="low",
            actor="test",
            created_at=NOW,
        )
        with pytest.raises(UnknownNode, match="cannot retain"):
            retain_improvement(store, proposal)
    finally:
        store.close()


def _approval_for(proposal_id: str):  # type: ignore[no-untyped-def]
    from soul_knowledge.improvement import ApprovalRecord

    return ApprovalRecord(proposal_id=proposal_id, approved_by="ops", decision="approved", decided_at=NOW, note="")


def test_merge_unknown_source_raises(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        approval = _approval_for("merge:n-ghost:n-1")
        with pytest.raises(UnknownNode, match="merge source"):
            merge_nodes(store, source_node_id="n-ghost", target_node_id="n-1", approval=approval)
    finally:
        store.close()


def test_merge_unknown_target_raises(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        approval = _approval_for("merge:n-1:n-ghost")
        with pytest.raises(UnknownNode, match="merge target"):
            merge_nodes(store, source_node_id="n-1", target_node_id="n-ghost", approval=approval)
    finally:
        store.close()


def test_improvement_rollback_unknown_node_raises(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        with pytest.raises(UnknownNode, match="cannot roll back unknown node"):
            rollback_improvement(store, node_id="n-ghost")
    finally:
        store.close()


def test_improvement_rollback_without_revision_raises(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        with pytest.raises(UnknownImprovement, match="has no revision 99"):
            rollback_improvement(store, node_id="n-1", to_version=99)
    finally:
        store.close()


# ------------------------------------------------------------ runtime guards


def test_is_forbidden_module_rejects_empty_name() -> None:
    assert runtime_guard.is_forbidden_module("") is False


def test_forbidden_entry_point_detection_for_console_script() -> None:
    class _EntryPoint:
        def __init__(self, name: str, group: str) -> None:
            self.name = name
            self.group = group
            self.value = f"{name}:main"

    class _Distribution:
        entry_points = [_EntryPoint("graphify-mcp", "console_scripts")]

    flagged = runtime_guard._is_forbidden_entry_point(_Distribution.entry_points[0])  # noqa: SLF001
    assert flagged is True


def test_install_import_guard_returns_installed_guard() -> None:
    guard = runtime_guard.install_import_guard()
    try:
        assert isinstance(guard, runtime_guard.ImportGuard)
    finally:
        guard.uninstall()


def test_assert_clean_fails_closed_when_a_blocked_root_is_still_resolvable() -> None:
    # The finder's blocklist is the global FORBIDDEN_IMPORT_ROOTS; a custom
    # blocked root outside that list is resolvable, and assert_clean must say
    # so loudly instead of passing vacuously.
    guard = runtime_guard.import_guard(["json"])
    with pytest.raises(runtime_guard.ForbiddenImportError, match="did not block"):
        guard.assert_clean()


def test_socket_guard_double_install_returns_self() -> None:
    guard = runtime_guard.SocketGuard()
    first = guard.install()
    try:
        assert guard.install() is first
    finally:
        guard.uninstall()


def test_socket_guard_allows_unix_connect_ex(tmp_path: Path) -> None:
    import socket

    listener_path = tmp_path / "l.sock"
    listener = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    listener.bind(str(listener_path))
    listener.listen(1)
    guard = runtime_guard.SocketGuard()
    guard.install()
    try:
        client = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
        try:
            result = client.connect_ex(str(listener_path))
            assert result == 0
        finally:
            client.close()
    finally:
        guard.uninstall()
        listener.close()


def test_socket_guard_allows_unix_sendto(tmp_path: Path) -> None:
    import socket

    receiver_path = tmp_path / "r.sock"
    receiver = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
    receiver.bind(str(receiver_path))
    guard = runtime_guard.SocketGuard()
    guard.install()
    try:
        sender = socket.socket(socket.AF_UNIX, socket.SOCK_DGRAM)
        try:
            sent = sender.sendto(b"ping", str(receiver_path))
            assert sent == 4
        finally:
            sender.close()
        assert receiver.recv(8) == b"ping"
    finally:
        guard.uninstall()
        receiver.close()


def test_socket_guard_passes_unix_path_create_connection_through() -> None:
    guard = runtime_guard.SocketGuard()
    guard.install()
    try:
        with pytest.raises(Exception) as excinfo:  # noqa: B017 - the point is the denial type
            import socket

            path_address: Any = "/nonexistent/soul-wave3.sock"
            socket.create_connection(path_address, timeout=0.05)
        assert not isinstance(excinfo.value, runtime_guard.OutboundSocketDenied)
    finally:
        guard.uninstall()


def test_handler_registry_accepts_a_plain_mapping() -> None:
    registry = {"sampling/createMessage": object()}
    assert runtime_guard._handler_registry(registry) is registry  # noqa: SLF001


def test_assert_mcp_capabilities_rejects_non_mapping() -> None:
    with pytest.raises(runtime_guard.CapabilityViolation, match="must be a mapping"):
        runtime_guard.assert_mcp_capabilities(["tools"])


def test_outbound_probe_is_true_while_the_guard_denies() -> None:
    assert runtime_guard._probe_outbound_denied() is True  # noqa: SLF001


def test_outbound_probe_reports_false_when_the_denied_connect_raises_oserror(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    def _raise_oserror(*args: object, **kwargs: object) -> None:
        raise OSError("network unreachable")

    # The probe self-installs a SocketGuard that (at guard depth 0) rebinds
    # socket.create_connection to the module-global shim; under an already
    # installed guard the existing socket binding survives. Patch both.
    monkeypatch.setattr(runtime_guard, "_denied_create_connection", _raise_oserror)
    import socket

    monkeypatch.setattr(socket, "create_connection", _raise_oserror)
    assert runtime_guard._probe_outbound_denied() is False  # noqa: SLF001


def test_outbound_probe_reports_false_when_the_connect_succeeds(monkeypatch: pytest.MonkeyPatch) -> None:
    import socket

    def _succeeds(*args: object, **kwargs: object) -> None:
        return None

    monkeypatch.setattr(runtime_guard, "_denied_create_connection", _succeeds)
    monkeypatch.setattr(socket, "create_connection", _succeeds)
    assert runtime_guard._probe_outbound_denied() is False  # noqa: SLF001


def test_import_probe_fails_closed_when_a_resolvable_root_is_unblocked(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    class _NoopGuard:
        def install(self) -> _NoopGuard:
            return self

        def uninstall(self) -> None:
            pass

        def assert_not_loaded(self) -> None:
            pass

    monkeypatch.setattr(runtime_guard, "import_guard", lambda roots: _NoopGuard())
    monkeypatch.setattr(runtime_guard, "_resolvable_without_import", lambda root: True)
    monkeypatch.setattr(runtime_guard, "find_spec", lambda root: object())
    with pytest.raises(runtime_guard.ForbiddenImportError, match="did not block"):
        runtime_guard._probe_imports_blocked()  # noqa: SLF001


def test_verify_runtime_fails_closed_when_outbound_probe_is_vacuous(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    monkeypatch.setattr(runtime_guard, "_probe_outbound_denied", lambda: False)
    with pytest.raises(runtime_guard.OutboundSocketDenied, match="did not deny"):
        runtime_guard.verify_runtime(env={})


def test_verify_runtime_fails_closed_when_import_probe_is_vacuous(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(runtime_guard, "_probe_outbound_denied", lambda: True)
    monkeypatch.setattr(
        runtime_guard, "_probe_imports_blocked", lambda: {"actively_blocked": [], "not_installed": ["x"]}
    )
    with pytest.raises(runtime_guard.ForbiddenImportError, match="vacuous"):
        runtime_guard.verify_runtime(env={})


def test_verify_runtime_rejects_non_mapping_capability_snapshot() -> None:
    with pytest.raises(runtime_guard.CapabilityViolation, match="must be a mapping"):
        runtime_guard.verify_runtime(env={}, mcp_capabilities=["tools"])  # type: ignore[arg-type]


# ---------------------------------------------------------------------- store


def test_live_store_at_ignores_in_memory_databases() -> None:
    assert EventStore.live_store_at(":memory:") is None


def test_event_store_context_manager_closes(tmp_path: Path) -> None:
    path = tmp_path / "soul.sqlite3"
    with EventStore(path) as store:
        assert store.knowledge_high_watermark() == 0
    with pytest.raises(sqlite3.ProgrammingError):
        store.knowledge_high_watermark()


def test_verify_chain_detects_a_tampered_anchor_head(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        store.append_domain(
            Event(kind=EventKind.NODE_UPSERT, node_id="n-1", domain=PrimaryDomain.P1, node=_node("n-1")),
            idempotency_key="test:seed:n-1",
        )
        with store.transaction() as conn:
            conn.execute("UPDATE system_state SET value = ? WHERE key = 'chain_head_hash'", ("0" * 64,))
        report = store.verify_chain()
        assert report.ok is False
        assert report.first_bad_seq is not None
    finally:
        store.close()


def test_relation_event_for_unknown_node_is_an_invalid_relation(tmp_path: Path) -> None:
    from soul_knowledge.domain import Relation, RelationKind

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        event = Event(
            kind=EventKind.RELATION_ADD,
            node_id="n-ghost",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.DEPENDS_ON, source_node_id="n-ghost", target_node_id="n-other"),
        )
        with pytest.raises(InvalidRelation, match="references unknown node"):
            store.append_domain(event, idempotency_key="test:rel:ghost")
    finally:
        store.close()


def test_commit_reports_a_combined_error_when_the_failure_audit_also_fails(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    from soul_knowledge import audit as audit_module
    from soul_knowledge.domain import Relation, RelationKind
    from soul_knowledge.events import Invocation

    store = EventStore(tmp_path / "soul.sqlite3")
    try:

        def _failing_complete(self: AuditLog, *args: object, **kwargs: object) -> object:
            raise sqlite3.OperationalError("audit table gone")

        monkeypatch.setattr(audit_module.AuditLog, "complete", _failing_complete)
        event = Event(
            kind=EventKind.RELATION_ADD,
            node_id="n-ghost",
            domain=PrimaryDomain.P1,
            relation=Relation(kind=RelationKind.DEPENDS_ON, source_node_id="n-ghost", target_node_id="n-other"),
        )
        invocation = Invocation.create("knowledge.log_event", "0" * 64, actor="test")
        with pytest.raises(StoreError, match="failure audit could not be recorded"):
            store.commit(event, invocation=invocation)
    finally:
        store.close()


def test_journal_outcome_json_is_none_for_an_incomplete_invocation(tmp_path: Path) -> None:
    from soul_knowledge.events import Invocation

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        audit = AuditLog(store)
        journal = audit.begin(Invocation.create("knowledge.query", "1" * 64, actor="test"))
        assert store.journal_outcome_json(journal.invocation_id) is None
    finally:
        store.close()


def test_heal_lying_projection_watermark_without_marker_is_false(tmp_path: Path) -> None:
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        assert store.heal_lying_projection_watermark() is False
    finally:
        store.close()


def test_duplicate_journal_begin_is_a_typed_store_error(tmp_path: Path) -> None:
    from soul_knowledge.events import Invocation

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        audit = AuditLog(store)
        invocation = Invocation.create("knowledge.query", "2" * 64, actor="test")
        audit.begin(invocation)
        with pytest.raises(StoreError, match="already has a journal"):
            audit.begin(invocation)
    finally:
        store.close()


# -------------------------------------------------------------------- security


def test_resolve_within_root_refuses_a_file_root(tmp_path: Path) -> None:
    root = tmp_path / "a-file"
    root.write_text("x", encoding="utf-8")
    with pytest.raises(PathEscapeError, match="is not a directory"):
        resolve_within_root(root, "anything")


def test_host_allowed_rejects_a_host_that_normalizes_to_empty() -> None:
    assert host_allowed("", ("localhost",)) is False
    assert host_allowed(None, ("localhost",)) is False


# -------------------------------------------------------------- source watcher


def test_watcher_snapshot_skips_non_candidate_files(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Title\nbody\n", encoding="utf-8")
    (tmp_path / "data.bin").write_bytes(b"\x00\x01")
    watcher = SourceWatcher(tmp_path, state_path=tmp_path / "state.json", patterns=(".md",))
    snapshot = watcher.snapshot()
    assert sorted(snapshot) == ["note.md"]


def test_watcher_refuses_unreadable_state(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Title\nbody\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text("{not json", encoding="utf-8")
    watcher = SourceWatcher(tmp_path, state_path=state, patterns=(".md",))
    with pytest.raises(WatcherStateError, match="unreadable watcher state"):
        watcher.poll()


def test_watcher_refuses_state_anchored_to_another_root(tmp_path: Path) -> None:
    (tmp_path / "note.md").write_text("# Title\nbody\n", encoding="utf-8")
    state = tmp_path / "state.json"
    state.write_text(json.dumps({"root": str(tmp_path / "elsewhere"), "files": {}}), encoding="utf-8")
    watcher = SourceWatcher(tmp_path, state_path=state, patterns=(".md",))
    with pytest.raises(WatcherStateError, match="anchored to"):
        watcher.poll()


# ------------------------------------------------------------ source registry


def test_registry_scan_skips_directories_matching_the_pattern(tmp_path: Path) -> None:
    corpus = tmp_path / "corpus"
    (corpus / "dir.md").mkdir(parents=True)
    (corpus / "real.md").write_text("# Real\nbody\n", encoding="utf-8")
    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        registry = SourceRegistry(store)
        entries, _blob, _chars = registry._scan(corpus, SourceKind.SOUL_CORPUS)  # noqa: SLF001
        assert [entry.path for entry in entries] == ["real.md"]
    finally:
        store.close()


# --------------------------------------------------------- scheduler (utcnow)


def test_scheduler_defaults_to_utc_now(tmp_path: Path) -> None:
    from soul_knowledge.review import ReviewEngine
    from soul_knowledge.scheduler import DueReviewScheduler

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        scheduler = DueReviewScheduler(store, ReviewEngine(store))
        assert scheduler.scan_due() == []
    finally:
        store.close()


def test_score_items_with_zero_quality_evidence_selects_no_m_bin() -> None:
    # NOTE: the ``local_total == 0`` guard inside the M-bin loop is
    # structurally unreachable (bins only collect entries with q["m"] > 0);
    # this pins the observable behavior for zero-quality evidence instead.
    from soul_knowledge.scoring import score_items

    item = {
        "evidence_id": "e-1",
        "node_id": "n-1",
        "source_version_id": "sv-test",
        "source_uri": "soul://corpus/a.md",
        "observed_at": "2026-10-20T00:00:00+00:00",
        "context_tags": (),
        "independence_key": "grp",
        "outcome": "0.5",
        "correctness": "0.9",
        "reliability": "0.8",
        "reproducibility": "0.7",
        "scope_fit": "0.6",
        "successful_use": "0.5",
        "task_coverage": "0.4",
        "quality_at_use": "0",
        "contradiction": "0.1",
        "utilization_u": "0.5",
        "latency_ms": 1000,
        "compute_ms": 100,
        "output_chars": 500,
    }
    assessment = score_items([item], at=datetime(2026, 10, 25, tzinfo=UTC))
    assert assessment is not None


def test_score_engine_exposes_its_config(tmp_path: Path) -> None:
    from soul_knowledge.scoring import ScoreEngine

    store = EventStore(tmp_path / "soul.sqlite3")
    try:
        engine = ScoreEngine(store)
        assert engine.config is engine._config  # noqa: SLF001
        assert engine.config.contract_version
    finally:
        store.close()

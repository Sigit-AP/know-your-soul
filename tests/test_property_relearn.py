"""Property tests for the operator-gated relearn lifecycle (Task 6).

Wave-2 follow-up to ``docs/recovery/codebase-push-property-tests.md``; pins
the frozen contract ``docs/contracts/relearn-contract.md`` on arbitrary
evidence sets and arbitrary changed-file subsets:

* ``preview`` is deterministic and read-only, and its carried/quarantined
  classification always agrees with the ``apply`` receipt for the same mode;
* RESET always carries zero evidence and states the exact zero vector
  (P=M=E=0, U=1, level 1); a relearn record is a zero-weight
  ``knowledge_affecting=False`` admin event that never moves the knowledge
  watermark;
* ACCUMULATE partitions old evidence into carried/quarantined with no overlap
  and no loss, and the quarantine state equals the receipt's list;
* every apply takes a verified backup whose manifest hashes match the staged
  bytes, and rollback restores the byte-exact pre-relearn database;
* idempotent replay returns the original receipt; a key rebound to the other
  mode fails loud with ``IdempotencyConflict``;
* ``assert_source_compatible`` refuses exactly the ids outside the compatible
  set unless BOTH explicit flags are set.

All runs are deterministic (hypothesis ``derandomize=True``); every store is
a fresh file-backed database under a per-example temp directory.
"""

from __future__ import annotations

import hashlib
import json
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Literal

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.domain import (
    Event,
    EventKind,
    Evidence,
    Node,
    NodeKind,
    PrimaryDomain,
    ScoreVector,
)
from know_your_soul.relearn import (
    _RESET_SCORES,
    MIXED_SOURCE_VERSIONS,
    BackupUnavailable,
    MixedSourceVersions,
    NoPendingRelearn,
    OperatorDecisionRequired,
    RelearnApproval,
    RelearnEngine,
    RelearnError,
    UnknownRelearn,
)
from know_your_soul.source_registry import (
    RELEARN_PENDING,
    ParserVersions,
    SourceKind,
    SourceRegistry,
)
from know_your_soul.store import EventStore, IdempotencyConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

PARSER_VERSIONS = ParserVersions(
    parser_version="1.0.0",
    normalization_version="1.0.0",
    engine_version="0.9.95",
    scoring_contract_version="1.0.0",
)

Mode = Literal["RESET", "ACCUMULATE"]

_anchor_path = RelearnEngine._anchor_path  # noqa: SLF001 - pinned private helper

# The evidence anchors available to the scenario: a.md/b.md exist in the
# corpus manifest; ghost.md never does (the unmapped-anchor case).
_ANCHORS = ("a.md", "b.md", "ghost.md")


def _scratch() -> Path:
    return Path(tempfile.mkdtemp(prefix="soul-relearn-"))


def _node(node_id: str, source_version_id: str) -> Node:
    return Node.model_validate(
        {
            "node_id": node_id,
            "kind": NodeKind.LEAF,
            "domain": PrimaryDomain.P1,
            "source_version_id": source_version_id,
            "payload": {"fact": node_id},
            "summary": f"node {node_id}",
            "scores": ScoreVector.model_validate(
                {"p": "0.5", "m": "0.4", "e": "0.3", "uncertainty": "0.4", "confidence": "0.6"}
            ),
            "created_at": NOW,
            "updated_at": NOW,
            "version": 1,
        }
    )


def _evidence(evidence_id: str, node_id: str, source_version_id: str, source_uri: str) -> Evidence:
    return Evidence.model_validate(
        {
            "evidence_id": evidence_id,
            "node_id": node_id,
            "source_version_id": source_version_id,
            "source_uri": source_uri,
            "observed_at": NOW,
            "independence_key": f"grp-{evidence_id}",
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
    )


class Scenario:
    """A file-backed store: one node, drawn evidence, a drawn source change."""

    def __init__(self, anchors: list[str], changed: list[str]) -> None:
        root = _scratch()
        self.db_path = root / "soul.db"
        self.corpus = root / "corpus"
        self.corpus.mkdir()
        (self.corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
        (self.corpus / "b.md").write_text("beta v1\n", encoding="utf-8")
        self.store = EventStore(self.db_path)
        self.registry = SourceRegistry(self.store)
        self.v1 = self.registry.observe(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        self.anchors = anchors
        self.store.append_domain(
            Event(
                kind=EventKind.NODE_UPSERT,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                node=_node("n-1", self.v1.source_version_id),
                source_version_id=self.v1.source_version_id,
                occurred_at=NOW,
            ),
            idempotency_key="seed:n-1",
        )
        for index, anchor in enumerate(anchors):
            evidence_id = f"e{index}"
            self.store.append_domain(
                Event(
                    kind=EventKind.EVIDENCE_APPEND,
                    node_id="n-1",
                    domain=PrimaryDomain.P1,
                    evidence=_evidence(evidence_id, "n-1", self.v1.source_version_id, anchor),
                    source_version_id=self.v1.source_version_id,
                    occurred_at=NOW,
                ),
                idempotency_key=f"seed:{evidence_id}",
            )
        self.evidence_ids = tuple(f"e{index}" for index in range(len(anchors)))
        self.engine = RelearnEngine(
            self.store,
            self.registry,
            backup_root=root / "backups",
            scoring_contract_version="1.0.0",
        )
        for name in changed:
            (self.corpus / name).write_text(f"{name} v2 changed\n", encoding="utf-8")
        self.offer = self.engine.offer(self.corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
        assert self.offer is not None

    def approve(self, mode: Mode) -> RelearnApproval:
        assert self.offer is not None
        return RelearnApproval(offer_id=self.offer.offer_id, mode=mode, approved_by="operator", decided_at=NOW)

    def db_sha256(self) -> str:
        self.engine.checkpoint()
        return hashlib.sha256(self.db_path.read_bytes()).hexdigest()


_anchors = st.lists(st.sampled_from(_ANCHORS), min_size=0, max_size=5)
_changed = st.lists(st.sampled_from(("a.md", "b.md")), min_size=1, max_size=2, unique=True)
_mode = st.sampled_from(("RESET", "ACCUMULATE"))


# ------------------------------------------------------------------- preview


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=8, deadline=None)
def test_preview_is_deterministic(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    assert scenario.engine.preview(mode) == scenario.engine.preview(mode)


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_preview_is_read_only(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    watermark_before = scenario.store.knowledge_high_watermark()
    scenario.engine.preview(mode)
    assert scenario.store.knowledge_high_watermark() == watermark_before
    assert scenario.registry.relearn_state() == RELEARN_PENDING
    assert scenario.engine.quarantined_evidence_ids() == frozenset()
    assert scenario.engine._relearn_records() == []  # noqa: SLF001


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_preview_reset_carries_nothing_and_states_the_zero_vector(anchors: list[str], changed: list[str]) -> None:
    scenario = Scenario(anchors, changed)
    preview = scenario.engine.preview("RESET")
    assert preview.evidence_carried == 0
    assert preview.carried_evidence_ids == ()
    assert preview.quarantined_evidence_ids == scenario.evidence_ids
    assert preview.scores_after == dict(_RESET_SCORES)
    assert preview.scores_after == {
        "p": "0.000000000000",
        "m": "0.000000000000",
        "e": "0.000000000000",
        "u": "1.000000000000",
        "professional_level": 1,
    }
    assert preview.promotion_blocked is True


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_preview_accumulate_partitions_evidence_without_loss(anchors: list[str], changed: list[str]) -> None:
    scenario = Scenario(anchors, changed)
    preview = scenario.engine.preview("ACCUMULATE")
    carried = set(preview.carried_evidence_ids)
    quarantined = set(preview.quarantined_evidence_ids)
    assert carried.isdisjoint(quarantined)
    assert carried | quarantined == set(scenario.evidence_ids)
    assert preview.carried_evidence_ids == tuple(sorted(carried))
    assert preview.quarantined_evidence_ids == tuple(sorted(quarantined))
    assert preview.scores_after is None
    # Carried evidence is exactly the evidence anchored to an unchanged file
    # with a mapped manifest anchor; ghost anchors are never carried.
    expected_carried = {
        evidence_id
        for evidence_id, anchor in zip(scenario.evidence_ids, anchors, strict=True)
        if anchor in ("a.md", "b.md") and anchor not in changed
    }
    assert carried == expected_carried


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_preview_classification_agrees_with_the_apply_receipt(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    preview = scenario.engine.preview(mode)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    assert receipt.carried_evidence_ids == preview.carried_evidence_ids
    assert receipt.quarantined_evidence_ids == preview.quarantined_evidence_ids
    assert receipt.old_evidence_carried == preview.evidence_carried


def test_preview_without_a_pending_offer_is_refused() -> None:
    scenario = Scenario.__new__(Scenario)  # no offer: build the store only
    root = _scratch()
    scenario.store = EventStore(root / "soul.db")  # type: ignore[attr-defined]
    scenario.registry = SourceRegistry(scenario.store)  # type: ignore[attr-defined]
    scenario.engine = RelearnEngine(  # type: ignore[attr-defined]
        scenario.store, scenario.registry, backup_root=root / "backups"
    )
    for mode in ("RESET", "ACCUMULATE"):
        with pytest.raises(NoPendingRelearn):
            scenario.engine.preview(mode)  # type: ignore[arg-type]


# -------------------------------------------------------------------- apply


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_apply_never_moves_the_knowledge_watermark(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    before = scenario.store.knowledge_high_watermark()
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    assert scenario.store.knowledge_high_watermark() == before
    assert receipt.evidence_weight == 0
    assert receipt.knowledge_seq is None
    assert receipt.promotion_blocked is True


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_apply_records_a_zero_weight_admin_event(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    assert scenario.engine._relearn_records() == []  # noqa: SLF001
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    records = scenario.engine._relearn_records()  # noqa: SLF001
    assert len(records) == 1
    record = records[0]
    assert record["relearn_id"] == receipt.relearn_id
    assert record["evidence_weight"] == 0
    assert record["promotion_blocked"] is True
    assert record["mode"] == mode


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_reset_apply_states_the_zero_vector_and_quarantines_every_old_evidence(
    anchors: list[str], changed: list[str]
) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply("RESET", scenario.approve("RESET"), at=NOW)
    assert receipt.scores_after == dict(_RESET_SCORES)
    assert receipt.carried_evidence_ids == ()
    assert receipt.quarantined_evidence_ids == scenario.evidence_ids
    assert scenario.engine.quarantined_evidence_ids() == frozenset(scenario.evidence_ids)


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_accumulate_apply_quarantine_state_equals_the_receipt(anchors: list[str], changed: list[str]) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"), at=NOW)
    assert scenario.engine.quarantined_evidence_ids() == frozenset(receipt.quarantined_evidence_ids)
    assert receipt.scores_after is None


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=6, deadline=None)
def test_apply_supersedes_the_previous_version_and_clears_the_gate(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    previous = scenario.store.retained_source_version(scenario.v1.source_version_id)
    assert previous is not None
    assert previous["source_state"] == "SUPERSEDED"
    assert previous["superseded_by"] == receipt.new_source_version_id
    assert scenario.registry.relearn_state() != RELEARN_PENDING


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_apply_with_a_mismatched_approval_is_refused_and_changes_nothing(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    other: Mode = "ACCUMULATE" if mode == "RESET" else "RESET"
    watermark_before = scenario.store.knowledge_high_watermark()
    with pytest.raises(OperatorDecisionRequired):
        scenario.engine.apply(mode, scenario.approve(other))
    with pytest.raises(OperatorDecisionRequired):
        scenario.engine.apply(mode, None)
    with pytest.raises(OperatorDecisionRequired):
        scenario.engine.apply(
            mode, RelearnApproval(offer_id="not-the-offer", mode=mode, approved_by="operator", decided_at=NOW)
        )
    assert scenario.registry.relearn_state() == RELEARN_PENDING
    assert scenario.store.knowledge_high_watermark() == watermark_before
    assert scenario.engine._relearn_records() == []  # noqa: SLF001


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_apply_idempotent_replay_returns_the_original_receipt(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    first = scenario.engine.apply(mode, scenario.approve(mode), at=NOW, idempotency_key="rl-key")
    replayed = scenario.engine.apply(mode, scenario.approve(mode), at=NOW, idempotency_key="rl-key")
    assert replayed.idempotent_replay is True
    assert first.idempotent_replay is False
    assert replayed.relearn_id == first.relearn_id
    assert replayed.carried_evidence_ids == first.carried_evidence_ids
    assert replayed.quarantined_evidence_ids == first.quarantined_evidence_ids
    assert replayed.backup == first.backup
    # One relearn record only: the replay did not re-apply.
    assert len(scenario.engine._relearn_records()) == 1  # noqa: SLF001


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_apply_key_reused_for_the_other_mode_fails_loud(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    other: Mode = "ACCUMULATE" if mode == "RESET" else "RESET"
    scenario.engine.apply(mode, scenario.approve(mode), at=NOW, idempotency_key="rl-key")
    with pytest.raises(IdempotencyConflict):
        scenario.engine.apply(other, None, idempotency_key="rl-key")


# ------------------------------------------------------------------- backup


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_backup_manifest_hashes_match_the_staged_bytes(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    backup_root = scenario.engine._backup_root  # noqa: SLF001
    assert backup_root is not None
    backup_dir = backup_root / receipt.relearn_id
    assert backup_dir.is_dir()
    assert not list(backup_dir.parent.glob(f".{receipt.relearn_id}.tmp-*"))  # no staging residue
    assert receipt.backup.verified is True
    kinds = [artifact.kind for artifact in receipt.backup.artifacts]
    assert kinds.count("sqlite") == 1
    assert set(kinds) <= {"sqlite", "current_pointer", "generation_file"}
    for artifact in receipt.backup.artifacts:
        relative = Path("generation") / artifact.path if artifact.kind == "generation_file" else Path(artifact.path)
        staged = backup_dir / relative
        assert staged.is_file()
        assert hashlib.sha256(staged.read_bytes()).hexdigest() == artifact.sha256
        assert staged.stat().st_size == artifact.byte_length
    # The staged manifest round-trips to exactly the receipt's manifest.
    on_disk = json.loads((backup_dir / "manifest.json").read_text(encoding="utf-8"))
    assert on_disk == json.loads(receipt.backup.model_dump_json())


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_backup_is_taken_before_any_mutation_of_the_apply(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    records_before = len(scenario.engine._relearn_records())  # noqa: SLF001
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    # The backup's database predates the apply's mutation: restoring it drops
    # the relearn record (verified below via the rollback round-trip), and the
    # post-apply store does carry it.
    assert len(scenario.engine._relearn_records()) == records_before + 1  # noqa: SLF001
    sqlite_artifact = next(a for a in receipt.backup.artifacts if a.kind == "sqlite")
    assert scenario.db_sha256() != sqlite_artifact.sha256  # the apply really mutated the store


# ----------------------------------------------------------------- rollback


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_rollback_restores_the_byte_exact_pre_relearn_database(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    audit_before = scenario.store.audit_event_count()
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    rollback = scenario.engine.rollback(receipt.relearn_id)
    sqlite_artifact = next(a for a in receipt.backup.artifacts if a.kind == "sqlite")
    assert rollback.verified is True
    # restored_db_sha256 is measured at the byte-replace point, before the
    # single linked rollback audit event lands in the restored database.
    assert rollback.restored_db_sha256 == sqlite_artifact.sha256
    assert scenario.store.audit_event_count() == audit_before + 1
    # The shared store object stays valid after the transplant (W3-1) and the
    # restored state is logically pre-relearn: no quarantine, no record, and
    # the pending offer gate is back.
    assert scenario.engine.quarantined_evidence_ids() == frozenset()
    assert scenario.engine._relearn_records() == []  # noqa: SLF001
    assert scenario.registry.relearn_state() == RELEARN_PENDING
    pending = scenario.engine.pending_offer()
    assert pending is not None and scenario.offer is not None
    assert pending.offer_id == scenario.offer.offer_id


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_rollback_is_single_shot(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    scenario.engine.rollback(receipt.relearn_id)
    with pytest.raises(RelearnError, match="already rolled back"):
        scenario.engine.rollback(receipt.relearn_id)


@given(unknown=st.uuids().map(lambda u: f"rl-{u.hex}"))
@settings(derandomize=True, max_examples=5, deadline=None)
def test_rollback_of_an_unknown_relearn_is_refused(unknown: str) -> None:
    scenario = Scenario([], ["a.md"])
    with pytest.raises(UnknownRelearn):
        scenario.engine.rollback(unknown)


# --------------------------------------------------------- source scoping


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=5, deadline=None)
def test_accumulate_keeps_the_carried_version_compatible_reset_breaks_it(
    anchors: list[str], changed: list[str]
) -> None:
    scenario = Scenario(anchors, changed)
    baseline = {scenario.registry.active().source_version_id}  # type: ignore[union-attr]
    assert scenario.engine.compatible_source_version_ids() == frozenset(baseline)
    scenario.engine.apply("ACCUMULATE", scenario.approve("ACCUMULATE"), at=NOW)
    compatible = scenario.engine.compatible_source_version_ids()
    assert scenario.v1.source_version_id in compatible  # carried version stays readable

    scenario_b = Scenario(anchors, changed)
    scenario_b.engine.apply("RESET", scenario_b.approve("RESET"), at=NOW)
    compatible_b = scenario_b.engine.compatible_source_version_ids()
    assert scenario_b.v1.source_version_id not in compatible_b


@given(
    ids=st.lists(st.text(alphabet="abcdef", min_size=1, max_size=6), min_size=0, max_size=8, unique=True),
)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_assert_source_compatible_refuses_exactly_the_ids_outside_the_allowed_set(
    ids: list[str],
) -> None:
    scenario = Scenario([], ["a.md"])
    allowed = scenario.engine.compatible_source_version_ids()
    outside = sorted(set(ids) - allowed)
    if outside:
        with pytest.raises(MixedSourceVersions) as caught:
            scenario.engine.assert_source_compatible(ids)
        assert caught.value.error_code == MIXED_SOURCE_VERSIONS
        assert list(caught.value.incompatible) == outside
        # One flag alone never suffices; both together do.
        with pytest.raises(MixedSourceVersions):
            scenario.engine.assert_source_compatible(ids, mixed_source_versions=True)
        with pytest.raises(MixedSourceVersions):
            scenario.engine.assert_source_compatible(ids, operator_approval=True)
        scenario.engine.assert_source_compatible(ids, mixed_source_versions=True, operator_approval=True)
    else:
        scenario.engine.assert_source_compatible(ids)


@given(
    uri=st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="/."), max_size=20),
    paths=st.lists(
        st.text(alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="/."), max_size=12),
        max_size=5,
    ),
)
@settings(derandomize=True, max_examples=30, deadline=None)
def test_anchor_path_returns_none_or_a_manifest_member(uri: str, paths: list[str]) -> None:
    manifest = set(paths)
    anchor = _anchor_path(uri, manifest)
    assert anchor is None or anchor in manifest
    if uri in manifest:
        assert anchor == uri


@given(
    head=st.text(alphabet="ab", min_size=1, max_size=6),
    tail=st.text(alphabet="cd", min_size=1, max_size=6),
)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_anchor_path_resolves_a_unique_suffix_match(head: str, tail: str) -> None:
    path = f"{head}/{tail}"
    assert _anchor_path(f"prefix/{path}", {path, "unrelated"}) == path


def test_reset_scores_constant_is_the_frozen_zero_vector() -> None:
    assert _RESET_SCORES == {
        "p": "0.000000000000",
        "m": "0.000000000000",
        "e": "0.000000000000",
        "u": "1.000000000000",
        "professional_level": 1,
    }


# ------------------------------------------------------------- status surface


def test_pending_offer_is_none_without_a_pending_gate() -> None:
    root = _scratch()
    store = EventStore(root / "soul.db")
    engine = RelearnEngine(store, SourceRegistry(store), backup_root=root / "backups")
    assert engine.pending_offer() is None
    status = engine.status()
    assert status.pending_offer is None
    assert status.quarantined_evidence_ids == ()


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_status_reflects_the_pending_offer_before_apply(anchors: list[str], changed: list[str]) -> None:
    scenario = Scenario(anchors, changed)
    status = scenario.engine.status()
    assert status.state == RELEARN_PENDING
    assert status.pending_offer is not None and scenario.offer is not None
    assert status.pending_offer.offer_id == scenario.offer.offer_id
    assert status.active_source_version_id == scenario.v1.source_version_id


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_status_after_apply_has_a_cleared_gate_and_sorted_quarantine(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    status = scenario.engine.status()
    assert status.state == "CLEAR"
    assert status.pending_offer is None
    assert status.quarantined_evidence_ids == tuple(sorted(receipt.quarantined_evidence_ids))
    assert status.active_source_version_id == receipt.new_source_version_id


# ----------------------------------------------------------- receipt contract


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_apply_receipt_names_the_offer_and_the_drawn_timestamp(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    assert scenario.offer is not None
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    assert receipt.offer_id == scenario.offer.offer_id
    assert receipt.new_source_version_id == scenario.offer.current_source_version_id
    assert receipt.previous_source_version_id == scenario.offer.previous_source_version_id
    records = scenario.engine._relearn_records()  # noqa: SLF001
    assert records[0]["at"] == NOW.isoformat()


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_apply_receipt_is_frozen_and_quarantine_sorted(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    assert receipt.quarantined_evidence_ids == tuple(sorted(receipt.quarantined_evidence_ids))
    assert receipt.carried_evidence_ids == tuple(sorted(receipt.carried_evidence_ids))
    with pytest.raises(Exception, match="frozen"):
        receipt.evidence_weight = 1  # type: ignore[misc]


@given(anchors=_anchors, changed=_changed)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_preview_nodes_reused_counts_exactly_the_carried_nodes(anchors: list[str], changed: list[str]) -> None:
    scenario = Scenario(anchors, changed)
    preview = scenario.engine.preview("ACCUMULATE")
    carried = set(preview.carried_evidence_ids)
    rows = scenario.engine._evidence_rows()  # noqa: SLF001
    expected = {str(row["node_id"]) for row in rows if str(row["evidence_id"]) in carried}
    assert preview.nodes_reused == len(expected)


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_classify_is_deterministic_within_a_process(anchors: list[str], changed: list[str], mode: Mode) -> None:
    scenario = Scenario(anchors, changed)
    assert scenario.offer is not None
    first = scenario.engine._classify(scenario.offer, mode)  # noqa: SLF001
    second = scenario.engine._classify(scenario.offer, mode)  # noqa: SLF001
    assert first == second


@given(anchors=_anchors, changed=_changed, mode=_mode, version=st.text(alphabet="0123456789.", min_size=1, max_size=12))
@settings(derandomize=True, max_examples=4, deadline=None)
def test_backup_records_the_configured_scoring_contract_version(
    anchors: list[str], changed: list[str], mode: Mode, version: str
) -> None:
    scenario = Scenario(anchors, changed)
    scenario.engine._scoring_contract_version = version  # noqa: SLF001
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    assert receipt.backup.scoring_contract_version == version
    assert receipt.backup.relearn_id == receipt.relearn_id


@given(anchors=_anchors, changed=_changed, mode=_mode)
@settings(derandomize=True, max_examples=4, deadline=None)
def test_rollback_receipt_restores_exactly_the_backup_artifacts(
    anchors: list[str], changed: list[str], mode: Mode
) -> None:
    scenario = Scenario(anchors, changed)
    receipt = scenario.engine.apply(mode, scenario.approve(mode), at=NOW)
    rollback = scenario.engine.rollback(receipt.relearn_id)
    assert rollback.restored_artifacts == receipt.backup.artifacts
    assert rollback.relearn_id == receipt.relearn_id


def test_an_in_memory_store_can_never_apply() -> None:
    store = EventStore()
    registry = SourceRegistry(store)
    engine = RelearnEngine(store, registry)  # no backup root for :memory:
    root = _scratch()
    corpus = root / "corpus"
    corpus.mkdir()
    (corpus / "a.md").write_text("alpha v1\n", encoding="utf-8")
    registry.observe(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    (corpus / "a.md").write_text("alpha v2\n", encoding="utf-8")
    offer = engine.offer(corpus, SourceKind.SOUL_CORPUS, PARSER_VERSIONS)
    assert offer is not None
    with pytest.raises(BackupUnavailable):
        engine.apply("RESET", RelearnApproval(offer_id=offer.offer_id, mode="RESET", approved_by="op", decided_at=NOW))


def test_approval_rejects_unknown_fields() -> None:
    with pytest.raises(Exception, match="extra"):
        RelearnApproval(
            offer_id="o",
            mode="RESET",
            approved_by="op",
            decided_at=NOW,
            **{"surprise": 1},  # type: ignore[call-arg]
        )


def test_mixed_source_error_message_opens_with_the_code() -> None:
    error = MixedSourceVersions(["b", "a"])
    assert error.error_code == MIXED_SOURCE_VERSIONS
    assert str(error).startswith(MIXED_SOURCE_VERSIONS)
    assert error.incompatible == ("b", "a")  # the constructor takes the caller's order

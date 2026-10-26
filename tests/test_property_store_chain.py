"""Property tests for the event store: chain integrity, idempotency, replay.

Pins the store contract (spec section 7) on arbitrary write sequences:

* ``verify_chain`` re-derives a clean chain after any sequence of valid writes;
* tampering with any field of any row — at any position — is detected, and the
  reported ``first_bad_seq`` is at or before the tampered row;
* truncating the tail or deleting the persisted anchor fails closed;
* an idempotency key replays a byte-identical request and fails loud
  (``IdempotencyConflict``) on a semantically different one (finding F3);
* replay reconstructs exactly the appended domain events, in ``event_seq``
  order; ``event_seq`` is dense and monotone, and ``knowledge_seq`` moves only
  for knowledge-affecting events.

All runs are deterministic (hypothesis ``derandomize=True``) and every store
lives in memory.
"""

from __future__ import annotations

import sqlite3
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from soul_knowledge.domain import Event, EventKind, Node, NodeKind, PrimaryDomain
from soul_knowledge.store import EventStore, IdempotencyConflict, VersionConflict

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

_slug = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="-"), min_size=1, max_size=10
)


def _node(node_id: str, summary: str = "summary", domain: PrimaryDomain = PrimaryDomain.P1) -> Node:
    return Node(
        node_id=node_id,
        kind=NodeKind.LEAF,
        domain=domain,
        summary=summary,
        provenance=("soul://corpus/x.md",),
        source_version_id="sv-1",
        created_at=NOW,
        updated_at=NOW,
    )


def _upsert_event(node_id: str, summary: str = "summary", affecting: bool = True) -> Event:
    return Event(
        kind=EventKind.NODE_UPSERT,
        node_id=node_id,
        domain=PrimaryDomain.P1,
        node=_node(node_id, summary),
        source_version_id="sv-1",
        occurred_at=NOW,
        knowledge_affecting=affecting,
    )


def _append(store: EventStore, event: Event, key: str, **kwargs: Any) -> Any:
    return store.append_domain(event, idempotency_key=key, **kwargs)


# ------------------------------------------------------------- chain integrity


@given(ids=st.lists(_slug, min_size=1, max_size=8, unique=True))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_chain_verifies_after_arbitrary_valid_writes(ids: list[str]) -> None:
    store = EventStore()
    for index, node_id in enumerate(ids):
        _append(store, _upsert_event(node_id), key=f"k-{index}")
    report = store.verify_chain()
    assert report.ok
    assert report.checked == len(ids)
    store.close()


@given(ids=st.lists(_slug, min_size=1, max_size=8, unique=True))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_event_seq_is_dense_and_monotone_from_one(ids: list[str]) -> None:
    store = EventStore()
    seqs = [_append(store, _upsert_event(node_id), key=f"k-{i}").event_seq for i, node_id in enumerate(ids)]
    assert seqs == list(range(1, len(ids) + 1))
    store.close()


def _tampered_copy(store: EventStore, workdir: Path, drop_trigger: str, sql: str, params: tuple[Any, ...]) -> Path:
    """Copy the store to a sibling file, drop one append-only trigger, tamper."""
    tampered = workdir / "tampered.sqlite3"
    source = sqlite3.connect(store.db_path)
    target = sqlite3.connect(tampered)
    source.backup(target)
    source.close()
    target.close()
    connection = sqlite3.connect(tampered)
    connection.execute(f"DROP TRIGGER {drop_trigger}")
    connection.execute(sql, params)
    connection.commit()
    connection.close()
    return tampered


@given(
    ids=st.lists(_slug, min_size=2, max_size=6, unique=True),
    data=st.data(),
)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_tamper_at_any_position_is_detected_at_or_before_it(ids: list[str], data: Any) -> None:
    with tempfile.TemporaryDirectory() as workdir:
        store = EventStore(Path(workdir) / "soul.sqlite3")
        for index, node_id in enumerate(ids):
            _append(store, _upsert_event(node_id), key=f"k-{index}")
        target_seq = data.draw(st.integers(min_value=1, max_value=len(ids)), label="tampered event_seq")
        tampered = _tampered_copy(
            store,
            Path(workdir),
            "event_log_no_update",
            "UPDATE event_log SET payload_json = ? WHERE event_seq = ?",
            ('{"tampered":true}', target_seq),
        )
        report = EventStore(tampered).verify_chain()
        assert not report.ok
        assert report.first_bad_seq is not None and report.first_bad_seq <= target_seq
        store.close()


@given(
    ids=st.lists(_slug, min_size=2, max_size=6, unique=True),
    data=st.data(),
)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_actor_field_tamper_is_detected(ids: list[str], data: Any) -> None:
    with tempfile.TemporaryDirectory() as workdir:
        store = EventStore(Path(workdir) / "soul.sqlite3")
        for index, node_id in enumerate(ids):
            _append(store, _upsert_event(node_id), key=f"k-{index}")
        target_seq = data.draw(st.integers(min_value=1, max_value=len(ids)), label="tampered event_seq")
        tampered = _tampered_copy(
            store,
            Path(workdir),
            "event_log_no_update",
            "UPDATE event_log SET actor = ? WHERE event_seq = ?",
            ("mallory", target_seq),
        )
        report = EventStore(tampered).verify_chain()
        assert not report.ok
        assert report.first_bad_seq is not None and report.first_bad_seq <= target_seq
        store.close()


@given(ids=st.lists(_slug, min_size=2, max_size=6, unique=True))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_tail_truncation_is_detected_via_the_anchor(ids: list[str]) -> None:
    with tempfile.TemporaryDirectory() as workdir:
        store = EventStore(Path(workdir) / "soul.sqlite3")
        for index, node_id in enumerate(ids):
            _append(store, _upsert_event(node_id), key=f"k-{index}")
        tampered = _tampered_copy(
            store, Path(workdir), "event_log_no_delete", "DELETE FROM event_log WHERE event_seq = ?", (len(ids),)
        )
        report = EventStore(tampered).verify_chain()
        assert not report.ok
        store.close()


@given(
    ids=st.lists(_slug, min_size=1, max_size=6, unique=True),
    data=st.data(),
)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_append_only_triggers_refuse_mutation_at_any_position(ids: list[str], data: Any) -> None:
    store = EventStore()
    for index, node_id in enumerate(ids):
        _append(store, _upsert_event(node_id), key=f"k-{index}")
    target_seq = data.draw(st.integers(min_value=1, max_value=len(ids)), label="target event_seq")
    with pytest.raises(sqlite3.IntegrityError):
        store.execute_raw("UPDATE event_log SET actor = ? WHERE event_seq = ?", ("mallory", target_seq))
    with pytest.raises(sqlite3.IntegrityError):
        store.execute_raw("DELETE FROM event_log WHERE event_seq = ?", (target_seq,))
    assert store.verify_chain().ok
    store.close()


@given(ids=st.lists(_slug, min_size=1, max_size=6, unique=True))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_missing_anchor_fails_closed_when_events_exist(ids: list[str]) -> None:
    store = EventStore()
    for index, node_id in enumerate(ids):
        _append(store, _upsert_event(node_id), key=f"k-{index}")
    store.execute_raw("DELETE FROM system_state WHERE key IN ('chain_head_hash', 'chain_event_count')")
    assert not store.verify_chain().ok
    store.close()


# ------------------------------------------------------------- idempotency


@given(node_id=_slug, summary=st.text(alphabet="abc", min_size=1, max_size=10))
@settings(derandomize=True, max_examples=20, deadline=None)
def test_identical_retry_replays_the_original_receipt(node_id: str, summary: str) -> None:
    store = EventStore()
    first = _append(store, _upsert_event(node_id, summary), key="same-key")
    second = _append(store, _upsert_event(node_id, summary), key="same-key")
    assert not first.idempotent_replay
    assert second.idempotent_replay
    assert (second.domain_event_id, second.event_seq, second.node_version) == (
        first.domain_event_id,
        first.event_seq,
        first.node_version,
    )
    assert len(list(store.replay_domain())) == 1
    store.close()


@given(
    node_id=_slug,
    first=st.text(alphabet="abc", min_size=1, max_size=6),
    second=st.text(alphabet="def", min_size=1, max_size=6),
)
@settings(derandomize=True, max_examples=20, deadline=None)
def test_key_reuse_with_different_payload_is_a_typed_conflict(node_id: str, first: str, second: str) -> None:
    store = EventStore()
    _append(store, _upsert_event(node_id, first), key="colliding-key")
    with pytest.raises(IdempotencyConflict):
        _append(store, _upsert_event(node_id, second), key="colliding-key")
    store.close()


@given(node_id=_slug)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_expected_version_is_enforced(node_id: str) -> None:
    store = EventStore()
    receipt = _append(store, _upsert_event(node_id), key="k-1")
    assert receipt.node_version == 1
    _append(store, _upsert_event(node_id, "v2"), key="k-2", expected_version=1)
    with pytest.raises(VersionConflict):
        _append(store, _upsert_event(node_id, "v3"), key="k-3", expected_version=1)
    store.close()


# ------------------------------------------------------------- replay/watermark


@given(ids=st.lists(_slug, min_size=1, max_size=8, unique=True))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_replay_reconstructs_every_write_in_order(ids: list[str]) -> None:
    store = EventStore()
    for index, node_id in enumerate(ids):
        _append(store, _upsert_event(node_id), key=f"k-{index}")
    replayed = list(store.replay_domain())
    assert [event.node_id for event in replayed] == ids
    assert all(event.kind is EventKind.NODE_UPSERT for event in replayed)
    store.close()


@given(flags=st.lists(st.booleans(), min_size=1, max_size=8))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_knowledge_seq_moves_only_for_knowledge_affecting_events(flags: list[bool]) -> None:
    store = EventStore()
    expected_seq = 0
    for index, affecting in enumerate(flags):
        receipt = _append(store, _upsert_event(f"node-{index}", affecting=affecting), key=f"k-{index}")
        if affecting:
            expected_seq += 1
            assert receipt.knowledge_seq == expected_seq
        else:
            assert receipt.knowledge_seq is None
    assert store.knowledge_high_watermark() == expected_seq
    store.close()


@given(ids=st.lists(_slug, min_size=1, max_size=6, unique=True))
@settings(derandomize=True, max_examples=15, deadline=None)
def test_current_node_returns_the_latest_revision(ids: list[str]) -> None:
    store = EventStore()
    for node_id in ids:
        _append(store, _upsert_event(node_id), key=f"up-{node_id}")
        _append(store, _upsert_event(node_id, f"v2-{node_id}"), key=f"up2-{node_id}")
    for node_id in ids:
        current = store.current_node(node_id)
        assert current is not None
        assert current.version == 2
        assert current.summary == f"v2-{node_id}"
    store.close()


@given(node_id=_slug)
@settings(derandomize=True, max_examples=15, deadline=None)
def test_store_roundtrip_preserves_node_payload(node_id: str) -> None:
    store = EventStore()
    original = _node(node_id, "roundtrip summary")
    _append(
        store,
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id=node_id,
            domain=PrimaryDomain.P1,
            node=original,
            source_version_id="sv-1",
            occurred_at=NOW,
        ),
        key="k-rt",
    )
    restored = store.current_node(node_id)
    assert restored is not None
    assert restored.node_id == original.node_id
    assert restored.summary == original.summary
    assert restored.provenance == original.provenance
    store.close()

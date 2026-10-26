"""Tests for ``know_your_soul.analytics.timeline`` (ATM F-C2).

Pins the learning-timeline contract: the event log is the single source of
truth, ``recorded_at`` is the timeline clock (never the wall clock), scoring
follows the frozen contract through ``scoring.score_items`` (decay included),
duplicates are suppressed by content (the scoring dedup recipe), and the
output is a canonical dict plus deterministic ``TIMELINE.md`` and quantized
sparkline points for downstream SVG/PNG layers.
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.analytics.timeline import (
    TIMELINE_SCHEMA_VERSION,
    TimelineError,
    build_timeline,
    render_timeline_markdown,
    sparkline,
    timeline_json,
)
from know_your_soul.domain import canonical_json

_BASE = datetime(2026, 1, 1, tzinfo=UTC)

# ------------------------------------------------------------------ helpers


def _iso(moment: datetime) -> str:
    return moment.isoformat()


def _evidence(
    evidence_id: str,
    node_id: str,
    observed_at: str,
    *,
    outcome: str = "1.000000000000",
    correctness: str = "1.000000000000",
    reliability: str = "1.000000000000",
    reproducibility: str = "1.000000000000",
    scope_fit: str = "1.000000000000",
    successful_use: str = "1.000000000000",
    task_coverage: str = "1.000000000000",
    quality_at_use: str = "1.000000000000",
    contradiction: str = "0.000000000000",
    utilization_u: str = "0.500000000000",
    independence_key: str | None = None,
    context_tags: tuple[str, ...] = (),
) -> dict[str, object]:
    return {
        "evidence_id": evidence_id,
        "node_id": node_id,
        "source_version_id": "sv-test",
        "source_uri": "soul://test",
        "observed_at": observed_at,
        "context_tags": list(context_tags),
        "independence_key": independence_key if independence_key is not None else evidence_id,
        "outcome": outcome,
        "correctness": correctness,
        "reliability": reliability,
        "reproducibility": reproducibility,
        "scope_fit": scope_fit,
        "successful_use": successful_use,
        "task_coverage": task_coverage,
        "quality_at_use": quality_at_use,
        "contradiction": contradiction,
        "utilization_u": utilization_u,
        "latency_ms": 10,
        "compute_ms": 10,
        "output_chars": 10,
    }


def _record(
    event_seq: int,
    event_type: str,
    node_id: str | None,
    domain: str | None,
    recorded_at: str,
    *,
    payload: dict[str, object] | None = None,
) -> dict[str, object]:
    return {
        "event_seq": event_seq,
        "event_type": event_type,
        "node_id": node_id,
        "domain": domain,
        "occurred_at": recorded_at,
        "recorded_at": recorded_at,
        "payload": payload if payload is not None else {},
    }


def _evidence_record(
    event_seq: int,
    evidence: dict[str, object],
    domain: str = "P1",
    recorded_at: str | None = None,
) -> dict[str, object]:
    moment = recorded_at if recorded_at is not None else _iso(_BASE + timedelta(minutes=event_seq))
    return _record(
        event_seq,
        "evidence.append",
        str(evidence["node_id"]),
        domain,
        moment,
        payload={"evidence": evidence},
    )


def _node(timeline: dict[str, object], node_id: str) -> Any:
    for entry in timeline["nodes"]:  # type: ignore[union-attr]
        if entry["node_id"] == node_id:
            return entry
    raise AssertionError(f"node {node_id} missing from timeline")


# ------------------------------------------------------------- basic build


class TestBuildTimeline:
    def test_empty_event_log(self) -> None:
        timeline = build_timeline([])
        assert timeline["schema_version"] == TIMELINE_SCHEMA_VERSION
        assert timeline["events_processed"] == 0
        assert timeline["nodes"] == []
        assert timeline["domains"] == []

    def test_single_perfect_evidence(self) -> None:
        evidence = _evidence("ev1", "n1", _iso(_BASE))
        timeline = build_timeline([_evidence_record(1, evidence, recorded_at=_iso(_BASE))])
        node = _node(timeline, "n1")
        assert node["domain"] == "P1"
        assert node["evidence_count"] == 1
        assert len(node["points"]) == 1
        point = node["points"][0]
        assert point["at"] == _iso(_BASE)
        assert point["event_seq"] == 1
        assert point["scores"]["p"] == "1.000000000000"
        assert point["scores"]["m"] == "0.000000000000"  # one group < min_independence_groups
        assert float(point["scores"]["e"]) > 0.9
        # Single item, total weight 1: u_p = 1/sqrt(2).
        assert point["uncertainties"]["p"] == "0.707106781187"
        # p=1 -> base level 5, capped to 2 by group cap and the u_p > 0.70 cap.
        assert point["professional_level"] == 2
        assert point["changes"] == ["evidence_added", "level_up", "score_changed"]
        assert timeline["events_processed"] == 1
        assert timeline["points_emitted"] == 1

    def test_recorded_at_is_the_timeline_clock(self) -> None:
        evidence = _evidence("ev1", "n1", _iso(_BASE))
        record = _evidence_record(1, evidence, recorded_at=_iso(_BASE + timedelta(days=3)))
        record["occurred_at"] = _iso(_BASE - timedelta(days=30))
        timeline = build_timeline([record])
        assert _node(timeline, "n1")["points"][0]["at"] == _iso(_BASE + timedelta(days=3))

    def test_input_order_does_not_matter(self) -> None:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE)),
            _evidence_record(
                2,
                _evidence("ev2", "n1", _iso(_BASE), outcome="-1.000000000000", correctness="0.000000000000"),
                recorded_at=_iso(_BASE + timedelta(days=1)),
            ),
        ]
        assert canonical_json(build_timeline(records)) == canonical_json(build_timeline(list(reversed(records))))

    def test_non_knowledge_events_are_counted_not_scored(self) -> None:
        records = [
            _record(1, "node.upsert", "n1", "P1", _iso(_BASE)),
            _evidence_record(2, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE + timedelta(hours=1))),
            _record(3, "relation.add", "n1", "P1", _iso(_BASE + timedelta(hours=2))),
        ]
        timeline = build_timeline(records)
        assert timeline["events_processed"] == 3
        assert timeline["events_by_type"] == {"evidence.append": 1, "node.upsert": 1, "relation.add": 1}
        assert len(_node(timeline, "n1")["points"]) == 1


# ------------------------------------------------------ changes and learning


class TestChanges:
    def test_score_change_on_contradicting_evidence(self) -> None:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE)),
            _evidence_record(
                2,
                _evidence(
                    "ev2",
                    "n1",
                    _iso(_BASE),
                    outcome="-1.000000000000",
                    correctness="0.000000000000",
                    reproducibility="0.000000000000",
                    scope_fit="0.000000000000",
                    contradiction="1.000000000000",
                ),
                # Same recorded_at keeps both evidence items at age zero, so
                # the weighted mean is exactly 0.5 (no decay in this fixture).
                recorded_at=_iso(_BASE),
            ),
        ]
        node = _node(build_timeline(records), "n1")
        first, second = node["points"]
        assert first["scores"]["p"] == "1.000000000000"
        assert second["scores"]["p"] == "0.500000000000"
        assert "score_changed" in second["changes"]
        assert "evidence_added" in second["changes"]

    def test_duplicate_evidence_id_is_suppressed_but_decay_is_visible(self) -> None:
        evidence = _evidence("ev1", "n1", _iso(_BASE))
        records = [
            _evidence_record(1, evidence, recorded_at=_iso(_BASE)),
            _evidence_record(2, dict(evidence), recorded_at=_iso(_BASE + timedelta(days=10))),
        ]
        node = _node(build_timeline(records), "n1")
        assert node["duplicates_suppressed"] == 1
        first, second = node["points"]
        assert second["changes"] == ["evidence_duplicate"]
        # Uniform decay rescales every weight by the same factor: scores are
        # invariant, but the uncertainty grows with age (scoring contract).
        assert second["scores"]["p"] == first["scores"]["p"]
        assert float(second["uncertainties"]["p"]) > float(first["uncertainties"]["p"])

    def test_content_duplicate_with_fresh_id_is_suppressed(self) -> None:
        original = _evidence("ev1", "n1", _iso(_BASE))
        clone = dict(original, evidence_id="ev2")
        records = [
            _evidence_record(1, original, recorded_at=_iso(_BASE)),
            _evidence_record(2, clone, recorded_at=_iso(_BASE + timedelta(days=1))),
        ]
        node = _node(build_timeline(records), "n1")
        assert node["evidence_count"] == 2
        assert node["duplicates_suppressed"] == 1
        first, second = node["points"]
        assert second["changes"] == ["evidence_duplicate"]
        assert second["scores"] == first["scores"]

    def test_utilization_bin_histogram(self) -> None:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE), utilization_u="0.100000000000")),
            _evidence_record(2, _evidence("ev2", "n1", _iso(_BASE), utilization_u="0.100000000000")),
            _evidence_record(3, _evidence("ev3", "n1", _iso(_BASE), utilization_u="1.000000000000")),
        ]
        bins = _node(build_timeline(records), "n1")["utilization_bins"]
        assert bins == {
            "0.1": 2,
            "0.2": 0,
            "0.3": 0,
            "0.4": 0,
            "0.5": 0,
            "0.6": 0,
            "0.7": 0,
            "0.8": 0,
            "0.9": 0,
            "1.0": 1,
        }

    def test_m_bin_transition_is_recorded(self) -> None:
        records = [
            _evidence_record(i + 1, _evidence(f"ev{i}", "n1", _iso(_BASE), utilization_u="0.500000000000"))
            for i in range(3)
        ]
        records += [
            _evidence_record(
                i + 4,
                _evidence(f"ev{i + 3}", "n1", _iso(_BASE), utilization_u="1.000000000000"),
            )
            for i in range(3)
        ]
        node = _node(build_timeline(records), "n1")
        third = node["points"][2]
        assert third["selected_m_bin"] == "0.5"
        assert "m_bin_changed" in third["changes"]
        last = node["points"][-1]
        assert last["selected_m_bin"] == "1.0"
        assert "m_bin_changed" in last["changes"]

    def test_level_down_after_hostile_evidence(self) -> None:
        perfect = [_evidence_record(i + 1, _evidence(f"good{i}", "n1", _iso(_BASE))) for i in range(5)]
        hostile = [
            _evidence_record(
                i + 6,
                _evidence(
                    f"bad{i}",
                    "n1",
                    _iso(_BASE),
                    outcome="-1.000000000000",
                    correctness="0.000000000000",
                    reproducibility="0.000000000000",
                    scope_fit="0.000000000000",
                    successful_use="0.000000000000",
                    quality_at_use="0.000000000000",
                    contradiction="1.000000000000",
                ),
            )
            for i in range(10)
        ]
        node = _node(build_timeline(perfect + hostile), "n1")
        assert node["points"][4]["professional_level"] == 3
        assert node["points"][-1]["professional_level"] == 2
        assert any("level_down" in point["changes"] for point in node["points"])
        assert node["level_transitions"] >= 2


# ------------------------------------------------------------------ domains


class TestDomainSeries:
    def test_domain_mean_tracks_member_scores(self) -> None:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE)),
            _evidence_record(
                2,
                _evidence(
                    "ev2",
                    "n2",
                    _iso(_BASE),
                    outcome="-1.000000000000",
                    correctness="0.000000000000",
                    reproducibility="0.000000000000",
                    scope_fit="0.000000000000",
                    contradiction="1.000000000000",
                ),
                recorded_at=_iso(_BASE + timedelta(hours=1)),
            ),
        ]
        timeline = build_timeline(records)
        domains = timeline["domains"]
        assert len(domains) == 1
        domain = domains[0]
        assert domain["domain"] == "P1"
        assert domain["nodes"] == ["n1", "n2"]
        points = domain["points"]
        assert points[0]["mean"]["p"] == "1.000000000000"
        assert points[0]["nodes"] == 1
        assert points[-1]["mean"]["p"] == "0.500000000000"
        assert points[-1]["nodes"] == 2

    def test_domain_point_is_not_repeated_when_unchanged(self) -> None:
        evidence = _evidence("ev1", "n1", _iso(_BASE))
        records = [
            _evidence_record(1, evidence, recorded_at=_iso(_BASE)),
            _evidence_record(2, dict(evidence), recorded_at=_iso(_BASE + timedelta(days=1))),
        ]
        domain = build_timeline(records)["domains"][0]
        # The duplicate event does not move the mean, so no second point.
        assert len(domain["points"]) == 1

    def test_retired_node_leaves_the_domain_series(self) -> None:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE)),
            _evidence_record(2, _evidence("ev2", "n2", _iso(_BASE)), recorded_at=_iso(_BASE + timedelta(hours=1))),
            _record(3, "node.retire", "n2", "P1", _iso(_BASE + timedelta(hours=2))),
        ]
        timeline = build_timeline(records)
        assert _node(timeline, "n2")["retired"] is True
        domain = timeline["domains"][0]
        assert domain["nodes"] == ["n1"]
        last = domain["points"][-1]
        assert last["nodes"] == 1
        assert last["event_seq"] == 3
        assert last["mean"]["p"] == "1.000000000000"


# ---------------------------------------------------------------- sparkline


class TestSparkline:
    def _points(self) -> list[dict[str, object]]:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE)),
            _evidence_record(
                2,
                _evidence(
                    "ev2",
                    "n1",
                    _iso(_BASE),
                    outcome="-1.000000000000",
                    correctness="0.000000000000",
                    reproducibility="0.000000000000",
                    scope_fit="0.000000000000",
                    contradiction="1.000000000000",
                ),
                # Both items share the same age at this point, so p = 0.5.
                recorded_at=_iso(_BASE + timedelta(days=10)),
            ),
        ]
        return _node(build_timeline(records), "n1")["points"]

    def test_quantized_step_values(self) -> None:
        data = sparkline(self._points(), buckets=2, dimension="p")
        assert data["dimension"] == "p"
        assert data["buckets"] == 2
        assert data["scale"] == 100
        assert data["from"] == _iso(_BASE)
        assert data["to"] == _iso(_BASE + timedelta(days=10))
        # Step function: bucket value = last point at or before the bucket end.
        assert data["values"] == [100, 50]

    def test_explicit_window_can_leave_leading_nulls(self) -> None:
        data = sparkline(
            self._points(),
            buckets=4,
            dimension="p",
            scale=10,
            start=_iso(_BASE - timedelta(days=10)),
            end=_iso(_BASE + timedelta(days=10)),
        )
        # Bucket ends: -5d, 0d, +5d, +10d — nothing before the first point.
        assert data["values"] == [None, 10, 10, 5]
        assert all(value is None or 0 <= value <= 10 for value in data["values"])

    def test_single_point_fills_every_bucket(self) -> None:
        records = [_evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE))]
        points = _node(build_timeline(records), "n1")["points"]
        data = sparkline(points, buckets=5, dimension="p")
        assert data["values"] == [100, 100, 100, 100, 100]

    def test_invalid_arguments_fail_closed(self) -> None:
        with pytest.raises(TimelineError):
            sparkline([], buckets=4)
        with pytest.raises(TimelineError):
            sparkline(self._points(), buckets=0)
        with pytest.raises(TimelineError):
            sparkline(self._points(), buckets=4, scale=0)
        with pytest.raises(TimelineError):
            sparkline(self._points(), buckets=4, dimension="zzz")


# ------------------------------------------------------------------ render


class TestRender:
    def _timeline(self) -> dict[str, object]:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)), recorded_at=_iso(_BASE)),
            _evidence_record(
                2,
                _evidence(
                    "ev2",
                    "n1",
                    _iso(_BASE),
                    outcome="-1.000000000000",
                    correctness="0.000000000000",
                    reproducibility="0.000000000000",
                    scope_fit="0.000000000000",
                    contradiction="1.000000000000",
                ),
                # Same recorded_at: both items at age zero, so p = 0.5 exactly.
                recorded_at=_iso(_BASE),
            ),
        ]
        return build_timeline(records)

    def test_timeline_json_is_canonical_and_round_trips(self) -> None:
        timeline = self._timeline()
        text = timeline_json(timeline)
        assert text.endswith("\n")
        assert json.loads(text) == json.loads(canonical_json(timeline))

    def test_markdown_is_deterministic_and_covers_nodes_and_domains(self) -> None:
        timeline = self._timeline()
        first = render_timeline_markdown(timeline)
        assert first == render_timeline_markdown(timeline)
        assert first.startswith("# TIMELINE")
        assert "n1" in first
        assert "P1" in first
        assert "1.000000000000" in first
        assert "0.500000000000" in first

    def test_markdown_rejects_a_foreign_schema(self) -> None:
        with pytest.raises(TimelineError):
            render_timeline_markdown({"schema_version": 999})


# --------------------------------------------------------------- validation


class TestValidation:
    def test_missing_required_key(self) -> None:
        with pytest.raises(TimelineError):
            build_timeline([{"event_seq": 1}])

    def test_naive_recorded_at_is_rejected(self) -> None:
        record = _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE)))
        record["recorded_at"] = "2026-01-01T00:00:00"
        with pytest.raises(TimelineError):
            build_timeline([record])

    def test_duplicate_event_seq_is_rejected(self) -> None:
        records = [
            _evidence_record(1, _evidence("ev1", "n1", _iso(_BASE))),
            _evidence_record(1, _evidence("ev2", "n2", _iso(_BASE))),
        ]
        with pytest.raises(TimelineError):
            build_timeline(records)

    def test_evidence_append_without_payload_is_rejected(self) -> None:
        with pytest.raises(TimelineError):
            build_timeline([_record(1, "evidence.append", "n1", "P1", _iso(_BASE))])


# ------------------------------------------------------- property-based tests


def _record_stream() -> st.SearchStrategy[list[dict[str, object]]]:
    outcomes = st.sampled_from(["1.000000000000", "0.000000000000", "-1.000000000000"])

    @st.composite
    def build(draw: st.DrawFn) -> list[dict[str, object]]:
        count = draw(st.integers(min_value=1, max_value=8))
        node_ids = draw(st.lists(st.sampled_from(["n1", "n2", "n3"]), min_size=count, max_size=count))
        records = []
        for index in range(count):
            recorded = _BASE + timedelta(minutes=10 * index)
            evidence = _evidence(
                f"ev{index}",
                node_ids[index],
                _iso(recorded),
                outcome=draw(outcomes),
                correctness=draw(outcomes).replace("-1.000000000000", "0.000000000000"),
            )
            records.append(_evidence_record(index + 1, evidence, recorded_at=_iso(recorded)))
        return records

    return build()


@given(records=_record_stream())
@settings(max_examples=50, derandomize=True)
def test_timeline_is_deterministic_under_shuffle(records: list[dict[str, object]]) -> None:
    baseline = canonical_json(build_timeline(records))
    assert canonical_json(build_timeline(list(reversed(records)))) == baseline


@given(records=_record_stream())
@settings(max_examples=50, derandomize=True)
def test_node_points_are_monotonic_in_event_order(records: list[dict[str, object]]) -> None:
    timeline = build_timeline(records)
    for node in timeline["nodes"]:  # type: ignore[union-attr]
        points = node["points"]
        seqs = [int(point["event_seq"]) for point in points]
        assert seqs == sorted(seqs)
        assert len(set(seqs)) == len(seqs)
        stamps = [str(point["at"]) for point in points]
        assert stamps == sorted(stamps)
        counts = [int(point["evidence_count"]) for point in points]
        assert counts == sorted(counts)


@given(records=_record_stream())
@settings(max_examples=50, derandomize=True)
def test_recomputing_without_new_evidence_never_lowers_uncertainty(
    records: list[dict[str, object]],
) -> None:
    """Decay contract: replaying the last event at a later recorded_at leaves
    scores invariant but never decreases uncertainty."""
    last: dict[str, Any] = dict(records[-1])
    later = datetime.fromisoformat(str(last["recorded_at"])) + timedelta(days=5)
    replay: dict[str, Any] = dict(last)
    replay["event_seq"] = int(last["event_seq"]) + 1
    replay["recorded_at"] = _iso(later)
    timeline = build_timeline([*records, replay])
    node_id = str(last["node_id"])
    points = _node(timeline, node_id)["points"]
    before, after = points[-2], points[-1]
    assert after["scores"] == before["scores"]
    assert float(after["uncertainties"]["p"]) >= float(before["uncertainties"]["p"])

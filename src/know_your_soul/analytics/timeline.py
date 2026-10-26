"""Learning timeline over the append-only event log (ATM F-C2).

The event log (SQLite) is the single source of truth; this module is a pure
function over event **records** read from it by the caller — it never opens
the store itself and never writes. One record is one ``event_log`` row:

``{event_seq, event_type, node_id, domain, occurred_at, recorded_at, payload}``

with ``payload`` the parsed ``payload_json`` of a domain event.

Contract:

* **``recorded_at`` is the timeline clock.** Points are stamped with the
  store-assigned ``recorded_at``; the wall clock is never consulted, so the
  artifact is reproducible from the log alone.
* **Canonical order.** Records are replayed in ascending ``event_seq``;
  input ordering is irrelevant and duplicate sequences fail closed.
* **Scoring follows the frozen contract.** Each point recomputes the node's
  accumulated evidence through :func:`know_your_soul.scoring.score_items` at
  the point's timestamp, so recency decay (half-lives P=90/M=30/E=7 days) is
  applied exactly as the contract defines it: uniform decay leaves the P/M/E
  means invariant while uncertainty grows — and the timeline shows both.
* **Dedup by content.** Exact content duplicates (the scoring recipe:
  canonical JSON over all fields except ``evidence_id``) and replayed
  evidence ids are suppressed and counted, never silently double-scored.
* **Series per node and per domain.** Domain points carry the mean of the
  member nodes' latest measured P/M/E (nodes without evidence and retired
  nodes are excluded) and are emitted only when the mean or membership moves.
* **Sparkline data.** :func:`sparkline` quantizes any dimension of a point
  series into ``0..scale`` integer buckets (step function over the series
  window) for downstream SVG/PNG layers.

origin: idea from Graphify ``reflect.py`` (learning timeline, decay,
fresh-only dedup); implementation original — event-log SSOT, Decimal
arithmetic and canonical ordering are know-your-soul contracts.
"""

from __future__ import annotations

import hashlib
from collections import Counter
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from datetime import UTC, datetime
from decimal import ROUND_HALF_EVEN, Decimal
from typing import Any

from know_your_soul.domain import canonical_json, quantize_score
from know_your_soul.scoring import ScoringConfig, score_items, serialize_score

__all__ = [
    "TIMELINE_SCHEMA_VERSION",
    "TimelineError",
    "build_timeline",
    "render_timeline_markdown",
    "sparkline",
    "timeline_json",
]

TIMELINE_SCHEMA_VERSION = 1

_DIMENSIONS = ("p", "m", "e")
_UTILIZATION_BINS = tuple(f"0.{digit}" for digit in range(1, 10)) + ("1.0",)
_EPOCH = datetime(1970, 1, 1, tzinfo=UTC)

_REQUIRED_RECORD_KEYS = frozenset(
    {"event_seq", "event_type", "node_id", "domain", "occurred_at", "recorded_at", "payload"}
)

#: The fields a scoring item needs (frozen evidence schema 1.0.0).
_EVIDENCE_ITEM_KEYS = (
    "evidence_id",
    "node_id",
    "source_uri",
    "observed_at",
    "context_tags",
    "independence_key",
    "outcome",
    "correctness",
    "reliability",
    "reproducibility",
    "scope_fit",
    "successful_use",
    "task_coverage",
    "quality_at_use",
    "contradiction",
    "utilization_u",
    "latency_ms",
    "compute_ms",
    "output_chars",
)


class TimelineError(ValueError):
    """A malformed event record or an invalid timeline argument (fail-closed)."""


# --------------------------------------------------------------- primitives


def _parse_aware(value: Any, *, field: str) -> datetime:
    if not isinstance(value, str):
        raise TimelineError(f"{field} must be an ISO-8601 string, got {type(value).__name__}")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError as error:
        raise TimelineError(f"{field} is not a parseable ISO-8601 timestamp: {value!r}") from error
    if moment.tzinfo is None:
        raise TimelineError(f"{field} must be timezone-aware: {value!r}")
    return moment


def _microseconds_since_epoch(moment: datetime) -> int:
    delta = moment - _EPOCH
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _scores_dict(assessment: Any) -> dict[str, str]:
    return {dimension: serialize_score(getattr(assessment, dimension)) for dimension in _DIMENSIONS}


def _uncertainties_dict(assessment: Any) -> dict[str, str]:
    return {
        "p": serialize_score(assessment.u_p),
        "m": serialize_score(assessment.u_m),
        "e": serialize_score(assessment.u_e),
    }


# ------------------------------------------------------------------- records


@dataclass(frozen=True)
class _Record:
    event_seq: int
    event_type: str
    node_id: str | None
    domain: str | None
    occurred_at: str
    recorded_at: str
    recorded_at_dt: datetime
    payload: Mapping[str, Any]


def _normalize(records: Iterable[Mapping[str, Any]]) -> list[_Record]:
    normalized: list[_Record] = []
    seen_seqs: set[int] = set()
    for index, raw in enumerate(records):
        if not isinstance(raw, Mapping):
            raise TimelineError(f"record #{index} is not a mapping")
        missing = _REQUIRED_RECORD_KEYS - set(raw)
        if missing:
            raise TimelineError(f"record #{index} is missing required keys: {sorted(missing)}")
        event_seq = raw["event_seq"]
        if isinstance(event_seq, bool) or not isinstance(event_seq, int):
            raise TimelineError(f"record #{index}: event_seq must be an int")
        if event_seq in seen_seqs:
            raise TimelineError(f"duplicate event_seq {event_seq}: the log order must be unambiguous")
        seen_seqs.add(event_seq)
        payload = raw["payload"]
        if not isinstance(payload, Mapping):
            raise TimelineError(f"record #{index}: payload must be a mapping")
        normalized.append(
            _Record(
                event_seq=event_seq,
                event_type=str(raw["event_type"]),
                node_id=str(raw["node_id"]) if raw["node_id"] is not None else None,
                domain=str(raw["domain"]) if raw["domain"] is not None else None,
                occurred_at=str(raw["occurred_at"]),
                recorded_at=str(raw["recorded_at"]),
                recorded_at_dt=_parse_aware(raw["recorded_at"], field="recorded_at"),
                payload=payload,
            )
        )
    normalized.sort(key=lambda record: record.event_seq)
    return normalized


def _evidence_item(record: _Record) -> dict[str, Any]:
    evidence = record.payload.get("evidence")
    if not isinstance(evidence, Mapping):
        raise TimelineError(f"event_seq {record.event_seq}: evidence.append without an evidence payload")
    missing = [key for key in _EVIDENCE_ITEM_KEYS if key not in evidence]
    if missing:
        raise TimelineError(f"event_seq {record.event_seq}: evidence payload is missing {missing}")
    return {key: evidence[key] for key in _EVIDENCE_ITEM_KEYS}


def _content_hash(item: Mapping[str, Any]) -> str:
    """The scoring dedup recipe: canonical JSON over all fields but the id."""
    content = {key: value for key, value in item.items() if key != "evidence_id"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _bin_of(item: Mapping[str, Any]) -> str | None:
    try:
        value = format(Decimal(str(item["utilization_u"])), ".1f")
    except Exception:
        return None
    return value if value in _UTILIZATION_BINS else None


# -------------------------------------------------------------------- state


class _NodeState:
    def __init__(self, node_id: str, domain: str | None) -> None:
        self.node_id = node_id
        self.domain = domain
        self.evidence_by_id: dict[str, dict[str, Any]] = {}
        self.content_hashes: set[str] = set()
        self.duplicates_suppressed = 0
        self.retired = False
        self.points: list[dict[str, Any]] = []
        self.utilization_bins: dict[str, int] = {bin_label: 0 for bin_label in _UTILIZATION_BINS}
        self.level_transitions = 0
        # Zero-evidence baseline (spec section 9): the first point diffs
        # against this, so a first perfect evidence already reads as learning.
        self.last_scores: dict[str, str] = {dimension: serialize_score(Decimal(0)) for dimension in _DIMENSIONS}
        self.last_level = 1
        self.last_bin: str | None = None

    def items(self) -> list[dict[str, Any]]:
        return [self.evidence_by_id[key] for key in sorted(self.evidence_by_id)]


class _DomainState:
    def __init__(self, domain: str) -> None:
        self.domain = domain
        self.points: list[dict[str, Any]] = []
        self.last_signature: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None = None


def _domain_mean(members: list[_NodeState]) -> dict[str, str]:
    count = Decimal(len(members))
    return {
        dimension: format(
            quantize_score(sum((Decimal(member.last_scores[dimension]) for member in members), Decimal(0)) / count),
            "f",
        )
        for dimension in _DIMENSIONS
    }


def _emit_domain_point(
    domain_state: _DomainState,
    record: _Record,
    members: list[_NodeState],
) -> None:
    scored = [member for member in members if member.points and not member.retired]
    if not scored:
        signature: tuple[tuple[str, ...], tuple[tuple[str, str], ...]] | None = None
        mean: dict[str, str] | None = None
    else:
        ordered = sorted(scored, key=lambda member: member.node_id)
        mean = _domain_mean(ordered)
        signature = (tuple(member.node_id for member in ordered), tuple(sorted(mean.items())))
    if signature == domain_state.last_signature:
        return
    domain_state.last_signature = signature
    if mean is None:
        return
    domain_state.points.append(
        {
            "at": record.recorded_at,
            "event_seq": record.event_seq,
            "nodes": len(scored),
            "mean": mean,
        }
    )


# -------------------------------------------------------------------- driver


def build_timeline(
    records: Iterable[Mapping[str, Any]],
    *,
    config: ScoringConfig | None = None,
) -> dict[str, Any]:
    """Replay event-log records into a canonical per-node/per-domain timeline."""
    cfg = config if config is not None else ScoringConfig.default()
    ordered = _normalize(records)

    nodes: dict[str, _NodeState] = {}
    domains: dict[str, _DomainState] = {}
    events_by_type: Counter[str] = Counter()
    points_emitted = 0

    def node_for(node_id: str, domain: str | None) -> _NodeState:
        state = nodes.get(node_id)
        if state is None:
            state = _NodeState(node_id, domain)
            nodes[node_id] = state
            if domain is not None:
                domains.setdefault(domain, _DomainState(domain))
        return state

    for record in ordered:
        events_by_type[record.event_type] += 1

        if record.event_type == "evidence.append":
            item = _evidence_item(record)
            node_id = str(item["node_id"])
            state = node_for(node_id, record.domain)

            evidence_id = str(item["evidence_id"])
            content_hash = _content_hash(item)
            duplicate = evidence_id in state.evidence_by_id or content_hash in state.content_hashes
            if duplicate:
                state.duplicates_suppressed += 1
            if evidence_id not in state.evidence_by_id:
                # The store persists every appended evidence row; the content
                # dedup only suppresses its scoring weight, not its arrival.
                state.evidence_by_id[evidence_id] = item
            state.content_hashes.add(content_hash)
            bin_label = _bin_of(item)
            if bin_label is not None:
                state.utilization_bins[bin_label] += 1

            assessment = score_items(state.items(), at=record.recorded_at_dt, config=cfg)
            scores = _scores_dict(assessment)
            changes: list[str] = ["evidence_duplicate" if duplicate else "evidence_added"]
            if scores != state.last_scores:
                changes.append("score_changed")
            if assessment.professional_level > state.last_level:
                changes.append("level_up")
                state.level_transitions += 1
            elif assessment.professional_level < state.last_level:
                changes.append("level_down")
                state.level_transitions += 1
            if assessment.selected_m_bin != state.last_bin:
                changes.append("m_bin_changed")
            state.last_scores = scores
            state.last_level = assessment.professional_level
            state.last_bin = assessment.selected_m_bin
            state.points.append(
                {
                    "at": record.recorded_at,
                    "event_seq": record.event_seq,
                    "changes": sorted(changes),
                    "scores": scores,
                    "uncertainties": _uncertainties_dict(assessment),
                    "professional_level": assessment.professional_level,
                    "selected_m_bin": assessment.selected_m_bin,
                    "evidence_count": len(state.evidence_by_id),
                }
            )
            points_emitted += 1
        elif record.event_type == "node.retire" and record.node_id is not None:
            state = node_for(record.node_id, record.domain)
            state.retired = True
        elif record.node_id is not None:
            node_for(record.node_id, record.domain)

        if record.domain is not None and record.domain in domains:
            domain_state = domains[record.domain]
            members = [state for state in nodes.values() if state.domain == record.domain]
            _emit_domain_point(domain_state, record, members)

    node_entries: list[dict[str, Any]] = []
    for node_id in sorted(nodes):
        state = nodes[node_id]
        node_entries.append(
            {
                "node_id": node_id,
                "domain": state.domain,
                "retired": state.retired,
                "first_at": state.points[0]["at"] if state.points else None,
                "last_at": state.points[-1]["at"] if state.points else None,
                "evidence_count": len(state.evidence_by_id),
                "duplicates_suppressed": state.duplicates_suppressed,
                "utilization_bins": dict(state.utilization_bins),
                "level_transitions": state.level_transitions,
                "final": (
                    {
                        "scores": dict(state.last_scores),
                        "professional_level": state.last_level,
                        "selected_m_bin": state.last_bin,
                    }
                    if state.points
                    else None
                ),
                "points": state.points,
            }
        )

    domain_entries = [
        {
            "domain": domain,
            "nodes": sorted(
                state.node_id
                for state in nodes.values()
                if state.domain == domain and state.points and not state.retired
            ),
            "points": domains[domain].points,
        }
        for domain in sorted(domains)
    ]

    return {
        "schema_version": TIMELINE_SCHEMA_VERSION,
        "contract_version": cfg.contract_version,
        "events_processed": len(ordered),
        "events_by_type": {event_type: events_by_type[event_type] for event_type in sorted(events_by_type)},
        "points_emitted": points_emitted,
        "duplicates_suppressed": sum(state.duplicates_suppressed for state in nodes.values()),
        "nodes": node_entries,
        "domains": domain_entries,
    }


# ----------------------------------------------------------------- sparkline


def sparkline(
    points: Iterable[Mapping[str, Any]],
    *,
    buckets: int,
    scale: int = 100,
    dimension: str = "p",
    start: str | None = None,
    end: str | None = None,
) -> dict[str, Any]:
    """Quantize one score dimension of a point series into integer buckets.

    Bucket ``i`` holds the value of the last point at or before the bucket's
    right edge (a step function), scaled to ``0..scale`` with
    ``ROUND_HALF_EVEN``; buckets before the first point are ``null``. The
    window defaults to the series' own first/last timestamps, so no wall
    clock is ever consulted.
    """
    if buckets < 1:
        raise TimelineError("buckets must be >= 1")
    if scale < 1:
        raise TimelineError("scale must be >= 1")
    if dimension not in _DIMENSIONS:
        raise TimelineError(f"dimension must be one of {_DIMENSIONS}, got {dimension!r}")

    series = list(points)
    if not series:
        raise TimelineError("sparkline requires at least one point")
    stamped: list[tuple[int, Decimal]] = []
    for point in series:
        moment = _parse_aware(point.get("at"), field="point.at")
        scores = point.get("scores")
        if not isinstance(scores, Mapping) or dimension not in scores:
            raise TimelineError(f"point is missing scores.{dimension}")
        stamped.append((_microseconds_since_epoch(moment), Decimal(str(scores[dimension]))))
    stamped.sort()

    start_us = _microseconds_since_epoch(_parse_aware(start, field="start")) if start is not None else stamped[0][0]
    end_us = _microseconds_since_epoch(_parse_aware(end, field="end")) if end is not None else stamped[-1][0]
    if end_us < start_us:
        raise TimelineError("sparkline window end precedes its start")

    quantum = Decimal(1)
    span = end_us - start_us
    values: list[int | None] = []
    cursor = 0
    last_value: Decimal | None = None
    for index in range(buckets):
        bucket_end = start_us + span * (index + 1) // buckets
        while cursor < len(stamped) and stamped[cursor][0] <= bucket_end:
            last_value = stamped[cursor][1]
            cursor += 1
        # Step function: buckets with no new point keep the last value; only
        # buckets before the first point stay null.
        values.append(
            None if last_value is None else int((last_value * scale).quantize(quantum, rounding=ROUND_HALF_EVEN))
        )

    return {
        "dimension": dimension,
        "buckets": buckets,
        "scale": scale,
        "from": start if start is not None else series[0]["at"],
        "to": end if end is not None else series[-1]["at"],
        "values": values,
    }


# -------------------------------------------------------------------- render


def timeline_json(timeline: Mapping[str, Any]) -> str:
    """Canonical one-line JSON artifact form of a :func:`build_timeline` dict."""
    if timeline.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        raise TimelineError(f"not a v1 timeline dict (schema_version={timeline.get('schema_version')!r})")
    return canonical_json(timeline) + "\n"


def render_timeline_markdown(timeline: Mapping[str, Any]) -> str:
    """Deterministic ``TIMELINE.md`` rendering of a :func:`build_timeline` dict."""
    if timeline.get("schema_version") != TIMELINE_SCHEMA_VERSION:
        raise TimelineError(f"not a v1 timeline dict (schema_version={timeline.get('schema_version')!r})")

    lines = [
        "# TIMELINE — P/M/E learning series",
        "",
        f"events processed: {timeline['events_processed']}"
        f" · points emitted: {timeline['points_emitted']}"
        f" · duplicates suppressed: {timeline['duplicates_suppressed']}",
        f"scoring contract: {timeline['contract_version']}",
        "",
        "## Nodes",
        "",
    ]
    nodes = timeline["nodes"]
    if nodes:
        for node in nodes:
            lines.append(f"### {node['node_id']} (domain {node['domain']})")
            lines.append("")
            lines.append(
                f"evidence: {node['evidence_count']} · duplicates suppressed: {node['duplicates_suppressed']}"
                f" · level transitions: {node['level_transitions']} · retired: {node['retired']}"
            )
            lines.append("")
            if node["points"]:
                lines.append("| at | event_seq | P | M | E | level | m bin | changes |")
                lines.append("|---|---|---|---|---|---|---|---|")
                for point in node["points"]:
                    lines.append(
                        f"| {point['at']} | {point['event_seq']} | {point['scores']['p']}"
                        f" | {point['scores']['m']} | {point['scores']['e']}"
                        f" | {point['professional_level']} | {point['selected_m_bin']}"
                        f" | {', '.join(point['changes'])} |"
                    )
            else:
                lines.append("(no scored evidence)")
            lines.append("")
    else:
        lines.append("(no nodes)")
        lines.append("")

    lines.append("## Domains")
    lines.append("")
    domains = timeline["domains"]
    if domains:
        for domain in domains:
            lines.append(f"### {domain['domain']}")
            lines.append("")
            if domain["points"]:
                lines.append("| at | event_seq | nodes | mean P | mean M | mean E |")
                lines.append("|---|---|---|---|---|---|")
                for point in domain["points"]:
                    lines.append(
                        f"| {point['at']} | {point['event_seq']} | {point['nodes']}"
                        f" | {point['mean']['p']} | {point['mean']['m']} | {point['mean']['e']} |"
                    )
            else:
                lines.append("(no domain points)")
            lines.append("")
    else:
        lines.append("(no domains)")
        lines.append("")

    return "\n".join(lines)

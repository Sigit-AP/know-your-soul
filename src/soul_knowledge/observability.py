"""Structured metrics, structured logs, and health/readiness probes.

Task 9 core of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``.

Everything is in-process and deterministic: no sockets, no clock-dependent
metric state beyond the timestamp on log records, no provider anything. The
logger redacts every field through :func:`soul_knowledge.security.redact_value`
so a secret can never ride out on a log line, and every record can carry a
``correlation_id`` that ties it to an invocation's final audit event.

Health answers "is the store intact" (select works, hash chain verifies,
migrations applied); readiness answers "may we serve traffic" (health plus no
stale incomplete journals and, when a projector is attached, an active
projection whose knowledge watermark matches the store).
"""

from __future__ import annotations

import json
import sys
import threading
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import TYPE_CHECKING, Any, TextIO

from soul_knowledge.domain import canonical_json
from soul_knowledge.security import redact_value
from soul_knowledge.store import DEFAULT_MIGRATIONS_DIR, EventStore

if TYPE_CHECKING:
    from soul_knowledge.projection import GraphProjector

__all__ = [
    "QUERY_AUDIT_OPERATIONS",
    "HealthReport",
    "MetricsRegistry",
    "StructuredLogger",
    "available_migration_versions",
    "check_health",
    "check_readiness",
    "collect_store_metrics",
    "export_query_audit",
]


# ------------------------------------------------------------------- metrics


def _label_key(labels: Mapping[str, str]) -> tuple[tuple[str, str], ...]:
    return tuple(sorted((str(key), str(value)) for key, value in labels.items()))


class MetricsRegistry:
    """Thread-safe counters and gauges with a deterministic snapshot.

    Series are keyed by ``(name, sorted labels)``; ``snapshot`` and
    ``render_text`` sort by that key so equal state renders byte-identical
    output (safe for golden fixtures and diff-based monitoring).
    """

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._counters: dict[tuple[str, tuple[tuple[str, str], ...]], int] = {}
        self._gauges: dict[tuple[str, tuple[tuple[str, str], ...]], float] = {}

    def increment(self, name: str, amount: int = 1, **labels: str) -> None:
        if amount < 0:
            raise ValueError("counters only move forward")
        key = (name, _label_key(labels))
        with self._lock:
            self._counters[key] = self._counters.get(key, 0) + amount

    def set_gauge(self, name: str, value: float, **labels: str) -> None:
        key = (name, _label_key(labels))
        with self._lock:
            self._gauges[key] = value

    def snapshot(self) -> dict[str, Any]:
        with self._lock:
            counters = sorted(self._counters.items())
            gauges = sorted(self._gauges.items())
        return {
            "counters": [{"name": name, "labels": dict(labels), "value": value} for (name, labels), value in counters],
            "gauges": [{"name": name, "labels": dict(labels), "value": value} for (name, labels), value in gauges],
        }

    def render_text(self) -> str:
        """Prometheus-style text exposition; deterministic for equal state."""
        lines: list[str] = []
        snapshot = self.snapshot()
        for entry in snapshot["counters"]:
            lines.append(f"# TYPE {entry['name']} counter")
            lines.append(f"{self._series_line(entry)}")
        for entry in snapshot["gauges"]:
            lines.append(f"# TYPE {entry['name']} gauge")
            lines.append(f"{self._series_line(entry)}")
        return "\n".join(lines) + ("\n" if lines else "")

    @staticmethod
    def _series_line(entry: Mapping[str, Any]) -> str:
        labels = entry["labels"]
        name = str(entry["name"])
        if labels:
            rendered = ",".join(f'{key}="{value}"' for key, value in sorted(labels.items()))
            return f"{name}{{{rendered}}} {entry['value']}"
        return f"{name} {entry['value']}"


def collect_store_metrics(store: EventStore, registry: MetricsRegistry) -> MetricsRegistry:
    """Snapshot the store's operational gauges into *registry*."""
    metrics = registry
    metrics.set_gauge("event_log_events", store.event_count())
    metrics.set_gauge("domain_events", store.domain_event_count())
    metrics.set_gauge("audit_events", store.audit_event_count())
    metrics.set_gauge("evidence_items", store.evidence_count())
    metrics.set_gauge("source_versions", store.source_version_count())
    metrics.set_gauge("knowledge_high_watermark", store.knowledge_high_watermark())
    metrics.set_gauge("event_high_watermark", store.event_high_watermark())
    metrics.set_gauge("pending_journals", len(store.incomplete_journals()))
    return metrics


# ------------------------------------------------------------- query audit

#: The read-only operations whose invocations make up the query-audit trail
#: (task-9 brief: query-audit export). Mutations are covered by the domain
#: event log itself; this export answers "who queried what, when, and how did
#: it end" without exposing request payloads.
QUERY_AUDIT_OPERATIONS: tuple[str, ...] = ("knowledge.query", "knowledge.get_node", "knowledge.get_subtree")


def export_query_audit(store: EventStore, *, operations: Sequence[str] = QUERY_AUDIT_OPERATIONS) -> str:
    """Export the completed query-operation audit trail as canonical JSON lines.

    One record per completed invocation of a query operation, ordered by the
    final audit event sequence — deterministic for equal store state (safe for
    diff-based review and golden fixtures). Records carry the audit identity
    (``audit_event_id``/``audit_event_seq``/``invocation_id``), the operation,
    actor, request hash, idempotency key, timestamps, outcome status and
    duration — never request payloads — and every field passes through
    :func:`soul_knowledge.security.redact_value` so a secret-shaped fragment
    (for example inside an error message) cannot ride out on the export.
    In-progress journals are excluded: they are a readiness concern, not yet
    audit history.
    """
    ops = tuple(operations)
    if not ops:
        return ""
    placeholders = ", ".join("?" for _ in ops)
    with store.read() as conn:
        rows = conn.execute(
            "SELECT invocation_id, audit_event_id, audit_event_seq, operation, request_hash, actor, "
            "idempotency_key, started_at, completed_at, outcome_json FROM invocation_journal "
            f"WHERE status = 'complete' AND operation IN ({placeholders}) ORDER BY audit_event_seq",
            ops,
        ).fetchall()
    lines: list[str] = []
    for row in rows:
        outcome: Mapping[str, Any] = {}
        if row["outcome_json"] is not None:
            parsed = json.loads(str(row["outcome_json"]))
            if isinstance(parsed, Mapping):
                outcome = parsed
        record: dict[str, Any] = {
            "audit_event_seq": row["audit_event_seq"],
            "audit_event_id": row["audit_event_id"],
            "invocation_id": row["invocation_id"],
            "operation": row["operation"],
            "actor": row["actor"],
            "request_hash": row["request_hash"],
            "idempotency_key": row["idempotency_key"],
            "started_at": row["started_at"],
            "completed_at": row["completed_at"],
            "status": outcome.get("status"),
            "duration_ms": outcome.get("duration_ms", 0),
        }
        if outcome.get("error"):
            record["error"] = outcome["error"]
        lines.append(canonical_json(redact_value(record)))
    return "".join(line + "\n" for line in lines)


# -------------------------------------------------------------------- logs


def _default_sink(record: Mapping[str, Any]) -> None:
    sys.stderr.write(canonical_json(record) + "\n")


class StructuredLogger:
    """JSON-lines structured logger with mandatory redaction.

    Every record carries ``ts`` (UTC ISO-8601), ``level``, ``event`` and
    ``component``, plus caller fields. All fields pass through
    :func:`redact_value` before they reach the sink, so secrets cannot leak
    through logs. ``correlation_id`` links a record to the invocation and its
    single final audit event (spec 7.7).
    """

    def __init__(
        self,
        sink: Callable[[Mapping[str, Any]], None] | None = None,
        *,
        component: str = "soul-knowledge",
    ) -> None:
        self._sink = sink
        self._component = component
        self._records: list[Mapping[str, Any]] = []
        self._lock = threading.Lock()

    @classmethod
    def to_stream(cls, stream: TextIO, *, component: str = "soul-knowledge") -> StructuredLogger:
        def sink(record: Mapping[str, Any]) -> None:
            stream.write(canonical_json(record) + "\n")

        return cls(sink, component=component)

    def log(self, level: str, event: str, **fields: Any) -> Mapping[str, Any]:
        record = redact_value(
            {
                "ts": datetime.now(UTC).isoformat(),
                "level": level,
                "event": event,
                "component": self._component,
                **fields,
            }
        )
        assert isinstance(record, dict)
        with self._lock:
            self._records.append(record)
        (self._sink or _default_sink)(record)
        return record

    def info(self, event: str, **fields: Any) -> Mapping[str, Any]:
        return self.log("info", event, **fields)

    def warning(self, event: str, **fields: Any) -> Mapping[str, Any]:
        return self.log("warning", event, **fields)

    def error(self, event: str, **fields: Any) -> Mapping[str, Any]:
        return self.log("error", event, **fields)

    def records(self) -> list[Mapping[str, Any]]:
        """Every emitted record, in order (primarily for probes and tests)."""
        with self._lock:
            return list(self._records)


# ------------------------------------------------------------------ health


@dataclass(frozen=True)
class HealthReport:
    """The fail-closed verdict of a health or readiness probe."""

    ok: bool
    checks: Mapping[str, bool] = field(default_factory=dict)
    details: Mapping[str, Any] = field(default_factory=dict)


def available_migration_versions(migrations_dir: str | Path | None = None) -> list[int]:
    """Migration versions available on disk (sorted)."""
    directory = Path(migrations_dir) if migrations_dir is not None else DEFAULT_MIGRATIONS_DIR
    return sorted(int(path.name.split("_", 1)[0]) for path in directory.glob("*.sql"))


def check_health(store: EventStore, *, migrations_dir: str | Path | None = None) -> HealthReport:
    """Liveness of the store itself: select, hash chain, migrations."""
    checks: dict[str, bool] = {}
    details: dict[str, Any] = {}
    try:
        with store.read() as conn:
            conn.execute("SELECT 1").fetchone()
        checks["store_select"] = True
    except Exception as error:  # noqa: BLE001 - health must report, never raise
        checks["store_select"] = False
        details["store_select"] = f"{type(error).__name__}: {error}"
    try:
        report = store.verify_chain()
        checks["chain"] = report.ok
        details["chain_checked_events"] = report.checked
        if not report.ok:
            details["chain_first_bad_seq"] = report.first_bad_seq
    except Exception as error:  # noqa: BLE001
        checks["chain"] = False
        details["chain"] = f"{type(error).__name__}: {error}"
    try:
        applied = store.applied_migrations()
        available = available_migration_versions(migrations_dir)
        checks["migrations"] = applied == available
        details["migrations_applied"] = applied
        details["migrations_available"] = available
    except Exception as error:  # noqa: BLE001
        checks["migrations"] = False
        details["migrations"] = f"{type(error).__name__}: {error}"
    return HealthReport(ok=all(checks.values()), checks=checks, details=details)


def check_readiness(
    store: EventStore,
    *,
    projector: GraphProjector | None = None,
    migrations_dir: str | Path | None = None,
    stale_after_seconds: float = 300.0,
) -> HealthReport:
    """Traffic gate: health plus stale journals and projection freshness."""
    health = check_health(store, migrations_dir=migrations_dir)
    checks: dict[str, bool] = dict(health.checks)
    details: dict[str, Any] = dict(health.details)
    stale = store.incomplete_journals(stale_after_seconds)
    checks["no_stale_journals"] = not stale
    details["stale_journals"] = len(stale)
    if projector is not None:
        manifest = projector.active_manifest()
        checks["projection_present"] = manifest is not None
        if manifest is not None:
            watermark = int(manifest.get("source_knowledge_high_watermark", -1))
            store_watermark = store.knowledge_high_watermark()
            checks["projection_watermark"] = watermark == store_watermark
            details["projection_version"] = manifest.get("projection_version")
            details["projection_watermark"] = watermark
            details["store_watermark"] = store_watermark
    return HealthReport(ok=all(checks.values()), checks=checks, details=details)

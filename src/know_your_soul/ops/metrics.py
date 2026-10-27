"""Operational metrics from the store and the projection (ATM wave-2 OPS).

:func:`collect_ops_metrics` snapshots the operational state into one
canonical dict — JSON-serializable, float-free (every ratio is an integer
permille, every latency an integer millisecond), deterministic for equal
state, and strictly **read-only**: it runs ``SELECT`` statements and file
stats only, so collection can never mutate the source of truth or the
projection.

Sections:

* ``counts`` — event/domain/audit/evidence/source-version totals, journal
  liveness, watermarks;
* ``latency_ms`` — per-operation summaries of the ``duration_ms`` the audit
  journal recorded for completed invocations (nearest-rank p50/p95,
  half-up mean; malformed outcome rows are counted, never guessed);
* ``publish_capacity`` — the F-3 write-side position: conservative estimate
  vs guard vs cap;
* ``projection`` — the active generation's artifact sizes from disk, its
  watermark and its agreement with the store;
* ``health`` — a deterministic weighted score (0..100) over the same
  primitives :mod:`know_your_soul.observability` exposes.

:func:`render_markdown` renders that dict as a stable markdown report.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from know_your_soul.observability import available_migration_versions
from know_your_soul.projection import ARTIFACT_MAX_BYTES
from know_your_soul.store import PUBLISH_CAPACITY_RATIO, EventStore

__all__ = [
    "HEALTH_COMPONENT_WEIGHTS",
    "METRICS_SCHEMA_VERSION",
    "collect_ops_metrics",
    "latency_summary",
    "render_markdown",
]

METRICS_SCHEMA_VERSION = 1

#: Health-score weights; they sum to exactly 100. Projection components are
#: only scored when a projection root is supplied.
HEALTH_COMPONENT_WEIGHTS: dict[str, int] = {
    "store_select": 10,
    "hash_chain": 25,
    "migrations": 15,
    "no_stale_journals": 10,
    "projection_present": 10,
    "projection_watermark": 15,
    "publish_capacity": 15,
}


# ------------------------------------------------------------------ latency


def latency_summary(durations: Sequence[int]) -> dict[str, int | None]:
    """Deterministic integer summary: count/min/max/mean/p50/p95.

    The mean rounds half-up on integers; percentiles are nearest-rank on the
    sorted values. Negative, boolean or non-integer inputs are programming
    errors and raise ``ValueError``.
    """
    values = sorted(durations)
    for value in values:
        if isinstance(value, bool) or not isinstance(value, int):
            raise ValueError(f"duration must be an int, got {type(value).__name__}")
        if value < 0:
            raise ValueError(f"duration must be >= 0, got {value}")
    if not values:
        return {"count": 0, "min": None, "max": None, "mean": None, "p50": None, "p95": None}
    count = len(values)

    def _percentile(percent: int) -> int:
        rank = -(-percent * count // 100)  # ceil(percent * count / 100)
        return values[max(0, rank - 1)]

    total = sum(values)
    mean = (2 * total + count) // (2 * count)  # round half up
    return {
        "count": count,
        "min": values[0],
        "max": values[-1],
        "mean": mean,
        "p50": _percentile(50),
        "p95": _percentile(95),
    }


def _audit_durations(store: EventStore) -> tuple[dict[str, dict[str, int | None]], int]:
    """Group completed invocations' ``duration_ms`` by operation.

    Rows whose outcome is missing, unparsable, or carries a non-integer or
    negative duration are skipped and counted — never silently folded in.
    """
    durations: dict[str, list[int]] = {}
    excluded = 0
    with store.read() as conn:
        rows = conn.execute(
            "SELECT operation, outcome_json FROM invocation_journal WHERE status = 'complete' ORDER BY invocation_id"
        ).fetchall()
    for row in rows:
        duration: Any = None
        raw = row["outcome_json"]
        if raw is not None:
            try:
                outcome = json.loads(str(raw))
            except json.JSONDecodeError:
                outcome = None
            if isinstance(outcome, dict):
                duration = outcome.get("duration_ms")
        if isinstance(duration, bool) or not isinstance(duration, int) or duration < 0:
            excluded += 1
            continue
        durations.setdefault(str(row["operation"]), []).append(duration)
    summaries = {operation: latency_summary(durations[operation]) for operation in sorted(durations)}
    return summaries, excluded


# ------------------------------------------------------------------ sections


def _collect_counts(store: EventStore, *, stale_after_seconds: float, excluded_latency_rows: int) -> dict[str, int]:
    stale = store.incomplete_journals(stale_after_seconds)
    pending = store.incomplete_journals()
    with store.read() as conn:
        complete = int(
            conn.execute("SELECT COUNT(*) AS n FROM invocation_journal WHERE status = 'complete'").fetchone()["n"]
        )
    return {
        "audit_events": store.audit_event_count(),
        "domain_events": store.domain_event_count(),
        "event_high_watermark": store.event_high_watermark(),
        "events_total": store.event_count(),
        "evidence_items": store.evidence_count(),
        "journals_complete": complete,
        "journals_in_progress": len(pending),
        "journals_stale": len(stale),
        "knowledge_high_watermark": store.knowledge_high_watermark(),
        "latency_rows_excluded": excluded_latency_rows,
        "source_versions": store.source_version_count(),
    }


def _collect_publish_capacity(store: EventStore) -> dict[str, Any]:
    estimated = store.estimate_publish_bytes()
    guard = int(ARTIFACT_MAX_BYTES * PUBLISH_CAPACITY_RATIO)
    return {
        "cap_bytes": ARTIFACT_MAX_BYTES,
        "estimated_bytes": estimated,
        "guard_bytes": guard,
        "headroom_bytes": guard - estimated,
        "ok": estimated <= guard,
        "utilization_permille": (estimated * 1000) // ARTIFACT_MAX_BYTES,
    }


def _current_generation_name(projection_root: Path) -> str | None:
    pointer = projection_root / "CURRENT"
    try:
        name = pointer.read_text(encoding="utf-8").strip()
    except OSError:
        return None
    if not name or len(name) != 64 or any(char not in "0123456789abcdef" for char in name):
        return None
    return name


def _collect_projection(store: EventStore, projection_root: Path) -> dict[str, Any]:
    """Artifact sizes and watermark agreement of the active generation.

    Purely observational: an absent or malformed projection is reported as
    ``present: False`` (the diagnostics module owns the severity ladder).
    """
    root = Path(projection_root)
    name = _current_generation_name(root)
    if name is None:
        return {"present": False}
    generation = root / "generations" / name
    if not generation.is_dir():
        return {"present": False}
    try:
        manifest = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
    except OSError, json.JSONDecodeError:
        return {"present": False}
    if not isinstance(manifest, dict):
        return {"present": False}
    artifacts: dict[str, int] = {}
    for path in sorted(generation.rglob("*")):
        if path.is_file():
            relative = path.relative_to(generation).as_posix()
            artifacts[relative] = path.stat().st_size
    manifest_watermark = manifest.get("source_knowledge_high_watermark")
    store_watermark = store.knowledge_high_watermark()
    return {
        "present": True,
        "artifacts": artifacts,
        "edge_count": int(manifest.get("edge_count", 0)),
        "generation": name,
        "node_count": int(manifest.get("node_count", 0)),
        "total_bytes": sum(artifacts.values()),
        "watermark": manifest_watermark if isinstance(manifest_watermark, int) else None,
        "watermark_matches_store": manifest_watermark == store_watermark,
    }


def _collect_health(
    store: EventStore,
    *,
    projection: dict[str, Any] | None,
    capacity: dict[str, Any],
    stale_journals: int,
    migrations_dir: str | Path | None,
) -> dict[str, Any]:
    components: dict[str, dict[str, Any]] = {}

    def _record(name: str, ok: bool) -> None:
        components[name] = {"ok": bool(ok), "weight": HEALTH_COMPONENT_WEIGHTS[name]}

    try:
        with store.read() as conn:
            conn.execute("SELECT 1").fetchone()
        _record("store_select", True)
    except Exception:  # noqa: BLE001 - metrics report state, never raise
        _record("store_select", False)
    try:
        _record("hash_chain", store.verify_chain().ok)
    except Exception:  # noqa: BLE001
        _record("hash_chain", False)
    try:
        _record("migrations", store.applied_migrations() == available_migration_versions(migrations_dir))
    except Exception:  # noqa: BLE001
        _record("migrations", False)
    _record("no_stale_journals", stale_journals == 0)
    if projection is not None:
        present = bool(projection.get("present"))
        _record("projection_present", present)
        _record("projection_watermark", present and bool(projection.get("watermark_matches_store")))
    _record("publish_capacity", capacity["ok"])

    score = sum(entry["weight"] for entry in components.values() if entry["ok"])
    max_score = sum(entry["weight"] for entry in components.values())
    return {"components": components, "max_score": max_score, "score": score}


# ------------------------------------------------------------------ collect


def collect_ops_metrics(
    store: EventStore,
    *,
    projection_root: str | Path | None = None,
    migrations_dir: str | Path | None = None,
    stale_after_seconds: float = 300.0,
) -> dict[str, Any]:
    """Snapshot the operational metrics as one canonical, float-free dict.

    Read-only: collection runs ``SELECT`` statements and file stats only.
    """
    latency, excluded = _audit_durations(store)
    counts = _collect_counts(store, stale_after_seconds=stale_after_seconds, excluded_latency_rows=excluded)
    capacity = _collect_publish_capacity(store)
    projection = _collect_projection(store, Path(projection_root)) if projection_root is not None else None
    health = _collect_health(
        store,
        projection=projection,
        capacity=capacity,
        stale_journals=counts["journals_stale"],
        migrations_dir=migrations_dir,
    )
    return {
        "schema_version": METRICS_SCHEMA_VERSION,
        "counts": counts,
        "health": health,
        "latency_ms": latency,
        "projection": projection,
        "publish_capacity": capacity,
    }


# ------------------------------------------------------------------ render


def render_markdown(metrics: Mapping[str, Any]) -> str:
    """Render the collected metrics as a stable markdown report."""
    lines: list[str] = ["# Operational Metrics", ""]

    lines.append("## Counts")
    lines.append("")
    lines.append("| metric | value |")
    lines.append("|---|---|")
    counts = metrics["counts"]
    for key in sorted(counts):
        lines.append(f"| {key} | {counts[key]} |")
    lines.append("")

    lines.append("## Latency")
    lines.append("")
    latency = metrics["latency_ms"]
    if latency:
        lines.append("| operation | count | min | mean | p50 | p95 | max |")
        lines.append("|---|---|---|---|---|---|---|")
        for operation in sorted(latency):
            entry = latency[operation]
            cells = (
                operation,
                str(entry["count"]),
                str(entry["min"]),
                str(entry["mean"]),
                str(entry["p50"]),
                str(entry["p95"]),
                str(entry["max"]),
            )
            lines.append("| " + " | ".join(cells) + " |")
    else:
        lines.append("No completed invocations recorded yet.")
    lines.append("")

    lines.append("## Publish Capacity")
    lines.append("")
    capacity = metrics["publish_capacity"]
    lines.append("| metric | value |")
    lines.append("|---|---|")
    for key in sorted(capacity):
        lines.append(f"| {key} | {capacity[key]} |")
    lines.append("")

    projection = metrics["projection"]
    if projection is not None:
        lines.append("## Projection")
        lines.append("")
        if projection.get("present"):
            lines.append(f"- generation: `{projection['generation']}`")
            lines.append(f"- nodes: {projection['node_count']} · edges: {projection['edge_count']}")
            lines.append(
                f"- watermark: {projection['watermark']} (matches store: {projection['watermark_matches_store']})"
            )
            lines.append(f"- total artifact bytes: {projection['total_bytes']}")
            lines.append("")
            lines.append("| artifact | bytes |")
            lines.append("|---|---|")
            for name in sorted(projection["artifacts"]):
                lines.append(f"| {name} | {projection['artifacts'][name]} |")
        else:
            lines.append("No active projection.")
        lines.append("")

    lines.append("## Health")
    lines.append("")
    health = metrics["health"]
    lines.append(f"Score: **{health['score']}/{health['max_score']}**")
    lines.append("")
    lines.append("| component | weight | ok |")
    lines.append("|---|---|---|")
    for name in sorted(health["components"]):
        component = health["components"][name]
        lines.append(f"| {name} | {component['weight']} | {'yes' if component['ok'] else 'no'} |")
    lines.append("")
    return "\n".join(lines)

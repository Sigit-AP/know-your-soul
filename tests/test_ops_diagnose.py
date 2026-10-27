"""Tests for ``know_your_soul.ops.diagnose`` (ATM wave-2 OPS).

Pins the self-check diagnostics contract: hash chain, manifest vs disk,
watermark consistency, read-model validation, publish capacity, stale
journals and file permissions — every check fail-closed, every finding
tiered by severity, and the exit code derived from the worst finding.
Everything is read-only: diagnostics never write to the store or the
projection.
"""

from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
from typing import Any, cast

import pytest

from know_your_soul.ops.diagnose import (
    EXIT_ISSUES,
    EXIT_OK,
    SEVERITY_ORDER,
    DiagnosticReport,
    Finding,
    exit_code,
    render_markdown,
    run_diagnostics,
)
from know_your_soul.projection import ARTIFACT_MAX_BYTES, GraphProjector
from know_your_soul.read_model_validator import ReadModelValidator, ValidationReport
from know_your_soul.store import ChainReport, EventStore

# ------------------------------------------------------------------ helpers


def _store(tmp_path: Path) -> EventStore:
    return EventStore(tmp_path / "diagnose.sqlite3")


def _projected(tmp_path: Path) -> tuple[EventStore, Path]:
    store = EventStore(tmp_path / "with-projection.sqlite3")
    projector = GraphProjector(store, output_root=tmp_path / "proj")
    projector.rebuild()
    return store, tmp_path / "proj"


def _findings(report: DiagnosticReport, code: str) -> list[Finding]:
    return [finding for finding in report.findings if finding.code == code]


class _StubValidator(ReadModelValidator):
    """A validator that returns a canned report (never reads the disk)."""

    def __init__(self, report: ValidationReport) -> None:
        self._report = report

    def validate(self, generation_dir: Path, manifest: Any) -> ValidationReport:  # noqa: ARG002
        return self._report


class _ExplodingStore:
    """A store-shaped object whose reads raise — diagnostics must contain it."""

    db_path = ":memory:"

    def __getattr__(self, name: str) -> Any:
        raise RuntimeError(f"simulated store failure in {name}")


# ------------------------------------------------------------------ findings


def test_finding_is_frozen() -> None:
    finding = Finding(code="X", severity="info", message="m", details={})
    with pytest.raises(Exception):  # noqa: B017 - frozen dataclass refuses assignment
        finding.code = "Y"  # type: ignore[misc]


def test_finding_rejects_unknown_severity() -> None:
    with pytest.raises(ValueError):
        Finding(code="X", severity="fatal", message="m", details={})


def test_severity_order_is_tiered() -> None:
    assert SEVERITY_ORDER == ("info", "warning", "error")


def test_report_ok_requires_no_errors() -> None:
    report = DiagnosticReport(findings=(Finding("A", "warning", "w", {}),), checked=("c",))
    assert report.ok is True
    bad = DiagnosticReport(findings=(Finding("A", "error", "e", {}),), checked=("c",))
    assert bad.ok is False


def test_report_worst_severity() -> None:
    report = DiagnosticReport(
        findings=(Finding("A", "info", "i", {}), Finding("B", "warning", "w", {})),
        checked=("c",),
    )
    assert report.worst_severity == "warning"
    assert report.error_count == 0
    assert report.warning_count == 1


def test_report_counts() -> None:
    report = DiagnosticReport(
        findings=(
            Finding("A", "info", "i", {}),
            Finding("B", "warning", "w", {}),
            Finding("C", "error", "e", {}),
            Finding("D", "error", "e", {}),
        ),
        checked=("c",),
    )
    assert report.error_count == 2
    assert report.warning_count == 1


def test_empty_report_is_ok() -> None:
    report = DiagnosticReport(findings=(), checked=("c",))
    assert report.ok is True
    assert report.worst_severity == "info"


def test_exit_code_mapping() -> None:
    assert exit_code(DiagnosticReport(findings=(), checked=())) == EXIT_OK
    assert exit_code(DiagnosticReport(findings=(Finding("A", "warning", "w", {}),), checked=())) == EXIT_OK
    assert exit_code(DiagnosticReport(findings=(Finding("A", "error", "e", {}),), checked=())) == EXIT_ISSUES
    assert EXIT_OK == 0
    assert EXIT_ISSUES == 1


# ------------------------------------------------------------------ healthy


def test_healthy_empty_store_without_projection_root(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = run_diagnostics(store)
    assert report.ok is True
    assert exit_code(report) == EXIT_OK
    assert "hash_chain" in report.checked
    assert "store_select" in report.checked
    # Projection checks are explicitly reported as skipped.
    assert _findings(report, "PROJECTION_CHECKS_SKIPPED")


def test_healthy_projected_store(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    report = run_diagnostics(store, projection_root=root)
    errors = [finding for finding in report.findings if finding.severity == "error"]
    assert errors == []
    assert exit_code(report) == EXIT_OK
    for check in ("manifest_load", "manifest_vs_disk", "watermark_consistency", "validator", "publish_capacity"):
        assert check in report.checked


def test_diagnostics_are_deterministic(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    first = run_diagnostics(store, projection_root=root)
    second = run_diagnostics(store, projection_root=root)
    assert [(f.code, f.severity, f.message) for f in first.findings] == [
        (f.code, f.severity, f.message) for f in second.findings
    ]


def test_diagnostics_do_not_write_to_store(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    before = (store.event_count(), store.audit_event_count(), store.event_high_watermark())
    run_diagnostics(store, projection_root=root)
    assert before == (store.event_count(), store.audit_event_count(), store.event_high_watermark())


# ------------------------------------------------------------------ chain


def test_broken_chain_is_error(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _BrokenChain:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def verify_chain(self) -> ChainReport:
            return ChainReport(ok=False, checked=3, first_bad_seq=4)

    report = run_diagnostics(cast(EventStore, _BrokenChain(store)))
    chain = _findings(report, "CHAIN_BROKEN")
    assert len(chain) == 1
    assert chain[0].severity == "error"
    assert chain[0].details["first_bad_seq"] == 4
    assert exit_code(report) == EXIT_ISSUES


def test_exploding_store_fails_closed_never_raises() -> None:
    report = run_diagnostics(cast(EventStore, _ExplodingStore()))
    assert report.ok is False
    assert _findings(report, "CHECK_CRASHED")
    assert exit_code(report) == EXIT_ISSUES


def test_exploding_store_messages_are_sanitized() -> None:
    report = run_diagnostics(cast(EventStore, _ExplodingStore()))
    for finding in report.findings:
        assert "-----BEGIN" not in finding.message


# ------------------------------------------------------------------ journals


def test_stale_journals_are_a_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.execute_raw(
        "INSERT INTO invocation_journal (invocation_id, audit_event_id, operation, request_hash, actor, "
        "idempotency_key, started_at, status) VALUES ('inv-stale', 'audit-inv-stale', 'knowledge.query', "
        "'h', 'tester', NULL, '2020-01-01T00:00:00+00:00', 'in_progress')",
    )
    report = run_diagnostics(store, stale_after_seconds=0.0)
    stale = _findings(report, "STALE_JOURNALS")
    assert len(stale) == 1
    assert stale[0].severity == "warning"
    assert exit_code(report) == EXIT_OK


def test_fresh_journals_are_not_stale(tmp_path: Path) -> None:
    store = _store(tmp_path)
    store.execute_raw(
        "INSERT INTO invocation_journal (invocation_id, audit_event_id, operation, request_hash, actor, "
        "idempotency_key, started_at, status) VALUES ('inv-fresh', 'audit-inv-fresh', 'knowledge.query', "
        "'h', 'tester', NULL, '2020-01-01T00:00:00+00:00', 'in_progress')",
    )
    report = run_diagnostics(store, stale_after_seconds=1e12)
    assert _findings(report, "STALE_JOURNALS") == []


# ------------------------------------------------------------------ manifest


def test_missing_projection_root_is_a_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    report = run_diagnostics(store, projection_root=tmp_path / "absent")
    pointer = _findings(report, "PROJECTION_ABSENT")
    assert len(pointer) == 1
    assert pointer[0].severity == "warning"
    assert exit_code(report) == EXIT_OK


def test_invalid_current_pointer_is_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    (root / "generations").mkdir(parents=True)
    (root / "CURRENT").write_text("not-a-hash\n", encoding="utf-8")
    report = run_diagnostics(store, projection_root=root)
    assert _findings(report, "CURRENT_POINTER_INVALID")


def test_current_pointing_nowhere_is_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    (root / "generations").mkdir(parents=True)
    (root / "CURRENT").write_text("f" * 64 + "\n", encoding="utf-8")
    report = run_diagnostics(store, projection_root=root)
    assert _findings(report, "CURRENT_POINTER_INVALID")


def test_unreadable_manifest_is_an_error(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    (generation_dir / "manifest.json").write_text("{ not json", encoding="utf-8")
    report = run_diagnostics(store, projection_root=root)
    assert _findings(report, "MANIFEST_UNREADABLE")


def test_tampered_artifact_is_an_error(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    target = generation_dir / "GRAPH_REPORT.md"
    target.write_bytes(target.read_bytes() + b"tampered")
    report = run_diagnostics(store, projection_root=root)
    mismatch = _findings(report, "ARTIFACT_HASH_MISMATCH")
    assert len(mismatch) == 1
    assert mismatch[0].severity == "error"
    assert "GRAPH_REPORT.md" in mismatch[0].details["artifacts"]


def test_missing_artifact_is_an_error(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    os.remove(generation_dir / "labels.json")
    report = run_diagnostics(store, projection_root=root)
    missing = _findings(report, "ARTIFACT_MISSING")
    assert len(missing) == 1
    assert "labels.json" in missing[0].details["artifacts"]


def test_unmanifested_file_is_a_warning(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    (generation_dir / "stray.txt").write_text("stray", encoding="utf-8")
    report = run_diagnostics(store, projection_root=root)
    extra = _findings(report, "ARTIFACT_UNMANIFESTED")
    assert len(extra) == 1
    assert extra[0].severity == "warning"
    assert "stray.txt" in extra[0].details["artifacts"]


# ------------------------------------------------------------------ watermark


def test_watermark_mismatch_is_a_warning(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    manifest_path = generation_dir / "manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["source_knowledge_high_watermark"] = 42
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    report = run_diagnostics(store, projection_root=root)
    mismatch = _findings(report, "WATERMARK_MISMATCH")
    assert len(mismatch) == 1
    assert mismatch[0].severity == "warning"
    assert mismatch[0].details["manifest_watermark"] == 42
    assert mismatch[0].details["store_watermark"] == 0


def test_projection_version_mismatch_is_a_warning(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    store.set_projection_version("gen-stale")
    report = run_diagnostics(store, projection_root=root)
    mismatch = _findings(report, "PROJECTION_VERSION_MISMATCH")
    assert len(mismatch) == 1
    assert mismatch[0].severity == "warning"


# ------------------------------------------------------------------ validator


def test_validator_failure_is_an_error(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    validator = _StubValidator(ValidationReport(ok=False, errors=("schema exploded",), node_count=0, edge_count=0))
    report = run_diagnostics(store, projection_root=root, validator=validator)
    failure = _findings(report, "VALIDATOR_FAILED")
    assert len(failure) == 1
    assert failure[0].severity == "error"
    assert "schema exploded" in failure[0].details["errors"]


def test_validator_pass_produces_no_finding(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    validator = _StubValidator(ValidationReport(ok=True, errors=(), node_count=0, edge_count=0))
    report = run_diagnostics(store, projection_root=root, validator=validator)
    assert _findings(report, "VALIDATOR_FAILED") == []
    assert "validator" in report.checked


def test_default_validator_runs_on_real_generation(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    report = run_diagnostics(store, projection_root=root)
    assert _findings(report, "VALIDATOR_FAILED") == []


def test_validator_crash_is_contained(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)

    class _CrashingValidator(_StubValidator):
        def validate(self, generation_dir: Path, manifest: Any) -> ValidationReport:
            raise RuntimeError("validator exploded")

    report = run_diagnostics(store, projection_root=root, validator=_CrashingValidator(ValidationReport(ok=True)))
    crashed = _findings(report, "CHECK_CRASHED")
    assert crashed
    assert any("validator" in finding.message for finding in crashed)


# ------------------------------------------------------------------ capacity


def test_capacity_guard_exceeded_is_a_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    estimate = store.estimate_publish_bytes()

    class _FatStore:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def estimate_publish_bytes(self, **kwargs: Any) -> int:
            guard = int(ARTIFACT_MAX_BYTES * 0.95)
            return guard + 1

    report = run_diagnostics(cast(EventStore, _FatStore(store)))
    warning = _findings(report, "CAPACITY_GUARD_EXCEEDED")
    assert len(warning) == 1
    assert warning[0].severity == "warning"
    assert exit_code(report) == EXIT_OK
    assert estimate >= 0


def test_capacity_cap_exceeded_is_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)

    class _HugeStore:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def estimate_publish_bytes(self, **kwargs: Any) -> int:
            return ARTIFACT_MAX_BYTES + 1

    report = run_diagnostics(cast(EventStore, _HugeStore(store)))
    failure = _findings(report, "CAPACITY_EXCEEDED")
    assert len(failure) == 1
    assert failure[0].severity == "error"
    assert exit_code(report) == EXIT_ISSUES


# ------------------------------------------------------------------ permissions


def test_world_readable_store_is_a_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o644)
    report = run_diagnostics(store)
    findings = _findings(report, "PERMISSIONS_TOO_OPEN")
    assert len(findings) == 1
    assert findings[0].severity == "warning"
    assert exit_code(report) == EXIT_OK


def test_private_store_file_is_clean(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o600)
    report = run_diagnostics(store)
    assert _findings(report, "PERMISSIONS_TOO_OPEN") == []


def test_group_readable_store_is_a_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o640)
    report = run_diagnostics(store)
    assert len(_findings(report, "PERMISSIONS_TOO_OPEN")) == 1


def test_permissions_check_can_be_disabled(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o644)
    report = run_diagnostics(store, check_permissions=False)
    assert _findings(report, "PERMISSIONS_TOO_OPEN") == []
    assert "file_permissions" not in report.checked


def test_world_readable_projection_dir_is_a_warning(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    os.chmod(store.db_path, 0o600)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    os.chmod(generation_dir / "graph.json", 0o644)
    report = run_diagnostics(store, projection_root=root)
    findings = _findings(report, "PERMISSIONS_TOO_OPEN")
    assert findings
    assert any("graph.json" in str(finding.details.get("path", "")) for finding in findings)


def test_memory_store_skips_store_file_permission(tmp_path: Path) -> None:
    store = EventStore(":memory:")
    report = run_diagnostics(store)
    assert _findings(report, "PERMISSIONS_TOO_OPEN") == []


# ------------------------------------------------------------------ rendering


def test_render_markdown_structure(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    os.chmod(store.db_path, 0o644)
    report = run_diagnostics(store, projection_root=root)
    text = render_markdown(report)
    assert "# Diagnostics Report" in text
    assert "PERMISSIONS_TOO_OPEN" in text
    assert "warning" in text
    assert "## Findings" in text


def test_render_markdown_healthy_report(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o600)
    text = render_markdown(run_diagnostics(store))
    assert "# Diagnostics Report" in text
    assert "ok" in text.lower()


def test_render_markdown_is_deterministic(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    os.chmod(store.db_path, 0o644)
    report = run_diagnostics(store, projection_root=root)
    assert render_markdown(report) == render_markdown(report)


def test_render_orders_findings_by_severity(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o644)

    class _BrokenChain:
        def __init__(self, inner: EventStore) -> None:
            self._inner = inner
            self.db_path = inner.db_path

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

        def verify_chain(self) -> ChainReport:
            return ChainReport(ok=False, checked=1, first_bad_seq=1)

    report = run_diagnostics(cast(EventStore, _BrokenChain(store)))
    text = render_markdown(report)
    assert text.index("error") < text.index("warning")


def test_findings_carry_no_absolute_tmp_paths_unredacted(tmp_path: Path) -> None:
    # Finding messages must not embed host secrets; paths are allowed but the
    # message itself must be redacted through the security layer.
    store, root = _projected(tmp_path)
    report = run_diagnostics(store, projection_root=root)
    blob = json.dumps([finding.message for finding in report.findings])
    assert "-----BEGIN" not in blob


def test_checked_list_is_sorted_and_unique(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    report = run_diagnostics(store, projection_root=root)
    assert list(report.checked) == sorted(set(report.checked))


def test_artifact_hash_uses_sha256_contract(tmp_path: Path) -> None:
    # Sanity: the manifest's artifact hashes really are sha256 of the bytes on
    # disk — the invariant manifest_vs_disk enforces.
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    manifest = json.loads((generation_dir / "manifest.json").read_text(encoding="utf-8"))
    for name, digest in manifest["artifacts"].items():
        assert hashlib.sha256((generation_dir / name).read_bytes()).hexdigest() == digest


# ------------------------------------------------------------------ hardening


def test_migrations_drift_is_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    empty_migrations = tmp_path / "no-migrations"
    empty_migrations.mkdir()
    report = run_diagnostics(store, migrations_dir=empty_migrations)
    drift = _findings(report, "MIGRATIONS_DRIFT")
    assert len(drift) == 1
    assert drift[0].severity == "error"
    assert exit_code(report) == EXIT_ISSUES


def test_root_without_current_is_a_warning(tmp_path: Path) -> None:
    store = _store(tmp_path)
    root = tmp_path / "proj"
    root.mkdir()
    report = run_diagnostics(store, projection_root=root)
    assert _findings(report, "PROJECTION_ABSENT")
    assert exit_code(report) == EXIT_OK


def test_manifest_non_object_is_an_error(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    (generation_dir / "manifest.json").write_text("[1, 2]", encoding="utf-8")
    report = run_diagnostics(store, projection_root=root)
    assert _findings(report, "MANIFEST_UNREADABLE")


def test_render_empty_report_shows_no_findings() -> None:
    text = render_markdown(DiagnosticReport(findings=(), checked=()))
    assert "No findings." in text
    assert "**ok**" in text


def test_finding_details_default_empty() -> None:
    assert Finding(code="X", severity="info", message="m").details == {}


def test_render_groups_errors_before_warnings(tmp_path: Path) -> None:
    store, root = _projected(tmp_path)
    generation_dir = GraphProjector(store, output_root=root).active_generation_dir()
    assert generation_dir is not None
    os.remove(generation_dir / "labels.json")  # error
    (generation_dir / "stray.txt").write_text("x", encoding="utf-8")  # warning
    report = run_diagnostics(store, projection_root=root)
    text = render_markdown(report)
    assert text.index("ARTIFACT_MISSING") < text.index("ARTIFACT_UNMANIFESTED")
    assert exit_code(report) == EXIT_ISSUES


def test_missing_store_file_is_an_error(tmp_path: Path) -> None:
    store = _store(tmp_path)
    os.chmod(store.db_path, 0o600)
    store.close()
    os.remove(store.db_path)

    class _GoneStore:
        def __init__(self, inner: EventStore, path: str) -> None:
            self._inner = inner
            self.db_path = path

        def __getattr__(self, name: str) -> Any:
            return getattr(self._inner, name)

    report = run_diagnostics(cast(EventStore, _GoneStore(store, str(tmp_path / "diagnose.sqlite3"))))
    assert _findings(report, "FILE_UNREADABLE")

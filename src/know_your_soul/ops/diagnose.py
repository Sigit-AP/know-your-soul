"""Self-check diagnostics with tiered severity (ATM wave-2 OPS).

:func:`run_diagnostics` walks the operational invariants of a store and its
projection and reports every deviation as a :class:`Finding` with a tiered
severity — ``info`` (state worth knowing), ``warning`` (degraded but
serving), ``error`` (integrity or safety violation). The report's
:func:`exit_code` is ``0`` when nothing exceeds ``warning`` and ``1`` when at
least one ``error`` finding exists, so the diagnostics can gate scripts and
CI directly.

Checks (each fail-closed: a crashing check becomes a ``CHECK_CRASHED``
error finding, never an exception escaping the report):

* ``store_select`` / ``hash_chain`` / ``migrations`` — the store itself is
  readable, its hash chain verifies end-to-end, and applied migrations match
  the migrations on disk;
* ``stale_journals`` — in-progress invocation journals beyond the staleness
  threshold (warning: a crashed writer, not corruption);
* ``projection_pointer`` / ``manifest_load`` — the CURRENT pointer names a
  real generation whose manifest parses;
* ``manifest_vs_disk`` — every manifest artifact exists on disk with its
  recorded sha256 (missing/mismatch = error), and no unmanifested file sits
  in the generation directory (warning);
* ``watermark_consistency`` — the manifest's knowledge watermark and
  projection version agree with the store (mismatch = warning: the read
  model is stale, not corrupt);
* ``validator`` — the read-model validator passes on the active generation
  (failure = error);
* ``publish_capacity`` — the F-3 estimate vs guard (warning) and cap
  (error);
* ``file_permissions`` — the store file and published artifacts are not
  group/other-readable (warning; posix only).

Everything is strictly read-only: diagnostics run ``SELECT`` statements,
file reads and ``stat`` calls only. Finding messages and details pass
through :func:`know_your_soul.security.redact_value`, so a secret-shaped
fragment inside an error string cannot ride out on a report.
"""

from __future__ import annotations

import hashlib
import json
import os
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from know_your_soul.observability import available_migration_versions
from know_your_soul.projection import ARTIFACT_MAX_BYTES
from know_your_soul.read_model_validator import ReadModelValidator
from know_your_soul.security import redact_value
from know_your_soul.store import PUBLISH_CAPACITY_RATIO, EventStore

__all__ = [
    "EXIT_ISSUES",
    "EXIT_OK",
    "SEVERITY_ORDER",
    "DiagnosticReport",
    "Finding",
    "exit_code",
    "render_markdown",
    "run_diagnostics",
]

SEVERITY_ORDER = ("info", "warning", "error")
EXIT_OK = 0
EXIT_ISSUES = 1


@dataclass(frozen=True)
class Finding:
    """One tiered diagnostic observation."""

    code: str
    severity: str
    message: str
    details: dict[str, Any] = field(default_factory=dict)

    def __post_init__(self) -> None:
        if self.severity not in SEVERITY_ORDER:
            raise ValueError(f"unknown severity {self.severity!r}")


@dataclass(frozen=True)
class DiagnosticReport:
    """The fail-closed verdict of a full self-check."""

    findings: tuple[Finding, ...] = ()
    checked: tuple[str, ...] = ()

    @property
    def ok(self) -> bool:
        return not any(finding.severity == "error" for finding in self.findings)

    @property
    def error_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "error")

    @property
    def warning_count(self) -> int:
        return sum(1 for finding in self.findings if finding.severity == "warning")

    @property
    def worst_severity(self) -> str:
        for severity in reversed(SEVERITY_ORDER):
            if any(finding.severity == severity for finding in self.findings):
                return severity
        return "info"


def exit_code(report: DiagnosticReport) -> int:
    """The process exit code for a report: 1 iff any error finding exists."""
    return EXIT_ISSUES if report.error_count else EXIT_OK


# ------------------------------------------------------------------ internals


def _sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _finding(code: str, severity: str, message: str, **details: Any) -> Finding:
    redacted = redact_value({"message": message, "details": details})
    assert isinstance(redacted, dict)
    return Finding(code=code, severity=severity, message=str(redacted["message"]), details=dict(redacted["details"]))


class _Checks:
    """Accumulator: runs each check, contains crashes as CHECK_CRASHED findings."""

    def __init__(self) -> None:
        self.findings: list[Finding] = []
        self.checked: list[str] = []

    def run(self, name: str, body: Any) -> None:
        self.checked.append(name)
        try:
            body()
        except Exception as error:  # noqa: BLE001 - diagnostics never raise; they report
            self.findings.append(
                _finding("CHECK_CRASHED", "error", f"check {name} failed to run: {type(error).__name__}: {error}")
            )

    def add(self, finding: Finding) -> None:
        self.findings.append(finding)


# ------------------------------------------------------------------ report


def render_markdown(report: DiagnosticReport) -> str:
    """Render a report as a stable markdown document, errors first."""
    lines: list[str] = ["# Diagnostics Report", ""]
    status = "ok" if report.ok else "issues"
    lines.append(f"Status: **{status}** — {report.error_count} error(s), {report.warning_count} warning(s).")
    lines.append("")
    lines.append("## Findings")
    lines.append("")
    if report.findings:
        ordered = sorted(report.findings, key=lambda f: (-SEVERITY_ORDER.index(f.severity), f.code))
        lines.append("| severity | code | message |")
        lines.append("|---|---|---|")
        for finding in ordered:
            lines.append(f"| {finding.severity} | {finding.code} | {finding.message} |")
    else:
        lines.append("No findings.")
    lines.append("")
    lines.append("## Checks")
    lines.append("")
    for name in report.checked:
        lines.append(f"- {name}")
    lines.append("")
    return "\n".join(lines)


# ------------------------------------------------------------------ main


def run_diagnostics(
    store: EventStore,
    *,
    projection_root: str | Path | None = None,
    migrations_dir: str | Path | None = None,
    stale_after_seconds: float = 300.0,
    validator: ReadModelValidator | None = None,
    check_permissions: bool = True,
) -> DiagnosticReport:
    """Run the full self-check; strictly read-only, fail-closed per check."""
    checks = _Checks()

    def _store_select() -> None:
        with store.read() as conn:
            conn.execute("SELECT 1").fetchone()

    checks.run("store_select", _store_select)

    def _hash_chain() -> None:
        report = store.verify_chain()
        if not report.ok:
            checks.add(
                _finding(
                    "CHAIN_BROKEN",
                    "error",
                    "the event-log hash chain does not verify",
                    checked=report.checked,
                    first_bad_seq=report.first_bad_seq,
                )
            )

    checks.run("hash_chain", _hash_chain)

    def _migrations() -> None:
        applied = store.applied_migrations()
        available = available_migration_versions(migrations_dir)
        if applied != available:
            checks.add(
                _finding(
                    "MIGRATIONS_DRIFT",
                    "error",
                    "applied migrations differ from the migrations on disk",
                    applied=applied,
                    available=available,
                )
            )

    checks.run("migrations", _migrations)

    def _stale_journals() -> None:
        stale = store.incomplete_journals(stale_after_seconds)
        if stale:
            checks.add(
                _finding(
                    "STALE_JOURNALS",
                    "warning",
                    f"{len(stale)} in-progress journal(s) are stale beyond {stale_after_seconds}s",
                    count=len(stale),
                    stale_after_seconds=stale_after_seconds,
                )
            )

    checks.run("stale_journals", _stale_journals)

    # ---------------------------------------------------------- projection
    generation_dir: Path | None = None
    manifest: dict[str, Any] | None = None
    if projection_root is None:
        checks.add(
            _finding("PROJECTION_CHECKS_SKIPPED", "info", "no projection root supplied; projection checks skipped")
        )
    else:
        root = Path(projection_root)

        def _pointer() -> tuple[Path | None, dict[str, Any] | None]:
            if not root.is_dir():
                checks.add(
                    _finding("PROJECTION_ABSENT", "warning", f"projection root {root} does not exist", path=str(root))
                )
                return None, None
            pointer = root / "CURRENT"
            try:
                name = pointer.read_text(encoding="utf-8").strip()
            except OSError:
                checks.add(
                    _finding(
                        "PROJECTION_ABSENT", "warning", "no active projection (CURRENT pointer missing)", path=str(root)
                    )
                )
                return None, None
            if not name or len(name) != 64 or any(char not in "0123456789abcdef" for char in name):
                checks.add(
                    _finding("CURRENT_POINTER_INVALID", "error", "CURRENT does not name a 64-hex generation", name=name)
                )
                return None, None
            generation = root / "generations" / name
            if not generation.is_dir():
                checks.add(
                    _finding("CURRENT_POINTER_INVALID", "error", "CURRENT names a generation absent on disk", name=name)
                )
                return None, None
            try:
                checks.checked.append("manifest_load")
                loaded = json.loads((generation / "manifest.json").read_text(encoding="utf-8"))
            except (OSError, json.JSONDecodeError) as error:
                checks.add(
                    _finding(
                        "MANIFEST_UNREADABLE",
                        "error",
                        f"manifest.json of the active generation is unreadable: {type(error).__name__}",
                    )
                )
                return generation, None
            if not isinstance(loaded, dict):
                checks.add(_finding("MANIFEST_UNREADABLE", "error", "manifest.json is not an object"))
                return generation, None
            return generation, loaded

        def _projection_pointer() -> None:
            nonlocal generation_dir, manifest
            generation_dir, manifest = _pointer()

        checks.run("projection_pointer", _projection_pointer)

        if generation_dir is not None and manifest is not None:

            def _manifest_vs_disk() -> None:
                assert generation_dir is not None and manifest is not None
                artifacts = manifest.get("artifacts", {})
                artifact_map = artifacts if isinstance(artifacts, dict) else {}
                missing: list[str] = []
                mismatched: list[str] = []
                for name in sorted(artifact_map):
                    target = generation_dir / name
                    if not target.is_file():
                        missing.append(name)
                        continue
                    try:
                        digest = _sha256(target.read_bytes())
                    except OSError:
                        missing.append(name)
                        continue
                    if digest != artifact_map[name]:
                        mismatched.append(name)
                if missing:
                    checks.add(
                        _finding(
                            "ARTIFACT_MISSING",
                            "error",
                            f"{len(missing)} manifest artifact(s) are absent or unreadable on disk",
                            artifacts=missing,
                        )
                    )
                if mismatched:
                    checks.add(
                        _finding(
                            "ARTIFACT_HASH_MISMATCH",
                            "error",
                            f"{len(mismatched)} artifact(s) on disk no longer match the manifest hash",
                            artifacts=mismatched,
                        )
                    )
                expected = set(artifact_map) | {"manifest.json"}
                extra = sorted(
                    path.relative_to(generation_dir).as_posix()
                    for path in generation_dir.rglob("*")
                    if path.is_file() and path.relative_to(generation_dir).as_posix() not in expected
                )
                if extra:
                    checks.add(
                        _finding(
                            "ARTIFACT_UNMANIFESTED",
                            "warning",
                            f"{len(extra)} file(s) on disk are not named by the manifest",
                            artifacts=extra,
                        )
                    )

            checks.run("manifest_vs_disk", _manifest_vs_disk)

            def _watermark() -> None:
                assert manifest is not None
                store_watermark = store.knowledge_high_watermark()
                manifest_watermark = manifest.get("source_knowledge_high_watermark")
                if manifest_watermark != store_watermark:
                    checks.add(
                        _finding(
                            "WATERMARK_MISMATCH",
                            "warning",
                            "the active projection's knowledge watermark differs from the store's",
                            manifest_watermark=manifest_watermark,
                            store_watermark=store_watermark,
                        )
                    )
                store_version = store.projection_version()
                manifest_version = manifest.get("projection_version")
                if store_version is not None and manifest_version is not None and store_version != manifest_version:
                    checks.add(
                        _finding(
                            "PROJECTION_VERSION_MISMATCH",
                            "warning",
                            "the store's projection pointer disagrees with the active manifest",
                            store_version=store_version,
                            manifest_version=manifest_version,
                        )
                    )

            checks.run("watermark_consistency", _watermark)

            def _validator() -> None:
                assert generation_dir is not None and manifest is not None
                active = validator if validator is not None else ReadModelValidator()
                report = active.validate(generation_dir, manifest)
                if not report.ok:
                    checks.add(
                        _finding(
                            "VALIDATOR_FAILED",
                            "error",
                            f"the read-model validator rejects the active generation ({len(report.errors)} error(s))",
                            errors=list(report.errors),
                            node_count=report.node_count,
                            edge_count=report.edge_count,
                        )
                    )

            checks.run("validator", _validator)

    # ----------------------------------------------------------- capacity

    def _publish_capacity() -> None:
        estimated = store.estimate_publish_bytes()
        guard = int(ARTIFACT_MAX_BYTES * PUBLISH_CAPACITY_RATIO)
        if estimated > ARTIFACT_MAX_BYTES:
            checks.add(
                _finding(
                    "CAPACITY_EXCEEDED",
                    "error",
                    "the publish estimate exceeds the per-artifact cap; publishing is impossible",
                    estimated_bytes=estimated,
                    cap_bytes=ARTIFACT_MAX_BYTES,
                )
            )
        elif estimated > guard:
            checks.add(
                _finding(
                    "CAPACITY_GUARD_EXCEEDED",
                    "warning",
                    "the publish estimate is past the write-side guard; the store will refuse further growth",
                    estimated_bytes=estimated,
                    guard_bytes=guard,
                    cap_bytes=ARTIFACT_MAX_BYTES,
                )
            )

    checks.run("publish_capacity", _publish_capacity)

    # ---------------------------------------------------------- permissions

    if check_permissions:

        def _permissions() -> None:
            paths: list[Path] = []
            if store.db_path != ":memory:":
                paths.append(Path(store.db_path))
            if generation_dir is not None:
                paths.extend(sorted(path for path in generation_dir.rglob("*") if path.is_file()))
            for path in paths:
                if not path.exists():
                    checks.add(
                        _finding("FILE_UNREADABLE", "error", f"expected file is missing: {path}", path=str(path))
                    )
                    continue
                mode = stat.S_IMODE(path.stat().st_mode)
                if not os.access(path, os.R_OK):
                    checks.add(_finding("FILE_UNREADABLE", "error", f"file is not readable: {path}", path=str(path)))
                if mode & 0o077:
                    checks.add(
                        _finding(
                            "PERMISSIONS_TOO_OPEN",
                            "warning",
                            f"file is group/other-accessible (mode {oct(mode)}): {path.name}",
                            path=str(path),
                            mode=oct(mode),
                        )
                    )

        checks.run("file_permissions", _permissions)

    return DiagnosticReport(findings=tuple(checks.findings), checked=tuple(sorted(set(checks.checked))))

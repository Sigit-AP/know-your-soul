"""Tests for ``know_your_soul.ops.retention`` (ATM F-D2).

Pins the retention contract: keep-N newest generations plus pinned ones,
never the CURRENT generation, never a generation referenced by any backup,
an audit event per real deletion, an honest dry-run mode, and typed errors.
Retention never touches the SQLite store — it only ever removes generation
directories under ``<projection-root>/generations/``.
"""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from know_your_soul.ops.retention import (
    DEFAULT_AUDIT_FILENAME,
    RetentionError,
    RetentionPolicy,
    plan_retention,
    run_retention,
)

# ------------------------------------------------------------------ helpers


def _make_generation(root: Path, name: str, watermark: int, *, manifest: object | None = None) -> Path:
    gen = root / "generations" / name
    gen.mkdir(parents=True)
    if manifest is None:
        manifest = {"source_knowledge_high_watermark": watermark, "artifacts": {"graph.json": "x" * 64}}
    (gen / "manifest.json").write_text(json.dumps(manifest), encoding="utf-8")
    (gen / "graph.json").write_text('{"nodes": [], "links": []}\n', encoding="utf-8")
    return gen


def _make_root(tmp_path: Path, gens: dict[str, int], current: str | None) -> Path:
    root = tmp_path / "projection"
    root.mkdir()
    for name, watermark in gens.items():
        _make_generation(root, name, watermark)
    if current is not None:
        (root / "CURRENT").write_text(current + "\n", encoding="utf-8")
    return root


def _names(report_deleted: tuple) -> list[str]:
    return [record.generation for record in report_deleted]


# ------------------------------------------------------------------ keep-N


def test_keep_n_deletes_oldest_by_watermark(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3, "gen-d": 4}, current="gen-d")
    report = run_retention(root, RetentionPolicy(keep=1))
    assert _names(report.deleted) == ["gen-a", "gen-b"]  # oldest first
    assert report.kept == ("gen-c",)
    assert not (root / "generations" / "gen-a").exists()
    assert not (root / "generations" / "gen-b").exists()
    assert (root / "generations" / "gen-c").is_dir()
    assert (root / "generations" / "gen-d").is_dir()


def test_current_never_deleted_even_with_keep_zero(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2}, current="gen-b")
    report = run_retention(root, RetentionPolicy(keep=0))
    assert _names(report.deleted) == ["gen-a"]
    assert (root / "generations" / "gen-b").is_dir()
    assert any(skip.generation == "gen-b" and skip.reason == "current" for skip in report.skipped)


def test_keep_larger_than_candidates_deletes_nothing(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2}, current="gen-b")
    report = run_retention(root, RetentionPolicy(keep=5))
    assert report.deleted == ()
    assert (root / "generations" / "gen-a").is_dir()


# ------------------------------------------------------------------- pinned


def test_policy_pinned_generations_survive(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3}, current="gen-c")
    report = run_retention(root, RetentionPolicy(keep=0, pinned=("gen-a",)))
    assert _names(report.deleted) == ["gen-b"]
    assert (root / "generations" / "gen-a").is_dir()
    assert any(skip.generation == "gen-a" and skip.reason == "pinned" for skip in report.skipped)


def test_pinned_marker_file_survives(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2}, current="gen-b")
    (root / "generations" / "gen-a" / "PINNED").write_text("operator milestone\n", encoding="utf-8")
    report = run_retention(root, RetentionPolicy(keep=0))
    assert report.deleted == ()
    assert any(skip.generation == "gen-a" and skip.reason == "pinned-marker" for skip in report.skipped)


# ------------------------------------------------------- backup-referenced


def test_backup_referenced_generation_survives(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3}, current="gen-c")
    backup = tmp_path / "backup-1"
    backup.mkdir()
    (backup / "CURRENT").write_text("gen-a\n", encoding="utf-8")
    report = run_retention(root, RetentionPolicy(keep=0), backup_dirs=(backup,))
    assert _names(report.deleted) == ["gen-b"]
    assert (root / "generations" / "gen-a").is_dir()
    assert any(skip.generation == "gen-a" and skip.reason == "backup-referenced" for skip in report.skipped)


def test_missing_explicit_backup_dir_fails_closed(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1}, current="gen-a")
    with pytest.raises(RetentionError, match="backup"):
        run_retention(root, RetentionPolicy(keep=0), backup_dirs=(tmp_path / "nope",))


# ------------------------------------------------------------------ dry-run


def test_dry_run_deletes_nothing_and_writes_no_audit(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3}, current="gen-c")
    report = run_retention(root, RetentionPolicy(keep=1), dry_run=True)
    assert report.dry_run is True
    assert _names(report.deleted) == ["gen-a"]
    assert (root / "generations" / "gen-a").is_dir()
    assert not (root / DEFAULT_AUDIT_FILENAME).exists()
    assert report.audit_path is None


# -------------------------------------------------------------------- audit


def test_every_deletion_is_audited(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3}, current="gen-c")
    report = run_retention(root, RetentionPolicy(keep=1))
    audit_path = root / DEFAULT_AUDIT_FILENAME
    assert report.audit_path == str(audit_path)
    lines = audit_path.read_text(encoding="utf-8").splitlines()
    assert len(lines) == 1
    events = [json.loads(line) for line in lines]
    assert [event["generation"] for event in events] == ["gen-a"]
    assert [event["sequence"] for event in events] == [1]
    for event, record in zip(events, report.deleted, strict=True):
        assert event["event"] == "retention.delete"
        assert event["size_bytes"] == record.size_bytes
        assert event["file_count"] == record.file_count
        assert event["keep"] == 1
        assert event["reason"] == "over-keep"


def test_audit_sequence_continues_across_runs(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3}, current="gen-c")
    run_retention(root, RetentionPolicy(keep=1))  # deletes gen-a
    _make_generation(root, "gen-d", 4)
    (root / "CURRENT").write_text("gen-d\n", encoding="utf-8")
    run_retention(root, RetentionPolicy(keep=1))  # deletes gen-b
    lines = (root / DEFAULT_AUDIT_FILENAME).read_text(encoding="utf-8").splitlines()
    assert [json.loads(line)["generation"] for line in lines] == ["gen-a", "gen-b"]
    assert [json.loads(line)["sequence"] for line in lines] == [1, 2]


# -------------------------------------------------------------- fail-closed


def test_unreadable_manifest_is_skipped_not_deleted(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2}, current="gen-b")
    bad = root / "generations" / "gen-a"
    (bad / "manifest.json").write_text("not json {", encoding="utf-8")
    report = run_retention(root, RetentionPolicy(keep=0))
    assert report.deleted == ()
    assert bad.is_dir()
    assert any(skip.generation == "gen-a" and skip.reason == "unreadable-manifest" for skip in report.skipped)


def test_symlinked_generation_is_never_removed(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1}, current="gen-a")
    outside = tmp_path / "outside"
    outside.mkdir()
    (outside / "precious.txt").write_text("do not touch\n", encoding="utf-8")
    os.symlink(outside, root / "generations" / "gen-link")
    report = run_retention(root, RetentionPolicy(keep=0))
    assert report.deleted == ()
    assert (outside / "precious.txt").is_file()
    assert any(skip.generation == "gen-link" and skip.reason == "unsafe-symlink" for skip in report.skipped)


def test_missing_root_raises_typed_error(tmp_path: Path) -> None:
    with pytest.raises(RetentionError, match="projection root"):
        run_retention(tmp_path / "nope", RetentionPolicy())


def test_negative_keep_is_a_typed_error() -> None:
    with pytest.raises(RetentionError, match="keep"):
        RetentionPolicy(keep=-1)


def test_no_generations_dir_is_an_empty_run(tmp_path: Path) -> None:
    root = tmp_path / "projection"
    root.mkdir()
    report = run_retention(root, RetentionPolicy())
    assert report.deleted == ()
    assert report.kept == ()


# ------------------------------------------------------------ determinism


def test_plan_is_deterministic(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2, "gen-c": 3, "gen-d": 4}, current="gen-d")
    first = plan_retention(root, RetentionPolicy(keep=1))
    second = plan_retention(root, RetentionPolicy(keep=1))
    assert first == second
    # planning alone never deletes and never audits
    assert (root / "generations" / "gen-a").is_dir()
    assert not (root / DEFAULT_AUDIT_FILENAME).exists()


def test_deletion_records_carry_size_and_file_count(tmp_path: Path) -> None:
    root = _make_root(tmp_path, {"gen-a": 1, "gen-b": 2}, current="gen-b")
    report = run_retention(root, RetentionPolicy(keep=0))
    (record,) = report.deleted
    assert record.file_count == 2  # manifest.json + graph.json
    assert record.size_bytes > 0
    assert record.path.endswith("generations/gen-a")

"""Tests for ``scripts/license_guard.py`` (ATM F-D5).

The guard fails closed: a missing NOTICE, a NOTICE that lost the Graphify
attribution or the pinned dependency version, a ``pyproject.toml`` whose
``license-files`` dropped NOTICE, or a forbidden Graphify marker phrase in
``src/`` must each turn the guard red with an actionable message. A clean
repo (and a clean minimal fixture root) stays green.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "license_guard.py"

NOTICE_OK = (
    "Know Your Soul\n"
    "Copyright 2026 Know Your Soul contributors\n"
    "\n"
    "This product includes a design inspired by and a runtime dependency on\n"
    "Graphify (https://github.com/Graphlet-AI/graphify), Copyright 2026\n"
    "Safi Shamsi, licensed under Apache-2.0 OR MIT. The observed dependency\n"
    "is pinned as graphifyy==0.9.95 (see pyproject.toml and uv.lock).\n"
    "\n"
    "This product's ingestion pipeline (contract v3 addendum) uses the\n"
    "parser-category dependencies tree-sitter and tree-sitter-language-pack\n"
    "(https://github.com/tree-sitter/tree-sitter), Copyright the tree-sitter\n"
    "contributors, licensed under MIT; and pypdf (https://github.com/py-pdf/pypdf),\n"
    "Copyright the pypdf contributors, licensed under BSD-3-Clause.\n"
)

PYPROJECT_OK = (
    "[project]\n"
    'name = "fixture"\n'
    'license = "Apache-2.0"\n'
    'license-files = ["LICENSE", "NOTICE"]\n'
    'dependencies = ["graphifyy[mcp]==0.9.95"]\n'
)


def _load_guard() -> ModuleType:
    spec = importlib.util.spec_from_file_location("license_guard", SCRIPT)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _make_root(
    tmp_path: Path,
    *,
    notice: str | None = NOTICE_OK,
    pyproject: str | None = PYPROJECT_OK,
    license_text: str | None = "Apache License\n",
    src_files: dict[str, str] | None = None,
) -> Path:
    root = tmp_path / "repo"
    root.mkdir()
    if notice is not None:
        (root / "NOTICE").write_text(notice, encoding="utf-8")
    if pyproject is not None:
        (root / "pyproject.toml").write_text(pyproject, encoding="utf-8")
    if license_text is not None:
        (root / "LICENSE").write_text(license_text, encoding="utf-8")
    src = root / "src"
    src.mkdir()
    for rel, text in (src_files or {}).items():
        target = src / rel
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text(text, encoding="utf-8")
    return root


# ------------------------------------------------------------- the real repo


def test_real_repo_is_green() -> None:
    """Probe (plan F-D5): the clean repository passes the guard, exit 0."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ----------------------------------------------------------- minimal fixtures


def test_clean_minimal_root_passes(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, src_files={"pkg/mod.py": '"""clean"""\n'})
    assert guard.run_guard(root) == []
    assert guard.main(["--root", str(root)]) == 0


def test_missing_notice_fails(tmp_path: Path) -> None:
    """Probe (plan F-D5): deleting NOTICE turns the guard red."""
    guard = _load_guard()
    root = _make_root(tmp_path, notice=None)
    failures = guard.run_guard(root)
    assert any("NOTICE" in failure and "missing" in failure.lower() for failure in failures)
    assert guard.main(["--root", str(root)]) == 1


def test_notice_without_graphify_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("Graphify", "Upstream"))
    failures = guard.run_guard(root)
    assert any("Graphify" in failure for failure in failures)


def test_notice_without_author_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("Safi Shamsi", "Someone Else"))
    failures = guard.run_guard(root)
    assert any("Safi Shamsi" in failure for failure in failures)


def test_notice_without_pin_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("graphifyy==0.9.95", "graphifyy"))
    failures = guard.run_guard(root)
    assert any("graphifyy==0.9.95" in failure for failure in failures)


def test_pin_mismatch_with_pyproject_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(
        tmp_path,
        pyproject=PYPROJECT_OK.replace("==0.9.95", "==0.9.96"),
    )
    failures = guard.run_guard(root)
    assert any("graphifyy==0.9.96" in failure for failure in failures)


def test_unpinned_dependency_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(
        tmp_path,
        notice=NOTICE_OK,
        pyproject=PYPROJECT_OK.replace("graphifyy[mcp]==0.9.95", "graphifyy[mcp]"),
    )
    failures = guard.run_guard(root)
    assert any("pin" in failure.lower() for failure in failures)


def test_license_files_without_notice_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(
        tmp_path,
        pyproject=PYPROJECT_OK.replace('license-files = ["LICENSE", "NOTICE"]', 'license-files = ["LICENSE"]'),
    )
    failures = guard.run_guard(root)
    assert any("license-files" in failure and "NOTICE" in failure for failure in failures)


def test_missing_license_file_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, license_text=None)
    failures = guard.run_guard(root)
    assert any("LICENSE" in failure and "missing" in failure.lower() for failure in failures)


def test_missing_pyproject_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, pyproject=None)
    failures = guard.run_guard(root)
    assert any("pyproject.toml" in failure for failure in failures)


# ------------------------------------------------------------ src/ scanning


@pytest.mark.parametrize(
    "phrase",
    [
        "copied verbatim from graphify/export.py",
        "moved verbatim from graphify/cluster.py",
        "generated by /graphify build",
        "Copyright 2026 Safi Shamsi",
        "Copyright (c) 2026 Safi Shamsi",
    ],
)
def test_forbidden_phrases_in_src_fail(tmp_path: Path, phrase: str) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, src_files={"pkg/mod.py": f"# {phrase}\nx = 1\n"})
    failures = guard.run_guard(root)
    assert any("mod.py:1" in failure for failure in failures), failures
    assert guard.main(["--root", str(root)]) == 1


def test_forbidden_phrase_case_insensitive(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, src_files={"pkg/mod.py": "# COPIED VERBATIM FROM GRAPHIFY/x.py\n"})
    assert guard.run_guard(root) != []


def test_legitimate_graphify_mentions_pass(tmp_path: Path) -> None:
    """Naming the dependency or adapter module is not a violation."""
    guard = _load_guard()
    root = _make_root(
        tmp_path,
        src_files={
            "pkg/adapter.py": (
                '"""Adapter over the graphify read-model loader."""\nfrom graphify.analyze import god_nodes\n'
            )
        },
    )
    assert guard.run_guard(root) == []


def test_forbidden_scan_skips_files_outside_src(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path)
    (root / "docs").mkdir()
    (root / "docs" / "notes.md").write_text("copied verbatim from graphify\n", encoding="utf-8")
    assert guard.run_guard(root) == []


def test_multiple_failures_all_reported(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(
        tmp_path,
        notice=NOTICE_OK.replace("Safi Shamsi", "Someone Else"),
        src_files={"pkg/a.py": "# generated by /graphify\n"},
    )
    failures = guard.run_guard(root)
    assert len(failures) >= 2

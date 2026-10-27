"""Parser-category attribution tests for ``scripts/license_guard.py``.

Contract v3 (``docs/contracts/ingestion-contract-v3.md``) sanctions the
parser-category dependencies tree-sitter / tree-sitter-language-pack (MIT) and
pypdf (BSD-3-Clause). The license guard must keep their NOTICE attribution
auditable — attribution text present, declared parser dependencies exactly
``==`` pinned, and the pin recorded in NOTICE — and must keep rejecting
verbatim-copying markers, now also for the parser tooling.
"""

from __future__ import annotations

import importlib.util
import subprocess
import sys
from pathlib import Path
from types import ModuleType

REPO_ROOT = Path(__file__).resolve().parents[1]
SCRIPT = REPO_ROOT / "scripts" / "license_guard.py"

GRAPHIFY_NOTICE = (
    "This product includes a design inspired by and a runtime dependency on\n"
    "Graphify (https://github.com/Graphlet-AI/graphify), Copyright 2026\n"
    "Safi Shamsi, licensed under Apache-2.0 OR MIT. The observed dependency\n"
    "is pinned as graphifyy==0.9.95 (see pyproject.toml and uv.lock).\n"
)

PARSER_NOTICE = (
    "\nThis product's ingestion pipeline (contract v3 addendum) uses the\n"
    "parser-category dependencies tree-sitter and tree-sitter-language-pack\n"
    "(https://github.com/tree-sitter/tree-sitter), Copyright the tree-sitter\n"
    "contributors, licensed under MIT; and pypdf (https://github.com/py-pdf/pypdf),\n"
    "Copyright the pypdf contributors, licensed under BSD-3-Clause.\n"
)

NOTICE_OK = "Know Your Soul\nCopyright 2026 Know Your Soul contributors\n\n" + GRAPHIFY_NOTICE + PARSER_NOTICE

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
    name: str = "repo",
) -> Path:
    root = tmp_path / name
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


def _with_dependency(pyproject: str, dependency: str) -> str:
    return pyproject.replace(
        '"graphifyy[mcp]==0.9.95"]',
        f'"graphifyy[mcp]==0.9.95", "{dependency}"]',
    )


# ------------------------------------------------------------- the real repo


def test_real_repo_is_green() -> None:
    """The repository itself must carry the parser-category attribution."""
    result = subprocess.run(
        [sys.executable, str(SCRIPT), "--root", str(REPO_ROOT)],
        capture_output=True,
        text=True,
    )
    assert result.returncode == 0, result.stderr
    assert "OK" in result.stdout


# ------------------------------------------------------ NOTICE attributions


def test_clean_fixture_with_parser_attributions_passes(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, src_files={"pkg/mod.py": '"""clean"""\n'})
    assert guard.run_guard(root) == []
    assert guard.main(["--root", str(root)]) == 0


def test_notice_without_tree_sitter_attribution_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("tree-sitter", "syntax-lib"))
    failures = guard.run_guard(root)
    assert any("tree-sitter" in failure for failure in failures), failures
    assert guard.main(["--root", str(root)]) == 1


def test_notice_without_tree_sitter_license_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("licensed under MIT", "licensed permissively"))
    failures = guard.run_guard(root)
    assert any("tree-sitter" in failure and "MIT" in failure for failure in failures), failures


def test_notice_without_pypdf_attribution_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("pypdf", "pdf-lib"))
    failures = guard.run_guard(root)
    assert any("pypdf" in failure for failure in failures), failures


def test_notice_without_pypdf_license_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("BSD-3-Clause", "a permissive license"))
    failures = guard.run_guard(root)
    assert any("pypdf" in failure and "BSD" in failure for failure in failures), failures


def test_notice_missing_both_parser_attributions_reports_both(tmp_path: Path) -> None:
    guard = _load_guard()
    notice = "Know Your Soul\nCopyright 2026 Know Your Soul contributors\n\n" + GRAPHIFY_NOTICE
    root = _make_root(tmp_path, notice=notice)
    failures = guard.run_guard(root)
    assert any("tree-sitter" in failure for failure in failures), failures
    assert any("pypdf" in failure for failure in failures), failures


def test_graphify_attribution_is_still_required(tmp_path: Path) -> None:
    """Regression: extending the guard must not drop the Graphify rule."""
    guard = _load_guard()
    root = _make_root(tmp_path, notice=NOTICE_OK.replace("Safi Shamsi", "Someone Else"))
    failures = guard.run_guard(root)
    assert any("Safi Shamsi" in failure for failure in failures), failures


# ------------------------------------------- declared parser dependencies


def test_declared_parser_dependency_must_be_pinned(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, pyproject=_with_dependency(PYPROJECT_OK, "pypdf"))
    failures = guard.run_guard(root)
    assert any("pypdf" in failure and "pin" in failure.lower() for failure in failures), failures


def test_declared_parser_dependency_rejects_a_range_specifier(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, pyproject=_with_dependency(PYPROJECT_OK, "pypdf>=6"))
    failures = guard.run_guard(root)
    assert any("pypdf" in failure and "pin" in failure.lower() for failure in failures), failures


def test_pinned_parser_dependency_requires_the_notice_pin(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, pyproject=_with_dependency(PYPROJECT_OK, "pypdf==6.21.3"))
    failures = guard.run_guard(root)
    assert any("pypdf==6.21.3" in failure for failure in failures), failures


def test_pinned_parser_dependency_with_notice_pin_passes(tmp_path: Path) -> None:
    guard = _load_guard()
    notice = NOTICE_OK + "\nThe observed pypdf dependency is pinned as pypdf==6.21.3.\n"
    root = _make_root(tmp_path, notice=notice, pyproject=_with_dependency(PYPROJECT_OK, "pypdf==6.21.3"))
    assert guard.run_guard(root) == []


def test_tree_sitter_language_pack_pin_rule(tmp_path: Path) -> None:
    guard = _load_guard()
    pyproject = _with_dependency(PYPROJECT_OK, "tree-sitter-language-pack==1.22.1")
    root = _make_root(tmp_path, pyproject=pyproject)
    failures = guard.run_guard(root)
    assert any("tree-sitter-language-pack==1.22.1" in failure for failure in failures), failures
    notice = (
        NOTICE_OK
        + "\nThe observed tree-sitter-language-pack dependency is pinned as tree-sitter-language-pack==1.22.1.\n"
    )
    root = _make_root(tmp_path, notice=notice, pyproject=pyproject, name="repo2")
    assert guard.run_guard(root) == []


def test_tree_sitter_language_pack_is_not_confused_with_tree_sitter(tmp_path: Path) -> None:
    """Prefix bleed: declaring tree-sitter-language-pack must not satisfy (or
    be satisfied by) the tree-sitter attribution entry — names are exact."""
    guard = _load_guard()
    pyproject = _with_dependency(PYPROJECT_OK, "tree-sitter==0.25.2")
    root = _make_root(tmp_path, pyproject=pyproject)
    failures = guard.run_guard(root)
    assert any("tree-sitter==0.25.2" in failure for failure in failures), failures
    assert all("tree-sitter-language-pack==" not in failure for failure in failures), failures


# ------------------------------------------------------ src/ marker scanning


def test_verbatim_tree_sitter_marker_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, src_files={"pkg/ingest.py": "# copied verbatim from tree-sitter/src\nx = 1\n"})
    failures = guard.run_guard(root)
    assert any("ingest.py:1" in failure for failure in failures), failures


def test_verbatim_pypdf_marker_fails(tmp_path: Path) -> None:
    guard = _load_guard()
    root = _make_root(tmp_path, src_files={"pkg/pdf.py": "# moved verbatim from pypdf/generic.py\nx = 1\n"})
    failures = guard.run_guard(root)
    assert any("pdf.py:1" in failure for failure in failures), failures


def test_legitimate_parser_mentions_pass(tmp_path: Path) -> None:
    """Importing the sanctioned parser libraries is not a license violation."""
    guard = _load_guard()
    root = _make_root(
        tmp_path,
        src_files={"pkg/extract.py": '"""Extractor over tree-sitter and pypdf."""\nimport pypdf\n'},
    )
    assert guard.run_guard(root) == []

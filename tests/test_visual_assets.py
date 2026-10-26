"""Tests for the generated visual assets in docs/assets/.

The assets are produced by ``tools/generate_visual_assets.py`` from real
project data (frozen bench corpus projection, real scoring engine, real
bench ledgers). These tests assert the committed files are valid SVG, carry
real-data markers, and are byte-identical to a fresh deterministic
regeneration.
"""

from __future__ import annotations

import re
import subprocess
import sys
import xml.etree.ElementTree as ET
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
ASSETS = REPO_ROOT / "docs" / "assets"
GENERATOR = REPO_ROOT / "tools" / "generate_visual_assets.py"

SVG_NAMES = (
    "graph-formation.svg",
    "learning-timeline.svg",
    "bench-impact.svg",
    "architecture.svg",
)


@pytest.mark.parametrize("name", SVG_NAMES)
def test_asset_is_valid_svg(name: str) -> None:
    path = ASSETS / name
    assert path.exists(), f"{name} missing — run tools/generate_visual_assets.py"
    assert 5_000 < path.stat().st_size < 500_000, f"{name} has an implausible size"
    root = ET.parse(path).getroot()
    assert root.tag == "{http://www.w3.org/2000/svg}svg"


def test_graph_formation_uses_real_projection() -> None:
    content = (ASSETS / "graph-formation.svg").read_text(encoding="utf-8")
    # KROOT, all seven primary domains, and a real corpus file node.
    assert "KROOT" in content
    for dom in ("P1", "P2", "P3", "P4", "P5", "P6", "P7"):
        assert f">{dom}<" in content
    assert "06-heuristics-catalog" in content
    assert "36 nodes" in content and "35" in content


def test_bench_impact_uses_real_ledger() -> None:
    content = (ASSETS / "bench-impact.svg").read_text(encoding="utf-8")
    ledger_names = re.findall(r"ledger-\d{8}T\d{6}Z\.json", content)
    assert ledger_names, "bench-impact.svg must name its source ledger"
    assert (REPO_ROOT / "bench" / "results" / ledger_names[0]).exists()
    case_ids = set(re.findall(r">(q\d\d-[a-z0-9-]+)<", content))
    assert len(case_ids) == 24
    assert "mean 94." in content


def test_learning_timeline_shows_real_scores() -> None:
    content = (ASSETS / "learning-timeline.svg").read_text(encoding="utf-8")
    assert "P · proficiency" in content
    assert "M · maturity" in content
    assert "E · effectiveness" in content
    final = re.search(r"final: P=([\d.]+) M=([\d.]+) E=([\d.]+)", content)
    assert final, "learning-timeline.svg must annotate the final P/M/E scores"
    assert all(0.0 <= float(v) <= 1.0 for v in final.groups())
    assert "node.upsert" in content and "relation.add" in content


def test_assets_are_byte_deterministic() -> None:
    """Regenerate into a temp dir and byte-compare (generator --check)."""
    ledger = re.findall(
        r"ledger-\d{8}T\d{6}Z\.json",
        (ASSETS / "bench-impact.svg").read_text(encoding="utf-8"),
    )[0]
    result = subprocess.run(
        [sys.executable, str(GENERATOR), "--check", "--ledger", ledger],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=300,
    )
    assert result.returncode == 0, f"regeneration drifted:\n{result.stdout}\n{result.stderr}"

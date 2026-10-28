#!/usr/bin/env python3
"""Real benchmark: Rust kernel vs the Python reference extractor (W5, SUB-93).

Measures in-process extraction time (parse + walk + artifact) for:

* every phase-1 fixture (12 languages), median of N runs;
* one large deterministic source file (~1 MiB Python), median of N runs.

The Rust side runs through ``know_your_soul_kernel`` (loaded by
``kernel_bridge``); the Python side runs ``TreeSitterExtractor`` directly.
Both sides are verified byte-identical before timing — a faster but divergent
kernel would be a bug, not a win.

Usage: ``.venv/bin/python kernel-rust/benchmark.py [--runs 25]``
Output: markdown table to stdout (consumed by docs/recovery/w5-rust-kernel.md).
"""

from __future__ import annotations

import argparse
import statistics
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from know_your_soul.ingest import kernel_bridge  # noqa: E402
from know_your_soul.ingest.extractor import TreeSitterExtractor  # noqa: E402
from know_your_soul.ingest.languages import get_spec, supported_languages  # noqa: E402

FIXTURES = REPO_ROOT / "tests" / "fixtures" / "ingest"


def big_python_source() -> str:
    """A deterministic ~1 MiB Python source (no clock, no randomness)."""
    lines: list[str] = ["import os", "import collections", "MAX_ITEMS = 128", ""]
    for index in range(1400):
        lines.append(f"class Model{index}:")
        lines.append(f'    """Docstring for model {index}."""')
        lines.append("    LIMIT = 32")
        for meth in range(4):
            lines.append(f"    def method_{meth}(self, value_{index}):")
            lines.append(f"        result = compute(value_{index}, offset={meth})")
            lines.append("        return result")
        lines.append("")
        lines.append(f"def top_level_{index}(arg):")
        lines.append(f"    instance = Model{index}()")
        lines.append(f"    return instance.method_0({index})")
        lines.append("")
    return "\n".join(lines) + "\n"


def time_median(fn, runs: int) -> float:
    samples: list[float] = []
    for _ in range(runs):
        start = time.perf_counter_ns()
        fn()
        samples.append((time.perf_counter_ns() - start) / 1e6)
    return statistics.median(samples)


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--runs", type=int, default=25)
    args = parser.parse_args()

    if not kernel_bridge.kernel_available():
        print(f"kernel unavailable: {kernel_bridge.kernel_unavailable_reason()}", file=sys.stderr)
        return 1
    kernel = kernel_bridge.kernel_module()

    rows: list[tuple[str, int, float, float, float]] = []
    for language in supported_languages():
        fixture = sorted((FIXTURES / language).iterdir())[0]
        source = fixture.read_text(encoding="utf-8")
        display = f"{language}/{fixture.name}"
        # byte-identity guard before any timing claim
        python_json = TreeSitterExtractor(get_spec(language)).extract(display, source).canonical_json()
        rust_json = kernel.extract_source(display, source, language, False)
        assert rust_json == python_json, f"{language}: kernel diverged — refusing to benchmark"

        def run_python() -> None:
            TreeSitterExtractor(get_spec(language)).extract(display, source)

        def time_rust() -> None:
            kernel.extract_source(display, source, language, False)

        python_ms = time_median(run_python, args.runs)
        rust_ms = time_median(time_rust, args.runs)
        rows.append((f"fixture `{display}`", len(source.encode()), python_ms, rust_ms, python_ms / rust_ms))

    big = big_python_source()
    big_bytes = len(big.encode())
    assert big_bytes < 1024 * 1024, "benchmark source must stay under the 1 MiB bound"
    python_json = TreeSitterExtractor(get_spec("python")).extract("bench/big.py", big).canonical_json()
    rust_json = kernel.extract_source("bench/big.py", big, "python", False)
    assert rust_json == python_json, "big file: kernel diverged"

    def run_python_big() -> None:
        TreeSitterExtractor(get_spec("python")).extract("bench/big.py", big)

    def run_rust_big() -> None:
        kernel.extract_source("bench/big.py", big, "python", False)

    python_ms = time_median(run_python_big, max(3, args.runs // 5))
    rust_ms = time_median(run_rust_big, max(3, args.runs // 5))
    rows.append((f"big file `bench/big.py` ({1400} classes)", big_bytes, python_ms, rust_ms, python_ms / rust_ms))

    print("| input | bytes | python (ms) | rust (ms) | speedup |")
    print("|---|---|---|---|---|")
    for name, size, py_ms, rs_ms, speedup in rows:
        print(f"| {name} | {size} | {py_ms:.3f} | {rs_ms:.3f} | {speedup:.1f}x |")
    fixture_rows = rows[:-1]
    geo = 1.0
    for _, _, py_ms, rs_ms, _ in fixture_rows:
        geo *= py_ms / rs_ms
    geo **= 1.0 / len(fixture_rows)
    print(f"\nfixture geometric mean speedup: {geo:.2f}x; big file: {rows[-1][4]:.1f}x")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

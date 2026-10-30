"""Tests for the token-reduction benchmark harness (SUB-108, wave 8).

The harness lives in ``bench/tokens/run.py`` (a script, loaded here as a
module, same pattern as ``test_codebase_bench.py``). The corpus is vendored
under ``bench/tokens/corpus/`` (sha256-pinned in ``bench/tokens/manifest.json``)
— fully offline, no fetch step.

Gates pinned here:

* corpus integrity vs ``manifest.json`` (per-file sha256, exact file set);
* estimator parity with the frozen contract formula
  ``ceil(utf8_byte_length / 4)`` (``know_your_soul.contracts``);
* question-set integrity: every question's source files exist in its tier,
  tiers are nested (small ⊂ medium ⊂ large), file counts match the pins;
* determinism: two full measurement passes produce identical per-question
  results (hits, query tokens, rubric coverage) and identical tier metrics;
* quality gate: rubric coverage at/above the suite floor and retrieval
  recall (every positive question retrieves at least one hit, every
  negative control retrieves none);
* ledger structure: the machine-readable result carries every field the
  report table is rendered from.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path
from typing import Any

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
TOKENS_BENCH_PATH = REPO_ROOT / "bench" / "tokens" / "run.py"


def _load_tokens_bench() -> Any:
    spec = importlib.util.spec_from_file_location("kys_tokens_bench_run", TOKENS_BENCH_PATH)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module  # dataclass needs the module registered
    spec.loader.exec_module(module)
    return module


tb = _load_tokens_bench()

CORPUS_MISSING = not tb.corpus_present()
requires_corpus = pytest.mark.skipif(
    CORPUS_MISSING,
    reason="token bench corpus not vendored (expected at bench/tokens/data/corpus.tar.gz)",
)

_MEASURED: dict[str, Any] | None = None


def measured() -> dict[str, Any]:
    """One shared measurement per pytest process (a pass costs ~35s: each

    tier is ingested twice for the byte-determinism check)."""
    global _MEASURED
    if _MEASURED is None:
        _MEASURED = tb.measure()
    assert _MEASURED is not None
    return _MEASURED


# ------------------------------------------------------------- self checks


class TestSelfChecks:
    def test_check_exits_zero(self):
        assert tb.cmd_check() == 0

    @requires_corpus
    def test_verify_exits_zero(self):
        assert tb.cmd_verify() == 0

    def test_estimator_matches_frozen_contract(self):
        # The suite estimator must be the frozen contract formula:
        # ceil(utf8_byte_length / 4). The contract helper measures the
        # canonical-JSON payload; the suite measures raw bytes — same formula
        # on both sides of the encoding boundary.
        from know_your_soul.contracts import canonical_json, estimated_tokens_of_payload

        for text in ("", "a", "abcd", "abcde", "héllo wörld", "x" * 1025):
            raw = text.encode("utf-8")
            assert tb.estimate_tokens_bytes(raw) == -(-len(raw) // 4)
            payload = canonical_json(text).encode("utf-8")
            assert estimated_tokens_of_payload(text) == tb.estimate_tokens_bytes(payload)


class TestQuestionSet:
    def test_tiers_nested_and_counts(self):
        assert set(tb.TIER_FILES) == {"small", "medium", "large"}
        small = set(tb.TIER_FILES["small"])
        medium = set(tb.TIER_FILES["medium"])
        large = set(tb.TIER_FILES["large"])
        assert small < medium < large
        # Pinned sizes: small mirrors the "fits in context" scale, large the
        # 52-file scale of the documented Graphify reference claim.
        assert len(tb.TIER_FILES["small"]) == tb.TIER_FILE_COUNTS["small"]
        assert len(tb.TIER_FILES["medium"]) == tb.TIER_FILE_COUNTS["medium"]
        assert len(tb.TIER_FILES["large"]) == tb.TIER_FILE_COUNTS["large"]

    def test_questions_reference_corpus_files(self):
        questions = tb.load_questions()
        for tier, specs in questions["tiers"].items():
            tier_files = set(tb.TIER_FILES[tier])
            for q in specs:
                assert q["question"].strip(), f"{tier}/{q['id']}: empty question"
                for source in q["source_files"]:
                    assert source in tier_files, f"{tier}/{q['id']}: {source} not in tier"
                if q.get("expect_hits", True):
                    assert q["rubric_terms"], f"{tier}/{q['id']}: positive question without rubric"

    def test_each_tier_has_negative_control(self):
        questions = tb.load_questions()
        for tier, specs in questions["tiers"].items():
            negatives = [q for q in specs if not q.get("expect_hits", True)]
            assert negatives, f"tier {tier}: no negative-control question"


# --------------------------------------------------------------- run gates


@requires_corpus
class TestDeterminism:
    def test_two_measure_passes_identical(self):
        result = measured()
        assert result["determinism"]["identical"] is True
        assert result["determinism"]["pass_a_sha256"] == result["determinism"]["pass_b_sha256"]

    def test_measure_twice_same_metrics(self):
        first = measured()
        second = tb.measure()
        assert tb.metrics_view(first) == tb.metrics_view(second)


@requires_corpus
class TestQualityGate:
    def test_rubric_coverage_above_floor(self):
        result = measured()
        for tier, summary in result["tiers"].items():
            assert summary["mean_rubric_coverage"] >= tb.RUBRIC_FLOOR, (
                f"{tier}: mean rubric coverage {summary['mean_rubric_coverage']} < floor {tb.RUBRIC_FLOOR}"
            )

    def test_retrieval_recall(self):
        result = measured()
        for tier, summary in result["tiers"].items():
            for q in summary["questions"]:
                if q.expect_hits:
                    assert q.hits >= 1, f"{tier}/{q.id}: positive question retrieved nothing"
                else:
                    assert q.hits == 0, f"{tier}/{q.id}: negative control returned hits"

    def test_run_gate_passes(self):
        result = measured()
        gate = tb.evaluate_gate(result)
        assert gate["failures"] == []
        assert gate["ok"] is True


@requires_corpus
class TestLedger:
    def test_ledger_structure(self, tmp_path: Path):
        result = measured()
        ledger = tb.build_ledger(result, stamp="20000101T000000Z")
        blob = json.dumps(ledger, indent=2, sort_keys=True)
        assert json.loads(blob) == ledger  # JSON round-trip clean
        assert ledger["suite"] == "tokens"
        assert ledger["estimator"] == "ceil(utf8_bytes/4)"
        assert ledger["budget_tokens"] == tb.CONTEXT_BUDGET_TOKENS
        for tier in ("small", "medium", "large"):
            entry = ledger["tiers"][tier]
            for key in (
                "files",
                "bytes",
                "raw_tokens",
                "mean_query_tokens",
                "reduction_ratio",
                "saving_pct",
                "mean_rubric_coverage",
                "questions",
            ):
                assert key in entry, f"{tier}: ledger missing {key}"
            assert entry["raw_tokens"] > 0
            assert entry["mean_query_tokens"] > 0
            assert entry["reduction_ratio"] > 0
        reference = ledger["graphify_reference"]
        assert reference["reduction_ratio"] == 71.5
        assert "different corpus" in reference["note"]
        assert ledger["determinism"]["identical"] is True

    def test_report_renders_per_tier_table(self):
        result = measured()
        ledger = tb.build_ledger(result, stamp="20000101T000000Z")
        report = tb.render_markdown(ledger)
        assert "| Tier | Files |" in report
        assert "71.5x" in report  # documented reference, clearly labelled
        for tier in ("small", "medium", "large"):
            assert f"| {tier} |" in report

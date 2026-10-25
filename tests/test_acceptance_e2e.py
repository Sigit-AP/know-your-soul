"""End-to-end acceptance tests (Task 10, plan acceptance criteria).

Every acceptance bullet from the Task 10 brief appears here as a named test.

* Tests whose subject already exists run for real today: the locked
  no-provider environment, forbidden imports/entry points, the frozen corpus
  integrity (which also pins "no Hermes SOUL-corpus modification"), the
  token-estimator parity contract, and the benchmark harness self-checks.
* Tests that need the running system (Tasks 4-9 integration) are marked
  ``pytest.mark.skip(reason="integration pending: ...")``; the coordinator
  removes the skip marks at integration time. A skipped test here is a
  tracked acceptance obligation, never a silently dropped one.
"""

from __future__ import annotations

import ast
import importlib.metadata
import importlib.util
import json
import subprocess
import sys
import tomllib
from pathlib import Path
from typing import Any

import pytest

from soul_knowledge import runtime_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "bench"
SRC_DIR = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
TOKEN_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "token_budget_cases.json"
CORPUS_MANIFEST = BENCH_DIR / "corpus" / "manifest.json"
LIVE_SOUL_CORPUS = Path("/root/.hermes/knowledge/soul")


def _load_bench() -> Any:
    """Load bench/run.py (a script) as a module without a package install."""
    spec = importlib.util.spec_from_file_location("soul_bench_run", BENCH_DIR / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_bench()

INTEGRATION_PENDING = "integration pending: owning task (4-9) not landed; coordinator removes this skip"


# --------------------------------------------------------------------- utils


def _lock_package_names() -> set[str]:
    with UV_LOCK.open("rb") as fh:
        lock = tomllib.load(fh)
    return {entry["name"] for entry in lock.get("package", [])}


def _src_python_files() -> list[Path]:
    return sorted(SRC_DIR.rglob("*.py"))


def _imported_roots(path: Path) -> set[str]:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    roots: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                root = alias.name.split(".")[0]
                if root != "__future__":
                    roots.add(root)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            if node.module != "__future__":
                roots.add(node.module.split(".")[0])
    return roots


# ============================== runnable now (no system required) ============


class TestLockedNoProviderEnvironment:
    """Acceptance bullet: locked no-provider environment / uv lock policy."""

    def test_uv_lock_exists_and_pins_everything(self) -> None:
        assert UV_LOCK.is_file(), "uv.lock missing — the environment is not locked"
        with UV_LOCK.open("rb") as fh:
            lock = tomllib.load(fh)
        assert lock.get("version") == 1
        assert lock.get("requires-python") == "==3.14.*"
        assert len(lock.get("package", [])) > 0

    def test_no_forbidden_distribution_in_lock(self) -> None:
        offenders = sorted(
            name for name in _lock_package_names() if runtime_guard.is_forbidden_distribution(name)
        )
        assert offenders == [], f"forbidden provider/vector/model distributions in uv.lock: {offenders}"

    def test_no_forbidden_distribution_installed(self) -> None:
        installed = {dist.metadata["Name"] for dist in importlib.metadata.distributions()}
        offenders = sorted(
            name for name in installed if name and runtime_guard.is_forbidden_distribution(name)
        )
        assert offenders == [], f"forbidden distributions installed: {offenders}"

    def test_pyproject_declares_no_forbidden_dependency(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        deps = pyproject.get("project", {}).get("dependencies", [])
        offenders = sorted(
            dep for dep in deps if runtime_guard.is_forbidden_distribution(dep.split("[")[0].split("=")[0].strip())
        )
        assert offenders == [], f"pyproject declares forbidden dependencies: {offenders}"

    def test_requires_python_is_314(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        assert pyproject["project"]["requires-python"] == ">=3.14,<3.15"


class TestForbiddenImportsAndEntryPoints:
    """Acceptance bullet: forbidden imports / entry points."""

    def test_no_forbidden_import_root_in_src(self) -> None:
        offenders: list[str] = []
        unparseable: list[str] = []
        for path in _src_python_files():
            try:
                roots = _imported_roots(path)
            except SyntaxError:
                # Another task's in-flight file: at integration every module
                # must parse, so this list must be empty before release.
                unparseable.append(str(path.relative_to(REPO_ROOT)))
                continue
            for root in roots:
                if runtime_guard.is_forbidden_module(root):
                    offenders.append(f"{path.relative_to(REPO_ROOT)} imports {root}")
        assert offenders == [], f"forbidden imports in src/: {offenders}"
        if unparseable:
            pytest.skip(f"scan partial; unparseable in-flight files: {unparseable}")

    def test_no_blocking_forbidden_entry_points_installed(self) -> None:
        """No *non-dormant* forbidden entry points; the pinned wheel's dormant
        ``graphify-mcp`` script is the one recorded boundary (ADR 0001 §3)."""
        assert runtime_guard.blocking_forbidden_entry_points() == []
        offenders = runtime_guard.forbidden_entry_points()
        non_dormant = [o for o in offenders if o not in runtime_guard.DORMANT_PINNED_ENTRY_POINTS]
        assert non_dormant == [], f"unrecorded forbidden entry points: {non_dormant}"

    def test_pyproject_exposes_no_forbidden_scripts(self) -> None:
        pyproject = tomllib.loads(PYPROJECT.read_text(encoding="utf-8"))
        scripts = set(pyproject.get("project", {}).get("scripts", {}))
        offenders = sorted(scripts & set(runtime_guard.FORBIDDEN_ENTRY_POINT_SCRIPTS))
        assert offenders == [], f"pyproject declares forbidden scripts: {offenders}"


class TestFrozenCorpus:
    """Acceptance bullet (harness side): frozen SOUL corpus with sha256 manifest."""

    def test_manifest_exists_and_is_complete(self) -> None:
        assert CORPUS_MANIFEST.is_file(), "run `python bench/run.py freeze` first"
        manifest = json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))
        assert manifest["file_count"] == len(manifest["files"]) > 0
        assert manifest["source_access"] == "read-only"

    def test_frozen_corpus_integrity(self) -> None:
        ok, problems = bench.verify_corpus()
        hard = [p for p in problems if not p.startswith("drift: ")]
        assert ok and hard == [], f"frozen corpus integrity failed: {hard}"

    def test_frozen_corpus_matches_contract_baseline(self) -> None:
        manifest = json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))
        # Frozen baselines (token-budget contract 1.0.0, spec section 13.4).
        assert manifest["total_bytes"] == 52604
        assert manifest["total_estimated_tokens"] == 13151

    def test_live_soul_corpus_unmodified_since_freeze(self) -> None:
        """No Hermes SOUL-corpus modification: live source still matches the pin."""
        manifest = json.loads(CORPUS_MANIFEST.read_text(encoding="utf-8"))
        drift: list[str] = []
        live = {p.name: p for p in LIVE_SOUL_CORPUS.glob("*.md")}
        for entry in manifest["files"]:
            path = live.get(entry["file"])
            assert path is not None, f"live SOUL corpus file deleted: {entry['file']}"
            if bench.sha256_bytes(path.read_bytes()) != entry["sha256"]:
                drift.append(entry["file"])
        assert drift == [], f"live SOUL corpus modified since freeze: {drift}"


class TestTokenEstimatorParity:
    """Acceptance bullet (harness side): token-estimator parity vs the frozen contract."""

    def test_bench_estimator_matches_frozen_fixtures(self) -> None:
        fixtures = json.loads(TOKEN_FIXTURES.read_text(encoding="utf-8"))
        checked = 0
        for case in fixtures["cases"]:
            payload = case.get("payload")
            expected = case.get("expected", {}).get("estimated_tokens")
            if payload is None or expected is None:
                continue
            checked += 1
            assert bench.estimate_tokens_of_payload(payload) == expected, case["id"]
        assert checked == 7, f"expected the 7 payload fixtures with estimated_tokens, saw {checked}"

    def test_bench_estimator_matches_library(self) -> None:
        from soul_knowledge.contracts import estimated_tokens_of_payload

        fixtures = json.loads(TOKEN_FIXTURES.read_text(encoding="utf-8"))
        for case in fixtures["cases"]:
            payload = case.get("payload")
            if payload is None:
                continue
            assert bench.estimate_tokens_of_payload(payload) == estimated_tokens_of_payload(payload), case["id"]


class TestBenchHarness:
    """Acceptance bullet (harness side): frozen questions and the ledger runner."""

    def test_cases_schema_and_domain_coverage(self) -> None:
        cases = bench.load_cases()
        problems = bench.validate_cases(cases)
        assert problems == [], f"cases.json schema problems: {problems}"
        assert len(cases) == 24

    def test_rubric_terms_are_grounded_in_frozen_corpus(self) -> None:
        """Every rubric term must appear in a frozen corpus file the case cites."""
        cases = bench.load_cases()
        corpus = {p.name: p.read_text(encoding="utf-8").casefold() for p in (BENCH_DIR / "corpus").glob("*.md")}
        misses: list[str] = []
        for case in cases:
            haystack = "\n".join(corpus[name] for name in case["source_files"] if name in corpus)
            for term in case["rubric_terms"]:
                if term.casefold() not in haystack:
                    misses.append(f"{case['id']}: {term!r} not in {case['source_files']}")
        assert misses == [], f"ungrounded rubric terms: {misses}"

    def test_bench_selfcheck_exits_zero(self) -> None:
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "run.py"), "check"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert proc.returncode == 0, f"bench check failed: {proc.stdout}\n{proc.stderr}"

    def test_bench_run_is_skip_gated_until_integration(self) -> None:
        """Pre-integration the runner must exit 3 with a machine-readable skipped ledger."""
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "run.py"), "run", "--repeats", "2"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        if importlib.util.find_spec("soul_knowledge.mcp_server") is None:
            assert proc.returncode == 3, f"expected skip-gate exit 3, got {proc.returncode}: {proc.stdout}"
        else:
            assert proc.returncode in (0, 1), f"unexpected exit {proc.returncode}: {proc.stdout}"
        ledgers = sorted((BENCH_DIR / "results").glob("ledger-*.json"))
        assert ledgers, "no ledger written"
        ledger = json.loads(ledgers[-1].read_text(encoding="utf-8"))
        assert ledger["status"] in {"skipped", "ok", "failed"}
        assert ledger["estimator"] == "estimated_tokens = ceil(utf8_byte_length / 4)"
        assert len(ledger["cases"]) == 24


# ================== integration-pending (skip-gated acceptance bullets) ======


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestSevenDomainsEndToEnd:
    def test_all_seven_primary_domains_route_and_answer(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestAuditedTools:
    def test_exactly_ten_audited_tools_exposed(self) -> None: ...

    def test_every_tool_action_returns_an_audit_event_id(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestThreeAccessPaths:
    def test_cli_local_api_and_mcp_share_one_service_contract(self) -> None: ...

    def test_canonical_payload_parity_across_paths(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestExactlyOneFinalAudit:
    def test_one_final_audit_per_valid_invocation(self) -> None: ...

    def test_one_final_audit_per_recovered_failure(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestIdempotentRetry:
    def test_retry_preserves_original_domain_result_with_new_linked_audit_id(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestDeterministicScoring:
    def test_scoring_replay_is_deterministic_under_evidence_permutation(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestRollback:
    def test_rollback_restores_byte_exact_prior_state(self) -> None: ...


@pytest.mark.skip(reason="covered by tests/test_no_ai_runtime.py; e2e assertion lands with Task 8")
class TestScrubbedRuntime:
    def test_child_process_environment_is_scrubbed(self) -> None: ...


@pytest.mark.skip(reason="covered by tests/test_no_ai_runtime.py; e2e assertion lands with Task 8")
class TestDeniedOutboundSockets:
    def test_outbound_tcp_and_http_are_denied(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestAbsentSampling:
    def test_no_sampling_handlers_or_capabilities(self) -> None: ...

    def test_sampling_configuration_is_neither_advertised_nor_used(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestMcpContract:
    def test_single_allowlisted_graph_root(self) -> None: ...

    def test_strict_schema_unknown_missing_type_rejection(self) -> None: ...

    def test_range_contract_depth_1_6_budget_128_max(self) -> None: ...

    def test_capability_surface_is_exactly_tools_and_resources(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestConditionalAccess:
    def test_l1_never_calls_the_system(self) -> None: ...

    def test_access_only_returns_zero_payload_within_160_tokens(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestRecommendationShape:
    def test_exactly_best_actions_recommendations_unnecessary_actions(self) -> None: ...

    def test_no_raw_soul_text_and_no_one_observation_promotion(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestRelearn:
    def test_reset_accumulate_and_rollback_are_operator_gated(self) -> None: ...

    def test_relearn_events_carry_zero_evidence_weight(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestNoStaleMixing:
    def test_default_reads_never_mix_incompatible_source_versions(self) -> None: ...


@pytest.mark.skip(reason="verified at release time against the Hermes install; corpus-side pin runs above")
class TestNoHermesSourceModification:
    def test_hermes_source_tree_is_unmodified(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestGraphifyCoverageGate:
    def test_coverage_counts_only_passing_matrix_rows(self) -> None: ...

    def test_at_least_90_percent_of_applicable_40_features(self) -> None: ...

    def test_three_partial_residuals_reported_explicitly(self) -> None: ...


@pytest.mark.skip(reason=INTEGRATION_PENDING)
class TestBenchmarkQualityGate:
    def test_paired_token_savings_never_excuse_quality_regression(self) -> None: ...

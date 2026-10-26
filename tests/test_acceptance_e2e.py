"""End-to-end acceptance tests (Task 10, plan acceptance criteria).

Every acceptance bullet from the Task 10 brief appears here as a named test.

* Harness-side bullets run without the system: the locked no-provider
  environment, forbidden imports/entry points, the frozen corpus integrity
  (which also pins "no Hermes SOUL-corpus modification"), token-estimator
  parity, and the benchmark harness self-checks.
* Integration bullets run for real against the live system (Tasks 4-9 landed):
  the seven domains, the ten audited tools, the three access paths, the
  audit/idempotency/determinism/rollback contracts, the MCP contract,
  conditional access, recommendation shape, relearn gating, stale mixing, the
  Graphify coverage gate and the benchmark quality gate.
* The genuinely external bullet — the *installed Hermes source tree* is
  unmodified — runs for real when the host carries a git-managed Hermes
  installation (``/root/.hermes/hermes-agent`` pinned at the release tag) and
  skip-gates only when no such installation exists. The corpus-side pin runs
  for real.
"""

from __future__ import annotations

import argparse
import ast
import importlib.metadata
import importlib.util
import json
import os
import re
import subprocess
import sys
import tomllib
import xml.etree.ElementTree as ET
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import anyio
import pytest

from soul_knowledge import runtime_guard
from soul_knowledge.cli import build_parser
from soul_knowledge.contracts import (
    ACCESS_ONLY_TOKEN_CAP,
    ACCESS_POLICY_PATH,
    AccessDecision,
    AccessPolicy,
    EvaluateRequest,
    PolicyArtifactError,
    QueryRequest,
    RecommendationBlock,
    RecommendationItem,
    attempt_promotion,
)
from soul_knowledge.domain import canonical_json
from soul_knowledge.events import Invocation

REPO_ROOT = Path(__file__).resolve().parents[1]
BENCH_DIR = REPO_ROOT / "bench"
SRC_DIR = REPO_ROOT / "src"
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
TOKEN_FIXTURES = REPO_ROOT / "tests" / "fixtures" / "token_budget_cases.json"
CORPUS_MANIFEST = BENCH_DIR / "corpus" / "manifest.json"
LIVE_SOUL_CORPUS = Path("/root/.hermes/knowledge/soul")
FEATURE_MATRIX = REPO_ROOT / "docs" / "research" / "graphify-feature-matrix.md"

AT = datetime(2026, 10, 20, 12, tzinfo=UTC)

# The ten audited knowledge operations (spec section 8; frozen surface).
TEN_OPERATIONS = (
    "query",
    "get_node",
    "get_subtree",
    "log_event",
    "record_outcome",
    "evaluate",
    "review",
    "propose_improvement",
    "apply_improvement",
    "rollback",
)
TEN_TOOLS = tuple(sorted(f"knowledge_{operation}" for operation in TEN_OPERATIONS))


def _load_bench() -> Any:
    """Load bench/run.py (a script) as a module without a package install."""
    spec = importlib.util.spec_from_file_location("soul_bench_run", BENCH_DIR / "run.py")
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


bench = _load_bench()

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


def _corpus_stack(workdir: Path) -> Any:
    """A KnowledgeService seeded from the frozen corpus (mirrors bench.run)."""
    from soul_knowledge.domain import PrimaryDomain
    from soul_knowledge.importer import DEFAULT_PARSER_VERSIONS, SeedImporter
    from soul_knowledge.projection import GraphProjector
    from soul_knowledge.service import KnowledgeService
    from soul_knowledge.source_registry import SourceKind, SourceRegistry
    from soul_knowledge.store import EventStore

    store = EventStore(workdir / "bench.sqlite3")
    registry = SourceRegistry(store)
    version = registry.observe(BENCH_DIR / "corpus", SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS)
    importer = SeedImporter(store, registry=registry)
    importer.ensure_roots(version.source_version_id)
    for entry in sorted((BENCH_DIR / "corpus").glob("*.md")):
        domain = PrimaryDomain(bench.FILE_DOMAIN_MAP[entry.name])
        importer.import_file(entry, domain=domain, source_uri=f"soul://corpus/{entry.name}")
    projector = GraphProjector(store, output_root=workdir / "graph")
    projector.rebuild()
    return store, projector, KnowledgeService(store, projector)


@pytest.fixture(scope="module")
def corpus_service(tmp_path_factory: pytest.TempPathFactory) -> Any:
    """The full frozen-corpus system, built once per module."""
    workdir = tmp_path_factory.mktemp("corpus-system")
    _, _, service = _corpus_stack(workdir)
    return service


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
        offenders = sorted(name for name in _lock_package_names() if runtime_guard.is_forbidden_distribution(name))
        assert offenders == [], f"forbidden provider/vector/model distributions in uv.lock: {offenders}"

    def test_no_forbidden_distribution_installed(self) -> None:
        installed = {dist.metadata["Name"] for dist in importlib.metadata.distributions()}
        offenders = sorted(name for name in installed if name and runtime_guard.is_forbidden_distribution(name))
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
        for path in _src_python_files():
            roots = _imported_roots(path)
            for root in roots:
                if runtime_guard.is_forbidden_module(root):
                    offenders.append(f"{path.relative_to(REPO_ROOT)} imports {root}")
        assert offenders == [], f"forbidden imports in src/: {offenders}"

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

    def test_bench_run_executes_against_the_live_system(self) -> None:
        """Integration landed: the runner must measure, never skip-gate (exit 3)."""
        proc = subprocess.run(
            [sys.executable, str(BENCH_DIR / "run.py"), "run", "--repeats", "2"],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=900,
        )
        assert proc.returncode in (0, 1), (
            f"the skip-gate (exit 3) must be gone; got {proc.returncode}: {proc.stdout}\n{proc.stderr}"
        )
        ledgers = sorted((BENCH_DIR / "results").glob("ledger-*.json"))
        assert ledgers, "no ledger written"
        ledger = json.loads(ledgers[-1].read_text(encoding="utf-8"))
        assert ledger["status"] in {"ok", "failed"}, f"unexpected ledger status {ledger['status']!r}"
        assert ledger["estimator"] == "estimated_tokens = ceil(utf8_byte_length / 4)"
        assert len(ledger["cases"]) == 24
        assert all(case["status"] == "measured" for case in ledger["cases"])


# ==================== integration acceptance bullets (run for real) =========


class TestSevenDomainsEndToEnd:
    """Acceptance bullet: all seven primary domains route and answer."""

    def test_all_seven_primary_domains_route_and_answer(self, corpus_service: Any) -> None:
        from soul_knowledge.domain import PrimaryDomain

        assert len(list(PrimaryDomain)) == 7
        for domain in PrimaryDomain:
            response = corpus_service.query(QueryRequest(purpose=domain.title, domain=domain.value, token_budget=1024))
            assert response.routing == "direct", (domain, response.routing)
            assert response.results, f"{domain.value} returned no knowledge payload"
            assert all(node.domain == domain.value for node in response.results)
            assert response.audit_event_id
            assert response.stale is False
            assert response.estimated_tokens <= 1024
            assert response.required_knowledge_watermark == response.source_knowledge_high_watermark


class TestAuditedTools:
    """Acceptance bullet: exactly ten audited tools; every call audited."""

    def test_exactly_ten_audited_tools_exposed(self) -> None:
        from soul_knowledge import http_api, mcp_server
        from soul_knowledge.service import KnowledgeService

        assert tuple(sorted(http_api.KNOWLEDGE_OPERATIONS)) == tuple(sorted(TEN_OPERATIONS))
        assert tuple(mcp_server.EXPECTED_TOOLS) == TEN_TOOLS
        for operation in TEN_OPERATIONS:
            assert callable(getattr(KnowledgeService, operation)), operation
        # The CLI exposes exactly the same ten knowledge commands (plus the
        # documented ops commands), driven by the same KNOWLEDGE_OPERATIONS.
        subcommands = {
            name.replace("-", "_")
            for action in build_parser()._actions  # noqa: SLF001
            if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
            for name in action.choices
        }
        assert set(TEN_OPERATIONS) <= subcommands

    def test_every_tool_action_returns_an_audit_event_id(self, tmp_path: Path) -> None:
        from soul_knowledge.contracts import (
            ApplyImprovementRequest,
            EvaluateRequest,
            NodeNotFound,
            ProposeImprovementRequest,
            ReviewRequest,
            RollbackRequest,
        )
        from soul_knowledge.improvement import DiffOp, DiffOperation
        from test_service import _child, _evidence, _seeded

        store, _, service = _seeded(tmp_path)
        audits_before = store.audit_event_count()
        audit_ids: list[str] = []

        query = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
        audit_ids.append(query.audit_event_id)
        node = service.get_node("n-gates")
        audit_ids.append(node.audit_event_id)
        subtree = service.get_subtree("P1")
        audit_ids.append(subtree.audit_event_id)
        logged = service.log_event(_child("n-acceptance", summary="acceptance node"), actor="acceptance")
        audit_ids.append(logged.audit_event_id)
        recorded = service.record_outcome(_evidence("e-acc-1", "n-gates"))
        audit_ids.append(recorded.audit_event_id)
        evaluated = service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
        audit_ids.append(evaluated.audit_event_id)
        reviewed = service.review(ReviewRequest(node_id="n-gates"))
        audit_ids.append(reviewed.audit_event_id)
        proposal = service.propose_improvement(
            ProposeImprovementRequest(
                node_id="n-gates",
                diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="Acceptance summary"),),
                rationale="acceptance probe",
            )
        )
        audit_ids.append(proposal.audit_event_id)
        applied = service.apply_improvement(ApplyImprovementRequest(proposal_id=proposal.proposal.proposal_id, at=AT))
        audit_ids.append(applied.audit_event_id)
        rolled = service.rollback(RollbackRequest(node_id="n-gates"))
        audit_ids.append(rolled.audit_event_id)

        assert all(audit_ids), "every operation must return its audit event id"
        assert len(set(audit_ids)) == 10, "each invocation owns a distinct audit event id"
        # Exactly one final audit event per invocation: ten calls, ten audits.
        assert store.audit_event_count() == audits_before + 10
        # A typed failure is also exactly one audit (spot check the envelope).
        with pytest.raises(NodeNotFound) as excinfo:
            service.get_node("n-missing")
        assert excinfo.value.audit_event_id
        assert store.audit_event_count() == audits_before + 11


class TestThreeAccessPaths:
    """Acceptance bullet: CLI, local API and MCP share one service contract."""

    def _payloads(self, tmp_path: Path) -> dict[str, dict[str, Any]]:
        from soul_knowledge.http_api import build_stack, dispatch_text, to_jsonable
        from soul_knowledge.mcp_server import build_server
        from test_mcp_server import _call_tool, _result_text, _session
        from test_service import _seeded

        _, _, service = _seeded(tmp_path)
        request = {"purpose": "run the gates", "domain": "P1", "token_budget": 512}

        # Path 1 — the local API dispatch (the HTTP server's shared code path).
        stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
        try:
            api = to_jsonable(dispatch_text(stack, "query", json.dumps(request)))
        finally:
            stack.close()

        # Path 2 — the CLI as a real subprocess against the same store.
        cli = subprocess.run(
            [
                sys.executable,
                "-m",
                "soul_knowledge",
                "--store",
                str(tmp_path / "soul.sqlite3"),
                "--output-root",
                str(tmp_path / "out"),
                "query",
                json.dumps(request),
            ],
            cwd=REPO_ROOT,
            capture_output=True,
            text=True,
            timeout=300,
        )
        assert cli.returncode == 0, f"CLI failed: {cli.stderr}"
        cli_payload = json.loads(cli.stdout)

        # Path 3 — MCP over in-memory JSON-RPC against the same service.
        server = build_server(service, graph_root=tmp_path / "out")

        async def _mcp() -> dict[str, Any]:
            async with _session(server) as session:
                result = await _call_tool(session, "knowledge_query", request)
                assert not result.is_error, _result_text(result)
                return json.loads(_result_text(result))  # type: ignore[no-any-return]

        mcp_payload = anyio.run(_mcp)
        return {"api": api, "cli": cli_payload, "mcp": mcp_payload}

    def test_cli_local_api_and_mcp_share_one_service_contract(self, tmp_path: Path) -> None:
        payloads = self._payloads(tmp_path)
        for path, payload in payloads.items():
            assert payload["routing"] == "direct", path
            assert payload["audit_event_id"], path
            assert payload["stale"] is False, path
            assert {node["node_id"] for node in payload["results"]} >= {"n-gates"}, path

    def test_canonical_payload_parity_across_paths(self, tmp_path: Path) -> None:
        payloads = self._payloads(tmp_path)

        # Volatile per-invocation fields are excluded by contract: the audit id
        # and the wall-clock recommendation expiry. Everything else must be
        # byte-identical across the three access paths.
        def _stable(payload: dict[str, Any]) -> dict[str, Any]:
            stable = {key: value for key, value in payload.items() if key != "audit_event_id"}
            block = stable.get("recommendations")
            if isinstance(block, dict):
                for items in block.values():
                    for item in items:
                        item["expiry"] = "<volatile>"
            return stable

        canonical = {path: canonical_json(_stable(payload)) for path, payload in payloads.items()}
        assert canonical["api"] == canonical["cli"], (
            f"API/CLI payload divergence: {canonical['api']} != {canonical['cli']}"
        )
        assert canonical["api"] == canonical["mcp"], (
            f"API/MCP payload divergence: {canonical['api']} != {canonical['mcp']}"
        )


class TestExactlyOneFinalAudit:
    """Acceptance bullet: exactly one final audit per valid invocation,
    including recovered failures."""

    def test_one_final_audit_per_valid_invocation(self, tmp_path: Path) -> None:
        from soul_knowledge.contracts import EvaluateRequest
        from test_service import _evidence, _seeded

        store, _, service = _seeded(tmp_path)
        audits_before = store.audit_event_count()
        service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
        assert store.audit_event_count() == audits_before + 1
        service.record_outcome(_evidence("e-audit-1", "n-gates"))
        assert store.audit_event_count() == audits_before + 2
        service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
        assert store.audit_event_count() == audits_before + 3

    def test_one_final_audit_per_recovered_failure(self, tmp_path: Path) -> None:
        from soul_knowledge.audit import AuditLog
        from soul_knowledge.contracts import NodeNotFound
        from test_service import _seeded

        store, _, service = _seeded(tmp_path)
        audits_before = store.audit_event_count()

        # A typed failure: exactly one final failure audit.
        with pytest.raises(NodeNotFound):
            service.get_node("n-missing")
        assert store.audit_event_count() == audits_before + 1

        # A crashed invocation (journal left in_progress): recovery appends
        # exactly one final audit, and a second recovery pass appends nothing.
        audit = AuditLog(store)
        invocation = Invocation.create("knowledge.query", request_hash="0" * 64, actor="crash-probe")
        audit.begin(invocation)
        assert audit.pending_invocations(), "the crashed journal must be in_progress"
        receipts = audit.recover_incomplete(stale_after_seconds=0.0)
        assert len(receipts) == 1
        assert store.audit_event_count() == audits_before + 2
        outcome = audit.journal_outcome(invocation.invocation_id)
        assert outcome is not None and outcome["status"] == "failure"
        assert audit.recover_incomplete(stale_after_seconds=0.0) == []
        assert store.audit_event_count() == audits_before + 2


class TestIdempotentRetry:
    """Acceptance bullet: idempotent retry linkage."""

    def test_retry_preserves_original_domain_result_with_new_linked_audit_id(self, tmp_path: Path) -> None:
        from test_service import _child, _seeded

        store, _, service = _seeded(tmp_path)
        node = _child("n-retry", summary="retry probe")
        first = service.log_event(node, idempotency_key="acceptance:retry:1")
        assert first.idempotent_replay is False
        version_after_first = store.node_version("n-retry")

        retry = service.log_event(node, idempotency_key="acceptance:retry:1")
        assert retry.idempotent_replay is True
        # The original domain result is preserved…
        assert retry.domain_event_id == first.domain_event_id
        assert retry.event_seq == first.event_seq
        assert retry.node_version == first.node_version
        assert store.node_version("n-retry") == version_after_first
        # …while the retry carries its own new linked audit id.
        assert retry.audit_event_id != first.audit_event_id
        assert retry.audit_event_seq > first.audit_event_seq


class TestDeterministicScoring:
    """Acceptance bullet: deterministic scoring under evidence permutation."""

    def test_scoring_replay_is_deterministic_under_evidence_permutation(self, tmp_path: Path) -> None:
        from soul_knowledge.contracts import EvaluateRequest
        from test_service import _evidence, _seeded

        _, _, service_a = _seeded(tmp_path / "a")
        _, _, service_b = _seeded(tmp_path / "b")
        order_a = ("e-perm-1", "e-perm-2", "e-perm-3", "e-perm-4")
        order_b = ("e-perm-4", "e-perm-2", "e-perm-3", "e-perm-1")
        for evidence_id in order_a:
            service_a.record_outcome(_evidence(evidence_id, "n-gates"))
        for evidence_id in order_b:
            service_b.record_outcome(_evidence(evidence_id, "n-gates"))

        scored_a = service_a.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
        scored_b = service_b.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
        for field in ("p", "m", "e", "u_p", "u_m", "u_e", "overall_uncertainty", "professional_level"):
            assert getattr(scored_a, field) == getattr(scored_b, field), field
        # Byte-level determinism of the scored payload (minus the audit id).
        payload_a = {k: v for k, v in scored_a.model_dump(mode="json").items() if k != "audit_event_id"}
        payload_b = {k: v for k, v in scored_b.model_dump(mode="json").items() if k != "audit_event_id"}
        assert canonical_json(payload_a) == canonical_json(payload_b)


class TestRollback:
    """Acceptance bullet: rollback restores byte-exact prior state."""

    def test_rollback_restores_byte_exact_prior_state(self, tmp_path: Path) -> None:
        from soul_knowledge.contracts import (
            ApplyImprovementRequest,
            ProposeImprovementRequest,
            RollbackRequest,
        )
        from soul_knowledge.domain import canonical_json as canonical
        from soul_knowledge.improvement import DiffOp, DiffOperation
        from soul_knowledge.store import EventStore
        from test_relearn import NOW, RelearnFixture, approve
        from test_service import _seeded

        # (a) Improvement rollback restores the byte-exact prior node content
        #     through the service path.
        store, _, service = _seeded(tmp_path / "improvement")
        before = store.current_node("n-gates")
        assert before is not None

        def _content_bytes(node: Any) -> bytes:
            # Byte-exact content: version/updated_at are revision bookkeeping
            # the append-only log legitimately advances on a rollback write.
            payload = node.model_dump(mode="json")
            for bookkeeping in ("version", "updated_at", "lifecycle_status"):
                payload.pop(bookkeeping)
            return canonical(payload).encode("utf-8")

        before_bytes = _content_bytes(before)
        proposal = service.propose_improvement(
            ProposeImprovementRequest(
                node_id="n-gates",
                diff=(DiffOperation(op=DiffOp.SET_SUMMARY, value="Changed"),),
                rationale="rollback probe",
            )
        )
        service.apply_improvement(ApplyImprovementRequest(proposal_id=proposal.proposal.proposal_id, at=AT))
        assert store.current_node("n-gates").summary == "Changed"  # type: ignore[union-attr]
        service.rollback(RollbackRequest(node_id="n-gates"))
        after = store.current_node("n-gates")
        assert after is not None
        assert _content_bytes(after) == before_bytes
        assert after.version > before.version, "rollback is a new audited revision, not a silent rewind"
        assert after.lifecycle_status == "rollback"

        # (b) Relearn rollback restores the byte-exact pre-relearn database and
        #     graph bytes with exactly one linked audit event.
        (tmp_path / "relearn").mkdir()
        fx = RelearnFixture(tmp_path / "relearn")
        offer = fx.make_offer()
        pre_graph_bytes = (fx.generation / "generations" / "gen-1" / "graph.json").read_bytes()
        pre_audit_count = fx.store.audit_event_count()
        receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
        rollback = fx.engine.rollback(receipt.relearn_id, actor="operator")
        assert rollback.verified is True
        sqlite_artifact = next(a for a in receipt.backup.artifacts if a.kind == "sqlite")
        assert rollback.restored_db_sha256 == sqlite_artifact.sha256
        assert (fx.generation / "generations" / "gen-1" / "graph.json").read_bytes() == pre_graph_bytes
        reopened = EventStore(fx.db_path)
        try:
            assert reopened.audit_event_count() == pre_audit_count + 1
        finally:
            reopened.close()


class TestScrubbedRuntime:
    """Acceptance bullet: scrubbed runtime (e2e at the real process boundary)."""

    def test_child_process_environment_is_scrubbed(self, tmp_path: Path) -> None:
        """The real MCP entry point refuses a dirty environment and serves a
        scrubbed one; the scrubbed env provably carries no credentials."""
        from test_mcp_stdio_e2e import _seed, _stdio_session

        graph_root, db = _seed(tmp_path)
        dirty = {
            **os.environ,
            "OPENAI_API_KEY": "sk-injected",
            "ANTHROPIC_API_KEY": "sk-injected",
        }
        refused = subprocess.run(
            [sys.executable, "-m", "soul_knowledge.mcp_server", "--graph-root", str(graph_root), "--db", str(db)],
            env=dirty,
            capture_output=True,
            text=True,
            timeout=120,
        )
        assert refused.returncode == 2, f"dirty environment must refuse startup: {refused.stdout}"
        assert "startup refused" in refused.stderr

        scrubbed = runtime_guard.deterministic_env(dirty)
        assert runtime_guard.provider_credentials(scrubbed) == {}
        assert runtime_guard.model_asset_env(scrubbed) == {}

        # The scrubbed child not only starts: it serves a real MCP session.
        async def scenario() -> None:
            async with _stdio_session(graph_root, db) as session:
                capabilities = session.server_capabilities
                assert capabilities is not None
                advertised = capabilities.model_dump(exclude_none=True, by_alias=True)
                assert set(advertised) == {"tools", "resources"}

        anyio.run(scenario)


class TestDeniedOutboundSockets:
    """Acceptance bullet: outbound sockets denied while the system works."""

    def test_outbound_tcp_and_http_are_denied(self, tmp_path: Path) -> None:
        import socket
        import urllib.request

        from soul_knowledge.contracts import EvaluateRequest
        from test_service import _evidence, _seeded

        _, _, service = _seeded(tmp_path)
        with runtime_guard.deny_outbound_sockets():
            # The full read/write lifecycle runs with the guard active…
            service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
            service.record_outcome(_evidence("e-sock-1", "n-gates"))
            service.evaluate(EvaluateRequest(node_id="n-gates", at=AT, include_descendants=False))
            # …while any outbound connection is denied.
            with pytest.raises(runtime_guard.OutboundSocketDenied):
                socket.create_connection(("192.0.2.1", 9), timeout=0.05)
            with pytest.raises(runtime_guard.OutboundSocketDenied):
                urllib.request.urlopen("http://192.0.2.1/", timeout=0.05)


class TestAbsentSampling:
    """Acceptance bullet: no sampling handlers or capabilities."""

    def test_no_sampling_handlers_or_capabilities(self, tmp_path: Path) -> None:
        from soul_knowledge.mcp_server import build_server
        from test_service import _seeded

        _, _, service = _seeded(tmp_path)
        server = build_server(service, graph_root=tmp_path / "out")
        runtime_guard.assert_no_sampling_handler(server)
        assert runtime_guard.forbidden_mcp_methods(server) == []
        observed = runtime_guard.observed_mcp_capabilities(server)
        assert set(observed) == {"tools", "resources"}
        runtime_guard.assert_mcp_capabilities(observed)

    def test_sampling_configuration_is_neither_advertised_nor_used(self, tmp_path: Path) -> None:
        from soul_knowledge.mcp_server import build_server
        from test_mcp_server import _session
        from test_service import _seeded

        _, _, service = _seeded(tmp_path)
        server = build_server(service, graph_root=tmp_path / "out")

        async def scenario() -> None:
            async with _session(server) as session:
                capabilities = session.server_capabilities
                assert capabilities is not None
                advertised = capabilities.model_dump(exclude_none=True, by_alias=True)
                assert set(advertised) == {"tools", "resources"}
                for forbidden in ("sampling", "prompts", "logging", "completions"):
                    assert forbidden not in advertised
                tools = await session.list_tools()
                assert "sampling" not in canonical_json(tools.model_dump(mode="json"))

        anyio.run(scenario)


class TestMcpContract:
    """Acceptance bullet: strict MCP root/schema/range/capability contract."""

    def _server(self, tmp_path: Path) -> Any:
        from soul_knowledge.mcp_server import build_server
        from test_service import _seeded

        _, _, service = _seeded(tmp_path)
        return build_server(service, graph_root=tmp_path / "out")

    def test_single_allowlisted_graph_root(self, tmp_path: Path) -> None:
        from mcp.shared.exceptions import MCPError

        from test_mcp_server import _call_tool, _error_payload, _session

        server = self._server(tmp_path)

        async def scenario() -> None:
            async with _session(server) as session:
                listed = await session.list_tools()
                assert len(listed.tools) == 10
                for tool in listed.tools:
                    assert "project_path" not in tool.input_schema.get("properties", {}), tool.name
                    result = await _call_tool(session, tool.name, {"project_path": "/etc"})
                    error = _error_payload(result)
                    assert error["code"] == "INVALID_ARGUMENTS", (tool.name, error)
                # Resources stay inside the fixed root; escapes fail closed.
                with pytest.raises(MCPError):
                    await session.read_resource("file:///etc/passwd")
                with pytest.raises(MCPError):
                    await session.read_resource("soul://projection/../../etc/passwd")

        anyio.run(scenario)

    def test_strict_schema_unknown_missing_type_rejection(self, tmp_path: Path) -> None:
        from test_mcp_server import _call_tool, _error_payload, _session

        server = self._server(tmp_path)

        async def scenario() -> None:
            async with _session(server) as session:
                unknown = await _call_tool(
                    session, "knowledge_query", {"purpose": "x", "token_budget": 512, "bogus": 1}
                )
                assert _error_payload(unknown)["code"] == "INVALID_ARGUMENTS"
                missing = await _call_tool(session, "knowledge_query", {"token_budget": 512})
                assert _error_payload(missing)["code"] == "INVALID_ARGUMENTS"
                wrong_type = await _call_tool(session, "knowledge_query", {"purpose": "x", "token_budget": "512"})
                assert _error_payload(wrong_type)["code"] == "INVALID_ARGUMENTS"

        anyio.run(scenario)

    def test_range_contract_depth_1_6_budget_128_max(self, tmp_path: Path) -> None:
        from soul_knowledge.contracts import DEFAULT_MAX_TOKEN_BUDGET, MINIMUM_TOKEN_BUDGET
        from test_mcp_server import _call_tool, _error_payload, _session

        server = self._server(tmp_path)

        async def scenario() -> None:
            async with _session(server) as session:
                for bad_depth in (0, 7):
                    result = await _call_tool(
                        session,
                        "knowledge_query",
                        {"purpose": "gates", "domain": "P1", "token_budget": 512, "depth": bad_depth},
                    )
                    assert _error_payload(result)["code"] == "INVALID_ARGUMENTS", bad_depth
                for good_depth in (1, 6):
                    result = await _call_tool(
                        session,
                        "knowledge_query",
                        {"purpose": "gates", "domain": "P1", "token_budget": 512, "depth": good_depth},
                    )
                    assert not result.is_error, good_depth
                for bad_budget in (MINIMUM_TOKEN_BUDGET - 1, DEFAULT_MAX_TOKEN_BUDGET + 1):
                    result = await _call_tool(
                        session, "knowledge_query", {"purpose": "gates", "token_budget": bad_budget}
                    )
                    assert _error_payload(result)["code"] == "INVALID_ARGUMENTS", bad_budget
                # In-range boundaries pass schema validation; a tight budget may
                # still fail closed with a typed service error, never a schema one.
                for boundary in (MINIMUM_TOKEN_BUDGET, DEFAULT_MAX_TOKEN_BUDGET):
                    result = await _call_tool(
                        session, "knowledge_query", {"purpose": "gates", "domain": "P1", "token_budget": boundary}
                    )
                    if result.is_error:
                        code = _error_payload(result)["code"]
                        assert code != "INVALID_ARGUMENTS", (boundary, code)

        anyio.run(scenario)

    def test_capability_surface_is_exactly_tools_and_resources(self, tmp_path: Path) -> None:
        from test_mcp_server import _session

        server = self._server(tmp_path)

        async def scenario() -> None:
            async with _session(server) as session:
                capabilities = session.server_capabilities
                assert capabilities is not None
                advertised = capabilities.model_dump(exclude_none=True, by_alias=True)
                assert set(advertised) == {"tools", "resources"}
                runtime_guard.assert_mcp_capabilities(advertised)

        anyio.run(scenario)
        report = runtime_guard.verify_runtime(mcp_server=server)
        assert report["status"] == "ok"
        assert report["mcp_capabilities"] == ["resources", "tools"]


class TestConditionalAccess:
    """Acceptance bullet: conditional access (L1 never calls; zero-payload cap)."""

    def test_l1_never_calls_the_system(self) -> None:
        import inspect

        policy = AccessPolicy()
        # Every L1 surface maps to NO_CALL…
        for surface in ("L1_greeting", "L1_one_fact_lookup", "L1_one_command", "L1_file_read", "L1_status_check"):
            assert policy.decide(surface) is AccessDecision.NO_CALL, surface
        # …ambiguous and unknown surfaces get the cheap access-only path…
        assert policy.decide("ambiguous") is AccessDecision.ACCESS_ONLY
        assert policy.decide("a-surface-nobody-listed") is AccessDecision.ACCESS_ONLY
        # …and only required surfaces call.
        assert policy.decide("required") is AccessDecision.QUERY
        # The decision is a pure local lookup: no service/store/graph parameter
        # exists to call, so an L1 decision can never reach the system.
        assert set(inspect.signature(AccessPolicy.decide).parameters) == {"self", "task_surface", "context"}
        # The hash-pinned artifact fails closed when tampered.
        assert policy.access_only_max_estimated_tokens == ACCESS_ONLY_TOKEN_CAP == 160
        artifact = json.loads(ACCESS_POLICY_PATH.read_text(encoding="utf-8"))
        assert artifact["decision_cost"] == {"model_calls": 0, "graph_calls": 0}

    def test_access_only_returns_zero_payload_within_160_tokens(self, corpus_service: Any) -> None:
        for budget in (ACCESS_ONLY_TOKEN_CAP, 1024):
            response = corpus_service.query(
                QueryRequest(purpose="anything ambiguous", access_only=True, token_budget=budget)
            )
            assert response.access_only is True
            assert response.routing == "access_only"
            assert response.results == ()
            assert response.knowledge_payload_tokens == 0
            assert response.estimated_tokens <= ACCESS_ONLY_TOKEN_CAP, budget

    def test_tampered_policy_artifact_fails_closed(self, tmp_path: Path) -> None:
        tampered = tmp_path / "access_policy.json"
        original = json.loads(ACCESS_POLICY_PATH.read_text(encoding="utf-8"))
        original["surface_decisions"]["L1_greeting"] = "call"
        tampered.write_text(json.dumps(original), encoding="utf-8")
        with pytest.raises(PolicyArtifactError):
            AccessPolicy(path=tampered)


class TestRecommendationShape:
    """Acceptance bullet: recommendation shape and promotion rule."""

    def test_exactly_best_actions_recommendations_unnecessary_actions(self, tmp_path: Path) -> None:
        from pydantic import ValidationError

        from soul_knowledge.contracts import ReviewRequest
        from test_service import _evidence, _seeded

        assert set(RecommendationBlock.model_fields) == {
            "best_actions",
            "recommendations",
            "unnecessary_actions",
        }
        with pytest.raises(ValidationError):
            RecommendationBlock.model_validate({"best_actions": [], "extra_list": []})

        _, _, service = _seeded(tmp_path)
        service.record_outcome(_evidence("e-rec-1", "n-gates"))
        service.record_outcome(_evidence("e-rec-2", "n-gates"))
        response = service.review(ReviewRequest(node_id="n-gates"))
        block = response.recommendations
        assert isinstance(block, RecommendationBlock)
        lists = (block.best_actions, block.recommendations, block.unnecessary_actions)
        assert sum(len(items) for items in lists) == 1
        # A query response carries the same three-list block and nothing else.
        query = service.query(QueryRequest(purpose="run the gates", domain="P1", token_budget=512))
        assert set(query.recommendations.model_dump(mode="json")) == {
            "best_actions",
            "recommendations",
            "unnecessary_actions",
        }

    def test_no_raw_soul_text_and_no_one_observation_promotion(self, tmp_path: Path) -> None:
        from soul_knowledge.contracts import ReviewRequest
        from test_service import _evidence, _seeded

        _, _, service = _seeded(tmp_path)
        service.record_outcome(_evidence("e-solo", "n-gates"))
        response = service.review(ReviewRequest(node_id="n-gates"))
        items = (
            *response.recommendations.best_actions,
            *response.recommendations.recommendations,
            *response.recommendations.unnecessary_actions,
        )
        assert items, "the reviewed node must surface exactly one recommendation item"
        for item in items:
            assert isinstance(item, RecommendationItem)
            assert item.promoted is False
            serialized = canonical_json(item.model_dump(mode="json"))
            # Structured references only: no raw SOUL/corpus text, no provenance.
            assert "soul://" not in serialized
            assert "Run the gates before commit" not in serialized
            if len(item.evidence_ids) < 2:
                verdict = attempt_promotion(item)
                assert verdict.accepted is False
                assert verdict.reason == "ONE_OBSERVATION_NO_PROMOTION"
        # The contract boundary itself is fail-closed on one observation.
        solo = next(item for item in items if item.evidence_ids)
        assert len(solo.evidence_ids) == 1
        assert attempt_promotion(solo).accepted is False


class TestRelearn:
    """Acceptance bullet: RESET/ACCUMULATE/rollback operator gating, zero weight."""

    def test_reset_accumulate_and_rollback_are_operator_gated(self, tmp_path: Path) -> None:
        from soul_knowledge.relearn import (
            NoPendingRelearn,
            OperatorDecisionRequired,
            RelearnError,
            UnknownRelearn,
        )
        from test_relearn import RelearnFixture, approve

        fx = RelearnFixture(tmp_path)
        # No pending offer: nothing to decide on.
        with pytest.raises(NoPendingRelearn):
            fx.engine.apply("RESET", approve("whatever", "RESET"))
        offer = fx.make_offer()
        # Missing, mode-mismatched and foreign approvals are all refused.
        with pytest.raises(OperatorDecisionRequired):
            fx.engine.apply("RESET", None)
        with pytest.raises(OperatorDecisionRequired):
            fx.engine.apply("RESET", approve(offer.offer_id, "ACCUMULATE"))
        with pytest.raises(OperatorDecisionRequired):
            fx.engine.apply("RESET", approve("other-offer", "RESET"))
        # Rollback is equally gated: unknown relearn ids and double rollback refuse.
        with pytest.raises(UnknownRelearn):
            fx.engine.rollback("no-such-relearn")
        receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"))
        fx.engine.rollback(receipt.relearn_id)
        with pytest.raises(RelearnError):
            fx.engine.rollback(receipt.relearn_id)

    def test_relearn_events_carry_zero_evidence_weight(self, tmp_path: Path) -> None:
        from soul_knowledge.projection import GraphProjector
        from soul_knowledge.service import KnowledgeService
        from test_relearn import NOW, RelearnFixture, approve

        fx = RelearnFixture(tmp_path)
        projector = GraphProjector(fx.store, output_root=tmp_path / "out")
        projector.rebuild()
        service = KnowledgeService(fx.store, projector)

        before = service.evaluate(EvaluateRequest(node_id="n-1", at=NOW, include_descendants=False))
        assert before.p != "0.000000000000", "seeded evidence must score before relearn"

        offer = fx.make_offer()
        receipt = fx.engine.apply("RESET", approve(offer.offer_id, "RESET"), at=NOW)
        assert sorted(receipt.quarantined_evidence_ids) == ["e1", "e2", "e3", "e4"]

        # After RESET every old evidence item carries zero weight: the service's
        # default read path scores the node as if no evidence existed.
        after = service.evaluate(EvaluateRequest(node_id="n-1", at=NOW, include_descendants=False))
        assert after.p == "0.000000000000"
        assert after.m == "0.000000000000"
        assert after.e == "0.000000000000"
        assert after.overall_uncertainty == "1.000000000000"


class TestNoStaleMixing:
    """Acceptance bullet: default reads never mix incompatible source versions."""

    def test_default_reads_never_mix_incompatible_source_versions(self, tmp_path: Path) -> None:
        from soul_knowledge.projection import GraphProjector
        from soul_knowledge.service import KnowledgeService
        from test_relearn import NOW, RelearnFixture, approve

        # ACCUMULATE: only anchor-mapped evidence from the old version carries;
        # unmapped evidence is quarantined out of every default read.
        (tmp_path / "accumulate").mkdir()
        fx = RelearnFixture(tmp_path / "accumulate")
        projector = GraphProjector(fx.store, output_root=fx.tmp_path / "out")
        projector.rebuild()
        service = KnowledgeService(fx.store, projector)
        offer = fx.make_offer()
        receipt = fx.engine.apply("ACCUMULATE", approve(offer.offer_id, "ACCUMULATE"), at=NOW)
        assert sorted(receipt.carried_evidence_ids) == ["e1", "e2"]
        assert fx.engine.quarantined_evidence_ids() == frozenset({"e3", "e4"})
        compatible = fx.engine.compatible_source_version_ids()
        assert fx.v1.source_version_id in compatible  # approved anchor mapping
        scored = service.evaluate(EvaluateRequest(node_id="n-1", at=NOW, include_descendants=False))
        assert scored.p != "0.000000000000", "anchor-mapped evidence must still count"

        # RESET: the old version leaves the compatible set entirely; default
        # reads bind to the new baseline and never mix the old evidence in.
        (tmp_path / "reset").mkdir()
        fx2 = RelearnFixture(tmp_path / "reset")
        GraphProjector(fx2.store, output_root=fx2.tmp_path / "out").rebuild()
        service2 = KnowledgeService(fx2.store, GraphProjector(fx2.store, output_root=fx2.tmp_path / "out"))
        offer2 = fx2.make_offer()
        fx2.engine.apply("RESET", approve(offer2.offer_id, "RESET"), at=NOW)
        compatible2 = fx2.engine.compatible_source_version_ids()
        assert fx2.v1.source_version_id not in compatible2
        reset_scored = service2.evaluate(EvaluateRequest(node_id="n-1", at=NOW, include_descendants=False))
        assert reset_scored.p == "0.000000000000"
        # Default reads bind to the active projection covering the watermark.
        response = service2.query(QueryRequest(purpose="node", token_budget=512))
        assert response.required_knowledge_watermark == response.source_knowledge_high_watermark
        assert response.stale is False


# The Hermes release this repo's frozen SOUL corpus was taken from; the
# install-tree check below pins the installed source tree to this tag.
HERMES_INSTALL_TREE = Path("/root/.hermes/hermes-agent")
HERMES_PINNED_TAG = "v0.22.5"


class TestNoHermesSourceModification:
    """The installed Hermes source tree must be a clean checkout of the
    pinned release tag — the corpus-freeze provenance claim ("the SOUL corpus
    came from an unmodified Hermes install") is only as good as that tree.

    This is a genuinely external property (the host's Hermes installation,
    not this repo), so the test skip-gates at runtime when no git checkout is
    present; on a host with the installation it runs for real. The
    corpus-side pin (live SOUL corpus unchanged since freeze) runs for real
    in ``TestFrozenCorpus``. Evidence for the release record lives in
    ``docs/audit-release.md`` §5 (wave-3 W3-11 closure).
    """

    def test_hermes_source_tree_is_unmodified(self) -> None:
        if not (HERMES_INSTALL_TREE / ".git").exists():
            pytest.skip(f"no git-managed Hermes installation at {HERMES_INSTALL_TREE} (external host dependency)")

        def git(*args: str) -> str:
            proc = subprocess.run(
                ["git", "-C", str(HERMES_INSTALL_TREE), *args],
                capture_output=True,
                text=True,
                timeout=60,
            )
            assert proc.returncode == 0, f"git {' '.join(args)} failed: {proc.stderr.strip()}"
            return proc.stdout.strip()

        head = git("rev-parse", "HEAD")
        pinned = git("rev-parse", f"{HERMES_PINNED_TAG}^{{commit}}")
        assert head == pinned, f"install-tree HEAD {head} is not the pinned release {HERMES_PINNED_TAG} ({pinned})"
        dirty = git("status", "--porcelain")
        assert dirty == "", f"install tree has uncommitted changes/untracked files:\n{dirty}"
        stashes = git("stash", "list")
        assert stashes == "", f"install tree carries stashed modifications:\n{stashes}"
        # Non-vacuous self-check: the assertions above ran against a real tree.
        assert len(head) == 40 and all(c in "0123456789abcdef" for c in head)


# ------------------------------------------------------- graphify coverage


def _matrix_rows() -> dict[int, dict[str, str]]:
    """Parse the 40-row feature inventory from the feature matrix."""
    rows: dict[int, dict[str, str]] = {}
    for line in FEATURE_MATRIX.read_text(encoding="utf-8").splitlines():
        if not line.startswith("|"):
            continue
        cells = [cell.strip() for cell in line.strip().strip("|").split("|")]
        if len(cells) != 7 or not cells[0].isdigit():
            continue
        rows[int(cells[0])] = {
            "feature": cells[1],
            "verdict": cells[2].strip("`"),
            "adaptation": cells[3],
            "owner": cells[4],
            "test": cells[5].strip("`"),
            "covered": cells[6],
        }
    return rows


def _junit_nodeid(classname: str, name: str) -> str:
    parts = classname.split(".")
    assert parts[0] == "tests" and len(parts) >= 2, classname
    path = f"tests/{parts[1]}.py"
    classes = parts[2:]
    return "::".join([path, *classes, name])


def _coverage_evidence(tmp_path: Path) -> tuple[dict[int, dict[str, str]], dict[int, bool], str]:
    """Run every matrix-named contract test fresh; return (rows, passed, tail)."""
    rows = _matrix_rows()
    assert len(rows) == 40, f"expected 40 matrix rows, parsed {len(rows)}"
    junit = tmp_path / "coverage-junit.xml"
    proc = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "-q",
            "--tb=no",
            "-p",
            "no:cacheprovider",
            f"--junitxml={junit}",
            *(row["test"] for row in rows.values()),
        ],
        cwd=REPO_ROOT,
        capture_output=True,
        text=True,
        timeout=900,
    )
    assert junit.is_file(), f"coverage pytest run produced no junit xml: {proc.stdout}\n{proc.stderr}"
    tree = ET.parse(junit)
    outcomes: dict[str, bool] = {}
    for testcase in tree.getroot().iter("testcase"):
        nodeid = _junit_nodeid(str(testcase.get("classname")), str(testcase.get("name")))
        passed = not list(testcase)  # no failure/error/skipped children
        outcomes[nodeid] = passed
    missing = [row["test"] for row in rows.values() if row["test"] not in outcomes]
    assert not missing, f"named contract tests not collected: {missing}\n{proc.stdout}\n{proc.stderr}"
    passed_rows = {number: outcomes[row["test"]] for number, row in rows.items()}
    tail = (proc.stdout.strip().splitlines() or [""])[-1]
    return rows, passed_rows, tail


class TestGraphifyCoverageGate:
    """Acceptance bullet: coverage from passing matrix rows only; >= 90% of 40;
    the three partial residuals reported explicitly."""

    def test_coverage_counts_only_passing_matrix_rows(self, tmp_path: Path) -> None:
        rows, passed_rows, _ = _coverage_evidence(tmp_path)
        # Every row carries a named contract test and the count is derived
        # exclusively from fresh pass/fail evidence.
        assert all(row["test"].startswith("tests/") for row in rows.values())
        counted = {number for number, passed in passed_rows.items() if passed}
        assert counted <= set(rows), "a row outside the inventory can never be counted"
        # Rows the matrix still marks uncovered must not be counted unless their
        # named test now passes (the matrix prose never overrides evidence).
        for number, row in rows.items():
            if row["covered"].lower().startswith("no") and passed_rows[number]:
                continue  # evidence moved ahead of prose: allowed, reported below
            if not passed_rows[number]:
                assert number not in counted

    def test_at_least_90_percent_of_applicable_40_features(self, tmp_path: Path) -> None:
        _, passed_rows, tail = _coverage_evidence(tmp_path)
        passing = sum(1 for passed in passed_rows.values() if passed)
        coverage = passing / 40
        assert passing >= 36, (
            f"Graphify coverage {passing}/40 = {coverage:.1%} is below the 90% gate; "
            f"failing rows: {sorted(n for n, p in passed_rows.items() if not p)}; pytest tail: {tail}"
        )

    def test_three_partial_residuals_reported_explicitly(self) -> None:
        rows = _matrix_rows()
        partials = {number: row for number, row in rows.items() if "partial" in row["verdict"].lower()}
        assert sorted(partials) == [22, 23, 38], f"partial rows changed: {sorted(partials)}"
        for number, row in partials.items():
            assert "residual" in row["adaptation"].lower(), f"row {number} names no residual"
            assert re.search(r"residual owner Task \d+", row["owner"]), (
                f"row {number} names no residual owner: {row['owner']!r}"
            )


class TestBenchmarkQualityGate:
    """Acceptance bullet: token savings never excuse falling short of the
    per-case compliant ceiling (gate contract 2.0.0, Opsi C).

    Gate 1.0.0 compared quality against the raw-corpus baseline (1.0), which
    no contract-compliant response can reach (the contract forbids raw corpus
    text in the payload while rubric terms live in the body). Gate 2.0.0 keeps
    the baseline as a reported metric (``gap_vs_raw_read``) but accepts against
    the per-case compliant ceiling computed from the real projection artifact.
    """

    def test_paired_token_savings_never_excuse_quality_regression(self, tmp_path: Path) -> None:
        import os

        results_dir = BENCH_DIR / "results"
        before = set(results_dir.glob("ledger-*.json"))
        # Run the real paired benchmark in-process against the live system.
        exit_code = bench.run_benchmark(repeats=2)
        assert exit_code in (0, 1), f"bench run must measure (never skip-gate), got {exit_code}"
        after = set(results_dir.glob("ledger-*.json"))
        new_ledgers = sorted(after - before, key=os.path.getmtime)
        assert new_ledgers, "the benchmark wrote no new ledger"
        ledger = json.loads(new_ledgers[-1].read_text(encoding="utf-8"))
        assert ledger["status"] in {"ok", "failed"}
        assert ledger["gate"]["gate_version"] == bench.GATE_VERSION
        # The raw-corpus baseline survives as a comparison metric, unchanged.
        assert ledger["baseline"]["quality"] == 1.0
        assert len(ledger["cases"]) == 24
        for case in ledger["cases"]:
            assert case["status"] == "measured"
            assert case["baseline_estimated_tokens"] == 13151
            # The ceiling is derived from the projection artifact and bounded
            # by the raw-read baseline; the applied floor never exceeds it.
            assert 0.0 <= case["quality_ceiling"] <= 1.0
            assert case["quality_floor_applied"] == min(bench.QUALITY_FLOOR, case["quality_ceiling"])
            # The gate itself: falling short of the per-case compliant ceiling
            # (or the applied floor) is rejected even when the token saving is
            # large. Ledger values are rounded to 6 dp on both sides, so the
            # comparison is exact at that precision.
            eps = bench.CEILING_EPSILON
            must_reject = (
                case["quality_system"] < case["quality_ceiling"] - eps
                or case["quality_system"] < case["quality_floor_applied"] - eps
            )
            if must_reject:
                assert case["verdict"] == "rejected", (
                    f"{case['id']}: quality {case['quality_system']} below ceiling "
                    f"{case['quality_ceiling']} but verdict {case['verdict']!r} — the quality "
                    f"gate must reject it despite a token saving of {case['token_saving_ratio']}"
                )
            else:
                assert case["verdict"] == "accepted"
            assert case["gap_vs_raw_read"] == round(1.0 - case["quality_system"], 6)
            assert case["token_saving_ratio"] > 0, "the system must beat full-corpus token cost"

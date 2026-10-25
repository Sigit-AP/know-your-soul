#!/usr/bin/env python3
"""Deterministic benchmark harness for the SOUL knowledge system (Task 10).

Subcommands:

* ``freeze`` — copy the pinned SOUL corpus from the read-only source
  directory into ``bench/corpus/`` and write a sha256 manifest. The source
  (``/root/.hermes/knowledge/soul``) is opened read-only and never modified.
* ``verify`` — verify ``bench/corpus/`` integrity against the manifest and
  report drift of the live source corpus (informational, never a write).
* ``check`` — self-checks that run without the system: cases schema,
  token-estimator parity against the frozen contract fixtures, frozen-corpus
  integrity. Exits 0 on pass, 1 on failure.
* ``run`` — the full paired benchmark (raw-SOUL baseline vs the knowledge
  system): N repeats, p50/p95 latency, token-estimator parity, deterministic
  quality rubric, machine-readable JSON ledger under ``bench/results/``.
  When the system under test cannot be imported or constructed (Tasks 4-9
  integration pending), ``run`` writes a ``skipped`` ledger and exits 3.

Token estimator (frozen contract, ``docs/mathematics/token-budget-contract.md``):
``estimated_tokens = ceil(utf8_byte_length / 4)`` applied to the canonical
JSON payload (sorted keys, compact separators, UTF-8). Parity with the frozen
fixtures in ``tests/fixtures/token_budget_cases.json`` is enforced by
``check`` and by ``tests/test_acceptance_e2e.py``.

Quality gate (contract): a token saving never excuses a quality regression —
a case whose quality verdict loses against its paired baseline is rejected
even when it saves tokens.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import sys
import tempfile
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent
CORPUS_DIR = BENCH_DIR / "corpus"
MANIFEST_PATH = CORPUS_DIR / "manifest.json"
CASES_PATH = BENCH_DIR / "cases.json"
RESULTS_DIR = BENCH_DIR / "results"
FIXTURES_PATH = REPO_ROOT / "tests" / "fixtures" / "token_budget_cases.json"

# The pinned source of the frozen corpus. READ-ONLY: this harness may open
# files under this directory for reading and hashing only, never for writing.
SOURCE_CORPUS_DIR = Path("/root/.hermes/knowledge/soul")

MIN_TOKEN_BUDGET = 128
ACCESS_ONLY_CAP = 160
DEFAULT_REPEATS = 5
QUALITY_FLOOR = 0.6

PRIMARY_DOMAINS = ("P1", "P2", "P3", "P4", "P5", "P6", "P7")

# Frozen corpus file -> expected primary domain (spec section 6). Used both
# for the seeded import and as the routing expectation source for cases.
FILE_DOMAIN_MAP: dict[str, str] = {
    "01-identity-compliance.md": "P6",
    "02-state-ledger.md": "P6",
    "03-reasoning-modes.md": "P5",
    "04-decision-taxonomy.md": "P4",
    "05-assumptions-tradeoffs.md": "P4",
    "06-heuristics-catalog.md": "P2",
    "07-calibration-bayes.md": "P1",
    "08-anti-bias.md": "P5",
    "09-premortem-steelman.md": "P4",
    "10-kill-criteria.md": "P3",
    "11-effort-router.md": "P3",
    "12-communication.md": "P6",
    "13-p1-coding-engineer.md": "P7",
    "14-p2-strategic-planner.md": "P7",
    "15-p3-system-designer.md": "P7",
    "16-p4-system-auditor.md": "P7",
    "17-p5-security-engineer.md": "P7",
    "18-p6-system-architect.md": "P7",
    "19-mitigations.md": "P3",
    "20-execution-cycle.md": "P3",
    "21-completion-criteria.md": "P6",
    "22-query-playbook.md": "P6",
    "23-glossary-bilingual.md": "P6",
    "24-metrics-and-levels.md": "P7",
    "25-measured-facts.md": "P1",
    "26-reasoning-synthesis.md": "P5",
    "27-part4-usage-and-schema.md": "P7",
    "28-operating-stack.md": "P6",
}


# Integration skip-gate: the benchmark measures the whole system only once all
# three access paths (Task 7 CLI/API, Task 8 MCP) exist on top of the service.
# The gate opens automatically when these modules land; the coordinator does
# not need to edit this file. Until then `run` exits 3 with a skipped ledger.
INTEGRATION_MODULES = ("soul_knowledge.cli", "soul_knowledge.http_api", "soul_knowledge.mcp_server")


class SystemUnavailable(RuntimeError):
    """The system under test cannot be imported or constructed yet."""


def integration_ready() -> None:
    """Raise SystemUnavailable while any access-path adapter is missing."""
    import importlib.util

    missing = [name for name in INTEGRATION_MODULES if importlib.util.find_spec(name) is None]
    if missing:
        raise SystemUnavailable("integration pending: missing adapters " + ", ".join(missing))


# --------------------------------------------------------------------- utils


def utc_now_iso() -> str:
    return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%SZ")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def canonical_json_bytes(payload: Any) -> bytes:
    """Canonical payload encoding per the frozen token-budget contract."""
    return json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")


def estimate_tokens(data: bytes) -> int:
    """``estimated_tokens = ceil(utf8_byte_length / 4)`` (frozen contract)."""
    return math.ceil(len(data) / 4)


def estimate_tokens_of_payload(payload: Any) -> int:
    return estimate_tokens(canonical_json_bytes(payload))


def percentile(sorted_samples: list[float], pct: float) -> float:
    """Nearest-rank percentile; ``sorted_samples`` must be sorted ascending."""
    if not sorted_samples:
        raise ValueError("percentile of empty sample set")
    rank = max(1, math.ceil(pct / 100.0 * len(sorted_samples)))
    return sorted_samples[rank - 1]


# ------------------------------------------------------------- frozen corpus


def freeze_corpus() -> int:
    """Snapshot the pinned SOUL corpus into bench/corpus (source read-only)."""
    if not SOURCE_CORPUS_DIR.is_dir():
        print(f"freeze: source corpus missing: {SOURCE_CORPUS_DIR}", file=sys.stderr)
        return 1
    sources = sorted(SOURCE_CORPUS_DIR.glob("*.md"))
    if not sources:
        print("freeze: source corpus holds no .md files", file=sys.stderr)
        return 1
    CORPUS_DIR.mkdir(parents=True, exist_ok=True)
    entries = []
    total_bytes = 0
    for src in sources:
        data = src.read_bytes()  # read-only: the source is never opened for writing
        dest = CORPUS_DIR / src.name
        dest.write_bytes(data)
        entries.append(
            {
                "file": src.name,
                "bytes": len(data),
                "sha256": sha256_bytes(data),
                "estimated_tokens": estimate_tokens(data),
                "expected_primary_domain": FILE_DOMAIN_MAP.get(src.name),
            }
        )
        total_bytes += len(data)
    manifest = {
        "manifest_version": "1.0.0",
        "frozen_at": utc_now_iso(),
        "source_dir": str(SOURCE_CORPUS_DIR),
        "source_access": "read-only",
        "estimator": "estimated_tokens = ceil(utf8_byte_length / 4)",
        "token_budget_contract": "docs/mathematics/token-budget-contract.md",
        "file_count": len(entries),
        "total_bytes": total_bytes,
        "total_estimated_tokens": estimate_tokens(b"x" * total_bytes),
        "files": entries,
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"freeze: {len(entries)} files, {total_bytes} bytes -> {CORPUS_DIR}")
    return 0


def verify_corpus() -> tuple[bool, list[str]]:
    """Check bench/corpus against its manifest; report live-source drift."""
    problems: list[str] = []
    if not MANIFEST_PATH.is_file():
        return False, ["bench/corpus/manifest.json missing — run `python bench/run.py freeze`"]
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    for entry in manifest["files"]:
        path = CORPUS_DIR / entry["file"]
        if not path.is_file():
            problems.append(f"missing frozen file: {entry['file']}")
            continue
        data = path.read_bytes()
        if sha256_bytes(data) != entry["sha256"]:
            problems.append(f"hash mismatch: {entry['file']}")
    frozen_names = {e["file"] for e in manifest["files"]}
    for extra in sorted(CORPUS_DIR.glob("*.md")):
        if extra.name not in frozen_names:
            problems.append(f"untracked file in frozen corpus: {extra.name}")
    drift: list[str] = []
    if SOURCE_CORPUS_DIR.is_dir():
        live = {p.name: sha256_bytes(p.read_bytes()) for p in SOURCE_CORPUS_DIR.glob("*.md")}
        for entry in manifest["files"]:
            live_hash = live.pop(entry["file"], None)
            if live_hash is None:
                drift.append(f"live source deleted: {entry['file']}")
            elif live_hash != entry["sha256"]:
                drift.append(f"live source changed since freeze: {entry['file']}")
        for name in sorted(live):
            drift.append(f"live source added since freeze: {name}")
    return (not problems, problems + [f"drift: {d}" for d in drift])


# --------------------------------------------------------------- case schema


def load_cases() -> list[dict[str, Any]]:
    cases = json.loads(CASES_PATH.read_text(encoding="utf-8"))["cases"]
    return cases


def validate_cases(cases: list[dict[str, Any]]) -> list[str]:
    problems: list[str] = []
    ids: set[str] = set()
    domains_seen: set[str] = set()
    for case in cases:
        cid = case.get("id", "<missing id>")
        for field in ("id", "question", "expected_primary_domain", "rubric_terms"):
            if field not in case:
                problems.append(f"{cid}: missing field {field}")
        if cid in ids:
            problems.append(f"{cid}: duplicate case id")
        ids.add(cid)
        domain = case.get("expected_primary_domain")
        if domain not in PRIMARY_DOMAINS:
            problems.append(f"{cid}: invalid expected_primary_domain {domain!r}")
        else:
            domains_seen.add(domain)
        if not case.get("rubric_terms"):
            problems.append(f"{cid}: rubric_terms must be non-empty")
        budget = case.get("token_budget", MIN_TOKEN_BUDGET)
        if budget < MIN_TOKEN_BUDGET and not case.get("access_only", False):
            problems.append(f"{cid}: token_budget {budget} below the {MIN_TOKEN_BUDGET} minimum")
    missing = set(PRIMARY_DOMAINS) - domains_seen
    if missing:
        problems.append(f"domains not covered by any case: {sorted(missing)}")
    if len(cases) < 20:
        problems.append(f"only {len(cases)} cases; the frozen set requires ~24 spanning P1-P7")
    return problems


# ------------------------------------------------------------- quality rubric


def rubric_score(rubric_terms: list[str], answer_text: str) -> float:
    """Deterministic rubric: fraction of required terms present (case-folded)."""
    if not rubric_terms:
        return 1.0
    haystack = answer_text.casefold()
    hits = sum(1 for term in rubric_terms if term.casefold() in haystack)
    return hits / len(rubric_terms)


# ------------------------------------------------------------- system adapter


def build_service(workdir: Path) -> Any:
    """Construct a KnowledgeService seeded from the frozen corpus.

    Written against the documented Task 2-4 interfaces (EventStore,
    SourceRegistry, SeedImporter, GraphProjector, KnowledgeService). Any
    import or construction failure means the integration is pending: the
    caller turns that into a ``skipped`` ledger, never a fake result.
    """
    try:
        from soul_knowledge.domain import PrimaryDomain
        from soul_knowledge.importer import DEFAULT_PARSER_VERSIONS, SeedImporter
        from soul_knowledge.projection import GraphProjector
        from soul_knowledge.service import KnowledgeService
        from soul_knowledge.source_registry import SourceKind, SourceRegistry
        from soul_knowledge.store import EventStore
    except Exception as exc:  # noqa: BLE001 — any failure means "not integrated yet"
        raise SystemUnavailable(f"system import failed: {exc}") from exc
    try:
        store = EventStore(workdir / "bench.sqlite3")
        registry = SourceRegistry(store)
        version = registry.observe(CORPUS_DIR, SourceKind.SOUL_CORPUS, DEFAULT_PARSER_VERSIONS)
        importer = SeedImporter(store, registry=registry)
        importer.ensure_roots(version.source_version_id)
        for entry in sorted(CORPUS_DIR.glob("*.md")):
            domain = PrimaryDomain(FILE_DOMAIN_MAP[entry.name])
            importer.import_file(entry, domain=domain, source_uri=f"soul://corpus/{entry.name}")
        projector = GraphProjector(store, output_root=workdir / "graph")
        projector.rebuild()
        return KnowledgeService(store, projector)
    except SystemUnavailable:
        raise
    except Exception as exc:  # noqa: BLE001
        raise SystemUnavailable(f"system construction failed: {type(exc).__name__}: {exc}") from exc


def query_once(service: Any, case: dict[str, Any]) -> tuple[Any, float]:
    """Run one timed query; returns (response, latency_ms)."""
    from soul_knowledge.contracts import QueryRequest

    request = QueryRequest(
        purpose=case["question"],
        domain=case.get("domain"),
        token_budget=case.get("token_budget", 1024),
        access_only=case.get("access_only", False),
        depth=case.get("depth", 2),
    )
    started = time.perf_counter_ns()
    response = service.query(request, actor="bench")
    latency_ms = (time.perf_counter_ns() - started) / 1e6
    return response, latency_ms


# -------------------------------------------------------------------- runner


def run_benchmark(repeats: int) -> int:
    cases = load_cases()
    problems = validate_cases(cases)
    ok, corpus_problems = verify_corpus()
    problems += [p for p in corpus_problems if not p.startswith("drift: ")]
    if problems:
        for problem in problems:
            print(f"run: {problem}", file=sys.stderr)
        return 1

    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    baseline_tokens = manifest["total_estimated_tokens"]

    ledger: dict[str, Any] = {
        "ledger_version": "1.0.0",
        "generated_at": utc_now_iso(),
        "repeats": repeats,
        "estimator": "estimated_tokens = ceil(utf8_byte_length / 4)",
        "token_budget_contract": "docs/mathematics/token-budget-contract.md (1.0.0)",
        "corpus_manifest_sha256": sha256_bytes(MANIFEST_PATH.read_bytes()),
        "baseline": {
            "kind": "raw-soul-corpus",
            "description": (
                "Reading the whole raw frozen corpus answers every question "
                "(quality 1.0 by construction) at full corpus token cost."
            ),
            "estimated_tokens": baseline_tokens,
            "bytes": manifest["total_bytes"],
            "quality": 1.0,
        },
        "cases": [],
    }

    with tempfile.TemporaryDirectory(prefix="soul-bench-") as tmp:
        try:
            integration_ready()
            service = build_service(Path(tmp))
        except SystemUnavailable as exc:
            ledger["status"] = "skipped"
            ledger["skip_reason"] = str(exc)
            for case in cases:
                ledger["cases"].append(
                    {
                        "id": case["id"],
                        "expected_primary_domain": case["expected_primary_domain"],
                        "status": "pending-integration",
                    }
                )
            RESULTS_DIR.mkdir(parents=True, exist_ok=True)
            out = RESULTS_DIR / f"ledger-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
            out.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
            print(f"run: SKIPPED ({exc}); ledger -> {out}")
            return 3

        all_passed = True
        for case in cases:
            latencies: list[float] = []
            last_response: Any = None
            for _ in range(repeats):
                last_response, latency = query_once(service, case)
                latencies.append(latency)
            latencies.sort()
            payload = last_response.canonical_payload()
            system_tokens = estimate_tokens_of_payload(payload)
            answer_text = json.dumps(payload, ensure_ascii=False)
            quality_system = rubric_score(case["rubric_terms"], answer_text)
            quality_baseline = ledger["baseline"]["quality"]
            quality_regression = quality_system < quality_baseline
            # Contract quality gate: a token saving never excuses a quality
            # regression — a case that loses quality is rejected even when it
            # saves tokens.
            if quality_regression or quality_system < QUALITY_FLOOR:
                verdict = "rejected"
                all_passed = False
            else:
                verdict = "accepted"
            ledger["cases"].append(
                {
                    "id": case["id"],
                    "expected_primary_domain": case["expected_primary_domain"],
                    "status": "measured",
                    "latency_ms": {
                        "p50": percentile(latencies, 50),
                        "p95": percentile(latencies, 95),
                        "samples": latencies,
                    },
                    "system_estimated_tokens": system_tokens,
                    "baseline_estimated_tokens": baseline_tokens,
                    "token_saving_ratio": round(1.0 - system_tokens / baseline_tokens, 6),
                    "quality_system": round(quality_system, 6),
                    "quality_baseline": quality_baseline,
                    "quality_regression": quality_regression,
                    "verdict": verdict,
                }
            )

    ledger["status"] = "ok" if all_passed else "failed"
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"ledger-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"run: {ledger['status'].upper()}; ledger -> {out}")
    return 0 if all_passed else 1


# -------------------------------------------------------------------- checks


def check_estimator_parity() -> list[str]:
    """Verify the local estimator against the frozen contract fixtures."""
    problems: list[str] = []
    fixtures = json.loads(FIXTURES_PATH.read_text(encoding="utf-8"))
    for case in fixtures["cases"]:
        payload = case.get("payload")
        expected = case.get("expected", {}).get("estimated_tokens")
        if payload is None or expected is None:
            continue
        got = estimate_tokens_of_payload(payload)
        if got != expected:
            problems.append(f"{case['id']}: estimator {got} != frozen expected {expected}")
    try:
        from soul_knowledge.contracts import estimated_tokens_of_payload as reference
    except Exception:  # noqa: BLE001 — library parity is best-effort pre-integration
        return problems
    for case in fixtures["cases"]:
        payload = case.get("payload")
        if payload is None:
            continue
        if estimate_tokens_of_payload(payload) != reference(payload):
            problems.append(f"{case['id']}: local estimator diverges from soul_knowledge.contracts")
    return problems


def run_checks() -> int:
    problems: list[str] = []
    try:
        cases = load_cases()
        problems += validate_cases(cases)
    except FileNotFoundError:
        problems.append(f"cases file missing: {CASES_PATH}")
    ok, corpus_problems = verify_corpus()
    if not ok:
        problems += [p for p in corpus_problems if not p.startswith("drift: ")]
    problems += check_estimator_parity()
    if problems:
        for problem in problems:
            print(f"check: FAIL {problem}")
        return 1
    print("check: OK (cases schema, frozen corpus integrity, estimator parity)")
    return 0


# ---------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="bench/run.py", description=(__doc__ or "").splitlines()[0])
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("freeze", help="snapshot the pinned SOUL corpus into bench/corpus (source read-only)")
    sub.add_parser("verify", help="verify frozen corpus integrity and report live-source drift")
    sub.add_parser("check", help="run the no-system self-checks")
    run_parser = sub.add_parser("run", help="run the paired benchmark (exit 3 = skipped, integration pending)")
    run_parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS, help="timed repeats per case")
    args = parser.parse_args(argv)

    if args.command == "freeze":
        return freeze_corpus()
    if args.command == "verify":
        ok, problems = verify_corpus()
        for line in problems:
            print(f"verify: {'FAIL' if not line.startswith('drift: ') else 'INFO'} {line}")
        if ok:
            print("verify: OK (frozen corpus matches manifest)")
            return 0
        return 1
    if args.command == "check":
        return run_checks()
    if args.command == "run":
        if args.repeats < 1:
            print("run: --repeats must be >= 1", file=sys.stderr)
            return 2
        return run_benchmark(args.repeats)
    return 2  # pragma: no cover


if __name__ == "__main__":
    sys.exit(main())

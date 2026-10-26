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

Quality gate (Opsi C, gate contract 2.1.0 — see docs/recovery/ and
``bench/README.md``): a case is accepted iff its quality reaches the
per-case COMPLIANT CEILING — the maximum rubric coverage any
contract-compliant response can deliver for that case — and the quality
floor. The ceiling is computed from the real projected graph artifact
(never hardcoded), so it rises automatically when enrichment improves.
Gate 2.1.0 (Ruling Q03) additionally restricts ceiling candidates to
nodes reachable under the single-root routing contract: the subtree of
the case's ``expected_primary_domain`` root, plus the subtree of any root
the deterministic router actually selected for the case. A ceiling the
old formula (2.0.0, kept verbatim in the ledger) computed from cross-root
node sets no compliant single-root response can assemble is lowered to
the routing-feasible maximum and annotated (``ceiling_routing_note``);
when the routing-feasible maximum equals the unrestricted value, the
2.0.0 record stands byte-identical. The raw-corpus baseline (quality 1.0,
full corpus tokens) is still reported per case as ``gap_vs_raw_read`` —
the recalibration changes what the gate *accepts*, never what it
*measures*.
"""

from __future__ import annotations

import argparse
import hashlib
import itertools
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
# Gate contract 2.1.0 (Opsi C + Ruling Q03). Float tolerance for ratio
# comparisons.
CEILING_EPSILON = 1e-9
# Exact subset maximization of the compliant ceiling up to this many source
# nodes per case; beyond it a deterministic greedy fallback is used.
CEILING_EXACT_MAX_NODES = 12

# Gate 2.1.0 (Ruling Q03): the compliant ceiling must be achievable by a
# compliant SINGLE-ROOT response, so ceiling candidates are restricted to
# the subtrees of the roots the routing contract designates for the case
# (the expected primary domain, plus any root the deterministic router
# actually selected). When that routing-feasible maximum equals the
# unrestricted (gate 2.0.0) ceiling value, the 2.0.0 record stands
# byte-identical; when it is lower, the ceiling is lowered and annotated.
VACUOUS_CEILING_NOTE = "vacuous: rubric unreachable under expected routing (case-design debt)"

GATE_VERSION = "2.1.0"

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
INTEGRATION_MODULES = ("know_your_soul.cli", "know_your_soul.http_api", "know_your_soul.mcp_server")


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


def rubric_scoring_text(payload: dict[str, Any]) -> str:
    """The JSON text the rubric scores: *payload* minus ``provenance``.

    Wave-3 W3-8(a): provenance is routing metadata — per the spec it
    contributes zero relevance, yet it rides the payload, so a rubric term
    could be satisfied by a corpus *filename* inside a ``soul://`` URI alone
    (e.g. q16's ``steelman`` via ``09-premortem-steelman.md``) with no
    delivered knowledge. Excluding the field on BOTH sides (system response
    and compliant ceiling) keeps the signal honest and the comparison
    symmetric. Provenance still counts toward token estimation — those are
    real wire bytes a compliant response must pay for.
    """
    return json.dumps({key: value for key, value in payload.items() if key != "provenance"}, ensure_ascii=False)


# ------------------------------------------------------- compliant ceiling


def latest_graph_path(output_root: Path) -> Path:
    """The newest published ``generations/*/graph.json`` under *output_root*."""
    candidates = sorted(output_root.glob("generations/*/graph.json"))
    if not candidates:
        raise SystemUnavailable(f"no projected graph.json under {output_root}")
    return candidates[-1]


def _ceiling_payload(nodes: list[dict[str, Any]], source_files: list[str]) -> dict[str, Any]:
    """A synthetic ``QueryResponse.canonical_payload`` for *nodes*.

    Keys and value shapes mirror the frozen response contract exactly
    (``know_your_soul.contracts.QueryResponse.canonical_payload``), with
    fixed dummy metadata. The rubric is scored over the JSON of this object
    minus ``provenance`` (``rubric_scoring_text``) — the same way the harness
    scores a real response — so payload keys (e.g. ``estimated_tokens``)
    count identically on both sides of the comparison. Provenance stays in
    the payload for token estimation; it is excluded from rubric scoring on
    both sides (W3-8(a)).
    """
    return {
        "access_only": False,
        "audit_event_id": "0" * 64,
        "candidates": [],
        "confidence": "EXTRACTED",
        "estimated_tokens": 0,
        "knowledge_payload_tokens": 0,
        "node_versions": {node["id"]: 1 for node in nodes},
        "projection_version": "0" * 64,
        "provenance": [f"soul://corpus/{name}" for name in source_files],
        "recommendations": {"best_actions": [], "recommendations": [], "unnecessary_actions": []},
        "required_knowledge_watermark": 0,
        "results": [
            {
                "confidence": "EXTRACTED",
                "domain": node["domain"],
                "kind": node["kind"],
                "node_id": node["id"],
                "p": "0.000000000000",
                "summary": node["label"],
                "version": 1,
            }
            for node in nodes
        ],
        "routing": "ranked",
        "source_knowledge_high_watermark": 0,
        "stale": False,
        "truncated": False,
    }


def compliant_ceiling(
    case: dict[str, Any],
    graph_nodes: list[dict[str, Any]],
    *,
    allowed_ids: set[str] | None = None,
) -> dict[str, Any]:
    """Per-case COMPLIANT CEILING (gate contract 2.1.0, Opsi C).

    Definition: the maximum ``rubric_score`` obtainable by any subset *S* of
    the projected-graph nodes whose ``source_file`` is one of the case's
    ``source_files``, when *S* is packed into a contract-shaped response
    payload (``_ceiling_payload``) that fits the case's ``token_budget``.
    When *allowed_ids* is given, candidates are additionally restricted to
    those node ids (used by the gate 2.1.0 single-root routing-feasibility
    constraint; ``None`` reproduces the gate 2.0.0 formula exactly).

    Everything is derived from the real projection artifact — node labels are
    the enriched summaries the system is actually allowed to publish — so the
    ceiling rises automatically when enrichment improves and can never be
    gamed by editing this harness. Exact subset maximization while the per-case
    node count is at most ``CEILING_EXACT_MAX_NODES``; a deterministic greedy
    fallback (marginal hits, then fewer tokens, then node id) beyond that.
    """
    rubric_terms = list(case["rubric_terms"])
    source_files = list(case.get("source_files", []))
    budget = int(case.get("token_budget", 1024))
    wanted = set(source_files)
    nodes = [node for node in graph_nodes if node.get("source_file") in wanted]
    if allowed_ids is not None:
        nodes = [node for node in nodes if node["id"] in allowed_ids]
    nodes.sort(key=lambda node: node["id"])  # deterministic order

    def score_of(subset: tuple[int, ...]) -> float:
        # Exact: score the real payload text, the same way a live response is
        # scored — no set-union approximation, so cross-field matches count.
        # Provenance is excluded from the scored text on both sides (W3-8(a)).
        payload = _ceiling_payload([nodes[i] for i in subset], source_files)
        return rubric_score(rubric_terms, rubric_scoring_text(payload))

    def tokens_of(subset: tuple[int, ...]) -> int:
        return estimate_tokens_of_payload(_ceiling_payload([nodes[i] for i in subset], source_files))

    def marginal_key(index: int, current: tuple[int, ...]) -> tuple[int, int, str]:
        gain = score_of(tuple(sorted((*current, index)))) - score_of(current)
        return (-round(gain * 1e9), tokens_of((index,)), nodes[index]["id"])

    full = tuple(range(len(nodes)))
    if tokens_of(full) <= budget:
        # Scores are monotone in the subset, so the unconstrained optimum is
        # the full set whenever it fits the budget (the common case).
        best_subset, best_score, method = full, score_of(full), "exact-full"
    elif len(nodes) <= CEILING_EXACT_MAX_NODES:
        # Exact: enumerate every subset, keep the best-scoring feasible one;
        # ties break towards the larger subset, then lexicographic node order.
        best_subset, best_score, method = (), 0.0, "exact-subset"
        for size in range(len(nodes) + 1):
            for subset in itertools.combinations(range(len(nodes)), size):
                if tokens_of(subset) > budget:
                    continue
                score = score_of(subset)
                if score > best_score + CEILING_EPSILON or (
                    abs(score - best_score) <= CEILING_EPSILON and len(subset) > len(best_subset)
                ):
                    best_subset, best_score = subset, score
    else:  # deterministic greedy fallback (documented; never hit at 24 cases)
        best_subset, method = (), "greedy-fallback"
        remaining = set(range(len(nodes)))
        while remaining:
            ranked = sorted(remaining, key=lambda i: marginal_key(i, best_subset))
            picked = None
            for candidate in ranked:
                trial = tuple(sorted((*best_subset, candidate)))
                if tokens_of(trial) <= budget:
                    picked = candidate
                    break
            if picked is None:
                break
            remaining.discard(picked)
            best_subset = tuple(sorted((*best_subset, picked)))
        best_score = score_of(best_subset)

    return {
        "quality_ceiling": round(best_score, 6),
        "ceiling_method": method,
        "ceiling_node_ids": [nodes[i]["id"] for i in best_subset],
        "ceiling_estimated_tokens": tokens_of(best_subset),
        "source_node_count": len(nodes),
    }


def _root_subtree_node_ids(links: list[dict[str, Any]], root_id: str) -> set[str]:
    """Node ids reachable from *root_id* via ``parent_of`` edges (inclusive).

    The projected graph is a strict ``parent_of`` tree rooted at the primary
    domains, so this set is exactly the node population a compliant
    single-root response routed to *root_id* may assemble results from
    (frozen Task 4 service contract: results come only from the selected
    root's traversal subtree).
    """
    children: dict[str, list[str]] = {}
    for link in links:
        if link.get("relation") == "parent_of":
            children.setdefault(str(link.get("source")), []).append(str(link.get("target")))
    seen: set[str] = set()
    stack = [root_id]
    while stack:
        current = stack.pop()
        if current in seen:
            continue
        seen.add(current)
        stack.extend(children.get(current, ()))
    return seen


def routing_feasible_ceiling(
    case: dict[str, Any],
    graph: dict[str, Any],
    routed_domains: list[str],
) -> dict[str, Any]:
    """Gate 2.1.0 ceiling: the gate 2.0.0 ceiling under Ruling Q03.

    Ruling Q03 (controller, 2026-10-25, progress.md): the compliant ceiling
    must respect BOTH contract bounds — (1) the compliant payload (budget, no
    raw text) and (2) the case's routing expectation (a single routed root;
    results assembled only from that root's subtree). A ceiling computed from
    a cross-root node set overestimates what a compliant system can deliver.

    Feasibility set: the union of the subtrees of (a) the case's
    ``expected_primary_domain`` root and (b) every root the deterministic
    router actually selected for this case (*routed_domains*). Both are
    legitimate routing designations for the case — the case-design intent and
    the frozen coverage-dominance router's decision — so the ceiling stays
    the maximum a compliant response *as this case is actually routed* can
    achieve. Only the routing decision is consulted here, never the measured
    quality, so the gate standard cannot be gamed by the response itself.

    When the routing-feasible maximum equals the unrestricted (2.0.0) value,
    the 2.0.0 record is returned byte-identical — the correction only ever
    lowers ceilings that no compliant single-root response can reach, and
    annotates them via ``ceiling_routing_note``.
    """
    links = graph.get("links", [])
    unrestricted = compliant_ceiling(case, graph["nodes"])
    expected = case["expected_primary_domain"]
    expected_ids = _root_subtree_node_ids(links, expected)
    expected_max = compliant_ceiling(case, graph["nodes"], allowed_ids=expected_ids)
    allowed = set(expected_ids)
    for domain in routed_domains:
        allowed |= _root_subtree_node_ids(links, domain)
    feasible = expected_max if allowed == expected_ids else compliant_ceiling(case, graph["nodes"], allowed_ids=allowed)
    if feasible["quality_ceiling"] >= unrestricted["quality_ceiling"] - CEILING_EPSILON:
        # The 2.0.0 ceiling is already achievable under the routing contract:
        # return its record byte-identical, plus informational notes only.
        result = dict(unrestricted)
        if expected_max["quality_ceiling"] < unrestricted["quality_ceiling"] - CEILING_EPSILON:
            result["ceiling_routing_note"] = (
                "ceiling feasible only via the router's actual domain choice outside the "
                "expected primary domain (case-design debt: routing-expectation mismatch)"
            )
        return result
    result = dict(feasible)
    result["ceiling_method"] = feasible["ceiling_method"] + "+single-root"
    result["ceiling_unrestricted"] = unrestricted["quality_ceiling"]
    if expected_max["quality_ceiling"] <= CEILING_EPSILON:
        result["ceiling_routing_note"] = VACUOUS_CEILING_NOTE
    else:
        result["ceiling_routing_note"] = "ceiling lowered to the single-root routing-feasible maximum"
    return result


# ------------------------------------------------------------- system adapter


def build_service(workdir: Path) -> Any:
    """Construct a KnowledgeService seeded from the frozen corpus.

    Written against the documented Task 2-4 interfaces (EventStore,
    SourceRegistry, SeedImporter, GraphProjector, KnowledgeService). Any
    import or construction failure means the integration is pending: the
    caller turns that into a ``skipped`` ledger, never a fake result.
    """
    try:
        from know_your_soul.domain import PrimaryDomain
        from know_your_soul.importer import DEFAULT_PARSER_VERSIONS, SeedImporter
        from know_your_soul.projection import GraphProjector
        from know_your_soul.service import KnowledgeService
        from know_your_soul.source_registry import SourceKind, SourceRegistry
        from know_your_soul.store import EventStore
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
    from know_your_soul.contracts import QueryRequest

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
        "ledger_version": "2.0.0",
        "generated_at": utc_now_iso(),
        "repeats": repeats,
        "estimator": "estimated_tokens = ceil(utf8_byte_length / 4)",
        "token_budget_contract": "docs/mathematics/token-budget-contract.md (1.0.0)",
        "corpus_manifest_sha256": sha256_bytes(MANIFEST_PATH.read_bytes()),
        "gate": {
            "gate_version": GATE_VERSION,
            "rule": (
                "accepted iff quality_system >= quality_ceiling (per-case compliant ceiling, "
                "single-root routing-feasible, epsilon 1e-9) AND quality_system >= "
                "quality_floor_applied (min(QUALITY_FLOOR, quality_ceiling))"
            ),
            "quality_floor": QUALITY_FLOOR,
            "rubric_scope": (
                "rubric terms are scored over the response/ceiling payload JSON minus the "
                "provenance field (wave-3 W3-8(a)): provenance is routing metadata with zero "
                "relevance per spec, so a term appearing only in a soul:// URI filename cannot "
                "count as delivered knowledge; provenance still counts toward token estimation "
                "on both sides"
            ),
            "ceiling_definition": (
                "max rubric_score over subsets of projected-graph nodes whose source_file is in "
                "the case's source_files, packed into a contract-shaped response payload within "
                "the case token_budget, with candidates restricted to nodes reachable under the "
                "single-root routing contract (Ruling Q03): the subtree of the case's "
                "expected_primary_domain root plus the subtree of every root the deterministic "
                "router actually selected for the case; when this routing-feasible maximum "
                "equals the gate 2.0.0 value, the 2.0.0 record stands byte-identical; computed "
                "from the live projection artifact, never hardcoded"
            ),
            "ceiling_definition_2.0.0": (
                "max rubric_score over subsets of projected-graph nodes whose source_file is in "
                "the case's source_files, packed into a contract-shaped response payload within "
                "the case token_budget; computed from the live projection artifact, never hardcoded"
            ),
            "legacy_rule_1.0.0": (
                "accepted iff quality_system >= 1.0 (raw-corpus baseline) AND >= QUALITY_FLOOR — "
                "provably unreachable for any contract-compliant response; kept here for audit only"
            ),
        },
        "baseline": {
            "kind": "raw-soul-corpus",
            "description": (
                "Reading the whole raw frozen corpus answers every question "
                "(quality 1.0 by construction) at full corpus token cost. "
                "Reported as a comparison metric only; the acceptance gate "
                "compares against the per-case compliant ceiling (gate 2.0.0), "
                "and each case reports gap_vs_raw_read explicitly."
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
            graph = json.loads(latest_graph_path(Path(tmp) / "graph").read_text(encoding="utf-8"))
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
            # W3-8(a): rubric scoring excludes the provenance field (routing
            # metadata, zero relevance per spec); token estimation does not.
            answer_text = rubric_scoring_text(payload)
            quality_system = rubric_score(case["rubric_terms"], answer_text)
            # Gate 2.1.0 (Ruling Q03): the ceiling is computed against the
            # roots the routing contract designates for this case — the
            # expected primary domain plus the domains the deterministic
            # router actually selected. Routing only, never quality, feeds
            # the ceiling, so the response cannot relax its own standard.
            routed_domains = sorted({str(r.get("domain")) for r in payload.get("results", [])})
            ceiling = routing_feasible_ceiling(case, graph, routed_domains)
            quality_baseline = ledger["baseline"]["quality"]
            quality_ceiling = ceiling["quality_ceiling"]
            # The floor can never demand more than the achievable maximum: a
            # case whose ceiling sits below QUALITY_FLOOR carries an
            # enrichment gap (rubric terms absent from every publishable
            # summary), not a system regression — flagged, not rejected on
            # floor grounds.
            quality_floor_applied = min(QUALITY_FLOOR, quality_ceiling)
            below_ceiling = quality_system < quality_ceiling - CEILING_EPSILON
            below_floor = quality_system < quality_floor_applied - CEILING_EPSILON
            # Gate 2.1.0 (Opsi C + Ruling Q03): a token saving never excuses
            # falling short of what a contract-compliant response can deliver
            # — a case that loses against its per-case compliant ceiling is
            # rejected even when it saves tokens.
            if below_ceiling or below_floor:
                verdict = "rejected"
                all_passed = False
            else:
                verdict = "accepted"
            case_entry = {
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
                "gap_vs_raw_read": round(quality_baseline - quality_system, 6),
                "quality_ceiling": quality_ceiling,
                "ceiling_method": ceiling["ceiling_method"],
                "ceiling_node_ids": ceiling["ceiling_node_ids"],
                "ceiling_estimated_tokens": ceiling["ceiling_estimated_tokens"],
                "ceiling_routed_domains": routed_domains,
                "quality_floor_applied": round(quality_floor_applied, 6),
                "ceiling_below_floor": quality_ceiling < QUALITY_FLOOR - CEILING_EPSILON,
                "quality_regression": below_ceiling,
                "verdict": verdict,
            }
            # Gate 2.1.0 transparency: when the single-root routing
            # constraint changed (or narrowly did not change) the ceiling,
            # the ledger says so explicitly, next to the 2.0.0 value.
            if "ceiling_unrestricted" in ceiling:
                case_entry["ceiling_unrestricted_2.0.0"] = ceiling["ceiling_unrestricted"]
            if ceiling.get("ceiling_routing_note"):
                case_entry["ceiling_routing_note"] = ceiling["ceiling_routing_note"]
            ledger["cases"].append(case_entry)

    ledger["status"] = "ok" if all_passed else "failed"
    rejected = [case["id"] for case in ledger["cases"] if case.get("verdict") == "rejected"]
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    out = RESULTS_DIR / f"ledger-{datetime.now(UTC).strftime('%Y%m%dT%H%M%SZ')}.json"
    out.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"run: {ledger['status'].upper()}; ledger -> {out}")
    annotated = [
        (case["id"], case["ceiling_routing_note"]) for case in ledger["cases"] if case.get("ceiling_routing_note")
    ]
    for cid, note in annotated:
        print(f"run: note {cid}: {note}")
    if rejected:
        print(f"run: rejected ({len(rejected)}/{len(cases)}): {', '.join(rejected)}")
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
        from know_your_soul.contracts import estimated_tokens_of_payload as reference
    except Exception:  # noqa: BLE001 — library parity is best-effort pre-integration
        return problems
    for case in fixtures["cases"]:
        payload = case.get("payload")
        if payload is None:
            continue
        if estimate_tokens_of_payload(payload) != reference(payload):
            problems.append(f"{case['id']}: local estimator diverges from know_your_soul.contracts")
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

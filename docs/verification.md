# Verification Evidence — Super Power Knowledge SOUL

Status: **INTEGRATED (Task 10, SUB-10, 2026-10-25 UTC).** Every row carries
fresh command output and exit codes from the final gate run. Base: HEAD
`e1835b8` plus the Task 10 integration changes (acceptance e2e made real,
`tests/test_no_ai_runtime.py` extended, `tests/test_scoring.py` typing fix,
repo-wide `ruff format` normalization) — uncommitted by rule; the coordinator
commits.

Gate order (plan Task 10): full `pytest -q`, type checking, lint, dependency
audit, `python bench/run.py`, then the Graphify coverage calculation.

## 1. Test suite

| Gate | Command | Exit code | Evidence (tail of fresh output) | Date (UTC) |
|------|---------|-----------|----------------------------------|------------|
| Full test suite | `.venv/bin/pytest tests/ -q` | **0** | `566 passed, 1 skipped, 1 warning in 71.55s` | 2026-10-25 |
| Acceptance e2e | `.venv/bin/pytest tests/test_acceptance_e2e.py -q` | **0** | `48 passed, 1 skipped in ~24s` (1 skip = external Hermes-install check) | 2026-10-25 |
| Type checking | `.venv/bin/pyright src tests bench` | **0** | `0 errors, 0 warnings, 0 informations` | 2026-10-25 |
| Lint | `.venv/bin/ruff check src tests bench` | **0** | `All checks passed!` | 2026-10-25 |
| Format | `.venv/bin/ruff format --check src tests bench` | **0** | `87 files already formatted` | 2026-10-25 |
| Dependency audit | `/root/.local/bin/uv lock --check` | **0** | `Resolved 73 packages in 2ms` (lock consistent; zero forbidden distributions — `TestLockedNoProviderEnvironment`) | 2026-10-25 |

Pre-existing issues found and fixed by the full-tree gates at integration
(both recorded in `docs/audit-release.md` §2): one pyright error in
`tests/test_scoring.py:132` (typed literal `"0"` → `Decimal("0")`), and
`ruff format` drift across 16 committed files (mechanical normalization,
behavior unchanged — verified by the green suite above).

## 2. Benchmark (paired raw-SOUL vs system)

Ledger: `bench/results/ledger-20261025T192059Z.json` (`repeats: 5`,
`status: failed`, gate contract **2.0.0** "Opsi C"). Command
`python bench/run.py run` exited **1**: the recalibrated gate accepts
**21/24** cases and rejects 3 — q03, q05, q16 — each a *genuine* retrieval
defect (system quality strictly below the per-case compliant ceiling; see
`docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md`).

Gate recalibration (SUB-22, controller ruling "Opsi C"): gate 1.0.0 compared
`quality_system` against the raw-corpus baseline (1.0), which is provably
unreachable for any contract-compliant response (no raw corpus text in the
payload + 1024-token budget, while rubric terms live in section bodies;
measured compliant ceiling 34/83 terms ≈ 0.41). Gate 2.0.0 accepts iff
`quality_system >= quality_ceiling` (per-case, computed from the live
projection artifact, never hardcoded) AND `>= min(QUALITY_FLOOR, ceiling)`.
The raw-corpus baseline remains in the ledger as a metric
(`gap_vs_raw_read` per case, mean 0.5861).

| Metric | Source of truth | Value | Evidence |
|--------|-----------------|-------|----------|
| Frozen corpus | `bench/corpus/manifest.json` | 28 files, 52,604 bytes, 13,151 est. tokens | `freeze: 28 files, 52604 bytes` exit 0; matches contract baseline tok-011 |
| Frozen questions | `bench/cases.json` | 24 cases, all 7 domains covered | `check: OK` exit 0 |
| Estimator parity | `tests/fixtures/token_budget_cases.json` | 7/7 payload fixtures match | `TestTokenEstimatorParity` |
| Repeats | bench ledger | 5 per case | ledger `repeats: 5` |
| Latency p50 (per case, across 24 cases) | bench ledger | median **8.46 ms** (min 3.29, max 16.11) | ledger `latency_ms.p50` |
| Latency p95 (per case, across 24 cases) | bench ledger | median **11.02 ms** (max 35.67) | ledger `latency_ms.p95` |
| Token savings (paired) | bench ledger | mean **94.28%** (min 92.45%); system 123–993 est. tokens vs 13,151 raw-corpus baseline | ledger `token_saving_ratio` |
| Compliant ceiling | bench ledger | per-case 0.0–1.0, mean **0.4181**; 18/24 cases below the 0.6 floor (`ceiling_below_floor`, enrichment debt) | ledger `quality_ceiling`, `ceiling_method: exact-full` |
| Quality verdicts | bench ledger | **21/24 accepted**; quality mean 0.4139; rejected: q03 (0 vs 0.333), q05 (0 vs 0.5, routing `ambiguous`), q16 (0.5 vs 0.75, misrouted to P6) | ledger `quality_system`, `quality_ceiling`, `verdict` |
| Quality gate behavior | bench ledger | every below-ceiling case rejected *despite* ≥92% token savings — the gate works | `TestBenchmarkQualityGate` |

Diagnosis history: gate 1.0.0 rejected 24/24 (then 4/24 post-enrichment)
because it demanded raw-read parity; the recalibration record
(`docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md`) carries the
unreachability proof and the before/after numbers. The remaining 3 rejections
are retrieval-line defects (owner: retrieval/service), not bench
miscalibration.

## 3. Graphify coverage

Coverage counts **only** matrix rows whose owning contract tests pass; the
target is ≥ 90% of the applicable 40-feature inventory
(`docs/research/graphify-feature-matrix.md`).

| Item | Value | Evidence |
|------|-------|----------|
| Applicable features (denominator) | 40 | feature matrix §1 |
| Rows with passing named contract tests | **40/40** | fresh junit run of all 40 named tests: `40 passed in 6.89s`, exit 0 (`TestGraphifyCoverageGate`) |
| Counted coverage | **39/40 = 97.5%** (row 23 excluded: residual open) | matrix rule: partials count only once residual closed and test passes |
| Coverage gate (≥ 90%) | **PASS** | 97.5% ≥ 90% |
| Partial residual 1 | Row 22 (schema/version provenance) — **closed at Task 3** (event log is source of truth; deterministic rebuild migrates) | matrix §3.2 note; named test passes |
| Partial residual 2 | Row 23 (deterministic serialization + atomic writes) — **OPEN**, residual owner Task 9: whole-set durability / power-loss recovery | matrix note "Row 23's residual stays open with Task 9" (audit wave-1 fix round) |
| Partial residual 3 | Row 38 (token-reduction benchmark) — residual owner Task 10: **closed at integration** by the delivered harness (N repeats, p50/p95 latency, quality rubric, paired ledger); note the harness currently *measures a failing quality gate* (finding T10-1), which is a system quality issue, not missing instrumentation | `bench/run.py`, ledger 151538Z, `tests/test_service.py::test_canonical_token_parity` passes |

Matrix doc correction at integration: three named-test cells (rows 25/36/40)
were corrected to the tests that actually landed (SUB-12 added the row-25
test to `tests/test_operations.py` and the row-36/40 tests to
`tests/test_security.py`; the matrix cells and a Task 9 verification note
record this).

## 4. Acceptance criteria traceability

Every Task 10 acceptance bullet is a named test in
`tests/test_acceptance_e2e.py`. Status at integration: **48 passed,
1 skipped** (exit 0).

| Acceptance bullet | Test | Status |
|-------------------|------|--------|
| Seven domains routed | `TestSevenDomainsEndToEnd` | **PASS** (real, frozen-corpus system) |
| Exactly ten audited tools | `TestAuditedTools` | **PASS** (2 tests) |
| Three access paths | `TestThreeAccessPaths` | **PASS** (2 tests; CLI subprocess + local API + MCP, canonical payload parity minus per-invocation audit id and wall-clock expiry) |
| One final audit per valid invocation | `TestExactlyOneFinalAudit::test_one_final_audit_per_valid_invocation` | **PASS** |
| One final audit per recovered failure | `TestExactlyOneFinalAudit::test_one_final_audit_per_recovered_failure` | **PASS** (typed failure + crash-recovery journal, second recovery pass appends nothing) |
| Idempotent retry linkage | `TestIdempotentRetry` | **PASS** (original domain result preserved, new linked audit id per retry) |
| Deterministic scoring | `TestDeterministicScoring` | **PASS** (byte-identical replay under evidence permutation) |
| Rollback | `TestRollback` | **PASS** (improvement content byte-exact; relearn DB + graph byte-exact, one linked audit) |
| Locked no-provider environment | `TestLockedNoProviderEnvironment` | **PASS** (5 tests) |
| Scrubbed runtime | `TestScrubbedRuntime` (+ `tests/test_no_ai_runtime.py`) | **PASS** (dirty env → MCP startup refused exit 2; scrubbed env serves a real session) |
| Forbidden imports/entry points | `TestForbiddenImportsAndEntryPoints` | **PASS** (3 tests) |
| Denied outbound sockets | `TestDeniedOutboundSockets` (+ `tests/test_no_ai_runtime.py::test_runtime_surface_runs_with_outbound_sockets_denied`) | **PASS** (full lifecycle under the guard) |
| Absent sampling handlers/capabilities | `TestAbsentSampling` | **PASS** (2 tests) |
| MCP root/schema/range/capability contract | `TestMcpContract` | **PASS** (4 tests, real in-memory JSON-RPC) |
| Conditional access | `TestConditionalAccess` | **PASS** (3 tests incl. tampered-artifact fail-closed) |
| Recommendation shape | `TestRecommendationShape` | **PASS** (2 tests) |
| RESET/ACCUMULATE/rollback | `TestRelearn` | **PASS** (2 tests; operator-gated, zero evidence weight after RESET) |
| No stale mixing | `TestNoStaleMixing` | **PASS** (ACCUMULATE anchor-mapped carry; RESET version exclusion) |
| No Hermes source modification | `TestNoHermesSourceModification` + `test_live_soul_corpus_unmodified_since_freeze` | corpus-side pin **PASS**; install-tree check **SKIP-GATED (external)** |
| Graphify coverage ≥ 90% | `TestGraphifyCoverageGate` | **PASS** (3 tests) |
| Benchmark quality gate | `TestBenchmarkQualityGate` | **PASS** (gate semantics verified on a real run; the *system* currently fails the gate — §2) |

## 5. Recovery log

Launch blockers and their closures live in
`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave1-security.md`
(wave 1, F1–F6, all closed/verified at `e1835b8`) and
`docs/audit-release.md`. A finding closes only after a fresh independent
reviewer verifies both the regression probe and the fix.

| Finding ID | Class | Status | Recovery doc |
|------------|-------|--------|--------------|
| T10-1 (benchmark quality) | launch-blocking | **PARTIALLY CLOSED — gate recalibrated (Opsi C, 21/24); 3 retrieval defects open** | `docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md`, `docs/audit-release.md` §2/§3 |

## 6. Remaining skip (external, recorded)

`TestNoHermesSourceModification::test_hermes_source_tree_is_unmodified` stays
skip-gated: it verifies the *installed Hermes source tree*, which is external
to this repo and checked at release time against the Hermes installation. The
corpus-side pin (live SOUL corpus unchanged since freeze) runs for real in
`TestFrozenCorpus` and passes.

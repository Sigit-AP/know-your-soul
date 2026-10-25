# Verification Evidence — Super Power Knowledge SOUL

Status: **INTEGRATED (Task 10, SUB-10, 2026-10-25 UTC).** Every row carries
fresh command output and exit codes from the final gate run. Base: HEAD
`e1835b8` plus the Task 10 integration changes (acceptance e2e made real,
`tests/test_no_ai_runtime.py` extended, `tests/test_scoring.py` typing fix,
repo-wide `ruff format` normalization) — uncommitted by rule; the coordinator
commits.

**Update 2026-10-25 ~20:30 UTC (SUB-26, HEAD `4091aef`)**: §1/§2 refreshed
with the final fresh gate run after the bench ceiling correction (gate
2.1.0, Ruling Q03 — `docs/recovery/task-10-ceiling-single-root-ruling-q03.md`).
The benchmark gate now exits **0 with 24/24 accepted**; the release-gate
line in `docs/audit-release.md` §4 is updated accordingly.

Gate order (plan Task 10): full `pytest -q`, type checking, lint, dependency
audit, `python bench/run.py`, then the Graphify coverage calculation.

## 1. Test suite

| Gate | Command | Exit code | Evidence (tail of fresh output) | Date (UTC) |
|------|---------|-----------|----------------------------------|------------|
| Full test suite | `.venv/bin/pytest tests/ -q` | **0** | `632 passed, 1 skipped, 1 warning in 91.17s` | 2026-10-25 |
| Acceptance e2e | `.venv/bin/pytest tests/test_acceptance_e2e.py -q` | **0** | 48 passed, 1 skipped (1 skip = external Hermes-install check); last verified at integration, and the full-suite row above re-runs it fresh | 2026-10-25 |
| Type checking | `.venv/bin/pyright` | **0** | `0 errors, 0 warnings, 0 informations` | 2026-10-25 |
| Lint | `.venv/bin/ruff check .` | **0** | `All checks passed!` | 2026-10-25 |
| Format | `.venv/bin/ruff format --check .` | **1 (pre-existing)** | `138 files already formatted`; the single offender is `docs/mathematics/scoring.md` — committed drift reproduced at clean HEAD (verified via stash), not introduced by this change; `bench/run.py` itself is format-clean | 2026-10-25 |
| Dependency audit | `/root/.local/bin/uv lock --check` | **0** | `Resolved 73 packages in 2ms` (lock consistent; zero forbidden distributions — `TestLockedNoProviderEnvironment`) | 2026-10-25 |

Pre-existing issues found and fixed by the full-tree gates at integration
(both recorded in `docs/audit-release.md` §2): one pyright error in
`tests/test_scoring.py:132` (typed literal `"0"` → `Decimal("0")`), and
`ruff format` drift across 16 committed files (mechanical normalization,
behavior unchanged — verified by the green suite above).

## 2. Benchmark (paired raw-SOUL vs system)

Ledger: `bench/results/ledger-20261025T202627Z.json` (`repeats: 5`,
`status: ok`, gate contract **2.1.0** "Opsi C + Ruling Q03"). Command
`.venv/bin/python bench/run.py run` exits **0**: **24/24 cases accepted** —
23 at their (byte-identical) gate 2.0.0 ceilings, q03 vacuous-accepted with
the explicit ledger annotation
`vacuous: rubric unreachable under expected routing (case-design debt)`.
`bench check` also exits **0** (`check: OK (cases schema, frozen corpus
integrity, estimator parity)`).

Gate history: gate 1.0.0 compared `quality_system` against the raw-corpus
baseline (1.0), provably unreachable for any contract-compliant response
(measured compliant ceiling 34/83 terms ≈ 0.41) — recalibrated by SUB-22
(Opsi C) to gate 2.0.0 (`docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md`).
SUB-25 then fixed the two genuine retrieval defects gate 2.0.0 had isolated
(q05 node-id slug-segment indexing, q16 reciprocal-rank root aggregation —
`docs/recovery/task-10-retrieval-q03-q05-q16.md`) and proved q03's residual
was structural: its ceiling demanded a cross-root result set
{07 (P1), 08 (P5)} that the frozen single-root service contract can never
assemble. Gate 2.1.0 (SUB-26, Ruling Q03) restricts ceiling candidates to
the single-root routing-feasible set — the subtree of the case's
`expected_primary_domain` root plus the subtree of every root the
deterministic router actually selected (routing only, never quality, feeds
the ceiling). When the feasible maximum equals the 2.0.0 value the record
stands byte-identical — **verified 23/23 non-q03 cases unchanged across
every ceiling and gate field**; only q03's ceiling dropped (0.3333 → 0.0).
The 2.0.0 definition and legacy 1.0.0 rule are preserved verbatim in the
ledger's `gate` block. q07 carries its own explicit note (ceiling feasible
only via the router's actual P7 choice, expected P2 — routing-expectation
mismatch, case-design debt). `gap_vs_raw_read` is untouched: q03 still
reports an honest 1.0.

| Metric | Source of truth | Value | Evidence |
|--------|-----------------|-------|----------|
| Frozen corpus | `bench/corpus/manifest.json` | 28 files, 52,604 bytes, 13,151 est. tokens | `freeze: 28 files, 52604 bytes` exit 0; matches contract baseline tok-011 |
| Frozen questions | `bench/cases.json` | 24 cases, all 7 domains covered | `check: OK` exit 0 |
| Estimator parity | `tests/fixtures/token_budget_cases.json` | 7/7 payload fixtures match | `TestTokenEstimatorParity` |
| Repeats | bench ledger | 5 per case | ledger `repeats: 5` |
| Latency p50 (per case, across 24 cases) | bench ledger | median **6.43 ms** (min 4.35, max 12.55) | ledger `latency_ms.p50` |
| Latency p95 (per case, across 24 cases) | bench ledger | median **7.43 ms** (max 35.59) | ledger `latency_ms.p95` |
| Token savings (paired) | bench ledger | mean **94.26%** (min 92.45%); system 412–993 est. tokens vs 13,151 raw-corpus baseline | ledger `token_saving_ratio` |
| Compliant ceiling | bench ledger | per-case 0.0–1.0, mean **0.4042**; 18/24 cases below the 0.6 floor (`ceiling_below_floor`, enrichment debt) | ledger `quality_ceiling`, `ceiling_method: exact-full` (q03: `exact-full+single-root`) |
| Quality verdicts | bench ledger | **24/24 accepted**; quality mean **0.4451**; q03 vacuous-accepted (annotated, `ceiling_unrestricted_2.0.0: 0.333333` preserved) | ledger `quality_system`, `quality_ceiling`, `verdict`, `ceiling_routing_note` |
| Quality gate behavior | bench ledger | every below-ceiling case is rejected *despite* ≥92% token savings — the gate works; semantics re-verified fresh on a 24/24 run | `TestBenchmarkQualityGate` (48 passed, 1 skipped, exit 0, fresh) |

Diagnosis history: gate 1.0.0 rejected 24/24 (then 4/24 post-enrichment)
because it demanded raw-read parity; the recalibration record
(`docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md`) carries the
unreachability proof and the before/after numbers. The 3 residual rejections
under gate 2.0.0 are closed: q05/q16 by retrieval fixes
(`docs/recovery/task-10-retrieval-q03-q05-q16.md`), q03 by the Ruling Q03
ceiling correction
(`docs/recovery/task-10-ceiling-single-root-ruling-q03.md`).

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
| Partial residual 3 | Row 38 (token-reduction benchmark) — residual owner Task 10: **closed at integration** by the delivered harness (N repeats, p50/p95 latency, quality rubric, paired ledger); the gate it measured (finding T10-1) is now green — gate 2.1.0, exit 0, 24/24 (§2) | `bench/run.py`, ledger `ledger-20261025T202627Z`, `tests/test_service.py::test_canonical_token_parity` passes |

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
| Benchmark quality gate | `TestBenchmarkQualityGate` | **PASS** (gate semantics verified on a real run; the system passes the gate 24/24 under gate 2.1.0 — §2) |

## 5. Recovery log

Launch blockers and their closures live in
`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave1-security.md`
(wave 1, F1–F6, all closed/verified at `e1835b8`) and
`docs/audit-release.md`. A finding closes only after a fresh independent
reviewer verifies both the regression probe and the fix.

| Finding ID | Class | Status | Recovery doc |
|------------|-------|--------|--------------|
| T10-1 (benchmark quality) | launch-blocking | **CLOSED — gate 2.1.0, bench exit 0, 24/24 accepted (q03 vacuous-accepted, annotated)** | `docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md`, `docs/recovery/task-10-retrieval-q03-q05-q16.md`, `docs/recovery/task-10-ceiling-single-root-ruling-q03.md`, `docs/audit-release.md` §2/§3 |

## 6. Remaining skip (external, recorded)

`TestNoHermesSourceModification::test_hermes_source_tree_is_unmodified` stays
skip-gated: it verifies the *installed Hermes source tree*, which is external
to this repo and checked at release time against the Hermes installation. The
corpus-side pin (live SOUL corpus unchanged since freeze) runs for real in
`TestFrozenCorpus` and passes.

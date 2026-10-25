# Verification Evidence — Super Power Knowledge SOUL

Status: **SKELETON — evidence to be filled at integration (Task 10 part 2).**
Every row must carry the *actual* fresh command output and exit code; no claim
without evidence. Run all gates from the repo root with the locked `.venv`.

Gate order (plan Task 10): full `pytest -q`, type checking, lint, dependency
audit, `python bench/run.py`, then the Graphify coverage calculation.

## 1. Test suite

| Gate | Command | Exit code | Evidence (tail of fresh output) | Date (UTC) |
|------|---------|-----------|----------------------------------|------------|
| Full test suite | `.venv/bin/pytest -q` | _pending_ | _pending_ | _pending_ |
| Acceptance e2e | `.venv/bin/pytest tests/test_acceptance_e2e.py -q` | 0 (part 1: 18 passed, 30 skipped) | skips are integration-gated bullets | 2026-10-25 |
| Type checking | `.venv/bin/pyright` | _pending_ | _pending_ | _pending_ |
| Lint | `.venv/bin/ruff check` | _pending_ | _pending_ | _pending_ |
| Dependency audit | `uv audit` (or recorded equivalent) | _pending_ | _pending_ | _pending_ |

## 2. Benchmark (paired raw-SOUL vs system)

| Metric | Source of truth | Value | Evidence |
|--------|-----------------|-------|----------|
| Frozen corpus | `bench/corpus/manifest.json` | 28 files, 52,604 bytes, 13,151 est. tokens | matches frozen contract baseline (tok-011) |
| Frozen questions | `bench/cases.json` | 24 cases, all 7 domains covered | `bench/run.py check` exit 0 |
| Estimator parity | `tests/fixtures/token_budget_cases.json` | 7/7 payload fixtures match | `TestTokenEstimatorParity` |
| Repeats / latency p50 | bench ledger | _pending integration_ | _pending_ |
| Latency p95 | bench ledger | _pending integration_ | _pending_ |
| Token savings (paired) | bench ledger | _pending integration_ | _pending_ |
| Quality verdicts | bench ledger | _pending integration_; gate: quality regression ⇒ rejected even with token savings | _pending_ |
| Ledger path | `bench/results/ledger-*.json` | _pending integration_ | _pending_ |

## 3. Graphify coverage

Coverage counts **only** matrix rows whose owning contract tests pass; the
target is ≥ 90% of the applicable 40-feature inventory
(`docs/research/graphify-feature-matrix.md`).

| Item | Value | Evidence |
|------|-------|----------|
| Applicable features (denominator) | 40 | feature matrix |
| Rows with passing contract tests | _pending_ | _pending_ |
| Coverage % | _pending_ | _pending_ |
| Partial residual 1 | _pending_ (owner + reason) | _pending_ |
| Partial residual 2 | _pending_ (owner + reason) | _pending_ |
| Partial residual 3 | _pending_ (owner + reason) | _pending_ |

## 4. Acceptance criteria traceability

Every Task 10 acceptance bullet is a named test in
`tests/test_acceptance_e2e.py`. Status legend: PASS (runs for real now),
SKIP-GATED (named, `integration pending`, coordinator removes the skip).

| Acceptance bullet | Test | Status |
|-------------------|------|--------|
| Seven domains routed | `TestSevenDomainsEndToEnd` | SKIP-GATED |
| Exactly ten audited tools | `TestAuditedTools` | SKIP-GATED |
| Three access paths | `TestThreeAccessPaths` | SKIP-GATED |
| One final audit per valid invocation | `TestExactlyOneFinalAudit::test_one_final_audit_per_valid_invocation` | SKIP-GATED |
| One final audit per recovered failure | `TestExactlyOneFinalAudit::test_one_final_audit_per_recovered_failure` | SKIP-GATED |
| Idempotent retry linkage | `TestIdempotentRetry` | SKIP-GATED |
| Deterministic scoring | `TestDeterministicScoring` | SKIP-GATED |
| Rollback | `TestRollback` | SKIP-GATED |
| Locked no-provider environment | `TestLockedNoProviderEnvironment` | PASS (5 tests) |
| Scrubbed runtime | `TestScrubbedRuntime` (+ `tests/test_no_ai_runtime.py`) | SKIP-GATED (e2e), covered in unit gate |
| Forbidden imports/entry points | `TestForbiddenImportsAndEntryPoints` | PASS (3 tests) |
| Denied outbound sockets | `TestDeniedOutboundSockets` (+ `tests/test_no_ai_runtime.py`) | SKIP-GATED (e2e), covered in unit gate |
| Absent sampling handlers/capabilities | `TestAbsentSampling` | SKIP-GATED |
| MCP root/schema/range/capability contract | `TestMcpContract` | SKIP-GATED (4 tests) |
| Conditional access | `TestConditionalAccess` | SKIP-GATED (2 tests) |
| Recommendation shape | `TestRecommendationShape` | SKIP-GATED (2 tests) |
| RESET/ACCUMULATE/rollback | `TestRelearn` | SKIP-GATED (2 tests) |
| No stale mixing | `TestNoStaleMixing` | SKIP-GATED |
| No Hermes source modification | `TestNoHermesSourceModification` + `test_live_soul_corpus_unmodified_since_freeze` | corpus-side pin PASS; tree check SKIP-GATED |

## 5. Recovery log

Launch blockers and their closures live in `docs/recovery/<finding-id>.md` and
are tracked in `docs/audit-release.md`. A finding closes only after a fresh
independent reviewer verifies both the regression probe and the fix.

| Finding ID | Class | Status | Recovery doc |
|------------|-------|--------|--------------|
| _none yet_ | — | — | — |

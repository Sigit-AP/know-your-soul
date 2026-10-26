# Verification Evidence — Know Your Soul

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

**Update 2026-10-25 ~23:59 UTC (SUB-34, wave-3 audit fix round, HEAD
`ff06e3d` + uncommitted wave-3 fixes)**: four audit-wave-3 minors closed —
W3-7 (ops-path hardlink refusal + post-operation re-resolution), W3-9
(`body_timeout_seconds <= 0` refused at startup), W3-8(a) (bench rubric
excludes the `provenance` field from scoring text on both sides; **0/24
per-case numbers changed**, bench stays exit 0, 24/24), W3-11 (the external
Hermes-install test is now a real git-clean/tag-pin check — it **runs and
passes** on this host; the last vacuous skip is gone). Recovery doc:
`docs/recovery/wave3-ops-timeout-rubric-external.md`.

**Update 2026-10-26 (SUB-44, HEAD `51565d3` + in-flight fix waves)**: §1/§2
refreshed with fresh gate output after W3-2 (injective ids, `c1165b5`) and
the final redaction residuals (`51565d3`): full suite **898 passed, 0
skipped** (732 core + SUB-40/SUB-43 additions); bench ledger
`20261026T031852Z` (token savings 94.18%, system 420–968 est. tokens;
quality/ceiling means unchanged); README bench commands
fixed to `uv run python bench/run.py …` (plain `python` fails
`ModuleNotFoundError` outside the uv environment, audit H-1); `ruff format`
offender count corrected to 162/163 files (§1).

**Update 2026-10-26 ~12:35 UTC (SUB-52, HEAD `81e1823` + this fix wave)**:
three final residuals closed — SUB48-1 (`publish_capacity_bytes` overrides
above `ARTIFACT_MAX_BYTES` now fail closed at construction with the typed
`InvalidPublishCapacity`; live-probe value `2**20` rejected), F-SUB51-1
(`inspect_backup` surfaces the typed `BackupError` on non-SQLite files),
F-SUB51-2 (dead `PayloadTooLarge` branch removed from `error_envelope`,
behavior pinned unchanged). §1/§2 refreshed with fresh gate output: full
suite **1369 passed, 0 skipped**; code coverage **99.47%** (7,227 stmts, 38
miss); bench ledger `20261026T122910Z` — **zero drift** vs `20261026T031852Z`
(0/216 per-case field comparisons changed, verified programmatically).
Recovery doc: `docs/recovery/sub52-final-residual-closures.md`.

*Concurrency note:* these rows were measured while the SUB-40..43 fix waves
were landing in the working tree; the tree is transiently red mid-wave
(e.g. `uv run ruff check` and even collection can fail on another agent's
half-landed files). The numbers above are real dated outputs from green
windows; the coordinator's final gate run supersedes them.

Gate order (plan Task 10): full `pytest -q`, type checking, lint, dependency
audit, `uv run python bench/run.py`, then the Graphify coverage calculation.

**Update 2026-10-26 ~21:5x UTC (SUB-70, ATM final integration, HEAD
`65bb301` + this wave)**: the built-in export registry
(`exporters/builtin.py`) now wires **all** ATM wave A/B/C modules — 30
artifacts (16 format + 14 analytics), each capped and round-trip-validated —
into the operational CLI (`export --formats all`, `analytics --modules all`,
short aliases `dot|mermaid|tabular|graphml|svg|html|book|centrality|...`).
End-to-end on a real store built from `bench/corpus` (36 nodes / 35 edges /
71 domain events): all 30 artifacts produced, hashed, idempotent on re-run,
store event count unchanged (74/74 before/after); a second generation after
a real `record-outcome` turns `generation-diff` into a real diff and the
timeline shows a real scored point (P=1.0, level 2). Fresh gates: full suite
**2025 passed** (251.62s), `ruff check`/`format` clean on `src/`+`tests/`,
pyright **0/0/0**, `bench/run.py check` **OK**. Full evidence + artifact
table (size + sha256): §7 and `docs/recovery/atm-integration-final.md`.

## 1. Test suite

| Gate | Command | Exit code | Evidence (tail of fresh output) | Date (UTC) |
|------|---------|-----------|----------------------------------|------------|
| Full test suite | `.venv/bin/pytest tests/ -q` | **0** | `1369 passed, 1 warning in 244.02s` — **0 skipped** (SUB-52 wave incl. SUB48-1/F-SUB51-1/F-SUB51-2 regression probes); supersedes the dated historical rows `898 passed` (SUB-44), `632 passed, 1 skipped` (pre-wave-3) and `690 passed` (wave-3 fix round) | 2026-10-26 |
| Acceptance e2e | `.venv/bin/pytest tests/test_acceptance_e2e.py -q` | **0** | `49 passed in 48.72s` — the install-tree check (`TestNoHermesSourceModification`) executes for real; skip-gates only when no git-managed Hermes install exists | 2026-10-26 |
| Code coverage | `.venv/bin/pytest tests/ -q --cov=know_your_soul --cov-report=term` | **0** | **99.47%** (7,227 statements, 38 miss; supersedes 99.46% / 7,220 / 39 from the wave-3 push) | 2026-10-26 |
| Type checking | `.venv/bin/pyright` | **0** | `0 errors, 0 warnings, 0 informations` | 2026-10-26 |
| Lint | `.venv/bin/ruff check .` | **0** | `All checks passed!` | 2026-10-26 |
| Format | `.venv/bin/ruff format --check .` | **1 (pre-existing)** | `1 file would be reformatted, 190 files already formatted`; the single offender is `docs/mathematics/scoring.md` (comment alignment inside a fenced code block) — drift committed in `724843c` and never touched since (verified `git log --follow docs/mathematics/scoring.md` → sole commit `724843c`), i.e. pre-existing every fix wave; the contracts directory is frozen, so the drift is documented, not reformatted | 2026-10-26 |
| Dependency audit | `/root/.local/bin/uv lock --check` | **0** | `Resolved 101 packages in 3ms` (lock consistent; zero forbidden distributions — `TestLockedNoProviderEnvironment`) | 2026-10-26 |

Pre-existing issues found and fixed by the full-tree gates at integration
(both recorded in `docs/audit-release.md` §2): one pyright error in
`tests/test_scoring.py:132` (typed literal `"0"` → `Decimal("0")`), and
`ruff format` drift across 16 committed files (mechanical normalization,
behavior unchanged — verified by the green suite above).

## 2. Benchmark (paired raw-SOUL vs system)

Ledger: `bench/results/ledger-20261026T122910Z.json` (`repeats: 5`,
`status: ok`, gate contract **2.1.0** "Opsi C + Ruling Q03") — refreshed
2026-10-26 ~12:29 UTC by SUB-52 after the final residual closures;
**zero drift** vs `20261026T031852Z` (0/216 per-case comparisons across
`quality_system`, `quality_ceiling`, `system_estimated_tokens`,
`token_saving_ratio`, `gap_vs_raw_read`, `ceiling_node_ids`,
`ceiling_method`, `verdict`, `expected_primary_domain` — verified
programmatically). Command
`.venv/bin/python bench/run.py run` (equivalently `uv run python
bench/run.py run`) exits **0**: **24/24 cases accepted** —
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

**Wave-3 W3-8(a) (SUB-34)**: rubric scoring now excludes the `provenance`
field from the scored payload text on BOTH sides (system response and
compliant ceiling) via `rubric_scoring_text` — per the spec, provenance is
routing metadata with zero relevance, so a rubric term present only in a
`soul://` URI filename can no longer count as delivered knowledge (q16's
`steelman` was satisfiable by `09-premortem-steelman.md`'s *filename* alone).
Provenance still counts toward token estimation on both sides. Fresh ledger
`bench/results/ledger-20261025T235210Z.json` (repeats=5, exit 0, **24/24
accepted**): **0/24 cases changed** in `quality_system`, `quality_ceiling`,
`system_estimated_tokens`, `token_saving_ratio`, `gap_vs_raw_read`,
`ceiling_node_ids` or `ceiling_method` versus `20261025T233953Z` (verified
programmatically); the gate record carries an explicit `rubric_scope` note.
q16 stays 0.75 = ceiling — its `steelman` hit is content-delivered
(`test_q16_steel_man_hit_is_content_delivered`).

| Metric | Source of truth | Value | Evidence |
|--------|-----------------|-------|----------|
| Frozen corpus | `bench/corpus/manifest.json` | 28 files, 52,604 bytes, 13,151 est. tokens | `freeze: 28 files, 52604 bytes` exit 0; matches contract baseline tok-011 |
| Frozen questions | `bench/cases.json` | 24 cases, all 7 domains covered | `check: OK` exit 0 |
| Estimator parity | `tests/fixtures/token_budget_cases.json` | 7/7 payload fixtures match | `TestTokenEstimatorParity` |
| Repeats | bench ledger | 5 per case | ledger `repeats: 5` |
| Latency p50 (per case, across 24 cases) | bench ledger | median **9.69 ms** (min 4.82, max 16.70; host-dependent, re-measured 2026-10-26 ~12:29 UTC) | ledger `latency_ms.p50` |
| Latency p95 (per case, across 24 cases) | bench ledger | median **12.15 ms** (max 39.74; host-dependent) | ledger `latency_ms.p95` |
| Token savings (paired) | bench ledger | mean **94.18%** (min 92.64%); system 420–968 est. tokens vs 13,151 raw-corpus baseline | ledger `token_saving_ratio` |
| Compliant ceiling | bench ledger | per-case 0.0–1.0, mean **0.4042**; 18/24 cases below the 0.6 floor (`ceiling_below_floor`, enrichment debt) | ledger `quality_ceiling`, `ceiling_method: exact-full` (q03: `exact-full+single-root`) |
| Quality verdicts | bench ledger | **24/24 accepted**; quality mean **0.4451**; q03 vacuous-accepted (annotated, `ceiling_unrestricted_2.0.0: 0.333333` preserved) | ledger `quality_system`, `quality_ceiling`, `verdict`, `ceiling_routing_note` |
| Quality gate behavior | bench ledger | every below-ceiling case is rejected *despite* ≥92% token savings — the gate works; semantics re-verified fresh on a 24/24 run | `TestBenchmarkQualityGate` (1 passed, exit 0, fresh 2026-10-26; full e2e file 49 passed, 0 skipped) |

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
| Counted coverage | **40/40 = 100%** (row 23 residual closed in `71f7547`) | matrix rule: partials count only once residual closed and test passes |
| Coverage gate (≥ 90%) | **PASS** | 100% ≥ 90% |
| Partial residual 1 | Row 22 (schema/version provenance) — **closed at Task 3** (event log is source of truth; deterministic rebuild migrates) | matrix §3.2 note; named test passes |
| Partial residual 2 | Row 23 (deterministic serialization + atomic writes) — **CLOSED** in `71f7547` (SUB-29): staged-backup fsync, two-phase backup activation, orphan-temp sweep; 12 crash-injection tests green | `tests/test_power_loss_recovery.py` (12 passed); `docs/recovery/` row-23 durability record; matrix row 23 Covered?=yes |
| Partial residual 3 | Row 38 (token-reduction benchmark) — residual owner Task 10: **closed at integration** by the delivered harness (N repeats, p50/p95 latency, quality rubric, paired ledger); the gate it measured (finding T10-1) is now green — gate 2.1.0, exit 0, 24/24 (§2) | `bench/run.py`, ledger `ledger-20261025T202627Z`, `tests/test_service.py::test_canonical_token_parity` passes |

Matrix doc correction at integration: three named-test cells (rows 25/36/40)
were corrected to the tests that actually landed (SUB-12 added the row-25
test to `tests/test_operations.py` and the row-36/40 tests to
`tests/test_security.py`; the matrix cells and a Task 9 verification note
record this).

## 4. Acceptance criteria traceability

Every Task 10 acceptance bullet is a named test in
`tests/test_acceptance_e2e.py`. Status at integration: **48 passed,
1 skipped** (exit 0); after the wave-3 W3-11 closure: **49 passed,
0 skipped** (exit 0, fresh).

| Acceptance bullet | Test | Status |
|-------------------|------|--------|
| Seven domains routed | `TestSevenDomainsEndToEnd` | **PASS** (real, frozen-corpus system) |
| Exactly ten audited tools (v1) — **superseded: fourteen tools under contract v2** (operator ruling 2026-10-26; addendum `docs/contracts/retrieval-verbs-v2.md`; the ten v1 operations unchanged) | `TestAuditedTools` | **PASS** (2 tests, updated to the fourteen-op pin) |
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
| No Hermes source modification | `TestNoHermesSourceModification` + `test_live_soul_corpus_unmodified_since_freeze` | **PASS** — both sides run for real: corpus-side pin green; install-tree check verifies HEAD == `v0.22.5^{commit}`, clean `git status --porcelain`, empty stash against `/root/.hermes/hermes-agent` (wave-3 W3-11 closure; skip-gates only when no git-managed install exists) |
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
| W3-7 (ops-path hardlink/TOCTOU) | minor (security, bounded) | **CLOSED** — multi-linked regular files refused (`st_nlink > 1`); post-operation re-resolution detects parent-swap; 7 non-vacuous probes | `docs/recovery/wave3-ops-timeout-rubric-external.md` |
| W3-9 (`body_timeout_seconds=0` wedge) | minor (config) | **CLOSED** — non-positive body deadlines refused at startup before any socket is bound | `docs/recovery/wave3-ops-timeout-rubric-external.md` |
| W3-8(a) (rubric counts provenance filenames) | minor (measurement) | **CLOSED** — provenance excluded from rubric scoring text on both sides; 0/24 per-case numbers changed; bench exit 0, 24/24 | `docs/recovery/wave3-ops-timeout-rubric-external.md` |
| W3-11 (external test was a vacuous stub) | minor (evidence integrity) | **CLOSED** — real git-clean/tag-pin check, runs and passes on this host; proven non-vacuous against a dirty synthetic tree | `docs/recovery/wave3-ops-timeout-rubric-external.md` |
| SUB48-1 (`publish_capacity_bytes` override above `ARTIFACT_MAX_BYTES` disarmed F-3) | minor (fail-closed guard) | **CLOSED** — construction rejects overrides above the cap with typed `InvalidPublishCapacity` (fail-closed); live-probe value `2**20` rejected; boundary value stays constructible | `docs/recovery/sub52-final-residual-closures.md` |
| F-SUB51-1 (`inspect_backup` leaked untyped `sqlite3.DatabaseError` on non-SQLite files) | important (typed error contract) | **CLOSED** — first pragma wrapped; `BackupError("... is not a SQLite database")`; `restore_store` keeps its documented typed contract | `docs/recovery/sub52-final-residual-closures.md` |
| F-SUB51-2 (dead `PayloadTooLarge` branch in `error_envelope`) | cosmetic (hygiene) | **CLOSED** — branch removed; behavior pinned unchanged (code/`retryable`/`audit_event_id`) | `docs/recovery/sub52-final-residual-closures.md` |

## 6. External check (wave-3 W3-11 closure — no vacuous skips remain)

`TestNoHermesSourceModification::test_hermes_source_tree_is_unmodified` is a
**real test**: it verifies the installed Hermes source tree
(`/root/.hermes/hermes-agent`) is a clean checkout of the pinned release tag
(`HEAD == v0.22.5^{commit}`, empty `git status --porcelain`, empty stash
list). On hosts with the installation it runs and passes (verified fresh:
HEAD `bf0203a794a07aac3ff764972ab9087baa4e4f4e` == tag, clean tree); on hosts
without a git-managed installation it skip-gates at runtime — an honest
"not applicable", never a silent pass. The corpus-side pin (live SOUL corpus
unchanged since freeze) runs for real in `TestFrozenCorpus` and passes.

## 7. ATM export/analytics integration (SUB-70)

| Gate | Command | Exit code | Evidence | Date (UTC) |
|------|---------|-----------|----------|------------|
| Integration tests | `.venv/bin/pytest tests/test_builtin_integration.py tests/test_export_cli.py -q` | **0** | `24 passed` (13 new + 11 CLI tests (registry reference updated)) | 2026-10-26 |
| Full suite | `.venv/bin/python -m pytest tests/ -q` | **0** | `2025 passed, 1 warning in 251.62s` | 2026-10-26 |
| Lint/type | `ruff check src/ tests/` + `ruff format --check` + `pyright src tests` | **0** | `All checks passed!` / 147 files formatted / `0 errors, 0 warnings` | 2026-10-26 |
| Bench | `python bench/run.py check` | **0** | `check: OK (cases schema, frozen corpus integrity, estimator parity)` | 2026-10-26 |
| E2E export | `know-your-soul --store bench.sqlite3 --output-root graph export --formats all` | **0** | 16 format artifacts on disk, sha256 per artifact in summary; re-run all `written: false` | 2026-10-26 |
| E2E analytics | `know-your-soul ... analytics --modules all` | **0** | 14 analytics artifacts; timeline `events_processed` == store domain events; report staleness `{known: true, stale: false}`; health `{ok: true, score: 1.0}` | 2026-10-26 |
| E2E diff/timeline (2 generations) | real `record-outcome` + `rebuild` + `analytics --modules generation-diff,timeline` | **0** | diff `status: "diff"` (36→36 nodes, 35→35 edges); timeline point P=`1.000000000000`, level 2 | 2026-10-26 |
| Store read-only | `SELECT COUNT(*) FROM event_log` before/after export+analytics | — | **74 / 74** — no write, no rebuild | 2026-10-26 |
| Frozen surface | `test_export_and_analytics_are_not_knowledge_or_http_ops` | **0** | export/analytics ∉ 14 knowledge ops, ∉ OPS_OPERATIONS, no MCP/HTTP dispatch | 2026-10-26 |

The full artifact listing (30 names with byte sizes and sha256 from the real
run) is in `docs/recovery/atm-integration-final.md` §"Tabel artifact".
Usage: `docs/operations.md` §"Export and analytics artifacts (ATM)".

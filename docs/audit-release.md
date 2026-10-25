# Audit and Release Record — Super Power Knowledge SOUL

Status: **INTEGRATED (Task 10, SUB-10, 2026-10-25 UTC). Release decision:
ALL LAUNCH BLOCKERS CLOSED (T10-1 closed 2026-10-25 ~20:30 UTC by SUB-25 +
SUB-26; bench gate 2.1.0 exits 0, 24/24) — final release decision and the
`docs: publish verified release evidence` commit remain with the coordinator
after the final whole-branch review.**

This document records the independent audit of the built system, the
classification of every finding, the recovery loop for launch blockers, and
the release decision. Every line cites fresh command output or a named
artifact.

## 1. Audit scope

| Item | Value |
|------|-------|
| Audited revision | `e1835b8` (+ uncommitted Task 10 integration changes: acceptance/runtime-guard tests, `test_scoring.py` typing fix, repo-wide `ruff format` normalization) |
| Auditors | SUB-8 (security + correctness wave 1, at `3e60a2b`), SUB-9 (spec-compliance wave 1, at `3e60a2b`), SUB-10 (integration gates + benchmark at `e1835b8`) |
| Audit date (UTC) | 2026-10-25 |
| Spec | `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md` |
| Plan | `docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md` |
| Verification evidence | `docs/verification.md` (all gates fresh, exit codes recorded) |

## 2. Findings

Classification: **launch-blocking** (release impossible until closed),
**fast-follow** (scheduled immediately after release), **track** (recorded,
no committed date).

| Finding ID | Class | Summary | Affected global criterion | Status |
|------------|-------|---------|---------------------------|--------|
| F1 | launch-blocking | Quarantined evidence still scores after RESET | relearn contract | **CLOSED — verified, no patch needed** (audit-wave1-security.md §F1; SUB-11 verified at wiring `3a90f51`; acceptance probe `TestRelearn::test_relearn_events_carry_zero_evidence_weight` passes fresh) |
| F2 | fast-follow | Relearn apply retry after success raised NoPendingRelearn | idempotent retry | **CLOSED** in `87d6fdf` |
| F3 | launch-blocking | Idempotency-key collision across operations silently dropped writes | idempotent retry / audit | **CLOSED** in `87d6fdf` (fails loud with IdempotencyConflict) |
| F4 | fast-follow | Secret shapes outside validator regex reached graph.json unredacted | security | **CLOSED** in `beca29c` |
| F5 | fast-follow | Corpus symlink escape (resolve_within_root unwired) | security | **CLOSED** in `beca29c` |
| F6 | track | Raw bidi/zero-width persisted in published artifacts | security | **CLOSED** in `beca29c` (publish-time sanitization) |
| T10-1 | **launch-blocking** | Benchmark quality gate rejected (nearly) all cases despite ~94% token savings | benchmark quality gate: token savings never excuse quality shortfalls | **CLOSED** — root cause split in three: (a) *gate miscalibration* — gate 1.0.0 demanded raw-read parity (quality == 1.0), provably unreachable for any contract-compliant response (compliant ceiling measured 34/83 terms ≈ 0.41) — **CLOSED (SUB-22)** by gate 2.0.0: acceptance vs the per-case compliant ceiling computed from the live projection artifact + `min(floor, ceiling)`; raw baseline kept as `gap_vs_raw_read` (docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md); (b) *2 genuine retrieval defects* the recalibrated gate isolated: q05 (0 vs 0.5; routing `ambiguous` P2/P7 tie — dead node_id field on slug segments) and q16 (0.5 vs 0.75; misrouted P6 vs P4 — undiscounted top-3 sum) — **CLOSED (SUB-25, commit `4091aef`)**: node-id slug-segment indexing + reciprocal-rank root aggregation, both RED-first probed (docs/recovery/task-10-retrieval-q03-q05-q16.md); (c) *q03 ceiling-semantics defect* — gate 2.0.0's ceiling (0.3333) demanded a cross-root result set {07 (P1), 08 (P5)} no compliant single-root response can assemble — **CLOSED (SUB-26)** by gate 2.1.0 (Ruling Q03): ceiling candidates restricted to the single-root routing-feasible set, q03 vacuous-accepted with explicit annotation, 23/23 other ceilings verified byte-identical (docs/recovery/task-10-ceiling-single-root-ruling-q03.md; ledger `ledger-20261025T202627Z`, exit 0, 24/24) |
| T10-2 | track | Matrix row 23 residual open: whole-set durability / power-loss recovery | Graphify coverage accounting | OPEN with Task 9 lineage; coverage counted 39/40 = 97.5% (gate ≥90% passes) |
| T10-3 | track | `importer.py` reads file bytes without containment/size bound (F5 residual, flagged by SUB-12) | security defense-in-depth | OPEN, flagged to coordinator |
| T10-4 | — | Full-tree pyright error `tests/test_scoring.py:132` (typed literal) | type gate | **CLOSED** at integration (`Decimal("0")`); pyright `0 errors` exit 0 |
| T10-5 | — | `ruff format` drift across 16 committed files | format gate | **CLOSED** at integration (mechanical `ruff format`; suite re-verified green, 566 passed) |

## 3. Recovery loop (launch blockers only)

For every launch blocker, evidence, root cause, the affected global
criterion, the smallest fix, the regression probe, and rollback are recorded.
Wave-1 blockers F1/F3 closed with TDD probes in `tests/test_audit_wave1_*.py`
(see `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave1-security.md`).

Stop rule: three recovery iterations without verified improvement, or any fix
that violates a global constraint, stops execution for operator escalation.
Not triggered: F1/F3 closed in one iteration each; T10-1 closed in four
iterations (each with verified improvement: 4/24 → 21/24 → 23/24 → 24/24).

| Finding ID | Recovery doc | Iterations | Reviewer verification | Closed (UTC) |
|------------|--------------|-----------|------------------------|--------|
| F1 | audit-wave1-security.md §F1 | 1 | SUB-11 probes: F1 regression PASS at wiring `3a90f51`; SUB-10 acceptance probe green | 2026-10-25 |
| F3 | audit-wave1-security.md §F3 | 1 | SUB-11 RED→GREEN probes; bench build regression observed by SUB-10 mid-flight, re-verified green after commit | 2026-10-25 |
| T10-1 | `docs/recovery/task-10-T10-1-opsi-c-gate-recalibration.md` + `docs/recovery/task-10-retrieval-q03-q05-q16.md` + `docs/recovery/task-10-ceiling-single-root-ruling-q03.md` + ledgers `ledger-20261025T190544Z` (gate 1.0.0) / `ledger-20261025T192059Z` (gate 2.0.0, 21/24) / `ledger-20261025T200345Z` (gate 2.0.0 + retrieval fixes, 23/24) / `ledger-20261025T202627Z` (gate 2.1.0, 24/24, exit 0) | 4 ((1) routing `c410fa4` + Opsi A enrichment `2cd51ea`; (2) Opsi C gate recalibration; (3) retrieval fixes q05/q16 `4091aef`; (4) Ruling Q03 ceiling correction, gate 2.1.0) | gate-calibration half CLOSED by SUB-22 with unreachability proof (ceiling 34/83 ≈ 0.41 < 1.0); retrieval defects CLOSED by SUB-25 with RED-first probes; q03 CLOSED by SUB-26 under controller Ruling Q03 — q03 is vacuous-accepted (`case-design debt`, annotated in the ledger), **not** measured-as-delivered: its `gap_vs_raw_read` stays 1.0; anti-Goodhart verified: 23/23 other ceilings byte-identical | 2026-10-25 |

## 4. Release gate

Release requires **all** of:

- [x] Full `pytest -q` green with no integration-pending acceptance skips
      remaining (`632 passed, 1 skipped`, exit 0, fresh 2026-10-25 ~20:30 UTC;
      the one skip is the external
      Hermes-install check — `docs/verification.md` §1/§6).
- [x] Type checking (`pyright` exit 0), lint (`ruff check` exit 0), format
      (clean on all changed files; the single repo-wide `ruff format --check`
      offender is `docs/mathematics/scoring.md`, committed drift reproduced
      at clean HEAD — pre-existing, not introduced here), dependency audit
      (`uv lock --check` exit 0) — fresh exit codes in
      `docs/verification.md` §1.
- [x] **`python bench/run.py run` exit 0 — MET: ledger status `ok`, 24/24
      cases accepted under gate 2.1.0 (Opsi C + Ruling Q03).** 23 cases at
      their byte-identical gate 2.0.0 ceilings; q03 vacuous-accepted with the
      explicit ledger annotation
      `vacuous: rubric unreachable under expected routing (case-design debt)`.
      Ledger: `bench/results/ledger-20261025T202627Z.json`.
- [x] Graphify coverage ≥ 90% of the applicable 40-feature inventory, counted
      only from matrix rows whose owning contract tests pass (39/40 = 97.5%;
      the three partial residuals reported explicitly in
      `docs/verification.md` §3).
- [x] Zero open launch-blocking findings — **MET (T10-1 closed; §2)**.
- [ ] Final commit `docs: publish verified release evidence` made only after
      the final fresh verification run — pending coordinator.

## 5. Release decision

| Item | Value |
|------|-------|
| Decision | **ALL LAUNCH BLOCKERS CLOSED** — final release decision passes to the coordinator's whole-branch review (board, 2026-10-25 ~20:15 UTC); the previous NO-RELEASE trigger (T10-1) is closed |
| Release commit | — (coordinator owns the `docs: publish verified release evidence` commit after the final whole-branch review) |
| Fast-follow items | none open (F2/F4/F5 closed in wave-1 fix round) |
| Track items | T10-2 (row 23 residual, Task 9), T10-3 (importer.py unbounded read), external Hermes-install verification at release time; case-design debt (non-blocking, ledger-annotated): q03 rubric unreachable under expected routing (vacuous-accepted), q07 ceiling feasible only via actual P7 routing vs expected P2 |
| Unmeasured claims (explicit) | (1) NL answer quality vs the raw-corpus read — measured gap remains (mean `gap_vs_raw_read` 0.5549, ledger 202627Z); against the achievable compliant ceiling the system delivers 24/24, one of them (q03) vacuous-accepted as case-design debt, not delivered quality. (2) Latency claims limited to 5 repeats on this host (p50 median 6.43 ms, p95 median 7.43 ms). (3) Hermes source-tree non-modification verified corpus-side only; install-tree check pending at release. (4) Token savings (mean 94.26%) measured against the raw-corpus baseline; they do not imply raw-read-equivalent answers while `gap_vs_raw_read` > 0. |

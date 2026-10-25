# Audit and Release Record — Super Power Knowledge SOUL

Status: **INTEGRATED (Task 10, SUB-10, 2026-10-25 UTC). Release decision:
NO-RELEASE — one launch-blocking finding open (T10-1, benchmark quality).**

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
| T10-1 | **launch-blocking** | Benchmark quality gate: 0/24 cases accepted (quality mean 0.0778 vs baseline 1.0) despite ~96% token savings | benchmark quality gate: token savings never excuse quality regression | **OPEN** — routing fixed in `c410fa4`; response payloads carry summaries, not section content, so corpus-grounded rubric terms are absent. Owner: retrieval/service quality line (Agent-2) |
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
Not triggered: F1/F3 closed in one iteration each; T10-1 is at iteration 1
(routing fixed, content depth open).

| Finding ID | Recovery doc | Iterations | Reviewer verification | Closed (UTC) |
|------------|--------------|-----------|------------------------|--------|
| F1 | audit-wave1-security.md §F1 | 1 | SUB-11 probes: F1 regression PASS at wiring `3a90f51`; SUB-10 acceptance probe green | 2026-10-25 |
| F3 | audit-wave1-security.md §F3 | 1 | SUB-11 RED→GREEN probes; bench build regression observed by SUB-10 mid-flight, re-verified green after commit | 2026-10-25 |
| T10-1 | this file §2 + `bench/results/ledger-20261025T151538Z.json` | 1 (routing iteration `c410fa4` improved routing, not payload quality) | pending fresh reviewer | — |

## 4. Release gate

Release requires **all** of:

- [x] Full `pytest -q` green with no integration-pending acceptance skips
      remaining (`566 passed, 1 skipped`, exit 0; the one skip is the external
      Hermes-install check — `docs/verification.md` §1/§6).
- [x] Type checking (`pyright` exit 0), lint (`ruff check` exit 0), format
      (`ruff format --check` exit 0), dependency audit (`uv lock --check`
      exit 0) — fresh exit codes in `docs/verification.md` §1.
- [ ] **`python bench/run.py run` exit 0 — NOT MET: exit 1, ledger status
      `failed`, 0/24 cases accepted (quality gate; T10-1).** The ledger is
      committed-class evidence: `bench/results/ledger-20261025T151538Z.json`.
- [x] Graphify coverage ≥ 90% of the applicable 40-feature inventory, counted
      only from matrix rows whose owning contract tests pass (39/40 = 97.5%;
      the three partial residuals reported explicitly in
      `docs/verification.md` §3).
- [ ] Zero open launch-blocking findings — **NOT MET (T10-1 open)**.
- [ ] Final commit `docs: publish verified release evidence` made only after
      the final fresh verification run — pending coordinator.

## 5. Release decision

| Item | Value |
|------|-------|
| Decision | **NO-RELEASE** (one launch blocker: T10-1 benchmark quality) |
| Release commit | — (coordinator owns the `docs: publish verified release evidence` commit after the gate goes green) |
| Fast-follow items | none open (F2/F4/F5 closed in wave-1 fix round) |
| Track items | T10-2 (row 23 residual, Task 9), T10-3 (importer.py unbounded read), external Hermes-install verification at release time |
| Unmeasured claims (explicit) | (1) NL answer quality — measured and FAILING (T10-1), no quality claim is made. (2) Latency claims limited to 5 repeats on this host (p50 median 4.04 ms, p95 median 5.16 ms). (3) Hermes source-tree non-modification verified corpus-side only; install-tree check pending at release. (4) Token savings (mean 95.95%) measured against the raw-corpus baseline; they do not imply usable answers while T10-1 is open. |

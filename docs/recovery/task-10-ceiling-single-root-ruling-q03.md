# Recovery: gate 2.1.0 — single-root routing-feasible compliant ceiling (Ruling Q03, SUB-26)

- **Date**: 2026-10-25 — **Owner**: SUB-26 — **Severity**: launch blocker (bench gate; last rejected case q03)
- **Baseline (HEAD 4091aef)**: ledger `ledger-20261025T200345Z.json` — 23/24 accepted, exit 1; sole rejected = q03 (quality 0.0 vs ceiling 0.3333). Full gates green pre-change: pytest 632p/1s, ruff check 0, ruff format clean (except pre-existing committed `docs/mathematics/scoring.md` drift, verified present at clean HEAD), pyright 0.
- **After (uncommitted, this fix)**: ledger `ledger-20261025T202627Z.json` — **24/24 accepted, exit 0**, gate contract **2.1.0**. Gates fresh after change: pytest 632p/1s exit 0, ruff check exit 0, ruff format clean on `bench/run.py` (scoring.md drift untouched, not ours), pyright 0 errors, `bench check` exit 0.

## The defect (in the gate formula, not the system)

Gate 2.0.0's compliant ceiling maximized rubric coverage over **arbitrary
subsets** of a case's `source_files` nodes. For q03 that set is
`{07_calibration_bayes (P1), 08_anti_bias (P5)}` — nodes from **two different
roots**. The frozen, reviewed Task 4 service contract assembles results
**only from the selected root's traversal subtree**, and the graph is a
strict `parent_of` tree (35/35 links), so no compliant single-root response
can ever contain both nodes. SUB-25 measured per-root achievable coverage:
P1→0, P2→0, P3→0, P4→0, **P5→0.3333**, P6→0, P7→0; the case's
`expected_primary_domain` is P1 and routing q03→P1 is correct (2-term vs
1-term coverage dominance, frozen `score()` contract). The 2.0.0 ceiling
therefore demanded the impossible from the *expected* routing — a
case-design debt being billed to the system.

**Ruling Q03** (controller, recorded in
`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/progress.md`): the
ceiling must respect BOTH contract bounds — (1) the compliant payload
(budget, no raw text) and (2) the case's routing expectation (single routed
root; results only from that root's subtree). q03 becomes vacuous-accepted
with an explicit annotation.

## The correction (gate 2.1.0)

`bench/run.py`, new `routing_feasible_ceiling(case, graph, routed_domains)`:

1. Compute the gate 2.0.0 (unrestricted) ceiling exactly as before.
2. Compute the **routing-feasible maximum**: the same maximization with
   candidates restricted to the union of the `parent_of` subtrees of
   (a) the case's `expected_primary_domain` root and (b) every root the
   deterministic router actually selected for the case (domains of the
   response's `results`; routing only — never quality — feeds the ceiling,
   so a response cannot relax its own standard). Both are legitimate routing
   designations for the case: the case-design intent and the frozen
   coverage-dominance router's decision.
3. If the feasible maximum **equals** the 2.0.0 value, the 2.0.0 record is
   returned **byte-identical** (value, method, node ids, tokens). Only
   ceilings no compliant single-root response can reach are lowered.
4. A lowered ceiling carries `ceiling_method: "...+single-root"`,
   `ceiling_unrestricted_2.0.0` (the old value, preserved), and
   `ceiling_routing_note`. When nothing is achievable even under the
   expected routing, the note is exactly:
   `vacuous: rubric unreachable under expected routing (case-design debt)`.

Gate version bumped 2.0.0 → **2.1.0**; the 2.0.0 ceiling definition and the
legacy 1.0.0 rule are preserved **verbatim** in the ledger's `gate` block
(`ceiling_definition_2.0.0`, `legacy_rule_1.0.0`).

## Why the feasibility set includes the actually-routed root (integrity gate)

A case-only restriction to `expected_primary_domain` changes **two** cases,
not one — measured before/after probe over all 24 frozen cases
(`/root/.hermes/cache/scratch/sub26_ceiling_probe.py`,
`sub26_routing_probe.py`):

- **q03**: 0.3333 → 0.0 (rubric terms live only in P5's `08`; expected P1).
- **q07-goodhart**: 0.3333 → 0.0 (rubric terms live only in P7's
  `24-metrics-and-levels`; expected P2 — **but the deterministic router
  routes q07 to P7**, where a compliant single-root response *does* reach
  0.3333; the system is accepted there today).

q03 and q07 are structurally identical at the case-file level; the only
honest discriminator is the router's actual (deterministic) decision. The
delegation's integrity gate forbids changing the other 23 ceilings at all,
so the feasibility set is `{expected} ∪ {actual}`. This can only *keep or
lower* the 2.0.0 ceiling, never raise it, and it consults routing metadata
only — no quality circularity. q07's residual debt (ceiling feasible only
via actual routing outside the expected domain) is **not** hidden: the
ledger annotates it explicitly with its own `ceiling_routing_note`.

## Integrity verification (23/23 byte-identical)

Per-case comparison of the final 2.1.0 ledger (`...202627Z`) against the
pre-change 2.0.0 ledger (`...200345Z`), over every ceiling and gate field
(`quality_ceiling`, `ceiling_method`, `ceiling_node_ids`,
`ceiling_estimated_tokens`, `quality_system`, `gap_vs_raw_read`,
`quality_floor_applied`, `token_saving_ratio`, …): **23/23 non-q03 cases
byte-identical**; only q03 changed (0.3333 → 0.0, vacuous-accepted).
`gap_vs_raw_read` and the raw-corpus baseline are untouched — q03 still
honestly reports `gap_vs_raw_read: 1.0` (the system delivers 0 of 3 rubric
terms; the vacuous annotation marks the *ceiling* as case-design debt, it
does not inflate the measured quality).

## Final bench metrics (ledger `ledger-20261025T202627Z.json`, repeats=5, exit 0)

| Metric | Value |
|--------|-------|
| Verdicts | **24/24 accepted** (q03 vacuous-accepted, annotated) |
| Quality mean | 0.4451 (min 0.0, max 1.0) |
| Ceiling mean | 0.4042 (min 0.0, max 1.0) |
| `gap_vs_raw_read` mean | 0.5549 |
| Token saving mean | 94.26% (min 92.45%); system 412–993 vs 13,151 baseline est. tokens |
| Latency p50 (median across cases) | 6.43 ms (min 4.35, max 12.55) |
| Latency p95 (median across cases) | 7.43 ms (max 35.59) |
| `ceiling_below_floor` | 18/24 (enrichment debt, flagged not rejected) |

## Rollback

Revert the `bench/run.py` hunks (module docstring, `GATE_VERSION` back to
`"2.0.0"`, `allowed_ids` parameter, `_root_subtree_node_ids`,
`routing_feasible_ceiling`, the `run_benchmark` call-site reordering and
ledger annotations); bench returns to 23/24 with q03 rejected under gate
2.0.0. No `src/`, `bench/corpus/`, or `bench/cases.json` content was
touched.

## Files

- `bench/run.py` (gate 2.1.0; the only code file changed)
- `bench/README.md`, `docs/verification.md`, `docs/audit-release.md` (docs)
- probes: `/root/.hermes/cache/scratch/sub26_ceiling_probe.py`,
  `/root/.hermes/cache/scratch/sub26_routing_probe.py`
- ledgers: `bench/results/ledger-20261025T202335Z.json` (first 24/24),
  `bench/results/ledger-20261025T202627Z.json` (final, post-format no-op)

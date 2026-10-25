# Recovery: bench gate 2.0.0 residuals q03/q05/q16 — retrieval mechanism fixes (SUB-25)

- **Date**: 2026-10-25 — **Owner**: SUB-25 — **Severity**: launch blocker (bench gate 2.0.0)
- **Baseline (HEAD 42a8794)**: ledger `ledger-20261025T194640Z.json` — 21/24 accepted, exit 1; rejected: q03 (quality 0 vs ceiling 0.333), q05 (0 vs 0.5, ambiguous P2/P7), q16 (0.5 vs 0.75, misroute P6). Full gates green pre-change: pytest 628p/1s, ruff 0, format clean, pyright 0.
- **After (uncommitted, this fix)**: ledger `ledger-20261025T200345Z.json` — **23/24 accepted, exit 1**; q05 and q16 accepted *at their exact ceilings* (0.5/0.5, 0.75/0.75); mean quality 0.4139 → 0.4451; only q03 remains rejected. Gates fresh: pytest 632p/1s (25→31 retrieval tests, +4 new), ruff check 0, ruff format clean, pyright 0.

## q05 (FIXED) — dead node_id field made a genuine content signal invisible

- **Evidence**: query terms `heuristic, forbid, removing, something, understanding, exist`; only two hits — `06_heuristics_catalog` matches `heuristic` (df=1) and `27_part4_usage_and_schema` matches `exist` (df=1). Identical IDF ⇒ exact tie 3.917770732084279 = 3.917770732084279, both summary-only ⇒ `ambiguous (P2, P7)`, empty results, quality 0.
- **Root cause**: `tokenize` uses `[\w]+` and `\w` **includes underscore**, so every real corpus slug (`06_heuristics_catalog`) indexed as ONE token. The `node_id` field — the contract's strongest field (`FIELD_WEIGHTS["node_id"]=4`) — therefore never produced a term match for any multi-word id. The tie was *not* genuine: the stable id names the concept (`heuristics` → normalize → `heuristic`); the signal existed but the field was dead.
- **Fix (retrieval.py, `LexicalIndex.from_graph_nodes`)**: index the slug's underscore-separated segments (`tokenize(node_id.replace("_", " "))`). Same four searchable fields (spec 13.1 untouched); the tie disappears by *content* (node_id boost 1.15), never by graph order. After: q05 routes P2, payload {P2, 06} covers "Chesterton's Fence" → quality 0.5 = ceiling → accepted.
- **Regression probes**: `test_node_id_slug_segments_are_indexed_terms`, `test_node_id_slug_segment_breaks_a_cross_root_tie_by_content` (both RED pre-fix, synthetic nodes, no bench-case hardcoding).

## q16 (FIXED) — undiscounted top-3 sum rewarded weak padding

- **Evidence**: `09_premortem_steelman` (P4) is the single strongest hit (17.2738, 5-term coverage); P6 aggregated `21_completion_criteria` (14.1670) + `12_communication` (3.5123, one common term `delivery`) = 17.6793 > 17.2738 → misroute P6, quality 0.5.
- **Root cause**: a *plain* top-three sum lets a root whose top hit is weaker win purely because a weak single-term secondary hit pads the total.
- **Fix (retrieval.py, `PrimaryRouter.route`)**: reciprocal-rank discount inside the same bounded top-3 window — hit at rank i weighs `score/i` (1, 1/2, 1/3). Breadth still wins when supporting hits are strong; weak padding no longer beats the strongest hit. Deterministic Decimal (prec 34), no per-case logic. After: P4 17.2738 > P6 15.9232 → route P4 → payload covers `pre-mortem, steelman, mitigation` → 0.75 = ceiling → accepted.
- **Regression probes**: `test_root_aggregation_discounts_supporting_hits_by_reciprocal_rank` (RED pre-fix: plain sum ties → ambiguous), `test_root_aggregation_still_rewards_comparable_breadth` (green both sides; pins that breadth is damped, not killed). Pre-existing `test_root_aggregation_is_bounded_by_its_three_strongest_hits` stays green unchanged.
- **Full-corpus diff** (probe over all 24 frozen cases, before vs after): only q05 (ambiguous→P2), q16 (P6→P4), and q23's token count (981→975, same route/quality) changed; the other 21 cases are byte-identical in route, results count and quality.

## q03 (DOCUMENTED, NOT FIXED IN SCOPE) — structural: ceiling assumes cross-root assembly

- **Evidence** (fresh probe, this HEAD + fix): terms `countermeasure, confidence, claim, external, evidence, behind`. Ranked hits: `07_calibration_bayes` (P1) 7.8355 (`confidence`+`evidence`, 2 terms), `08_anti_bias` (P5) 3.9178 (`countermeasure`, 1 term), `12/21` (P6) 3.5123 each (`claim`). Routing P1 is correct under every honest mechanism: coverage dominance is the frozen `score()` contract, the case's own `expected_primary_domain` is P1, and the margin is 2:1.
- **Structural bound (measured)**: the graph is a strict `parent_of` tree (35/35 links), and the frozen `service.py` assembles results exclusively from the *routed root's traversal subtree*. Rubric coverage from every possible single root's subtree: P1→0, P2→0, P3→0, P4→0, **P5→0.3333** (`overconfidence`, via `08_anti_bias`), P6→0, P7→0. The ceiling (0.3333, nodes {07, 08}) requires nodes from **two different roots in one response** — impossible for any compliant single-root query.
- **Conclusion**: q03's gap is not a retrieval ranking bug. It is either (a) a service result-assembly limitation (no cross-root ranked inclusion — `service.py` is frozen for SUB-25), or (b) a ceiling-semantics issue (ceiling maximizes over arbitrary source-file node subsets that no single-root response can produce — bench/ is frozen). Fixing it from `retrieval.py` would require deliberately misrouting a 2-term-coverage query to a 1-term root — a per-case Goodhart patch, forbidden by the work rules and by the coverage-dominance contract.
- **Escalation**: coordinator adjudicates between allowing a `service.py` change (include top-ranked cross-root hits within budget) or recalibrating q03's ceiling to the single-root reachable maximum (0.0). No unilateral action taken.

## Rollback

Revert the two hunks in `src/soul_knowledge/retrieval.py` (slug-segment tokenization in `from_graph_nodes`; reciprocal-rank discount in `PrimaryRouter.route`) and the four added tests in `tests/test_retrieval.py`; bench returns to 21/24.

## Files

- `src/soul_knowledge/retrieval.py` (2 mechanism fixes, commented)
- `tests/test_retrieval.py` (+4 tests; 27 → 31 passed — 3 RED-first mechanism probes + 1 breadth-preservation probe; all 27 pre-existing contract tests green unchanged)
- probes: `/root/.hermes/cache/scratch/sub25_probe.py`, `sub25_probe24.py`, routing diffs `sub25_routing_before.txt` / `sub25_routing_after.txt`

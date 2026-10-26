# SUB-61 — ATM Wave A2: communities (F-A3) + bridges (F-A5) + review_signals (F-A6)

Date: 2026-10-26 ~19:45 UTC. Base: HEAD `a7f331a`. All modules TDD
(RED → GREEN); every gate number below is from a fresh command on this host.
Clean-room rule (plan §2) held: only *ideas* were taken from Graphify
(`cluster.py`/`analyze.py` feature classes); every name, formula, comment and
line here is original.

## Scope (6 new files, 0 existing files touched)

- `src/know_your_soul/analytics/__init__.py` — minimal package marker.
  `analytics/__init__.py` is assigned to SUB-60, but their branch had not
  landed when these modules needed to import; per the coordination rule a
  minimal marker was created and recorded on the board. If SUB-60's version
  conflicts, theirs wins and this one is dropped.
- `src/know_your_soul/analytics/communities.py`
- `src/know_your_soul/analytics/bridges.py`
- `src/know_your_soul/analytics/review_signals.py`
- `tests/test_analytics_communities.py` / `test_analytics_bridges.py` /
  `test_analytics_review_signals.py` (70 tests total, incl. 4 hypothesis
  property tests, all `derandomize=True`)

## F-A3 `communities.py` — canonical-order label propagation

- **LPA**: asynchronous rounds over nodes in sorted-id order; each node takes
  the weight-max neighbour label, ties break to the lexicographically smallest
  label. Iteration cap `max_iterations=64`; output honestly reports
  `iterations_used` and `converged`.
- **Stable ids**: communities sorted by `(-size, smallest member id)`, numbered
  `0..n-1`; id 0 is always the largest.
- **Deterministic label**: highest-undirected-degree member (self-loops
  discarded), tie-break smallest id; label text = hub's `label` field, falling
  back to the id.
- **Membership sig**: `sha256("\n".join(sorted(members)))[:16]`.
- **Cohesion**: `2·internal_pairs / (n·(n-1))`, self-loops discarded, rendered
  as 12-decimal string via `domain.quantize_score`; singletons vacuously `1.0`.
- **Remap**: greedy one-to-one by overlap in `(-overlap, prev_id, curr_id)`
  order; `label_still_applies` = identical membership OR strict majority
  overlap on BOTH sides (`2·overlap > len(prev)` and `> len(curr)`).
- Determinism pins: input-permutation byte-identity test, cross-`PYTHONHASHSEED`
  subprocess test (seeds 0/1/42/2026 → identical output), sig order-stability
  property test, detection permutation-invariance property test.

## F-A5 `bridges.py` — surprising connections

- Candidates: links crossing primary domains and/or communities (self-loops
  and dangling endpoints excluded). Without a `membership` map the domain
  stands in for the community.
- **Surprise score (our design)**: `(3·distance + 2·rarity + 1·novelty) / 6`
  where distance = BFS hops between the endpoints' communities with the
  candidate's own edge instance removed (multiset adjacency — parallel links
  between the same community pair still count; `-1` unreachable → component
  1), rarity = `(E − count(relation)) / E` over all links, novelty =
  `1 / min(version(s), version(t))`. All components 12-decimal strings.
- Canonical order `(-surprise, source, target, relation)`; `top_n` bound;
  `counts.by_kind` summary.

## F-A6 `review_signals.py` — review candidate catalog

- Fixed catalog order: `low_confidence_edge` (AMBIGUOUS links),
  `isolated_node` (degree 0; self-loop is not connectivity),
  `low_cohesion_community` (size ≥ 3 and cohesion < 0.15), `stale_evidence`
  (node `source_version_id` absent from caller-supplied current versions —
  fail-closed silent without that view), `missing_provenance`.
- Items carry `reason_codes` + `source_node_ids`/`source_node_versions`
  (contract recommendation-contract.md shape), `low_confidence` hint flag
  (primary findings: edge/isolated; hints: cohesion/stale/provenance).
- **Round-robin quota diversification** across signal types in catalog order:
  a noisy signal cannot swallow the whole quota; `signal_counts` always covers
  the full catalog, `emitted_counts` reports what was actually emitted.
- Pure module: no store, no service changes (service.review wiring is a later
  wave's job, per the brief).

## Gates (fresh, this host)

- RED (observed first): `pytest` on the 3 new test files → 3 collection
  errors (`ModuleNotFoundError`), exit=1.
- GREEN: `pytest tests/test_analytics_{communities,bridges,review_signals}.py`
  → **70 passed, exit=0**.
- `ruff check` + `ruff format --check` on the 6 files: **clean, exit=0**.
- `pyright` on the 6 files: **0 errors, 0 warnings, exit=0**.
- Isolation proof (HEAD `a7f331a` worktree `/tmp/sub61-head` + only my files,
  `PYTHONPATH` override): my 3 files + `test_domain` + `test_review` +
  `test_property_scoring_invariants` → **142 passed, exit=0**.

## Shared-tree caveat (not mine)

Full-suite run in the shared tree at ~19:40 UTC: **1706 passed / 29 failed**.
ALL 29 failures trace to sibling WIP, not these modules:

- 28 failures: `matplotlib` + `pillow` were added to `pyproject.toml` dev deps
  and `uv.lock` by a sibling (visual-PNG tooling, `tools/generate_visual_pngs.py`
  untracked at the time), tripping the no-AI dependency guard
  (`ForbiddenDependencyError`) across `test_no_ai_runtime` / `test_mcp_*` /
  `test_dependency_policy` / `test_acceptance_e2e`.
- 1 failure: `test_reader_storm_under_write_stream_bounds_repairs` — timing-
  sensitive under parallel load; passes solo (verified, exit=0).

None of my 6 files is imported by any failing test; the failing set is
identical with my files removed (they are additive and imported only by their
own tests).

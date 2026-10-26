# SUB-47: fixes for the two wave-2 property-test findings

Date: 2026-10-26 · Branch: feature/superpower-knowledge-soul · HEAD at start: 42a9a3e
Findings source: `docs/recovery/codebase-push-property-tests-wave2.md` (§Real findings)

## F-SUB45-1 — relearn anchor nondeterminism across processes (IMPORTANT, fixed)

**Bug.** `RelearnEngine._anchor_path` (`src/soul_knowledge/relearn.py`)
iterated a `set` when the exact-match branch missed. With manifest paths
`{"b.md", "a/b.md"}` and evidence `source_uri="z/a/b.md"` (suffix-matches
BOTH), the chosen anchor flipped with `PYTHONHASHSEED` (verified: seeds
0/1/2/3 → `"b.md"`, seed 42 → `"a/b.md"`). When one candidate anchor is
hash-mapped and the other is not, the same evidence row is carried in one
process and quarantined in another — an ACCUMULATE whose preview (CLI
process) and apply (ops-surface process) run under different seeds could
disagree.

**Fix (src/soul_knowledge/relearn.py).** The suffix scan now iterates
`sorted(manifest_paths, key=lambda p: (-len(p), p))`: deterministic order,
and among several suffix matches the longest (most specific) manifest path
wins. Exact membership still short-circuits. Deterministic by construction —
no dependence on hash randomization.

**Regression tests (tests/test_sub47_wave2_fixes.py +
tests/_anchor_classify_child.py).**

- `test_anchor_path_prefers_the_longest_suffix_match` — unit pin:
  `"z/a/b.md"` over `{"b.md", "a/b.md"}` always anchors `"a/b.md"`;
  shorter-only matches, exact short-circuit, and the `None` case unchanged.
- `test_preview_and_apply_agree_across_hash_seeds` — spawns a real child
  process (`tests/_anchor_classify_child.py`, same pattern as
  `tests/_crash_child.py`) that builds the ambiguous-anchor scenario
  (corpus `b.md` + `a/b.md`; only `a/b.md` changes in v2; evidence
  `e-carry→ext/b.md`, `e-ambig→ext/a/b.md`, `e-exact→a/b.md`) and prints
  the ACCUMULATE carried/quarantined partition as JSON, for both `preview`
  and `apply`, under `PYTHONHASHSEED ∈ {0, 1, 2, 3, 42, 2026}`. All twelve
  runs must produce the identical partition
  `{carried: [e-carry], quarantined: [e-ambig, e-exact]}`.

**RED evidence (pre-fix, exit=1):** unit test failed `'b.md' != 'a/b.md'`
under `PYTHONHASHSEED=0`; cross-seed test showed preview@seed0 carrying
`e-ambig` while the fixed expectation quarantines it (the seed-dependent
divergence). GREEN: 11/11 under seeds 0/42/2026.

## F-SUB45-2 — importer manifest guard let untyped errors escape (fixed)

**Bug.** `SeedImporter.import_directory`
(`src/soul_knowledge/importer.py`) documents fail-closed typed
`ImportManifestError` for an unreadable/mis-anchored manifest, but the guard
caught only `(OSError, json.JSONDecodeError)`. A valid-JSON non-object
manifest (`5`, `[1,2]`, `"str"`, `null`) escaped as a bare `AttributeError`
at `stored.get("root")`, and invalid UTF-8 escaped as `UnicodeDecodeError`
(both verified by wave 2). An ops surface catching the documented typed
error saw an untyped crash instead.

**Fix (src/soul_knowledge/importer.py).** The guard now catches
`(OSError, ValueError)` — `ValueError` covers both `JSONDecodeError` and
`UnicodeDecodeError` — and validates the manifest shape before any `.get`:
`stored` must be a dict, `files` (when present) must be a dict of dicts.
Every broken-manifest shape now raises the typed `ImportManifestError`.

**Regression tests (tests/test_sub47_wave2_fixes.py).** Parametrized
valid-JSON non-object manifests (`5`, `[1, 2]`, `"str"`, `null`, `true`),
invalid-UTF-8 bytes, a non-object `files` section, and a non-object `files`
entry each raise `ImportManifestError`; a well-formed manifest is still
accepted (control). The wave-2 property test
`test_a_corrupt_manifest_fails_closed` in
`tests/test_property_importer_e2e.py` had its now-stale known-gap early
return replaced: any parseable-but-malformed manifest is asserted to raise
the typed error (the only early return left is a well-formed,
well-anchored manifest).

**RED evidence (pre-fix, exit=1):** 9 tests failing with the untyped
`AttributeError`/`UnicodeDecodeError` escapes. GREEN: 11/11.

## Gates (fresh, post-fix)

- `pytest tests/ -q`: **1132 passed**, 0 failed (exit 0; 1121 + 11 new)
- affected subset (relearn, importer, wave-2 property files,
  service_relearn_api + new file): **111 passed**
- `ruff check`: exit 0 · `ruff format --check` on all touched files: clean
  (repo-wide `ruff format --check` still flags `docs/mathematics/scoring.md`
  — pre-existing drift at HEAD 42a9a3e, verified via `git stash`; out of
  scope, untouched)
- `pyright`: **0 errors, 0 warnings**
- `bench/run.py run`: **exit 0, 24/24**, ledger `20261026T072727Z`; per-case
  scalar comparison vs pinned ledger `20261026T031852Z`: **zero drift**

No commits made (pool rule). Frozen contracts and fixtures untouched.
Files touched: `src/soul_knowledge/relearn.py`,
`src/soul_knowledge/importer.py`, `tests/test_sub47_wave2_fixes.py` (new),
`tests/_anchor_classify_child.py` (new),
`tests/test_property_importer_e2e.py` (stale-gap update only), this doc.

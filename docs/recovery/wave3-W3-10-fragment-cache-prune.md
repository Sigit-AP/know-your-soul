# Wave 3 — W3-10: `FragmentCache.prune` is never called from production code

- **Finding**: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave3.md` §W3-10 (Minor).
- **Owner**: SUB-32 (decision + guard). Wiring, if ever wanted, belongs to the
  `projection.py` owner — that file is outside SUB-32's scope (SUB-33 owns it
  in wave 3).

## Where it lives

`FragmentCache` is defined in `src/soul_knowledge/projection.py:340` (not in
`retrieval.py`/`service.py` as first assumed). Verified facts:

- `grep -rn "FragmentCache(" src/` → **zero** hits: the class is *never
  instantiated* in production. `GraphProjector(cache=...)` and
  `GraphifyAdapter(cache=...)` accept it optionally; no caller passes one.
- `grep -rn "\.prune(" src/` → zero hits; `prune` is exercised only by
  `tests/test_projection.py::test_cache_prune_bounded_by_live_documents`.

## Chesterton's fence: why does it exist?

The docstring pins it to the projection-matrix rows 4/5/8/9: content-hash
keying (mtime never participates), namespace pinned to the fragment-generator
version, portability across absolute roots, and `prune` bounding the entry
count by the live document set. It is a designed, fully unit-tested component
for incremental fragment derivation across rebuilds — built ahead of its
callsite. It is **not** abandonware with unknown purpose; the purpose is
recorded and the behaviors are tested.

## Decision: keep it unwired; guard the dangerous middle state

The audit offers "wire it or delete it". Within SUB-32's file scope neither
the class nor its callsites can change (both live in `projection.py` /
`graphify_adapter.py` / CLI-importer wiring). The honest resolution available
now:

1. **Keep** the class and `prune` — deleting a matrix-pinned, tested component
   because its callsite never landed would destroy recorded intent.
2. **Do not wire it half-way.** The only harmful state is "instantiated in
   production without `prune`" (unbounded growth across corpus edits — the
   audit's concern). Today's unwired state has zero operational impact.
3. **Guard test** (`tests/test_audit_wave3_residual.py::
   test_fragment_cache_never_wired_without_prune`): scans production sources
   and fails the moment any production module instantiates `FragmentCache`
   without a production `.prune(` call. Non-vacuous: it pins exactly the
   failure mode the audit names, in both directions.

## Wiring spec (for whoever wires the cache)

When a production caller passes `cache=` to `GraphProjector`/`GraphifyAdapter`:

- **When to prune**: once per successful rebuild, after the new generation is
  published (pointer flipped), never mid-rebuild — a failed rebuild must not
  drop still-live entries.
- **Live-key set**: `cache.key_for(rel_path, content)` for every corpus file
  in the rebuilt set (the same walk the import performs), i.e.
  `cache.prune([key_for(p, bytes) for each live document])`.
- **Bound**: entry count ≤ number of live documents; one entry per
  (rel_path, content hash) pair; a generator-version bump changes the
  namespace, so old namespaces are orphaned directories — delete or ignore by
  policy, they are not `prune`'s job.

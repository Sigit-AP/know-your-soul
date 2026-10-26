# Codebase push: property-based test wave 2 (SUB-45)

Date: 2026-10-26 · Branch: feature/superpower-knowledge-soul · HEAD at start: b00a0b9

## Goal

Fill the honest residual gap named by wave 1
(`docs/recovery/codebase-push-property-tests.md`): property suites for the
four heavy-fixture surfaces — **projection**, **relearn**, **importer e2e**,
**mcp_server** — targeting ratio ≥1.45 and density ≥65 without padding. Every
test pins a real invariant discovered by reading `src/`; hypothesis runs use
`derandomize=True` and no wall-clock deadline. Zero `src/` changes, zero
existing-test edits.

## What was added (4 new files, tests/ only)

| File | Tests | Invariants pinned |
|---|---|---|
| `test_property_projection.py` | 59 | `_truncate_at_word_boundary`: ≤cap, prefix-only, identity within cap, cuts only at a space unless single-word, idempotent · `enriched_summary`: ≤max_length, deterministic, starts with truncated heading, whole-fragment fill, restated-lead suppression, first-prose-sentence-only, holds under tiny caps · `_section_blocks`/`_section_fragments`: fenced content never contributes, list markers dropped, bold-lead digest · `_clean_sentence`: no Cc/Cf, collapsed whitespace, emphasis stripped, secret shapes redacted · quarantine: per-node removal, receipt carries (node_id, field, category) and never secret material, idempotent, cross-field split caught, nested payloads scanned, sorted by node id, clean prose untouched · rebuild determinism: byte-identical generations, inode preservation on unchanged artifacts, manifest binds every artifact hash (and never its own), counts match graph.json, fixed-watermark rebuild ≡ fresh store, `StalePublicationError` on older watermark, `repair_if_stale` no-op/torn-pointer recovery, `ShrinkGuardError`, typed `OversizedArtifactError` before any write, non-positive cap refused at construction, quarantined node never reaches an artifact + typed audit event · `FragmentCache`: portable content keys, put/get round-trip, overwrite in place, prune keeps exactly live set + idempotent, corrupt entry → None · pointer hygiene: malformed `CURRENT` → None · `_knowledge_events`: only knowledge-affecting ≤ watermark, in append order · `_source_section_fragments` key set ≡ importer id walk (incl. `-n<occurrence>` suffixes) · manifest records pinned partitioner/hub policy/adapter version |
| `test_property_relearn.py` | 36 | preview deterministic + read-only (watermark/gate/quarantine/records untouched); RESET preview carries nothing and states the exact zero vector (P=M=E=0, U=1, level 1); ACCUMULATE partitions evidence with no overlap/loss and carries exactly the unchanged-anchor set; preview ≡ apply classification; apply never moves the knowledge watermark (`evidence_weight=0`, `knowledge_seq=None`); relearn record is a zero-weight admin event; supersession (retention, never deletion) + gate cleared; mismatched approval refused with zero side effects; idempotent replay returns the original receipt (one record only); key rebound to the other mode → `IdempotencyConflict`; backup manifest hashes/sizes match staged bytes, exactly one sqlite artifact, no staging residue, staged manifest ≡ receipt; backup predates the apply mutation; rollback restores byte-exact backup (`restored_db_sha256` ≡ sqlite artifact), exactly one linked audit event, shared store object stays valid (W3-1), pending gate restored; rollback single-shot; unknown relearn → `UnknownRelearn`; ACCUMULATE keeps carried version compatible / RESET breaks it; `assert_source_compatible` refuses exactly the outside set and requires BOTH flags; `_anchor_path` safe invariants (None-or-member, exact short-circuit, unique suffix); in-memory store → `BackupUnavailable`; status surface (pending/after-apply); approval extra-forbid; receipt frozen + sorted id lists; nodes_reused ≡ carried node set; in-process classify determinism |
| `test_property_importer_e2e.py` | 30 | import→rebuild→`get_node`/`query` round-trip: every imported id published and resolvable; query budget enforced, returned ids ⊆ projection; distinctive-heading query finds its node · import deterministic across fresh stores (identical receipts + projection content hash) · manifest-anchored incremental: unchanged rerun imports nothing, a content change reimports exactly the changed file, mis-anchored/corrupt manifest fails closed typed · ids stay in the validator charset `^[\w.:-]+$`, unique per import, section ids nest under the file id; legacy-colliding names (`a-b.md` vs `a_b.md`) never merge (both published) · roots materialized exactly once across imports · wikilinks resolve exactly when the target is imported (incl. self-links); unresolved entries always name the file's own declared links, sorted; never-imported targets always unresolved · `retire_source` removes exactly that scope, idempotent, excluded from the projection · `_read_contained_file` byte-cap boundary (≤cap ok, >cap typed `OversizedPayloadError`); symlink escape → `PathEscapeError`; in-root symlink followed; path outside import_root refused · `_remap_fragment_ids` purity: id_map covers every old id, contains-edges re-scoped, reference targets never rewritten · manifest write records resolved root + per-file sha256/source_version_id · headingless file → exactly the file node; provenance carries the source_uri; receipt frozen; string domain → TypeError before any write |
| `test_property_mcp_server.py` | 44 | tool surface exactness: exactly the ten spec operations, sorted, bijective mapping, names match `^[a-zA-Z0-9_-]{1,64}$`, required-set snapshot pinned, every schema a closed object, no path-like property anywhere (F1 shape structurally absent) · strict validation: valid args returned unchanged (independent copy), unknown property/missing required always `INVALID_ARGUMENTS` naming the key, non-object body rejected (None ≡ {}), bool/numeric-string/float never an integer budget, budget bounds exactly [128, max] incl. custom max, depth exactly [1,6], string booleans rejected, mode enum case-sensitive, node_id/tag/idempotency length limits exact, diff op enum closed + closed items + minItems 1, validation deterministic on arbitrary JSON and never mutates input · error envelope: canonical JSON, sanitized (no Cc/Cf except \t\n\r), ≤512-char message, audit id present iff it exists, deterministic · `_nodes_payload`: never exceeds the byte cap, honest `truncated` flag, tail truncation, sorted, deterministic · unknown resource uri → typed MCPError; resource set exactly the four sanitized `soul://` projections · audit parity: unknown tool and invalid args each produce exactly one final audit event + `isError` · typed `NODE_NOT_FOUND` with audit id · INTERNAL_ERROR never leaks raw exception text · rollback tool: `{}` validates, targetless dispatch fails typed · `ToolError` carries code + audit id · relearn-status resource served canonically without a pending gate |

Total: **169 new test functions**, +2,952 test LOC. Runtime of the 4 files:
~36s; full suite 182s → 219s (**1.20×**, well under the 2× budget).

## Metrics (same method as wave 1: `cat src/soul_knowledge/*.py | wc -l`, etc.)

| Metric | Wave-1 end | Before wave 2 | After wave 2 | Target | Status |
|---|---|---|---|---|---|
| test LOC | 20,812 | 22,401 | **25,353** | — | +2,952 |
| test functions | 875 | 927 | **1,098** | — | +171 |
| test:kode ratio | 1.325 | 1.338 | **1.514** | ≥1.45 | **met** |
| density (funcs/kLOC) | 55.7 | 55.4 | **65.6** | ≥65 | **met** |

(Between wave 1 and wave 2, sibling work added 411 test LOC / 52 functions
and 7 src LOC; wave-2 baselines above are measured at HEAD b00a0b9.)

## Real findings (reported, not fixed — src is out of scope)

**F-SUB45-1 (correctness, cross-process):** `RelearnEngine._anchor_path`
(`src/soul_knowledge/relearn.py:350`) documents a *deterministic* mapping but
iterates a `set` when the exact-match branch misses: with manifest paths
`{"b.md", "a/b.md"}` and evidence `source_uri="z/a/b.md"`, the result flips
between `"b.md"` and `"a/b.md"` across `PYTHONHASHSEED` values (verified:
seeds 0/1/2/3 → `"b.md"`, seed 42 → `"a/b.md"`). When one candidate anchor is
hash-mapped and the other is not, the same evidence row is carried in one
process and quarantined in another — an ACCUMULATE whose preview (CLI) and
apply (ops surface) run in different processes can disagree. Suggested fix
owner: relearn owner — iterate `sorted(manifest_paths)` and prefer the
longest match.

**F-SUB45-2 (typed-error hygiene):** `SeedImporter.import_directory`
(`src/soul_knowledge/importer.py:473-486`) documents fail-closed typed
`ImportManifestError` for an unreadable/mis-anchored manifest, but the guard
catches only `(OSError, json.JSONDecodeError)`. A manifest of valid JSON that
is not an object (`5`, `[1,2]`, `"str"`, `null`) escapes as a bare
`AttributeError` at `stored.get("root")`, and invalid UTF-8 escapes as
`UnicodeDecodeError` (verified for both classes). Impact: an ops surface
catching the documented typed error sees an untyped crash instead. Suggested
fix owner: importer owner — catch `ValueError` (covers both decode and JSON
errors) and check `isinstance(stored, dict)` before `.get`.

**Behavioral notes (verified as intended, pinned in tests):** a self-wikilink
(`[[alpha]]` inside `alpha.md`) resolves to a self-`depends_on` edge — the
target scope is live by the time links resolve; first-pass `import_directory`
receipts legitimately list in-corpus links as unresolved when the target file
sorts later in the pass (the second pass resolves them);
`restored_db_sha256` is measured at the byte-replace point, *before* the
single linked rollback audit event lands, so the live db hash after rollback
differs from it by design.

## Gates (fresh, at submission)

- `pytest tests/ -q`: **1121 passed**, 0 failed (219s)
- `pytest tests/test_property_projection.py tests/test_property_relearn.py tests/test_property_importer_e2e.py tests/test_property_mcp_server.py -q`: **169 passed**
- `ruff check tests/`: exit 0 · `ruff format --check tests/`: clean
- `pyright`: **0 errors, 0 warnings**

No commits made (pool rule). No existing test file edited. No `src/` or
frozen-contract file touched. Fixtures untouched.

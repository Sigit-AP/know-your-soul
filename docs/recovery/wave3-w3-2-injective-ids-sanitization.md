# Wave-3 fixes (SUB-33): W3-2 injective ids, sanitization minors W3-3/W3-4/W3-5/W3-6, generation-dir parent fsync

- **Date**: 2026-10-25 → 2026-10-26 UTC — **Owner**: SUB-33 — **Basis**: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-wave3.md` (W3-2 + minors 2a/2b/2c/2d + SUB-29 side finding).
- **Scope respected**: `src/soul_knowledge/{importer,projection,security}.py`, `tests/` (+new `tests/test_wave3_ids_sanitization.py`), this doc, matrix `Covered?` cells 22/37/38/40. `fragment_generator.py`, `bench/`, `service.py`, `http_api.py` untouched (owned by others / frozen).
- **Probes**: `/root/.hermes/cache/scratch/sub33/probe_prefix.py` (pre-fix RED evidence + post-fix flip, both exit 0); bench ledger copy `…/sub33/ledger-20261026T002447Z.json`.

## 1. W3-2 — file-scoped id collisions silently merged corpus files

**Defect (pre-fix probe, exit 0)**: `file_scoped_id` (Graphify `make_id` recipe) maps `'a-b.md'`, `'a_b.md'`, `'a b.md'` → `'a_b'` and `'a/b_c.md'`, `'a_b/c.md'` → `'a_b_c'` (`COLLIDE=True` ×4 pairs, incl. `'Alpha.md'` ≡ `'alpha.md'` via casefold). E2E: `alpha-one.md` + `alpha_one.md` in one corpus published one merged node set (file node titled by the SECOND file, sections interleaved).

**Fix**: the fragment generator is a frozen contract, so the re-scoping happens at the import boundary (`importer._remap_fragment_ids`) before any event reaches the store; the projector's enrichment replica (`projection._source_section_fragments`) walks the same new recipe. New scheme:

- **Encoding E**: `\w` characters (letters/digits/`_`, Unicode word chars) are literal; the escape marker `-` and every non-`\w` char become `-<lowercase hex codepoint>-`.
- **File id**: normalized path stem (same declared normalization as legacy: `\`→`/`, `..` collapse, final-suffix drop; empty segments from leading-slash equivalence dropped), encoded per `/`-segment, segments joined with `:`.
- **Section id**: `<file id>.<E(heading)>`, plus `-n<occurrence>` for repeated identical headings.

**Injectivity proof sketch** (property test: 20 000 seeded random distinct paths → 0 collisions, `tests/test_wave3_ids_sanitization.py::test_injective_file_id_random_distinct_paths_never_collide`):

1. E is injective: an image string decodes uniquely — literal runs are `\w+`, every `-` opens a hex escape group closed by the next `-`, `-` never appears literally. Distinct strings ⇒ distinct images.
2. File ids: `:` never appears inside an E-image, so the segment split is unambiguous; per-segment E is injective ⇒ distinct normalized path-stems ⇒ distinct ids. Injectivity holds modulo the *declared* path normalization only (separator style, `..`/`.` collapse, final-suffix drop — identical to the legacy equivalence classes).
3. Section ids: file ids contain no `.`, E-keys contain no `.`, and `-n<digits>` can never appear inside an E-image (`n` is not a hex digit, so no valid escape group starts it) ⇒ the composition is injective in (path, heading, occurrence), and a file id can never equal a section id.

Deterministic and host-stable (pure functions of path/heading text); pure `\w` names keep natural ids (`alpha.md` → `alpha`). All ids stay inside the read-model validator's semantic charset (`[\w.:-]+`, no `/`, `\`, `..`).

**Wikilinks**: `references` edges arrive with legacy targets; the importer resolves them through the live store's scope index (legacy id → injective id; a legacy id shared by two live scopes maps to `None` = ambiguous, fails safe to `unresolved`, never guessed).

**Post-fix probe (exit 0)**: `alpha-2d-one | Alpha One` and `alpha_one | Alpha Two` publish as separate file nodes, each section under its own file and enriched from its own body.

## 2. W3-3 — `_clean_sentence` redact-then-strip reconstituted split secrets

**Fix**: order is now strip Cc/Cf → emphasis → whitespace collapse → path mask → **redact last** (matches the service read path). Pre-fix probe: post-clean text matched `aws access key`; post-fix: no SECRET_PATTERNS match, `[REDACTED …]` present. Emphasis-before-redact also closes `pass**word**: hunter2…`.

## 3. W3-4 — redact bypass via ZWSP/fullwidth/homoglyph + base64/hex runs

**Fix** (`security.redact_text`, SECRET_PATTERNS untouched — frozen validator contract):

- Two passes: pinned patterns + entropy rule over the raw text; then, only when a *match view* (NFKC → strip all Cf/Cc controls → Cyrillic/Greek homoglyph fold) differs AND exposes a hidden shape, the redacted normalized view is returned. **Clean input stays byte-identical** (frozen token-budget contract preserved; frozen corpus contains no high-entropy runs ≥40 or long hex — verified programmatically).
- High-entropy run rule: runs `[A-Za-z0-9+/=_-]{40,}` redacted only when spanning ≥3 character classes; pure single-case hex (hash-shaped: sha256 pins, git objects) and UUIDs (36 chars < floor) are deliberately left alone. Two-way tests both directions.
- The quarantine scan (`projection._secret_category`) now matches raw **and** match view, so an obfuscated secret shape in a node field is *quarantined* (per-node fail-closed + typed audit event, wave-2 F7 semantics) exactly like plaintext — redaction never silently substitutes for quarantine.

**Post-fix probe (exit 0)**: `AKIA\u200bIOSFODNN7EXAMPLE`, `password：hunter2secret` (fullwidth colon), `pаssword: hunter2secret` (Cyrillic а) all redacted; sha256/UUID/prose with fullwidth clean text byte-identical.

## 4. W3-5 — >256-char store summaries published verbatim, cut mid-word at read

**Fix** (write side, `projection.py`): `enriched_summary` caps the heading itself via `_truncate_at_word_boundary` (last whole word ≤ 256; a single >256-char word is hard-cut — nothing better exists). Published labels are now always ≤ `ENRICHED_SUMMARY_MAX_LENGTH`, so the read-side 256-char limit never fires and the bench ceiling never counts an undeliverable label. Probe: 575-char summary → 251-char label ending `…tok035` (whole words).

## 5. W3-6 — raw newlines/tabs fused words at read

**Fix**: publish-side whitespace collapse in `_sanitize_entry` (summary heading, provenance, context_tags, source_file display copy): after `sanitize_text`, every `\s+` run → single space. Probe: `"Line one\n# Injected Heading\nLine three\twith tab"` → published label single-line, words space-separated.

## 6. Durability (SUB-29 side finding) — `generations/` parent fsync

`_build_generation` fsynced the new generation *directory* but never its parent chain, so a power loss could strand a fully written generation behind a missing directory entry. Fix: after the generation-dir fsync, fsync `generations/` and the output root. Probe: fsync spy records `…/out/generations` pre-activation (pre-fix: absent; post-fix: present).

## Bench re-measurement (id scheme changed, `bench/` untouched)

Fresh worktree `/tmp/sub33-bench` @ HEAD `ff06e3d` + only SUB-33 files, `uv sync --frozen`. Node ids changed (e.g. `07_calibration_bayes` → `07-2d-calibration-2d-bayes`) — reported here as required:

| Metric | Release baseline (`ledger-20261025T202627Z`) | SUB-33 (`ledger-20261026T002447Z`) |
|---|---|---|
| Verdict | 24/24 accepted, exit 0 (q03/q07 notes) | 24/24 accepted, exit 0 (same notes) |
| quality mean | 0.4451 | 0.4451 (unchanged) |
| ceiling mean | 0.4042 | 0.4042 (unchanged) |
| gap_vs_raw mean | 0.5549 | 0.5549 (unchanged) |
| token saving mean / min | 94.26% / 92.45% | 94.18% / 92.64% |
| p50 / p95 median | 6.43 ms / 7.43 ms | 6.98 ms / 9.01 ms (host noise) |

Token deltas come solely from node ids appearing in canonical payloads (`system_estimated_tokens` +8…+22 on most cases; q07/q23/q24 slightly *lower* — routing composition shifted, verdicts/quality unchanged). Frozen corpus manifest sha256 unchanged (`bench check` exit 0). `ceiling_node_ids` record the new scheme. `bench/` was not modified.

## TDD evidence

- RED: new module collection ImportError exit 2 (pre-fix), plus non-vacuous pre-fix behavioral probe (exit 0) showing every defect live (above).
- GREEN (fresh): `tests/test_wave3_ids_sanitization.py` 18/18 exit 0; full suite `pytest tests/ -q` 708 passed exit 0 (incl. sibling WIP files of SUB-32/SUB-34, none failing).
- Ripple fixes in existing tests: `test_importer.py`, `test_projection.py`, `test_update_pipeline.py` id expectations updated to the injective scheme; wave-2 F7/wave-1 F4 quarantine tests pass unchanged (semantics preserved).

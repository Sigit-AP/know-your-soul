# SUB-86 — W1: ingestion contract v3 addendum + parser-category guard extension

Date: 2026-10-27. Scope: `docs/contracts/ingestion-contract-v3.md` (new,
frozen), `src/know_your_soul/runtime_guard.py`, `scripts/license_guard.py`,
`NOTICE`, `tests/` (3 new files + 2 extended). Not touched:
`graph_model.py` (SUB-84), `store.py` (SUB-85), the 14 frozen operations, and
the v1/v2 contracts (byte-identical, proven by pinned sha256 digests).

## Authority

Master plan W1 (`.superpowers/sdd/2026-10-27-universal-knowledge-graph/MASTER-PLAN.md`):
"Kontrak beku tetap 14 ops; kapabilitas baru lewat addendum kontrak v3" and
"runtime guard diperluas ke paket baru" for the parser category
(tree-sitter-language-pack + pypdf), keeping the no-model boundary
fail-closed.

## What was built

**Contract v3 addendum** (`docs/contracts/ingestion-contract-v3.md`, frozen
upon publication, v2 addendum style):

- Two proposal-only verbs: `knowledge.ingest_file(path, token_budget=1024)`
  (single file; atomic proposal payload — never truncates, undersized budget
  is `METADATA_EXCEEDS_BUDGET`) and `knowledge.ingest_directory(path,
  recursive=true, max_files=1024, token_budget=1024)` (bytewise-sorted
  traversal, per-file typed failures that never abort the walk, `max_files`
  `[1,65536]`, tail-first truncation with `truncated: true`).
- Proposal-only discipline: ingestion never mutates store/projection/graph;
  application rides the existing operator-gated propose/apply path; one final
  audit event per invocation carrying path + sha256 + `extraction_version` +
  counts, never the input content.
- Stable node ids `kind:sha256(path:kind:name:line)[:col]`; cross-file
  references land in `unresolved_refs` (`pending`), never guessed.
- Edge kind `bridges`: primary↔primary only (`INVALID_BRIDGE_ENDPOINT` at
  apply), never part of `contains`, stored directed / symmetric meaning,
  proposal-created from recorded evidence only.
- Primary-node rules: `primary: true` decomposition roots, arbitrarily deep
  `contains` below them, never contained by another primary, proposal-only
  creation.
- Path discipline: ingestion-roots allowlist; `..` / outside-root / symlink
  escape → `PATH_OUTSIDE_ROOT`; the v2 no-path-like-property rule is amended
  for exactly these two verbs (`path` is the single path property).
- Adapter mapping: CLI `ingest-file`/`ingest-directory`, HTTP
  `POST /v1/knowledge/ingest/file|directory`, MCP
  `knowledge_ingest_file`/`knowledge_ingest_directory` (closed schemas).

**Runtime guard extension** (`runtime_guard.py`): new parser-category
allowlist `ALLOWED_PARSER_DISTRIBUTIONS` = {pypdf, tree-sitter,
tree-sitter-language-pack} + `ALLOWED_PARSER_IMPORT_ROOTS` (underscore
spellings), `is_allowed_parser_distribution` (exact match after
canonicalization — stricter than the forbidden matcher on purpose),
`parser_category_violations` / `assert_parser_category_disjoint` invariant
probe wired into `verify_runtime`, new report key
`allowed_parser_distributions`. `pypdf` left `FORBIDDEN_DISTRIBUTIONS`;
everything else (provider/model/embedding/vector, incl. `watchdog`) stays
fail-closed — proven by parametrized rejection tests.

**License guard extension** (`scripts/license_guard.py` + `NOTICE`): NOTICE
attribution for tree-sitter / tree-sitter-language-pack (MIT) and pypdf
(BSD-3-Clause) is required verbatim; a declared parser dependency must be an
exact `==` pin recorded in NOTICE as `name==version` (same rule as the
Graphify pin); verbatim-copy marker phrases extended to tree-sitter/pypdf.

**Dependency policy** (`tests/test_dependency_policy.py`):
`APPROVED_PARSER_DEPENDENCIES` checkpoint — the test set and the guard
allowlist must match exactly; a parser dependency in pyproject.toml must be
an exact extra-free `==` pin. Nothing else loosened.

## Pinned-test updates

- `tests/test_license_guard.py` — the `NOTICE_OK` fixture gained the
  parser-attribution paragraph (the guard requires it unconditionally now).
- `tests/test_dependency_policy.py` — +3 parser-policy tests above; no
  existing assertion weakened.

## Probes (TDD)

- RED: `pytest` over the 3 new + 2 extended test files → **48 failed, 51
  errors, exit 1** (v3 doc missing, allowlist absent, pypdf still forbidden,
  license guard without parser attribution).
- GREEN: same files → **213 passed, exit 0** (166 tests are new: 57 contract
  shape, 89 guard extension, 17 license attribution, 3 dependency policy).
- Regression: full suite **3633 passed, exit 0** (278.9s; includes sibling
  W1 files).
- Gates: `ruff check` exit 0, `ruff format --check` exit 0 (7 touched files),
  `pyright` 0 errors/0 warnings on all touched files,
  `python scripts/license_guard.py` exit 0 on the real repo.
- Freeze proof: `git status docs/contracts/` shows only the new v3 file; the
  four v1/v2 digests are pinned in `tests/test_contract_v3_shape.py`.

No commits (pool rule). `graph_model.py` / `store.py` untouched.

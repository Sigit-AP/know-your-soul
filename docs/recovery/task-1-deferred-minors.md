# Task 1 deferred minors — closure record (SUB-30, 2026-10-25)

Scope: the 9 minor findings deferred from the Task 1 review, recorded in
`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/progress.md:33`. The verbatim
review text was never persisted; the authoritative sources are progress.md:33,
`task-1-report.md` §6/§8.7 and `review-final-whole-branch.md` §4. All findings
closed against HEAD `560705c`, uncommitted per subagent rule.

RED evidence: the 12 new tests were run against the pre-fix code — **12 failed, 69
passed, exit 1**. GREEN (fresh, post-fix): **81 passed, exit 0** in
`tests/test_no_ai_runtime.py` + `tests/test_dependency_policy.py`.

| # | Finding | Status | Evidence |
|---|---------|--------|----------|
| 1 | Anti-CLI-scraping scan holes (`os.system`/`popen`/`exec`/non-literal argv) | FIXED | `runtime_guard.cli_scraping_violations()` (AST, alias/from-import aware) flags the full `os.system`/`os.popen`/`os.spawn*`/`os.exec*`/`os.posix_spawn*`/`subprocess.*` surface plus `eval`/`exec`; any process spawn is a violation, so non-literal argv is refused structurally. `test_static_scan_fails_closed_on_every_shell_out_family` (11 hostile snippets, RED→GREEN), `test_product_sources_never_shell_out_at_all` (real src tree). Residual (documented): reflection-built calls (`getattr(os, "sys"+"tem")`) are not statically resolvable — the socket/import guards remain the runtime backstop. |
| 2 | `sendmsg` untested | FIXED (coverage) | `test_outbound_sendmsg_is_denied`, `test_unix_sendmsg_still_works_while_guarded`, `test_sendmsg_patch_is_restored_after_uninstall`. Non-vacuity proven by mutation probe: with `sendmsg` removed from `SocketGuard.install`, the AF_INET `sendmsg` probe call succeeds (test would FAIL); with the patch, `OutboundSocketDenied` is raised. |
| 3 | Dirty entry-point argument ignored | FIXED | `_is_forbidden_entry_point` strips whitespace from value/name, splits on the first `:`, and **raises `ForbiddenDependencyError` on an unparseable value** (no module target) instead of ignoring it. RED: `" graphify.serve:_main"` and `":_main"` slipped through → now caught/refused (`test_dirty_entry_point_values_fail_closed`, `test_unparseable_entry_point_value_fails_closed`; probe: `forbidden_entry_points([fake])` → `['d:console_scripts:sneaky= graphify.serve:_main']`). |
| 4 | Import-probe overpromise | FIXED | New `import_block_evidence()`: per-root evidence split into `actively_blocked` (root resolves here AND the guard rejects it — 7 graphify roots in this env) vs `not_installed` (37 provider roots — reported as "nothing to block", no longer counted as "blocked"). Resolvability is checked via `PathFinder.find_spec` with **zero `sys.modules` side effects** (a plain unguarded `find_spec("graphify.exporters.graphdb")` imports the parent — that bug was found and fixed during this round). `verify_runtime` report gains `import_block_evidence` and raises if `actively_blocked` is empty (vacuous probe). |
| 5 | Matrix Task 8 ownership only in prose | FIXED (scoped) | Row 36 Owner cell now reads `Task 9 (co-owner Task 8: MCP parity)`; `test_matrix_task8_coownership_is_annotated_on_row_36_not_only_in_prose` pins cell + prose consistency (RED→GREEN). Rows 22/40 co-ownership cells remain prose-only: outside the delegated annotation scope (rows 1/36); flagged to the coordinator. |
| 6 | Rows 1/36 denominator tension | FIXED | Rows 1 and 36 now carry an explicit denominator note: the excluded sub-surface (non-markdown grammars / DB push) leaves the denominator exactly once via §4; the row stays in the 40 and is counted on its applicable scope — no double counting. `test_feature_matrix_denominator_is_exactly_40_and_machine_checkable` parses the matrix: exactly rows 1–40, unique, each with owner + named contract test, and no numbered row inside the §4 exclusion table. |
| 7 | Lock extras tripwire weakness | FIXED | Real hole found while reproducing: the legacy `_reachable_packages` marks packages seen **by name**, so an edge-requested extra arriving after the first visit is silently dropped (synthetic lock: `mcp → graphifyy[openai]` never reaches `openai`). Fix: `_reachable_packages_with_extras` is a fixpoint walk that revisits on new extras and records every followed (package, extra); `test_reachability_walk_follows_only_approved_extras` fails unless followed ⊆ `APPROVED_WALKED_EXTRAS = ENABLED_EXTRAS + (("pyjwt","crypto"),)` — the live lock currently follows exactly that set (`pyjwt[crypto]` edge from the MCP SDK, now explicitly recorded). `test_enabled_extras_match_pyproject_exactly` pins pyproject drift. The forbidden-distribution reachability tests now use the fixpoint walk. |
| 8 | Exotic MCP capability family enumeration | FIXED | `observed_mcp_capabilities` now classifies every registered method: enumerated capability family (tools/resources/prompts/logging/completions/sampling/elicitation), or explicitly benign protocol core (`ping`, `server/*` — the pinned SDK registers `server/discover` — `notifications/*`); **anything else raises `CapabilityViolation`** instead of being ignored (task-1-report §8.7 residual closed). Probe: `experimental/frobnicate` → refused; product server (`ping, server/discover, tools/*, resources/*`) still passes. |
| 9 | Prefix-match dependency policy false-positive risk | FIXED | `is_forbidden_distribution` is now exact or `entry + "-"` bounded. No false positives: `torches`, `torchweed`, `ollamarama` no longer flagged. No false negatives: `torchaudio` and `psycopg2` added explicitly to `FORBIDDEN_DISTRIBUTIONS` (boundary matching alone would have lost them); `torch-cuda`, `openai-whisper`, `langchain-community`, `psycopg2-binary` still caught by the boundary rule (probe-verified). |

## Gates (fresh, final, 2026-10-25 23:32 UTC)

```
$ .venv/bin/pytest tests/ -q            → 672 passed, 1 skipped, exit 0
$ .venv/bin/ruff check src tests bench  → All checks passed! exit 0
$ .venv/bin/ruff format --check src tests bench → 94 files already formatted, exit 0
$ .venv/bin/pyright                     → 0 errors, 0 warnings, 0 informations, exit 0
```

The full suite includes other agents' uncommitted WIP (SUB-28 wave-2 track items,
SUB-29 row-23 durability): all green together. One incident during this round: a
temporary debug test file (`tests/test_dbg_tmp.py`) left by this agent loaded
`graphify.exporters` into `sys.modules` and caused 12 spurious full-suite failures —
identified with a pytest load-watcher plugin, file deleted, suite re-verified clean.
The episode validated the strengthened probe: `assert_not_loaded` correctly fires
when a forbidden module is resident.

Files touched: `src/soul_knowledge/runtime_guard.py`, `tests/test_no_ai_runtime.py`,
`tests/test_dependency_policy.py`, `docs/research/graphify-feature-matrix.md`
(rows 1 and 36 only), this file. No other lane's files modified.

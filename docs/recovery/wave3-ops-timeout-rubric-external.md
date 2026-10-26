# Wave-3 minors: ops-path hardlink/TOCTOU, body_timeout=0, provenance rubric, external test

- **Date**: 2026-10-25 (SUB-34) — **Findings**: audit-wave3.md W3-7, W3-8(a), W3-9, W3-11
- **Files**: `src/soul_knowledge/http_api.py`, `bench/run.py` (provenance-rubric item only),
  `tests/test_audit_wave3_ops_boundary.py` (new, 14 tests), `tests/test_acceptance_e2e.py`
- **TDD evidence**: RED `10 failed, 4 passed` (exit 1) on the new file pre-fix; GREEN `14 passed` (exit 0) post-fix.

## W3-7 — ops-path containment: hardlinks refused, TOCTOU re-checked

Path-only containment (`security.resolve_within_root`, frozen) is structurally blind to
hardlinks and had a resolve-then-use window. Closed in the adapter (the only layer allowed
to change), deterministically:

1. **Hardlink refusal** — `_reject_hardlinked_ops_file` (called by `_resolve_ops_path`):
   an existing regular file with `st_nlink > 1` inside the ops root is refused with
   `PATH_ESCAPE` (HTTP 400). Another name shares the inode — possibly outside the root —
   and containment cannot see it. Missing paths (backup destinations yet to be written)
   pass; directories are exempt (their link count counts subdirectories).
2. **Post-operation re-resolution** — `_recheck_ops_path` runs after every backup and
   restore for each involved path: the candidate is re-resolved and must name the
   *identical* path as pre-operation. A parent-directory swap mid-operation either escapes
   the root (resolution raises) or moves the path — both fail closed with `PATH_ESCAPE`.
   Wire messages still name only the caller-supplied candidate (wave-2 sanitization holds).

Probes (all in `tests/test_audit_wave3_ops_boundary.py`, non-vacuous):
- `test_restore_from_hardlink_refused`: hardlink inside ops root → outside inode;
  pre-fix the restore ran and 500'd on the outside bytes; post-fix 400 `PATH_ESCAPE`,
  no restore output written.
- `test_backup_onto_hardlink_refused`: 400 `PATH_ESCAPE`; outside inode byte-untouched.
- `test_backup_parent_swap_detected` / `test_restore_parent_swap_detected`: a monkeypatched
  backup/restore swaps a parent dir for a symlink mid-operation; pre-fix 200, post-fix 400.
- Positive controls: normal backup+restore and an undisturbed nested-path backup still 200.

Residual boundary (documented, accepted): an attacker who can swap inodes *and* win the
race between the operation's final write and the re-check still needs write access inside
the ops root plus ownership/read of the target (`fs.protected_hardlinks`) — operator-level,
unchanged from the audit's assessment.

## W3-9 — `body_timeout_seconds <= 0` refused at startup

`BoundaryMiddleware.__init__` now raises `ValueError` unless `body_timeout_seconds > 0`
(`not x > 0` also rejects NaN). `create_app` and `serve` both construct the middleware, so
the refusal fires before any socket is bound — proven by
`test_serve_rejects_zero_body_timeout_before_binding` (no socket file exists after the
refusal). Positive control: a valid timeout serves a real query (200).

## W3-8(a) — bench rubric excludes provenance from scoring text

`bench/run.py` adds `rubric_scoring_text(payload)`: the payload JSON minus the
`provenance` field. It is used for BOTH the measured system response and the compliant
ceiling (`compliant_ceiling.score_of`), keeping the comparison symmetric. Provenance still
counts toward token estimation on both sides — those are real wire bytes. The ledger gate
record carries an explicit `rubric_scope` note.

**Per-case verification** (ledger `20261025T233953Z` → `20261025T235210Z`, repeats=5,
compared programmatically over `quality_system`, `quality_ceiling`,
`system_estimated_tokens`, `token_saving_ratio`, `gap_vs_raw_read`, `ceiling_node_ids`,
`ceiling_method`): **0/24 cases changed; bench exit 0; 24/24 accepted.** q16 stays
0.75 = ceiling — its `steelman` hit is carried by the delivered result summary
("SOUL 09 — Pre-mortem and steelman — …"), not the URI; proven by
`test_q16_steel_man_hit_is_content_delivered`, which asserts the term survives exclusion
and the provenance URI is absent from the scoring text. The pre-fix probe
(`/root/.hermes/cache/scratch/sub34_provenance_probe.py`) predicted exactly this delta
table before any code change.

## W3-11 — external Hermes-install check is now a real test

`TestNoHermesSourceModification::test_hermes_source_tree_is_unmodified` (was: body `...`
under a permanent skip) now runs the real check against `/root/.hermes/hermes-agent`:
HEAD == `v0.22.5^{commit}`, `git status --porcelain` empty, `git stash list` empty. It
skip-gates at runtime only when no git-managed installation exists (external host
dependency); on this host it **runs and passes** (verified: HEAD
`bf0203a794a07aac3ff764972ab9087baa4e4f4e` == tag, clean tree, empty stash).

Non-vacuous proof: pointed at a synthetic dirty checkout
(`/root/.hermes/cache/scratch/sub34_fake_hermes`, same tag + one modified file) the test
**fails** with `AssertionError: install tree has uncommitted changes/untracked files`.

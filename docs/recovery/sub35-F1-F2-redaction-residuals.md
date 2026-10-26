# SUB-35-F1 + SUB-35-F2 — redaction residuals (wave-3 re-review)

- **Date**: 2026-10-26 — **Fix agent**: SUB-36 — **Target**: `feature/superpower-knowledge-soul` @ `5112339` (+ this fix)
- **Findings**: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/review-fixes-wave3.md` (SUB-35 re-review, 2 new Minor)
- **Tests**: `tests/test_sub35_redaction_residuals.py` (24 tests)

## SUB-35-F1 — entropy-run false positives on the repo's own naming conventions

**Defect.** `_ENTROPY_RUN_RE` (`[A-Za-z0-9+/=_-]{40,}`) counted `-` toward the
"symbol" character class, so any ≥40-char run with lower+digit+symbol tripped
the ≥3-class rule. The repo's own dated kebab paths
(`docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design`),
mixed-case kebab (`task-10-T10-1-opsi-c-gate-recalibration`), snake_case test
names with digits, and `/`-joined path runs were redacted as
`[REDACTED:high-entropy token]` — 121 runs across 35 of 55 docs (measured;
matches SUB-35's 35/55). Fail-closed (no leak), but content-corrupting on the
project's natural vocabulary, and the frozen-corpus byte-identity claim only
covered the pinned corpus.

**Fix** (`security.py::_redact_entropy_runs`). A run is redacted only when it
(a) is not pure hex (unchanged), (b) contains **no** `-` or `_` (identifier
separators — kebab slugs, snake_case, paths are identifier-shaped, not
secret-shaped), and (c) mixes **all three** of lower, upper and digit classes.
The new rule redacts a strict subset of the old rule's hits, so every input
that was byte-identical before stays byte-identical (frozen bench corpus
29/29 — 28 markdown docs + `manifest.json` — and token-estimator parity
re-verified via `bench check`).

**Pinned-pattern side of the same class.** While building the repo-wide
two-way guard, the scan surfaced a pre-existing sibling FP: the
`openai-style key` pattern `sk-[A-Za-z0-9_\-]{16,}` matched mid-word —
`task-10-T10-1-…` contains `sk-1…`. The pattern now carries a
`(?<![A-Za-z0-9_\-])` lookbehind: real keys are token-boundaried (whitespace,
quote, `=`/`:`), identifiers are not.

**Documented boundary (extended).** Standalone ≥40-char secrets that are
pure-hex, all-one-case, or base64url (`-`/`_` alphabet) are not reachable by
the entropy rule; the famous prefixed token families (OpenAI/Stripe `sk-…`,
GitHub, Slack, AWS `AKIA…`, JWT, bearer) stay pinned-pattern territory, and
`key = value` forms stay covered by the credential-assignment pattern.

## SUB-35-F2 — Zs-class split-secret bypass

**Defect.** `_redaction_match_view` stripped only Cf/Cc separators, so
`AKIA<Zs>IOSFODNN7EXAMPLE` with `Zs ∈ {U+00A0, U+1680, U+2000–U+200A,
U+202F, U+205F, U+3000}` evaded matching. Worse end-to-end: publish
sanitization (W3-6) collapses Zs to a plain ASCII space *before* the
quarantine scan, so the scan saw the secret re-shaped into a form the view
deliberately keeps split — `graph.json` carried the full key material
separated by one visible space.

**Fix, two layers.**

1. `security.py::_redaction_match_view` now drops every non-ASCII Zs
   character **before** NFKC (NFKC would fold them into plain ASCII spaces,
   keeping the secret split). ASCII space survives — the `\s+` patterns need
   it and the raw pass already covers it. Clean text carrying exotic spacing
   (French NNBSP thousands, em spaces) stays byte-identical: the normalized
   view is still only returned when it exposes a secret shape.
2. `projection.py::_quarantine_secret_nodes` now also scans the raw
   pre-sanitization free-text fields (`summary`, `source_file`, `provenance`,
   `context_tags`) per node, so a Zs-split secret is quarantined even though
   publish sanitization reshapes it before the artifact scan. The artifact
   scan itself is unchanged (defense in depth).

## Probe evidence (non-vacuous RED → GREEN)

- **RED** (pre-fix @ `5112339`): `pytest tests/test_sub35_redaction_residuals.py`
  → **21 failed / 3 passed, exit 1** (the 3 passing are the parity guards —
  frozen corpus, real-secret blobs, legit-Zs — which must hold on both sides).
  Log: `/root/.hermes/cache/scratch/sub36_red.log`.
- **GREEN** (this fix): **24 passed, exit 0**.
- **Full bypass matrix re-run** (`test_full_split_secret_bypass_matrix_stays_closed`):
  plain + ZWSP + ZWNJ + BOM + tab + NBSP + hair + ideographic splits of the AWS
  shape all redacted; fullwidth-colon and Cyrillic-homoglyph credential keys
  redacted; base64 blob redacted; pure-hex sha256 untouched.
- **Two-way F1 guards**: every ≥40-char run in `docs/**/*.md` (248 runs, 121
  previously redacted across 35 docs — guards pin `>=100`/`>=30` so the scan
  cannot silently go vacuous) survives `redact_text` verbatim; real secret
  blobs (base64 with padding, AWS-secret-key shape, mixed-case alnum-40,
  seeded random base64) are still redacted.
- **Zs quarantine e2e**: a node summary carrying `AKIA\u200a…` is quarantined
  — absent from `graph.json`, one typed `security.projection_quarantine`
  audit event naming the node (never the secret), `verify_chain().ok`, clean
  nodes keep serving.

## Gates (fresh, this fix)

| Gate | Result |
|------|--------|
| `pytest tests/ -q` | **732 passed** (708 + 24 new), exit 0 |
| `ruff check src tests bench` | exit 0 |
| `ruff format --check src tests bench` | exit 0 |
| `pyright` | 0 errors / 0 warnings, exit 0 |
| `python bench/run.py check` | exit 0 (corpus integrity + estimator parity) |
| `python bench/run.py run` | exit 0, ledger `ledger-20261026T013628Z.json`, **24/24 accepted**; per-case quality/token numbers byte-identical to the pre-fix ledger `ledger-20261026T003827Z.json` |

Residual (pre-existing, documented by SUB-35, untouched): `ruff format --check .`
on the whole repo still reports `docs/mathematics/scoring.md` drift since
`724843c` — outside this fix's scope.

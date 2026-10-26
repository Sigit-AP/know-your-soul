# SUB-43 — final-audit S-1 (Zl/Zp split-secret) + S-2 (cross-field split-secret)

- **Date**: 2026-10-26 — **Fix agent**: SUB-43 — **Target**: `feature/superpower-knowledge-soul` @ `51565d3` (+ this fix)
- **Findings**: `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-final-security.md` (SUB-37b, S-1/S-2)
- **Tests**: `tests/test_sub43_zl_zp_crossfield.py` (12 tests)
- **RED** (pre-fix): `pytest tests/test_sub43_zl_zp_crossfield.py` → **9 failed / 3 passed, exit 1** (the 3 passing are the two-way parity guards — legit Zl/Zp text byte-identical, clean multi-field node publishes, clean Zl/Zp node publishes — which must hold on both sides). Log: `/root/.hermes/cache/scratch/sub43_red.log`.
- **GREEN** (this fix): **12 passed, exit 0**.

## S-1 — Zl/Zp (U+2028/U+2029) split-secret bypass

**Defect.** `_redaction_match_view` dropped only Zs-class separators before
NFKC. NFKC does not fold Zl/Zp and they are not Cf/Cc, so
`AKIA\u2028IOSFODNN7EXAMPLE` survived both the raw pass and the match view.
Publish sanitization (`_WHITESPACE_RE = \s+`, which *does* cover Zl/Zp) then
collapsed the separator to one ASCII space — exactly the form the match view
deliberately keeps — so quarantine and the read-model validator both passed
and `graph.json` carried the full key, recoverable by a space-strip
(SUB-37b probes A2/B1, including the enrichment body-sentence path).

**Fix** (`security.py::_redaction_match_view`). Every separator category —
Zs (non-ASCII), Zl, Zp — is now dropped before NFKC; plain ASCII space still
survives (pinned patterns need `\s+`, and the raw pass already covers it).
Same parity argument as SUB-35-F2: the normalized view is only returned when
it exposes a secret shape, so clean text carrying rare-but-legit Zl/Zp
(French/technical line-paragraph separators) stays byte-identical — guarded
by `test_redact_text_leaves_legitimate_zl_zp_text_byte_identical`.

## S-2 — secret split ACROSS publish-bound fields

**Defect.** `_quarantine_secret_nodes` scanned each field separately. Half a
key in `summary` + half in `context_tags` (SUB-37b probe A1), or half in the
heading + half in the first body sentence fused into the enriched label by
the `" — "` join (probe B2), published both halves side by side in one node
— trivially recoverable from the artifact.

**Fix, two layers** (`projection.py`).

1. `_quarantine_secret_nodes` now additionally scans **joined views** per
   node: the raw (pre-sanitization) fields and the publish-bound
   (sanitized) fields, each joined with `_CROSS_FIELD_JOINER = U+200B`
   (ZWSP). ZWSP is dropped by the match view (so split halves re-join) but
   is not matched by `\s` or any pinned-pattern alphabet (so the join itself
   can never bridge a raw-pattern match — no false cross-field positives
   from the joiner). Beyond the full field-order join, **pairwise
   both-orders** joins catch a two-way split with unrelated fields sitting
   between the halves, whichever field holds the head. The audit event names
   the joined field set (e.g. `context_tags+summary`,
   `<joined-publish-fields>`) — never secret material.
2. `_secret_category` gains a **compact token view** third pass: the match
   view reduced to the pinned-pattern alphabet (`A-Za-z0-9_-.`) with every
   separator/punctuation dropped. A secret fused by arbitrary join
   punctuation (`" — "`, `"; "`) re-forms there. Patterns requiring
   whitespace or `[:=]` (bearer, credential assignment, private-key blocks)
   cannot match this view, so their false-positive surface is unchanged.

**Documented boundary (extended).** Splits wider than two fields/fragments
(split-N-way) and splits whose halves publish in *different nodes* remain
out of reach of pattern-based scanning — acknowledged by the audit as a
design limit. The compact view's FP surface is limited to the pinned prefix
shapes (AWS/GitHub/Slack/JWT/OpenAI); a legit text would have to compact
into e.g. `AKIA` + 16 upper/digit chars across a field boundary.

## Probe evidence (non-vacuous)

- Each probe pins that the halves alone are clean (`_secret_category` →
  `None` on `"AKIAIOSFOD"`, `"NN7EXAMPLE…"`, per-field), so the detection is
  provably from the joined/compact views.
- **Zl/Zp quarantine e2e** (parametrized U+2028/U+2029): node absent from
  `graph.json`, exactly one `security.projection_quarantine` audit event
  naming the node (never the secret), `verify_chain().ok`, clean node keeps
  serving.
- **Cross-field e2e**: `summary="note about AKIAIOSFOD"` +
  `context_tags=("NN7EXAMPLE",)` → node quarantined; neither half appears in
  `graph.json`; clean sibling node publishes.
- **Enrichment join**: `enriched_summary("AKIAIOSFOD", ([], ["NN7EXAMPLE
  is the rest of the sentence."]))` label is caught by `_secret_category`.
- **Full bypass matrix re-run**
  (`test_full_bypass_matrix_including_zl_zp_and_cross_field`):
  plain/ZWSP/ZWNJ/BOM/tab/NBSP/hair/ideographic/**Zl**/**Zp**/fullwidth/
  homoglyph/base64 redacted; pure-hex sha256 untouched; cross-field family
  closed at the quarantine layer.

## False-positive verification

- **Frozen bench corpus**: `bench check` exit 0 (corpus integrity +
  estimator parity); `bench run` exit 0, **24/24 accepted**, ledger
  `ledger-20261026T034223Z.json` — per-case quality/ceiling/tokens/
  ceiling_node_ids/verdicts **byte-identical** to the pre-fix ledger
  `ledger-20261026T031852Z.json` (verified programmatically). No corpus node
  quarantined by the new scans.
- **Repo docs**: 88 markdown docs scanned — none contain Zl/Zp, so the S-1
  match-view change is byte-neutral on the repo; `redact_text` modifies
  exactly the same 2 pre-existing true positives as before the fix
  (`wave1-R1…` carries `admin@example.com`, `wave3-w3-2…` carries fullwidth
  `password：hunter2secret`). 0 new FP.
- **Two-way guards** hold on both sides of the fix: legit Zl/Zp text
  byte-identical through `redact_text`; legit multi-field node
  (summary + provenance + 3 tags) publishes with zero quarantine events.

## Gates (fresh, this fix)

| Gate | Result |
|------|--------|
| `pytest tests/ -q` | **898 passed**, exit 0 (includes sibling agents' WIP property tests) |
| `ruff check` on touched files | exit 0 |
| `ruff format --check` on touched files | exit 0 |
| `pyright` on touched files | 0 errors / 0 warnings, exit 0 |
| `python bench/run.py check` | exit 0 |
| `python bench/run.py run` | exit 0, 24/24 accepted |

Repo-wide `ruff check` / `ruff format --check` / `pyright` currently fail
only on `tests/test_final_adapter_fixes.py` — SUB-42's in-flight WIP
(S-3/socket + adapter fixes), outside this fix's scope and untouched.
Pre-existing residual (unchanged): `ruff format --check .` reports
`docs/mathematics/scoring.md` drift since `724843c`.

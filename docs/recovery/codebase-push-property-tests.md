# Codebase push: property-based test wave (SUB-40)

Date: 2026-10-26 · Branch: feature/superpower-knowledge-soul · HEAD at start: 51565d3

## Goal

Close parity-assessment gap G3 (test:kode ratio 1.16 vs Graphify 1.52; density
45.9 vs 76.1 functions/kLOC) **honestly**: every added test pins a real,
contract-named invariant discovered by reading `src/`, exercised through
hypothesis with `derandomize=True` (no per-run seeds, no wall-clock deadlines).

## What was added (8 new files, tests/ only; 0 src changes)

| File | Tests | Invariants pinned |
|---|---|---|
| `test_property_scoring_invariants.py` | 18 | 12dp ROUND_HALF_EVEN serialization round-trip/idempotence; quantize within half-ULP; confidence ≡ 1−u; overall = max(u); G>0 ⟹ P≥0.70 ∧ M,E>0; promotable ⟹ contract preconditions; more eligible evidence never raises uncertainty; exclusion accounting conservation; fully-excluded ⟹ zero-total-weight everywhere |
| `test_property_retrieval.py` | 31 | rank invariant under input permutation (ties never graph-order); strict `(-score,-field_rank,node_id)` ordering; idf closed bounds; field boost ≤1.15; tokenize/query_terms adversarial-unicode safe; normalize_term non-lengthening + idempotent; alias exact round-trip (all 7 domains); "pem" prefix → typed AmbiguousDomain; estimated_tokens = ceil(bytes/4), key-order invariant, monotone; traversal node/edge/allowlist guards, determinism, unique visits, cross-edge consistency; route outcomes typed + structurally consistent; missing root never falls back |
| `test_property_store_chain.py` | 14 | chain verifies after arbitrary write sequences; event_seq dense from 1; payload/actor tamper at arbitrary position detected (`first_bad_seq ≤ tampered`); tail truncation + anchor deletion fail closed; append-only triggers refuse UPDATE/DELETE at any position; idempotent replay preserves receipt; key reuse with different payload → IdempotencyConflict; expected_version enforced; knowledge_seq only for affecting events; replay/current_node round-trip |
| `test_property_security.py` | 23 | redact_text idempotent + deterministic; every pinned secret shape (8 families) generated and removed; clean prose byte-identical; sanitize strips exactly Cc/Cf minus tab/LF/CR, idempotent, fail-closed overlong; ensure_text_size boundary; resolve_within_root never escapes (fuzz + traversal + absolute); empty allowlist fails closed; 127/8 loopback; wrong/absent key → typed Unauthorized; redact_value credential keys + immutability |
| `test_property_injective_ids.py` | 14 | `_escape_id_text` round-trips through an independent decoder (bijection onto image); pairwise non-collision over hostile alphabet; file id invariant under declared normalization, collision-free outside it; deterministic + path-safe; section keys/occurrence ids never collide |
| `test_property_service_budget.py` | 8 | budget <128 → BudgetTooSmall; >max → BudgetExceedsMax; every successful response `estimated_tokens ≤ budget`; access-only ≤ min(budget,160); adversarial purpose → typed errors only; routing deterministic |
| `test_property_domain_canonical.py` | 24 | canonical_json round-trip/determinism/key-order/unicode-verbatim; storage_payload quantize+parse-back, datetime ISO, idempotence; StrictDecimal refuses bool/float; ScoreVector bounds; Evidence outcome/utilization-bin/latency/naive-timestamp rejection; Relation closed allowlist; Node extra="forbid" + immutability |
| `test_property_review_improvement.py` | 22 | decide deterministic; target≡desired ⟺ transitioned; blocked ⟹ no transition; override chain precedence (retired/dups/contradiction/rollback); promotion never below scoring gate; qualifying needs margin+evidence+low-u; relearn-pending and one-event-cannot-promote block; hysteresis both directions; cooldown blocks non-safety only; classify_impact rules; content_fingerprint ignores version/status; regression gate flags exactly identity drift; proposal id deterministic + rationale-sensitive |

Total: **154 new test items** (148 `def test_` functions + parametrizations),
~2,030 LOC. Runtime of the 8 files: 67s; full suite 106.5s → 182s (**1.71×,
under the 2× budget**).

## Metrics (measured exactly like SUB-38: `cat src/soul_knowledge/*.py | wc -l` etc.)

| Metric | Before | After | Target | Status |
|---|---|---|---|---|
| test LOC | 18,159 | 20,812 | — | +2,653 |
| test functions | 717 | 875 | — | +158* |
| test:kode ratio | 1.161 | **1.325** | ≥1.45 | not met |
| density (funcs/kLOC) | 45.8 | **55.7** | ≥65 | not met |

\* includes 10 functions from sibling SUB-43's WIP file counted in the totals;
SUB-40's own share is 148 functions / +2,030 LOC.

**Honest gap statement:** the residual gap to 1.45/65 is ≈2,900 LOC /
≈145 functions. The remaining *honest* ground is heavier-fixture surfaces
(projection rebuild invariance, relearn rollback algebra, importer e2e
idempotence, mcp_server tool contracts). Filling it faster would require
padding, which the anti-Goodhart rule forbids. Recommend a follow-up wave
scoped to those four modules.

## Real finding (reported, not fixed — src is out of scope)

**F-SUB40-1 (minor, cosmetic):** `domain.storage_payload` renders quantized
Decimals via `str()`, which switches to scientific notation below adjusted
exponent −6: `storage_payload(Decimal("0.0000000000001"))` → `"0E-12"`, and a
quantized `1E-12` renders as `"1E-12"` — while `scoring.serialize_score` uses
`format(q, "f")` and always emits fixed-point (`"0.000000000000"`). The two
12-decimal renderings of the same value therefore diverge for sub-1e-6 scores,
despite `serialize_score`'s docstring claiming identity with the store
boundary's rendering. Parse-back is safe (`Decimal("0E-12") == 0`), so impact
is shape-consistency only (fixture/diff tooling comparing serialized forms).
Suggested fix owner: whoever next touches `domain.storage_payload` — render
with `format(quantize_score(v), "f")` after confirming frozen fixtures never
contain sub-1e-6 non-zero scores.

## Gates (fresh, at submission)

- `pytest tests/test_property_*.py`: **154 passed** (exit 0)
- Full suite: **893 passed, 2 failed** — both failures in
  `tests/test_sub43_zl_zp_crossfield.py`, a sibling agent's RED-first WIP
  (their src edits to security.py/projection.py in flight); proven not mine:
  my 8 files pass together, and the failing file/tests are absent from my diff
- `ruff check tests/`: exit 0 · `ruff format --check tests/test_property_*`: clean
- `pyright`: 0 errors, 0 warnings
- `uv lock --check`: exit 0 (hypothesis 6.170.9 already in dev group/lock —
  SUB-39 landed it first; no pyproject conflict)

No commits made (pool rule). No existing test file edited. Fixtures untouched.

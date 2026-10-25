# Token Budget Contract

Contract version: 1.0.0
Status: FROZEN (2026-10-24)
Binding spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`, sections 8, 13.1, and 13.4, quoted verbatim below.
Fixtures: `tests/fixtures/token_budget_cases.json` (13 cases, all complete-payload unless noted). Verified by: `tests/test_measurement_contracts.py`.

Any change to the estimator, the minimum budget, the caps, or the frozen
baselines requires a new contract version and operator approval.

## Estimator

The dependency-free budget unit is `estimated_tokens = ceil(utf8_byte_length / 4)`;
it is not claimed to equal any provider tokenizer. The estimator applies to the
complete canonical JSON `QueryResponse` (sorted keys, compact separators,
UTF-8), including provenance, audit/projection metadata, and truncation fields.
CLI stdout, API JSON, and MCP tool content carry the same canonical payload;
transport envelopes are excluded.

## Budget rules

- Budgets below 128 estimated tokens return `BUDGET_TOO_SMALL` without knowledge payload.
- If mandatory metadata cannot fit, the result is an error rather than an over-budget success.
- Every successful response must satisfy the budget under this exact algorithm.
- Tool schemas enforce: budget is an integer `[128, configured_max]` (spec
  section 13.1).
- The `access_only=true` query for ambiguous surfaces is capped at 160 estimated
  tokens with zero knowledge payload (see `docs/contracts/access-policy.md`).

## Frozen initial baselines (spec section 13.4)

Measured from the host:

- SOUL.md 31,279 bytes / 7,820 estimated tokens
- SOUL corpus 52,604 bytes / 13,151 estimated tokens

No provider-tokenizer equality is claimed.

## Paired measurement and quality gate

Token savings are a paired measurement with a quality gate: every token-saving
claim pairs `baseline_estimated_tokens` with `system_estimated_tokens` and a
quality verdict. A case that loses quality is rejected even when it saves tokens.

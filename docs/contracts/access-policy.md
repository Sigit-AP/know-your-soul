# Conditional Access Policy

Contract version: 1.0.0
Status: FROZEN (2026-10-24)
Binding spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`, section 13.2, quoted verbatim below.
Fixtures: `tests/fixtures/access_recommendation_relearn_cases.json` (`access_cases`). Verified by: `tests/test_measurement_contracts.py`.

Any change to the access classes, the cap, or the decision cost requires a new
contract version and operator approval.

## Policy artifact

Conditional access is a hash-pinned local policy artifact, so deciding whether
to call the system never costs a model call or a graph call.

## Classes

- **L1**: L1 surfaces (greeting, one fact lookup, one command, a file read, a status check) never call the system.
- **Ambiguous**: Ambiguous cases may call `knowledge.query` with `access_only=true`, capped at 160 estimated tokens with zero knowledge payload.
- **Required**: when knowledge is needed, the system is called normally through
  the fixed tool contract.

No eleventh public tool is added: conditional access uses only the existing ten
knowledge operations.

## Fixture coverage

The fixture file pins all five L1 surface classes (`L1_greeting`,
`L1_one_fact_lookup`, `L1_one_command`, `L1_file_read`, `L1_status_check`), two
ambiguous cases at the 160-token cap with zero knowledge payload, and one
required case.

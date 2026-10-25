# Source-Version and Relearn Contract

Contract version: 1.0.0
Status: FROZEN (2026-10-24)
Binding spec: `docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`, section 13.3, quoted verbatim below.
Fixtures: `tests/fixtures/access_recommendation_relearn_cases.json` (`relearn_cases`). Verified by: `tests/test_measurement_contracts.py`.

Any change to the relearn modes, the mixed-source policy, or the rollback
guarantee requires a new contract version and operator approval.

## Source-version registry

A source-version registry stores SHA-256, byte/char/line counts, manifest hash,
parser/normalization/engine/scoring versions, `superseded_by`, and deletion
state. Every node and evidence item carries `source_version_id`. Version
identity is hash based; mtime is never a correctness gate.

## Relearn trigger

When SOUL.md or its corpus changes, the system emits a deterministic
`RelearnOffer` and sets `RELEARN_PENDING`. It never chooses a mode
automatically. No operator decision means no change.

## Modes

- RESET starts with zero old evidence; P=M=E=0, U=1, level 1, and promotion
  blocked until new evidence exists.
- ACCUMULATE reuses only nodes whose anchors map deterministically. Evidence is
  carried only where the approved mapping still holds; unmapped evidence is quarantined from scoring.

## Supersession and contradiction

Supersession uses valid-from/valid-to relation fields with audit retention,
never deletion. Contradictions are retained in both directions and resolved by
the frozen signed recency rule or left `needs_improvement` on a tie.

## Mixed source versions

Default scoring, recommendation, and promotion exclude evidence incompatible
with the active source manifest. A mixed response requires explicit operator
approval and `mixed_source_versions=true`; otherwise it fails with
`MIXED_SOURCE_VERSIONS`.

## Evidence weight of lifecycle events

Relearn, source-version, backup, and rollback events carry zero evidence weight.
Only `log_event`, `record_outcome`, and `evaluate` can move P/M/E, and relearn
can never trigger promotion.

## Backup and rollback

Every apply takes a verified atomic backup of SQLite, generation directory,
manifest, `CURRENT`, source registry, and scoring-contract version. Rollback restores exact pre-relearn hashes, graph bytes, and P/M/E values with exactly one linked audit event.

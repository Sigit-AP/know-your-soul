# Task 2 finding I4 — store boundary did not quantize Decimal scores

## Evidence

`Evidence(correctness='0.1234567890123456789')` was stored literally as
`0.1234567890123456789` in `evidence.payload_json`; the store wrote
`model_dump(mode='json')` with full Decimal precision and never called
`quantize_score`, so event hashes were computed over unquantized payloads.

## Root cause

Spec §9's serialization rule ("serialized scores are quantized to 12 decimal
places with ROUND_HALF_EVEN") was implemented in the domain layer but never
applied at the persistence boundary.

## Affected global criterion

Spec §9 serialization rule; downstream tasks assume stored scores are 12 dp.

## Smallest fix applied (ruling I4)

New `domain.storage_payload` recursively prepares a Python-mode model dump for
storage: every Decimal is quantized to 12 dp with `ROUND_HALF_EVEN` and
rendered as a string; datetimes become ISO strings. The store applies it before
persisting or hashing any payload — `event_log.payload_json`,
`evidence.payload_json`, `node_revision.payload_json` and journal
`outcome_json` — so the stored representation and the hash input are always the
quantized one. Values with more than 12 decimal places are quantized, never
rejected.

## Regression probes

- `tests/test_task2_fixes.py::test_evidence_scores_are_stored_quantized_to_12_decimal_places`
- `tests/test_task2_fixes.py::test_score_quantization_rounds_half_even_at_the_store_boundary`
  (tie probes `0.1234567890125` → down, `0.1234567890115` → up)
- `tests/test_task2_fixes.py::test_event_hashes_are_computed_over_the_quantized_payload`
  (event + node_revision payloads quantized; `verify_chain` still ok)

## Rollback note

Revert the boundary to `model_dump(mode='json')`; pre-fix databases stored
full-precision strings and remain readable, but post-fix rows quantize, so a
mixed-history database is internally consistent only because replay revalidates
each payload independently.

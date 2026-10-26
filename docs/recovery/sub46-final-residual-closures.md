# SUB-46 — final residual closures (F-2 typed rejection, F-SUB40-1, H-7, S-7)

Source: coordinator dispatch 2026-10-26 (~05:30 UTC) — the four residuals left
open after waves 1–3, the final flow/security/hygiene audits, and the codebase
push. Scope restricted to `projection.py`, `domain.py`, `observability.py`,
the related tests, and documentation. TDD: RED proven against the pre-fix
code, GREEN fresh; probe `/.hermes/cache/scratch/sub46/probe_final_residuals.py`
(exit 1 pre-fix, exit 0 post-fix).

## 1. Projection typed rejection (final-flow F-2 residual, SUB-41 handover)

**Finding.** `GraphProjector.rebuild(upto_knowledge_seq=...)` accepted a
sequence number *ahead* of the store's knowledge high watermark and wrote a
manifest whose `source_knowledge_high_watermark` lied. The activation CAS then
poisoned `active_projection_watermark`, so no honest rebuild could ever
activate again — the read model froze permanently, even across restarts.
SUB-41 closed the *heal* side (read-path repair + `recover_projection` +
`store.heal_lying_projection_watermark`) and left the preventive typed
rejection in the projector as the recorded residual.

**Fix.** `projection.py::GraphProjector.rebuild` now refuses
`upto_knowledge_seq > store.knowledge_high_watermark()` with a typed
`ProjectionValidationError` naming both values, before any generation is
built — fail-closed, no manifest written, no marker touched. The type was
chosen (not a new subclass) because the adapter maps are keyed by exact type
name and already pin `ProjectionValidationError → 422`; the heal paths are
not duplicated.

**Tests.** `tests/test_audit_final_flow.py`:

- `test_rebuild_rejects_a_future_upto_knowledge_seq_typed` (RED pre-fix:
  `DID NOT RAISE`) — typed refusal at `watermark + 1` and `10**9`, active
  manifest byte-identical afterwards, `status_for_error(...) == 422`, and the
  boundary `upto == watermark` still rebuilds.
- The two pre-existing heal tests no longer *create* the lie through the now
  rejected API; `_simulate_lying_manifest` constructs the genuinely-arising
  corrupted state (on-disk manifest tamper + poisoned CAS marker via
  `store.execute_raw`) — the heal behavior under test is unchanged.

**Probe evidence (non-vacuous).** Pre-fix the probe exits 1 and demonstrates
the original freeze live: after `rebuild(upto=3)`, the honest rebuild at
watermark 2 dies on `StalePublicationError: watermark 2 cannot replace the
active 3`. Post-fix: typed rejection, manifest untouched, boundary rebuild
ok, simulated lie still healed on read.

## 2. F-SUB40-1 — `storage_payload` Decimal shape

**Finding (SUB-40, minor).** `domain.storage_payload` rendered quantized
Decimals with `str()`, which switches to scientific notation for exact zero
(`0E-12`) and adjusted exponents below −6 (`1E-12`), while
`scoring.serialize_score` renders the same values fixed-point
(`0.000000000000`). Parse-back was always safe; only the shape differed.

**Fix.** `domain.py::storage_payload` now renders
`format(quantize_score(value), "f")` — byte-for-byte the
`serialize_score` shape. No value changes; the quantization itself is
untouched.

**Frozen-contract verification.** The change alters stored bytes only where a
quantized zero or sub-1e-6 score was persisted (`0E-12` → `0.000000000000`).
Verified empirically that nothing frozen moves:

- Task 3A fixtures (`tests/fixtures/*.json`) untouched (git-clean) and their
  suites pass fresh (`test_scoring.py`, `test_measurement_contracts.py`);
  fixtures already carry fixed-point strings produced by `serialize_score`.
- `graph.json`/`graph.html` render scores via `ScoreVector.quantized()`,
  not `storage_payload` (projection.py:836-837), so published artifacts are
  byte-stable; bench re-run post-fix: `check` exit 0, `run` exit 0, 24/24
  accepted, **0 per-case diffs** vs pre-fix ledger
  `bench/results/ledger-20261026T031852Z.json` across quality/ceiling/
  tokens/gap/ceiling_node_ids (new ledger `ledger-20261026T060457Z.json`).
- Hash chains over already-stored events re-derive from stored bytes, so
  existing databases verify unchanged; only newly written payloads carry the
  fixed-point shape.

`ScoreVector.quantized()` deliberately keeps its `str()` rendering: it feeds
the published artifacts, and changing it would rewrite frozen bench bytes for
zero behavioural gain. The shapes now differ only there, by design.

**Tests.** `tests/test_property_domain_canonical.py`: explicit pins
(zero, ±1E-12, sub-quantum, 0.5, 31337.1337 — RED pre-fix: `'0E-12' !=
'0.000000000000'`) and the fixed-point property widened to `min_value=0`
with an added `rendered == serialize_score(value)` invariant.

## 3. H-7 — dead code `StructuredLogger.to_stream`

Re-verified zero call sites before removal:
`grep -rn to_stream --include='*.py' src tests bench` → the definition only
(the one docs hit is the H-7 report itself). Removed the classmethod and the
then-unused `TextIO` import from `observability.py`. Full suite + pyright
fresh after removal.

## 4. S-7 — documented design limit: full-rewrite chain tamper

**Finding (SUB-37b, minor).** `store.verify_chain` detects *partial* tamper:
any modified event row breaks its own hash and every later link, a truncated
tail breaks the persisted anchor (`chain_head_hash`/`chain_event_count`), and
a missing anchor with events present fails closed (probe matrix T1/T2/T3/T6 in
the final security audit). What it **cannot** detect is a *full rewrite*: an
attacker with write access to the SQLite file can replace every event row,
recompute the whole hash chain, and recompute the anchor in the same
transaction — `verify_chain` then reports `ok=True` over a fully forged log,
because the anchor lives in the same database it guards.

This is a stated design boundary, not a code defect to patch: the chain is an
integrity *tripwire* against partial/corrupt mutation (and crash residue), not
a custody proof against a writer who owns the whole file. Operators who need
the stronger property must anchor outside the database:

- `backup_store` produces a manifest with SHA-256 over the protected set; a
  backup held on separate storage is an external anchor — restoring or
  diffing against it exposes a rewritten log.
- `inspect_backup` / `diagnose_projection` give the comparison surface.

The limitation is recorded in `docs/audit-release.md` (§2) as
CLOSED-as-documented; no code change was made or is recommended inside this
repository's trust model.

## Gates (fresh, post-fix)

- `pytest tests/ -q` → exit 0 (see board line for the fresh count; SUB-45's
  property wave-2 files were landing concurrently and are not this change).
- `ruff check src tests bench` exit 0; `ruff format --check src tests bench`
  clean on the touched files; `pyright` 0 errors.
- RED→GREEN and probe evidence as cited above.

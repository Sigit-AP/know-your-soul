# Task 3 finding F2 — deleted sources were never retired

## Evidence

`SeedImporter.import_directory` (src/soul_knowledge/importer.py) dropped removed
files from its incremental manifest but emitted no retire; `SourceWatcher.poll`
reported `REMOVED` yet the update pipeline (the `on_change` wiring) had no
retire path. A deleted file's nodes lingered in the store and in every later
projection, and the shrink guard (src/soul_knowledge/projection.py
`_build_generation`) blocked any correcting rebuild.

## Root cause

No production path translated a watcher `REMOVED` change into `node.retire`
domain events, and no scoped `allow_shrink` rebuild existed for that path.

## Affected global criterion

Spec §7 event/projection flow: the projection must reflect the live source set;
deletions are knowledge-affecting mutations, and the append-only store must
retain their history.

## Smallest fix applied (controller ruling, binding)

- `SeedImporter.retire_source(source_scope) -> tuple[str, ...]`: replays domain
  events to find live nodes whose import scope (`payload["source_file"]`)
  matches, then emits `node.retire` through `EventStore.commit` with
  retire-scoped idempotency keys
  (`retire:{scope}:{node_id}:{node_version}`), so a replayed removal is a
  no-op and retired nodes stay in history as superseded revisions.
- New `src/soul_knowledge/update_pipeline.py::update_from_changes`: the
  watcher-driven update pipeline. `REMOVED` changes retire first; all batches
  re-import incrementally; the rebuild runs with `allow_shrink=True` exactly
  when the batch carried a removal — scoped to the retire pipeline, never a
  projector-wide setting. A plain `rebuild()` still trips the shrink guard.

## Regression probes

- `tests/test_update_pipeline.py::test_removed_source_is_retired_and_leaves_the_projection`
  (delete → poll → nodes superseded in the store with version bumped, retire
  events in the replay, the next projection no longer contains them, a second
  identical poll is a full no-op: no changes, no new events, same graph)
- `tests/test_update_pipeline.py::test_retire_batch_replay_is_idempotent`
  (replaying the same change batch retires nothing and moves no watermark)
- `tests/test_update_pipeline.py::test_shrink_guard_still_blocks_an_unscoped_shrink`
  (a plain rebuild after an out-of-pipeline retire still raises
  `ShrinkGuardError`; the scoped pipeline rebuild succeeds)

## Rollback note

Remove `update_pipeline.py` and `SeedImporter.retire_source`; already-committed
`node.retire` events are append-only history and remain valid (the projection
replayer has handled `NODE_RETIRE` since Task 3), so rollback only loses the
automated retire trigger, never data.

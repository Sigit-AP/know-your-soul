# Arch review F-1 — serve-time source watcher wiring (dead flow closed)

## Evidence

Architecture review (`.superpowers/sdd/2026-10-24-superpower-knowledge-soul/review-architecture.md`,
F-1, MEDIUM): `source_watcher.py` and `update_pipeline.py` (~370 LOC plus
tests) had no production caller — no CLI subcommand, API route, MCP tool or
service method invoked `SourceWatcher.poll` / `update_from_changes`. The only
live source-change detection was the manual `ops/relearn_scan`. Spec 13.5
requires the doc-change watcher to drive the deterministic update pipeline.

## Decision

Wire, not delete (operator choice via SUB-54 brief): the watcher now runs for
the API server's lifespan and publishes the gated relearn offer on corpus
change. Deleting would have abandoned the spec-13.5 flow the plan Task 3
explicitly produced (`SourceWatcher.poll()` + "deterministic doc-change
update triggering").

## Fix applied

* `update_pipeline.SourceWatchLoop` — the lifecycle driver. Polls a
  `SourceWatcher` on an interval and routes every change batch through an
  injected gate callback. Typed `UpdatePipelineError` on invalid timing or a
  double start; `stop()` is idempotent; the daemon thread is named
  `soul-source-watch`.
* Cooldown: after a poll whose callback emitted an offer, the loop pauses for
  `cooldown_seconds`; edits during the pause accumulate on disk and arrive as
  one batched poll (the watcher snapshot is untouched while paused, so no
  change is ever absorbed silently).
* Fail-isolated: watcher errors (`WatcherStateError`, `OSError`) and callback
  failures become typed `WatchPollReceipt` errors, are logged, and back the
  poll off exponentially (cap `max_error_backoff_seconds`) — the loop never
  takes its server down.
* `http_api.create_app`/`serve` accept `watch_root` (+ interval/cooldown/
  state path). The loop starts/stops with the Starlette lifespan, so it rides
  exactly the server's lifetime. The gate callback is the service's audited
  `offer_relearn(root, SOUL_CORPUS, actor="source-watcher")` — **the same
  gated path as `ops/relearn_scan`**: offer + `RELEARN_PENDING`, never an
  auto-apply, never a baseline flip (final-audit F-4 semantics preserved).
* Startup validation (typed, fail-closed): watch root must be an existing
  directory; the watcher snapshot defaults to `source-watch.json` alongside
  the store file and **must resolve outside the watched root** (a state file
  inside would perturb the registry's all-files `SOUL_CORPUS` fingerprint and
  mint spurious offers); an in-memory store requires an explicit
  `watch_state_path`.
* `SourceWatcher(patterns=None)` watches every regular file — the same file
  set the registry's `SOUL_CORPUS` scan hashes, so watcher detection and the
  registry fingerprint can never disagree about what counts as a change.
* CLI: `soul-knowledge serve --watch PATH [--watch-interval S]
  [--watch-cooldown S] [--watch-state PATH]`.

The detection half is what is wired. `update_from_changes` /
`apply_approved_update` remain the library form of the gated batch flow
(their tests pin them); the apply half stays operator-driven through
`ops/relearn_preview`/`relearn_apply` — nothing in the watcher path may apply
or flip the baseline on its own.

## Regression probes

`tests/test_serve_watcher.py` (17 tests), notably:

- `test_serve_watcher_emits_offer_without_intervention_and_holds_the_baseline`
  — the review probe: edit the corpus, the offer appears with no operator
  action; the approved baseline id, the knowledge watermark and the
  projection `CURRENT` pointer are byte-identical afterwards; the detection
  is journaled as `knowledge.relearn_offer` with actor `source-watcher`.
- `test_serve_watcher_survives_restart_without_duplicate_offers` — restart
  with an unchanged corpus re-emits nothing; a post-restart edit is detected.
- `test_serve_watcher_error_isolation_keeps_the_server_up` — a corrupt
  watcher state file never 500s the API; removing it resumes detection.
- `test_running_loop_cools_down_after_an_offer` — the cooldown batches a
  save storm into one offer.
- Loop lifecycle/config typed errors, callback-failure isolation, state
  recovery, CLI flag forwarding.

## Rollback note

Drop `--watch` usage; without `watch_root` the app builds exactly the
pre-change Starlette app (the lifespan is only attached when watching).

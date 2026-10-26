# docs/assets — generated visual assets

All SVGs here are generated from **real project data** by
`tools/generate_visual_assets.py` — no mockups, no network, no AI calls at
generation time. Dark theme, system font stacks, commitable plain SVG.

| File | What it shows | Data source |
|------|---------------|-------------|
| `graph-formation.svg` | How nodes are formed: corpus files → seed-import events → typed relations → projected graph; the full real graph (36 nodes / 35 edges) with a hand-rolled deterministic radial layout, nodes coloured by primary domain P1–P7 | live projection of the frozen `bench/corpus` (same build path as `bench/run.py`) |
| `learning-timeline.svg` | P/M/E score evolution of a real graph node over a deterministic 12-outcome demo scenario, scored by the frozen `ScoreEngine` at fixed timestamps; plus cumulative event-store growth over the real seed-import replay | real `KnowledgeService.record_outcome` + `ScoreEngine.score_node`; real `EventStore.replay_domain` |
| `bench-impact.svg` | Per-case token saving vs the raw-corpus baseline (mean ≈ 94 %), quality-gate verdicts, system tokens and p50 latency | real bench ledger `bench/results/ledger-*.json` (newest with ≥ 20 measured cases) |
| `architecture.svg` | Layered architecture: adapters → service → domain/scoring → projection → event store, cross-cutting concerns | real module map of `src/know_your_soul` |

## Regenerate

From the repository root, with the locked environment:

```bash
uv run python tools/generate_visual_assets.py
```

Pin a specific bench ledger for `bench-impact.svg` (otherwise the newest
measured ledger is used):

```bash
uv run python tools/generate_visual_assets.py --ledger ledger-20261026T184651Z.json
```

Verify the committed assets are byte-identical to a fresh regeneration
(used by `tests/test_visual_assets.py`):

```bash
uv run python tools/generate_visual_assets.py --check
```

## Determinism notes

* The projection build is content-addressed; node ids, labels, domains and
  edges derive from the sha256-pinned corpus.
* The radial layout is a pure function of sorted node ids and edge order —
  no wall-clock, no RNG state, no dict-order dependence.
* The scoring demo uses fixed evidence values and fixed timestamps, and
  scores against an explicit `at=` instant, so recency decay never depends
  on when the generator runs.
* `bench-impact.svg` records its source ledger in an XML comment at the top
  of the file; `--check` against that pinned ledger stays green even after
  newer ledgers land.

# Institutional graph benchmark

Graph-algorithm benchmark in the style of the two institutional standards,
[LDBC Graphalytics](https://ldbcouncil.org/benchmarks/graphalytics/) and the
[GAP Benchmark Suite](https://github.com/sbeamer/gapbs) — kernels, matchers,
and methodology verified from the official sources. Deterministic, no-AI:
every kernel runs against a reference implementation and the verdict is
PASS/FAIL on correctness plus a measured performance ratio.

## Run it

```bash
uv run python bench/institutional/run.py              # full report, writes results/
uv run python bench/institutional/run.py --strict     # exit 1 if any kernel FAILs
uv run python bench/institutional/run.py --repeats 5  # more timing repeats
```

## What it measures

Eight kernels, each validated against a reference (networkx / numpy
self-validated implementations) on eleven datasets with known-correct answers:

| Kernel | What it checks |
|---|---|
| `pagerank` | epsilon tolerance vs converged reference |
| `wcc` | weakly connected components, set equivalence |
| `cdlp` | community detection via label propagation, exact cover + equivalence |
| `lcc` | local clustering coefficient, epsilon tolerance |
| `tc` | triangle count, exact integer match |
| `louvain` | partition validity + modularity cross-validation |
| `bc` | betweenness centrality, exact vs Brandes reference |
| `cycles` | cycle set equality vs reference enumeration |

Kernels the product does not expose (BFS, SSSP) are reported as honest skips.

## Latest results

**8/8 kernels PASS** (verdicts from real runs, `--strict` exit 0). Full
per-dataset detail: `results/institutional-latest.{json,md}`.

Datasets are generated deterministically (seeded, no network) so the benchmark
runs anywhere offline. See `docs/recovery/institutional-benchmark.md` for the
full methodology and source citations.

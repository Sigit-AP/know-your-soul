# Final hygiene fixes — documentation + repo hygiene (H-1..H-9)

- Date: 2026-10-26 (SUB-44), repo HEAD `51565d3` + in-flight fix waves.
- Scope: docs/hygiene only. Source of findings:
  `.superpowers/sdd/2026-10-24-superpower-knowledge-soul/audit-final-hygiene.md`.
- Every number below comes from a command run by the fixer on this host
  (command → output → exit code recorded per item).

## H-1 (Important) — README bench commands fail as written — FIXED

Reproduced: `python bench/run.py run` with the shell's plain `python`
→ `ModuleNotFoundError: No module named 'soul_knowledge'`, exit 1.
Fix: `README.md` install block now uses `uv sync --frozen` +
`uv run python bench/run.py check|run` (verified fresh, both exit 0:
`check: OK (cases schema, frozen corpus integrity, estimator parity)`;
`run: OK; ledger -> bench/results/ledger-20261026T031852Z.json`, 24/24
accepted). `bench/README.md` command block updated the same way.
`uv sync --frozen`, `uv run pytest -q` (864 passed in a mid-window run;
final settled run below), `uv run pyright` all verified exit 0 on this host.

## H-2 (Important) — stale "integration pending" notes — FIXED

Removed `(exit 3 while integration is pending)` from `README.md`; removed
exit-code-3 row and the "Integration skip-gate" section (incl. the
"The gate opens automatically when Tasks 7-8 land" note) from
`bench/README.md`. Replaced with the current behavior: `run` exits 0 with
24/24 accepted under gate contract 2.1.0.

## H-3 (Important) — stale bench numbers in verification.md — FIXED

Re-measured from fresh ledger `bench/results/ledger-20261026T031852Z.json`
(`uv run python bench/run.py run`, exit 0, repeats=5, status ok):

| Metric | Old (stale, ledger 20261025T202627Z) | Fresh (20261026T031852Z) |
|--------|--------------------------------------|---------------------------|
| Token saving mean / min | 94.26% / 92.45% | **94.18% / 92.64%** |
| System est. tokens | 412–993 | **420–968** |
| Latency p50 median (24 cases) | 6.43 ms | **14.29 ms** (host-dependent) |
| Latency p95 median | 7.43 ms | **20.40 ms** (host-dependent) |
| Quality mean / ceiling mean | 0.4451 / 0.4042 | **unchanged** (verified identical) |
| `gap_vs_raw_read` mean | 0.5549 | **unchanged** (0.5549) |

`docs/verification.md` §2 table and `docs/audit-release.md` §4/§5 updated;
the shift is attributed to W3-2 (`c1165b5`, injective ids).

## H-4 (Important) — stale test counts — FIXED

Fresh: `.venv/bin/python -m pytest tests/ -q` →
`898 passed, 1 warning in 220.12s`, exit 0 (732 core + SUB-40 property
tests + SUB-43 redaction probes, both landed concurrently; a mid-window
run measured `864 passed, 1 warning in 186.17s`, also exit 0).
`ruff format --check .` →
`1 file would be reformatted, 162 files already formatted`, exit 1 (see
H-8). `docs/verification.md` §1 and `docs/audit-release.md` §4 updated;
the 632/690 rows are kept only as explicitly-labeled historical rows.

## H-5 (Important) — 39/40 vs 40/40 contradiction — FIXED

`docs/audit-release.md` §4 now states **40/40 = 100%**, consistent with
`docs/verification.md` §3 and the matrix. Verified fresh:
`pytest tests/ -q -k GraphifyCoverage` → `3 passed`, exit 0 (the gate
re-runs all 40 named contract tests). `docs/research/graphify-feature-matrix.md`
gained a current-status cell ("Verified adopted coverage at release
2026-10-26: 40/40 = 100%") next to the historical Task 1/Task 3 cells.

## H-6 (Minor) — ledger evidence not reproducible from repo — FIXED (staged, not committed)

- All 8 tracked `skipped`-era ledgers (`20261025T110519Z..112325Z`, all
  verified `status: skipped` programmatically) removed from tracking via
  `git rm --cached` (files kept on disk).
- `.gitignore`: `bench/results/` → `bench/results/*` with an explicit
  exception for the fresh green evidence ledger
  `!bench/results/ledger-20261026T031852Z.json` (force-added, staged).
- Changes are **staged only** — the coordinator commits (project rule).

## H-7 (Minor) — dead code `StructuredLogger.to_stream` — REPORTED, not touched

Verified: `grep -rn to_stream --include='*.py'` → single hit, the
definition itself (`src/soul_knowledge/observability.py:215`,
classmethod). Zero call sites in `src/`, `tests/`, `bench/`. The file is
owned by another in-flight fix wave (visible as modified in
`git status`), so it is reported here for the owner to remove or wire —
not edited.

## H-8 (Minor) — pre-existing format drift in scoring.md — DOCUMENTED

`ruff format --check .` exit 1, sole offender
`docs/mathematics/scoring.md` (comment alignment in a fenced code block).
Verified pre-existing: `git log --follow docs/mathematics/scoring.md`
shows a single commit `724843c` ("feat: add explainable dynamic knowledge
scoring") — the drift predates every fix wave. `docs/contracts/` +
`docs/mathematics/` are frozen, so the drift is documented in
`docs/verification.md` §1 (format row), not reformatted.

## H-9 (Minor) — task-era prose + install-story contradiction — FIXED

- `docs/operations.md`: removed forward references ("Task 9 core",
  "wiring lands in Task 7", "delivered by Task 7 (cli.py)"); the
  Container section now states the install story explicitly and
  consistently with `deploy/Dockerfile`'s header note:
  **local/CI reproduction = `uv sync --frozen` (bit-exact, uv.lock);
  container build = `pip install .` (pyproject ranges, pip does not read
  uv.lock — use `uv export` constraints or uv for a bit-exact image).**
- `deploy/Dockerfile` header comment verified consistent with the above
  (it already documents the pip-vs-lock caveat). One residual outside
  this fix's scope (deploy/ is owned elsewhere): the Dockerfile
  ENTRYPOINT comment still says "delivered by Task 7 (cli.py)".
- `docs/research/graphify-feature-matrix.md`: added the current 40/40
  status cell (H-9 last bullet).

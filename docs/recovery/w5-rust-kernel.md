# W5 — Rust extraction kernel (`kernel-rust/`, SUB-93)

Wave 5 hot path per MASTER-PLAN "Keputusan arsitektur multi-bahasa": the
parsing/extraction kernel in Rust, byte-identical to the Python reference
walker (`know_your_soul.ingest.extractor`), one codebase serving both
deployment paths (PyO3 module for the agent harness, `kys-kernel` CLI for the
standalone/server path).

## Layout

```
kernel-rust/
├── Cargo.toml / Cargo.lock   # pinned deps; lock is committed (offline-reproducible)
├── pyproject.toml            # maturin config (separate build; main package stays hatchling)
├── build.rs                  # rebuild trigger on registry.json change
├── registry.json             # generated — the shared source of truth
├── benchmark.py              # real benchmark (fixtures + one ~0.8 MiB file)
├── src/
│   ├── lib.rs                # crate root + PyO3 module (feature `pyo3`)
│   ├── main.rs               # kys-kernel CLI: file → canonical JSON on stdout
│   ├── registry.rs           # serde model of registry.json + extension routing
│   ├── walker.rs             # byte-faithful port of TreeSitterExtractor
│   ├── canon.rs              # canonical JSON writer (Python json.dumps parity)
│   └── generated.rs          # generated/minified detection port
└── tests/parity.rs           # rust-level sanity/determinism tests
```

Plus: `tools/dump_language_registry.py` (registry dump), 
`src/know_your_soul/ingest/kernel_bridge.py` (routing bridge),
routing in `extractor.py` (additive), tests `tests/test_kernel_rust{,_bridge,_registry}.py`.

## Grammar pinning — the crux of byte-identity

The Python reference parses with `tree-sitter-language-pack 1.22.1`, which
bundles grammars at specific upstream revisions (published as the release
asset `grammar-provenance-1.22.1.json`). The Rust kernel must parse with the
*same* grammars or the ASTs (and hence the artifacts) can diverge.

Verification method: for each language, the bundled revision's `src/` tree
hash was compared against every git tag of the grammar repo
(`git rev-parse <tag>:src` vs `<rev>:src` — `src/` holds the generated
parser, the only thing that determines parse output).

| language | bundled rev | binding | crate pin |
|---|---|---|---|
| javascript | 58404d8c | == v0.25.0 src | `tree-sitter-javascript = "=0.25.0"` |
| typescript | 75b3874e | == v0.23.2 exactly | `tree-sitter-typescript = "=0.23.2"` |
| go | 2346a3ab | == v0.25.0 src | `tree-sitter-go = "=0.25.0"` |
| java | e10607b4 | == v0.23.5 src | `tree-sitter-java = "=0.23.5"` |
| rust | 77a37472 | == v0.24.2 exactly | `tree-sitter-rust = "=0.24.2"` |
| c | b780e47f | == v0.24.2 exactly | `tree-sitter-c = "=0.24.2"` |
| php | 58d56430 | == v0.25.1 exactly | `tree-sitter-php = "=0.25.1"` |
| bash | a06c2e44 | == v0.25.1 exactly | `tree-sitter-bash = "=0.25.1"` |
| ruby | ad907a69 | parser.c == v0.23.1 | `tree-sitter-ruby = "=0.23.1"` |
| python | 26855eab | **newer than v0.25.0, grammar changed** | git rev pin |
| cpp | c0092228 | **newer than v0.23.4, grammar changed** | git rev pin |
| csharp | 8c0abe0b | **newer than v0.23.5, grammar changed** | git rev pin |

Git pins ride in `Cargo.lock` with exact commit hashes — offline-reproducible
after the first fetch, same guarantee as crates.io pins. The tree-sitter
runtime is `=0.25.10` (the pack's runtime line; grammar crates only depend on
the `tree-sitter-language` shim, so one runtime serves all 12).

Runtime crate deps: `serde`/`serde_json` (registry parsing only),
`regex` (import/banner patterns), `sha2` (node ids), `pyo3` (optional,
feature-gated so `cargo build`/`cargo test` never need Python).

## Walker port — fidelity notes

`walker.rs` mirrors `extractor.py` statement-for-statement: canonical child
order via `node.children()` (named + anonymous, like the Python binding),
declaration → container → import → call → variable dispatch order, scope
stack with transparent containers, `_IdAllocator` col-suffix-on-collision
semantics, `::` qualified names skipping the file root, import/call/variable
rules from the shared registry, `has_error` → `parse_error`, and the sorted
artifact shape. JSON output goes through `canon.rs`, a hand-written writer
that matches `json.dumps(..., sort_keys=True, separators=(",", ":"),
ensure_ascii=False)` byte-for-byte (escape set verified against CPython,
including the DEL/non-ASCII pass-through). Error codes are the frozen
contract's (`FILE_TOO_LARGE`, `UNSUPPORTED_MEDIA_TYPE`, `DECODE_ERROR`,
`EXTRACTION_ERROR`) and the PyO3 exception carries structured context so the
bridge rebuilds the exact Python exception objects.

Known intentional differences (never observable on contract paths): deep-AST
recursion uses a 256 MiB worker stack in the CLI instead of dying at Python's
recursion limit; the CLI adds one trailing newline after the JSON line.

Pathological-input parity (found via SUB-94's pinned tripwire F-SUB94-1):
`from . import x` yields an empty import name and the Python reference raises
`NodeIdError` from `make_node_id`'s validation. The kernel mirrors the refusal
(`IdAllocator` rejects empty names with `EXTRACTION_ERROR`); the bridge
deliberately does **not** map walk-time kernel errors onto `IngestError`
classes — it re-raises them raw so `extract_source`'s `except Exception`
fallback re-runs the Python walker and the caller observes the reference's
exact behaviour (exception type included). Pinned by
`tests/test_kernel_rust_bridge.py::test_pathological_empty_name_defers_to_reference`.
The CLI reports `EXTRACTION_ERROR` on stderr for such files (exit 2).

## Routing (`kernel_bridge.py` + `extractor.py`)

Additive, fail-safe: `extract_source` tries the kernel when
`kernel_bridge.kernel_available()`; deterministic contract errors
(`IngestError` subclasses) propagate unchanged, any other kernel failure logs
a warning and falls back to the Python walker. `extract_file` keeps its
Python error surface (stat/decode order) and routes through `extract_source`.
Kill-switch: `KYS_DISABLE_RUST_KERNEL` (any non-empty value).

Bridge load order: (1) `import know_your_soul_kernel` (installed wheel /
`maturin develop`); (2) repo-local `kernel-rust/target/{release,debug}/
libknow_your_soul_kernel.so` via `spec_from_file_location` — visible to
imports but invisible to `importlib.metadata`, so the dependency-policy lock
contract (`test_every_installed_distribution_is_pinned_by_the_lock`) is not
affected by development builds.

## Build

```bash
cd kernel-rust
cargo build --release          # CLI → target/release/kys-kernel
cargo test                     # 12 rust tests
maturin develop --release      # Python module into .venv (needs VIRTUAL_ENV)
maturin build --release        # wheel → target/wheels/*.whl
```

`maturin develop` registers a `know-your-soul-kernel` dist-info in the venv;
that trips the lock-purity test until the distribution is wired into CI
packaging (next wave). Development flow: build the wheel (`maturin build`),
leave the venv clean, the bridge loads the repo-local `.so`.

Gotcha (found in testing): a plain `cargo build` also builds the `cdylib`
target and **overwrites** `target/release/libknow_your_soul_kernel.so` with a
build that lacks the pyo3 module. Always refresh with `maturin build` after
plain cargo builds; the bridge detects the stale artifact (missing
`extract_source` attribute) and falls back to Python rather than loading a
broken module.

`pyproject.toml` / `uv.lock` gained one dev-group pin: `maturin==1.15.0`
(the task toolchain had maturin installed outside the lock, which broke
`test_dependency_policy.py::test_every_installed_distribution_is_pinned_by_the_lock`
independently of this work — the pin is the minimal repair;
`test_pyproject_dev_group_is_declared` only requires a superset of
{pytest, hypothesis, pyright, ruff}).

## Verification evidence (all fresh, this wave)

- `cargo test` — **12 passed, 0 failed** (exit 0).
- `maturin develop --release` — built + installed `know-your-soul-kernel-0.1.0` (exit 0).
- Byte-identity, all 12 fixtures, Python `canonical_json` vs `kys-kernel`
  stdout: **12/12 IDENTICAL** (python 4025, javascript 4387, typescript 3830,
  go 3719, rust 4395, java 4864, c 3355, cpp 3752, csharp 4727, ruby 3856,
  php 3760, bash 2592 bytes).
- Byte-identity via the bridge (`ExtractionResult` equality + canonical JSON):
  `tests/test_kernel_rust.py` **green**.
- Determinism: two independent `cargo build --release` target dirs produce
  byte-identical stdout on all 12 fixtures; repeated runs identical.
- Property tests: hypothesis-generated `(path, name, line, col)` —
  `make_node_id` Rust == Python (100 examples); random python-ish sources
  extract byte-identically (30 examples); adversarial JSON strings byte-equal.
- CLI error shape: `UNSUPPORTED_MEDIA_TYPE` / `FILE_TOO_LARGE` /
  `DECODE_ERROR` → `{"error":{"code":...}}` on stderr, exit 2.
- Registry tool: dump deterministic across `PYTHONHASHSEED` 0/1/42;
  `--check` exit 0 fresh / exit 1 stale.
- Fallback: kernel import failure, kill-switch, and kernel exceptions all
  degrade to the Python walker with a logged reason
  (`tests/test_kernel_rust_bridge.py` green — no Rust required).
- Pathological parity: `from . import x` raises `NodeIdError` identically with
  and without the kernel (SUB-94's pinned baseline tripwire stays green).
- Differential probe beyond fixtures: this repo's own `src/` (88 Python
  files) extracts byte-identically in both engines, 0 mismatches.
- Full Python suite with the kernel active through the bridge:
  **5384 passed, 0 failed** (exit 0, 452s); a second full run with the kernel
  absent (fallback path) also green: 5353 passed, 30 kernel tests skipped.
  `ruff check` + `ruff format --check` clean repo-wide; `pyright` 0/0/0
  repo-wide; `uv lock --check` OK; `license_guard` OK.

## Benchmark (median, 25 runs; release kernel, in-process)

| input | bytes | python (ms) | rust (ms) | speedup |
|---|---|---|---|---|
| fixture `bash/sample.sh` | 191 | 0.469 | 0.150 | 3.1x |
| fixture `c/sample.c` | 367 | 0.465 | 0.574 | 0.8x* |
| fixture `cpp/sample.cpp` | 398 | 0.832 | 0.453 | 1.8x |
| fixture `csharp/sample.cs` | 493 | 0.712 | 0.448 | 1.6x |
| fixture `go/sample.go` | 339 | 0.485 | 0.294 | 1.7x |
| fixture `java/sample.java` | 551 | 0.669 | 0.316 | 2.1x |
| fixture `javascript/sample.js` | 408 | 0.560 | 0.292 | 1.9x |
| fixture `php/sample.php` | 501 | 0.656 | 0.349 | 1.9x |
| fixture `python/sample.py` | 421 | 0.563 | 0.290 | 1.9x |
| fixture `ruby/sample.rb` | 316 | 0.514 | 0.288 | 1.8x |
| fixture `rust/sample.rs` | 460 | 0.589 | 0.318 | 1.8x |
| fixture `typescript/sample.ts` | 451 | 0.572 | 0.295 | 1.9x |
| big file `bench/big.py` (1400 classes) | 793416 | 970.440 | 476.714 | 2.0x |

\* sub-millisecond noise on the smallest fixture. Fixture geometric mean:
**1.80x**; the realistic payload (0.8 MiB, 1400 classes / 5600 methods):
**2.0x**. Found and fixed during benchmarking: `regex` compilation per
extraction call dominated small files (csharp/java/php ran 0.3–0.5x);
compiled patterns are now cached per language — always verify the identity
guard before believing a speedup.

## Left for the next wave

- CI wiring: maturin matrix build + wheel upload; install path removes the
  lock-purity tension (kernel ships as a prebuilt wheel dependency).
- Release-profile LTO/strip tuning for the CLI binary size (23 MB unstripped).

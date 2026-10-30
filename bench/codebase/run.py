#!/usr/bin/env python3
"""Codebase benchmark: ingestion + retrieval over REAL open-source codebases.

SUB-94 (wave 5). Deterministic, no-AI measurement of the product's code
ingestion pipeline (``know_your_soul.ingest.pipeline``, the real tree-sitter
extractor and the two-phase resolver, used as-is) and retrieval v2
(``SymbolLookup`` + ``CallHierarchy``) against real OSS codebases pinned by
commit and vendored under ``data/``.

Dataset (manifest.json, per-file sha256):

* ``requests``           — psf/requests @ v2.32.4 (Python, well-known)
* ``chalk``              — chalk/chalk @ v5.6.2 (JavaScript, well-known)
* ``tree-sitter-python`` — tree-sitter/tree-sitter-python @ v0.23.6
  (multi-language: C scanner, JavaScript grammar, Python bindings)
* ``fixture``            — tiny hand-written py+js library (in ``fixture/``),
  the manually verified ground-truth anchor for exact callers/callees.

Subcommands::

    fetch    # download the pinned archives (NETWORK) and repack them
             # deterministically into data/ + manifest.json
    verify   # vendored-archive + per-file integrity vs manifest (offline)
    check    # no-dataset self-checks (fixture, ground truth, baseline)
    run      # full measurement -> results/ledger-<ts>.json + report-<ts>.md

Gates (``run`` exits 0 only when all hold):

* correctness (strict): every ground-truth lookup hits (fixture: top-1,
  real repos: hit@10), every hierarchy query returns the exact expected
  caller/callee set, the negative lookup returns nothing;
* determinism (strict): two ingestions of the same target produce
  byte-identical ``canonical_ingestion_dump`` output;
* regression vs baseline.json: node/edge/ref/resolved counts must match the
  stored baseline exactly (truth is strict); timings must stay within a
  loose factor (TIMING_TOLERANCE, default 10x + 250ms slack) of baseline —
  perf regressions are surfaced, machine jitter is tolerated. On shared CI
  runners (``GITHUB_ACTIONS``) the timing comparison is advisory only: it is
  reported, never fatal — jitter on a noisy runner is not a regression,
  while counts and failed-file lists stay STRICT there too.

No network is used outside ``fetch``; ``verify``/``check``/``run`` are fully
offline.
"""

from __future__ import annotations

import argparse
import gzip
import hashlib
import io
import json
import os
import sys
import tarfile
import tempfile
import time
import tracemalloc
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

DATA_DIR = BENCH_DIR / "data"
RESULTS_DIR = BENCH_DIR / "results"
FIXTURE_DIR = BENCH_DIR / "fixture"
MANIFEST_PATH = BENCH_DIR / "manifest.json"
GROUND_TRUTH_PATH = BENCH_DIR / "ground_truth.json"
BASELINE_PATH = BENCH_DIR / "baseline.json"

DATASET_FETCHED = "2026-10-28"
MANIFEST_VERSION = 1
GROUND_TRUTH_VERSION = 1
BASELINE_VERSION = 1

#: Loose timing tolerance for the regression gate: machine jitter must never
#: fail the gate, a real order-of-magnitude slowdown must.
TIMING_TOLERANCE = 10.0
TIMING_SLACK_MS = 250.0


def _truthy(value: str | None) -> bool:
    return value is not None and value.strip().lower() in {"1", "true", "yes", "on"}


def timing_gate_enabled(env: dict[str, str] | None = None) -> bool:
    """Whether the LOOSE timing comparison may fail the gate.

    Shared CI runners (``GITHUB_ACTIONS``) are noisy neighbours: the
    10x + 250ms window was measured against a quiet machine and wall-clock
    jitter alone blows past it (walk_ms 891ms vs a 762ms window, observed on
    both ubuntu and macos jobs). On CI the timing comparison is therefore
    advisory — violations are printed, never fatal. The truth gates (counts,
    failed-file lists, determinism) stay STRICT on every platform.
    """
    environ = os.environ if env is None else env
    return not _truthy(environ.get("GITHUB_ACTIONS"))


DEFAULT_REPEATS = 20
LOOKUP_TOP_K = 10

#: The pinned dataset. `commit` is the peeled tag commit; the fetch URL is a
#: function of the commit, so the bytes can never drift behind the pin.
REPOS: tuple[dict[str, str], ...] = (
    {
        "name": "requests",
        "slug": "psf/requests",
        "tag": "v2.32.4",
        "commit": "021dc729f0b71a3030cefdbec7fb57a0e80a6cfd",
        "language": "python",
    },
    {
        "name": "chalk",
        "slug": "chalk/chalk",
        "tag": "v5.6.2",
        "commit": "51557784b829c87ff8d138206598764f2eb957b1",
        "language": "javascript",
    },
    {
        "name": "tree-sitter-python",
        "slug": "tree-sitter/tree-sitter-python",
        "tag": "v0.23.6",
        "commit": "bffb65a8cfe4e46290331dfef0dbf0ef3679de11",
        "language": "multi (c/javascript/python)",
    },
)


def archive_url(spec: dict[str, str]) -> str:
    return f"https://codeload.github.com/{spec['slug']}/tar.gz/{spec['commit']}"


# ------------------------------------------------------------------ hashing


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# ------------------------------------------------------- deterministic tar
#
# GitHub's codeload tarballs are NOT byte-stable, so `fetch` repacks the
# extracted tree into a canonical tar.gz: entries sorted bytewise, zeroed
# mtimes/owners, fixed modes, gzip mtime=0. The vendored archives in data/
# are therefore byte-reproducible from the pinned commits.


def canonical_tarball(root: Path) -> bytes:
    paths = sorted((p for p in root.rglob("*") if p.is_file()), key=lambda p: p.relative_to(root).as_posix())
    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            seen_dirs: set[str] = set()
            for path in paths:
                rel = path.relative_to(root).as_posix()
                parts = rel.split("/")[:-1]
                for i in range(1, len(parts) + 1):
                    dirname = "/".join(parts[:i])
                    if dirname not in seen_dirs:
                        seen_dirs.add(dirname)
                        info = tarfile.TarInfo(dirname)
                        info.type = tarfile.DIRTYPE
                        info.mode = 0o755
                        info.mtime = 0
                        info.uid = info.gid = 0
                        info.uname = info.gname = ""
                        tar.addfile(info)
                data = path.read_bytes()
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mode = 0o755 if _is_executable(path) else 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def _is_executable(path: Path) -> bool:
    import os

    return bool(os.stat(path).st_mode & 0o111)


def extract_archive(archive: Path, dest: Path) -> list[str]:
    """Extract a vendored archive into dest; returns sorted relpaths.

    Fail-closed on absolute paths / ``..`` segments.
    """
    names: list[str] = []
    with tarfile.open(archive, "r:gz") as tar:
        for member in tar.getmembers():
            name = member.name
            if name.startswith("/") or ".." in name.split("/"):
                raise SystemExit(f"unsafe archive member: {name!r}")
            if member.isdir():
                continue
            target = dest / name
            target.parent.mkdir(parents=True, exist_ok=True)
            src = tar.extractfile(member)
            assert src is not None
            target.write_bytes(src.read())
            names.append(name)
    return sorted(names)


# ------------------------------------------------------------------ fetch


def cmd_fetch() -> int:
    import urllib.request

    DATA_DIR.mkdir(parents=True, exist_ok=True)
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "fetched": DATASET_FETCHED,
        "note": "codeload tarballs are not byte-stable; archives here are canonical repacks (see run.py).",
        "repos": [],
    }
    for spec in REPOS:
        url = archive_url(spec)
        print(f"fetching {spec['name']}: {url}")
        with tempfile.TemporaryDirectory(prefix="kys-codebench-") as tmp:
            tmpdir = Path(tmp)
            blob_path = tmpdir / "orig.tar.gz"
            with urllib.request.urlopen(url, timeout=180) as resp:  # noqa: S310 - pinned public archive
                blob_path.write_bytes(resp.read())
            src_dir = tmpdir / "src"
            src_dir.mkdir()
            with tarfile.open(blob_path, "r:gz") as tar:
                tar.extractall(src_dir, filter="data")
            roots = list(src_dir.iterdir())
            if len(roots) != 1 or not roots[0].is_dir():
                print(f"FATAL: unexpected archive layout for {spec['name']}", file=sys.stderr)
                return 1
            tree = roots[0]
            archive_bytes = canonical_tarball(tree)
            archive_path = DATA_DIR / f"{spec['name']}.tar.gz"
            archive_path.write_bytes(archive_bytes)
            files = []
            for path in sorted(p for p in tree.rglob("*") if p.is_file()):
                rel = path.relative_to(tree).as_posix()
                files.append({"path": rel, "sha256": sha256_file(path), "size": path.stat().st_size})
        manifest["repos"].append(
            {
                "name": spec["name"],
                "slug": spec["slug"],
                "tag": spec["tag"],
                "commit": spec["commit"],
                "url": url,
                "language": spec["language"],
                "archive": {
                    "file": f"data/{spec['name']}.tar.gz",
                    "sha256": sha256_bytes(archive_bytes),
                    "bytes": len(archive_bytes),
                },
                "file_count": len(files),
                "total_bytes": sum(f["size"] for f in files),
                "files": files,
            }
        )
        print(f"  {len(files)} files, archive {len(archive_bytes)} bytes, sha256={sha256_bytes(archive_bytes)[:16]}…")
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"manifest written: {MANIFEST_PATH}")
    return 0


# ------------------------------------------------------------------ verify


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest missing: {MANIFEST_PATH} (run: fetch)")
    return json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))


def dataset_present() -> bool:
    if not MANIFEST_PATH.exists():
        return False
    try:
        manifest = load_manifest()
    except SystemExit:
        return False
    return all((BENCH_DIR / entry["archive"]["file"]).exists() for entry in manifest["repos"])


def verify_repo(entry: dict[str, Any]) -> list[str]:
    """Verify one vendored repo against the manifest; returns error strings."""
    errors: list[str] = []
    archive = BENCH_DIR / entry["archive"]["file"]
    if not archive.exists():
        return [f"{entry['name']}: archive missing: {archive}"]
    digest = sha256_file(archive)
    if digest != entry["archive"]["sha256"]:
        return [f"{entry['name']}: archive sha256 {digest} != manifest {entry['archive']['sha256']}"]
    with tempfile.TemporaryDirectory(prefix="kys-codebench-verify-") as tmp:
        names = extract_archive(archive, Path(tmp))
        expected = {f["path"]: f for f in entry["files"]}
        if set(names) != set(expected):
            missing = sorted(set(expected) - set(names))[:5]
            extra = sorted(set(names) - set(expected))[:5]
            errors = [f"{entry['name']}: file set mismatch (missing={missing} extra={extra})"]
            return errors
        for f in entry["files"]:
            path = Path(tmp) / f["path"]
            if sha256_file(path) != f["sha256"]:
                errors.append(f"{entry['name']}: {f['path']} sha256 mismatch")
    return errors


def cmd_verify() -> int:
    manifest = load_manifest()
    total_errors: list[str] = []
    for entry in manifest["repos"]:
        errors = verify_repo(entry)
        total_errors.extend(errors)
        status = "OK" if not errors else f"{len(errors)} ERRORS"
        print(f"{entry['name']}: {entry['file_count']} files, {status}")
        for error in errors[:10]:
            print(f"  {error}", file=sys.stderr)
    if total_errors:
        print(f"verify FAILED: {len(total_errors)} errors", file=sys.stderr)
        return 1
    print("verify OK: all vendored archives match the pinned manifest")
    return 0


# ------------------------------------------------------------ ground truth


def load_ground_truth() -> dict[str, Any]:
    if not GROUND_TRUTH_PATH.exists():
        raise SystemExit(f"ground truth missing: {GROUND_TRUTH_PATH}")
    truth = json.loads(GROUND_TRUTH_PATH.read_text(encoding="utf-8"))
    if truth.get("version") != GROUND_TRUTH_VERSION:
        raise SystemExit(f"ground truth version mismatch: {truth.get('version')} != {GROUND_TRUTH_VERSION}")
    return truth


def load_baseline() -> dict[str, Any] | None:
    if not BASELINE_PATH.exists():
        return None
    baseline = json.loads(BASELINE_PATH.read_text(encoding="utf-8"))
    if baseline.get("version") != BASELINE_VERSION:
        raise SystemExit(f"baseline version mismatch: {baseline.get('version')} != {BASELINE_VERSION}")
    return baseline


# ------------------------------------------------------------------ check


def cmd_check() -> int:
    """No-dataset self-checks (exit 0/1)."""
    failures: list[str] = []
    truth = load_ground_truth()

    fixture_files = {p.relative_to(FIXTURE_DIR).as_posix() for p in FIXTURE_DIR.rglob("*") if p.is_file()}
    fixture_truth = truth["targets"].get("fixture")
    if fixture_truth is None:
        failures.append("ground truth: no 'fixture' target")
    else:
        for query in fixture_truth.get("lookups", []):
            expect = expect_of(query)
            if expect is not None and expect["path"] not in fixture_files:
                failures.append(f"fixture lookup {query['query']!r}: path {expect['path']} not in fixture")
        for query in fixture_truth.get("hierarchy", []):
            if query["file"] not in fixture_files:
                failures.append(f"fixture hierarchy {query['symbol']!r}: file {query['file']} not in fixture")
            for entry in query["expect"]:
                if entry["path"] not in fixture_files:
                    failures.append(f"fixture hierarchy {query['symbol']!r}: expect path {entry['path']} missing")

    repo_names = {spec["name"] for spec in REPOS}
    manifest_repos: set[str] = set()
    if MANIFEST_PATH.exists():
        manifest_repos = {entry["name"] for entry in load_manifest()["repos"]}
        if manifest_repos != repo_names:
            failures.append(f"manifest repos {sorted(manifest_repos)} != REPOS {sorted(repo_names)}")
    for name in sorted(repo_names):
        if name not in truth["targets"]:
            failures.append(f"ground truth: no target for repo {name!r}")
        elif not truth["targets"][name].get("lookups"):
            failures.append(f"ground truth: repo {name!r} has no lookups")

    baseline = load_baseline()
    if baseline is not None:
        if set(baseline.get("targets", {})) != repo_names | {"fixture"}:
            failures.append("baseline targets != {fixture} ∪ REPOS")

    if dataset_present():
        print("dataset: vendored archives present (run 'verify' for hash check)")
    else:
        print("dataset: NOT vendored yet (run 'fetch' with network); verify/run will skip honestly")
    if failures:
        for failure in failures:
            print(f"check FAILED: {failure}", file=sys.stderr)
        return 1
    print("check OK: fixture, ground truth, manifest and baseline are internally consistent")
    return 0


# ------------------------------------------------------------- measurement


@dataclass
class TargetResult:
    name: str
    ingest: dict[str, Any]
    queries: dict[str, Any]
    determinism: dict[str, Any]


class _TimedExtractor:
    """Instrumentation proxy around the real extractor module.

    Delegates every attribute to ``know_your_soul.ingest.extractor`` (the
    product extractor runs as-is); only the wall time of each
    ``extract_source`` call is recorded.
    """

    def __init__(self) -> None:
        from know_your_soul.ingest import extractor

        self._real = extractor
        self.EXTRACTION_VERSION = extractor.EXTRACTION_VERSION
        self.durations_ms: list[float] = []

    def extract_source(self, *args: Any, **kwargs: Any) -> Any:
        started = time.perf_counter()
        try:
            return self._real.extract_source(*args, **kwargs)
        finally:
            self.durations_ms.append((time.perf_counter() - started) * 1000.0)


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (same convention as bench/run.py)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, round(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _ingest_once(root: Path, *, timed: bool) -> tuple[Any, Any, dict[str, Any]]:
    from know_your_soul.ingest.pipeline import IngestionPipeline, canonical_ingestion_dump
    from know_your_soul.store import EventStore

    store = EventStore()
    extractor = _TimedExtractor() if timed else None
    pipeline = IngestionPipeline(store, extractor=extractor)

    walk_started = time.perf_counter()
    report = pipeline.ingest_path(root, resolve=False)
    walk_ms = (time.perf_counter() - walk_started) * 1000.0

    resolve_started = time.perf_counter()
    pipeline.resolve_pending()
    resolve_ms = (time.perf_counter() - resolve_started) * 1000.0

    with store.read() as conn:
        nodes = conn.execute("SELECT COUNT(*) AS c FROM symbols").fetchone()["c"]
        edges = conn.execute("SELECT COUNT(*) AS c FROM graph_edges").fetchone()["c"]
        refs_total = conn.execute("SELECT COUNT(*) AS c FROM unresolved_refs").fetchone()["c"]
        refs_resolved = conn.execute("SELECT COUNT(*) AS c FROM unresolved_refs WHERE status = 'resolved'").fetchone()[
            "c"
        ]
        refs_pending = conn.execute("SELECT COUNT(*) AS c FROM unresolved_refs WHERE status = 'pending'").fetchone()[
            "c"
        ]
        refs_failed = conn.execute("SELECT COUNT(*) AS c FROM unresolved_refs WHERE status = 'failed'").fetchone()["c"]

    dump = canonical_ingestion_dump(store)
    stats = dict(report.stats)
    failed_files = sorted(f"{entry.relpath} [{entry.error_code}]" for entry in report.files if entry.status == "failed")
    ingest = {
        "stats": stats,
        "failed_files": failed_files,
        "nodes": int(nodes),
        "edges": int(edges),
        "refs_total": int(refs_total),
        "refs_resolved": int(refs_resolved),
        "refs_pending": int(refs_pending),
        "refs_failed": int(refs_failed),
        "ref_resolved_pct": round(refs_resolved / refs_total, 6) if refs_total else None,
        "walk_ms": walk_ms,
        "resolve_ms": resolve_ms,
        "extract_p50_ms": _percentile(extractor.durations_ms, 0.50) if extractor else None,
        "extract_p95_ms": _percentile(extractor.durations_ms, 0.95) if extractor else None,
        "files_per_s": round(stats["proposed"] / (walk_ms / 1000.0), 3) if walk_ms > 0 else None,
        "dump_sha256": sha256_bytes(dump.encode("utf-8")),
    }
    return store, report, ingest


def ingest_target(root: Path) -> tuple[Any, dict[str, Any], dict[str, Any]]:
    """Ingest a target twice; returns (storeA, ingest metrics, determinism)."""
    tracemalloc.start()
    store_a, _report_a, ingest = _ingest_once(root, timed=True)
    _current, peak = tracemalloc.get_traced_memory()
    tracemalloc.stop()
    ingest["tracemalloc_peak_bytes"] = int(peak)

    store_b, _report_b, ingest_b = _ingest_once(root, timed=False)
    determinism = {
        "run_a_sha256": ingest["dump_sha256"],
        "run_b_sha256": ingest_b["dump_sha256"],
        "byte_identical": ingest["dump_sha256"] == ingest_b["dump_sha256"],
    }
    store_b.close()
    return store_a, ingest, determinism


def expect_of(query: dict[str, Any]) -> dict[str, Any] | None:
    return query.get("expect")


def _run_lookups(store: Any, lookups: list[dict[str, Any]], *, repeats: int) -> dict[str, Any]:
    from know_your_soul.retrieval_v2 import SymbolLookup

    lookup = SymbolLookup()
    results = []
    for spec in lookups:
        latencies: list[float] = []
        hits: tuple[Any, ...] = ()
        for _ in range(repeats):
            started = time.perf_counter()
            hits = lookup.search(store, spec["query"], limit=LOOKUP_TOP_K)
            latencies.append((time.perf_counter() - started) * 1000.0)
        expect = expect_of(spec)
        top1_ok: bool | None = None
        hit_at_k: bool
        if expect is None:  # negative query: must return nothing
            hit_at_k = len(hits) == 0
        else:
            wanted = (expect["name"], expect["path"])
            found = [(h.name, h.path) for h in hits]
            hit_at_k = wanted in found
            if spec.get("top") == 1:
                top1_ok = bool(hits) and (hits[0].name, hits[0].path) == wanted
        results.append(
            {
                "query": spec["query"],
                "expect": expect,
                "hit_at_k": hit_at_k,
                "top1_ok": top1_ok,
                "hits": [
                    {"name": h.name, "kind": h.kind, "path": h.path, "line": h.line, "source": h.source}
                    for h in hits[:LOOKUP_TOP_K]
                ],
                "p50_ms": _percentile(latencies, 0.50),
                "p95_ms": _percentile(latencies, 0.95),
            }
        )
    return {"repeats": repeats, "k": LOOKUP_TOP_K, "results": results}


def _run_hierarchy(store: Any, queries: list[dict[str, Any]]) -> dict[str, Any]:
    from know_your_soul.retrieval_v2 import CallHierarchy

    hierarchy = CallHierarchy()
    results = []
    for spec in queries:
        report = (
            hierarchy.callers(store, spec["symbol"], file=spec["file"], depth=spec["depth"])
            if spec["direction"] == "callers"
            else hierarchy.callees(store, spec["symbol"], file=spec["file"], depth=spec["depth"])
        )
        actual = sorted(
            {(entry.name, entry.kind, entry.path) for definition in report.definitions for entry in definition.entries}
        )
        expected = sorted((e["name"], e["kind"], e["path"]) for e in spec["expect"])
        results.append(
            {
                "symbol": spec["symbol"],
                "file": spec["file"],
                "direction": spec["direction"],
                "depth": spec["depth"],
                "expect": spec["expect"],
                "actual": [{"name": n, "kind": k, "path": p} for (n, k, p) in actual],
                "exact_match": actual == expected,
            }
        )
    return {"results": results}


def run_target_queries(store: Any, truth: dict[str, Any], *, repeats: int) -> dict[str, Any]:
    queries: dict[str, Any] = {}
    if truth.get("lookups"):
        queries["lookups"] = _run_lookups(store, truth["lookups"], repeats=repeats)
    if truth.get("hierarchy"):
        queries["hierarchy"] = _run_hierarchy(store, truth["hierarchy"])
    return queries


# ------------------------------------------------------------------ gates


def correctness_failures(target: str, queries: dict[str, Any], determinism: dict[str, Any]) -> list[str]:
    failures: list[str] = []
    if not determinism["byte_identical"]:
        failures.append(
            f"{target}: determinism FAILED (run A {determinism['run_a_sha256'][:12]}… "
            f"!= run B {determinism['run_b_sha256'][:12]}…)"
        )
    for result in queries.get("lookups", {}).get("results", []):
        if not result["hit_at_k"]:
            failures.append(f"{target}: lookup {result['query']!r} missed (expect {result['expect']})")
        if result["top1_ok"] is False:
            failures.append(f"{target}: lookup {result['query']!r} not top-1")
    for result in queries.get("hierarchy", {}).get("results", []):
        if not result["exact_match"]:
            failures.append(
                f"{target}: hierarchy {result['direction']}({result['symbol']}@{result['file']}) "
                f"!= ground truth (actual {result['actual']})"
            )
    return failures


_COUNT_KEYS = ("nodes", "edges", "refs_total", "refs_resolved", "refs_pending", "refs_failed")
_STAT_KEYS = ("proposed", "failed", "skipped", "scanned")


def timing_regressions(baseline: dict[str, Any], results: list[TargetResult]) -> list[str]:
    """LOOSE timing comparisons (advisory on CI, fatal locally)."""
    failures: list[str] = []
    base_targets = baseline["targets"]
    for result in results:
        base = base_targets.get(result.name)
        if base is None:
            continue
        for key in ("walk_ms", "resolve_ms", "extract_p50_ms", "extract_p95_ms"):
            actual = result.ingest[key]
            base_value = base[key]
            if actual is None or base_value is None:
                continue
            if actual > base_value * TIMING_TOLERANCE + TIMING_SLACK_MS:
                failures.append(
                    f"{result.name}: {key} {actual:.1f}ms > tolerance({TIMING_TOLERANCE}x "
                    f"{base_value:.1f}ms + {TIMING_SLACK_MS}ms)"
                )
    return failures


def regression_failures(
    baseline: dict[str, Any], results: list[TargetResult], *, timing_gate: bool | None = None
) -> list[str]:
    """Baseline gate: counts strict, timing loose (TIMING_TOLERANCE + slack).

    ``timing_gate=None`` auto-detects the environment: the timing comparison
    is fatal locally, advisory on ``GITHUB_ACTIONS`` runners (see
    :func:`timing_gate_enabled`). Truth checks are never relaxed.
    """
    failures: list[str] = []
    base_targets = baseline["targets"]
    for result in results:
        base = base_targets.get(result.name)
        if base is None:
            failures.append(f"{result.name}: no baseline entry")
            continue
        for key in _STAT_KEYS:
            actual = result.ingest["stats"][key]
            if actual != base["stats"][key]:
                failures.append(f"{result.name}: stats.{key} {actual} != baseline {base['stats'][key]}")
        if result.ingest["failed_files"] != base["failed_files"]:
            failures.append(
                f"{result.name}: failed_files changed (baseline {len(base['failed_files'])} entries, "
                f"now {len(result.ingest['failed_files'])}; diff "
                f"{sorted(set(result.ingest['failed_files']) ^ set(base['failed_files']))[:6]})"
            )
        for key in _COUNT_KEYS:
            actual = result.ingest[key]
            if actual != base[key]:
                failures.append(f"{result.name}: {key} {actual} != baseline {base[key]}")
    gate_on = timing_gate_enabled() if timing_gate is None else timing_gate
    if gate_on:
        failures.extend(timing_regressions(baseline, results))
    return failures


def correctness_summary(results: list[TargetResult]) -> dict[str, Any]:
    lookups = [r for t in results for r in t.queries.get("lookups", {}).get("results", [])]
    hierarchy = [r for t in results for r in t.queries.get("hierarchy", {}).get("results", [])]
    positive = [r for r in lookups if r["expect"] is not None]
    return {
        "lookup_hit_at_k": sum(1 for r in positive if r["hit_at_k"]),
        "lookup_total": len(positive),
        "lookup_top1_ok": sum(1 for r in positive if r["top1_ok"] is not False),
        "negative_ok": all(r["hit_at_k"] for r in lookups if r["expect"] is None),
        "hierarchy_exact": sum(1 for r in hierarchy if r["exact_match"]),
        "hierarchy_total": len(hierarchy),
        "determinism_ok": all(t.determinism["byte_identical"] for t in results),
    }


# ------------------------------------------------------------------ run


def measure(*, repeats: int = DEFAULT_REPEATS, target_names: list[str] | None = None) -> list[TargetResult]:
    """Full measurement over fixture + vendored repos. Offline."""
    if not dataset_present():
        raise SystemExit("dataset not vendored (run: fetch). Skipping is the caller's job.")
    manifest = load_manifest()
    truth = load_ground_truth()

    roots: dict[str, Path] = {"fixture": FIXTURE_DIR}
    tmp = tempfile.TemporaryDirectory(prefix="kys-codebench-")
    for entry in manifest["repos"]:
        dest = Path(tmp.name) / entry["name"]
        dest.mkdir()
        extract_archive(BENCH_DIR / entry["archive"]["file"], dest)
        roots[entry["name"]] = dest

    results: list[TargetResult] = []
    try:
        for name in ("fixture",) + tuple(e["name"] for e in manifest["repos"]):
            if target_names is not None and name not in target_names:
                continue
            print(f"[{name}] ingesting (2 runs) …", flush=True)
            store, ingest, determinism = ingest_target(roots[name])
            print(
                f"[{name}] {ingest['stats']['proposed']} files proposed, {ingest['nodes']} nodes, "
                f"{ingest['edges']} edges, refs resolved {ingest['refs_resolved']}/{ingest['refs_total']}, "
                f"walk {ingest['walk_ms']:.0f}ms, resolve {ingest['resolve_ms']:.0f}ms, "
                f"peak {ingest['tracemalloc_peak_bytes'] / 1e6:.1f}MB, "
                f"determinism {'OK' if determinism['byte_identical'] else 'FAILED'}",
                flush=True,
            )
            queries = run_target_queries(store, truth["targets"][name], repeats=repeats)
            store.close()
            results.append(TargetResult(name=name, ingest=ingest, queries=queries, determinism=determinism))
    finally:
        tmp.cleanup()
    return results


def render_markdown(stamp: str, results: list[TargetResult], summary: dict[str, Any], gate: dict[str, Any]) -> str:
    lines = [
        f"# Codebase benchmark report — {stamp}",
        "",
        "Dataset: vendored OSS repos pinned by commit (see `manifest.json`) plus the",
        "manually verified `fixture/`. Harness: `bench/codebase/run.py` (offline).",
        "",
        "## Ingestion (product pipeline as-is)",
        "",
        "| Target | Files (prop/fail/skip) | Nodes | Edges | Refs resolved | Walk ms | Resolve ms |"
        " Extract p50/p95 ms | Peak MB | Det. |",
        "|---|---|---|---|---|---|---|---|---|---|",
    ]
    for t in results:
        i = t.ingest
        s = i["stats"]
        resolved = f"{i['refs_resolved']}/{i['refs_total']} ({(i['ref_resolved_pct'] or 0) * 100:.1f}%)"
        lines.append(
            f"| {t.name} | {s['proposed']}/{s['failed']}/{s['skipped']} | {i['nodes']} | {i['edges']} | "
            f"{resolved} | {i['walk_ms']:.0f} | {i['resolve_ms']:.0f} | "
            f"{(i['extract_p50_ms'] or 0):.2f}/{(i['extract_p95_ms'] or 0):.2f} | "
            f"{i['tracemalloc_peak_bytes'] / 1e6:.1f} | {'OK' if t.determinism['byte_identical'] else 'FAIL'} |"
        )
    lines += [
        "",
        "## Retrieval correctness vs manually verified ground truth",
        "",
        f"- Symbol lookups: {summary['lookup_hit_at_k']}/{summary['lookup_total']} hit@{LOOKUP_TOP_K}"
        f" ({summary['lookup_top1_ok']}/{summary['lookup_total']} top-1 where pinned);"
        f" negative query empty: {'yes' if summary['negative_ok'] else 'NO'}",
        f"- Call hierarchy: {summary['hierarchy_exact']}/{summary['hierarchy_total']} exact caller/callee sets",
        f"- Determinism (2 runs byte-identical): {'yes' if summary['determinism_ok'] else 'NO'}",
        "",
        "## Query latency (ms)",
        "",
        "| Target | Query | p50 | p95 |",
        "|---|---|---|---|",
    ]
    for t in results:
        for r in t.queries.get("lookups", {}).get("results", []):
            lines.append(f"| {t.name} | `{r['query']}` | {r['p50_ms']:.2f} | {r['p95_ms']:.2f} |")
    lines += [
        "",
        "## Gate",
        "",
        f"- correctness: {'PASS' if gate['correctness_ok'] else 'FAIL'}",
        f"- regression vs baseline: {gate['regression']}",
        f"- overall: **{'PASS' if gate['ok'] else 'FAIL'}**",
        "",
    ]
    for failure in gate["failures"]:
        lines.append(f"  - {failure}")
    for warning in gate.get("timing_advisory", []):
        lines.append(f"  - WARN (timing, advisory on CI): {warning}")
    return "\n".join(lines) + ("\n" if not lines[-1].endswith("\n") else "")


def cmd_run(args: argparse.Namespace) -> int:
    repeats = int(args.repeats)
    results = measure(repeats=repeats)

    all_failures: list[str] = []
    for t in results:
        all_failures.extend(correctness_failures(t.name, t.queries, t.determinism))
    summary = correctness_summary(results)

    baseline = load_baseline()
    regression: list[str] = []
    timing_warnings: list[str] = []
    if baseline is not None and not args.no_baseline:
        regression = regression_failures(baseline, results)
        if not timing_gate_enabled():
            # Shared CI runner: timings are advisory — printed and recorded,
            # never fatal (the strict truth checks above still fail the run).
            timing_warnings = timing_regressions(baseline, results)
    all_failures.extend(regression)

    gate = {
        "correctness_ok": summary["lookup_hit_at_k"] == summary["lookup_total"]
        and summary["hierarchy_exact"] == summary["hierarchy_total"]
        and summary["negative_ok"]
        and summary["determinism_ok"]
        and not any(f for f in all_failures if f not in regression),
        "regression": "PASS" if not regression else f"FAIL ({len(regression)} violations)",
        "ok": not all_failures,
        "failures": all_failures,
        # Advisory-only timing overruns (CI runners): reported, never fatal.
        "timing_advisory": timing_warnings,
    }

    stamp = utc_stamp()
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ledger = {
        "suite": "codebase",
        "stamp": stamp,
        "repeats": repeats,
        "dataset": {"manifest": sha256_file(MANIFEST_PATH), "fetched": DATASET_FETCHED},
        "ground_truth": sha256_file(GROUND_TRUTH_PATH),
        "baseline": sha256_file(BASELINE_PATH) if baseline is not None else None,
        "results": [
            {"name": t.name, "ingest": t.ingest, "queries": t.queries, "determinism": t.determinism} for t in results
        ],
        "summary": summary,
        "gate": gate,
    }
    ledger_path = RESULTS_DIR / f"ledger-{stamp}.json"
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path = RESULTS_DIR / f"report-{stamp}.md"
    report_path.write_text(render_markdown(stamp, results, summary, gate), encoding="utf-8")
    print(f"ledger: {ledger_path}")
    print(f"report: {report_path}")

    if args.update_baseline:
        write_baseline(results)
        print(f"baseline updated: {BASELINE_PATH}")

    print(
        f"gate: correctness {'PASS' if gate['correctness_ok'] else 'FAIL'}, "
        f"regression {gate['regression']}, overall {'PASS' if gate['ok'] else 'FAIL'}"
    )
    for warning in timing_warnings:
        print(f"  WARN (timing, advisory on CI): {warning}", file=sys.stderr)
    for failure in all_failures:
        print(f"  FAIL: {failure}", file=sys.stderr)
    return 0 if not all_failures else 1


def write_baseline(results: list[TargetResult]) -> None:
    targets: dict[str, Any] = {}
    for t in results:
        targets[t.name] = {
            "stats": t.ingest["stats"],
            "failed_files": t.ingest["failed_files"],
            **{key: t.ingest[key] for key in _COUNT_KEYS},
            **{key: t.ingest[key] for key in ("walk_ms", "resolve_ms", "extract_p50_ms", "extract_p95_ms")},
        }
    baseline = {
        "version": BASELINE_VERSION,
        "created": utc_stamp(),
        "note": (
            "counts are a STRICT gate (truth), timings a LOOSE gate "
            f"({TIMING_TOLERANCE}x + {TIMING_SLACK_MS}ms slack, machine jitter); "
            "refresh with: uv run python bench/codebase/run.py run --update-baseline"
        ),
        "targets": targets,
    }
    BASELINE_PATH.write_text(json.dumps(baseline, indent=2, sort_keys=True) + "\n", encoding="utf-8")


# ------------------------------------------------------------------ main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="codebase ingestion+retrieval benchmark (SUB-94)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("fetch", help="download pinned repos and vendor deterministic archives (NETWORK)")
    sub.add_parser("verify", help="verify vendored archives against manifest.json (offline)")
    sub.add_parser("check", help="no-dataset self-checks (offline)")
    run_parser = sub.add_parser("run", help="full measurement -> results/ (offline)")
    run_parser.add_argument("--repeats", type=int, default=DEFAULT_REPEATS)
    run_parser.add_argument("--update-baseline", action="store_true", help="rewrite baseline.json from this run")
    run_parser.add_argument("--no-baseline", action="store_true", help="skip the baseline regression gate")
    args = parser.parse_args(argv)

    if args.command == "fetch":
        return cmd_fetch()
    if args.command == "verify":
        return cmd_verify()
    if args.command == "check":
        return cmd_check()
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())

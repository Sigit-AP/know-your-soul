#!/usr/bin/env python3
"""Token-reduction benchmark: raw corpus vs product retrieval (SUB-108, W8).

Honest, deterministic, no-AI measurement of how many tokens the product's
retrieval (``SymbolLookup`` + ``assemble_context``, used as-is) needs to
answer a frozen question set over a pinned MIXED corpus (multi-language
code + markdown + text + small PDFs), versus reading the whole raw corpus.

Corpus: vendored OFFLINE as a canonical (byte-reproducible) tar.gz under
``data/`` — the same packaging pattern as ``bench/codebase`` — with per-file
sha256 pins in ``manifest.json``. Three nested tiers — small (6 files),
medium (26), large (52) — because token reduction scales with corpus size;
the small tier is reported with the same honesty even though a tiny corpus
barely needs a graph. The 52-file large tier mirrors the scale of
Graphify's documented reference claim (71.5x on their 52-file mixed corpus
of Karpathy repos + papers + images — a DIFFERENT corpus, a DIFFERENT
estimator and a different retrieval product; see GRAPHIFY_REFERENCE and
bench/tokens/README.md). The reference is context, never a gate.

Estimator: the frozen contract formula ``ceil(utf8_bytes / 4)``
(``know_your_soul.contracts.estimated_tokens_of_payload``), applied to raw
file bytes for the corpus and to the rendered context + question for the
per-query cost. Unlike Graphify's harness, negative-control questions
(no match) are INCLUDED in the mean query cost — excluding them flatters
the ratio.

Subcommands::

    freeze   # regenerate the deterministic PDFs + manifest.json (offline)
    verify   # corpus integrity vs manifest (offline)
    check    # no-corpus self-checks (exit 0/1)
    run      # full measurement -> results/ledger-<ts>.json + report-<ts>.md

Gates (``run`` exits 0 only when all hold):

* corpus verifies against the pinned manifest;
* determinism: two ingest passes are byte-identical per tier and two query
  passes produce identical per-question results;
* retrieval recall: every positive question retrieves >= 1 hit, every
  negative control retrieves none;
* rubric coverage: mean coverage over positive questions >= RUBRIC_FLOOR
  per tier (rubric = terms a sufficient answer must carry, checked as
  case-folded substrings of the assembled context — the product returns
  symbol/section identities with provenance, never raw bodies, so rubric
  terms are drawn from names/headings/paths).
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
import tempfile
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

BENCH_DIR = Path(__file__).resolve().parent
REPO_ROOT = BENCH_DIR.parent.parent
sys.path.insert(0, str(REPO_ROOT / "src"))

DATA_DIR = BENCH_DIR / "data"
ARCHIVE_PATH = DATA_DIR / "corpus.tar.gz"
RESULTS_DIR = BENCH_DIR / "results"
MANIFEST_PATH = BENCH_DIR / "manifest.json"
QUESTIONS_PATH = BENCH_DIR / "questions.json"

HARNESS_VERSION = 1
MANIFEST_VERSION = 1
QUESTIONS_VERSION = 1

CHARS_PER_TOKEN = 4
#: Context budget per question (tokens). Graphify's MCP default is 2000;
#: matching it keeps the per-query cost comparable in shape.
CONTEXT_BUDGET_TOKENS = 2000
LOOKUP_TOP_K = 10
#: Quality floor: mean rubric coverage per tier must reach this. Fixed
#: constant — calibrating it to the measured value would be Goodhart.
RUBRIC_FLOOR = 0.5

#: Documented reference claim (DIFFERENT corpus/estimator/product — context
#: only, never a gate). Sources: Graphify-Labs/graphify docs/how-it-works.md
#: and worked/karpathy-repos/review.md (52 files: 29 Python + 14 docs + 5
#: PDFs + 4 images, ~92,616 words, ~123,488 naive tokens, avg query cost
#: ~1,726 tokens -> 71.5x; code-only subset 8.8x; 4-file corpus 5.4x;
#: 6-file corpus ~1x). Their estimator: words*133/100 for the corpus,
#: len(text)//4 for answers, 2000-token budget, no-match questions EXCLUDED
#: from the average (benchmark.py:83-92).
GRAPHIFY_REFERENCE: dict[str, Any] = {
    "claim": "71.5x fewer tokens per query vs reading raw files",
    "reduction_ratio": 71.5,
    "corpus": "Karpathy repos (nanoGPT/minGPT/micrograd) + 5 papers + 4 images",
    "files": 52,
    "corpus_tokens": 123488,
    "avg_query_tokens": 1726,
    "sources": [
        "https://github.com/Graphify-Labs/graphify/blob/v8/docs/how-it-works.md",
        "https://github.com/Graphify-Labs/graphify/blob/v5/worked/karpathy-repos/review.md",
    ],
    "note": (
        "different corpus, different estimator (words*133/100 vs ceil(bytes/4)), "
        "different retrieval product; their average EXCLUDES no-match questions, "
        "ours includes them. Context only — never a gate."
    ),
}

# ------------------------------------------------------------- corpus tiers
#
# The pool is flat under corpus/; tiers are nested subsets (small ⊂ medium ⊂
# large). Code files are copied from the repos vendored by bench/codebase
# (psf/requests v2.32.4, chalk v5.6.2, tree-sitter-python v0.23.6 — pinned
# there by commit) plus five SOUL-protocol markdown docs from bench/corpus;
# the PDFs are generated deterministically by this file (PDF_SPECS, freeze).

TIER_FILE_COUNTS: dict[str, int] = {"small": 6, "medium": 26, "large": 52}

_SMALL_FILES: tuple[str, ...] = (
    "code/requests__api.py",
    "code/requests__exceptions.py",
    "code/chalk__index.js",
    "code/tree_sitter__scanner.c",
    "docs/requests__README.md",
    "papers/attention-notes.pdf",
)

_MEDIUM_EXTRA: tuple[str, ...] = (
    "code/requests__auth.py",
    "code/requests__cookies.py",
    "code/requests__models.py",
    "code/requests__sessions.py",
    "code/requests__structures.py",
    "code/requests__utils.py",
    "code/requests__help.py",
    "code/requests__status_codes.py",
    "code/requests__compat.py",
    "code/requests__init__.py",
    "code/chalk__ansi-styles.js",
    "code/chalk__supports-color.js",
    "code/chalk__test-chalk.js",
    "code/tree_sitter__grammar.js",
    "code/tree_sitter__parser.h",
    "docs/requests__CONTRIBUTING.md",
    "docs/requests__SECURITY.md",
    "docs/chalk__readme.md",
    "papers/flash-attention-notes.pdf",
    "papers/tokenizer-notes.pdf",
)

_LARGE_EXTRA: tuple[str, ...] = (
    "code/requests__test_utils.py",
    "code/requests__test_lowlevel.py",
    "code/requests__test_structures.py",
    "code/requests__test_testserver.py",
    "code/requests__testserver_server.py",
    "code/requests__setup.py",
    "code/requests__docs_conf.py",
    "code/requests__flask_theme_support.py",
    "code/tree_sitter__python3_grammar.py",
    "code/tree_sitter__python2_grammar.py",
    "code/chalk__utilities.js",
    "code/chalk__rainbow.js",
    "text/tree_sitter__expressions.txt",
    "text/tree_sitter__literals.txt",
    "text/tree_sitter__statements.txt",
    "text/tree_sitter__pattern_matching.txt",
    "docs/requests__HISTORY.md",
    "docs/chalk__code-of-conduct.md",
    "docs/tree_sitter__README.md",
    "papers/rag-notes.pdf",
    "papers/quantization-notes.pdf",
    "docs/soul__effort-router.md",
    "docs/soul__query-playbook.md",
    "docs/soul__measured-facts.md",
    "docs/soul__glossary-bilingual.md",
    "docs/soul__identity-compliance.md",
)

TIER_FILES: dict[str, tuple[str, ...]] = {
    "small": _SMALL_FILES,
    "medium": _SMALL_FILES + _MEDIUM_EXTRA,
    "large": _SMALL_FILES + _MEDIUM_EXTRA + _LARGE_EXTRA,
}

#: Vendored-file provenance: (archive-or-dir, source relpath) per pool file.
#: Resolved against REPO_ROOT; used ONLY by `freeze --rebuild-pool` (a
#: maintainer command) — verify/check/run read the pinned pool as-is.
POOL_SOURCES: dict[str, tuple[str, str]] = {
    "code/requests__api.py": ("codebase:requests", "src/requests/api.py"),
    "code/requests__exceptions.py": ("codebase:requests", "src/requests/exceptions.py"),
    "code/requests__auth.py": ("codebase:requests", "src/requests/auth.py"),
    "code/requests__cookies.py": ("codebase:requests", "src/requests/cookies.py"),
    "code/requests__models.py": ("codebase:requests", "src/requests/models.py"),
    "code/requests__sessions.py": ("codebase:requests", "src/requests/sessions.py"),
    "code/requests__structures.py": ("codebase:requests", "src/requests/structures.py"),
    "code/requests__utils.py": ("codebase:requests", "src/requests/utils.py"),
    "code/requests__help.py": ("codebase:requests", "src/requests/help.py"),
    "code/requests__status_codes.py": ("codebase:requests", "src/requests/status_codes.py"),
    "code/requests__compat.py": ("codebase:requests", "src/requests/compat.py"),
    "code/requests__init__.py": ("codebase:requests", "src/requests/__init__.py"),
    "code/requests__test_utils.py": ("codebase:requests", "tests/test_utils.py"),
    "code/requests__test_lowlevel.py": ("codebase:requests", "tests/test_lowlevel.py"),
    "code/requests__test_structures.py": ("codebase:requests", "tests/test_structures.py"),
    "code/requests__test_testserver.py": ("codebase:requests", "tests/test_testserver.py"),
    "code/requests__testserver_server.py": ("codebase:requests", "tests/testserver/server.py"),
    "code/requests__setup.py": ("codebase:requests", "setup.py"),
    "code/requests__docs_conf.py": ("codebase:requests", "docs/conf.py"),
    "code/requests__flask_theme_support.py": ("codebase:requests", "docs/_themes/flask_theme_support.py"),
    "code/chalk__index.js": ("codebase:chalk", "source/index.js"),
    "code/chalk__ansi-styles.js": ("codebase:chalk", "source/vendor/ansi-styles/index.js"),
    "code/chalk__supports-color.js": ("codebase:chalk", "source/vendor/supports-color/index.js"),
    "code/chalk__test-chalk.js": ("codebase:chalk", "test/chalk.js"),
    "code/chalk__utilities.js": ("codebase:chalk", "source/utilities.js"),
    "code/chalk__rainbow.js": ("codebase:chalk", "examples/rainbow.js"),
    "code/tree_sitter__scanner.c": ("codebase:tree-sitter-python", "src/scanner.c"),
    "code/tree_sitter__grammar.js": ("codebase:tree-sitter-python", "grammar.js"),
    "code/tree_sitter__parser.h": ("codebase:tree-sitter-python", "src/tree_sitter/parser.h"),
    "code/tree_sitter__python3_grammar.py": ("codebase:tree-sitter-python", "examples/python3-grammar.py"),
    "code/tree_sitter__python2_grammar.py": ("codebase:tree-sitter-python", "examples/python2-grammar.py"),
    "text/tree_sitter__expressions.txt": ("codebase:tree-sitter-python", "test/corpus/expressions.txt"),
    "text/tree_sitter__literals.txt": ("codebase:tree-sitter-python", "test/corpus/literals.txt"),
    "text/tree_sitter__statements.txt": ("codebase:tree-sitter-python", "test/corpus/statements.txt"),
    "text/tree_sitter__pattern_matching.txt": ("codebase:tree-sitter-python", "test/corpus/pattern_matching.txt"),
    "docs/requests__README.md": ("codebase:requests", "README.md"),
    "docs/requests__CONTRIBUTING.md": ("codebase:requests", ".github/CONTRIBUTING.md"),
    "docs/requests__SECURITY.md": ("codebase:requests", ".github/SECURITY.md"),
    "docs/requests__HISTORY.md": ("codebase:requests", "HISTORY.md"),
    "docs/chalk__readme.md": ("codebase:chalk", "readme.md"),
    "docs/chalk__code-of-conduct.md": ("codebase:chalk", "code-of-conduct.md"),
    "docs/tree_sitter__README.md": ("codebase:tree-sitter-python", "README.md"),
    "docs/soul__effort-router.md": ("soul", "11-effort-router.md"),
    "docs/soul__query-playbook.md": ("soul", "22-query-playbook.md"),
    "docs/soul__measured-facts.md": ("soul", "25-measured-facts.md"),
    "docs/soul__glossary-bilingual.md": ("soul", "23-glossary-bilingual.md"),
    "docs/soul__identity-compliance.md": ("soul", "01-identity-compliance.md"),
}

#: Deterministic single-page "paper notes" PDFs. The generator below emits
#: fixed bytes (no timestamps, no ids), so `freeze` is byte-reproducible.
PDF_SPECS: dict[str, tuple[str, tuple[str, ...]]] = {
    "papers/attention-notes.pdf": (
        "Notes on Attention Mechanisms",
        (
            "Scaled dot-product attention maps queries and keys to weights.",
            "Attention weights come from a softmax over query-key products.",
            "Multi-head attention runs several attention heads in parallel.",
            "Each head learns a different projection of queries keys values.",
            "The transformer block stacks attention with feedforward layers.",
            "Causal self-attention masks future positions with a triangle.",
            "nanoGPT and minGPT implement causal self-attention in Python.",
        ),
    ),
    "papers/flash-attention-notes.pdf": (
        "Notes on FlashAttention",
        (
            "FlashAttention is an IO-aware exact attention algorithm.",
            "It tiles the attention matrix to fit SRAM blocks.",
            "Tiling avoids materializing the full N by N score matrix.",
            "Recomputation in the backward pass trades compute for memory.",
            "Memory footprint grows linearly instead of quadratically.",
        ),
    ),
    "papers/tokenizer-notes.pdf": (
        "Notes on Tokenizers",
        (
            "Byte pair encoding merges the most frequent symbol pairs.",
            "The merge table is learned from a training corpus.",
            "A fixed vocabulary maps token ids to byte sequences.",
            "Token counts decide the cost of a context window.",
        ),
    ),
    "papers/rag-notes.pdf": (
        "Notes on Retrieval Augmented Generation",
        (
            "Retrieval augmented generation fetches context before decoding.",
            "A corpus is chunked and indexed for nearest neighbour lookup.",
            "Only the top-k chunks join the prompt, not the whole corpus.",
            "This benchmark measures exactly that token saving, without AI.",
        ),
    ),
    "papers/quantization-notes.pdf": (
        "Notes on Quantization",
        (
            "Quantization stores weights in lower precision formats.",
            "Int8 quantization multiplies integers by a per-tensor scale.",
            "Calibration picks scales from representative activations.",
        ),
    ),
}

# ------------------------------------------------------------------ hashing


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def sha256_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def utc_stamp() -> str:
    from datetime import UTC, datetime

    return datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")


# ---------------------------------------------------------------- estimator


def estimate_tokens_bytes(data: bytes) -> int:
    """Frozen contract estimator: ``ceil(utf8_byte_length / 4)``."""
    return -(-len(data) // CHARS_PER_TOKEN)


# ------------------------------------------------- deterministic mini PDF


def make_pdf_bytes(lines: tuple[str, ...]) -> bytes:
    """Minimal one-page PDF, byte-deterministic (no ids, no timestamps).

    Helvetica base font, one text line per ``lines`` entry, latin-1 only —
    the specs above are authored inside that alphabet. Verified readable by
    pypdf (the product's document lane) before pinning.
    """
    content_parts = ["BT", "/F1 12 Tf", "72 750 Td"]
    for index, line in enumerate(lines):
        safe = line.replace("\\", r"\\").replace("(", r"\(").replace(")", r"\)")
        content_parts.append(f"({safe}) Tj" if index == 0 else f"0 -20 Td ({safe}) Tj")
    content_parts.append("ET")
    stream = "\n".join(content_parts).encode("latin-1")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        b"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 612 792] "
        b"/Resources << /Font << /F1 4 0 R >> >> /Contents 5 0 R >>",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
    ]
    import io

    out = io.BytesIO()
    out.write(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, start=1):
        offsets.append(out.tell())
        out.write(b"%d 0 obj\n" % number + body + b"\nendobj\n")
    xref_pos = out.tell()
    out.write(b"xref\n0 %d\n" % (len(objects) + 1))
    out.write(b"0000000000 65535 f \n")
    for offset in offsets:
        out.write(b"%010d 00000 n \n" % offset)
    out.write(b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (len(objects) + 1, xref_pos))
    return out.getvalue()


# ------------------------------------------------------- deterministic tar
#
# Same canonical-packaging pattern as bench/codebase/run.py: entries sorted
# bytewise, zeroed mtimes/owners, fixed modes, gzip mtime=0 — the vendored
# archive is byte-reproducible by `freeze`.


def canonical_tarball(file_bytes: dict[str, bytes]) -> bytes:
    import gzip
    import io
    import tarfile

    raw = io.BytesIO()
    with gzip.GzipFile(filename="", mode="wb", fileobj=raw, mtime=0) as gz:
        with tarfile.open(fileobj=gz, mode="w") as tar:
            seen_dirs: set[str] = set()
            for rel in sorted(file_bytes):
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
                data = file_bytes[rel]
                info = tarfile.TarInfo(rel)
                info.size = len(data)
                info.mode = 0o644
                info.mtime = 0
                info.uid = info.gid = 0
                info.uname = info.gname = ""
                tar.addfile(info, io.BytesIO(data))
    return raw.getvalue()


def extract_pool(dest: Path) -> list[str]:
    """Extract the vendored corpus archive into dest; returns sorted relpaths.

    Fail-closed on absolute paths / ``..`` segments.
    """
    import tarfile

    names: list[str] = []
    with tarfile.open(ARCHIVE_PATH, "r:gz") as tar:
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


def build_pool_bytes() -> dict[str, bytes]:
    """The full corpus pool in memory: POOL_SOURCES + generated PDFs."""
    pool = {relpath: make_pdf_bytes(lines) for relpath, (_title, lines) in PDF_SPECS.items()}
    for relpath, (origin, source) in sorted(POOL_SOURCES.items()):
        pool[relpath] = _read_pool_source(origin, source)
    return pool


# ------------------------------------------------------------------ freeze


def _read_pool_source(origin: str, relpath: str) -> bytes:
    import tarfile

    if origin == "soul":
        return (REPO_ROOT / "bench" / "corpus" / relpath).read_bytes()
    archive = REPO_ROOT / "bench" / "codebase" / "data" / f"{origin.split(':', 1)[1]}.tar.gz"
    with tarfile.open(archive, "r:gz") as tar:
        member = tar.extractfile(relpath)
        if member is None:  # pragma: no cover - defensive
            raise SystemExit(f"pool source missing: {origin} {relpath}")
        return member.read()


def cmd_freeze() -> int:
    """Rebuild the canonical corpus archive and rewrite manifest.json.

    Byte-deterministic: PDFs from PDF_SPECS, pool files from POOL_SOURCES
    (needs the bench/codebase archives vendored), canonical tar packaging.
    Re-running must produce the identical archive sha256 — drift means a
    source changed and the re-pin must be an explicit decision.
    """
    pool = build_pool_bytes()
    archive_bytes = canonical_tarball(pool)
    DATA_DIR.mkdir(parents=True, exist_ok=True)
    if ARCHIVE_PATH.exists() and sha256_file(ARCHIVE_PATH) != sha256_bytes(archive_bytes):
        print("freeze: archive bytes drifted — re-pinning (explicit re-freeze)", file=sys.stderr)
    ARCHIVE_PATH.write_bytes(archive_bytes)
    manifest: dict[str, Any] = {
        "version": MANIFEST_VERSION,
        "note": (
            "mixed corpus (code multi-language + markdown + text + small PDFs), "
            "canonical tar.gz (sorted entries, zeroed mtimes, gzip mtime=0); "
            "PDFs generated deterministically by run.py make_pdf_bytes; "
            "tiers nested small < medium < large"
        ),
        "archive": {
            "file": "data/corpus.tar.gz",
            "sha256": sha256_bytes(archive_bytes),
            "bytes": len(archive_bytes),
        },
        "tier_file_counts": TIER_FILE_COUNTS,
        "tiers": {tier: list(files) for tier, files in TIER_FILES.items()},
        "files": [
            {"path": relpath, "sha256": sha256_bytes(data), "size": len(data)} for relpath, data in sorted(pool.items())
        ],
    }
    MANIFEST_PATH.write_text(json.dumps(manifest, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    print(f"freeze OK: {len(pool)} files, archive {len(archive_bytes)} bytes, manifest {MANIFEST_PATH}")
    return 0


# ------------------------------------------------------------------ verify


def load_manifest() -> dict[str, Any]:
    if not MANIFEST_PATH.exists():
        raise SystemExit(f"manifest missing: {MANIFEST_PATH} (run: freeze)")
    manifest = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
    if manifest.get("version") != MANIFEST_VERSION:
        raise SystemExit(f"manifest version mismatch: {manifest.get('version')} != {MANIFEST_VERSION}")
    return manifest


def corpus_present() -> bool:
    return MANIFEST_PATH.exists() and ARCHIVE_PATH.exists()


def verify_corpus(manifest: dict[str, Any], pool_root: Path | None = None) -> list[str]:
    """Verify archive + per-file pins. Extracts to a temp dir unless a

    pre-extracted ``pool_root`` is given (measure reuses its own)."""
    errors: list[str] = []
    if not ARCHIVE_PATH.exists():
        return [f"archive missing: {ARCHIVE_PATH} (run: freeze)"]
    digest = sha256_file(ARCHIVE_PATH)
    if digest != manifest["archive"]["sha256"]:
        return [f"archive sha256 {digest} != manifest {manifest['archive']['sha256']}"]
    if ARCHIVE_PATH.stat().st_size != manifest["archive"]["bytes"]:
        return ["archive size mismatch"]

    expected = {f["path"]: f for f in manifest["files"]}

    def _check(root: Path) -> list[str]:
        found: list[str] = []
        actual = {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}
        missing = sorted(set(expected) - actual)
        extra = sorted(actual - set(expected))
        if missing:
            found.append(f"corpus files missing: {missing[:5]}")
        if extra:
            found.append(f"unpinned corpus files present: {extra[:5]}")
        for relpath in sorted(set(expected) & actual):
            path = root / relpath
            if sha256_file(path) != expected[relpath]["sha256"]:
                found.append(f"{relpath}: sha256 mismatch")
            if path.stat().st_size != expected[relpath]["size"]:
                found.append(f"{relpath}: size mismatch")
        return found

    if pool_root is not None:
        errors.extend(_check(pool_root))
    else:
        with tempfile.TemporaryDirectory(prefix="kys-tokenbench-verify-") as tmp:
            extract_pool(Path(tmp))
            errors.extend(_check(Path(tmp)))
    for tier, files in manifest.get("tiers", {}).items():
        for relpath in files:
            if relpath not in expected:
                errors.append(f"tier {tier}: {relpath} not in manifest files")
    return errors


def cmd_verify() -> int:
    manifest = load_manifest()
    errors = verify_corpus(manifest)
    tiers = {tier: len(files) for tier, files in manifest["tiers"].items()}
    print(f"manifest: {len(manifest['files'])} files, tiers {tiers}")
    if errors:
        for error in errors[:10]:
            print(f"verify FAILED: {error}", file=sys.stderr)
        return 1
    print("verify OK: corpus archive matches the pinned manifest")
    return 0


# ------------------------------------------------------------------- check


def load_questions() -> dict[str, Any]:
    if not QUESTIONS_PATH.exists():
        raise SystemExit(f"questions missing: {QUESTIONS_PATH}")
    questions = json.loads(QUESTIONS_PATH.read_text(encoding="utf-8"))
    if questions.get("version") != QUESTIONS_VERSION:
        raise SystemExit(f"questions version mismatch: {questions.get('version')} != {QUESTIONS_VERSION}")
    return questions


def cmd_check() -> int:
    failures: list[str] = []
    for tier, files in TIER_FILES.items():
        if len(files) != TIER_FILE_COUNTS[tier]:
            failures.append(f"tier {tier}: {len(files)} files != pinned {TIER_FILE_COUNTS[tier]}")
        if len(set(files)) != len(files):
            failures.append(f"tier {tier}: duplicate pool paths")
    if not (set(TIER_FILES["small"]) < set(TIER_FILES["medium"]) < set(TIER_FILES["large"])):
        failures.append("tiers are not strictly nested small < medium < large")
    if set(TIER_FILES["large"]) != set(POOL_SOURCES) | set(PDF_SPECS):
        failures.append("large tier != POOL_SOURCES + PDF_SPECS")
    try:
        questions = load_questions()
        if set(questions["tiers"]) != set(TIER_FILES):
            failures.append("questions tiers != harness tiers")
        for tier, specs in questions["tiers"].items():
            tier_files = set(TIER_FILES[tier])
            ids: set[str] = set()
            for q in specs:
                if q["id"] in ids:
                    failures.append(f"{tier}: duplicate question id {q['id']}")
                ids.add(q["id"])
                for source in q["source_files"]:
                    if source not in tier_files:
                        failures.append(f"{tier}/{q['id']}: source {source} outside tier")
                if q.get("expect_hits", True) and not q["rubric_terms"]:
                    failures.append(f"{tier}/{q['id']}: positive question without rubric terms")
    except SystemExit as exc:
        failures.append(str(exc))
    if corpus_present():
        manifest = load_manifest()
        if manifest.get("tier_file_counts") != TIER_FILE_COUNTS:
            failures.append("manifest tier counts != harness pins")
        if {t: list(f) for t, f in manifest.get("tiers", {}).items()} != {t: list(f) for t, f in TIER_FILES.items()}:
            failures.append("manifest tier lists != harness TIER_FILES")
        print("corpus: vendored (run 'verify' for hash check)")
    else:
        failures.append("corpus not vendored (run: freeze)")
    if failures:
        for failure in failures:
            print(f"check FAILED: {failure}", file=sys.stderr)
        return 1
    print("check OK: tiers, questions, manifest and pins are internally consistent")
    return 0


# ------------------------------------------------------------- measurement


@dataclass
class QuestionResult:
    id: str
    question: str
    expect_hits: bool
    hits: int
    query_tokens: int
    rubric_terms: int
    rubric_matched: int
    rubric_coverage: float | None
    source_files: tuple[str, ...]
    p50_ms: float
    p95_ms: float


def _percentile(values: list[float], q: float) -> float:
    """Nearest-rank percentile (same convention as bench/run.py)."""
    if not values:
        return 0.0
    ordered = sorted(values)
    rank = max(1, round(q * len(ordered)))
    return ordered[min(rank, len(ordered)) - 1]


def _materialize_tier(tier: str, pool_root: Path, dest: Path) -> None:
    for relpath in TIER_FILES[tier]:
        target = dest / relpath
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes((pool_root / relpath).read_bytes())


def _ingest_tier(root: Path) -> tuple[Any, str]:
    from know_your_soul.ingest.pipeline import IngestionPipeline, canonical_ingestion_dump
    from know_your_soul.store import EventStore

    store = EventStore()
    pipeline = IngestionPipeline(store)
    pipeline.ingest_path(root, resolve=False)
    pipeline.resolve_pending()
    return store, sha256_bytes(canonical_ingestion_dump(store).encode("utf-8"))


def _rubric_coverage(rendered: str, terms: list[str]) -> tuple[int, float | None]:
    if not terms:
        return 0, None
    folded = rendered.casefold()
    matched = sum(1 for term in terms if term.casefold() in folded)
    return matched, matched / len(terms)


def _answer_questions(store: Any, specs: list[dict[str, Any]], *, repeats: int) -> list[QuestionResult]:
    from know_your_soul.retrieval_v2 import (
        ContextConfig,
        ContextItem,
        SymbolLookup,
        assemble_context,
    )

    lookup = SymbolLookup()
    config = ContextConfig(budget_tokens=CONTEXT_BUDGET_TOKENS)
    results: list[QuestionResult] = []
    for spec in specs:
        latencies: list[float] = []
        rendered = ""
        hits: tuple[Any, ...] = ()
        for _ in range(repeats):
            started = time.perf_counter()
            hits = tuple(lookup.search(store, spec["question"], limit=LOOKUP_TOP_K))
            items = [
                ContextItem(
                    node_id=h.node_id,
                    name=h.name,
                    kind=h.kind,
                    path=h.path,
                    line=h.line,
                    col=h.col,
                    score=h.score,
                )
                for h in hits
            ]
            assembled = assemble_context(store, items, config)
            rendered = assembled.render()
            latencies.append((time.perf_counter() - started) * 1000.0)
        query_tokens = estimate_tokens_bytes(spec["question"].encode("utf-8")) + estimate_tokens_bytes(
            rendered.encode("utf-8")
        )
        matched, coverage = _rubric_coverage(rendered, list(spec["rubric_terms"]))
        results.append(
            QuestionResult(
                id=spec["id"],
                question=spec["question"],
                expect_hits=bool(spec.get("expect_hits", True)),
                hits=len(hits),
                query_tokens=query_tokens,
                rubric_terms=len(spec["rubric_terms"]),
                rubric_matched=matched,
                rubric_coverage=coverage,
                source_files=tuple(spec["source_files"]),
                p50_ms=_percentile(latencies, 0.50),
                p95_ms=_percentile(latencies, 0.95),
            )
        )
    return results


def measure(*, repeats: int = 5) -> dict[str, Any]:
    """Full measurement over all tiers. Offline; no clocks in the metrics."""
    if not corpus_present():
        raise SystemExit("corpus not vendored (run: freeze)")
    manifest = load_manifest()
    questions = load_questions()

    sizes = {f["path"]: f["size"] for f in manifest["files"]}
    tiers: dict[str, Any] = {}
    ingest_hashes: dict[str, tuple[str, str]] = {}
    query_hashes: dict[str, tuple[str, str]] = {}
    with tempfile.TemporaryDirectory(prefix="kys-tokenbench-pool-") as pool_tmp:
        pool_root = Path(pool_tmp)
        extract_pool(pool_root)
        errors = verify_corpus(manifest, pool_root)
        if errors:
            raise SystemExit(f"corpus verification failed: {errors[:5]}")
        for tier in ("small", "medium", "large"):
            files = list(TIER_FILES[tier])
            raw_bytes = sum(sizes[relpath] for relpath in files)
            raw_tokens = sum(estimate_tokens_bytes((pool_root / relpath).read_bytes()) for relpath in files)
            with tempfile.TemporaryDirectory(prefix=f"kys-tokenbench-{tier}-") as tmp:
                root = Path(tmp)
                _materialize_tier(tier, pool_root, root)
                store_a, dump_a = _ingest_tier(root)
                store_b, dump_b = _ingest_tier(root)
                store_b.close()

                specs = questions["tiers"][tier]
                pass_a = _answer_questions(store_a, specs, repeats=repeats)
                pass_b = _answer_questions(store_a, specs, repeats=1)
                store_a.close()

            def _fingerprint(results: list[QuestionResult]) -> str:
                payload = [(r.id, r.hits, r.query_tokens, r.rubric_matched, r.rubric_coverage) for r in results]
                return sha256_bytes(json.dumps(payload, sort_keys=True).encode("utf-8"))

            hash_a, hash_b = _fingerprint(pass_a), _fingerprint(pass_b)
            ingest_hashes[tier] = (dump_a, dump_b)
            query_hashes[tier] = (hash_a, hash_b)

            positive = [r for r in pass_a if r.expect_hits]
            coverages = [r.rubric_coverage for r in positive if r.rubric_coverage is not None]
            mean_query_tokens = sum(r.query_tokens for r in pass_a) / len(pass_a)
            tiers[tier] = {
                "files": len(files),
                "bytes": raw_bytes,
                "raw_tokens": raw_tokens,
                "mean_query_tokens": mean_query_tokens,
                "reduction_ratio": raw_tokens / mean_query_tokens if mean_query_tokens else 0.0,
                "saving_pct": (1.0 - mean_query_tokens / raw_tokens) * 100.0 if raw_tokens else 0.0,
                "mean_rubric_coverage": sum(coverages) / len(coverages) if coverages else 0.0,
                "recall_hits": sum(1 for r in positive if r.hits >= 1),
                "recall_total": len(positive),
                "negatives_ok": all(r.hits == 0 for r in pass_a if not r.expect_hits),
                "questions": pass_a,
            }
    combined_a = sha256_bytes(json.dumps({t: h[0] for t, h in query_hashes.items()}, sort_keys=True).encode("utf-8"))
    combined_b = sha256_bytes(json.dumps({t: h[1] for t, h in query_hashes.items()}, sort_keys=True).encode("utf-8"))
    return {
        "tiers": tiers,
        "determinism": {
            "ingest_identical": all(a == b for a, b in ingest_hashes.values()),
            "query_identical": all(a == b for a, b in query_hashes.values()),
            "ingest_hashes": {t: list(h) for t, h in ingest_hashes.items()},
            "query_hashes": {t: list(h) for t, h in query_hashes.items()},
            "identical": all(a == b for a, b in ingest_hashes.values())
            and all(a == b for a, b in query_hashes.values()),
            "pass_a_sha256": combined_a,
            "pass_b_sha256": combined_b,
        },
    }


def metrics_view(result: dict[str, Any]) -> dict[str, Any]:
    """The deterministic subset of a measurement (no clocks, no hashes)."""
    view: dict[str, Any] = {"tiers": {}}
    for tier, summary in result["tiers"].items():
        view["tiers"][tier] = {
            key: summary[key]
            for key in (
                "files",
                "bytes",
                "raw_tokens",
                "mean_query_tokens",
                "reduction_ratio",
                "saving_pct",
                "mean_rubric_coverage",
                "recall_hits",
                "recall_total",
                "negatives_ok",
            )
        }
        view["tiers"][tier]["questions"] = [
            {
                "id": r.id,
                "hits": r.hits,
                "query_tokens": r.query_tokens,
                "rubric_matched": r.rubric_matched,
                "rubric_coverage": r.rubric_coverage,
            }
            for r in summary["questions"]
        ]
    return view


# ------------------------------------------------------------------ ledger


def build_ledger(result: dict[str, Any], *, stamp: str) -> dict[str, Any]:
    determinism = dict(result["determinism"])
    tiers: dict[str, Any] = {}
    for tier, summary in result["tiers"].items():
        tiers[tier] = {
            "files": summary["files"],
            "bytes": summary["bytes"],
            "raw_tokens": summary["raw_tokens"],
            "mean_query_tokens": round(summary["mean_query_tokens"], 3),
            "reduction_ratio": round(summary["reduction_ratio"], 2),
            "saving_pct": round(summary["saving_pct"], 2),
            "mean_rubric_coverage": round(summary["mean_rubric_coverage"], 4),
            "recall_hits": summary["recall_hits"],
            "recall_total": summary["recall_total"],
            "negatives_ok": summary["negatives_ok"],
            "questions": [
                {
                    "id": r.id,
                    "question": r.question,
                    "expect_hits": r.expect_hits,
                    "hits": r.hits,
                    "query_tokens": r.query_tokens,
                    "rubric_terms": r.rubric_terms,
                    "rubric_matched": r.rubric_matched,
                    "rubric_coverage": None if r.rubric_coverage is None else round(r.rubric_coverage, 4),
                    "source_files": list(r.source_files),
                    "p50_ms": round(r.p50_ms, 3),
                    "p95_ms": round(r.p95_ms, 3),
                }
                for r in summary["questions"]
            ],
        }
    return {
        "suite": "tokens",
        "harness_version": HARNESS_VERSION,
        "stamp": stamp,
        "estimator": "ceil(utf8_bytes/4)",
        "budget_tokens": CONTEXT_BUDGET_TOKENS,
        "top_k": LOOKUP_TOP_K,
        "rubric_floor": RUBRIC_FLOOR,
        "manifest_sha256": sha256_file(MANIFEST_PATH),
        "questions_sha256": sha256_file(QUESTIONS_PATH),
        "tiers": tiers,
        "graphify_reference": GRAPHIFY_REFERENCE,
        "determinism": determinism,
        "gate": evaluate_gate(result),
    }


def evaluate_gate(result: dict[str, Any]) -> dict[str, Any]:
    failures: list[str] = []
    if not result["determinism"]["ingest_identical"]:
        failures.append("determinism: ingest passes not byte-identical")
    if not result["determinism"]["query_identical"]:
        failures.append("determinism: query passes differ")
    for tier, summary in result["tiers"].items():
        if summary["recall_hits"] != summary["recall_total"]:
            failures.append(f"{tier}: retrieval recall {summary['recall_hits']}/{summary['recall_total']}")
        if not summary["negatives_ok"]:
            failures.append(f"{tier}: negative control returned hits")
        if summary["mean_rubric_coverage"] < RUBRIC_FLOOR:
            failures.append(
                f"{tier}: mean rubric coverage {summary['mean_rubric_coverage']:.4f} < floor {RUBRIC_FLOOR}"
            )
    return {
        "determinism_ok": result["determinism"]["identical"],
        "recall_ok": all(s["recall_hits"] == s["recall_total"] and s["negatives_ok"] for s in result["tiers"].values()),
        "rubric_ok": all(s["mean_rubric_coverage"] >= RUBRIC_FLOOR for s in result["tiers"].values()),
        "failures": failures,
        "ok": not failures,
    }


def render_markdown(ledger: dict[str, Any]) -> str:
    lines = [
        f"# Token-reduction benchmark report — {ledger['stamp']}",
        "",
        "Mixed pinned corpus (code + markdown + text + PDF), frozen estimator "
        "`ceil(utf8_bytes/4)`, product retrieval (`SymbolLookup` + "
        f"`assemble_context`, budget {ledger['budget_tokens']} tokens). "
        "Negative-control questions are INCLUDED in the mean query cost.",
        "",
        "## Per-tier results (reduction scales with corpus size)",
        "",
        "| Tier | Files | Bytes | Raw tokens | Mean query tokens | Reduction | Saving | Rubric | Recall |",
        "|---|---|---|---|---|---|---|---|---|",
    ]
    for tier in ("small", "medium", "large"):
        t = ledger["tiers"][tier]
        lines.append(
            f"| {tier} | {t['files']} | {t['bytes']:,} | {t['raw_tokens']:,} | "
            f"{t['mean_query_tokens']:.1f} | {t['reduction_ratio']:.1f}x | {t['saving_pct']:.2f}% | "
            f"{t['mean_rubric_coverage']:.2f} | {t['recall_hits']}/{t['recall_total']} |"
        )
    ref = ledger["graphify_reference"]
    lines += [
        "",
        "## Documented reference (DIFFERENT corpus — context only, never a gate)",
        "",
        f"- Graphify claim: **{ref['reduction_ratio']}x** ({ref['claim']}) on their own "
        f"{ref['files']}-file corpus ({ref['corpus']}), ~{ref['corpus_tokens']:,} naive tokens / "
        f"~{ref['avg_query_tokens']:,} avg query tokens.",
        f"- {ref['note']}",
        "",
        "## Per-question detail",
        "",
        "| Tier | Question | Hits | Query tokens | Rubric |",
        "|---|---|---|---|---|",
    ]
    for tier in ("small", "medium", "large"):
        for q in ledger["tiers"][tier]["questions"]:
            coverage = "neg" if q["rubric_coverage"] is None else f"{q['rubric_matched']}/{q['rubric_terms']}"
            lines.append(f"| {tier} | `{q['question'][:48]}` | {q['hits']} | {q['query_tokens']} | {coverage} |")
    gate = ledger["gate"]
    lines += [
        "",
        "## Gate",
        "",
        f"- determinism (2 ingest passes byte-identical, 2 query passes identical): "
        f"{'PASS' if gate['determinism_ok'] else 'FAIL'}",
        f"- retrieval recall (positives hit, negatives empty): {'PASS' if gate['recall_ok'] else 'FAIL'}",
        f"- rubric coverage >= {ledger['rubric_floor']}: {'PASS' if gate['rubric_ok'] else 'FAIL'}",
        f"- overall: **{'PASS' if gate['ok'] else 'FAIL'}**",
        "",
    ]
    for failure in gate["failures"]:
        lines.append(f"  - {failure}")
    return "\n".join(lines) + "\n"


def cmd_run(args: argparse.Namespace) -> int:
    result = measure(repeats=int(args.repeats))
    stamp = utc_stamp()
    ledger = build_ledger(result, stamp=stamp)
    RESULTS_DIR.mkdir(parents=True, exist_ok=True)
    ledger_path = RESULTS_DIR / f"ledger-{stamp}.json"
    ledger_path.write_text(json.dumps(ledger, indent=2, sort_keys=True) + "\n", encoding="utf-8")
    report_path = RESULTS_DIR / f"report-{stamp}.md"
    report_path.write_text(render_markdown(ledger), encoding="utf-8")
    print(f"ledger: {ledger_path}")
    print(f"report: {report_path}")
    for tier in ("small", "medium", "large"):
        t = ledger["tiers"][tier]
        print(
            f"[{tier:6s}] files={t['files']:3d} raw={t['raw_tokens']:7,} tok "
            f"query={t['mean_query_tokens']:7.1f} tok reduction={t['reduction_ratio']:7.1f}x "
            f"saving={t['saving_pct']:6.2f}% rubric={t['mean_rubric_coverage']:.2f} "
            f"recall={t['recall_hits']}/{t['recall_total']}"
        )
    gate = ledger["gate"]
    print(f"gate: {'PASS' if gate['ok'] else 'FAIL'}")
    for failure in gate["failures"]:
        print(f"  FAIL: {failure}", file=sys.stderr)
    return 0 if gate["ok"] else 1


# --------------------------------------------------------------------- main


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="token-reduction benchmark (SUB-108, W8)")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser(
        "freeze",
        help="rebuild the canonical corpus archive + manifest.json (offline; needs bench/codebase data)",
    )
    sub.add_parser("verify", help="corpus integrity vs manifest.json (offline)")
    sub.add_parser("check", help="no-corpus self-checks (offline)")
    run_parser = sub.add_parser("run", help="full measurement -> results/ (offline)")
    run_parser.add_argument("--repeats", type=int, default=5)
    args = parser.parse_args(argv)

    if args.command == "freeze":
        return cmd_freeze()
    if args.command == "verify":
        return cmd_verify()
    if args.command == "check":
        return cmd_check()
    return cmd_run(args)


if __name__ == "__main__":
    raise SystemExit(main())

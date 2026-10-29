"""Byte-identity tests: Rust extraction kernel vs the pure-Python extractor (W5, SUB-93).

The Rust kernel (``know_your_soul_kernel`` via PyO3 / ``kys-kernel`` CLI) must
produce canonical JSON artifacts that are **byte-identical** to the reference
Python walker for every phase-1 fixture, and stable ids must match
:func:`know_your_soul.graph_model.make_node_id` exactly.

F1 (audit-final-v020, fixed W6/SUB-97): the Python side of every parity check
below is the **pure-Python walker invoked directly**
(``TreeSitterExtractor(get_spec(...)).extract(...)``) — never ``extract_file``
/``extract_source``, which route through the kernel when it is available and
would compare the kernel against itself. Parity is asserted on the **raw**
``ExtractionResult`` (dataclass equality, emission order included): both
engines emit nodes/edges/refs in the canonical sort order by contract, so raw
equality is required, not just canonical-JSON equality.

Tests that need the built kernel skip with a clear reason when it is absent
(CI wiring lands in a later wave); set ``KYS_REQUIRE_RUST_KERNEL=1`` to make an
absent kernel a hard failure instead of a skip.
"""

from __future__ import annotations

import hashlib
import json
import os
import subprocess
from pathlib import Path

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st

from know_your_soul.domain import canonical_json
from know_your_soul.graph_model import make_node_id
from know_your_soul.ingest import kernel_bridge
from know_your_soul.ingest.extractor import TreeSitterExtractor
from know_your_soul.ingest.languages import get_spec, supported_languages

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
REPO_ROOT = Path(__file__).resolve().parents[1]
KERNEL_TARGET = REPO_ROOT / "kernel-rust" / "target"

ALL_LANGUAGES = supported_languages()
#: Dual-engine languages (kernel + Python reference, byte-identical by gate).
#: Languages with ``engine == "python"`` (e.g. dart — no crates.io grammar
#: parse-identical to the bundled revision, docs/recovery/w7-languages.md)
#: are served by the Python walker through the bridge's fallback path.
DUAL_ENGINE_LANGUAGES = tuple(name for name in ALL_LANGUAGES if get_spec(name).engine == "rust")
PYTHON_FALLBACK_LANGUAGES = tuple(name for name in ALL_LANGUAGES if get_spec(name).engine == "python")


def _kernel_required() -> None:
    if kernel_bridge.kernel_available():
        return
    reason = kernel_bridge.kernel_unavailable_reason()
    if os.environ.get("KYS_REQUIRE_RUST_KERNEL"):
        pytest.fail(f"Rust kernel required but unavailable: {reason}")
    pytest.skip(f"Rust kernel not built ({reason}); skipping byte-identity tests")


def _kernel_cli() -> Path:
    for profile in ("release", "debug"):
        candidate = KERNEL_TARGET / profile / "kys-kernel"
        if candidate.is_file() and os.access(candidate, os.X_OK):
            return candidate
    if os.environ.get("KYS_REQUIRE_RUST_KERNEL"):
        pytest.fail(f"kys-kernel binary missing under {KERNEL_TARGET}")
    pytest.skip(f"kys-kernel binary missing under {KERNEL_TARGET} (cargo build --release)")
    raise AssertionError("unreachable")


def _fixture_rel(language: str) -> str:
    fixture = sorted((FIXTURES / language).iterdir())[0]
    return f"{language}/{fixture.name}"


def _python_walker_reference(relative: str):
    """The pure-Python walker, invoked directly — NO kernel routing (F1).

    ``extract_file``/``extract_source`` route to the Rust kernel when it is
    available, which made the pre-F1 parity tests compare the kernel against
    itself. This helper is the honest reference side.
    """
    source = (FIXTURES / relative).read_text(encoding="utf-8")
    return TreeSitterExtractor(get_spec(relative.split("/", 1)[0])).extract(relative, source)


@pytest.mark.parametrize("language", DUAL_ENGINE_LANGUAGES)
def test_bridge_matches_python_byte_identical(language: str) -> None:
    """Raw ``ExtractionResult`` from the kernel equals the Python walker's.

    F1 fix: the Python side is the direct walker (see ``_python_walker_reference``),
    and equality is asserted on the raw dataclass — emission order included,
    not just the canonical form. Both engines emit in canonical sort order.
    """
    _kernel_required()
    relative = _fixture_rel(language)
    python_result = _python_walker_reference(relative)
    rust_result = kernel_bridge.extract_file_via_kernel(FIXTURES / relative, display_path=relative)
    assert rust_result == python_result, "raw ExtractionResult mismatch (order or content)"
    assert rust_result.canonical_json() == python_result.canonical_json()


@pytest.mark.parametrize("language", DUAL_ENGINE_LANGUAGES)
def test_cli_matches_python_byte_identical(language: str) -> None:
    cli = _kernel_cli()
    relative = _fixture_rel(language)
    python_json = _python_walker_reference(relative).canonical_json()
    proc = subprocess.run(
        [str(cli), str(FIXTURES / relative), "--display-path", relative],
        capture_output=True,
        text=True,
    )
    assert proc.returncode == 0, f"{language}: kys-kernel failed: {proc.stderr}"
    assert proc.stdout == python_json + "\n", f"{language}: CLI stdout is not byte-identical"


def test_bridge_extraction_is_deterministic() -> None:
    _kernel_required()
    fixture = FIXTURES / "python" / "sample.py"
    first = kernel_bridge.extract_file_via_kernel(fixture, display_path="python/sample.py").canonical_json()
    second = kernel_bridge.extract_file_via_kernel(fixture, display_path="python/sample.py").canonical_json()
    assert first == second


def test_cli_two_runs_byte_identical() -> None:
    cli = _kernel_cli()
    fixture = FIXTURES / "rust" / "sample.rs"
    outputs = [
        subprocess.run(
            [str(cli), str(fixture), "--display-path", "rust/sample.rs"], capture_output=True, text=True
        ).stdout
        for _ in range(2)
    ]
    assert outputs[0] == outputs[1]
    assert outputs[0].endswith("\n")


def test_cli_error_shape_for_unknown_extension(tmp_path: Path) -> None:
    cli = _kernel_cli()
    target = tmp_path / "notes.txt"
    target.write_text("hello\n", encoding="utf-8")
    proc = subprocess.run([str(cli), str(target)], capture_output=True, text=True)
    assert proc.returncode == 2
    payload = json.loads(proc.stderr)
    assert payload["error"]["code"] == "UNSUPPORTED_MEDIA_TYPE"


def test_cli_rejects_files_over_the_1mib_bound(tmp_path: Path) -> None:
    cli = _kernel_cli()
    target = tmp_path / "big.py"
    target.write_bytes(b"x" * (1024 * 1024 + 1))
    proc = subprocess.run([str(cli), str(target)], capture_output=True, text=True)
    assert proc.returncode == 2
    assert json.loads(proc.stderr)["error"]["code"] == "FILE_TOO_LARGE"


def test_cli_rejects_invalid_utf8(tmp_path: Path) -> None:
    cli = _kernel_cli()
    target = tmp_path / "bad.py"
    target.write_bytes(b"\xff\xfe\x00invalid")
    proc = subprocess.run([str(cli), str(target)], capture_output=True, text=True)
    assert proc.returncode == 2
    assert json.loads(proc.stderr)["error"]["code"] == "DECODE_ERROR"


def test_kernel_reports_supported_languages() -> None:
    """The kernel serves exactly the dual-engine (``engine == "rust"``) set."""
    _kernel_required()
    assert tuple(kernel_bridge.kernel_supported_languages()) == DUAL_ENGINE_LANGUAGES


@pytest.mark.parametrize("language", PYTHON_FALLBACK_LANGUAGES)
def test_python_fallback_language_not_served_by_kernel(language: str) -> None:
    """Python-fallback languages are absent from the kernel's served set…"""
    _kernel_required()
    assert language not in kernel_bridge.kernel_supported_languages()


@pytest.mark.parametrize("language", PYTHON_FALLBACK_LANGUAGES)
def test_python_fallback_language_routes_to_reference(language: str) -> None:
    """…and ``extract_source`` still answers via the Python walker fallback.

    The kernel has no compiled grammar for these parsers, so the bridge's
    documented fallback re-runs the reference walker; the observable result
    must equal the direct Python walker's, byte-identically.
    """
    _kernel_required()
    relative = _fixture_rel(language)
    expected = _python_walker_reference(relative)
    from know_your_soul.ingest.extractor import extract_source

    source = (FIXTURES / relative).read_text(encoding="utf-8")
    result = extract_source(relative, source, language=language)
    assert result == expected
    assert result.canonical_json() == expected.canonical_json()


def test_kernel_make_node_id_matches_python() -> None:
    _kernel_required()
    kernel = kernel_bridge.kernel_module()
    cases = [
        ("src/a.py", "function", "f", 1, None),
        ("src/a.py", "function", "f", 1, 0),
        ("b/c.rs", "method", "m::x", 42, 7),
        ("weird path/ünïcode.py", "class", "Klasse", 3, None),
    ]
    for path, kind, name, line, col in cases:
        expected = make_node_id(path=path, kind=kind, name=name, line=line, col=col)
        assert kernel.make_node_id(path, kind, name, line, col) == expected


@given(
    path=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40),
    name=st.text(alphabet=st.characters(blacklist_categories=("Cs",)), min_size=1, max_size=40),
    line=st.integers(min_value=1, max_value=100000),
    col=st.one_of(st.none(), st.integers(min_value=0, max_value=1000)),
)
@settings(max_examples=100, deadline=None)
def test_make_node_id_property_matches_python(path: str, name: str, line: int, col: int | None) -> None:
    _kernel_required()
    kernel = kernel_bridge.kernel_module()
    expected = make_node_id(path=path, kind="function", name=name, line=line, col=col)
    assert kernel.make_node_id(path, kind="function", name=name, line=line, col=col) == expected


@given(data=st.data())
@settings(max_examples=30, deadline=None)
def test_extract_source_property_python_parity(data: st.DataObject) -> None:
    """Random small python sources extract identically (raw) in both engines.

    Identifiers exclude Python keywords: a keyword used as a declaration name
    (e.g. ``def else():``) is a syntax error, and tree-sitter *error recovery*
    differs between the two engines' parser cores — a documented residual
    (docs/recovery/w6-f1-parity.md), not part of the valid-source parity
    contract this test gates.
    """
    import keyword

    _kernel_required()
    identifiers = data.draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12).filter(
                lambda name: not keyword.iskeyword(name)
            ),
            min_size=1,
            max_size=6,
            unique=True,
        )
    )
    lines = [f"MAX_{identifiers[0].upper()} = {data.draw(st.integers(0, 100))}"]
    for name in identifiers:
        body = data.draw(st.sampled_from(["pass", f"return {name}", f"print({identifiers[0]})"]))
        lines.append(f"def {name}():")
        lines.append(f"    {body}")
    source = "\n".join(lines) + "\n"
    python_result = TreeSitterExtractor(get_spec("python")).extract("prop/sample.py", source)
    rust_result = kernel_bridge.extract_source_via_kernel("prop/sample.py", source, language="python")
    assert rust_result == python_result, "raw ExtractionResult mismatch (order or content)"
    assert rust_result.canonical_json() == python_result.canonical_json()


def test_canonical_json_serializer_matches_python() -> None:
    """Adversarial strings through the kernel's JSON writer equal Python's."""
    _kernel_required()
    kernel = kernel_bridge.kernel_module()
    nasty = [
        'quote"backslash\\',
        "line\nbreak\ttab\rcr",
        "control\x00\x1f chars",
        "del\x7flatínünïcode日本語",
        "emoji 🎉 and — dashes",
        "",
    ]
    for text in nasty:
        payload = {"k": text, "n": 1, "b": True, "z": False}
        assert kernel.canonical_json(payload) == canonical_json(payload)


GENERATED_CASES = [
    ("api.pb.go", "package api\n"),
    ("bundle.min.js", "var a=1;\n"),
    ("src/main.rs", "fn main() {}\n"),
    ("gen.go", "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage gen\n"),
    ("gen2.go", "/*\n * @generated by tool\n */\npackage gen\n"),
    ("plain.py", 'x = "code generated do not edit"\n'),  # string literal, not a comment
    ("note.js", "// automatically generated\n"),
    ("big.js", ("a;" * 1500 + "\n") * 2),  # minified-shaped
    ("big.py", ("a;" * 1500 + "\n") * 2),  # same shape, wrong extension
    ("short.js", "var a = 1;\n"),
    ("grpc.cs", "namespace X {}\n"),
    ("MyGrpc.cs", "namespace X {}\n"),
]


@pytest.mark.parametrize(("path", "source"), GENERATED_CASES)
def test_detect_generated_matches_python(path: str, source: str) -> None:
    _kernel_required()
    from know_your_soul.ingest.extractor import detect_generated as python_detect

    kernel = kernel_bridge.kernel_module()
    assert kernel.detect_generated(path, source) == python_detect(path, source)


def _ingest_fixtures_dump(tmp_path: Path, name: str) -> str:
    """Ingest the full 12-language fixture tree into a fresh store; dump it."""
    from know_your_soul.ingest.pipeline import IngestionPipeline, canonical_ingestion_dump
    from know_your_soul.store import EventStore

    with EventStore(str(tmp_path / name)) as store:
        IngestionPipeline(store).ingest_path(FIXTURES)
        return canonical_ingestion_dump(store)


def test_cross_engine_store_byte_identical(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """F1 gate: the persisted store is byte-identical across engines.

    Ingest the same fixture tree from scratch twice — once routed through the
    Rust kernel, once forced onto the pure-Python walker via the kill-switch —
    and require the full canonical ingestion dump (rowids included) to be
    byte-identical. Rowids/ref_ids are pure functions of emission order, and
    both engines emit in the canonical sort order by contract.
    """
    _kernel_required()
    with_kernel = _ingest_fixtures_dump(tmp_path, "with_kernel.sqlite3")

    monkeypatch.setenv(kernel_bridge.KILL_SWITCH_ENV, "1")
    kernel_bridge.reset_probe()
    assert not kernel_bridge.kernel_available()
    without_kernel = _ingest_fixtures_dump(tmp_path, "without_kernel.sqlite3")
    kernel_bridge.reset_probe()

    assert with_kernel == without_kernel, (
        "cross-engine store divergence: "
        f"with-kernel sha256={hashlib.sha256(with_kernel.encode()).hexdigest()[:16]} "
        f"vs no-kernel sha256={hashlib.sha256(without_kernel.encode()).hexdigest()[:16]}"
    )

"""Byte-identity tests: Rust extraction kernel vs the pure-Python extractor (W5, SUB-93).

The Rust kernel (``know_your_soul_kernel`` via PyO3 / ``kys-kernel`` CLI) must
produce canonical JSON artifacts that are **byte-identical** to the reference
Python walker for every phase-1 fixture, and stable ids must match
:func:`know_your_soul.graph_model.make_node_id` exactly.

Tests that need the built kernel skip with a clear reason when it is absent
(CI wiring lands in a later wave); set ``KYS_REQUIRE_RUST_KERNEL=1`` to make an
absent kernel a hard failure instead of a skip.
"""

from __future__ import annotations

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
from know_your_soul.ingest.extractor import extract_file
from know_your_soul.ingest.languages import supported_languages

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"
REPO_ROOT = Path(__file__).resolve().parents[1]
KERNEL_TARGET = REPO_ROOT / "kernel-rust" / "target"

ALL_LANGUAGES = supported_languages()


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


@pytest.mark.parametrize("language", ALL_LANGUAGES)
def test_bridge_matches_python_byte_identical(language: str) -> None:
    _kernel_required()
    relative = _fixture_rel(language)
    python_result = extract_file(FIXTURES / relative, display_path=relative)
    rust_result = kernel_bridge.extract_file_via_kernel(FIXTURES / relative, display_path=relative)
    assert rust_result == python_result, "ExtractionResult mismatch"
    assert rust_result.canonical_json() == python_result.canonical_json()


@pytest.mark.parametrize("language", ALL_LANGUAGES)
def test_cli_matches_python_byte_identical(language: str) -> None:
    cli = _kernel_cli()
    relative = _fixture_rel(language)
    python_json = extract_file(FIXTURES / relative, display_path=relative).canonical_json()
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
    _kernel_required()
    assert tuple(kernel_bridge.kernel_supported_languages()) == ALL_LANGUAGES


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
    """Random small python-ish sources extract byte-identically in both engines."""
    _kernel_required()
    identifiers = data.draw(
        st.lists(
            st.text(alphabet="abcdefghijklmnopqrstuvwxyz_", min_size=1, max_size=12),
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
    from know_your_soul.ingest.extractor import extract_source

    python_result = extract_source("prop/sample.py", source, language="python")
    rust_result = kernel_bridge.extract_source_via_kernel("prop/sample.py", source, language="python")
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

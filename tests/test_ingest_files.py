"""File-handling tests for the ingest extractor (W2, SUB-87): size bound,
typed errors, generated detection, language detection."""

from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from know_your_soul.ingest.extractor import (
    MAX_SOURCE_FILE_SIZE_BYTES,
    FileTooLargeError,
    IngestError,
    SourceDecodeError,
    UnsupportedMediaTypeError,
    detect_generated,
    extract_file,
)

MIB = 1024 * 1024


def write(path: Path, content: bytes) -> Path:
    path.write_bytes(content)
    return path


# ------------------------------------------------------------------ size cap


def test_size_cap_is_one_mebibyte():
    assert MAX_SOURCE_FILE_SIZE_BYTES == MIB


def test_file_at_exact_limit_is_read(tmp_path: Path):
    body = b"#" * (MIB - 2) + b"\n\n"
    target = write(tmp_path / "exact.py", body)
    result = extract_file(target)
    assert result.size == MIB


def test_file_one_byte_over_limit_raises(tmp_path: Path):
    target = write(tmp_path / "over.py", b"x" * (MIB + 1))
    with pytest.raises(FileTooLargeError) as excinfo:
        extract_file(target)
    assert excinfo.value.code == "FILE_TOO_LARGE"
    assert excinfo.value.size == MIB + 1
    assert excinfo.value.limit == MIB


def test_oversize_file_is_never_read(tmp_path: Path, monkeypatch: pytest.MonkeyPatch):
    """The guard must trigger from stat alone — content is never decoded."""
    target = write(tmp_path / "bomb.py", b"\xff" * (MIB + 10))
    # Invalid UTF-8 would raise SourceDecodeError if the bytes were read.
    with pytest.raises(FileTooLargeError):
        extract_file(target)


def test_file_too_large_is_ingest_error():
    assert issubclass(FileTooLargeError, IngestError)


# ----------------------------------------------------------- language detect


def test_unknown_extension_raises_unsupported(tmp_path: Path):
    target = write(tmp_path / "notes.md", b"# hi\n")
    with pytest.raises(UnsupportedMediaTypeError) as excinfo:
        extract_file(target)
    assert excinfo.value.code == "UNSUPPORTED_MEDIA_TYPE"


def test_extensionless_file_raises_unsupported(tmp_path: Path):
    target = write(tmp_path / "Makefile", b"all:\n\techo hi\n")
    with pytest.raises(UnsupportedMediaTypeError):
        extract_file(target)


def test_unsupported_media_type_is_ingest_error():
    assert issubclass(UnsupportedMediaTypeError, IngestError)


@pytest.mark.parametrize(
    ("name", "language"),
    [
        ("x.py", "python"),
        ("x.js", "javascript"),
        ("x.ts", "typescript"),
        ("x.go", "go"),
        ("x.rs", "rust"),
        ("x.java", "java"),
        ("x.c", "c"),
        ("x.cpp", "cpp"),
        ("x.cs", "csharp"),
        ("x.rb", "ruby"),
        ("x.php", "php"),
        ("x.sh", "bash"),
    ],
)
def test_extract_file_detects_language(tmp_path: Path, name: str, language: str):
    target = write(tmp_path / name, b"\n")
    assert extract_file(target).language == language


def test_display_path_used_for_ids(tmp_path: Path):
    target = write(tmp_path / "real.py", b"def f():\n    return 1\n")
    result = extract_file(target, display_path="pkg/real.py")
    assert result.path == "pkg/real.py"
    assert all(node.path == "pkg/real.py" for node in result.nodes)


def test_sha256_matches_file_bytes(tmp_path: Path):
    data = b"def f():\n    return 1\n"
    target = write(tmp_path / "hash.py", data)
    result = extract_file(target)
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)


# --------------------------------------------------------------- decode guard


def test_invalid_utf8_raises_decode_error(tmp_path: Path):
    target = write(tmp_path / "bad.py", b"def f():\xff\xfe\n")
    with pytest.raises(SourceDecodeError) as excinfo:
        extract_file(target)
    assert excinfo.value.code == "DECODE_ERROR"


def test_decode_error_is_ingest_error():
    assert issubclass(SourceDecodeError, IngestError)


def test_utf8_bom_is_accepted(tmp_path: Path):
    target = write(tmp_path / "bom.py", b"\xef\xbb\xbfdef f():\n    return 1\n")
    result = extract_file(target)
    assert "f" in {node.name for node in result.nodes}


def test_non_bom_unicode_source(tmp_path: Path):
    target = write(tmp_path / "uni.py", "def f():\n    return 'héllo'\n".encode())
    assert "f" in {node.name for node in extract_file(target).nodes}


# -------------------------------------------------------------- generated hint


@pytest.mark.parametrize(
    "name",
    [
        "types.pb.go",
        "service_grpc.pb.go",
        "thing_mock.go",
        "thing_mocks.go",
        "schema.generated.js",
        "schema.gen.ts",
        "bundle.pb.js",
        "app-min.js",
        "app.min.js",
        "model_pb2.py",
        "out.pb.cc",
        "out.pb.h",
        "Designer.g.cs",
        "ThingGrpc.cs",
        "api.generated.rs",
    ],
)
def test_generated_by_filename(name: str):
    assert detect_generated(name, "int x;\n") is True


@pytest.mark.parametrize(
    "name",
    [
        "main.go",
        "app.js",
        "mod.ts",
        "util.py",
        "lib.rs",
        "Thing.cs",
        "service.pbtxt.go",  # not a .pb.go
        "generated.py",  # bare word, no marker pattern
    ],
)
def test_not_generated_by_filename(name: str):
    assert detect_generated(name, "int x;\n") is False


@pytest.mark.parametrize(
    "banner",
    [
        "// Code generated by protoc-gen-go. DO NOT EDIT.\npackage x\n",
        "// Code generated by tool. DO NOT EDIT\npackage x\n",
        "# @generated by build\nx = 1\n",
        "/* <auto-generated> */\nint x;\n",
        "// Automatically generated by foolib; do not modify.\nint x;\n",
        "// This file was automatically generated.\nint x;\n",
        (
            "# ------------------------------------------------------------------------\n"
            "# <auto-generated>\n"
            "#     This code is generated.\n"
            "# ------------------------------------------------------------------------\n"
            "x = 1\n"
        ),
    ],
)
def test_generated_by_banner(banner: str):
    assert detect_generated("plain.go", banner) is True


def test_banner_beyond_scan_window_is_ignored():
    content = ("x = 1\n" * 100) + "# @generated\n"
    assert detect_generated("late.py", content) is False


def test_generated_word_in_code_is_not_a_banner():
    content = "x = 'this value was generated at runtime'\n"
    assert detect_generated("code.py", content) is False


def test_minified_javascript_detected():
    line = "function a(){return 1};" * 200  # >4KB single line, dense punctuation
    assert len(line) > 4096
    assert detect_generated("bundle.js", line + "\n") is True


def test_minified_requires_long_lines():
    content = "function a(){return 1};\n" * 200  # many short lines, not minified
    assert detect_generated("normal.js", content) is False


def test_minified_check_only_for_script_extensions():
    line = "x" * 5000 + ";{}(),;"
    assert detect_generated("big.py", line) is False


def test_generated_hint_propagates_to_result(tmp_path: Path):
    target = write(tmp_path / "model_pb2.py", b"x = 1\n")
    result = extract_file(target)
    assert result.generated_hint is True


def test_normal_file_hint_is_false(tmp_path: Path):
    target = write(tmp_path / "plain.py", b"x = 1\n")
    assert extract_file(target).generated_hint is False


def test_generated_hint_in_artifact(tmp_path: Path):
    target = write(tmp_path / "x_pb2.py", b"x = 1\n")
    artifact = extract_file(target).to_artifact()
    assert artifact["generated_hint"] is True

"""Kernel bridge routing tests (wave W5, SUB-93).

Routing contract (MASTER-PLAN multi-language decision): the extractor prefers
the Rust kernel; when the kernel is absent, disabled, or fails unexpectedly,
the pure-Python tree-sitter walker handles the request with an identical
result, and the reason is logged. Nothing here requires the Rust kernel to be
built — the pure-Python fallback path must always work.
"""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from know_your_soul.ingest import kernel_bridge
from know_your_soul.ingest.extractor import TreeSitterExtractor, extract_file, extract_source
from know_your_soul.ingest.languages import get_spec

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"


def _python_reference(relative: str):
    source = (FIXTURES / relative).read_text(encoding="utf-8")
    return TreeSitterExtractor(get_spec(relative.split("/", 1)[0])).extract(relative, source)


@pytest.fixture(autouse=True)
def _reset_kernel_probe():
    kernel_bridge.reset_probe()
    yield
    kernel_bridge.reset_probe()


def test_fallback_when_kernel_module_is_absent(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """Simulate an environment without the Rust kernel: import fails."""
    monkeypatch.setattr(kernel_bridge, "_import_kernel_module", lambda: None)
    kernel_bridge.reset_probe()
    with caplog.at_level(logging.INFO, logger="know_your_soul.ingest"):
        assert kernel_bridge.kernel_available() is False
        assert kernel_bridge.kernel_unavailable_reason()
        result = extract_source("x/sample.py", "def f():\n    pass\n", language="python")
    reference = TreeSitterExtractor(get_spec("python")).extract("x/sample.py", "def f():\n    pass\n")
    assert result == reference
    assert any("rust kernel" in record.message.lower() for record in caplog.records)


def test_kill_switch_env_disables_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("KYS_DISABLE_RUST_KERNEL", "1")
    kernel_bridge.reset_probe()
    assert kernel_bridge.kernel_available() is False
    assert "KYS_DISABLE_RUST_KERNEL" in kernel_bridge.kernel_unavailable_reason()
    result = extract_source("x/sample.go", "package main\n", language="go")
    assert result.language == "go"


def test_kernel_exception_falls_back_to_python(monkeypatch: pytest.MonkeyPatch, caplog) -> None:
    """An unexpected kernel failure must degrade to the Python walker."""

    class _BrokenKernel:
        @staticmethod
        def extract_source(*args, **kwargs):
            raise RuntimeError("simulated kernel panic")

    monkeypatch.setattr(kernel_bridge, "_import_kernel_module", lambda: _BrokenKernel)
    kernel_bridge.reset_probe()
    assert kernel_bridge.kernel_available() is True
    with caplog.at_level(logging.WARNING, logger="know_your_soul.ingest"):
        result = extract_source("x/sample.py", "def g():\n    return 1\n", language="python")
    reference = TreeSitterExtractor(get_spec("python")).extract("x/sample.py", "def g():\n    return 1\n")
    assert result == reference
    assert any("simulated kernel panic" in record.message for record in caplog.records)


def test_routing_prefers_kernel_when_available(monkeypatch: pytest.MonkeyPatch) -> None:
    calls: list[tuple] = []

    class _RecordingKernel:
        @staticmethod
        def extract_source(path, source, language, generated_hint):
            calls.append((path, language))
            return kernel_bridge._reference_extract_source(
                path, source, language=language, generated_hint=generated_hint
            )

    monkeypatch.setattr(kernel_bridge, "_import_kernel_module", lambda: _RecordingKernel)
    kernel_bridge.reset_probe()
    extract_source("x/sample.py", "def h():\n    pass\n", language="python")
    assert calls == [("x/sample.py", "python")]


def test_extract_file_fallback_matches_python(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(kernel_bridge, "_import_kernel_module", lambda: None)
    kernel_bridge.reset_probe()
    result = extract_file(FIXTURES / "python" / "sample.py", display_path="python/sample.py")
    assert result.language == "python"
    assert result.sha256 == _python_reference("python/sample.py").sha256


def test_error_paths_never_touch_the_kernel(monkeypatch: pytest.MonkeyPatch) -> None:
    """Unsupported media type must raise before any kernel involvement."""
    monkeypatch.setattr(kernel_bridge, "_import_kernel_module", lambda: None)
    kernel_bridge.reset_probe()
    from know_your_soul.ingest.extractor import UnsupportedMediaTypeError

    with pytest.raises(UnsupportedMediaTypeError):
        extract_source("x/notes.txt", "hello\n")


def test_kernel_module_accessor_contract() -> None:
    """kernel_module() returns the loaded module or raises RuntimeError."""
    if kernel_bridge.kernel_available():
        module = kernel_bridge.kernel_module()
        assert hasattr(module, "extract_source")
        assert callable(module.make_node_id)
    else:
        with pytest.raises(RuntimeError, match="not available"):
            kernel_bridge.kernel_module()


def test_relative_from_import_succeeds_through_kernel_routing() -> None:
    """F-SUB94-1 is fixed in the reference walker (wave W5, SUB-95):
    `from . import x` produces an import node for the imported name.
    The Rust kernel (kernel-rust, SUB-93) still refuses this shape with
    EXTRACTION_ERROR, so with the kernel active the bridge must fall back
    and the caller must see the fixed reference result — an extraction,
    never an exception."""
    expected_name = "x"
    reference = TreeSitterExtractor(get_spec("python")).extract("m/a.py", "from . import x\n")
    import_nodes = [node for node in reference.nodes if node.kind == "import"]
    assert [node.name for node in import_nodes] == [expected_name]
    if not kernel_bridge.kernel_available():
        pytest.skip("rust kernel not built")
    result = extract_source("m/a.py", "from . import x\n", language="python")
    assert result == reference

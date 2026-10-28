"""Optional Rust extraction kernel bridge (wave W5, SUB-93).

Routing contract (MASTER-PLAN, multi-language decision): the Rust kernel
(``know_your_soul_kernel``, built from ``kernel-rust/`` via maturin) is the
hot path; the pure-Python tree-sitter walker in
:mod:`know_your_soul.ingest.extractor` stays the reference and the guaranteed
fallback. Byte-identity between the two is proven by
``tests/test_kernel_rust.py``.

Load order for the kernel module:

1. ``import know_your_soul_kernel`` (installed via ``maturin develop`` or a
   prebuilt wheel — the distribution path);
2. the repo-local build ``kernel-rust/target/{release,debug}/
   libknow_your_soul_kernel.so`` (the development path — visible to imports
   but invisible to ``importlib.metadata``, so the dependency-policy lock
   contract is untouched).

Kill-switch: ``KYS_DISABLE_RUST_KERNEL`` (any non-empty value) forces the
Python path. Every fallback logs its reason. No model, provider, clock or
randomness anywhere.
"""

from __future__ import annotations

import importlib
import importlib.util
import json
import logging
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from know_your_soul.ingest.extractor import (
    ExtractedEdge,
    ExtractedNode,
    ExtractionResult,
    FileTooLargeError,
    SourceDecodeError,
    TreeSitterExtractor,
    UnresolvedRef,
    UnsupportedMediaTypeError,
)
from know_your_soul.ingest.languages import get_spec

if TYPE_CHECKING:
    pass

__all__ = [
    "KILL_SWITCH_ENV",
    "extract_file_via_kernel",
    "extract_source_via_kernel",
    "kernel_available",
    "kernel_module",
    "kernel_supported_languages",
    "kernel_unavailable_reason",
    "reset_probe",
]

logger = logging.getLogger(__name__)

#: Kill-switch env var (MASTER-PLAN pillar 7: every optimization has one).
KILL_SWITCH_ENV = "KYS_DISABLE_RUST_KERNEL"

_state: str = "unprobed"  # "unprobed" | "ready" | "unavailable"
_module: Any = None
_reason: str = ""


def reset_probe() -> None:
    """Forget the cached probe so the next call re-resolves (tests, ops)."""
    global _state, _module, _reason
    _state = "unprobed"
    _module = None
    _reason = ""


def _repo_root() -> Path:
    return Path(__file__).resolve().parents[3]


def _import_kernel_module() -> Any | None:
    """Import the kernel module, or ``None`` when it cannot be loaded."""
    try:
        return importlib.import_module("know_your_soul_kernel")
    except Exception as error:  # ImportError, OSError (bad .so), anything else
        logger.debug("rust kernel site-packages import failed: %s", error)
    for profile in ("release", "debug"):
        candidate = _repo_root() / "kernel-rust" / "target" / profile / "libknow_your_soul_kernel.so"
        if not candidate.is_file():
            continue
        try:
            spec = importlib.util.spec_from_file_location("know_your_soul_kernel", candidate)
            if spec is None or spec.loader is None:
                continue
            module = importlib.util.module_from_spec(spec)
            spec.loader.exec_module(module)
            if not hasattr(module, "extract_source"):
                # a plain `cargo build` overwrites this path with a cdylib
                # that lacks the pyo3 module — rebuild via `maturin build`.
                logger.debug("rust kernel at %s lacks the pyo3 module (stale cargo build)", candidate)
                continue
            logger.debug("rust kernel loaded from %s", candidate)
            return module
        except Exception as error:  # noqa: BLE001 — any load failure falls through
            logger.debug("rust kernel load from %s failed: %s", candidate, error)
    return None


def _probe() -> None:
    global _state, _module, _reason
    if _state != "unprobed":
        return
    if os.environ.get(KILL_SWITCH_ENV):
        _state = "unavailable"
        _reason = f"disabled via {KILL_SWITCH_ENV}"
        logger.info("rust kernel unavailable: %s", _reason)
        return
    module = _import_kernel_module()
    if module is None:
        _state = "unavailable"
        _reason = "module know_your_soul_kernel not found (build kernel-rust/ via maturin)"
        logger.info("rust kernel unavailable: %s", _reason)
        return
    _module = module
    _state = "ready"
    _reason = ""
    logger.debug("rust kernel ready (version %s)", getattr(module, "kernel_version", lambda: "?")())


def kernel_available() -> bool:
    """True when the Rust kernel is loaded and ready."""
    _probe()
    return _state == "ready"


def kernel_unavailable_reason() -> str:
    """Why the kernel is unavailable ("" when it is available)."""
    _probe()
    return _reason


def kernel_module() -> Any:
    """The loaded kernel module; raises RuntimeError when unavailable."""
    _probe()
    if _module is None:
        raise RuntimeError(f"rust kernel not available: {_reason}")
    return _module


def kernel_supported_languages() -> tuple[str, ...]:
    """Languages the kernel serves (from the shared registry dump), sorted."""
    return tuple(kernel_module().supported_languages())


def _map_kernel_error(error: BaseException) -> BaseException:
    """Map a kernel error onto the reference extractor's exception classes.

    Only the pre-extraction contract errors map one-to-one; ``EXTRACTION_ERROR``
    is deliberately *not* mapped — it means the walk itself failed (e.g. the
    kernel's empty-name guard on dot-only relative imports, which the fixed
    Python reference handles by falling through to the imported name), and the
    caller's fallback re-runs the Python walker so the observable behaviour
    is exactly the reference's.
    """
    args = getattr(error, "args", ())
    if len(args) >= 3 and isinstance(args[0], str) and isinstance(args[2], dict):
        code, _message, context = args[0], args[1], args[2]
        if code == FileTooLargeError.code:
            return FileTooLargeError(context["path"], size=context["size"], limit=context["limit"])
        if code == UnsupportedMediaTypeError.code:
            return UnsupportedMediaTypeError(context["path"])
        if code == SourceDecodeError.code:
            return SourceDecodeError(context["path"])
    return error


def _result_from_artifact(payload: dict[str, Any]) -> ExtractionResult:
    """Rebuild the ExtractionResult dataclass from the kernel's artifact dict."""
    return ExtractionResult(
        path=payload["path"],
        language=payload["language"],
        sha256=payload["sha256"],
        size=payload["size"],
        generated_hint=payload["generated_hint"],
        parse_error=payload["parse_error"],
        extraction_version=payload["extraction_version"],
        nodes=tuple(ExtractedNode(**node) for node in payload["nodes"]),
        edges=tuple(ExtractedEdge(**edge) for edge in payload["edges"]),
        unresolved_refs=tuple(UnresolvedRef(**ref) for ref in payload["unresolved_refs"]),
    )


def extract_source_via_kernel(
    path: str,
    source: str,
    *,
    language: str,
    generated_hint: bool = False,
) -> ExtractionResult:
    """Extract via the Rust kernel; kernel errors map onto reference types."""
    module = kernel_module()
    try:
        payload = module.extract_source(path, source, language, generated_hint)
    except Exception as error:
        raise _map_kernel_error(error) from error
    return _result_from_artifact(json.loads(payload))


def extract_file_via_kernel(path: str | Path, *, display_path: str | None = None) -> ExtractionResult:
    """The kernel's standalone file path (CLI parity); same mapping rules."""
    module = kernel_module()
    try:
        payload = module.extract_file(str(path), display_path)
    except Exception as error:
        raise _map_kernel_error(error) from error
    return _result_from_artifact(json.loads(payload))


def _reference_extract_source(
    path: str,
    source: str,
    *,
    language: str,
    generated_hint: bool = False,
) -> ExtractionResult:
    """The pure-Python walker — the reference implementation and fallback."""
    return TreeSitterExtractor(get_spec(language)).extract(path, source, generated_hint=generated_hint)

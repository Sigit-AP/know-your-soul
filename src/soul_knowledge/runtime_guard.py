"""Fail-closed runtime guards for the deterministic, model-free knowledge boundary.

Spec: ``docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md`` sections 4 and 13;
plan: Task 1 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``.

Design rules:

* Every check **fails closed**: a violation raises a :class:`RuntimeGuardError`
  subclass. Nothing here warns, logs and continues, retries, downloads, reads a
  credential, or opens a network connection.
* The guards describe and enforce an *enabled runtime surface*, not the content
  of an installed distribution. Dormant optional source inside the pinned
  Graphify distribution (``graphify.llm``, ``graphify.serve``, provider extras)
  is outside that surface: it is unimportable while the import guard is
  installed and unreachable from the locked environment.
* :func:`verify_runtime` is the single entry point used by later tasks and by
  the release gate. It returns a JSON-serializable report and never silently
  degrades.
"""

from __future__ import annotations

import ast
import importlib.metadata as importlib_metadata
import os
import re
import socket
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.machinery import PathFinder
from importlib.util import find_spec
from typing import Any

__all__ = [
    "ALLOWED_MCP_CAPABILITIES",
    "CapabilityViolation",
    "DORMANT_PINNED_ENTRY_POINTS",
    "FORBIDDEN_DISTRIBUTIONS",
    "FORBIDDEN_ENTRY_POINT_SCRIPTS",
    "FORBIDDEN_IMPORT_ROOTS",
    "FORBIDDEN_MCP_METHODS",
    "ForbiddenDependencyError",
    "ForbiddenImportError",
    "GRAPHIFY_PIN",
    "ImportGuard",
    "OutboundSocketDenied",
    "PROVIDER_CREDENTIAL_ENV",
    "PROVIDER_IMPORT_ROOTS",
    "ProviderCredentialError",
    "PythonVersionError",
    "REQUIRED_PYTHON",
    "RuntimeGuardError",
    "SAFE_ENV_KEYS",
    "SocketGuard",
    "SUPPORTED_PYTHON_RANGE",
    "assert_mcp_capabilities",
    "assert_no_forbidden_distributions",
    "assert_no_forbidden_entry_points",
    "assert_no_forbidden_imports",
    "assert_no_model_asset_env",
    "assert_no_provider_credentials",
    "assert_no_sampling_handler",
    "assert_supported_python",
    "blocking_forbidden_entry_points",
    "cli_scraping_violations",
    "deny_outbound_sockets",
    "deterministic_env",
    "forbidden_entry_points",
    "forbidden_mcp_methods",
    "import_block_evidence",
    "import_guard",
    "install_import_guard",
    "install_socket_guard",
    "installed_distributions",
    "is_forbidden_distribution",
    "is_forbidden_module",
    "model_asset_env",
    "observed_mcp_capabilities",
    "provider_credentials",
    "removed_credential_names",
    "scrubbed_env",
    "verify_runtime",
]

REQUIRED_PYTHON: tuple[int, int] = (3, 14)
SUPPORTED_PYTHON_RANGE: str = ">=3.14,<3.15"
GRAPHIFY_PIN: str = "graphifyy[mcp]==0.9.95"

# ---------------------------------------------------------------------------
# Forbidden import surface
# ---------------------------------------------------------------------------

# Provider SDKs, model runtimes, embedding/vector stores and outbound sinks.
PROVIDER_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "azure.ai",
        "boto3",
        "botocore",
        "cohere",
        "google.generativeai",
        "groq",
        "huggingface_hub",
        "langchain",
        "litellm",
        "llama_cpp",
        "mistralai",
        "ollama",
        "onnxruntime",
        "pinecone",
        "qdrant_client",
        "sentence_transformers",
        "tensorflow",
        "tiktoken",
        "together",
        "torch",
        "transformers",
        "vllm",
        "voyageai",
        "weaviate",
        "chromadb",
        "faiss",
        "lancedb",
        "faster_whisper",
        "yt_dlp",
    }
)

# Dormant Graphify surfaces the product must never import (research artifact
# ``docs/research/graphify-mcp-security-deep.md`` section 7.2 R1/R3/R4/R5/R6/R11).
GRAPHIFY_REJECTED_IMPORT_ROOTS: frozenset[str] = frozenset(
    {
        "graphify.llm",
        "graphify.transcribe",
        "graphify.ingest",
        "graphify.prs",
        "graphify.serve",
        "graphify.mcp_ingest",
        "graphify.exporters.graphdb",
        "graphify.exporters",
    }
)

FORBIDDEN_IMPORT_ROOTS: frozenset[str] = (
    PROVIDER_IMPORT_ROOTS
    | GRAPHIFY_REJECTED_IMPORT_ROOTS
    | frozenset({"graspologic", "graspologic_native", "neo4j", "falkordb", "psycopg", "jieba"})
)

FORBIDDEN_DISTRIBUTIONS: frozenset[str] = frozenset(
    {
        "openai",
        "anthropic",
        "boto3",
        "botocore",
        "cohere",
        "google-genai",
        "google-generativeai",
        "vertexai",
        "groq",
        "tiktoken",
        "transformers",
        "sentence-transformers",
        "torch",
        "torchaudio",
        "torchvision",
        "tensorflow",
        "onnxruntime",
        "llama-cpp-python",
        "ollama",
        "mistralai",
        "together",
        "voyageai",
        "faiss-cpu",
        "faiss-gpu",
        "chromadb",
        "pinecone-client",
        "weaviate-client",
        "qdrant-client",
        "lancedb",
        "langchain",
        "langchain-core",
        "litellm",
        "vllm",
        "faster-whisper",
        "yt-dlp",
        "graspologic",
        "graspologic-native",
        "neo4j",
        "falkordb",
        "psycopg",
        "psycopg-binary",
        "psycopg2",
        "jieba",
        "jieba-py",
        # Document/media/site tooling that pulls model assets or writes files the
        # deterministic runtime must not touch. Kept in one shared set so the
        # runtime guard and the packaging tests cannot drift apart.
        "google-cloud-aiplatform",
        "watchdog",
        "pypdf",
        "markdownify",
        "python-docx",
        "openpyxl",
        "pillow",
        "matplotlib",
        "robotframework",
    }
)

# ---------------------------------------------------------------------------
# Forbidden environment surface
# ---------------------------------------------------------------------------

PROVIDER_CREDENTIAL_ENV: frozenset[str] = frozenset(
    {
        "OPENAI_API_KEY",
        "OPENAI_ORG_ID",
        "OPENAI_BASE_URL",
        "ANTHROPIC_API_KEY",
        "ANTHROPIC_AUTH_TOKEN",
        "GOOGLE_API_KEY",
        "GOOGLE_APPLICATION_CREDENTIALS",
        "GEMINI_API_KEY",
        "VERTEXAI_API_KEY",
        "MISTRAL_API_KEY",
        "COHERE_API_KEY",
        "GROQ_API_KEY",
        "TOGETHER_API_KEY",
        "VOYAGE_API_KEY",
        "REPLICATE_API_TOKEN",
        "AZURE_OPENAI_API_KEY",
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "AWS_SESSION_TOKEN",
        "HF_TOKEN",
        "HUGGING_FACE_HUB_TOKEN",
        "HUGGINGFACE_HUB_TOKEN",
        "LANGSMITH_API_KEY",
        "WANDB_API_KEY",
        "PINECONE_API_KEY",
        "WEAVIATE_API_KEY",
        "QDRANT_API_KEY",
        "HF_HOME",
        "HF_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TIKTOKEN_CACHE_DIR",
        "TORCH_HOME",
        "OLLAMA_HOST",
        "OLLAMA_MODELS",
        "TOKENIZERS_PARALLELISM",
    }
)

MODEL_ASSET_ENV: frozenset[str] = frozenset(
    {
        "HF_HOME",
        "HF_HUB_CACHE",
        "HF_MODULES_CACHE",
        "HUGGINGFACE_HUB_CACHE",
        "TRANSFORMERS_CACHE",
        "TIKTOKEN_CACHE_DIR",
        "TORCH_HOME",
        "OLLAMA_HOST",
        "OLLAMA_MODELS",
    }
)

_CREDENTIAL_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r"(^|_)API_?KEY$"),
    re.compile(r"(^|_)API_?TOKEN$"),
    re.compile(r"(^|_)ACCESS_?TOKEN$"),
    re.compile(r"(^|_)AUTH_?TOKEN$"),
    re.compile(r"(^|_)SECRET$"),
    re.compile(r"(^|_)SECRET_?KEY$"),
    re.compile(r"(^|_)TOKEN$"),
    re.compile(r"^AWS_(ACCESS|SECRET|SESSION)"),
    re.compile(r"^AZURE_OPENAI"),
    re.compile(r"^COHERE_"),
    re.compile(r"^GOOGLE_(API|APPLICATION)"),
    re.compile(r"^GEMINI_"),
    re.compile(r"^GROQ_"),
    re.compile(r"^HF_"),
    re.compile(r"^HUGGING"),
    re.compile(r"^LANGSMITH_"),
    re.compile(r"^MISTRAL_"),
    re.compile(r"^OLLAMA"),
    re.compile(r"^OPENAI_"),
    re.compile(r"^PINECONE_"),
    re.compile(r"^QDRANT_"),
    re.compile(r"^REPLICATE_"),
    re.compile(r"^TIKTOKEN_"),
    re.compile(r"^TOGETHER_"),
    re.compile(r"^TORCH_"),
    re.compile(r"^TRANSFORMERS_"),
    re.compile(r"^VOYAGE_"),
    re.compile(r"^WANDB_"),
    re.compile(r"^WEAVIATE_"),
)

# Process essentials that may survive scrubbing (mirrors the Hermes stdio child
# baseline in ``tools/mcp_tool_config.py`` ``_SAFE_ENV_KEYS``).
SAFE_ENV_KEYS: frozenset[str] = frozenset(
    {
        "PATH",
        "HOME",
        "USER",
        "LOGNAME",
        "LANG",
        "LANGUAGE",
        "LC_ALL",
        "LC_CTYPE",
        "TERM",
        "SHELL",
        "TMPDIR",
        "TEMP",
        "TMP",
        "TZ",
        "PYTHONHASHSEED",
        "PYTHONUNBUFFERED",
        "PYTHONIOENCODING",
        "VIRTUAL_ENV",
        "SSL_CERT_FILE",
        "SSL_CERT_DIR",
        "SYSTEMROOT",
        "COMSPEC",
        "PATHEXT",
        "WINDIR",
    }
)

# ---------------------------------------------------------------------------
# MCP capability surface
# ---------------------------------------------------------------------------

ALLOWED_MCP_CAPABILITIES: frozenset[str] = frozenset({"tools", "resources"})

FORBIDDEN_MCP_CAPABILITIES: frozenset[str] = frozenset({"sampling", "prompts", "logging", "completions", "elicitation"})

FORBIDDEN_MCP_METHODS: frozenset[str] = frozenset(
    {
        "sampling/createMessage",
        "prompts/list",
        "prompts/get",
        "logging/setLevel",
        "completion/complete",
        "elicitation/create",
    }
)


# ---------------------------------------------------------------------------
# Errors
# ---------------------------------------------------------------------------


class RuntimeGuardError(RuntimeError):
    """Base class for every fail-closed runtime guard violation."""


class PythonVersionError(RuntimeGuardError):
    """The interpreter is not the supported Python."""


class ForbiddenImportError(RuntimeGuardError):
    """A forbidden module was imported, is loaded, or could still be resolved."""


class ForbiddenDependencyError(RuntimeGuardError):
    """A forbidden distribution is installed or reachable in the locked environment."""


class ProviderCredentialError(RuntimeGuardError):
    """A provider credential or model-asset variable is present in the environment."""


class OutboundSocketDenied(RuntimeGuardError):
    """An outbound network connection was attempted while the socket guard was active."""


class CapabilityViolation(RuntimeGuardError):
    """The declared MCP capability surface is not exactly tools+resources."""


# ---------------------------------------------------------------------------
# Python version
# ---------------------------------------------------------------------------


def assert_supported_python(version_info: tuple[int, int] | None = None) -> tuple[int, int]:
    """Fail closed unless the interpreter is the supported Python."""
    current = tuple(version_info or sys.version_info[:2])
    if tuple(current[:2]) != REQUIRED_PYTHON:
        raise PythonVersionError(
            f"Python {REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]} is required ({SUPPORTED_PYTHON_RANGE}); "
            f"got {current[0]}.{current[1]}"
        )
    return REQUIRED_PYTHON


# ---------------------------------------------------------------------------
# Distributions and entry points
# ---------------------------------------------------------------------------


def _canonical(name: str) -> str:
    return re.sub(r"[-_.]+", "-", name).strip().lower()


def installed_distributions() -> dict[str, str]:
    """Installed distributions as ``{canonical_name: version}``."""
    return {
        _canonical(distribution.metadata["Name"]): distribution.version
        for distribution in importlib_metadata.distributions()
    }


def is_forbidden_distribution(name: str) -> bool:
    """True when *name* is a forbidden distribution or one of its variants.

    The single source of truth for both the runtime guard and the packaging
    tests (``tests/test_dependency_policy.py``). Matching is **exact or
    separator-bounded** (``entry`` or ``entry + "-"``): a variant such as
    ``torchaudio`` needs an explicit entry, while ``torch-cuda`` is caught by
    the ``torch`` entry. A bare prefix match would false-positive on benign
    lookalikes (``torches``, ``ollamarama``) and is deliberately not used.
    """
    normalized = _canonical(name)
    return any(normalized == entry or normalized.startswith(entry + "-") for entry in FORBIDDEN_DISTRIBUTIONS)


def forbidden_distributions(installed: Mapping[str, str] | None = None) -> list[str]:
    """Names of forbidden distributions present in the environment, sorted."""
    available = installed_distributions() if installed is None else dict(installed)
    return sorted(name for name in available if is_forbidden_distribution(name))


def assert_no_forbidden_distributions(installed: Mapping[str, str] | None = None) -> None:
    offenders = forbidden_distributions(installed)
    if offenders:
        raise ForbiddenDependencyError("forbidden provider/vector/model distributions present: " + ", ".join(offenders))


def is_forbidden_module(fullname: str) -> bool:
    """True when *fullname* is a forbidden module or one of its submodules."""
    if not fullname:
        return False
    for root in FORBIDDEN_IMPORT_ROOTS:
        if fullname == root or fullname.startswith(root + "."):
            return True
    return False


# Console-script names the product must never expose. ``graphify-mcp`` targets
# ``graphify.serve:_main`` and is therefore already caught by the forbidden
# import-root rule; the name is listed explicitly so a future retarget cannot
# slip past unnoticed.
FORBIDDEN_ENTRY_POINT_SCRIPTS: frozenset[str] = frozenset({"graphify-mcp"})

# The pinned Graphify wheel unavoidably ships
# ``graphify-mcp = graphify.serve:_main``. The entry-point policy fails closed on
# it, but the wheel cannot be changed without forking upstream, so this exact
# entry point is a *recorded boundary* rather than a rule silently dropped. ADR
# 0001 section 3 (no-model boundary) and
# ``docs/research/graphify-feature-matrix.md`` §5 record the residual and its
# owner; ``verify_runtime`` reports it explicitly.
DORMANT_PINNED_ENTRY_POINTS: frozenset[str] = frozenset({"graphifyy:console_scripts:graphify-mcp=graphify.serve:_main"})


def _is_forbidden_entry_point(entry_point: Any) -> bool:
    raw_value = str(entry_point.value)
    module = raw_value.split(":", 1)[0].strip()
    if not module:
        # A value with no parseable module target cannot be verified against the
        # forbidden import roots. Fail closed instead of silently ignoring it.
        raise ForbiddenDependencyError(
            f"unverifiable entry point {str(entry_point.name)!r}: value {raw_value!r} has no module target"
        )
    if is_forbidden_module(module):
        return True
    name = str(entry_point.name).strip()
    if entry_point.group == "console_scripts" and (
        name in FORBIDDEN_ENTRY_POINT_SCRIPTS or is_forbidden_distribution(name)
    ):
        return True
    return False


def forbidden_entry_points(distributions: Iterable[Any] | None = None) -> list[str]:
    """Every forbidden entry point exposed by the environment, sorted.

    Fails closed on (a) any entry point whose target module is a forbidden import
    root — this includes the pinned wheel's ``graphify-mcp = graphify.serve:_main``
    — and (b) provider console-script names or the explicit
    :data:`FORBIDDEN_ENTRY_POINT_SCRIPTS` names. Output is
    ``dist:group:name=value``.
    """
    sources = importlib_metadata.distributions() if distributions is None else distributions
    offenders: list[str] = []
    for distribution in sources:
        name = distribution.metadata["Name"]
        for entry_point in distribution.entry_points:
            if _is_forbidden_entry_point(entry_point):
                offenders.append(f"{name}:{entry_point.group}:{entry_point.name}={entry_point.value}")
    return sorted(offenders)


def blocking_forbidden_entry_points(distributions: Iterable[Any] | None = None) -> list[str]:
    """Forbidden entry points that are not the recorded pinned-wheel boundary."""
    return [
        offender for offender in forbidden_entry_points(distributions) if offender not in DORMANT_PINNED_ENTRY_POINTS
    ]


def assert_no_forbidden_entry_points(
    distributions: Iterable[Any] | None = None,
    *,
    allow_pinned_boundary: bool = False,
) -> None:
    """Fail closed on forbidden entry points.

    The default is strict: even the pinned-wheel ``graphify-mcp`` boundary raises.
    :func:`verify_runtime` passes ``allow_pinned_boundary=True`` so it can run in
    an environment that still ships the pinned wheel, and reports that boundary
    explicitly instead of hiding it.
    """
    offenders = (
        blocking_forbidden_entry_points(distributions)
        if allow_pinned_boundary
        else forbidden_entry_points(distributions)
    )
    if offenders:
        raise ForbiddenDependencyError("forbidden entry points exposed: " + ", ".join(offenders))


# ---------------------------------------------------------------------------
# Environment scrubbing
# ---------------------------------------------------------------------------


def _is_credential_like(key: str) -> bool:
    if key in PROVIDER_CREDENTIAL_ENV:
        return True
    return any(pattern.search(key) for pattern in _CREDENTIAL_PATTERNS)


def _is_model_asset_like(key: str) -> bool:
    return key in MODEL_ASSET_ENV


def provider_credentials(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Provider credentials / model-asset variables present in *env*, sorted by name."""
    source = os.environ if env is None else env
    return {key: source[key] for key in sorted(source) if _is_credential_like(key)}


def model_asset_env(env: Mapping[str, str] | None = None) -> dict[str, str]:
    """Model-asset cache variables present in *env*, sorted by name."""
    source = os.environ if env is None else env
    return {key: source[key] for key in sorted(source) if _is_model_asset_like(key)}


def assert_no_provider_credentials(env: Mapping[str, str] | None = None) -> None:
    found = provider_credentials(env)
    if found:
        raise ProviderCredentialError(
            "provider credentials must be absent from the runtime environment: " + ", ".join(found)
        )


def assert_no_model_asset_env(env: Mapping[str, str] | None = None) -> None:
    found = model_asset_env(env)
    if found:
        raise ProviderCredentialError("model-asset environment must be absent: " + ", ".join(found))


def removed_credential_names(env: Mapping[str, str] | None = None) -> tuple[str, ...]:
    """Names that :func:`scrubbed_env` (or :func:`deterministic_env`) would remove."""
    source = os.environ if env is None else env
    return tuple(key for key in sorted(source) if (_is_credential_like(key) or _is_model_asset_like(key)))


def scrubbed_env(
    env: Mapping[str, str] | None = None,
    *,
    allow: Iterable[str] = (),
) -> dict[str, str]:
    """Return an allowlisted environment for a child process or a verification run.

    Keeps process essentials plus ``XDG_*`` and the explicit *allow* names, and
    drops every provider credential and model-asset variable.
    """
    source = os.environ if env is None else env
    permitted = set(allow)
    scrubbed: dict[str, str] = {}
    for key, value in source.items():
        if key in permitted:
            scrubbed[key] = value
            continue
        if _is_credential_like(key) or _is_model_asset_like(key):
            continue
        if key in SAFE_ENV_KEYS or key.startswith("XDG_"):
            scrubbed[key] = value
    return scrubbed


def deterministic_env(
    env: Mapping[str, str] | None = None,
    *,
    allow: Iterable[str] = (),
) -> dict[str, str]:
    """A scrubbed environment with ``PYTHONHASHSEED=0`` for deterministic runs.

    Spec section 13.5: the guarded build runs one pinned partitioner with a fixed
    hash seed, so hash-order cannot leak into published artifacts.
    """
    scrubbed = scrubbed_env(env, allow=allow)
    scrubbed["PYTHONHASHSEED"] = "0"
    return scrubbed


# ---------------------------------------------------------------------------
# Import guard
# ---------------------------------------------------------------------------


@dataclass
class ImportGuard:
    """Meta-path guard that makes forbidden modules raise instead of importing."""

    blocked: frozenset[str] = field(default_factory=lambda: FORBIDDEN_IMPORT_ROOTS)
    _finder: Any = field(default=None, init=False, repr=False)
    _installed: bool = field(default=False, init=False, repr=False)

    def install(self) -> ImportGuard:
        if not self._installed:
            self._finder = _BlockingFinder(self.blocked)
            sys.meta_path.insert(0, self._finder)
            self._installed = True
        return self

    def uninstall(self) -> None:
        if self._installed and self._finder is not None:
            while self._finder in sys.meta_path:
                sys.meta_path.remove(self._finder)
            self._finder = None
            self._installed = False

    def __enter__(self) -> ImportGuard:
        return self.install()

    def __exit__(self, *exc_info: object) -> None:
        self.uninstall()

    def loaded(self) -> list[str]:
        return sorted(name for name in sys.modules if is_forbidden_module(name))

    def assert_not_loaded(self) -> None:
        loaded = self.loaded()
        if loaded:
            raise ForbiddenImportError("forbidden modules are loaded: " + ", ".join(loaded))

    def assert_clean(self) -> None:
        """Fail closed unless every blocked root is unloaded *and* unresolvable."""
        self.assert_not_loaded()
        already_installed = self._installed
        self.install()
        try:
            for root in sorted(self.blocked):
                try:
                    spec = find_spec(root)
                except ForbiddenImportError:
                    continue
                except ModuleNotFoundError:
                    # The parent package of this dotted root is absent entirely,
                    # so the module cannot be imported at all.
                    continue
                if spec is not None:
                    raise ForbiddenImportError(f"the import guard did not block {root!r}")
        finally:
            if not already_installed:
                self.uninstall()


class _BlockingFinder:
    """Meta-path finder that raises for every forbidden module name."""

    def __init__(self, blocked: frozenset[str]) -> None:
        self.blocked = blocked

    def find_spec(self, fullname: str, path: Any = None, target: Any = None) -> None:
        if is_forbidden_module(fullname):
            raise ForbiddenImportError(
                f"{fullname!r} is outside the enabled deterministic runtime and will not be imported"
            )
        return None


def import_guard(blocked: Iterable[str] | None = None) -> ImportGuard:
    """A :class:`ImportGuard` context manager (installs on ``__enter__``)."""
    return ImportGuard(frozenset(blocked) if blocked is not None else FORBIDDEN_IMPORT_ROOTS)


def install_import_guard(blocked: Iterable[str] | None = None) -> ImportGuard:
    """Install the guard immediately and return it (call ``uninstall()`` to drop it)."""
    return import_guard(blocked).install()


def assert_no_forbidden_imports(blocked: Iterable[str] | None = None) -> None:
    """Fail closed unless every blocked root is loaded-free and unresolvable."""
    guard = import_guard(blocked)
    guard.assert_clean()


def _resolvable_without_import(root: str) -> bool:
    """True when *root* resolves, without importing anything or touching meta-path.

    Uses :class:`importlib.machinery.PathFinder` directly, so the probe has no
    ``sys.modules`` side effects (a plain ``find_spec`` on a dotted root imports
    its parent package — which would both pollute the process and short-circuit
    the guarded probe that follows).
    """
    parts = root.split(".")
    path: Any = None
    for index in range(len(parts)):
        name = ".".join(parts[: index + 1])
        spec = PathFinder.find_spec(name, path)
        if spec is None:
            return False
        path = spec.submodule_search_locations
    return True


def import_block_evidence(blocked: Iterable[str] | None = None) -> dict[str, list[str]]:
    """Non-vacuous probe of the import guard, with honest per-root evidence.

    Returns ``{"actively_blocked": [...], "not_installed": [...]}``:

    * ``actively_blocked`` — roots that **resolve** in this environment and are
      actively rejected by the guard. Only these prove the guard bites.
    * ``not_installed`` — roots with no resolvable module here; the guard has
      nothing to block for them, and they are reported as such instead of
      being silently counted as "blocked" (the old probe's overpromise).

    Fails closed: a forbidden module already present in ``sys.modules`` is a
    violation (the meta-path guard cannot intercept an already-loaded module),
    and a resolvable root the guard does not reject raises
    :class:`ForbiddenImportError` — never a soft report.
    """
    roots = sorted(FORBIDDEN_IMPORT_ROOTS if blocked is None else blocked)
    rootset = frozenset(roots)
    resolvable: list[str] = []
    absent: list[str] = []
    for root in roots:
        # A root with a forbidden ancestor is covered by the ancestor's probe:
        # importing the child requires importing the blocked parent first.
        parts = root.split(".")
        if any(".".join(parts[:index]) in rootset for index in range(1, len(parts))):
            continue
        (resolvable if _resolvable_without_import(root) else absent).append(root)
    guard = import_guard(roots)
    guard.install()
    try:
        guard.assert_not_loaded()
        actively_blocked: list[str] = []
        for root in resolvable:
            try:
                find_spec(root)
            except ForbiddenImportError:
                actively_blocked.append(root)
                continue
            raise ForbiddenImportError(f"the import guard did not block {root!r}")
    finally:
        guard.uninstall()
    return {"actively_blocked": actively_blocked, "not_installed": absent}


# ---------------------------------------------------------------------------
# Socket guard
# ---------------------------------------------------------------------------


class SocketGuard:
    """Denies outbound TCP/UDP while leaving local AF_UNIX sockets usable.

    Local IPC is the product's default transport (Unix socket), so AF_UNIX stays
    allowed; every AF_INET/AF_INET6 connect, connect-ex, sendto and
    ``create_connection`` call raises :class:`OutboundSocketDenied`.

    The guard is re-entrant and **reference counted**. Only the first
    ``install()`` captures the real originals and patches the class; nested
    installs (for example the probe :func:`verify_runtime` runs inside an
    already-active guard) merely bump the count, and the patch is restored when
    the last guard uninstalls. Without the count, a nested guard captures the
    already-denied functions as its own "originals": uninstalling it would leave
    ``socket.socket.connect`` patched while ``_SOCKET_ORIGINALS`` was emptied, so
    an *allowed* AF_UNIX connect would raise ``KeyError``.
    """

    _LOCAL_FAMILIES = frozenset({socket.AF_UNIX})

    def __init__(self) -> None:
        self._installed = False

    def install(self) -> SocketGuard:
        global _SOCKET_GUARD_DEPTH
        if self._installed:
            return self
        if _SOCKET_GUARD_DEPTH == 0:
            _SOCKET_ORIGINALS.update(
                {
                    "connect": socket.socket.connect,
                    "connect_ex": socket.socket.connect_ex,
                    "sendto": socket.socket.sendto,
                    "sendmsg": socket.socket.sendmsg,
                    "create_connection": socket.create_connection,
                }
            )
            socket.socket.connect = _denied_connect  # type: ignore[method-assign]
            socket.socket.connect_ex = _denied_connect_ex  # type: ignore[method-assign]
            socket.socket.sendto = _denied_sendto  # type: ignore[method-assign]
            socket.socket.sendmsg = _denied_sendmsg  # type: ignore[method-assign]
            socket.create_connection = _denied_create_connection  # type: ignore[assignment]
        _SOCKET_GUARD_DEPTH += 1
        self._installed = True
        return self

    def uninstall(self) -> None:
        global _SOCKET_GUARD_DEPTH
        if not self._installed:
            return
        _SOCKET_GUARD_DEPTH = max(_SOCKET_GUARD_DEPTH - 1, 0)
        if _SOCKET_GUARD_DEPTH == 0 and _SOCKET_ORIGINALS:
            socket.socket.connect = _SOCKET_ORIGINALS["connect"]  # type: ignore[method-assign]
            socket.socket.connect_ex = _SOCKET_ORIGINALS["connect_ex"]  # type: ignore[method-assign]
            socket.socket.sendto = _SOCKET_ORIGINALS["sendto"]  # type: ignore[method-assign]
            socket.socket.sendmsg = _SOCKET_ORIGINALS["sendmsg"]  # type: ignore[method-assign]
            socket.create_connection = _SOCKET_ORIGINALS["create_connection"]
            _SOCKET_ORIGINALS.clear()
        self._installed = False

    def __enter__(self) -> SocketGuard:
        return self.install()

    def __exit__(self, *exc_info: object) -> None:
        self.uninstall()


# Originals for the active SocketGuard plus the install depth. Module-level so
# the patched class attributes are plain functions: a bound method assigned to a
# class attribute would not receive the socket instance, and a signature
# mismatch would silently call the wrong parameter. The depth makes the guard
# re-entrant, so the real originals survive every nested install.
_SOCKET_ORIGINALS: dict[str, Any] = {}
_SOCKET_GUARD_DEPTH: int = 0


def _deny(target: Any) -> OutboundSocketDenied:
    return OutboundSocketDenied(
        f"outbound network is denied in the deterministic runtime (attempted connect to {target!r})"
    )


def _denied_connect(sock: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
    if sock.family not in SocketGuard._LOCAL_FAMILIES:
        raise _deny(address)
    return _SOCKET_ORIGINALS["connect"](sock, address, *args, **kwargs)


def _denied_connect_ex(sock: socket.socket, address: Any, *args: Any, **kwargs: Any) -> Any:
    if sock.family not in SocketGuard._LOCAL_FAMILIES:
        raise _deny(address)
    return _SOCKET_ORIGINALS["connect_ex"](sock, address, *args, **kwargs)


def _denied_sendto(sock: socket.socket, data: Any, *args: Any, **kwargs: Any) -> Any:
    if sock.family not in SocketGuard._LOCAL_FAMILIES:
        raise _deny(args[0] if args else kwargs.get("address"))
    return _SOCKET_ORIGINALS["sendto"](sock, data, *args, **kwargs)


def _denied_sendmsg(sock: socket.socket, *args: Any, **kwargs: Any) -> Any:
    if sock.family not in SocketGuard._LOCAL_FAMILIES:
        raise _deny(args[1] if len(args) > 1 else "sendmsg")
    return _SOCKET_ORIGINALS["sendmsg"](sock, *args, **kwargs)


def _denied_create_connection(address: Any, *args: Any, **kwargs: Any) -> Any:
    if isinstance(address, str):  # AF_UNIX path
        return _SOCKET_ORIGINALS["create_connection"](address, *args, **kwargs)
    raise _deny(address)


def deny_outbound_sockets() -> SocketGuard:
    """A :class:`SocketGuard` context manager (installs on ``__enter__``)."""
    return SocketGuard()


def install_socket_guard() -> SocketGuard:
    """Install outbound denial immediately and return the guard."""
    return SocketGuard().install()


# ---------------------------------------------------------------------------
# MCP capability surface
# ---------------------------------------------------------------------------


def _handler_registry(target: Any) -> Mapping[str, Any]:
    if isinstance(target, Mapping):
        return target
    for attribute in ("_request_handlers", "request_handlers", "_handlers"):
        registry = getattr(target, attribute, None)
        if isinstance(registry, Mapping):
            return registry
    raise CapabilityViolation("cannot verify the MCP surface: the server exposes no request-handler registry")


def _is_forbidden_mcp_method(method: str) -> bool:
    lowered = method.lower()
    for forbidden in FORBIDDEN_MCP_METHODS:
        family = forbidden.split("/")[0]
        if lowered == forbidden.lower() or lowered.startswith(family + "/"):
            return True
    return False


def forbidden_mcp_methods(server: Any) -> list[str]:
    """Forbidden MCP methods present in a server/handler registry, sorted."""
    return sorted(str(method) for method in _handler_registry(server) if _is_forbidden_mcp_method(str(method)))


def assert_no_sampling_handler(server: Any) -> None:
    """Fail closed when a server registers sampling, prompts, logging or completions."""
    registered = forbidden_mcp_methods(server)
    if registered:
        raise CapabilityViolation("MCP server registers forbidden handlers: " + ", ".join(registered))


def assert_mcp_capabilities(capabilities: Any) -> None:
    """Fail closed unless the advertised capability keys are exactly tools+resources."""
    if not isinstance(capabilities, Mapping):
        raise CapabilityViolation(f"MCP capabilities must be a mapping, got {type(capabilities).__name__}")
    keys = {str(key) for key in capabilities}
    if keys != ALLOWED_MCP_CAPABILITIES:
        extra = sorted(keys - ALLOWED_MCP_CAPABILITIES)
        missing = sorted(ALLOWED_MCP_CAPABILITIES - keys)
        raise CapabilityViolation(
            f"MCP capabilities must be exactly {sorted(ALLOWED_MCP_CAPABILITIES)}; extra={extra} missing={missing}"
        )


# Capability families the guard can observe, in the MCP SDK's own naming. A
# registered ``completion/complete`` handler advertises the ``completions``
# capability, so the method family is aliased.
_CAPABILITY_FAMILIES: tuple[str, ...] = (
    "tools",
    "resources",
    "prompts",
    "logging",
    "completions",
    "sampling",
    "elicitation",
)
_METHOD_FAMILY_ALIASES: Mapping[str, str] = {"completion": "completions"}

# Protocol-core methods that advertise **no** capability and are therefore
# neither allowed nor forbidden surface: ``ping``, the ``server/*`` family
# (the pinned SDK registers ``server/discover``) and ``notifications/*``.
# These are enumerated explicitly; any *other* unrecognised family is an exotic
# surface the guard cannot classify, and fails closed instead of being ignored.
_BENIGN_MCP_METHODS: frozenset[str] = frozenset({"ping"})
_BENIGN_MCP_METHOD_FAMILIES: frozenset[str] = frozenset({"server", "notifications"})


def _method_family(method: str) -> str | None:
    family = method.split("/")[0].strip().lower()
    family = _METHOD_FAMILY_ALIASES.get(family, family)
    return family if family in _CAPABILITY_FAMILIES else None


def observed_mcp_capabilities(server: Any) -> dict[str, dict[str, Any]]:
    """Capability families observed from a server's real request-handler registry.

    The families are derived from the handlers the server *actually* registers,
    never from :data:`ALLOWED_MCP_CAPABILITIES`, so a server that exposes
    sampling, prompts, logging or completions is caught instead of echoed back. A
    server with no verifiable handler registry is refused (fail closed).

    Every registered method must classify: an enumerated capability family, or
    an explicitly benign protocol-core method (``ping``, ``server/*``,
    ``notifications/*``). An exotic method family outside both sets is
    *rejected* with :class:`CapabilityViolation` — never silently ignored.
    """
    registry = _handler_registry(server)
    observed: dict[str, dict[str, Any]] = {}
    for raw_method in registry:
        method = str(raw_method)
        family = _method_family(method)
        if family is not None:
            observed.setdefault(family, {})
            continue
        lowered = method.strip().lower()
        if lowered in _BENIGN_MCP_METHODS or lowered.split("/")[0] in _BENIGN_MCP_METHOD_FAMILIES:
            continue
        raise CapabilityViolation(
            f"unverifiable MCP method {method!r}: its family is neither an enumerated capability "
            f"({sorted(_CAPABILITY_FAMILIES)}) nor a benign protocol method; refusing to guess"
        )
    return {family: observed[family] for family in sorted(observed)}


# ---------------------------------------------------------------------------
# Static shell-out scan (anti-CLI-scraping)
# ---------------------------------------------------------------------------

# Call targets that spawn a process. The product never spawns a process
# (feature-matrix row 36: no credential may ride an argv, and Graphify is used
# as a library, never scraped through its CLI), so *any* use in product sources
# is a violation — a non-literal argv cannot be statically verified and is
# refused rather than skipped.
_SHELL_OUT_CALLS: frozenset[str] = frozenset(
    {
        "os.system",
        "os.popen",
        "os.spawnl",
        "os.spawnle",
        "os.spawnlp",
        "os.spawnlpe",
        "os.spawnv",
        "os.spawnve",
        "os.spawnvp",
        "os.spawnvpe",
        "os.posix_spawn",
        "os.posix_spawnp",
        "os.execl",
        "os.execle",
        "os.execlp",
        "os.execlpe",
        "os.execv",
        "os.execve",
        "os.execvp",
        "os.execvpe",
        "subprocess.run",
        "subprocess.call",
        "subprocess.check_call",
        "subprocess.check_output",
        "subprocess.Popen",
        "subprocess.getoutput",
        "subprocess.getstatusoutput",
    }
)

# Dynamic-code evaluation is an unverifiable shell-out/import vector: a string
# built at runtime can hide ``os.system('graphify ...')`` from any literal scan.
_DYNAMIC_EVAL_CALLS: frozenset[str] = frozenset({"eval", "exec"})

_SCANNABLE_MODULES: frozenset[str] = frozenset({"os", "subprocess"})


def _call_target(func: ast.expr, aliases: Mapping[str, str]) -> str | None:
    """Resolve a call's dotted target, following import aliases and from-imports."""
    if isinstance(func, ast.Name):
        name = func.id
        if name in aliases:
            return aliases[name]
        return name if name in _DYNAMIC_EVAL_CALLS else None
    if isinstance(func, ast.Attribute):
        parts: list[str] = []
        current: ast.expr = func
        while isinstance(current, ast.Attribute):
            parts.append(current.attr)
            current = current.value
        if not isinstance(current, ast.Name):
            return None
        parts.append(current.id)
        dotted = ".".join(reversed(parts))
        base, _, rest = dotted.partition(".")
        if base in aliases:
            dotted = aliases[base] + ("." + rest if rest else "")
        if dotted in _SHELL_OUT_CALLS:
            return dotted
        tail = ".".join(dotted.split(".")[-2:])
        return tail if tail in _SHELL_OUT_CALLS else None
    return None


def cli_scraping_violations(source: str, filename: str = "<source>") -> list[str]:
    """Static scan: fail closed on every shell-out / dynamic-eval vector.

    Returns sorted ``"filename:lineno reason"`` violations. Detects
    ``os.system``/``os.popen``/``os.spawn*``/``os.exec*``/``os.posix_spawn*``,
    the full ``subprocess`` call surface (``run``/``call``/``check_call``/
    ``check_output``/``Popen``/``getoutput``/``getstatusoutput``) and the
    ``eval``/``exec`` builtins, following import aliases and from-imports so
    ``import subprocess as sp`` or ``from subprocess import run`` cannot hide a
    call. Non-literal argv is covered structurally: the product never spawns
    processes at all, so the call itself is the violation regardless of what
    its arguments look like.
    """
    tree = ast.parse(source, filename=filename)
    aliases: dict[str, str] = {}
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name in _SCANNABLE_MODULES:
                    aliases[alias.asname or alias.name] = alias.name
        elif isinstance(node, ast.ImportFrom) and node.module in _SCANNABLE_MODULES:
            for alias in node.names:
                aliases[alias.asname or alias.name] = f"{node.module}.{alias.name}"
    violations: list[str] = []
    for node in ast.walk(tree):
        if not isinstance(node, ast.Call):
            continue
        target = _call_target(node.func, aliases)
        if target is None or (target not in _SHELL_OUT_CALLS and target not in _DYNAMIC_EVAL_CALLS):
            continue
        if target in _DYNAMIC_EVAL_CALLS:
            violations.append(
                f"{filename}:{node.lineno} evaluates dynamic code via {target} "
                "(an unverifiable shell-out/import vector)"
            )
            continue
        literals = [
            argument.value
            for argument in node.args
            if isinstance(argument, ast.Constant) and isinstance(argument.value, str)
        ]
        if any("graphify" in literal for literal in literals):
            reason = f"shells out to the graphify CLI via {target}"
        elif literals:
            reason = f"spawns a process via {target} (no process spawn is allowed on the product surface)"
        else:
            reason = f"spawns a process via {target} with a non-literal argv that cannot be statically verified"
        violations.append(f"{filename}:{node.lineno} {reason}")
    return sorted(violations)


# ---------------------------------------------------------------------------
# Verification
# ---------------------------------------------------------------------------


def _probe_outbound_denied() -> bool:
    with SocketGuard():
        try:
            socket.create_connection(("192.0.2.1", 9), timeout=0.05)
        except OutboundSocketDenied:
            return True
        except OSError:
            return False
        return False


def _probe_imports_blocked() -> dict[str, list[str]]:
    """Non-vacuous import-guard probe; raises if a resolvable root is unblocked."""
    return import_block_evidence()


def verify_runtime(
    env: Mapping[str, str] | None = None,
    *,
    strict_env: bool = False,
    dirty_distributions: Mapping[str, str] | None = None,
    mcp_server: Any = None,
    mcp_capabilities: Mapping[str, Any] | None = None,
) -> dict[str, Any]:
    """Run every guard and return a JSON-serializable report, or raise.

    ``strict_env=True`` additionally refuses an ambient environment that carries
    provider credentials; the default records them as removed from the messenger
    surface instead, because the Hermes host process may legitimately hold keys
    the product never receives.

    MCP capability evidence is **required**: pass a live ``mcp_server`` (whose
    registered handlers are observed directly) or an ``mcp_capabilities``
    snapshot. With neither, verification fails closed rather than comparing the
    guard's own allowlist against itself.
    """
    assert_supported_python()

    installed = installed_distributions() if dirty_distributions is None else dict(dirty_distributions)
    assert_no_forbidden_distributions(installed)
    # The pinned wheel ships `graphify-mcp = graphify.serve:_main`; the entry-point
    # policy rejects it, and this boundary is reported explicitly below instead of
    # being silently dropped (ADR 0001 §3, feature matrix §5).
    assert_no_forbidden_entry_points(allow_pinned_boundary=True)
    pinned_entry_point_boundary = sorted(
        offender for offender in forbidden_entry_points() if offender in DORMANT_PINNED_ENTRY_POINTS
    )

    ambient = dict(os.environ if env is None else env)
    if strict_env:
        assert_no_provider_credentials(ambient)

    scrubbed = scrubbed_env(ambient)
    assert_no_provider_credentials(scrubbed)
    assert_no_model_asset_env(scrubbed)

    outbound_denied = _probe_outbound_denied()
    if not outbound_denied:
        raise OutboundSocketDenied("the outbound socket guard did not deny a probe connection")

    imports_blocked = _probe_imports_blocked()
    if not imports_blocked["actively_blocked"]:
        raise ForbiddenImportError(
            "the import-block probe is vacuous: no installed forbidden root was actively blocked"
        )

    if mcp_server is None and mcp_capabilities is None:
        raise CapabilityViolation(
            "the MCP capability surface cannot be verified: no server or capability snapshot was supplied"
        )
    if mcp_server is not None:
        assert_no_sampling_handler(mcp_server)
    if mcp_capabilities is not None:
        if not isinstance(mcp_capabilities, Mapping):
            raise CapabilityViolation(
                f"MCP capability snapshot must be a mapping, got {type(mcp_capabilities).__name__}"
            )
        observed_capabilities: Mapping[str, Any] = {str(key): value for key, value in mcp_capabilities.items()}
        capability_evidence = "capability_snapshot"
    else:
        observed_capabilities = observed_mcp_capabilities(mcp_server)
        capability_evidence = "server_registry"
    assert_mcp_capabilities(observed_capabilities)

    return {
        "status": "ok",
        "python": f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
        "python_range": SUPPORTED_PYTHON_RANGE,
        "interpreter": sys.version.split()[0],
        "graphify_pin": GRAPHIFY_PIN,
        "installed_distributions": len(installed),
        "forbidden_distributions": [],
        "forbidden_entry_points": blocking_forbidden_entry_points(),
        "pinned_entry_point_boundary": pinned_entry_point_boundary,
        "ambient_credentials_removed": list(removed_credential_names(ambient)),
        "scrubbed_credentials_removed": len(removed_credential_names(ambient)),
        "outbound_sockets_denied": outbound_denied,
        "forbidden_imports_blocked": True,
        "import_block_evidence": imports_blocked,
        "mcp_capabilities": sorted(observed_capabilities),
        "mcp_capability_evidence": capability_evidence,
        "forbidden_import_roots": sorted(FORBIDDEN_IMPORT_ROOTS),
    }

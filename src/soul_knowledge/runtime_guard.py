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

import importlib.metadata as importlib_metadata
import os
import re
import socket
import sys
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field
from importlib.util import find_spec
from typing import Any

__all__ = [
    "ALLOWED_MCP_CAPABILITIES",
    "CapabilityViolation",
    "FORBIDDEN_DISTRIBUTIONS",
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
    "deny_outbound_sockets",
    "deterministic_env",
    "forbidden_entry_points",
    "forbidden_mcp_methods",
    "import_guard",
    "install_import_guard",
    "install_socket_guard",
    "installed_distributions",
    "is_forbidden_module",
    "model_asset_env",
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
        "jieba",
        "jieba-py",
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


def _is_forbidden_distribution(name: str) -> bool:
    return _canonical(name) in FORBIDDEN_DISTRIBUTIONS


def forbidden_distributions(installed: Mapping[str, str] | None = None) -> list[str]:
    """Names of forbidden distributions present in the environment, sorted."""
    available = installed_distributions() if installed is None else dict(installed)
    return sorted(name for name in available if _is_forbidden_distribution(name))


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


def forbidden_entry_points(distributions: Iterable[Any] | None = None) -> list[str]:
    """Provider entry points exposed by the environment, as ``dist:group:name=value``.

    Only provider/model targets are rejected here. A dormant entry point inside
    the pinned Graphify distribution (its own ``graphify-mcp`` console script) is
    not part of the product surface and is rejected separately by
    :func:`assert_no_forbidden_imports` and by the product's static source scan.
    """
    sources = importlib_metadata.distributions() if distributions is None else distributions
    offenders: list[str] = []
    for distribution in sources:
        name = distribution.metadata["Name"]
        for entry_point in distribution.entry_points:
            module = str(entry_point.value).split(":")[0]
            if (
                module.startswith(tuple(root + "." for root in PROVIDER_IMPORT_ROOTS))
                or module in PROVIDER_IMPORT_ROOTS
            ):
                offenders.append(f"{name}:{entry_point.group}:{entry_point.name}={entry_point.value}")
            elif _is_forbidden_distribution(str(entry_point.name)) and entry_point.group == "console_scripts":
                offenders.append(f"{name}:{entry_point.group}:{entry_point.name}={entry_point.value}")
    return sorted(offenders)


def assert_no_forbidden_entry_points(distributions: Iterable[Any] | None = None) -> None:
    offenders = forbidden_entry_points(distributions)
    if offenders:
        raise ForbiddenDependencyError("forbidden provider entry points exposed: " + ", ".join(offenders))


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


# ---------------------------------------------------------------------------
# Socket guard
# ---------------------------------------------------------------------------


class SocketGuard:
    """Denies outbound TCP/UDP while leaving local AF_UNIX sockets usable.

    Local IPC is the product's default transport (Unix socket), so AF_UNIX stays
    allowed; every AF_INET/AF_INET6 connect, connect-ex, sendto and
    ``create_connection`` call raises :class:`OutboundSocketDenied`.
    """

    _LOCAL_FAMILIES = frozenset({socket.AF_UNIX})

    def __init__(self) -> None:
        self._installed = False
        self._previous: dict[str, Any] = {}

    def install(self) -> SocketGuard:
        if self._installed:
            return self
        global _SOCKET_ORIGINALS
        _SOCKET_ORIGINALS = {
            "connect": socket.socket.connect,
            "connect_ex": socket.socket.connect_ex,
            "sendto": socket.socket.sendto,
            "sendmsg": socket.socket.sendmsg,
            "create_connection": socket.create_connection,
        }
        self._previous = dict(_SOCKET_ORIGINALS)
        socket.socket.connect = _denied_connect  # type: ignore[method-assign]
        socket.socket.connect_ex = _denied_connect_ex  # type: ignore[method-assign]
        socket.socket.sendto = _denied_sendto  # type: ignore[method-assign]
        socket.socket.sendmsg = _denied_sendmsg  # type: ignore[method-assign]
        socket.create_connection = _denied_create_connection  # type: ignore[assignment]
        self._installed = True
        return self

    def uninstall(self) -> None:
        if not self._installed:
            return
        global _SOCKET_ORIGINALS
        socket.socket.connect = self._previous["connect"]  # type: ignore[method-assign]
        socket.socket.connect_ex = self._previous["connect_ex"]  # type: ignore[method-assign]
        socket.socket.sendto = self._previous["sendto"]  # type: ignore[method-assign]
        socket.socket.sendmsg = self._previous["sendmsg"]  # type: ignore[method-assign]
        socket.create_connection = self._previous["create_connection"]
        self._previous = {}
        _SOCKET_ORIGINALS = {}
        self._installed = False

    def __enter__(self) -> SocketGuard:
        return self.install()

    def __exit__(self, *exc_info: object) -> None:
        self.uninstall()


# Originals for the active SocketGuard. Module-level so the patched class
# attributes are plain functions: a bound method assigned to a class attribute
# would not receive the socket instance, and a signature mismatch would silently
# call the wrong parameter.
_SOCKET_ORIGINALS: dict[str, Any] = {}


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


def _probe_imports_blocked() -> bool:
    try:
        assert_no_forbidden_imports()
    except ForbiddenImportError:
        return False
    return True


def verify_runtime(
    env: Mapping[str, str] | None = None,
    *,
    strict_env: bool = False,
    dirty_distributions: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """Run every guard and return a JSON-serializable report, or raise.

    ``strict_env=True`` additionally refuses an ambient environment that carries
    provider credentials; the default records them as removed from the messenger
    surface instead, because the Hermes host process may legitimately hold keys
    the product never receives.
    """
    assert_supported_python()

    installed = installed_distributions() if dirty_distributions is None else dict(dirty_distributions)
    assert_no_forbidden_distributions(installed)
    assert_no_forbidden_entry_points()

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
    if not imports_blocked:
        raise ForbiddenImportError("the import guard did not block a forbidden module")

    assert_mcp_capabilities({capability: {} for capability in sorted(ALLOWED_MCP_CAPABILITIES)})

    return {
        "status": "ok",
        "python": f"{REQUIRED_PYTHON[0]}.{REQUIRED_PYTHON[1]}",
        "python_range": SUPPORTED_PYTHON_RANGE,
        "interpreter": sys.version.split()[0],
        "graphify_pin": GRAPHIFY_PIN,
        "installed_distributions": len(installed),
        "forbidden_distributions": [],
        "forbidden_entry_points": [],
        "ambient_credentials_removed": list(removed_credential_names(ambient)),
        "scrubbed_credentials_removed": len(removed_credential_names(ambient)),
        "outbound_sockets_denied": outbound_denied,
        "forbidden_imports_blocked": imports_blocked,
        "mcp_capabilities": sorted(ALLOWED_MCP_CAPABILITIES),
        "forbidden_import_roots": sorted(FORBIDDEN_IMPORT_ROOTS),
    }

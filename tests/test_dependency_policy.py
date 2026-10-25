"""Dependency policy for the deterministic runtime.

Task 1 of ``docs/superpowers/plans/2026-10-24-superpower-knowledge-soul.md``.

These tests pin the *packaging* contract of the standalone system:

* Python 3.14 only.
* Graphify pinned exactly to ``graphifyy[mcp]==0.9.95``.
* One ``uv.lock`` that pins the complete transitively reachable environment.
* No provider / vector / model package is reachable in that environment, is
  installed, or is exposed through an entry point.

They are written to fail closed: a missing lock, an unreadable lock entry, or an
unresolvable direct dependency is a failure, never a skip.
"""

from __future__ import annotations

import importlib.metadata as importlib_metadata
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
SRC = REPO_ROOT / "src"

PROJECT_NAME = "soul-knowledge"
GRAPHIFY_REQUIREMENT = "graphifyy[mcp]==0.9.95"
GRAPHIFY_VERSION = "0.9.95"
REQUIRES_PYTHON = ">=3.14,<3.15"
# uv normalizes the project's requires-python into a bare-series pin.
LOCKED_PYTHON = "==3.14.*"

# (package, extra) pairs the runtime is allowed to enable. Everything else that
# Graphify ships as an optional extra stays dormant and must not be reachable.
ENABLED_EXTRAS = (("graphifyy", "mcp"),)

# Provider, vector, model-runtime, outbound-sink and model-asset distributions.
# Any of these installed *or* transitively reachable from the enabled extras
# breaks the no-model boundary in spec section 4.2 / plan Global Constraints.
FORBIDDEN_DISTRIBUTION_PREFIXES = (
    "openai",
    "anthropic",
    "google-genai",
    "google-generativeai",
    "vertexai",
    "google-cloud-aiplatform",
    "boto3",
    "botocore",
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
    "cohere",
    "groq",
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
    "watchdog",
    "pypdf",
    "markdownify",
    "python-docx",
    "openpyxl",
    "pillow",
    "matplotlib",
    "robotframework",
)

# Nothing else may be reachable: the walk follows dependency edges and evaluates
# their environment markers, so Windows/emscripten-only packages are skipped
# exactly as uv skips them on this platform.


def _normalized(name: str) -> str:
    return canonicalize_name(name)


def _is_forbidden_distribution(name: str) -> bool:
    normalized = _normalized(name)
    return any(
        normalized == prefix or normalized.startswith(f"{prefix}-") or normalized.startswith(prefix)
        for prefix in FORBIDDEN_DISTRIBUTION_PREFIXES
    )


@pytest.fixture(scope="module")
def pyproject() -> dict:
    assert PYPROJECT.is_file(), "pyproject.toml must exist after Task 1"
    with PYPROJECT.open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def lock() -> dict:
    assert UV_LOCK.is_file(), "uv.lock must exist after Task 1"
    with UV_LOCK.open("rb") as handle:
        return tomllib.load(handle)


@pytest.fixture(scope="module")
def locked_packages(lock: dict) -> dict[str, dict]:
    packages: dict[str, dict] = {}
    assert isinstance(lock.get("package"), list), "uv.lock must contain [[package]] entries"
    for entry in lock["package"]:
        packages[_normalized(entry["name"])] = entry
    return packages


def _installed_versions() -> dict[str, str]:
    return {_normalized(dist.metadata["Name"]): dist.version for dist in importlib_metadata.distributions()}


def _reachable_packages(locked_packages: dict[str, dict]) -> set[str]:
    """Transitive closure of the enabled environment from the lock file.

    Walks dependency edges of every reached package plus the explicitly enabled
    extras, evaluating each edge's environment marker so Windows/emscripten-only
    packages are skipped exactly as uv skips them on this platform. Optional
    extras that are not enabled (Graphify's provider, vector, sink and
    model-asset extras) are deliberately not walked, so a dormant extra can never
    be reported as part of the enabled runtime surface.
    """
    seen: set[str] = set()
    pending: list[tuple[str, frozenset[str]]] = [(PROJECT_NAME, frozenset())]
    enabled: dict[str, frozenset[str]] = {}
    for package, extra in ENABLED_EXTRAS:
        enabled[package] = enabled.get(package, frozenset()) | {extra}

    while pending:
        name, extras = pending.pop()
        if name in seen:
            continue
        package = locked_packages.get(name)
        if package is None:
            continue
        seen.add(name)
        requests = list(package.get("dependencies") or [])
        for extra in extras | enabled.get(name, frozenset()):
            requests.extend((package.get("optional-dependencies") or {}).get(extra) or [])
        for request in requests:
            if not _marker_applies(request):
                continue
            pending.append((_normalized(request["name"]), frozenset(request.get("extra") or ())))
    return seen


def _marker_applies(request: dict) -> bool:
    """Evaluate a uv lock dependency edge marker in this environment."""
    marker = request.get("marker")
    if not marker:
        return True
    return bool(Marker(marker).evaluate())


# ---------------------------------------------------------------- Python 3.14


def test_running_python_is_314() -> None:
    assert sys.version_info[:2] == (3, 14), f"the system must run on Python 3.14, got {sys.version.split()[0]}"


def test_pyproject_requires_python_314(pyproject: dict) -> None:
    assert pyproject["project"]["requires-python"] == REQUIRES_PYTHON


def test_pyproject_pins_graphify_exactly(pyproject: dict) -> None:
    dependencies = pyproject["project"]["dependencies"]
    assert GRAPHIFY_REQUIREMENT in dependencies, dependencies
    parsed = [Requirement(item) for item in dependencies]
    graphify = [item for item in parsed if canonicalize_name(item.name) == "graphifyy"]
    assert len(graphify) == 1, "exactly one graphifyy requirement"
    requirement = graphify[0]
    assert requirement.extras == {"mcp"}, "the mcp extra is the only enabled Graphify extra"
    assert [str(spec) for spec in requirement.specifier] == ["==0.9.95"], str(requirement)


def test_pyproject_has_no_provider_dependency(pyproject: dict) -> None:
    for item in pyproject["project"]["dependencies"]:
        assert not _is_forbidden_distribution(Requirement(item).name), item


def test_pyproject_dev_group_is_declared(pyproject: dict) -> None:
    dev = pyproject.get("dependency-groups", {}).get("dev")
    assert dev, "a pinned dev dependency group is required for the task gates"
    names = {_normalized(Requirement(item).name) for item in dev}
    assert {"pytest", "hypothesis", "pyright", "ruff"} <= names, names


# ------------------------------------------------------------------ uv.lock


def test_lock_is_a_v1_uv_lock(lock: dict, locked_packages: dict[str, dict]) -> None:
    assert lock.get("version") == 1
    assert lock.get("requires-python") == LOCKED_PYTHON
    assert PROJECT_NAME in locked_packages
    assert locked_packages[PROJECT_NAME]["version"] == "0.1.0"


def test_lock_pins_graphify_with_hash(locked_packages: dict[str, dict]) -> None:
    entry = locked_packages["graphifyy"]
    assert entry["version"] == GRAPHIFY_VERSION
    hashes = [artifact.get("hash", "") for artifact in entry.get("wheels") or []]
    hashes += [entry.get("sdist", {}).get("hash", "")]
    assert any(value.startswith("sha256:") for value in hashes), "the Graphify pin must be artifact-hashed"


def test_every_locked_package_is_version_pinned(locked_packages: dict[str, dict]) -> None:
    for name, entry in locked_packages.items():
        assert entry.get("version"), f"{name} has no locked version"
        Version(entry["version"])


def test_enabled_environment_contains_no_forbidden_distribution(locked_packages: dict[str, dict]) -> None:
    reachable = _reachable_packages(locked_packages)
    assert "graphifyy" in reachable and "mcp" in reachable, "the walk must reach the real environment"
    offenders = sorted(name for name in reachable if _is_forbidden_distribution(name))
    assert offenders == [], f"forbidden distributions are reachable: {offenders}"


def test_dormant_graphify_extras_are_not_reachable(locked_packages: dict[str, dict]) -> None:
    """Graphify ships provider/vector/sink extras; they stay out of the runtime."""
    reachable = _reachable_packages(locked_packages)
    for package in ("openai", "tiktoken", "anthropic", "boto3", "graspologic-native", "neo4j"):
        assert package not in reachable, package


def test_lock_holds_no_forbidden_distribution_at_all(locked_packages: dict[str, dict]) -> None:
    """The lock resolves only the enabled extras, so even dormant extras are absent."""
    offenders = sorted(name for name in locked_packages if _is_forbidden_distribution(name))
    assert offenders == [], offenders


def test_locked_extras_are_only_the_enabled_ones(locked_packages: dict[str, dict]) -> None:
    graphify_extras = set(locked_packages["graphifyy"].get("optional-dependencies") or {})
    assert graphify_extras == {"mcp"}, graphify_extras


def test_reachable_packages_match_the_installed_environment(locked_packages: dict[str, dict]) -> None:
    installed = _installed_versions()
    reachable = _reachable_packages(locked_packages)
    missing, mismatched = [], []
    for name in sorted(reachable):
        locked_version = locked_packages[name]["version"]
        installed_version = installed.get(name)
        if installed_version is None:
            missing.append(name)
            continue
        if Version(installed_version) != Version(locked_version):
            mismatched.append(f"{name}: locked {locked_version} != installed {installed_version}")
    assert missing == [], f"packages required by the lock are not installed: {missing}"
    assert mismatched == [], mismatched


def test_every_installed_distribution_is_pinned_by_the_lock(locked_packages: dict[str, dict]) -> None:
    installed = _installed_versions()
    unlocked = sorted(name for name in installed if name not in locked_packages)
    assert unlocked == [], f"installed but not locked: {unlocked}"


def test_installed_environment_holds_no_forbidden_distribution() -> None:
    offenders = sorted(name for name in _installed_versions() if _is_forbidden_distribution(name))
    assert offenders == [], f"forbidden distributions installed: {offenders}"


def test_direct_requirements_are_locked_off(locked_packages: dict[str, dict], pyproject: dict) -> None:
    """Every direct requirement resolves to exactly one locked, installed version."""
    direct = list(pyproject["project"]["dependencies"]) + list(pyproject.get("dependency-groups", {}).get("dev", []))
    installed = _installed_versions()
    for item in direct:
        requirement = Requirement(item)
        name = _normalized(requirement.name)
        assert name in locked_packages, f"{item} is not in uv.lock"
        locked_version = locked_packages[name]["version"]
        assert requirement.specifier.contains(Version(locked_version), prereleases=True), (
            f"{item} does not accept the locked version {locked_version}"
        )
        assert installed.get(name) == locked_version, f"{name} is not installed at its locked version"


# ------------------------------------------------------------ entry points


def test_no_entry_point_points_at_a_forbidden_module() -> None:
    offenders = []
    for dist in importlib_metadata.distributions():
        for entry_point in dist.entry_points:
            value = str(entry_point.value)
            module = value.split(":")[0].split(".")[0]
            if _is_forbidden_distribution(module) or _is_forbidden_distribution(entry_point.name):
                offenders.append(f"{dist.metadata['Name']}:{entry_point.group}:{entry_point.name}={value}")
    assert offenders == [], offenders


def test_project_entry_points_are_declared_and_deterministic(pyproject: dict) -> None:
    scripts = pyproject["project"].get("scripts", {})
    assert scripts == {"soul-knowledge": "soul_knowledge.cli:main"}, scripts
    assert pyproject["project"]["name"] == PROJECT_NAME


def test_graphify_console_scripts_are_the_only_graphify_commands() -> None:
    distribution = importlib_metadata.distribution("graphifyy")
    names = sorted(
        entry_point.name for entry_point in distribution.entry_points if entry_point.group == "console_scripts"
    )
    assert names == ["graphify", "graphify-mcp"], names

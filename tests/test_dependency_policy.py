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
import re
import sys
import tomllib
from pathlib import Path

import pytest
from packaging.markers import Marker
from packaging.requirements import Requirement
from packaging.utils import canonicalize_name
from packaging.version import Version

from know_your_soul import runtime_guard

REPO_ROOT = Path(__file__).resolve().parents[1]
PYPROJECT = REPO_ROOT / "pyproject.toml"
UV_LOCK = REPO_ROOT / "uv.lock"
SRC = REPO_ROOT / "src"

PROJECT_NAME = "know-your-soul"
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
#
# There is exactly one source of truth for this list:
# ``know_your_soul.runtime_guard.FORBIDDEN_DISTRIBUTIONS`` plus its
# ``is_forbidden_distribution`` matcher (exact name or prefix variant). This test
# imports that shared rule instead of restating it, so the packaging policy and
# the runtime guard cannot drift apart.

# Nothing else may be reachable: the walk follows dependency edges and evaluates
# their environment markers, so Windows/emscripten-only packages are skipped
# exactly as uv skips them on this platform.


def _normalized(name: str) -> str:
    return canonicalize_name(name)


def _is_forbidden_distribution(name: str) -> bool:
    return runtime_guard.is_forbidden_distribution(name)


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
    # The fixpoint walk (item 7) follows edge-requested extras the legacy walk
    # silently dropped, so this check cannot miss a package hidden behind one.
    reachable, _ = _reachable_packages_with_extras(locked_packages)
    assert "graphifyy" in reachable and "mcp" in reachable, "the walk must reach the real environment"
    offenders = sorted(name for name in reachable if _is_forbidden_distribution(name))
    assert offenders == [], f"forbidden distributions are reachable: {offenders}"


def test_dormant_graphify_extras_are_not_reachable(locked_packages: dict[str, dict]) -> None:
    """Graphify ships provider/vector/sink extras; they stay out of the runtime."""
    reachable, _ = _reachable_packages_with_extras(locked_packages)
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
    # The only entry point the guard policy rejects is the recorded pinned-wheel
    # boundary (graphifyy's dormant `graphify-mcp`), asserted next.
    assert runtime_guard.blocking_forbidden_entry_points() == []


def test_forbidden_distribution_policy_has_one_shared_source_of_truth() -> None:
    """The packaging test and the runtime guard must not keep separate lists."""
    declared = runtime_guard.FORBIDDEN_DISTRIBUTIONS
    assert declared, "the shared forbidden-distribution set must not be empty"
    for name in declared:
        assert _is_forbidden_distribution(name), name
    # The matcher catches boundary variants the exact list cannot enumerate.
    assert _is_forbidden_distribution("torchaudio")
    assert _is_forbidden_distribution("openai")
    assert not _is_forbidden_distribution("graphifyy")
    assert not _is_forbidden_distribution("networkx")


def test_forbidden_distribution_matching_is_boundary_aware() -> None:
    """Item 9: exact or separator-bounded matches only — no raw prefix bleed.

    The old matcher used ``startswith(entry)``, so a benign distribution whose
    name merely *begins* with a forbidden one (``torches``, ``ollamarama``)
    would be flagged (false positive), while variants separated by a digit
    (``psycopg2``) or a suffix without separator needed luck to be caught.
    Matching is now exact or ``entry + "-"`` bounded: no false positives, and
    every real variant stays caught (no false negatives).
    """
    for name in (
        "openai",
        "torch",
        "torchaudio",
        "torchvision",
        "torch-cuda",
        "openai-whisper",
        "langchain",
        "langchain-core",
        "langchain-community",
        "psycopg",
        "psycopg2",
        "psycopg2-binary",
        "faiss-cpu",
        "faiss-gpu",
        "sentence-transformers",
        "faster-whisper",
        "yt-dlp",
    ):
        assert _is_forbidden_distribution(name), f"false negative: {name}"
    for name in ("torches", "torchweed", "ollamarama", "groceries", "graphifyy", "networkx", "mcp"):
        assert not _is_forbidden_distribution(name), f"false positive: {name}"


# ------------------------------------------------- extras tripwire (item 7)

# Every (package, extra) the lock walk is allowed to follow. ENABLED_EXTRAS is
# the packaging contract; pyjwt[crypto] is an edge-requested extra pulled in by
# the pinned MCP SDK's auth surface and is recorded here deliberately — any
# *other* extra appearing in the walk fails this test and forces a review.
APPROVED_WALKED_EXTRAS = ENABLED_EXTRAS + (("pyjwt", "crypto"),)


def _reachable_packages_with_extras(locked_packages: dict[str, dict]) -> tuple[set[str], set[tuple[str, str]]]:
    """Fixpoint walk: the legacy walk plus the extras actually followed.

    Two deliberate strengthenings over ``_reachable_packages``:

    * a package is **revisited** when an edge later requests a new extra of it
      (the legacy walk marks it seen by name and silently drops the extra —
      that was the tripwire weakness);
    * every (package, extra) pair followed — from ENABLED_EXTRAS or from any
      dependency edge — is recorded, so the tripwire test can compare the walk
      against the approved set.
    """
    seen_extras: dict[str, set[str]] = {}
    followed: set[tuple[str, str]] = set()
    pending: list[tuple[str, frozenset[str]]] = [(PROJECT_NAME, frozenset())]
    enabled: dict[str, frozenset[str]] = {}
    for package, extra in ENABLED_EXTRAS:
        enabled[package] = enabled.get(package, frozenset()) | {extra}

    while pending:
        name, extras = pending.pop()
        package = locked_packages.get(name)
        if package is None:
            continue
        effective = set(extras) | set(enabled.get(name, frozenset()))
        followed.update((name, extra) for extra in effective)
        new_extras = effective - seen_extras.get(name, set())
        if name in seen_extras and not new_extras:
            continue
        seen_extras.setdefault(name, set()).update(effective)
        requests = list(package.get("dependencies") or [])
        for extra in sorted(effective):
            requests.extend((package.get("optional-dependencies") or {}).get(extra) or [])
        for request in requests:
            if not _marker_applies(request):
                continue
            pending.append((_normalized(request["name"]), frozenset(request.get("extra") or ())))
    return set(seen_extras), followed


def test_reachability_walk_follows_only_approved_extras(locked_packages: dict[str, dict]) -> None:
    """Tripwire: any extra edge beyond the approved set is a hard failure."""
    reachable, followed = _reachable_packages_with_extras(locked_packages)
    assert "graphifyy" in reachable, "the walk must reach the real environment"
    assert followed <= set(APPROVED_WALKED_EXTRAS), f"unapproved extras followed by the lock walk: {followed}"


def test_extras_tripwire_fires_on_an_undeclared_extra_edge() -> None:
    """The legacy walk drops edge-requested extras for already-seen packages;
    the strengthened walk exposes them and the tripwire fires."""
    synthetic = {
        # graphifyy is visited (and marked seen) before the mcp edge requests
        # its "openai" extra, so the legacy seen-by-name walk drops the extra.
        PROJECT_NAME: {"version": "0.1.0", "dependencies": [{"name": "graphifyy"}]},
        "graphifyy": {
            "version": GRAPHIFY_VERSION,
            "dependencies": [{"name": "mcp"}],
            "optional-dependencies": {
                "mcp": [{"name": "mcp"}],
                "openai": [{"name": "openai"}],
            },
        },
        "mcp": {"version": "2.3.0", "dependencies": [{"name": "graphifyy", "extra": ["openai"]}]},
        "openai": {"version": "2.0.0", "dependencies": []},
    }
    # Legacy walk: graphifyy is already seen when the mcp edge requests its
    # "openai" extra, so the provider package is never reached — the hole.
    legacy_reachable = _reachable_packages(synthetic)
    assert "openai" not in legacy_reachable, "the legacy walk must still hide the edge"
    # Fixpoint walk: the extra edge is followed and the tripwire sees it.
    reachable, followed = _reachable_packages_with_extras(synthetic)
    assert "openai" in reachable, "the fixpoint walk must expose the extra edge"
    assert ("graphifyy", "openai") in followed
    assert not followed <= set(APPROVED_WALKED_EXTRAS)


def test_enabled_extras_match_pyproject_exactly(pyproject: dict) -> None:
    """ENABLED_EXTRAS is the review checkpoint; pyproject must not drift ahead of it."""
    declared = set()
    for item in pyproject["project"]["dependencies"]:
        requirement = Requirement(item)
        for extra in requirement.extras:
            declared.add((_normalized(requirement.name), extra))
    assert declared == set(ENABLED_EXTRAS), declared


# ----------------------------------- feature-matrix consistency (items 5 and 6)

MATRIX = REPO_ROOT / "docs" / "research" / "graphify-feature-matrix.md"


def _matrix_rows() -> dict[int, list[str]]:
    rows: dict[int, list[str]] = {}
    for line in MATRIX.read_text(encoding="utf-8").splitlines():
        match = re.match(r"\|\s*(\d+)\s*\|", line)
        if match:
            number = int(match.group(1))
            assert number not in rows, f"matrix row {number} appears twice"
            rows[number] = [cell.strip() for cell in line.strip().strip("|").split("|")]
    return rows


def test_feature_matrix_denominator_is_exactly_40_and_machine_checkable() -> None:
    """Item 6: the denominator is the 40 numbered rows — nothing more, nothing less."""
    rows = _matrix_rows()
    assert sorted(rows) == list(range(1, 41)), "the denominator is exactly the 40 numbered rows"
    for number, cells in rows.items():
        assert len(cells) == 7, f"row {number} must carry all seven cells"
        assert "Task" in cells[4], f"row {number} has no owning task"
        assert "::" in cells[5], f"row {number} has no named contract test"
    # The §4 exclusion table must not contain numbered rows: excluded surfaces
    # leave the denominator exactly once, never double-counted against a row.
    text = MATRIX.read_text(encoding="utf-8")
    exclusions = text.split("## 4.")[1].split("## 5.")[0]
    assert not re.search(r"\|\s*\d+\s*\|", exclusions), "a numbered row leaked into the exclusions table"


def test_matrix_rows_1_and_36_resolve_the_denominator_tension() -> None:
    """Rows 1/36 embed §4 sub-surface exclusions; the row text must say how the
    denominator treats that (row stays in the 40; excluded sub-surface counted once)."""
    rows = _matrix_rows()
    for number in (1, 36):
        cell_text = " | ".join(rows[number]).lower()
        assert "denominator" in cell_text, f"row {number} does not resolve its denominator tension"


def test_matrix_task8_coownership_is_annotated_on_row_36_not_only_in_prose() -> None:
    """Item 5: Task 8 co-ownership must be visible on the row, not only in prose."""
    rows = _matrix_rows()
    owner_cell = rows[36][4]
    assert "Task 9" in owner_cell and "Task 8" in owner_cell, owner_cell
    # The prose claim and the annotated row must not drift apart.
    text = MATRIX.read_text(encoding="utf-8")
    assert "Task 8 co-owns" in text, "the prose co-ownership statement must remain"


def test_project_entry_points_are_declared_and_deterministic(pyproject: dict) -> None:
    scripts = pyproject["project"].get("scripts", {})
    assert scripts == {"know-your-soul": "know_your_soul.cli:main"}, scripts
    assert pyproject["project"]["name"] == PROJECT_NAME


def test_graphify_mcp_console_script_is_rejected_by_the_entry_point_policy() -> None:
    """Policy now fails closed on the pinned wheel's dormant `graphify-mcp` script.

    Upstream ships `graphify-mcp = graphify.serve:_main`; it is not on the product
    surface, but the policy rejects it rather than excluding it. See ADR 0001 §3
    and `docs/research/graphify-feature-matrix.md` §5 for the recorded boundary.
    """
    distribution = importlib_metadata.distribution("graphifyy")
    names = sorted(
        entry_point.name for entry_point in distribution.entry_points if entry_point.group == "console_scripts"
    )
    assert names == ["graphify", "graphify-mcp"], names
    offenders = runtime_guard.forbidden_entry_points([distribution])
    assert offenders == ["graphifyy:console_scripts:graphify-mcp=graphify.serve:_main"], offenders
    with pytest.raises(runtime_guard.ForbiddenDependencyError):
        runtime_guard.assert_no_forbidden_entry_points([distribution])
    assert offenders[0] in runtime_guard.DORMANT_PINNED_ENTRY_POINTS
    assert runtime_guard.blocking_forbidden_entry_points([distribution]) == []

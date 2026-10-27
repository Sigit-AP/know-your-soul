"""Parser-category extension of the runtime guard (contract v3 addendum).

The ingestion addendum (``docs/contracts/ingestion-contract-v3.md``) sanctions
exactly three parser-category distributions — ``tree-sitter``,
``tree-sitter-language-pack`` (MIT) and ``pypdf`` (BSD-3-Clause) — for
deterministic, model-free extraction. These tests prove two directions at
once:

* the guard **admits** the parser category (allowlist present, disjoint from
  the forbidden surface, reflected in the ``verify_runtime`` report); and
* the guard **stays fail-closed** everywhere else — provider, model, embedding
  and vector distributions/imports are rejected exactly as before the
  extension. The allowlist is an addition, never a relaxation.
"""

from __future__ import annotations

from importlib.metadata import EntryPoint

import pytest

from know_your_soul import runtime_guard

PARSER_DISTRIBUTIONS = ("pypdf", "tree-sitter", "tree-sitter-language-pack")
PARSER_IMPORT_ROOTS = ("pypdf", "tree_sitter", "tree_sitter_language_pack")

PROVIDER_MODEL_VECTOR_DISTRIBUTIONS = (
    "openai",
    "anthropic",
    "boto3",
    "cohere",
    "groq",
    "mistralai",
    "together",
    "voyageai",
    "ollama",
    "litellm",
    "vllm",
    "torch",
    "torchaudio",
    "tensorflow",
    "onnxruntime",
    "llama-cpp-python",
    "tiktoken",
    "sentence-transformers",
    "transformers",
    "faiss-cpu",
    "chromadb",
    "pinecone-client",
    "weaviate-client",
    "qdrant-client",
    "lancedb",
    "langchain",
    "faster-whisper",
    "graspologic",
    "neo4j",
    "psycopg",
    "watchdog",
)

PROVIDER_MODEL_VECTOR_IMPORT_ROOTS = (
    "openai",
    "anthropic",
    "torch",
    "transformers",
    "sentence_transformers",
    "tiktoken",
    "onnxruntime",
    "pinecone",
    "qdrant_client",
    "weaviate",
    "chromadb",
    "faiss",
    "lancedb",
    "graphify.llm",
    "graphify.serve",
    "graphify.ingest",
)


# ------------------------------------------------------- allowlist contents


def test_parser_allowlist_contents_are_exact() -> None:
    assert runtime_guard.ALLOWED_PARSER_DISTRIBUTIONS == frozenset(PARSER_DISTRIBUTIONS)


def test_parser_import_roots_contents_are_exact() -> None:
    assert runtime_guard.ALLOWED_PARSER_IMPORT_ROOTS == frozenset(PARSER_IMPORT_ROOTS)


@pytest.mark.parametrize(
    "name",
    [
        "ALLOWED_PARSER_DISTRIBUTIONS",
        "ALLOWED_PARSER_IMPORT_ROOTS",
        "assert_parser_category_disjoint",
        "is_allowed_parser_distribution",
        "parser_category_violations",
    ],
)
def test_new_surface_is_exported(name: str) -> None:
    assert name in runtime_guard.__all__, name
    assert hasattr(runtime_guard, name), name


# ------------------------------------------------------------ disjointness


@pytest.mark.parametrize("name", PARSER_DISTRIBUTIONS)
def test_parser_distributions_are_not_forbidden(name: str) -> None:
    assert not runtime_guard.is_forbidden_distribution(name), name


@pytest.mark.parametrize("root", PARSER_IMPORT_ROOTS)
def test_parser_import_roots_are_not_forbidden_modules(root: str) -> None:
    assert not runtime_guard.is_forbidden_module(root), root


def test_pypdf_left_the_forbidden_distribution_set() -> None:
    assert "pypdf" not in runtime_guard.FORBIDDEN_DISTRIBUTIONS


def test_allowlist_and_forbidden_sets_are_disjoint() -> None:
    assert not (runtime_guard.ALLOWED_PARSER_DISTRIBUTIONS & runtime_guard.FORBIDDEN_DISTRIBUTIONS)


def test_parser_category_violations_is_empty_on_a_clean_guard() -> None:
    assert runtime_guard.parser_category_violations() == []


def test_assert_parser_category_disjoint_passes_on_a_clean_guard() -> None:
    runtime_guard.assert_parser_category_disjoint()


def test_parser_category_violations_detects_a_collision() -> None:
    """Fail-closed proof: if a parser name ever lands in the forbidden set, the
    invariant probe must report it instead of silently disagreeing."""
    original = runtime_guard.FORBIDDEN_DISTRIBUTIONS
    try:
        runtime_guard.FORBIDDEN_DISTRIBUTIONS = original | {"pypdf"}
        violations = runtime_guard.parser_category_violations()
        assert any("pypdf" in violation for violation in violations), violations
        with pytest.raises(runtime_guard.ForbiddenDependencyError):
            runtime_guard.assert_parser_category_disjoint()
    finally:
        runtime_guard.FORBIDDEN_DISTRIBUTIONS = original


# ------------------------------------------------------ matcher behaviour


@pytest.mark.parametrize(
    "name",
    [
        "pypdf",
        "PyPDF",
        "tree-sitter",
        "tree_sitter",
        "tree-sitter-language-pack",
        "tree_sitter_language_pack",
    ],
)
def test_parser_matcher_accepts_canonical_variants(name: str) -> None:
    assert runtime_guard.is_allowed_parser_distribution(name), name


@pytest.mark.parametrize(
    "name",
    [
        "pypdf2",
        "pypdfium2",
        "tree-sitter-cli",
        "tree-sitter-language-pack-extra",
        "openai",
        "numpy",
        "graphifyy",
        "networkx",
    ],
)
def test_parser_matcher_is_exact_not_prefix(name: str) -> None:
    """The allowlist matches exact names only: a lookalike or a suffix variant
    is *not* sanctioned (mirrors the forbidden matcher's no-bleed rule)."""
    assert not runtime_guard.is_allowed_parser_distribution(name), name


# ------------------------------------------- fail-closed surface is intact


@pytest.mark.parametrize("name", PROVIDER_MODEL_VECTOR_DISTRIBUTIONS)
def test_provider_model_vector_distributions_stay_forbidden(name: str) -> None:
    assert runtime_guard.is_forbidden_distribution(name), name


@pytest.mark.parametrize("root", PROVIDER_MODEL_VECTOR_IMPORT_ROOTS)
def test_provider_model_vector_imports_stay_forbidden(root: str) -> None:
    assert runtime_guard.is_forbidden_module(root), root


def test_provider_import_roots_are_still_a_subset_of_the_forbidden_roots() -> None:
    assert runtime_guard.PROVIDER_IMPORT_ROOTS <= runtime_guard.FORBIDDEN_IMPORT_ROOTS


def test_graphify_rejected_roots_are_unchanged() -> None:
    assert runtime_guard.GRAPHIFY_REJECTED_IMPORT_ROOTS <= runtime_guard.FORBIDDEN_IMPORT_ROOTS
    assert "graphify.llm" in runtime_guard.FORBIDDEN_IMPORT_ROOTS


def test_synthetic_parser_only_environment_is_accepted() -> None:
    installed = {"pypdf": "6.21.3", "tree-sitter": "0.25.2", "tree-sitter-language-pack": "1.22.1"}
    assert runtime_guard.forbidden_distributions(installed) == []
    runtime_guard.assert_no_forbidden_distributions(installed)


def test_synthetic_mixed_environment_still_raises() -> None:
    installed = {"pypdf": "6.21.3", "openai": "2.0.0"}
    assert runtime_guard.forbidden_distributions(installed) == ["openai"]
    with pytest.raises(runtime_guard.ForbiddenDependencyError, match="openai"):
        runtime_guard.assert_no_forbidden_distributions(installed)


def test_numpy_named_vector_lookalikes_stay_forbidden() -> None:
    """Embedding/vector stores named in the task brief stay rejected."""
    for name in ("faiss-cpu", "faiss-gpu", "qdrant-client", "lancedb", "chromadb"):
        with pytest.raises(runtime_guard.ForbiddenDependencyError):
            runtime_guard.assert_no_forbidden_distributions({name: "1.0.0"})


# --------------------------------------------------------------- entry points


def _fake_distribution(name: str, entry_points: list[EntryPoint]):
    class _Metadata:
        def __getitem__(self, key: str) -> str:
            assert key == "Name"
            return name

    class _Distribution:
        metadata = _Metadata()

        @property
        def entry_points(self) -> list[EntryPoint]:
            return entry_points

    return _Distribution()


def test_parser_console_scripts_are_not_entry_point_offenders() -> None:
    distribution = _fake_distribution(
        "pypdf",
        [EntryPoint("pypdf-tool", "pypdf:main", "console_scripts")],
    )
    assert runtime_guard.forbidden_entry_points([distribution]) == []


def test_provider_console_scripts_stay_entry_point_offenders() -> None:
    distribution = _fake_distribution(
        "openai",
        [EntryPoint("openai", "openai.cli:main", "console_scripts")],
    )
    offenders = runtime_guard.forbidden_entry_points([distribution])
    assert offenders == ["openai:console_scripts:openai=openai.cli:main"], offenders


# ------------------------------------------------------------ verify_runtime


def test_verify_runtime_reports_the_parser_allowlist() -> None:
    report = runtime_guard.verify_runtime(mcp_capabilities={"tools": {}, "resources": {}})
    assert report["allowed_parser_distributions"] == sorted(PARSER_DISTRIBUTIONS)


def test_verify_runtime_accepts_parser_distributions() -> None:
    report = runtime_guard.verify_runtime(
        dirty_distributions={"pypdf": "6.21.3", "tree-sitter-language-pack": "1.22.1"},
        mcp_capabilities={"tools": {}, "resources": {}},
    )
    assert report["status"] == "ok"
    assert report["forbidden_distributions"] == []


def test_verify_runtime_still_rejects_a_provider_mix() -> None:
    with pytest.raises(runtime_guard.ForbiddenDependencyError):
        runtime_guard.verify_runtime(
            dirty_distributions={"pypdf": "6.21.3", "openai": "2.0.0"},
            mcp_capabilities={"tools": {}, "resources": {}},
        )

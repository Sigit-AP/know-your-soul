"""Shape tests for the v3 ingestion contract addendum.

The addendum (``docs/contracts/ingestion-contract-v3.md``) is frozen upon
publication: these tests pin its structure — the two ingestion verbs, the
typed-error vocabulary, the `bridges` edge kind, the primary-node rules and
the adapter mapping — and prove the v1/v2 contracts in the same directory stay
byte-identical via pinned sha256 digests.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "docs" / "contracts"
V3 = CONTRACTS / "ingestion-contract-v3.md"

# Pinned digests of the frozen contracts (v1 + v2). A single byte of drift in
# any of these files fails the pin — the addendum mechanism exists precisely
# so these documents never change.
FROZEN_CONTRACTS: dict[str, str] = {
    "access-policy.md": "68cdbaf88ef5e85dfc868bb35452c8f4b431b577fd69ff3bd3eac422e654d24e",
    "recommendation-contract.md": "811800cad1233ce14031f6a1e54220477bbda097aeea5e3e8d5b8827b2b1fe82",
    "relearn-contract.md": "24b6223eb4be813a9a4809147dce8dc30af17717ca3137727cae7194e58e6579",
    "retrieval-verbs-v2.md": "51541fa68e029a4b9c2130188e862cd11a5e27b31db30e596b378718af76d6da",
}


@pytest.fixture(scope="module")
def text() -> str:
    assert V3.is_file(), "the v3 ingestion addendum must exist"
    # Whitespace-normalized: prose wraps at the margin, so phrase pins match
    # against a single-space-folded rendering.
    return re.sub(r"\s+", " ", V3.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------- envelope


def test_addendum_file_exists() -> None:
    assert V3.is_file(), "docs/contracts/ingestion-contract-v3.md is missing"


def test_contract_version_header(text: str) -> None:
    assert "Contract version: 3.0.0 (addendum)" in text


def test_status_is_active_and_dated(text: str) -> None:
    assert "Status: ACTIVE (2026-10-27)" in text


def test_authority_references_the_master_plan(text: str) -> None:
    assert ".superpowers/sdd/2026-10-27-universal-knowledge-graph/MASTER-PLAN.md" in text


def test_declares_v1_v2_unchanged(text: str) -> None:
    assert "unchanged" in text
    assert "byte-identical" in text


def test_frozen_upon_publication(text: str) -> None:
    assert "frozen upon publication" in text


def test_changes_require_a_new_contract_version(text: str) -> None:
    assert "requires a new contract version and operator approval" in text


# ------------------------------------------------------------------- verbs


def test_defines_ingest_file_verb(text: str) -> None:
    assert "## `knowledge.ingest_file(path, token_budget=1024)`" in text


def test_defines_ingest_directory_verb(text: str) -> None:
    assert "## `knowledge.ingest_directory(path, recursive=true, max_files=1024, token_budget=1024)`" in text


def test_proposals_never_mutate_the_graph(text: str) -> None:
    assert "never mutates" in text
    assert "proposal" in text


def test_apply_only_via_the_operator_gated_path(text: str) -> None:
    assert "operator-gated" in text
    assert "propose/apply" in text


def test_audit_event_names(text: str) -> None:
    assert "knowledge.ingest_file" in text
    assert "knowledge.ingest_directory" in text


def test_audit_records_hashes_not_content(text: str) -> None:
    assert "sha256" in text
    assert "never the input content" in text


def test_determinism_guarantee(text: str) -> None:
    assert "byte-identical proposal" in text
    assert "deterministic" in text


def test_parser_allowlist_is_named(text: str) -> None:
    for name in ("tree-sitter", "tree-sitter-language-pack", "pypdf"):
        assert name in text, name


def test_no_model_boundary_stays_fail_closed(text: str) -> None:
    assert "fail-closed" in text
    for category in ("provider", "model", "embedding", "vector"):
        assert category in text, category


# ------------------------------------------------------------- typed errors


@pytest.mark.parametrize("error", ["PATH_OUTSIDE_ROOT", "PATH_NOT_FOUND", "PATH_NOT_READABLE"])
def test_typed_path_errors(text: str, error: str) -> None:
    assert error in text


@pytest.mark.parametrize("error", ["UNSUPPORTED_MEDIA_TYPE", "FILE_TOO_LARGE"])
def test_typed_input_errors(text: str, error: str) -> None:
    assert error in text


@pytest.mark.parametrize("error", ["BUDGET_TOO_SMALL", "BUDGET_EXCEEDS_MAX", "METADATA_EXCEEDS_BUDGET"])
def test_frozen_budget_errors(text: str, error: str) -> None:
    assert error in text


def test_budget_floor_matches_the_frozen_semantics(text: str) -> None:
    assert "below 128" in text


# ------------------------------------------------------------- ingest_file


def test_node_id_format_is_the_stable_codegraph_form(text: str) -> None:
    assert "kind:sha256(path:kind:name:line)[:col]" in text


def test_unresolved_refs_are_pending_and_never_guessed(text: str) -> None:
    assert "unresolved_refs" in text
    assert '"pending"' in text
    assert "never guessed" in text


def test_extraction_version_is_versioned_independently(text: str) -> None:
    assert "extraction_version" in text


def test_ingest_file_payload_is_atomic(text: str) -> None:
    assert "atomic" in text
    assert "never truncates" in text


# -------------------------------------------------------- ingest_directory


def test_directory_traversal_order_is_deterministic(text: str) -> None:
    assert "sorted bytewise" in text


def test_directory_max_files_range(text: str) -> None:
    assert "[1,65536]" in text


def test_directory_reports_guards_hit(text: str) -> None:
    assert 'guards_hit: ["max_files"]' in text


def test_directory_per_file_failure_never_aborts_the_walk(text: str) -> None:
    assert "never aborts the walk" in text


def test_directory_truncates_tail_first(text: str) -> None:
    assert "tail-first" in text
    assert "truncated: true" in text


def test_directory_stats_counts(text: str) -> None:
    for key in ("proposed", "failed", "skipped"):
        assert key in text, key


# ------------------------------------------------------------------ bridges


def test_bridges_edge_kind_section(text: str) -> None:
    assert "## Edge kind `bridges`" in text


def test_bridges_endpoints_must_be_primary(text: str) -> None:
    assert "INVALID_BRIDGE_ENDPOINT" in text


def test_bridges_never_participate_in_contains(text: str) -> None:
    section = text.split("## Edge kind `bridges`")[1]
    assert "contains" in section


def test_bridges_are_evidence_based(text: str) -> None:
    section = text.split("## Edge kind `bridges`")[1]
    assert "evidence" in section
    assert "never" in section and "implicitly" in section


# ----------------------------------------------------------------- primary


def test_primary_node_rules_section(text: str) -> None:
    assert "## Primary node rules" in text


def test_primary_marker_is_named(text: str) -> None:
    assert "primary: true" in text


def test_primary_is_never_contained_by_another_primary(text: str) -> None:
    section = text.split("## Primary node rules")[1]
    assert "never contained by another primary" in section


def test_primary_creation_is_proposal_only(text: str) -> None:
    section = text.split("## Primary node rules")[1]
    assert "proposal-only" in section


def test_primary_may_root_arbitrary_depth_contains(text: str) -> None:
    section = text.split("## Primary node rules")[1]
    assert "arbitrarily deep" in section


# ---------------------------------------------------------- adapter mapping


@pytest.mark.parametrize(
    "fragment",
    [
        "know-your-soul ingest-file",
        "POST /v1/knowledge/ingest/file",
        "knowledge_ingest_file",
        "know-your-soul ingest-directory",
        "POST /v1/knowledge/ingest/directory",
        "knowledge_ingest_directory",
    ],
)
def test_adapter_mapping_covers_all_three_surfaces(text: str, fragment: str) -> None:
    assert fragment in text


def test_mcp_schemas_are_closed(text: str) -> None:
    assert "additionalProperties: false" in text


def test_path_deviation_is_confined_to_the_two_ingestion_verbs(text: str) -> None:
    assert "exactly one path-like property" in text


# ------------------------------------------------- frozen contract integrity


@pytest.mark.parametrize("filename", sorted(FROZEN_CONTRACTS))
def test_v1_v2_contracts_are_byte_identical(filename: str) -> None:
    path = CONTRACTS / filename
    assert path.is_file(), f"{filename} is missing from docs/contracts"
    assert _sha256(path) == FROZEN_CONTRACTS[filename], (
        f"{filename} drifted from its frozen digest — v1/v2 contracts are byte-frozen; changes belong in a new addendum"
    )


def test_only_the_addenda_are_unpinned_documents_in_the_contracts_directory() -> None:
    # Contract v4 (extraction-contract-v4.md, SUB-106) joins v3 as a pinned
    # addendum; its own digest lives in test_extraction_contract_v4_shape.py.
    on_disk = sorted(path.name for path in CONTRACTS.glob("*.md"))
    expected = sorted([*FROZEN_CONTRACTS, "ingestion-contract-v3.md", "extraction-contract-v4.md"])
    assert on_disk == expected, on_disk

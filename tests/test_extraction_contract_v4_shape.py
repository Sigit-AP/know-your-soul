"""Shape tests for the v4 extraction contract addendum (SUB-106).

The addendum (``docs/contracts/extraction-contract-v4.md``) is frozen upon
publication: these tests pin its structure — the ``submit_extraction`` verb,
the typed-error vocabulary, the truth-tier rules, the corroboration rules,
the review surface and the adapter mapping — and prove the v1/v2/v3 contracts
in the same directory stay byte-identical via pinned sha256 digests.
"""

from __future__ import annotations

import hashlib
import re
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[1]
CONTRACTS = REPO_ROOT / "docs" / "contracts"
V4 = CONTRACTS / "extraction-contract-v4.md"

# Pinned digests of the frozen contracts (v1 + v2 + v3). A single byte of
# drift in any of these files fails the pin — the addendum mechanism exists
# precisely so these documents never change.
FROZEN_CONTRACTS: dict[str, str] = {
    "access-policy.md": "68cdbaf88ef5e85dfc868bb35452c8f4b431b577fd69ff3bd3eac422e654d24e",
    "recommendation-contract.md": "811800cad1233ce14031f6a1e54220477bbda097aeea5e3e8d5b8827b2b1fe82",
    "relearn-contract.md": "24b6223eb4be813a9a4809147dce8dc30af17717ca3137727cae7194e58e6579",
    "retrieval-verbs-v2.md": "51541fa68e029a4b9c2130188e862cd11a5e27b31db30e596b378718af76d6da",
    "ingestion-contract-v3.md": "952a0f6dd0344374d299237ce1236e0c228b574a291bafe83ee02c0c27ea1467",
}


@pytest.fixture(scope="module")
def text() -> str:
    assert V4.is_file(), "the v4 extraction addendum must exist"
    # Whitespace-normalized: prose wraps at the margin, so phrase pins match
    # against a single-space-folded rendering.
    return re.sub(r"\s+", " ", V4.read_text(encoding="utf-8"))


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


# --------------------------------------------------------------- envelope


def test_addendum_file_exists() -> None:
    assert V4.is_file(), "docs/contracts/extraction-contract-v4.md is missing"


def test_contract_version_header(text: str) -> None:
    assert "Contract version: 4.0.0 (addendum)" in text


def test_status_is_active_and_dated(text: str) -> None:
    assert "Status: ACTIVE (2026-10-30)" in text


def test_authority_references_the_dispatch(text: str) -> None:
    assert "SUB-106" in text


def test_scope_declares_the_prior_contracts_unchanged(text: str) -> None:
    assert "byte-identical" in text
    assert "submit_extraction" in text


def test_frozen_upon_publication(text: str) -> None:
    assert "frozen upon publication" in text


@pytest.mark.parametrize("name", sorted(FROZEN_CONTRACTS))
def test_prior_contracts_stay_byte_identical(name: str) -> None:
    path = CONTRACTS / name
    assert path.is_file(), f"{name} is missing"
    assert _sha256(path) == FROZEN_CONTRACTS[name], f"{name} drifted from its frozen digest"


# ------------------------------------------------------------------- verb


def test_verb_signature_is_pinned(text: str) -> None:
    assert "`knowledge.submit_extraction(source_sha256, payload, method, confidence, source_path=None)`" in text


def test_input_fields_are_pinned(text: str) -> None:
    for field in ("`source_sha256`", "`source_path`", "`payload`", "`method`", "`confidence`"):
        assert field in text, field


def test_confidence_is_a_decimal_string(text: str) -> None:
    assert "decimal string" in text
    assert "never a binary float" in text


def test_receipt_fields_are_pinned(text: str) -> None:
    for field in (
        "`receipt_id`",
        "`status`",
        "`duplicate`",
        "`confidence_reported`",
        "`confidence_effective`",
        "`corroboration`",
        "`audit_event_id`",
    ):
        assert field in text, field


def test_receipt_statuses_are_pinned(text: str) -> None:
    for status in ("`pending_review`", "`corroborated`", "`contradiction_flagged`"):
        assert status in text, status


def test_proposal_only_discipline_is_pinned(text: str) -> None:
    assert "Nothing here writes the canonical graph" in text
    assert "extraction approve" in text


def test_idempotency_key_is_pinned(text: str) -> None:
    assert "`(source_sha256, actor, method_fingerprint)`" in text


def test_exactly_one_audit_event_is_pinned(text: str) -> None:
    assert "Exactly one final audit event per invocation (`knowledge.submit_extraction`)" in text


# --------------------------------------------------------------- errors


@pytest.mark.parametrize(
    "code",
    [
        "PAYLOAD_SCHEMA",
        "METHOD_SCHEMA",
        "CONFIDENCE_NOT_DECIMAL",
        "CONFIDENCE_OUT_OF_RANGE",
        "PAYLOAD_TOO_LARGE",
        "SOURCE_HASH_MISMATCH",
        "SOURCE_PATH_OUTSIDE_ROOT",
        "SOURCE_NOT_FOUND",
        "EXTRACTION_DUPLICATE",
        "EXTRACTION_NOT_FOUND",
        "EXTRACTION_STATE",
        "EXTRACTION_QUEUE_FULL",
    ],
)
def test_typed_error_vocabulary_is_pinned(text: str, code: str) -> None:
    assert f"`{code}`" in text, code


# ------------------------------------------------------------- truth tier


def test_truth_tier_is_pinned(text: str) -> None:
    assert "`agent-extracted`" in text or '"agent-extracted"' in text
    assert "intake_cap 0.35" in text
    assert "tier_cap 0.65" in text


def test_tier_promotion_is_operator_gated(text: str) -> None:
    assert "record_outcome" in text
    assert "no path in this contract writes a confidence" in text.lower() or "never above" in text


# ----------------------------------------------------------- corroboration


def test_corroboration_rules_are_pinned(text: str) -> None:
    assert "overlap_ratio ≥ 0.5" in text
    assert "+ 0.10 ×" in text
    assert "− 0.20 ×" in text
    assert "clamped to" in text and "[0, 0.65]" in text


def test_corroboration_requires_distinct_actors(text: str) -> None:
    assert "actors differ" in text and "never corroborates itself" in text


def test_contradiction_flags_both_sides(text: str) -> None:
    assert "**both**" in text


def test_group_recompute_is_deterministic(text: str) -> None:
    assert "pure function of the ordered stored rows" in text


# -------------------------------------------------------- review surface


def test_review_surface_is_pinned(text: str) -> None:
    assert "`know-your-soul extraction review [--status S] [--receipt ID]`" in text
    assert "`know-your-soul extraction approve RECEIPT_ID`" in text
    assert "`know-your-soul extraction reject RECEIPT_ID [--note]`" in text
    assert "`GET /api/extractions`" in text


def test_review_is_not_an_mcp_tool(text: str) -> None:
    assert "deliberately not an MCP tool" in text


def test_rollback_coverage_is_pinned(text: str) -> None:
    assert "byte-exact" in text and "knowledge.rollback" in text


# -------------------------------------------------------- adapter mapping


def test_adapter_mapping_table_is_pinned(text: str) -> None:
    row = (
        "| `submit_extraction` | `know-your-soul submit-extraction` |"
        " `POST /v1/knowledge/extraction` | `knowledge_submit_extraction` |"
    )
    assert row in text


def test_mcp_schema_is_closed(text: str) -> None:
    assert "additionalProperties: false" in text


def test_path_amendment_is_confined_to_source_path(text: str) -> None:
    assert "source_path" in text
    assert "no-path" in text


def test_superpower_write_lane_and_quota(text: str) -> None:
    assert "**write** lane" in text
    assert "token bucket" in text

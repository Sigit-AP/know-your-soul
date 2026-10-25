"""Contract tests for the frozen mathematical and measurement contracts (Task 3A).

These tests pin the contracts and fixtures produced by Task 3A against spec
sections 8, 9, and 13 of
``docs/superpowers/specs/2026-10-24-superpower-knowledge-soul-design.md``.

The module embeds an independent, test-only reference implementation of the
section 9 scoring mathematics (pure ``Decimal``, precision 34). It is not
feature code: it exists to verify that the frozen fixtures in
``tests/fixtures/`` are internally consistent with the spec formulas and that
the contract documents encode the spec clauses verbatim. Any change to a
formula, parameter, or the M definition requires a new contract version and
operator approval (spec section 9).
"""

from __future__ import annotations

import hashlib
import json
import random
from collections import Counter
from datetime import datetime
from decimal import ROUND_HALF_EVEN, Decimal, localcontext
from pathlib import Path
from typing import Any

import pytest

ROOT = Path(__file__).resolve().parent.parent
DOC_FILES = {
    "evidence_schema": ROOT / "docs" / "mathematics" / "evidence-schema.md",
    "scoring_contract": ROOT / "docs" / "mathematics" / "scoring-contract.md",
    "token_budget_contract": ROOT / "docs" / "mathematics" / "token-budget-contract.md",
    "access_policy": ROOT / "docs" / "contracts" / "access-policy.md",
    "recommendation_contract": ROOT / "docs" / "contracts" / "recommendation-contract.md",
    "relearn_contract": ROOT / "docs" / "contracts" / "relearn-contract.md",
}
SCORING_FIXTURES = ROOT / "tests" / "fixtures" / "scoring_cases.json"
TOKEN_FIXTURES = ROOT / "tests" / "fixtures" / "token_budget_cases.json"
ACCESS_FIXTURES = ROOT / "tests" / "fixtures" / "access_recommendation_relearn_cases.json"

QUANTUM = Decimal("1E-12")
PRECISION = 34
HALF_LIVES = {"p": Decimal("90"), "m": Decimal("30"), "e": Decimal("7")}
M_BIN_QUALITY_GATE = Decimal("0.70")
M_BIN_MIN_GROUPS = 3
LEVEL_THRESHOLDS = (Decimal("0.30"), Decimal("0.50"), Decimal("0.70"), Decimal("0.85"))
PROMOTION_MIN_GROUPS = 10
PROMOTION_MAX_U_P = Decimal("0.50")
PROMOTION_QUALITY_GATE = Decimal("0.70")
ACCESS_ONLY_CAP_TOKENS = 160
MINIMUM_BUDGET_TOKENS = 128
SOUL_MD_BASELINE_BYTES = 31279
SOUL_MD_BASELINE_TOKENS = 7820
SOUL_CORPUS_BASELINE_BYTES = 52604
SOUL_CORPUS_BASELINE_TOKENS = 13151

EVIDENCE_FIELDS = {
    "evidence_id",
    "node_id",
    "source_uri",
    "observed_at",
    "context_tags",
    "independence_key",
    "outcome",
    "correctness",
    "reliability",
    "reproducibility",
    "scope_fit",
    "successful_use",
    "task_coverage",
    "quality_at_use",
    "contradiction",
    "utilization_u",
    "latency_ms",
    "compute_ms",
    "output_chars",
}
DECIMAL_EVIDENCE_FIELDS = (
    "outcome",
    "correctness",
    "reliability",
    "reproducibility",
    "scope_fit",
    "successful_use",
    "quality_at_use",
    "contradiction",
    "utilization_u",
)
VALID_UTILIZATION_BINS = {Decimal(f"0.{i}") for i in range(1, 10)} | {Decimal("1.0")}

RECOMMENDATION_LISTS = ("best_actions", "recommendations", "unnecessary_actions")
RECOMMENDATION_ITEM_FIELDS = {
    "source_node_ids",
    "source_node_versions",
    "evidence_ids",
    "learned_class",
    "reason_codes",
    "expiry",
}
L1_SURFACE_CLASSES = {
    "L1_greeting",
    "L1_one_fact_lookup",
    "L1_one_command",
    "L1_file_read",
    "L1_status_check",
}
ZERO_WEIGHT_EVENT_KINDS = ("relearn", "source_version", "backup", "rollback")
SCORE_MOVING_TOOLS = ("log_event", "record_outcome", "evaluate")


# --------------------------------------------------------------------- helpers


def _load_json(path: Path) -> Any:
    assert path.is_file(), f"missing contract artifact: {path.relative_to(ROOT)}"
    return json.loads(path.read_text(encoding="utf-8"))


def _doc_text(key: str) -> str:
    path = DOC_FILES[key]
    assert path.is_file(), f"missing contract document: {path.relative_to(ROOT)}"
    return path.read_text(encoding="utf-8")


def _canonical_json(value: Any) -> str:
    """Deterministic one-line JSON (sorted keys, compact separators), UTF-8."""
    return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False)


def _quantize(value: Decimal) -> Decimal:
    return value.quantize(QUANTUM, rounding=ROUND_HALF_EVEN)


def _s(value: Decimal) -> str:
    """Serialized score: 12 decimal places, ROUND_HALF_EVEN (spec section 9).

    Rendered in fixed-point notation so an exact zero reads ``0.000000000000``
    (semantically identical to the store's ``str(quantize())`` rendering
    ``0E-12``: same value, same quantum).
    """
    return format(_quantize(value), "f")


def _estimated_tokens(payload: Any) -> int:
    """estimated_tokens = ceil(utf8_byte_length / 4) over the canonical payload."""
    byte_length = len(_canonical_json(payload).encode("utf-8"))
    return -(-byte_length // 4)


def _parse_ts(value: str) -> datetime:
    return datetime.fromisoformat(value)


def _microseconds(delta: Any) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _jaccard(query_tags: frozenset[str], evidence_tags: frozenset[str]) -> Decimal:
    union = query_tags | evidence_tags
    if not union:
        return Decimal(1)
    intersection = query_tags & evidence_tags
    return Decimal(len(intersection)) / Decimal(len(union))


# ------------------------------------- reference scoring (spec section 9 only)


def _classify_evidence(item: dict[str, Any], at: datetime, query_tags: frozenset[str]) -> tuple[str, str | None]:
    """Classify one evidence item.

    Returns ("eligible", None) or ("excluded", count_key). Exclusion
    precedence (literal reading, recorded in the Task 3A report):
    missing provenance, then invalid fields, then zero reliability, then
    disjoint/out-of-context.
    """
    if not item.get("source_uri"):
        return "excluded", "missing_provenance"
    try:
        outcome = Decimal(item["outcome"])
        if not (Decimal(-1) <= outcome <= Decimal(1)):
            return "excluded", "other_exclusions"
        for field in (
            "correctness",
            "reliability",
            "reproducibility",
            "scope_fit",
            "successful_use",
            "quality_at_use",
            "contradiction",
        ):
            value = Decimal(item[field])
            if not (Decimal(0) <= value <= Decimal(1)):
                return "excluded", "other_exclusions"
        if item["task_coverage"] is None:
            return "excluded", "other_exclusions"  # empty rubric denominator
        coverage = Decimal(item["task_coverage"])
        if not (Decimal(0) <= coverage <= Decimal(1)):
            return "excluded", "other_exclusions"
        utilization = Decimal(item["utilization_u"])
        if utilization not in VALID_UTILIZATION_BINS:
            return "excluded", "other_exclusions"
        if not (int(item["latency_ms"]) > 0):
            return "excluded", "other_exclusions"
        if not (int(item["compute_ms"]) > 0):
            return "excluded", "other_exclusions"
        if not (int(item["output_chars"]) >= 0):
            return "excluded", "other_exclusions"
        observed = _parse_ts(item["observed_at"])
        if _microseconds(at - observed) < 0:
            return "excluded", "other_exclusions"  # evidence from the future
    except Exception:
        return "excluded", "other_exclusions"
    if Decimal(item["reliability"]) == 0:
        return "excluded", "zero_reliability"
    if query_tags and _jaccard(query_tags, frozenset(item["context_tags"])) == 0:
        return "excluded", "disjoint_context"
    return "eligible", None


def _dedup_key(item: dict[str, Any]) -> str:
    """Canonical content hash over all fields except ``evidence_id``."""
    content = {k: v for k, v in item.items() if k != "evidence_id"}
    return hashlib.sha256(_canonical_json(content).encode("utf-8")).hexdigest()


def _dedup(items: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Exact duplicates are represented once, keeping the lexicographically
    smallest UTF-8 evidence_id; the result is sorted by ascending evidence_id."""
    retained: dict[str, dict[str, Any]] = {}
    for item in items:
        key = _dedup_key(item)
        if key not in retained or item["evidence_id"] < retained[key]["evidence_id"]:
            retained[key] = item
    return sorted(retained.values(), key=lambda entry: entry["evidence_id"].encode("utf-8"))


def compute_scores(case: dict[str, Any]) -> dict[str, Any]:
    """Reference implementation of the spec section 9 scoring contract."""
    with localcontext() as context:
        context.prec = PRECISION
        at = _parse_ts(case["at"])
        query_tags = frozenset(case.get("query_context_tags", []))
        node_depths = case.get("node_depths", {})
        counts = {
            "missing_provenance": 0,
            "disjoint_context": 0,
            "zero_reliability": 0,
            "other_exclusions": 0,
        }
        eligible: list[dict[str, Any]] = []
        for item in case["evidence"]:
            status, count_key = _classify_evidence(item, at, query_tags)
            if status == "eligible":
                eligible.append(item)
            else:
                assert count_key is not None
                counts[count_key] += 1

        deduped = _dedup(eligible)
        group_sizes = Counter(item["independence_key"] for item in deduped)
        ln2 = Decimal(2).ln()
        microseconds_per_day = Decimal(86_400_000_000)

        enriched: list[dict[str, Any]] = []
        for item in deduped:
            observed = _parse_ts(item["observed_at"])
            age_days = Decimal(_microseconds(at - observed)) / microseconds_per_day
            independence = Decimal(1) / Decimal(group_sizes[item["independence_key"]]).sqrt()
            if not query_tags:
                relevance = Decimal(1)
            else:
                relevance = _jaccard(query_tags, frozenset(item["context_tags"]))
            depth = Decimal(node_depths.get(item["node_id"], 0))
            reliability = Decimal(item["reliability"])
            q = {
                dimension: reliability * (-(ln2) * age_days / half_life).exp() * relevance * independence / (1 + depth)
                for dimension, half_life in HALF_LIVES.items()
            }
            p_j = (
                Decimal("0.30") * Decimal(item["correctness"])
                + Decimal("0.25") * ((Decimal(item["outcome"]) + 1) / 2)
                + Decimal("0.20") * Decimal(item["reproducibility"])
                + Decimal("0.15") * Decimal(item["scope_fit"])
                + Decimal("0.10") * (1 - Decimal(item["contradiction"]))
            )
            benefit = Decimal(item["successful_use"]) * Decimal(item["task_coverage"])
            cost = (
                Decimal("0.4") * min(Decimal(2), Decimal(item["latency_ms"]) / 1000)
                + Decimal("0.4") * min(Decimal(2), Decimal(item["output_chars"]) / 4000)
                + Decimal("0.2") * min(Decimal(2), Decimal(item["compute_ms"]) / 1000)
            )
            e_j = Decimal(0) if benefit + cost == 0 else benefit / (benefit + cost)
            enriched.append({"item": item, "q": q, "p_j": p_j, "e_j": e_j})

        scores: dict[str, Decimal] = {}
        totals: dict[str, Decimal] = {}
        uncertainty: dict[str, Decimal] = {}
        zero_total_weight: dict[str, bool] = {}

        for dimension in ("p", "e"):
            total = sum((entry["q"][dimension] for entry in enriched), Decimal(0))
            totals[dimension] = total
            if total == 0:
                scores[dimension] = Decimal(0)
                uncertainty[dimension] = Decimal(1)
                zero_total_weight[dimension] = True
            else:
                term = "p_j" if dimension == "p" else "e_j"
                scores[dimension] = sum(
                    ((entry["q"][dimension] / total) * entry[term] for entry in enriched),
                    Decimal(0),
                )
                uncertainty[dimension] = Decimal(1) / (1 + total).sqrt()
                zero_total_weight[dimension] = False

        # M: maximum effective utilization under the quality gate.
        total_m = sum((entry["q"]["m"] for entry in enriched), Decimal(0))
        totals["m"] = total_m
        bins: dict[Decimal, list[dict[str, Any]]] = {}
        for entry in enriched:
            if entry["q"]["m"] > 0:
                bins.setdefault(Decimal(entry["item"]["utilization_u"]), []).append(entry)

        best: tuple[Decimal, Decimal, list[str], int] | None = None
        for utilization in sorted(bins):
            members = bins[utilization]
            local_total = sum((entry["q"]["m"] for entry in members), Decimal(0))
            if local_total == 0:
                continue
            groups = len({entry["item"]["independence_key"] for entry in members})
            quality = sum(
                ((entry["q"]["m"] / local_total) * Decimal(entry["item"]["quality_at_use"]) for entry in members),
                Decimal(0),
            )
            success = sum(
                (
                    (entry["q"]["m"] / local_total)
                    * Decimal(entry["item"]["successful_use"])
                    * Decimal(entry["item"]["task_coverage"])
                    for entry in members
                ),
                Decimal(0),
            )
            if groups < M_BIN_MIN_GROUPS or quality < M_BIN_QUALITY_GATE:
                continue
            value = utilization * success
            id_list = sorted(entry["item"]["evidence_id"] for entry in members)
            if (
                best is None
                or value > best[0]
                or (value == best[0] and (utilization < best[1] or (utilization == best[1] and id_list < best[2])))
            ):
                best = (value, utilization, id_list, len(members))

        if total_m == 0:
            scores["m"] = Decimal(0)
            uncertainty["m"] = Decimal(1)
            zero_total_weight["m"] = True
            selected_m_bin = None
            m_bin_evidence_count = None
        else:
            uncertainty["m"] = Decimal(1) / (1 + total_m).sqrt()
            zero_total_weight["m"] = False
            if best is None:
                scores["m"] = Decimal(0)
                selected_m_bin = None
                m_bin_evidence_count = None
            else:
                scores["m"] = best[0]
                selected_m_bin = format(best[1], ".1f")
                m_bin_evidence_count = best[3]

        p_score = scores["p"]
        m_score = scores["m"]
        e_score = scores["e"]

        if zero_total_weight["p"]:
            professional_level = 1
            groups_p = 0
        else:
            groups_p = len({entry["item"]["independence_key"] for entry in enriched if entry["q"]["p"] > 0})
            base_level = 1 + sum(p_score >= threshold for threshold in LEVEL_THRESHOLDS)
            level = base_level
            if groups_p < 5:
                level = min(level, 2)
            elif groups_p < 10:
                level = min(level, 3)
            elif groups_p < 20:
                level = min(level, 4)
            if uncertainty["p"] > Decimal("0.70"):
                level = min(level, 2)
            if uncertainty["p"] > Decimal("0.50"):
                level = min(level, 3)
            if uncertainty["p"] > Decimal("0.30"):
                level = min(level, 4)
            professional_level = level

        promotable = bool(
            not zero_total_weight["p"]
            and p_score >= PROMOTION_QUALITY_GATE
            and groups_p >= PROMOTION_MIN_GROUPS
            and uncertainty["p"] <= PROMOTION_MAX_U_P
        )

        if p_score < PROMOTION_QUALITY_GATE or p_score == 0 or m_score == 0 or e_score == 0:
            g_score = Decimal(0)
        else:
            g_score = p_score ** Decimal("0.50") * m_score ** Decimal("0.30") * e_score ** Decimal("0.20")

        overall_uncertainty = max(uncertainty.values())

        return {
            "p": _s(p_score),
            "m": _s(m_score),
            "e": _s(e_score),
            "u_p": _s(uncertainty["p"]),
            "u_m": _s(uncertainty["m"]),
            "u_e": _s(uncertainty["e"]),
            "overall_uncertainty": _s(overall_uncertainty),
            "confidence_p": _s(1 - uncertainty["p"]),
            "confidence_m": _s(1 - uncertainty["m"]),
            "confidence_e": _s(1 - uncertainty["e"]),
            "selected_m_bin": selected_m_bin,
            "m_bin_evidence_count": m_bin_evidence_count,
            "professional_level": professional_level,
            "promotable": promotable,
            "g": _s(g_score),
            "exclusion_counts": counts,
            "zero_total_weight": {
                dimension: (
                    {"zero": True, "reason": "ZERO_TOTAL_WEIGHT"}
                    if zero_total_weight[dimension]
                    else {"zero": False, "reason": None}
                )
                for dimension in ("p", "m", "e")
            },
        }


# ------------------------------------------------------------------ fixtures


@pytest.fixture(scope="module")
def scoring_cases() -> dict[str, Any]:
    return _load_json(SCORING_FIXTURES)


@pytest.fixture(scope="module")
def token_cases() -> dict[str, Any]:
    return _load_json(TOKEN_FIXTURES)


@pytest.fixture(scope="module")
def access_cases() -> dict[str, Any]:
    return _load_json(ACCESS_FIXTURES)


# ------------------------------------------------------------- document tests


def test_all_contract_documents_exist() -> None:
    for key in DOC_FILES:
        _doc_text(key)


def test_documents_are_frozen_and_versioned() -> None:
    for key in DOC_FILES:
        text = _doc_text(key)
        assert "Status: FROZEN" in text, f"{key} is not marked FROZEN"
        assert "Contract version:" in text, f"{key} has no contract version"
        assert "operator approval" in text, f"{key} lacks the change-control clause"


def test_evidence_schema_document_encodes_spec_section_9_fields() -> None:
    text = _doc_text("evidence_schema")
    for field in EVIDENCE_FIELDS:
        assert f"`{field}`" in text, f"evidence schema is missing field `{field}`"
    for clause in (
        "`outcome` in `[-1,1]`",
        "`utilization_u` in `{0.1,0.2,...,1.0}`",
        "`latency_ms > 0`, `compute_ms > 0`, and `output_chars >= 0`",
        "unscorable and reported separately",
        "12 decimal places with `ROUND_HALF_EVEN`",
        "lexicographically smallest UTF-8 `evidence_id`",
    ):
        assert clause in text, f"evidence schema is missing clause: {clause}"


def test_scoring_contract_document_encodes_spec_section_9_verbatim() -> None:
    text = _doc_text("scoring_contract")
    for clause in (
        "`q_j,d = reliability_j * exp(-ln(2)*age_days_j/h_d) * context_relevance_j * independence_j / (1 + depth_j)`",
        "`h_P=90`, `h_M=30`, `h_E=7` days",
        "`p_j = 0.30*correctness_j + 0.25*((outcome_j+1)/2) + 0.20*reproducibility_j + "
        "0.15*scope_fit_j + 0.10*(1-contradiction_j)`",
        "`M = max over eligible u of (u * S(u))`; if no bin is eligible, `M=0` and `selected_m_bin=null`",
        "`benefit_j = successful_use_j * task_coverage_j`",
        "`e_j = benefit_j/(benefit_j+cost_j)`, or zero if both are zero",
        "`U_d=1/sqrt(1+sum(q_d))`",
        "level 1 below 0.30; 2 at 0.30; 3 at 0.50; 4 at 0.70; 5 at 0.85",
        "fewer than 5 groups caps at 2; fewer than 10 caps at 3; fewer than 20 caps at 4",
        "`U_P>0.70` caps at 2, `U_P>0.50` caps at 3, and `U_P>0.30` caps at 4",
        "`G=0` when `P<0.70`; otherwise `G=P^0.50 * M^0.30 * E^0.20`",
        "Decimal` with precision 34",
        "quantized to 12 decimal places with `ROUND_HALF_EVEN`",
        "`independence_j=1/sqrt(group_size)`",
        "`age_days_j` is the nonnegative integer microsecond difference",
        "`context_relevance=1` for an empty query context",
        "`Q(u)=sum(w_j,u*quality_at_use_j)` and `S(u)=sum(w_j,u*successful_use_j*task_coverage_j)`",
        "at least three distinct independence groups",
        "Ties are resolved toward the smaller `u`",
        "`task_coverage` is `covered_required_task_items / total_required_task_items`",
        "`cost_j = 0.4*min(2, latency_ms_j/1000) + 0.4*min(2, output_chars_j/4000) + 0.2*min(2, compute_ms_j/1000)`",
        "at least 10 distinct independence groups with `q_P>0` and `U_P<=0.50`",
        "`ZERO_TOTAL_WEIGHT`",
    ):
        assert clause in text, f"scoring contract is missing clause: {clause}"


def test_token_budget_contract_document_encodes_spec_verbatim() -> None:
    text = _doc_text("token_budget_contract")
    for clause in (
        "`estimated_tokens = ceil(utf8_byte_length / 4)`",
        "Budgets below 128 estimated tokens return `BUDGET_TOO_SMALL` without knowledge payload",
        "an error rather than an over-budget success",
        "SOUL.md 31,279 bytes / 7,820 estimated tokens",
        "SOUL corpus 52,604 bytes / 13,151 estimated tokens",
        "No provider-tokenizer equality is claimed",
        "A case that loses quality is rejected even when it saves tokens",
        "budget is an integer `[128, configured_max]`",
    ):
        assert clause in text, f"token budget contract is missing clause: {clause}"


def test_access_policy_document_encodes_spec_section_13_2_verbatim() -> None:
    text = _doc_text("access_policy")
    for clause in (
        "L1 surfaces (greeting, one fact lookup, one command, a file read, a status check) never call the system",
        "capped at 160 estimated tokens with zero knowledge payload",
        "No eleventh public tool is added",
        "never costs a model call or a graph call",
        "`access_only=true`",
    ):
        assert clause in text, f"access policy is missing clause: {clause}"


def test_recommendation_contract_document_encodes_spec_verbatim() -> None:
    text = _doc_text("recommendation_contract")
    for clause in (
        "exactly three lists: `best_actions`, `recommendations`, and `unnecessary_actions`",
        "never returns raw SOUL or corpus text",
        "One observation can never promote an action or a node",
        "source node IDs and versions, evidence IDs, learned class, reason codes, and expiry",
    ):
        assert clause in text, f"recommendation contract is missing clause: {clause}"


def test_relearn_contract_document_encodes_spec_section_13_3_verbatim() -> None:
    text = _doc_text("relearn_contract")
    for clause in (
        "RESET starts with zero old evidence; P=M=E=0, U=1, level 1",
        "unmapped evidence is quarantined from scoring",
        "`MIXED_SOURCE_VERSIONS`",
        "carry zero evidence weight",
        "Only `log_event`, `record_outcome`, and `evaluate` can move P/M/E",
        "Rollback restores exact pre-relearn hashes, graph bytes, and P/M/E values with exactly one linked audit event",
        "`RelearnOffer`",
        "`RELEARN_PENDING`",
        "mtime is never a correctness gate",
        "`mixed_source_versions=true`",
    ):
        assert clause in text, f"relearn contract is missing clause: {clause}"


# ------------------------------------------------------- scoring fixture tests


def test_scoring_fixture_counts_and_required_case_types(scoring_cases: dict[str, Any]) -> None:
    cases = scoring_cases["cases"]
    assert len(cases) >= 30, f"need at least 30 scoring fixtures, found {len(cases)}"
    ids = [case["id"] for case in cases]
    assert len(ids) == len(set(ids)), "duplicate scoring fixture ids"
    present = {tag for case in cases for tag in case["case_type"]}
    for required in (
        "hand_calculated",
        "bounds",
        "deterministic_replay",
        "recency",
        "context_relevance",
        "duplicate_resistance",
        "correlated_groups",
        "contradiction",
        "zero_evidence",
        "disjoint_context",
        "all_zero_reliability",
        "depth_discount",
        "m_bin_local_denominator",
        "bin_boundary",
        "null_m_bin",
        "zero_total_weight",
        "e_units",
        "level_caps",
        "p_gate",
        "permutation_replay",
    ):
        assert required in present, f"no scoring fixture tagged `{required}`"
    hand_calculated = [case for case in cases if "hand_calculated" in case["case_type"]]
    assert len(hand_calculated) >= 3, "need at least 3 independently hand-calculated fixtures"
    for case in hand_calculated:
        assert case.get("derivation"), f"{case['id']} is marked hand_calculated without a derivation"


def test_scoring_evidence_schema(scoring_cases: dict[str, Any]) -> None:
    for case in scoring_cases["cases"]:
        for item in case["evidence"]:
            assert set(item) == EVIDENCE_FIELDS, (
                f"{case['id']}:{item.get('evidence_id')} has fields {sorted(set(item) ^ EVIDENCE_FIELDS)} "
                "outside the frozen evidence schema"
            )
            assert isinstance(item["context_tags"], list)
            for field in DECIMAL_EVIDENCE_FIELDS:
                assert isinstance(item[field], str), (
                    f"{case['id']}:{item['evidence_id']} `{field}` must be a decimal string"
                )
                Decimal(item[field])
            if item["task_coverage"] is not None:
                assert isinstance(item["task_coverage"], str)
                Decimal(item["task_coverage"])
            for field in ("latency_ms", "compute_ms", "output_chars"):
                assert isinstance(item[field], int), f"{case['id']}:{item['evidence_id']} `{field}` must be an integer"
            datetime.fromisoformat(item["observed_at"])


def test_scoring_fixtures_recompute_exactly(scoring_cases: dict[str, Any]) -> None:
    for case in scoring_cases["cases"]:
        recomputed = compute_scores(case)
        assert recomputed == case["expected"], (
            f"{case['id']}: fixture does not match the spec section 9 reference computation\n"
            f"expected: {json.dumps(case['expected'], sort_keys=True)}\n"
            f"recomputed: {json.dumps(recomputed, sort_keys=True)}"
        )


def test_scoring_permutation_replay_is_byte_identical(scoring_cases: dict[str, Any]) -> None:
    rng = random.Random(20261024)
    for case in scoring_cases["cases"]:
        baseline = _canonical_json(compute_scores(case))
        evidence = case["evidence"]
        for _ in range(min(4, max(1, len(evidence)))):
            permuted = list(evidence)
            rng.shuffle(permuted)
            replayed = dict(case, evidence=permuted)
            assert _canonical_json(compute_scores(replayed)) == baseline, (
                f"{case['id']}: permuting input evidence changed the 12-decimal output"
            )


def test_scoring_bounds_and_invariants(scoring_cases: dict[str, Any]) -> None:
    for case in scoring_cases["cases"]:
        expected = case["expected"]
        for key in ("p", "m", "e", "g"):
            value = Decimal(expected[key])
            assert Decimal(0) <= value <= Decimal(1), f"{case['id']}:{key} out of [0,1]"
        for key in ("u_p", "u_m", "u_e", "overall_uncertainty"):
            value = Decimal(expected[key])
            assert Decimal(0) <= value <= Decimal(1), f"{case['id']}:{key} out of [0,1]"
        assert Decimal(expected["overall_uncertainty"]) == max(
            Decimal(expected["u_p"]), Decimal(expected["u_m"]), Decimal(expected["u_e"])
        ), f"{case['id']}: overall uncertainty must be max(U_P,U_M,U_E)"
        assert 1 <= expected["professional_level"] <= 5
        for key in ("p", "m", "e", "u_p", "u_m", "u_e", "overall_uncertainty", "g"):
            text = expected[key]
            assert "." in text and len(text.split(".")[1]) == 12, (
                f"{case['id']}:{key}={text} is not serialized at 12 decimal places"
            )


def test_one_observation_never_promotes(scoring_cases: dict[str, Any]) -> None:
    single_group_cases = [
        case for case in scoring_cases["cases"] if len({item["independence_key"] for item in case["evidence"]}) <= 1
    ]
    assert single_group_cases, "need at least one single-observation scoring fixture"
    for case in single_group_cases:
        assert case["expected"]["promotable"] is False, f"{case['id']}: one observation promoted"


def test_p_gate_blocks_g_and_promotion(scoring_cases: dict[str, Any]) -> None:
    gated = [
        case
        for case in scoring_cases["cases"]
        if Decimal(case["expected"]["p"]) < PROMOTION_QUALITY_GATE
        and not case["expected"]["zero_total_weight"]["p"]["zero"]
    ]
    assert gated, "need at least one scored fixture below the P gate"
    for case in gated:
        assert Decimal(case["expected"]["g"]) == 0, f"{case['id']}: G must be 0 when P<0.70"
        assert case["expected"]["promotable"] is False


# --------------------------------------------------------- token fixture tests


def test_token_fixture_counts_and_required_case_types(token_cases: dict[str, Any]) -> None:
    cases = token_cases["cases"]
    assert len(cases) >= 12, f"need at least 12 token fixtures, found {len(cases)}"
    present = {tag for case in cases for tag in case["case_type"]}
    for required in (
        "complete_payload",
        "budget_too_small",
        "truncation",
        "metadata_exceeds_budget",
        "multibyte_utf8",
        "transport_parity",
        "frozen_baseline",
        "paired_quality_gate",
        "access_only",
    ):
        assert required in present, f"no token fixture tagged `{required}`"


def test_token_estimator_matches_fixtures(token_cases: dict[str, Any]) -> None:
    for case in token_cases["cases"]:
        payload = case.get("payload")
        if payload is None:
            continue
        recomputed = _estimated_tokens(payload)
        assert recomputed == case["expected"]["estimated_tokens"], (
            f"{case['id']}: estimated_tokens {case['expected']['estimated_tokens']} != "
            f"ceil(utf8_bytes/4) = {recomputed}"
        )


def test_token_budget_rules(token_cases: dict[str, Any]) -> None:
    for case in token_cases["cases"]:
        budget = case.get("budget")
        expected = case["expected"]
        outcome = expected["outcome"]
        if budget is not None and budget < MINIMUM_BUDGET_TOKENS:
            assert outcome == "BUDGET_TOO_SMALL", f"{case['id']}: budgets below 128 must return BUDGET_TOO_SMALL"
            assert expected["knowledge_payload_tokens"] == 0
            continue
        if outcome in ("ok", "truncated"):
            assert expected["estimated_tokens"] <= budget, f"{case['id']}: successful response exceeds its budget"
        if outcome == "metadata_exceeds_budget":
            assert budget is not None and budget >= MINIMUM_BUDGET_TOKENS
            assert expected["estimated_tokens"] > budget, (
                f"{case['id']}: error case must be genuinely over budget (error, not over-budget success)"
            )
        if outcome == "truncated":
            assert case["payload"]["truncated"] is True, f"{case['id']}: truncated outcome must set truncated=true"


def test_token_frozen_baselines(token_cases: dict[str, Any]) -> None:
    baselines = token_cases["frozen_baselines"]
    soul_md = baselines["soul_md"]
    corpus = baselines["soul_corpus"]
    assert soul_md["bytes"] == SOUL_MD_BASELINE_BYTES
    assert soul_md["estimated_tokens"] == SOUL_MD_BASELINE_TOKENS
    assert corpus["bytes"] == SOUL_CORPUS_BASELINE_BYTES
    assert corpus["estimated_tokens"] == SOUL_CORPUS_BASELINE_TOKENS
    assert -(-soul_md["bytes"] // 4) == soul_md["estimated_tokens"]
    assert -(-corpus["bytes"] // 4) == corpus["estimated_tokens"]


def test_token_paired_quality_gate(token_cases: dict[str, Any]) -> None:
    paired = [case for case in token_cases["cases"] if "paired_quality_gate" in case["case_type"]]
    assert len(paired) >= 2, "need paired measurement fixtures for both accept and reject"
    for case in paired:
        measurement = case["paired_measurement"]
        assert measurement["baseline_estimated_tokens"] > 0
        assert measurement["system_estimated_tokens"] >= 0
        if measurement["quality_maintained"]:
            assert case["expected"]["verdict"] == "accepted"
        else:
            assert case["expected"]["verdict"] == "rejected", (
                f"{case['id']}: a case that loses quality is rejected even when it saves tokens"
            )


# ------------------------------------------- access / recommendation / relearn


def test_access_policy_fixtures(access_cases: dict[str, Any]) -> None:
    cases = access_cases["access_cases"]
    l1_seen: set[str] = set()
    for case in cases:
        surface = case["surface_class"]
        expected = case["expected"]
        if surface in L1_SURFACE_CLASSES:
            l1_seen.add(surface)
            assert expected["decision"] == "never_call", (
                f"{case['id']}: L1 surface {surface} must never call the system"
            )
            assert expected["model_calls"] == 0 and expected["graph_calls"] == 0
        elif surface == "ambiguous":
            assert expected["decision"] == "may_call_access_only"
            assert expected["access_only"] is True
            assert expected["max_estimated_tokens"] <= ACCESS_ONLY_CAP_TOKENS
            assert expected["knowledge_payload_tokens"] == 0
        elif surface == "required":
            assert expected["decision"] == "call"
            assert expected.get("access_only", False) is False
        else:  # pragma: no cover - fixture authoring guard
            raise AssertionError(f"{case['id']}: unknown surface class {surface}")
    assert l1_seen == L1_SURFACE_CLASSES, "all five L1 surface classes must be covered"
    ambiguous = [case for case in cases if case["surface_class"] == "ambiguous"]
    assert any(case["expected"]["max_estimated_tokens"] == ACCESS_ONLY_CAP_TOKENS for case in ambiguous), (
        "need an ambiguous-cap fixture at exactly 160 estimated tokens"
    )


def test_recommendation_fixtures(access_cases: dict[str, Any]) -> None:
    cases = access_cases["recommendation_cases"]
    assert cases, "need recommendation fixtures"
    for case in cases:
        block = case["recommendation_block"]
        assert set(block) == set(RECOMMENDATION_LISTS), (
            f"{case['id']}: recommendation block must contain exactly "
            "best_actions / recommendations / unnecessary_actions"
        )
        serialized = _canonical_json(block)
        for fragment in case.get("raw_soul_forbidden_fragments", []):
            assert fragment not in serialized, f"{case['id']}: recommendation response contains raw SOUL/corpus text"
        for list_name in RECOMMENDATION_LISTS:
            for item in block[list_name]:
                assert RECOMMENDATION_ITEM_FIELDS <= set(item), (
                    f"{case['id']}:{list_name} item missing fields {sorted(RECOMMENDATION_ITEM_FIELDS - set(item))}"
                )
                assert len(item["source_node_ids"]) == len(item["source_node_versions"])
        expectation = case["expected"]
        if expectation.get("single_observation_rejection"):
            promoted_items = [
                item
                for list_name in ("best_actions", "recommendations", "unnecessary_actions")
                for item in block[list_name]
                if item.get("promoted")
            ]
            for item in promoted_items:
                assert len(item["evidence_ids"]) > 1, (
                    f"{case['id']}: one observation can never promote an action or a node"
                )


def test_relearn_fixtures(access_cases: dict[str, Any]) -> None:
    cases = {case["id"]: case for case in access_cases["relearn_cases"]}
    assert cases, "need relearn fixtures"

    reset = [case for case in cases.values() if case.get("mode") == "RESET"]
    assert reset, "need a RESET relearn fixture"
    for case in reset:
        scores = case["expected"]["scores"]
        assert scores["p"] == "0.000000000000"
        assert scores["m"] == "0.000000000000"
        assert scores["e"] == "0.000000000000"
        assert scores["u"] == "1.000000000000"
        assert scores["professional_level"] == 1
        assert case["expected"]["promotion_blocked"] is True
        assert case["expected"]["old_evidence_carried"] == 0

    accumulate = [case for case in cases.values() if case.get("mode") == "ACCUMULATE"]
    assert accumulate, "need an ACCUMULATE relearn fixture"
    for case in accumulate:
        quarantined = set(case["expected"]["quarantined_evidence_ids"])
        scored = set(case["expected"]["scored_evidence_ids"])
        assert not (quarantined & scored), f"{case['id']}: unmapped evidence is quarantined from scoring"

    for case in cases.values():
        expected = case["expected"]
        if expected.get("default_mixed_response"):
            assert expected["outcome"] == "error"
            assert expected["error_code"] == "MIXED_SOURCE_VERSIONS"
        if expected.get("mixed_source_versions") is True:
            assert expected["operator_approval"] is True, (
                f"{case['id']}: a mixed response requires explicit operator approval"
            )

    zero_weight = [case for case in cases.values() if case.get("case_type") == "zero_evidence_weight"]
    assert zero_weight, "need a zero-evidence-weight relearn fixture"
    for case in zero_weight:
        assert set(case["zero_weight_event_kinds"]) == set(ZERO_WEIGHT_EVENT_KINDS)
        assert set(case["score_moving_tools"]) == set(SCORE_MOVING_TOOLS)

    rollback = [case for case in cases.values() if case.get("case_type") == "rollback"]
    assert rollback, "need a rollback fixture"
    for case in rollback:
        expected = case["expected"]
        assert expected["restores_pre_relearn_hashes"] is True
        assert expected["restores_pre_relearn_scores"] is True
        assert expected["linked_audit_events"] == 1

    offers = [case for case in cases.values() if case.get("case_type") == "relearn_offer"]
    assert offers, "need a RelearnOffer fixture"
    for case in offers:
        assert case["expected"]["emits"] == "RelearnOffer"
        assert case["expected"]["status"] == "RELEARN_PENDING"
        assert case["expected"]["automatic_mode_selection"] is False

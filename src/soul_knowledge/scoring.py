"""Calibrated P/M/E scoring engine (Task 5 core).

Implements the FROZEN scoring contract 1.0.0
(``docs/mathematics/scoring-contract.md``) over evidence items conforming to
the FROZEN evidence schema 1.0.0 (``docs/mathematics/evidence-schema.md``).
No formula, threshold, or parameter is chosen here: every constant comes from
``scoring_config.toml``, which mirrors the frozen contract, and any change to
it requires a new contract version and operator approval (spec section 9).

Numeric rules (contract, "Numeric semantics"):

* Pure :class:`~decimal.Decimal`, precision 34, no binary-float conversion.
* Intermediate comparisons use full precision; serialized scores are
  quantized to 12 decimal places with ``ROUND_HALF_EVEN``.
* Before scoring, exact duplicates are deduplicated by canonical content hash
  (all fields except ``evidence_id``), the lexicographically smallest UTF-8
  ``evidence_id`` is retained, and the retained evidence is sorted by
  ascending UTF-8 ``evidence_id``. That single order drives every sum, so
  permuting the input produces byte-identical 12-decimal output.

The engine reads the event store only through the public replay/read surface
(``EventStore.replay_domain``); scoring never writes.
"""

from __future__ import annotations

import hashlib
import tomllib
from collections import Counter, defaultdict
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass, field
from datetime import UTC, datetime
from decimal import Decimal, localcontext
from pathlib import Path
from typing import Any

from soul_knowledge.domain import (
    DECIMAL_PRECISION,
    Evidence,
    RelationKind,
    canonical_json,
    quantize_score,
)
from soul_knowledge.store import EventStore

__all__ = [
    "MICROSECONDS_PER_DAY",
    "SCORING_CONFIG_PATH",
    "ScoreAssessment",
    "ScoreEngine",
    "ScoringConfig",
    "ScoringConfigError",
    "UnknownNodeError",
    "ZeroWeightStatus",
    "evidence_item",
    "score_items",
    "serialize_score",
]

SCORING_CONFIG_PATH = Path(__file__).resolve().parent / "scoring_config.toml"

PRECISION = DECIMAL_PRECISION
MICROSECONDS_PER_DAY = Decimal(86_400 * 1_000_000)

_EXCLUSION_KEYS = ("missing_provenance", "disjoint_context", "zero_reliability", "other_exclusions")
_DIMENSIONS = ("p", "m", "e")
_VALID_UTILIZATION_BINS = frozenset(Decimal(f"0.{i}") for i in range(1, 10)) | {Decimal("1.0")}


class ScoringConfigError(ValueError):
    """The scoring configuration artifact violates the frozen contract."""


class UnknownNodeError(LookupError):
    """The requested node is not present in the replayed event history."""


# --------------------------------------------------------------- configuration


def _toml_decimal(table: Mapping[str, Any], key: str, *, section: str) -> Decimal:
    raw = table.get(key)
    if not isinstance(raw, str):
        raise ScoringConfigError(f"[{section}].{key} must be a decimal string, got {raw!r}")
    try:
        return Decimal(raw)
    except ArithmeticError as error:
        raise ScoringConfigError(f"[{section}].{key} is not a decimal: {raw!r}") from error


def _toml_int(table: Mapping[str, Any], key: str, *, section: str) -> int:
    raw = table.get(key)
    if isinstance(raw, bool) or not isinstance(raw, int):
        raise ScoringConfigError(f"[{section}].{key} must be an integer, got {raw!r}")
    return raw


def _require_keys(table: Mapping[str, Any], expected: set[str], *, section: str) -> None:
    actual = set(table)
    if actual != expected:
        raise ScoringConfigError(
            f"[{section}] must contain exactly {sorted(expected)}, got {sorted(actual)}"
        )


@dataclass(frozen=True)
class ScoringConfig:
    """The frozen scoring-contract parameters, loaded from TOML.

    Every parameter is pinned by scoring contract 1.0.0; the loader fails
    closed on an unknown contract version, unknown keys, or values that
    violate the contract's internal consistency (e.g. P weights must sum to
    exactly 1).
    """

    contract_version: str
    half_life_days: Mapping[str, Decimal]
    p_weights: Mapping[str, Decimal]
    m_quality_gate: Decimal
    m_min_independence_groups: int
    e_cost_weights: Mapping[str, Decimal]
    e_baselines: Mapping[str, Decimal]
    e_cost_cap: Decimal
    level_thresholds: tuple[Decimal, ...]
    level_group_caps: tuple[tuple[int, int], ...]
    level_uncertainty_caps: tuple[tuple[Decimal, int], ...]
    promotion_quality_gate: Decimal
    promotion_min_groups: int
    promotion_max_u_p: Decimal
    g_exponents: Mapping[str, Decimal]

    @classmethod
    def default(cls) -> ScoringConfig:
        """Load the bundled ``scoring_config.toml`` (frozen contract 1.0.0)."""
        return cls.load(SCORING_CONFIG_PATH)

    @classmethod
    def load(cls, path: str | Path) -> ScoringConfig:
        data = tomllib.loads(Path(path).read_text(encoding="utf-8"))
        expected = {
            "meta",
            "half_life_days",
            "p_weights",
            "m_bin",
            "e_cost",
            "level",
            "promotion",
            "composite",
        }
        if set(data) != expected:
            raise ScoringConfigError(
                f"config must contain exactly {sorted(expected)}, got {sorted(set(data))}"
            )

        meta = data["meta"]
        _require_keys(meta, {"contract_version"}, section="meta")
        contract_version = str(meta["contract_version"])
        if contract_version != "1.0.0":
            raise ScoringConfigError(
                f"contract_version {contract_version!r} is not the frozen '1.0.0'"
            )

        half_lives = data["half_life_days"]
        _require_keys(half_lives, {"p", "m", "e"}, section="half_life_days")
        half_life_days = {d: _toml_decimal(half_lives, d, section="half_life_days") for d in _DIMENSIONS}
        if any(value <= 0 for value in half_life_days.values()):
            raise ScoringConfigError("half-lives must be positive")

        p_weights_raw = data["p_weights"]
        _require_keys(
            p_weights_raw,
            {"correctness", "outcome", "reproducibility", "scope_fit", "contradiction"},
            section="p_weights",
        )
        p_weights = {key: _toml_decimal(p_weights_raw, key, section="p_weights") for key in p_weights_raw}
        if sum(p_weights.values(), Decimal(0)) != Decimal(1):
            raise ScoringConfigError("P weights must sum to exactly 1")

        m_bin = data["m_bin"]
        _require_keys(m_bin, {"quality_gate", "min_independence_groups"}, section="m_bin")
        m_quality_gate = _toml_decimal(m_bin, "quality_gate", section="m_bin")
        m_min_groups = _toml_int(m_bin, "min_independence_groups", section="m_bin")

        e_cost = data["e_cost"]
        _require_keys(
            e_cost,
            {
                "latency_weight",
                "output_weight",
                "compute_weight",
                "latency_baseline_ms",
                "output_baseline_chars",
                "compute_baseline_ms",
                "cap",
            },
            section="e_cost",
        )
        e_cost_weights = {
            "latency": _toml_decimal(e_cost, "latency_weight", section="e_cost"),
            "output": _toml_decimal(e_cost, "output_weight", section="e_cost"),
            "compute": _toml_decimal(e_cost, "compute_weight", section="e_cost"),
        }
        if sum(e_cost_weights.values(), Decimal(0)) != Decimal(1):
            raise ScoringConfigError("E cost weights must sum to exactly 1")
        e_baselines = {
            "latency_ms": _toml_decimal(e_cost, "latency_baseline_ms", section="e_cost"),
            "output_chars": _toml_decimal(e_cost, "output_baseline_chars", section="e_cost"),
            "compute_ms": _toml_decimal(e_cost, "compute_baseline_ms", section="e_cost"),
        }
        e_cost_cap = _toml_decimal(e_cost, "cap", section="e_cost")

        level = data["level"]
        _require_keys(level, {"thresholds", "group_caps", "uncertainty_caps"}, section="level")
        thresholds = tuple(Decimal(str(raw)) for raw in level["thresholds"])
        if len(thresholds) != 4 or tuple(sorted(thresholds)) != thresholds:
            raise ScoringConfigError("[level].thresholds must be four ascending decimals")
        group_caps = tuple((int(pair[0]), int(pair[1])) for pair in level["group_caps"])
        uncertainty_caps = tuple((Decimal(str(pair[0])), int(pair[1])) for pair in level["uncertainty_caps"])

        promotion = data["promotion"]
        _require_keys(promotion, {"quality_gate", "min_independence_groups", "max_u_p"}, section="promotion")
        promotion_quality_gate = _toml_decimal(promotion, "quality_gate", section="promotion")
        promotion_min_groups = _toml_int(promotion, "min_independence_groups", section="promotion")
        promotion_max_u_p = _toml_decimal(promotion, "max_u_p", section="promotion")

        composite = data["composite"]
        _require_keys(composite, {"p_exponent", "m_exponent", "e_exponent"}, section="composite")
        g_exponents = {key: _toml_decimal(composite, key, section="composite") for key in composite}
        if sum(g_exponents.values(), Decimal(0)) != Decimal(1):
            raise ScoringConfigError("composite exponents must sum to exactly 1")

        return cls(
            contract_version=contract_version,
            half_life_days=half_life_days,
            p_weights=p_weights,
            m_quality_gate=m_quality_gate,
            m_min_independence_groups=m_min_groups,
            e_cost_weights=e_cost_weights,
            e_baselines=e_baselines,
            e_cost_cap=e_cost_cap,
            level_thresholds=thresholds,
            level_group_caps=group_caps,
            level_uncertainty_caps=uncertainty_caps,
            promotion_quality_gate=promotion_quality_gate,
            promotion_min_groups=promotion_min_groups,
            promotion_max_u_p=promotion_max_u_p,
            g_exponents=g_exponents,
        )


# ------------------------------------------------------------- result models


@dataclass(frozen=True)
class ZeroWeightStatus:
    """Whether a dimension scored against zero total eligible weight."""

    zero: bool
    reason: str | None  # "ZERO_TOTAL_WEIGHT" when zero


def serialize_score(value: Decimal) -> str:
    """The 12-decimal fixed-point serialization (spec section 9).

    Exact zero renders as ``0.000000000000``; the value is identical to the
    store boundary's ``str(quantize_score())`` rendering.
    """
    return format(quantize_score(value), "f")


@dataclass(frozen=True)
class ScoreAssessment:
    """The full-precision result of one scoring run.

    P, M and E remain separately visible (spec section 9); ``g`` is the
    optional gated composite and never replaces the three primary scores.
    Uncertainties are full precision; ``to_contract_dict()`` applies the
    12-decimal ``ROUND_HALF_EVEN`` serialization.
    """

    p: Decimal
    m: Decimal
    e: Decimal
    u_p: Decimal
    u_m: Decimal
    u_e: Decimal
    overall_uncertainty: Decimal
    professional_level: int
    selected_m_bin: str | None
    m_bin_evidence_count: int | None
    promotable: bool
    g: Decimal
    exclusion_counts: Mapping[str, int]
    zero_total_weight: Mapping[str, ZeroWeightStatus]
    explanations: Mapping[str, Any] = field(default_factory=dict)

    @property
    def confidence_p(self) -> Decimal:
        return 1 - self.u_p

    @property
    def confidence_m(self) -> Decimal:
        return 1 - self.u_m

    @property
    def confidence_e(self) -> Decimal:
        return 1 - self.u_e

    def to_contract_dict(self) -> dict[str, Any]:
        """The serialized assessment, shaped exactly like the frozen fixtures."""
        return {
            "p": serialize_score(self.p),
            "m": serialize_score(self.m),
            "e": serialize_score(self.e),
            "u_p": serialize_score(self.u_p),
            "u_m": serialize_score(self.u_m),
            "u_e": serialize_score(self.u_e),
            "overall_uncertainty": serialize_score(self.overall_uncertainty),
            "confidence_p": serialize_score(self.confidence_p),
            "confidence_m": serialize_score(self.confidence_m),
            "confidence_e": serialize_score(self.confidence_e),
            "selected_m_bin": self.selected_m_bin,
            "m_bin_evidence_count": self.m_bin_evidence_count,
            "professional_level": self.professional_level,
            "promotable": self.promotable,
            "g": serialize_score(self.g),
            "exclusion_counts": {key: int(self.exclusion_counts.get(key, 0)) for key in _EXCLUSION_KEYS},
            "zero_total_weight": {
                dimension: {
                    "zero": self.zero_total_weight[dimension].zero,
                    "reason": self.zero_total_weight[dimension].reason,
                }
                for dimension in _DIMENSIONS
            },
        }


# ------------------------------------------------------------- item plumbing


def evidence_item(evidence: Evidence) -> dict[str, Any]:
    """Adapt a domain :class:`Evidence` to the frozen evidence-schema mapping.

    Decimal fields are rendered with ``str`` (never via binary float), so the
    canonical content hash and every downstream computation stay decimal-pure.
    """
    return {
        "evidence_id": evidence.evidence_id,
        "node_id": evidence.node_id,
        "source_uri": evidence.source_uri,
        "observed_at": evidence.observed_at.isoformat(),
        "context_tags": list(evidence.context_tags),
        "independence_key": evidence.independence_key,
        "outcome": str(evidence.outcome),
        "correctness": str(evidence.correctness),
        "reliability": str(evidence.reliability),
        "reproducibility": str(evidence.reproducibility),
        "scope_fit": str(evidence.scope_fit),
        "successful_use": str(evidence.successful_use),
        "task_coverage": str(evidence.task_coverage),
        "quality_at_use": str(evidence.quality_at_use),
        "contradiction": str(evidence.contradiction),
        "utilization_u": str(evidence.utilization_u),
        "latency_ms": evidence.latency_ms,
        "compute_ms": evidence.compute_ms,
        "output_chars": evidence.output_chars,
    }


def _microseconds(delta: Any) -> int:
    return (delta.days * 86_400 + delta.seconds) * 1_000_000 + delta.microseconds


def _jaccard(query_tags: frozenset[str], evidence_tags: frozenset[str]) -> Decimal:
    union = query_tags | evidence_tags
    if not union:
        return Decimal(1)
    intersection = query_tags & evidence_tags
    return Decimal(len(intersection)) / Decimal(len(union))


def _classify(item: Mapping[str, Any], at: datetime, query_tags: frozenset[str]) -> str | None:
    """Return the exclusion-count key, or ``None`` when the item is eligible.

    Precedence (evidence schema 1.0.0, "Validity and exclusion classes"):
    missing provenance, then invalid fields, then zero reliability, then
    disjoint/out-of-context.
    """
    if not item.get("source_uri"):
        return "missing_provenance"
    try:
        outcome = Decimal(str(item["outcome"]))
        if not (Decimal(-1) <= outcome <= Decimal(1)):
            return "other_exclusions"
        for field_name in (
            "correctness",
            "reliability",
            "reproducibility",
            "scope_fit",
            "successful_use",
            "quality_at_use",
            "contradiction",
        ):
            value = Decimal(str(item[field_name]))
            if not (Decimal(0) <= value <= Decimal(1)):
                return "other_exclusions"
        if item["task_coverage"] is None:
            return "other_exclusions"  # empty rubric denominator
        coverage = Decimal(str(item["task_coverage"]))
        if not (Decimal(0) <= coverage <= Decimal(1)):
            return "other_exclusions"
        utilization = Decimal(str(item["utilization_u"]))
        if utilization not in _VALID_UTILIZATION_BINS:
            return "other_exclusions"
        if not int(item["latency_ms"]) > 0:
            return "other_exclusions"
        if not int(item["compute_ms"]) > 0:
            return "other_exclusions"
        if not int(item["output_chars"]) >= 0:
            return "other_exclusions"
        observed = datetime.fromisoformat(str(item["observed_at"]))
        if _microseconds(at - observed) < 0:
            return "other_exclusions"  # evidence from the future is invalid
    except Exception:
        return "other_exclusions"
    if Decimal(str(item["reliability"])) == 0:
        return "zero_reliability"
    if query_tags and _jaccard(query_tags, frozenset(str(tag) for tag in item["context_tags"])) == 0:
        return "disjoint_context"
    return None


def _dedup_key(item: Mapping[str, Any]) -> str:
    """Canonical content hash over all fields except ``evidence_id``."""
    content = {key: value for key, value in item.items() if key != "evidence_id"}
    return hashlib.sha256(canonical_json(content).encode("utf-8")).hexdigest()


def _dedup(items: Iterable[Mapping[str, Any]]) -> list[Mapping[str, Any]]:
    """Exact duplicates are represented once (smallest UTF-8 evidence_id);
    the retained items are sorted by ascending UTF-8 evidence_id."""
    retained: dict[str, Mapping[str, Any]] = {}
    for item in items:
        key = _dedup_key(item)
        current = retained.get(key)
        if current is None or str(item["evidence_id"]) < str(current["evidence_id"]):
            retained[key] = item
    return sorted(retained.values(), key=lambda entry: str(entry["evidence_id"]).encode("utf-8"))


# ------------------------------------------------------------------- scoring


@dataclass(frozen=True)
class _Enriched:
    item: Mapping[str, Any]
    q: Mapping[str, Decimal]
    p_j: Decimal
    e_j: Decimal


def _weighted_mean(enriched: Sequence[_Enriched], dimension: str, term: str, total: Decimal) -> Decimal:
    return sum(
        ((entry.q[dimension] / total) * getattr(entry, term) for entry in enriched),
        Decimal(0),
    )


def score_items(
    items: Sequence[Mapping[str, Any]],
    *,
    at: datetime,
    query_context_tags: Iterable[str] = (),
    node_depths: Mapping[str, int] | None = None,
    config: ScoringConfig | None = None,
) -> ScoreAssessment:
    """Score a set of frozen-schema evidence items at time ``at``.

    This is the pure core: deterministic, store-free, and replay-safe. All
    Decimal arithmetic runs at precision 34 inside one local context.
    """
    cfg = config if config is not None else ScoringConfig.default()
    depths = node_depths if node_depths is not None else {}
    with localcontext() as context:
        context.prec = PRECISION
        query_tags = frozenset(str(tag) for tag in query_context_tags)

        counts = {key: 0 for key in _EXCLUSION_KEYS}
        eligible: list[Mapping[str, Any]] = []
        for item in items:
            exclusion = _classify(item, at, query_tags)
            if exclusion is None:
                eligible.append(item)
            else:
                counts[exclusion] += 1

        deduped = _dedup(eligible)
        group_sizes = Counter(str(item["independence_key"]) for item in deduped)
        ln2 = Decimal(2).ln()

        enriched: list[_Enriched] = []
        for item in deduped:
            observed = datetime.fromisoformat(str(item["observed_at"]))
            age_days = Decimal(_microseconds(at - observed)) / MICROSECONDS_PER_DAY
            independence = Decimal(1) / Decimal(group_sizes[str(item["independence_key"])]).sqrt()
            relevance = (
                Decimal(1)
                if not query_tags
                else _jaccard(query_tags, frozenset(str(tag) for tag in item["context_tags"]))
            )
            depth = Decimal(int(depths.get(str(item["node_id"]), 0)))
            reliability = Decimal(str(item["reliability"]))
            q = {
                dimension: reliability
                * (-(ln2) * age_days / cfg.half_life_days[dimension]).exp()
                * relevance
                * independence
                / (1 + depth)
                for dimension in _DIMENSIONS
            }
            p_j = (
                cfg.p_weights["correctness"] * Decimal(str(item["correctness"]))
                + cfg.p_weights["outcome"] * ((Decimal(str(item["outcome"])) + 1) / 2)
                + cfg.p_weights["reproducibility"] * Decimal(str(item["reproducibility"]))
                + cfg.p_weights["scope_fit"] * Decimal(str(item["scope_fit"]))
                + cfg.p_weights["contradiction"] * (1 - Decimal(str(item["contradiction"])))
            )
            benefit = Decimal(str(item["successful_use"])) * Decimal(str(item["task_coverage"]))
            cost = (
                cfg.e_cost_weights["latency"]
                * min(cfg.e_cost_cap, Decimal(int(item["latency_ms"])) / cfg.e_baselines["latency_ms"])
                + cfg.e_cost_weights["output"]
                * min(cfg.e_cost_cap, Decimal(int(item["output_chars"])) / cfg.e_baselines["output_chars"])
                + cfg.e_cost_weights["compute"]
                * min(cfg.e_cost_cap, Decimal(int(item["compute_ms"])) / cfg.e_baselines["compute_ms"])
            )
            e_j = Decimal(0) if benefit + cost == 0 else benefit / (benefit + cost)
            enriched.append(_Enriched(item=item, q=q, p_j=p_j, e_j=e_j))

        scores: dict[str, Decimal] = {}
        totals: dict[str, Decimal] = {}
        uncertainties: dict[str, Decimal] = {}
        zero_total_weight: dict[str, ZeroWeightStatus] = {}

        for dimension, term in (("p", "p_j"), ("e", "e_j")):
            total = sum((entry.q[dimension] for entry in enriched), Decimal(0))
            totals[dimension] = total
            if total == 0:
                scores[dimension] = Decimal(0)
                uncertainties[dimension] = Decimal(1)
                zero_total_weight[dimension] = ZeroWeightStatus(zero=True, reason="ZERO_TOTAL_WEIGHT")
            else:
                scores[dimension] = _weighted_mean(enriched, dimension, term, total)
                uncertainties[dimension] = Decimal(1) / (1 + total).sqrt()
                zero_total_weight[dimension] = ZeroWeightStatus(zero=False, reason=None)

        # M: maximum effective utilization under the quality gate.
        total_m = sum((entry.q["m"] for entry in enriched), Decimal(0))
        totals["m"] = total_m
        bins: dict[Decimal, list[_Enriched]] = defaultdict(list)
        for entry in enriched:
            if entry.q["m"] > 0:
                bins[Decimal(str(entry.item["utilization_u"]))].append(entry)

        bin_reports: list[dict[str, Any]] = []
        best: tuple[Decimal, Decimal, list[str], int] | None = None
        for utilization in sorted(bins):
            members = bins[utilization]
            local_total = sum((entry.q["m"] for entry in members), Decimal(0))
            if local_total == 0:
                continue
            groups = len({str(entry.item["independence_key"]) for entry in members})
            quality = sum(
                ((entry.q["m"] / local_total) * Decimal(str(entry.item["quality_at_use"])) for entry in members),
                Decimal(0),
            )
            success = sum(
                (
                    (entry.q["m"] / local_total)
                    * Decimal(str(entry.item["successful_use"]))
                    * Decimal(str(entry.item["task_coverage"]))
                    for entry in members
                ),
                Decimal(0),
            )
            eligible_bin = groups >= cfg.m_min_independence_groups and quality >= cfg.m_quality_gate
            value = utilization * success
            id_list = sorted(str(entry.item["evidence_id"]) for entry in members)
            bin_reports.append(
                {
                    "utilization": format(utilization, ".1f"),
                    "evidence_count": len(members),
                    "independence_groups": groups,
                    "quality_at_use": serialize_score(quality),
                    "success_coverage": serialize_score(success),
                    "value": serialize_score(value),
                    "eligible": eligible_bin,
                }
            )
            if not eligible_bin:
                continue
            if (
                best is None
                or value > best[0]
                or (value == best[0] and (utilization < best[1] or (utilization == best[1] and id_list < best[2])))
            ):
                best = (value, utilization, id_list, len(members))

        if total_m == 0:
            scores["m"] = Decimal(0)
            uncertainties["m"] = Decimal(1)
            zero_total_weight["m"] = ZeroWeightStatus(zero=True, reason="ZERO_TOTAL_WEIGHT")
            selected_m_bin: str | None = None
            m_bin_evidence_count: int | None = None
        else:
            uncertainties["m"] = Decimal(1) / (1 + total_m).sqrt()
            zero_total_weight["m"] = ZeroWeightStatus(zero=False, reason=None)
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

        if zero_total_weight["p"].zero:
            professional_level = 1
            groups_p = 0
        else:
            groups_p = len({str(entry.item["independence_key"]) for entry in enriched if entry.q["p"] > 0})
            level = 1 + sum(p_score >= threshold for threshold in cfg.level_thresholds)
            for minimum_groups, cap in cfg.level_group_caps:
                if groups_p < minimum_groups:
                    level = min(level, cap)
                    break
            for u_threshold, cap in cfg.level_uncertainty_caps:
                if uncertainties["p"] > u_threshold:
                    level = min(level, cap)
            professional_level = level

        promotable = bool(
            not zero_total_weight["p"].zero
            and p_score >= cfg.promotion_quality_gate
            and groups_p >= cfg.promotion_min_groups
            and uncertainties["p"] <= cfg.promotion_max_u_p
        )

        if (
            p_score < cfg.promotion_quality_gate
            or p_score == 0
            or m_score == 0
            or e_score == 0
        ):
            g_score = Decimal(0)
        else:
            g_score = (
                p_score ** cfg.g_exponents["p_exponent"]
                * m_score ** cfg.g_exponents["m_exponent"]
                * e_score ** cfg.g_exponents["e_exponent"]
            )

        overall_uncertainty = max(uncertainties.values())

        explanations = _build_explanations(
            enriched,
            totals=totals,
            bin_reports=bin_reports,
            selected_m_bin=selected_m_bin,
            exclusion_counts=counts,
        )

        return ScoreAssessment(
            p=p_score,
            m=m_score,
            e=e_score,
            u_p=uncertainties["p"],
            u_m=uncertainties["m"],
            u_e=uncertainties["e"],
            overall_uncertainty=overall_uncertainty,
            professional_level=professional_level,
            selected_m_bin=selected_m_bin,
            m_bin_evidence_count=m_bin_evidence_count,
            promotable=promotable,
            g=g_score,
            exclusion_counts=counts,
            zero_total_weight=zero_total_weight,
            explanations=explanations,
        )


def _build_explanations(
    enriched: Sequence[_Enriched],
    *,
    totals: Mapping[str, Decimal],
    bin_reports: Sequence[Mapping[str, Any]],
    selected_m_bin: str | None,
    exclusion_counts: Mapping[str, int],
) -> dict[str, Any]:
    """Evidence-weight explanations: per-dimension weights in canonical order."""
    return {
        "p": _dimension_explanation(enriched, "p", totals["p"], "p_j"),
        "e": _dimension_explanation(enriched, "e", totals["e"], "e_j"),
        "m": {
            "total_weight": serialize_score(totals["m"]),
            "bins": list(bin_reports),
            "selected_m_bin": selected_m_bin,
        },
        "excluded": {key: int(exclusion_counts.get(key, 0)) for key in _EXCLUSION_KEYS},
    }


def _dimension_explanation(
    enriched: Sequence[_Enriched], dimension: str, total: Decimal, term: str
) -> dict[str, Any]:
    entries = []
    for entry in enriched:
        value = getattr(entry, term)
        weight = entry.q[dimension] / total if total != 0 else Decimal(0)
        entries.append(
            {
                "evidence_id": str(entry.item["evidence_id"]),
                "weight": serialize_score(weight),
                "term": serialize_score(value),
                "contribution": serialize_score(weight * value),
            }
        )
    return {"total_weight": serialize_score(total), "evidence": entries}


# ------------------------------------------------------------------ the engine


class ScoreEngine:
    """Read-only scoring over the event store.

    Evidence and relations are collected by replaying the immutable domain
    history (``EventStore.replay_domain``), so scoring sees exactly the same
    canonical order as every other replay consumer. The engine never writes.
    """

    def __init__(self, store: EventStore, *, config: ScoringConfig | None = None) -> None:
        self._store = store
        self._config = config if config is not None else ScoringConfig.default()

    @property
    def config(self) -> ScoringConfig:
        return self._config

    def score_node(
        self,
        node_id: str,
        context: Iterable[str] = (),
        at: datetime | None = None,
    ) -> ScoreAssessment:
        """Score one node's own evidence (depth 0) at ``at`` (default: now)."""
        moment = at if at is not None else datetime.now(UTC)
        snapshot = self._snapshot()
        if node_id not in snapshot[0]:
            raise UnknownNodeError(f"unknown node {node_id!r}")
        items = [evidence_item(ev) for ev in snapshot[1].get(node_id, ())]
        return score_items(
            items,
            at=moment,
            query_context_tags=context,
            node_depths={node_id: 0},
            config=self._config,
        )

    def aggregate_primary(
        self,
        primary_id: str,
        context: Iterable[str] = (),
        at: datetime | None = None,
    ) -> ScoreAssessment:
        """Recompute the same formulas over the primary plus its descendants.

        The union of the primary node's own evidence and descendant evidence
        is deduplicated by ``evidence_id`` (dedup retains one canonical item)
        and weighted with the depth factor ``1/(1+depth)``, where depth is the
        BFS distance from the primary along ``parent_of`` edges. Child scores
        are never averaged directly (contract, "Aggregation").
        """
        moment = at if at is not None else datetime.now(UTC)
        known_nodes, evidence_by_node, children = self._snapshot()
        if primary_id not in known_nodes:
            raise UnknownNodeError(f"unknown node {primary_id!r}")

        node_depths: dict[str, int] = {primary_id: 0}
        frontier = [primary_id]
        while frontier:
            following: list[str] = []
            for current in frontier:
                for child in sorted(children.get(current, ())):
                    if child not in node_depths:
                        node_depths[child] = node_depths[current] + 1
                        following.append(child)
            frontier = following

        items = [
            evidence_item(ev)
            for depth_node in sorted(node_depths)
            for ev in evidence_by_node.get(depth_node, ())
        ]
        return score_items(
            items,
            at=moment,
            query_context_tags=context,
            node_depths=node_depths,
            config=self._config,
        )

    def _snapshot(
        self,
    ) -> tuple[set[str], dict[str, list[Evidence]], dict[str, set[str]]]:
        """Replay the domain history once into nodes/evidence/parent-of edges."""
        known_nodes: set[str] = set()
        evidence_by_node: dict[str, list[Evidence]] = defaultdict(list)
        children: dict[str, set[str]] = defaultdict(set)
        for event in self._store.replay_domain():
            if event.node is not None:
                known_nodes.add(event.node.node_id)
            if event.evidence is not None:
                known_nodes.add(event.evidence.node_id)
                evidence_by_node[event.evidence.node_id].append(event.evidence)
            if event.relation is not None and event.relation.kind is RelationKind.PARENT_OF:
                known_nodes.add(event.relation.source_node_id)
                known_nodes.add(event.relation.target_node_id)
                children[event.relation.source_node_id].add(event.relation.target_node_id)
        return known_nodes, evidence_by_node, children

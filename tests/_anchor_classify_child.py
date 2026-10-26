"""Cross-seed classification probe for ``tests/test_sub47_wave2_fixes.py``.

Not a test module. Spawned as a real subprocess under a chosen
``PYTHONHASHSEED``; it builds the ambiguous-anchor relearn scenario
(F-SUB45-1: manifest paths ``{"b.md", "a/b.md"}`` with evidence
``source_uri="ext/a/b.md"`` suffix-matching BOTH) and prints the ACCUMULATE
classification as canonical JSON on stdout. The parent test compares the
output of ``preview`` and ``apply`` across several hash seeds: before the
fix the sets flip with the seed, so preview-in-one-process can disagree
with apply-in-another; after the fix every process prints the same bytes.

Usage: ``python tests/_anchor_classify_child.py <workdir> <preview|apply>``
"""

from __future__ import annotations

import json
import sys
from datetime import UTC, datetime
from pathlib import Path

NOW = datetime(2026, 10, 25, 12, 0, tzinfo=UTC)

# (evidence_id, source_uri): e-carry suffix-matches only "b.md"; e-ambig
# suffix-matches BOTH "b.md" and "a/b.md" (the F-SUB45-1 ambiguity);
# e-exact is an exact manifest member. Only "a/b.md" changes in v2, so the
# carried/quarantined split of e-ambig is decided entirely by the anchor.
EVIDENCE = (
    ("e-carry", "ext/b.md"),
    ("e-ambig", "ext/a/b.md"),
    ("e-exact", "a/b.md"),
)


def main() -> int:
    workdir = Path(sys.argv[1])
    phase = sys.argv[2]

    from know_your_soul.domain import (
        Event,
        EventKind,
        Evidence,
        Node,
        NodeKind,
        PrimaryDomain,
        ScoreVector,
    )
    from know_your_soul.relearn import RelearnApproval, RelearnEngine
    from know_your_soul.source_registry import ParserVersions, SourceKind, SourceRegistry
    from know_your_soul.store import EventStore

    parser_versions = ParserVersions(
        parser_version="1.0.0",
        normalization_version="1.0.0",
        engine_version="0.9.95",
        scoring_contract_version="1.0.0",
    )
    corpus = workdir / "corpus"
    (corpus / "a").mkdir(parents=True)
    (corpus / "b.md").write_text("top-level b v1\n", encoding="utf-8")
    (corpus / "a" / "b.md").write_text("nested a/b v1\n", encoding="utf-8")

    store = EventStore(workdir / "soul.db")
    registry = SourceRegistry(store)
    v1 = registry.observe(corpus, SourceKind.SOUL_CORPUS, parser_versions)
    node = Node.model_validate(
        {
            "node_id": "n-1",
            "kind": NodeKind.LEAF,
            "domain": PrimaryDomain.P1,
            "source_version_id": v1.source_version_id,
            "payload": {"fact": "n-1"},
            "summary": "node n-1",
            "scores": ScoreVector.model_validate(
                {"p": "0.5", "m": "0.4", "e": "0.3", "uncertainty": "0.4", "confidence": "0.6"}
            ),
            "created_at": NOW,
            "updated_at": NOW,
            "version": 1,
        }
    )
    store.append_domain(
        Event(
            kind=EventKind.NODE_UPSERT,
            node_id="n-1",
            domain=PrimaryDomain.P1,
            node=node,
            source_version_id=v1.source_version_id,
            occurred_at=NOW,
        ),
        idempotency_key="seed:n-1",
    )
    for evidence_id, source_uri in EVIDENCE:
        evidence = Evidence.model_validate(
            {
                "evidence_id": evidence_id,
                "node_id": "n-1",
                "source_version_id": v1.source_version_id,
                "source_uri": source_uri,
                "observed_at": NOW,
                "independence_key": f"grp-{evidence_id}",
                "outcome": "0.5",
                "correctness": "0.9",
                "reliability": "0.8",
                "reproducibility": "0.7",
                "scope_fit": "0.6",
                "successful_use": "0.5",
                "task_coverage": "0.4",
                "quality_at_use": "0.75",
                "contradiction": "0.1",
                "utilization_u": "0.5",
                "latency_ms": 1000,
                "compute_ms": 100,
                "output_chars": 500,
            }
        )
        store.append_domain(
            Event(
                kind=EventKind.EVIDENCE_APPEND,
                node_id="n-1",
                domain=PrimaryDomain.P1,
                evidence=evidence,
                source_version_id=v1.source_version_id,
                occurred_at=NOW,
            ),
            idempotency_key=f"seed:{evidence_id}",
        )

    engine = RelearnEngine(store, registry, backup_root=workdir / "backups", scoring_contract_version="1.0.0")
    # v2: only the nested file changes, so the unchanged-anchor set is {"b.md"}.
    (corpus / "a" / "b.md").write_text("nested a/b v2 changed\n", encoding="utf-8")
    offer = engine.offer(corpus, SourceKind.SOUL_CORPUS, parser_versions)
    assert offer is not None

    if phase == "preview":
        preview = engine.preview("ACCUMULATE")
        result = {"carried": list(preview.carried_evidence_ids), "quarantined": list(preview.quarantined_evidence_ids)}
    elif phase == "apply":
        approval = RelearnApproval(offer_id=offer.offer_id, mode="ACCUMULATE", approved_by="operator", decided_at=NOW)
        receipt = engine.apply("ACCUMULATE", approval)
        result = {"carried": list(receipt.carried_evidence_ids), "quarantined": list(receipt.quarantined_evidence_ids)}
    else:  # pragma: no cover - usage guard
        print(f"unknown phase {phase!r}", file=sys.stderr)
        return 2

    sys.stdout.write(json.dumps(result, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

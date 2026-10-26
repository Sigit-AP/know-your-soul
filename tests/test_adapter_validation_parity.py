"""Cross-validation parity guard for the two adapter validators (arch review F-2).

The HTTP adapter validates wire payloads with pydantic models
(``http_api._OPERATIONS[op].parse``); the MCP adapter validates with its JSON
schemas plus the ``_op_*`` request builders. Two mechanisms, one contract —
kept in lockstep not by a mirror comment but by this contract test.

**Invariant:** for every probe over the *shared semantic input space* of a
knowledge operation, both adapters make the SAME accept/reject decision. The
shared space excludes transport-only fields (``idempotency_key`` on both
sides, ``expected_version`` MCP-only) and maps the two structural wrappings
(``log_event``/``record_outcome`` nest the model under ``node``/``evidence``
on MCP; ``apply_improvement``'s approval injects ``proposal_id`` on MCP).

**Pinned intentional divergences** (``_PINNED``): each entry pins the exact
decision pair so a silent spread or flip fails the build. Justifications:

* D1 ``token_budget`` bounds — MCP schemas reject outside [128, max] to guide
  LLM clients early; the HTTP adapter passes the value to the service, which
  rejects typed (``BUDGET_TOO_SMALL``/``BUDGET_EXCEEDS_MAX``, frozen codes).
  Both reject the invocation overall (proven end-to-end below).
* D2 length caps (node_id/domain/tag/rationale maxLength) — MCP schema caps
  guide clients; the HTTP side's frozen contract models cap only what the
  spec caps, and overlong well-formed values fail typed downstream
  (``NODE_NOT_FOUND`` etc.).
* D3 ``at`` / ``include_due`` — the deterministic-clock and due-scan knobs
  are HTTP/CLI-only; the MCP tool schemas deliberately do not expose them
  (unknown property).
* D4 approval shape — MCP takes ``{approved_by, decision, decided_at?}`` and
  injects the outer ``proposal_id``; HTTP takes a complete ``ApprovalRecord``
  (which requires ``proposal_id`` and ``decided_at``). Semantically
  equivalent under the mapping; pinned in both directions.

Decision record: docs/recovery/arch-review-F2-adapter-validation-parity.md.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest
from pydantic import BaseModel, ConfigDict

from soul_knowledge import http_api, mcp_server
from soul_knowledge.contracts import KnowledgeError
from soul_knowledge.domain import PrimaryDomain, RelationKind
from soul_knowledge.http_api import build_stack, dispatch_text
from soul_knowledge.importer import SeedImporter
from soul_knowledge.improvement import DiffOp

# --------------------------------------------------------------------- harness


class _StubResult(BaseModel):
    """Stand-in service result satisfying both response wrappers."""

    model_config = ConfigDict(extra="allow")

    def canonical_payload(self) -> dict[str, Any]:
        return {}


class _StubService:
    """Records that a call reached the service; validation is what we probe."""

    def __getattr__(self, name: str) -> Any:
        return lambda *args, **kwargs: _StubResult()


def _mcp_adapter() -> mcp_server._Adapter:
    return mcp_server._Adapter(
        _StubService(),  # type: ignore[arg-type]
        graph_root=Path("."),
        audit_log=None,  # type: ignore[arg-type] - validation paths never audit
        actor="parity-probe",
        max_token_budget=8192,
        resource_max_bytes=65536,
    )


def http_accepts(op: str, payload: Any) -> bool:
    """The HTTP adapter's decision on a raw JSON payload (parse only)."""
    try:
        http_api._OPERATIONS[op].parse(json.dumps(payload).encode("utf-8"))
    except Exception:
        return False
    return True


def mcp_accepts(op: str, args: Any) -> bool:
    """The MCP adapter's decision: schema validation plus the request builder."""
    adapter = _mcp_adapter()
    tool = f"knowledge_{op}"
    try:
        validated = mcp_server._validate_arguments(tool, adapter._schemas[tool], args)
        adapter._dispatch(tool, validated)
    except Exception:
        return False
    return True


def _to_mcp_args(op: str, probe: Any) -> Any:
    """Render a shared-space probe in the MCP shape (structural mapping rule)."""
    if not isinstance(probe, dict):
        return probe
    if op == "log_event":
        return {"node": probe}
    if op == "record_outcome":
        return {"evidence": probe}
    if op == "apply_improvement" and isinstance(probe.get("approval"), dict):
        approval = {key: value for key, value in probe["approval"].items() if key != "proposal_id"}
        return {**probe, "approval": approval}
    return dict(probe)


# ------------------------------------------------------------------ probe space

VALID_NODE: dict[str, Any] = {"node_id": "n-1", "domain": "P1", "source_version_id": "sv-1"}
VALID_EVIDENCE: dict[str, Any] = {
    "evidence_id": "e-1",
    "node_id": "n-1",
    "source_version_id": "sv-1",
    "source_uri": "corpus/alpha.md",
    "observed_at": "2026-10-26T12:00:00+00:00",
    "independence_key": "ik-1",
    "outcome": "0.5",
    "correctness": "0.5",
    "reliability": "0.5",
    "reproducibility": "0.5",
    "scope_fit": "0.5",
    "successful_use": "0.5",
    "task_coverage": "0.5",
    "quality_at_use": "0.5",
    "contradiction": "0.0",
    "utilization_u": "0.5",
    "latency_ms": 10,
    "compute_ms": 10,
    "output_chars": 10,
}
VALID_DIFF = [{"op": "set_summary", "value": "x"}]
VALID_APPROVAL = {
    "proposal_id": "p-1",
    "approved_by": "op",
    "decision": "approved",
    "decided_at": "2026-10-26T12:00:00+00:00",
}

BASE: dict[str, dict[str, Any]] = {
    "query": {"purpose": "p", "token_budget": 512},
    "get_node": {"node_id": "n-1"},
    "get_subtree": {"node_id": "n-1"},
    "log_event": dict(VALID_NODE),
    "record_outcome": dict(VALID_EVIDENCE),
    "evaluate": {"node_id": "n-1"},
    "review": {"node_id": "n-1"},
    "propose_improvement": {"node_id": "n-1", "diff": VALID_DIFF, "rationale": "r"},
    "apply_improvement": {"proposal_id": "p-1"},
    "rollback": {"node_id": "n-1"},
    "path": {"from_id": "a", "to_id": "b"},
    "explain": {"node_id": "n-1"},
    "affected": {"node_id": "n-1"},
    "god_nodes": {},
}

# (op, probe-name, probe-payload). Shared semantic space only.
PROBES: list[tuple[str, str, Any]] = []


def _add(op: str, name: str, **overlay: Any) -> None:
    PROBES.append((op, name, {**BASE[op], **overlay}))


for _op in BASE:
    PROBES.append((_op, "valid-base", dict(BASE[_op])))
    PROBES.append((_op, "unknown-extra", {**BASE[_op], "zzz_unknown": 1}))

# Type coercion edges: both adapters must reject (strict JSON typing).
_add("query", "purpose-int", purpose=42)
_add("query", "purpose-empty", purpose="")
_add("query", "token_budget-numstr", token_budget="512")
_add("query", "token_budget-bool", token_budget=True)
_add("query", "access_only-str", access_only="true")
_add("query", "depth-numstr", depth="2")
_add("query", "depth-zero", depth=0)
_add("query", "depth-over", depth=7)
_add("query", "mode-bad-enum", mode="random")
_add("query", "mode-null", mode=None)
_add("query", "relation-bad-member", relation_allowlist=["nonsense"])
_add("query", "relation-null-ok", relation_allowlist=None)
_add("query", "context_tags-item-int", context_tags=[42])
_add("query", "max_nodes-zero", max_nodes=0)
_add("query", "max_edges-over", max_edges=4097)
_add("get_node", "node_id-int", node_id=42)
_add("get_node", "node_id-empty", node_id="")
_add("get_node", "node_id-null", node_id=None)
_add("get_subtree", "depth-numstr", depth="2")
_add("get_subtree", "depth-bool", depth=True)
_add("get_subtree", "depth-zero", depth=0)
_add("get_subtree", "depth-over", depth=7)
_add("get_subtree", "depth-null", depth=None)
_add("get_subtree", "mode-bad-enum", mode="random")
_add("get_subtree", "mode-null", mode=None)
_add("get_subtree", "relation-bad-member", relation_allowlist=["nonsense"])
_add("get_subtree", "max_nodes-numstr", max_nodes="10")
_add("get_subtree", "max_nodes-over", max_nodes=1025)
_add("get_subtree", "max_edges-zero", max_edges=0)
_add("evaluate", "at-garbage", at="not-a-date")
_add("evaluate", "at-space-separator", at="2026-10-26 12:00:00+00:00")
_add("evaluate", "at-int", at=5)
_add("evaluate", "include_descendants-str", include_descendants="yes")
_add("review", "include_due-int", include_due=1)
_add("propose_improvement", "rationale-empty", rationale="")
_add("propose_improvement", "base_version-zero", base_version=0)
_add("propose_improvement", "base_version-numstr", base_version="2")
_add("propose_improvement", "diff-empty", diff=[])
_add("propose_improvement", "diff-missing-op", diff=[{"key": "k"}])
_add("propose_improvement", "diff-bad-op", diff=[{"op": "explode"}])
_add("propose_improvement", "diff-extra-key", diff=[{"op": "set_summary", "value": "x", "bogus": 1}])
_add("propose_improvement", "diff-key-int", diff=[{"op": "set_summary", "key": 5, "value": "x"}])
_add("propose_improvement", "diff-not-list", diff={"op": "set_summary"})
_add("apply_improvement", "approval-bad-decision", approval={**VALID_APPROVAL, "decision": "maybe"})
_add("apply_improvement", "approval-empty-approved-by", approval={**VALID_APPROVAL, "approved_by": ""})
_add("apply_improvement", "approval-null-ok", approval=None)
_add("rollback", "both-targets", node_id="n-1", relearn_id="r-1")
_add("rollback", "relearn-with-version", node_id=None, relearn_id="r-1", to_version=2)
_add("rollback", "relearn-target-ok", node_id=None, relearn_id="r-1")
_add("rollback", "to_version-zero", to_version=0)
_add("path", "from_id-int", from_id=42)
_add("path", "token_budget-numstr", token_budget="512")
_add("explain", "token_budget-bool", token_budget=True)
_add("affected", "depth-numstr", depth="3")
_add("affected", "depth-over", depth=7)
_add("affected", "max_nodes-bool", max_nodes=False)
_add("god_nodes", "limit-numstr", limit="5")
_add("god_nodes", "limit-zero", limit=0)
_add("god_nodes", "limit-over", limit=65)
# Whole-payload shape probes.
PROBES.append(("query", "payload-not-object", [1, 2]))
PROBES.append(("get_node", "payload-not-object", "n-1"))
PROBES.append(("god_nodes", "empty-object", {}))
PROBES.append(("query", "empty-object-missing-required", {}))
PROBES.append(("log_event", "node-not-object", "x"))
PROBES.append(("record_outcome", "evidence-not-object", "x"))
# Nested model probes (log_event/record_outcome share one model on both sides).
for _name, _node in [
    ("node-missing-domain", {"node_id": "n-1", "source_version_id": "sv-1"}),
    ("node-bad-domain", {**VALID_NODE, "domain": "P9"}),
    ("node-extra-key", {**VALID_NODE, "bogus": 1}),
    ("node-version-zero", {**VALID_NODE, "version": 0}),
    ("node-kind-bad", {**VALID_NODE, "kind": "weird"}),
]:
    PROBES.append(("log_event", _name, _node))
for _name, _evidence in [
    ("evidence-bad-decimal", {**VALID_EVIDENCE, "outcome": "abc"}),
    ("evidence-decimal-out-of-range", {**VALID_EVIDENCE, "outcome": "1.5"}),
    ("evidence-latency-zero", {**VALID_EVIDENCE, "latency_ms": 0}),
    ("evidence-naive-observed-at", {**VALID_EVIDENCE, "observed_at": "2026-10-26T12:00:00"}),
    ("evidence-utilization-off-bin", {**VALID_EVIDENCE, "utilization_u": "0.55"}),
    ("evidence-extra-key", {**VALID_EVIDENCE, "bogus": 1}),
]:
    PROBES.append(("record_outcome", _name, _evidence))

# ------------------------------------------------- pinned intentional divergences
# (op, probe-name) -> (http adapter accepts, mcp adapter accepts, family)

_PINNED: dict[tuple[str, str], tuple[bool, bool, str]] = {}
for _op in ("query", "get_subtree", "path", "explain", "affected", "god_nodes"):
    _PINNED[(_op, "budget-below-min")] = (True, False, "D1")
    _PINNED[(_op, "budget-over-max")] = (True, False, "D1")
    PROBES.append((_op, "budget-below-min", {**BASE[_op], "token_budget": 64}))
    PROBES.append((_op, "budget-over-max", {**BASE[_op], "token_budget": 99999999}))
for _op, _field in [("query", "domain"), ("get_node", "node_id"), ("get_subtree", "node_id")]:
    _overlong: Any = {_field: "x" * 300}
    _PINNED[(_op, f"{_field}-overlong")] = (True, False, "D2")
    PROBES.append((_op, f"{_field}-overlong", {**BASE[_op], **_overlong}))
_PINNED[("query", "context_tags-item-overlong")] = (True, False, "D2")
PROBES.append(("query", "context_tags-item-overlong", {**BASE["query"], "context_tags": ["x" * 257]}))
_PINNED[("propose_improvement", "rationale-overlong")] = (True, False, "D2")
PROBES.append(("propose_improvement", "rationale-overlong", {**BASE["propose_improvement"], "rationale": "r" * 1025}))
for _op in ("review", "propose_improvement", "apply_improvement", "rollback"):
    _PINNED[(_op, "at-http-only")] = (True, False, "D3")
    PROBES.append((_op, "at-http-only", {**BASE[_op], "at": "2026-10-26T12:00:00+00:00"}))
_PINNED[("review", "include_due-http-only")] = (True, False, "D3")
PROBES.append(("review", "include_due-http-only", {**BASE["review"], "include_due": True}))
# D4: the MCP-minimal approval shape (no proposal_id/decided_at) is rejected
# by the HTTP side, which requires a complete ApprovalRecord.
_PINNED[("apply_improvement", "approval-mcp-minimal-shape")] = (False, True, "D4")
PROBES.append(
    (
        "apply_improvement",
        "approval-mcp-minimal-shape",
        {"proposal_id": "p-1", "approval": {"approved_by": "op", "decision": "approved"}},
    )
)
# Under the mapping rule (MCP injects the outer proposal_id) the full approval
# shape is ordinary parity — both adapters accept it.
PROBES.append(
    (
        "apply_improvement",
        "approval-full-shape",
        {"proposal_id": "p-1", "approval": dict(VALID_APPROVAL)},
    )
)


# ------------------------------------------------------------------ the guard


@pytest.mark.parametrize(("op", "name", "probe"), PROBES, ids=[f"{op}:{name}" for op, name, _ in PROBES])
def test_adapter_validation_parity(op: str, name: str, probe: Any) -> None:
    http = http_accepts(op, probe)
    mcp = mcp_accepts(op, _to_mcp_args(op, probe))
    pinned = _PINNED.get((op, name))
    if pinned is not None:
        expected_http, expected_mcp, family = pinned
        assert (http, mcp) == (expected_http, expected_mcp), (
            f"pinned {family} divergence moved: {op}:{name} now {(http, mcp)}, documented as "
            f"{(expected_http, expected_mcp)} — update the pin and docs/recovery/arch-review-F2-* "
            "only with an explicit decision"
        )
        return
    assert http == mcp, (
        f"adapter validation drift on {op}:{name}: HTTP accepts={http}, MCP accepts={mcp} — "
        "the two validators must accept/reject the same inputs (arch review F-2)"
    )


def test_probe_space_covers_every_operation() -> None:
    covered = {op for op, _, _ in PROBES}
    assert covered == set(http_api.KNOWLEDGE_OPERATIONS)


def test_operation_inventories_match() -> None:
    """The 14 knowledge operations exist on both adapters, 1:1 by name."""
    mcp_ops = {operation.removeprefix("knowledge_") for operation in mcp_server._TOOL_TO_OPERATION}
    assert mcp_ops == set(http_api.KNOWLEDGE_OPERATIONS)
    assert len(mcp_server.EXPECTED_TOOLS) == 14 == len(http_api.KNOWLEDGE_OPERATIONS)


def test_vocabulary_enums_match() -> None:
    """The MCP schema's closed vocabularies equal the domain enums."""
    assert sorted(mcp_server._DIFF_OPS) == sorted(op.value for op in DiffOp)
    schemas = _mcp_adapter()._schemas
    relation_enum = schemas["knowledge_query"]["properties"]["relation_allowlist"]["items"]["enum"]
    assert relation_enum == sorted(relation.value for relation in RelationKind)


def test_mcp_schema_fields_exist_in_the_http_model() -> None:
    """Every MCP-exposed field (minus transport keys) exists in the HTTP model.

    The one sanctioned asymmetry is D3: HTTP/CLI-only knobs the MCP tool
    schemas deliberately do not expose (``at`` on
    review/propose_improvement/apply_improvement/rollback, ``include_due`` on
    review), and the MCP-only nesting/transport fields (``node``,
    ``evidence``, ``expected_version``, ``idempotency_key``).
    ``log_event``/``record_outcome`` are excluded: their MCP surface wraps one
    shared model whose fields are validated identically on both sides (covered
    by the nested-model probes).
    """
    schemas = _mcp_adapter()._schemas
    http_fields: dict[str, set[str]] = {}
    for op in http_api.KNOWLEDGE_OPERATIONS:
        parsed = http_api._OPERATIONS[op].parse(json.dumps(BASE[op]).encode("utf-8"))
        http_fields[op] = set(type(parsed).model_fields)
    mcp_only_allow = {"idempotency_key", "expected_version", "node", "evidence"}
    http_only_allow: dict[str, set[str]] = {
        "review": {"at", "include_due"},
        "propose_improvement": {"at"},
        "apply_improvement": {"at"},
        "rollback": {"at"},
    }
    for op in http_api.KNOWLEDGE_OPERATIONS:
        if op in {"log_event", "record_outcome"}:
            continue
        mcp_props = set(schemas[f"knowledge_{op}"]["properties"])
        assert mcp_props - mcp_only_allow <= http_fields[op], (
            f"MCP exposes fields the HTTP model lacks: {sorted(mcp_props - mcp_only_allow - http_fields[op])}"
        )
        http_only = http_fields[op] - (mcp_props - mcp_only_allow)
        assert http_only <= http_only_allow.get(op, set()), (
            f"HTTP model exposes fields the MCP schema lacks: {sorted(http_only)}"
        )


# ------------------------------------------- D1 end-to-end: both reject overall


def _seeded_stack(tmp_path: Path) -> Any:
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_bytes(b"# Alpha\n\n## Lesson\n\nAlways roll back before retrying.\n")
    stack = build_stack(tmp_path / "soul.sqlite3", tmp_path / "out")
    SeedImporter(stack.store, state_dir=tmp_path / "import-state").import_directory(corpus, domain=PrimaryDomain.P1)
    stack.projector.rebuild()
    return stack


@pytest.mark.parametrize("budget", [64, 99999999])
def test_budget_bounds_rejected_overall_on_both_adapters(tmp_path: Path, budget: int) -> None:
    """D1: the layer differs, the outcome doesn't — both reject, typed."""
    stack = _seeded_stack(tmp_path)
    try:
        with pytest.raises(KnowledgeError) as http_error:
            dispatch_text(stack, "query", json.dumps({"purpose": "p", "token_budget": budget}))
        assert http_error.value.code in {"BUDGET_TOO_SMALL", "BUDGET_EXCEEDS_MAX"}

        with pytest.raises(mcp_server.ToolError) as mcp_error:
            mcp_server._validate_arguments(
                "knowledge_query", _mcp_adapter()._schemas["knowledge_query"], {"purpose": "p", "token_budget": budget}
            )
        assert mcp_server is not None and mcp_error.value.code == "INVALID_ARGUMENTS"
    finally:
        stack.close()

"""Tests for ``graph_model`` — universal knowledge graph model v2 (SUB-84).

Pins:

* universal node kinds: knowledge hierarchy + code + document kinds in one
  open-forward registry (append-only, deterministic iteration);
* codegraph-style stable node ids: ``kind:sha256(path:kind:name:line)[:col]``
  with byte-exact vectors (the first vector reproduces codegraph's own
  documented example ``function:c1a23b8765bedac66f0d6e29332c2041``);
* hierarchy = ``contains`` edges rooted at a source node (file/document),
  arbitrary depth, single-parent, acyclic — all validated eagerly, fail-closed;
* primary ("ibu") nodes as first-class citizens: many primaries, each with one
  domain + one purpose, inter-linked by symmetric weighted ``bridges`` edges;
* deterministic split proposals (size/heterogeneity thresholds) and review
  candidates (stale watermark, low P/M/E, content drift) — both proposal-only;
* determinism: canonical dump invariant under insertion permutations, stable
  across ``PYTHONHASHSEED``, id stability under renaming of unrelated fields.
"""

from __future__ import annotations

import hashlib
import os
import subprocess
import sys
from datetime import UTC, datetime
from decimal import Decimal
from typing import Any

import pytest
from hypothesis import given, settings
from hypothesis import strategies as st
from pydantic import ValidationError

from know_your_soul.domain import PrimaryDomain, ScoreVector
from know_your_soul.graph_model import (
    CORE_EDGE_KINDS,
    CORE_NODE_KINDS,
    SYMMETRIC_EDGE_KINDS,
    BridgeError,
    ContainsCycleError,
    CoreEdgeKind,
    CoreNodeKind,
    DuplicateEdgeError,
    DuplicateNodeError,
    EdgeKindError,
    GraphEdge,
    GraphModelError,
    GraphNode,
    InvalidBridgeEndpointError,
    MultipleParentsError,
    NodeIdError,
    NodeKindError,
    PrimaryContainmentError,
    PrimaryError,
    ReviewPolicy,
    SplitThresholds,
    UniversalGraph,
    UnknownNodeError,
    all_edge_kinds,
    all_node_kinds,
    file_node_id,
    is_edge_kind,
    is_file_node_id,
    is_node_kind,
    make_node_id,
    parse_node_id,
    propose_primary_splits,
    register_edge_kind,
    register_node_kind,
    review_candidates,
)

TS = datetime(2026, 10, 27, 12, 0, 0, tzinfo=UTC)
HASH_A = hashlib.sha256(b"content-a").hexdigest()
HASH_B = hashlib.sha256(b"content-b").hexdigest()

KNOWLEDGE_KINDS = ("root", "primary", "child", "branch", "leaf")
CODE_KINDS = (
    "file",
    "module",
    "class",
    "function",
    "method",
    "variable",
    "constant",
    "interface",
    "type",
    "import",
)
DOCUMENT_KINDS = ("document", "section", "page", "paragraph")
ALL_CORE_KINDS = KNOWLEDGE_KINDS + CODE_KINDS + DOCUMENT_KINDS


def _nid(kind: str, name: str, path: str = "p", line: int | None = 1) -> str:
    return make_node_id(path=path, kind=kind, name=name, line=line)


def _node(
    kind: str,
    name: str,
    path: str = "p",
    line: int | None = 1,
    **kw: Any,
) -> GraphNode:
    return GraphNode(node_id=_nid(kind, name, path, line), kind=kind, name=name, path=path, line=line, **kw)


def _primary(name: str, domain: PrimaryDomain = PrimaryDomain.P1, **kw: Any) -> GraphNode:
    return GraphNode(
        node_id=make_node_id(path="", kind="primary", name=name),
        kind="primary",
        name=name,
        path="",
        line=None,
        domain=domain,
        purpose=kw.pop("purpose", f"tujuan {name}"),
        **kw,
    )


def _leaf(name: str, domain: PrimaryDomain = PrimaryDomain.P1, **kw: Any) -> GraphNode:
    return GraphNode(
        node_id=make_node_id(path="", kind="leaf", name=name),
        kind="leaf",
        name=name,
        path="",
        line=None,
        domain=domain,
        **kw,
    )


def _file(path: str = "src/a.py", **kw: Any) -> GraphNode:
    return GraphNode(node_id=file_node_id(path), kind="file", name=path.rsplit("/", 1)[-1], path=path, **kw)


def _contains(source: str, target: str) -> GraphEdge:
    return GraphEdge(source=source, target=target, kind="contains")


def _tree_graph(depth: int = 4, fanout: int = 2, path: str = "src/a.py") -> tuple[UniversalGraph, str, list[str]]:
    """A deterministic ``contains`` tree: file root, ``depth`` levels, ``fanout`` children."""
    g = UniversalGraph()
    root = _file(path)
    g.add_node(root)
    ids = [root.node_id]
    frontier = [root.node_id]
    counter = 0
    for _level in range(depth):
        nxt = []
        for parent in frontier:
            for _ in range(fanout):
                counter += 1
                child = _node("function", f"f{counter}", path=path, line=counter)
                g.add_node(child)
                g.add_edge(_contains(parent, child.node_id))
                ids.append(child.node_id)
                nxt.append(child.node_id)
        frontier = nxt
    return g, root.node_id, ids


# ----------------------------------------------------------------- node kinds


class TestCoreNodeKinds:
    @pytest.mark.parametrize("kind", ALL_CORE_KINDS)
    def test_core_kind_is_registered(self, kind: str) -> None:
        assert is_node_kind(kind)

    @pytest.mark.parametrize("kind", ALL_CORE_KINDS)
    def test_core_kind_enum_value_matches(self, kind: str) -> None:
        assert CoreNodeKind[kind.upper()].value == kind

    def test_knowledge_kinds_preserved(self) -> None:
        for kind in KNOWLEDGE_KINDS:
            assert kind in CORE_NODE_KINDS

    def test_code_kinds_present(self) -> None:
        for kind in CODE_KINDS:
            assert kind in CORE_NODE_KINDS

    def test_document_kinds_present(self) -> None:
        for kind in DOCUMENT_KINDS:
            assert kind in CORE_NODE_KINDS

    def test_core_kinds_are_sorted_and_unique(self) -> None:
        assert tuple(sorted(set(CORE_NODE_KINDS))) == tuple(CORE_NODE_KINDS)

    def test_all_node_kinds_sorted(self) -> None:
        kinds = all_node_kinds()
        assert list(kinds) == sorted(kinds)

    def test_all_node_kinds_contains_core(self) -> None:
        assert set(CORE_NODE_KINDS) <= set(all_node_kinds())

    def test_unregistered_kind_is_not_a_node_kind(self) -> None:
        assert not is_node_kind("definitely_not_a_kind")

    def test_enum_lookup_rejects_unknown(self) -> None:
        with pytest.raises(ValueError):
            CoreNodeKind("NOPE")


class TestNodeKindRegistry:
    @pytest.mark.parametrize("kind", ["route", "component", "enum_member", "x_custom", "a" * 32])
    def test_register_valid_custom_kind(self, kind: str) -> None:
        register_node_kind(kind)
        assert is_node_kind(kind)
        assert kind in all_node_kinds()

    def test_register_is_idempotent(self) -> None:
        register_node_kind("idem_kind")
        before = all_node_kinds()
        register_node_kind("idem_kind")
        assert all_node_kinds() == before

    def test_register_core_kind_is_noop(self) -> None:
        before = all_node_kinds()
        register_node_kind("function")
        assert all_node_kinds() == before

    @pytest.mark.parametrize(
        "bad",
        ["", "Function", "CLASS", "9lives", "_hidden", "has space", "has:colon", "has-dash", "has.dot", "é", "a" * 33],
    )
    def test_register_invalid_kind_fails_closed(self, bad: str) -> None:
        with pytest.raises(NodeKindError):
            register_node_kind(bad)
        assert not is_node_kind(bad)

    def test_registry_errors_are_graph_model_errors(self) -> None:
        with pytest.raises(GraphModelError):
            register_node_kind("BAD!")


# ----------------------------------------------------------------- edge kinds


class TestEdgeKinds:
    @pytest.mark.parametrize(
        "kind",
        [
            "contains",
            "bridges",
            "calls",
            "imports",
            "exports",
            "extends",
            "implements",
            "references",
            "type_of",
            "returns",
            "instantiates",
            "overrides",
            "decorates",
            "navigates",
        ],
    )
    def test_core_edge_kind_registered(self, kind: str) -> None:
        assert is_edge_kind(kind)
        assert CoreEdgeKind[kind.upper()].value == kind

    def test_core_edge_kinds_sorted_unique(self) -> None:
        assert tuple(sorted(set(CORE_EDGE_KINDS))) == tuple(CORE_EDGE_KINDS)

    def test_bridges_is_symmetric(self) -> None:
        assert "bridges" in SYMMETRIC_EDGE_KINDS

    def test_contains_is_not_symmetric(self) -> None:
        assert "contains" not in SYMMETRIC_EDGE_KINDS

    def test_register_custom_edge_kind(self) -> None:
        register_edge_kind("documents")
        assert is_edge_kind("documents")
        assert "documents" in all_edge_kinds()

    @pytest.mark.parametrize("bad", ["", "Calls", "has:colon", "9x"])
    def test_register_invalid_edge_kind(self, bad: str) -> None:
        with pytest.raises(EdgeKindError):
            register_edge_kind(bad)

    def test_unknown_edge_kind_rejected_on_model(self) -> None:
        with pytest.raises(ValidationError):
            GraphEdge(source="a", target="b", kind="not_an_edge")


# -------------------------------------------------------------------- node id


class TestNodeIdByteExact:
    """Byte-exact vectors; the first one reproduces codegraph's own example."""

    @pytest.mark.parametrize(
        ("path", "kind", "name", "line", "col", "expected"),
        [
            ("src/utils.ts", "function", "normalizePath", 12, None, "function:c1a23b8765bedac66f0d6e29332c2041"),
            ("src/utils.ts", "function", "normalizePath", 12, 4, "function:c1a23b8765bedac66f0d6e29332c2041:4"),
            ("src/app.py", "class", "App", 1, None, "class:8e6a2f341441f042049b16327d5ffec3"),
            ("src/app.py", "method", "run", 10, None, "method:faf5ad1a9bf59753ec1cf9fafb2f17f2"),
            ("", "primary", "Pembelajaran", None, None, "primary:dfa4009e4eff681379d910457dad55d5"),
            ("docs/soul.md", "document", "soul.md", None, None, "document:4c3c8c11ac4d3040781e9c4a74c16a06"),
            ("docs/soul.md", "section", "Tujuan", 3, None, "section:7e03ff20cea35db1a34d59838ad4b8b6"),
            ("src/a.py", "variable", "x", 7, None, "variable:e748892f1701d2bf0711381c1c550d4d"),
        ],
    )
    def test_byte_exact_vectors(
        self, path: str, kind: str, name: str, line: int | None, col: int | None, expected: str
    ) -> None:
        assert make_node_id(path=path, kind=kind, name=name, line=line, col=col) == expected

    @pytest.mark.parametrize("kind", ALL_CORE_KINDS)
    def test_id_format_per_kind(self, kind: str) -> None:
        node_id = make_node_id(path="p", kind=kind, name="n", line=1)
        prefix, digest = node_id.split(":")
        assert prefix == kind
        assert len(digest) == 32
        assert all(c in "0123456789abcdef" for c in digest)

    @pytest.mark.parametrize("line", [1, 2, 10, 999999])
    def test_line_changes_id(self, line: int) -> None:
        assert make_node_id(path="p", kind="function", name="f", line=line) != make_node_id(
            path="p", kind="function", name="f", line=line + 1
        )

    def test_line_omitted_differs_from_any_line(self) -> None:
        base = make_node_id(path="p", kind="leaf", name="n")
        assert base != make_node_id(path="p", kind="leaf", name="n", line=1)

    def test_col_appended_after_digest(self) -> None:
        node_id = make_node_id(path="p", kind="function", name="f", line=1, col=7)
        assert node_id.endswith(":7")
        assert node_id.count(":") == 2

    def test_col_zero_is_kept(self) -> None:
        assert make_node_id(path="p", kind="function", name="f", line=1, col=0).endswith(":0")

    @pytest.mark.parametrize("col", [0, 1, 80])
    def test_col_shares_digest_with_col_free_id(self, col: int) -> None:
        with_col = make_node_id(path="p", kind="function", name="f", line=9, col=col)
        without = make_node_id(path="p", kind="function", name="f", line=9)
        assert with_col.startswith(without + ":")

    def test_digest_matches_hashlib_directly(self) -> None:
        preimage = "a/b.py:function:f:3"
        expected = hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:32]
        assert make_node_id(path="a/b.py", kind="function", name="f", line=3) == f"function:{expected}"

    def test_line_omitted_preimage_has_three_segments(self) -> None:
        preimage = ":primary:Pembelajaran"
        expected = hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:32]
        assert make_node_id(path="", kind="primary", name="Pembelajaran") == f"primary:{expected}"

    @pytest.mark.parametrize(
        ("kwargs", "match"),
        [
            ({"path": "p", "kind": "nope", "name": "n"}, "kind"),
            ({"path": "p", "kind": "function", "name": ""}, "name"),
            ({"path": "p", "kind": "function", "name": "n", "line": 0}, "line"),
            ({"path": "p", "kind": "function", "name": "n", "line": -3}, "line"),
            ({"path": "p", "kind": "function", "name": "n", "line": True}, "line"),
            ({"path": "p", "kind": "function", "name": "n", "col": 2}, "col"),
            ({"path": "p", "kind": "function", "name": "n", "line": 1, "col": -1}, "col"),
            ({"path": "p", "kind": "function", "name": "n", "line": 1, "col": False}, "col"),
        ],
    )
    def test_invalid_arguments_fail_closed(self, kwargs: dict[str, Any], match: str) -> None:
        with pytest.raises((NodeIdError, NodeKindError), match=match):
            make_node_id(**kwargs)

    def test_errors_are_graph_model_errors(self) -> None:
        with pytest.raises(GraphModelError):
            make_node_id(path="p", kind="function", name="")


class TestNodeIdParse:
    @pytest.mark.parametrize(
        ("node_id", "kind", "col"),
        [
            ("function:c1a23b8765bedac66f0d6e29332c2041", "function", None),
            ("function:c1a23b8765bedac66f0d6e29332c2041:4", "function", 4),
            ("primary:dfa4009e4eff681379d910457dad55d5", "primary", None),
        ],
    )
    def test_parse_roundtrip(self, node_id: str, kind: str, col: int | None) -> None:
        parts = parse_node_id(node_id)
        assert parts.kind == kind
        assert parts.col == col
        assert len(parts.digest) == 32

    @pytest.mark.parametrize(
        "bad",
        [
            "file:src/a.py",
            "function:xyz",
            "function:C1A23B8765BEDAC66F0D6E29332C2041",
            ":c1a23b8765bedac66f0d6e29332c2041",
            "function:c1a23b8765bedac66f0d6e29332c2041:xx",
            "",
        ],
    )
    def test_parse_rejects_malformed(self, bad: str) -> None:
        with pytest.raises(NodeIdError):
            parse_node_id(bad)

    def test_file_node_id_shape(self) -> None:
        assert file_node_id("src/a.py") == "file:src/a.py"

    def test_file_node_id_detected(self) -> None:
        assert is_file_node_id("file:src/a.py")
        assert not is_file_node_id("function:c1a23b8765bedac66f0d6e29332c2041")

    def test_file_node_id_requires_nonempty_path(self) -> None:
        with pytest.raises(NodeIdError):
            file_node_id("")

    def test_id_stable_across_hashseed(self) -> None:
        code = (
            "from know_your_soul.graph_model import make_node_id, file_node_id;"
            "print(make_node_id(path='src/a.py', kind='function', name='f', line=3));"
            "print(file_node_id('src/a.py'))"
        )
        outputs = set()
        for seed in ("0", "1", "42"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"}
            out = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, env=env, cwd=_repo_root(), text=True
            )
            assert out.returncode == 0, out.stderr
            outputs.add(out.stdout)
        assert len(outputs) == 1
        assert "function:ba35f7d5fa703db1cfc637fc9ecf62a7" in outputs.pop()


def _repo_root() -> str:
    import pathlib

    return str(pathlib.Path(__file__).resolve().parent.parent)


# ------------------------------------------------------------------ node model


class TestGraphNodeModel:
    def test_id_consistency_enforced(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id=_nid("function", "a"), kind="function", name="b", path="p", line=1)

    def test_file_node_id_consistency(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id=_nid("file", "a.py", "src/a.py", 1), kind="file", name="a.py", path="src/a.py")

    def test_file_node_requires_path(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id=file_node_id("x"), kind="file", name="x", path="")

    def test_valid_file_node(self) -> None:
        node = _file("src/a.py")
        assert node.kind == "file"
        assert node.line is None

    def test_primary_requires_domain(self) -> None:
        with pytest.raises(ValidationError, match="domain"):
            GraphNode(
                node_id=make_node_id(path="", kind="primary", name="X"),
                kind="primary",
                name="X",
                purpose="tujuan",
            )

    def test_primary_requires_purpose(self) -> None:
        with pytest.raises(ValidationError, match="purpose"):
            GraphNode(
                node_id=make_node_id(path="", kind="primary", name="X"),
                kind="primary",
                name="X",
                domain=PrimaryDomain.P1,
            )

    def test_primary_single_responsibility_ok(self) -> None:
        node = _primary("Belajar", PrimaryDomain.P1)
        assert node.domain is PrimaryDomain.P1
        assert node.purpose

    def test_unknown_kind_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id="whatever", kind="nope", name="n")

    def test_col_requires_line(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id=_nid("function", "a"), kind="function", name="a", path="p", col=2)

    @pytest.mark.parametrize("bad", [0, -1])
    def test_line_must_be_positive(self, bad: int) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id="x", kind="leaf", name="n", line=bad)

    @pytest.mark.parametrize("bad", ["zzz", HASH_A[:-1], HASH_A + "0"])
    def test_content_hash_must_be_sha256_hex(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id="x", kind="leaf", name="n", content_hash=bad)

    def test_negative_watermark_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id="x", kind="leaf", name="n", watermark=-1)

    def test_naive_updated_at_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id="x", kind="leaf", name="n", updated_at=datetime(2026, 1, 1))

    def test_frozen(self) -> None:
        node = _leaf("a")
        with pytest.raises(ValidationError):
            node.name = "b"  # type: ignore[misc]

    def test_extra_fields_forbidden(self) -> None:
        with pytest.raises(ValidationError):
            GraphNode(node_id="x", kind="leaf", name="n", provider="openai")  # type: ignore[call-arg]

    def test_scores_default_zero(self) -> None:
        assert _leaf("a").scores == ScoreVector.zero()


# ------------------------------------------------------------------ edge model


class TestGraphEdgeModel:
    def test_bridge_requires_weight(self) -> None:
        with pytest.raises(ValidationError, match="weight"):
            GraphEdge(source="a", target="b", kind="bridges", reason="r")

    def test_bridge_requires_reason(self) -> None:
        with pytest.raises(ValidationError, match="reason"):
            GraphEdge(source="a", target="b", kind="bridges", weight=Decimal("0.5"))

    def test_bridge_ok(self) -> None:
        edge = GraphEdge(source="a", target="b", kind="bridges", weight=Decimal("0.5"), reason="terkait")
        assert edge.weight == Decimal("0.5")

    @pytest.mark.parametrize("bad", ["-0.1", "1.1"])
    def test_weight_bounds(self, bad: str) -> None:
        with pytest.raises(ValidationError):
            GraphEdge(source="a", target="b", kind="bridges", weight=Decimal(bad), reason="r")

    def test_float_weight_rejected(self) -> None:
        with pytest.raises(ValidationError):
            GraphEdge(source="a", target="b", kind="bridges", weight=0.5, reason="r")  # type: ignore[arg-type]

    def test_col_requires_line(self) -> None:
        with pytest.raises(ValidationError):
            GraphEdge(source="a", target="b", kind="calls", col=3)

    def test_edge_frozen(self) -> None:
        edge = GraphEdge(source="a", target="b", kind="calls")
        with pytest.raises(ValidationError):
            edge.kind = "imports"  # type: ignore[misc]


# -------------------------------------------------------------- graph container


class TestUniversalGraphBasics:
    def test_empty_graph(self) -> None:
        g = UniversalGraph()
        assert g.node_count == 0
        assert g.edge_count == 0

    def test_add_and_get_node(self) -> None:
        g = UniversalGraph()
        node = _leaf("a")
        g.add_node(node)
        assert g.get_node(node.node_id) is node
        assert g.has_node(node.node_id)
        assert g.node_count == 1

    def test_get_unknown_node_fails_closed(self) -> None:
        with pytest.raises(UnknownNodeError):
            UniversalGraph().get_node("nope")

    def test_duplicate_identical_node_is_noop(self) -> None:
        g = UniversalGraph()
        node = _leaf("a")
        g.add_node(node)
        g.add_node(node)
        assert g.node_count == 1

    def test_conflicting_node_fails_closed(self) -> None:
        g = UniversalGraph()
        g.add_node(_leaf("a"))
        with pytest.raises(DuplicateNodeError):
            g.add_node(_leaf("a", summary="different"))

    def test_edge_requires_existing_source(self) -> None:
        g = UniversalGraph()
        g.add_node(_leaf("a"))
        with pytest.raises(UnknownNodeError):
            g.add_edge(GraphEdge(source="ghost", target=_nid("leaf", "a", "", None), kind="references"))

    def test_edge_requires_existing_target(self) -> None:
        g = UniversalGraph()
        g.add_node(_leaf("a"))
        with pytest.raises(UnknownNodeError):
            g.add_edge(GraphEdge(source=_nid("leaf", "a", "", None), target="ghost", kind="references"))

    def test_duplicate_identical_edge_is_noop(self) -> None:
        g, _, _ = _tree_graph(depth=1, fanout=1)
        edges = g.edges()
        g.add_edge(edges[0])
        assert g.edge_count == 1

    def test_conflicting_edge_fails_closed(self) -> None:
        g = UniversalGraph()
        a, b = _leaf("a"), _leaf("b")
        g.add_node(a)
        g.add_node(b)
        g.add_edge(GraphEdge(source=a.node_id, target=b.node_id, kind="references"))
        with pytest.raises(DuplicateEdgeError):
            g.add_edge(GraphEdge(source=a.node_id, target=b.node_id, kind="references", reason="different"))

    def test_nodes_sorted_by_id(self) -> None:
        g, _, ids = _tree_graph(depth=2, fanout=2)
        assert [n.node_id for n in g.nodes()] == sorted(ids)

    def test_edges_sorted_canonically(self) -> None:
        g, _, _ = _tree_graph(depth=2, fanout=2)
        keys = [(e.source, e.target, e.kind) for e in g.edges()]
        assert keys == sorted(keys)

    def test_constructor_from_iterables(self) -> None:
        f = _file()
        fn = _node("function", "f", path="src/a.py")
        g = UniversalGraph(nodes=[f, fn], edges=[_contains(f.node_id, fn.node_id)])
        assert g.node_count == 2
        assert g.edge_count == 1

    def test_errors_are_graph_model_errors(self) -> None:
        with pytest.raises(GraphModelError):
            UniversalGraph().get_node("x")


# ------------------------------------------------------------ contains hierarchy


class TestContainsHierarchy:
    def test_single_parent_enforced(self) -> None:
        g, _, ids = _tree_graph(depth=1, fanout=2)
        other = _file("src/other.py")
        g.add_node(other)
        with pytest.raises(MultipleParentsError):
            g.add_edge(_contains(other.node_id, ids[1]))

    def test_reparent_attempt_fails(self) -> None:
        g, root, ids = _tree_graph(depth=2, fanout=1)
        with pytest.raises(MultipleParentsError):
            g.add_edge(_contains(root, ids[2]))

    def test_self_loop_is_a_cycle(self) -> None:
        g = UniversalGraph()
        a = _leaf("a")
        g.add_node(a)
        with pytest.raises(ContainsCycleError):
            g.add_edge(_contains(a.node_id, a.node_id))

    def test_two_cycle_rejected(self) -> None:
        g = UniversalGraph()
        f = _file()
        a = _node("function", "a", path="src/a.py")
        g.add_node(f)
        g.add_node(a)
        g.add_edge(_contains(f.node_id, a.node_id))
        with pytest.raises(ContainsCycleError):
            g.add_edge(_contains(a.node_id, f.node_id))

    def test_long_cycle_rejected(self) -> None:
        g, root, ids = _tree_graph(depth=3, fanout=1)
        # ids[-1] is the deepest node; root has no parent, so only the cycle
        # check (not the single-parent check) can reject this edge.
        with pytest.raises(ContainsCycleError):
            g.add_edge(_contains(ids[-1], root))

    def test_cycle_error_mentions_nodes(self) -> None:
        g = UniversalGraph()
        f = _file()
        a = _node("function", "a", path="src/a.py")
        g.add_node(f)
        g.add_node(a)
        g.add_edge(_contains(f.node_id, a.node_id))
        with pytest.raises(ContainsCycleError, match="contains"):
            g.add_edge(_contains(a.node_id, f.node_id))

    def test_depth_of_root_is_zero(self) -> None:
        g, root, _ = _tree_graph()
        assert g.depth(root) == 0

    @pytest.mark.parametrize("level", [1, 2, 3, 4])
    def test_depth_matches_level(self, level: int) -> None:
        g, root, ids = _tree_graph(depth=4, fanout=1)
        assert g.depth(ids[level]) == level

    def test_arbitrary_depth_chain(self) -> None:
        g = UniversalGraph()
        f = _file()
        g.add_node(f)
        parent = f.node_id
        for i in range(1, 51):
            node = _node("paragraph", f"para{i}", path="doc.md", line=i)
            g.add_node(node)
            g.add_edge(_contains(parent, node.node_id))
            parent = node.node_id
        assert g.depth(parent) == 50
        assert g.contains_root(parent) == f.node_id

    def test_contains_children_sorted(self) -> None:
        g, root, ids = _tree_graph(depth=1, fanout=3)
        children = g.contains_children(root)
        assert list(children) == sorted(children)
        assert len(children) == 3

    def test_contains_parent(self) -> None:
        g, root, ids = _tree_graph(depth=1, fanout=1)
        assert g.contains_parent(ids[1]) == root
        assert g.contains_parent(root) is None

    def test_subtree_ids_cover_all(self) -> None:
        g, root, ids = _tree_graph(depth=3, fanout=2)
        assert set(g.subtree_ids(root)) == set(ids)

    def test_subtree_excludes_siblings(self) -> None:
        g, root, ids = _tree_graph(depth=2, fanout=2)
        sub = g.subtree_ids(ids[1])
        assert ids[2] not in sub
        assert root not in sub

    def test_subtree_unknown_root_fails(self) -> None:
        with pytest.raises(UnknownNodeError):
            UniversalGraph().subtree_ids("nope")

    def test_subtree_deterministic_order(self) -> None:
        g, root, _ = _tree_graph(depth=3, fanout=3)
        assert g.subtree_ids(root) == g.subtree_ids(root)
        # BFS: root first
        assert g.subtree_ids(root)[0] == root

    def test_validate_accepts_forest(self) -> None:
        g, _, _ = _tree_graph(depth=3, fanout=2)
        g2, _, _ = _tree_graph(depth=2, fanout=1, path="src/b.py")
        for n in g2.nodes():
            g.add_node(n)
        for e in g2.edges():
            g.add_edge(e)
        g.validate()  # two disjoint trees: fine

    def test_document_rooted_hierarchy(self) -> None:
        g = UniversalGraph()
        doc = GraphNode(
            node_id=make_node_id(path="docs/soul.md", kind="document", name="soul.md"),
            kind="document",
            name="soul.md",
            path="docs/soul.md",
        )
        sec = GraphNode(
            node_id=make_node_id(path="docs/soul.md", kind="section", name="Tujuan", line=3),
            kind="section",
            name="Tujuan",
            path="docs/soul.md",
            line=3,
        )
        para = GraphNode(
            node_id=make_node_id(path="docs/soul.md", kind="paragraph", name="p1", line=4),
            kind="paragraph",
            name="p1",
            path="docs/soul.md",
            line=4,
        )
        for n in (doc, sec, para):
            g.add_node(n)
        g.add_edge(_contains(doc.node_id, sec.node_id))
        g.add_edge(_contains(sec.node_id, para.node_id))
        g.validate()
        assert g.depth(para.node_id) == 2
        assert g.contains_root(para.node_id) == doc.node_id


# -------------------------------------------------------------------- bridges


def _two_primaries() -> tuple[UniversalGraph, GraphNode, GraphNode]:
    g = UniversalGraph()
    a = _primary("Alpha", PrimaryDomain.P1)
    b = _primary("Beta", PrimaryDomain.P2)
    g.add_node(a)
    g.add_node(b)
    return g, a, b


class TestBridges:
    def test_add_bridge_symmetric_storage(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(b.node_id, a.node_id, weight=Decimal("0.7"), reason="saling menopang")
        (edge,) = g.edges()
        assert (edge.source, edge.target) == tuple(sorted([a.node_id, b.node_id]))

    def test_bridge_lookup_both_directions(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.7"), reason="r")
        assert g.bridges_of(a.node_id)[0].kind == "bridges"
        assert g.bridges_of(b.node_id)[0].kind == "bridges"

    def test_duplicate_bridge_same_pair_noop(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.7"), reason="r")
        g.add_bridge(b.node_id, a.node_id, weight=Decimal("0.7"), reason="r")
        assert g.edge_count == 1

    def test_conflicting_bridge_fails_closed(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.7"), reason="r")
        with pytest.raises(DuplicateEdgeError):
            g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.2"), reason="r")

    def test_self_bridge_rejected(self) -> None:
        g, a, _ = _two_primaries()
        with pytest.raises(BridgeError):
            g.add_bridge(a.node_id, a.node_id, weight=Decimal("0.5"), reason="r")

    @pytest.mark.parametrize("other_kind", ["leaf", "function", "document"])
    def test_bridge_requires_primary_endpoints(self, other_kind: str) -> None:
        g, a, _ = _two_primaries()
        other = _node(other_kind, "x", path="", line=None) if other_kind != "function" else _node("function", "x")
        g.add_node(other)
        with pytest.raises(BridgeError):
            g.add_bridge(a.node_id, other.node_id, weight=Decimal("0.5"), reason="r")

    def test_bridge_weight_and_reason_stored(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.25"), reason="berbagi konteks")
        (edge,) = g.edges()
        assert edge.weight == Decimal("0.25")
        assert edge.reason == "berbagi konteks"

    def test_bridge_does_not_create_contains_parent(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.5"), reason="r")
        assert g.contains_parent(a.node_id) is None
        assert g.contains_parent(b.node_id) is None
        g.validate()

    def test_many_primaries_star(self) -> None:
        g = UniversalGraph()
        center = _primary("Center", PrimaryDomain.P1)
        g.add_node(center)
        for i, domain in enumerate(PrimaryDomain):
            if domain is PrimaryDomain.P1:
                continue
            p = _primary(f"P{i}", domain)
            g.add_node(p)
            g.add_bridge(center.node_id, p.node_id, weight=Decimal("0.1"), reason=f"link {i}")
        assert len(g.primaries()) == 7
        assert len(g.bridges_of(center.node_id)) == 6

    def test_primaries_sorted(self) -> None:
        g, a, b = _two_primaries()
        assert list(g.primaries()) == sorted([a.node_id, b.node_id])


# ------------------------------------------------- primary containment (F3)


class TestPrimaryContainmentInvariant:
    """F3 (audit-final-v020): a primary is never contained by another primary.

    Contract v3 (``docs/contracts/ingestion-contract-v3.md``, "Primary node
    rules"): cross-primary structure rides ``bridges`` exclusively. The
    invariant is enforced eagerly at ``add_edge`` and re-proven by
    ``validate()``; the bridge endpoint failure is typed
    ``INVALID_BRIDGE_ENDPOINT``.
    """

    def test_primary_contained_by_primary_is_rejected(self) -> None:
        g, a, b = _two_primaries()
        with pytest.raises(PrimaryContainmentError):
            g.add_edge(_contains(a.node_id, b.node_id))
        assert g.edge_count == 0  # nothing was inserted

    def test_rejection_is_typed_primary_contained_by_primary(self) -> None:
        g, a, b = _two_primaries()
        with pytest.raises(PrimaryContainmentError) as caught:
            g.add_edge(_contains(b.node_id, a.node_id))  # reversed: still primary -> primary
        assert caught.value.code == "PRIMARY_CONTAINED_BY_PRIMARY"
        assert a.node_id in str(caught.value) and b.node_id in str(caught.value)

    def test_validate_reproves_the_invariant(self) -> None:
        # Eager enforcement makes an invalid graph unreachable through the
        # public API; validate() still re-checks (defense in depth), proven by
        # injecting the edge past the check.
        g, a, b = _two_primaries()
        edge = _contains(a.node_id, b.node_id)
        g._edges[(edge.source, edge.target, edge.kind, -1, -1)] = edge  # noqa: SLF001
        with pytest.raises(PrimaryContainmentError):
            g.validate()

    def test_primary_containing_non_primary_is_allowed(self) -> None:
        g, a, _ = _two_primaries()
        leaf = _leaf("child-lesson")
        g.add_node(leaf)
        g.add_edge(_contains(a.node_id, leaf.node_id))
        assert g.contains_parent(leaf.node_id) == a.node_id
        g.validate()

    def test_non_primary_containing_primary_is_allowed(self) -> None:
        # Only the primary -> primary direction is forbidden; a decomposition
        # root may sit under a file/document root.
        g, a, _ = _two_primaries()
        f = _file("src/root.py")
        g.add_node(f)
        g.add_edge(_contains(f.node_id, a.node_id))
        assert g.contains_parent(a.node_id) == f.node_id
        g.validate()

    def test_bridge_endpoints_failure_is_typed_invalid_bridge_endpoint(self) -> None:
        g, a, _ = _two_primaries()
        leaf = _leaf("plain")
        g.add_node(leaf)
        with pytest.raises(InvalidBridgeEndpointError) as caught:
            g.add_bridge(a.node_id, leaf.node_id, weight=Decimal("0.5"), reason="r")
        assert caught.value.code == "INVALID_BRIDGE_ENDPOINT"

    def test_bridge_self_loop_stays_bridge_error_without_endpoint_code(self) -> None:
        g, a, _ = _two_primaries()
        with pytest.raises(BridgeError) as caught:
            g.add_bridge(a.node_id, a.node_id, weight=Decimal("0.5"), reason="r")
        assert not isinstance(caught.value, InvalidBridgeEndpointError)


# ------------------------------------------------------------ split proposals


def _primary_tree(
    domains: tuple[PrimaryDomain, ...],
    per_domain: int = 3,
    root_domain: PrimaryDomain = PrimaryDomain.P1,
) -> tuple[UniversalGraph, GraphNode]:
    g = UniversalGraph()
    root = _primary("Root", root_domain)
    g.add_node(root)
    for d in domains:
        for i in range(per_domain):
            leaf = _leaf(f"{d.value}-{i}", d)
            g.add_node(leaf)
            g.add_edge(_contains(root.node_id, leaf.node_id))
    return g, root


class TestSplitThresholds:
    @pytest.mark.parametrize(
        ("field", "value"),
        [("max_subtree_nodes", 0), ("max_distinct_domains", 0), ("max_distinct_kinds", -1)],
    )
    def test_thresholds_must_be_positive(self, field: str, value: int) -> None:
        with pytest.raises(GraphModelError):
            SplitThresholds(**{field: value})

    def test_defaults(self) -> None:
        t = SplitThresholds()
        assert t.max_subtree_nodes == 500
        assert t.max_distinct_domains == 3
        assert t.max_distinct_kinds == 8


class TestSplitProposals:
    def test_small_homogeneous_tree_no_proposal(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1,), per_domain=3)
        assert propose_primary_splits(g, root.node_id) == ()

    def test_proposal_only_graph_unchanged(self) -> None:
        g, root = _primary_tree(tuple(PrimaryDomain)[:5], per_domain=4)
        before = g.canonical_dump()
        propose_primary_splits(g, root.node_id, SplitThresholds(max_subtree_nodes=5, max_distinct_domains=2))
        assert g.canonical_dump() == before

    def test_non_primary_root_fails_closed(self) -> None:
        g, root, _ = _tree_graph(depth=1, fanout=1)
        with pytest.raises(PrimaryError):
            propose_primary_splits(g, root)

    def test_unknown_root_fails_closed(self) -> None:
        with pytest.raises(UnknownNodeError):
            propose_primary_splits(UniversalGraph(), "nope")

    def test_domain_heterogeneity_triggers(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1, PrimaryDomain.P2, PrimaryDomain.P3), per_domain=2)
        proposals = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=2))
        assert proposals
        assert all("domain_heterogeneity" in p.reasons for p in proposals)

    def test_domain_split_excludes_root_domain(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1, PrimaryDomain.P2, PrimaryDomain.P3), per_domain=2)
        proposals = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=2))
        proposed_domains = {p.domain for p in proposals}
        assert PrimaryDomain.P1 not in proposed_domains
        assert proposed_domains == {PrimaryDomain.P2, PrimaryDomain.P3}

    def test_domain_split_members_cover_non_root_domains(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1, PrimaryDomain.P2), per_domain=3)
        (proposal,) = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=1))
        assert len(proposal.member_node_ids) == 3
        for nid in proposal.member_node_ids:
            assert g.get_node(nid).domain is PrimaryDomain.P2

    def test_size_overflow_homogeneous_splits_by_child_subtrees(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1,), per_domain=6)
        proposals = propose_primary_splits(g, root.node_id, SplitThresholds(max_subtree_nodes=3))
        assert len(proposals) >= 1
        assert all("subtree_size" in p.reasons for p in proposals)
        total = sum(len(p.member_node_ids) for p in proposals)
        assert total == 6  # all descendants covered exactly once

    def test_size_split_members_disjoint(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1,), per_domain=7)
        proposals = propose_primary_splits(g, root.node_id, SplitThresholds(max_subtree_nodes=3))
        seen: set[str] = set()
        for p in proposals:
            assert not (seen & set(p.member_node_ids))
            seen |= set(p.member_node_ids)

    def test_kind_heterogeneity_triggers(self) -> None:
        g = UniversalGraph()
        root = _primary("Root", PrimaryDomain.P1)
        g.add_node(root)
        kinds = ["leaf", "branch", "child", "section", "paragraph"]
        for i, kind in enumerate(kinds):
            node = GraphNode(
                node_id=make_node_id(path="", kind=kind, name=f"n{i}"),
                kind=kind,
                name=f"n{i}",
                domain=PrimaryDomain.P1,
            )
            g.add_node(node)
            g.add_edge(_contains(root.node_id, node.node_id))
        proposals = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_kinds=3))
        assert proposals
        assert all("kind_heterogeneity" in p.reasons for p in proposals)

    def test_proposals_sorted_and_deterministic(self) -> None:
        g, root = _primary_tree(tuple(PrimaryDomain)[:4], per_domain=2)
        first = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=2))
        second = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=2))
        assert first == second
        assert [p.suggested_name for p in first] == sorted(p.suggested_name for p in first)

    def test_metrics_carry_size_domains_kinds(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1, PrimaryDomain.P2), per_domain=2)
        (proposal,) = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=1))
        metrics = dict(proposal.metrics)
        assert metrics["subtree_size"] == 4
        assert metrics["distinct_domains"] == 2
        assert metrics["distinct_kinds"] == 1

    def test_reasons_fixed_order(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1, PrimaryDomain.P2), per_domain=5)
        (proposal,) = propose_primary_splits(
            g, root.node_id, SplitThresholds(max_subtree_nodes=3, max_distinct_domains=1)
        )
        assert list(proposal.reasons) == sorted(proposal.reasons)

    def test_suggested_name_includes_root_name(self) -> None:
        g, root = _primary_tree((PrimaryDomain.P1, PrimaryDomain.P2), per_domain=2)
        (proposal,) = propose_primary_splits(g, root.node_id, SplitThresholds(max_distinct_domains=1))
        assert proposal.suggested_name.startswith("Root")


# ------------------------------------------------------------ review candidates


class TestReviewPolicy:
    def test_defaults(self) -> None:
        policy = ReviewPolicy()
        assert policy.stale_watermark is None
        assert policy.min_score == Decimal("0.2")
        assert policy.check_drift

    def test_min_score_bounds(self) -> None:
        with pytest.raises(GraphModelError):
            ReviewPolicy(min_score=Decimal("1.5"))

    def test_negative_watermark_rejected(self) -> None:
        with pytest.raises(GraphModelError):
            ReviewPolicy(stale_watermark=-1)

    def test_float_min_score_rejected(self) -> None:
        with pytest.raises(GraphModelError):
            ReviewPolicy(min_score=0.2)  # type: ignore[arg-type]


def _review_graph() -> tuple[UniversalGraph, GraphNode]:
    g = UniversalGraph()
    root = _primary("Root", PrimaryDomain.P1)
    g.add_node(root)
    return g, root


def _attach(g: UniversalGraph, root: GraphNode, name: str, **kw: Any) -> GraphNode:
    leaf = _leaf(name, PrimaryDomain.P1, **kw)
    g.add_node(leaf)
    g.add_edge(_contains(root.node_id, leaf.node_id))
    return leaf


class TestReviewCandidates:
    def test_healthy_subtree_no_candidates(self) -> None:
        g, root = _review_graph()
        _attach(
            g,
            root,
            "fresh",
            watermark=10,
            scores=ScoreVector(p=Decimal("0.9"), m=Decimal("0.9"), e=Decimal("0.9")),
            content_hash=HASH_A,
            reviewed_hash=HASH_A,
        )
        assert review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5)) == ()

    def test_stale_watermark_flagged(self) -> None:
        g, root = _review_graph()
        stale = _attach(g, root, "stale", watermark=3)
        candidates = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5, min_score=Decimal(0)))
        assert [c.node_id for c in candidates] == [stale.node_id]
        assert "stale" in candidates[0].reasons

    def test_missing_watermark_is_stale(self) -> None:
        g, root = _review_graph()
        node = _attach(g, root, "never")
        candidates = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=1, min_score=Decimal(0)))
        assert [c.node_id for c in candidates] == [node.node_id]
        assert "stale" in candidates[0].reasons

    def test_watermark_at_threshold_not_stale(self) -> None:
        g, root = _review_graph()
        _attach(g, root, "edge", watermark=5)
        assert review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5, min_score=Decimal(0))) == ()

    @pytest.mark.parametrize("field", ["p", "m", "e"])
    def test_low_score_flagged_per_axis(self, field: str) -> None:
        g, root = _review_graph()
        scores = ScoreVector.model_validate(
            {"p": "0.9", "m": "0.9", "e": "0.9", field: "0.1", "uncertainty": "0", "confidence": "1"}
        )
        node = _attach(g, root, "low", scores=scores)
        candidates = review_candidates(g, root.node_id, ReviewPolicy(min_score=Decimal("0.2")))
        assert [c.node_id for c in candidates] == [node.node_id]
        assert "low_score" in candidates[0].reasons

    def test_score_at_threshold_not_flagged(self) -> None:
        g, root = _review_graph()
        scores = ScoreVector(p=Decimal("0.2"), m=Decimal("0.2"), e=Decimal("0.2"))
        _attach(g, root, "ok", scores=scores)
        assert review_candidates(g, root.node_id, ReviewPolicy(min_score=Decimal("0.2"))) == ()

    def test_content_drift_flagged(self) -> None:
        g, root = _review_graph()
        node = _attach(
            g,
            root,
            "drift",
            scores=ScoreVector(p=Decimal(1), m=Decimal(1), e=Decimal(1)),
            content_hash=HASH_B,
            reviewed_hash=HASH_A,
        )
        candidates = review_candidates(g, root.node_id, ReviewPolicy())
        assert [c.node_id for c in candidates] == [node.node_id]
        assert "content_drift" in candidates[0].reasons

    def test_matching_hashes_not_flagged(self) -> None:
        g, root = _review_graph()
        _attach(
            g,
            root,
            "same",
            scores=ScoreVector(p=Decimal(1), m=Decimal(1), e=Decimal(1)),
            content_hash=HASH_A,
            reviewed_hash=HASH_A,
        )
        assert review_candidates(g, root.node_id, ReviewPolicy()) == ()

    def test_unreviewed_content_is_not_drift(self) -> None:
        g, root = _review_graph()
        _attach(
            g,
            root,
            "unreviewed",
            scores=ScoreVector(p=Decimal(1), m=Decimal(1), e=Decimal(1)),
            content_hash=HASH_A,
        )
        assert review_candidates(g, root.node_id, ReviewPolicy()) == ()

    def test_drift_check_disabled(self) -> None:
        g, root = _review_graph()
        _attach(
            g,
            root,
            "drift",
            scores=ScoreVector(p=Decimal(1), m=Decimal(1), e=Decimal(1)),
            content_hash=HASH_B,
            reviewed_hash=HASH_A,
        )
        assert review_candidates(g, root.node_id, ReviewPolicy(check_drift=False)) == ()

    def test_combined_reasons_sorted(self) -> None:
        g, root = _review_graph()
        node = _attach(g, root, "bad", watermark=1, content_hash=HASH_B, reviewed_hash=HASH_A)
        (candidate,) = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5))
        assert candidate.node_id == node.node_id
        assert list(candidate.reasons) == ["content_drift", "low_score", "stale"]

    def test_candidates_sorted_by_node_id(self) -> None:
        g, root = _review_graph()
        _attach(g, root, "a", watermark=1)
        _attach(g, root, "b", watermark=1)
        _attach(g, root, "c", watermark=1)
        candidates = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5))
        ids = [c.node_id for c in candidates]
        assert ids == sorted(ids)
        assert len(ids) == 3

    def test_root_itself_not_a_candidate(self) -> None:
        g, root = _review_graph()
        _attach(g, root, "kid", watermark=99)
        candidates = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5, min_score=Decimal(0)))
        assert candidates == ()

    def test_deep_descendants_included(self) -> None:
        g, root = _review_graph()
        mid = _attach(g, root, "mid", watermark=99)
        deep = _leaf("deep", PrimaryDomain.P1, watermark=1)
        g.add_node(deep)
        g.add_edge(_contains(mid.node_id, deep.node_id))
        candidates = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5, min_score=Decimal(0)))
        assert [c.node_id for c in candidates] == [deep.node_id]

    def test_proposal_only_graph_unchanged(self) -> None:
        g, root = _review_graph()
        _attach(g, root, "x", watermark=1)
        before = g.canonical_dump()
        review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=5))
        assert g.canonical_dump() == before

    def test_unknown_root_fails_closed(self) -> None:
        with pytest.raises(UnknownNodeError):
            review_candidates(UniversalGraph(), "nope", ReviewPolicy())

    def test_details_carry_evidence(self) -> None:
        g, root = _review_graph()
        _attach(g, root, "d", watermark=2)
        (candidate,) = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=9, min_score=Decimal(0)))
        details = dict(candidate.details)
        assert details["watermark"] == "2"
        assert details["stale_watermark"] == "9"


# --------------------------------------------------------------- canonical dump


class TestCanonicalDump:
    def test_dump_is_byte_stable(self) -> None:
        g, _, _ = _tree_graph(depth=2, fanout=2)
        assert g.canonical_dump() == g.canonical_dump()

    def test_dump_invariant_under_node_permutation(self) -> None:
        import random

        g, _, _ = _tree_graph(depth=2, fanout=2)
        nodes = list(g.nodes())
        edges = list(g.edges())
        reference = g.canonical_dump()
        rng = random.Random(42)
        for _ in range(5):
            order = nodes[:]
            rng.shuffle(order)
            g2 = UniversalGraph()
            for n in order:
                g2.add_node(n)
            for e in edges:
                g2.add_edge(e)
            assert g2.canonical_dump() == reference

    def test_dump_invariant_under_edge_permutation(self) -> None:
        import random

        g, _, _ = _tree_graph(depth=2, fanout=2)
        # rebuild: nodes first, edges in shuffled (but still acyclic-valid) order.
        nodes = list(g.nodes())
        reference = g.canonical_dump()
        rng = random.Random(7)
        for _ in range(5):
            # topological order of contains edges keeps insertion legal.
            edges = sorted(g.edges(), key=lambda e: (g.depth(e.source), e.target))
            head = edges[: len(edges) // 2]
            tail = edges[len(edges) // 2 :]
            rng.shuffle(tail)
            g2 = UniversalGraph()
            for n in nodes:
                g2.add_node(n)
            for e in head + tail:
                g2.add_edge(e)
            assert g2.canonical_dump() == reference

    def test_dump_contains_version_marker(self) -> None:
        g = UniversalGraph()
        assert '"graph_model_version":2' in g.canonical_dump()

    def test_dump_sha256_stable(self) -> None:
        g = UniversalGraph()
        f = _file("src/a.py")
        g.add_node(f)
        digest = hashlib.sha256(g.canonical_dump().encode("utf-8")).hexdigest()
        assert digest == hashlib.sha256(g.canonical_dump().encode("utf-8")).hexdigest()
        assert len(digest) == 64

    def test_dump_includes_bridges_with_weight(self) -> None:
        g, a, b = _two_primaries()
        g.add_bridge(a.node_id, b.node_id, weight=Decimal("0.5"), reason="r")
        dump = g.canonical_dump()
        assert '"kind":"bridges"' in dump
        assert "0.500000000000" in dump

    def test_dump_stable_across_hashseed(self) -> None:
        code = (
            "from datetime import UTC, datetime;"
            "from know_your_soul.graph_model import UniversalGraph, GraphNode, file_node_id;"
            "g=UniversalGraph();"
            "g.add_node(GraphNode(node_id=file_node_id('a.py'),kind='file',name='a.py',path='a.py',"
            "updated_at=datetime(2026,1,1,tzinfo=UTC)));"
            "print(g.canonical_dump())"
        )
        outputs = set()
        for seed in ("0", "1", "42"):
            env = {**os.environ, "PYTHONHASHSEED": seed, "PYTHONPATH": "src"}
            out = subprocess.run(
                [sys.executable, "-c", code], capture_output=True, env=env, cwd=_repo_root(), text=True
            )
            assert out.returncode == 0, out.stderr
            outputs.add(out.stdout)
        assert len(outputs) == 1


# ------------------------------------------------------------- property tests


_KIND_ST = st.sampled_from(sorted(ALL_CORE_KINDS))
_NAME_ST = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Lu", "Nd"), whitelist_characters="_-."), min_size=1, max_size=24
)
_PATH_ST = st.text(
    alphabet=st.characters(whitelist_categories=("Ll", "Nd"), whitelist_characters="/_."), min_size=0, max_size=32
)


@settings(derandomize=True, max_examples=25, deadline=None)
@given(kind=_KIND_ST, name=_NAME_ST, path=_PATH_ST, line=st.integers(min_value=1, max_value=100000))
def test_property_id_deterministic(kind: str, name: str, path: str, line: int) -> None:
    first = make_node_id(path=path, kind=kind, name=name, line=line)
    second = make_node_id(path=path, kind=kind, name=name, line=line)
    assert first == second
    parts = parse_node_id(first)
    assert parts.kind == kind


@settings(derandomize=True, max_examples=25, deadline=None)
@given(
    kind=_KIND_ST,
    name=_NAME_ST,
    path=_PATH_ST,
    line=st.integers(min_value=1, max_value=100000),
    other_line=st.integers(min_value=1, max_value=100000),
)
def test_property_id_sensitive_to_line(kind: str, name: str, path: str, line: int, other_line: int) -> None:
    if line == other_line:
        return
    assert make_node_id(path=path, kind=kind, name=name, line=line) != make_node_id(
        path=path, kind=kind, name=name, line=other_line
    )


@settings(derandomize=True, max_examples=25, deadline=None)
@given(data=st.data())
def test_property_random_forest_never_cyclic(data: st.DataObject) -> None:
    """Any forest built by attaching new nodes under existing ones stays valid."""
    g = UniversalGraph()
    root = _file()
    g.add_node(root)
    population = [root.node_id]
    for i in range(data.draw(st.integers(min_value=0, max_value=40))):
        node = _node("function", f"f{i}", line=i + 1)
        g.add_node(node)
        parent = data.draw(st.sampled_from(population))
        g.add_edge(_contains(parent, node.node_id))
        population.append(node.node_id)
    g.validate()
    for nid in population:
        assert g.contains_root(nid) == root.node_id


@settings(derandomize=True, max_examples=25, deadline=None)
@given(data=st.data())
def test_property_closing_a_cycle_always_raises(data: st.DataObject) -> None:
    g = UniversalGraph()
    root = _file()
    g.add_node(root)
    chain = [root.node_id]
    for i in range(data.draw(st.integers(min_value=1, max_value=12))):
        node = _node("function", f"f{i}", line=i + 1)
        g.add_node(node)
        g.add_edge(_contains(chain[-1], node.node_id))
        chain.append(node.node_id)
    ancestor = data.draw(st.sampled_from(chain[:-1]))
    # Non-root ancestors already have a parent, so the single-parent rule fires
    # first; pointing at the (parentless) root exercises the cycle check itself.
    if ancestor == chain[0]:
        with pytest.raises(ContainsCycleError):
            g.add_edge(_contains(chain[-1], ancestor))
    else:
        with pytest.raises(MultipleParentsError):
            g.add_edge(_contains(chain[-1], ancestor))


@settings(derandomize=True, max_examples=25, deadline=None)
@given(permutation_seed=st.integers(min_value=0, max_value=1000))
def test_property_canonical_dump_permutation_invariant(permutation_seed: int) -> None:
    import random

    g, _, _ = _tree_graph(depth=2, fanout=3)
    reference = g.canonical_dump()
    nodes = list(g.nodes())
    edges = sorted(g.edges(), key=lambda e: (g.depth(e.source), e.target, e.kind))
    rng = random.Random(permutation_seed)
    node_order = nodes[:]
    rng.shuffle(node_order)
    g2 = UniversalGraph()
    for n in node_order:
        g2.add_node(n)
    for e in edges:
        g2.add_edge(e)
    assert g2.canonical_dump() == reference


@settings(derandomize=True, max_examples=25, deadline=None)
@given(
    stale_watermark=st.integers(min_value=0, max_value=100),
    watermark=st.one_of(st.none(), st.integers(min_value=0, max_value=100)),
)
def test_property_review_candidates_proposal_only(stale_watermark: int, watermark: int | None) -> None:
    g, root = _review_graph()
    _attach(g, root, "x", watermark=watermark)
    before = g.canonical_dump()
    candidates = review_candidates(g, root.node_id, ReviewPolicy(stale_watermark=stale_watermark, min_score=Decimal(0)))
    assert g.canonical_dump() == before
    expected_stale = watermark is None or watermark < stale_watermark
    assert (len(candidates) == 1) == expected_stale


@settings(derandomize=True, max_examples=25, deadline=None)
@given(size=st.integers(min_value=1, max_value=30), threshold=st.integers(min_value=1, max_value=30))
def test_property_split_proposals_cover_once(size: int, threshold: int) -> None:
    g, root = _primary_tree((PrimaryDomain.P1,), per_domain=size)
    before = g.canonical_dump()
    proposals = propose_primary_splits(g, root.node_id, SplitThresholds(max_subtree_nodes=threshold))
    assert g.canonical_dump() == before
    members = [m for p in proposals for m in p.member_node_ids]
    if size > threshold:
        assert len(members) == size
        assert len(set(members)) == size
    else:
        assert proposals == ()

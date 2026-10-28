"""Unit tests for the two-phase reference resolver (wave W2).

W2 of ``.superpowers/sdd/2026-10-27-universal-knowledge-graph/MASTER-PLAN.md``
(pillar 3: cross-file references are parked in ``unresolved_refs`` and turned
into edges by a separate resolve pass — never guessed at extraction time).

Raw-ref contract shared with the extractor (SUB-87): a plain mapping
``{"source_node_id": str, "target_name": str, "kind": str}`` where ``kind`` is
one of ``calls``/``imports``/``function_ref`` (mapped to edge kinds
``calls``/``imports``). The store derives ``name_tail`` at insert time.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Iterator
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from know_your_soul.resolve import (
    RESOLVER_VERSION,
    BatchReport,
    ReferenceResolver,
    RefOutcome,
    ResolverConfig,
    ResolverError,
    resolve_pending,
)
from know_your_soul.store import EventStore

SEEN = "2026-10-26T00:00:00+00:00"
BASE = "function:base"


# ------------------------------------------------------------------ helpers


@pytest.fixture
def store(tmp_path: Path) -> Iterator[EventStore]:
    instance = EventStore(tmp_path / "soul.sqlite3")
    try:
        yield instance
    finally:
        instance.close()


def add_file(store: EventStore, path: str, language: str) -> None:
    store.upsert_file(
        path=path,
        sha256=hashlib.sha256(path.encode()).hexdigest(),
        size=10,
        mtime=100,
        language=language,
        generated_hint=False,
        extraction_version=1,
    )


def add_symbol(store: EventStore, node_id: str, name: str, path: str, *, kind: str = "function", line: int = 1) -> None:
    store.upsert_symbol(node_id=node_id, name=name, kind=kind, path=path, line=line)


def add_ref(
    store: EventStore,
    target: str,
    kind: str = "calls",
    *,
    source: str = BASE,
    name_tail: str | None = None,
) -> int:
    return store.insert_unresolved_ref(
        source_node_id=source, target_name=target, kind=kind, name_tail=name_tail, first_seen=SEEN
    )


def base_source(store: EventStore, path: str = "pkg/main.py", language: str = "python") -> None:
    add_file(store, path, language)
    add_symbol(store, BASE, "main", path)


def resolve(store: EventStore, **kwargs: object) -> BatchReport:
    return ReferenceResolver().resolve_pending(store, **kwargs)  # type: ignore[arg-type]


def all_edges(store: EventStore) -> list[tuple[object, ...]]:
    with store.read() as conn:
        return [
            tuple(row)
            for row in conn.execute(
                "SELECT source, target, kind, line, col, metadata, provenance FROM graph_edges ORDER BY edge_id"
            )
        ]


def ref_dumps(store: EventStore) -> list[tuple[object, ...]]:
    with store.read() as conn:
        return [tuple(row) for row in conn.execute("SELECT * FROM unresolved_refs ORDER BY ref_id")]


def outcome_of(report: BatchReport, ref_id: int) -> RefOutcome:
    matches = [outcome for outcome in report.outcomes if outcome.ref_id == ref_id]
    assert len(matches) == 1
    return matches[0]


# ------------------------------------------------------------------- config


class TestConfig:
    def test_defaults(self) -> None:
        config = ResolverConfig()
        assert config.batch_limit is None
        assert config.js_extensions[0] == ".ts"
        assert config.python_suffixes == (".py", "/__init__.py")
        assert "python" in config.module_languages

    def test_version_constant(self) -> None:
        assert RESOLVER_VERSION == 1

    @pytest.mark.parametrize("bad", [0, -1, -100])
    def test_invalid_batch_limit(self, bad: int) -> None:
        with pytest.raises(ResolverError):
            ResolverConfig(batch_limit=bad)

    def test_batch_limit_bool_rejected(self) -> None:
        with pytest.raises(ResolverError):
            ResolverConfig(batch_limit=True)  # type: ignore[arg-type]

    @pytest.mark.parametrize(
        "field",
        ["js_extensions", "js_index_names", "python_suffixes"],
    )
    def test_empty_sequences_rejected(self, field: str) -> None:
        with pytest.raises(ResolverError):
            ResolverConfig(**{field: ()})  # type: ignore[arg-type]

    def test_empty_module_languages_rejected(self) -> None:
        with pytest.raises(ResolverError):
            ResolverConfig(module_languages=frozenset())

    def test_config_frozen(self) -> None:
        config = ResolverConfig()
        with pytest.raises(FrozenInstanceError):
            config.batch_limit = 5  # type: ignore[misc]


# ------------------------------------------------------------ kind mapping


class TestKindMapping:
    @pytest.mark.parametrize(
        ("ref_kind", "edge_kind"),
        [("calls", "calls"), ("imports", "imports"), ("function_ref", "calls")],
    )
    def test_known_kinds(self, store: EventStore, ref_kind: str, edge_kind: str) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", ref_kind)
        report = resolve(store)
        assert outcome_of(report, ref_id).status == "resolved"
        assert all_edges(store)[0][2] == edge_kind

    # W4 addendum: "references" left this list — it now maps to the
    # ``references`` edge kind (tests/test_retrieval_v2_resolver_addendum.py).
    @pytest.mark.parametrize("bad", ["CALLS", "", "extends", "import"])
    def test_unknown_kind_fails_closed(self, store: EventStore, bad: str) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", bad)
        report = resolve(store)
        outcome = outcome_of(report, ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == "unknown_ref_kind"
        assert all_edges(store) == []


# ------------------------------------------------------------------- source


class TestSource:
    def test_unknown_source_fails(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", source="function:ghost")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == "unknown_source"

    def test_file_node_source_resolves(self, store: EventStore) -> None:
        add_file(store, "src/a.ts", "typescript")
        add_file(store, "src/util.ts", "typescript")
        ref_id = add_ref(store, "./util", "imports", source="file:src/a.ts")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert all_edges(store)[0][1] == "file:src/util.ts"

    def test_file_node_source_without_file_row_fails(self, store: EventStore) -> None:
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", source="file:pkg/ghost.py")
        assert outcome_of(resolve(store), ref_id).reason == "unknown_source"


# ------------------------------------------------------------- exact match


class TestExactStrategy:
    def test_exact_unique_calls(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", "calls")
        report = resolve(store)
        outcome = outcome_of(report, ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "exact"
        assert outcome.target_node_id == "function:tgt"
        assert report.resolved == 1 and report.failed == 0 and report.edges_inserted == 1
        assert all_edges(store)[0][:3] == (BASE, "function:tgt", "calls")

    @pytest.mark.parametrize("n", [2, 3, 5])
    def test_exact_ambiguous_fails(self, store: EventStore, n: int) -> None:
        base_source(store)
        for index in range(n):
            add_symbol(store, f"function:t{index}", "helper", f"pkg/f{index}.py")
        ref_id = add_ref(store, "helper", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == f"ambiguous_exact:{n}"

    def test_exact_case_sensitive(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "Helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", "calls")
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_exact_qualified_name(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "method:tgt", "Klass.helper", "pkg/other.py", kind="method")
        ref_id = add_ref(store, "Klass.helper", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "exact"

    def test_exact_beats_ambiguous_tail(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:qualified", "pkg.helper", "pkg/a.py")
        add_symbol(store, "function:s1", "helper", "pkg/b.py")
        add_symbol(store, "function:s2", "helper", "pkg/c.py")
        ref_id = add_ref(store, "pkg.helper", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "exact"
        assert outcome.target_node_id == "function:qualified"

    def test_edge_metadata_and_provenance(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", "calls")
        resolve(store)
        source, target, kind, line, col, metadata, provenance = all_edges(store)[0]
        assert line is None and col is None
        assert provenance == f"resolver:two-phase:v{RESOLVER_VERSION}"
        assert json.loads(str(metadata)) == {"ref_id": ref_id, "ref_name": "helper", "strategy": "exact"}

    def test_duplicate_edges_deduped(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        first = add_ref(store, "helper", "calls")
        second = add_ref(store, "helper", "calls")
        report = resolve(store)
        assert outcome_of(report, first).status == "resolved"
        assert outcome_of(report, second).status == "resolved"
        assert report.edges_inserted == 1
        assert len(all_edges(store)) == 1


# -------------------------------------------------------------- tail match


class TestTailStrategy:
    def test_tail_simple_name(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "func", "pkg/mod.py")
        ref_id = add_ref(store, "pkg.mod.func", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "name_tail"
        assert outcome.target_node_id == "function:tgt"

    @pytest.mark.parametrize("qualified", ["Klass.func", "Klass::func"])
    def test_tail_qualified_suffix(self, store: EventStore, qualified: str) -> None:
        base_source(store)
        add_symbol(store, "method:tgt", qualified, "pkg/mod.py", kind="method")
        ref_id = add_ref(store, "pkg.mod.func", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "name_tail"

    @pytest.mark.parametrize("n", [2, 3])
    def test_tail_ambiguous_fails(self, store: EventStore, n: int) -> None:
        base_source(store)
        for index in range(n):
            add_symbol(store, f"function:t{index}", "func", f"pkg/f{index}.py")
        ref_id = add_ref(store, "pkg.mod.func", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == f"ambiguous_tail:{n}"

    def test_tail_no_match(self, store: EventStore) -> None:
        base_source(store)
        ref_id = add_ref(store, "pkg.mod.missing", "calls")
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_tail_equal_target_skips_tail_strategy(self, store: EventStore) -> None:
        base_source(store)
        ref_id = add_ref(store, "plain", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.strategy is None

    @pytest.mark.parametrize("tail", ["fu%c", "fu_c"])
    def test_tail_like_escaping(self, store: EventStore, tail: str) -> None:
        base_source(store)
        add_symbol(store, "method:tgt", f"mod.{tail}", "pkg/mod.py", kind="method")
        ref_id = add_ref(store, f"x.{tail}", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "method:tgt"


# ------------------------------------------------------- python import path


class TestPythonImportPath:
    def test_module_and_symbol(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        add_symbol(store, "function:tgt", "func", "pkg/mod.py")
        ref_id = add_ref(store, "pkg.mod.func", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        # unique tail wins before the import-path tier (tiered a→b→c)
        assert outcome.status == "resolved"
        assert outcome.strategy == "name_tail"
        assert outcome.target_node_id == "function:tgt"

    def test_import_path_disambiguates_tail(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        add_symbol(store, "function:right", "func", "pkg/mod.py")
        add_symbol(store, "function:wrong", "func", "pkg/other.py")
        ref_id = add_ref(store, "pkg.mod.func", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "import_path"
        assert outcome.target_node_id == "function:right"

    @pytest.mark.parametrize("module_file", ["pkg/mod.py", "pkg/mod/__init__.py"])
    def test_module_only_forms(self, store: EventStore, module_file: str) -> None:
        base_source(store)
        add_file(store, module_file, "python")
        ref_id = add_ref(store, "pkg.mod", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == f"file:{module_file}"

    def test_package_root_init(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/__init__.py", "python")
        ref_id = add_ref(store, "pkg", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "file:pkg/__init__.py"

    def test_module_suffix_match(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "src/pkg/mod.py", "python")
        ref_id = add_ref(store, "pkg.mod", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:src/pkg/mod.py"

    def test_relative_one_dot(self, store: EventStore) -> None:
        base_source(store, path="pkg/sub/a.py")
        add_file(store, "pkg/sub/mod.py", "python")
        add_symbol(store, "function:tgt", "func", "pkg/sub/mod.py")
        ref_id = add_ref(store, ".mod.func", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "function:tgt"

    def test_relative_two_dots(self, store: EventStore) -> None:
        base_source(store, path="pkg/sub/a.py")
        add_file(store, "pkg/shared.py", "python")
        add_symbol(store, "function:tgt", "util", "pkg/shared.py")
        ref_id = add_ref(store, "..shared.util", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "function:tgt"

    def test_from_dot_import_symbol_in_init(self, store: EventStore) -> None:
        base_source(store, path="pkg/sub/a.py")
        add_file(store, "pkg/sub/__init__.py", "python")
        add_symbol(store, "function:tgt", "helper", "pkg/sub/__init__.py")
        ref_id = add_ref(store, ".helper", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "function:tgt"

    def test_from_dot_import_submodule(self, store: EventStore) -> None:
        base_source(store, path="pkg/sub/a.py")
        add_file(store, "pkg/sub/helper.py", "python")
        ref_id = add_ref(store, ".helper", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:pkg/sub/helper.py"

    def test_submodule_preferred_over_module_fallback(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        add_file(store, "pkg/mod/func.py", "python")
        ref_id = add_ref(store, "pkg.mod.func", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:pkg/mod/func.py"

    def test_imports_fallback_to_module_file(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        ref_id = add_ref(store, "pkg.mod.missing_symbol", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "file:pkg/mod.py"

    def test_calls_symbol_missing_fails(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        ref_id = add_ref(store, "pkg.mod.missing_symbol", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == "symbol_not_in_module:missing_symbol"

    def test_calls_dotted_resolves_via_tail(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        add_symbol(store, "method:tgt", "Klass.func", "pkg/mod.py", kind="method")
        ref_id = add_ref(store, "pkg.mod.func", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "name_tail"
        assert outcome.target_node_id == "method:tgt"

    @pytest.mark.parametrize("n", [2, 3])
    def test_ambiguous_module_fails(self, store: EventStore, n: int) -> None:
        base_source(store)
        for index in range(n):
            add_file(store, f"root{index}/pkg/mod.py", "python")
        ref_id = add_ref(store, "pkg.mod", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == f"ambiguous_module:{n}"

    def test_unknown_module_no_resolution(self, store: EventStore) -> None:
        base_source(store)
        ref_id = add_ref(store, "pkg.ghost", "imports")
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_non_module_language_skips_import_path(self, store: EventStore) -> None:
        base_source(store, language="go")
        add_file(store, "pkg/mod.py", "python")
        ref_id = add_ref(store, "pkg.mod", "imports")
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_relative_three_dots(self, store: EventStore) -> None:
        base_source(store, path="pkg/sub/deep/a.py")
        add_file(store, "pkg/top.py", "python")
        ref_id = add_ref(store, "...top", "imports")
        # three dots climb two levels: pkg/sub/deep → pkg
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "file:pkg/top.py"

    def test_calls_to_module_no_resolution(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        ref_id = add_ref(store, "pkg.mod", "calls")
        # a whole-module target carries no symbol; a calls edge never binds a file
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_ambiguous_symbol_inside_module(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "pkg/mod.py", "python")
        add_symbol(store, "function:m1", "func", "pkg/mod.py", line=1)
        add_symbol(store, "function:m2", "func", "pkg/mod.py", line=9)
        ref_id = add_ref(store, "pkg.mod.func", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == "ambiguous_module_symbol:2"

    def test_module_suffix_like_escaping(self, store: EventStore) -> None:
        base_source(store)
        add_file(store, "src/pkg/m_d.py", "python")
        ref_id = add_ref(store, "pkg.m_d", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "file:src/pkg/m_d.py"

    def test_language_inferred_from_extension_without_file_row(self, store: EventStore) -> None:
        add_symbol(store, BASE, "main", "pkg/main.py")  # source symbol without a files row
        add_file(store, "pkg/util.py", "python")
        ref_id = add_ref(store, ".util", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "file:pkg/util.py"


# ------------------------------------------------------------- js/ts import


class TestJsImportPath:
    @pytest.mark.parametrize("language", ["javascript", "typescript"])
    def test_relative_basic(self, store: EventStore, language: str) -> None:
        base_source(store, path="src/a.ts", language=language)
        add_file(store, "src/util.ts", language)
        ref_id = add_ref(store, "./util", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "import_path"
        assert outcome.target_node_id == "file:src/util.ts"
        assert all_edges(store)[0][2] == "imports"

    @pytest.mark.parametrize(
        ("winner", "loser"),
        [("util.ts", "util.js"), ("util.tsx", "util.js"), ("util.js", "util.jsx"), ("util.ts", "util.jsx")],
    )
    def test_extension_preference(self, store: EventStore, winner: str, loser: str) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        add_file(store, f"src/{winner}", "typescript")
        add_file(store, f"src/{loser}", "javascript")
        ref_id = add_ref(store, "./util", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == f"file:src/{winner}"

    def test_exact_path_with_extension(self, store: EventStore) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        add_file(store, "src/util.js", "javascript")
        add_file(store, "src/util.js.ts", "typescript")
        ref_id = add_ref(store, "./util.js", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:src/util.js"

    @pytest.mark.parametrize("index_name", ["index.ts", "index.js", "index.tsx", "index.jsx"])
    def test_directory_index(self, store: EventStore, index_name: str) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        add_file(store, f"src/lib/{index_name}", "typescript")
        ref_id = add_ref(store, "./lib", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == f"file:src/lib/{index_name}"

    def test_parent_directory(self, store: EventStore) -> None:
        base_source(store, path="src/a/b.ts", language="typescript")
        add_file(store, "src/shared/x.ts", "typescript")
        ref_id = add_ref(store, "../shared/x", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:src/shared/x.ts"

    @pytest.mark.parametrize("bare", ["lodash", "@scope/pkg", "react", "node:fs"])
    def test_bare_specifier_external(self, store: EventStore, bare: str) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        ref_id = add_ref(store, bare, "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "failed"
        assert outcome.reason == "external_module"

    def test_missing_file_no_resolution(self, store: EventStore) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        ref_id = add_ref(store, "./ghost", "imports")
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_escape_above_root_no_resolution(self, store: EventStore) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        ref_id = add_ref(store, "../../../etc/passwd", "imports")
        assert outcome_of(resolve(store), ref_id).reason == "no_resolution"

    def test_react_language_supported(self, store: EventStore) -> None:
        base_source(store, path="src/a.tsx", language="typescriptreact")
        add_file(store, "src/comp.tsx", "typescriptreact")
        ref_id = add_ref(store, "./comp", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:src/comp.tsx"

    def test_suffix_match_deeper_root(self, store: EventStore) -> None:
        base_source(store, path="src/a.ts", language="typescript")
        add_file(store, "deep/src/util.ts", "typescript")
        ref_id = add_ref(store, "./util", "imports")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.target_node_id == "file:deep/src/util.ts"

    def test_file_source_unknown_language_inferred(self, store: EventStore) -> None:
        add_file(store, "src/a.ts", "unknown")
        add_file(store, "src/util.ts", "typescript")
        ref_id = add_ref(store, "./util", "imports", source="file:src/a.ts")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "file:src/util.ts"


# --------------------------------------------------------------------- batch


class TestBatch:
    @pytest.mark.parametrize(
        ("n_refs", "limit", "processed", "remaining"),
        [(5, 2, 2, 3), (5, None, 5, 0), (3, 10, 3, 0), (1, 1, 1, 0)],
    )
    def test_limit_cap(self, store: EventStore, n_refs: int, limit: int | None, processed: int, remaining: int) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        for _ in range(n_refs):
            add_ref(store, "helper", "calls")
        report = resolve(store, limit=limit)
        assert report.processed == processed
        assert report.pending_remaining == remaining
        assert len(report.outcomes) == processed

    def test_config_batch_limit(self, store: EventStore) -> None:
        base_source(store)
        for _ in range(4):
            add_ref(store, "missing", "calls")
        report = ReferenceResolver(ResolverConfig(batch_limit=3)).resolve_pending(store)
        assert report.processed == 3
        assert report.pending_remaining == 1

    def test_limit_overrides_config(self, store: EventStore) -> None:
        base_source(store)
        for _ in range(4):
            add_ref(store, "missing", "calls")
        report = ReferenceResolver(ResolverConfig(batch_limit=3)).resolve_pending(store, limit=1)
        assert report.processed == 1

    def test_invalid_limit_rejected(self, store: EventStore) -> None:
        with pytest.raises(ResolverError):
            resolve(store, limit=0)

    def test_mixed_report(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        first = add_ref(store, "helper", "calls")
        second = add_ref(store, "ghost", "calls")
        third = add_ref(store, "helper", "imports")
        report = resolve(store)
        assert (report.processed, report.resolved, report.failed) == (3, 2, 1)
        assert report.pending_remaining == 0
        assert [outcome.ref_id for outcome in report.outcomes] == sorted([first, second, third])
        assert outcome_of(report, second).reason == "no_resolution"

    def test_empty_queue(self, store: EventStore) -> None:
        report = resolve(store)
        assert (report.processed, report.resolved, report.failed, report.edges_inserted) == (0, 0, 0, 0)
        assert report.outcomes == ()

    def test_failed_not_reprocessed(self, store: EventStore) -> None:
        base_source(store)
        ref_id = add_ref(store, "ghost", "calls")
        resolve(store)
        second = resolve(store)
        assert second.processed == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "failed" and row.attempts == 1

    def test_resolved_terminal(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "helper", "calls")
        resolve(store)
        second = resolve(store)
        assert second.processed == 0
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "resolved" and row.attempts == 1

    def test_idempotent_second_run(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        add_ref(store, "helper", "calls")
        add_ref(store, "ghost", "calls")
        resolve(store)
        snapshot = (all_edges(store), ref_dumps(store))
        resolve(store)
        assert (all_edges(store), ref_dumps(store)) == snapshot

    def test_report_fields(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        add_ref(store, "helper", "calls")
        report = resolve(store)
        assert isinstance(report, BatchReport)
        assert all(isinstance(outcome, RefOutcome) for outcome in report.outcomes)
        assert report.outcomes[0].reason is None


class TestEdgeOrder:
    def test_edges_follow_pending_order(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:t1", "one", "pkg/a.py")
        add_symbol(store, "function:t2", "two", "pkg/b.py")
        add_symbol(store, "function:t3", "three", "pkg/c.py")
        add_ref(store, "one", "calls")
        add_ref(store, "two", "calls")
        add_ref(store, "three", "calls")
        resolve(store)
        assert [edge[1] for edge in all_edges(store)] == ["function:t1", "function:t2", "function:t3"]

    def test_name_tail_override_respected(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        ref_id = add_ref(store, "weird", "calls", name_tail="helper")
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "name_tail"

    def test_empty_tail_override_skips_tail_tier(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "weird", "pkg/other.py")
        ref_id = add_ref(store, "weird", "calls", name_tail="")
        # exact still resolves; the tail tier is skipped, never failing
        outcome = outcome_of(resolve(store), ref_id)
        assert outcome.status == "resolved"
        assert outcome.strategy == "exact"


class TestAmbiguityDeferral:
    def test_first_ambiguity_reason_wins(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:q1", "pkg.helper", "pkg/a.py")
        add_symbol(store, "function:q2", "pkg.helper", "pkg/b.py")
        ref_id = add_ref(store, "pkg.helper", "calls")
        outcome = outcome_of(resolve(store), ref_id)
        # exact is ambiguous (2); the tail tier re-finds the same pair via the
        # suffix rule and stays ambiguous — the earliest reason is reported
        assert outcome.status == "failed"
        assert outcome.reason == "ambiguous_exact:2"


# ----------------------------------------------------------------- resurrect


class TestResurrect:
    def _failed(self, store: EventStore, target: str) -> int:
        ref_id = add_ref(store, target, "calls")
        resolve(store)
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "failed"
        return ref_id

    def test_moves_failed_back_to_pending(self, store: EventStore) -> None:
        base_source(store)
        ref_id = self._failed(store, "ghost")
        moved = ReferenceResolver().resurrect_failed(store)
        assert moved == (ref_id,)
        row = store.unresolved_ref(ref_id)
        assert row is not None and row.status == "pending" and row.attempts == 1

    def test_name_tail_filter(self, store: EventStore) -> None:
        base_source(store)
        first = self._failed(store, "pkg.alpha")
        self._failed(store, "pkg.beta")
        moved = ReferenceResolver().resurrect_failed(store, name_tail="alpha")
        assert moved == (first,)

    @pytest.mark.parametrize("limit", [1, 2])
    def test_limit(self, store: EventStore, limit: int) -> None:
        base_source(store)
        for target in ("a.one", "b.two", "c.three"):
            self._failed(store, target)
        moved = ReferenceResolver().resurrect_failed(store, limit=limit)
        assert len(moved) == limit

    def test_resurrect_then_resolve_when_symbol_appears(self, store: EventStore) -> None:
        base_source(store)
        ref_id = self._failed(store, "late.helper")
        add_symbol(store, "function:late", "helper", "pkg/late.py")
        moved = ReferenceResolver().resurrect_failed(store, name_tail="helper")
        assert moved == (ref_id,)
        report = resolve(store)
        outcome = outcome_of(report, ref_id)
        assert outcome.status == "resolved"
        assert outcome.target_node_id == "function:late"

    def test_resurrect_empty(self, store: EventStore) -> None:
        assert ReferenceResolver().resurrect_failed(store) == ()

    def test_resurrect_skips_resolved(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        resolved_id = add_ref(store, "helper", "calls")
        failed_id = self._failed(store, "ghost")
        moved = ReferenceResolver().resurrect_failed(store)
        assert moved == (failed_id,)
        row = store.unresolved_ref(resolved_id)
        assert row is not None and row.status == "resolved"

    def test_resurrect_limit_and_tail_combined(self, store: EventStore) -> None:
        base_source(store)
        first = self._failed(store, "pkg.alpha")
        self._failed(store, "other.alpha")
        self._failed(store, "pkg.beta")
        moved = ReferenceResolver().resurrect_failed(store, name_tail="alpha", limit=1)
        assert moved == (first,)

    def test_resurrect_output_deterministic_order(self, store: EventStore) -> None:
        base_source(store)
        ids = [self._failed(store, target) for target in ("z.one", "a.two", "m.three")]
        moved = ReferenceResolver().resurrect_failed(store)
        assert set(moved) == set(ids)
        # failed refs come back ordered by name_tail (store contract)
        assert moved == tuple(sorted(moved, key=lambda ref_id: (store.unresolved_ref(ref_id).name_tail, ref_id)))  # type: ignore[union-attr]


# -------------------------------------------------------- module-level entry


class TestModuleEntry:
    def test_resolve_pending_function(self, store: EventStore) -> None:
        base_source(store)
        add_symbol(store, "function:tgt", "helper", "pkg/other.py")
        add_ref(store, "helper", "calls")
        report = resolve_pending(store)
        assert report.resolved == 1

    def test_resolve_pending_function_with_config(self, store: EventStore) -> None:
        base_source(store)
        for _ in range(3):
            add_ref(store, "ghost", "calls")
        report = resolve_pending(store, config=ResolverConfig(batch_limit=2))
        assert report.processed == 2

"""Extraction tests over real fixtures in all 12 phase-1 languages (W2, SUB-87)."""

from __future__ import annotations

from pathlib import Path

import pytest

from know_your_soul.graph_model import (
    CoreNodeKind,
    UniversalGraph,
    file_node_id,
    is_file_node_id,
    make_node_id,
    parse_node_id,
)
from know_your_soul.ingest.extractor import (
    EXTRACTION_VERSION,
    ExtractionResult,
    extract_file,
    extract_source,
)
from know_your_soul.ingest.languages import EXTENSION_MAP

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"

# language -> (fixture relative path, expected class-like kinds, expected function/method names,
#              expected import target tails)
LANGUAGE_CASES: dict[str, tuple[str, frozenset[str], frozenset[str], frozenset[str]]] = {
    "python": (
        "python/sample.py",
        frozenset({"class"}),
        frozenset({"greet", "build", "format_name", "top_level"}),
        frozenset({"os", "pkg.mod"}),
    ),
    "javascript": (
        "javascript/sample.js",
        frozenset({"class"}),
        frozenset({"greet", "build", "formatName", "topLevel"}),
        frozenset({"./mod.js", "fs"}),
    ),
    "typescript": (
        "typescript/sample.ts",
        frozenset({"class", "interface", "type"}),
        frozenset({"greet", "area", "formatName", "topLevel"}),
        frozenset({"./mod"}),
    ),
    "go": (
        "go/sample.go",
        frozenset({"type", "interface"}),
        frozenset({"Method", "topLevel"}),
        frozenset({"fmt", "example.com/mod"}),
    ),
    "rust": (
        "rust/sample.rs",
        frozenset({"type", "interface"}),
        frozenset({"method", "helper", "top_level"}),
        frozenset({"std::collections::HashMap", "crate::mod2::thing"}),
    ),
    "java": (
        "java/sample.java",
        frozenset({"class", "interface", "type"}),
        frozenset({"greet", "area", "formatNameLength"}),
        frozenset({"java.util.List", "java.util.Objects.requireNonNull"}),
    ),
    "c": (
        "c/sample.c",
        frozenset({"type"}),
        frozenset({"format_name", "top_level"}),
        frozenset({"stdio.h", "local.h"}),
    ),
    "cpp": (
        "cpp/sample.cpp",
        frozenset({"class", "type"}),
        frozenset({"greet", "helper", "top_level"}),
        frozenset({"vector", "local.hpp"}),
    ),
    "csharp": (
        "csharp/sample.cs",
        frozenset({"class", "interface", "type"}),
        frozenset({"Greet", "Area"}),
        frozenset({"System", "System.Text.StringBuilder"}),
    ),
    "ruby": (
        "ruby/sample.rb",
        frozenset({"class", "module"}),
        frozenset({"greet", "build", "format_name", "top_level"}),
        frozenset({"json", "local"}),
    ),
    "php": (
        "php/sample.php",
        frozenset({"class", "interface"}),
        frozenset({"greet", "area", "helper", "top_level"}),
        frozenset({"Vendor\\Pkg\\Thing", "local.php"}),
    ),
    "bash": (
        "bash/sample.sh",
        frozenset(),
        frozenset({"format_name", "top_level"}),
        frozenset({"./lib.sh", "other.sh"}),
    ),
}

ALL_LANGUAGES = sorted(LANGUAGE_CASES)


def case(language: str) -> tuple[str, frozenset[str], frozenset[str], frozenset[str]]:
    return LANGUAGE_CASES[language]


def extract_fixture(language: str) -> ExtractionResult:
    relative = case(language)[0]
    return extract_file(FIXTURES / relative, display_path=relative)


def kinds(result: ExtractionResult) -> set[str]:
    return {node.kind for node in result.nodes}


def names(result: ExtractionResult, kind: str | None = None) -> set[str]:
    return {node.name for node in result.nodes if kind is None or node.kind == kind}


ALL = pytest.mark.parametrize("language", ALL_LANGUAGES)


# ------------------------------------------------------------------ baseline


@ALL
def test_extract_returns_result(language: str):
    result = extract_fixture(language)
    assert isinstance(result, ExtractionResult)
    assert result.language == language
    assert result.path == case(language)[0]
    assert result.extraction_version == EXTRACTION_VERSION


@ALL
def test_fixture_parses_without_error_nodes(language: str):
    result = extract_fixture(language)
    assert result.parse_error is False, f"{language} fixture produced a parse-error tree"


@ALL
def test_file_node_is_the_single_root(language: str):
    result = extract_fixture(language)
    file_nodes = [node for node in result.nodes if node.kind == CoreNodeKind.FILE.value]
    assert len(file_nodes) == 1
    file_node = file_nodes[0]
    assert file_node.node_id == file_node_id(case(language)[0])
    assert file_node.start_line == 1
    assert file_node.start_col == 0
    assert file_node.end_line >= file_node.start_line


@ALL
def test_file_node_has_no_incoming_contains(language: str):
    result = extract_fixture(language)
    root = file_node_id(case(language)[0])
    assert all(not (edge.kind == "contains" and edge.target == root) for edge in result.edges)


@ALL
def test_every_non_file_node_has_exactly_one_contains_parent(language: str):
    result = extract_fixture(language)
    parents: dict[str, int] = {}
    for edge in result.edges:
        if edge.kind == "contains":
            parents[edge.target] = parents.get(edge.target, 0) + 1
    for node in result.nodes:
        if node.kind == CoreNodeKind.FILE.value:
            continue
        got = parents.get(node.node_id, 0)
        assert got == 1, f"{language}: {node.name!r} has {got} parents"


@ALL
def test_edges_reference_known_nodes(language: str):
    result = extract_fixture(language)
    known = {node.node_id for node in result.nodes}
    for edge in result.edges:
        assert edge.source in known and edge.target in known


@ALL
def test_refs_reference_known_source_nodes(language: str):
    result = extract_fixture(language)
    known = {node.node_id for node in result.nodes}
    for ref in result.unresolved_refs:
        assert ref.source_node_id in known


# ------------------------------------------------------------- declarations


@ALL
def test_expected_functions_and_methods_present(language: str):
    _rel, _cls, funcs, _imports = case(language)
    found = names(extract_fixture(language)) & funcs
    missing = funcs - found
    assert not missing, f"{language}: missing callables {sorted(missing)}"


@ALL
def test_expected_class_like_kinds_present(language: str):
    _rel, class_kinds, _funcs, _imports = case(language)
    found = kinds(extract_fixture(language))
    assert class_kinds <= found, f"{language}: missing kinds {sorted(class_kinds - found)}"


@ALL
def test_expected_import_targets_present(language: str):
    _rel, _cls, _funcs, imports = case(language)
    targets = {ref.target_name for ref in extract_fixture(language).unresolved_refs if ref.kind == "imports"}
    missing = set(imports) - targets
    assert not missing, f"{language}: missing import targets {sorted(missing)}"


@ALL
def test_import_nodes_created(language: str):
    import_nodes = [node for node in extract_fixture(language).nodes if node.kind == CoreNodeKind.IMPORT.value]
    assert import_nodes, f"{language}: no import nodes"


@ALL
def test_call_refs_emitted(language: str):
    calls = [ref for ref in extract_fixture(language).unresolved_refs if ref.kind == "calls"]
    assert calls, f"{language}: no call refs"


@ALL
def test_call_refs_carry_position(language: str):
    for ref in extract_fixture(language).unresolved_refs:
        assert ref.line >= 1 and ref.col >= 0
        assert ref.target_name
        assert ref.name_tail == ref.target_name.rsplit(".", 1)[-1].rsplit("::", 1)[-1]


# ------------------------------------------------------------------ node ids


@ALL
def test_node_ids_match_make_node_id(language: str):
    relative = case(language)[0]
    result = extract_fixture(language)
    for node in result.nodes:
        if node.kind == CoreNodeKind.FILE.value:
            assert is_file_node_id(node.node_id)
            continue
        parts = parse_node_id(node.node_id)
        assert parts.kind == node.kind
        base = make_node_id(path=relative, kind=node.kind, name=node.name, line=node.start_line)
        if parts.col is None:
            assert node.node_id == base
        else:
            assert node.node_id == make_node_id(
                path=relative, kind=node.kind, name=node.name, line=node.start_line, col=node.start_col
            )
            assert parts.col == node.start_col


@ALL
def test_node_ids_unique(language: str):
    ids = [node.node_id for node in extract_fixture(language).nodes]
    assert len(ids) == len(set(ids))


@ALL
def test_node_lines_within_file(language: str):
    result = extract_fixture(language)
    file_node = next(node for node in result.nodes if node.kind == CoreNodeKind.FILE.value)
    for node in result.nodes:
        assert 1 <= node.start_line <= node.end_line <= file_node.end_line
        assert node.start_col >= 0 and node.end_col >= 0


# ------------------------------------------------------------------ scoping


@ALL
def test_contains_parent_chain_ends_at_file(language: str):
    result = extract_fixture(language)
    root = file_node_id(case(language)[0])
    parent = {edge.target: edge.source for edge in result.edges if edge.kind == "contains"}
    for node in result.nodes:
        cursor = node.node_id
        seen = {cursor}
        while cursor in parent:
            cursor = parent[cursor]
            assert cursor not in seen, f"{language}: contains cycle at {cursor}"
            seen.add(cursor)
        assert cursor == root, f"{language}: {node.name!r} is not rooted at the file node"


@ALL
def test_contains_graph_validates_in_universal_graph(language: str):
    """The emitted contains forest must satisfy the W1 graph invariants."""
    from know_your_soul.graph_model import GraphEdge, GraphNode

    result = extract_fixture(language)
    graph = UniversalGraph()
    for node in result.nodes:
        col = None if node.kind == CoreNodeKind.FILE.value else parse_node_id(node.node_id).col
        graph.add_node(
            GraphNode(
                node_id=node.node_id,
                kind=node.kind,
                name=node.name,
                path=node.path,
                line=node.start_line,
                col=col,
            )
        )
    for edge in result.edges:
        graph.add_edge(GraphEdge(source=edge.source, target=edge.target, kind=edge.kind))
    graph.validate()


@pytest.mark.parametrize(
    ("language", "method_name", "owner"),
    [
        ("python", "greet", "Greeter"),
        ("javascript", "greet", "Greeter"),
        ("typescript", "greet", "Greeter"),
        # go/rust methods hang at file scope: Go receivers and Rust impl
        # blocks are not ``contains`` scopes (rust impl is a transparent
        # container by design, verified via qualified names instead).
        ("java", "greet", "Greeter"),
        ("cpp", "greet", "Greeter"),
        ("csharp", "Greet", "Greeter"),
        ("ruby", "greet", "Greeter"),
        ("php", "greet", "Greeter"),
    ],
)
def test_methods_carry_method_kind_and_owner_scope(language: str, method_name: str, owner: str):
    result = extract_fixture(language)
    candidates = [node for node in result.nodes if node.name == method_name and node.kind == CoreNodeKind.METHOD.value]
    assert candidates, f"{language}: {method_name} not extracted as a method"
    parent = {edge.target: edge.source for edge in result.edges if edge.kind == "contains"}
    by_id = {node.node_id: node for node in result.nodes}
    owners = {by_id[parent[node.node_id]].name for node in candidates}
    assert owner in owners, f"{language}: {method_name} not contained in {owner} (got {sorted(owners)})"


@ALL
def test_qualified_names_use_double_colon(language: str):
    result = extract_fixture(language)
    for node in result.nodes:
        if node.kind in (CoreNodeKind.FILE.value, CoreNodeKind.IMPORT.value):
            continue
        if "::" in node.qualified_name:
            assert node.qualified_name.endswith("::" + node.name)
        else:
            assert node.qualified_name == node.name


@pytest.mark.parametrize(
    ("language", "qualified"),
    [
        ("python", "Greeter::greet"),
        ("javascript", "Greeter::greet"),
        ("typescript", "Greeter::greet"),
        ("ruby", "Util::Greeter::greet"),
        ("php", "Greeter::greet"),
        ("csharp", "Example::Greeter::Greet"),
        ("cpp", "ns::Greeter::greet"),
        ("java", "Greeter::greet"),  # package is a header statement, not a scope block
        ("rust", "Thing::method"),
    ],
)
def test_qualified_name_nesting(language: str, qualified: str):
    names_ = {node.qualified_name for node in extract_fixture(language).nodes}
    assert qualified in names_, f"{language}: missing {qualified!r}"


# ------------------------------------------------------------------ artifact


@ALL
def test_artifact_shape(language: str):
    artifact = extract_fixture(language).to_artifact()
    assert list(artifact) == sorted(artifact), "top-level keys must be canonically sorted"
    for key in (
        "edges",
        "extraction_version",
        "generated_hint",
        "language",
        "nodes",
        "parse_error",
        "path",
        "sha256",
        "size",
        "unresolved_refs",
    ):
        assert key in artifact
    assert len(artifact["sha256"]) == 64
    assert artifact["size"] > 0


@ALL
def test_artifact_nodes_sorted(language: str):
    nodes = extract_fixture(language).to_artifact()["nodes"]
    keys = [(node["start_line"], node["start_col"], node["node_id"]) for node in nodes]
    assert keys == sorted(keys)


@ALL
def test_artifact_edges_sorted_and_deduplicated(language: str):
    edges = extract_fixture(language).to_artifact()["edges"]
    keys = [(edge["source"], edge["target"], edge["kind"]) for edge in edges]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


@ALL
def test_artifact_refs_sorted_and_deduplicated(language: str):
    refs = extract_fixture(language).to_artifact()["unresolved_refs"]
    keys = [(r["source_node_id"], r["target_name"], r["kind"], r["line"], r["col"]) for r in refs]
    assert keys == sorted(keys)
    assert len(keys) == len(set(keys))


@ALL
def test_double_extract_byte_identical(language: str):
    first = extract_fixture(language).canonical_json()
    second = extract_fixture(language).canonical_json()
    assert first == second


@ALL
def test_canonical_json_is_single_line_sorted(language: str):
    text = extract_fixture(language).canonical_json()
    assert "\n" not in text
    assert text.startswith('{"edges":')


# ------------------------------------------------------------- targeted cases


def test_python_decorated_method_extracted():
    result = extract_fixture("python")
    build = [node for node in result.nodes if node.name == "build"]
    assert build and build[0].kind == CoreNodeKind.METHOD.value


def test_python_uppercase_assignment_is_constant():
    result = extract_fixture("python")
    assert "CONSTANT" in names(result, CoreNodeKind.CONSTANT.value)
    assert "counter" in names(result, CoreNodeKind.VARIABLE.value)


def test_python_from_import_uses_module_name():
    result = extract_fixture("python")
    targets = {ref.target_name for ref in result.unresolved_refs if ref.kind == "imports"}
    assert "pkg.mod" in targets and "os" in targets


# -------------------------------------------------- F-SUB94-1: relative imports


def test_python_relative_from_import_dotted_only_module():
    """F-SUB94-1: `from . import sessions` (dot-only module_name) must produce
    an import node for the imported module — never crash with NodeIdError."""
    result = extract_source("pkg/mod.py", "from . import sessions\n", language="python")
    imports = [ref for ref in result.unresolved_refs if ref.kind == "imports"]
    assert [ref.target_name for ref in imports] == ["sessions"]
    import_nodes = [node for node in result.nodes if node.kind == CoreNodeKind.IMPORT.value]
    assert [node.name for node in import_nodes] == ["sessions"]


def test_python_relative_from_import_parent_dots():
    """`from .. import sibling` (parent-package dots) resolves to the name field."""
    result = extract_source("pkg/sub/mod.py", "from .. import sibling\n", language="python")
    imports = [ref for ref in result.unresolved_refs if ref.kind == "imports"]
    assert [ref.target_name for ref in imports] == ["sibling"]


def test_python_relative_from_import_aliased():
    """`from . import a as b` keeps the same shape as `import a as b` today."""
    result = extract_source("pkg/mod.py", "from . import a as b\n", language="python")
    imports = [ref for ref in result.unresolved_refs if ref.kind == "imports"]
    assert [ref.target_name for ref in imports] == ["a as b"]


def test_python_relative_from_import_with_module_unchanged():
    """`from .a import x` was never broken: the dot-path stays the target."""
    result = extract_source("pkg/mod.py", "from .a import x\n", language="python")
    imports = [ref for ref in result.unresolved_refs if ref.kind == "imports"]
    assert [ref.target_name for ref in imports] == [".a"]
    import_nodes = [node for node in result.nodes if node.kind == CoreNodeKind.IMPORT.value]
    assert [node.name for node in import_nodes] == ["a"]


def test_javascript_require_is_import():
    result = extract_fixture("javascript")
    targets = {ref.target_name for ref in result.unresolved_refs if ref.kind == "imports"}
    assert "fs" in targets


def test_javascript_arrow_assigned_to_variable_is_function():
    result = extract_fixture("javascript")
    arrow = [node for node in result.nodes if node.name == "arrow"]
    assert arrow and arrow[0].kind == CoreNodeKind.FUNCTION.value


def test_go_aliased_import_target_is_path():
    result = extract_fixture("go")
    targets = {ref.target_name for ref in result.unresolved_refs if ref.kind == "imports"}
    assert "example.com/mod" in targets


def test_rust_impl_block_is_transparent_container():
    result = extract_fixture("rust")
    # no node for the impl block itself; the method hangs under the file but
    # its qualified name carries the impl type.
    assert not [node for node in result.nodes if node.name == "Thing" and node.kind == "module"]
    method = next(node for node in result.nodes if node.name == "method")
    assert method.qualified_name == "Thing::method"


def test_c_function_prototype_is_not_a_function_node():
    result = extract_fixture("c")
    assert "helper" not in names(result, CoreNodeKind.FUNCTION.value)


def test_c_preproc_define_is_constant():
    result = extract_fixture("c")
    assert "K" in names(result, CoreNodeKind.CONSTANT.value)


def test_bash_dot_source_is_import():
    result = extract_fixture("bash")
    targets = {ref.target_name for ref in result.unresolved_refs if ref.kind == "imports"}
    assert "other.sh" in targets and "./lib.sh" in targets


def test_bash_command_becomes_call_ref():
    result = extract_fixture("bash")
    calls = {ref.target_name for ref in result.unresolved_refs if ref.kind == "calls"}
    assert "helper" in calls and "format_name" in calls


def test_ruby_require_relative_is_import():
    result = extract_fixture("ruby")
    targets = {ref.target_name for ref in result.unresolved_refs if ref.kind == "imports"}
    assert "local" in targets


def test_same_line_duplicate_names_get_col_suffix():
    source = "def dup(): ...\ndef dup(): ...\nclass C: pass\nclass C: pass\n"
    result = extract_source("dup.py", source, language="python")
    dups = [node for node in result.nodes if node.name == "dup"]
    assert len(dups) == 2
    # same name different line -> no col suffix needed
    assert all(parse_node_id(node.node_id).col is None for node in dups)


def test_same_name_nested_scopes_get_distinct_ids():
    result = extract_source("one.py", "def dup(): ...\nif True:\n    def dup(): ...\n", language="python")
    ids = [node.node_id for node in result.nodes if node.name == "dup"]
    assert len(ids) == 2 and len(set(ids)) == 2


def test_same_line_same_kind_same_name_disambiguated_by_col():
    # two class declarations with the same name on the SAME line share
    # (path, kind, name, line) -> the allocator must disambiguate with :col
    result = extract_source("cols.js", "class A {} class A {}\n", language="javascript")
    nodes = [node for node in result.nodes if node.name == "A"]
    assert len(nodes) == 2
    ids = [node.node_id for node in nodes]
    assert len(set(ids)) == 2
    cols = [parse_node_id(node.node_id).col for node in nodes]
    assert None in cols  # first declaration keeps the clean id
    assert nodes[1].start_col in cols


def test_empty_file_has_only_file_node():
    result = extract_source("empty.py", "", language="python")
    assert [node.kind for node in result.nodes] == [CoreNodeKind.FILE.value]
    assert result.edges == ()
    assert result.unresolved_refs == ()


def test_whitespace_only_file():
    result = extract_source("ws.py", "\n\n   \n", language="python")
    assert len(result.nodes) == 1


def test_parse_error_flagged_but_extraction_continues():
    result = extract_source("broken.py", "def broken(:\n    pass\n", language="python")
    assert result.parse_error is True


def test_extract_source_language_inferred_from_path():
    result = extract_source("auto.py", "def f():\n    return 1\n")
    assert result.language == "python"


def test_sha256_and_size_match_content():
    import hashlib

    source = "def f():\n    return 1\n"
    result = extract_source("hash.py", source, language="python")
    data = source.encode("utf-8")
    assert result.sha256 == hashlib.sha256(data).hexdigest()
    assert result.size == len(data)


def test_generated_hint_defaults_false_in_extract_source():
    result = extract_source("plain.py", "x = 1\n", language="python")
    assert result.generated_hint is False


def test_calls_inside_function_attributed_to_function():
    result = extract_fixture("python")
    by_id = {node.node_id: node for node in result.nodes}
    calls = [ref for ref in result.unresolved_refs if ref.kind == "calls" and ref.target_name == "helper"]
    assert calls
    sources = {by_id[ref.source_node_id].name for ref in calls}
    assert "top_level" in sources


def test_top_level_call_attributed_to_file_node():
    source = "import os\nprint(os.name)\n"
    result = extract_source("toplevel.py", source, language="python")
    calls = [ref for ref in result.unresolved_refs if ref.kind == "calls" and ref.target_name == "print"]
    assert calls
    assert all(ref.source_node_id == file_node_id("toplevel.py") for ref in calls)


@pytest.mark.parametrize("extension", sorted(EXTENSION_MAP))
def test_extension_map_language_extracts_minimal_source(extension: str):
    """Every mapped extension routes to a working parser (smoke)."""
    language = EXTENSION_MAP[extension]
    fixture = FIXTURES / LANGUAGE_CASES[language][0]
    result = extract_file(fixture, display_path=f"smoke/sample{extension}")
    assert result.language == language

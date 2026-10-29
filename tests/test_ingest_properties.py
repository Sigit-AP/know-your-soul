"""Property tests for the ingest extractor (W2, SUB-87).

Properties under test:
* determinism — same input, byte-identical artifact, also across processes
  with different ``PYTHONHASHSEED``;
* contains-forest invariant — the emitted ``contains`` edges always form a
  valid forest rooted at the file node (checked with the W1
  :class:`UniversalGraph` invariant engine, which fails closed on cycles and
  multiple parents);
* id stability — appending trailing content never changes earlier node ids.
"""

from __future__ import annotations

import keyword
import os
import subprocess
import sys
from pathlib import Path

import pytest
from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from know_your_soul.graph_model import UniversalGraph, parse_node_id
from know_your_soul.ingest.extractor import ExtractionResult, extract_file, extract_source

FIXTURES = Path(__file__).parent / "fixtures" / "ingest"

FIXTURE_CASES = {
    "python": "python/sample.py",
    "javascript": "javascript/sample.js",
    "typescript": "typescript/sample.ts",
    "go": "go/sample.go",
    "rust": "rust/sample.rs",
    "java": "java/sample.java",
    "c": "c/sample.c",
    "cpp": "cpp/sample.cpp",
    "csharp": "csharp/sample.cs",
    "ruby": "ruby/sample.rb",
    "php": "php/sample.php",
    "bash": "bash/sample.sh",
}

ALL = pytest.mark.parametrize("language", sorted(FIXTURE_CASES))


def build_graph(result: ExtractionResult) -> None:
    """Feed the artifact into the W1 invariant engine; any violation raises."""
    from know_your_soul.graph_model import GraphEdge as GE
    from know_your_soul.graph_model import GraphNode as GN

    graph = UniversalGraph()
    for node in result.nodes:
        col = None if node.kind == "file" else parse_node_id(node.node_id).col
        graph.add_node(
            GN(node_id=node.node_id, kind=node.kind, name=node.name, path=node.path, line=node.start_line, col=col)
        )
    for edge in result.edges:
        graph.add_edge(GE(source=edge.source, target=edge.target, kind=edge.kind))
    graph.validate()


# --------------------------------------------------------------- determinism


@ALL
def test_determinism_across_10_runs(language: str):
    reference = extract_file(FIXTURES / FIXTURE_CASES[language], display_path=FIXTURE_CASES[language]).canonical_json()
    for _ in range(9):
        again = extract_file(FIXTURES / FIXTURE_CASES[language], display_path=FIXTURE_CASES[language]).canonical_json()
        assert again == reference


@ALL
def test_determinism_independent_of_extraction_order(language: str):
    """Extracting other files in between must not change a file's artifact."""
    target = FIXTURE_CASES[language]
    first = extract_file(FIXTURES / target, display_path=target).canonical_json()
    for other in sorted(set(FIXTURE_CASES.values()) - {target}):
        extract_file(FIXTURES / other, display_path=other)
    second = extract_file(FIXTURES / target, display_path=target).canonical_json()
    assert first == second


@pytest.mark.parametrize("language", ["python", "javascript", "go", "rust"])
@pytest.mark.parametrize("seed", ["0", "1", "42", "12345"])
def test_byte_identical_across_pythonhashseed(language: str, seed: str):
    """Spawn a fresh interpreter under a foreign hash seed; bytes must match."""
    fixture = FIXTURE_CASES[language]
    code = (
        "from know_your_soul.ingest.extractor import extract_file;"
        f"print(extract_file({str(FIXTURES / fixture)!r}, display_path={fixture!r}).canonical_json())"
    )
    env = dict(os.environ, PYTHONHASHSEED=seed, PYTHONPATH=str(Path(__file__).parent.parent / "src"))
    run = subprocess.run(
        [sys.executable, "-c", code],
        check=True,
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
    )
    expected = extract_file(FIXTURES / fixture, display_path=fixture).canonical_json()
    assert run.stdout.strip() == expected


# --------------------------------------------------------- contains invariant


@ALL
def test_contains_forest_validates(language: str):
    build_graph(extract_file(FIXTURES / FIXTURE_CASES[language], display_path=FIXTURE_CASES[language]))


@ALL
def test_no_contains_self_loops(language: str):
    result = extract_file(FIXTURES / FIXTURE_CASES[language], display_path=FIXTURE_CASES[language])
    assert all(edge.source != edge.target for edge in result.edges)


@ALL
def test_contains_edges_only_kind_emitted(language: str):
    """The extractor resolves nothing: the only edges are ``contains``."""
    result = extract_file(FIXTURES / FIXTURE_CASES[language], display_path=FIXTURE_CASES[language])
    assert {edge.kind for edge in result.edges} <= {"contains"}


# -------------------------------------------------------------- id stability


@ALL
def test_ids_stable_when_content_appended(language: str):
    relative = FIXTURE_CASES[language]
    source = (FIXTURES / relative).read_text(encoding="utf-8")
    before = extract_source(relative, source)
    after = extract_source(relative, source + "\n\n# trailing comment\n\n\n")
    before_ids = {node.node_id for node in before.nodes}
    after_ids = {node.node_id for node in after.nodes}
    missing = before_ids - {file for file in after_ids}
    assert not missing, f"{language}: appending trailing lines changed ids"


@ALL
def test_ids_change_when_path_changes(language: str):
    relative = FIXTURE_CASES[language]
    source = (FIXTURES / relative).read_text(encoding="utf-8")
    original = extract_source(relative, source)
    moved = extract_source("moved/" + relative, source)
    original_symbols = {node.node_id for node in original.nodes if node.kind != "file"}
    moved_symbols = {node.node_id for node in moved.nodes if node.kind != "file"}
    assert original_symbols.isdisjoint(moved_symbols), "node ids must embed the path"


@ALL
def test_node_id_digest_matches_contract(language: str):
    """Every symbol id re-derives from (path, kind, name, line[, col])."""
    import hashlib

    relative = FIXTURE_CASES[language]
    result = extract_source(relative, (FIXTURES / relative).read_text(encoding="utf-8"))
    for node in result.nodes:
        if node.kind == "file":
            assert node.node_id == f"file:{relative}"
            continue
        parts = parse_node_id(node.node_id)
        preimage = f"{relative}:{node.kind}:{node.name}:{node.start_line}"
        digest = hashlib.sha256(preimage.encode("utf-8")).hexdigest()[:32]
        assert parts.digest == digest


# ----------------------------------------------------------- hypothesis: fuzz


# Keywords excluded: `def if():` is invalid Python; tree-sitter error recovery
# on invalid sources differs between parser cores (documented residual in
# docs/recovery/w6-f1-parity.md).
_IDENTIFIER = st.from_regex(r"[a-z][a-z0-9_]{0,12}", fullmatch=True).filter(lambda s: s not in keyword.kwlist)
_ARG_COUNT = st.integers(min_value=0, max_value=3)


def _python_source(functions: list[tuple[str, int]], classes: list[str]) -> str:
    lines: list[str] = ["import os", ""]
    for cls in classes:
        lines.append(f"class {cls.title().replace('_', '')}:")
        lines.append("    def method(self):")
        lines.append("        return helper()")
        lines.append("")
    for name, argc in functions:
        params = ", ".join(f"a{i}" for i in range(argc))
        lines.append(f"def {name}({params}):")
        lines.append(f"    return os.path.join({params or '1'})")
        lines.append("")
    return "\n".join(lines)


@given(
    functions=st.lists(st.tuples(_IDENTIFIER, _ARG_COUNT), min_size=1, max_size=6, unique_by=lambda f: f[0]),
    classes=st.lists(_IDENTIFIER, min_size=0, max_size=3, unique=True),
)
@settings(max_examples=40, suppress_health_check=[HealthCheck.too_slow], deadline=None)
def test_fuzz_python_invariants(functions: list[tuple[str, int]], classes: list[str]):
    source = _python_source(functions, classes)
    result = extract_source("fuzz/gen.py", source, language="python")
    # determinism
    assert result.canonical_json() == extract_source("fuzz/gen.py", source, language="python").canonical_json()
    # contains forest validates
    build_graph(result)
    # every declared function is found exactly once at top level
    found = {node.name for node in result.nodes if node.kind == "function"}
    for name, _argc in functions:
        assert name in found
    # ids unique
    ids = [node.node_id for node in result.nodes]
    assert len(ids) == len(set(ids))


@given(blank_lines=st.lists(st.sampled_from(["", " ", "\t", "# comment"]), min_size=0, max_size=12))
@settings(max_examples=30, deadline=None)
def test_fuzz_blank_python_files(blank_lines: list[str]):
    source = "\n".join(blank_lines)
    result = extract_source("fuzz/blank.py", source, language="python")
    assert result.canonical_json() == extract_source("fuzz/blank.py", source, language="python").canonical_json()
    build_graph(result)
    assert [node.kind for node in result.nodes] == ["file"]


@given(seed_text=st.text(alphabet=st.characters(whitelist_categories=("L", "N", "P", "Z")), max_size=400))
@settings(max_examples=30, deadline=None)
def test_fuzz_arbitrary_text_never_crashes(seed_text: str):
    """Arbitrary unicode text must extract deterministically (parse errors OK)."""
    first = extract_source("fuzz/blob.js", seed_text, language="javascript")
    second = extract_source("fuzz/blob.js", seed_text, language="javascript")
    assert first.canonical_json() == second.canonical_json()
    build_graph(first)


# --------------------------------------------------------- cross-fixture corpus


def test_whole_fixture_corpus_artifact_hash_stable():
    """One canonical digest over all 12 fixtures; any drift fails loud."""
    import hashlib

    combined = hashlib.sha256()
    for language in sorted(FIXTURE_CASES):
        relative = FIXTURE_CASES[language]
        digest = hashlib.sha256(
            extract_file(FIXTURES / relative, display_path=relative).canonical_json().encode("utf-8")
        ).hexdigest()
        combined.update(digest.encode("ascii"))
    assert combined.hexdigest() == extract_corpus_digest()


def extract_corpus_digest() -> str:
    import hashlib

    combined = hashlib.sha256()
    for language in sorted(FIXTURE_CASES):
        relative = FIXTURE_CASES[language]
        digest = hashlib.sha256(
            extract_file(FIXTURES / relative, display_path=relative).canonical_json().encode("utf-8")
        ).hexdigest()
        combined.update(digest.encode("ascii"))
    return combined.hexdigest()

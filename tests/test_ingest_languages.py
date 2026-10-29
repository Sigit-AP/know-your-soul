"""Data-registry tests for the ingest language registry (W2, SUB-87).

``languages.py`` is pure data: frozen dataclasses and tuples only. These
tests pin the shape of that data against the frozen contract vocabulary
(``CoreNodeKind``) and the real grammars shipped in
``tree-sitter-language-pack``.
"""

from __future__ import annotations

import dataclasses
import re

import pytest
from tree_sitter_language_pack import get_parser

from know_your_soul.graph_model import CoreNodeKind
from know_your_soul.ingest.languages import (
    EXTENSION_MAP,
    LANGUAGES,
    ImportRule,
    LanguageSpec,
    get_spec,
    language_for_path,
    supported_languages,
)

EXPECTED_LANGUAGES = (
    "bash",
    "c",
    "cpp",
    "csharp",
    "dart",
    "go",
    "haskell",
    "java",
    "javascript",
    "kotlin",
    "lua",
    "perl",
    "php",
    "python",
    "ruby",
    "rust",
    "scala",
    "swift",
    "typescript",
)

#: Languages served by the Rust kernel (engine == "rust"); the rest are
#: Python-fallback by design (docs/recovery/w7-languages.md).
EXPECTED_RUST_ENGINE = (
    "bash",
    "c",
    "cpp",
    "csharp",
    "go",
    "haskell",
    "java",
    "javascript",
    "kotlin",
    "lua",
    "perl",
    "php",
    "python",
    "ruby",
    "rust",
    "scala",
    "typescript",
)
EXPECTED_PYTHON_ENGINE = ("dart", "swift")

#: Grammars that expose no callee field on their call node, so no call
#: extraction is possible in the declarative model (verified against the
#: real grammars in tree-sitter-language-pack 1.22.1; W7 audit).
NO_CALL_FIELD_GRAMMARS = frozenset({"dart", "kotlin", "swift"})

ALL_LANGUAGES = pytest.mark.parametrize("language", EXPECTED_LANGUAGES)


# ------------------------------------------------------------------ registry


def test_exactly_the_nineteen_supported_languages():
    assert supported_languages() == EXPECTED_LANGUAGES


def test_engine_assignment_is_exact():
    rust = tuple(name for name in supported_languages() if get_spec(name).engine == "rust")
    python = tuple(name for name in supported_languages() if get_spec(name).engine == "python")
    assert rust == EXPECTED_RUST_ENGINE
    assert python == EXPECTED_PYTHON_ENGINE


@ALL_LANGUAGES
def test_engine_is_a_known_value(language: str):
    assert get_spec(language).engine in ("rust", "python")


def test_supported_languages_sorted():
    assert supported_languages() == tuple(sorted(supported_languages()))


@ALL_LANGUAGES
def test_language_registered(language: str):
    assert language in LANGUAGES


@ALL_LANGUAGES
def test_get_spec_roundtrip(language: str):
    spec = get_spec(language)
    assert isinstance(spec, LanguageSpec)
    assert spec.name == language


def test_get_spec_unknown_language_raises():
    with pytest.raises(KeyError):
        get_spec("cobol")


# ------------------------------------------------------------------ pure data


@ALL_LANGUAGES
def test_spec_is_frozen_dataclass(language: str):
    spec = get_spec(language)
    assert dataclasses.is_dataclass(spec)
    with pytest.raises(dataclasses.FrozenInstanceError):
        spec.name = "mutated"  # type: ignore[misc]


@ALL_LANGUAGES
def test_spec_collections_are_tuples(language: str):
    spec = get_spec(language)
    for field in dataclasses.fields(spec):
        value = getattr(spec, field.name)
        assert not isinstance(value, (list, dict, set)), (
            f"{language}.{field.name} must be immutable data, got {type(value).__name__}"
        )


@ALL_LANGUAGES
def test_spec_has_no_callables(language: str):
    """The registry is declarative: no lambdas/functions hiding in the data."""
    spec = get_spec(language)
    for field in dataclasses.fields(spec):
        value = getattr(spec, field.name)
        assert not callable(value), f"{language}.{field.name} must be pure data"


# ------------------------------------------------------------- contract kinds


@ALL_LANGUAGES
def test_declaration_kinds_map_to_core_kinds(language: str):
    spec = get_spec(language)
    core = {kind.value for kind in CoreNodeKind}
    for node_type, kind in spec.declaration_kinds:
        assert kind in core, f"{language}: {node_type!r} maps to non-core kind {kind!r}"


@ALL_LANGUAGES
def test_declaration_node_types_are_grammar_identifiers(language: str):
    spec = get_spec(language)
    for node_type, _kind in spec.declaration_kinds:
        assert re.fullmatch(r"[a-z][a-z0-9_]*", node_type), f"{language}: bad node type {node_type!r}"


@ALL_LANGUAGES
def test_declaration_kinds_have_no_duplicate_node_types(language: str):
    spec = get_spec(language)
    node_types = [node_type for node_type, _ in spec.declaration_kinds]
    assert len(node_types) == len(set(node_types))


@ALL_LANGUAGES
def test_every_language_declares_a_function_like_kind(language: str):
    spec = get_spec(language)
    kinds = {kind for _, kind in spec.declaration_kinds}
    assert kinds & {"function", "method"}, f"{language} declares no function-like kind"


@ALL_LANGUAGES
def test_scope_kinds_are_core_kinds(language: str):
    spec = get_spec(language)
    core = {kind.value for kind in CoreNodeKind}
    for kind in spec.scope_kinds:
        assert kind in core


@ALL_LANGUAGES
def test_method_scope_kinds_are_scope_kinds(language: str):
    spec = get_spec(language)
    for kind in spec.method_scope_kinds:
        assert kind in spec.scope_kinds


# ------------------------------------------------------------------- imports


@ALL_LANGUAGES
def test_import_rules_nonempty(language: str):
    assert get_spec(language).import_rules, f"{language} has no import rules"


@ALL_LANGUAGES
def test_import_rules_are_import_rule_instances(language: str):
    for rule in get_spec(language).import_rules:
        assert isinstance(rule, ImportRule)


@ALL_LANGUAGES
def test_import_rule_regexes_compile(language: str):
    for rule in get_spec(language).import_rules:
        if rule.target_regex:
            re.compile(rule.target_regex)


@ALL_LANGUAGES
def test_import_rule_has_some_target_strategy(language: str):
    for rule in get_spec(language).import_rules:
        assert rule.target_fields or rule.target_regex, (
            f"{language}: import rule for {rule.node_type!r} cannot find a target"
        )


# ---------------------------------------------------------------------- calls


@ALL_LANGUAGES
def test_call_types_nonempty_unless_grammar_has_no_callee_field(language: str):
    """Bidirectional gate: every language extracts calls, except the audited
    grammars whose call node carries no callee field (kotlin/swift
    ``call_expression``, dart's selector-based invocations) — those must
    declare empty ``call_types`` (honest data over dead strategies)."""
    spec = get_spec(language)
    if language in NO_CALL_FIELD_GRAMMARS:
        assert spec.call_types == (), f"{language}: grammar has no callee field; call_types must be empty"
    else:
        assert spec.call_types, f"{language} has no call types"


@ALL_LANGUAGES
def test_call_name_fields_nonempty(language: str):
    spec = get_spec(language)
    if spec.call_types:
        assert spec.call_name_fields, f"{language} has no call name fields"


@ALL_LANGUAGES
def test_identifier_types_nonempty(language: str):
    assert get_spec(language).identifier_types, f"{language} has no identifier types"


# ------------------------------------------------------------------ variables


@ALL_LANGUAGES
def test_variable_rules_map_to_variable_or_constant(language: str):
    for rule in get_spec(language).variable_rules:
        assert rule.kind in ("variable", "constant")


# ------------------------------------------------------------------ grammars


@ALL_LANGUAGES
def test_parser_loads_for_language(language: str):
    parser = get_parser(language)
    assert parser is not None


@ALL_LANGUAGES
def test_declaration_node_types_exist_in_grammar(language: str):
    """Every declared node type must be a real node type of the real grammar."""
    from tree_sitter_language_pack import get_language

    grammar = get_language(language)
    declared = {node_type for node_type, _ in get_spec(language).declaration_kinds}
    declared = set(get_spec(language).call_types) | declared
    declared |= {rule.node_type for rule in get_spec(language).import_rules}
    declared |= {rule.node_type for rule in get_spec(language).variable_rules}
    declared |= {node_type for node_type, _ in get_spec(language).container_types}
    missing = {t for t in declared if grammar.id_for_node_kind(t, True) is None}
    assert not missing, f"{language}: unknown grammar node types {sorted(missing)}"


# --------------------------------------------------------------- extensions


def test_extension_map_is_lowercase_dotted():
    for extension in EXTENSION_MAP:
        assert extension.startswith(".")
        assert extension == extension.lower()


def test_extension_map_targets_registered_languages():
    for extension, language in EXTENSION_MAP.items():
        assert language in LANGUAGES, f"{extension} -> unregistered {language!r}"


@ALL_LANGUAGES
def test_every_language_has_an_extension(language: str):
    extensions = [ext for ext, lang in EXTENSION_MAP.items() if lang == language]
    assert extensions, f"{language} is unreachable: no extension maps to it"


@pytest.mark.parametrize(
    ("filename", "expected"),
    [
        ("a.py", "python"),
        ("a.pyw", "python"),
        ("a.js", "javascript"),
        ("a.mjs", "javascript"),
        ("a.cjs", "javascript"),
        ("a.jsx", "javascript"),
        ("a.ts", "typescript"),
        ("a.mts", "typescript"),
        ("a.cts", "typescript"),
        ("a.go", "go"),
        ("a.rs", "rust"),
        ("a.java", "java"),
        ("a.c", "c"),
        ("a.h", "c"),
        ("a.cpp", "cpp"),
        ("a.cc", "cpp"),
        ("a.cxx", "cpp"),
        ("a.hpp", "cpp"),
        ("a.hxx", "cpp"),
        ("a.cs", "csharp"),
        ("a.rb", "ruby"),
        ("a.php", "php"),
        ("a.sh", "bash"),
        ("a.bash", "bash"),
        ("a.kt", "kotlin"),
        ("a.kts", "kotlin"),
        ("a.swift", "swift"),
        ("a.scala", "scala"),
        ("a.sc", "scala"),
        ("a.lua", "lua"),
        ("a.dart", "dart"),
        ("a.hs", "haskell"),
        ("a.lhs", "haskell"),
        ("a.pl", "perl"),
        ("a.pm", "perl"),
        ("dir/deep/file.py", "python"),
        ("UPPER.PY", "python"),
        ("no_extension", None),
        ("a.md", None),
        ("a.txt", None),
        ("a.json", None),
        ("a.yaml", None),
        ("a.pyc", None),
    ],
)
def test_language_for_path(filename: str, expected: str | None):
    assert language_for_path(filename) == expected

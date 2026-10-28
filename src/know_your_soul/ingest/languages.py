"""Declarative language registry for code ingestion (wave W2, spec: RESEARCH-INGESTION §2a).

This module is **pure data**: frozen dataclasses and tuples, no behaviour.
Each :class:`LanguageSpec` mirrors codegraph's ``LanguageExtractor`` table —
grammar node types mapped onto the frozen :class:`~know_your_soul.graph_model.CoreNodeKind`
vocabulary, plus the field names needed to find names, import targets and
call callees. The walker in :mod:`know_your_soul.ingest.extractor` interprets
these tables; adding a language means adding data here, never code there.

Phase-1 languages (MASTER-PLAN W2): python, javascript, typescript, go, rust,
java, c, cpp, csharp, ruby, php, bash.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import PurePosixPath

__all__ = [
    "EXTENSION_MAP",
    "LANGUAGES",
    "ImportRule",
    "LanguageSpec",
    "VariableRule",
    "get_spec",
    "language_for_path",
    "supported_languages",
]


@dataclass(frozen=True)
class ImportRule:
    """How to recognise and destructure one import construct.

    * ``node_type``: grammar node type of the import statement.
    * ``target_fields``: grammar fields tried in order (``child_by_field_name``)
      whose text is the import target (e.g. ``source`` for ES imports).
    * ``target_regex``: fallback applied to the node text; group 1 is the target.
    * ``match_text``: when non-empty, the rule only fires for nodes whose
      callee/command name is in this set (``require``, ``source``, ``.``).
    * ``strip_chars``: characters stripped from both ends of the raw target
      (quotes, angle brackets).
    """

    node_type: str
    target_fields: tuple[str, ...] = ()
    target_regex: str = ""
    match_text: tuple[str, ...] = ()
    strip_chars: str = "\"'<>"


@dataclass(frozen=True)
class VariableRule:
    """How to recognise one variable/constant declaration construct.

    * ``node_type``: grammar node type of the declarator/assignment.
    * ``name_field``: grammar field holding the declared name.
    * ``kind``: ``variable`` or ``constant``.
    * ``const_when_uppercase``: promote to ``constant`` when the name is
      ALL_CAPS (python/ruby/bash convention).
    * ``const_keyword``: promote to ``constant`` when the parent node's first
      child text equals this keyword (JS/TS ``const``).
    * ``function_value_types``: when the ``value`` field child has one of
      these grammar types, the declaration is a **function** node named by
      the variable (arrow functions, function expressions).
    """

    node_type: str
    name_field: str
    kind: str = "variable"
    const_when_uppercase: bool = False
    const_keyword: str = ""
    function_value_types: tuple[str, ...] = ()


@dataclass(frozen=True)
class LanguageSpec:
    """The declarative extraction table for one language.

    * ``declaration_kinds``: ``(grammar node type, core kind)`` pairs; order
      is irrelevant to the walker but kept as a tuple for immutability.
    * ``conditional_kinds``: ``(node type, ((child type, kind), ...), default)``
      for declarations whose kind depends on a child (Go ``type_spec``:
      ``interface_type`` → ``interface``, anything else → ``type``).
    * ``container_types``: ``(node type, name field)`` pairs for transparent
      scope containers that open a scope and contribute the qualified-name
      segment but create **no node** (Rust ``impl_item``).
    * ``name_fields``: fields tried, recursively, to find a declaration's
      name (C ``function_definition`` resolves through ``declarator``).
    * ``scope_kinds`` / ``method_scope_kinds``: which node kinds open a
      ``contains`` scope, and which of those flip enclosed functions to
      methods (plus transparent containers, which always flip).
    * ``scoped_functions_become_methods``: a node mapped to ``function``
      becomes ``method`` when the innermost scope is class-ish or a
      transparent container.
    """

    name: str
    parser: str
    declaration_kinds: tuple[tuple[str, str], ...]
    import_rules: tuple[ImportRule, ...]
    call_types: tuple[str, ...]
    identifier_types: tuple[str, ...]
    conditional_kinds: tuple[tuple[str, tuple[tuple[str, str], ...], str], ...] = ()
    container_types: tuple[tuple[str, str], ...] = ()
    name_fields: tuple[str, ...] = ("name",)
    scope_kinds: tuple[str, ...] = ("module", "class", "interface", "type", "function", "method")
    method_scope_kinds: tuple[str, ...] = ("class", "interface", "type")
    scoped_functions_become_methods: bool = True
    call_name_fields: tuple[str, ...] = ("function", "method", "name", "macro")
    variable_rules: tuple[VariableRule, ...] = ()


# --------------------------------------------------------------------------
# per-language tables

_PY = LanguageSpec(
    name="python",
    parser="python",
    declaration_kinds=(
        ("class_definition", "class"),
        ("function_definition", "function"),
    ),
    import_rules=(
        ImportRule("import_statement", target_fields=("name",)),
        ImportRule("import_from_statement", target_fields=("module_name", "name")),
    ),
    call_types=("call",),
    call_name_fields=("function",),
    identifier_types=("identifier",),
    variable_rules=(
        VariableRule("assignment", name_field="left", kind="variable", const_when_uppercase=True),
    ),
)

_JS_DECLARATIONS = (
    ("class_declaration", "class"),
    ("function_declaration", "function"),
    ("generator_function_declaration", "function"),
    ("method_definition", "method"),
)
_JS_IMPORTS = (
    ImportRule("import_statement", target_fields=("source",)),
    ImportRule(
        "call_expression",
        target_regex=r"require\(\s*[\"']([^\"']+)[\"']",
        match_text=("require",),
    ),
)
_JS_CALLS = ("call_expression",)
_JS_IDENTIFIERS = ("identifier", "property_identifier", "type_identifier", "shorthand_property_identifier")
_JS_VARIABLES = (
    VariableRule(
        "variable_declarator",
        name_field="name",
        kind="variable",
        const_keyword="const",
        function_value_types=("arrow_function", "function_expression", "function"),
    ),
)

_JS = LanguageSpec(
    name="javascript",
    parser="javascript",
    declaration_kinds=_JS_DECLARATIONS,
    import_rules=_JS_IMPORTS,
    call_types=_JS_CALLS,
    call_name_fields=("function",),
    identifier_types=_JS_IDENTIFIERS,
    variable_rules=_JS_VARIABLES,
    scoped_functions_become_methods=False,
)

_TS = LanguageSpec(
    name="typescript",
    parser="typescript",
    declaration_kinds=_JS_DECLARATIONS
    + (
        ("abstract_class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("type_alias_declaration", "type"),
        ("enum_declaration", "type"),
        ("internal_module", "module"),
        ("method_signature", "method"),
        ("function_signature", "function"),
    ),
    import_rules=_JS_IMPORTS,
    call_types=_JS_CALLS,
    call_name_fields=("function",),
    identifier_types=_JS_IDENTIFIERS,
    variable_rules=_JS_VARIABLES,
    scoped_functions_become_methods=False,
)

_GO = LanguageSpec(
    name="go",
    parser="go",
    declaration_kinds=(
        ("function_declaration", "function"),
        ("method_declaration", "method"),
        ("package_clause", "module"),
        ("const_spec", "constant"),
        ("var_spec", "variable"),
    ),
    conditional_kinds=(
        ("type_spec", (("interface_type", "interface"),), "type"),
    ),
    import_rules=(
        ImportRule("import_spec", target_fields=("path",)),
    ),
    call_types=("call_expression",),
    call_name_fields=("function",),
    identifier_types=("identifier", "field_identifier", "type_identifier", "package_identifier"),
    scoped_functions_become_methods=False,
)

_RUST = LanguageSpec(
    name="rust",
    parser="rust",
    declaration_kinds=(
        ("function_item", "function"),
        ("struct_item", "type"),
        ("enum_item", "type"),
        ("union_item", "type"),
        ("trait_item", "interface"),
        ("type_item", "type"),
        ("mod_item", "module"),
        ("const_item", "constant"),
        ("static_item", "constant"),
        ("function_signature_item", "function"),
    ),
    container_types=(("impl_item", "type"),),
    import_rules=(
        ImportRule("use_declaration", target_fields=("argument",)),
    ),
    call_types=("call_expression", "macro_invocation"),
    call_name_fields=("function", "macro", "name"),
    identifier_types=("identifier", "type_identifier", "field_identifier"),
)

_JAVA = LanguageSpec(
    name="java",
    parser="java",
    declaration_kinds=(
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("enum_declaration", "type"),
        ("annotation_type_declaration", "interface"),
        ("record_declaration", "class"),
        ("method_declaration", "method"),
        ("constructor_declaration", "method"),
        ("package_declaration", "module"),
    ),
    import_rules=(
        ImportRule("import_declaration", target_regex=r"import\s+(?:static\s+)?([\w.]+)"),
    ),
    call_types=("method_invocation", "object_creation_expression"),
    call_name_fields=("name", "type", "function"),
    identifier_types=("identifier", "type_identifier"),
    scoped_functions_become_methods=False,
)

_C_DECLARATIONS = (
    ("function_definition", "function"),
    ("struct_specifier", "type"),
    ("enum_specifier", "type"),
    ("union_specifier", "type"),
    ("type_definition", "type"),
    ("preproc_def", "constant"),
)
_C_IMPORTS = (ImportRule("preproc_include", target_fields=("path",)),)
_C_CALLS = ("call_expression",)
_C_IDENTIFIERS = ("identifier", "type_identifier", "field_identifier")

_C = LanguageSpec(
    name="c",
    parser="c",
    declaration_kinds=_C_DECLARATIONS,
    import_rules=_C_IMPORTS,
    call_types=_C_CALLS,
    identifier_types=_C_IDENTIFIERS,
    name_fields=("name", "declarator"),
    scoped_functions_become_methods=False,
)

_CPP = LanguageSpec(
    name="cpp",
    parser="cpp",
    declaration_kinds=_C_DECLARATIONS
    + (
        ("class_specifier", "class"),
        ("namespace_definition", "module"),
    ),
    import_rules=_C_IMPORTS,
    call_types=_C_CALLS,
    identifier_types=_C_IDENTIFIERS + ("namespace_identifier",),
    name_fields=("name", "declarator"),
)

_CSHARP = LanguageSpec(
    name="csharp",
    parser="csharp",
    declaration_kinds=(
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("struct_declaration", "type"),
        ("enum_declaration", "type"),
        ("record_declaration", "class"),
        ("delegate_declaration", "type"),
        ("method_declaration", "method"),
        ("constructor_declaration", "method"),
        ("namespace_declaration", "module"),
        ("file_scoped_namespace_declaration", "module"),
    ),
    import_rules=(
        ImportRule("using_directive", target_regex=r"using\s+(?:static\s+)?(?:[\w.]+\s*=\s*)?([\w.]+)\s*;"),
    ),
    call_types=("invocation_expression",),
    call_name_fields=("function", "name"),
    identifier_types=("identifier",),
    variable_rules=(
        VariableRule("variable_declarator", name_field="name", kind="variable"),
    ),
    scoped_functions_become_methods=False,
)

_RUBY = LanguageSpec(
    name="ruby",
    parser="ruby",
    declaration_kinds=(
        ("class", "class"),
        ("module", "module"),
        ("method", "function"),
        ("singleton_method", "method"),
    ),
    import_rules=(
        ImportRule(
            "call",
            target_regex=r"(?:require|require_relative)\s*[\"']([^\"']+)[\"']",
            match_text=("require", "require_relative"),
        ),
    ),
    call_types=("call",),
    call_name_fields=("method",),
    identifier_types=("identifier", "constant"),
    variable_rules=(
        VariableRule("assignment", name_field="left", kind="variable", const_when_uppercase=True),
    ),
)

_PHP = LanguageSpec(
    name="php",
    parser="php",
    declaration_kinds=(
        ("class_declaration", "class"),
        ("interface_declaration", "interface"),
        ("trait_declaration", "type"),
        ("enum_declaration", "type"),
        ("function_definition", "function"),
        ("method_declaration", "method"),
        ("namespace_definition", "module"),
    ),
    import_rules=(
        ImportRule("namespace_use_declaration", target_regex=r"use\s+([\\\w]+)"),
        ImportRule(
            "require_expression",
            target_regex=r"require(?:_once)?\s*[\"']?([^\s\"';)]+)",
        ),
        ImportRule(
            "include_expression",
            target_regex=r"include(?:_once)?\s*[\"']?([^\s\"';)]+)",
        ),
    ),
    call_types=("function_call_expression", "member_call_expression", "scoped_call_expression"),
    call_name_fields=("function", "name"),
    identifier_types=("name",),
    scoped_functions_become_methods=False,
)

_BASH = LanguageSpec(
    name="bash",
    parser="bash",
    declaration_kinds=(
        ("function_definition", "function"),
    ),
    import_rules=(
        ImportRule("command", target_fields=("argument",), match_text=("source", ".")),
    ),
    call_types=("command",),
    call_name_fields=("name",),
    identifier_types=("word", "variable_name"),
    variable_rules=(
        VariableRule("variable_assignment", name_field="name", kind="variable", const_when_uppercase=True),
    ),
    scoped_functions_become_methods=False,
)

#: The language registry. Iteration order is alphabetical for determinism.
LANGUAGES: dict[str, LanguageSpec] = {
    spec.name: spec
    for spec in sorted(
        (_PY, _JS, _TS, _GO, _RUST, _JAVA, _C, _CPP, _CSHARP, _RUBY, _PHP, _BASH),
        key=lambda spec: spec.name,
    )
}

#: Extension → language (codegraph EXTENSION_MAP pattern; ``.h`` defaults to C).
EXTENSION_MAP: dict[str, str] = {
    ".py": "python",
    ".pyw": "python",
    ".js": "javascript",
    ".mjs": "javascript",
    ".cjs": "javascript",
    ".jsx": "javascript",
    ".ts": "typescript",
    ".mts": "typescript",
    ".cts": "typescript",
    ".go": "go",
    ".rs": "rust",
    ".java": "java",
    ".c": "c",
    ".h": "c",
    ".cpp": "cpp",
    ".cc": "cpp",
    ".cxx": "cpp",
    ".hpp": "cpp",
    ".hh": "cpp",
    ".hxx": "cpp",
    ".cs": "csharp",
    ".rb": "ruby",
    ".rake": "ruby",
    ".php": "php",
    ".sh": "bash",
    ".bash": "bash",
}


def supported_languages() -> tuple[str, ...]:
    """Every registered language, sorted (deterministic iteration)."""
    return tuple(sorted(LANGUAGES))


def get_spec(language: str) -> LanguageSpec:
    """The spec for *language*; raises :class:`KeyError` for unknown names."""
    return LANGUAGES[language]


def language_for_path(path: str) -> str | None:
    """The language for *path*'s extension (case-insensitive), or ``None``."""
    suffix = PurePosixPath(path).suffix.lower()
    return EXTENSION_MAP.get(suffix)

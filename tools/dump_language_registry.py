#!/usr/bin/env python3
"""Dump the declarative language registry to ``kernel-rust/registry.json`` (wave W5, SUB-93).

:mod:`know_your_soul.ingest.languages` is the single source of truth for the
extraction tables; the Rust kernel (``kernel-rust/``) consumes this JSON dump
so both implementations read identical data. The dump is fully deterministic:
canonical JSON (sorted keys, ``,``/``:`` separators, ``ensure_ascii=False``)
plus one trailing newline, independent of ``PYTHONHASHSEED``.

Usage::

    .venv/bin/python tools/dump_language_registry.py [--check]

``--check`` verifies the on-disk file matches a fresh dump (exit 1 on drift)
without writing. Default mode rewrites ``kernel-rust/registry.json``.
"""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT / "src"))

from know_your_soul.domain import canonical_json  # noqa: E402
from know_your_soul.ingest.languages import EXTENSION_MAP, LANGUAGES  # noqa: E402

REGISTRY_PATH = REPO_ROOT / "kernel-rust" / "registry.json"
REGISTRY_FORMAT_VERSION = 1


def build_registry() -> dict[str, object]:
    """The deterministic, JSON-ready registry payload (all fields explicit)."""
    languages: dict[str, object] = {}
    for name in sorted(LANGUAGES):
        spec = LANGUAGES[name]
        languages[name] = {
            "call_name_fields": list(spec.call_name_fields),
            "call_types": list(spec.call_types),
            "conditional_kinds": [
                [node_type, [[child_type, kind] for child_type, kind in mapping], default]
                for node_type, mapping, default in spec.conditional_kinds
            ],
            "container_types": [[node_type, field] for node_type, field in spec.container_types],
            "declaration_kinds": [[node_type, kind] for node_type, kind in spec.declaration_kinds],
            "engine": spec.engine,
            "identifier_types": list(spec.identifier_types),
            "import_rules": [
                {
                    "match_text": list(rule.match_text),
                    "node_type": rule.node_type,
                    "strip_chars": rule.strip_chars,
                    "target_fields": list(rule.target_fields),
                    "target_regex": rule.target_regex,
                }
                for rule in spec.import_rules
            ],
            "method_scope_kinds": list(spec.method_scope_kinds),
            "name": spec.name,
            "name_fields": list(spec.name_fields),
            "parser": spec.parser,
            "scope_kinds": list(spec.scope_kinds),
            "scoped_functions_become_methods": spec.scoped_functions_become_methods,
            "variable_rules": [
                {
                    "const_keyword": rule.const_keyword,
                    "const_when_uppercase": rule.const_when_uppercase,
                    "function_value_types": list(rule.function_value_types),
                    "kind": rule.kind,
                    "name_field": rule.name_field,
                    "node_type": rule.node_type,
                }
                for rule in spec.variable_rules
            ],
        }
    return {
        "extension_map": {extension: EXTENSION_MAP[extension] for extension in sorted(EXTENSION_MAP)},
        "format_version": REGISTRY_FORMAT_VERSION,
        "languages": languages,
    }


def dump_registry() -> str:
    """The canonical JSON text of the registry (one trailing newline)."""
    return canonical_json(build_registry()) + "\n"


def main() -> int:
    parser = argparse.ArgumentParser(description=(__doc__ or "").splitlines()[0])
    parser.add_argument("--check", action="store_true", help="verify the on-disk dump is fresh; never write")
    parser.add_argument("--stdout", action="store_true", help="print the dump to stdout; never write")
    args = parser.parse_args()
    text = dump_registry()
    if args.stdout:
        sys.stdout.write(text)
        return 0
    if args.check:
        if not REGISTRY_PATH.is_file():
            print(f"FAIL: {REGISTRY_PATH} is missing; run tools/dump_language_registry.py")
            return 1
        on_disk = REGISTRY_PATH.read_text(encoding="utf-8")
        if on_disk != text:
            print(f"FAIL: {REGISTRY_PATH} is stale; run tools/dump_language_registry.py")
            return 1
        print(f"OK: {REGISTRY_PATH} matches a fresh dump ({len(text)} bytes)")
        return 0
    REGISTRY_PATH.parent.mkdir(parents=True, exist_ok=True)
    REGISTRY_PATH.write_text(text, encoding="utf-8")
    print(f"wrote {REGISTRY_PATH} ({len(text)} bytes, {len(LANGUAGES)} languages)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

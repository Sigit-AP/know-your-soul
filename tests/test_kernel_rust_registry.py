"""Registry dump determinism and freshness tests (wave W5, SUB-93).

``kernel-rust/registry.json`` is the shared source of truth consumed by the
Rust kernel; it must be a byte-exact, deterministic dump of
:mod:`know_your_soul.ingest.languages`. The tool runs as a subprocess so these
tests exercise the exact CLI contract (``--stdout`` / ``--check``).
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
DUMP_TOOL = REPO_ROOT / "tools" / "dump_language_registry.py"
REGISTRY_JSON = REPO_ROOT / "kernel-rust" / "registry.json"

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


def _run_dump(*args: str, seed: str | None = None) -> subprocess.CompletedProcess[str]:
    env = dict(os.environ)
    if seed is not None:
        env["PYTHONHASHSEED"] = seed
    return subprocess.run(
        [sys.executable, str(DUMP_TOOL), *args],
        cwd=REPO_ROOT,
        env=env,
        capture_output=True,
        text=True,
    )


def _fresh_dump() -> str:
    proc = _run_dump("--stdout")
    assert proc.returncode == 0, proc.stderr
    return proc.stdout


def test_dump_is_byte_deterministic() -> None:
    assert _fresh_dump() == _fresh_dump()


def test_dump_is_stable_across_hash_seeds() -> None:
    outputs = []
    for seed in ("0", "1", "42"):
        proc = _run_dump("--stdout", seed=seed)
        assert proc.returncode == 0, proc.stderr
        outputs.append(proc.stdout)
    assert outputs[0] == outputs[1] == outputs[2]


def test_dump_ends_with_single_newline_and_canonical_shape() -> None:
    text = _fresh_dump()
    assert text.endswith("\n") and not text.endswith("\n\n")
    body = text[:-1]
    assert '", "' not in body and '": ' not in body, "separators must be canonical (no spaces)"
    assert json.loads(body) is not None


def test_repo_registry_json_matches_fresh_dump() -> None:
    assert REGISTRY_JSON.is_file(), "kernel-rust/registry.json missing; run tools/dump_language_registry.py"
    assert REGISTRY_JSON.read_text(encoding="utf-8") == _fresh_dump()


def test_dump_tool_check_mode_exit_codes() -> None:
    proc = _run_dump("--check")
    assert proc.returncode == 0, proc.stderr or proc.stdout
    original = REGISTRY_JSON.read_bytes()
    try:
        REGISTRY_JSON.write_bytes(original + b" ")
        proc = _run_dump("--check")
        assert proc.returncode == 1
    finally:
        REGISTRY_JSON.write_bytes(original)
    proc = _run_dump("--check")
    assert proc.returncode == 0


def test_registry_payload_covers_all_supported_languages() -> None:
    payload = json.loads(REGISTRY_JSON.read_text(encoding="utf-8"))
    assert payload["format_version"] == 1
    assert tuple(sorted(payload["languages"])) == EXPECTED_LANGUAGES
    extension_map = payload["extension_map"]
    assert extension_map[".py"] == "python"
    assert extension_map[".h"] == "c"
    for name, spec in payload["languages"].items():
        assert spec["name"] == name
        assert spec["parser"], name
        assert spec["engine"] in ("rust", "python"), name
        assert isinstance(spec["declaration_kinds"], list), name
        for node_type, kind in spec["declaration_kinds"]:
            assert isinstance(node_type, str) and isinstance(kind, str)
        for rule in spec["import_rules"]:
            assert set(rule) == {"match_text", "node_type", "strip_chars", "target_fields", "target_regex"}
        for rule in spec["variable_rules"]:
            assert set(rule) == {
                "const_keyword",
                "const_when_uppercase",
                "function_value_types",
                "kind",
                "name_field",
                "node_type",
            }

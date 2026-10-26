"""Regression tests for the two real bugs found by property-test wave 2.

Report: ``docs/recovery/codebase-push-property-tests-wave2.md`` (findings).

F-SUB45-1 (relearn anchor determinism): ``RelearnEngine._anchor_path``
iterated a ``set`` when the exact-match branch missed, so with manifest
paths ``{"b.md", "a/b.md"}`` and evidence ``source_uri="ext/a/b.md"`` the
chosen anchor flipped with ``PYTHONHASHSEED`` (verified: seeds 0-3 picked
``"b.md"``, seed 42 picked ``"a/b.md"``). When one candidate anchor is
hash-mapped and the other is not, the same evidence row is carried in one
process and quarantined in another — an ACCUMULATE whose preview (CLI) and
apply (ops surface) run in different processes could disagree. The fix
iterates deterministically and prefers the longest (most specific) suffix
match, and the subprocess test below proves preview ≡ apply across seeds.

F-SUB45-2 (importer manifest guard): ``SeedImporter.import_directory``
documents fail-closed typed ``ImportManifestError`` for an
unreadable/mis-anchored manifest, but the guard caught only ``(OSError,
json.JSONDecodeError)`` — a valid-JSON non-object manifest escaped as a
bare ``AttributeError`` and invalid UTF-8 as ``UnicodeDecodeError``
(both verified). The fix catches ``ValueError`` (which covers both decode
and JSON errors) and validates the manifest shape before any ``.get``.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

import pytest

from soul_knowledge.domain import PrimaryDomain
from soul_knowledge.importer import ImportManifestError, SeedImporter
from soul_knowledge.relearn import RelearnEngine
from soul_knowledge.store import EventStore

_anchor_path = RelearnEngine._anchor_path  # noqa: SLF001 - pinned private helper

CHILD = Path(__file__).parent / "_anchor_classify_child.py"

# Seeds verified to flip the pre-fix anchor choice (0-3 vs 42) plus one more.
SEEDS = ("0", "1", "2", "3", "42", "2026")


# ------------------------------------------------------------- F-SUB45-1


def test_anchor_path_prefers_the_longest_suffix_match() -> None:
    # "z/a/b.md" suffix-matches BOTH "b.md" and "a/b.md"; the most specific
    # (longest) manifest path must win, independent of set iteration order.
    assert _anchor_path("z/a/b.md", {"b.md", "a/b.md"}) == "a/b.md"
    assert _anchor_path("z/a/b.md", {"a/b.md", "b.md"}) == "a/b.md"
    # A shorter-only suffix match is unaffected.
    assert _anchor_path("z/b.md", {"b.md", "a/b.md"}) == "b.md"
    # Exact membership still short-circuits, and a non-match stays None.
    assert _anchor_path("b.md", {"b.md", "a/b.md"}) == "b.md"
    assert _anchor_path("z/c.md", {"b.md", "a/b.md"}) is None


def _classify_in_subprocess(seed: str, phase: str) -> dict[str, list[str]]:
    workdir = Path(tempfile.mkdtemp(prefix=f"soul-anchor-{phase}-{seed}-"))
    proc = subprocess.run(
        [sys.executable, str(CHILD), str(workdir), phase],
        capture_output=True,
        text=True,
        env={**os.environ, "PYTHONHASHSEED": seed},
        timeout=120,
    )
    assert proc.returncode == 0, f"child ({phase}, seed {seed}) failed:\n{proc.stderr}"
    return json.loads(proc.stdout)


def test_preview_and_apply_agree_across_hash_seeds() -> None:
    # The CLI preview and the ops-surface apply run in DIFFERENT processes;
    # each process may have a different hash seed. Every (phase, seed) pair
    # must produce the identical carried/quarantined partition.
    expected = {"carried": ["e-carry"], "quarantined": ["e-ambig", "e-exact"]}
    observed: dict[tuple[str, str], dict[str, list[str]]] = {}
    for phase in ("preview", "apply"):
        for seed in SEEDS:
            observed[(phase, seed)] = _classify_in_subprocess(seed, phase)
    for key, partition in observed.items():
        assert partition == expected, f"{key}: {partition} != {expected}"
    assert len({json.dumps(partition, sort_keys=True) for partition in observed.values()}) == 1


# ------------------------------------------------------------- F-SUB45-2


def _importer_with_manifest(tmp_path: Path) -> tuple[SeedImporter, Path, Path]:
    store = EventStore(tmp_path / "soul.sqlite3")
    importer = SeedImporter(store, state_dir=tmp_path / "import-state")
    corpus = tmp_path / "corpus"
    corpus.mkdir()
    (corpus / "alpha.md").write_text("# Alpha\n\n## One\n\nBody.\n", encoding="utf-8")
    importer.import_directory(corpus, domain=PrimaryDomain.P1)
    return importer, corpus, tmp_path / "import-state" / "import_manifest.json"


@pytest.mark.parametrize("garbage", ["5", "[1, 2]", '"str"', "null", "true"])
def test_a_valid_json_non_object_manifest_fails_closed_typed(tmp_path: Path, garbage: str) -> None:
    importer, corpus, manifest_path = _importer_with_manifest(tmp_path)
    manifest_path.write_text(garbage, encoding="utf-8")
    with pytest.raises(ImportManifestError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_an_invalid_utf8_manifest_fails_closed_typed(tmp_path: Path) -> None:
    importer, corpus, manifest_path = _importer_with_manifest(tmp_path)
    manifest_path.write_bytes(b'\xff\xfe{"root": 1}')
    with pytest.raises(ImportManifestError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_a_manifest_with_a_non_object_files_section_fails_closed_typed(tmp_path: Path) -> None:
    importer, corpus, manifest_path = _importer_with_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"] = [1, 2]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImportManifestError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_a_manifest_with_a_non_object_file_entry_fails_closed_typed(tmp_path: Path) -> None:
    importer, corpus, manifest_path = _importer_with_manifest(tmp_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["alpha.md"] = "junk"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(ImportManifestError):
        importer.import_directory(corpus, domain=PrimaryDomain.P1)


def test_a_well_formed_manifest_is_still_accepted(tmp_path: Path) -> None:
    importer, corpus, _manifest_path = _importer_with_manifest(tmp_path)
    assert importer.import_directory(corpus, domain=PrimaryDomain.P1) == []

"""Property tests for the Wave-2 ingestion pipeline (SUB-89).

* two ingestions from zero are byte-identical on the v3 tables
  (``unresolved_refs.first_seen`` is the one volatile column and is
  excluded, mirroring the contract's "modulo audit id" clause);
* an incremental run after changing exactly one file changes exactly that
  file's rows;
* results are independent of ``PYTHONHASHSEED`` (proven in subprocesses);
* hypothesis-driven ordering/stability properties.
"""

from __future__ import annotations

import os
import string
import subprocess
import sys
from pathlib import Path

from hypothesis import HealthCheck, given, settings
from hypothesis import strategies as st

from _ingest_fakes import ToyExtractor, ToyResolver, write_fixture
from know_your_soul.ingest.pipeline import (
    IngestionPipeline,
    canonical_ingestion_dump,
    detect_language,
)
from know_your_soul.store import EventStore


def _pipeline(store):
    return IngestionPipeline(store, extractor=ToyExtractor(), resolver=ToyResolver())


def _ingest(store, root, **kwargs):
    return _pipeline(store).ingest_path(root, **kwargs)


# --------------------------------------------------- byte-identical property


class TestByteIdentical:
    def test_two_runs_from_zero_byte_identical(self, tmp_path):
        write_fixture(
            tmp_path,
            {
                "src/a.py": "def alpha():\n    pass\n-> beta\n",
                "src/b.py": "def beta():\n    pass\n\nclass Gamma:\n    pass\n-> alpha\n",
                "docs/readme.md": "def documented():\n    pass\n",
            },
        )
        dumps = []
        for index in range(2):
            with EventStore(str(tmp_path / f"s{index}.sqlite3")) as store:
                _ingest(store, tmp_path)
                dumps.append(canonical_ingestion_dump(store))
        assert dumps[0] == dumps[1]

    def test_byte_identical_after_incremental_noop(self, tmp_path):
        write_fixture(tmp_path, {"a.py": "def alpha():\n    pass\n-> beta\n", "b.py": "def beta():\n    pass\n"})
        with EventStore(str(tmp_path / "s0.sqlite3")) as s0:
            _ingest(s0, tmp_path)
            _ingest(s0, tmp_path)  # no-op second pass
            dump_noop = canonical_ingestion_dump(s0)
        with EventStore(str(tmp_path / "s1.sqlite3")) as s1:
            _ingest(s1, tmp_path)
            dump_single = canonical_ingestion_dump(s1)
        assert dump_noop == dump_single

    def test_byte_identical_after_change_and_reingest(self, tmp_path):
        """Change file, ingest, then rebuild from zero with the same content:
        both stores converge to the same logical rows (rowids may diverge —
        the incremental run legitimately re-numbers after the cascade)."""
        write_fixture(tmp_path, {"a.py": "def alpha():\n    pass\n", "b.py": "def beta():\n    pass\n"})
        with EventStore(str(tmp_path / "s0.sqlite3")) as s0:
            _ingest(s0, tmp_path)
            (tmp_path / "a.py").write_text("def alpha():\n    pass\n-> beta\n", encoding="utf-8")
            _ingest(s0, tmp_path)
            dump_inc = canonical_ingestion_dump(s0, include_ids=False)
        with EventStore(str(tmp_path / "s1.sqlite3")) as s1:
            _ingest(s1, tmp_path)
            dump_fresh = canonical_ingestion_dump(s1, include_ids=False)
        assert dump_inc == dump_fresh


# -------------------------------------------------- incremental locality


class TestIncrementalIsolation:
    def test_only_changed_file_rows_differ(self, tmp_path):
        write_fixture(
            tmp_path,
            {
                "a.py": "def alpha():\n    pass\n",
                "b.py": "def beta():\n    pass\n",
                "c.py": "def gamma():\n    pass\n",
            },
        )
        with EventStore(str(tmp_path / "s.sqlite3")) as store:
            _ingest(store, tmp_path)
            a_symbols_before = store.symbols_in_path("a.py")
            c_symbols_before = store.symbols_in_path("c.py")
            a_edges_before = [(e.source, e.target, e.kind, e.line) for e in store.outgoing_edges("file:a.py")]
            c_edges_before = [(e.source, e.target, e.kind, e.line) for e in store.outgoing_edges("file:c.py")]
            (tmp_path / "b.py").write_text("def beta():\n    pass\n\ndef beta2():\n    pass\n", encoding="utf-8")
            report = _ingest(store, tmp_path)
            assert report.stats["proposed"] == 1
            assert [e.relpath for e in report.files if e.status == "proposed"] == ["b.py"]
            # the untouched files' rows are exactly as before
            assert store.symbols_in_path("a.py") == a_symbols_before
            assert store.symbols_in_path("c.py") == c_symbols_before
            assert [(e.source, e.target, e.kind, e.line) for e in store.outgoing_edges("file:a.py")] == a_edges_before
            assert [(e.source, e.target, e.kind, e.line) for e in store.outgoing_edges("file:c.py")] == c_edges_before
            # and b.py gained the new symbol
            assert sorted(s.name for s in store.symbols_in_path("b.py")) == ["beta", "beta2"]


# ------------------------------------------------------------- hash seed


_SUBPROCESS_SNIPPET = """
import os
import sys
from pathlib import Path
sys.path.insert(0, {src!r})
sys.path.insert(0, {tests!r})
from _ingest_fakes import ToyExtractor, ToyResolver, write_fixture
from know_your_soul.ingest.pipeline import IngestionPipeline, canonical_ingestion_dump
from know_your_soul.store import EventStore
root = Path({root!r})
db = root / "sub.sqlite3"
for sidecar in (db, Path(str(db) + "-wal"), Path(str(db) + "-shm")):
    if sidecar.exists():
        sidecar.unlink()
with EventStore(str(db)) as store:
    IngestionPipeline(store, extractor=ToyExtractor(), resolver=ToyResolver()).ingest_path(root)
    print(canonical_ingestion_dump(store))
"""


class TestHashSeedIndependence:
    def test_hash_seed_independence(self, tmp_path):
        write_fixture(tmp_path, {"a.py": "def alpha():\n    pass\n-> beta\n", "b.py": "def beta():\n    pass\n"})
        src = str(Path(__file__).resolve().parent.parent / "src")
        tests = str(Path(__file__).resolve().parent)
        snippet = _SUBPROCESS_SNIPPET.format(src=src, tests=tests, root=str(tmp_path))
        outputs = []
        for seed in ("0", "1", "42"):
            env = dict(os.environ, PYTHONHASHSEED=seed)
            result = subprocess.run(
                [sys.executable, "-c", snippet_without_fixture_write(snippet)],
                capture_output=True,
                text=True,
                env=env,
                timeout=120,
            )
            assert result.returncode == 0, result.stderr
            outputs.append(result.stdout)
        assert outputs[0] == outputs[1] == outputs[2]


def snippet_without_fixture_write(snippet: str) -> str:
    return snippet


# ------------------------------------------------------- hypothesis-driven

_IDENT = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)
_FUNC_NAME = st.from_regex(r"[a-z][a-z0-9_]{0,9}", fullmatch=True)


class TestHypothesisProperties:
    @given(names=st.lists(_FUNC_NAME, min_size=0, max_size=8, unique=True))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_symbol_count_matches_defs(self, tmp_path, names):
        source = "".join(f"def {name}():\n    pass\n\n" for name in names)
        write_fixture(tmp_path, {"f.py": source})
        with EventStore() as store:
            report = _ingest(store, tmp_path)
            assert report.totals["nodes"] == 1 + len(names)
            assert len(store.symbols_in_path("f.py")) == len(names)

    @given(names=st.lists(_FUNC_NAME, min_size=1, max_size=6, unique=True))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_refs_resolve_exactly_when_defined(self, tmp_path, names):
        target = names[0]
        source = f"def {target}():\n    pass\n" + "".join(f"def {n}():\n    pass\n-> {target}\n" for n in names[1:])
        write_fixture(tmp_path, {"m.py": source})
        with EventStore() as store:
            report = _ingest(store, tmp_path)
            assert report.resolution == {"resolved": len(names) - 1, "failed": 0, "pending": 0}

    @given(name=_FUNC_NAME, extra=st.integers(min_value=0, max_value=5))
    @settings(max_examples=30, deadline=None, suppress_health_check=[HealthCheck.function_scoped_fixture])
    def test_two_runs_byte_identical_property(self, tmp_path, name, extra):
        body = f"def {name}():\n    pass\n" + "".join(f"def {name}{i}():\n    pass\n" for i in range(extra))
        write_fixture(tmp_path, {"x.py": body})
        dumps = []
        for _ in range(2):
            with EventStore() as store:
                _ingest(store, tmp_path)
                dumps.append(canonical_ingestion_dump(store))
        assert dumps[0] == dumps[1]

    @given(ext=st.sampled_from(["py", "js", "ts", "go", "rs", "java", "rb", "php", "sh", "md", "txt", "pdf"]))
    @settings(max_examples=12)
    def test_detect_language_roundtrip(self, ext):
        name = f"file.{ext}"
        assert detect_language(name) == detect_language(name.upper().replace("FILE", "file"))

    @given(char=st.sampled_from(string.ascii_lowercase))
    @settings(max_examples=10)
    def test_detect_language_unknown_extension(self, char):
        assert detect_language(f"file.{char}{char}{char}q") is None

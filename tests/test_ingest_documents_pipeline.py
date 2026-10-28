"""Pipeline integration tests for Wave-3 document ingestion (SUB-90).

The pipeline routes non-code formats (``.pdf``/``.md``/``.txt``) to the
document extractor; code files keep the SUB-87 tree-sitter path. Incremental
tracking (size/mtime pre-filter + sha256 confirm), cascade deletes, the
proposal-only report and the two-phase resolver lane are exercised end to
end against a real store.
"""

from __future__ import annotations

import hashlib

import pytest

from _doc_fixtures import build_encrypted_pdf_bytes, build_pdf_bytes
from _ingest_fakes import ToyResolver, write_fixture
from know_your_soul.graph_model import SplitThresholds, file_node_id, make_node_id
from know_your_soul.ingest.documents import DOCUMENT_EXTRACTION_VERSION
from know_your_soul.ingest.extractor import EXTRACTION_VERSION
from know_your_soul.ingest.pipeline import (
    IngestionBudget,
    IngestionBudgetError,
    IngestionPipeline,
    canonical_ingestion_dump,
)
from know_your_soul.store import EventStore

PDF_PAGES = ["alpha one\n\nbeta two", "gamma three", "delta four"]


def make_pipeline(store, **kwargs):
    kwargs.setdefault("resolver", ToyResolver())
    return IngestionPipeline(store, **kwargs)


def write_pdf(root, relpath, pages):
    target = root / relpath
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(build_pdf_bytes(pages))


def symbols(store, path):
    with store.read() as conn:
        return conn.execute("SELECT * FROM symbols WHERE path = ? ORDER BY node_id", (path,)).fetchall()


def edges(store):
    with store.read() as conn:
        return conn.execute("SELECT * FROM graph_edges ORDER BY source, target").fetchall()


def refs(store):
    with store.read() as conn:
        return conn.execute("SELECT * FROM unresolved_refs ORDER BY ref_id").fetchall()


# ------------------------------------------------------------------ routing


class TestDocumentRouting:
    def test_markdown_proposed(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# Title\n\nbody text\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            entry = report.files[0]
            assert entry.status == "proposed"
            assert entry.language == "markdown"
            assert entry.nodes > 1

    def test_text_proposed(self, tmp_path):
        write_fixture(tmp_path, {"notes.txt": "alpha\n\nbeta\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.files[0].status == "proposed"
            assert report.files[0].language == "text"

    def test_pdf_proposed(self, tmp_path):
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            entry = report.files[0]
            assert entry.status == "proposed"
            assert entry.language == "pdf"

    def test_mixed_tree_code_and_documents(self, tmp_path):
        write_fixture(tmp_path, {"src/a.py": "def alpha():\n    pass\n", "doc.md": "# T\n\nx\n", "n.txt": "y\n"})
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            statuses = {entry.relpath: entry.status for entry in report.files}
            assert statuses == {
                "book.pdf": "proposed",
                "doc.md": "proposed",
                "n.txt": "proposed",
                "src/a.py": "proposed",
            }
            languages = {entry.relpath: entry.language for entry in report.files}
            assert languages["src/a.py"] == "python"
            assert languages["doc.md"] == "markdown"
            assert languages["book.pdf"] == "pdf"
            assert languages["n.txt"] == "text"

    def test_single_pdf_file_ingest(self, tmp_path):
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path / "book.pdf")
            assert report.files[0].status == "proposed"

    def test_files_table_language_column(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n"})
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            languages = {row.path: row.language for row in store.list_files()}
            assert languages == {"book.pdf": "pdf", "doc.md": "markdown"}

    def test_extraction_version_per_format(self, tmp_path):
        write_fixture(tmp_path, {"a.py": "def f():\n    pass\n", "doc.md": "# T\n\nx\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            versions = {row.path: row.extraction_version for row in store.list_files()}
            assert versions["a.py"] == EXTRACTION_VERSION
            assert versions["doc.md"] == DOCUMENT_EXTRACTION_VERSION


# ------------------------------------------------------------------ contents


class TestStoredContent:
    def test_markdown_sections_and_paragraphs(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# Alpha\n\n## Beta\n\nbody\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            rows = symbols(store, "doc.md")
            kinds = sorted(row["kind"] for row in rows)
            assert kinds == ["document", "paragraph", "section", "section"]

    def test_markdown_contains_chain(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# Alpha\n\n## Beta\n\ntext\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            rows = {row["name"]: row for row in symbols(store, "doc.md")}
            edge_pairs = {(row["source"], row["target"]) for row in edges(store)}
            assert (file_node_id("doc.md"), rows["doc.md"]["node_id"]) in edge_pairs
            assert (rows["doc.md"]["node_id"], rows["Alpha"]["node_id"]) in edge_pairs
            assert (rows["Alpha"]["node_id"], rows["Beta"]["node_id"]) in edge_pairs

    def test_pdf_pages_and_paragraphs(self, tmp_path):
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            rows = symbols(store, "book.pdf")
            kinds = sorted(row["kind"] for row in rows)
            assert kinds.count("page") == 3
            assert kinds.count("paragraph") == 4
            assert kinds.count("document") == 1

    def test_pdf_page_names_and_lines(self, tmp_path):
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            pages = sorted((row["name"], row["line"]) for row in symbols(store, "book.pdf") if row["kind"] == "page")
            assert pages == [("page-1", 1), ("page-2", 2), ("page-3", 3)]

    def test_pdf_no_text_layer_zero_children(self, tmp_path):
        write_pdf(tmp_path, "scan.pdf", ["", ""])
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            rows = symbols(store, "scan.pdf")
            assert [row["kind"] for row in rows] == ["document"]
            document_id = rows[0]["node_id"]
            assert not [row for row in edges(store) if row["source"] == document_id]
            # file root + document = 2 nodes in the report
            assert report.files[0].nodes == 2

    def test_text_paragraph_chunks(self, tmp_path):
        write_fixture(tmp_path, {"notes.txt": "one\n\ntwo\n\nthree\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            rows = symbols(store, "notes.txt")
            assert [row["kind"] for row in rows].count("paragraph") == 3


# ------------------------------------------------------------------ failures


class TestDocumentFailures:
    def test_broken_pdf_typed_failure(self, tmp_path):
        write_fixture(tmp_path, {"ok.md": "# T\n\nx\n"})
        (tmp_path / "broken.pdf").write_bytes(b"%PDF-1.4 garbage broken bytes")
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            by_path = {entry.relpath: entry for entry in report.files}
            assert by_path["broken.pdf"].status == "failed"
            assert by_path["broken.pdf"].error_code == "PDF_PARSE_ERROR"
            # one bad file never aborts the walk
            assert by_path["ok.md"].status == "proposed"

    def test_encrypted_pdf_typed_failure(self, tmp_path):
        (tmp_path / "locked.pdf").write_bytes(build_encrypted_pdf_bytes(["secret"]))
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.files[0].status == "failed"
            assert report.files[0].error_code == "PDF_ENCRYPTED"

    def test_invalid_utf8_text_decode_error(self, tmp_path):
        (tmp_path / "bad.txt").write_bytes(b"\xff\xfe\x00\x01binary")
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.files[0].status == "failed"
            assert report.files[0].error_code == "DECODE_ERROR"

    def test_pdf_budget_still_enforced(self, tmp_path):
        write_pdf(tmp_path, "big.pdf", PDF_PAGES)
        with EventStore() as store:
            pipeline = make_pipeline(store, budget=IngestionBudget(max_file_bytes=100))
            report = pipeline.ingest_path(tmp_path)
            assert report.files[0].error_code == "FILE_TOO_LARGE"

    def test_node_budget_counts_documents(self, tmp_path):
        body = "\n\n".join(f"paragraph {index}" for index in range(20))
        write_fixture(tmp_path, {"big.txt": body})
        with EventStore() as store:
            pipeline = make_pipeline(store, budget=IngestionBudget(max_nodes=5))
            with pytest.raises(IngestionBudgetError):
                pipeline.ingest_path(tmp_path)


# ------------------------------------------------------------------ incremental


class TestIncremental:
    def test_second_run_skips_documents(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n"})
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            report = make_pipeline(store).ingest_path(tmp_path)
            assert {entry.status for entry in report.files} == {"skipped"}
            assert report.stats["proposed"] == 0

    def test_incremental_dump_stable(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n"})
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            first = canonical_ingestion_dump(store)
            make_pipeline(store).ingest_path(tmp_path)
            assert canonical_ingestion_dump(store) == first

    def test_content_change_reingests(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# Before\n\nx\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            write_fixture(tmp_path, {"doc.md": "# After\n\nlonger body here\n"})
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.files[0].status == "proposed"
            names = {row["name"] for row in symbols(store, "doc.md")}
            assert "After" in names
            assert "Before" not in names

    def test_deleted_document_cascades(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n", "keep.txt": "y\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            (tmp_path / "doc.md").unlink()
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.stats["deleted"] == 1
            assert symbols(store, "doc.md") == []
            assert not [row for row in edges(store) if row["source"] == file_node_id("doc.md")]

    def test_pdf_change_reingests(self, tmp_path):
        write_pdf(tmp_path, "book.pdf", ["one"])
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            write_pdf(tmp_path, "book.pdf", ["one", "two", "three"])
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.files[0].status == "proposed"
            pages = [row for row in symbols(store, "book.pdf") if row["kind"] == "page"]
            assert len(pages) == 3


# ------------------------------------------------------------------ references


class TestDocumentReferences:
    def test_markdown_links_parked_pending(self, tmp_path):
        write_fixture(tmp_path, {"a.md": "see [x](b.md)\n", "b.md": "# B\n\ny\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path, resolve=False)
            rows = refs(store)
            assert len(rows) == 1
            assert rows[0]["kind"] == "references"
            assert rows[0]["target_name"] == "b.md"
            assert rows[0]["status"] == "pending"

    def test_link_resolves_to_document_node(self, tmp_path):
        write_fixture(tmp_path, {"a.md": "see [x](b.md)\n", "b.md": "# B\n\ny\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.resolution == {"resolved": 1, "failed": 0, "pending": 0}
            b_document = make_node_id(path="b.md", kind="document", name="b.md", line=1)
            assert any(row["target"] == b_document and row["kind"] == "references" for row in edges(store))

    def test_unresolvable_link_fails_closed(self, tmp_path):
        write_fixture(tmp_path, {"a.md": "see [x](missing.md)\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.resolution == {"resolved": 0, "failed": 1, "pending": 0}

    def test_real_resolver_parks_unknown_kind_deterministically(self, tmp_path):
        # The W2 resolver's kind table predates ``references`` (frozen);
        # the ref enters the resolve lane and gets a typed, deterministic
        # outcome instead of crashing. See docs/recovery/w3-documents.md.
        from know_your_soul.resolve.resolver import ReferenceResolver

        write_fixture(tmp_path, {"a.md": "see [x](b.md)\n", "b.md": "# B\n\ny\n"})
        with EventStore() as store:
            report = IngestionPipeline(store, resolver=ReferenceResolver()).ingest_path(tmp_path)
            assert report.resolution == {"resolved": 0, "failed": 1, "pending": 0}
            failed = store.failed_unresolved_refs(name_tail="b.md")
            assert len(failed) == 1
            assert failed[0].kind == "references"

    def test_cross_document_graph_connected(self, tmp_path):
        write_fixture(tmp_path, {"a.md": "[x](b.md)\n", "b.md": "[y](c.md)\n", "c.md": "# C\n\nz\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.resolution == {"resolved": 2, "failed": 0, "pending": 0}
            reference_edges = [row for row in edges(store) if row["kind"] == "references"]
            assert len(reference_edges) == 2


# ------------------------------------------------------------------ proposals


class TestPrimarySuggestions:
    def test_large_document_triggers_split_suggestion(self, tmp_path):
        body = "\n\n".join(f"paragraph number {index} with text" for index in range(30))
        write_fixture(tmp_path, {"big.txt": body})
        with EventStore() as store:
            report = make_pipeline(store, split_thresholds=SplitThresholds(max_subtree_nodes=10)).ingest_path(tmp_path)
            assert report.primary_suggestions
            assert any("subtree_size" in s["reasons"] for s in report.primary_suggestions)

    def test_small_document_no_suggestion(self, tmp_path):
        write_fixture(tmp_path, {"small.txt": "one\n\ntwo\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.primary_suggestions == ()

    def test_suggestions_are_proposal_only(self, tmp_path):
        body = "\n\n".join(f"p {index}" for index in range(20))
        write_fixture(tmp_path, {"big.txt": body})
        with EventStore() as store:
            report = make_pipeline(store, split_thresholds=SplitThresholds(max_subtree_nodes=5)).ingest_path(tmp_path)
            assert report.status == "proposal"
            assert report.primary_suggestions
            # nothing but the v3 ingestion tables was written
            kinds = {row["kind"] for row in symbols(store, "big.txt")}
            assert "primary" not in kinds


# ------------------------------------------------------------------ determinism


class TestDeterminism:
    def test_byte_identical_two_stores(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nsee [x](other.md)\n", "other.md": "# O\n\ny\n"})
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        dumps = []
        for _index in range(2):
            with EventStore() as store:
                make_pipeline(store).ingest_path(tmp_path)
                dumps.append(canonical_ingestion_dump(store))
        assert dumps[0] == dumps[1]

    def test_report_input_sha256_stable(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n"})
        with EventStore() as store:
            first = make_pipeline(store).ingest_path(tmp_path)
        with EventStore() as store:
            second = make_pipeline(store).ingest_path(tmp_path)
        assert first.input_sha256 == second.input_sha256

    def test_audit_event_recorded(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n"})
        with EventStore() as store:
            report = make_pipeline(store).ingest_path(tmp_path)
            assert report.audit_event_id is not None

    def test_document_node_id_matches_contract(self, tmp_path):
        write_fixture(tmp_path, {"doc.md": "# T\n\nx\n"})
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            rows = symbols(store, "doc.md")
            document = [row for row in rows if row["kind"] == "document"][0]
            assert document["node_id"] == make_node_id(path="doc.md", kind="document", name="doc.md", line=1)

    def test_sha256_of_raw_bytes_stored(self, tmp_path):
        write_pdf(tmp_path, "book.pdf", PDF_PAGES)
        raw = (tmp_path / "book.pdf").read_bytes()
        with EventStore() as store:
            make_pipeline(store).ingest_path(tmp_path)
            row = [row for row in store.list_files() if row.path == "book.pdf"][0]
            assert row.sha256 == hashlib.sha256(raw).hexdigest()
            assert row.size == len(raw)

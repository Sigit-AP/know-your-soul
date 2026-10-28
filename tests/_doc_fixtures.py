"""Deterministic document fixtures for the W3 document-ingestion tests (SUB-90).

``build_pdf_bytes`` synthesizes a minimal multi-page PDF with a real text
layer using only pypdf itself (no external assets, no renderer): one blank
page per entry, one Helvetica text-draw op per page. Output is a pure
function of the input strings — two builds of the same pages are
byte-identical — so extraction fixtures are stable across runs and machines.
"""

from __future__ import annotations

import hashlib
import io

from pypdf import PdfWriter
from pypdf.generic import DecodedStreamObject, DictionaryObject, NameObject


def _escape_pdf_string(text: str) -> str:
    return text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)").replace("\n", "\\n")


def build_pdf_bytes(pages: list[str]) -> bytes:
    """Build a byte-deterministic PDF whose pages carry *pages* as text."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({_escape_pdf_string(text)}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def build_encrypted_pdf_bytes(pages: list[str], *, password: str = "s3cret") -> bytes:
    """A password-encrypted PDF (no text layer reachable without the password)."""
    writer = PdfWriter()
    for text in pages:
        page = writer.add_blank_page(width=612, height=792)
        font = DictionaryObject()
        font[NameObject("/Type")] = NameObject("/Font")
        font[NameObject("/Subtype")] = NameObject("/Type1")
        font[NameObject("/BaseFont")] = NameObject("/Helvetica")
        fonts = DictionaryObject()
        fonts[NameObject("/F1")] = font
        resources = DictionaryObject()
        resources[NameObject("/Font")] = fonts
        page[NameObject("/Resources")] = resources
        stream = DecodedStreamObject()
        stream.set_data(f"BT /F1 12 Tf 72 720 Td ({_escape_pdf_string(text)}) Tj ET".encode("latin-1"))
        page[NameObject("/Contents")] = writer._add_object(stream)
    writer.encrypt(password)
    buffer = io.BytesIO()
    writer.write(buffer)
    return buffer.getvalue()


def pdf_sha256(pages: list[str]) -> str:
    return hashlib.sha256(build_pdf_bytes(pages)).hexdigest()

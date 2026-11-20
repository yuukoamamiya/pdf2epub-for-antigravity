"""Input identity gates for PDF write commands.

``ocr-pages`` derives its output directory from the configured title, while
``-i`` only replaces the source path.  These tests pin the guard that stops a
mismatched source (or a non-PDF that PyMuPDF would happily open page by page)
from writing into another book's workspace.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands.sources import validate_pdf_source


def _pdf_bytes() -> bytes:
    return b"%PDF-1.7\n1 0 obj\n<< /Type /Catalog >>\nendobj\ntrailer\n%%EOF\n"


def _write_pdf(path: Path) -> str:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(_pdf_bytes())
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_accepts_pdf_and_returns_digest(tmp_path: Path):
    source = tmp_path / "book.pdf"
    digest = _write_pdf(source)

    assert validate_pdf_source(source) == digest


def test_rejects_non_pdf_suffix_even_with_pdf_bytes(tmp_path: Path):
    source = tmp_path / "book.epub"
    _write_pdf(source)

    with pytest.raises(ValueError, match="requires a PDF input"):
        validate_pdf_source(source)


def test_rejects_pdf_suffix_without_pdf_header(tmp_path: Path):
    source = tmp_path / "book.pdf"
    source.write_bytes(b"PK\x03\x04 this is really an epub")

    with pytest.raises(ValueError, match="not a PDF"):
        validate_pdf_source(source)


def test_rejects_missing_input(tmp_path: Path):
    with pytest.raises(ValueError, match="does not exist"):
        validate_pdf_source(tmp_path / "missing.pdf")


def test_rejects_source_that_conflicts_with_recorded_progress(tmp_path: Path):
    output_dir = tmp_path / "output" / "Book"
    (output_dir / "pages").mkdir(parents=True)
    (output_dir / "pages" / "ocr_progress.json").write_text(
        json.dumps({"source_sha256": "a" * 64}), encoding="utf-8"
    )
    source = tmp_path / "other.pdf"
    _write_pdf(source)

    with pytest.raises(ValueError, match="Refusing to mix a different PDF") as error:
        validate_pdf_source(source, output_dir=output_dir)
    assert "--allow-source-change" in str(error.value)


def test_rejects_source_that_conflicts_with_probe(tmp_path: Path):
    output_dir = tmp_path / "output" / "Book"
    output_dir.mkdir(parents=True)
    (output_dir / "pdf_text_probe.json").write_text(
        json.dumps({"source_sha256": "b" * 64}), encoding="utf-8"
    )
    source = tmp_path / "other.pdf"
    _write_pdf(source)

    with pytest.raises(ValueError, match="pdf_text_probe="):
        validate_pdf_source(source, output_dir=output_dir)


def test_accepts_source_that_matches_recorded_progress(tmp_path: Path):
    output_dir = tmp_path / "output" / "Book"
    (output_dir / "pages").mkdir(parents=True)
    source = tmp_path / "book.pdf"
    digest = _write_pdf(source)
    (output_dir / "pages" / "ocr_progress.json").write_text(
        json.dumps({"source_sha256": digest}), encoding="utf-8"
    )

    assert validate_pdf_source(source, output_dir=output_dir) == digest


def test_allow_source_change_bypasses_recorded_identity(tmp_path: Path):
    output_dir = tmp_path / "output" / "Book"
    (output_dir / "pages").mkdir(parents=True)
    (output_dir / "pages" / "ocr_progress.json").write_text(
        json.dumps({"source_sha256": "c" * 64}), encoding="utf-8"
    )
    source = tmp_path / "replacement.pdf"
    digest = _write_pdf(source)

    assert (
        validate_pdf_source(source, output_dir=output_dir, allow_source_change=True)
        == digest
    )


def test_ocr_pages_command_rejects_non_pdf_before_writing(tmp_path: Path, monkeypatch):
    """End-to-end proof that the guard runs before probe/preprocess writes."""

    from pdf2epub.commands import ocr as ocr_commands

    (tmp_path / "input").mkdir()
    epub = tmp_path / "input" / "book.epub"
    epub.write_bytes(b"PK\x03\x04 fake epub")
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "\n".join(
            [
                'title: "Guard Book"',
                "input_pdf: input/book.epub",
                "ocr:",
                "  backend: chandra",
                "  secondary:",
                "    enabled: false",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    monkeypatch.chdir(tmp_path)
    args = SimpleNamespace(
        config=str(config_path),
        input=None,
        resume=False,
        start_page=None,
        end_page=None,
        max_workers=None,
        allow_empty_pages=False,
        retry_pages="",
        allow_slow_secondary=False,
        allow_source_change=False,
    )

    assert ocr_commands.ocr_pages_command(args) == 1
    output_dir = tmp_path / "output" / "Guard Book"
    assert not (output_dir / "pdf_text_probe.json").exists()
    assert not (output_dir / "input.pdf").exists()

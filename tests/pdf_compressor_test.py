"""Happy-path and failure coverage for the standalone PDF compressor."""

from pathlib import Path

import pymupdf as fitz

from pdf2epub.pdf_compressor import compress_pdf


def _write_sample_pdf(path: Path) -> None:
    document = fitz.open()
    page = document.new_page(width=200, height=200)
    page.insert_text((40, 100), "Hello pdf2epub")
    document.save(path)
    document.close()


def test_compress_pdf_writes_binarized_output(tmp_path: Path):
    source = tmp_path / "input.pdf"
    target = tmp_path / "output.pdf"
    _write_sample_pdf(source)

    success, stats = compress_pdf(source, target, dpi=72)

    assert success is True
    assert target.is_file() and target.stat().st_size > 0
    assert stats["page_count"] == 1
    assert stats["input_size_mb"] > 0
    assert stats["output_size_mb"] > 0
    assert stats["compression_ratio"] > 0

    with fitz.open(target) as compressed:
        assert len(compressed) == 1


def test_compress_pdf_reports_missing_input(tmp_path: Path):
    success, stats = compress_pdf(tmp_path / "missing.pdf", tmp_path / "out.pdf")

    assert success is False
    assert "error" in stats
    assert not (tmp_path / "out.pdf").exists()

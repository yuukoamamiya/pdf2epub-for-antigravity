import json
from pathlib import Path

import fitz

from pdf2epub.ocr.artifacts import OCRPageResult
from pdf2epub.ocr_pages import ocr_full_book_pagewise
from pdf2epub.ocr_progress import assess_progress


def _make_pdf(path: Path, page_count: int = 2) -> None:
    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    document.save(path)
    document.close()


def _result(markdown: str) -> OCRPageResult:
    return OCRPageResult(markdown=markdown, backend="fake")


def test_assess_progress_detects_missing_tail_page(tmp_path: Path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text("one", encoding="utf-8")
    (pages_dir / "page_002.md").write_text("two", encoding="utf-8")
    (pages_dir / "ocr_progress.json").write_text(
        json.dumps(
            {
                "schema_version": 2,
                "mode": "ocr",
                "backend": "fake",
                "source_sha256": "source",
                "total_pages": 3,
                "pages_processed": [1, 2],
                "failed_pages": [],
                "empty_pages": [],
                "allowed_empty_pages": [],
            }
        ),
        encoding="utf-8",
    )

    report = assess_progress(pages_dir, expected_total_pages=3)

    assert report["ready"] is False
    assert report["missing_pages"] == [3]
    assert any("non-contiguous" in error for error in report["errors"])


def test_pagewise_ocr_returns_incomplete_summary_for_failed_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path)

    def fake_ocr_page(*args, **kwargs):
        page_number = args[6]
        if page_number == 2:
            raise RuntimeError("backend unavailable")
        return _result("page one")

    monkeypatch.setattr("pdf2epub.ocr_pages.ocr_pdf_page", fake_ocr_page)
    summary = ocr_full_book_pagewise(
        pdf_path=pdf_path,
        output_dir=tmp_path / "output",
        backend="fake",
        config={"ocr": {"backends": {"fake": {}}}},
        max_workers=1,
    )

    assert summary["failed_pages"] == [2]
    assert summary["missing_pages"] == [2]
    progress = json.loads(
        (tmp_path / "output" / "pages" / "ocr_progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["total_pages"] == 2
    assert progress["failed_pages"] == [2]


def test_empty_ocr_result_requires_explicit_acknowledgement(tmp_path, monkeypatch):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path, page_count=1)

    monkeypatch.setattr(
        "pdf2epub.ocr_pages.ocr_pdf_page",
        lambda *args, **kwargs: _result(""),
    )
    output_dir = tmp_path / "output"
    first = ocr_full_book_pagewise(
        pdf_path=pdf_path,
        output_dir=output_dir,
        backend="fake",
        config={"ocr": {"backends": {"fake": {}}}},
        max_workers=1,
    )
    assert first["empty_pages"] == [1]
    assert first["missing_pages"] == [1]

    second = ocr_full_book_pagewise(
        pdf_path=pdf_path,
        output_dir=output_dir,
        backend="fake",
        config={"ocr": {"backends": {"fake": {}}}},
        resume=True,
        allow_empty_pages=True,
        max_workers=1,
    )
    assert second["empty_pages"] == []
    assert second["missing_pages"] == []


def test_corrupt_progress_is_rebuilt_on_resume(tmp_path, monkeypatch):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path, page_count=1)
    output_dir = tmp_path / "output"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "ocr_progress.json").write_text("{broken", encoding="utf-8")

    monkeypatch.setattr(
        "pdf2epub.ocr_pages.ocr_pdf_page",
        lambda *args, **kwargs: _result("recovered"),
    )
    summary = ocr_full_book_pagewise(
        pdf_path=pdf_path,
        output_dir=output_dir,
        backend="fake",
        config={"ocr": {"backends": {"fake": {}}}},
        resume=True,
        max_workers=1,
    )

    assert summary["missing_pages"] == []
    progress = json.loads(
        (pages_dir / "ocr_progress.json").read_text(encoding="utf-8")
    )
    assert progress["schema_version"] == 2
    assert progress["pages_processed"] == [1]


def test_retry_pages_reprocesses_a_completed_page(tmp_path, monkeypatch):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path, page_count=1)
    output_dir = tmp_path / "output"
    responses = iter(["first pass", "second pass"])
    monkeypatch.setattr(
        "pdf2epub.ocr_pages.ocr_pdf_page",
        lambda *args, **kwargs: _result(next(responses)),
    )

    first = ocr_full_book_pagewise(
        pdf_path=pdf_path,
        output_dir=output_dir,
        backend="fake",
        config={"ocr": {"backends": {"fake": {}}}},
        max_workers=1,
    )
    assert first["missing_pages"] == []
    assert (output_dir / "pages" / "page_001.md").read_text(encoding="utf-8") == "first pass"

    second = ocr_full_book_pagewise(
        pdf_path=pdf_path,
        output_dir=output_dir,
        backend="fake",
        config={"ocr": {"backends": {"fake": {}}}},
        resume=True,
        retry_pages=[1],
        max_workers=1,
    )
    assert second["missing_pages"] == []
    assert (output_dir / "pages" / "page_001.md").read_text(encoding="utf-8") == "second pass"

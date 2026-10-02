import json
from pathlib import Path

import fitz

from pdf2epub.ocr.artifacts import OCRPageResult
from pdf2epub.ocr_consensus import (
    compare_ocr_texts,
    common_ocr_risk_reasons,
    consensus_is_current,
    load_consensus_manifest,
    secondary_backend_name,
    secondary_ocr_enabled,
)
from pdf2epub.ocr_pages import run_secondary_ocr_consensus


def _make_pdf(path: Path, page_count: int = 2) -> None:
    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    document.save(path)
    document.close()


def test_consensus_ignores_markdown_layout_only_differences():
    config = {"ocr": {"consensus": {}}}

    report = compare_ocr_texts("# Heading\nA *short* line", "Heading\nA short line", config)

    assert report["status"] == "agree"
    assert report["reasons"] == []


def test_consensus_flags_missing_lines_and_numeric_changes():
    config = {"ocr": {"consensus": {}}}

    report = compare_ocr_texts(
        "Chapter 12\nThe second line is present.",
        "Chapter 13",
        config,
    )

    assert report["status"] == "review_required"
    assert "numeric markers differ" in report["reasons"]
    assert "OCR text differs above the configured threshold" in report["reasons"]


def test_consensus_flags_one_missing_line_even_when_text_is_long():
    config = {"ocr": {"consensus": {}}}
    primary = "\n".join(["shared line"] * 40 + ["important final line"])
    secondary = "\n".join(["shared line"] * 40)

    report = compare_ocr_texts(primary, secondary, config)

    assert report["status"] == "review_required"
    assert "non-empty line counts differ" in report["reasons"]


def test_common_ocr_risk_flags_anomalously_sparse_internal_page():
    page_texts = {
        "page_001.md": {"primary": "word\n" * 500, "secondary": "word\n" * 500},
        "page_002.md": {"primary": "word\n" * 100, "secondary": "word\n" * 100},
        "page_003.md": {"primary": "word\n" * 500, "secondary": "word\n" * 500},
    }

    risks = common_ocr_risk_reasons(
        page_texts,
        {"ocr": {"consensus": {"sample_every": 0}}},
    )

    assert "page_002.md" in risks
    assert any("density" in reason for reason in risks["page_002.md"])


def test_secondary_ocr_switch_controls_consensus_and_keeps_legacy_alias():
    disabled = {
        "ocr": {"secondary": {"enabled": False, "backend": "paddle"}}
    }
    enabled = {
        "ocr": {"secondary": {"enabled": True, "backend": "paddle"}}
    }
    missing_backend = {"ocr": {"secondary": {"enabled": True}}}
    legacy = {"ocr": {"secondary_backend": "paddle"}}

    assert secondary_ocr_enabled(disabled) is False
    assert secondary_backend_name(disabled) is None
    assert secondary_ocr_enabled(enabled) is True
    assert secondary_backend_name(enabled) == "paddle"
    assert secondary_ocr_enabled(missing_backend) is True
    assert secondary_backend_name(missing_backend) is None
    assert secondary_ocr_enabled(legacy) is True
    assert secondary_backend_name(legacy) == "paddle"


def test_pagewise_secondary_consensus_scopes_visual_review(monkeypatch, tmp_path: Path):
    pdf_path = tmp_path / "input.pdf"
    _make_pdf(pdf_path)
    output_dir = tmp_path / "output"
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "page_001.md").write_text("same text\n", encoding="utf-8")
    (pages_dir / "page_002.md").write_text("Chapter 12\nline two\n", encoding="utf-8")

    def fake_secondary(*args, **kwargs):
        page_number = kwargs["page_number"]
        text = "same text\n" if page_number == 1 else "Chapter 13\n"
        return OCRPageResult(markdown=text, backend="fake")

    monkeypatch.setattr("pdf2epub.ocr_pages.ocr_pdf_page", fake_secondary)
    config = {
        "ocr": {
            "backend": "chandra",
            "secondary_backend": "fake",
            "backends": {"fake": {"max_workers": 1}},
            "consensus": {},
        }
    }

    summary = run_secondary_ocr_consensus(
        ocr_pdf=pdf_path,
        output_dir=output_dir,
        total_pages=2,
        primary_backend="chandra",
        config=config,
        max_workers=1,
        resume=False,
    )

    assert summary["failed_pages"] == []
    assert summary["review_pages"] == ["page_002.md"]
    manifest = load_consensus_manifest(output_dir)
    assert manifest["complete"] is True
    assert consensus_is_current(
        output_dir,
        config,
        primary_backend="chandra",
    )
    record = json.loads(
        (output_dir / "ocr_consensus" / "page_002.json").read_text(encoding="utf-8")
    )
    assert record["action"] == "visual_review"

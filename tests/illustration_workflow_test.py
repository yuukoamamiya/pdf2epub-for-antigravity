import json
from pathlib import Path

import pytest

from pdf2epub.ocr.illustration_extractor import inject_illustrations_into_text
from pdf2epub.refine.illustration_prepare import (
    apply_illustration_bindings,
    load_current_illustration_pages,
    prepare_illustration_subagent,
    validate_illustration_decisions,
)
from pdf2epub.refine.page_merger import _merge_full_page_insertions


def test_full_page_insert_repairs_sentence_and_keeps_image_after_it():
    result = _merge_full_page_insertions(
        [
            (10, "The sentence starts"),
            (11, "![Image](../images/plate.png)"),
            (12, "and continues here."),
        ],
        {11},
    )

    assert "The sentence starts and continues here." in result
    assert result.index("The sentence starts and continues here.") < result.index("plate.png")


def test_unreviewed_or_ordinary_illustration_keeps_physical_order():
    entries = [
        (10, "The sentence starts"),
        (11, "![Image](../images/figure.png)"),
        (12, "and continues here."),
    ]
    assert _merge_full_page_insertions(entries, set()) == "\n\n".join(content for _, content in entries)


def test_full_page_insert_does_not_join_complete_sentence_or_heading():
    complete = _merge_full_page_insertions(
        [(1, "The sentence ends."), (2, "![Image](plate.png)"), (3, "A new paragraph")],
        {2},
    )
    heading = _merge_full_page_insertions(
        [(1, "The sentence starts"), (2, "![Image](plate.png)"), (3, "## New section")],
        {2},
    )
    assert complete.index("plate.png") < complete.index("A new paragraph")
    assert heading.index("plate.png") < heading.index("## New section")


def test_vllm_full_page_marker_is_materialized():
    result = inject_illustrations_into_text(
        "[illustration]",
        [{"path": "../images/plate.png", "placement": "full_page"}],
    )
    assert "[illustration]" not in result
    assert "![Image](../images/plate.png)" in result


def _write_one_page_pdf(path: Path) -> None:
    import pymupdf as fitz

    document = fitz.open()
    for _ in range(3):
        document.new_page()
    document.save(path)
    document.close()


def _write_candidate_fixture(root: Path) -> None:
    pages = root / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Previous sentence", encoding="utf-8")
    (pages / "page_002.md").write_text("![Image](../images/plate.png)", encoding="utf-8")
    (pages / "page_003.md").write_text("continues here.", encoding="utf-8")
    (pages / "page_002.ocr.json").write_text(
        json.dumps(
            {
                "page_number": 2,
                "page_box": [0, 0, 1000, 1000],
                "blocks": [
                    {
                        "order": 0,
                        "label": "Image",
                        "bbox": [0, 0, 1000, 1000],
                        "html": "<img>",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    _write_one_page_pdf(root / "input.pdf")


def test_illustration_decision_is_hash_bound_and_applied(tmp_path: Path):
    _write_candidate_fixture(tmp_path)
    paths = prepare_illustration_subagent(tmp_path, book_title="Book", config={})
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    assert report["status"] == "pending_review"
    assert report["candidate_pages"]
    assert report["candidate_pages"][0]["previous_page"] == 1
    assert report["candidate_pages"][0]["next_page"] == 3

    (tmp_path / "illustration_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "candidate_report_sha256": report["report_sha256"],
                "decisions": [
                    {
                        "page": 2,
                        "role": "full_page_insert",
                        "confidence": "high",
                        "reason": "full-page colour plate",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    validation = validate_illustration_decisions(tmp_path)
    assert validation["valid"] is True
    applied = apply_illustration_bindings(tmp_path)
    assert applied["valid"] is True
    assert load_current_illustration_pages(tmp_path) == {2}

    (tmp_path / "pages" / "page_002.md").write_text("changed", encoding="utf-8")
    with pytest.raises(ValueError, match="stale"):
        load_current_illustration_pages(tmp_path)


def test_no_candidate_does_not_require_subagent(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Ordinary body text.", encoding="utf-8")
    paths = prepare_illustration_subagent(tmp_path, book_title="Book", config={})
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["status"] == "no_review_required"
    assert validate_illustration_decisions(tmp_path)["valid"] is True
    assert apply_illustration_bindings(tmp_path)["bindings"] == []


def test_native_pdf_uses_native_evidence_for_illustration_review(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Ordinary body text.", encoding="utf-8")
    (pages / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text"}),
        encoding="utf-8",
    )

    paths = prepare_illustration_subagent(
        tmp_path,
        book_title="Book",
        config={"ocr": {"secondary": {"enabled": True, "backend": "paddle"}}},
    )
    report = json.loads(paths["report"].read_text(encoding="utf-8"))

    assert report["ocr_evidence_mode"] == "single_ocr"

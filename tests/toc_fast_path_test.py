import copy
import json
from pathlib import Path

import pymupdf as fitz

from pdf2epub.refine.toc_fast_path import (
    prepare_toc_fast_path,
    validate_toc_fast_path_review,
)


def _write_pdf(path: Path, page_count: int = 5) -> None:
    document = fitz.open()
    for _ in range(page_count):
        document.new_page()
    document.save(path)
    document.close()


def _write_pages(output_dir: Path) -> None:
    pages = {
        1: "A Book\nAuthor Name\nExample Press\n",
        2: "Contents\n\n1. Chapter One .... 3\n2. Chapter Two .... 5\n",
        3: "Front matter\n\nChapter One\nFirst body paragraph\n",
        4: "Chapter One continues\n",
        5: "Chapter Two\nSecond body paragraph\n",
    }
    pages_dir = output_dir / "pages"
    pages_dir.mkdir()
    for page, text in pages.items():
        (pages_dir / f"page_{page:03d}.md").write_text(text, encoding="utf-8")


def _outline() -> dict:
    return {
        "schema_version": 1,
        "source": "pdf-native-outline",
        "pdf_file": "input.pdf",
        "physical_page_is_authoritative": True,
        "total_pages": 5,
        "extracted": True,
        "entry_count": 2,
        "chapters": [
            {
                "title": "Chapter One",
                "level": 1,
                "start_page": 3,
                "end_page": 4,
                "children": [],
            },
            {
                "title": "Chapter Two",
                "level": 1,
                "start_page": 5,
                "end_page": 5,
                "children": [],
            },
        ],
    }


def test_native_outline_fast_path_selects_contents_and_boundary_pages(tmp_path: Path):
    _write_pages(tmp_path)
    _write_pdf(tmp_path / "input.pdf")
    outline_path = tmp_path / "outline_toc_draft.json"
    outline_path.write_text(json.dumps(_outline()), encoding="utf-8")

    result = prepare_toc_fast_path(
        tmp_path,
        pdf_path=tmp_path / "input.pdf",
        pages_dir=tmp_path / "pages",
        book_title="A Book",
        model="configured-model",
        page_source_kind="native_text",
        outline_path=outline_path,
        outline_draft=_outline(),
    )

    assert result["data"]["status"] == "eligible"
    assert result["data"]["contents_pages"] == [2]
    assert result["data"]["bookmark_start_pages"] == [3, 5]
    assert result["data"]["review_pages"] == [1, 2, 3, 4, 5]
    assert (tmp_path / "toc_fast_path_prompt.md").is_file()
    assert (tmp_path / "toc_review_images" / "page_003.png").is_file()


def test_fast_path_requires_visual_boundary_decisions_and_preserves_outline(tmp_path: Path):
    _write_pages(tmp_path)
    _write_pdf(tmp_path / "input.pdf")
    outline = _outline()
    outline_path = tmp_path / "outline_toc_draft.json"
    outline_path.write_text(json.dumps(outline), encoding="utf-8")
    result = prepare_toc_fast_path(
        tmp_path,
        pdf_path=tmp_path / "input.pdf",
        pages_dir=tmp_path / "pages",
        book_title="A Book",
        model="configured-model",
        page_source_kind="native_text",
        outline_path=outline_path,
        outline_draft=outline,
    )
    manifest = result["data"]

    tree = copy.deepcopy(outline)
    tree.update(
        {
            "book_title": "A Book",
            "author": "Author Name",
            "publisher": "Example Press",
        }
    )
    tree["chapters"][0]["boundary_info"] = {"start_line": 3}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (tmp_path / "toc_fast_path_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "accepted",
                "outline_sha256": manifest["outline_sha256"],
                "review_pages": manifest["review_pages"],
                "entries": [
                    {
                        "outline_entry": 1,
                        "title": "Chapter One",
                        "start_page": 3,
                        "mid_page": True,
                        "start_line": 3,
                    },
                    {
                        "outline_entry": 2,
                        "title": "Chapter Two",
                        "start_page": 5,
                        "mid_page": False,
                        "start_line": None,
                    },
                ],
                "fallback_reasons": [],
            }
        ),
        encoding="utf-8",
    )

    report = validate_toc_fast_path_review(tmp_path)

    assert report["valid"] is True


def test_fast_path_rejects_visual_fallback_and_non_native_inputs(tmp_path: Path):
    _write_pages(tmp_path)
    _write_pdf(tmp_path / "input.pdf")
    outline = _outline()
    outline_path = tmp_path / "outline_toc_draft.json"
    outline_path.write_text(json.dumps(outline), encoding="utf-8")

    result = prepare_toc_fast_path(
        tmp_path,
        pdf_path=tmp_path / "input.pdf",
        pages_dir=tmp_path / "pages",
        book_title="A Book",
        model="configured-model",
        page_source_kind="ocr",
        outline_path=outline_path,
        outline_draft=outline,
    )
    assert result["data"]["status"] == "not_eligible"
    assert "native-text" in " ".join(result["data"]["fallback_reasons"])


def test_fast_path_visual_fallback_blocks_local_refinement(tmp_path: Path):
    _write_pages(tmp_path)
    _write_pdf(tmp_path / "input.pdf")
    outline = _outline()
    outline_path = tmp_path / "outline_toc_draft.json"
    outline_path.write_text(json.dumps(outline), encoding="utf-8")
    result = prepare_toc_fast_path(
        tmp_path,
        pdf_path=tmp_path / "input.pdf",
        pages_dir=tmp_path / "pages",
        book_title="A Book",
        model="configured-model",
        page_source_kind="native_text",
        outline_path=outline_path,
        outline_draft=outline,
    )
    manifest = result["data"]
    (tmp_path / "toc_fast_path_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "status": "fallback_required",
                "outline_sha256": manifest["outline_sha256"],
                "review_pages": manifest["review_pages"],
                "entries": [],
                "fallback_reasons": ["contents entry did not match the bookmark"],
            }
        ),
        encoding="utf-8",
    )

    report = validate_toc_fast_path_review(tmp_path)

    assert report["valid"] is False
    assert report["status"] == "fallback_required"
    assert "refine_subagent_prompt.md" in str(report["full_review_prompt"])


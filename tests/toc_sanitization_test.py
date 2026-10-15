import json
from pathlib import Path

from pdf2epub.refine.pagination import build_pagination_map
from pdf2epub.refine.toc_sanitization import sanitize_toc_titles


def _write_page(pages: Path, number: int, text: str) -> None:
    (pages / f"page_{number:03d}.md").write_text(text, encoding="utf-8")


def test_toc_page_suffix_is_removed_only_with_two_independent_signals(
    tmp_path: Path,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    _write_page(
        pages,
        10,
        "# Chapter 1\n\nChapter body.\n\n10\n",
    )
    pagination_path = tmp_path / "pagination_map.json"
    build_pagination_map(pages, pagination_path)
    toc = {
        "chapters": [{
            "title": "Chapter 1 10",
            "level": 1,
            "start_page": 10,
            "end_page": 10,
        }]
    }

    normalized, report = sanitize_toc_titles(toc, pages, pagination_path)

    assert normalized["chapters"][0]["title"] == "Chapter 1"
    assert report["status"] == "applied"
    assert report["changed_count"] == 1
    assert report["changes"][0]["evidence"]["pagination"]["physical_page"] == 10
    assert report["changes"][0]["evidence"]["source_heading"]["match"] == "exact"


def test_toc_page_suffix_is_preserved_when_source_heading_evidence_is_missing(
    tmp_path: Path,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    _write_page(pages, 10, "A different heading\n\n10\n")
    pagination_path = tmp_path / "pagination_map.json"
    build_pagination_map(pages, pagination_path)
    toc = {
        "chapters": [{
            "title": "Chapter 1 10",
            "level": 1,
            "start_page": 10,
            "end_page": 10,
        }]
    }

    normalized, report = sanitize_toc_titles(toc, pages, pagination_path)

    assert normalized == toc
    assert report["status"] == "no_changes"
    assert report["candidates"][0]["action"] == "kept"


def test_legitimate_numeric_title_is_preserved_even_with_matching_page_label(
    tmp_path: Path,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    _write_page(pages, 2, "# Chapter 1 2\n\nBody.\n\n2\n")
    pagination_path = tmp_path / "pagination_map.json"
    build_pagination_map(pages, pagination_path)
    toc = {
        "chapters": [{
            "title": "Chapter 1 2",
            "level": 1,
            "start_page": 2,
            "end_page": 2,
        }]
    }

    normalized, report = sanitize_toc_titles(toc, pages, pagination_path)

    assert normalized["chapters"][0]["title"] == "Chapter 1 2"
    assert report["status"] == "no_changes"
    assert report["candidates"][0]["action"] == "kept"


def test_refine_local_persists_toc_normalization_and_updates_toc_hash(
    tmp_path: Path,
) -> None:
    pages = tmp_path / "pages"
    pages.mkdir()
    _write_page(pages, 10, "# Chapter 1\n\nChapter body.\n\n10\n")
    toc_path = tmp_path / "toc_tree.json"
    toc_path.write_text(
        json.dumps({
            "chapters": [{
                "title": "Chapter 1 10",
                "level": 1,
                "start_page": 10,
                "end_page": 10,
            }]
        }),
        encoding="utf-8",
    )
    from pdf2epub.refine.main import RefinedBreakdown

    RefinedBreakdown(config={}, max_tokens=8000).process_from_toc(
        tmp_path / "input.pdf",
        tmp_path,
        "Book",
    )

    normalized_toc = json.loads(toc_path.read_text(encoding="utf-8"))
    report = json.loads(
        (tmp_path / "toc_title_normalization.json").read_text(encoding="utf-8")
    )
    assert normalized_toc["chapters"][0]["title"] == "Chapter 1"
    assert report["status"] == "applied"
    assert report["source_toc_sha256"] != report["normalized_toc_sha256"]
    progress = json.loads(
        (tmp_path / "ocr_markdown" / "tree_progress.json").read_text(
            encoding="utf-8"
        )
    )
    assert progress["fingerprint"]["toc_sha256"] == report["normalized_toc_sha256"]

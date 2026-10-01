from pathlib import Path

from pdf2epub.utils.ocr_artifacts import (
    clean_ocr_page_artifacts,
    remove_printed_page_number_lines,
)
from pdf2epub.refine.page_merger import PageMerger
from pdf2epub.refine.toc_tree import TOCNode


def test_clean_ocr_page_artifacts_removes_blank_page_placeholder():
    content = (
        "![Blank white page](../images/page_004_img_001.png)"
        "A completely blank white page with no visible content, text, or markings."
    )

    assert clean_ocr_page_artifacts(content) == ""


def test_clean_ocr_page_artifacts_keeps_real_image_and_text():
    content = "![Figure 1](../images/page_004_img_001.png)\nA real figure."

    assert clean_ocr_page_artifacts(content) == content


def test_remove_printed_page_number_lines_removes_edge_labels_only():
    lines = ["XII", "A paragraph with the number 12 in its prose.", "42"]

    assert remove_printed_page_number_lines(lines) == [
        "A paragraph with the number 12 in its prose."
    ]


def test_remove_printed_page_number_lines_removes_synthetic_stamp_but_not_body_number():
    lines = ["PDF Page: 7", "A body paragraph.", "The answer is 7."]

    assert remove_printed_page_number_lines(lines) == lines[1:]


def test_remove_printed_page_number_lines_leaves_mixed_running_title_for_polish():
    lines = ["Preface XII", "A paragraph."]

    assert remove_printed_page_number_lines(lines) == lines


def test_page_merger_removes_repeated_h2_header_and_keeps_blank_page_cleaned(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "## Preface and Acknowledgements\n\nFirst page text.", encoding="utf-8"
    )
    (pages / "page_002.md").write_text(
        "## Preface and Acknowledgements\n\nSecond page text.", encoding="utf-8"
    )
    (pages / "page_003.md").write_text(
        "![A blank page](../images/page_003_img_001.png)\n"
        "No text, lines, or other graphical elements.",
        encoding="utf-8",
    )

    node = TOCNode("Preface", 1, 1, 3)
    result = PageMerger().merge_node_content(node, pages)

    assert result.count("Preface and Acknowledgements") == 1
    assert "Second page text." in result
    assert "blank page" not in result.lower()
    assert "graphical elements" not in result.lower()

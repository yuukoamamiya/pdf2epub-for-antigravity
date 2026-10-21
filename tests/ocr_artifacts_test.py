import json
from pathlib import Path

from pdf2epub.utils.ocr_artifacts import (
    clean_ocr_page_artifacts,
    native_repeated_edge_furniture_index,
    remove_printed_page_number_lines,
    remove_native_page_furniture,
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


def test_native_edge_furniture_removes_doi_and_repeated_header_only_after_first_page():
    page_box = [0, 0, 100, 100]
    sidecars = [
        {
            "source_kind": "native_text",
            "page_number": 1,
            "page_box": page_box,
            "blocks": [
                {"bbox": [0, 2, 100, 6], "markdown_text": "Running header"},
                {"bbox": [0, 92, 100, 96], "markdown_text": "DOI: 10.1234/example-1"},
            ],
        },
        {
            "source_kind": "native_text",
            "page_number": 2,
            "page_box": page_box,
            "blocks": [
                {"bbox": [0, 2, 100, 6], "markdown_text": "Running header"},
            ],
        },
    ]
    repeated = native_repeated_edge_furniture_index(sidecars)

    first, first_removed = remove_native_page_furniture(
        "Running header\n\nBody text.\n\nDOI: 10.1234/example-1",
        sidecars[0],
        repeated_index=repeated,
        page_number=1,
    )
    second, second_removed = remove_native_page_furniture(
        "Running header\n\nSecond body.",
        sidecars[1],
        repeated_index=repeated,
        page_number=2,
    )

    assert "Running header" in first
    assert "DOI: 10.1234/example-1" not in first
    assert "Running header" not in second
    assert "Second body." in second
    assert {item["kind"] for item in first_removed} == {"doi_edge"}
    assert {item["kind"] for item in second_removed} == {"repeated_edge_text"}


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


def test_page_merger_removes_repeated_top_section_header_using_layout_evidence(
    tmp_path: Path,
):
    pages = tmp_path / "pages"
    pages.mkdir()
    for page_number, printed_page, body in (
        (1, 10, "enables commodities to regulate their supply [price] in response."),
        (2, 12, "once if it involves debtor-creditor relationships."),
    ):
        (pages / f"page_{page_number:03d}.md").write_text(
            f"## {printed_page} *The Doctrine of Circulation*\n\n{body}",
            encoding="utf-8",
        )
        (pages / f"page_{page_number:03d}.ocr.json").write_text(
            json.dumps(
                {
                    "page_number": page_number,
                    "backend": "chandra",
                    "page_box": [0, 0, 1123, 1588],
                    "blocks": [
                        {
                            "order": 0,
                            "label": "Section-Header",
                            "bbox": [125, 104, 497, 128],
                            "html": (
                                f"<h2>{printed_page} "
                                "<i>The Doctrine of Circulation</i></h2>"
                            ),
                        },
                        {
                            "order": 1,
                            "label": "Text",
                            "bbox": [127, 149, 873, 188],
                            "html": f"<p>{body}</p>",
                        },
                    ],
                }
            ),
            encoding="utf-8",
        )

    result = PageMerger().merge_node_content(TOCNode("Section", 1, 1, 2), pages)

    assert "The Doctrine of Circulation" not in result
    assert "enables commodities to regulate their supply [price]" in result
    assert "debtor-creditor relationships" in result


def test_page_merger_keeps_nonrepeated_section_heading_and_ignores_body_position(
    tmp_path: Path,
):
    pages = tmp_path / "pages"
    pages.mkdir()
    for page_number, y0 in ((1, 104), (2, 331)):
        (pages / f"page_{page_number:03d}.md").write_text(
            "## 10 *The Doctrine of Circulation*\n\nBody text.",
            encoding="utf-8",
        )
        (pages / f"page_{page_number:03d}.ocr.json").write_text(
            json.dumps(
                {
                    "page_number": page_number,
                    "backend": "chandra",
                    "page_box": [0, 0, 1123, 1588],
                    "blocks": [
                        {
                            "label": "Section-Header",
                            "bbox": [125, y0, 497, y0 + 24],
                            "html": "<h2>10 <i>The Doctrine of Circulation</i></h2>",
                        }
                    ],
                }
            ),
            encoding="utf-8",
        )

    result = PageMerger().merge_node_content(TOCNode("Section", 1, 1, 2), pages)

    assert result.count("The Doctrine of Circulation") == 2


def test_page_merger_applies_native_sidecar_furniture_cleanup(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    page_box = [0, 0, 100, 100]
    (pages / "page_001.md").write_text(
        "Running header\n\nFirst body.", encoding="utf-8"
    )
    (pages / "page_002.md").write_text(
        "Running header\n\nSecond body.\n\nDOI: 10.1234/example-1",
        encoding="utf-8",
    )
    for page_number, blocks in {
        1: [
            {"bbox": [0, 2, 100, 6], "markdown_text": "Running header"},
            {"bbox": [10, 30, 90, 40], "markdown_text": "First body."},
        ],
        2: [
            {"bbox": [0, 2, 100, 6], "markdown_text": "Running header"},
            {"bbox": [10, 30, 90, 40], "markdown_text": "Second body."},
            {"bbox": [0, 92, 100, 96], "markdown_text": "DOI: 10.1234/example-1"},
        ],
    }.items():
        (pages / f"page_{page_number:03d}.ocr.json").write_text(
            json.dumps(
                {
                    "source_kind": "native_text",
                    "page_number": page_number,
                    "page_box": page_box,
                    "blocks": blocks,
                }
            ),
            encoding="utf-8",
        )

    result = PageMerger().merge_node_content(TOCNode("Section", 1, 1, 2), pages)

    assert result.count("Running header") == 1
    assert "First body." in result
    assert "Second body." in result
    assert "DOI: 10.1234/example-1" not in result


def test_page_merger_uses_absolute_start_and_end_lines_on_one_page(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "line 1\nline 2\nline 3\nline 4\nline 5", encoding="utf-8"
    )

    node = TOCNode(
        "Section",
        1,
        1,
        1,
        boundary_info={"start_line": 2, "end_line": 5},
    )
    result = PageMerger().merge_node_content(node, pages)

    assert result == "line 2\nline 3\nline 4"


def test_page_merger_keeps_prefix_before_next_midpage_section(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Previous page.", encoding="utf-8")
    (pages / "page_002.md").write_text(
        "The previous sentence continues.\nIt ends here.\n# Next section\nNext text.",
        encoding="utf-8",
    )

    previous = TOCNode("Previous", 1, 1, 2)
    next_node = TOCNode(
        "Next",
        1,
        2,
        2,
        boundary_info={"start_line": 3},
    )
    result = PageMerger().merge_node_content(previous, pages, next_node)

    assert "The previous sentence continues." in result
    assert "It ends here." in result
    assert "# Next section" not in result


def test_page_merger_joins_an_unfinished_prose_block_across_pages(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "The concept begins at the end of the page and continues",
        encoding="utf-8",
    )
    (pages / "page_002.md").write_text(
        "on the next page.",
        encoding="utf-8",
    )

    node = TOCNode("Section", 1, 1, 2)
    result = PageMerger().merge_node_content(node, pages)

    assert result == (
        "The concept begins at the end of the page and continues on the next page."
    )


def test_page_merger_joins_numbered_paragraph_continuing_across_pages(
    tmp_path: Path,
):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "18. This numbered paragraph ends with a continuation,",
        encoding="utf-8",
    )
    (pages / "page_002.md").write_text(
        "which continues on the next page.", encoding="utf-8"
    )

    node = TOCNode("Section", 1, 1, 2)
    result = PageMerger().merge_node_content(node, pages)

    assert result == (
        "18. This numbered paragraph ends with a continuation, "
        "which continues on the next page."
    )


def test_page_merger_keeps_completed_page_paragraph_boundary(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("The first paragraph ends.", encoding="utf-8")
    (pages / "page_002.md").write_text("The next paragraph begins.", encoding="utf-8")

    node = TOCNode("Section", 1, 1, 2)
    result = PageMerger().merge_node_content(node, pages)

    assert result == "The first paragraph ends.\n\nThe next paragraph begins."


def test_page_merger_does_not_join_a_page_starting_with_a_footnote_or_heading(
    tmp_path: Path,
):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "The body sentence is incomplete at the page edge",
        encoding="utf-8",
    )
    (pages / "page_002.md").write_text(
        "# New section\n\n[^1]: A footnote definition.",
        encoding="utf-8",
    )

    node = TOCNode("Section", 1, 1, 2)
    result = PageMerger().merge_node_content(node, pages)

    assert result == (
        "The body sentence is incomplete at the page edge\n\n"
        "# New section\n\n[^1]: A footnote definition."
    )

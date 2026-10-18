import pytest

from pdf2epub.epub.footnotes import FootnoteManager, display_footnote_key
from pdf2epub.epub.footnotes import validate_footnote_graph
from pdf2epub.markdown_to_html import convert_markdown_to_html
from pdf2epub.refine.endnote_normalization import (
    EndnoteNormalizationError,
    normalize_endnotes,
    validate_normalized_endnotes,
)
from pdf2epub.refine.endnote_scopes import (
    build_endnote_scope_directory,
    scope_for_line,
)


def _simple_book(
    *,
    body: str,
    notes: str,
    children: list[dict],
    notes_file: str = "chapter_3.md",
    body_file: str = "chapter_2.md",
) -> tuple[dict, dict, dict]:
    toc = {
        "chapters": [
            {"title": "Preface", "start_page": 1, "end_page": 1},
            {
                "title": "Part 1: Roots",
                "start_page": 2,
                "end_page": 10,
                "children": children,
            },
            {
                "title": "Notes",
                "start_page": 11,
                "end_page": 12,
                "type": "notes",
            },
        ]
    }
    progress = {
        "units": [
            {
                "unit_id": "chapter_1",
                "index_path": [1],
                "title": "Preface",
                "page_range": [1, 1],
                "file": "chapter_1.md",
            },
            {
                "unit_id": "chapter_2",
                "index_path": [2],
                "title": "Part 1: Roots",
                "page_range": [2, 10],
                "file": body_file,
            },
            {
                "unit_id": "chapter_3",
                "index_path": [3],
                "title": "Notes",
                "page_range": [11, 12],
                "file": notes_file,
                "type": "notes",
            },
        ]
    }
    texts = {
        "chapter_1.md": "## Preface\nIntro.\n",
        body_file: body,
        notes_file: notes,
    }
    return toc, progress, texts


def test_scope_directory_splits_body_and_notes_headings() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n\n"
            "## 1. First Chapter\n"
            "Body one <sup>1</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Abbreviations Used in Notes\n"
            "## Preface\n"
            "1. Pref note.\n"
            "## Chapter 1: First Chapter\n"
            "1. First one.\n"
            "2. First two.\n"
            "## Chapter 2: Second Chapter\n"
            "1. Second one.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 5},
            {"title": "2. Second Chapter", "start_page": 6, "end_page": 10},
        ],
    )
    texts["chapter_2.part2.md"] = (
        "Continuation <sup>2</sup>.\n"
        "## 2. Second Chapter\n"
        "Body two <sup>1</sup>.\n"
    )
    progress["units"][1]["part_files"] = [
        "chapter_2.part1.md",
        "chapter_2.part2.md",
    ]
    texts["chapter_2.part1.md"] = texts.pop("chapter_2.md")

    directory = build_endnote_scope_directory(toc, progress, texts)

    assert directory["body_spans"]["chapter_2.part1.md"] == [
        {"start_line": 1, "end_line": 3, "scope_key": "u2"},
        {"start_line": 3, "end_line": 5, "scope_key": "u2c1"},
    ]
    assert directory["body_spans"]["chapter_2.part2.md"] == [
        {"start_line": 1, "end_line": 2, "scope_key": "u2c1"},
        {"start_line": 2, "end_line": 4, "scope_key": "u2c2"},
    ]
    assert directory["notes_spans"]["chapter_3.md"] == [
        {"start_line": 1, "end_line": 3, "scope_key": None},
        {"start_line": 3, "end_line": 5, "scope_key": "u1"},
        {"start_line": 5, "end_line": 8, "scope_key": "u2c1"},
        {"start_line": 8, "end_line": 10, "scope_key": "u2c2"},
    ]
    assert [item["key"] for item in directory["scope_ref_keys"]["u2c1"]] == [
        "1",
        "2",
    ]
    assert [item["key"] for item in directory["scope_ref_keys"]["u2c2"]] == [
        "1"
    ]
    assert any(
        item["heading"] == "## Abbreviations Used in Notes"
        for item in directory["unmatched_notes_headings"]
    )
    assert directory["ambiguous_headings"] == []


def test_restarted_note_numbers_are_scoped_per_chapter() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>1</sup> B<sup>2</sup>.\n"
            "## 2. Second Chapter\n"
            "C<sup>1</sup> D<sup>2</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "1. One.\n"
            "2. Two.\n"
            "## Chapter 2: Second Chapter\n"
            "1. One again.\n"
            "2. Two again.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 5},
            {"title": "2. Second Chapter", "start_page": 6, "end_page": 10},
        ],
    )
    directory = build_endnote_scope_directory(toc, progress, texts)

    normalized, report = normalize_endnotes(
        texts,
        {"chapter_1.md": "", "chapter_2.md": "", "chapter_3.md": "notes"},
        scopes=directory,
    )

    assert normalized["chapter_2.md"] == (
        "# Part 1: Roots\n"
        "## 1. First Chapter\n"
        "A[^fnc-u2c1-1] B[^fnc-u2c1-2].\n"
        "## 2. Second Chapter\n"
        "C[^fnc-u2c2-1] D[^fnc-u2c2-2].\n"
    )
    assert normalized["chapter_3.md"] == (
        "# Notes\n"
        "## Chapter 1: First Chapter\n"
        "[^fnc-u2c1-1]: One.\n"
        "[^fnc-u2c1-2]: Two.\n"
        "## Chapter 2: Second Chapter\n"
        "[^fnc-u2c2-1]: One again.\n"
        "[^fnc-u2c2-2]: Two again.\n"
    )
    assert report["scope_mode"] == "per_chapter"
    assert report["scoped_keys"] == ["1", "2"]
    assert report["duplicate_definition_occurrences"] == []
    assert report["orphan_definition_keys"] == []


def test_double_numbered_notes_use_inner_key_with_body_evidence() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>16</sup> B<sup>17</sup> C<sup>3</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "1. 16. Sixteen.\n"
            "2. 17. Seventeen.\n"
            "3. 2005 was a year.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 10}
        ],
    )
    directory = build_endnote_scope_directory(toc, progress, texts)

    normalized, report = normalize_endnotes(
        texts,
        {"chapter_1.md": "", "chapter_2.md": "", "chapter_3.md": "notes"},
        scopes=directory,
    )

    assert "A[^16] B[^17] C[^3]." in normalized["chapter_2.md"]
    assert "[^16]: Sixteen." in normalized["chapter_3.md"]
    assert "[^17]: Seventeen." in normalized["chapter_3.md"]
    assert "[^3]: 2005 was a year." in normalized["chapter_3.md"]
    assert report["double_numbered_definition_count"] == 2
    assert report["scope_mode"] == "global"


def test_duplicate_definition_in_one_scope_keeps_extra_as_orphan() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>66</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "66. First definition.\n"
            "66. Second definition.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 10}
        ],
    )
    directory = build_endnote_scope_directory(toc, progress, texts)

    normalized, report = normalize_endnotes(
        texts,
        {"chapter_1.md": "", "chapter_2.md": "", "chapter_3.md": "notes"},
        scopes=directory,
    )

    assert "A[^fnc-u2c1-66]." in normalized["chapter_2.md"]
    assert "[^fnc-u2c1-66]: First definition." in normalized["chapter_3.md"]
    assert (
        "[^fnc-u2c1-66-o2]: Second definition."
        in normalized["chapter_3.md"]
    )
    assert report["duplicate_definition_occurrences"] == [
        {
            "scope_key": "u2c1",
            "key": "66",
            "occurrences": [
                {"file": "chapter_3.md", "line": 3},
                {"file": "chapter_3.md", "line": 4},
            ],
            "output_keys": ["fnc-u2c1-66", "fnc-u2c1-66-o2"],
        }
    ]
    assert report["orphan_definition_keys"] == ["fnc-u2c1-66-o2"]
    assert report["orphan_scoped_definitions"] == [
        {
            "scope_key": "u2c1",
            "key": "66",
            "output_key": "fnc-u2c1-66-o2",
            "file": "chapter_3.md",
            "line": 4,
        }
    ]


def test_duplicate_key_without_chapter_heading_fails_closed() -> None:
    toc = {
        "chapters": [
            {"title": "Notes", "start_page": 11, "end_page": 12, "type": "notes"}
        ]
    }
    progress = {
        "units": [
            {
                "unit_id": "chapter_1",
                "index_path": [1],
                "title": "Notes",
                "page_range": [11, 12],
                "file": "chapter_1.md",
                "type": "notes",
            }
        ]
    }
    texts = {"chapter_1.md": "# Notes\n1. First.\n1. Second.\n"}
    directory = build_endnote_scope_directory(toc, progress, texts)

    with pytest.raises(EndnoteNormalizationError, match="no chapter scope"):
        normalize_endnotes(texts, {"chapter_1.md": "notes"}, scopes=directory)


def test_reference_in_scope_without_definition_fails_closed() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>9</sup>.\n"
            "## 2. Second Chapter\n"
            "B<sup>9</sup>.\n"
            "## 3. Third Chapter\n"
            "C<sup>9</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "9. A note.\n"
            "## Chapter 2: Second Chapter\n"
            "9. B note.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 4},
            {"title": "2. Second Chapter", "start_page": 5, "end_page": 7},
            {"title": "3. Third Chapter", "start_page": 8, "end_page": 10},
        ],
    )
    directory = build_endnote_scope_directory(toc, progress, texts)

    with pytest.raises(
        EndnoteNormalizationError,
        match="referenced in scope u2c3 but has no definition",
    ):
        normalize_endnotes(
            texts,
            {"chapter_1.md": "", "chapter_2.md": "", "chapter_3.md": "notes"},
            scopes=directory,
        )


def _digital_capitalism_book(
    notes: str,
) -> tuple[dict, dict, dict]:
    toc = {
        "chapters": [
            {
                "title": "Introduction: The Enchanted Network",
                "start_page": 1,
                "end_page": 10,
            },
            {
                "title": "1 The Neoliberal Networking Drive",
                "start_page": 11,
                "end_page": 20,
            },
            {
                "title": "2 Going Global: The Neoliberal Project",
                "start_page": 21,
                "end_page": 30,
            },
            {
                "title": "Notes",
                "start_page": 31,
                "end_page": 35,
                "type": "notes",
            },
        ]
    }
    progress = {
        "units": [
            {
                "unit_id": "chapter_1",
                "index_path": [1],
                "title": "Introduction: The Enchanted Network",
                "page_range": [1, 10],
                "file": "chapter_1.md",
            },
            {
                "unit_id": "chapter_2",
                "index_path": [2],
                "title": "1 The Neoliberal Networking Drive",
                "page_range": [11, 20],
                "file": "chapter_2.md",
            },
            {
                "unit_id": "chapter_3",
                "index_path": [3],
                "title": "2 Going Global: The Neoliberal Project",
                "page_range": [21, 30],
                "file": "chapter_3.md",
            },
            {
                "unit_id": "chapter_4",
                "index_path": [4],
                "title": "Notes",
                "page_range": [31, 35],
                "file": "chapter_4.md",
                "type": "notes",
            },
        ]
    }
    texts = {
        "chapter_1.md": (
            "## Introduction: The Enchanted Network\n"
            "Body <sup>1</sup>.\n"
        ),
        "chapter_2.md": (
            "## 1 The Neoliberal Networking Drive\n"
            "Body <sup>1</sup>.\n"
        ),
        "chapter_3.md": (
            "## 2 Going Global: The Neoliberal Project\n"
            "Body <sup>1</sup>.\n"
        ),
        "chapter_4.md": notes,
    }
    return toc, progress, texts


def test_plain_text_notes_headings_map_to_body_scopes() -> None:
    toc, progress, texts = _digital_capitalism_book(
        "Notes\n"
        "\n"
        "Introduction\n"
        "\n"
        "1. Intro note.\n"
        "\n"
        "Chapter 1\n"
        "\n"
        "1. First note.\n"
        "\n"
        "Notes to Chapter 1\n"
        "\n"
        "2. First two.\n"
        "\n"
        "Chapter 2\n"
        "\n"
        "1. Second note.\n"
    )

    directory = build_endnote_scope_directory(toc, progress, texts)

    assert directory["notes_spans"]["chapter_4.md"] == [
        {"start_line": 1, "end_line": 3, "scope_key": None},
        {"start_line": 3, "end_line": 7, "scope_key": "u1"},
        {"start_line": 7, "end_line": 15, "scope_key": "u2"},
        {"start_line": 15, "end_line": 18, "scope_key": "u3"},
    ]
    assert directory["scopes"]["u1"]["notes_files"] == ["chapter_4.md"]
    assert directory["scopes"]["u2"]["notes_files"] == ["chapter_4.md"]
    assert directory["scopes"]["u3"]["notes_files"] == ["chapter_4.md"]
    assert directory["ambiguous_headings"] == []
    assert directory["unmatched_notes_headings"] == []


def test_plain_text_heading_inside_paragraph_is_not_a_boundary() -> None:
    toc, progress, texts = _digital_capitalism_book(
        "Notes\n"
        "\n"
        "Introduction\n"
        "\n"
        "1. Intro note.\n"
        "\n"
        "This sentence mentions\n"
        "Chapter 1\n"
        "in the middle of the paragraph.\n"
        "\n"
        "Chapter 2\n"
        "\n"
        "1. Second note.\n"
    )

    directory = build_endnote_scope_directory(toc, progress, texts)

    assert directory["notes_spans"]["chapter_4.md"] == [
        {"start_line": 1, "end_line": 3, "scope_key": None},
        {"start_line": 3, "end_line": 11, "scope_key": "u1"},
        {"start_line": 11, "end_line": 14, "scope_key": "u3"},
    ]
    assert scope_for_line(directory, "chapter_4.md", 8) == "u1"
    assert directory["ambiguous_headings"] == []


def test_plain_text_heading_matching_two_scopes_is_ambiguous() -> None:
    toc = {
        "chapters": [
            {"title": "1 Alpha", "start_page": 1, "end_page": 5},
            {"title": "1 Beta", "start_page": 6, "end_page": 10},
            {
                "title": "Notes",
                "start_page": 11,
                "end_page": 12,
                "type": "notes",
            },
        ]
    }
    progress = {
        "units": [
            {
                "unit_id": "chapter_1",
                "index_path": [1],
                "title": "1 Alpha",
                "page_range": [1, 5],
                "file": "chapter_1.md",
            },
            {
                "unit_id": "chapter_2",
                "index_path": [2],
                "title": "1 Beta",
                "page_range": [6, 10],
                "file": "chapter_2.md",
            },
            {
                "unit_id": "chapter_3",
                "index_path": [3],
                "title": "Notes",
                "page_range": [11, 12],
                "file": "chapter_3.md",
                "type": "notes",
            },
        ]
    }
    texts = {
        "chapter_1.md": "## 1 Alpha\nA<sup>1</sup>.\n",
        "chapter_2.md": "## 1 Beta\nB<sup>1</sup>.\n",
        "chapter_3.md": (
            "Notes\n"
            "\n"
            "Chapter 1\n"
            "\n"
            "1. Ambiguous note.\n"
        ),
    }

    directory = build_endnote_scope_directory(toc, progress, texts)

    assert directory["notes_spans"]["chapter_3.md"] == [
        {"start_line": 1, "end_line": 6, "scope_key": None},
    ]
    assert directory["ambiguous_headings"] == [
        {
            "file": "chapter_3.md",
            "line": 3,
            "heading": "Chapter 1",
            "candidates": ["u1", "u2"],
        }
    ]


def test_markdown_notes_headings_still_match_body_scopes() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>1</sup>.\n"
            "## 2. Second Chapter\n"
            "B<sup>1</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "1. One.\n"
            "## Chapter 2: Second Chapter\n"
            "1. Two.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 5},
            {"title": "2. Second Chapter", "start_page": 6, "end_page": 10},
        ],
    )

    directory = build_endnote_scope_directory(toc, progress, texts)

    assert directory["notes_spans"]["chapter_3.md"] == [
        {"start_line": 1, "end_line": 2, "scope_key": None},
        {"start_line": 2, "end_line": 4, "scope_key": "u2c1"},
        {"start_line": 4, "end_line": 6, "scope_key": "u2c2"},
    ]
    assert directory["ambiguous_headings"] == []


def test_wrapped_date_line_is_not_a_scoped_definition() -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>1</sup> B<sup>30</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "1. First note text.\n"
            "30 January 2005, continuation line.\n"
            "\n"
            "30 Second note starts after a blank line.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 10}
        ],
    )
    directory = build_endnote_scope_directory(toc, progress, texts)

    normalized, report = normalize_endnotes(
        texts,
        {"chapter_1.md": "", "chapter_2.md": "", "chapter_3.md": "notes"},
        scopes=directory,
    )

    assert (
        "[^30]: Second note starts after a blank line."
        in normalized["chapter_3.md"]
    )
    assert "30 January 2005, continuation line." in normalized["chapter_3.md"]
    assert report["definition_keys"] == ["1", "30"]
    assert report["scoped_keys"] == []
    assert report["duplicate_definition_occurrences"] == []


def test_validate_rejects_raw_numeric_reference_for_scoped_definition() -> None:
    report = validate_normalized_endnotes(
        {
            "chapter_1.md": "Body <sup>1</sup>.\n",
            "chapter_2.md": "[^fnc-u2c1-1]: One.\n",
        },
        {"chapter_1.md": "", "chapter_2.md": "notes"},
    )

    assert report["valid"] is False
    assert any(
        "raw scoped endnote references remain" in error
        for error in report["errors"]
    )


def test_display_footnote_key_hides_internal_scope() -> None:
    assert display_footnote_key("fnc-u5c4-106") == "106"
    assert display_footnote_key("fnc-u5c5-66-o2") == "66"
    assert display_footnote_key("197n67") == "197n67"
    assert display_footnote_key("1") == "1"


def test_scoped_footnotes_render_display_numbers_in_epub(tmp_path) -> None:
    toc, progress, texts = _simple_book(
        body=(
            "# Part 1: Roots\n"
            "## 1. First Chapter\n"
            "A<sup>1</sup>.\n"
            "## 2. Second Chapter\n"
            "C<sup>1</sup>.\n"
        ),
        notes=(
            "# Notes\n"
            "## Chapter 1: First Chapter\n"
            "1. One.\n"
            "## Chapter 2: Second Chapter\n"
            "1. One again.\n"
        ),
        children=[
            {"title": "1. First Chapter", "start_page": 2, "end_page": 5},
            {"title": "2. Second Chapter", "start_page": 6, "end_page": 10},
        ],
    )
    directory = build_endnote_scope_directory(toc, progress, texts)
    normalized, _ = normalize_endnotes(
        texts,
        {"chapter_1.md": "", "chapter_2.md": "", "chapter_3.md": "notes"},
        scopes=directory,
    )
    body_path = tmp_path / "chapter_2.md"
    notes_path = tmp_path / "chapter_3.md"
    body_path.write_text(normalized["chapter_2.md"], encoding="utf-8")
    notes_path.write_text(normalized["chapter_3.md"], encoding="utf-8")
    structure = [
        {"unit_id": "chapter_2", "file_path": body_path, "part_files": [body_path]},
        {
            "unit_id": "chapter_3",
            "file_path": notes_path,
            "part_files": [notes_path],
            "type": "notes",
        },
    ]

    manager = FootnoteManager(tmp_path, auto_global=True, epub_structure=structure)
    body_html = convert_markdown_to_html(
        normalized["chapter_2.md"],
        "Book",
        standalone=False,
        footnote_manager=manager,
        source_chapter=body_path.stem,
    )
    notes_html = convert_markdown_to_html(
        normalized["chapter_3.md"],
        "Book",
        standalone=False,
        footnote_manager=manager,
        source_chapter=notes_path.stem,
    )

    assert "[fnc-" not in body_html
    assert "[fnc-" not in notes_html
    assert ">[1]</a>" in body_html
    assert ">[1]:</strong>" in notes_html
    report = validate_footnote_graph(
        {"chapter_2.html": body_html, "chapter_3.html": notes_html}
    )
    assert report["forward_hrefs"] == 2
    assert report["unlinked_sup_count"] == 0

import pytest

from pdf2epub.build_epub import validate_centralized_endnotes_for_build
from pdf2epub.epub.footnotes import FootnoteManager, validate_footnote_graph
from pdf2epub.footnote_normalization import validate_polish_footnote_normalization
from pdf2epub.markdown_to_html import convert_markdown_to_html
from pdf2epub.refine.endnote_normalization import (
    EndnoteNormalizationError,
    normalize_endnotes,
)


def test_typed_notes_normalize_numbered_html_and_unicode_references() -> None:
    source = {
        "chapter_1.md": "Body <sup class=\"note\">1</sup> and ², but x<sup>99</sup> stays.\n",
        "chapter_12.md": "1. First endnote.\n2 Second endnote.\n",
    }

    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "chapter_12.md": "notes"},
    )

    assert normalized["chapter_1.md"] == (
        "Body [^1] and [^2], but x<sup>99</sup> stays.\n"
    )
    assert normalized["chapter_12.md"] == (
        "[^1]: First endnote.\n[^2]: Second endnote.\n"
    )
    assert report["converted_reference_count"] == 2
    assert report["orphan_definition_keys"] == []


def test_typed_notes_can_be_validated_without_a_notes_heading() -> None:
    source_body = "Body <sup>1</sup>.\n"
    source_notes = "1. Endnote text.\n"
    normalized, _ = normalize_endnotes(
        {"chapter_1.md": source_body, "chapter_12.md": source_notes},
        {"chapter_1.md": "", "chapter_12.md": "notes"},
    )

    assert validate_polish_footnote_normalization(
        source_body,
        normalized["chapter_1.md"],
        expected_endnote_keys={"1"},
    ) == []
    assert validate_polish_footnote_normalization(
        source_notes,
        normalized["chapter_12.md"],
        file_role="notes",
        expected_endnote_keys={"1"},
    ) == []


def test_duplicate_typed_endnote_keys_block_normalization() -> None:
    with pytest.raises(EndnoteNormalizationError, match="duplicate centralized endnote key 1"):
        normalize_endnotes(
            {
                "chapter_1.md": "Body [^1].\n",
                "chapter_12.part1.md": "1. First.\n",
                "chapter_12.part2.md": "1. Duplicate.\n",
            },
            {
                "chapter_1.md": "",
                "chapter_12.part1.md": "notes",
                "chapter_12.part2.md": "notes",
            },
        )


def test_unconverted_typed_endnotes_are_rejected() -> None:
    errors = validate_polish_footnote_normalization(
        "Body <sup>1</sup>.\n",
        "Body <sup>1</sup>.\n",
        expected_endnote_keys={"1"},
    )

    assert any("migration mismatch" in error for error in errors)


def test_build_gate_rejects_raw_typed_endnotes(tmp_path):
    body = tmp_path / "chapter_1.md"
    notes = tmp_path / "chapter_12.md"
    body.write_text("Body <sup>1</sup>.\n", encoding="utf-8")
    notes.write_text("1. Endnote.\n", encoding="utf-8")
    structure = [
        {"file_path": body, "part_files": [body], "type": ""},
        {"file_path": notes, "part_files": [notes], "type": "notes"},
    ]

    report = validate_centralized_endnotes_for_build(structure)

    assert report["valid"] is False
    assert any("raw centralized endnote" in error for error in report["errors"])


def test_build_gate_accepts_normalized_typed_endnotes(tmp_path):
    body = tmp_path / "chapter_1.md"
    notes = tmp_path / "chapter_12.md"
    body.write_text("Body [^1].\n", encoding="utf-8")
    notes.write_text("[^1]: Endnote.\n", encoding="utf-8")
    structure = [
        {"file_path": body, "part_files": [body], "type": ""},
        {"file_path": notes, "part_files": [notes], "type": "notes"},
    ]

    report = validate_centralized_endnotes_for_build(structure)

    assert report == {"valid": True, "definition_keys": ["1"], "errors": []}


def test_normalized_typed_endnotes_render_as_cross_file_epub_links(tmp_path):
    body = tmp_path / "chapter_1.md"
    notes = tmp_path / "chapter_12.md"
    normalized, _ = normalize_endnotes(
        {
            body.name: "Body <sup>1</sup>.\n",
            notes.name: "1. Endnote text.\n",
        },
        {body.name: "", notes.name: "notes"},
    )
    body.write_text(normalized[body.name], encoding="utf-8")
    notes.write_text(normalized[notes.name], encoding="utf-8")
    structure = [
        {"unit_id": "chapter_1", "file_path": body, "part_files": [body]},
        {
            "unit_id": "chapter_12",
            "file_path": notes,
            "part_files": [notes],
            "type": "notes",
        },
    ]

    manager = FootnoteManager(tmp_path, auto_global=True, epub_structure=structure)
    html = {
        "chapter_1.html": convert_markdown_to_html(
            normalized[body.name],
            "Book",
            standalone=False,
            footnote_manager=manager,
            source_chapter=body.stem,
        ),
        "chapter_12.html": convert_markdown_to_html(
            normalized[notes.name],
            "Book",
            standalone=False,
            footnote_manager=manager,
            source_chapter=notes.stem,
        ),
    }

    report = validate_footnote_graph(html)

    assert report["forward_hrefs"] == 1
    assert report["unlinked_sup_count"] == 0

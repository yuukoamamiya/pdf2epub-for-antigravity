import pytest

from pdf2epub.build_epub import validate_centralized_endnotes_for_build
from pdf2epub.epub.footnotes import FootnoteManager, validate_footnote_graph
from pdf2epub.footnote_normalization import (
    endnote_definition_keys,
    validate_polish_footnote_normalization,
)
from pdf2epub.markdown_to_html import convert_markdown_to_html
from pdf2epub.refine.endnote_normalization import (
    EndnoteNormalizationError,
    normalize_endnotes,
    validate_normalized_endnotes,
)


def test_list_marked_notes_definition_is_normalized_locally() -> None:
    source = {
        "chapter_1.md": "Body <sup>145</sup>.\n",
        "chapter_12.md": "- 145. Katz, Personal Influence, 16.\n",
    }
    roles = {"chapter_1.md": "", "chapter_12.md": "notes"}

    normalized, report = normalize_endnotes(source, roles)

    assert normalized["chapter_12.md"] == (
        "[^145]: Katz, Personal Influence, 16.\n"
    )
    assert "- 145." not in normalized["chapter_12.md"]
    assert report["converted_reference_count"] == 1
    assert validate_normalized_endnotes(normalized, roles) == {
        "valid": True,
        "definition_keys": ["145"],
        "errors": [],
    }
    assert endnote_definition_keys(
        source["chapter_12.md"], file_role="notes"
    ) == ["145"]


def test_numbered_block_pipeline_matches_golden_output() -> None:
    """Pin the shared numbered-block loader against a golden normalization."""
    source = {
        "chapter_1.md": "Body <sup>1</sup>, <sup>2</sup> and <sup>7</sup>.\n",
        "notes.md": (
            "1. First note.\n"
            "continued line one\n"
            "continued line two\n"
            "\n"
            "2) Second note.\n"
            "\n"
            "<sup>7</sup> Html note.\n"
        ),
    }

    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "notes.md": "notes"},
    )

    assert normalized == {
        "chapter_1.md": "Body [^1], [^2] and [^7].\n",
        "notes.md": (
            "[^1]: First note.\n"
            "    continued line one\n"
            "    continued line two\n"
            "\n"
            "[^2]: Second note.\n"
            "\n"
            "[^7]: Html note.\n"
        ),
    }
    assert report["definition_keys"] == ["1", "2", "7"]
    assert report["definition_count"] == 3
    assert report["normalized_definition_count"] == 3
    assert report["converted_reference_count"] == 3
    assert report["reference_counts"] == {"1": 1, "2": 1, "7": 1}
    assert report["orphan_definition_keys"] == []
    assert report["scope_mode"] == "global"


def test_typed_notes_normalize_numbered_html_and_unicode_references() -> None:
    source = {
        "chapter_1.md": "Body <sup class=\"note\">1</sup> and ², but x<sup>99</sup> stays.\n",
        "chapter_12.md": "1. First endnote.\n\n2 Second endnote.\n",
    }

    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "chapter_12.md": "notes"},
    )

    assert normalized["chapter_1.md"] == (
        "Body [^1] and [^2], but x<sup>99</sup> stays.\n"
    )
    assert normalized["chapter_12.md"] == (
        "[^1]: First endnote.\n\n[^2]: Second endnote.\n"
    )
    assert report["converted_reference_count"] == 2
    assert report["orphan_definition_keys"] == []


def test_wrapped_date_line_is_not_a_new_endnote_definition() -> None:
    source = {
        "chapter_1.md": "Body <sup>1</sup> and <sup>30</sup>.\n",
        "chapter_12.md": (
            "1. First note text.\n"
            "30 January 2005, continuation line.\n"
            "\n"
            "30 Second note starts after a blank line.\n"
        ),
    }

    normalized, report = normalize_endnotes(
        source,
        {"chapter_1.md": "", "chapter_12.md": "notes"},
    )

    assert (
        "[^30]: Second note starts after a blank line."
        in normalized["chapter_12.md"]
    )
    assert "30 January 2005, continuation line." in normalized["chapter_12.md"]
    assert report["definition_keys"] == ["1", "30"]
    assert report["duplicate_definition_occurrences"] == []


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


def test_local_definition_key_does_not_double_count_its_heading_reference():
    source = "# Index<sup>1</sup>\n\n[^1]: Note: original pagination.\n"
    target = source.replace("<sup>1</sup>", "[^1]")

    assert validate_polish_footnote_normalization(
        source, target, expected_endnote_keys={"1"}
    ) == []


def test_references_after_notes_and_numeric_chapter_heading_are_counted():
    text = (
        "Body [^1].\n\n## Notes\n[^1]: First.\n\n"
        "## Chapter 2\nMore [^2].\n\n[^2]: Second.\n"
    )

    assert validate_polish_footnote_normalization(text, text) == []


def test_mixed_reference_syntaxes_are_counted_per_source_occurrence():
    source = "A [^1] and <sup>1</sup>.\n"
    target = "A [^1] and [^1].\n"

    assert validate_polish_footnote_normalization(
        source, target, expected_endnote_keys={"1"}
    ) == []


def test_unrelated_math_superscript_is_preserved_when_footnotes_migrate():
    source = "Body <sup>1</sup> and x<sup>99</sup>.\n\n## Notes\n1. Endnote.\n"
    target = "Body [^1] and x<sup>99</sup>.\n\n## Notes\n[^1]: Endnote.\n"

    assert validate_polish_footnote_normalization(
        source, target, expected_endnote_keys={"1"}
    ) == []


def test_mixed_local_and_notes_definitions_keep_all_definition_keys():
    source = (
        "[^9]: Local definition.\n\n## Notes\n"
        "1. Central note.\n[^2]: Another local note definition.\n"
    )
    target = (
        "[^9]: Local definition.\n\n## Notes\n"
        "[^1]: Central note.\n[^2]: Another local note definition.\n"
    )

    assert validate_polish_footnote_normalization(source, target) == []


def test_local_markdown_definition_does_not_activate_unicode_math_superscript():
    source = "Body<sup>2</sup>, x².\n\n[^2]: Note.\n"
    target = "Body[^2], x².\n\n[^2]: Note.\n"

    assert validate_polish_footnote_normalization(source, target) == []


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


def test_mixed_page_footnotes_and_embedded_chapter_notes_normalize_without_collision() -> None:
    source = {
        "ch1.md": (
            "Body with page footnote[^fnm-star-1] and endnote<sup>1</sup>.\n\n"
            "## Note\n\n"
            "<sup>1</sup> First chapter endnote.\n"
            "continued line.\n\n"
            "[^fnm-star-1]: Page footnote moved by footnote-apply.\n"
        ),
        "ch2.md": (
            "Second chapter endnote<sup>1</sup> and unique<sup>2</sup>.\n\n"
            "## Notes\n\n"
            "1. Second chapter endnote one.\n\n"
            "2. Second chapter endnote two.\n"
        ),
    }
    roles = {"ch1.md": "chapter", "ch2.md": "chapter"}

    normalized, report = normalize_endnotes(source, roles)

    assert report["status"] == "validated"
    assert report["valid"] is True
    assert report["embedded_note_files"] == ["ch1.md", "ch2.md"]
    assert "[^fnm-star-1]: Page footnote moved by footnote-apply." in normalized["ch1.md"]
    assert "[^fnc-ch1-1]" in normalized["ch1.md"]
    assert "[^fnc-ch1-1]: First chapter endnote.\n    continued line." in normalized["ch1.md"]
    assert "[^fnc-ch2-1]" in normalized["ch2.md"]
    assert "[^fnc-ch2-1]: Second chapter endnote one." in normalized["ch2.md"]
    assert "[^2]" in normalized["ch2.md"]
    assert "[^2]: Second chapter endnote two." in normalized["ch2.md"]


def test_no_notes_role_and_no_embedded_notes_stays_not_applicable() -> None:
    source = {"ch1.md": "Ordinary chapter text<sup>1</sup> without Notes heading.\n"}
    normalized, report = normalize_endnotes(source, {"ch1.md": "chapter"})
    assert report["status"] == "not_applicable"
    assert normalized == source


def test_embedded_chapter_note_colliding_with_existing_markdown_def_is_scoped() -> None:
    source = {
        "ch1.md": (
            "Page footnote[^1] and embedded endnote<sup>1</sup>.\n\n"
            "## Notes\n\n"
            "<sup>1</sup> Embedded note one.\n\n"
            "[^1]: Existing page footnote one.\n"
        ),
    }
    normalized, report = normalize_endnotes(source, {"ch1.md": "chapter"})
    assert report["status"] == "validated"
    assert "[^1]: Existing page footnote one." in normalized["ch1.md"]
    assert "[^fnc-ch1-1]: Embedded note one." in normalized["ch1.md"]
    assert "Page footnote[^1] and embedded endnote[^fnc-ch1-1]." in normalized["ch1.md"]


def test_cross_part_embedded_chapter_notes_resolve_across_split_files() -> None:
    source = {
        "ch1.part1.md": "Body opening with first reference<sup>1</sup>.\n",
        "ch1.part2.md": (
            "Continuation prose.\n\n"
            "## Notes\n\n"
            "1. Cross-part note defined in part two.\n"
        ),
    }
    normalized, report = normalize_endnotes(
        source, {"ch1.part1.md": "chapter", "ch1.part2.md": "chapter"}
    )

    assert report["status"] == "validated"
    assert report["valid"] is True
    assert "Body opening with first reference[^1]." in normalized["ch1.part1.md"]
    assert "[^1]: Cross-part note defined in part two." in normalized["ch1.part2.md"]
    assert report["orphan_definition_keys"] == []

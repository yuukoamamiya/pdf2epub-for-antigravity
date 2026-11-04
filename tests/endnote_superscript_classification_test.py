from pdf2epub.footnote_normalization import (
    _is_math_or_formula_superscript,
    validate_polish_footnote_normalization,
)
from pdf2epub.refine.endnote_normalization import (
    normalize_endnotes,
    validate_normalized_endnotes,
)


def test_bare_year_superscript_is_not_silently_ignored_by_polish_validation():
    source = "It ended in 1965<sup>2</sup>.\n\n[^2]: A note.\n"
    target = "It ended in 1965.\n\n[^2]: A note.\n"

    errors = validate_polish_footnote_normalization(source, target)

    assert any("reference migration mismatch" in error for error in errors)
    assert not any("preserved superscript mismatch" in error for error in errors)


def test_year_superscripts_require_explicit_citation_evidence():
    prose = "In 1965<sup>2</sup>, the event ended."
    unrelated_page_cue = "In 1965<sup>2</sup>. See p. 3 for details."
    citation = "Citations: Königsberg 1856<sup>2</sup>, p. 444."
    prefix_citation = "Citations: <sup>2</sup>1961, pp. 2-3."
    cross_line = "Caption: In 1965<sup>2</sup>.\nSee p. 444."

    assert not _is_math_or_formula_superscript(
        prose, prose.index("<sup>"), prose.index("</sup>") + len("</sup>")
    )
    assert not _is_math_or_formula_superscript(
        unrelated_page_cue,
        unrelated_page_cue.index("<sup>"),
        unrelated_page_cue.index("</sup>") + len("</sup>"),
    )
    for text in (citation, prefix_citation):
        start = text.index("<sup>")
        end = text.index("</sup>") + len("</sup>")
        assert _is_math_or_formula_superscript(text, start, end)

    start = cross_line.index("<sup>")
    end = cross_line.index("</sup>") + len("</sup>")
    assert not _is_math_or_formula_superscript(cross_line, start, end)

    assert _is_math_or_formula_superscript("x<sup>2</sup>", 1, 14, key="2")


def test_dropped_edition_marker_fails_even_without_footnote_definitions():
    source = "Citations: 1856<sup>2</sup>, p. 444."
    errors = validate_polish_footnote_normalization(source, "Citations: 1856, p. 444.")

    assert any("preserved superscript mismatch" in error for error in errors)


def test_explicit_citation_list_preserves_editions_when_keys_collide_with_notes():
    source = "[^1]: Citations: 1856<sup>2</sup>, 1972<sup>3</sup>.\n"
    errors = validate_polish_footnote_normalization(
        source,
        source,
        file_role="notes",
        expected_endnote_keys={"1", "2", "3"},
    )

    assert errors == []


def test_citation_list_marker_loss_or_number_change_fails_with_colliding_keys():
    source = "[^1]: Citations: 1856<sup>2</sup>, 1972<sup>3</sup>.\n"
    for target in (
        "[^1]: Citations: 1856, 1972.\n",
        "[^1]: Citations: 1856<sup>3</sup>, 1972<sup>2</sup>.\n",
    ):
        errors = validate_polish_footnote_normalization(
            source,
            target,
            file_role="notes",
            expected_endnote_keys={"1", "2", "3"},
        )
        assert any("preserved superscript mismatch" in error for error in errors)


def test_nearby_prose_superscript_is_not_exempted_by_a_citation_list():
    line = "[^1]: Citations: 1856<sup>2</sup>, 1972<sup>3</sup>. In 1965<sup>2</sup> it ended."
    start = line.rindex("<sup>")
    end = line.index("</sup>", start) + len("</sup>")

    assert not _is_math_or_formula_superscript(line, start, end, key="2")
    errors = validate_polish_footnote_normalization(
        line,
        line,
        file_role="notes",
        expected_endnote_keys={"1", "2", "3"},
    )
    assert any("reference migration mismatch" in error for error in errors)


def test_notes_validation_accepts_preserved_citation_editions_but_not_lost_year_ref():
    source = "1. See 1965<sup>2</sup>.\n\n2. Citations: 1856<sup>2</sup>, p. 444.\n"
    target = "[^1]: See 1965[^2].\n\n[^2]: Citations: 1856<sup>2</sup>, p. 444.\n"

    assert validate_polish_footnote_normalization(
        source, target, file_role="notes", expected_endnote_keys={"1", "2"}
    ) == []

    missing = "[^1]: See 1965.\n\n[^2]: Citations: 1856<sup>2</sup>, p. 444.\n"
    errors = validate_polish_footnote_normalization(
        source, missing, file_role="notes", expected_endnote_keys={"1", "2"}
    )
    assert any("reference migration mismatch" in e for e in errors)


def test_normalization_uses_current_line_for_year_and_citation_classification():
    source = {
        "body.md": (
            "Ended in 1965<sup>2</sup>.\n"
            "Citation: 1856<sup>2</sup>, p. 444.\n"
            "```html\n"
            "x<sup>2</sup>\n"
            "```\n"
            "x<sup>2</sup>\n"
        ),
        "notes.md": "2. Note.\n",
    }

    normalized, report = normalize_endnotes(
        source, {"body.md": "", "notes.md": "notes"}
    )

    assert "Ended in 1965[^2]." in normalized["body.md"]
    assert "Citation: 1856<sup>2</sup>, p. 444." in normalized["body.md"]
    assert "```html\nx<sup>2</sup>\n```" in normalized["body.md"]
    assert "x<sup>2</sup>" in normalized["body.md"].split("```", 2)[-1]
    assert report["converted_reference_count"] == 1
    assert validate_normalized_endnotes(
        normalized, {"body.md": "", "notes.md": "notes"}
    )["valid"]


def test_unconverted_reference_inside_notes_is_rejected_by_polish_validation():
    source_notes = "1. See 1965<sup>2</sup>.\n\n2. Another note.\n"
    normalized, _ = normalize_endnotes(
        {"notes.md": source_notes}, {"notes.md": "notes"}
    )

    errors = validate_polish_footnote_normalization(
        source_notes,
        normalized["notes.md"],
        file_role="notes",
        expected_endnote_keys={"1", "2"},
    )

    assert any("reference migration mismatch" in error for error in errors)

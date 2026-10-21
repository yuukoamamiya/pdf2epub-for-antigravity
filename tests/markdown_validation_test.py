from pdf2epub.markdown_validation import (
    detect_polish_page_furniture,
    polish_content_integrity_check,
    polish_structure_audit,
)


NOTE_COUNT = 150
NOTE_TEXT = "Katz, Personal Influence, 16."


def _source_book(note_count: int = NOTE_COUNT) -> str:
    references = " ".join(f"<sup>{number}</sup>" for number in range(1, note_count + 1))
    notes = "\n\n".join(
        f"- {number}. {NOTE_TEXT}" for number in range(1, note_count + 1)
    )
    return f"Body references {references}.\n\n{notes}\n"


def _normalized_book(note_count: int = NOTE_COUNT) -> str:
    references = " ".join(f"<sup>{number}</sup>" for number in range(1, note_count + 1))
    notes = "\n\n".join(
        f"[^{number}]: {NOTE_TEXT}" for number in range(1, note_count + 1)
    )
    return f"Body references {references}.\n\n{notes}\n"


def test_polish_integrity_allows_massively_normalized_list_endnotes():
    report = polish_content_integrity_check(
        _source_book(), _normalized_book(), file_role="notes"
    )

    assert report["valid"] is True, report["errors"]
    assert report["missing_numeric_markers"] == []
    assert report["duplicated_prose_blocks"] == []


def test_polish_integrity_list_note_tolerance_is_scoped_to_notes_role():
    source = "\n\n".join(f"+ {number}. {NOTE_TEXT}" for number in range(1, 11))
    target = "\n\n".join(
        f"[^{number}]: {NOTE_TEXT}" for number in range(1, 11)
    )

    strict = polish_content_integrity_check(source, target)
    notes = polish_content_integrity_check(source, target, file_role="notes")

    assert notes["valid"] is True, notes["errors"]
    assert strict["valid"] is False
    assert any("content integrity loss" in error for error in strict["errors"])


def test_polish_integrity_rejects_dropped_body_list_prefix_outside_notes():
    source = (
        "Introductory sentence with enough words to keep the body stable.\n\n"
        "- 1. First ordinary list item with substantive text.\n"
    )
    target = (
        "Introductory sentence with enough words to keep the body stable.\n\n"
        "First ordinary list item with substantive text.\n"
    )

    report = polish_content_integrity_check(source, target)

    assert report["valid"] is False
    assert "1" in report["missing_numeric_markers"]


def test_polish_integrity_rejects_dropped_body_list_number_outside_notes():
    source = (
        "Introductory sentence with enough words to keep the body stable.\n\n"
        "- 1. First ordinary list item with substantive text.\n"
    )
    target = (
        "Introductory sentence with enough words to keep the body stable.\n\n"
        "- First ordinary list item with substantive text.\n"
    )

    report = polish_content_integrity_check(source, target)

    assert report["valid"] is False
    assert "1" in report["missing_numeric_markers"]


def test_polish_integrity_still_rejects_removed_body_prose():
    paragraph = (
        "This ordinary paragraph belongs to the book body and must survive the "
        "polish normalization pass even when every endnote is rewritten."
    )
    source = (
        f"Body references <sup>1</sup>.\n\n{paragraph}\n\n"
        "- 1. Katz, Personal Influence, 16.\n"
    )
    target = (
        "Body references <sup>1</sup>.\n\n"
        "[^1]: Katz, Personal Influence, 16.\n"
    )

    report = polish_content_integrity_check(source, target, file_role="notes")

    assert report["valid"] is False
    assert any("content integrity loss" in error for error in report["errors"])


def test_polish_integrity_still_rejects_duplicated_body_prose():
    paragraph = (
        "This ordinary paragraph appears once in the source and copying it a "
        "second time under another heading must remain a blocking signal."
    )
    source = (
        f"{paragraph}\n\nBody references <sup>1</sup>.\n\n"
        "- 1. Katz, Personal Influence, 16.\n"
    )
    target = (
        f"{paragraph}\n\n{paragraph}\n\nBody references <sup>1</sup>.\n\n"
        "[^1]: Katz, Personal Influence, 16.\n"
    )

    report = polish_content_integrity_check(source, target, file_role="notes")

    assert report["valid"] is False
    assert report["duplicated_prose_blocks"]
    assert any("duplication" in error for error in report["errors"])


def test_polish_structure_audit_flags_body_text_appended_to_heading():
    source = (
        "A sufficiently long body paragraph explains the previous idea.\n\n"
        "## 10 *The Doctrine of Circulation*\n\n"
        "The following paragraph begins here.\n"
    )
    target = (
        "A sufficiently long body paragraph explains the previous idea.\n\n"
        "## *The Doctrine of Circulation* [price] in response to prevailing social demand.\n\n"
        "The following paragraph begins here.\n"
    )

    audit = polish_structure_audit(source, target)

    assert audit["status"] == "review"
    assert audit["heading_content_appends"]
    assert "body prose may have been appended" in audit["reason"]


def test_registered_part_headings_are_exempt_from_page_furniture_gate():
    text = "Body text.\n\n# PART I\n\n## PART II\n\n## PART III\n"

    exempt = detect_polish_page_furniture(
        text,
        protected_toc_titles=["PART I", "PART II", "PART III"],
    )
    unprotected = detect_polish_page_furniture(text)

    assert exempt == []
    assert {item["kind"] for item in unprotected} == {
        "running_title_plus_page_label"
    }


def test_registered_part_heading_matches_toc_title_with_subtitle():
    text = "Body text.\n\n# PART I\n"

    exempt = detect_polish_page_furniture(
        text,
        protected_toc_titles=["PART I: The Roots of Informationalized Capitalism"],
    )

    assert exempt == []


def test_plain_text_part_heading_is_not_exempted_by_toc_title():
    text = "Body text.\n\nPART I\n"

    findings = detect_polish_page_furniture(
        text,
        protected_toc_titles=["PART I"],
    )

    assert [item["kind"] for item in findings] == [
        "running_title_plus_page_label"
    ]


def test_markdown_part_heading_is_not_exempted_when_unregistered():
    text = "Body text.\n\n# PART I\n"

    findings = detect_polish_page_furniture(text)

    assert [item["kind"] for item in findings] == [
        "running_title_plus_page_label"
    ]

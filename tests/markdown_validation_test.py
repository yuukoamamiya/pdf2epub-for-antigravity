from pdf2epub.markdown_validation import polish_content_integrity_check


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
    report = polish_content_integrity_check(_source_book(), _normalized_book())

    assert report["valid"] is True, report["errors"]
    assert report["missing_numeric_markers"] == []
    assert report["duplicated_prose_blocks"] == []


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

    report = polish_content_integrity_check(source, target)

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

    report = polish_content_integrity_check(source, target)

    assert report["valid"] is False
    assert report["duplicated_prose_blocks"]
    assert any("duplication" in error for error in report["errors"])

from __future__ import annotations

from types import SimpleNamespace

import pytest

from pdf2epub.refine.footnote_edit_plan import (
    FootnoteEdit,
    create_edit_plan,
    create_footnote_edit_plan,
    transform_edits,
    validate_edits,
)


def test_create_plan_is_in_memory_and_preserves_untouched_units():
    source = {"chapter.md": "Body <sup>1</sup>.\n", "other.md": "Untouched.\n"}
    edits = {
        "chapter.md": [
            FootnoteEdit(5, 17, "[^1]", "bind-body-marker"),
        ]
    }
    snapshot_hashes = {"chapter.md": "source-digest"}

    plan = create_edit_plan(source, edits)
    bound_plan = create_footnote_edit_plan(
        source,
        SimpleNamespace(
            edits_by_file={"chapter.md": [(5, 17, "[^1]")]},
            notes_by_chapter={},
            resolved=[],
            preserved_roles={},
            errors=[],
        ),
        source_hashes=snapshot_hashes,
    )

    assert plan.valid
    assert plan.transformed_texts == {
        "chapter.md": "Body [^1].\n",
        "other.md": "Untouched.\n",
    }
    assert source["chapter.md"] == "Body <sup>1</sup>.\n"
    assert bound_plan.source_hashes == snapshot_hashes


def test_plan_rejects_overlapping_and_same_offset_edits_before_transform():
    source = {"chapter.md": "abcdef"}
    overlapping = {
        "chapter.md": [
            FootnoteEdit(1, 4, "", "remove-note-source"),
            FootnoteEdit(3, 5, "[^1]", "bind-body-marker"),
        ]
    }
    same_offset = {
        "chapter.md": [
            FootnoteEdit(2, 2, "A", "first-marker"),
            FootnoteEdit(2, 2, "B", "second-marker"),
        ]
    }

    assert any("overlapping edits" in error for error in validate_edits(source, overlapping))
    assert any("multiple insertions" in error for error in validate_edits(source, same_offset))
    assert not create_edit_plan(source, overlapping).valid
    with pytest.raises(ValueError, match="overlapping edits"):
        transform_edits(source, overlapping)


def test_plan_rejects_unknown_units_and_out_of_range_spans():
    errors = validate_edits(
        {"chapter.md": "abc"},
        {
            "chapter.md": [FootnoteEdit(0, 4, "", "remove")],
            "other.md": [FootnoteEdit(0, 1, "", "remove")],
        },
    )

    assert any("outside source length" in error for error in errors)
    assert any("unknown source unit" in error for error in errors)

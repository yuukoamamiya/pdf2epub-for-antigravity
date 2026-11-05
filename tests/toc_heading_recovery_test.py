"""Reviewed layout insertion is the only path for headings absent from Markdown."""
from types import SimpleNamespace

import pytest

from pdf2epub.refine.page_merger import PageMerger


def _layout_node(title, *, start_line=1, checked=None, level=2):
    return SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="layout-reviewed", title=title, level=level,
        boundary_info={
            "start_line": start_line, "heading_action": "insert",
            "heading_evidence_id": "layout-reviewed",
            "heading_checked_range": checked or {"start_line": start_line, "end_line": start_line + 1},
        },
    )


def test_layout_insert_materializes_only_with_explicit_evidence_and_source_check():
    merger = PageMerger()
    merger._toc_nodes = [_layout_node("Recovered title")]
    lines, _, _ = merger._materialize_reviewed_headings(1, ["Body text."])
    assert lines == ["## Recovered title", "", "Body text."]


def test_layout_insert_blocks_when_exact_title_exists_at_same_anchor():
    merger = PageMerger()
    merger._toc_nodes = [_layout_node("Same title", checked={"start_line": 1, "end_line": 3})]
    with pytest.raises(ValueError, match="already contains the heading"):
        merger._materialize_reviewed_headings(1, ["# Same title", "", "Body."])


def test_layout_insert_blocks_multiline_title_at_anchor_even_with_later_body():
    merger = PageMerger()
    merger._toc_nodes = [_layout_node("Part Chapter", checked={"start_line": 1, "end_line": 4})]
    with pytest.raises(ValueError, match="already contains the heading"):
        merger._materialize_reviewed_headings(1, ["# Part", "## Chapter", "Body."])


def test_layout_insert_allows_same_title_at_a_distinct_source_position():
    merger = PageMerger()
    merger._toc_nodes = [_layout_node("Same title", start_line=3, checked={"start_line": 3, "end_line": 4})]
    lines, _, _ = merger._materialize_reviewed_headings(1, ["# Same title", "", "Body."])
    assert lines.count("# Same title") == 1
    assert lines.count("## Same title") == 1


def test_layout_insert_rejects_empty_or_nonlocal_checked_ranges():
    merger = PageMerger()
    for checked in ({"start_line": 1, "end_line": 1}, {"start_line": 2, "end_line": 3}):
        merger._toc_nodes = [_layout_node("Recovered", checked=checked)]
        with pytest.raises(ValueError, match="absence check"):
            merger._materialize_reviewed_headings(1, ["Body.", "Another line."])


def test_layout_insert_preserves_number_identity_in_unrelated_reviewed_nodes():
    merger = PageMerger()
    first = _layout_node("Section II", level=3)
    second = SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="numbered-section", title="2", level=3,
        boundary_info={"start_line": 2, "heading_end_line": 3, "heading_action": "bind"},
    )
    merger._toc_nodes = [first, second]
    lines, _, _ = merger._materialize_reviewed_headings(1, ["Body.", "2", "Section body."])
    assert "### Section II" in lines
    assert "### 2" in lines

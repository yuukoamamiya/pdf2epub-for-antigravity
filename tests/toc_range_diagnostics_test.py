"""Focused regressions for page and line range diagnostics in reviewed TOCs."""
import copy

import pymupdf as fitz

from pdf2epub.refine.pdf_outline import extract_pdf_outline
from pdf2epub.refine.subagent_workflow import validate_toc_tree_data


def _node(title, level, start, end, **kwargs):
    return {"title": title, "level": level, "start_page": start, "end_page": end,
            "children": [], **kwargs}


def test_parent_child_page_conflict_diagnostic_has_identifying_context():
    parent = _node("Parent", 1, 235, 235, node_id="p-id")
    child = _node("Sub", 2, 236, 236, node_id="sub-id")
    parent["children"] = [child]
    sibling = _node("Next", 1, 236, 237, node_id="next-id",
                    boundary_info={"start_line": 30})
    original = copy.deepcopy([parent, sibling])
    errors = validate_toc_tree_data({"chapters": [parent, sibling]}, 237)
    matches = [e for e in errors if "outside its parent page range" in e]
    assert matches
    message = matches[0]
    assert "p-id" in message and "sub-id" in message and "next-id" in message
    assert "235-235" in message and "236-236" in message
    assert "range 236-237, start_line 30" in message
    assert "candidate parent closure range 235-236 is not applied" in message
    assert "shares page 236" in message and "start_line 30" in message
    assert "Neither direction is applied automatically" in message
    assert [parent, sibling] == original


def test_child_cannot_cross_parent_exclusive_line_boundary():
    parent = _node("Parent", 1, 235, 236, node_id="p",
                   boundary_info={"end_line": 30})
    child = _node("Sub", 2, 236, 236, node_id="s",
                  boundary_info={"start_line": 10, "end_line": 35})
    parent["children"] = [child]
    sibling = _node("Next chapter", 1, 236, 237, node_id="n",
                    boundary_info={"start_line": 30})
    before = copy.deepcopy([parent, sibling])
    errors = validate_toc_tree_data({"chapters": [parent, sibling]}, 237)
    assert any("crosses parent" in e and "exclusive line 30" in e for e in errors)
    assert any("lines 10-35" in e and "exclusive line 30" in e for e in errors)
    assert [parent, sibling] == before


def test_valid_same_page_handoff_equal_exclusive_line_and_parent_child():
    parent = _node("Parent", 1, 1, 2, node_id="p",
                   boundary_info={"start_line": 2, "end_line": 30})
    child = _node("Sub", 2, 1, 2, node_id="s",
                  boundary_info={"start_line": 3, "end_line": 30})
    parent["children"] = [child]
    next_chapter = _node("Next", 1, 2, 3, node_id="n",
                         boundary_info={"start_line": 30})
    errors = validate_toc_tree_data({"chapters": [parent, next_chapter]}, 3)
    assert errors == []


def test_deep_descendant_is_checked_against_each_ancestor_line_boundary():
    root = _node("Root", 1, 1, 2, node_id="root",
                 boundary_info={"start_line": 1, "end_line": 20})
    child = _node("Child", 2, 1, 2, node_id="child",
                  boundary_info={"start_line": 2, "end_line": 25})
    grandchild = _node("Grandchild", 3, 2, 2, node_id="grandchild",
                       boundary_info={"start_line": 3, "end_line": 25})
    child["children"] = [grandchild]
    root["children"] = [child]
    errors = validate_toc_tree_data({"chapters": [root]}, 2)
    assert any("crosses parent 'root'" in e for e in errors)


def test_boundary_info_invalid_type_is_reported_without_crashing():
    node = _node("Chapter", 1, 1, 1, node_id="x", boundary_info=["bad"])
    errors = validate_toc_tree_data({"chapters": [node]}, 1)
    assert any("boundary_info must be an object" in e for e in errors)


def test_heading_end_line_is_supported_boundary_metadata():
    node = _node("Chapter", 1, 1, 1, node_id="x",
                 boundary_info={"start_line": 1, "heading_end_line": 3})
    assert validate_toc_tree_data({"chapters": [node]}, 1) == []


def test_bad_parent_and_previous_boundaries_do_not_crash_and_bool_is_invalid():
    parent = _node("Parent", 1, 1, 1, node_id="p", boundary_info=["bad"])
    first = _node("First", 2, 1, 1, node_id="c1",
                  boundary_info={"start_line": True})
    second = _node("Second", 2, 1, 1, node_id="c2",
                   boundary_info={"start_line": 5})
    parent["children"] = [first, second]
    errors = validate_toc_tree_data({"chapters": [parent]}, 1)
    assert any("boundary_info must be an object" in e for e in errors)
    assert any("start_line must be a positive integer" in e for e in errors)

    # A malformed prior sibling boundary must also be treated as absent.
    root = _node("Root", 1, 1, 2, node_id="r")
    prior = _node("Prior", 2, 1, 1, node_id="a", boundary_info=["bad"])
    following = _node("Following", 2, 1, 2, node_id="b",
                      boundary_info={"start_line": 4})
    root["children"] = [prior, following]
    sibling_errors = validate_toc_tree_data({"chapters": [root]}, 2)
    assert any("chapters[0].children[0].boundary_info must be an object" in e
               for e in sibling_errors)


def test_native_outline_cross_page_inferred_child_range_warns_for_review(tmp_path):
    path = tmp_path / "book.pdf"
    doc = fitz.open()
    for _ in range(4):
        doc.new_page()
    # Child bookmark begins after the parent's next sibling, yielding an
    # inferred child range outside the parent's draft range.
    doc.set_toc([[1, "Parent", 1], [2, "Sub", 3], [1, "Next", 2]])
    doc.save(path)
    doc.close()

    draft = extract_pdf_outline(path, tmp_path / "draft.json", total_pages=4)
    warning = next(w for w in draft["warnings"] if "range conflict" in w)
    assert "'Sub'" in warning and "page 3" in warning
    assert "'Parent'" in warning and "ends page 1" in warning
    assert "OCR" in warning and "same-page line boundaries" in warning


def test_sibling_nodes_must_share_one_logical_level():
    errors = validate_toc_tree_data(
        {"chapters": [_node("A", 2, 1, 1), _node("B", 3, 2, 2)]}, 2
    )
    assert any("sibling nodes must share one logical level" in error for error in errors)

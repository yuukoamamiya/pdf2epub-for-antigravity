import json
from pathlib import Path
from types import SimpleNamespace
import pytest

from pdf2epub.refine.main import RefinedBreakdown
from pdf2epub.refine.page_merger import PageMerger
from pdf2epub.refine.heading_ranges import source_range_matches_title
from pdf2epub.refine.toc_tree import TOCNode
from pdf2epub.refine.toc_structure_review import (
    build_structure_review,
    create_structure_review_snapshot,
    validate_structure_review,
)


def test_standalone_italic_title_and_italic_prose_are_candidates_with_explicit_dispositions(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "*A Real Heading*\n\n*This is a complete emphasized sentence.*\n", encoding="utf-8"
    )
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"schema_version": 1, "chapters": []}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    candidates_doc, manifest = create_structure_review_snapshot(
        tmp_path, pages, "ocr", outline, pdf
    )
    rows = candidates_doc["candidates"]
    assert [row["excerpt"] for row in rows] == [
        "*A Real Heading*", "*This is a complete emphasized sentence.*"
    ]
    title_row, prose_row = rows
    tree = {"schema_version": 2, "chapters": [{
        "node_id": title_row["candidate_id"], "title": "A Real Heading",
        "level": 1, "start_page": 1, "end_page": 1,
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    (tmp_path / "refine_subagent_manifest.json").write_text(
        json.dumps({"schema_version": 1, "structure_review": manifest}), encoding="utf-8"
    )
    decisions = {
        "schema_version": 2,
        "source_fingerprint": manifest["source_fingerprint"],
        "candidate_inventory_sha256": candidates_doc["inventory_sha256"],
        "candidate_decisions": [
            {"candidate_id": title_row["candidate_id"], "disposition": "toc_node",
             "node_id": title_row["candidate_id"]},
            {"candidate_id": prose_row["candidate_id"], "disposition": "body_text"},
        ],
        "page_coverage": [{"page": 1, "reviewed": True, "evidence": ["page_text"]}],
    }
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    expected = create_structure_review_snapshot(tmp_path, pages, "ocr", outline, pdf)
    assert validate_structure_review(tmp_path, tree, expected_snapshot=expected) == []


def test_multiline_emphasis_candidate_requires_explicit_region_end(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("*A title\nwith a second line*\nBody", encoding="utf-8")
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    candidates, _ = create_structure_review_snapshot(
        tmp_path, pages, "ocr", outline, tmp_path / "input.pdf"
    )
    assert candidates["candidates"][0]["line"] == 1
    assert candidates["candidates"][0]["end_line"] == 2
    assert candidates["candidates"][0]["excerpt"] == "*A title with a second line*"


def test_same_page_text_and_layout_headings_preserve_body_order():
    merger = PageMerger()
    merger._toc_nodes = [
        SimpleNamespace(start_page=1, binding_mode="visible", node_id="cand-text",
                        boundary_info={"start_line": 2, "heading_end_line": 3, "heading_action": "bind"}, level=1, title="First heading"),
        SimpleNamespace(start_page=1, binding_mode="visible", node_id="layout-region",
                        boundary_info={"start_line": 4, "heading_action": "insert",
                                       "heading_evidence_id": "layout-region",
                                       "heading_checked_range": {"start_line": 4, "end_line": 5}}, level=2, title="Second heading"),
    ]
    rendered, _, _ = merger._materialize_reviewed_headings(
        1, ["Opening body.", "First heading", "Body between headings.", "Closing body."]
    )
    output = "\n".join(rendered)
    assert output.index("Opening body.") < output.index("# First heading")
    assert output.index("# First heading") < output.index("Body between headings.")
    assert output.index("Body between headings.") < output.index("## Second heading")
    assert output.index("## Second heading") < output.index("Closing body.")
    assert output.count("body.") == 2


def test_reviewed_run_in_prefix_materializes_only_the_exact_heading_range():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="run-in",
        boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind", "heading_prefix": "*Heading.*"},
        level=2, title="Heading.",
    )]
    rendered, _, _ = merger._materialize_reviewed_headings(
        1, ["*Heading.* The short body remains in the paragraph."]
    )
    assert rendered == ["## *Heading.*", "", "The short body remains in the paragraph."]


def test_reviewed_roman_label_is_not_duplicated_in_run_in_heading_prefix():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="run-in-roman",
        boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind", "heading_prefix": "I Heading"},
        level=2, title="Heading", numbering_label="I",
    )]
    rendered, _, _ = merger._materialize_reviewed_headings(1, ["I Heading The body remains here."])
    assert rendered == ["## I Heading", "", "The body remains here."]


def test_run_in_prefix_is_relative_to_heading_marker_free_source_line():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="run-in",
        boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind", "heading_prefix": "*Heading.*"},
        level=2, title="Heading.",
    )]
    rendered, _, _ = merger._materialize_reviewed_headings(
        1, ["#### *Heading.*    Body"]
    )
    assert rendered == ["## *Heading.*", "", "Body"]


def test_run_in_heading_preserves_original_paragraph_continuation_and_boundaries():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="run-in",
        boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind", "heading_prefix": "*Heading.*"},
        level=2, title="Heading.",
    )]
    rendered, _, _ = merger._materialize_reviewed_headings(1, [
        "*Heading.* Body first line", "continuation of the same paragraph.", "", "Next paragraph."
    ])
    assert rendered == ["## *Heading.*", "", "Body first line", "continuation of the same paragraph.", "", "Next paragraph."]


def test_mismatched_reviewed_heading_range_fails_closed():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="run-in",
        boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind"}, level=2, title="Heading.",
    )]
    with pytest.raises(ValueError, match="does not match canonical TOC title"):
        merger._materialize_reviewed_headings(
            1, ["Heading. This 51 character contaminated paragraph remains body."]
        )


def test_reviewed_source_heading_level_is_rewritten_to_canonical_level():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="heading",
        boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind"}, level=2, title="Heading",
    )]
    rendered, _, _ = merger._materialize_reviewed_headings(1, ["#### Heading"])
    assert rendered == ["## Heading"]


def test_multiline_heading_range_materializes_once_and_preserves_inline_markup_ids_and_footnote():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="layout-like-but-bound",
        boundary_info={"start_line": 1, "heading_end_line": 3, "heading_action": "bind"},
        level=1, title="第一部分 法哲学的体系建构",
    )]
    source = [
        "# 第一部分",
        "## *法哲学的体系建构* <a id=\"author-id\"></a>[^1]",
        "Body remains outside the reviewed heading range.",
    ]
    rendered, line_positions, _ = merger._materialize_reviewed_headings(1, source)
    assert len([line for line in rendered if line.startswith("# ")]) == 1
    heading = next(line for line in rendered if line.startswith("# "))
    assert "##" not in heading and "*法哲学的体系建构*" in heading
    assert 'id="author-id"' in heading and "[^1]" in heading
    assert any("Body remains outside" in line for line in rendered)
    assert line_positions[1] == line_positions[2]


def test_inserted_heading_at_page_number_line_keeps_new_heading_and_removes_only_original_row(tmp_path):
    merger = PageMerger()
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Opening body\n\n2\nFollowing body", encoding="utf-8")
    node = TOCNode("Recovered heading", 2, 1, 1, node_id="layout-insertion", boundary_info={
            "start_line": 3, "heading_action": "insert",
            "heading_evidence_id": "layout-insertion",
            "heading_checked_range": {"start_line": 3, "end_line": 4},
        })
    merger.set_toc_nodes([node])
    merger.set_reviewed_page_number_lines({1: {3}})
    content = merger.merge_node_content(node, pages)
    assert "## Recovered heading" in content
    assert "\n2\n" not in content
    assert "Following body" in content


def test_overlapping_reviewed_heading_ranges_block_instead_of_overwriting():
    merger = PageMerger()
    merger._toc_nodes = [
        SimpleNamespace(start_page=1, binding_mode="visible", node_id="first",
                        boundary_info={"start_line": 1, "heading_end_line": 3, "heading_action": "bind"},
                        level=1, title="First title"),
        SimpleNamespace(start_page=1, binding_mode="visible", node_id="second",
                        boundary_info={"start_line": 2, "heading_end_line": 3, "heading_action": "bind"},
                        level=2, title="Second title"),
    ]
    with pytest.raises(ValueError):
        merger._materialize_reviewed_headings(1, ["First title", "Second title", "Body"])


def test_layout_insert_rejects_existing_multiline_heading_at_same_anchor():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="layout-current",
        boundary_info={"start_line": 1, "heading_action": "insert",
                       "heading_evidence_id": "layout-current",
                       "heading_checked_range": {"start_line": 1, "end_line": 4}},
        level=1, title="Part Chapter",
    )]
    with pytest.raises(ValueError, match="already contains the heading"):
        merger._materialize_reviewed_headings(1, ["# Part", "## Chapter", "Body"])


def test_layout_insert_allows_same_title_at_another_position_outside_checked_range():
    merger = PageMerger()
    merger._toc_nodes = [SimpleNamespace(
        start_page=1, binding_mode="visible", node_id="layout-current",
        boundary_info={"start_line": 3, "heading_action": "insert",
                       "heading_evidence_id": "layout-current",
                       "heading_checked_range": {"start_line": 3, "end_line": 4}},
        level=2, title="Same title",
    )]
    rendered, _, _ = merger._materialize_reviewed_headings(1, ["# Same title", "", "Body"])
    assert rendered.count("## Same title") == 1
    assert rendered.count("# Same title") == 1


def test_eof_layout_heading_respects_explicit_boundary_and_spans_next_page(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("First\nbody A\nlast A", encoding="utf-8")
    (pages / "page_002.md").write_text("body B", encoding="utf-8")
    first = TOCNode("First", 1, 1, 1, boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind", "end_line": 4}, node_id="text-1")
    second = TOCNode("Second", 1, 2, 2, boundary_info={"start_line": 1, "heading_action": "insert",
                                                        "heading_evidence_id": "layout-2",
                                                        "heading_checked_range": {"start_line": 1, "end_line": 2}}, node_id="layout-2")
    merger = PageMerger()
    merger.set_toc_nodes([first, second])

    first_content = merger.merge_node_content(first, pages, next_node=second)
    second_content = merger.merge_node_content(second, pages)

    assert "# Second" not in first_content
    assert "last A" in first_content
    assert second_content.count("# Second") == 1
    assert "body B" in second_content


def test_eof_layout_heading_respects_next_node_cut_and_grouped_merge(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("First\nbody A\nlast A", encoding="utf-8")
    (pages / "page_002.md").write_text("body B", encoding="utf-8")
    first = TOCNode("First", 1, 1, 1, boundary_info={"start_line": 1, "heading_end_line": 2, "heading_action": "bind"}, node_id="text-1")
    second = TOCNode("Second", 1, 2, 2, boundary_info={"start_line": 1, "heading_action": "insert",
                                                        "heading_evidence_id": "layout-2",
                                                        "heading_checked_range": {"start_line": 1, "end_line": 2}}, node_id="layout-2")
    merger = PageMerger()
    merger.set_toc_nodes([first, second])

    first_content = merger.merge_node_content(first, pages, next_node=second)
    grouped_content = merger.merge_nodes_content([first], pages, next_node=second)
    second_content = merger.merge_node_content(second, pages)

    assert "# Second" not in first_content
    assert "# Second" not in grouped_content
    assert first_content.count("last A") == grouped_content.count("last A") == 1
    assert second_content.count("# Second") == 1


def test_layout_only_section_header_is_reviewed_and_recovered_by_refine_local(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Body follows the omitted heading.\n", encoding="utf-8")
    sidecar = {
        "page_number": 1,
        "coordinate_system": "normalized",
        "page_box": [0, 0, 1, 1],
        "blocks": [{"text": "Recovered Heading", "label": "Section-Header", "bbox": [0.1, 0.08, 0.9, 0.14]}],
    }
    (pages / "page_001.ocr.json").write_text(json.dumps(sidecar), encoding="utf-8")
    (pages / "page_001.html").write_text(
        '<div data-label="Section-Header">Recovered Heading</div>', encoding="utf-8"
    )
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"schema_version": 1, "chapters": []}), encoding="utf-8")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, tmp_path / "input.pdf")
    inventory = json.loads((tmp_path / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    candidate = next(row for row in inventory["candidates"] if row["kind"] == "layout_region")
    tree = {"schema_version": 2, "chapters": [{
        "node_id": candidate["candidate_id"], "title": "Recovered Heading",
        "level": 1, "start_page": 1, "end_page": 1,
        "boundary_info": {"start_line": 1, "heading_action": "insert",
                          "heading_evidence_id": candidate["candidate_id"],
                          "heading_checked_range": {"start_line": 1, "end_line": 2}}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (tmp_path / "refine_subagent_manifest.json").write_text(
        json.dumps({"schema_version": 1, "structure_review": manifest}), encoding="utf-8"
    )
    decisions = {
        "schema_version": 2,
        "source_fingerprint": manifest["source_fingerprint"],
        "candidate_inventory_sha256": inventory["inventory_sha256"],
        "candidate_decisions": [{
            "candidate_id": candidate["candidate_id"], "disposition": "toc_node",
            "node_id": candidate["candidate_id"], "binding_mode": "visible",
            "reason": "The Section-Header region contains this heading omitted from Markdown.",
            "evidence": {"kind": "layout_region", "page": 1, "block": 0,
                         "source": "pages/page_001.ocr.json", "bbox": candidate["bbox"],
                         "start_line": 1},
        }],
        "page_coverage": [{"page": 1, "reviewed": True, "evidence": ["page_text"]}],
    }
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")

    RefinedBreakdown(config={}, max_tokens=8000).process_from_toc(
        tmp_path / "input.pdf", tmp_path, "Book"
    )

    output = (tmp_path / "ocr_markdown" / "chapter_1.md").read_text(encoding="utf-8")
    assert "# Recovered Heading" in output
    assert output.count("Body follows the omitted heading.") == 1


def test_parent_gap_uses_reviewed_heading_range_and_page_number_projection(tmp_path: Path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text(
        "# Parent\n\n## Subtitle\n\n2\n", encoding="utf-8"
    )
    (pages / "page_002.md").write_text("## Child\nChild body.\n", encoding="utf-8")
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"schema_version": 1, "chapters": []}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"synthetic")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    candidates = json.loads((tmp_path / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    decisions = []
    for candidate in candidates["candidates"]:
        disposition = (
            "page_number" if candidate.get("page") == 1 and candidate.get("line") == 5
            else "body_text"
        )
        decisions.append({"candidate_id": candidate["candidate_id"], "disposition": disposition})
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps({
        "schema_version": 2,
        "source_fingerprint": manifest["source_fingerprint"],
        "candidate_inventory_sha256": candidates["inventory_sha256"],
        "candidate_decisions": decisions,
        "page_coverage": [
            {"page": 1, "reviewed": True, "evidence": ["visual"]},
            {"page": 2, "reviewed": True, "evidence": ["visual"]},
        ],
    }), encoding="utf-8")

    parent = TOCNode("Parent Subtitle", 1, 1, 2, boundary_info={
        "start_line": 1, "heading_end_line": 4,
    })
    parent.children = [TOCNode("Child", 2, 2, 2)]
    assert RefinedBreakdown._children_cover_parent(parent, pages) is True

    (pages / "page_001.md").write_text(
        "# Parent\n\n## Subtitle\n\n2\nReal parent prose.\n", encoding="utf-8"
    )
    assert RefinedBreakdown._children_cover_parent(parent, pages) is False

    parent.end_page = 1
    refreshed_manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    candidates = json.loads((tmp_path / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    refreshed_decisions = [
        {"candidate_id": row["candidate_id"],
         "disposition": "page_number" if row.get("page") == 1 and row.get("line") == 5 else "body_text"}
        for row in candidates["candidates"]
    ]
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps({
        "schema_version": 2,
        "source_fingerprint": refreshed_manifest["source_fingerprint"],
        "candidate_inventory_sha256": candidates["inventory_sha256"],
        "candidate_decisions": refreshed_decisions,
        "page_coverage": [
            {"page": 1, "reviewed": True, "evidence": ["visual"]},
            {"page": 2, "reviewed": True, "evidence": ["visual"]},
        ],
    }), encoding="utf-8")
    parent.boundary_info["end_line"] = 6
    assert RefinedBreakdown._range_has_standalone_content(parent, pages, 1, 1) is False
    parent.boundary_info["end_line"] = 7
    assert RefinedBreakdown._range_has_standalone_content(parent, pages, 1, 1) is True


@pytest.mark.parametrize("line", ["Theory", "*Theory*", "_Theory_"])
def test_insert_absence_check_recognizes_plain_and_emphasized_anchor_title(line: str):
    assert source_range_matches_title([line, "Body follows."], 1, 3, "Theory", anchor_line=1)


@pytest.mark.parametrize("lines,title", [
    (["Later body.", "Same title"], "Same title"),
    (["# Part", "## Theory"], "Theory"),
])
def test_insert_absence_check_does_not_search_later_source_lines(lines, title):
    assert not source_range_matches_title(lines, 1, len(lines) + 1, title, anchor_line=1)

import json
from pathlib import Path
from types import SimpleNamespace

from pdf2epub.refine.main import RefinedBreakdown
from pdf2epub.refine.page_merger import PageMerger
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
        "boundary_info": {"start_line": 1}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    (tmp_path / "refine_subagent_manifest.json").write_text(
        json.dumps({"schema_version": 1, "structure_review": manifest}), encoding="utf-8"
    )
    decisions = {
        "schema_version": 1,
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
                        boundary_info={"start_line": 2}, level=1, title="First heading"),
        SimpleNamespace(start_page=1, binding_mode="visible", node_id="layout-region",
                        boundary_info={"start_line": 4}, level=2, title="Second heading"),
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


def test_eof_layout_heading_respects_explicit_boundary_and_spans_next_page(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("First\nbody A\nlast A", encoding="utf-8")
    (pages / "page_002.md").write_text("body B", encoding="utf-8")
    first = TOCNode("First", 1, 1, 1, boundary_info={"start_line": 1, "end_line": 4}, node_id="text-1")
    second = TOCNode("Second", 1, 1, 2, boundary_info={"start_line": 4}, node_id="layout-2")
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
    first = TOCNode("First", 1, 1, 1, boundary_info={"start_line": 1}, node_id="text-1")
    second = TOCNode("Second", 1, 1, 2, boundary_info={"start_line": 4}, node_id="layout-2")
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
        "boundary_info": {"start_line": 1}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    (tmp_path / "refine_subagent_manifest.json").write_text(
        json.dumps({"schema_version": 1, "structure_review": manifest}), encoding="utf-8"
    )
    decisions = {
        "schema_version": 1,
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

import hashlib
import json
import pytest

from pdf2epub.refine.toc_structure_review import (
    _heading_like_text_regions,
    build_structure_review,
    create_structure_review_snapshot,
    reviewed_page_number_lines,
    validate_structure_review,
)
from pdf2epub.refine.toc_tree import TOCNode


def _prepared(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("# Introduction\nBody paragraph.\n", encoding="utf-8")
    (pages / "page_002.md").write_text("CHAPTER ONE\nMore body.\n", encoding="utf-8")
    (pages / "page_001.ocr.json").write_text(
        json.dumps({"page_number": 1, "coordinate_system": "normalized", "blocks": [
            {"text": "Introduction", "bbox": [0.1, 0.1, 0.8, 0.15]}
        ]}), encoding="utf-8",
    )
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"entries": [{"title": "Introduction", "start_page": 1}]}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"synthetic pdf source")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    candidates = json.loads((tmp_path / "toc_heading_candidates.json").read_text())
    rows = candidates["candidates"]
    node_row = next(row for row in rows if row["kind"] == "text_line")
    decisions = {
        "schema_version": 2,
        "source_fingerprint": manifest["source_fingerprint"],
        "candidate_inventory_sha256": candidates["inventory_sha256"],
        "candidate_decisions": [
            {"candidate_id": row["candidate_id"],
             "disposition": "toc_node" if row is node_row else "body_text",
             **({"node_id": node_row["candidate_id"], "binding_mode": "visible"} if row is node_row else {})}
            for row in rows
        ],
        "page_coverage": [{"page": 1, "reviewed": True, "evidence": ["visual"]},
                          {"page": 2, "reviewed": True, "evidence": ["visual"]}],
    }
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    tree = {"schema_version": 2, "chapters": [{"title": "Introduction", "level": 1, "start_page": 1, "end_page": 2,
                                               "boundary_info": {"start_line": node_row["line"], "heading_end_line": node_row["end_line"] + 1, "heading_action": "bind"}, "node_id": node_row["candidate_id"]}]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    return pages, outline, pdf, tree


def _snapshot(tmp_path, pages, outline, pdf):
    return create_structure_review_snapshot(tmp_path, pages, "ocr", outline, pdf)


def test_review_requires_complete_candidate_and_page_coverage(tmp_path):
    pages, outline, pdf, tree = _prepared(tmp_path)
    assert validate_structure_review(tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)) == []
    decisions_path = tmp_path / "toc_structure_decisions.json"
    decisions = json.loads(decisions_path.read_text())
    decisions["candidate_decisions"].pop()
    decisions["page_coverage"].pop()
    decisions_path.write_text(json.dumps(decisions), encoding="utf-8")
    errors = validate_structure_review(tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf))
    assert any("candidate disposition missing" in error for error in errors)
    assert any("page coverage" in error for error in errors)


def test_bound_heading_range_must_fit_page_line_count(tmp_path):
    pages, outline, pdf, tree = _prepared(tmp_path)
    tree["chapters"][0]["boundary_info"]["heading_end_line"] = 999
    errors = validate_structure_review(tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf))
    assert any("valid in-page exclusive heading range" in error for error in errors)


def test_standalone_numerals_are_explicit_candidates_but_code_lists_and_footnotes_are_not():
    lines = [
        "2", "", "正文里的数字 2 保留。", "II", "三", "# 8",
        "```", "4", "```", "    5", "1. A real list item", "[^2]: footnote",
    ]
    rows = _heading_like_text_regions(1, lines)
    assert [(row["line"], row["excerpt"]) for row in rows if row["kind"] == "standalone_number"] == [
        (1, "2"), (4, "II"), (5, "三"), (6, "# 8"),
    ]


def test_roman_wrappers_and_invalid_fence_closers_are_handled_as_source_lines():
    lines = ["ii.", "（二）", "```", "```not-a-close", "3", "```"]
    rows = _heading_like_text_regions(1, lines)
    assert [(row["line"], row["excerpt"]) for row in rows if row["kind"] == "standalone_number"] == [
        (1, "ii."), (2, "（二）"),
    ]


def test_same_numeral_has_position_scoped_page_heading_and_body_dispositions(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("2\nBody number 2.\n2\n### 2\nSection body.\n", encoding="utf-8")
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"synthetic")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    inventory = json.loads((tmp_path / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    numeric = {row["line"]: row for row in inventory["candidates"] if row["kind"] == "standalone_number"}
    heading = numeric[4]
    tree = {"schema_version": 2, "chapters": [{
        "node_id": heading["candidate_id"], "title": "2", "level": 3,
        "start_page": 1, "end_page": 1, "boundary_info": {"start_line": 4, "heading_end_line": 5, "heading_action": "bind"}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    decisions = {
        "schema_version": 2,
        "source_fingerprint": manifest["source_fingerprint"],
        "candidate_inventory_sha256": inventory["inventory_sha256"],
        "candidate_decisions": [
            {"candidate_id": numeric[1]["candidate_id"], "disposition": "page_number"},
            {"candidate_id": numeric[3]["candidate_id"], "disposition": "body_text"},
            {"candidate_id": heading["candidate_id"], "disposition": "toc_node", "node_id": heading["candidate_id"]},
                {"candidate_id": next(row["candidate_id"] for row in inventory["candidates"] if row["line"] == 5), "disposition": "body_text"},
        ],
            "page_coverage": [{"page": 1, "reviewed": True, "evidence": ["visual"]}],
    }
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    expected = create_structure_review_snapshot(tmp_path, pages, "ocr", outline, pdf)
    assert validate_structure_review(tmp_path, tree, expected_snapshot=expected) == []
    assert reviewed_page_number_lines(tmp_path) == {1: {1}}


def test_reviewed_run_in_prefix_validates_against_exact_prefix_not_full_candidate(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("# *Heading.* Body text\n", encoding="utf-8")
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"synthetic")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    candidates = json.loads((tmp_path / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    candidate = next(row for row in candidates["candidates"] if row["kind"] == "text_line")
    tree = {"schema_version": 2, "chapters": [{
        "title": "Heading.", "level": 2, "start_page": 1, "end_page": 1,
        "node_id": candidate["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind", "heading_prefix": "*Heading.*"}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    decisions = {"schema_version": 2, "source_fingerprint": manifest["source_fingerprint"],
                 "candidate_inventory_sha256": candidates["inventory_sha256"],
                 "candidate_decisions": [{"candidate_id": candidate["candidate_id"],
                    "disposition": "toc_node", "node_id": candidate["candidate_id"], "binding_mode": "visible"}],
                 "page_coverage": [{"page": 1, "reviewed": True, "evidence": ["visual"]}]}
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    expected = _snapshot(tmp_path, pages, outline, pdf)
    assert validate_structure_review(tmp_path, tree, expected_snapshot=expected) == []


def test_reviewed_number_label_can_live_in_source_title_or_separate_metadata(tmp_path):
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("# 1. Heading\nBody\n", encoding="utf-8")
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"synthetic")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    candidates = json.loads((tmp_path / "toc_heading_candidates.json").read_text(encoding="utf-8"))
    candidate = next(row for row in candidates["candidates"] if row["kind"] == "text_line")
    evidence = {"page": 1, "line": 1, "excerpt": "1. Heading"}
    tree = {"schema_version": 2, "chapters": [{
        "title": "Heading", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": candidate["candidate_id"], "binding_mode": "visible",
        "numbering_label": "1.", "numbering_evidence": evidence,
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"}, "children": [],
    }]}
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    decisions = {"schema_version": 2, "source_fingerprint": manifest["source_fingerprint"],
                 "candidate_inventory_sha256": candidates["inventory_sha256"],
                 "candidate_decisions": [{"candidate_id": candidate["candidate_id"],
                    "disposition": "toc_node", "node_id": candidate["candidate_id"],
                    "binding_mode": "visible", "numbering_label": "1.", "numbering_evidence": evidence}],
                 "page_coverage": [{"page": 1, "reviewed": True, "evidence": ["visual"]}]}
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(decisions), encoding="utf-8")
    expected = _snapshot(tmp_path, pages, outline, pdf)
    assert validate_structure_review(tmp_path, tree, expected_snapshot=expected) == []


def test_review_rejects_duplicate_unknown_unresolved_and_stale_sources(tmp_path):
    pages, outline, pdf, _ = _prepared(tmp_path)
    decision_path = tmp_path / "toc_structure_decisions.json"
    decisions = json.loads(decision_path.read_text())
    decisions["candidate_decisions"][0]["disposition"] = "unresolved"
    decisions["candidate_decisions"].append(dict(decisions["candidate_decisions"][0]))
    decision_path.write_text(json.dumps(decisions), encoding="utf-8")
    errors = validate_structure_review(tmp_path, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf))
    assert any("unresolved candidate" in error for error in errors)
    assert any("duplicate disposition" in error for error in errors)

    # The current source fingerprint changes when a selected page changes.
    (pages / "page_001.md").write_text("# Changed\n", encoding="utf-8")
    expected = _snapshot(tmp_path, pages, outline, pdf)
    assert any("stale" in error for error in validate_structure_review(tmp_path, expected_snapshot=expected))


@pytest.mark.parametrize("source_name", ["outline_toc_draft.json", "pages/page_001.ocr.json"])
def test_review_currentness_includes_outline_and_layout_evidence(tmp_path, source_name):
    pages, outline, pdf, _ = _prepared(tmp_path)
    source = tmp_path / source_name
    source.write_text(source.read_text(encoding="utf-8") + " ", encoding="utf-8")
    expected = _snapshot(tmp_path, pages, outline, pdf)
    errors = validate_structure_review(tmp_path, expected_snapshot=expected)
    assert any("stale" in error or "inventory differs" in error for error in errors)


def test_reviewer_added_candidate_requires_allowed_location_and_tree_link(tmp_path):
    pages, _, _, tree = _prepared(tmp_path)
    path = tmp_path / "toc_structure_decisions.json"
    decisions = json.loads(path.read_text())
    decisions["candidate_decisions"].append({
        "candidate_id": "reviewer-page2-line99", "disposition": "body_text",
        "evidence": {"page": 2, "line": 99}, "excerpt": "More body.",
    })
    path.write_text(json.dumps(decisions), encoding="utf-8")
    assert any("outside the allowlist" in error for error in validate_structure_review(tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, tmp_path / "outline_toc_draft.json", tmp_path / "input.pdf")))


def test_valid_reviewer_added_candidate_uses_exact_allowlisted_line(tmp_path):
    pages, outline, pdf, _ = _prepared(tmp_path)
    excerpt = "More body."
    cid = "reviewer-" + hashlib.sha256(f"2:2:{excerpt}".encode()).hexdigest()[:20]
    path = tmp_path / "toc_structure_decisions.json"
    decisions = json.loads(path.read_text())
    decisions["candidate_decisions"].append({
        "candidate_id": cid, "disposition": "body_text",
        "evidence": {"page": 2, "line": 2}, "excerpt": excerpt,
    })
    path.write_text(json.dumps(decisions), encoding="utf-8")
    assert validate_structure_review(tmp_path, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)) == []


def _atx_fixture(tmp_path, pages_text):
    pages = tmp_path / "pages"
    pages.mkdir()
    for page, text in pages_text.items():
        (pages / f"page_{page:03d}.md").write_text(text, encoding="utf-8")
    outline = tmp_path / "outline_toc_draft.json"
    outline.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    pdf = tmp_path / "input.pdf"
    pdf.write_bytes(b"synthetic pdf source")
    manifest = build_structure_review(tmp_path, pages, "ocr", outline, pdf)
    candidates = json.loads((tmp_path / "toc_heading_candidates.json").read_text())
    return pages, outline, pdf, manifest, candidates, candidates["candidates"]


def _write_atx_review(tmp_path, manifest, candidates, rows, tree, toc_rows=(), reasons=None):
    reasons = reasons or {}
    decisions = [
        {
            "candidate_id": row["candidate_id"],
            "disposition": "toc_node" if (row["page"], row["line"]) in toc_rows else "body_text",
            **({"node_id": row["candidate_id"], "binding_mode": "visible"}
               if (row["page"], row["line"]) in toc_rows else {}),
            **({"reason": reasons[(row["page"], row["line"])]}
               if (row["page"], row["line"]) in reasons else {}),
        }
        for row in rows
    ]
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    pages = sorted({row["page"] for row in rows})
    document = {
        "schema_version": 2,
        "source_fingerprint": manifest["source_fingerprint"],
        "candidate_inventory_sha256": candidates["inventory_sha256"],
        "candidate_decisions": decisions,
        "page_coverage": [
            {"page": page, "reviewed": True, "evidence": ["visual"]} for page in pages
        ],
    }
    (tmp_path / "toc_structure_decisions.json").write_text(json.dumps(document), encoding="utf-8")


def test_atx_heading_level_is_preserved_on_candidates():
    lines = ["## I", "", "### Anmerkungen", "", "2", "## Postskriptum"]
    rows = {row["line"]: row for row in _heading_like_text_regions(1, lines)}
    assert rows[1]["kind"] == "standalone_number"
    assert rows[1]["atx_level"] == 2
    assert rows[3]["kind"] == "text_line"
    assert rows[3]["atx_level"] == 3
    assert "atx_level" not in rows[5]
    assert rows[6]["atx_level"] == 2


def test_unreasoned_atx_subheading_inside_chapter_blocks(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path, {1: "# Chapter One\n\nBody paragraph.\n\n## Section A\n\nMore body.\n"}
    )
    chapter = next(row for row in rows if row["line"] == 1)
    section = next(row for row in rows if row["line"] == 5)
    assert section["atx_level"] == 2
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": [],
    }]}
    _write_atx_review(tmp_path, manifest, candidates, rows, tree, toc_rows={(1, 1)})
    errors = validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    )
    assert any("without an explicit reason" in error for error in errors)
    assert any(section["candidate_id"] in error for error in errors)


def test_reasoned_atx_subheading_inside_chapter_is_accepted(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path, {1: "# Chapter One\n\nBody paragraph.\n\n## Section A\n\nMore body.\n"}
    )
    chapter = next(row for row in rows if row["line"] == 1)
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": [],
    }]}
    _write_atx_review(
        tmp_path, manifest, candidates, rows, tree, toc_rows={(1, 1)},
        reasons={(1, 5): "OCR promoted a repeated running head to a heading"},
    )
    assert validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    ) == []


def test_blank_atx_reason_does_not_excuse_a_heading(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path, {1: "# Chapter One\n\nBody paragraph.\n\n## Section A\n\nMore body.\n"}
    )
    chapter = next(row for row in rows if row["line"] == 1)
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": [],
    }]}
    _write_atx_review(
        tmp_path, manifest, candidates, rows, tree, toc_rows={(1, 1)},
        reasons={(1, 5): "   "},
    )
    errors = validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    )
    assert any("without an explicit reason" in error for error in errors)


def test_atx_heading_outside_reviewed_nodes_needs_no_reason(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path, {
            1: "# Chapter One\n\nBody paragraph.\n",
            2: "## Front matter heading\n\nMore body.\n",
        }
    )
    chapter = next(row for row in rows if row["page"] == 1 and row["line"] == 1)
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": [],
    }]}
    _write_atx_review(tmp_path, manifest, candidates, rows, tree, toc_rows={(1, 1)})
    assert validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    ) == []


def test_numeral_atx_heading_can_model_a_nested_section(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path, {1: "# Chapter One\n\n## I\n\nBody paragraph.\n"}
    )
    chapter = next(row for row in rows if row["line"] == 1)
    section = next(row for row in rows if row["line"] == 3)
    assert section["kind"] == "standalone_number"
    assert section["atx_level"] == 2
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": [{
            "title": "I", "level": 2, "start_page": 1, "end_page": 1,
            "node_id": section["candidate_id"], "binding_mode": "visible",
            "boundary_info": {"start_line": 3, "heading_end_line": 4, "heading_action": "bind"},
            "children": [],
        }],
    }]}
    _write_atx_review(tmp_path, manifest, candidates, rows, tree, toc_rows={(1, 1), (1, 3)})
    assert validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    ) == []


def test_node_id_roundtrip_is_independent_of_title_and_tree_path():
    node = TOCNode(title="Original", level=1, start_page=1, end_page=2, node_id="source-p1-l1-deadbeef")
    encoded = node.to_dict()
    encoded["title"] = "Translated"
    restored = TOCNode.from_dict(encoded)
    assert restored.node_id == "source-p1-l1-deadbeef"


def test_modern_review_rejects_missing_explicit_node_id(tmp_path):
    pages, outline, pdf, tree = _prepared(tmp_path)
    tree["chapters"][0].pop("node_id")
    (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    errors = validate_structure_review(tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf))
    assert any("non-empty stable node_id" in error for error in errors)


@pytest.mark.parametrize("tamper", ["delete_inventory", "bad_disposition", "bad_node_id", "bad_evidence", "visual_allowlist"])
def test_malformed_review_artifacts_fail_closed_without_crashing(tmp_path, tamper):
    pages, outline, pdf, tree = _prepared(tmp_path)
    expected = _snapshot(tmp_path, pages, outline, pdf)
    if tamper == "delete_inventory":
        (tmp_path / "toc_heading_candidates.json").unlink()
    elif tamper in {"bad_disposition", "bad_evidence"}:
        path = tmp_path / "toc_structure_decisions.json"
        decisions = json.loads(path.read_text())
        if tamper == "bad_disposition":
            decisions["candidate_decisions"][0]["disposition"] = []
        else:
            decisions["candidate_decisions"][0]["evidence"] = []
        path.write_text(json.dumps(decisions), encoding="utf-8")
    elif tamper == "bad_node_id":
        tree["chapters"][0]["node_id"] = []
        (tmp_path / "toc_tree.json").write_text(json.dumps(tree), encoding="utf-8")
    else:
        path = tmp_path / "toc_structure_review_manifest.json"
        manifest = json.loads(path.read_text())
        manifest["visual_evidence"] = []
        path.write_text(json.dumps(manifest), encoding="utf-8")
    errors = validate_structure_review(tmp_path, tree, expected_snapshot=expected)
    assert errors


def test_plain_numeral_between_accepted_numbered_headings_needs_a_reason(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path,
        {1: "# Chapter One\n\n## I\n\nBody one.\n\nII\n\nBody two.\n\n## III\n\nBody three.\n"},
    )
    chapter = next(row for row in rows if row["line"] == 1)
    first = next(row for row in rows if row["line"] == 3)
    second = next(row for row in rows if row["line"] == 7)
    third = next(row for row in rows if row["line"] == 11)
    assert second["kind"] == "standalone_number"
    assert "atx_level" not in second
    children = [
        {
            "title": "I", "level": 2, "start_page": 1, "end_page": 1,
            "node_id": first["candidate_id"], "binding_mode": "visible",
            "boundary_info": {"start_line": 3, "heading_end_line": 4, "heading_action": "bind"},
            "children": [],
        },
        {
            "title": "III", "level": 2, "start_page": 1, "end_page": 1,
            "node_id": third["candidate_id"], "binding_mode": "visible",
            "boundary_info": {"start_line": 11, "heading_end_line": 12, "heading_action": "bind"},
            "children": [],
        },
    ]
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": children,
    }]}
    _write_atx_review(
        tmp_path, manifest, candidates, rows, tree,
        toc_rows={(1, 1), (1, 3), (1, 11)},
    )
    errors = validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    )
    assert any("mixes accepted numbered headings" in error for error in errors)
    assert any(second["candidate_id"] in error for error in errors)


def test_reasoned_plain_numeral_is_not_treated_as_a_heading(tmp_path):
    pages, outline, pdf, manifest, candidates, rows = _atx_fixture(
        tmp_path,
        {1: "# Chapter One\n\n## I\n\nBody one.\n\nII\n\nBody two.\n\n## III\n\nBody three.\n"},
    )
    chapter = next(row for row in rows if row["line"] == 1)
    first = next(row for row in rows if row["line"] == 3)
    third = next(row for row in rows if row["line"] == 11)
    children = [
        {
            "title": "I", "level": 2, "start_page": 1, "end_page": 1,
            "node_id": first["candidate_id"], "binding_mode": "visible",
            "boundary_info": {"start_line": 3, "heading_end_line": 4, "heading_action": "bind"},
            "children": [],
        },
        {
            "title": "III", "level": 2, "start_page": 1, "end_page": 1,
            "node_id": third["candidate_id"], "binding_mode": "visible",
            "boundary_info": {"start_line": 11, "heading_end_line": 12, "heading_action": "bind"},
            "children": [],
        },
    ]
    tree = {"schema_version": 2, "chapters": [{
        "title": "Chapter One", "level": 1, "start_page": 1, "end_page": 1,
        "node_id": chapter["candidate_id"], "binding_mode": "visible",
        "boundary_info": {"start_line": 1, "heading_end_line": 2, "heading_action": "bind"},
        "children": children,
    }]}
    _write_atx_review(
        tmp_path, manifest, candidates, rows, tree,
        toc_rows={(1, 1), (1, 3), (1, 11)},
        reasons={(1, 7): "This II is a body enumeration label, not a section heading"},
    )
    assert validate_structure_review(
        tmp_path, tree, expected_snapshot=_snapshot(tmp_path, pages, outline, pdf)
    ) == []

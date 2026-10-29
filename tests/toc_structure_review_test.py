import hashlib
import json
import pytest

from pdf2epub.refine.toc_structure_review import (
    build_structure_review,
    create_structure_review_snapshot,
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
        "schema_version": 1,
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
                                               "boundary_info": {"start_line": node_row["line"]}, "node_id": node_row["candidate_id"]}]}
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

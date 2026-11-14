from pdf2epub.refine.footnote_result_audit import audit_footnote_result
from pdf2epub.refine.footnote_candidates import candidate_for_block
from pdf2epub.refine.footnote_regions import build_page_footnote_regions


def test_apply_audit_checks_materialized_target_not_consumed_source_marker():
    source = {"chapter.md": "Body text<sup>37</sup>\n"}
    target = {"chapter.md": "Body text[^37]\n\n[^37]: A sufficiently long note definition body."}

    result = audit_footnote_result(
        source,
        records=[{"name": "chapter.md", "chapter_id": "chapter-1"}],
        target_texts=target,
        decisions=[{"file": "chapter.md", "role": "footnote_start", "key": "37"}],
    )

    assert not any(item["code"] == "unexplained_numeric_superscript" for item in result["findings"])


def test_apply_audit_reports_raw_superscript_left_in_materialized_target():
    source = {"chapter.md": "Body text<sup>37</sup>\n"}
    target = {"chapter.md": "Body text<sup>37</sup>\n"}

    result = audit_footnote_result(
        source,
        records=[{"name": "chapter.md", "chapter_id": "chapter-1"}],
        target_texts=target,
    )

    residual = [item for item in result["findings"] if item["code"] == "unexplained_numeric_superscript"]
    assert len(residual) == 1
    assert residual[0]["evidence_origin"] == "normalized_target"


def test_short_generic_superscript_candidate_stays_review_only_through_region_stage():
    block = {
        "order": 0,
        "label": "Text",
        "bbox": [80, 820, 920, 850],
        "html": "<sup>17</sup> Ebd.",
    }
    sidecar = {"page_box": [0, 0, 1000, 1000], "blocks": [block]}
    candidate = candidate_for_block(
        1, 0, block, sidecar, bottom_ratio=0.2,
        page_markdown="The claim<sup>17</sup> appears above.\n\n17. Ebd.",
    )

    assert candidate is not None
    assert candidate["leading_superscript_review"] is True
    regions = build_page_footnote_regions(
        1, sidecar, [candidate], [], bottom_ratio=0.2, auto_accept=True,
        continuous_regions=[],
    )
    assert len(regions) == 1
    assert regions[0]["disposition"] == "visual_audit_required"
    assert candidate["disposition"] == "review_required"


def test_explicit_footnote_label_keeps_existing_local_candidate_rules():
    block = {
        "order": 0,
        "label": "Footnote",
        "bbox": [80, 820, 920, 850],
        "html": "<sup>17</sup> A labeled note continues here.",
    }
    candidate = candidate_for_block(
        1, 0, block, {"page_box": [0, 0, 1000, 1000], "blocks": [block]},
        bottom_ratio=0.2,
    )
    assert candidate is not None
    assert "leading_superscript_review" not in candidate

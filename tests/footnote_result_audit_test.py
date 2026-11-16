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


def test_open_note_punctuation_is_only_a_visual_cue_at_mapped_physical_part_boundary():
    records = [
        {"name": "chapter.part1.md", "chapter_id": "ch1", "part_index": 1,
         "start_page": 4, "end_page": 5, "type": "chapter"},
        {"name": "chapter.part2.md", "chapter_id": "ch1", "part_index": 2,
         "start_page": 6, "end_page": 7, "type": "chapter"},
    ]
    result = audit_footnote_result(
        {"chapter.part1.md": "Body text.\n", "chapter.part2.md": "Continuation."},
        records=records,
        target_texts={
            "chapter.part1.md": "Body text.\n\n[^1]: A note that continues,\n",
            "chapter.part2.md": "Continuation.",
        },
        expected_notes=[{
            "chapter_id": "ch1", "target_file": "chapter.part1.md", "output_key": "1",
            "candidate_refs": [{"page": 5, "source": "primary", "block": 3}],
        }],
    )
    cue = [item for item in result["findings"] if item["code"] == "normalized_note_open_boundary_at_physical_page_break"]
    assert len(cue) == 1
    assert cue[0]["pages"] == [5, 6]
    assert cue[0]["requires_visual_review"] is True


def test_early_page_note_moved_to_later_target_unit_does_not_inherit_target_unit_page_range():
    records = [
        {"name": "chapter.part1.md", "chapter_id": "ch1", "part_index": 1,
         "start_page": 1, "end_page": 1, "type": "chapter"},
        {"name": "chapter.part2.md", "chapter_id": "ch1", "part_index": 2,
         "start_page": 5, "end_page": 5, "type": "chapter"},
        {"name": "chapter.part3.md", "chapter_id": "ch1", "part_index": 3,
         "start_page": 6, "end_page": 7, "type": "chapter"},
    ]
    result = audit_footnote_result(
        {"chapter.part1.md": "Source page one.", "chapter.part2.md": "", "chapter.part3.md": "Next."},
        records=records,
        target_texts={
            "chapter.part1.md": "Source page one.",
            "chapter.part2.md": "[^1]: An early note,\n",
            "chapter.part3.md": "Next.",
        },
        expected_notes=[{
            "chapter_id": "ch1", "target_file": "chapter.part2.md", "output_key": "1",
            "candidate_refs": [{"page": 1, "source": "primary", "block": 0}],
        }],
    )
    assert not any(item["code"] == "normalized_note_open_boundary_at_physical_page_break" for item in result["findings"])


def test_boundary_scan_uses_last_note_per_physical_page_and_per_chapter():
    records = [
        {"name": "ch1.part1.md", "chapter_id": "ch1", "part_index": 1, "start_page": 5, "end_page": 5, "type": "chapter"},
        {"name": "ch1.part2.md", "chapter_id": "ch1", "part_index": 2, "start_page": 6, "end_page": 8, "type": "chapter"},
        {"name": "ch2.part1.md", "chapter_id": "ch2", "part_index": 1, "start_page": 10, "end_page": 10, "type": "chapter"},
        {"name": "ch2.part2.md", "chapter_id": "ch2", "part_index": 2, "start_page": 11, "end_page": 12, "type": "chapter"},
    ]
    result = audit_footnote_result(
        {record["name"]: "body" for record in records},
        records=records,
        target_texts={
            "ch1.part1.md": "body",
            "ch1.part2.md": "[^1]: Earlier note,\n\n[^2]: Page-final note,\n",
            "ch2.part1.md": "body",
            "ch2.part2.md": "[^1]: Chapter-final note,\n",
        },
        expected_notes=[
            {"chapter_id": "ch1", "target_file": "ch1.part2.md", "output_key": "1", "candidate_refs": [{"page": 5, "source": "primary", "block": 1}]},
            {"chapter_id": "ch1", "target_file": "ch1.part2.md", "output_key": "2", "candidate_refs": [{"page": 5, "source": "primary", "block": 3}]},
            {"chapter_id": "ch2", "target_file": "ch2.part2.md", "output_key": "1", "candidate_refs": [{"page": 10, "source": "primary", "block": 2}]},
        ],
    )
    findings = [item for item in result["findings"] if item["code"] == "normalized_note_open_boundary_at_physical_page_break"]
    assert [item["pages"] for item in findings] == [[5, 6], [10, 11]]


def test_boundary_scan_binds_restarted_numbering_to_exact_output_key_and_source_unit():
    records = [
        {"name": "ch1.part1.md", "chapter_id": "ch1", "part_index": 1, "start_page": 1, "end_page": 1, "type": "chapter"},
        {"name": "ch1.part2.md", "chapter_id": "ch1", "part_index": 2, "start_page": 2, "end_page": 2, "type": "chapter"},
        {"name": "ch1.part3.md", "chapter_id": "ch1", "part_index": 3, "start_page": 3, "end_page": 4, "type": "chapter"},
    ]
    result = audit_footnote_result(
        {record["name"]: "body" for record in records},
        records=records,
        target_texts={
            "ch1.part1.md": "body",
            "ch1.part2.md": "body",
            "ch1.part3.md": "[^1]: First numbering run,\n\n[^fnm-number-1-o2]: Restarted note,\n",
        },
        expected_notes=[
            {"chapter_id": "ch1", "target_file": "ch1.part3.md", "output_key": "1",
             "candidate_refs": [{"page": 1, "source": "primary", "block": 4, "source_file": "ch1.part1.md"}]},
            {"chapter_id": "ch1", "target_file": "ch1.part3.md", "output_key": "fnm-number-1-o2",
             "candidate_refs": [{"page": 2, "source": "primary", "block": 2, "source_file": "ch1.part2.md"}]},
        ],
    )
    findings = [item for item in result["findings"] if item["code"] == "normalized_note_open_boundary_at_physical_page_break"]
    assert sorted(item["pages"] for item in findings) == [[1, 2], [2, 3]]


def test_comma_in_ordinary_body_and_endnote_mode_do_not_create_boundary_tasks():
    body = "A comma in ordinary prose, followed by more text."
    records = [
        {"name": "chapter.part1.md", "chapter_id": "ch1", "part_index": 1,
         "start_page": 4, "end_page": 5, "type": "chapter"},
        {"name": "chapter.part2.md", "chapter_id": "ch1", "part_index": 2,
         "start_page": 6, "end_page": 7, "type": "chapter"},
    ]
    normal = audit_footnote_result(
        {"chapter.part1.md": body, "chapter.part2.md": "More body."}, records=records,
        target_texts={"chapter.part1.md": body, "chapter.part2.md": "More body."},
    )
    endnotes = audit_footnote_result(
        {"chapter.part1.md": "[^1]: A note that continues,\n", "chapter.part2.md": "More."},
        records=records,
        target_texts={"chapter.part1.md": "[^1]: A note that continues,\n", "chapter.part2.md": "More."},
        mode="endnotes_only",
    )
    assert not any(item["code"] == "normalized_note_open_boundary_at_physical_page_break" for item in normal["findings"])
    assert not any(item["code"] == "normalized_note_open_boundary_at_physical_page_break" for item in endnotes["findings"])

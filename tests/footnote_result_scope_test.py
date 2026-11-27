from __future__ import annotations

from collections.abc import Mapping

from pdf2epub.refine.footnote_result_audit import audit_footnote_result


def _audit(source_texts: dict[str, str], records: list[dict] | None = None) -> dict:
    return audit_footnote_result(source_texts, records=records or [])


def _findings(report: Mapping, code: str) -> list[dict]:
    return [finding for finding in report["findings"] if finding["code"] == code]


def test_numbering_gap_is_reported_within_single_file_chapter_scope():
    report = _audit(
        {"chapter-1.md": "[^1]: First note.\n\n[^3]: Third note.\n"},
        [
            {
                "name": "chapter-1.md",
                "start_page": 1,
                "end_page": 4,
                "chapter_id": "toc-unit:chapter-1",
                "part_index": 1,
                "part_count": 1,
            }
        ],
    )

    gaps = _findings(report, "scoped_numbering_gap")
    assert len(gaps) == 1
    assert gaps[0]["neighbor_keys"] == [1, 3]
    assert gaps[0]["neighbor_files"] == ["chapter-1.md", "chapter-1.md"]
    assert gaps[0]["key"] == "2"


def test_numbering_gap_is_reported_across_parts_of_one_chapter():
    report = _audit(
        {
            "chapter-1.part1.md": "[^1]: First note.\n",
            "chapter-1.part2.md": "[^3]: Third note.\n",
        },
        [
            {
                "name": "chapter-1.part1.md",
                "start_page": 1,
                "end_page": 2,
                "chapter_id": "toc-unit:chapter-1",
                "part_index": 1,
                "part_count": 2,
            },
            {
                "name": "chapter-1.part2.md",
                "start_page": 3,
                "end_page": 4,
                "chapter_id": "toc-unit:chapter-1",
                "part_index": 2,
                "part_count": 2,
            },
        ],
    )

    gaps = _findings(report, "scoped_numbering_gap")
    assert len(gaps) == 1
    assert gaps[0]["neighbor_files"] == [
        "chapter-1.part1.md",
        "chapter-1.part2.md",
    ]


def test_numbering_resets_in_separate_chapters_are_not_merged():
    report = _audit(
        {
            "chapter-a.md": "[^1]: First note.\n",
            "chapter-b.md": "[^3]: Third note.\n",
        },
        [
            {
                "name": "chapter-a.md",
                "start_page": 1,
                "end_page": 2,
                "chapter_id": "toc-unit:chapter-a",
                "part_index": 1,
                "part_count": 1,
            },
            {
                "name": "chapter-b.md",
                "start_page": 3,
                "end_page": 4,
                "chapter_id": "toc-unit:chapter-b",
                "part_index": 1,
                "part_count": 1,
            },
        ],
    )

    assert _findings(report, "scoped_numbering_gap") == []


def test_physical_page_ranges_bound_gap_evidence_after_numbering_reset():
    report = _audit(
        {
            "page-5.md": "[^1]: First note on page five.\n[^3]: Third note on page five.\n",
            "page-6.md": "[^1]: First note on page six.\n[^3]: Third note on page six.\n",
        },
        [
            {
                "name": "page-5.md",
                "start_page": 5,
                "end_page": 5,
                "chapter_id": "toc-unit:chapter-1",
                "part_index": 1,
                "part_count": 2,
            },
            {
                "name": "page-6.md",
                "start_page": 6,
                "end_page": 6,
                "chapter_id": "toc-unit:chapter-1",
                "part_index": 2,
                "part_count": 2,
            },
        ],
    )

    gaps = _findings(report, "scoped_numbering_gap")
    assert len(gaps) == 2
    assert {tuple(finding["neighbor_files"]) for finding in gaps} == {
        ("page-5.md", "page-5.md"),
        ("page-6.md", "page-6.md"),
    }


def test_within_file_reset_without_line_page_evidence_is_reviewed_as_uncertain():
    report = _audit(
        {
            "chapter-1.md": (
                "[^4]: Earlier note in this chapter.\n"
                "[^1]: Later note with a lower number.\n"
            )
        },
        [
            {
                "name": "chapter-1.md",
                "start_page": 1,
                "end_page": 2,
                "chapter_id": "toc-unit:chapter-1",
                "part_index": 1,
                "part_count": 1,
            }
        ],
    )

    reset_reviews = _findings(report, "scoped_numbering_reset_uncertain")
    assert len(reset_reviews) == 1
    assert reset_reviews[0]["file"] == "chapter-1.md"
    assert reset_reviews[0].get("key") is None
    assert "reset" in reset_reviews[0]["detail"].lower()
    assert "no exact page or section boundary" in reset_reviews[0]["detail"].lower()
    assert "no missing key is inferred" in reset_reviews[0]["detail"].lower()


def test_short_ibid_and_ebd_footnotes_are_not_flagged_as_truncated():
    report = _audit(
        {"chapter.md": "[^1]: Ibid.\n\n[^2]: Ebd.\n"},
        [{"name": "chapter.md", "chapter_id": "toc-unit:chapter"}],
    )

    assert report["valid"] is True
    assert _findings(report, "normalized_note_truncated") == []
    assert _findings(report, "suspected_numbered_note_definition") == []


def test_inline_code_and_html_code_blocks_hide_numeric_note_markers():
    report = _audit(
        {
            "chapter.md": (
                "Example `[17] footnote` and `<sup>8</sup>` are code.\n\n"
                "<pre>\n[19] footnote\n<sup>20</sup>\n</pre>\n\n"
                "<code>[21] footnote</code>\n"
            )
        }
    )

    assert report["valid"] is True
    assert report["findings"] == []


def test_edition_year_and_equation_superscripts_are_not_note_candidates():
    report = _audit(
        {
            "chapter.md": (
                "Edition 1991<sup>2</sup>1994.\n"
                "x<sup>2</sup> + y<sup>2</sup> = z<sup>2</sup>.\n"
            )
        }
    )

    assert report["valid"] is True
    assert _findings(report, "unexplained_numeric_superscript") == []


def test_unexplained_inline_numeric_superscript_requires_review():
    report = _audit({"chapter.md": "The word<sup>3</sup> continues here.\n"})

    findings = _findings(report, "unexplained_numeric_superscript")
    assert len(findings) == 1
    assert findings[0]["file"] == "chapter.md"
    assert findings[0]["key"] == "3"


def test_marker_findings_on_one_line_have_distinct_task_ids():
    report = _audit(
        {"chapter.md": "Two orphan markers: <sup>4</sup> and <sup>5</sup>.\n"}
    )

    findings = _findings(report, "unexplained_numeric_superscript")
    assert {finding["key"] for finding in findings} == {"4", "5"}
    assert len({finding["task_id"] for finding in findings}) == len(findings)


def test_repeated_orphan_marker_occurrences_have_distinct_task_ids():
    report = _audit(
        {"chapter.md": "Repeated orphan markers: <sup>4</sup> and <sup>4</sup>.\n"}
    )

    findings = _findings(report, "unexplained_numeric_superscript")
    assert len(findings) == 2
    assert {finding["key"] for finding in findings} == {"4"}
    assert len({finding["task_id"] for finding in findings}) == 2


def test_markdown_definitions_keep_indented_continuation_blocks_together():
    report = _audit(
        {
            "chapter.md": (
                "[^1]: Ibid.\n"
                "    A short continuation belongs to this note.\n\n"
                "[^2]: Ebd.\n"
            )
        },
        [{"name": "chapter.md", "chapter_id": "toc-unit:chapter"}],
    )

    assert report["valid"] is True
    assert report["findings"] == []


def test_sequence_15_18_17_reports_one_gap_and_one_uncertain_reset_without_scanning_note_body():
    report = _audit(
        {
            "chapter.md": (
                "Body cites <sup>16</sup> and <sup>17</sup>.\n\n"
                "[^15]: Fifteenth note.\n\n"
                "[^18]: Eighteenth note quotes a marker <sup>17</sup>.\n\n"
                "[^17]: Seventeenth note after an unproven decrease.\n"
            ),
        },
        [{
            "name": "chapter.md", "start_page": 4, "end_page": 4,
            "chapter_id": "toc-unit:chapter", "part_index": 1, "part_count": 1,
        }],
    )

    gaps = _findings(report, "scoped_numbering_gap")
    resets = _findings(report, "scoped_numbering_reset_uncertain")
    assert len(gaps) == 1
    assert gaps[0]["neighbor_keys"] == [15, 18]
    assert gaps[0]["missing_keys"] == [16, 17]
    assert [item["key"] for item in gaps[0]["body_reference_occurrences"]] == ["16", "17"]
    assert len(resets) == 1
    assert resets[0]["neighbor_keys"] == [18, 17]
    # The same-numbered superscript inside a note definition is not a body
    # reference occurrence and cannot create another gap/reset finding.
    assert len(gaps) + len(resets) == 2


def test_page_bounded_numbering_reset_is_legal_after_a_gap():
    report = _audit(
        {
            "part-1.md": "[^15]: Fifteenth note.\n[^18]: Eighteenth note.\n",
            "part-2.md": "[^1]: A new page sequence begins.\n",
        },
        [
            {
                "name": "part-1.md", "start_page": 5, "end_page": 5,
                "chapter_id": "toc-unit:chapter", "part_index": 1, "part_count": 2,
            },
            {
                "name": "part-2.md", "start_page": 6, "end_page": 6,
                "chapter_id": "toc-unit:chapter", "part_index": 2, "part_count": 2,
            },
        ],
    )

    gaps = _findings(report, "scoped_numbering_gap")
    assert len(gaps) == 1
    assert gaps[0]["neighbor_keys"] == [15, 18]
    assert _findings(report, "scoped_numbering_reset_uncertain") == []


def test_target_segments_decode_scoped_keys_and_do_not_mask_post_reset_gaps():
    records = [
        {"name": "p1.md", "start_page": 1, "end_page": 2, "chapter_id": "ch1", "part_index": 1, "part_count": 2},
        {"name": "p2.md", "start_page": 3, "end_page": 4, "chapter_id": "ch1", "part_index": 2, "part_count": 2},
    ]
    source_texts = {
        "p1.md": "[^1]: Note 1.\n\n[^3]: Note 3.\n",
        "p2.md": "[^1]: Sec2 Note 1.\n\n[^3]: Sec2 Note 3.\n",
    }
    target_texts = {
        "p1.md": "[^1]: Note 1.\n\n[^fnm-number-2-o2]: Note 2.\n\n[^3]: Note 3.\n",
        "p2.md": "[^1]: Sec2 Note 1.\n\n[^3]: Sec2 Note 3.\n",
    }
    report = audit_footnote_result(source_texts, records=records, target_texts=target_texts)
    gaps = _findings(report, "scoped_numbering_gap")
    assert len(gaps) == 1
    assert gaps[0]["file"] == "p2.md"
    assert gaps[0]["missing_keys"] == [2]
    assert gaps[0]["evidence_origin"] == "source_markdown"


def test_confirmed_footnote_suppresses_uncertain_reset_by_task_or_source_task():
    source_texts = {"ch.md": "[^4]: Fourth.\n\n[^1]: Reset to first.\n"}
    records = [{"name": "ch.md", "start_page": 1, "end_page": 2, "chapter_id": "ch1", "part_index": 1, "part_count": 1}]
    initial = audit_footnote_result(source_texts, records=records)
    reset = _findings(initial, "scoped_numbering_reset_uncertain")[0]
    assert reset["evidence_origin"] == "source_markdown"

    suppressed_direct = audit_footnote_result(
        source_texts,
        records=records,
        current_exceptions=[{
            "task_id": reset["task_id"],
            "evidence_sha256": reset["evidence_sha256"],
            "outcome": "confirmed_footnote",
        }],
    )
    assert _findings(suppressed_direct, "scoped_numbering_reset_uncertain") == []

    suppressed_mapped = audit_footnote_result(
        source_texts,
        records=records,
        current_exceptions=[{
            "task_id": "fra-sidecar",
            "evidence_sha256": "sidecar-hash",
            "source_task_id": reset["task_id"],
            "source_evidence_sha256": reset["evidence_sha256"],
            "outcome": "confirmed_footnote",
        }],
    )
    assert _findings(suppressed_mapped, "scoped_numbering_reset_uncertain") == []

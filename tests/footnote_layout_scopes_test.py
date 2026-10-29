from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.refine.footnote_layout import layout_scope, table_adjacent
from pdf2epub.refine.footnote_regions import build_page_footnote_regions
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates


def _block(text: str, bbox: list[float], label: str = "Footnote") -> dict:
    return {"text": text, "label": label, "bbox": bbox}


def _candidate(block: int, key: str, *, label: str = "Footnote") -> dict:
    return {
        "block": block,
        "key": key,
        "label": label,
        "same_page_marker": True,
        "confidence": "high",
        "source_kind": "visual_ocr",
    }


def _regions(blocks: list[dict], candidates: list[dict]) -> list[dict]:
    sidecar = {"coordinate_system": "normalized", "blocks": blocks}
    return build_page_footnote_regions(
        1,
        sidecar,
        candidates,
        [],
        bottom_ratio=0.5,
        auto_accept=True,
        continuous_regions=[],
    )


def _prepare_page_scan(tmp_path: Path, blocks: list[dict]) -> dict:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_number": 1,
                "backend": "test",
                "coordinate_system": "normalized",
                "blocks": blocks,
            }
        ),
        encoding="utf-8",
    )
    (pages / "page_001.md").write_text("Body marker <sup>1</sup>\n", encoding="utf-8")
    return prepare_footnote_candidates(tmp_path)


def test_side_by_side_same_key_notes_remain_separate_and_require_review():
    blocks = [
        _block("1 Left note.", [70, 850, 450, 900]),
        _block("1 Right note.", [550, 850, 930, 900]),
    ]
    candidates = [_candidate(0, "1"), _candidate(1, "1")]

    regions = _regions(blocks, candidates)

    assert [region["block_indices"] for region in regions] == [[0], [1]]
    assert all(region["disposition"] == "visual_audit_required" for region in regions)
    assert all("duplicate_key_across_layout_scopes" in region["reasons"] for region in regions)
    assert [region["layout_scopes"][0]["placement"] for region in regions] == [
        "column_left",
        "column_right",
    ]


def test_single_column_labeled_notes_keep_existing_local_acceptance():
    blocks = [
        _block("Body", [100, 100, 900, 160], "Text"),
        _block("1 First note.", [100, 800, 450, 840]),
        _block("2 Second note.", [100, 850, 450, 890]),
    ]

    regions = _regions(blocks, [_candidate(1, "1"), _candidate(2, "2")])

    assert len(regions) == 1
    assert regions[0]["block_indices"] == [1, 2]
    assert regions[0]["disposition"] == "local_footnote_region"


def test_blocks_do_not_merge_from_vertical_overlap_without_horizontal_overlap():
    blocks = [
        _block("1 Left note.", [70, 850, 450, 900]),
        _block("1 Right note.", [550, 840, 930, 890]),
    ]

    regions = _regions(blocks, [_candidate(0, "1"), _candidate(1, "1")])

    assert [region["block_indices"] for region in regions] == [[0], [1]]


def test_page_point_geometry_keeps_separate_column_note_regions():
    blocks = [
        _block("1 Left note.", [70, 850, 450, 900]),
        _block("1 Right note.", [550, 850, 930, 900]),
    ]
    sidecar = {
        "coordinate_system": "page_points",
        "page_box": [0, 0, 1000, 1000],
        "blocks": blocks,
    }

    regions = build_page_footnote_regions(
        1,
        sidecar,
        [_candidate(0, "1"), _candidate(1, "1")],
        [],
        bottom_ratio=0.5,
        auto_accept=True,
        continuous_regions=[],
    )

    assert [region["block_indices"] for region in regions] == [[0], [1]]
    assert all(region["disposition"] == "visual_audit_required" for region in regions)


def test_full_width_footer_is_not_used_to_bridge_column_regions():
    blocks = [
        _block("1 Left note.", [70, 820, 450, 850]),
        _block("2 Full width footer.", [70, 860, 930, 890]),
        _block("1 Right note.", [550, 820, 930, 850]),
    ]

    regions = _regions(
        blocks,
        [_candidate(0, "1"), _candidate(1, "2"), _candidate(2, "1")],
    )

    assert [region["block_indices"] for region in regions] == [[0], [1], [2]]
    assert [region["layout_scopes"][0]["placement"] for region in regions] == [
        "column_left",
        "full_width",
        "column_right",
    ]


def test_mid_width_candidate_cannot_bridge_two_column_lanes():
    blocks = [
        _block("1 Left note.", [70, 820, 450, 850]),
        _block("2 Mid-width block.", [300, 850, 700, 880]),
        _block("1 Right note.", [550, 820, 930, 850]),
    ]

    regions = _regions(
        blocks,
        [_candidate(0, "1"), _candidate(1, "2"), _candidate(2, "1")],
    )

    assert [region["block_indices"] for region in regions] == [[0], [1], [2]]
    assert "layout_lane_bridge_ambiguity" in regions[1]["reasons"]
    assert all(
        region["disposition"] == "visual_audit_required" for region in regions
    )


def test_layout_scope_reports_body_lane_and_margin_note_separately():
    sidecar = {
        "coordinate_system": "normalized",
        "blocks": [
            _block("Body paragraph", [80, 400, 430, 470], "Text"),
            _block("1 Margin note", [840, 410, 970, 455], "Footnote"),
        ],
    }

    body_scope = layout_scope(sidecar, 0)
    margin_scope = layout_scope(sidecar, 1)

    assert body_scope["placement"] == "column_left"
    assert margin_scope["placement"] == "margin_right"
    assert margin_scope["scope_id"] != body_scope["scope_id"]


def test_table_adjacent_note_scope_exposes_nearby_table_block():
    sidecar = {
        "coordinate_system": "normalized",
        "blocks": [
            _block("Table 2. Results", [80, 700, 920, 755], "Table"),
            _block("a Note: values are rounded", [80, 760, 920, 790], "Footnote"),
        ],
    }

    scope = layout_scope(sidecar, 1)

    assert scope["placement"] == "full_width"
    assert {item["label"] for item in scope["nearby_blocks"]} == {"Table"}


def test_table_adjacency_requires_preceding_table_and_nonoverlapping_vertical_order():
    sidecar = {
        "coordinate_system": "normalized",
        "blocks": [
            _block("Table 2. Results", [100, 700, 900, 755], "Table"),
            _block("1 Note", [120, 760, 800, 790], "Text"),
        ],
    }
    reversed_blocks = {**sidecar, "blocks": list(reversed(sidecar["blocks"]))}
    overlapping_blocks = {
        **sidecar,
        "blocks": [
            sidecar["blocks"][0],
            _block("1 Note", [120, 750, 800, 790], "Text"),
        ],
    }

    assert table_adjacent(sidecar, 1)
    assert not table_adjacent(reversed_blocks, 1)
    assert not table_adjacent(overlapping_blocks, 1)


def test_explicit_footnote_label_away_from_tail_is_review_only():
    blocks = [
        _block("1 Possibly a margin note.", [100, 400, 450, 440]),
        _block("Ordinary page text continues below.", [100, 800, 900, 850], "Text"),
    ]

    regions = _regions(blocks, [_candidate(0, "1")])

    assert regions[0]["disposition"] == "visual_audit_required"
    assert "explicit_footnote_label_outside_page_tail" in regions[0]["reasons"]
    assert regions[0]["layout_scopes"][0]["placement"] == "column_left"


def test_scan_admits_narrow_explicit_margin_note_for_visual_review(tmp_path: Path):
    report = _prepare_page_scan(
        tmp_path,
        [
            _block("Body text", [100, 200, 800, 250], "Text"),
            _block("1 Margin note", [900, 400, 980, 450], "Footnote"),
        ],
    )

    candidates = report["pages"][0]["candidates"]
    assert [candidate["block"] for candidate in candidates] == [1]
    assert candidates[0]["confidence"] == "review"
    assert candidates[0]["layout_scope"]["placement"] == "margin_right"


def test_scan_keeps_full_width_body_label_out_of_margin_candidate_route(
    tmp_path: Path,
):
    report = _prepare_page_scan(
        tmp_path,
        [
            _block("36 Labeled but in body", [80, 200, 920, 250], "Footnote"),
            _block("Ordinary page content reaches the bottom", [80, 850, 920, 900], "Text"),
        ],
    )

    assert not any(page["candidates"] for page in report["pages"])


def test_scan_reviews_explicit_sup_letter_note_after_table(tmp_path: Path):
    report = _prepare_page_scan(
        tmp_path,
        [
            _block("Table 2. Results", [80, 300, 920, 400], "Table"),
            {
                **_block("Values rounded.", [80, 410, 920, 450], "Text"),
                "html": "<sup>a</sup> Values rounded.",
            },
            _block("Later body text continues here.", [80, 800, 920, 850], "Text"),
        ],
    )

    candidates = report["pages"][0]["candidates"]
    assert [candidate["block"] for candidate in candidates] == [1]
    assert candidates[0]["key"] == "fnm-letter-a"
    assert candidates[0]["confidence"] == "review"
    assert candidates[0]["disposition"] == "review_required"


def test_scan_marks_table_adjacent_candidate_for_review_with_table_context(
    tmp_path: Path,
):
    report = _prepare_page_scan(
        tmp_path,
        [
            _block("Table results", [100, 450, 900, 500], "Table"),
            _block("1 Note for table", [100, 505, 900, 550], "Text"),
        ],
    )

    candidate = report["pages"][0]["candidates"][0]
    assert candidate["block"] == 1
    assert candidate["confidence"] == "review"
    assert any(
        item["label"] == "Table"
        for item in candidate["layout_scope"]["nearby_blocks"]
    )

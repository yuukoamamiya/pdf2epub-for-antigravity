import pytest

from pdf2epub.refine.footnote_layout import body_flow_warning, region_layout_evidence
from pdf2epub.refine.footnote_regions import build_page_footnote_regions
from pdf2epub.refine.footnote_review_packet import build_review_packet, write_review_packet
from pdf2epub.refine.footnote_handoff import _write_unit_contexts
import json


def _sidecar(blocks):
    return {"coordinate_system": "normalized", "page_box": [0, 0, 1000, 1000], "blocks": blocks}


def _block(text, bbox, label="Text"):
    return {"text": text, "bbox": bbox, "label": label}


def test_other_column_text_does_not_count_as_body_after_left_note():
    sidecar = _sidecar([
        _block("TextLeft", [50, 100, 450, 700]),
        _block("1 note", [50, 800, 450, 850], "Footnote"),
        _block("TextRight", [550, 860, 950, 950]),
    ])
    assert body_flow_warning(sidecar, 1, [0.05, 0.8, 0.45, 0.85]) is None
    evidence = region_layout_evidence(sidecar, [1])
    assert evidence["body_continues_after_region"] is False


def test_same_lane_body_after_note_remains_a_warning():
    sidecar = _sidecar([
        _block("1 note", [50, 700, 450, 750], "Footnote"),
        _block("body resumes", [50, 800, 450, 950]),
    ])
    assert body_flow_warning(sidecar, 0, [0.05, 0.7, 0.45, 0.75]) == "ordinary_text_below_numeric_block"
    assert region_layout_evidence(sidecar, [0])["body_continues_after_region"] is True


def test_full_width_body_after_note_remains_a_warning():
    sidecar = _sidecar([
        _block("1 note", [50, 700, 450, 750], "Footnote"),
        _block("bridge prose", [50, 800, 950, 950]),
    ])
    assert body_flow_warning(sidecar, 0, [0.05, 0.7, 0.45, 0.75]) == "ordinary_text_below_numeric_block"
    assert region_layout_evidence(sidecar, [0])["body_continues_after_region"] is True


@pytest.mark.parametrize("wide_first", [True, False])
def test_aligned_wide_and_narrow_notes_join_region_in_either_order(wide_first):
    note_blocks = (
        [_block("1 First note.", [100, 700, 900, 720], "Footnote"), _block("2 Last note.", [100, 730, 500, 790], "Footnote")]
        if wide_first else
        [_block("1 First note.", [100, 700, 500, 720], "Footnote"), _block("2 Last note.", [100, 730, 900, 790], "Footnote")]
    )
    sidecar = _sidecar([_block("body", [100, 100, 900, 500]), *note_blocks])
    candidates = [
        {"block": 1, "key": "1", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
        {"block": 2, "key": "2", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
    ]
    regions = build_page_footnote_regions(
        1, sidecar, candidates, [], bottom_ratio=0.5, auto_accept=True,
        continuous_regions=[],
    )
    assert len(regions) == 1
    assert regions[0]["block_indices"] == [1, 2]


def test_full_width_bridge_does_not_fold_two_column_lanes():
    sidecar = _sidecar([
        _block("left note", [50, 800, 450, 840], "Footnote"),
        _block("bridge note", [50, 850, 950, 875], "Footnote"),
        _block("right note", [550, 800, 950, 840], "Footnote"),
    ])
    candidates = [
        {"block": i, "key": str(i), "label": "Footnote", "confidence": "review", "local_role": "footnote_start"}
        for i in range(3)
    ]
    regions = build_page_footnote_regions(
        1, sidecar, candidates, [], bottom_ratio=0.5, auto_accept=True,
        continuous_regions=[],
    )
    assert len(regions) >= 2
    assert any("layout_lane_bridge_ambiguity" in region.get("reasons", []) for region in regions)


def test_fold_does_not_transitively_absorb_unlabelled_body_candidate():
    sidecar = _sidecar([
        _block("body above", [100, 100, 900, 500]),
        _block("1 wide note", [100, 700, 900, 720], "Footnote"),
        _block("2 narrow note", [100, 730, 500, 750], "Footnote"),
        _block("302 ordinary text", [100, 760, 500, 820], "Text"),
    ])
    candidates = [
        {"block": 1, "key": "1", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
        {"block": 2, "key": "2", "label": "Footnote", "confidence": "review", "local_role": "footnote_start"},
        {"block": 3, "key": "302", "label": "Text", "confidence": "review", "local_role": "body"},
    ]
    regions = build_page_footnote_regions(
        1, sidecar, candidates, [], bottom_ratio=0.5, auto_accept=True,
        continuous_regions=[],
    )
    assert any(3 in region["block_indices"] and region["disposition"] == "visual_audit_required" for region in regions)


def test_packet_keeps_thin_high_confidence_neighbor_fact_and_resolvable_window_refs():
    left = {"page": 1, "source": "primary", "block": 2, "confidence": "review", "key": "1", "text": "1 uncertain note"}
    right = {"page": 2, "source": "secondary", "block": 4, "confidence": "high", "key": "1", "local_role": "footnote_continuation"}
    packet = build_review_packet({
        "schema_version": 4, "source_kind": "ocr", "ocr_evidence_mode": "two_ocr",
        "review_pages": [1], "visual_evidence": {"files": {"1": "ocr_review_images/page_001.png", "2": "ocr_review_images/page_002.png"}}, "pages": [
            {"page": 1, "candidates": [left], "sidecar": "pages/page_001.ocr.json",
             "footnote_regions": [{"region_id": "r1", "page": 1, "disposition": "visual_audit_required",
                                   "blocks": [{"page": 1, "source": "primary", "block": 2}]}]},
            {"page": 2, "secondary_only_candidates": [right], "secondary_sidecar": "ocr_secondary/page_002.ocr.json"},
        ],
        "cross_page_windows": [{"pages": [1, 2], "left_candidates": [left], "right_candidates": [right], "reason": "adjacent"}],
        "continuation_reviews": [], "bottom_detection": {},
    })
    refs = packet["cross_page_windows"][0]["candidate_refs"]
    assert set(refs) == {"1:primary:2", "2:secondary:4"}
    assert all(ref in packet["candidates"] for ref in refs)
    assert packet["candidates"]["2:secondary:4"]["confidence"] == "high"
    assert packet["visual_pages"][1]["secondary_sidecar"] == "ocr_secondary/page_002.ocr.json"
    assert packet["visual_pages"][1]["visual_file"] == "ocr_review_images/page_002.png"
    assert packet["regions"][0]["candidate_refs"] == ["1:primary:2"]


def test_multi_part_polish_contexts_are_small_and_part_scoped(tmp_path):
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "tree_progress.json").write_text(json.dumps({"units": [{
        "file": "chapter.part1.md", "part_files": ["chapter.part1.md", "chapter.part2.md"],
        "page_range": [1, 2],
    }]}), encoding="utf-8")
    huge = "verbose candidate body " * 10000
    report = {"pages": [{"page": 1, "sidecar": "pages/page_001.ocr.json", "candidates": [{
        "page": 1, "source": "primary", "block": 3, "key": "9", "confidence": "review",
        "local_role": "footnote_start", "text": huge,
    }]}], "cross_page_windows": [{"pages": [1, 2], "left_candidates": [{
        "page": 1, "source": "primary", "block": 3,
    }], "right_candidates": []}], "decisions": []}
    packet_path, packet_sha, _ = write_review_packet(tmp_path, report)
    files = _write_unit_contexts(
        tmp_path, report, review_packet=packet_path, review_packet_sha256=packet_sha,
    )
    contexts = {name: json.loads((tmp_path / relative).read_text(encoding="utf-8")) for name, relative in files.items()}
    assert contexts["chapter.part1.md"]["source_units"] == ["chapter.part1.md"]
    assert contexts["chapter.part2.md"]["source_units"] == ["chapter.part2.md"]
    assert contexts["chapter.part1.md"]["cross_page_windows"][0]["left_candidate_refs"] == ["1:primary:3"]
    assert set(contexts["chapter.part1.md"]["candidates"]) == {"1:primary:3"}
    assert all(len((tmp_path / relative).read_bytes()) < 10000 for relative in files.values())

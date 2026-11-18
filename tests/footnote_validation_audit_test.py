"""Pure footnote decision audits must stay deterministic and side-effect free."""

from __future__ import annotations

from pathlib import Path
import json

import pytest

from pdf2epub.refine.footnote_validation_audit import (
    audit_region_decisions,
    candidate_index,
    merge_region_and_flat_decisions,
    normalize_decisions,
    required_review_addresses,
    validate_continuation_traceability,
)
from pdf2epub.refine.footnote_validation_evidence import load_candidate_full_texts
from pdf2epub.workflow_contracts import sha256_file


def test_segment_validation_uses_full_hash_bound_sidecar_text(tmp_path: Path):
    from pdf2epub.refine.footnote_candidates import candidate_for_block

    full_text = "1 " + ("ordinary note text " * 25) + "TAIL SEGMENT"
    sidecar = {
        "coordinate_system": "normalized",
        "blocks": [{"text": full_text, "label": "Footnote", "bbox": [0.1, 0.9, 0.9, 0.98]}],
    }
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar_path.parent.mkdir()
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")
    candidate = candidate_for_block(
        1, 0, sidecar["blocks"][0], sidecar,
        bottom_ratio=0.5,
    )
    assert candidate is not None
    assert len(candidate["text"]) == 240
    report = {
        "ocr_evidence_mode": "single_ocr",
        "sidecar_sha256": {"pages/page_001.ocr.json": sha256_file(sidecar_path)},
        "pages": [{"page": 1, "candidates": [candidate]}],
    }
    full_texts, errors = load_candidate_full_texts(tmp_path, report, {(1, "primary", 0)})
    assert errors == []
    candidates = candidate_index(report)
    candidates[(1, "primary", 0)]["text"] = full_texts[(1, "primary", 0)]

    normalized, errors, _, _ = normalize_decisions([{
        "page": 1,
        "block": 0,
        "role": "body",
        "override": True,
        "segments": [{"text": "TAIL SEGMENT", "role": "footnote_start", "key": "1"}],
    }], candidates)

    assert errors == []
    assert normalized[0]["segments"][0]["text"] == "TAIL SEGMENT"


def test_required_review_addresses_preserves_fail_closed_malformed_entries():
    malformed = {
        "pages": [{"candidates": [{"confidence": "review", "page": 1}]}]
    }

    with pytest.raises(KeyError, match="block"):
        required_review_addresses(malformed)


def test_region_audit_reports_unknown_missing_duplicate_and_review_regions():
    decisions_data = {
        "region_audits": [
            {"region_id": "known", "disposition": "review_required", "decisions": []},
            {"region_id": "known", "disposition": "body_region", "decisions": []},
            {"region_id": "unknown", "disposition": "footnote_region", "decisions": []},
        ]
    }

    ids, nested, review, errors = audit_region_decisions(
        decisions_data, {"known", "missing"}
    )

    assert ids == {"known"}
    assert nested == []
    assert review == ["region:known"]
    assert errors == [
        "duplicate region audit: known",
        "region audit references unknown region: unknown",
        "missing region audit for missing",
    ]


def test_pure_exact_address_and_continuation_audits_do_not_write_files(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.chdir(tmp_path)
    report = {
        "pages": [
            {
                "candidates": [
                    {
                        "page": 2,
                        "block": 4,
                        "confidence": "review",
                        "key": "1",
                        "marker_label": "1",
                        "marker_identity": {"kind": "numeric"},
                    }
                ],
                "secondary_only_candidates": [],
                "audit_candidates": [],
            }
        ]
    }
    candidates = candidate_index(report)
    required = required_review_addresses(report)
    nested = [
        {
            "page": 2,
            "block": 4,
            "role": "footnote_continuation",
            "key": "unmatched",
        }
    ]
    merged = merge_region_and_flat_decisions(nested, nested)
    normalized, errors, review, seen = normalize_decisions(merged, candidates)
    continuation_errors = validate_continuation_traceability(candidates, normalized)

    assert required == {(2, "primary", 4)}
    assert errors == []
    assert review == []
    assert seen == required
    assert normalized[0]["key"] == "unmatched"
    assert continuation_errors == [
        "continuation 2:4 has no traceable footnote key"
    ]
    assert list(tmp_path.iterdir()) == []


def test_secondary_moved_decision_requires_explicit_text_and_safe_filename():
    address = (1, "secondary", 8)
    candidates = {address: {"page": 1, "block": 8}}
    normalized, errors, review, seen = normalize_decisions(
        [
            {
                "page": 1,
                "source": "secondary",
                "block": 8,
                "role": "footnote_definition",
                "key": "1",
                "text": "Note text.",
                "source_file": "../chapter.md",
                "primary_disposition": "absent",
            }
        ],
        candidates,
    )

    assert normalized == []
    assert errors == [
        "secondary-only footnote decision must include a safe source_file for 1:8"
    ]
    assert review == []
    assert seen == {address}


def test_reviewed_segments_must_be_unique_and_non_overlapping():
    address = (1, "primary", 0)
    candidates = {address: {
        "page": 1, "block": 0, "text": "Body sentence. 1 Note text.",
        "confidence": "high", "key": "1",
    }}
    base = {
        "page": 1, "source": "primary", "block": 0,
        "role": "body", "override": True,
    }
    valid, errors, _, _ = normalize_decisions([{
        **base,
        "segments": [
            {"role": "body", "text": "Body sentence."},
            {"role": "footnote_start", "key": "1", "text": "1 Note text."},
        ],
    }], candidates)
    assert errors == []
    assert len(valid) == 1

    _, errors, _, _ = normalize_decisions([{
        **base,
        "segments": [
            {"role": "body", "text": "Body sentence."},
            {"role": "footnote_start", "key": "1", "text": "sentence. 1"},
        ],
    }], candidates)
    assert errors == [
        "segmented decision has overlapping excerpts for 1:0"
    ]

    _, errors, _, _ = normalize_decisions([{
        **base,
        "segments": [{"role": "footnote_start", "key": "1", "text": "Note text."}],
    }], candidates)
    assert errors == []  # the exact reviewed note range may be narrower than the OCR block


def test_reviewed_body_override_splits_mixed_ocr_block_without_moving_body(tmp_path: Path):
    import json

    from pdf2epub.refine.footnote_apply_resolution import resolve_page_decisions

    text = "1 First note. Body sentence. 2 Second note."
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.ocr.json").write_text(json.dumps({
        "page_number": 1,
        "blocks": [{"order": 1, "label": "Footnote", "html": text}],
    }), encoding="utf-8")
    report = {"pages": [{"page": 1, "candidates": [{
        "page": 1, "source": "primary", "block": 0, "text": text,
        "confidence": "high", "key": "1", "local_role": "footnote_start",
    }]}]}
    decisions = [{
        "page": 1, "source": "primary", "block": 0,
        "role": "body", "override": True,
        "segments": [
            # Deliberately reversed. Apply order must follow exact source spans.
            {"role": "footnote_start", "key": "2", "text": "2 Second note."},
            {"role": "footnote_start", "key": "1", "text": "1 First note."},
            {"role": "body", "text": "Body sentence."},
        ],
    }]
    normalized, errors, _, _ = normalize_decisions(decisions, candidate_index(report))
    assert errors == []
    source_texts = {"chapter.md": text}
    result = resolve_page_decisions(
        tmp_path, report, {"decisions": normalized}, source_texts,
        [{"name": "chapter.md", "start_page": 1, "end_page": 1,
          "chapter_id": "chapter-1", "type": "chapter"}],
        "footnotes",
    )
    assert result.errors == []
    assert result.edits_by_file["chapter.md"] == [
        (0, len("1 First note."), ""),
        (len("1 First note. Body sentence. "), len(text), ""),
    ]
    assert [note["key"] for note in result.notes_by_chapter["chapter-1"]] == ["1", "2"]
    assert [note["parts"] for note in result.notes_by_chapter["chapter-1"]] == [
        ["First note."], ["Second note."],
    ]


def test_normalize_decisions_preserves_authorized_marker_label_override():
    from pdf2epub.refine.footnote_effective_decisions import compile_effective_decisions

    address = (1, "primary", 0)
    candidate = {
        "page": 1, "block": 0, "source": "primary", "confidence": "review",
        "local_role": "footnote_start", "key": "a", "marker_label": "α",
        "marker_identity": {"kind": "numeric"},
    }
    normalized, errors, _, _ = normalize_decisions([{
        "page": 1, "block": 0, "role": "footnote_start", "key": "a",
        "marker_label": "1", "override": True,
    }], {address: candidate})

    assert errors == []
    assert normalized[0]["marker_label"] == "1"
    assert normalized[0]["local_marker_label"] == "α"
    result = compile_effective_decisions(
        {"pages": [{"candidates": [candidate]}]}, normalized
    )
    assert result["errors"] == []
    assert result["decisions"][0]["marker_label"] == "1"
    assert result["decisions"][0]["override"] is True


def test_normalize_decisions_still_protects_non_numeric_printed_marker():
    address = (1, "primary", 0)
    candidate = {
        "page": 1, "block": 0, "source": "primary", "confidence": "review",
        "key": "roman-i", "marker_label": "i",
        "marker_identity": {"kind": "roman"},
    }
    normalized, errors, _, _ = normalize_decisions([{
        "page": 1, "block": 0, "role": "footnote_start", "key": "roman-i",
        "marker_label": "1", "override": True,
    }], {address: candidate})

    assert normalized == []
    assert any("preserve printed marker" in error for error in errors)

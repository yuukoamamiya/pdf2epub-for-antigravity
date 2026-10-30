"""Pure footnote decision audits must stay deterministic and side-effect free."""

from __future__ import annotations

from pathlib import Path

import pytest

from pdf2epub.refine.footnote_validation_audit import (
    audit_region_decisions,
    candidate_index,
    merge_region_and_flat_decisions,
    normalize_decisions,
    required_review_addresses,
    validate_continuation_traceability,
)


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

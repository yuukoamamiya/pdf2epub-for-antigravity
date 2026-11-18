from __future__ import annotations

from pdf2epub.refine.footnote_effective_decisions import candidate_index, compile_effective_decisions
from pdf2epub.refine.footnote_validation_audit import candidate_index as validation_candidate_index


def _report(page: int, block: int = 0, *, source: str = "primary") -> dict:
    rel = ("ocr_secondary" if source == "secondary" else "pages") + f"/page_{page:03d}.ocr.json"
    candidate = {
        "page": page,
        "source": source,
        "block": block,
        "confidence": "high",
        "local_role": "footnote_start",
        "key": str(page),
        "marker_label": str(page),
        "text": f"{page} synthetic footnote text",
    }
    field = "secondary_sidecar_sha256" if source == "secondary" else "sidecar_sha256"
    return {"pages": [{"candidates": [candidate]}], field: {rel: "sidecar-hash-a"}}


def test_matching_high_decisions_keep_all_review_details_for_regression_addresses():
    for page in (39, 213, 397):
        report = _report(page)
        explicit = [{
            "page": page,
            "source": "primary",
            "block": 0,
            "role": "footnote_start",
            "key": str(page),
            "marker_label": str(page),
            "review_note": f"review detail {page}",
            "confidence": "reviewed",
        }]

        result = compile_effective_decisions(report, explicit)

        assert result["errors"] == []
        assert len(result["decisions"]) == 1
        decision = result["decisions"][0]
        assert decision["review_note"] == f"review detail {page}"
        assert decision["local_role"] == "footnote_start"
        assert decision["review_role"] == "footnote_start"
        assert decision["effective_role"] == "footnote_start"
        assert decision["decision_source"] == "explicit_review"
        assert decision["candidate_evidence_id"]


def test_high_confidence_role_change_requires_explicit_override():
    report = _report(39)
    explicit = [{"page": 39, "block": 0, "role": "body", "key": "39"}]

    result = compile_effective_decisions(report, explicit)

    assert result["decisions"] == []
    assert any("role (footnote_start -> body)" in error and "override=true" in error for error in result["errors"])


def test_explicit_override_and_segmented_body_are_preserved():
    report = _report(213)
    explicit = [{
        "page": 213,
        "block": 0,
        "role": "body",
        "key": "213",
        "override": True,
        "segments": [{"text": "213 synthetic", "role": "body"}],
        "review_note": "split OCR block",
    }]

    result = compile_effective_decisions(report, explicit)

    assert result["errors"] == []
    decision = result["decisions"][0]
    assert decision["override"] is True
    assert decision["segments"][0]["text"] == "213 synthetic"
    assert decision["review_note"] == "split OCR block"
    assert decision["effective_role"] == "body"


def test_override_must_still_point_to_exact_candidate_and_key_changes_are_audited():
    report = _report(397)
    stale = compile_effective_decisions(report, [{
        "page": 397, "block": 9, "role": "body", "override": True,
    }])
    changed_key = compile_effective_decisions(report, [{
        "page": 397, "block": 0, "role": "footnote_start", "key": "new-key",
    }])

    assert any("non-candidate block" in error for error in stale["errors"])
    assert any("key (397 -> new-key)" in error and "override=true" in error for error in changed_key["errors"])


def test_secondary_only_effective_identity_uses_secondary_inventory_path():
    report = _report(39, source="secondary")
    result = compile_effective_decisions(report, [{
        "page": 39,
        "source": "secondary",
        "block": 0,
        "role": "footnote_start",
        "key": "39",
    }])

    assert result["errors"] == []
    assert result["decisions"][0]["candidate_evidence_id"]
    report["secondary_sidecar_sha256"]["ocr_secondary/page_039.ocr.json"] = "sidecar-hash-b"
    changed = compile_effective_decisions(report, [{
        "page": 39,
        "source": "secondary",
        "block": 0,
        "role": "footnote_start",
        "key": "39",
    }])
    assert changed["decisions"][0]["candidate_evidence_id"] != result["decisions"][0]["candidate_evidence_id"]


def test_confirmed_body_gap_merges_matching_ordinary_decision_with_provenance():
    from pdf2epub.refine.footnote_effective_decisions import project_confirmed_not_a_footnote

    report = _report(39)
    ordinary = {
        "page": 39, "source": "primary", "block": 0,
        "role": "body", "key": "39", "review_note": "already confirmed body",
    }
    projected_report, projected = project_confirmed_not_a_footnote(
        report,
        {
            "decisions": [ordinary],
            "continuation_reviews": [{
                "task_id": "gap-39", "outcome": "confirmed_not_a_footnote",
                "source_note": {"page": 39, "source": "primary", "block": 0},
            }],
        },
        [{"chapter_id": "chapter-39", "start_page": 39, "end_page": 39}],
    )
    compiled = compile_effective_decisions(projected_report, projected["decisions"])

    assert compiled["errors"] == []
    assert len(compiled["decisions"]) == 1
    effective = compiled["decisions"][0]
    assert effective["role"] == effective["effective_role"] == "body"
    assert effective["override"] is True
    assert effective["decision_source"] == "confirmed_not_a_footnote"
    assert effective["review_note"] == "already confirmed body"


def test_confirmed_body_gap_conflicts_with_ordinary_footnote_decision_explicitly():
    from pdf2epub.refine.footnote_effective_decisions import project_confirmed_not_a_footnote

    report = _report(213)
    projected_report, projected = project_confirmed_not_a_footnote(
        report,
        {
            "decisions": [{
                "page": 213, "source": "primary", "block": 0,
                "role": "footnote_start", "key": "213",
            }],
            "continuation_reviews": [{
                "task_id": "gap-213", "outcome": "confirmed_not_a_footnote",
                "source_note": {"page": 213, "source": "primary", "block": 0},
            }],
        },
        [],
    )
    compiled = compile_effective_decisions(projected_report, projected["decisions"])

    assert compiled["decisions"] == []
    assert any(
        "confirmed_not_a_footnote says body" in error
        and "explicit decision says footnote_start" in error
        for error in compiled["errors"]
    )


def test_confirmed_body_gap_accepts_exact_secondary_only_address():
    from pdf2epub.refine.footnote_effective_decisions import project_confirmed_not_a_footnote

    report = _report(397, source="secondary")
    projected_report, projected = project_confirmed_not_a_footnote(
        report,
        {"decisions": [], "continuation_reviews": [{
            "task_id": "secondary-gap", "outcome": "confirmed_not_a_footnote",
            "source_note": {"page": 397, "source": "secondary", "block": 0},
        }]},
        [],
    )
    compiled = compile_effective_decisions(projected_report, projected["decisions"])

    assert compiled["errors"] == []
    assert compiled["decisions"][0]["source"] == "secondary"
    assert compiled["decisions"][0]["decision_source"] == "confirmed_not_a_footnote"


def test_high_candidate_without_explicit_local_role_still_requires_role_override():
    report = {"pages": [{"candidates": [{
        "page": 39, "source": "primary", "block": 0, "confidence": "high",
        "label": "Footnote", "key": "39",
    }]}]}
    result = compile_effective_decisions(report, [{
        "page": 39, "block": 0, "role": "body", "key": "39",
    }])

    assert result["decisions"] == []
    assert any("role (footnote_start -> body)" in error and "override=true" in error for error in result["errors"])


def test_explicit_review_cannot_spoof_compiler_provenance():
    report = _report(213)
    result = compile_effective_decisions(report, [{
        "page": 213, "block": 0, "role": "footnote_start", "key": "213",
        "decision_source": "local_high", "effective_role": "body",
        "local_role": "body", "candidate_evidence_id": "fake",
    }])

    assert result["errors"] == []
    decision = result["decisions"][0]
    assert decision["decision_source"] == "explicit_review"
    assert decision["effective_role"] == "footnote_start"
    assert decision["local_role"] == "footnote_start"
    assert decision["candidate_evidence_id"] != "fake"


def test_duplicate_candidate_address_uses_one_shared_last_wins_projection():
    first = {
        "page": 39, "source": "primary", "block": 0,
        "confidence": "high", "local_role": "footnote_start", "key": "39",
    }
    last = {
        "page": 39, "source": "primary", "block": 0,
        "confidence": "high", "local_role": "footnote_definition", "key": "39",
    }
    report = {
        "pages": [{
            "candidates": [first],
            "audit_candidates": [last],
        }],
    }

    indexed = candidate_index(report)
    audited = validation_candidate_index(report)
    compiled = compile_effective_decisions(report, None)

    assert indexed == audited
    assert indexed[(39, "primary", 0)] == last
    assert compiled["errors"] == []
    assert compiled["decisions"][0]["role"] == "footnote_definition"

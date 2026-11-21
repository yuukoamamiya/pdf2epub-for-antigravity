import json
from pathlib import Path

import pytest

from pdf2epub.refine.footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.refine.illustration_prepare import (
    ILLUSTRATION_BINDINGS_SCHEMA_VERSION,
    apply_illustration_bindings,
    load_current_illustration_pages,
    prepare_illustration_subagent,
    validate_illustration_decisions,
)


def _prepare_illustration_without_candidates(root: Path) -> None:
    pages = root / "pages"
    pages.mkdir()
    (pages / "page_001.md").write_text("Ordinary body text.", encoding="utf-8")


def test_zero_illustration_candidates_write_not_applicable_receipt(tmp_path: Path):
    _prepare_illustration_without_candidates(tmp_path)
    prepare_illustration_subagent(tmp_path, book_title="Book", config={})

    validation = validate_illustration_decisions(tmp_path)
    assert validation["valid"] is True
    assert validation["status"] == "not_applicable"
    assert validation["candidate_pages"] == []
    assert validation["shortcut"]["kind"] == "zero_candidates"

    bindings = apply_illustration_bindings(tmp_path)
    assert bindings["status"] == "not_applicable"
    assert bindings["bindings"] == []
    assert load_current_illustration_pages(tmp_path) == set()


def test_zero_illustration_candidates_forward_roll_minor_schema(tmp_path: Path):
    _prepare_illustration_without_candidates(tmp_path)
    prepare_illustration_subagent(tmp_path, book_title="Book", config={})
    validate_illustration_decisions(tmp_path)
    apply_illustration_bindings(tmp_path)

    validation_path = tmp_path / "illustration_validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["schema_version"] = "1.1"
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    bindings_path = tmp_path / "illustration_bindings.json"
    bindings = json.loads(bindings_path.read_text(encoding="utf-8"))
    bindings["schema_version"] = "1.1"
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")

    assert load_current_illustration_pages(tmp_path) == set()

    bindings["schema_version"] = str(ILLUSTRATION_BINDINGS_SCHEMA_VERSION + 1)
    bindings_path.write_text(json.dumps(bindings), encoding="utf-8")
    with pytest.raises(ValueError, match="validated checkpoint"):
        load_current_illustration_pages(tmp_path)


def _write_zero_candidate_footnote_fixture(root: Path) -> None:
    pages = root / "pages"
    pages.mkdir()
    (pages / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "page_number": 1,
                "page_box": [0, 0, 1000, 1000],
                "blocks": [],
            }
        ),
        encoding="utf-8",
    )
    source = root / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text("Ordinary body text.\n", encoding="utf-8")
    (source / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "u1",
                        "page_range": [1, 1],
                        "file": "chapter_1.md",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    report = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_dir": "pages",
        "source_kind": "ocr",
        "ocr_evidence_mode": "single_ocr",
        "footnote_mode": "footnotes",
        "configured_footnote_mode": "footnotes",
        "footnote_mode_source": "config",
        "warnings": [],
        # Deliberately wrong: a zero-candidate shortcut must not hash sidecars.
        "sidecar_sha256": {"pages/page_001.ocr.json": "stale-hash"},
        "secondary_sidecar_sha256": {},
        "pages": [],
        "continuation_reviews": [],
        "review_pages": [],
        "review_required": False,
        "high_confidence_candidate_count": 0,
        "review_candidate_count": 0,
    }
    (root / "footnote_candidates.json").write_text(
        json.dumps(report), encoding="utf-8"
    )


def test_zero_footnote_candidates_write_not_applicable_receipt(tmp_path: Path):
    _write_zero_candidate_footnote_fixture(tmp_path)
    config = {"footnotes": {"mode": "footnotes"}}

    validation = validate_footnote_decisions(tmp_path, config=config)

    assert validation["valid"] is True
    assert validation["status"] == "not_applicable"
    assert validation["shortcut"]["kind"] == "zero_candidates"
    assert validation["decisions"] == []

    from pdf2epub.refine.footnote_apply_inputs import load_footnote_apply_inputs

    inputs = load_footnote_apply_inputs(tmp_path, config)
    assert inputs.validation["status"] == "not_applicable"


def test_zero_footnote_candidates_forward_roll_minor_schema(tmp_path: Path):
    _write_zero_candidate_footnote_fixture(tmp_path)
    config = {"footnotes": {"mode": "footnotes"}}
    validate_footnote_decisions(tmp_path, config=config)

    validation_path = tmp_path / "footnote_decision_validation.json"
    validation = json.loads(validation_path.read_text(encoding="utf-8"))
    validation["schema_version"] = f"{FOOTNOTE_PREPARE_SCHEMA_VERSION}.1"
    validation_path.write_text(json.dumps(validation), encoding="utf-8")

    from pdf2epub.refine.footnote_apply_inputs import load_footnote_apply_inputs

    assert load_footnote_apply_inputs(tmp_path, config).validation["status"] == "not_applicable"

    validation["schema_version"] = FOOTNOTE_PREPARE_SCHEMA_VERSION + 1
    validation_path.write_text(json.dumps(validation), encoding="utf-8")
    with pytest.raises(ValueError, match="older marker contract"):
        load_footnote_apply_inputs(tmp_path, config)

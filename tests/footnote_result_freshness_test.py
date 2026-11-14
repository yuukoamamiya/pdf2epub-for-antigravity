from __future__ import annotations

import json
from pathlib import Path

import pymupdf as fitz
import pytest

from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.workflow_contracts import sha256_file


def _prepare_review_case(tmp_path: Path) -> tuple[dict, dict, list[dict]]:
    body = "The claim<sup>17</sup> appears in this body paragraph."
    note = (
        "17. This is a note with citation to Smith 1999, page 14, "
        "and enough identifying text to match the exact sidecar block."
    )
    sidecar_note = "<sup>17</sup> " + note.split(". ", 1)[1]
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    source = source_dir / "chapter_1.md"
    source.write_text(f"{body}\n\n{note}\n", encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "index_path": [1],
                        "file": source.name,
                        "page_range": [1, 1],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (tmp_path / "toc_tree.json").write_text('{"chapters": []}', encoding="utf-8")

    pages = tmp_path / "pages"
    pages.mkdir()
    sidecar = {
        "schema_version": 1,
        "page_number": 1,
        "backend": "synthetic",
        "page_box": [0, 0, 1000, 1000],
        "blocks": [
            {"order": 0, "label": "Text", "bbox": [80, 100, 920, 130], "html": body},
            {"order": 1, "label": "Text", "bbox": [80, 820, 920, 860], "html": sidecar_note},
        ],
    }
    (pages / "page_001.ocr.json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )
    (pages / "page_001.md").write_text(f"{body}\n\n{note}\n", encoding="utf-8")

    pdf = fitz.open()
    pdf.new_page(width=612, height=792)
    pdf.save(tmp_path / "input_original.pdf")
    pdf.close()

    paths = prepare_footnote_subagent(tmp_path, book_title="Freshness Review")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    tasks = report["result_audit"]["findings"]
    assert tasks, json.dumps(report["result_audit"], indent=2)
    page_report = next(page for page in report["pages"] if page["page"] == 1)
    candidate = next(
        item for item in page_report["candidates"] if item["block"] == 1
    )
    review_candidates = [
        item for item in page_report["candidates"]
        if item.get("confidence") == "review"
        or item.get("disposition") == "review_required"
    ]
    decisions = [
        {
            "page": int(item["page"]),
            "source": str(item.get("source") or "primary"),
            "block": int(item["block"]),
            "role": "footnote_start",
            "key": str(item.get("key") or "17"),
            "confidence": "high",
        }
        for item in review_candidates
    ]
    if not any(item["page"] == 1 and item["block"] == 1 for item in decisions):
        decisions.append(
            {
                "page": 1,
                "source": "primary",
                "block": 1,
                "role": "footnote_start",
                "key": "17",
                "confidence": "high",
            }
        )
    audit_reviews = []
    for task in tasks:
        ref = next(
            (
                ref for ref in task.get("candidate_refs", [])
                if (int(ref.get("page", -1)), str(ref.get("source") or "primary"), int(ref.get("block", -1)))
                == (1, "primary", int(candidate["block"]))
            ),
            None,
        )
        assert ref is not None, task
        audit_reviews.append(
            {
                "task_id": task["task_id"],
                "evidence_sha256": task["evidence_sha256"],
                "outcome": "confirmed_footnote",
                "candidate_ref": {"page": 1, "source": "primary", "block": 1},
                "reason": "The rendered page image identifies this exact block as a footnote.",
                "evidence_pages": [1],
            }
        )
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": report["schema_version"],
                "decisions": decisions,
                "result_audit_reviews": audit_reviews,
            }
        ),
        encoding="utf-8",
    )
    return paths, report, tasks


def _validate_ready_case(tmp_path: Path) -> tuple[dict, dict, list[dict]]:
    paths, report, tasks = _prepare_review_case(tmp_path)
    image_path = tmp_path / "ocr_review_images" / "page_001.png"
    image_manifest = tmp_path / "ocr_review_images.json"
    assert image_path.is_file()
    assert image_manifest.is_file()
    assert sha256_file(image_path)
    assert json.loads(image_manifest.read_text(encoding="utf-8"))["source_sha256"]
    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation["errors"]
    return paths, report, tasks


def test_result_audit_rejects_pdf_change_with_saved_real_images(tmp_path):
    _, report, _ = _prepare_review_case(tmp_path)
    pdf_path = tmp_path / "input_original.pdf"
    image_path = tmp_path / "ocr_review_images" / "page_001.png"
    image_manifest = tmp_path / "ocr_review_images.json"
    saved_image = image_path.read_bytes()
    saved_manifest = image_manifest.read_bytes()
    saved_task = report["result_audit"]["findings"][0]

    changed = fitz.open(pdf_path)
    changed[0].insert_text((40, 50), "current source changed")
    changed_path = tmp_path / "changed.pdf"
    changed.save(changed_path)
    changed.close()
    changed_path.replace(pdf_path)

    assert image_path.read_bytes() == saved_image
    assert image_manifest.read_bytes() == saved_manifest
    result = validate_footnote_decisions(tmp_path)
    assert result["valid"] is False
    assert any(
        "stale for the current source PDF" in error
        for error in result["errors"]
    ), result["errors"]
    assert saved_task["visual_image_manifest_sha256"] == sha256_file(image_manifest)


@pytest.mark.parametrize("evidence", ["sidecar", "image"])
def test_result_audit_review_rejects_changed_exact_evidence(tmp_path, evidence):
    _prepare_review_case(tmp_path)
    if evidence == "sidecar":
        path = tmp_path / "pages" / "page_001.ocr.json"
    else:
        path = tmp_path / "ocr_review_images" / "page_001.png"
    path.write_bytes(path.read_bytes() + b"tampered")

    result = validate_footnote_decisions(tmp_path)
    assert result["valid"] is False
    assert any(
        "sidecar" in error.lower() or "image" in error.lower()
        for error in result["errors"]
    ), result["errors"]


@pytest.mark.parametrize("changed_tree", ["source", "target"])
def test_current_apply_receipt_rejects_changed_markdown_inventory(tmp_path, changed_tree):
    _validate_ready_case(tmp_path)
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")
    assert footnote_normalization_status(tmp_path)["current"] is True

    if changed_tree == "source":
        path = tmp_path / "ocr_markdown" / "chapter_1.md"
    else:
        path = tmp_path / "footnote_normalized" / "chapter_1.md"
    path.write_bytes(path.read_bytes() + b"\nchanged after apply\n")

    status = footnote_normalization_status(tmp_path)
    assert status["current"] is False
    assert any("result_audit" in item["code"] or "source" in item["code"] or "target" in item["code"] for item in status["failures"])


@pytest.mark.parametrize("changed_artifact", ["source", "candidate"])
def test_mid_apply_evidence_change_cannot_commit_success_receipt(
    tmp_path, monkeypatch, changed_artifact
):
    _validate_ready_case(tmp_path)
    import pdf2epub.refine.footnote_apply as apply_module

    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    candidates_path = tmp_path / "footnote_candidates.json"
    original_loader = apply_module.load_footnote_apply_inputs
    calls = 0

    def changing_loader(output_dir, config):
        nonlocal calls
        inputs = original_loader(output_dir, config)
        calls += 1
        if calls == 2:
            path = source_path if changed_artifact == "source" else candidates_path
            path.write_bytes(path.read_bytes() + b"\nchanged during apply\n")
        return inputs

    monkeypatch.setattr(apply_module, "load_footnote_apply_inputs", changing_loader)
    result = apply_footnote_normalization(tmp_path)

    assert result["valid"] is False
    assert "changed during apply" in " ".join(result["errors"]).lower()
    receipt = json.loads(
        (tmp_path / "footnote_normalization.json").read_text(encoding="utf-8")
    )
    assert receipt["valid"] is False
    assert footnote_normalization_status(tmp_path)["current"] is False

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pymupdf as fitz

from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_validation import validate_footnote_decisions


def _write_recovery_book(tmp_path: Path, monkeypatch) -> bytes:
    import pdf2epub.ocr_correction
    import pdf2epub.refine.footnote_report as report_module

    body = "The claim<sup>17</sup> appears in the discussion."
    note = (
        "17. This is a note with citation to Smith 1999, page 14, "
        "and enough identifying text to match this source block exactly."
    )
    next_list_item = "18. This numbered instruction continues the body list."
    existing_note_body = "A later body reference<sup>16</sup> appears here."
    existing_note = "16. An already identified page note provides an initial candidate."
    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    source_path = source_dir / "chapter_1.md"
    source_path.write_text(
        f"{body}\n\n{note}\n{next_list_item}\n\n"
        f"{existing_note_body}\n\n{existing_note}\n",
        encoding="utf-8",
    )
    source_bytes = source_path.read_bytes()
    (source_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "unit_id": "chapter_1",
                        "index_path": [1],
                        "file": "chapter_1.md",
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
            {"order": 1, "label": "Text", "bbox": [80, 200, 920, 230], "html": note},
            {"order": 2, "label": "Text", "bbox": [80, 240, 920, 270], "html": next_list_item},
            {"order": 3, "label": "Text", "bbox": [80, 500, 920, 530], "html": existing_note_body},
            {"order": 4, "label": "Footnote", "bbox": [80, 820, 920, 850], "html": existing_note},
        ],
    }
    (pages / "page_001.ocr.json").write_text(
        json.dumps(sidecar, ensure_ascii=False), encoding="utf-8"
    )
    (pages / "page_001.md").write_text(
        f"{body}\n\n{note}\n{next_list_item}\n\n"
        f"{existing_note_body}\n\n{existing_note}\n",
        encoding="utf-8",
    )

    pdf_path = tmp_path / "input_original.pdf"
    document = fitz.open()
    document.new_page(width=612, height=792)
    document.save(pdf_path)
    document.close()
    image_relative = "ocr_review_images/page_001.png"
    image_path = tmp_path / image_relative
    image_path.parent.mkdir(parents=True)
    image_path.write_bytes(b"synthetic visual page evidence")
    (tmp_path / "ocr_review_images.json").write_text(
        json.dumps({"source_sha256": "synthetic-source-pdf-hash"}),
        encoding="utf-8",
    )

    monkeypatch.setattr(
        report_module,
        "ensure_visual_review_images",
        lambda *_: {"available": True, "files": {"1": image_relative}},
    )
    monkeypatch.setattr(report_module, "ensure_region_review_images", lambda *_: {})
    monkeypatch.setattr(
        pdf2epub.ocr_correction, "review_images_are_current", lambda _path: True
    )
    return source_bytes


def test_missed_nonleading_note_recovers_through_exact_visual_review(tmp_path, monkeypatch):
    source_bytes = _write_recovery_book(tmp_path, monkeypatch)
    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))

    initial = prepare_footnote_candidates(tmp_path)
    page = next(row for row in initial["pages"] if row["page"] == 1)
    assert any(candidate["block"] == 4 for candidate in page["candidates"])
    assert all(candidate["block"] != 1 for candidate in page["candidates"])
    assert validate_footnote_decisions(tmp_path)["valid"] is True

    first_apply = apply_footnote_normalization(tmp_path)
    assert first_apply["valid"] is False
    assert first_apply["status"] == "review_required"
    pending_audit = json.loads(
        (tmp_path / "footnote_result_audit.json").read_text(encoding="utf-8")
    )
    assert pending_audit["valid"] is False
    assert {task["code"] for task in pending_audit["findings"]} >= {
        "unexplained_numeric_superscript",
        "suspected_numbered_note_definition",
    }
    pending_task = next(
        task for task in pending_audit["findings"]
        if task["code"] == "suspected_numbered_note_definition"
    )
    assert pending_task["key"] == "17"
    assert pending_task["file"] == "chapter_1.md"

    paths = prepare_footnote_subagent(tmp_path, book_title="Result Audit Recovery")
    prepared = json.loads(paths["report"].read_text(encoding="utf-8"))
    tasks = prepared["result_audit"]["findings"]
    task = next(
        (
            item for item in tasks
            if item["code"] == "suspected_numbered_note_definition"
            and item.get("source_address", {}).get("block") == 1
        ),
        None,
    )
    assert task is not None, json.dumps(tasks, indent=2)
    assert task["key"] == pending_task["key"]
    assert task["source_address"] == {
        "page": 1,
        "source": "primary",
        "block": 1,
        "sidecar": "pages/page_001.ocr.json",
        "sidecar_sha256": hashlib.sha256(sidecar_path.read_bytes()).hexdigest(),
    }
    page = next(row for row in prepared["pages"] if row["page"] == 1)
    admitted = next(
        candidate for candidate in page["candidates"]
        if candidate.get("result_audit_admitted") is True
    )
    assert (admitted["page"], admitted["source"], admitted["block"]) == (1, "primary", 1)
    assert admitted["key"] == "17"
    assert admitted["confidence"] == "review"
    assert task["candidate_refs"] == [{"page": 1, "source": "primary", "block": 1}]

    audit_reviews = [
        {
            "task_id": audit_task["task_id"],
            "evidence_sha256": audit_task["evidence_sha256"],
            "outcome": "confirmed_footnote",
            "candidate_ref": {"page": 1, "source": "primary", "block": 1},
            "reason": "The page image shows this numbered block is a page footnote, not a body list item.",
            "evidence_pages": [1],
        }
        for audit_task in tasks
        if audit_task.get("source_address", {}).get("block") == 1
    ]
    assert {review["task_id"] for review in audit_reviews} >= {task["task_id"]}
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps(
            {
                "schema_version": prepared["schema_version"],
                "decisions": [
                    {
                        "page": 1,
                        "source": "primary",
                        "block": 1,
                        "role": "footnote_start",
                        "key": "17",
                        "confidence": "high",
                    }
                ],
                "result_audit_reviews": audit_reviews,
            }
        ),
        encoding="utf-8",
    )

    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation["errors"]
    validated_review = next(
        item for item in validation["result_audit_reviews"]
        if item["task_id"] == task["task_id"]
    )
    assert validated_review["candidate_ref"] == {
        "page": 1,
        "source": "primary",
        "block": 1,
    }
    assert validated_review["evidence_sha256"] == task["evidence_sha256"]
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")

    target_path = tmp_path / "footnote_normalized" / "chapter_1.md"
    normalized = target_path.read_text(encoding="utf-8")
    assert "The claim[^17] appears in the discussion." in normalized
    assert "[^17]: This is a note with citation to Smith 1999, page 14" in normalized
    assert "18. This numbered instruction continues the body list." in normalized
    assert "17. This is a note" not in normalized
    assert source_path.read_bytes() == source_bytes

    current_audit = json.loads(
        (tmp_path / "footnote_result_audit.json").read_text(encoding="utf-8")
    )
    assert current_audit["valid"] is True
    assert current_audit["status"] == "validated"
    assert current_audit["findings"] == []
    reviewed_task = next(
        item for item in current_audit["reviewed_candidate_tasks"]
        if item["task_id"] == task["task_id"]
    )
    assert reviewed_task["evidence_sha256"] == task["evidence_sha256"]
    assert footnote_normalization_status(tmp_path)["current"] is True
    first_target_bytes = target_path.read_bytes()
    first_audit_bytes = (tmp_path / "footnote_result_audit.json").read_bytes()
    reapplied = apply_footnote_normalization(tmp_path)
    assert reapplied["valid"] is True, reapplied.get("errors")
    assert target_path.read_bytes() == first_target_bytes
    assert (tmp_path / "footnote_result_audit.json").read_bytes() == first_audit_bytes
    assert footnote_normalization_status(tmp_path)["current"] is True

    refreshed_paths = prepare_footnote_subagent(
        tmp_path, book_title="Result Audit Recovery"
    )
    refreshed_report = json.loads(
        refreshed_paths["report"].read_text(encoding="utf-8")
    )
    refreshed_page = next(
        row for row in refreshed_report["pages"] if row["page"] == 1
    )
    assert any(
        candidate.get("result_audit_admitted") is True
        and (candidate["page"], candidate["source"], candidate["block"])
        == (1, "primary", 1)
        for candidate in refreshed_page["candidates"]
    )
    refreshed_validation = validate_footnote_decisions(tmp_path)
    assert refreshed_validation["valid"] is True, refreshed_validation["errors"]
    refreshed_apply = apply_footnote_normalization(tmp_path)
    assert refreshed_apply["valid"] is True, refreshed_apply.get("errors")
    assert target_path.read_bytes() == first_target_bytes
    assert footnote_normalization_status(tmp_path)["current"] is True


def test_repeated_unresolved_result_review_escalates_after_second_attempt(tmp_path, monkeypatch):
    _write_recovery_book(tmp_path, monkeypatch)
    prepare_footnote_candidates(tmp_path)
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    assert apply_footnote_normalization(tmp_path)["status"] == "review_required"

    for attempt in (1, 2):
        paths = prepare_footnote_subagent(tmp_path, book_title="Repeated Result Review")
        report = json.loads(paths["report"].read_text(encoding="utf-8"))
        tasks = report["result_audit"]["findings"]
        assert tasks
        (tmp_path / "footnote_decisions.json").write_text(
            json.dumps({
                "schema_version": report["schema_version"],
                "decisions": [],
                "result_audit_reviews": [
                    {
                        "task_id": task["task_id"],
                        "evidence_sha256": task["evidence_sha256"],
                        "outcome": "review_required",
                        "reason": "The page evidence remains ambiguous after this review.",
                        "evidence_pages": task["pages"],
                    }
                    for task in tasks
                ],
            }),
            encoding="utf-8",
        )
        validation = validate_footnote_decisions(tmp_path)
        assert validation["valid"] is False
        expected = "review_required" if attempt == 1 else "human_review_required"
        assert validation["status"] == expected


def test_short_german_numbered_block_is_admitted_from_exact_generic_text_source(tmp_path, monkeypatch):
    _write_recovery_book(tmp_path, monkeypatch)
    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    source_path.write_text(
        source_path.read_text(encoding="utf-8").replace(
            "17. This is a note with citation to Smith 1999, page 14, and enough identifying text to match this source block exactly.",
            "17. Ebd.",
        ),
        encoding="utf-8",
    )
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"][1]["html"] = "17. Ebd."
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")
    (tmp_path / "pages" / "page_001.md").write_text(
        source_path.read_text(encoding="utf-8"), encoding="utf-8"
    )

    prepare_footnote_candidates(tmp_path)
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    assert apply_footnote_normalization(tmp_path)["status"] == "review_required"
    paths = prepare_footnote_subagent(tmp_path, book_title="Short German Note")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    task = next(
        item for item in report["result_audit"]["findings"]
        if item.get("source_address", {}).get("block") == 1
    )
    assert task["key"] == "17"
    assert task["context_status"] == "supplemental_candidate_admitted"
    assert task["source_address"]["page"] == 1


def test_exact_truncated_source_exception_survives_prepare_validate_apply(tmp_path, monkeypatch):
    _write_recovery_book(tmp_path, monkeypatch)
    source_text = "A discussion paragraph.\n\n[^85]: This note is cut off (5.435.\n"
    source_path = tmp_path / "ocr_markdown" / "chapter_1.md"
    source_path.write_text(source_text, encoding="utf-8")
    (tmp_path / "pages" / "page_001.md").write_text(source_text, encoding="utf-8")
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"] = [
        {"order": 0, "label": "Text", "bbox": [80, 100, 920, 130], "text": "A discussion paragraph."},
        {"order": 1, "label": "Text", "bbox": [80, 820, 920, 850], "text": "This note is cut off (5.435."},
    ]
    sidecar_path.write_text(json.dumps(sidecar, ensure_ascii=False), encoding="utf-8")

    prepare_footnote_candidates(tmp_path)
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    assert apply_footnote_normalization(tmp_path)["status"] == "review_required"
    paths = prepare_footnote_subagent(tmp_path, book_title="Truncated Note Review")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    task = next(
        item for item in report["result_audit"]["findings"]
        if item["code"] == "normalized_note_truncated"
    )
    assert task["source_address"]["block"] == 1
    assert task["pages"] == [1]
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps({
            "schema_version": report["schema_version"],
            "decisions": [],
            "result_audit_reviews": [{
                "task_id": task["task_id"],
                "evidence_sha256": task["evidence_sha256"],
                "outcome": "truncated_source_exception",
                "reason": "The page image confirms this is a literal truncated source excerpt.",
                "evidence_pages": task["pages"],
            }],
        }),
        encoding="utf-8",
    )
    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation["errors"]
    applied = apply_footnote_normalization(tmp_path)
    assert applied["valid"] is True, applied.get("errors")
    audit = json.loads((tmp_path / "footnote_result_audit.json").read_text(encoding="utf-8"))
    assert audit["valid"] is True
    assert audit["findings"] == []

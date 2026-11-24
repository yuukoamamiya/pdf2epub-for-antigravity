from __future__ import annotations

import hashlib
import json
import runpy
from pathlib import Path

import pytest

from pdf2epub.refine.footnote_handoff import (
    _populate_visual_context,
    _restore_successful_decision_addresses,
)
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates
from pdf2epub.refine.footnote_result_audit import audit_footnote_result
from pdf2epub.refine.footnote_result_review import validate_result_audit_reviews
from pdf2epub.workflow_contracts import sha256_file


def _content_hash(block):
    content = {key: str(block.get(key) or "") for key in ("text", "html", "label")}
    return hashlib.sha256(json.dumps(
        content, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")).hexdigest()


def _restore_decision(tmp_path, *, saved_file, old_block, sidecar_pages, records):
    exact_sidecars = {}
    for page, blocks in sidecar_pages.items():
        path = tmp_path / f"page_{page:03d}.json"
        path.write_text(json.dumps({"blocks": blocks}), encoding="utf-8")
        exact_sidecars[page] = {"primary": {"path": path.name, "sha256": sha256_file(path)}}
    decision = {"page": 1, "source": "primary", "block": 0, "role": "footnote_start", "key": "18"}
    review_data = {"decisions": [decision]}
    receipt = {
        "valid": True,
        "status": "validated",
        "decision_evidence": [{
            "page": 1,
            "source": "primary",
            "block": 0,
            "role": "footnote_start",
            "key": "18",
            "file": saved_file,
            "block_content_sha256": _content_hash(old_block),
        }],
    }
    report = {"pages": []}
    audit = {"findings": []}
    _restore_successful_decision_addresses(
        audit,
        report=report,
        output_dir=tmp_path,
        receipt=receipt,
        review_data=review_data,
        records=records,
        exact_sidecars=exact_sidecars,
        admitted_candidate_addresses=set(),
    )
    return decision, report, audit


def test_successful_decision_relocation_respects_part_and_owner_scope(tmp_path):
    old_block = {"text": "18. Exact note text.", "html": "18. Exact note text.", "label": "Footnote"}
    changed_block = {**old_block, "text": "Changed OCR text.", "html": "Changed OCR text."}

    progress = tmp_path / "ocr_markdown" / "tree_progress.json"
    progress.parent.mkdir()
    progress.write_text(json.dumps({"units": [{
        "name": "chapter.md",
        "page_range": [1, 2],
        "part_page_ranges": {"chapter.part1.md": [1, 1], "chapter.part2.md": [2, 2]},
    }]}), encoding="utf-8")
    part_decision, part_report, part_audit = _restore_decision(
        tmp_path,
        saved_file="chapter.part1.md",
        old_block=old_block,
        sidecar_pages={1: [changed_block], 2: [old_block]},
        records=[
            {"name": "chapter.part1.md", "start_page": 1, "end_page": 2},
            {"name": "chapter.part2.md", "start_page": 1, "end_page": 2},
        ],
    )
    assert (part_decision["page"], part_decision["block"]) == (1, 0)
    assert part_audit["findings"][0]["code"] == "successful_decision_evidence_changed"
    assert part_audit["findings"][0]["candidate_refs"] == [
        {"page": 1, "source": "primary", "block": 0}
    ]
    assert part_report["pages"][0]["candidates"][0]["block"] == 0

    unknown_decision, _, unknown_audit = _restore_decision(
        tmp_path,
        saved_file="missing-part.md",
        old_block=old_block,
        sidecar_pages={1: [changed_block, old_block]},
        records=[
            {"name": "overlap-a.md", "start_page": 1, "end_page": 1},
            {"name": "overlap-b.md", "start_page": 1, "end_page": 1},
        ],
    )
    assert (unknown_decision["page"], unknown_decision["block"]) == (1, 0)
    assert unknown_audit["findings"][0]["code"] == "successful_decision_evidence_changed"
    assert "owner unit cannot be resolved" in unknown_audit["findings"][0]["detail"]
    assert unknown_audit["findings"][0]["candidate_refs"] == [
        {"page": 1, "source": "primary", "block": 0}
    ]

    exact_decision, _, exact_audit = _restore_decision(
        tmp_path,
        saved_file="missing-part.md",
        old_block=old_block,
        sidecar_pages={1: [old_block, old_block]},
        records=[
            {"name": "overlap-a.md", "start_page": 1, "end_page": 1},
            {"name": "overlap-b.md", "start_page": 1, "end_page": 1},
        ],
    )
    assert (exact_decision["page"], exact_decision["block"]) == (1, 0)
    assert exact_audit["findings"] == []


def test_empty_candidate_refs_recover_all_task_page_candidate_sources(tmp_path, monkeypatch):
    import pdf2epub.refine.footnote_handoff as handoff

    sidecars = {}
    pages = []
    for page_number, candidates in (
        (1, [{"key": "36", "block": 0, "source": "primary"}]),
        (2, [
            {"key": "36", "block": 1, "source": "primary"},
            {"key": "36", "block": 2, "source": "secondary"},
        ]),
    ):
        primary = {"path": f"pages/page_{page_number:03d}.ocr.json", "sha256": f"p{page_number}"}
        secondary = {"path": f"ocr_secondary/page_{page_number:03d}.ocr.json", "sha256": f"s{page_number}"}
        sidecars[page_number] = {"primary": primary, "secondary": secondary}
        for entry in (primary, secondary):
            path = tmp_path / entry["path"]
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps({"blocks": [{"text": "candidate"} for _ in range(3)]}), encoding="utf-8")
        pages.append({
            "page": page_number,
            "candidates": [candidates[0]] if page_number == 1 else [],
            "audit_candidates": [candidates[0]] if page_number == 2 else [],
            "secondary_only_candidates": [candidates[1]] if page_number == 2 else [],
        })

    audit_task = {
        "task_id": "audit-task",
        "file": "chapter.md",
        "code": "scoped_numbering_gap",
        "key": "36",
        "candidate_refs": [],
        "task_pages": [1, 2],
        "pages": [1, 2],
    }
    report = {"pages": pages}
    image_dir = tmp_path / "ocr_review_images"
    image_dir.mkdir()
    for page_number in (1, 2):
        (image_dir / f"page_{page_number:03d}.png").write_bytes(f"image-{page_number}".encode())
    manifest = tmp_path / "ocr_review_images.json"
    manifest.write_text(json.dumps({"source_sha256": "current-pdf"}), encoding="utf-8")
    monkeypatch.setattr(
        handoff,
        "ensure_visual_review_images",
        lambda *_args: {"files": {"1": "ocr_review_images/page_001.png", "2": "ocr_review_images/page_002.png"}},
    )

    _populate_visual_context(
        {"findings": [audit_task]}, report,
        output_dir=tmp_path,
        records=[{"name": "chapter.md", "start_page": 1, "end_page": 2}],
        exact_sidecars=sidecars,
        config={},
    )

    assert audit_task["candidate_refs"] == [
        {"page": 1, "source": "primary", "block": 0},
        {"page": 2, "source": "primary", "block": 1},
        {"page": 2, "source": "secondary", "block": 2},
    ]
    assert audit_task["pages"] == [1, 2]
    assert audit_task["context_status"] == "exact_adjacent_note_pages_resolved"
    assert set(audit_task["visual_files"]) == {"1", "2"}


@pytest.mark.parametrize("role", ["footnote_start", "footnote_continuation"])
def test_confirmed_footnote_accepts_whole_block_role_override_without_segments(
    tmp_path, monkeypatch, role
):
    import pdf2epub.ocr_correction as correction_module

    monkeypatch.setattr(correction_module, "review_images_are_current", lambda *_args: True)
    target_dir = tmp_path / "footnote_normalized"
    target_dir.mkdir()
    target_text = "18. A complete footnote block from the exact sidecar.\n"
    target = target_dir / "chapter.md"
    target.write_text(target_text, encoding="utf-8")
    image_dir = tmp_path / "ocr_review_images"
    image_dir.mkdir()
    image = image_dir / "page_001.png"
    image.write_bytes(b"image")
    image_manifest = tmp_path / "ocr_review_images.json"
    image_manifest.write_text(json.dumps({"source_sha256": "pdf-hash"}), encoding="utf-8")
    task = {
        "task_id": "confirm-task",
        "file": "chapter.md",
        "code": "unexplained_numeric_superscript",
        "key": "18",
        "evidence_origin": "normalized_target",
        "evidence_sha256": "unused-by-target-lookup",
        "evidence_target_sha256": sha256_file(target),
        "candidate_refs": [{"page": 1, "source": "primary", "block": 4}],
        "pages": [1],
        "visual_files": {"1": {"path": "ocr_review_images/page_001.png", "sha256": sha256_file(image)}},
        "visual_image_manifest_sha256": sha256_file(image_manifest),
        "source_pdf_sha256": "pdf-hash",
    }
    task["evidence_sha256"] = hashlib.sha256(target_text.strip().encode()).hexdigest()
    raw_review = [{
        "task_id": task["task_id"],
        "evidence_sha256": task["evidence_sha256"],
        "outcome": "confirmed_footnote",
        "candidate_ref": {"page": 1, "source": "primary", "block": 4},
        "reason": "The page image confirms this whole block is a footnote.",
        "evidence_pages": [1],
    }]
    ordinary_decision = [{
        "page": 1,
        "source": "primary",
        "block": 4,
        "role": role,
        "override": True,
    }]

    normalized, errors = validate_result_audit_reviews(
        tmp_path, [task], raw_review, ordinary_decisions=ordinary_decision
    )

    assert errors == []
    assert normalized[0]["candidate_ref"] == {"page": 1, "source": "primary", "block": 4}


@pytest.mark.parametrize("ending", ["trans-", "unclosed ("])
def test_top_page_note_recall_is_review_only_and_avoids_nontext_roles(tmp_path, ending):
    helpers = runpy.run_path(str(Path(__file__).with_name("footnote_prepare_test.py")))
    block = helpers["_block"]
    write_sidecar = helpers["_write_sidecar"]
    long_note = "36 " + ("a long preceding note body with many words " * 20) + ending
    assert len(long_note) > 500
    write_sidecar(tmp_path, 1, [block(long_note, 0, label="Footnote", y0=820)])
    write_sidecar(tmp_path, 2, [
        block("A title that must not be treated as note continuation", 0, label="Title", y0=80),
        block("A table block", 1, label="Table", y0=130),
        block("A figure caption", 2, label="Caption", y0=170),
        block("Running header", 3, label="Page-Header", y0=35),
        block("continuation text from the previous note", 4, label="Text", y0=220),
    ])

    report = prepare_footnote_candidates(tmp_path)

    page = next(item for item in report["pages"] if item["page"] == 2)
    candidate = next(item for item in page["candidates"] if item["block"] == 4)
    assert candidate.get("top_note_recall") is True
    assert candidate["confidence"] == "review"
    assert candidate["disposition"] == "review_required"
    assert candidate.get("auto_continuation") is not True
    assert candidate.get("local_role") is None
    assert {item["block"] for item in page["candidates"]}.isdisjoint({0, 1, 2, 3})
    assert all(item.get("top_note_recall") is not True for item in page["candidates"] if item["block"] != 4)
    audit_manifest = json.loads((tmp_path / "footnote_visual_audit_manifest.json").read_text(encoding="utf-8"))
    assert not any(str(task.get("task_id", "")).startswith("continuation-gap-") for task in audit_manifest["tasks"])
    assert not any(4 in task.get("candidate_block_indices", []) for task in audit_manifest["tasks"] if task.get("kind") == "continuation_gap")


@pytest.mark.parametrize("key", ["40", "80"])
def test_degree_ambiguity_hint_survives_source_and_target_audits_without_changing_gate(key):
    from pdf2epub.refine.footnote_result_audit import _task_id

    source_text = f"The reading measures 1830<sup>{key}</sup>."
    raw_audit = audit_footnote_result({"chapter.md": source_text}, mode="footnotes")
    raw_finding = next(
        item for item in raw_audit["findings"]
        if item["code"] == "unexplained_numeric_superscript"
    )
    target_audit = audit_footnote_result(
        {"chapter.md": source_text},
        target_texts={"chapter.md": source_text},
        mode="footnotes",
    )
    target_finding = next(
        item for item in target_audit["findings"]
        if item["code"] == "unexplained_numeric_superscript"
    )

    assert raw_finding["review_hint"]
    assert "degree symbol" in raw_finding["review_hint"]
    assert target_finding.get("review_hint") == raw_finding["review_hint"]
    assert raw_finding["task_id"] == _task_id(
        raw_finding["code"], raw_finding["file"], raw_finding["line"],
        raw_finding["evidence_sha256"], f"occurrence={raw_finding['occurrence_sha256']}",
    )
    assert target_finding["task_id"] == raw_finding["task_id"]
    assert raw_finding["requires_visual_review"] is True
    assert target_finding["requires_visual_review"] is True

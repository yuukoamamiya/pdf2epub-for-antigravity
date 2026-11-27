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


def test_restore_admitted_candidates_drops_stale_source_markdown_review(tmp_path):
    from pdf2epub.refine.footnote_handoff import _restore_admitted_candidates

    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    unit_path = ocr_dir / "chapter_1.md"
    unit_path.write_text("Updated body text where the original excerpt is gone.\n", encoding="utf-8")

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    block = {"text": "18. Note block", "html": "18. Note block", "label": "Footnote"}
    sidecar_path = pages_dir / "page_001.ocr.json"
    sidecar_path.write_text(json.dumps({"blocks": [block]}), encoding="utf-8")
    sidecar_sha = sha256_file(sidecar_path)
    exact_sidecars = {1: {"primary": {"path": "pages/page_001.ocr.json", "sha256": sidecar_sha}}}

    reviewed_task = {
        "task_id": "task-stale-source",
        "code": "unexplained_numeric_superscript",
        "file": "chapter_1.md",
        "evidence_origin": "source_markdown",
        "evidence_file_sha256": "old-file-hash",
        "block_content_sha256": _content_hash(block),
        "source_address": {"page": 1, "source": "primary", "block": 0},
    }
    polish_task = {
        "task_id": "task-stale-polish",
        "code": "polish_semantic_issue",
        "file": "chapter_1.md",
        "evidence_origin": "source_markdown",
        "evidence_file_sha256": "old-file-hash",
        "evidence_excerpt": "Missing excerpt from earlier OCR",
        "polish_issue": {"raw_source_excerpt_sha256": hashlib.sha256(b"Missing excerpt from earlier OCR").hexdigest()},
        "block_content_sha256": _content_hash(block),
        "source_address": {"page": 1, "source": "primary", "block": 0},
    }
    audit = {"findings": [], "reviewed_candidate_tasks": []}
    report = {"pages": [{"page": 1, "candidates": []}]}
    prior_review_data = {
        "result_audit_reviews": [
            {"task_id": "task-stale-source"},
            {"task_id": "task-stale-polish"},
        ]
    }
    _restore_admitted_candidates(
        audit,
        report,
        output_dir=tmp_path,
        prior_result_audit={"reviewed_candidate_tasks": [reviewed_task, polish_task]},
        exact_sidecars=exact_sidecars,
        prior_review_data=prior_review_data,
        records=[{"name": "chapter_1.md", "start_page": 1, "end_page": 1}],
    )

    assert audit["reviewed_candidate_tasks"] == []
    assert prior_review_data["result_audit_reviews"] == []


def test_admit_truncated_continuation_blocks_recalls_across_parts_and_normalizes_pixels(tmp_path, monkeypatch):
    import pdf2epub.refine.footnote_handoff as handoff

    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    (ocr_dir / "ch1.part1.md").write_text("Body[^1]\n", encoding="utf-8")
    (ocr_dir / "ch1.part2.md").write_text("Continued prose.\n", encoding="utf-8")
    source_inv = {"ch1.part1.md": sha256_file(ocr_dir / "ch1.part1.md"), "ch1.part2.md": sha256_file(ocr_dir / "ch1.part2.md")}

    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    note_text = "This truncated footnote on page 1 ends mid-word with trans-"
    p1_sidecar = {
        "page_box": [0, 0, 2000, 3000],
        "coordinate_system": "page",
        "blocks": [{"order": 0, "label": "Footnote", "bbox": [100, 2400, 1800, 2700], "html": note_text}],
    }
    p2_sidecar = {
        "page_box": [0, 0, 2000, 3000],
        "coordinate_system": "page",
        "blocks": [
            {"order": 0, "label": "Text", "bbox": [100, 200, 1800, 400], "html": "<p>lation from the previous page.</p>"},
            {"order": 1, "label": "Text", "bbox": [100, 1400, 1800, 1500], "html": "<p>Continuation before footer.</p>"},
            {"order": 2, "label": "Footnote", "bbox": [100, 2500, 1800, 2700], "html": "2. Next footnote."},
        ],
    }
    (pages_dir / "page_001.ocr.json").write_text(json.dumps(p1_sidecar), encoding="utf-8")
    (pages_dir / "page_002.ocr.json").write_text(json.dumps(p2_sidecar), encoding="utf-8")
    exact_sidecars = {
        1: {"primary": {"path": "pages/page_001.ocr.json", "sha256": sha256_file(pages_dir / "page_001.ocr.json")}},
        2: {"primary": {"path": "pages/page_002.ocr.json", "sha256": sha256_file(pages_dir / "page_002.ocr.json")}},
    }

    monkeypatch.setattr(
        handoff,
        "ensure_visual_review_images",
        lambda _dir, pages, _cfg=None: {"files": {str(p): f"ocr_review_images/page_{int(p):03d}.png" for p in pages}},
    )

    report = {
        "review_pages": [5],
        "visual_evidence": {"files": {"5": "ocr_review_images/page_005.png"}},
        "pages": [
            {"page": 1, "candidates": []},
            {"page": 2, "candidates": [{"page": 2, "source": "primary", "block": 2, "bbox": [0.05, 0.83, 0.9, 0.9]}]},
        ],
    }
    records = [
        {"name": "ch1.part1.md", "chapter_id": "ch1", "start_page": 1, "end_page": 1},
        {"name": "ch1.part2.md", "chapter_id": "ch1", "start_page": 2, "end_page": 2},
    ]
    handoff._admit_truncated_continuation_blocks(
        report,
        output_dir=tmp_path,
        prior_result_audit={
            "source_sha256": source_inv,
            "findings": [{"code": "normalized_note_truncated", "file": "ch1.part1.md", "evidence_excerpt": f"[^1]: {note_text}"}],
        },
        records=records,
        exact_sidecars=exact_sidecars,
    )

    p2_cands = {c["block"]: c for c in report["pages"][1]["candidates"]}
    assert 0 in p2_cands and 1 in p2_cands
    assert p2_cands[0]["result_audit_admitted"] is True
    assert p2_cands[1]["result_audit_admitted"] is True
    assert "lation from the previous page." in p2_cands[0]["text"]
    assert p2_cands[0]["bbox"] == [0.05, 200 / 3000, 0.9, 400 / 3000]
    assert report["review_pages"] == [2, 5]
    assert report["pages"][1]["visual_file"] == "ocr_review_images/page_002.png"


def test_resolve_result_audit_deduplicates_findings_and_reviewed_tasks(tmp_path, monkeypatch):
    import pdf2epub.refine.footnote_handoff as handoff

    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    unit_path = ocr_dir / "chapter_1.md"
    unit_path.write_text("Body.\n", encoding="utf-8")
    (ocr_dir / "tree_progress.json").write_text(json.dumps({
        "units": [{"unit_id": "ch1", "index_path": [1], "file": "chapter_1.md", "page_range": [1, 1]}]
    }), encoding="utf-8")
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.ocr.json").write_text(json.dumps({"blocks": []}), encoding="utf-8")

    monkeypatch.setattr(handoff, "_carry_prior_audit_findings", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_restore_successful_decision_addresses", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_attach_polish_semantic_tasks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_restore_admitted_candidates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_resolve_truncated_definitions", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_admit_truncated_continuation_blocks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_admit_leading_superscripts", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_add_supplemental_note_tasks", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_bind_supplemental_candidates", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(handoff, "_bind_heading_tasks", lambda *_args, **_kwargs: None)

    def _inject_dups(audit, *_args, **_kwargs):
        audit["findings"] = [
            {"task_id": "dup-1", "file": "chapter_1.md", "pages": []},
            {"task_id": "dup-1", "file": "chapter_1.md", "pages": [], "extra": "kept"},
        ]
        audit["reviewed_candidate_tasks"] = [
            {"task_id": "dup-1", "file": "chapter_1.md"},
            {"task_id": "keep-2", "file": "chapter_1.md"},
        ]

    monkeypatch.setattr(handoff, "_link_marker_candidates", _inject_dups)
    monkeypatch.setattr(handoff, "ensure_visual_review_images", lambda *_args, **_kwargs: {"files": {}})

    ctx = handoff._PrepareContext(
        report={"pages": [{"page": 1, "sidecar": "pages/page_001.ocr.json"}]},
        selected_page_dir=pages_dir,
        model="inherit",
        records=[{"name": "chapter_1.md", "chapter_id": "ch1", "start_page": 1, "end_page": 1}],
        source_texts={"chapter_1.md": "Body.\n"},
        prior_review_data={},
        prior_audit_reviews=[],
        prior_result_audit={},
        prior_normalization={},
        current_target_inventory={},
        audit={"findings": [], "reviewed_candidate_tasks": []},
    )
    audit = handoff._resolve_result_audit(ctx, output_dir=tmp_path, config={}, selected_page_dir=pages_dir)

    assert [f["task_id"] for f in audit["findings"]] == ["dup-1"]
    assert audit["findings"][0]["extra"] == "kept"
    assert [t["task_id"] for t in audit["reviewed_candidate_tasks"]] == ["keep-2"]


def test_failed_apply_rollback_preserves_target_audit_findings_across_prepare_and_validate(tmp_path, monkeypatch):
    import pdf2epub.ocr_correction as correction_module
    from pdf2epub.refine.footnote_handoff import _carry_prior_audit_findings
    from pdf2epub.refine.footnote_source import markdown_inventory
    from pdf2epub.refine.footnote_versions import capture_last_good, preserve_failed_attempt, recover_interrupted_apply, mark_apply_running

    monkeypatch.setattr(correction_module, "review_images_are_current", lambda *_args: True)
    (tmp_path / "ocr_markdown").mkdir()
    (tmp_path / "ocr_markdown" / "ch1.md").write_bytes(b"Body.\n")
    (tmp_path / "footnote_normalized").mkdir()
    good_text = "Body.\n\n[^1]: Baseline note.\n"
    (tmp_path / "footnote_normalized" / "ch1.md").write_bytes(good_text.encode("utf-8"))
    good_inv = markdown_inventory(tmp_path / "footnote_normalized")
    (tmp_path / "footnote_normalization.json").write_text(json.dumps({
        "schema_version": 8, "valid": True, "status": "validated", "target_sha256": good_inv,
    }), encoding="utf-8")
    baseline_ver = capture_last_good(tmp_path)
    assert baseline_ver is not None

    failed_text = "Body<sup>9</sup>.\n\n[^1]: Baseline note.\n"
    mark_apply_running(tmp_path, {"ch1.md": failed_text}, baseline_ver)
    (tmp_path / "footnote_normalized" / "ch1.md").write_bytes(failed_text.encode("utf-8"))
    failed_inv = markdown_inventory(tmp_path / "footnote_normalized")
    failed_audit = audit_footnote_result({"ch1.md": "Body<sup>9</sup>.\n"}, target_texts={"ch1.md": failed_text})
    finding = dict(failed_audit["findings"][0])
    finding["pages"] = [1]
    img_dir = tmp_path / "ocr_review_images"
    img_dir.mkdir()
    img = img_dir / "page_001.png"
    img.write_bytes(b"png")
    img_manifest = tmp_path / "ocr_review_images.json"
    img_manifest.write_text(json.dumps({"source_sha256": "pdf"}), encoding="utf-8")
    finding["visual_files"] = {"1": {"path": "ocr_review_images/page_001.png", "sha256": sha256_file(img)}}
    finding["visual_image_manifest_sha256"] = sha256_file(img_manifest)
    finding["source_pdf_sha256"] = "pdf"
    attempt_dir = preserve_failed_attempt(tmp_path)
    assert attempt_dir is not None
    assert recover_interrupted_apply(tmp_path) is True
    (tmp_path / "footnote_normalization_attempt.json").write_text(json.dumps({
        "schema_version": 8, "valid": False, "status": "review_required", "attempt_evidence_dir": attempt_dir,
    }), encoding="utf-8")

    carried_audit = {"findings": []}
    _carry_prior_audit_findings(
        {
            "source_sha256": markdown_inventory(tmp_path / "ocr_markdown"),
            "target_sha256": failed_inv,
            "findings": [finding],
        },
        carried_audit,
        output_dir=tmp_path,
        current_target_inventory=markdown_inventory(tmp_path / "footnote_normalized"),
        records=[{"name": "ch1.md", "start_page": 1, "end_page": 1}],
        report={"footnote_mode": "footnotes"},
        config={},
    )
    assert len(carried_audit["findings"]) == 1
    carried_task = carried_audit["findings"][0]
    assert carried_task["evidence_target_sha256"] == failed_inv["ch1.md"]

    reviews, errors = validate_result_audit_reviews(
        tmp_path,
        [carried_task],
        [{
            "task_id": carried_task["task_id"],
            "evidence_sha256": carried_task["evidence_sha256"],
            "outcome": "legitimate_non_footnote",
            "reason": "Ordinal superscript in body.",
            "evidence_pages": [1],
        }],
    )
    assert errors == []
    assert len(reviews) == 1


def test_confirmed_reset_and_unaddressed_reset_survive_validation_and_next_prepare(tmp_path, monkeypatch):
    import pdf2epub.ocr_correction as correction_module
    from pdf2epub.refine.footnote_result_review import validate_applied_result_audit_reviews

    monkeypatch.setattr(correction_module, "review_images_are_current", lambda *_args: True)
    (tmp_path / "ocr_markdown").mkdir()
    (tmp_path / "ocr_markdown" / "ch1.md").write_text("[^4]: Fourth.\n\n[^1]: Reset.\n", encoding="utf-8")
    img_dir = tmp_path / "ocr_review_images"
    img_dir.mkdir()
    img = img_dir / "page_001.png"
    img.write_bytes(b"png")
    img_manifest = tmp_path / "ocr_review_images.json"
    img_manifest.write_text(json.dumps({"source_sha256": "pdf"}), encoding="utf-8")

    audit = audit_footnote_result(
        {"ch1.md": "[^4]: Fourth.\n\n[^1]: Reset.\n"},
        records=[{"name": "ch1.md", "start_page": 1, "end_page": 2, "chapter_id": "ch1", "part_index": 1, "part_count": 1}],
    )
    task = dict(audit["findings"][0])
    task["evidence_file_sha256"] = sha256_file(tmp_path / "ocr_markdown" / "ch1.md")
    task["pages"] = [1]
    task["visual_files"] = {"1": {"path": "ocr_review_images/page_001.png", "sha256": sha256_file(img)}}
    task["visual_image_manifest_sha256"] = sha256_file(img_manifest)
    task["source_pdf_sha256"] = "pdf"

    # When no sidecar candidate_refs exist, legitimate_non_footnote is permitted.
    task["candidate_refs"] = []
    reviews_no_ref, errs_no_ref = validate_result_audit_reviews(
        tmp_path,
        [task],
        [{
            "task_id": task["task_id"],
            "evidence_sha256": task["evidence_sha256"],
            "outcome": "legitimate_non_footnote",
            "reason": "Section reset verified on page image.",
            "evidence_pages": [1],
        }],
    )
    assert errs_no_ref == [] and len(reviews_no_ref) == 1

    # When candidate_refs exist and confirmed_footnote is chosen, applied history retains it.
    task["candidate_refs"] = [{"page": 1, "source": "primary", "block": 2}]
    ordinary_decisions = [{"page": 1, "source": "primary", "block": 2, "role": "footnote_start", "key": "1"}]
    confirm_review = {
        "task_id": task["task_id"],
        "evidence_sha256": task["evidence_sha256"],
        "outcome": "confirmed_footnote",
        "candidate_ref": {"page": 1, "source": "primary", "block": 2},
        "reason": "Section resets at part 2.",
        "evidence_pages": [1],
    }
    kept_reviews, kept_tasks, kept_errors = validate_applied_result_audit_reviews(
        tmp_path,
        [confirm_review],
        [task],
        ordinary_decisions=ordinary_decisions,
    )
    assert kept_errors == []
    assert len(kept_reviews) == 1 and kept_reviews[0]["outcome"] == "confirmed_footnote"
    assert len(kept_tasks) == 1


def test_sync_audit_review_pages_marks_review_required_and_pages(tmp_path):
    from pdf2epub.refine.footnote_handoff import _sync_audit_review_pages

    report = {
        "footnote_mode": "footnotes",
        "review_required": False,
        "review_pages": [],
        "pages": [
            {
                "page": 4,
                "visual_file": "ocr_review_images/page_004.png",
                "candidates": [
                    {
                        "page": 4,
                        "source": "primary",
                        "block": 2,
                        "confidence": "review",
                        "disposition": "review_required",
                    }
                ],
                "secondary_only_candidates": [],
            }
        ],
    }

    _sync_audit_review_pages(tmp_path, report, {})

    assert report["review_required"] is True
    assert report["review_pages"] == [4]

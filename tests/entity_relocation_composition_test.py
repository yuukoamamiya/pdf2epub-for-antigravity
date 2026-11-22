"""End-to-end entity coverage reuse across a reviewed footnote continuation move."""

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.entity_context import (
    entity_context_is_current,
    entity_context_status,
    prepare_entity_subagent_task,
    record_completed_entity_snapshot,
)
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_validation import validate_footnote_decisions
from pdf2epub.refine.footnote_versions import sync_polished_targets
from pdf2epub.validation_receipts import validation_receipt_is_current


def _set_decision(root: Path, report: dict, segments: list[dict]) -> None:
    path = root / "footnote_decisions.json"
    data = json.loads(path.read_text(encoding="utf-8"))
    decision = next(row for row in data["decisions"] if row.get("block") == 1)
    decision.update(role="body", key="17", override=True, segments=segments)
    data["result_audit_reviews"] = [
        {
            "task_id": task["task_id"],
            "evidence_sha256": task["evidence_sha256"],
            "outcome": "confirmed_footnote",
            "candidate_ref": {
                "page": int(task["source_address"]["page"]),
                "source": str(task["source_address"].get("source") or "primary"),
                "block": int(task["source_address"]["block"]),
            },
            "reason": "The rendered page image confirms this note block.",
            "evidence_pages": list(task.get("pages") or [1]),
        }
        for task in report.get("result_audit", {}).get("findings", [])
        if isinstance(task.get("source_address"), dict)
    ]
    path.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")


def _polish(root: Path, source: Path, *, resume: bool = False) -> dict:
    target = root / "polished_markdown"
    request = MarkdownHandoffRequest(
        output_dir=root,
        task="polish",
        source_dir=source,
        target_dir=target,
        source_language="English",
        target_language="English",
        config={"footnotes": {"mode": "auto"}},
        footnote_semantics_enabled=True,
        resume=resume,
    )
    prepare_markdown_subagent(request)
    target.mkdir(parents=True, exist_ok=True)
    if not resume:
        for src in source.glob("*.md"):
            (target / src.name).write_bytes(src.read_bytes())
    report = validate_markdown_subagent(
        root,
        "polish",
        source,
        target,
        require_footnote_semantic_review=True,
    )
    assert report["all_passed"] is True, report
    assert validation_receipt_is_current(
        root / "polish_validation.json", source, target / "validated",
        task="polish", output_dir=root, config={"footnotes": {"mode": "auto"}},
    )
    return report


def test_real_footnote_continuation_move_preserves_entity_coverage(tmp_path: Path):
    # Reuse the visual-evidence fixture, but make block 1 contain both the
    # existing note payload and a continuation initially left in body text.
    from tests.footnote_result_freshness_test import _prepare_review_case

    _prepare_review_case(tmp_path)
    source = tmp_path / "ocr_markdown" / "chapter_1.md"
    raw = source.read_text(encoding="utf-8").replace(
        "17. This is a note with citation to Smith 1999, page 14, and enough identifying text to match the exact sidecar block.",
        "17. Base note. Leaked continuation.",
    )
    source.write_text(raw, encoding="utf-8")
    page_md = tmp_path / "pages" / "page_001.md"
    page_md.write_text(raw, encoding="utf-8")
    sidecar_path = tmp_path / "pages" / "page_001.ocr.json"
    sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
    sidecar["blocks"][1]["html"] = "17. Base note. Leaked continuation."
    sidecar_path.write_text(json.dumps(sidecar), encoding="utf-8")

    paths = prepare_footnote_subagent(tmp_path, book_title="Entity relocation")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    _set_decision(tmp_path, report, [
        {"role": "footnote_start", "key": "17", "text": "17. Base note."},
        {"role": "body", "text": "Leaked continuation."},
    ])
    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation.get("errors")
    first = apply_footnote_normalization(tmp_path)
    assert first["valid"] is True, first
    normalized = tmp_path / "footnote_normalized"
    first_text = (normalized / "chapter_1.md").read_text(encoding="utf-8")
    assert "Leaked continuation." in first_text and "[^17]: Base note." in first_text

    # Establish a genuine current polish checkpoint and validated entity baseline.
    _polish(tmp_path, normalized)
    polished_source = tmp_path / "polished_markdown" / "validated"
    prepare_entity_subagent_task(tmp_path, "Book", polished_source, "polished", "English", "Chinese", {})
    entity_manifest = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    entities = json.loads((tmp_path / "translation_entities.template.json").read_text(encoding="utf-8"))
    entities["metadata"]["extraction_complete"] = True
    entities["terms"].append({"original": "Base note", "suggested_translation": "基础注释"})
    entity_path = tmp_path / "translation_entities.json"
    entity_path.write_text(json.dumps(entities, ensure_ascii=False), encoding="utf-8")
    record_completed_entity_snapshot(tmp_path, entity_manifest, entity_path)

    # Same OCR evidence; review now assigns the leaked text to the existing note.
    paths = prepare_footnote_subagent(tmp_path, book_title="Entity relocation")
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    _set_decision(tmp_path, report, [
        {"role": "footnote_start", "key": "17", "text": "17. Base note."},
        {"role": "footnote_continuation", "key": "17", "text": "Leaked continuation."},
    ])
    assert validate_footnote_decisions(tmp_path)["valid"] is True
    second = apply_footnote_normalization(tmp_path)
    assert second["valid"] is True, second
    final_text = (normalized / "chapter_1.md").read_text(encoding="utf-8")
    assert "[^17]: Base note.\n    Leaked continuation." in final_text

    # Apply's real publish/sync path updates the polished target; a real polish
    # validation then binds the changed source and target hashes.
    synchronized = sync_polished_targets(tmp_path)
    assert synchronized["chapter_1.md"]
    assert all(
        hint["status"] == "synchronized"
        for hint in synchronized["chapter_1.md"]
    )
    polished_target = tmp_path / "polished_markdown" / "chapter_1.md"
    assert polished_target.read_bytes() == (normalized / "chapter_1.md").read_bytes()
    _polish(tmp_path, normalized, resume=True)

    prepare_entity_subagent_task(tmp_path, "Book", polished_source, "polished", "English", "Chinese", {})
    manifest = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    current = json.loads(entity_path.read_text(encoding="utf-8"))
    assert manifest["pending_files"] == [], entity_context_status(tmp_path, polished_source, "English", "Chinese")
    assert manifest["reuse_reason"] == "verified_footnote_relocation"
    assert current["terms"] == entities["terms"]

    snapshot_id = manifest["snapshot_id"]
    source_hashes = manifest["source_sha256"]
    entity_bytes = entity_path.read_bytes()
    prepare_entity_subagent_task(tmp_path, "Book", polished_source, "polished", "English", "Chinese", {})
    repeated = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    assert repeated["pending_files"] == []
    assert repeated["snapshot_id"] == snapshot_id
    assert repeated["source_sha256"] == source_hashes
    assert entity_path.read_bytes() == entity_bytes
    assert entity_context_is_current(tmp_path, polished_source, "English", "Chinese")

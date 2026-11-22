"""Verified footnote moves can preserve the entity extraction coverage baseline."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from pdf2epub.entity_context import (
    entity_context_is_current,
    prepare_entity_subagent_task,
    record_completed_entity_snapshot,
)
from pdf2epub.refine.footnote_source import markdown_inventory
from pdf2epub.refine.footnote_versions import (
    _version_id,
    _exact_block_edit,
    capture_last_good,
    publish_success,
    sync_polished_targets,
)
from pdf2epub.workflow_contracts import atomic_write_json, sha256_file


def _normalization_receipt(root: Path, text: str, *, resolved_blocks=None) -> dict:
    normalized = root / "footnote_normalized"
    normalized.mkdir(parents=True, exist_ok=True)
    (normalized / "unit.md").write_text(text, encoding="utf-8")
    receipt = {
        "schema_version": 10,
        "status": "validated",
        "valid": True,
        "source_kind": "ocr",
        "ocr_evidence_mode": "single_ocr",
        "source_sha256": {"page_001.md": "a" * 64},
        "tree_progress_sha256": "b" * 64,
        "target_sha256": markdown_inventory(normalized),
        "resolved_blocks": list(resolved_blocks or []),
    }
    version_id = _version_id(receipt)
    receipt["version_id"] = version_id
    version = root / "footnote_normalization_versions" / version_id
    (version / "files").mkdir(parents=True, exist_ok=True)
    (version / "files" / "unit.md").write_text(text, encoding="utf-8")
    atomic_write_json(version / "receipt.json", receipt)
    return receipt


def _setup_completed_entity(root: Path):
    source_dir = root / "polished_markdown" / "validated"
    source_dir.mkdir(parents=True)
    (source_dir / "chapter_1.md").write_text("Body one.\n\n[^1]: Footnote A.\n", encoding="utf-8")
    (source_dir / "chapter_2.md").write_text("Body two.\n", encoding="utf-8")
    old_receipt = _normalization_receipt(root, "[^1]: Footnote A.\n")
    atomic_write_json(root / "footnote_normalization.json", old_receipt)
    prepare_entity_subagent_task(root, "Book", source_dir, "polished", "English", "Chinese", {})
    manifest = json.loads((root / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    template = json.loads((root / "translation_entities.template.json").read_text(encoding="utf-8"))
    template["metadata"]["extraction_complete"] = True
    template["terms"].append({"original": "Footnote A", "suggested_translation": "脚注甲"})
    entity_path = root / "translation_entities.json"
    entity_path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
    record_completed_entity_snapshot(root, manifest, entity_path)
    return source_dir, template


def _apply_verified_move(root: Path, source_dir: Path):
    from_version = json.loads((root / "footnote_normalization.json").read_text(encoding="utf-8"))["version_id"]
    source1, source2 = source_dir / "chapter_1.md", source_dir / "chapter_2.md"
    old1, old2 = source1.read_text(encoding="utf-8"), source2.read_text(encoding="utf-8")
    text1, status1 = _exact_block_edit(old1, "[^1]: Footnote A.", "")
    text2, status2 = _exact_block_edit(old2, "\0BBody two.", "[^1]: Footnote A.")
    assert status1 == status2 == "applied"
    (root / "polished_markdown").mkdir(exist_ok=True)
    (root / "polished_markdown" / "chapter_1.md.footnote-sync.bak").write_text(old1, encoding="utf-8")
    (root / "polished_markdown" / "chapter_2.md.footnote-sync.bak").write_text(old2, encoding="utf-8")
    source1.write_text(text1, encoding="utf-8")
    source2.write_text(text2, encoding="utf-8")
    polished_root = root / "polished_markdown"
    polished_root.mkdir(exist_ok=True)
    (polished_root / "chapter_1.md").write_text(text1, encoding="utf-8")
    (polished_root / "chapter_2.md").write_text(text2, encoding="utf-8")

    moved = {"kind": "footnote", "key": "1", "from_file": "chapter_1.md", "to_file": "chapter_2.md"}
    new_receipt = _normalization_receipt(root, "[^1]: Footnote A.\n", resolved_blocks=[moved])
    to_version = new_receipt["version_id"]
    atomic_write_json(root / "footnote_normalization.json", new_receipt)
    transition = {
        "from_version": from_version,
        "to_version": to_version,
        "from_source_sha256": {},
        "to_source_sha256": new_receipt["target_sha256"],
        "resolved_blocks": [moved],
        "files": {},
    }
    atomic_write_json(root / "footnote_normalization_changes.json", {"schema_version": 1, "transitions": [transition]})
    applied_files = {}
    for name, before, after, old_block, new_block in (
        ("chapter_1.md", old1, text1, "[^1]: Footnote A.", ""),
        ("chapter_2.md", old2, text2, "\0BBody two.", "[^1]: Footnote A."),
    ):
        applied_files[name] = {
            "target_sha256": sha256_file(polished_root / name),
            "to_source_sha256": new_receipt["target_sha256"],
            "hints": [{
                "status": "synchronized", "from_version": from_version, "to_version": to_version,
                "old_block": old_block, "new_block": new_block,
            }],
        }
    atomic_write_json(root / "footnote_normalization_versions" / "sync_applied.json", {"files": applied_files})


def _mock_relocation_freshness_gates(monkeypatch):
    from pdf2epub.refine import footnote_status
    from pdf2epub import validation_receipts

    monkeypatch.setattr(footnote_status, "footnote_normalization_is_current", lambda *_a, **_kw: True)
    monkeypatch.setattr(validation_receipts, "validation_receipt_is_current", lambda *_a, **_kw: True)


def test_verified_cross_unit_note_move_reuses_entity_table(tmp_path: Path, monkeypatch):
    _mock_relocation_freshness_gates(monkeypatch)
    source_dir, accepted = _setup_completed_entity(tmp_path)
    _apply_verified_move(tmp_path, source_dir)

    prepare_entity_subagent_task(tmp_path, "Book", source_dir, "polished", "English", "Chinese", {})
    manifest = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    current = json.loads((tmp_path / "translation_entities.json").read_text(encoding="utf-8"))
    assert manifest["pending_files"] == []
    assert manifest["reuse_reason"] == "verified_footnote_relocation"
    assert current["terms"] == accepted["terms"]
    assert entity_context_is_current(tmp_path, source_dir, "English", "Chinese")


def test_unproven_polish_rewrite_does_not_use_relocation_reuse(tmp_path: Path, monkeypatch):
    _mock_relocation_freshness_gates(monkeypatch)
    source_dir, accepted = _setup_completed_entity(tmp_path)
    _apply_verified_move(tmp_path, source_dir)
    # An unrelated edit after synchronization breaks exact reconstruction/hash proof.
    changed = source_dir / "chapter_1.md"
    changed.write_text(changed.read_text(encoding="utf-8").replace("Body one", "Body changed"), encoding="utf-8")
    entity_path = tmp_path / "translation_entities.json"
    failed = json.loads(entity_path.read_text(encoding="utf-8"))
    failed["terms"] = [{"original": "Unreviewed", "suggested_translation": "未审核"}]
    entity_path.write_text(json.dumps(failed, ensure_ascii=False), encoding="utf-8")

    prepare_entity_subagent_task(tmp_path, "Book", source_dir, "polished", "English", "Chinese", {})
    manifest = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    template = json.loads((tmp_path / "translation_entities.template.json").read_text(encoding="utf-8"))
    assert manifest["reuse_reason"] is None
    assert manifest["pending_files"] == ["chapter_1.md", "chapter_2.md"]
    assert template["terms"] == accepted["terms"]
    assert list((tmp_path / "entity_unvalidated").glob("*.json"))


def test_multiple_body_paragraphs_fold_into_note_only_in_exact_order():
    from pdf2epub.entity_context import _same_footnote_coverage

    old = [("17", "Base note.")]
    moved = ["First continuation.", "Second continuation."]
    assert _same_footnote_coverage(
        old,
        [("17", "Base note.\n    First continuation.\n    Second continuation.")],
        moved,
    )
    assert not _same_footnote_coverage(
        old,
        [("17", "Base note.\n    Second continuation.\n    First continuation.")],
        moved,
    )
    assert not _same_footnote_coverage(
        old,
        [("17", "Base note.\n    First continuation.\n    Altered continuation.")],
        moved,
    )


def test_real_normalization_without_current_polish_receipt_rejects_entity_reuse(tmp_path: Path):
    normalized = tmp_path / "footnote_normalized"
    polished = tmp_path / "polished_markdown"
    validated = polished / "validated"
    normalized.mkdir()
    validated.mkdir(parents=True)
    before = "# Chapter\n\nBody stays here.\n\nLeaked footnote paragraph with Hegel.\n"
    after = "# Chapter\n\nBody stays here.\n\n[^1]: Leaked footnote paragraph with Hegel.\n"
    normalized_file = normalized / "chapter_1.md"
    normalized_file.write_text(before, encoding="utf-8")
    target_before = markdown_inventory(normalized)
    first_receipt = {
        "schema_version": 10, "status": "validated", "valid": True,
        "source_kind": "ocr", "ocr_evidence_mode": "single_ocr",
        "source_dir": "ocr_markdown", "target_dir": "footnote_normalized",
        "source_sha256": {"page_001.md": "a" * 64},
        "tree_progress_sha256": "b" * 64, "target_sha256": target_before,
        "resolved_blocks": [],
    }
    atomic_write_json(tmp_path / "footnote_normalization.json", first_receipt)
    previous_version = capture_last_good(tmp_path)
    assert previous_version
    polished_file = polished / "chapter_1.md"
    polished_file.write_text(before, encoding="utf-8")
    (validated / "chapter_1.md").write_text(before, encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {
        "source_dir": "footnote_normalized", "source_sha256": target_before,
    })
    source_dir = validated
    prepare_entity_subagent_task(tmp_path, "Book", source_dir, "polished", "English", "Chinese", {})
    manifest = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    template = json.loads((tmp_path / "translation_entities.template.json").read_text(encoding="utf-8"))
    template["metadata"]["extraction_complete"] = True
    template["terms"].append({"original": "Hegel", "suggested_translation": "黑格尔"})
    entity_path = tmp_path / "translation_entities.json"
    entity_path.write_text(json.dumps(template, ensure_ascii=False), encoding="utf-8")
    record_completed_entity_snapshot(tmp_path, manifest, entity_path)

    normalized_file.write_text(after, encoding="utf-8")
    second_receipt = {
        **first_receipt,
        "target_sha256": markdown_inventory(normalized),
        "resolved_blocks": [{"kind": "body_to_note", "output_key": "1", "file": "chapter_1.md"}],
    }
    atomic_write_json(tmp_path / "footnote_normalization.json", second_receipt)
    next_version = publish_success(tmp_path, previous_version)
    assert next_version
    synchronized = sync_polished_targets(tmp_path)
    assert synchronized["chapter_1.md"][0]["status"] == "synchronized"
    (validated / "chapter_1.md").write_text(polished_file.read_text(encoding="utf-8"), encoding="utf-8")

    prepare_entity_subagent_task(tmp_path, "Book", source_dir, "polished", "English", "Chinese", {})
    current_manifest = json.loads((tmp_path / "entity_subagent_manifest.json").read_text(encoding="utf-8"))
    current = json.loads(entity_path.read_text(encoding="utf-8"))
    # This fixture drives the real normalization/publish/sync producers but has
    # no current polish validation receipt, so the freshness gate must reject reuse.
    assert current_manifest["pending_files"] == ["chapter_1.md"]
    assert current_manifest["reuse_reason"] is None
    assert current["terms"] == template["terms"]

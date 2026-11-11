import hashlib
import json
from pathlib import Path

from pdf2epub.validation_receipts import (
    build_input_snapshot,
    markdown_directory_sha256,
    validation_receipt_is_current,
)
from pdf2epub.commands.markdown import _sync_subagent_manifest_progress
from pdf2epub.commands.markdown_context import _load_pdf_continuation_files
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION


def _pages_fingerprint(pages_dir: Path) -> str:
    digest = hashlib.sha256()
    for page in sorted(
        [*pages_dir.glob("page_*.md"), *pages_dir.glob("page_*.ocr.json")]
    ):
        digest.update(page.name.encode("utf-8"))
        digest.update(hashlib.sha256(page.read_bytes()).digest())
    return digest.hexdigest()


def test_package_receipt_covers_toc_refinement_config_and_translated_toc(tmp_path: Path):
    source_dir = tmp_path / "polished_markdown" / "validated"
    target_dir = tmp_path / "translated" / "validated"
    pages_dir = tmp_path / "pages"
    (tmp_path / "ocr_markdown").mkdir()
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    pages_dir.mkdir()

    (source_dir / "chapter_1.md").write_text("polished", encoding="utf-8")
    (target_dir / "chapter_1.md").write_text("译文", encoding="utf-8")
    (pages_dir / "page_001.md").write_text("page", encoding="utf-8")
    toc = tmp_path / "toc_tree.json"
    toc.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    translated_toc = tmp_path / "toc_tree_translated.json"
    translated_toc.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    (tmp_path / "toc_translation_source.json").write_text(
        json.dumps({"toc": {"chapters": []}}), encoding="utf-8"
    )
    config = tmp_path / "config.yaml"
    config.write_text("title: Book\n", encoding="utf-8")
    pages_hash = _pages_fingerprint(pages_dir)
    progress = tmp_path / "ocr_markdown" / "tree_progress.json"
    progress.write_text(
        json.dumps(
            {
                "fingerprint": {
                    "toc_sha256": hashlib.sha256(toc.read_bytes()).hexdigest(),
                    "pages_sha256": pages_hash,
                }
            }
        ),
        encoding="utf-8",
    )

    snapshot = build_input_snapshot(tmp_path, config, translated=True)
    report = {
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
        "task": "translate",
        "scope": "full",
        "all_passed": True,
        "source_sha256": {
            "chapter_1.md": hashlib.sha256(
                (source_dir / "chapter_1.md").read_bytes()
            ).hexdigest()
        },
        "target_sha256": {
            "chapter_1.md": hashlib.sha256(
                (target_dir / "chapter_1.md").read_bytes()
            ).hexdigest()
        },
        "build_inputs": snapshot,
    }
    report_path = tmp_path / "translate_validation.json"
    report_path.write_text(json.dumps(report), encoding="utf-8")

    assert validation_receipt_is_current(
        report_path,
        source_dir,
        target_dir,
        task="translate",
        output_dir=tmp_path,
        config_path=config,
        require_build_inputs=True,
    )

    translated_toc.write_text(json.dumps({"chapters": [{"title": "changed"}]}), encoding="utf-8")
    assert not validation_receipt_is_current(
        report_path,
        source_dir,
        target_dir,
        task="translate",
        output_dir=tmp_path,
        config_path=config,
        require_build_inputs=True,
    )


def test_receipt_from_old_superscript_policy_is_not_current(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("Citations: 1856<sup>2</sup>, p. 4.", encoding="utf-8")
    (target_dir / "unit.md").write_text("Citations: 1856<sup>2</sup>, p. 4.", encoding="utf-8")
    receipt = tmp_path / "polish_validation.json"
    report = {
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION - 1,
        "task": "polish",
        "scope": "full",
        "all_passed": True,
        "source_sha256": markdown_directory_sha256(source_dir),
        "target_sha256": markdown_directory_sha256(target_dir),
    }
    receipt.write_text(json.dumps(report), encoding="utf-8")

    assert not validation_receipt_is_current(
        receipt, source_dir, target_dir, task="polish"
    )

    # A revalidation receipt using the current contract restores currentness.
    report["schema_version"] = MARKDOWN_VALIDATION_SCHEMA_VERSION
    receipt.write_text(json.dumps(report), encoding="utf-8")
    assert validation_receipt_is_current(
        receipt, source_dir, target_dir, task="polish"
    )


def test_unrelated_ocr_receipt_keeps_its_existing_schema_current(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "page_001.md").write_text("OCR source", encoding="utf-8")
    (target_dir / "page_001.md").write_text("OCR corrected", encoding="utf-8")
    receipt = tmp_path / "ocr-correct_validation.json"
    receipt.write_text(
        json.dumps({
            "schema_version": 3,
            "task": "ocr-correct", "scope": "full", "all_passed": True,
            "source_sha256": markdown_directory_sha256(source_dir),
            "target_sha256": markdown_directory_sha256(target_dir),
        }),
        encoding="utf-8",
    )

    assert validation_receipt_is_current(
        receipt, source_dir, target_dir, task="ocr-correct"
    )


def test_enabled_dynamic_memory_with_missing_manifest_contract_is_invalid_snapshot(tmp_path: Path):
    from pdf2epub.validation_receipts import _dynamic_terminology_snapshot_binding

    config = tmp_path / "config.yaml"
    config.write_text(
        "title: Book\ntranslation:\n  dynamic_terminology:\n    enabled: true\n",
        encoding="utf-8",
    )
    assert _dynamic_terminology_snapshot_binding(tmp_path, config) == "invalid"


def test_dynamic_receipt_rechecks_current_memory_without_build_input_gate(tmp_path: Path, monkeypatch):
    from pdf2epub import terminology_memory_validation
    from pdf2epub.validation_receipts import _dynamic_terminology_snapshot_binding

    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")
    (target_dir / "unit.md").write_text("target", encoding="utf-8")
    (tmp_path / "translate_subagent_manifest.json").write_text(json.dumps({
        "book_title": "Book", "source_language": "English", "target_language": "Chinese",
        "dynamic_terminology": {"schema_version": 1, "enabled": True},
    }), encoding="utf-8")
    receipt = tmp_path / "translate_validation.json"
    receipt.write_text(json.dumps({
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
        "task": "translate", "scope": "full", "all_passed": True,
        "source_sha256": markdown_directory_sha256(source_dir),
        "target_sha256": markdown_directory_sha256(target_dir),
    }), encoding="utf-8")
    calls = []

    def invalid_memory(*args, **kwargs):
        calls.append(kwargs)
        return {"enabled": True, "valid": False}

    monkeypatch.setattr(terminology_memory_validation, "validate_dynamic_terminology_units", invalid_memory)
    assert not validation_receipt_is_current(
        receipt, source_dir, target_dir, task="translate", output_dir=tmp_path,
        require_build_inputs=False,
    )
    assert len(calls) == 1


def test_enabled_dynamic_receipt_fails_when_manifest_memory_contract_is_missing(tmp_path: Path):
    source_dir, target_dir = tmp_path / "source", tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")
    (target_dir / "unit.md").write_text("target", encoding="utf-8")
    (tmp_path / "translate_subagent_manifest.json").write_text(json.dumps({
        "book_title": "Book", "source_language": "English", "target_language": "Chinese",
        "files": ["unit.md"],
    }), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text(
        "title: Book\ntranslation:\n  dynamic_terminology:\n    enabled: true\n",
        encoding="utf-8",
    )
    receipt = tmp_path / "translate_validation.json"
    receipt.write_text(json.dumps({
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
        "task": "translate", "scope": "full", "all_passed": True,
        "source_sha256": markdown_directory_sha256(source_dir),
        "target_sha256": markdown_directory_sha256(target_dir),
    }), encoding="utf-8")

    assert not validation_receipt_is_current(
        receipt, source_dir, target_dir, task="translate", output_dir=tmp_path,
        config_path=config, require_build_inputs=False,
    )


def test_package_receipt_fingerprint_includes_native_layout_sidecars(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    pages_dir = tmp_path / "pages"
    source_dir.mkdir()
    target_dir.mkdir()
    pages_dir.mkdir()
    (source_dir / "chapter.md").write_text("source", encoding="utf-8")
    (target_dir / "chapter.md").write_text("target", encoding="utf-8")
    (pages_dir / "page_001.md").write_text("page", encoding="utf-8")
    (pages_dir / "page_001.ocr.json").write_text("{\"layout\": 1}", encoding="utf-8")
    toc = tmp_path / "toc_tree.json"
    toc.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("title: Book\n", encoding="utf-8")
    progress_dir = tmp_path / "ocr_markdown"
    progress_dir.mkdir()
    (progress_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "fingerprint": {
                    "toc_sha256": hashlib.sha256(toc.read_bytes()).hexdigest(),
                    "pages_sha256": _pages_fingerprint(pages_dir),
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot = build_input_snapshot(tmp_path, config, translated=False)
    assert snapshot["refinement_fingerprint"]["current_pages_sha256"] == _pages_fingerprint(
        pages_dir
    )

    (pages_dir / "page_001.ocr.json").write_text("{\"layout\": 2}", encoding="utf-8")
    assert snapshot["refinement_fingerprint"]["current_pages_sha256"] != _pages_fingerprint(
        pages_dir
    )


def test_package_receipt_rejects_stale_refinement_page_fingerprint(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    pages_dir = tmp_path / "pages"
    source_dir.mkdir()
    target_dir.mkdir()
    pages_dir.mkdir()
    (source_dir / "chapter.md").write_text("source", encoding="utf-8")
    (target_dir / "chapter.md").write_text("target", encoding="utf-8")
    (pages_dir / "page_001.md").write_text("page", encoding="utf-8")
    toc = tmp_path / "toc_tree.json"
    toc.write_text(json.dumps({"chapters": []}), encoding="utf-8")
    config = tmp_path / "config.yaml"
    config.write_text("title: Book\n", encoding="utf-8")
    progress_dir = tmp_path / "ocr_markdown"
    progress_dir.mkdir()
    (progress_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "fingerprint": {
                    "toc_sha256": hashlib.sha256(toc.read_bytes()).hexdigest(),
                    "pages_sha256": "wrong",
                }
            }
        ),
        encoding="utf-8",
    )
    snapshot = build_input_snapshot(tmp_path, config, translated=False)
    assert snapshot["refinement_fingerprint"]["pages_sha256"] == "wrong"
    assert snapshot["refinement_fingerprint"]["current_pages_sha256"] != "wrong"


def test_continuation_metadata_uses_refinement_part_order(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    for name in ("chapter.part1.md", "chapter.part2.md", "chapter.part3.md"):
        (source_dir / name).write_text(name, encoding="utf-8")
    progress_dir = tmp_path / "ocr_markdown"
    progress_dir.mkdir()
    (progress_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "file": "chapter.md",
                        "part_files": [
                            "chapter.part1.md",
                            "chapter.part2.md",
                            "chapter.part3.md",
                        ],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    metadata = _load_pdf_continuation_files(tmp_path, source_dir)
    assert metadata["chapter.part2.md"] == {
        "is_continuation": True,
        "part_number": 2,
        "part_count": 3,
        "previous_file": "chapter.part1.md",
    }
    assert metadata["chapter.part3.md"]["part_number"] == 3
    assert metadata["chapter.part3.md"]["previous_file"] == "chapter.part2.md"

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "German",
        "Chinese",
        continuation_files=metadata,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["continuation_files"] == metadata
    assert "never add a Markdown heading" in paths["prompt"].read_text(encoding="utf-8")


def test_full_validation_reconciles_parent_manifest_progress(tmp_path: Path):
    manifest = tmp_path / "translate_subagent_manifest.json"
    manifest.write_text(
        json.dumps(
            {
                "files": ["a.md", "b.md"],
                "completed_files": ["a.md"],
                "pending_files": ["b.md"],
                "worker_handoffs": [
                    {"files": ["a.md"], "status": "pending"},
                    {"files": ["b.md"], "status": "pending"},
                ],
            }
        ),
        encoding="utf-8",
    )

    _sync_subagent_manifest_progress(
        tmp_path,
        "translate",
        {"scope": "full", "valid_files": ["a.md", "b.md"]},
    )

    updated = json.loads(manifest.read_text(encoding="utf-8"))
    assert updated["completed_files"] == ["a.md", "b.md"]
    assert updated["pending_files"] == []
    assert all(item["status"] == "completed" for item in updated["worker_handoffs"])

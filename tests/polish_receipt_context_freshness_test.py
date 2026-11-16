import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.commands.sources import _polished_stage_is_current
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.polish_footnote_semantics import build_semantic_review_contract
from pdf2epub.validation_receipts import validation_receipt_is_current


def _write_clear_sidecar(output_dir: Path, name: str, source: str, target: str, contract: dict) -> None:
    sidecar = output_dir / contract["sidecar"]
    sidecar.parent.mkdir(parents=True, exist_ok=True)
    sidecar.write_text(json.dumps({
        "schema_version": 1,
        "file": name,
        "source_sha256": hashlib.sha256(source.encode()).hexdigest(),
        "target_sha256": hashlib.sha256(target.encode()).hexdigest(),
        "context_sha256": contract["context_sha256"],
        "status": "clear",
        "reviewed_all": True,
        "reviewed_note_ids": contract["expected_note_ids"],
        "coverage": {
            "body_reviewed": {"status": "reviewed"},
            "definitions_reviewed": {"status": "reviewed"},
            "cross_boundary_reviewed": (
                {"status": "reviewed"}
                if contract["has_boundary_context"]
                else {"status": "not_applicable", "reason": "This unit has no boundary context."}
            ),
        },
        "issues": [],
    }), encoding="utf-8")


def _prepare_valid_polish(tmp_path: Path, *, context_kind: str):
    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    source_texts = {
        "part-a.md": "A sufficiently long source paragraph gives context for validation.\n",
        "part-b.md": "Another sufficiently long source paragraph remains in its assigned file.\n",
    }
    for name, text in source_texts.items():
        (source_dir / name).write_text(text, encoding="utf-8")
        (target_dir / name).write_text(text, encoding="utf-8")

    unit_context_files = None
    continuation_files = None
    changed_path = None
    if context_kind == "unit_context":
        context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
        context_dir.mkdir(parents=True)
        unit_context_files = {}
        for name in source_texts:
            context_path = context_dir / f"{name}.json"
            context_path.write_text('{"entries": [{"source": "old", "target": "旧"}]}', encoding="utf-8")
            unit_context_files[name] = context_path
        changed_path = unit_context_files["part-b.md"]
    elif context_kind == "tree_mapping":
        progress_path = source_dir / "tree_progress.json"
        progress_path.write_text(json.dumps({"units": [{"part_files": ["part-a.md", "part-b.md"]}]}), encoding="utf-8")
        from pdf2epub.refine.unit_mapping import load_pdf_continuation_files
        continuation_files = load_pdf_continuation_files(tmp_path, source_dir)
        changed_path = progress_path
    else:  # pragma: no cover - test helper contract
        raise AssertionError(context_kind)

    paths = prepare_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir, "English", "Chinese",
        unit_context_files=unit_context_files,
        continuation_files=continuation_files,
        footnote_semantics_enabled=True,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    for name in source_texts:
        source = (source_dir / name).read_bytes().decode("utf-8")
        target = (target_dir / name).read_bytes().decode("utf-8")
        contract = manifest["footnote_semantics"]["files"][name]
        _write_clear_sidecar(tmp_path, name, source, target, contract)
    report = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        require_footnote_semantic_review=True,
    )
    assert report["all_passed"] is True, json.dumps({"invalid": report["invalid"], "semantic": report.get("polish_footnote_semantic_reviews")}, ensure_ascii=False, indent=2)
    config = {"footnotes": {"mode": "auto"}}
    assert validation_receipt_is_current(
        tmp_path / "polish_validation.json", source_dir, target_dir,
        task="polish", output_dir=tmp_path, config=config,
    )
    assert _polished_stage_is_current(
        tmp_path, target_dir / "validated", source_dir, config=config,
    )
    return source_dir, target_dir, changed_path, config


@pytest.mark.parametrize("context_kind", ["unit_context", "tree_mapping"])
def test_polish_receipt_and_downstream_stage_recheck_prepared_context_without_reprepare(
    tmp_path: Path, context_kind: str,
):
    source_dir, target_dir, changed_path, config = _prepare_valid_polish(
        tmp_path, context_kind=context_kind,
    )
    if context_kind == "unit_context":
        changed_path.write_text('{"entries": [{"source": "new", "target": "新"}]}', encoding="utf-8")
    else:
        changed_path.write_text(json.dumps({"units": [{"part_files": ["part-b.md", "part-a.md"]}]}), encoding="utf-8")

    assert not validation_receipt_is_current(
        tmp_path / "polish_validation.json", source_dir, target_dir,
        task="polish", output_dir=tmp_path, config=config,
    )
    assert not _polished_stage_is_current(
        tmp_path, target_dir / "validated", source_dir, config=config,
    )


def test_polish_receipt_fails_closed_when_boundary_mapping_provenance_is_removed(tmp_path: Path):
    source_dir, target_dir, _changed_path, config = _prepare_valid_polish(
        tmp_path, context_kind="tree_mapping",
    )
    manifest_path = tmp_path / "polish_subagent_manifest.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    assert manifest["boundary_context_mapping_source"] == "tree_progress"
    del manifest["boundary_context_mapping_source"]
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    assert not validation_receipt_is_current(
        tmp_path / "polish_validation.json", source_dir, target_dir,
        task="polish", output_dir=tmp_path, config=config,
    )

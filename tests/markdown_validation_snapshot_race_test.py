import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.polish_footnote_semantics import build_semantic_review_contract


def _sidecar(source: str, target: str, contract: dict) -> dict:
    return {
        "schema_version": 1,
        "file": contract["file"],
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
                if contract.get("has_boundary_context")
                else {"status": "not_applicable", "reason": "This file has no split boundary."}
            ),
        },
        "issues": [],
    }


@pytest.mark.parametrize("mutated_input", ["source", "target", "sidecar", "boundary"])
def test_formal_validation_rejects_inputs_changed_after_staging(
    tmp_path: Path, monkeypatch, mutated_input: str,
):
    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    source = (
        "This is a sufficiently long prose paragraph used to exercise the formal "
        "validation snapshot race without triggering a short-line structure warning.\n"
    )
    neighbor = "Neighboring continuation part paragraph for boundary halo checks.\n"
    target = source
    source_path = source_dir / "chapter.md"
    neighbor_path = source_dir / "chapter.part2.md"
    target_path = target_dir / "chapter.md"
    source_path.write_bytes(source.encode("utf-8"))
    neighbor_path.write_bytes(neighbor.encode("utf-8"))
    target_path.write_bytes(target.encode("utf-8"))
    (target_dir / "chapter.part2.md").write_bytes(neighbor.encode("utf-8"))
    (source_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{"part_files": ["chapter.md", "chapter.part2.md"]}]}),
        encoding="utf-8",
    )

    from pdf2epub.refine.unit_mapping import load_pdf_continuation_files

    paths = prepare_markdown_subagent(
        MarkdownHandoffRequest(
            output_dir=tmp_path,
            task="polish",
            source_dir=source_dir,
            target_dir=target_dir,
            source_language="English",
            target_language="Chinese",
            footnote_semantics_enabled=True,
            continuation_files=load_pdf_continuation_files(tmp_path, source_dir),
        )
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    contract = manifest["footnote_semantics"]["files"]["chapter.md"]
    sidecar_path = tmp_path / contract["sidecar"]
    sidecar_path.parent.mkdir(parents=True, exist_ok=True)
    sidecar_path.write_text(json.dumps(_sidecar(source, target, contract)), encoding="utf-8")

    from pdf2epub import markdown_validation_persistence as persistence

    original_atomic_copy = persistence._atomic_copy

    def mutate_after_copy(src: Path, dst: Path) -> None:
        original_atomic_copy(src, dst)
        if mutated_input == "source":
            source_path.write_text("Changed source during validation.\n", encoding="utf-8")
        elif mutated_input == "target":
            target_path.write_text("Changed target during validation.\n", encoding="utf-8")
        elif mutated_input == "sidecar":
            sidecar_path.write_text(json.dumps(_sidecar(source, target, contract), indent=2), encoding="utf-8")
        else:
            neighbor_path.write_text("Mutated neighbor boundary during validation.\n", encoding="utf-8")

    monkeypatch.setattr(persistence, "_atomic_copy", mutate_after_copy)
    report = validate_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir,
        selected_files=["chapter.md"],
        require_footnote_semantic_review=True,
    )

    assert report["all_passed"] is False
    finding = next((
        item for item in report["invalid"]
        if item.get("kind") == "validation_snapshot_changed"
    ), None)
    assert finding is not None, (report.get("invalid"), report.get("valid_files"), report.get("polish_footnote_semantic_reviews"))
    assert finding["action"] in {
        "refresh_context_and_revalidate",
        "revalidate_current_target",
        "refresh_polish_semantic_review",
    }
    assert not (target_dir / "validated" / "chapter.md").exists()
    assert target_path.exists()
    assert sidecar_path.exists()

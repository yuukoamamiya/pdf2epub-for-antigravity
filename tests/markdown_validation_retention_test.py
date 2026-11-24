import json
from pathlib import Path

import pytest

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
from pdf2epub.validation_receipts import validation_receipt_is_current


SOURCE = (
    "This is a sufficiently long source paragraph used to validate that each "
    "independent unit keeps its own accepted result during recovery. " * 3
)
TARGET = "这是一个足够长的目标语言段落，用于验证恢复时每个独立单元都保留各自已验收的结果。" * 3


def _request(output: Path, source_dir: Path, target_dir: Path, *, resume: bool = False):
    return MarkdownHandoffRequest(
        output_dir=output,
        task="translate",
        source_dir=source_dir,
        target_dir=target_dir,
        source_language="English",
        target_language="Chinese",
        resume=resume,
    )


def _workspace(tmp_path: Path):
    source_dir, target_dir = tmp_path / "ocr_markdown", tmp_path / "translated"
    source_dir.mkdir()
    target_dir.mkdir()
    for name in ("one.md", "two.md"):
        (source_dir / name).write_text(SOURCE, encoding="utf-8")
        (target_dir / name).write_text(TARGET, encoding="utf-8")
    return source_dir, target_dir


def test_failed_full_validation_retains_good_copy_and_resume_reuses_it(tmp_path: Path):
    source_dir, target_dir = _workspace(tmp_path)
    prepare_markdown_subagent(_request(tmp_path, source_dir, target_dir))
    baseline = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )
    assert baseline["all_passed"] is True
    accepted_dir = target_dir / "validated"
    old_copy = (accepted_dir / "one.md").read_bytes()
    (target_dir / "two.md").unlink()

    failed = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )

    assert failed["all_passed"] is False
    assert failed["valid_files"] == ["one.md"]
    assert (accepted_dir / "one.md").read_bytes() == old_copy
    assert (accepted_dir / "two.md").read_bytes() == TARGET.encode("utf-8")
    assert validation_receipt_is_current(
        tmp_path / "translate_validation.json",
        source_dir,
        accepted_dir,
        task="translate",
        output_dir=tmp_path,
    ) is False

    resumed = prepare_markdown_subagent(
        _request(tmp_path, source_dir, target_dir, resume=True)
    )
    manifest = json.loads(resumed["manifest"].read_text(encoding="utf-8"))
    assert manifest["pending_files"] == ["two.md"]
    assert manifest["completed_files"] == ["one.md"]


def test_full_resume_reuses_good_file_after_one_file_refusal(tmp_path: Path):
    source_dir, target_dir = _workspace(tmp_path)
    prepare_markdown_subagent(_request(tmp_path, source_dir, target_dir))
    assert validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )["all_passed"] is True

    (target_dir / "two.md").write_text(
        "I cannot help you with that request.", encoding="utf-8"
    )
    failed = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )

    assert failed["all_passed"] is False
    assert failed["safety_blocked"] == ["two.md"]
    assert failed["valid_files"] == ["one.md"]
    resumed = prepare_markdown_subagent(
        _request(tmp_path, source_dir, target_dir, resume=True)
    )
    manifest = json.loads(resumed["manifest"].read_text(encoding="utf-8"))
    assert manifest["pending_files"] == ["two.md"]
    assert manifest["completed_files"] == ["one.md"]


def test_interrupted_promotion_keeps_previous_accepted_bytes(tmp_path: Path, monkeypatch):
    source_dir, target_dir = _workspace(tmp_path)
    validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )
    accepted = target_dir / "validated" / "one.md"
    before = accepted.read_bytes()
    (target_dir / "one.md").write_text(TARGET + "new edit", encoding="utf-8")

    from pdf2epub import markdown_validation_persistence as persistence

    def interrupt_copy(source: Path, destination: Path) -> None:
        destination.write_bytes(b"partial candidate")
        raise KeyboardInterrupt()

    monkeypatch.setattr(persistence, "_atomic_copy", interrupt_copy)
    with pytest.raises(KeyboardInterrupt):
        validate_markdown_subagent(
            tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
        )
    assert accepted.read_bytes() == before


def test_second_copy_promotion_failure_archives_old_good_bytes_and_stales_receipt(tmp_path: Path, monkeypatch):
    source_dir, target_dir = _workspace(tmp_path)
    baseline = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )
    assert baseline["all_passed"] is True
    accepted_dir = target_dir / "validated"
    old_one = (accepted_dir / "one.md").read_bytes()
    (target_dir / "one.md").write_text(TARGET + "修改。", encoding="utf-8")
    (target_dir / "two.md").write_text(TARGET + "修改。", encoding="utf-8")

    from pdf2epub import markdown_validation_persistence as persistence

    real_replace = persistence.os.replace
    final_promotions = 0

    def fail_second_promotion(source: Path, destination: Path) -> None:
        nonlocal final_promotions
        if Path(source).suffix == ".pending" and Path(destination).suffix == ".md":
            final_promotions += 1
            if final_promotions == 2:
                raise OSError("simulated interruption on second accepted copy")
        real_replace(source, destination)

    monkeypatch.setattr(persistence.os, "replace", fail_second_promotion)
    with pytest.raises(OSError, match="second accepted copy"):
        validate_markdown_subagent(
            tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
        )

    history = tmp_path / "scratch" / "translate" / "previous_validated"
    archived_one = next(history.glob("one.*.md"))
    assert archived_one.read_bytes() == old_one
    assert final_promotions == 2
    assert validation_receipt_is_current(
        tmp_path / "translate_validation.json",
        source_dir,
        accepted_dir,
        task="translate",
        output_dir=tmp_path,
    ) is False


def test_changed_input_during_candidate_copy_does_not_quarantine_or_replace(tmp_path: Path, monkeypatch):
    source_dir, target_dir = _workspace(tmp_path)
    validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )
    accepted = target_dir / "validated" / "one.md"
    before = accepted.read_bytes()
    (target_dir / "one.md").write_text(TARGET + "revision", encoding="utf-8")

    from pdf2epub import markdown_validation_persistence as persistence

    real_copy = persistence._atomic_copy

    def mutate_after_copy(source: Path, destination: Path) -> None:
        real_copy(source, destination)
        if destination.suffix == ".pending":
            (target_dir / "one.md").write_text(TARGET + "concurrent", encoding="utf-8")

    monkeypatch.setattr(persistence, "_atomic_copy", mutate_after_copy)
    report = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )

    assert report["all_passed"] is False
    assert report["valid_files"] == ["two.md"]
    assert accepted.read_bytes() == before
    assert (target_dir / "one.md").exists()
    assert report["quarantined_files"] == {}


def test_corrupt_candidate_is_not_promoted_or_certified(tmp_path: Path, monkeypatch):
    source_dir, target_dir = _workspace(tmp_path)
    validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )
    accepted = target_dir / "validated" / "one.md"
    before = accepted.read_bytes()

    from pdf2epub import markdown_validation_persistence as persistence

    real_copy = persistence._atomic_copy

    def corrupt_candidate(source: Path, destination: Path) -> None:
        real_copy(source, destination)
        if destination.suffix == ".pending":
            destination.write_bytes(b"corrupt candidate")

    monkeypatch.setattr(persistence, "_atomic_copy", corrupt_candidate)
    report = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )

    assert report["all_passed"] is False
    assert "one.md" not in report["valid_files"]
    assert accepted.read_bytes() == before
    assert report["quarantined_files"] == {}


def test_successful_full_inventory_shrink_retires_obsolete_copy(tmp_path: Path):
    source_dir, target_dir = _workspace(tmp_path)
    prepare_markdown_subagent(_request(tmp_path, source_dir, target_dir))
    assert validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )["all_passed"]
    old_copy = (target_dir / "validated" / "two.md").read_bytes()
    (source_dir / "two.md").unlink()
    (target_dir / "two.md").unlink()

    report = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir, target_language="Chinese"
    )

    validated = target_dir / "validated"
    assert report["all_passed"] is True
    assert sorted(path.name for path in validated.glob("*.md")) == ["one.md"]
    archived = tmp_path / "scratch" / "translate" / "retired_validated" / "two.md"
    assert archived.read_bytes() == old_copy
    assert validation_receipt_is_current(
        tmp_path / "translate_validation.json",
        source_dir,
        validated,
        task="translate",
        output_dir=tmp_path,
    ) is True


def test_relative_workspace_paths_allow_repeat_validation_and_archival(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    source_dir, target_dir = Path("source"), Path("target")
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text(SOURCE, encoding="utf-8")
    (target_dir / "unit.md").write_text(TARGET, encoding="utf-8")

    first = validate_markdown_subagent(
        Path("."), "translate", source_dir, target_dir, target_language="Chinese"
    )
    second = validate_markdown_subagent(
        Path("."), "translate", source_dir, target_dir, target_language="Chinese"
    )

    assert first["all_passed"] is True
    assert second["all_passed"] is True
    archives = list(Path("scratch/translate/previous_validated").glob("unit.*.md"))
    assert len(archives) == 1
    assert archives[0].read_bytes() == TARGET.encode("utf-8")


def test_scoped_translation_success_cannot_replace_failed_book_gate(tmp_path: Path):
    from pdf2epub.markdown_validation_persistence import _sync_partial_into_full_report
    from pdf2epub.workflow_contracts import markdown_validation_schema_version

    full = {
        "schema_version": markdown_validation_schema_version("translate"),
        "task": "translate",
        "scope": "full",
        "all_passed": False,
        "total": 1,
        "valid_files": ["one.md"],
        "source_sha256": {"one.md": "source"},
        "target_sha256": {"one.md": "target"},
        "entities": {"valid": False},
        "toc": {"valid": True},
        "toc_heading_bindings": {"valid": True},
        "translation_context": {"valid": True},
        "missing": [],
        "invalid": [],
        "safety_blocked": [],
        "protected_workspace_blocked": False,
        "review_required": [],
    }
    (tmp_path / "translate_validation.json").write_text(
        json.dumps(full), encoding="utf-8"
    )
    _sync_partial_into_full_report(tmp_path, {
        "task": "translate",
        "files_checked": ["one.md"],
        "valid_files": ["one.md"],
        "missing": [],
        "invalid": [],
        "safety_blocked": [],
        "target_language_blocked": [],
        "review_required": [],
        "retry_required": [],
        "human_review_required": [],
        "source_sha256": {"one.md": "source"},
        "target_sha256": {"one.md": "target"},
    })
    synced = json.loads((tmp_path / "translate_validation.json").read_text(encoding="utf-8"))
    assert synced["all_passed"] is False
    assert synced["entities"]["valid"] is False

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.refine.footnote_apply import _write_failure
from pdf2epub.refine.footnote_source import markdown_inventory
from pdf2epub.refine.footnote_versions import (
    capture_last_good,
    mark_apply_running,
    publish_success,
    recover_interrupted_apply,
    sync_polished_targets,
)
from pdf2epub.worker_manifest import _worker_manifest_projection
from pdf2epub.workflow_contracts import atomic_write_json, atomic_write_text, sha256_file


def _successful_version(root: Path, text: str) -> dict:
    target = root / "footnote_normalized"
    target.mkdir(parents=True, exist_ok=True)
    (target / "chapter.md").write_text(text, encoding="utf-8")
    digest = sha256_file(target / "chapter.md")
    receipt = {
        "schema_version": 10,
        "status": "validated",
        "valid": True,
        "source_dir": "ocr_markdown",
        "target_dir": "footnote_normalized",
        "source_sha256": {"chapter.md": "a" * 64},
        "target_sha256": {"chapter.md": digest},
    }
    atomic_write_json(root / "footnote_normalization.json", receipt)
    return receipt


def test_failed_apply_preserves_last_successful_receipt_and_restores_text(tmp_path: Path):
    receipt = _successful_version(tmp_path, "baseline\n")
    baseline_hash = sha256_file(tmp_path / "footnote_normalization.json")
    version = capture_last_good(tmp_path)
    mark_apply_running(tmp_path, {"chapter.md": "partial staged data\n"}, version)
    atomic_write_text(tmp_path / "footnote_normalized" / "chapter.md", "partial staged data\n")

    failed = _write_failure(tmp_path, ["audit unresolved"], status="review_required")

    assert failed["valid"] is False
    assert json.loads((tmp_path / "footnote_normalization.json").read_text(encoding="utf-8")) == receipt
    assert sha256_file(tmp_path / "footnote_normalization.json") == baseline_hash
    assert (tmp_path / "footnote_normalized" / "chapter.md").read_text(encoding="utf-8") == "baseline\n"
    assert json.loads((tmp_path / "footnote_normalization_attempt.json").read_text(encoding="utf-8"))["valid"] is False


def test_interrupted_materialization_recovers_on_explicit_resume_only(tmp_path: Path):
    _successful_version(tmp_path, "baseline\n")
    version = capture_last_good(tmp_path)
    target = tmp_path / "footnote_normalized" / "chapter.md"
    mark_apply_running(tmp_path, {"chapter.md": "interrupted replacement\n"}, version)
    atomic_write_text(target, "interrupted replacement\n")

    assert recover_interrupted_apply(tmp_path) is True
    assert target.read_text(encoding="utf-8") == "baseline\n"
    saved = next((tmp_path / "footnote_normalization_versions").glob("*/files/chapter.md"))
    assert target.read_bytes() == saved.read_bytes()


def test_recovery_does_not_revert_manual_normalized_edit_without_running_marker(tmp_path: Path):
    _successful_version(tmp_path, "baseline\n")
    assert capture_last_good(tmp_path)
    target = tmp_path / "footnote_normalized" / "chapter.md"
    target.write_text("manual edit\n", encoding="utf-8")

    assert recover_interrupted_apply(tmp_path) is False
    assert target.read_text(encoding="utf-8") == "manual edit\n"


def test_polish_resume_command_recovery_runs_before_source_selection_and_dry_run_is_read_only(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    import pdf2epub.commands.markdown as markdown_command
    from pdf2epub.pipeline_policy import PipelinePolicy

    normalized = tmp_path / "footnote_normalized"
    normalized.mkdir()
    target = normalized / "chapter.md"
    target.write_text("baseline\n", encoding="utf-8")
    receipt = {"valid": True, "status": "validated", "target_sha256": {"chapter.md": sha256_file(target)}}
    atomic_write_json(tmp_path / "footnote_normalization.json", receipt)
    version = capture_last_good(tmp_path)
    mark_apply_running(tmp_path, {"chapter.md": "partial\n"}, version)
    atomic_write_text(target, "partial\n")
    (tmp_path / "polished_markdown").mkdir()
    monkeypatch.setattr("pdf2epub.refine.footnote_unit_contexts.load_footnote_unit_contexts", lambda *_: {})
    monkeypatch.setattr("pdf2epub.refine.boundary_preview.load_boundary_preview", lambda *_: {})

    def select_after_recovery(output_dir, config):
        assert (output_dir / "footnote_normalized" / "chapter.md").read_text(encoding="utf-8") == "baseline\n"
        return output_dir / "footnote_normalized", "footnote_normalized"

    monkeypatch.setattr(markdown_command, "_resolve_pdf_polish_source", select_after_recovery)
    config = {"translation": {"source_language": "English", "target_language": "Chinese"}, "footnotes": {"mode": "disabled"}}
    args = SimpleNamespace(resume=True, dry_run=False, content_type="auto")
    prepared = markdown_command._prepare_task_source(
        args, "polish", config=config, policy=PipelinePolicy.from_config(config),
        output_dir=tmp_path, book_title="Synthetic", source_language="English",
        target_language="Chinese", footnote_mode="disabled", glossary_bundle=None,
        dynamic_attestation=None, config_path=None,
    )
    assert prepared is not None

    mark_apply_running(tmp_path, {"chapter.md": "partial again\n"}, version)
    atomic_write_text(target, "partial again\n")
    args.dry_run = True
    monkeypatch.setattr(markdown_command, "_resolve_pdf_polish_source", lambda out, _cfg: (out / "footnote_normalized", "footnote_normalized"))
    markdown_command._prepare_task_source(
        args, "polish", config=config, policy=PipelinePolicy.from_config(config),
        output_dir=tmp_path, book_title="Synthetic", source_language="English",
        target_language="Chinese", footnote_mode="disabled", glossary_bundle=None,
        dynamic_attestation=None, config_path=None,
    )
    assert target.read_text(encoding="utf-8") == "partial again\n"


def test_polish_cli_dry_run_is_read_only_and_resume_recovers_before_handoff(tmp_path: Path, monkeypatch):
    from types import SimpleNamespace
    import pdf2epub.commands.markdown as markdown_command

    output = tmp_path / "book"
    normalized = output / "footnote_normalized"
    polished = output / "polished_markdown"
    normalized.mkdir(parents=True)
    polished.mkdir()
    target = normalized / "chapter.md"
    target.write_text("baseline\n", encoding="utf-8")
    receipt = {"valid": True, "status": "validated", "target_sha256": {"chapter.md": sha256_file(target)}}
    atomic_write_json(output / "footnote_normalization.json", receipt)
    version = capture_last_good(output)
    mark_apply_running(output, {"chapter.md": "partial\n"}, version)
    atomic_write_text(target, "partial\n")
    config = {
        "title": "CLI Recovery Book",
        "translation": {"source_language": "English", "target_language": "Chinese"},
        "footnotes": {"mode": "disabled"},
        "subagent": {"batching": {"max_source_tokens": 2000}},
    }
    context = SimpleNamespace(config=config, book_title=config["title"], output_dir=output, config_path=None)
    monkeypatch.setattr(markdown_command, "load_book_context", lambda *_args, **_kwargs: context)
    monkeypatch.setattr("pdf2epub.refine.footnote_unit_contexts.load_footnote_unit_contexts", lambda *_: {})
    monkeypatch.setattr("pdf2epub.refine.boundary_preview.load_boundary_preview", lambda *_: {})
    monkeypatch.setattr(markdown_command, "_resolve_pdf_polish_source", lambda out, _cfg: (out / "footnote_normalized", "footnote_normalized"))
    args = SimpleNamespace(config="unused.yaml", resume=True, dry_run=True, content_type="auto")

    assert markdown_command.polish_command(args) == 0
    assert target.read_text(encoding="utf-8") == "partial\n"
    assert not (output / "polish_subagent_manifest.json").exists()

    args.dry_run = False
    def select_after_cli_recovery(out, _cfg):
        assert (out / "footnote_normalized" / "chapter.md").read_text(encoding="utf-8") == "baseline\n"
        return out / "footnote_normalized", "footnote_normalized"
    monkeypatch.setattr(markdown_command, "_resolve_pdf_polish_source", select_after_cli_recovery)
    assert markdown_command.polish_command(args) == 0
    assert (output / "polish_subagent_manifest.json").is_file()
    assert target.read_text(encoding="utf-8") == "baseline\n"


def test_polish_sync_patches_exact_block_and_keeps_unrelated_polish(tmp_path: Path):
    old = _successful_version(tmp_path, "Body before\n\nLeaked note text\n\nBody after\n")
    old_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    (polished / "chapter.md").write_text("Polished unrelated line\n\nBody before\n\nLeaked note text\n\nBody after\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": old["target_sha256"]})

    _successful_version(tmp_path, "Body before\n\nBody after\n\n[^1]: Leaked note text\n")
    publish_success(tmp_path, old_version)
    hints = sync_polished_targets(tmp_path)

    content = (polished / "chapter.md").read_text(encoding="utf-8")
    assert "Leaked note text" in content and "[^1]: Leaked note text" in content
    assert content.count("Leaked note text") == 1
    assert "Polished unrelated line" in content
    assert hints["chapter.md"][0]["status"] == "synchronized"
    assert (polished / "chapter.md.footnote-sync.bak").is_file()
    first_pass = content
    sync_polished_targets(tmp_path)
    assert (polished / "chapter.md").read_text(encoding="utf-8") == first_pass


def test_polish_sync_accumulates_multiple_successful_versions(tmp_path: Path):
    first = _successful_version(tmp_path, "Body A\n")
    first_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    target = polished / "chapter.md"
    target.write_text("Polished note\n\nBody A\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": first["target_sha256"]})
    _successful_version(tmp_path, "Body B\n")
    second_version = publish_success(tmp_path, first_version)
    _successful_version(tmp_path, "Body C\n")
    publish_success(tmp_path, second_version)

    hints = sync_polished_targets(tmp_path)

    content = target.read_text(encoding="utf-8")
    assert "Body C" in content and "Body B" not in content and "Polished note" in content
    assert len(hints["chapter.md"]) >= 1


def test_two_transition_hints_keep_earlier_review_when_later_change_syncs(tmp_path: Path):
    first = _successful_version(tmp_path, "Body A\n\nNote one\n")
    first_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    target = polished / "chapter.md"
    target.write_text("Rewritten note\n\nBody A\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": first["target_sha256"]})
    _successful_version(tmp_path, "Body B\n\nNote two\n")
    second_version = publish_success(tmp_path, first_version)
    _successful_version(tmp_path, "Body C\n\nNote two\n")
    publish_success(tmp_path, second_version)

    hints = sync_polished_targets(tmp_path)["chapter.md"]

    assert any(row.get("status") == "review_required" for row in hints)
    assert any(row.get("status") == "synchronized" for row in hints)
    final_hash = json.loads((tmp_path / "footnote_normalization.json").read_text(encoding="utf-8"))["target_sha256"]["chapter.md"]
    assert all(row["new_source_sha256"] == final_hash for row in hints)
    assert all(row["new_target_sha256"] == sha256_file(target) for row in hints)


def test_prior_hint_rebinds_after_polished_target_edit_without_invalidating_source(tmp_path: Path):
    old = _successful_version(tmp_path, "Old block\n")
    old_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    target = polished / "chapter.md"
    target.write_text("Old block\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": old["target_sha256"]})
    _successful_version(tmp_path, "New block\n")
    publish_success(tmp_path, old_version)
    initial = sync_polished_targets(tmp_path)["chapter.md"]
    current_hash = json.loads((tmp_path / "footnote_normalization.json").read_text(encoding="utf-8"))["target_sha256"]
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {
        "source_dir": "footnote_normalized", "source_sha256": current_hash,
        "footnote_change_hints": {"chapter.md": initial},
    })
    target.write_text("New block plus a legitimate reviewer edit\n", encoding="utf-8")

    rebound = sync_polished_targets(tmp_path)["chapter.md"]

    assert rebound[0]["status"] == "review_required"
    assert rebound[0]["new_source_sha256"] == current_hash["chapter.md"]
    assert rebound[0]["new_target_sha256"] == sha256_file(target)
    assert target.read_text(encoding="utf-8") == "New block plus a legitimate reviewer edit\n"


def test_polish_sync_refuses_repeated_or_substring_matches(tmp_path: Path):
    old = _successful_version(tmp_path, "Old block\n")
    old_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    target = polished / "chapter.md"
    target.write_text("Old block\n\nOld block\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": old["target_sha256"]})
    _successful_version(tmp_path, "New block\n")
    publish_success(tmp_path, old_version)

    hints = sync_polished_targets(tmp_path)

    assert target.read_text(encoding="utf-8") == "Old block\n\nOld block\n"
    assert hints["chapter.md"][0]["reason"] == "old block is repeated in polished target"


def test_polish_sync_noops_when_exact_change_already_applied(tmp_path: Path):
    old = _successful_version(tmp_path, "Old block\n")
    old_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    target = polished / "chapter.md"
    target.write_text("New block\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": old["target_sha256"]})
    _successful_version(tmp_path, "New block\n")
    publish_success(tmp_path, old_version)

    assert sync_polished_targets(tmp_path)["chapter.md"][0]["status"] == "synchronized"
    assert target.read_text(encoding="utf-8") == "New block\n"


def test_polish_sync_never_edits_a_matching_block_inside_fenced_code():
    from pdf2epub.refine.footnote_versions import _exact_block_edit

    text = "Rewritten body.\n\n```text\n\nOld block\n\n```\n"
    updated, status = _exact_block_edit(text, "Old block", "New block")

    assert updated == text
    assert status == "missing"


def test_stale_chain_is_ignored_and_change_hints_project_only_assigned_files(tmp_path: Path):
    old = _successful_version(tmp_path, "Old block\n")
    old_version = capture_last_good(tmp_path)
    polished = tmp_path / "polished_markdown"
    polished.mkdir()
    target = polished / "chapter.md"
    target.write_text("Old block\n", encoding="utf-8")
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {"source_dir": "footnote_normalized", "source_sha256": {"chapter.md": "b" * 64}})
    _successful_version(tmp_path, "New block\n")
    publish_success(tmp_path, old_version)

    hints = sync_polished_targets(tmp_path)["chapter.md"]
    assert hints[0]["status"] == "review_required"
    assert target.read_text(encoding="utf-8") == "Old block\n"
    current_hash = json.loads((tmp_path / "footnote_normalization.json").read_text(encoding="utf-8"))["target_sha256"]["chapter.md"]
    polished_hash = sha256_file(target)
    parent = {
        "task": "polish",
        "files": ["chapter.md", "sibling.md"],
        "source_sha256": {"chapter.md": current_hash, "sibling.md": "c" * 64},
        "footnote_change_target_sha256": {"chapter.md": polished_hash, "sibling.md": "d" * 64},
        "footnote_change_hints": {"chapter.md": hints, "sibling.md": [{"new_source_sha256": "c" * 64, "new_target_sha256": "d" * 64}]},
    }
    scoped = _worker_manifest_projection(
        parent, worker_id="worker-1", files=["chapter.md"], batches=[{"batch_id": "b1", "files": ["chapter.md"]}],
        chapter_mode=False, group={"files": ["chapter.md"]},
    )
    assert scoped["footnote_change_hints"] == {"chapter.md": hints}
    assert "sibling.md" not in scoped["footnote_change_hints"]
    unbound = dict(parent, footnote_change_target_sha256={})
    rejected = _worker_manifest_projection(
        unbound, worker_id="worker-1", files=["chapter.md"], batches=[{"batch_id": "b1", "files": ["chapter.md"]}],
        chapter_mode=False, group={"files": ["chapter.md"]},
    )
    assert "footnote_change_hints" not in rejected


def test_translation_resume_hints_rebind_to_current_polish_and_translation_bytes(tmp_path: Path):
    from pdf2epub.commands.markdown import _pdf_translation_footnote_hints

    normalized = tmp_path / "footnote_normalized"
    polished = tmp_path / "polished_markdown"
    translated = tmp_path / "translated"
    for directory in (normalized, polished, translated):
        directory.mkdir()
    (normalized / "chapter.md").write_text("Source with note.\n\n[^1]: moved note\n", encoding="utf-8")
    (polished / "chapter.md").write_text("Reviewer polished note wording.\n", encoding="utf-8")
    (translated / "chapter.md").write_text("既有中文译文保留。\n", encoding="utf-8")
    normalized_hash = sha256_file(normalized / "chapter.md")
    row = {"status": "review_required", "new_source_sha256": normalized_hash,
           "origin_old_source_sha256": "a" * 64, "origin_new_source_sha256": normalized_hash,
           "old_block": "old", "new_block": "new"}
    atomic_write_json(tmp_path / "polish_subagent_manifest.json", {
        "source_sha256": {"chapter.md": normalized_hash},
        "footnote_change_hints": {"chapter.md": [row]},
    })

    hints = _pdf_translation_footnote_hints(tmp_path, polished, translated)

    assert "chapter.md" in hints
    assert hints["chapter.md"][0]["polished_source_sha256"] == sha256_file(polished / "chapter.md")
    assert hints["chapter.md"][0]["existing_translation_sha256"] == sha256_file(translated / "chapter.md")
    assert (translated / "chapter.md").read_text(encoding="utf-8") == "既有中文译文保留。\n"
    (normalized / "chapter.md").write_text("stale normalized source\n", encoding="utf-8")
    assert _pdf_translation_footnote_hints(tmp_path, polished, translated) == {}


def test_translation_worker_receives_only_assigned_hash_fresh_footnote_hints():
    source_hash, target_hash = "a" * 64, "b" * 64
    parent = {
        "task": "translate",
        "source_sha256": {"chapter.md": source_hash, "sibling.md": "c" * 64},
        "footnote_translation_target_sha256": {"chapter.md": target_hash},
        "footnote_translation_change_hints": {
            "chapter.md": [{"polished_source_sha256": source_hash,
                            "existing_translation_sha256": target_hash,
                            "old_block": "old note", "new_block": "new note"}],
            "sibling.md": [{"polished_source_sha256": "c" * 64,
                            "existing_translation_sha256": "d" * 64}],
        },
    }
    scoped = _worker_manifest_projection(
        parent, worker_id="worker-translate-1", files=["chapter.md"],
        batches=[{"batch_id": "t1", "files": ["chapter.md"]}],
        chapter_mode=False, group={"files": ["chapter.md"]},
    )
    assert list(scoped["footnote_translation_change_hints"]) == ["chapter.md"]
    stale_parent = dict(parent, footnote_translation_target_sha256={})
    rejected = _worker_manifest_projection(
        stale_parent, worker_id="worker-translate-1", files=["chapter.md"],
        batches=[{"batch_id": "t1", "files": ["chapter.md"]}],
        chapter_mode=False, group={"files": ["chapter.md"]},
    )
    assert "footnote_translation_change_hints" not in rejected

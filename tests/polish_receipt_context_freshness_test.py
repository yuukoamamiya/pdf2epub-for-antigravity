import hashlib
import json
from pathlib import Path

import pytest

from pdf2epub.commands.sources import _polished_stage_is_current
from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_handoff_planning import plan_markdown_handoff
from pdf2epub.markdown_handoff_resume import unit_context_is_current
from pdf2epub.markdown_validation_persistence import capture_pdf_resume_contexts
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


def test_polish_boundary_resume_tracks_halo_bytes_and_mapping_not_neighbor_full_hash(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    previous = source_dir / "part-a.md"
    current = source_dir / "part-b.md"
    previous.write_text("outside " + "halo " * 5000, encoding="utf-8")
    current.write_text("own body", encoding="utf-8")
    continuation = {
        "part-a.md": {"is_continuation": True, "part_number": 1, "part_count": 2},
        "part-b.md": {
            "is_continuation": True, "part_number": 2, "part_count": 2,
            "previous_file": "part-a.md",
        },
    }

    before = plan_markdown_handoff(
        source_dir, "polish", declared_files=["part-a.md", "part-b.md"],
        continuation_files=continuation,
    )
    before_halo = before.preceding_contexts["part-b.md"]["text"]
    before_digest = before.boundary_context_sha256["part-b.md"]
    before_neighbor_hash = before.boundary_context_sources["part-b.md"][0]["source_sha256"]

    # Change bytes outside the clipped halo. Full-source provenance changes,
    # while the exact text and mapping the worker can read remain stable.
    previous.write_text("changed " + "halo " * 5000, encoding="utf-8")
    after = plan_markdown_handoff(
        source_dir, "polish", declared_files=["part-a.md", "part-b.md"],
        continuation_files=continuation,
    )
    assert after.preceding_contexts["part-b.md"]["text"] == before_halo
    assert after.boundary_context_sources["part-b.md"][0]["source_sha256"] != before_neighbor_hash
    assert after.boundary_context_sha256["part-b.md"] == before_digest

    old_context = {
        "boundary_context_sha256": {"part-b.md": before_digest},
        "unit_context_sha256": {},
    }
    assert unit_context_is_current(
        "part-b.md", previous_manifest=old_context, controlled_files={},
        context_sha256={}, unit_context_sha256={}, heading_contexts={},
        boundary_context_sha256=after.boundary_context_sha256, resume=True,
    )

    # Mapping metadata is part of the worker-visible projection identity even
    # when the clipped text happens to stay the same.
    changed_mapping = {
        name: {**metadata, "part_count": 3}
        for name, metadata in continuation.items()
    }
    remapped = plan_markdown_handoff(
        source_dir, "polish", declared_files=["part-a.md", "part-b.md"],
        continuation_files=changed_mapping,
    )
    assert remapped.preceding_contexts["part-b.md"]["text"] == after.preceding_contexts["part-b.md"]["text"]
    assert remapped.boundary_context_sha256["part-b.md"] != after.boundary_context_sha256["part-b.md"]
    assert not unit_context_is_current(
        "part-b.md", previous_manifest=old_context, controlled_files={},
        context_sha256={}, unit_context_sha256={}, heading_contexts={},
        boundary_context_sha256=remapped.boundary_context_sha256, resume=True,
    )

    smaller_budget = plan_markdown_handoff(
        source_dir, "polish",
        {"subagent": {"batching": {"max_source_tokens": 24}}},
        declared_files=["part-a.md", "part-b.md"],
        continuation_files=continuation,
    )
    assert smaller_budget.preceding_contexts["part-b.md"]["text"] != after.preceding_contexts["part-b.md"]["text"]
    assert smaller_budget.boundary_context_sha256["part-b.md"] != after.boundary_context_sha256["part-b.md"]


def test_polish_prepared_boundary_still_checks_full_neighbor_source_until_reprepare(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    texts = {
        "part-a.md": "outside " + "halo " * 5000,
        "part-b.md": "Another sufficiently long source paragraph remains in its assigned file.\n",
    }
    for name, text in texts.items():
        (source_dir / name).write_text(text, encoding="utf-8")
        (target_dir / name).write_text(text, encoding="utf-8")
    mapping_path = source_dir / "tree_progress.json"
    mapping_path.write_text(json.dumps({"units": [{"part_files": ["part-a.md", "part-b.md"]}]}), encoding="utf-8")

    from pdf2epub.refine.unit_mapping import load_pdf_continuation_files

    continuations = load_pdf_continuation_files(tmp_path, source_dir)
    paths = prepare_markdown_subagent(
        tmp_path, "polish", source_dir, target_dir, "English", "Chinese",
        continuation_files=continuations, footnote_semantics_enabled=True,
    )
    before, errors = capture_pdf_resume_contexts(tmp_path, "polish", ["part-b.md"])
    assert errors == {}
    assert "part-b.md" in before

    # The visible tail stays byte-identical, but the just-prepared source
    # provenance must still block validation until a fresh prepare runs.
    (source_dir / "part-a.md").write_text("changed " + "halo " * 5000, encoding="utf-8")
    _current, errors = capture_pdf_resume_contexts(tmp_path, "polish", ["part-b.md"])
    assert errors == {"part-b.md": "prepared boundary context source changed after preparation"}


def test_polish_command_resume_reuses_boundary_receipt_outside_halo_and_invalidates_inside(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch,
):
    from types import SimpleNamespace

    import pdf2epub.commands.markdown as markdown_commands

    output_dir = tmp_path / "book"
    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "polished_markdown"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    repeated_prefix = "outside " * 500
    shared_tail = "shared boundary tail " * 300
    source_texts = {
        "part-a.md": repeated_prefix + shared_tail,
        "part-b.md": "A sufficiently long source paragraph remains assigned to this file.\n",
    }
    for name, text in source_texts.items():
        (source_dir / name).write_text(text, encoding="utf-8")
        (target_dir / name).write_text(text, encoding="utf-8")
    (source_dir / "tree_progress.json").write_text(json.dumps({
        "units": [{"part_files": ["part-a.md", "part-b.md"]}],
    }), encoding="utf-8")
    config = {
        "title": "Synthetic boundary book",
        "translation": {"source_language": "English", "target_language": "Chinese"},
        "subagent": {"batching": {"max_source_tokens": 2500}},
        "footnotes": {"mode": "auto"},
    }
    context = SimpleNamespace(
        config=config, book_title=config["title"], output_dir=output_dir,
        config_path=None,
    )
    monkeypatch.setattr(markdown_commands, "load_book_context", lambda *_args, **_kwargs: context)
    args = SimpleNamespace(config="unused.yaml", resume=False, content_type="auto")

    assert markdown_commands.polish_command(args) == 0
    first_manifest_path = output_dir / "polish_subagent_manifest.json"
    first_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    for name in source_texts:
        source = (source_dir / name).read_bytes().decode("utf-8")
        target = (target_dir / name).read_bytes().decode("utf-8")
        _write_clear_sidecar(
            output_dir, name, source, target,
            first_manifest["footnote_semantics"]["files"][name],
        )
    validate_args = SimpleNamespace(config="unused.yaml", check_only=False)
    assert markdown_commands.polish_validate_command(validate_args) == 0
    assert validation_receipt_is_current(
        output_dir / "polish_validation.json", source_dir, target_dir,
        task="polish", output_dir=output_dir, config=config,
    )

    # Change only bytes beyond the prepared preceding halo. The source file
    # itself must be redone, while part-b's validated checkpoint remains reusable.
    outside_prefix = "changed outside " * 500
    (source_dir / "part-a.md").write_text(outside_prefix + shared_tail, encoding="utf-8")
    args.resume = True
    assert markdown_commands.polish_command(args) == 0
    outside_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    assert "part-b.md" in outside_manifest["completed_files"]
    assert "part-b.md" not in outside_manifest["pending_files"]

    # A fresh preparation refreshes full-neighbor provenance. The ordinary
    # single-file validation path can now reuse the old target and receipt.
    file_validate_args = SimpleNamespace(config="unused.yaml", check_only=False, file="part-b.md")
    assert markdown_commands.polish_validate_command(file_validate_args) == 0
    current_contexts, errors = capture_pdf_resume_contexts(output_dir, "polish", ["part-b.md"])
    assert errors == {}
    assert "part-b.md" in current_contexts

    # Alter the exact halo bytes; the same source/target checkpoint must no
    # longer be considered complete on the next normal resume preparation.
    (source_dir / "part-a.md").write_text(outside_prefix + "new boundary words " * 20, encoding="utf-8")
    assert markdown_commands.polish_command(args) == 0
    inside_manifest = json.loads(first_manifest_path.read_text(encoding="utf-8"))
    assert "part-b.md" not in inside_manifest["completed_files"]
    assert "part-b.md" in inside_manifest["pending_files"]

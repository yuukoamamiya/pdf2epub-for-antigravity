import json
from pathlib import Path

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.markdown_subagent_validation import validate_markdown_subagent


def _setup(tmp_path: Path, *, contexts: bool = True):
    source = tmp_path / "source"
    target = tmp_path / "translated"
    context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    source.mkdir()
    target.mkdir()
    context_dir.mkdir(parents=True)
    unit_context_files = {}
    for name in ("a.md", "b.md"):
        (source / name).write_text(f"Source paragraph for {name}.", encoding="utf-8")
        (target / name).write_text(f"中文译文，针对{name}。", encoding="utf-8")
        if contexts:
            context_path = context_dir / f"{name}.json"
            context_path.write_text(json.dumps({"entries": [{"source": name, "target": "条目"}]}), encoding="utf-8")
            unit_context_files[name] = context_path
    paths = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese",
        unit_context_files=unit_context_files,
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}} if contexts else {},
        config={"subagent": {"batching": {"max_files": 1}}},
    )
    return source, target, context_dir, unit_context_files, paths


def _read_manifest(path: Path):
    return json.loads(path.read_text(encoding="utf-8"))


def test_full_validation_receipt_keeps_resume_context_across_repeated_prepare(tmp_path: Path):
    source, target, context_dir, _, paths = _setup(tmp_path)
    report = validate_markdown_subagent(tmp_path, "translate", source, target)
    assert report["all_passed"]
    receipt = _read_manifest(tmp_path / "translate_validation.json")
    assert set(receipt["resume_contexts"]) == {"a.md", "b.md"}
    assert receipt["resume_contexts"]["a.md"]["unit_context_sha256"]["a.md"]

    (context_dir / "a.md.json").write_text(
        json.dumps({"entries": [{"source": "changed", "target": "新条目"}]}), encoding="utf-8"
    )
    first = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        unit_context_files={name: context_dir / f"{name}.json" for name in ("a.md", "b.md")},
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}},
        config={"subagent": {"batching": {"max_files": 1}}},
    )
    assert _read_manifest(first["manifest"])["pending_files"] == ["a.md"]
    second = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        unit_context_files={name: context_dir / f"{name}.json" for name in ("a.md", "b.md")},
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}},
        config={"subagent": {"batching": {"max_files": 1}}},
    )
    second_manifest = _read_manifest(second["manifest"])
    assert second_manifest["pending_files"] == ["a.md"]
    assert second_manifest["completed_files"] == ["b.md"]

    refreshed = validate_markdown_subagent(
        tmp_path, "translate", source, target, selected_files=["a.md"]
    )
    assert refreshed["all_passed"]
    final = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        unit_context_files={name: context_dir / f"{name}.json" for name in ("a.md", "b.md")},
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}},
        config={"subagent": {"batching": {"max_files": 1}}},
    )
    final_manifest = _read_manifest(final["manifest"])
    assert final_manifest["pending_files"] == []
    assert final_manifest["completed_files"] == ["a.md", "b.md"]


def test_partial_receipt_persists_snapshot_and_rejects_unprepared_context_change(tmp_path: Path):
    source, target, context_dir, unit_context_files, _ = _setup(tmp_path)
    prepared = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese",
        unit_context_files=unit_context_files,
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}},
    )
    assert validate_markdown_subagent(tmp_path, "translate", source, target)["all_passed"]
    (context_dir / "a.md.json").write_text(json.dumps({"entries": [{"source": "late"}]}), encoding="utf-8")
    target_bytes = (target / "a.md").read_bytes()
    for _ in range(2):
        report = validate_markdown_subagent(
            tmp_path, "translate", source, target, selected_files=["a.md"]
        )
        assert not report["all_passed"]
        assert "a.md" not in report["valid_files"]
        assert any(
            item["file"] == "a.md"
            and item.get("kind") == "prepared_context"
            and "changed after preparation" in item["reason"]
            for item in report["invalid"]
        )
        assert (target / "a.md").is_file()
        assert (target / "a.md").read_bytes() == target_bytes
        assert not (target / "validated" / "a.md").exists()
        ledger = _read_manifest(tmp_path / "translate_file_validation.json")
        assert ledger["files"]["a.md"]["valid"] is False
        assert ledger["files"]["a.md"]["resume_context"] is None
        assert any(item.get("action") == "refresh_context_and_revalidate" for item in report["retry_required"])
    ledger_bytes = (tmp_path / "translate_file_validation.json").read_bytes()
    resumed = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        unit_context_files={name: context_dir / f"{name}.json" for name in ("a.md", "b.md")},
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}},
        config={"subagent": {"batching": {"max_files": 1}}},
    )
    resumed_manifest = _read_manifest(resumed["manifest"])
    assert resumed_manifest["pending_files"] == ["a.md"]
    assert resumed_manifest["completed_files"] == ["b.md"]
    assert validate_markdown_subagent(tmp_path, "translate", source, target)["all_passed"]
    assert not (tmp_path / "translate_file_validation.json").exists()
    full_report = _read_manifest(tmp_path / "translate_validation.json")
    archive = full_report["superseded_partial_checkpoint_archive"]
    archived_path = tmp_path / archive["path"]
    assert archived_path.read_bytes() == ledger_bytes
    assert __import__("hashlib").sha256(archived_path.read_bytes()).hexdigest() == archive["sha256"]
    completed = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        unit_context_files={name: context_dir / f"{name}.json" for name in ("a.md", "b.md")},
        heading_contexts={"a.md": {"node_id": "a", "title": "A"}, "b.md": {"node_id": "b", "title": "B"}},
        config={"subagent": {"batching": {"max_files": 1}}},
    )
    assert _read_manifest(completed["manifest"])["completed_files"] == ["a.md", "b.md"]


def test_check_only_full_validation_does_not_archive_partial_ledger(tmp_path: Path):
    source, target, _, unit_context_files, _ = _setup(tmp_path)
    prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese",
        unit_context_files=unit_context_files,
    )
    validate_markdown_subagent(tmp_path, "translate", source, target, selected_files=["a.md"])
    ledger_path = tmp_path / "translate_file_validation.json"
    ledger_bytes = ledger_path.read_bytes()

    report = validate_markdown_subagent(
        tmp_path, "translate", source, target, create_validated_copy=False
    )

    assert report["all_passed"]
    assert ledger_path.read_bytes() == ledger_bytes
    assert not (tmp_path / "scratch" / "translate" / "superseded_checkpoints").exists()


def test_standalone_validation_without_manifest_remains_supported(tmp_path: Path):
    source, target = tmp_path / "source", tmp_path / "translated"
    source.mkdir()
    target.mkdir()
    (source / "unit.md").write_text("An ordinary source paragraph.", encoding="utf-8")
    (target / "unit.md").write_text("An ordinary target paragraph.", encoding="utf-8")

    report = validate_markdown_subagent(tmp_path, "translate", source, target)

    assert report["all_passed"]
    assert report["valid_files"] == ["unit.md"]
    assert report["invalid"] == []


def test_removed_unit_and_heading_context_require_formal_validation(tmp_path: Path):
    source, target, _, unit_context_files, _ = _setup(tmp_path)
    headings = {"a.md": {"node_id": "a"}, "b.md": {"node_id": "b"}}
    prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese",
        unit_context_files=unit_context_files, heading_contexts=headings,
    )
    assert validate_markdown_subagent(tmp_path, "translate", source, target)["all_passed"]

    for _ in range(2):
        prepared = prepare_markdown_subagent(
            tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        )
        manifest = _read_manifest(prepared["manifest"])
        assert manifest["pending_files"] == ["a.md", "b.md"]
    assert validate_markdown_subagent(tmp_path, "translate", source, target)["all_passed"]
    completed = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
    )
    assert _read_manifest(completed["manifest"])["completed_files"] == ["a.md", "b.md"]


def test_content_failure_with_stale_context_still_quarantines_target(tmp_path: Path):
    source, target, context_dir, unit_context_files, _ = _setup(tmp_path)
    prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese",
        unit_context_files=unit_context_files,
    )
    (target / "a.md").write_text("", encoding="utf-8")
    (context_dir / "a.md.json").write_text(json.dumps({"entries": [{"source": "late"}]}), encoding="utf-8")

    report = validate_markdown_subagent(
        tmp_path, "translate", source, target, selected_files=["a.md"]
    )

    assert any(item.get("kind") == "prepared_context" for item in report["invalid"])
    assert any("target is empty" in item["reason"] for item in report["invalid"])
    assert "a.md" in report["quarantined_files"]
    assert not (target / "a.md").exists()


def test_legacy_context_free_receipt_remains_reusable(tmp_path: Path):
    source, target, _, _, _ = _setup(tmp_path, contexts=False)
    # A context-free receipt predating resume snapshots remains compatible.
    report = validate_markdown_subagent(tmp_path, "translate", source, target)
    assert report["all_passed"]
    receipt_path = tmp_path / "translate_validation.json"
    receipt = _read_manifest(receipt_path)
    receipt.pop("resume_contexts", None)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")
    prepared = prepare_markdown_subagent(tmp_path, "translate", source, target, "English", "Chinese", resume=True)
    assert _read_manifest(prepared["manifest"])["completed_files"] == ["a.md", "b.md"]


def test_legacy_global_context_receipt_stays_pending_until_formal_validation(tmp_path: Path):
    source, target = tmp_path / "source", tmp_path / "translated"
    source.mkdir()
    target.mkdir()
    (source / "unit.md").write_text("A source paragraph.", encoding="utf-8")
    (target / "unit.md").write_text("A translated paragraph.", encoding="utf-8")
    glossary = tmp_path / "glossary.json"
    glossary.write_text(json.dumps({"entries": [{"source": "old"}]}), encoding="utf-8")

    initial = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese",
        context_files={"book": glossary},
    )
    assert validate_markdown_subagent(tmp_path, "translate", source, target)["all_passed"]
    receipt_path = tmp_path / "translate_validation.json"
    receipt = _read_manifest(receipt_path)
    receipt.pop("resume_contexts", None)
    receipt_path.write_text(json.dumps(receipt), encoding="utf-8")

    glossary.write_text(json.dumps({"entries": [{"source": "new"}]}), encoding="utf-8")
    prepare_kwargs = {"context_files": {"book": glossary}}
    first = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        **prepare_kwargs,
    )
    assert _read_manifest(first["manifest"])["pending_files"] == ["unit.md"]
    second = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        **prepare_kwargs,
    )
    assert _read_manifest(second["manifest"])["pending_files"] == ["unit.md"]

    assert validate_markdown_subagent(tmp_path, "translate", source, target)["all_passed"]
    completed = prepare_markdown_subagent(
        tmp_path, "translate", source, target, "English", "Chinese", resume=True,
        **prepare_kwargs,
    )
    assert _read_manifest(completed["manifest"])["completed_files"] == ["unit.md"]

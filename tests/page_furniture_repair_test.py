import json
from pathlib import Path

from pdf2epub.page_furniture_repair import (
    HANDOFF_DIR_NAME,
    MANIFEST_NAME,
    PROMPT_NAME,
    prepare_page_furniture_repair,
    validate_page_furniture_repair,
)


def _config():
    return {
        "subagent": {
            "models": {"default": "test-model"},
            "batching": {"max_files": 2, "max_source_tokens": 1000},
        }
    }


def test_prepare_page_furniture_repair_snapshots_and_scopes_files(tmp_path: Path):
    translated = tmp_path / "translated"
    reference = tmp_path / "polished_markdown" / "validated"
    translated.mkdir(parents=True)
    reference.mkdir(parents=True)
    for name in ("chapter_1.md", "chapter_2.md"):
        content = (
            f"# {name}\n"
            + "\n".join(f"A normal paragraph line {index}." for index in range(80))
            + "\nXII\n"
        )
        (translated / name).write_text(content, encoding="utf-8")
        (reference / name).write_text(content, encoding="utf-8")

    result = prepare_page_furniture_repair(
        tmp_path,
        _config(),
        "German",
        "Chinese",
    )

    manifest = json.loads((tmp_path / MANIFEST_NAME).read_text(encoding="utf-8"))
    assert manifest["pending_files"] == ["chapter_1.md", "chapter_2.md"]
    assert (tmp_path / PROMPT_NAME).is_file()
    assert (tmp_path / HANDOFF_DIR_NAME).is_dir()
    assert (tmp_path / "page_furniture_repair_original" / "chapter_1.md").is_file()
    assert result["pending_files"] == manifest["pending_files"]

    hints = manifest["repair_candidates"]["chapter_1.md"]
    assert hints["target"]["candidate_count"] >= 1
    assert hints["target"]["windows"]
    assert manifest["repair_file_stats"]["chapter_1.md"]["full_size_bytes"] > 0
    assert (
        manifest["repair_file_stats"]["chapter_1.md"]["estimated_tokens"]
        < manifest["file_stats"]["chapter_1.md"]["estimated_tokens"]
    )

    worker_manifests = list(
        (tmp_path / HANDOFF_DIR_NAME).glob("*_manifest_*.json")
    )
    assert worker_manifests
    worker = json.loads(worker_manifests[0].read_text(encoding="utf-8"))
    assert set(worker["repair_candidates"]) == set(worker["assigned_files"])
    worker_prompt = next(
        (tmp_path / HANDOFF_DIR_NAME).glob("*_prompt_*.md")
    ).read_text(encoding="utf-8")
    assert "do not load the full translated file" in worker_prompt


def test_validate_page_furniture_repair_stages_changed_targets(tmp_path: Path):
    translated = tmp_path / "translated"
    reference = tmp_path / "polished_markdown" / "validated"
    translated.mkdir(parents=True)
    reference.mkdir(parents=True)
    original = "# Chapter\nTranslated body.\n前言 XII\n"
    (translated / "chapter_1.md").write_text(original, encoding="utf-8")
    (reference / "chapter_1.md").write_text(
        "# Chapter\nOriginal body.\nPreface XII\n", encoding="utf-8"
    )

    prepare_page_furniture_repair(tmp_path, _config(), "German", "Chinese")
    (translated / "chapter_1.md").write_text(
        "# Chapter\nTranslated body.\n", encoding="utf-8"
    )

    report = validate_page_furniture_repair(tmp_path)

    assert report["all_passed"] is True
    assert report["repaired_files"] == ["chapter_1.md"]
    assert (translated / "validated" / "chapter_1.md").read_text(encoding="utf-8") == (
        "# Chapter\nTranslated body.\n"
    )


def test_validate_page_furniture_repair_tolerates_furniture_deletion_in_special_roles(tmp_path: Path):
    translated = tmp_path / "translated"
    reference = tmp_path / "polished_markdown" / "validated"
    translated.mkdir(parents=True)
    reference.mkdir(parents=True)
    source_content = "495\n\n2 Literaturhinweise\n\nEntry 1999 p. 12-14\n"
    original_target = "495\n\n2 文献指南\n\n条目 1999 p. 12-14\n"
    (reference / "chapter_8.2.1.md").write_text(source_content, encoding="utf-8")
    (translated / "chapter_8.2.1.md").write_text(original_target, encoding="utf-8")

    prepare_page_furniture_repair(tmp_path, _config(), "German", "Chinese")
    repaired_target = "2 文献指南\n\n条目 1999 p. 12-14\n"
    (translated / "chapter_8.2.1.md").write_text(repaired_target, encoding="utf-8")

    report = validate_page_furniture_repair(
        tmp_path,
        file_roles={"chapter_8.2.1.md": "bibliography"},
    )
    assert report["all_passed"] is True
    assert report["repaired_files"] == ["chapter_8.2.1.md"]

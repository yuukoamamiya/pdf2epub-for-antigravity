import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

from pdf2epub.html_translation.builder import HTMLEpubPipeline
from pdf2epub.refine.main import RefinedBreakdown
from pdf2epub.refine.subagent_workflow import (
    prepare_refine_subagent,
    validate_toc_tree_data,
)
from pdf2epub.subagent_workflow import (
    DEFAULT_SUBAGENT_MODEL,
    DEFAULT_TRANSLATION_MODEL,
    prepare_markdown_subagent,
    prepare_toc_translation_subagent,
    resolve_subagent_model,
)


def test_resolve_subagent_model_uses_translation_and_default_defaults():
    assert resolve_subagent_model({}, "translate") == DEFAULT_TRANSLATION_MODEL
    assert resolve_subagent_model({}, "translate-html") == DEFAULT_TRANSLATION_MODEL
    assert resolve_subagent_model({}, "refine") == DEFAULT_SUBAGENT_MODEL
    assert resolve_subagent_model({}, "polish") == DEFAULT_SUBAGENT_MODEL


def test_resolve_subagent_model_supports_configured_and_task_overrides():
    config = {
        "subagent": {
            "models": {"translation": "pro-custom", "default": "flash-custom"},
            "task_models": {"refine": "refine-custom"},
        }
    }
    assert resolve_subagent_model(config, "translate-novel") == "pro-custom"
    assert resolve_subagent_model(config, "polish") == "flash-custom"
    assert resolve_subagent_model(config, "refine") == "refine-custom"


def test_prepare_markdown_subagent_records_model(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate-html",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        config={"subagent": {"models": {"translation": "configured-pro"}}},
    )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")
    assert manifest["model"] == "configured-pro"
    assert "configured-pro" in prompt


def test_prepare_markdown_subagent_records_file_sizes_and_batches(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "a.md").write_text("one\ntwo\n", encoding="utf-8")
    (source_dir / "b.md").write_text("three", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        config={
            "subagent": {
                "batching": {
                    "max_files": 1,
                    "max_source_tokens": 100,
                    "max_concurrency": 2,
                }
            }
        },
    )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    stats = manifest["file_stats"]
    assert stats["a.md"]["size_bytes"] == len((source_dir / "a.md").read_bytes())
    assert stats["a.md"]["line_count"] == 2
    assert stats["a.md"]["nonempty_line_count"] == 2
    assert stats["a.md"]["estimated_tokens"] > 0
    assert manifest["batching"]["max_concurrency"] == 2
    assert manifest["recommended_batches"] == [["a.md"], ["b.md"]]


def test_prepare_markdown_subagent_does_not_trust_unvalidated_partial_file(
    tmp_path: Path,
):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")
    (target_dir / "unit.md").write_text("partial", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        resume=True,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["completed_files"] == []
    assert manifest["pending_files"] == ["unit.md"]


def test_prepare_markdown_subagent_accepts_only_matching_validated_checkpoint(
    tmp_path: Path,
):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    source.write_text("source", encoding="utf-8")
    (target_dir / "unit.md").write_text("complete", encoding="utf-8")
    (tmp_path / "translate_validation.json").write_text(
        json.dumps(
            {
                "valid_files": ["unit.md"],
                "source_sha256": {
                    "unit.md": hashlib.sha256(source.read_bytes()).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        resume=True,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert manifest["completed_files"] == ["unit.md"]
    assert manifest["pending_files"] == []


def test_prepare_markdown_subagent_emits_explicit_resume_lists(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    for name in ("done.md", "pending.md"):
        (source_dir / name).write_text(name, encoding="utf-8")
    (target_dir / "done.md").write_text("已完成", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        resume=True,
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")

    # A non-empty file without a validation report may be a truncated output
    # from an interrupted Subagent and must not be trusted as a checkpoint.
    assert manifest["completed_files"] == []
    assert manifest["pending_files"] == ["done.md", "pending.md"]
    assert "pending_files" in prompt


def test_metadata_source_keeps_identity_fields_out_of_translation_payload(tmp_path: Path):
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    pipeline.source_language = "English"
    pipeline.book_title = "The Original Book"
    pipeline.metadata = {
        "title": "The Original Book",
        "author": "Jane Doe",
        "publisher": "Example Press",
        "description": "<p>A short description.</p>",
        "rights": "Copyright 2026",
    }
    pipeline.parser = SimpleNamespace(toc=[], spine=[])

    source_path = pipeline.create_metadata_translation_source("Chinese")
    source = json.loads(source_path.read_text(encoding="utf-8"))

    assert source["preserved_metadata"] == {
        "author": "Jane Doe",
        "publisher": "Example Press",
    }
    assert source["model"] == DEFAULT_TRANSLATION_MODEL
    assert "author" not in source["translatable_metadata"]
    assert "publisher" not in source["translatable_metadata"]
    assert source["translatable_metadata"]["description"] == "A short description."


def test_metadata_validation_rejects_changed_publisher(tmp_path: Path):
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    source = {
        "schema_version": 1,
        "original_title": "Original",
        "target_language": "Chinese",
        "target_language_code": "zh",
        "preserved_metadata": {"author": "Jane Doe", "publisher": "Example Press"},
        "translatable_metadata": {"description": "A description", "rights": ""},
        "toc": [],
    }
    target = {
        **source,
        "translated_title": "译名",
        "preserved_metadata": {"author": "Jane Doe", "publisher": "Example 出版社"},
        "toc": [],
        "translated_description": "简介",
        "translated_rights": "",
    }
    (tmp_path / "metadata_translation_source.json").write_text(
        json.dumps(source), encoding="utf-8"
    )
    (tmp_path / "translated_metadata.json").write_text(
        json.dumps(target), encoding="utf-8"
    )

    report = pipeline.validate_translated_metadata()
    assert not report["valid"]
    assert any("publisher" in error for error in report["errors"])


def test_metadata_validation_rejects_missing_top_level_rights_field(tmp_path: Path):
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    source = {
        "schema_version": 1,
        "original_title": "Original",
        "target_language": "Chinese",
        "target_language_code": "zh",
        "preserved_metadata": {"author": "", "publisher": ""},
        "translatable_metadata": {"description": "", "rights": ""},
        "toc": [],
    }
    target = {
        **source,
        "translated_title": "译名",
        "preserved_metadata": {"author": "", "publisher": ""},
        "toc": [],
        "translated_description": "",
    }
    (tmp_path / "metadata_translation_source.json").write_text(
        json.dumps(source), encoding="utf-8"
    )
    (tmp_path / "translated_metadata.json").write_text(
        json.dumps(target), encoding="utf-8"
    )

    report = pipeline.validate_translated_metadata()
    assert not report["valid"]
    assert "translated_rights field is missing" in report["errors"]


def test_prepare_refine_subagent_writes_manifest_and_prompt(tmp_path: Path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text("Chapter", encoding="utf-8")
    (pages_dir / "page_002.md").write_text("Text", encoding="utf-8")

    paths = prepare_refine_subagent(
        tmp_path,
        "A Book",
        8000,
        config={"subagent": {"models": {"default": "configured-flash"}}},
    )

    assert paths["prompt"].exists()
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["page_count"] == 2
    assert manifest["model"] == "configured-flash"
    assert "configured-flash" in paths["prompt"].read_text(encoding="utf-8")
    assert "toc_tree.json" in paths["prompt"].read_text(encoding="utf-8")


def test_prepare_toc_translation_subagent_writes_clean_prompt(tmp_path: Path):
    (tmp_path / "toc_tree.json").write_text(
        json.dumps({"book_title": "A Book", "chapters": []}),
        encoding="utf-8",
    )

    paths = prepare_toc_translation_subagent(
        tmp_path,
        "English",
        "Chinese",
        config={"subagent": {"models": {"translation": "configured-pro"}}},
    )
    prompt = paths["prompt"].read_text(encoding="utf-8")
    source = json.loads(paths["source"].read_text(encoding="utf-8"))

    assert 'Read `toc_translation_source.json`' in prompt
    assert 'f"Read' not in prompt
    assert 'f"in the same directory' not in prompt
    assert source["model"] == "configured-pro"
    assert "configured-pro" in prompt


def test_validate_toc_tree_rejects_overlapping_siblings_and_bad_child():
    data = {
        "chapters": [
            {
                "title": "Chapter 1",
                "level": 1,
                "start_page": 1,
                "end_page": 5,
                "children": [
                    {"title": "Section", "level": 2, "start_page": 4, "end_page": 6}
                ],
            },
            {"title": "Chapter 2", "level": 1, "start_page": 4, "end_page": 8},
        ]
    }

    errors = validate_toc_tree_data(data, 8, range(1, 9))
    assert any("overlaps" in error for error in errors)
    assert any("outside its parent" in error for error in errors)


def test_local_refine_generates_units_without_constructing_model(tmp_path: Path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    (pages_dir / "page_001.md").write_text("One", encoding="utf-8")
    (pages_dir / "page_002.md").write_text("Two", encoding="utf-8")
    (tmp_path / "toc_tree.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "chapters": [
                    {"title": "One", "level": 1, "start_page": 1, "end_page": 1},
                    {"title": "Two", "level": 1, "start_page": 2, "end_page": 2},
                ],
            }
        ),
        encoding="utf-8",
    )

    refiner = RefinedBreakdown(
        config={"refine": {"max_tokens": 8000}},
        max_tokens=8000,
        local_only=True,
    )
    units = refiner.process_from_toc(
        pdf_path=tmp_path / "input.pdf",
        output_dir=tmp_path,
        book_title="A Book",
    )

    assert len(units) == 2
    assert (tmp_path / "ocr_markdown" / "chapter_1.md").exists()
    assert (tmp_path / "ocr_markdown" / "chapter_2.md").exists()

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
    detect_refusal,
    detect_bilingual_output,
    strip_outer_markdown_fences,
    prepare_markdown_subagent,
    prepare_toc_translation_subagent,
    resolve_subagent_model,
    validate_toc_translation_subagent,
)
from pdf2epub.footnote_normalization import validate_polish_footnote_normalization
from pdf2epub.cli import (
    _prepare_pdf_markdown_task,
    _validate_translation_entities,
    extract_entities_command,
    translate_toc_command,
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


def test_detect_refusal_flags_model_text_but_allows_matching_source_dialogue():
    assert detect_refusal(
        "This is an ordinary paragraph.",
        "I cannot translate this content because of safety policy.",
    )
    assert detect_refusal(
        "This is an ordinary paragraph.",
        "I can’t assist with this request.",
    )
    assert detect_refusal(
        "I cannot help you with that.",
        "我无法帮助你处理那件事。",
    ) is None


def test_detect_refusal_flags_chinese_disclaimer():
    reason = detect_refusal(
        "这是一本书中的普通段落。",
        "抱歉，我无法翻译或处理这部分内容。",
    )

    assert reason is not None
    assert "Chinese refusal" in reason


def test_detect_bilingual_output_is_advisory_for_long_unchanged_spans():
    line = "This is a deliberately long English paragraph that should remain unchanged in a bilingual output warning."
    warning = detect_bilingual_output(f"{line}\n{line}", f"{line}\n{line}")
    assert warning is not None
    assert warning["start_line"] == 1
    assert warning["end_line"] == 2


def test_strip_outer_markdown_fences_only_removes_wrapping_fence():
    cleaned, changed = strip_outer_markdown_fences("```markdown\n# 标题\n正文\n```\n")
    assert changed is True
    assert cleaned == "# 标题\n正文\n"
    unchanged, changed = strip_outer_markdown_fences("正文\n```\n内部\n")
    assert changed is False
    assert unchanged == "正文\n```\n内部\n"


def test_markdown_validation_reports_bilingual_warning_without_failing(tmp_path: Path):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    line = "This is a deliberately long English paragraph that should remain unchanged in a bilingual output warning."
    (source_dir / "unit.md").write_text(f"{line}\n{line}\n", encoding="utf-8")
    (target_dir / "unit.md").write_text(f"{line}\n{line}\n", encoding="utf-8")
    report = validate_markdown_subagent(tmp_path, "translate", source_dir, target_dir)
    assert report["all_passed"] is True
    assert report["bilingual_warnings"][0]["file"] == "unit.md"


def test_markdown_validation_excludes_bibliography_from_bilingual_warning(tmp_path: Path):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    line = "This is a deliberately long English bibliographic entry with a title, publisher, and publication year."
    (source_dir / "unit.md").write_text(f"{line}\n{line}\n", encoding="utf-8")
    (target_dir / "unit.md").write_text(f"{line}\n{line}\n", encoding="utf-8")
    report = validate_markdown_subagent(
        tmp_path, "translate", source_dir, target_dir,
        file_roles={"unit.md": "bibliography"},
    )
    assert report["bilingual_warnings"] == []


def test_markdown_validation_includes_structural_diff_summary(tmp_path: Path):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("# Original\nBody\n", encoding="utf-8")
    (target_dir / "unit.md").write_text("# 译文\n正文\n", encoding="utf-8")
    report = validate_markdown_subagent(tmp_path, "translate", source_dir, target_dir)
    diff = report["diff_summary"]["unit.md"]
    assert diff["line_count_changed"] is False
    assert diff["heading_count_changed"] is False
    assert diff["code_fence_changes"] is False


def test_polish_validation_allows_only_duplicate_heading_reduction(tmp_path: Path):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text(
        "## Preface and Acknowledgements\n\nText.\n\n"
        "## Preface and Acknowledgements\n\nMore text.\n",
        encoding="utf-8",
    )
    (target_dir / "unit.md").write_text(
        "## Preface and Acknowledgements\n\nText.\n\nMore text.\n",
        encoding="utf-8",
    )

    report = validate_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        structural_patterns=(r"^#{1,6}\s",),
        tolerate_duplicate_headings=True,
    )

    assert report["all_passed"] is True
    assert report["structural_warnings"][0]["file"] == "unit.md"


def test_polish_validation_still_rejects_unique_heading_removal(tmp_path: Path):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text("## Unique Section\n\nText.\n", encoding="utf-8")
    (target_dir / "unit.md").write_text("Text.\n", encoding="utf-8")

    report = validate_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        structural_patterns=(r"^#{1,6}\s",),
        tolerate_duplicate_headings=True,
    )

    assert report["all_passed"] is False
    assert "structural marker mismatch" in report["invalid"][0]["reason"]


def test_polish_footnote_normalization_accepts_verified_legacy_notes():
    source = """Body<sup>1</sup> and another<sup>2</sup>.

#### Notes

1. First note.
2. Second note.
"""
    target = """Body[^1] and another[^2].

#### Notes

[^1]: First note.
[^2]: Second note.
"""
    assert validate_polish_footnote_normalization(source, target) == []


def test_polish_footnote_normalization_rejects_unconverted_superscripts():
    source = """Body<sup>1</sup>.

#### Notes

1. First note.
"""
    errors = validate_polish_footnote_normalization(source, source)
    assert any("migration mismatch" in error for error in errors)
    assert any("<sup>" in error for error in errors)


def test_polish_validation_ignores_known_blank_page_image_artifact(tmp_path: Path):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "unit.md").write_text(
        "![Blank white page](../images/page_004_img_001.png)"
        "A completely blank white page with no visible content, text, or markings.\n",
        encoding="utf-8",
    )
    (target_dir / "unit.md").write_text("\n", encoding="utf-8")

    report = validate_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        structural_patterns=(r"!\[[^\]]*\]\([^)]+\)",),
    )

    assert report["all_passed"] is False
    # Removing the fake image is tolerated structurally, but an empty polished
    # unit remains invalid and must not be staged for EPUB building.
    assert report["invalid"][0]["reason"] == "target is empty"


def test_html_validation_quarantines_refusal_candidate(tmp_path: Path):
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    pipeline.compressed_units_dir = tmp_path / "compressed_units"
    pipeline.translated_dir = tmp_path / "translated_compressed"
    pipeline.compressed_units_dir.mkdir()
    pipeline.translated_dir.mkdir()
    (pipeline.compressed_units_dir / "chapter.md").write_text(
        "<span>Ordinary source text.</span>\n", encoding="utf-8"
    )
    (pipeline.translated_dir / "chapter.md").write_text(
        "<span>抱歉，我无法翻译或处理这部分内容。</span>\n", encoding="utf-8"
    )

    report = pipeline.validate_translated_units()

    assert report["safety_blocked"] == ["chapter.md"]
    assert any("refusal/disclaimer" in item["reason"] for item in report["invalid"])


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


def test_prepare_markdown_subagent_records_special_file_roles(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "refs.md").write_text("References\n", encoding="utf-8")
    paths = prepare_markdown_subagent(
        tmp_path, "translate", source_dir, tmp_path / "target", "English", "Chinese",
        file_roles={"refs.md": "bibliography"},
    )
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    prompt = paths["prompt"].read_text(encoding="utf-8")
    assert manifest["file_roles"] == {"refs.md": "bibliography"}
    assert "preserve author names" in prompt


def test_prepare_markdown_subagent_records_read_only_context_hash(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")
    glossary = tmp_path / "translation_entities.json"
    glossary.write_text('{"metadata": {"book_title": "Book"}}', encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        context_files={"translation_entities": glossary},
    )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["context_files"] == {"translation_entities": "translation_entities.json"}
    assert manifest["context_sha256"]["translation_entities"] == hashlib.sha256(
        glossary.read_bytes()
    ).hexdigest()
    assert "translation_entities.json" in paths["prompt"].read_text(encoding="utf-8")


def test_prepare_markdown_subagent_records_explicitly_skipped_context(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("source", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        skipped_context_files=("translation_entities",),
    )

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert manifest["skipped_context_files"] == ["translation_entities"]
    assert "Skipped context files" in paths["prompt"].read_text(encoding="utf-8")


def test_translation_entity_validation_accepts_explicit_skip(tmp_path: Path):
    output_dir = tmp_path / "output"
    output_dir.mkdir()
    (output_dir / "translate_subagent_manifest.json").write_text(
        json.dumps({"skipped_context_files": ["translation_entities"]}),
        encoding="utf-8",
    )

    report = _validate_translation_entities(
        output_dir, {"title": "Book", "translation": {"require_entities": True}}
    )
    assert report == {"valid": True, "skipped": True, "errors": []}


def test_extract_entities_uses_configured_language_and_selected_source_stage(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "title: Book\ntranslation:\n  source_language: French\n"
        "  target_language: Chinese\n  source_stage: ocr\n",
        encoding="utf-8",
    )
    source_dir = tmp_path / "output" / "Book" / "ocr_markdown"
    source_dir.mkdir(parents=True)
    (source_dir / "chapter_001.md").write_text("Bonjour", encoding="utf-8")

    result = extract_entities_command(
        SimpleNamespace(
            config=str(config_path), input=None, source_lang=None, target_lang=None
        )
    )

    assert result == 0
    manifest = json.loads(
        (tmp_path / "output" / "Book" / "entity_subagent_manifest.json").read_text(
            encoding="utf-8"
        )
    )
    assert manifest["source_language"] == "French"
    assert manifest["target_language"] == "Chinese"
    assert manifest["source_stage"] == "ocr"
    assert manifest["files"] == ["chapter_001.md"]


def test_translate_toc_command_prepares_independent_json_task(tmp_path: Path, monkeypatch):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text(
        "title: Book\ntranslation:\n  source_language: German\n"
        "  target_language: Chinese\n",
        encoding="utf-8",
    )
    output_dir = tmp_path / "output" / "Book"
    output_dir.mkdir(parents=True)
    (output_dir / "toc_tree.json").write_text(
        json.dumps({"book_title": "Book", "chapters": []}), encoding="utf-8"
    )

    result = translate_toc_command(
        SimpleNamespace(config=str(config_path), source_language=None, target_language=None)
    )

    assert result == 0
    source = json.loads(
        (output_dir / "toc_translation_source.json").read_text(encoding="utf-8")
    )
    assert source["source_language"] == "German"
    assert source["target_language"] == "Chinese"
    assert source["output_file"] == "toc_tree_translated.json"


def test_translate_skip_entities_is_recorded_in_prompt_and_manifest(
    tmp_path: Path, monkeypatch
):
    monkeypatch.chdir(tmp_path)
    config_path = tmp_path / "config.yaml"
    config_path.write_text("title: Book\n", encoding="utf-8")
    output_dir = tmp_path / "output" / "Book"
    source_dir = output_dir / "ocr_markdown"
    source_dir.mkdir(parents=True)
    (source_dir / "chapter.md").write_text("Source", encoding="utf-8")
    (output_dir / "toc_tree.json").write_text(
        json.dumps({"book_title": "Book", "chapters": []}), encoding="utf-8"
    )

    result = _prepare_pdf_markdown_task(
        SimpleNamespace(
            config=str(config_path),
            source_language=None,
            target_language=None,
            resume=False,
            skip_entities=True,
        ),
        "translate",
    )

    assert result == 0
    manifest = json.loads(
        (output_dir / "translate_subagent_manifest.json").read_text(encoding="utf-8")
    )
    prompt = (output_dir / "translate_subagent_prompt.md").read_text(encoding="utf-8")
    assert manifest["skipped_context_files"] == ["translation_entities"]
    assert "do not invent or expect a translation_entities.json context file" in prompt
    assert "Read translation_entities.json before translating" not in prompt


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


def test_metadata_validation_rejects_refusal_text(tmp_path: Path):
    pipeline = object.__new__(HTMLEpubPipeline)
    pipeline.output_dir = tmp_path
    source = {
        "schema_version": 1,
        "original_title": "Original",
        "target_language": "Chinese",
        "target_language_code": "zh",
        "preserved_metadata": {"author": "", "publisher": ""},
        "translatable_metadata": {"description": "Description", "rights": "Rights"},
        "toc": [],
    }
    target = {
        **source,
        "translated_title": "译名",
        "preserved_metadata": {"author": "", "publisher": ""},
        "toc": [],
        "translated_description": "简介",
        "translated_rights": "抱歉，我无法翻译或处理这部分内容。",
    }
    (tmp_path / "metadata_translation_source.json").write_text(
        json.dumps(source), encoding="utf-8"
    )
    (tmp_path / "translated_metadata.json").write_text(
        json.dumps(target), encoding="utf-8"
    )

    report = pipeline.validate_translated_metadata()
    assert not report["valid"]
    assert report["safety_blocked"] == ["translated_rights"]


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
    assert (tmp_path / "pagination_map.json").exists()
    assert "pagination_map.json" in paths["prompt"].read_text(encoding="utf-8")


def test_refine_local_splits_a_parent_when_children_cover_its_range(tmp_path: Path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    for number in range(1, 5):
        (pages_dir / f"page_{number:03d}.md").write_text(
            f"Page {number} content", encoding="utf-8"
        )
    (tmp_path / "toc_tree.json").write_text(
        json.dumps({
            "chapters": [{
                "title": "Container",
                "level": 1,
                "start_page": 1,
                "end_page": 4,
                "children": [
                    {"title": "First", "level": 2, "start_page": 1, "end_page": 2},
                    {"title": "Second", "level": 2, "start_page": 3, "end_page": 4},
                ],
            }]
        }),
        encoding="utf-8",
    )
    units = RefinedBreakdown(config={}, max_tokens=8000).process_from_toc(
        tmp_path / "input.pdf", tmp_path, "Book"
    )
    assert [unit["unit_id"] for unit in units] == ["chapter_1.1", "chapter_1.2"]


def test_refine_local_does_not_reuse_checkpoint_after_toc_changes(tmp_path: Path):
    pages_dir = tmp_path / "pages"
    pages_dir.mkdir()
    for number in range(1, 3):
        (pages_dir / f"page_{number:03d}.md").write_text("Content", encoding="utf-8")
    toc_path = tmp_path / "toc_tree.json"
    toc_path.write_text(json.dumps({
        "chapters": [{"title": "First", "level": 1, "start_page": 1, "end_page": 2}]
    }), encoding="utf-8")
    refiner = RefinedBreakdown(config={}, max_tokens=8000)
    first = refiner.process_from_toc(tmp_path / "input.pdf", tmp_path, "Book", resume=False)
    assert first[0]["title"] == "First"

    toc_path.write_text(json.dumps({
        "chapters": [{"title": "Renamed", "level": 1, "start_page": 1, "end_page": 2}]
    }), encoding="utf-8")
    second = refiner.process_from_toc(tmp_path / "input.pdf", tmp_path, "Book", resume=True)
    assert second[0]["title"] == "Renamed"


def test_toc_validation_requires_in_place_translated_fields(tmp_path: Path):
    source = {
        "schema_version": 1,
        "book_title": "Original",
        "chapters": [{"title": "Chapter", "level": 1, "start_page": 1, "end_page": 2}],
    }
    target = {
        **source,
        "book_title": "译名",
        "chapters": [{**source["chapters"][0], "title": "章节"}],
    }
    (tmp_path / "toc_translation_source.json").write_text(
        json.dumps({"toc": source}), encoding="utf-8"
    )
    (tmp_path / "toc_tree_translated.json").write_text(
        json.dumps(target), encoding="utf-8"
    )
    report = validate_toc_translation_subagent(tmp_path)
    assert report["valid"] is True
    assert report["resolved_book_title"] == "译名"


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
    )
    units = refiner.process_from_toc(
        pdf_path=tmp_path / "input.pdf",
        output_dir=tmp_path,
        book_title="A Book",
    )

    assert len(units) == 2
    assert (tmp_path / "ocr_markdown" / "chapter_1.md").exists()
    assert (tmp_path / "ocr_markdown" / "chapter_2.md").exists()

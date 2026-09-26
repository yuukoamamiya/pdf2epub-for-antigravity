"""Shared terminology hand-off helpers for command workflows.

PDF and HTML translation use the same book-specific entity glossary contract.
"""

import hashlib
import json
from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_book_context, load_command_config
from pdf2epub.commands.sources import (
    _polished_stage_is_current,
    _resolve_pdf_markdown_source,
)
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.common import book_output_dir


def _prepare_entity_subagent_task(
    output_dir: Path,
    book_title: str,
    source_dir: Path,
    source_stage: str,
    source_language: str,
    target_language: str,
    config: dict,
) -> Path:
    """Create the reusable book-specific terminology hand-off."""
    from pdf2epub.subagent_runtime import resolve_subagent_model

    source_files = sorted(source_dir.glob("*.md"))
    if not source_files:
        raise ValueError(f"No Markdown source found in {source_dir}")
    output_dir.mkdir(parents=True, exist_ok=True)
    model = resolve_subagent_model(config, "extract-entities")
    from pdf2epub.entity_extractor import create_entity_template, create_entity_extraction_prompt

    template_path = output_dir / "translation_entities.template.json"
    template_path.write_text(
        json.dumps(
            create_entity_template(
                book_title,
                source_language,
                target_language,
                (path.name for path in source_files),
            ),
            ensure_ascii=False,
            indent=2,
        ),
        encoding="utf-8",
    )
    manifest = {
        "schema_version": 1,
        "workflow": "antigravity-subagent",
        "task": "extract-entities",
        "source_language": source_language,
        "target_language": target_language,
        "model": model,
        "source_dir": source_dir.relative_to(output_dir).as_posix(),
        "source_stage": source_stage,
        "files": [path.name for path in source_files],
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_files
        },
        "output_file": "translation_entities.json",
        "template_file": "translation_entities.template.json",
    }
    (output_dir / "entity_subagent_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    (output_dir / "entity_subagent_prompt.md").write_text(
        create_entity_extraction_prompt(book_title, (source_language, target_language))
        + f"\n\nRecommended Antigravity model: `{model}`\n",
        encoding="utf-8",
    )
    logger.info(
        f"已生成实体提取 Subagent 任务：{output_dir / 'entity_subagent_prompt.md'}；"
        "完成后由 Subagent 写入 translation_entities.json。"
    )
    return output_dir / "translation_entities.json"


def _entity_context_is_current(
    output_dir: Path,
    source_dir: Path,
    source_language: str | None = None,
    target_language: str | None = None,
) -> bool:
    """Return whether the book entity hand-off matches its source snapshot."""
    entity_path = output_dir / "translation_entities.json"
    manifest_path = output_dir / "entity_subagent_manifest.json"
    if not entity_path.is_file() or not manifest_path.is_file():
        return False
    try:
        from pdf2epub.entity_extractor import validate_entities

        data = json.loads(entity_path.read_text(encoding="utf-8"))
        if validate_entities(data, source_language=source_language, target_language=target_language):
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if Path(manifest.get("source_dir", "")).as_posix() != source_dir.relative_to(output_dir).as_posix():
            return False
        for name, expected in (manifest.get("source_sha256", {}) or {}).items():
            path = source_dir / name
            if not path.is_file() or hashlib.sha256(path.read_bytes()).hexdigest() != expected:
                return False
        return bool(manifest.get("source_sha256"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False

def extract_entities_command(args):
    """Prepare entity extraction for an Antigravity Subagent (local only)."""
    config, _ = load_command_config(args)
    policy = PipelinePolicy.from_config(config)
    if not policy.requires_entities:
        logger.info(
            "This pipeline does not require entity extraction; the hand-off is skipped."
        )
        return 0
    book_title = config.get("title") or (Path(args.input).stem if args.input else None)
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    output_dir = book_output_dir(book_title)
    source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
    # EPUB terminology is extracted from the compressed translation units so
    # the same book-wide entity contract can be used by both pipelines.
    epub_units = output_dir / "compressed_units"
    is_epub = bool(config.get("input_epub") or (output_dir / "input.epub").is_file())
    if list(epub_units.glob("*.md")) and is_epub:
        source_dir, source_stage = epub_units, "epub-compressed"
    elif source_stage != "polished" or not _polished_stage_is_current(
        output_dir, source_dir, output_dir / "ocr_markdown"
    ):
        logger.error(
            "PDF entity extraction requires a current validated polished source "
            "for both OCR-derived and native-text PDFs. Run polish and "
            "polish-validate first."
        )
        return 1
    source_language = args.source_lang or policy.source_language or "English"
    target_language = args.target_lang or policy.target_language or "Chinese"
    try:
        _prepare_entity_subagent_task(
            output_dir,
            book_title,
            source_dir,
            source_stage,
            source_language,
            target_language,
            config,
        )
    except Exception as exc:
        logger.error(f"Could not prepare entity task: {exc}")
        return 1
    return 0


def extract_entities_validate_command(args):
    """Validate the optional entity JSON written by a workspace Subagent."""
    from pdf2epub.entity_extractor import validate_entities

    context = load_book_context(args, "extract-entities-validate")
    if context is None:
        return 1
    config = context.config
    policy = PipelinePolicy.from_config(config)
    if not policy.requires_entities:
        logger.info(
            "This pipeline does not require entity extraction; validation skipped."
        )
        return 0
    book_title = context.book_title
    entity_path = context.output_dir / "translation_entities.json"
    if not entity_path.exists():
        logger.error(f"Entity output not found: {entity_path}")
        return 1
    try:
        data = json.loads(entity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"Invalid entity JSON: {exc}")
        return 1
    errors = validate_entities(
        data,
        book_title,
        policy.source_language,
        policy.target_language,
    )
    manifest_path = entity_path.parent / "entity_subagent_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            source_dir = entity_path.parent / manifest.get("source_dir", "")
            expected_hashes = manifest.get("source_sha256", {})
            for name, expected in expected_hashes.items():
                source_path = source_dir / name
                if not source_path.is_file():
                    errors.append(f"entity source file is missing: {name}")
                elif hashlib.sha256(source_path.read_bytes()).hexdigest() != expected:
                    errors.append(f"entity source file changed after extraction: {name}")
        except (OSError, json.JSONDecodeError, TypeError):
            errors.append("invalid entity_subagent_manifest.json")
    report = {
        "task": "extract-entities",
        "valid": not errors,
        "errors": errors,
        "entity_sha256": hashlib.sha256(entity_path.read_bytes()).hexdigest(),
        "file": str(entity_path),
    }
    (entity_path.parent / "translation_entities_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if errors:
        for error in errors:
            logger.error(error)
        return 1
    logger.success(f"Entity Subagent output validated: {entity_path}")
    return 0

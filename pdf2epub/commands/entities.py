"""Entity extraction command handlers.

The format-neutral entity contract lives in :mod:`pdf2epub.entity_context`.
This module keeps command-specific PDF preparation and validation.
"""

import hashlib
import json
from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_book_context, load_command_config
from pdf2epub.commands.sources import (
    _polished_stage_is_current,
    _resolve_pdf_markdown_source,
    _resolve_pdf_polish_source,
)
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.entity_context import (
    prepare_entity_subagent_task,
    validate_entity_source_manifest,
)
from pdf2epub.utils.paths import book_output_dir
from pdf2epub.workflow_contracts import atomic_write_json


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
        output_dir, source_dir, _resolve_pdf_polish_source(output_dir, config)[0]
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
        prepare_entity_subagent_task(
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
            errors.extend(
                validate_entity_source_manifest(manifest, entity_path.parent, source_dir)
            )
        except (OSError, json.JSONDecodeError, TypeError):
            errors.append("invalid entity_subagent_manifest.json")
    report = {
        "task": "extract-entities",
        "valid": not errors,
        "errors": errors,
        "entity_sha256": hashlib.sha256(entity_path.read_bytes()).hexdigest(),
        "file": str(entity_path),
    }
    atomic_write_json(
        entity_path.parent / "translation_entities_validation.json",
        report,
    )
    if errors:
        for error in errors:
            logger.error(error)
        return 1
    logger.success(f"Entity Subagent output validated: {entity_path}")
    return 0

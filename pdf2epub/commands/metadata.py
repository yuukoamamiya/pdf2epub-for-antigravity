"""CLI entry points for metadata enrichment handoffs."""

from __future__ import annotations

import json

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.input_paths import resolve_book_input_path
from pdf2epub.metadata_enrichment.service import (
    enrichment_status,
    fetch_enrichment,
    prepare_enrichment,
    validate_enrichment,
)


def _context(args, operation):
    context = load_book_context(args, operation, init_logging=operation != "metadata-status")
    if not context:
        return None
    config = dict(context.config)
    if PipelinePolicy.from_config(config).metadata_enrichment_mode == "disabled":
        config["metadata_enrichment"] = {**(config.get("metadata_enrichment") or {}), "mode": "disabled"}
    return context.output_dir, config, context.config_path


def _emit(result: dict, as_json: bool) -> int:
    if as_json:
        print(json.dumps(result, ensure_ascii=False, indent=2))
    else:
        logger.info("Metadata enrichment: {}", result.get("state", result.get("status", "unknown")))
        for error in result.get("errors", []):
            logger.error("{}", error)
        for key, value in result.items():
            if key not in {"errors", "fields", "artifacts"}:
                logger.info("{}: {}", key, value)
    cover_status = (
        result.get("cover_candidate", {}).get("status")
        if isinstance(result.get("cover_candidate"), dict)
        else None
    )
    failed = (
        result.get("state") in {"invalid", "blocked", "request_failed"}
        or cover_status in {"invalid", "blocked", "request_failed"}
        or bool(result.get("missing_required_fields"))
        or bool(result.get("errors"))
    )
    return 1 if failed else 0


def metadata_prepare_command(args):
    context = _context(args, "metadata-prepare")
    if not context:
        return 1
    out, config, config_path = context
    source_key = "input_pdf" if config.get("input_pdf") else "input_epub" if config.get("input_epub") else "input" if config.get("input") else None
    source_path = resolve_book_input_path(config_value=config.get(source_key), config_path=config_path, output_dir=out).expanduser().resolve() if source_key else None
    epub_path = resolve_book_input_path(config_value=config.get("input_epub"), config_path=config_path, output_dir=out, extensions=[".epub"]).expanduser().resolve() if config.get("input_epub") else None
    return _emit(prepare_enrichment(out, config, source_path=source_path, epub_path=epub_path, config_path=config_path, resume=args.resume), args.json)


def metadata_fetch_command(args):
    context = _context(args, "metadata-fetch")
    if not context:
        return 1
    out, config, config_path = context
    return _emit(fetch_enrichment(out, config, retry=args.retry, cover_url=args.cover_url, config_path=config_path), args.json)


def metadata_validate_command(args):
    context = _context(args, "metadata-validate")
    if not context:
        return 1
    out, config, config_path = context
    return _emit(validate_enrichment(out, config, config_path=config_path), args.json)


def metadata_status_command(args):
    context = _context(args, "metadata-status")
    if not context:
        return 1
    out, config, config_path = context
    return _emit(enrichment_status(out, config, config_path=config_path), args.json)

"""PDF translated TOC command handlers."""

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.toc_translation_workflow import (
    prepare_toc_translation_subagent,
    validate_toc_translation_subagent,
)
from pdf2epub.subagent_runtime import resolve_subagent_model


def translate_toc_command(args):
    """Prepare the independent JSON TOC hand-off for a translation Subagent."""
    context = load_book_context(args, "toc-translation")
    if context is None:
        return 1
    translation = context.config.get("translation", {})
    source_language = args.source_language or translation.get(
        "source_language", "English"
    )
    target_language = args.target_language or translation.get(
        "target_language", "Chinese"
    )
    try:
        paths = prepare_toc_translation_subagent(
            context.output_dir,
            source_language,
            target_language,
            config=context.config,
        )
    except Exception as exc:
        logger.error(f"Could not prepare TOC translation task: {exc}")
        return 1
    logger.success(f"Wrote TOC Subagent prompt: {paths['prompt']}")
    logger.info(
        "Recommended Antigravity model: "
        f"{resolve_subagent_model(context.config, 'toc-translation')}"
    )
    logger.info("完成后运行 translate-toc-validate，或运行 translate-validate 进行全量校验。")
    return 0


def translate_toc_validate_command(args):
    """Validate the independent translated PDF TOC contract locally."""
    context = load_book_context(args, "toc-translation-validate")
    if context is None:
        return 1
    report = validate_toc_translation_subagent(context.output_dir)
    if report["valid"]:
        logger.success("Translated TOC validation passed")
        return 0
    for error in report["errors"]:
        logger.error(f"TOC: {error}")
    return 1


__all__ = ["translate_toc_command", "translate_toc_validate_command"]

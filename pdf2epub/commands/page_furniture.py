"""Command handlers for the one-time translated page-furniture repair."""

from loguru import logger

from pdf2epub.commands.markdown import _load_pdf_file_roles
from pdf2epub.commands.runtime import load_book_context
from pdf2epub.pipeline_policy import PipelinePolicy


def repair_page_furniture_command(args):
    """Prepare the existing translated Markdown repair hand-off."""
    context = load_book_context(args, "page-furniture-repair")
    if context is None:
        return 1
    policy = PipelinePolicy.from_config(context.config)
    if not policy.requires_translation:
        logger.error("Page-furniture repair requires a translation pipeline.")
        return 1
    baseline = context.output_dir / "translate_validation.json"
    if not baseline.is_file():
        logger.error(
            "A current translate-validation report is required before repair. "
            "Run translate-validate first."
        )
        return 1
    try:
        from pdf2epub.page_furniture_repair import prepare_page_furniture_repair

        paths = prepare_page_furniture_repair(
            context.output_dir,
            context.config,
            policy.source_language or "Original",
            policy.target_language or "Chinese",
            resume=bool(getattr(args, "resume", False)),
        )
    except Exception as exc:
        logger.error(f"Could not prepare page-furniture repair: {exc}")
        return 1
    logger.success(f"Wrote repair prompt: {paths['prompt']}")
    logger.success(f"Wrote repair manifest: {paths['manifest']}")
    logger.info(
        "Repair candidates: "
        f"{paths['candidate_count']} target hints; model context estimate "
        f"{paths['repair_context_tokens']} tokens vs "
        f"{paths['full_estimated_tokens']} full-file tokens."
    )
    logger.warning(
        "请把 page-furniture-repair_subagent_prompt.md 作为 Antigravity 的总入口；"
        "它会调度 page-furniture-repair_worker_handoffs/ 下的 worker，"
        "并在通过修复校验后自动打包。"
    )
    return 0


def repair_page_furniture_validate_command(args):
    """Validate and stage repaired translated Markdown."""
    context = load_book_context(args, "page-furniture-repair-validate")
    if context is None:
        return 1
    try:
        from pdf2epub.page_furniture_repair import validate_page_furniture_repair

        report = validate_page_furniture_repair(
            context.output_dir,
            file_roles=_load_pdf_file_roles(context.output_dir),
        )
    except Exception as exc:
        logger.error(f"Could not validate page-furniture repair: {exc}")
        return 1
    logger.info(
        f"Page-furniture repair validation: {report['completed']}/{report['total']} "
        f"files checked, {len(report.get('repaired_files', []))} changed"
    )
    for item in report.get("invalid", [])[:10]:
        logger.error(f"Invalid: {item['file']}: {item['reason']}")
    if report.get("all_passed"):
        logger.success(
            "Repaired translations staged in "
            f"{context.output_dir / 'translated' / 'validated'}"
        )
        return 0
    return 1


__all__ = [
    "repair_page_furniture_command",
    "repair_page_furniture_validate_command",
]

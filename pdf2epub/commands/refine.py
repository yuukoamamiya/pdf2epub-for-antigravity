"""PDF structure refinement command handlers.

TOC hand-off preparation and local unit generation are deterministic workflow
steps; structural judgment remains delegated to the workspace Subagent.
"""

import json

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.ocr_correction import select_refinement_pages
from pdf2epub.ocr_progress import assess_progress


def _require_complete_ocr(output_dir) -> bool:
    """Stop structure work until the physical OCR page set is complete."""
    probe = {}
    probe_path = output_dir / "pdf_text_probe.json"
    if probe_path.is_file():
        try:
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            probe = {}
    report = assess_progress(
        output_dir / "pages",
        expected_total_pages=probe.get("page_count"),
        expected_source_sha256=probe.get("source_sha256"),
        require_sidecars=True,
    )
    if report["ready"]:
        return True
    logger.error("OCR is incomplete; structure refinement is blocked")
    for error in report["errors"][:10]:
        logger.error(f"OCR readiness: {error}")
    return False


def _select_refinement_pages(output_dir, config):
    try:
        return select_refinement_pages(
            output_dir,
            require_correction=True,
            config=config,
        )
    except ValueError as exc:
        logger.error(str(exc))
        return None, None


def refine_command(args):
    """Prepare the PDF structure task for an Antigravity Subagent."""
    return refine_prepare_command(args)


def refine_prepare_command(args):
    """Prepare a PDF TOC task for an Antigravity workspace subagent."""
    from pdf2epub.refine.subagent_workflow import prepare_refine_subagent

    context = load_book_context(args, "refine-prepare")
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir
    if not _require_complete_ocr(output_dir):
        return 1
    pages_dir, _page_source_kind = _select_refinement_pages(output_dir, config)
    if pages_dir is None:
        return 1
    refine_config = config.get("refine", {})
    max_tokens = args.max_tokens or refine_config.get("max_tokens", 8000)
    try:
        paths = prepare_refine_subagent(
            output_dir,
            book_title,
            max_tokens,
            config=config,
            pages_dir=pages_dir,
        )
    except Exception as exc:
        logger.error(f"Could not prepare refine task: {exc}")
        return 1

    logger.success(f"Wrote subagent prompt: {paths['prompt']}")
    logger.success(f"Wrote subagent manifest: {paths['manifest']}")
    logger.info(
        "请在 Antigravity 中让子 Agent 阅读 refine_subagent_prompt.md，"
        "并在同一目录写入 toc_tree.json。完成后先运行 illustration-prepare（如有候选则交给工作区 Subagent，"
        "再运行 illustration-validate/illustration-apply），最后运行 pdf2epub refine-local。"
    )
    return 0


def refine_local_command(args):
    """Consume a subagent TOC and generate PDF work units without an LLM."""
    from pdf2epub.refine import RefinedBreakdown

    context = load_book_context(args, "refine-local")
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir
    if not _require_complete_ocr(output_dir):
        return 1
    pages_dir, _page_source_kind = _select_refinement_pages(output_dir, config)
    if pages_dir is None:
        return 1
    refine_config = config.get("refine", {})
    max_tokens = args.max_tokens or refine_config.get("max_tokens", 8000)
    try:
        refiner = RefinedBreakdown(
            config=config,
            max_tokens=max_tokens,
        )
        units = refiner.process_from_toc(
            pdf_path=output_dir / "input.pdf",
            output_dir=output_dir,
            book_title=book_title,
            resume=args.resume,
            pages_dir=pages_dir,
        )
    except Exception as exc:
        logger.error(f"Local refine failed: {exc}")
        return 1

    logger.success(f"Local refine complete: {len(units)} units generated")
    return 0


def illustration_prepare_command(args):
    """Prepare a compact full-page illustration review hand-off."""
    from pdf2epub.refine.illustration_prepare import prepare_illustration_subagent

    context = load_book_context(args, "illustration-prepare")
    if context is None:
        return 1
    if not _require_complete_ocr(context.output_dir):
        return 1
    illustration_config = context.config.get("illustration", {})
    if not isinstance(illustration_config, dict):
        illustration_config = {}
    review_dpi = getattr(args, "review_dpi", None)
    if review_dpi is None:
        review_dpi = illustration_config.get("review_dpi", 150)
    large_block_area = getattr(args, "large_block_area", None)
    if large_block_area is None:
        large_block_area = illustration_config.get("large_block_area", 0.55)
    max_text_chars = getattr(args, "max_text_chars", None)
    if max_text_chars is None:
        max_text_chars = illustration_config.get("max_text_chars", 180)
    try:
        paths = prepare_illustration_subagent(
            context.output_dir,
            book_title=context.book_title,
            config=context.config,
            review_dpi=review_dpi,
            large_block_area=large_block_area,
            max_text_chars=max_text_chars,
        )
    except Exception as exc:
        logger.error(f"Could not prepare illustration task: {exc}")
        return 1

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    logger.success(f"Wrote illustration candidate report: {paths['report']}")
    logger.success(f"Wrote illustration subagent manifest: {paths['manifest']}")
    logger.success(f"Wrote illustration subagent prompt: {paths['prompt']}")
    if manifest.get("status") == "pending_review":
        logger.info(
            "请让工作区 Subagent 只复核 illustration_subagent_prompt.md 列出的候选页，"
            "并写入 illustration_decisions.json。"
        )
    else:
        logger.info("没有发现需要复核的整页插图候选，本次不需要 Subagent。")
    return 0


def illustration_validate_command(args):
    """Validate the compact full-page illustration decisions."""
    from pdf2epub.refine.illustration_prepare import validate_illustration_decisions

    context = load_book_context(args, "illustration-validate")
    if context is None:
        return 1
    try:
        report = validate_illustration_decisions(
            context.output_dir,
            config=context.config,
        )
    except Exception as exc:
        logger.error(f"Could not validate illustration decisions: {exc}")
        return 1
    if report.get("valid"):
        logger.success("Full-page illustration decisions validated")
        return 0
    logger.error(f"Illustration validation status: {report.get('status', 'invalid')}")
    for error in report.get("errors", [])[:10]:
        logger.error(f"Illustration review: {error}")
    return 1


def illustration_apply_command(args):
    """Materialize validated full-page illustration bindings."""
    from pdf2epub.refine.illustration_prepare import apply_illustration_bindings

    context = load_book_context(args, "illustration-apply")
    if context is None:
        return 1
    try:
        report = apply_illustration_bindings(
            context.output_dir,
            config=context.config,
        )
    except Exception as exc:
        logger.error(f"Could not apply illustration bindings: {exc}")
        return 1
    if report.get("valid"):
        logger.success(
            f"Wrote illustration bindings: {context.output_dir / 'illustration_bindings.json'} "
            f"({len(report.get('bindings', []))} full-page insert(s))"
        )
        return 0
    logger.error("Illustration bindings were not applied because validation failed")
    for error in report.get("errors", [])[:10]:
        logger.error(f"Illustration binding: {error}")
    return 1


def footnote_prepare_command(args):
    """Prepare a compact layout-only footnote review hand-off.

    The local step reads OCR layout sidecars and never rewrites Markdown.  A
    workspace Subagent is needed only for ambiguous block assignments.
    """
    from pdf2epub.refine.footnote_prepare import prepare_footnote_subagent

    context = load_book_context(args, "footnote-prepare")
    if context is None:
        return 1
    output_dir = context.output_dir
    if not (output_dir / "toc_tree.json").is_file():
        logger.error("footnote-prepare requires toc_tree.json; run refine-local first")
        return 1
    if not (output_dir / "ocr_markdown").is_dir():
        logger.error("footnote-prepare requires ocr_markdown; run refine-local first")
        return 1

    footnote_config = context.config.get("footnotes", {})
    if not isinstance(footnote_config, dict):
        footnote_config = {}
    bottom_ratio = getattr(args, "bottom_ratio", None)
    if bottom_ratio is None:
        bottom_ratio = footnote_config.get("bottom_ratio", 0.64)
    context_blocks = getattr(args, "context_blocks", None)
    if context_blocks is None:
        context_blocks = footnote_config.get("context_blocks", 2)
    try:
        paths = prepare_footnote_subagent(
            output_dir,
            book_title=context.book_title,
            config=context.config,
            bottom_ratio=bottom_ratio,
            context_blocks=context_blocks,
        )
    except Exception as exc:
        logger.error(f"Could not prepare footnote task: {exc}")
        return 1

    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    logger.success(f"Wrote footnote candidate report: {paths['report']}")
    logger.success(f"Wrote footnote subagent manifest: {paths['manifest']}")
    logger.success(f"Wrote footnote subagent prompt: {paths['prompt']}")
    if manifest["status"] == "pending_review":
        logger.info(
            "请让工作区 Subagent 只复核 footnote_subagent_prompt.md 中列出的局部窗口，"
            "并写入 footnote_decisions.json。"
        )
    else:
        logger.info("脚注候选均达到本地预筛选条件，本次不需要 Subagent 复核。")
    return 0


def footnote_validate_command(args):
    """Validate the Subagent's compact footnote layout decisions."""
    from pdf2epub.refine.footnote_prepare import validate_footnote_decisions

    context = load_book_context(args, "footnote-validate")
    if context is None:
        return 1
    try:
        report = validate_footnote_decisions(
            context.output_dir,
            config=context.config,
        )
    except Exception as exc:
        logger.error(f"Could not validate footnote decisions: {exc}")
        return 1
    if report.get("valid"):
        logger.success("Footnote layout decisions validated")
        return 0
    logger.error(f"Footnote layout validation status: {report.get('status', 'invalid')}")
    for error in report.get("errors", [])[:10]:
        logger.error(f"Footnote layout: {error}")
    for item in report.get("human_review_required", [])[:10]:
        logger.error(f"Footnote layout requires human review: {item}")
    return 1


def footnote_apply_command(args):
    """Materialize validated page footnotes at logical chapter ends."""
    from pdf2epub.refine.footnote_apply import apply_footnote_normalization

    context = load_book_context(args, "footnote-apply")
    if context is None:
        return 1
    try:
        report = apply_footnote_normalization(
            context.output_dir,
            config=context.config,
        )
    except Exception as exc:
        logger.error(f"Could not apply footnote normalization: {exc}")
        return 1
    if report.get("valid"):
        logger.success(
            f"Wrote chapter-end footnote source: "
            f"{context.output_dir / 'footnote_normalized'} "
            f"({report.get('moved_count', 0)} moved, "
            f"{sum(report.get('preserved_roles', {}).values())} preserved citations/references)"
        )
        return 0
    logger.error("Footnote normalization requires review; original OCR Markdown was preserved")
    for error in report.get("errors", [])[:10]:
        logger.error(f"Footnote normalization: {error}")
    return 1

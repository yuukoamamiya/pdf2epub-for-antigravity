"""PDF structure refinement command handlers.

TOC hand-off preparation and local unit generation are deterministic workflow
steps; structural judgment remains delegated to the workspace Subagent.
"""

from loguru import logger

from pdf2epub.commands.runtime import load_book_context


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
    refine_config = config.get("refine", {})
    max_tokens = args.max_tokens or refine_config.get("max_tokens", 8000)
    try:
        paths = prepare_refine_subagent(output_dir, book_title, max_tokens, config=config)
    except Exception as exc:
        logger.error(f"Could not prepare refine task: {exc}")
        return 1

    logger.success(f"Wrote subagent prompt: {paths['prompt']}")
    logger.success(f"Wrote subagent manifest: {paths['manifest']}")
    logger.info(
        "请在 Antigravity 中让子 Agent 阅读 refine_subagent_prompt.md，"
        "并在同一目录写入 toc_tree.json。完成后运行 pdf2epub refine-local。"
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
        )
    except Exception as exc:
        logger.error(f"Local refine failed: {exc}")
        return 1

    logger.success(f"Local refine complete: {len(units)} units generated")
    return 0

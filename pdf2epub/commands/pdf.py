"""PDF packaging command handlers.

Source-stage selection and EPUB packaging are local operations. Translation and
source validation remain delegated to their existing command contracts.
"""

import traceback
from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.commands.sources import (
    _resolve_pdf_markdown_source,
    _resolve_pdf_polish_source,
)
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.common import sanitize_filename
from pdf2epub.validation_receipts import validation_receipt_is_current


def _validate_pdf_source_stage(
    args,
    source_stage: str,
    output_dir: Path | None = None,
    ocr_dir: Path | None = None,
    config_path: Path | None = None,
) -> int:
    """Check the immutable polish receipt without rerunning upstream gates."""
    if source_stage == "polished":
        if output_dir is not None and ocr_dir is not None:
            from pdf2epub.commands.sources import _polished_stage_is_current

            polished_dir = output_dir / "polished_markdown" / "validated"
            if _polished_stage_is_current(output_dir, polished_dir, ocr_dir):
                if config_path is not None and validation_receipt_is_current(
                    output_dir / "polish_validation.json",
                    ocr_dir,
                    polished_dir,
                    task="polish",
                    output_dir=output_dir,
                    config_path=config_path,
                    require_build_inputs=True,
                ):
                    return 0
        logger.error(
            "Refusing to build: polish receipt is missing or stale. "
            "Run polish-validate explicitly, then retry packaging."
        )
        return 1
    logger.error(
        "PDF packaging requires the current validated polished source. "
        "Run polish and polish-validate before building."
    )
    return 1


def _validate_translated_pdf(
    args,
    output_dir: Path | None = None,
    source_dir: Path | None = None,
    target_dir: Path | None = None,
    config_path: Path | None = None,
) -> int:
    """Check the immutable translation receipt without rerunning validation."""
    if output_dir is not None and source_dir is not None and target_dir is not None:
        report_path = output_dir / "translate_validation.json"
        if validation_receipt_is_current(
            report_path,
            source_dir,
            target_dir,
            task="translate",
            output_dir=output_dir,
            config_path=config_path,
            require_build_inputs=config_path is not None,
        ):
            return 0
    logger.error(
        "Refusing to build: translation receipt is missing or stale. "
        "Run translate-validate explicitly, then retry packaging."
    )
    return 1


def _warn_if_translated_checkpoint_is_ignored(
    policy: PipelinePolicy,
    args,
    output_dir: Path,
    source_dir: Path,
    config_path: Path | None = None,
) -> bool:
    """Warn when a current translation checkpoint exists but is not packaged.

    This is a best-effort guard against silently building the original-language
    EPUB after a completed translation. It never changes the build target or
    the default ``--translated`` behavior.
    """
    if not policy.requires_translation or getattr(args, "translated", False):
        return False
    target_dir = output_dir / "translated" / "validated"
    try:
        is_current = validation_receipt_is_current(
            output_dir / "translate_validation.json",
            source_dir,
            target_dir,
            task="translate",
            output_dir=output_dir,
            config_path=config_path,
            require_build_inputs=config_path is not None,
        )
    except (OSError, TypeError, ValueError):
        return False
    if not is_current:
        return False
    logger.warning(
        "检测到当前有效的译文 checkpoint，但本次 build-epub 未带 --translated："
        "将只构建原稿 EPUB。如需译文版，请运行 build-epub --translated。"
    )
    logger.warning(
        "A current translation checkpoint exists, but --translated was not "
        "passed; only the original-language EPUB will be built. Re-run with "
        "build-epub --translated to package the translation."
    )
    return True


def _load_build_epub():
    """Import the EPUB builder lazily so command parsing stays lightweight."""
    from ..build_epub import build_epub

    return build_epub


def _run_epub_build_stages(stages, build_fn=None):
    """Build each requested EPUB stage, collecting failures.

    ``stages`` is an ordered sequence of ``(label, BuildEpubConfig)`` pairs.
    Every stage is attempted even when an earlier one fails, so a failed
    English build cannot hide whether the translated EPUB is usable. Returns
    ``True`` only when all requested stages succeeded.
    """
    if build_fn is None:
        build_fn = _load_build_epub()
    all_succeeded = True
    for label, build_config in stages:
        try:
            epub_path = build_fn(build_config)
        except Exception as exc:
            all_succeeded = False
            logger.error(
                f"[{label} Build Failed] {label} EPUB build failed: {exc}"
            )
            traceback.print_exc()
            continue
        logger.success(f"{label} EPUB created: {epub_path}")
    return all_succeeded


def build_epub_command(args):
    """Handle the build-epub subcommand (toc_tree.json driven)."""
    from ..build_epub import BuildEpubConfig

    context = load_book_context(args, "build-epub")
    if context is None:
        return 1
    config = context.config
    policy = PipelinePolicy.from_config(config)

    only_translated = bool(getattr(args, "only_translated", False))
    translated_mode = bool(args.translated or only_translated)
    build_english = bool(args.translated and not only_translated)
    build_translated = translated_mode

    if translated_mode and not policy.requires_translation:
        logger.error(
            "pipeline: epub_conversion builds the original-language EPUB; "
            "--translated/--only-translated is not available."
        )
        return 1
    book_title = context.book_title
    output_dir = context.output_dir
    toc_tree_path = output_dir / "toc_tree.json"

    if not toc_tree_path.exists():
        logger.error(f"toc_tree.json not found at {toc_tree_path}")
        logger.info("Run 'refine-prepare', use a Subagent, then 'refine-local' first to generate toc_tree.json")
        return 1

    # V2 architecture stores Subagent results in validated/ subdirectories.
    source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
    polish_input_dir, _ = _resolve_pdf_polish_source(output_dir, config)
    if translated_mode:
        markdown_dir = output_dir / "translated" / "validated"
        logger.info("Building EPUB from translated markdown...")
        if source_stage != "polished":
            logger.error(
                "Refusing to build translated PDF EPUB: a current validated "
                "polished source is required for both OCR-derived and native-text "
                "PDFs. Run polish and polish-validate first."
            )
            return 1
        source_validation = _validate_pdf_source_stage(
            args,
            source_stage,
            output_dir=output_dir,
            ocr_dir=polish_input_dir,
            config_path=context.config_path,
        )
        if source_validation != 0:
            logger.error("Refusing to build: the source stage is not validated")
            return 1
        if not source_dir.is_dir() or not any(source_dir.glob("*.md")):
            logger.error(f"Source Markdown not found: {source_dir}")
            return 1
    else:
        _warn_if_translated_checkpoint_is_ignored(
            policy,
            args,
            output_dir,
            source_dir,
            config_path=context.config_path,
        )
        markdown_dir = source_dir
        logger.info(f"Building EPUB from {source_stage} markdown...")

    if not markdown_dir.is_dir() or not any(markdown_dir.glob("*.md")):
        logger.error(f"Markdown directory not found: {markdown_dir}")
        logger.info("Run the corresponding Subagent task and its -validate command first")
        return 1

    validation_result = (
        _validate_translated_pdf(
            args,
            output_dir=output_dir,
            source_dir=source_dir,
            target_dir=markdown_dir,
            config_path=context.config_path,
        )
        if translated_mode
        else 0
    )
    if not translated_mode:
        validation_result = _validate_pdf_source_stage(
            args,
            source_stage,
            output_dir=output_dir,
            ocr_dir=polish_input_dir,
            config_path=context.config_path,
        )
    if validation_result != 0:
        logger.error("Refusing to build from unvalidated Subagent output")
        return 1

    # Set up images directory
    images_dir = output_dir / "images"
    if not images_dir.exists():
        images_dir = None

    # Set up cover image
    cover_image = None
    if args.cover:
        cover_path = Path(args.cover)
        if cover_path.exists():
            cover_image = cover_path
        else:
            logger.warning(f"Cover image not found: {args.cover}")
    else:
        # Auto-detect cover in images directory
        if images_dir:
            for cover_name in ["cover.jpg", "cover.jpeg", "cover.png", "cover.gif"]:
                cover_path = images_dir / cover_name
                if cover_path.exists():
                    cover_image = cover_path
                    logger.info(f"Auto-detected cover image: {cover_path}")
                    break

    # Get target language from config
    target_language = (
        (policy.target_language or "Original")
        if not policy.requires_translation
        else (policy.target_language or "Chinese")
    )

    stages = []
    if build_english:
        source_language = policy.source_language or "English"
        safe_title = sanitize_filename(book_title)
        english_epub = output_dir / f"{safe_title}_en.epub"
        english_config = BuildEpubConfig(
            book_title=book_title,
            output_dir=output_dir,
            markdown_dir=source_dir,
            toc_tree_path=toc_tree_path,
            images_dir=images_dir,
            cover_image=cover_image,
            translated=False,
            target_language=source_language,
            config=config,
            output_epub=english_epub,
        )
        stages.append(("English", english_config))

    # Create config
    build_config = BuildEpubConfig(
        book_title=book_title,
        output_dir=output_dir,
        markdown_dir=markdown_dir,
        toc_tree_path=toc_tree_path,
        images_dir=images_dir,
        cover_image=cover_image,
        translated=build_translated,
        target_language=target_language,
        config=config,
    )

    stages.append(("Translated" if build_translated else "Original", build_config))

    return 0 if _run_epub_build_stages(stages) else 1

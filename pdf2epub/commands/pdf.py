"""PDF packaging command handlers.

Source-stage selection and EPUB packaging are local operations. Translation and
source validation remain delegated to their existing command contracts.
"""

from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.commands.sources import _resolve_pdf_markdown_source
from pdf2epub.utils.common import (
    sanitize_filename,
)


def _validate_pdf_source_stage(args, source_stage: str) -> int:
    """Validate a Subagent-produced source stage when one is selected."""
    if source_stage == "polished":
        from pdf2epub.commands.markdown import polish_validate_command

        return polish_validate_command(args)
    logger.error(
        "PDF packaging requires the current validated polished source. "
        "Run polish and polish-validate before building."
    )
    return 1


def _validate_translated_pdf(args) -> int:
    """Run the existing PDF translation validator before packaging."""
    from pdf2epub.commands.markdown import translate_validate_command

    return translate_validate_command(args)


def build_epub_command(args):
    """Handle the build-epub subcommand (toc_tree.json driven)."""
    import asyncio
    from pathlib import Path
    from ..build_epub import build_epub, BuildEpubConfig

    context = load_book_context(args, "build-epub")
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir
    toc_tree_path = output_dir / "toc_tree.json"

    if not toc_tree_path.exists():
        logger.error(f"toc_tree.json not found at {toc_tree_path}")
        logger.info("Run 'refine-prepare', use a Subagent, then 'refine-local' first to generate toc_tree.json")
        return 1

    # V2 architecture stores Subagent results in validated/ subdirectories.
    source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
    if args.translated:
        markdown_dir = output_dir / "translated" / "validated"
        logger.info("Building EPUB from translated markdown...")
        if source_stage != "polished":
            logger.error(
                "Refusing to build translated PDF EPUB: a current validated "
                "polished source is required for both OCR-derived and native-text "
                "PDFs. Run polish and polish-validate first."
            )
            return 1
        source_validation = _validate_pdf_source_stage(args, source_stage)
        if source_validation != 0:
            logger.error("Refusing to build: the English source stage is not validated")
            return 1
        if not source_dir.is_dir() or not any(source_dir.glob("*.md")):
            logger.error(f"English source Markdown not found: {source_dir}")
            return 1
    else:
        markdown_dir = source_dir
        logger.info(f"Building EPUB from {source_stage} markdown...")

    if not markdown_dir.is_dir() or not any(markdown_dir.glob("*.md")):
        logger.error(f"Markdown directory not found: {markdown_dir}")
        logger.info("Run the corresponding Subagent task and its -validate command first")
        return 1

    validation_result = _validate_translated_pdf(args) if args.translated else 0
    if not args.translated:
        validation_result = _validate_pdf_source_stage(args, source_stage)
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
    target_language = config.get("translation", {}).get("target_language", "Chinese")

    if args.translated:
        source_language = config.get("translation", {}).get(
            "source_language", "English"
        )
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
        try:
            english_path = build_epub(english_config)
            logger.success(f"English EPUB created: {english_path}")
        except Exception as e:
            logger.error(f"English EPUB build failed: {e}")
            import traceback
            traceback.print_exc()
            return 1

    # Create config
    build_config = BuildEpubConfig(
        book_title=book_title,
        output_dir=output_dir,
        markdown_dir=markdown_dir,
        toc_tree_path=toc_tree_path,
        images_dir=images_dir,
        cover_image=cover_image,
        translated=args.translated,
        target_language=target_language,
        config=config
    )

    try:
        # Build EPUB
        epub_path = build_epub(build_config)
        logger.success(f"EPUB created: {epub_path}")
        return 0
    except Exception as e:
        logger.error(f"EPUB build failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

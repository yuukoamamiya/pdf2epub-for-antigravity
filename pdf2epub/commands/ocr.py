"""Page-level OCR command handler.

OCR is the only workflow stage allowed to call the configured OCR service.
"""

import json
import shutil

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.utils.common import resolve_book_input_path


def ocr_pages_command(args):
    """Handle the ocr-pages subcommand (page-level OCR)."""
    from pdf2epub.ocr_pages import ocr_full_book_pagewise

    context = load_book_context(args, "ocr-pages")
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir

    # Find PDF
    pdf_path = resolve_book_input_path(
        args.input,
        config_value=config.get("input_pdf") or config.get("input"),
        config_path=context.config_path,
        output_dir=output_dir,
        extensions=(".pdf",),
        output_names=("input_original.pdf", "input.pdf"),
    )

    if not pdf_path.exists():
        logger.error(f"PDF not found: {pdf_path}")
        logger.info("Specify --input with the path to your PDF file")
        return 1

    # A searchable PDF is not automatically trustworthy: scanned books often
    # carry a hidden OCR text layer.  Only a conservative native-text probe may
    # bypass visual OCR; searchable-OCR and mixed PDFs stay on Chandra.
    from pdf2epub.pdf_text_probe import (
        extract_native_text_pages,
        probe_pdf_text_layer,
    )
    from pdf2epub.workflow_contracts import atomic_write_text

    try:
        probe = probe_pdf_text_layer(pdf_path)
        atomic_write_text(
            output_dir / "pdf_text_probe.json",
            json.dumps(probe, ensure_ascii=False, indent=2),
        )
    except Exception as exc:
        logger.warning(f"PDF text probe failed; continuing with visual OCR: {exc}")
        probe = {"recommendation": "ocr_required", "classification": "probe_failed"}

    if probe.get("recommendation") == "use_text_layer":
        output_dir.mkdir(parents=True, exist_ok=True)
        original_copy = output_dir / "input_original.pdf"
        processed_copy = output_dir / "input.pdf"
        if not original_copy.exists():
            shutil.copy2(pdf_path, original_copy)
        if not processed_copy.exists():
            shutil.copy2(pdf_path, processed_copy)
        try:
            extract_native_text_pages(pdf_path, output_dir, probe)
        except Exception as exc:
            logger.error(f"Native PDF text extraction failed; refusing silent fallback: {exc}")
            return 1
        logger.success(
            "Detected a high-confidence native-text PDF; skipped visual OCR and wrote page text."
        )
        logger.info(f"Output: {output_dir / 'pages'}")
        logger.info("Next step: pdf2epub refine-prepare")
        return 0

    # Preprocess PDF: copy to output dir + add page stamps + compress
    from pdf2epub.utils.pdf_utils import preprocess_pdf
    pdf_path = preprocess_pdf(pdf_path, output_dir)

    logger.info(f"Starting page-level OCR for: {book_title}")

    # Get OCR settings from config
    ocr_config = config.get('ocr', {})
    backend = ocr_config.get('backend', 'mistral')
    backend_config = ocr_config.get('backends', {}).get(backend, {})
    max_workers = args.max_workers or backend_config.get(
        'max_workers',
        ocr_config.get('vision', {}).get('max_workers', 5),
    )

    # Get credentials
    credentials = config.get('credentials', {}).get('providers', {})

    # Setup backend-specific parameters
    api_key = None
    base_url = None

    if backend == 'mistral':
        mistral_config = credentials.get('mistral', {})
        api_key = mistral_config.get('api_key')
        base_url = mistral_config.get('base_url')
    elif backend == 'azure':
        azure_config = credentials.get('azure', {})
        api_key = azure_config.get('api_key')
        base_url = azure_config.get('endpoint')

    try:
        ocr_full_book_pagewise(
            pdf_path=pdf_path,
            output_dir=output_dir,
            start_page=args.start_page or 1,
            end_page=args.end_page,
            backend=backend,
            api_key=api_key,
            base_url=base_url,
            resume=args.resume,
            config=config,
            max_workers=max_workers
        )

        logger.success(f"Page-level OCR complete!")
        logger.info(f"Output: {output_dir / 'pages'}")
        logger.info("Next step: pdf2epub refine-prepare")
        return 0

    except Exception as e:
        logger.error(f"OCR failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

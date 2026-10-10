"""Legacy chunk-shaped adapter for the optional local VLLM backend.

The normal page workflow uses the registered page backends directly. This
compatibility adapter is retained only for callers that still pass a PDF
chunk through the historical VLLM interface.
"""

from __future__ import annotations

import base64
import io
import re
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from loguru import logger
from tenacity import retry, retry_if_exception_type, stop_after_attempt, wait_exponential


def ocr_pdf_chunk_vllm(
    pdf_bytes: bytes,
    config: Dict,
    chunk_info: str,
    images_dir: Optional[Path] = None,
    page_number: int = 0,
    image_counter: int = 0,
    max_retries: int = 5,
    initial_backoff: float = 4.0,
) -> Tuple[str, List[Dict], int]:
    """OCR a PDF chunk with the configured VLLM-compatible client."""
    import pymupdf as fitz
    from PIL import Image
    from pdf2epub.ocr.backends.vllm import init_client

    logger.info(f"Starting VLLM OCR for {chunk_info}")
    client = init_client(config)
    doc = fitz.open(stream=pdf_bytes, filetype="pdf")
    markdown_parts: list[str] = []
    all_images: list[dict[str, Any]] = []
    max_wait = int(initial_backoff * (2**max_retries))

    try:
        for page_idx in range(len(doc)):
            page = doc[page_idx]
            logger.info(f"  Processing page {page_idx + 1}/{len(doc)}")
            pix = page.get_pixmap(matrix=fitz.Matrix(2, 2))
            img = Image.open(io.BytesIO(pix.tobytes("png")))

            @retry(
                retry=retry_if_exception_type(Exception),
                stop=stop_after_attempt(max_retries),
                wait=wait_exponential(multiplier=initial_backoff, max=max_wait),
                before_sleep=lambda retry_state: logger.warning(
                    f"Retry {retry_state.attempt_number}/{max_retries} for page "
                    f"{page_idx + 1}: {retry_state.outcome.exception()}"
                ),
                reraise=True,
            )
            def _ocr_page():
                return client.ocr(img)

            try:
                markdown_parts.append(_ocr_page())
            except Exception as exc:
                logger.error(
                    f"OCR failed for page {page_idx + 1} after {max_retries} retries: {exc}"
                )
                markdown_parts.append(f"\n\n[OCR Error on page {page_idx + 1}]\n\n")
    finally:
        doc.close()

    combined_markdown = "\n\n".join(markdown_parts)
    if images_dir:
        images_dir.mkdir(parents=True, exist_ok=True)
        pattern = r"!\[([^\]]*)\]\(data:image/([^;]+);base64,([^\)]+)\)"

        def replace_image(match):
            nonlocal image_counter
            alt_text, image_format, base64_data = match.groups()
            img_filename = f"page_{page_number}_img_{image_counter:03d}.{image_format}"
            img_path = images_dir / img_filename
            try:
                img_data = base64.b64decode(base64_data)
                img_path.write_bytes(img_data)
                all_images.append(
                    {"filename": img_filename, "format": image_format, "size": len(img_data)}
                )
                image_counter += 1
                return f"![{alt_text}](../images/{img_filename})"
            except Exception as exc:
                logger.error(f"Failed to save image: {exc}")
                return match.group(0)

        combined_markdown = re.sub(pattern, replace_image, combined_markdown)

    logger.success(
        f"VLLM OCR completed for {chunk_info} "
        f"({len(markdown_parts)} pages, {len(all_images)} images)"
    )
    return combined_markdown, all_images, image_counter

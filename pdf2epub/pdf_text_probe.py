"""Probe PDF text layers without trusting them by default.

Searchable PDFs are not necessarily born-digital.  A scanned PDF can contain
an OCR text layer which is less reliable than a fresh visual OCR pass.  This
module therefore only selects direct text extraction for conservative,
vector-text candidates; every other PDF remains on the configured OCR path.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from statistics import median
from typing import Any, Dict

import pymupdf

from .workflow_contracts import atomic_write_text


_REPLACEMENT_RE = re.compile("\\ufffd")


def _image_coverage(page: Any) -> float:
    """Return the fraction of the page covered by image rectangles."""
    page_area = float(page.rect.width * page.rect.height)
    if page_area <= 0:
        return 0.0
    covered = 0.0
    seen = set()
    try:
        image_infos = page.get_image_info(xrefs=True)
        for info in image_infos:
            rect = pymupdf.Rect(info.get("bbox", (0, 0, 0, 0)))
            key = (round(rect.x0, 3), round(rect.y0, 3), round(rect.x1, 3), round(rect.y1, 3))
            if key in seen:
                continue
            seen.add(key)
            covered += max(0.0, float((rect & page.rect).get_area()))
    except (AttributeError, TypeError, ValueError, RuntimeError):
        # Older PyMuPDF versions do not expose image_info consistently.
        for image in page.get_images(full=True):
            try:
                for rect in page.get_image_rects(image[0]):
                    covered += max(0.0, float((rect & page.rect).get_area()))
            except (IndexError, TypeError, ValueError, RuntimeError):
                continue
    return min(1.0, covered / page_area)


def probe_pdf_text_layer(pdf_path: Path) -> Dict[str, Any]:
    """Classify a PDF conservatively as native text or OCR-required.

    The classification is intentionally asymmetric: false negatives cost OCR
    time, while false positives can silently replace a trustworthy visual OCR
    result with a bad hidden OCR layer.
    """
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    page_chars = []
    replacement_chars = 0
    pages_with_text = 0
    pages_with_fonts = 0
    pages_with_large_images = 0
    with pymupdf.open(pdf_path) as document:
        page_count = len(document)
        for page in document:
            text = page.get_text("text", sort=True) or ""
            chars = len(text.strip())
            page_chars.append(chars)
            replacement_chars += len(_REPLACEMENT_RE.findall(text))
            if chars:
                pages_with_text += 1
            try:
                if page.get_fonts():
                    pages_with_fonts += 1
            except (AttributeError, RuntimeError):
                pass
            if _image_coverage(page) >= 0.70:
                pages_with_large_images += 1

    total_chars = sum(page_chars)
    text_page_ratio = pages_with_text / page_count if page_count else 0.0
    large_image_ratio = pages_with_large_images / page_count if page_count else 0.0
    font_page_ratio = pages_with_fonts / page_count if page_count else 0.0
    replacement_ratio = replacement_chars / total_chars if total_chars else 0.0
    median_chars = float(median(page_chars)) if page_chars else 0.0

    # These gates deliberately reject image-backed searchable PDFs.  A native
    # book with a few figure pages can still pass; a page-sized OCR image on a
    # substantial fraction of pages cannot.
    native_text = bool(
        page_count
        and text_page_ratio >= 0.95
        and median_chars >= 250
        and replacement_ratio <= 0.002
        and font_page_ratio >= 0.80
        and large_image_ratio <= 0.20
    )
    if native_text:
        classification = "native_text"
        recommendation = "use_text_layer"
    elif text_page_ratio >= 0.50:
        classification = "searchable_ocr_or_mixed"
        recommendation = "ocr_required"
    else:
        classification = "scanned_or_low_text"
        recommendation = "ocr_required"

    return {
        "schema_version": 1,
        "pdf_path": pdf_path.name,
        "source_sha256": hashlib.sha256(pdf_path.read_bytes()).hexdigest(),
        "page_count": page_count,
        "pages_with_text": pages_with_text,
        "text_page_ratio": round(text_page_ratio, 6),
        "total_text_chars": total_chars,
        "median_chars_per_page": median_chars,
        "replacement_chars": replacement_chars,
        "replacement_ratio": replacement_ratio,
        "pages_with_fonts": pages_with_fonts,
        "font_page_ratio": round(font_page_ratio, 6),
        "pages_with_large_images": pages_with_large_images,
        "large_image_ratio": round(large_image_ratio, 6),
        "classification": classification,
        "recommendation": recommendation,
    }


def _native_page_markdown(page: Any, images_dir: Path, page_number: int) -> tuple[str, int]:
    """Convert a native PDF page into ordered Markdown blocks."""
    blocks = page.get_text("dict", sort=True).get("blocks", [])
    output = []
    image_count = 0
    for block_index, block in enumerate(blocks, 1):
        block_type = block.get("type")
        if block_type == 0:
            lines = []
            for line in block.get("lines", []):
                text = "".join(str(span.get("text", "")) for span in line.get("spans", []))
                if text.strip():
                    lines.append(text.rstrip())
            if lines:
                output.append("\n".join(lines))
        elif block_type == 1 and block.get("image"):
            image_count += 1
            ext = str(block.get("ext") or "png").lower()
            if not re.fullmatch(r"[a-z0-9]+", ext):
                ext = "png"
            image_path = images_dir / f"native_page_{page_number:03d}_{block_index:02d}.{ext}"
            image_path.write_bytes(block["image"])
            output.append(f"![Image](../images/{image_path.name})")
    return "\n\n".join(output).strip() + "\n", image_count


def extract_native_text_pages(
    pdf_path: Path,
    output_dir: Path,
    probe: Dict[str, Any],
) -> None:
    """Write page Markdown and progress artifacts for a native-text PDF."""
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    images_dir = output_dir / "images"
    pages_dir.mkdir(parents=True, exist_ok=True)
    images_dir.mkdir(parents=True, exist_ok=True)

    page_stats: Dict[str, Dict[str, Any]] = {}
    pages_processed = []
    with pymupdf.open(pdf_path) as document:
        for page_number, page in enumerate(document, 1):
            markdown, image_count = _native_page_markdown(page, images_dir, page_number)
            page_path = pages_dir / f"page_{page_number:03d}.md"
            atomic_write_text(page_path, markdown)
            pages_processed.append(page_number)
            page_stats[str(page_number)] = {
                "tokens": max(1, len(markdown) // 4),
                "file": page_path.relative_to(output_dir).as_posix(),
                "char_count": len(markdown),
                "source": "native_text",
                "image_count": image_count,
            }

    atomic_write_text(
        pages_dir / "ocr_progress.json",
        json.dumps(
            {
                "schema_version": 1,
                "mode": "native_text",
                "pages_processed": pages_processed,
                "failed_pages": [],
                "global_image_counter": sum(item["image_count"] for item in page_stats.values()),
                "source_sha256": probe["source_sha256"],
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    atomic_write_text(
        pages_dir / "page_stats.json",
        json.dumps(page_stats, ensure_ascii=False, indent=2),
    )

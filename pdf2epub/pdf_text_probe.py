"""Classify PDF text layers for audit purposes.

Searchable PDFs are not necessarily born-digital.  A scanned PDF can contain
an OCR text layer which is less reliable than a fresh visual OCR pass.  The
probe therefore classifies every PDF conservatively, but it never selects the
direct text layer: every new run uses visual OCR.  ``classification`` and
``detected_recommendation`` are audit metadata only, and ``recommendation`` is
always ``ocr_required``.  Only legacy artifacts written by older versions may
still carry a ``use_text_layer`` recommendation.
"""

from __future__ import annotations

import re
from pathlib import Path
from statistics import median
from typing import Any, Callable, Dict

import pymupdf

from .workflow_contracts import sha256_file


_REPLACEMENT_RE = re.compile("\\ufffd")
_MULTI_COLUMN_MIN_LINES = 24
_MULTI_COLUMN_MIN_GAP_RATIO = 0.15
_MULTI_COLUMN_MIN_SIDE_RATIO = 0.20
_MULTI_COLUMN_MIN_Y_OVERLAP_RATIO = 0.45
_MULTI_COLUMN_MAX_MEDIAN_WIDTH_RATIO = 0.55
PDF_TEXT_PROBE_SCHEMA_VERSION = 3


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


def _detect_multi_column_layout(page: Any) -> Dict[str, Any] | None:
    """Detect a strong two-column text layout from PDF line geometry.

    A clean vector text layer is not enough to use PyMuPDF's default geometric
    order: it can interleave the lines of two newspaper-style columns.  This
    detector intentionally looks only at geometry and is conservative.  It
    requires two sizeable x-origin bands, substantial vertical overlap, and
    narrow enough line widths to leave a real column gutter.  The result is
    audit metadata only; it never changes the visual OCR page source.
    """
    try:
        page_width = float(page.rect.width)
        page_height = float(page.rect.height)
        raw_blocks = page.get_text("dict", sort=False).get("blocks", [])
    except (AttributeError, TypeError, ValueError, RuntimeError):
        return None
    if page_width <= 0 or page_height <= 0:
        return None

    lines: list[tuple[float, float, float, float]] = []
    for block in raw_blocks:
        if not isinstance(block, dict) or block.get("type") != 0:
            continue
        for line in block.get("lines", []):
            if not isinstance(line, dict):
                continue
            bbox = _bbox_list(line.get("bbox"))
            if bbox is None:
                continue
            x0, y0, x1, y1 = bbox
            # Headers, footers, and page labels should not create a false
            # column band.  Keep the body region broad enough for short pages.
            if y1 <= page_height * 0.08 or y0 >= page_height * 0.95:
                continue
            if not any(str(span.get("text") or "").strip() for span in line.get("spans", [])):
                continue
            lines.append((x0, y0, x1, y1))

    if len(lines) < _MULTI_COLUMN_MIN_LINES:
        return None

    ordered = sorted(lines, key=lambda item: (item[0], item[1], item[2]))
    minimum_side_lines = max(
        8,
        int(len(ordered) * _MULTI_COLUMN_MIN_SIDE_RATIO),
    )
    best: tuple[float, dict[str, Any]] | None = None
    for split in range(minimum_side_lines, len(ordered) - minimum_side_lines + 1):
        left = ordered[:split]
        right = ordered[split:]
        x_gap = right[0][0] - left[-1][0]
        x_gap_ratio = x_gap / page_width
        if x_gap_ratio < _MULTI_COLUMN_MIN_GAP_RATIO:
            continue

        left_y0 = min(item[1] for item in left)
        left_y1 = max(item[3] for item in left)
        right_y0 = min(item[1] for item in right)
        right_y1 = max(item[3] for item in right)
        y_overlap_ratio = max(
            0.0,
            min(left_y1, right_y1) - max(left_y0, right_y0),
        ) / page_height
        if y_overlap_ratio < _MULTI_COLUMN_MIN_Y_OVERLAP_RATIO:
            continue

        left_median_width = median(item[2] - item[0] for item in left) / page_width
        right_median_width = median(item[2] - item[0] for item in right) / page_width
        if max(left_median_width, right_median_width) > _MULTI_COLUMN_MAX_MEDIAN_WIDTH_RATIO:
            continue

        signal = {
            "line_count": len(ordered),
            "left_line_count": len(left),
            "right_line_count": len(right),
            "x0_gap_ratio": round(x_gap_ratio, 6),
            "column_split_x": round(
                (left[-1][0] + right[0][0]) / 2.0,
                3,
            ),
            "y_overlap_ratio": round(y_overlap_ratio, 6),
            "left_median_width_ratio": round(left_median_width, 6),
            "right_median_width_ratio": round(right_median_width, 6),
        }
        if best is None or x_gap_ratio > best[0]:
            best = (x_gap_ratio, signal)

    return best[1] if best is not None else None


def probe_pdf_text_layer(
    pdf_path: Path,
    *,
    progress_callback: Callable[[int, int], None] | None = None,
) -> Dict[str, Any]:
    """Classify a PDF conservatively for audit purposes.

    The classification is intentionally asymmetric: false negatives cost OCR
    time, while false positives could silently replace a trustworthy visual
    OCR result with a bad hidden OCR layer.  ``classification`` and
    ``detected_recommendation`` report that conservative decision, but
    ``recommendation`` is always ``ocr_required``: every new run uses visual
    OCR.
    """
    pdf_path = Path(pdf_path).resolve()
    if not pdf_path.is_file():
        raise FileNotFoundError(pdf_path)

    page_chars = []
    replacement_chars = 0
    pages_with_text = 0
    pages_with_fonts = 0
    pages_with_large_images = 0
    multi_column_pages: list[dict[str, Any]] = []
    with pymupdf.open(pdf_path) as document:
        page_count = len(document)
        if progress_callback is not None:
            progress_callback(0, page_count)
        progress_interval = max(1, page_count // 20) if page_count else 1
        for page_number, page in enumerate(document, 1):
            text = page.get_text("text", sort=True) or ""
            chars = len(text.strip())
            page_chars.append(chars)
            replacement_chars += len(_REPLACEMENT_RE.findall(text))
            if chars:
                pages_with_text += 1
            if chars:
                try:
                    if page.get_fonts():
                        pages_with_fonts += 1
                except (AttributeError, RuntimeError):
                    pass
            if _image_coverage(page) >= 0.70:
                pages_with_large_images += 1
            if chars:
                multi_column_signal = _detect_multi_column_layout(page)
                if multi_column_signal is not None:
                    multi_column_pages.append(
                        {"page": page_number, **multi_column_signal}
                    )
            if (
                progress_callback is not None
                and (page_number == page_count or page_number % progress_interval == 0)
            ):
                progress_callback(page_number, page_count)

    total_chars = sum(page_chars)
    text_page_ratio = pages_with_text / page_count if page_count else 0.0
    large_image_ratio = pages_with_large_images / page_count if page_count else 0.0
    font_page_ratio = pages_with_fonts / page_count if page_count else 0.0
    replacement_ratio = replacement_chars / total_chars if total_chars else 0.0
    median_chars = float(median(page_chars)) if page_chars else 0.0

    # These gates deliberately reject image-backed searchable PDFs.  A native
    # book with a few figure pages can still pass; a page-sized OCR image on a
    # substantial fraction of pages cannot.
    native_text_candidate = bool(
        page_count
        and text_page_ratio >= 0.95
        and median_chars >= 250
        and replacement_ratio <= 0.002
        and font_page_ratio >= 0.80
        and large_image_ratio <= 0.20
    )
    multi_column_page_ratio = (
        len(multi_column_pages) / page_count if page_count else 0.0
    )
    if native_text_candidate:
        classification = "native_text"
        detected_recommendation = "use_text_layer"
    elif text_page_ratio >= 0.50:
        classification = "searchable_ocr_or_mixed"
        detected_recommendation = "ocr_required"
    else:
        classification = "scanned_or_low_text"
        detected_recommendation = "ocr_required"

    return {
        "schema_version": PDF_TEXT_PROBE_SCHEMA_VERSION,
        "pdf_path": pdf_path.name,
        "source_sha256": sha256_file(pdf_path),
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
        "multi_column_pages": multi_column_pages,
        "multi_column_page_ratio": round(multi_column_page_ratio, 6),
        "layout_mode": "multi_column" if multi_column_pages else "single_column",
        "reading_order": "column_major" if multi_column_pages else "geometric",
        "classification": classification,
        "detected_recommendation": detected_recommendation,
        "recommendation": "ocr_required",
    }


def _bbox_list(value: Any) -> list[float] | None:
    """Return a JSON-safe PDF rectangle when the value is usable."""
    if not isinstance(value, (list, tuple)) or len(value) != 4:
        return None
    try:
        x0, y0, x1, y1 = (float(item) for item in value)
    except (TypeError, ValueError):
        return None
    if x1 <= x0 or y1 <= y0:
        return None
    return [x0, y0, x1, y1]

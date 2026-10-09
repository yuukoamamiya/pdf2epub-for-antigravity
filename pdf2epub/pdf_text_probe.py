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
from .ocr_progress import OCR_PROGRESS_SCHEMA_VERSION


_REPLACEMENT_RE = re.compile("\\ufffd")
_NATIVE_NOTE_START_RE = re.compile(r"^\s*\d{1,3}(?=\s|[.)、，:：])")
_MULTI_COLUMN_MIN_LINES = 24
_MULTI_COLUMN_MIN_GAP_RATIO = 0.15
_MULTI_COLUMN_MIN_SIDE_RATIO = 0.20
_MULTI_COLUMN_MIN_Y_OVERLAP_RATIO = 0.45
_MULTI_COLUMN_MAX_MEDIAN_WIDTH_RATIO = 0.55


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

    A clean vector text layer is not enough to use direct extraction: PyMuPDF's
    geometric sort can still interleave the lines of two newspaper-style
    columns.  This detector intentionally looks only at geometry and is
    conservative.  It requires two sizeable x-origin bands, substantial
    vertical overlap, and narrow enough line widths to leave a real column
    gutter.  A positive signal makes the page unsafe for the current native
    extraction path, so the caller can route the whole document through visual
    OCR and preserve reading order.
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
            "y_overlap_ratio": round(y_overlap_ratio, 6),
            "left_median_width_ratio": round(left_median_width, 6),
            "right_median_width_ratio": round(right_median_width, 6),
        }
        if best is None or x_gap_ratio > best[0]:
            best = (x_gap_ratio, signal)

    return best[1] if best is not None else None


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
    multi_column_pages: list[dict[str, Any]] = []
    with pymupdf.open(pdf_path) as document:
        page_count = len(document)
        for page_number, page in enumerate(document, 1):
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
            multi_column_signal = _detect_multi_column_layout(page)
            if multi_column_signal is not None:
                multi_column_pages.append(
                    {"page": page_number, **multi_column_signal}
                )

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
    if native_text_candidate and multi_column_pages:
        classification = "native_text_multicolumn"
        recommendation = "ocr_required"
    elif native_text_candidate:
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
        "multi_column_pages": multi_column_pages,
        "multi_column_page_ratio": round(multi_column_page_ratio, 6),
        "classification": classification,
        "recommendation": recommendation,
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


def _span_font_size(span: Dict[str, Any]) -> float | None:
    try:
        value = float(span.get("size"))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _native_span_is_superscript(
    span: Dict[str, Any],
    *,
    line_bbox: list[float] | None,
    page_font_size: float,
) -> bool:
    """Recognize likely native footnote/reference markers.

    PyMuPDF exposes the PDF superscript flag for many publishers, but not all
    of them.  The conservative fallback only wraps short numeric/symbol spans
    that are materially smaller than the page's body text.  A later footnote
    decision still has to connect the marker to a reviewed definition, so
    ordinary small text is not moved merely because it was wrapped in ``sup``.
    """
    text = str(span.get("text") or "").strip()
    if not text:
        return False
    try:
        flags = int(span.get("flags", 0) or 0)
    except (TypeError, ValueError):
        flags = 0
    if flags & 1:  # PyMuPDF's superscript font flag.
        return True
    if not re.fullmatch(r"(?:\d{1,4}|[*†‡§]+)", text):
        return False
    size = _span_font_size(span)
    if size is None or page_font_size <= 0 or size > page_font_size * 0.84:
        return False
    if line_bbox is None:
        return True
    span_bbox = _bbox_list(span.get("bbox"))
    if span_bbox is None:
        return True
    line_height = max(1.0, line_bbox[3] - line_bbox[1])
    return span_bbox[1] <= line_bbox[1] + line_height * 0.45


def _native_page_artifacts(
    page: Any,
    images_dir: Path,
    page_number: int,
) -> tuple[str, int, list[dict[str, Any]], list[float], float]:
    """Extract Markdown plus compact, source-native layout evidence."""
    raw_blocks = page.get_text("dict", sort=True).get("blocks", [])
    all_sizes = [
        size
        for block in raw_blocks
        if block.get("type") == 0
        for line in block.get("lines", [])
        for span in line.get("spans", [])
        if (size := _span_font_size(span)) is not None
    ]
    page_font_size = float(median(all_sizes)) if all_sizes else 0.0
    page_height = float(page.rect.height)
    bottom_boundary = float(page.rect.y0) + page_height * 0.64
    markdown_blocks: list[str] = []
    layout_blocks: list[dict[str, Any]] = []
    image_count = 0

    for block_index, block in enumerate(raw_blocks):
        block_type = block.get("type")
        bbox = _bbox_list(block.get("bbox"))
        if block_type == 0:
            line_records: list[dict[str, Any]] = []
            for line in block.get("lines", []):
                line_bbox = _bbox_list(line.get("bbox"))
                raw_parts: list[str] = []
                markdown_parts: list[str] = []
                line_sizes: list[float] = []
                line_font_names: set[str] = set()
                line_flags = 0
                for span in line.get("spans", []):
                    raw_text = str(span.get("text", ""))
                    raw_parts.append(raw_text)
                    span_text = raw_text
                    if _native_span_is_superscript(
                        span,
                        line_bbox=line_bbox,
                        page_font_size=page_font_size,
                    ):
                        span_text = f"<sup>{raw_text}</sup>"
                    markdown_parts.append(span_text)
                    size = _span_font_size(span)
                    if size is not None:
                        line_sizes.append(size)
                    font = str(span.get("font") or "").strip()
                    if font:
                        line_font_names.add(font)
                    try:
                        line_flags |= int(span.get("flags", 0) or 0)
                    except (TypeError, ValueError):
                        pass
                raw_text = "".join(raw_parts)
                if raw_text.strip():
                    line_size = float(median(line_sizes)) if line_sizes else None
                    line_records.append(
                        {
                            "raw_text": raw_text.rstrip(),
                            "markdown_text": "".join(markdown_parts).rstrip(),
                            "bbox": line_bbox or bbox,
                            "font_size": line_size,
                            "font_names": line_font_names,
                            "flags": line_flags,
                            "note_start": bool(
                                line_bbox
                                and line_bbox[1] >= bottom_boundary
                                and line_size is not None
                                and page_font_size > 0
                                and line_size <= page_font_size * 0.92
                                and _NATIVE_NOTE_START_RE.match(raw_text)
                                and not re.fullmatch(r"\s*\d{1,3}\s*", raw_text)
                            ),
                        }
                    )
            if not line_records:
                continue
            groups: list[list[dict[str, Any]]] = []
            current_group: list[dict[str, Any]] = []
            for line_record in line_records:
                if line_record["note_start"] and current_group:
                    groups.append(current_group)
                    current_group = []
                current_group.append(line_record)
            if current_group:
                groups.append(current_group)

            for group in groups:
                raw_lines = [str(item["raw_text"]) for item in group]
                markdown_lines = [str(item["markdown_text"]) for item in group]
                group_bboxes = [item["bbox"] for item in group if item.get("bbox")]
                group_bbox = None
                if group_bboxes:
                    group_bbox = [
                        min(item[0] for item in group_bboxes),
                        min(item[1] for item in group_bboxes),
                        max(item[2] for item in group_bboxes),
                        max(item[3] for item in group_bboxes),
                    ]
                group_sizes = [
                    float(item["font_size"])
                    for item in group
                    if item.get("font_size") is not None
                ]
                group_fonts = {
                    font
                    for item in group
                    for font in item.get("font_names", set())
                }
                group_flags = 0
                for item in group:
                    group_flags |= int(item.get("flags", 0) or 0)
                block_text = "\n".join(raw_lines)
                layout_blocks.append(
                    {
                        "order": len(layout_blocks),
                        "label": "Text",
                        "bbox": group_bbox or bbox,
                        "text": block_text,
                        "markdown_text": "\n".join(markdown_lines),
                        "html": block_text,
                        "line_count": len(group),
                        "source_block": block_index,
                        "font_size": round(float(median(group_sizes)), 3)
                        if group_sizes
                        else None,
                        "font_names": sorted(group_fonts),
                        "flags": group_flags,
                    }
                )
                markdown_blocks.append("\n".join(markdown_lines))
        elif block_type == 1 and block.get("image"):
            image_count += 1
            ext = str(block.get("ext") or "png").lower()
            if not re.fullmatch(r"[a-z0-9]+", ext):
                ext = "png"
            image_path = images_dir / f"native_page_{page_number:03d}_{block_index + 1:02d}.{ext}"
            image_path.write_bytes(block["image"])
            layout_blocks.append(
                {
                    "order": len(layout_blocks),
                    "label": "Image",
                    "bbox": bbox,
                    "text": "",
                    "asset": f"images/{image_path.name}",
                }
            )
            markdown_blocks.append(f"![Image](../images/{image_path.name})")

    page_box = [
        float(page.rect.x0),
        float(page.rect.y0),
        float(page.rect.x1),
        float(page.rect.y1),
    ]
    return (
        "\n\n".join(markdown_blocks).strip() + "\n",
        image_count,
        layout_blocks,
        page_box,
        page_font_size,
    )


def _native_page_markdown(page: Any, images_dir: Path, page_number: int) -> tuple[str, int]:
    """Convert a native PDF page into ordered Markdown blocks."""
    markdown, image_count, _layout_blocks, _page_box, _page_font_size = _native_page_artifacts(
        page, images_dir, page_number
    )
    return markdown, image_count


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
            markdown, image_count, layout_blocks, page_box, page_font_size = _native_page_artifacts(
                page, images_dir, page_number
            )
            page_path = pages_dir / f"page_{page_number:03d}.md"
            sidecar_path = pages_dir / f"page_{page_number:03d}.ocr.json"
            atomic_write_text(
                sidecar_path,
                json.dumps(
                    {
                        "schema_version": 1,
                        "page_number": page_number,
                        "backend": "native_text",
                        "source_kind": "native_text",
                        "coordinate_system": "page_points",
                        "page_box": page_box,
                        "body_font_size": round(page_font_size, 3)
                        if page_font_size > 0
                        else None,
                        "formats": {"markdown": page_path.name},
                        "blocks": layout_blocks,
                        "assets": [
                            block["asset"]
                            for block in layout_blocks
                            if block.get("asset")
                        ],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
            atomic_write_text(page_path, markdown)
            pages_processed.append(page_number)
            page_stats[str(page_number)] = {
                "tokens": max(1, len(markdown) // 4),
                "file": page_path.relative_to(output_dir).as_posix(),
                "char_count": len(markdown),
                "source": "native_text",
                "image_count": image_count,
                "artifact_file": sidecar_path.relative_to(output_dir).as_posix(),
            }

    atomic_write_text(
        pages_dir / "ocr_progress.json",
        json.dumps(
            {
                "schema_version": OCR_PROGRESS_SCHEMA_VERSION,
                "mode": "native_text",
                "backend": "native_text",
                "source_sha256": probe["source_sha256"],
                "total_pages": len(pages_processed),
                "pages_processed": pages_processed,
                "failed_pages": [],
                "empty_pages": [],
                "allowed_empty_pages": [],
                "missing_pages": [],
                "global_image_counter": sum(item["image_count"] for item in page_stats.values()),
            },
            ensure_ascii=False,
            indent=2,
        ),
    )
    atomic_write_text(
        pages_dir / "page_stats.json",
        json.dumps(page_stats, ensure_ascii=False, indent=2),
    )

"""Shared parsing primitives for PDF layout sidecars."""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional


_PAGE_POINT_COORDINATE_SYSTEMS = frozenset(
    {"page", "page_points", "pdf_points", "points"}
)
_NORMALIZED_COORDINATE_SYSTEMS = frozenset(
    {"normalized", "normalised", "ratio"}
)


def text_from_block(block: Mapping[str, Any]) -> str:
    """Return a compact plain-text view without changing stored source text."""
    value = block.get("text")
    if value is None:
        value = block.get("html", "")
    value = html.unescape(str(value or ""))
    value = re.sub(r"<[^>]+>", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def bbox_from_value(value: Any) -> Optional[list[float]]:
    """Convert a four-value or point-list box to ``x0, y0, x1, y1``."""
    if not isinstance(value, (list, tuple)):
        return None
    if len(value) == 4 and all(isinstance(item, (int, float)) for item in value):
        x0, y0, x1, y1 = (float(item) for item in value)
        return [x0, y0, x1, y1] if x1 > x0 and y1 > y0 else None

    points: list[tuple[float, float]] = []
    for item in value:
        if isinstance(item, Mapping):
            x, y = item.get("x"), item.get("y")
        elif isinstance(item, (list, tuple)) and len(item) >= 2:
            x, y = item[0], item[1]
        else:
            continue
        if isinstance(x, (int, float)) and isinstance(y, (int, float)):
            points.append((float(x), float(y)))
    if len(points) < 2:
        return None
    xs = [point[0] for point in points]
    ys = [point[1] for point in points]
    result = [min(xs), min(ys), max(xs), max(ys)]
    return result if result[2] > result[0] and result[3] > result[1] else None


def normalized_bbox(
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
) -> Optional[list[float]]:
    """Normalize common OCR/native block coordinates to page ratios.

    Native extraction uses PDF page points.  Chandra uses normalized 0..1000
    coordinates, while Paddle may provide pixel boxes.  The sidecar's explicit
    coordinate system wins; only legacy sidecars use the bounded-value
    fallback.
    """
    raw = block.get("bbox")
    if raw is None:
        raw = block.get("box")
    if raw is None:
        raw = block.get("bbox_px")
    bbox = bbox_from_value(raw)
    if bbox is None:
        return None

    page_box = bbox_from_value(sidecar.get("page_box"))
    coordinate_system = str(sidecar.get("coordinate_system") or "").strip().lower()
    if coordinate_system in _PAGE_POINT_COORDINATE_SYSTEMS:
        return _scale_to_page(bbox, page_box)
    if coordinate_system in _NORMALIZED_COORDINATE_SYSTEMS or max(bbox) <= 1000:
        return _scale_to_unit(bbox, 1000.0)

    # Pixel sidecars normally carry an explicit bbox_px.  If only a large
    # legacy bbox exists, retain the old page-box fallback rather than guess.
    pixel_bbox = bbox_from_value(block.get("bbox_px"))
    if pixel_bbox is not None and page_box is not None:
        bbox = pixel_bbox
    return _scale_to_page(bbox, page_box)


def load_layout_sidecar(
    path: Path,
    *,
    require_blocks: bool = True,
) -> dict[str, Any]:
    """Load and minimally validate a page layout sidecar.

    Footnote review requires an addressable block list.  Illustration review
    can also inspect a page that only has Markdown/image evidence, so it may
    opt into the permissive form.
    """
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"Invalid OCR sidecar {path}: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError(f"OCR sidecar must be an object: {path}")
    if "blocks" not in value and not require_blocks:
        value["blocks"] = []
    if not isinstance(value.get("blocks"), list):
        raise ValueError(f"OCR sidecar has no blocks array: {path}")
    return value


def _scale_to_unit(bbox: list[float], scale: float) -> list[float]:
    return [max(0.0, min(1.0, value / scale)) for value in bbox]


def _scale_to_page(
    bbox: list[float],
    page_box: Optional[list[float]],
) -> Optional[list[float]]:
    if page_box is None:
        return None
    width = page_box[2] - page_box[0]
    height = page_box[3] - page_box[1]
    if width <= 0 or height <= 0:
        return None
    return [
        max(0.0, min(1.0, (bbox[0] - page_box[0]) / width)),
        max(0.0, min(1.0, (bbox[1] - page_box[1]) / height)),
        max(0.0, min(1.0, (bbox[2] - page_box[0]) / width)),
        max(0.0, min(1.0, (bbox[3] - page_box[1]) / height)),
    ]


__all__ = [
    "bbox_from_value",
    "load_layout_sidecar",
    "normalized_bbox",
    "text_from_block",
]

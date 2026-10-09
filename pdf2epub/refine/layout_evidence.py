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


def layout_box_evidence(
    prediction: Mapping[str, Any] | None,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    labels: Optional[frozenset[str]] = frozenset({"footnote", "footnotes"}),
    min_overlap: float = 0.25,
) -> dict[str, Any]:
    """Return the strongest selected layout box overlapping an OCR block.

    PP-DocLayout boxes are evidence only.  This helper deliberately does not
    classify the OCR block; callers still combine the result with page-zone,
    numbering, and cross-page signals before moving content. ``labels=None``
    selects every model region and is useful when a model has no dedicated
    footnote class but still distinguishes text, headings, references, and
    page furniture.
    """
    if not isinstance(prediction, Mapping):
        return {
            "matched": False,
            "score": None,
            "overlap": 0.0,
            "label": None,
            "bbox": None,
        }
    block_bbox = normalized_bbox(block, sidecar)
    if block_bbox is None:
        return {
            "matched": False,
            "score": None,
            "overlap": 0.0,
            "label": None,
            "bbox": None,
        }
    page_box = bbox_from_value(prediction.get("page_box"))
    coordinate_system = str(prediction.get("coordinate_system") or "pixels").strip().lower()
    best: dict[str, Any] | None = None
    for item in prediction.get("boxes", []):
        if not isinstance(item, Mapping):
            continue
        label = str(item.get("label") or "").strip().casefold()
        if labels is not None and label not in labels:
            continue
        raw_bbox = item.get("bbox")
        if raw_bbox is None:
            raw_bbox = item.get("coordinate")
        raw = bbox_from_value(raw_bbox)
        if raw is None:
            continue
        if coordinate_system in _PAGE_POINT_COORDINATE_SYSTEMS:
            candidate_bbox = _scale_to_page(raw, page_box)
        elif coordinate_system in _NORMALIZED_COORDINATE_SYSTEMS:
            candidate_bbox = _scale_to_unit(raw, 1.0)
        elif page_box is not None:
            candidate_bbox = _scale_to_page(raw, page_box)
        else:
            candidate_bbox = _scale_to_unit(raw, 1000.0)
        if candidate_bbox is None:
            continue
        overlap = _intersection_over_block(block_bbox, candidate_bbox)
        if overlap < float(min_overlap):
            continue
        try:
            score = float(item.get("score"))
        except (TypeError, ValueError):
            score = 0.0
        candidate = {
            "matched": True,
            "score": round(score, 6),
            "overlap": round(overlap, 6),
            "label": label,
            "bbox": candidate_bbox,
        }
        if best is None or (candidate["overlap"], candidate["score"]) > (
            best["overlap"],
            best["score"],
        ):
            best = candidate
    return best or {
        "matched": False,
        "score": None,
        "overlap": 0.0,
        "label": None,
        "bbox": None,
    }


def layout_region_evidence(
    prediction: Mapping[str, Any] | None,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    min_overlap: float = 0.25,
) -> dict[str, Any]:
    """Return the best PP-DocLayout region for an OCR block.

    PP-DocLayout-L commonly emits generic ``text`` or ``paragraph_title``
    regions for footnote-like material rather than a dedicated ``footnotes``
    class. Keeping those labels in the evidence lets the reviewer see that a
    numbered bottom block is actually inside a heading/reference/footer region
    without granting the model authority to move it.
    """
    return layout_box_evidence(
        prediction,
        block,
        sidecar,
        labels=None,
        min_overlap=min_overlap,
    )


def _intersection_over_block(left: list[float], right: list[float]) -> float:
    x0 = max(left[0], right[0])
    y0 = max(left[1], right[1])
    x1 = min(left[2], right[2])
    y1 = min(left[3], right[3])
    intersection = max(0.0, x1 - x0) * max(0.0, y1 - y0)
    area = max(0.0, left[2] - left[0]) * max(0.0, left[3] - left[1])
    return intersection / area if area else 0.0


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
    "layout_box_evidence",
    "layout_region_evidence",
    "load_layout_sidecar",
    "normalized_bbox",
    "text_from_block",
]

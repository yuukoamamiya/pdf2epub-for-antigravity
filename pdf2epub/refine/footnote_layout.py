"""Layout-only evidence used by footnote candidate preparation.

The functions here describe page geometry and recall coverage. They do not
decide whether a block is a footnote and do not write workflow artifacts.
Keeping these facts separate lets candidate construction and visual review
reuse the same normalized layout evidence without creating a semantic cycle.
"""

from __future__ import annotations

import re
from statistics import median
from typing import Any, Iterable, Mapping, Optional

from .footnote_signals import (
    _CITATION_LABEL_RE,
    _FOOTNOTE_LABEL_RE,
    _FURNITURE_LABEL_RE,
    _NUMERIC_ONLY_RE,
    leading_footnote_key as _leading_footnote_key,
)
from .layout_evidence import (
    normalized_bbox as _normalise_bbox,
    text_from_block as _text_from_block,
)


DEFAULT_REGION_MAX_GAP = 0.035
DEFAULT_RECALL_AUDIT_EDGE_MARGIN = 0.04
DEFAULT_LAYOUT_GAP_FACTOR = 2.0
DEFAULT_LAYOUT_TAIL_FACTOR = 1.5
DEFAULT_TABLE_NOTE_MAX_GAP = 0.035
_TABLE_LABEL_RE = re.compile(r"\btable\b", re.IGNORECASE)
_INLINE_LIST_CONTEXT_END_RE = re.compile(r"(?:[:：]|[.!?。！？])\s*$")


def is_ordinary_body_block(block: Mapping[str, Any]) -> bool:
    """Return whether a later layout block can represent flowing body text."""
    label = str(block.get("label") or "").strip().lower()
    if (
        _FOOTNOTE_LABEL_RE.search(label)
        or _CITATION_LABEL_RE.search(label)
        or _FURNITURE_LABEL_RE.search(label)
    ):
        return False
    if _leading_footnote_key(_text_from_block(block)) is not None:
        return False
    return label in {"", "text", "body", "paragraph", "prose"}


def layout_records(sidecar: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return text blocks with normalized geometry for relative layout checks."""
    records: list[dict[str, Any]] = []
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list):
        return records
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        text = _text_from_block(block)
        bbox = _normalise_bbox(block, sidecar)
        if not text or not bbox:
            continue
        # A bare printed page number must not define the page's content tail.
        if _NUMERIC_ONLY_RE.fullmatch(text):
            continue
        label = str(block.get("label") or "").strip()
        if _FURNITURE_LABEL_RE.search(label):
            continue
        records.append(
            {
                "index": index,
                "bbox": bbox,
                "height": max(0.0, float(bbox[3]) - float(bbox[1])),
                "label": label,
                "text": text,
            }
        )
    return records


def page_layout_profile(sidecar: Mapping[str, Any]) -> dict[str, Any]:
    """Describe the page tail using only its own block distribution."""
    records = layout_records(sidecar)
    if not records:
        return {
            "content_top": None,
            "content_bottom": None,
            "median_block_height": 0.0,
            "median_vertical_gap": 0.0,
            "tail_tolerance": 0.0,
            "records": [],
        }
    heights = [record["height"] for record in records if record["height"] > 0]
    gaps: list[float] = []
    ordered = sorted(records, key=lambda item: int(item["index"]))
    for previous, current in zip(ordered, ordered[1:]):
        gap = float(current["bbox"][1]) - float(previous["bbox"][3])
        if gap >= 0:
            gaps.append(gap)
    if heights:
        sorted_heights = sorted(heights)
        typical_heights = sorted_heights[: max(1, (len(sorted_heights) + 1) // 2)]
        median_height = float(median(typical_heights))
    else:
        median_height = 0.0
    close_gaps = [
        gap for gap in gaps if median_height <= 0 or gap <= median_height * 4
    ]
    median_gap = float(median(close_gaps)) if close_gaps else 0.0
    tail_tolerance = max(
        median_height * DEFAULT_LAYOUT_TAIL_FACTOR,
        median_gap * DEFAULT_LAYOUT_GAP_FACTOR,
        1e-6,
    )
    return {
        "content_top": min(float(record["bbox"][1]) for record in records),
        "content_bottom": max(float(record["bbox"][3]) for record in records),
        "median_block_height": round(median_height, 6),
        "median_vertical_gap": round(median_gap, 6),
        "tail_tolerance": round(tail_tolerance, 6),
        "records": records,
    }


def region_layout_evidence(
    sidecar: Mapping[str, Any],
    indexes: Iterable[int],
    *,
    context_indexes: Iterable[int] = (),
) -> dict[str, Any]:
    """Return page-relative evidence for one candidate region."""
    profile = page_layout_profile(sidecar)
    index_set = {int(index) for index in indexes}
    context_index_set = {int(index) for index in context_indexes}
    records = profile.get("records", [])
    region_records = [
        record for record in records if int(record["index"]) in index_set
    ]
    if not region_records or profile.get("content_bottom") is None:
        return {
            "available": False,
            "tail_aligned": False,
            "detached_from_body": False,
            "body_continues_after_region": False,
        }

    first_index = min(index_set)
    region_top = min(float(record["bbox"][1]) for record in region_records)
    region_bottom = max(float(record["bbox"][3]) for record in region_records)
    content_bottom = float(profile["content_bottom"])
    tail_tolerance = float(profile.get("tail_tolerance") or 0.0)
    tail_gap = max(0.0, content_bottom - region_bottom)

    previous_records = [
        record
        for record in records
        if int(record["index"]) < first_index
        and int(record["index"]) not in index_set
        and int(record["index"]) not in context_index_set
    ]
    previous = previous_records[-1] if previous_records else None
    preceding_gap = (
        max(0.0, region_top - float(previous["bbox"][3]))
        if previous is not None
        else None
    )
    median_gap = float(profile.get("median_vertical_gap") or 0.0)
    median_height = float(profile.get("median_block_height") or 0.0)
    detach_gap = max(
        median_gap * DEFAULT_LAYOUT_GAP_FACTOR,
        median_height * 0.75,
        1e-6,
    )
    detached = previous is None or preceding_gap >= detach_gap

    body_continues = False
    body_after_indexes: list[int] = []
    blocks = sidecar.get("blocks", [])
    for record in records:
        record_index = int(record["index"])
        if record_index <= max(index_set) or record_index in index_set:
            continue
        block = blocks[record_index] if record_index < len(blocks) else None
        if not isinstance(block, Mapping) or not is_ordinary_body_block(block):
            continue
        later_bottom = float(record["bbox"][3])
        if later_bottom >= content_bottom - tail_tolerance:
            body_continues = True
            body_after_indexes.append(record_index)

    return {
        "available": True,
        "content_top": round(float(profile["content_top"]), 6),
        "content_bottom": round(content_bottom, 6),
        "region_top": round(region_top, 6),
        "region_bottom": round(region_bottom, 6),
        "tail_gap": round(tail_gap, 6),
        "tail_tolerance": round(tail_tolerance, 6),
        "preceding_gap": (
            round(preceding_gap, 6) if preceding_gap is not None else None
        ),
        "detach_gap": round(detach_gap, 6),
        "tail_aligned": tail_gap <= tail_tolerance,
        "detached_from_body": detached,
        "body_continues_after_region": body_continues,
        "body_after_block_indices": body_after_indexes,
        "median_block_height": round(median_height, 6),
        "median_vertical_gap": round(median_gap, 6),
    }


def layout_scope(
    sidecar: Mapping[str, Any], block_index: int
) -> dict[str, Any]:
    """Describe a block's coarse page position for human review.

    Scope labels are geometric hints only. They never establish note meaning
    or ownership. Wide blocks are kept distinct from column lanes so a footer
    spanning both columns cannot bridge two otherwise separate note regions.
    """
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list) or not 0 <= int(block_index) < len(blocks):
        return {"available": False, "placement": "unknown", "scope_id": None}
    block = blocks[int(block_index)]
    if not isinstance(block, Mapping):
        return {"available": False, "placement": "unknown", "scope_id": None}
    bbox = _normalise_bbox(block, sidecar)
    if not bbox:
        return {"available": False, "placement": "unknown", "scope_id": None}

    x0, y0, x1, y1 = (float(value) for value in bbox)
    width = x1 - x0
    center = (x0 + x1) / 2
    if width >= 0.72:
        placement = "full_width"
        scope_id = "full_width"
    elif x1 <= 0.22:
        placement = "margin_left"
        scope_id = "margin_left"
    elif x0 >= 0.78:
        placement = "margin_right"
        scope_id = "margin_right"
    elif center < 0.5:
        placement = "column_left"
        scope_id = "column_left"
    else:
        placement = "column_right"
        scope_id = "column_right"

    nearby: list[dict[str, Any]] = []
    for index, candidate in enumerate(blocks):
        if index == int(block_index) or not isinstance(candidate, Mapping):
            continue
        candidate_bbox = _normalise_bbox(candidate, sidecar)
        if not candidate_bbox:
            continue
        vertical_distance = max(
            0.0,
            y0 - float(candidate_bbox[3]),
            float(candidate_bbox[1]) - y1,
        )
        if vertical_distance > 0.06:
            continue
        candidate_center = (float(candidate_bbox[0]) + float(candidate_bbox[2])) / 2
        same_lane = (
            scope_id == "full_width"
            or (candidate_center < 0.5) == (center < 0.5)
        )
        if not same_lane:
            continue
        label = str(candidate.get("label") or "").strip()
        nearby.append(
            {
                "block": index,
                "label": label,
                "bbox": [round(float(value), 6) for value in candidate_bbox],
            }
        )
    return {
        "available": True,
        "scope_id": scope_id,
        "placement": placement,
        "bbox": [round(value, 6) for value in bbox],
        "nearby_blocks": nearby,
    }


def table_adjacent(
    sidecar: Mapping[str, Any],
    block_index: int,
    bbox: Optional[Iterable[float]] = None,
    *,
    max_gap: float = DEFAULT_TABLE_NOTE_MAX_GAP,
) -> bool:
    """Return whether a block follows a nearby labeled table geometrically.

    This is a location fact for candidate review, not evidence that the block
    is a table note. The table must precede the block in sidecar order, end
    above it on the page, and overlap its horizontal span.
    """
    blocks = sidecar.get("blocks", [])
    index = int(block_index)
    if not isinstance(blocks, list) or not 0 <= index < len(blocks):
        return False
    candidate_block = blocks[index]
    if not isinstance(candidate_block, Mapping):
        return False
    candidate_bbox = (
        [float(value) for value in bbox]
        if bbox is not None
        else _normalise_bbox(candidate_block, sidecar)
    )
    if not candidate_bbox or len(candidate_bbox) != 4:
        return False
    candidate_width = max(0.0, candidate_bbox[2] - candidate_bbox[0])
    if candidate_width <= 0:
        return False
    for table_index, table_block in enumerate(blocks[:index]):
        if not isinstance(table_block, Mapping):
            continue
        if not _TABLE_LABEL_RE.search(str(table_block.get("label") or "")):
            continue
        table_bbox = _normalise_bbox(table_block, sidecar)
        if not table_bbox:
            continue
        gap = float(candidate_bbox[1]) - float(table_bbox[3])
        table_width = max(0.0, float(table_bbox[2]) - float(table_bbox[0]))
        overlap = max(
            0.0,
            min(float(candidate_bbox[2]), float(table_bbox[2]))
            - max(float(candidate_bbox[0]), float(table_bbox[0])),
        )
        denominator = min(candidate_width, table_width)
        if (
            0.0 <= gap <= float(max_gap)
            and denominator > 0
            and overlap / denominator >= 0.2
        ):
            return True
    return False


def body_flow_warning(
    sidecar: Mapping[str, Any],
    block_index: int,
    bbox: Optional[Iterable[float]],
    *,
    layout_profile: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Detect a numbered candidate that is still embedded in body flow."""
    if not bbox:
        return None
    try:
        values = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if len(values) != 4:
        return None
    profile = (
        dict(layout_profile)
        if isinstance(layout_profile, Mapping)
        else page_layout_profile(sidecar)
    )
    content_bottom = profile.get("content_bottom")
    tail_tolerance = float(profile.get("tail_tolerance") or 0.0)
    median_height = float(profile.get("median_block_height") or 0.0)
    if content_bottom is not None and median_height > 0:
        block_height = max(0.0, values[3] - values[1])
        if (
            values[3] >= float(content_bottom) - tail_tolerance
            and block_height >= median_height * 3
        ):
            return "numbered_block_spans_body_flow"

    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list):
        return None
    for later_index in range(block_index + 1, len(blocks)):
        later = blocks[later_index]
        if not isinstance(later, Mapping) or not is_ordinary_body_block(later):
            continue
        later_bbox = _normalise_bbox(later, sidecar)
        if not later_bbox:
            continue
        if later_bbox[1] < values[3] - DEFAULT_REGION_MAX_GAP:
            continue
        if (
            content_bottom is not None
            and later_bbox[3] >= float(content_bottom) - tail_tolerance
        ):
            return "ordinary_text_below_numeric_block"
    return None


def inline_ordered_list_warning(
    sidecar: Mapping[str, Any],
    block_index: int,
    bbox: Optional[Iterable[float]],
    *,
    layout_profile: Optional[Mapping[str, Any]] = None,
) -> Optional[str]:
    """Recognize an ordered list embedded in ordinary body flow.

    A numbered list is only suppressed when it has a nearby preceding prose
    block ending in sentence punctuation, is followed by adjacent numbered
    blocks, and still has a meaningful bottom margin. A real page-footnote
    region near the physical page tail therefore remains available for the
    existing layout and visual checks.
    """
    if not bbox:
        return None
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list) or block_index <= 0 or block_index >= len(blocks):
        return None
    current = blocks[block_index]
    if not isinstance(current, Mapping):
        return None
    if _leading_footnote_key(_text_from_block(current)) is None:
        return None
    try:
        current_box = [float(value) for value in bbox]
    except (TypeError, ValueError):
        return None
    if len(current_box) != 4:
        return None

    profile = (
        dict(layout_profile)
        if isinstance(layout_profile, Mapping)
        else page_layout_profile(sidecar)
    )
    content_bottom = profile.get("content_bottom")
    if content_bottom is None:
        return None
    tail_tolerance = float(profile.get("tail_tolerance") or 0.0)
    median_height = float(profile.get("median_block_height") or 0.0)
    bottom_margin = max(tail_tolerance, median_height, 1e-6)
    if current_box[3] >= float(content_bottom) - bottom_margin:
        return None

    # Walk over an already-numbered run so the first list item can provide the
    # context for all later items in the same list.
    run_start = block_index
    while run_start > 0:
        previous = blocks[run_start - 1]
        if not isinstance(previous, Mapping):
            break
        if _leading_footnote_key(_text_from_block(previous)) is None:
            break
        run_start -= 1
    if run_start <= 0:
        return None
    context = blocks[run_start - 1]
    if not isinstance(context, Mapping) or not is_ordinary_body_block(context):
        return None
    context_text = _text_from_block(context).rstrip()
    if not _INLINE_LIST_CONTEXT_END_RE.search(context_text):
        return None
    context_box = _normalise_bbox(context, sidecar)
    first_list_box = _normalise_bbox(blocks[run_start], sidecar)
    if not context_box or not first_list_box:
        return None
    gap = float(first_list_box[1]) - float(context_box[3])
    max_gap = max(DEFAULT_REGION_MAX_GAP, median_height * 1.5)
    if gap < -DEFAULT_REGION_MAX_GAP or gap > max_gap:
        return None
    return "inline_ordered_list"


def bottom_geometry(
    bbox: Optional[Iterable[float]],
    *,
    bottom_ratio: float,
    intersection_ratio: float,
) -> dict[str, Any]:
    """Return page-bottom evidence for one normalized block."""
    if not bbox:
        return {
            "is_bottom": False,
            "bottom_edge": None,
            "intersection_ratio": 0.0,
        }
    try:
        values = [float(value) for value in bbox]
        height = max(0.0, values[3] - values[1])
        overlap = max(0.0, values[3] - max(values[1], float(bottom_ratio)))
        ratio = overlap / height if height > 0 else 0.0
        bottom_edge = values[3]
    except (TypeError, ValueError, IndexError):
        return {
            "is_bottom": False,
            "bottom_edge": None,
            "intersection_ratio": 0.0,
        }
    return {
        "is_bottom": bool(
            bottom_edge >= float(bottom_ratio)
            and ratio >= float(intersection_ratio)
        ),
        "bottom_edge": round(bottom_edge, 6),
        "intersection_ratio": round(ratio, 6),
    }


def bottom_detection_stats(
    sidecar: Mapping[str, Any],
    candidate_indexes: set[int],
    *,
    bottom_ratio: float,
    excluded_block_indexes: Optional[set[int]] = None,
) -> dict[str, int]:
    """Count bottom numeric evidence the candidate list did not cover."""
    stats = {
        "bottom_block_count": 0,
        "bottom_numeric_block_count": 0,
        "bottom_numeric_candidate_count": 0,
        "excluded_page_furniture_count": 0,
        "excluded_numeric_page_count": 0,
        "suspected_missed_count": 0,
    }
    excluded_block_indexes = set(excluded_block_indexes or set())
    layout_profile = page_layout_profile(sidecar)
    for index, block in enumerate(sidecar.get("blocks", [])):
        if index in excluded_block_indexes or not isinstance(block, Mapping):
            continue
        bbox = _normalise_bbox(block, sidecar)
        if not bbox or bbox[3] < float(bottom_ratio):
            continue
        stats["bottom_block_count"] += 1
        text = _text_from_block(block)
        key = _leading_footnote_key(text)
        label = str(block.get("label") or "").strip()
        is_furniture = bool(_FURNITURE_LABEL_RE.search(label))
        if (
            not _FOOTNOTE_LABEL_RE.search(label)
            and inline_ordered_list_warning(
                sidecar,
                index,
                bbox,
                layout_profile=layout_profile,
            )
        ):
            continue
        if is_furniture:
            stats["excluded_page_furniture_count"] += 1
        if _NUMERIC_ONLY_RE.fullmatch(text):
            stats["excluded_numeric_page_count"] += 1
        if not key:
            continue
        stats["bottom_numeric_block_count"] += 1
        if index in candidate_indexes:
            stats["bottom_numeric_candidate_count"] += 1
            continue
        if (
            is_furniture
            or _NUMERIC_ONLY_RE.fullmatch(text)
            or _CITATION_LABEL_RE.search(label)
        ):
            continue
        stats["suspected_missed_count"] += 1
    return stats


def suspected_missed_block_indexes(
    sidecar: Mapping[str, Any],
    candidate_indexes: set[int],
    *,
    bottom_ratio: float,
    bottom_intersection_ratio: float,
    excluded_block_indexes: Optional[set[int]] = None,
) -> list[int]:
    """Return bottom numeric blocks that need visual recall auditing."""
    missed: list[int] = []
    excluded_block_indexes = set(excluded_block_indexes or set())
    layout_profile = page_layout_profile(sidecar)
    for index, block in enumerate(sidecar.get("blocks", [])):
        if (
            index in candidate_indexes
            or index in excluded_block_indexes
            or not isinstance(block, Mapping)
        ):
            continue
        text = _text_from_block(block)
        bbox = _normalise_bbox(block, sidecar)
        key = _leading_footnote_key(text)
        label = str(block.get("label") or "").strip()
        if not text or not bbox or not key:
            continue
        if _NUMERIC_ONLY_RE.fullmatch(text):
            continue
        if _FURNITURE_LABEL_RE.search(label) or _CITATION_LABEL_RE.search(label):
            continue
        if (
            not _FOOTNOTE_LABEL_RE.search(label)
            and inline_ordered_list_warning(
                sidecar,
                index,
                bbox,
                layout_profile=layout_profile,
            )
        ):
            continue
        geometry = bottom_geometry(
            bbox,
            bottom_ratio=bottom_ratio,
            intersection_ratio=bottom_intersection_ratio,
        )
        if (
            bbox[3] >= float(bottom_ratio) + DEFAULT_RECALL_AUDIT_EDGE_MARGIN
            and not geometry["is_bottom"]
        ) or (
            _FOOTNOTE_LABEL_RE.search(label)
            and bbox[3] >= float(bottom_ratio)
            and not geometry["is_bottom"]
        ):
            missed.append(index)
    return missed


def region_union_bbox(
    boxes: Iterable[Optional[Iterable[float]]],
) -> Optional[list[float]]:
    """Return the smallest normalized bbox containing all valid boxes."""
    values: list[list[float]] = []
    for box in boxes:
        if not box:
            continue
        try:
            item = [float(value) for value in box]
        except (TypeError, ValueError):
            continue
        if len(item) == 4 and item[2] > item[0] and item[3] > item[1]:
            values.append(item)
    if not values:
        return None
    return [
        min(item[0] for item in values),
        min(item[1] for item in values),
        max(item[2] for item in values),
        max(item[3] for item in values),
    ]


def region_compact_blocks(
    sidecar: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    indexes: Iterable[int],
) -> list[dict[str, Any]]:
    """Build the compact block facts embedded in a region audit task."""
    by_index = {
        int(candidate["block"]): candidate
        for candidate in candidates
        if isinstance(candidate, Mapping)
        and str(candidate.get("block", "")).lstrip("-").isdigit()
    }
    blocks = sidecar.get("blocks", [])
    compact: list[dict[str, Any]] = []
    for raw_index in sorted({int(index) for index in indexes}):
        if (
            not 0 <= raw_index < len(blocks)
            or not isinstance(blocks[raw_index], Mapping)
        ):
            continue
        block = blocks[raw_index]
        candidate = by_index.get(raw_index, {})
        compact.append(
            {
                "block": raw_index,
                "order": block.get("order", raw_index),
                "label": str(block.get("label") or ""),
                "bbox": _normalise_bbox(block, sidecar),
                "text": _text_from_block(block)[:240],
                "key": candidate.get("key"),
                "candidate": bool(candidate),
                "audit_only": bool(candidate.get("audit_only")),
                "confidence": candidate.get("confidence"),
                "font_size": block.get("font_size"),
                "font_names": block.get("font_names"),
                "continuation_evidence": candidate.get("continuation_evidence"),
            }
        )
    return compact


# Private aliases preserve the old intra-module names for downstream callers
# while the public API exposes the evidence concepts by their domain names.
_is_ordinary_body_block = is_ordinary_body_block
_layout_records = layout_records
_page_layout_profile = page_layout_profile
_region_layout_evidence = region_layout_evidence
_layout_scope = layout_scope
_table_adjacent = table_adjacent
_body_flow_warning = body_flow_warning
_inline_ordered_list_warning = inline_ordered_list_warning
_bottom_geometry = bottom_geometry
_bottom_detection_stats = bottom_detection_stats
_suspected_missed_block_indexes = suspected_missed_block_indexes
_region_union_bbox = region_union_bbox
_region_compact_blocks = region_compact_blocks


__all__ = [
    "bottom_detection_stats",
    "bottom_geometry",
    "body_flow_warning",
    "inline_ordered_list_warning",
    "is_ordinary_body_block",
    "layout_records",
    "layout_scope",
    "page_layout_profile",
    "region_compact_blocks",
    "region_layout_evidence",
    "region_union_bbox",
    "suspected_missed_block_indexes",
    "table_adjacent",
]

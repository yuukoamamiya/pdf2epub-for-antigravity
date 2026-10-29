"""Build and compare page-level footnote candidate facts.

This module owns candidate construction for visual OCR and native-text PDF
sidecars, including compact review windows and secondary-OCR aggregation. It
does not accept a candidate as a final footnote and does not write workflow
artifacts; region acceptance remains in ``footnote_prepare``.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from .footnote_layout import (
    body_flow_warning as _body_flow_warning,
    bottom_geometry as _bottom_geometry,
    inline_ordered_list_warning as _inline_ordered_list_warning,
    page_layout_profile as _page_layout_profile,
    table_adjacent as _table_adjacent,
)
from .footnote_options import (
    DEFAULT_AUTO_ACCEPT,
    DEFAULT_BOTTOM_INTERSECTION_RATIO,
    DEFAULT_NATIVE_MAX_FONT_RATIO,
)
from .footnote_signals import (
    _CIRCLED_DIGITS_MAP,
    _CITATION_LABEL_RE,
    _FOOTNOTE_LABEL_RE,
    _INT_TO_CIRCLED_MAP,
    _NATIVE_NUMERIC_ONLY_RE,
    _NATIVE_SUP_MARKER_RE,
    _NUMERIC_ONLY_RE,
    _FURNITURE_LABEL_RE,
    leading_footnote_key as _leading_footnote_key,
    leading_footnote_marker as _leading_footnote_marker,
)
from .layout_evidence import (
    normalized_bbox as _normalise_bbox,
    text_from_block as _text_from_block,
)


_DECORATIVE_ACKNOWLEDGEMENT_RE = re.compile(
    r"^\s*[*†‡]\s*(?:acknowledg(?:e)?ments?|thanks|thank\s+you|"
    r"author['’]?s\s+note|致谢|鸣谢|感谢)?\s*$",
    re.IGNORECASE,
)


def candidate_for_block(
    page_number: int,
    block_index: int,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    bottom_ratio: float,
    bottom_intersection_ratio: float = DEFAULT_BOTTOM_INTERSECTION_RATIO,
    force_review: bool = False,
    source: str = "primary",
    region_evidence: bool = False,
    page_markdown: str = "",
    layout_profile: Optional[Mapping[str, Any]] = None,
) -> Optional[dict[str, Any]]:
    """Build one conservative candidate from a visual-OCR block."""
    text = _text_from_block(block)
    if not text:
        return None
    label = str(block.get("label") or "").strip()
    label_is_footnote = bool(_FOOTNOTE_LABEL_RE.search(label))
    label_is_citation = bool(_CITATION_LABEL_RE.search(label))
    label_is_furniture = bool(_FURNITURE_LABEL_RE.search(label))
    bbox = _normalise_bbox(block, sidecar)
    geometry = _bottom_geometry(
        bbox,
        bottom_ratio=bottom_ratio,
        intersection_ratio=bottom_intersection_ratio,
    )
    is_bottom = bool(geometry["is_bottom"])
    marker = _leading_footnote_marker(text) or _leading_footnote_marker(
        str(block.get("html") or "")
    )
    key = marker.key if marker else None
    layout_profile = (
        dict(layout_profile)
        if isinstance(layout_profile, Mapping)
        else _page_layout_profile(sidecar)
    )
    content_bottom = layout_profile.get("content_bottom")
    tail_tolerance = float(layout_profile.get("tail_tolerance") or 0.0)
    relative_tail = bool(
        bbox
        and content_bottom is not None
        and float(bbox[3]) >= float(content_bottom) - tail_tolerance
    )
    structural_label = bool(
        re.search(r"\b(?:table|tablecell|cell|caption|figure|image)\b", label, re.IGNORECASE)
    )
    table_adjacent = bool(
        key
        and not label_is_footnote
        and not structural_label
        and _table_adjacent(sidecar, block_index, bbox)
    )

    # Lists that follow a nearby prose sentence and still sit above the page
    # tail are body content, not page notes.  Keep explicitly labelled notes
    # reviewable even when their text happens to look like a list item.
    if not label_is_footnote and _inline_ordered_list_warning(
        sidecar,
        block_index,
        bbox,
        layout_profile=layout_profile,
    ):
        return None
    if not label_is_footnote and _DECORATIVE_ACKNOWLEDGEMENT_RE.fullmatch(text):
        return None

    # Labels are supporting evidence only. Citations, furniture and printed
    # page numbers must not become candidates from a label alone.
    if _NUMERIC_ONLY_RE.fullmatch(text):
        return None
    if label_is_citation and not region_evidence:
        return None
    if label_is_furniture and not region_evidence:
        return None
    # A leading letter followed by ')' is indistinguishable from a list item
    # without an explicit note label or independently detected note region.
    raw_html = str(block.get("html") or "")
    explicit_sup_letter = bool(
        marker
        and marker.kind == "letter"
        and re.match(r"^\s*<sup\b", raw_html, re.IGNORECASE)
    )
    if marker and marker.kind == "letter" and not (
        label_is_footnote or region_evidence or (table_adjacent and explicit_sup_letter)
    ):
        return None
    reliable_nonbottom_location = bool(
        bbox
        and (
            float(bbox[3]) >= 0.6
            or (
                float(bbox[2]) - float(bbox[0]) <= 0.45
                and (
                    float(bbox[0]) <= 0.12
                    or float(bbox[2]) >= 0.88
                )
            )
        )
    )
    explicit_label_review = bool(
        label_is_footnote
        and reliable_nonbottom_location
        and not (is_bottom or relative_tail or region_evidence)
    )
    if not (is_bottom or relative_tail or region_evidence or explicit_label_review or table_adjacent):
        return None
    if not (label_is_footnote or key or region_evidence):
        return None

    if label_is_footnote and is_bottom and key and not force_review and not region_evidence:
        confidence = "high"
        disposition = "local_candidate"
    else:
        confidence = "review"
        disposition = "review_required"

    candidate = {
        "page": page_number,
        "source": source,
        "block": block_index,
        "order": block.get("order", block_index),
        "label": label,
        "bbox": bbox,
        "key": key,
        "marker_label": marker.label if marker else "",
        "marker_identity": (
            {"kind": marker.kind, "label": marker.label, "key": marker.key}
            if marker else None
        ),
        "bottom_edge": geometry["bottom_edge"],
        "bottom_intersection_ratio": geometry["intersection_ratio"],
        "bottom_geometry": (
            "page_bottom"
            if is_bottom
            else "continuous_region"
            if region_evidence
            else "page_tail_profile"
            if relative_tail
            else "none"
        ),
        "relative_tail": relative_tail,
        "page_tail_tolerance": round(tail_tolerance, 6),
        "confidence": confidence,
        "disposition": disposition,
        "text": text[:240],
    }
    if key and page_markdown:
        candidate["same_page_marker"] = marker_before_block(
            page_markdown,
            sidecar,
            block_index,
            text,
            key=str(key),
        )
    flow_warning = _body_flow_warning(
        sidecar,
        block_index,
        bbox,
        layout_profile=layout_profile,
    )
    if flow_warning:
        candidate["body_flow_warning"] = flow_warning
        candidate["confidence"] = "review"
        candidate["disposition"] = "review_required"
        candidate["review_reason"] = flow_warning
    if explicit_label_review and not (is_bottom or relative_tail or region_evidence):
        candidate["review_reason"] = "explicit_footnote_label_outside_page_tail"
    if table_adjacent:
        candidate["review_reason"] = "marker_near_table_requires_visual_review"
    if explicit_label_review or table_adjacent:
        candidate["confidence"] = "review"
        candidate["disposition"] = "review_required"
    if force_review:
        candidate.setdefault("review_reason", "ocr_consensus_visual_review")
    if region_evidence:
        candidate["continuous_region_evidence"] = True
    return candidate


def candidate_block_indexes(candidate: Mapping[str, Any]) -> list[int]:
    """Return every sidecar block represented by a candidate window."""
    raw_indexes = candidate.get("block_indices")
    if isinstance(raw_indexes, list):
        indexes: list[int] = []
        for value in raw_indexes:
            try:
                index = int(value)
            except (TypeError, ValueError):
                continue
            if index >= 0 and index not in indexes:
                indexes.append(index)
        if indexes:
            return indexes
    try:
        index = int(candidate.get("block", -1))
    except (TypeError, ValueError):
        return []
    return [index] if index >= 0 else []


def window_for_page(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidate_blocks: list[dict[str, Any]],
    *,
    context_blocks: int,
) -> dict[str, Any]:
    """Build a bounded sidecar window around candidate blocks."""
    blocks = sidecar.get("blocks", [])
    candidate_indexes = {
        block_index
        for item in candidate_blocks
        for block_index in candidate_block_indexes(item)
    }
    indexes = set(candidate_indexes)
    for index in candidate_indexes:
        for offset in range(1, context_blocks + 1):
            if index - offset >= 0:
                indexes.add(index - offset)
            if index + offset < len(blocks):
                indexes.add(index + offset)

    compact_blocks = []
    for index in sorted(indexes):
        block = blocks[index]
        compact_blocks.append(
            {
                "block": index,
                "order": block.get("order", index),
                "label": str(block.get("label") or ""),
                "bbox": _normalise_bbox(block, sidecar),
                "text": _text_from_block(block)[:240],
                "candidate": index in candidate_indexes,
                "confidence": (
                    next(
                        (
                            str(item.get("confidence"))
                            for item in candidate_blocks
                            if index in candidate_block_indexes(item)
                        ),
                        None,
                    )
                    if index in candidate_indexes
                    else None
                ),
                "font_size": block.get("font_size"),
                "font_names": block.get("font_names"),
            }
        )
    return {"page": page_number, "blocks": compact_blocks}


def candidate_signature(
    candidates: Iterable[Mapping[str, Any]],
) -> list[tuple[str, str]]:
    """Return semantic evidence without comparing OCR line fragmentation."""
    result = []
    for candidate in candidates:
        label = str(candidate.get("label") or "").strip().casefold()
        key = str(candidate.get("key") or "")
        result.append((key, label))
    return sorted(result)


def aggregate_secondary_only_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse adjacent secondary OCR lines into reviewable note windows."""
    if not candidates:
        return []
    ordered = sorted(
        candidates,
        key=lambda item: (item.get("order", 0), item["block"]),
    )
    groups: list[list[dict[str, Any]]] = []
    for candidate in ordered:
        if not groups:
            groups.append([candidate])
            continue
        group = groups[-1]
        previous = group[-1]
        previous_block = int(previous["block"])
        current_block = int(candidate["block"])
        group_keys = {
            str(item.get("key") or "") for item in group if item.get("key")
        }
        current_key = str(candidate.get("key") or "")
        adjacent = current_block == previous_block + 1
        has_two_numeric_starts = bool(current_key and group_keys)
        if adjacent and not has_two_numeric_starts:
            group.append(candidate)
        else:
            groups.append([candidate])

    aggregated: list[dict[str, Any]] = []
    for group in groups:
        first = group[0]
        block_indices = [int(item["block"]) for item in group]
        merged = dict(first)
        merged["block_indices"] = block_indices
        merged["block_span"] = [block_indices[0], block_indices[-1]]
        merged["text"] = " ".join(
            str(item.get("text") or "").strip()
            for item in group
            if item.get("text")
        )[:240]
        merged["secondary_line_count"] = len(group)
        aggregated.append(merged)
    return aggregated


def _vertical_overlap(
    first: Optional[Iterable[float]],
    second: Optional[Iterable[float]],
) -> float:
    """Return vertical overlap ratio for normalized candidate boxes."""
    if not first or not second:
        return 0.0
    try:
        first_values = [float(value) for value in first]
        second_values = [float(value) for value in second]
        first_height = max(0.0, first_values[3] - first_values[1])
        second_height = max(0.0, second_values[3] - second_values[1])
        overlap = max(
            0.0,
            min(first_values[3], second_values[3])
            - max(first_values[1], second_values[1]),
        )
    except (TypeError, ValueError, IndexError):
        return 0.0
    denominator = min(first_height, second_height)
    return overlap / denominator if denominator > 0 else 0.0


def candidates_match(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
) -> bool:
    """Match semantic candidate regions across paragraph/line OCR layouts."""
    primary_key = str(primary.get("key") or "")
    secondary_key = str(secondary.get("key") or "")
    overlap = _vertical_overlap(primary.get("bbox"), secondary.get("bbox"))
    if primary_key and secondary_key:
        return primary_key == secondary_key and overlap >= 0.20
    return overlap >= 0.60


def marker_before_block(
    page_markdown: str,
    sidecar: Mapping[str, Any],
    block_index: int,
    block_text: str,
    *,
    key: str,
) -> bool:
    """Return whether a matching superscript occurs before this note block."""
    page_markdown = str(page_markdown or "")
    if not page_markdown:
        return False
    key = str(key)
    marker_keys = {key}
    unicode_key = key.translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
    if unicode_key != key:
        marker_keys.add(unicode_key)
    if key in _INT_TO_CIRCLED_MAP:
        marker_keys.add(_INT_TO_CIRCLED_MAP[key])

    chunks = re.split(r"\n{2,}", page_markdown.strip())
    if 0 <= block_index < len(chunks):
        block_start = sum(len(chunk) + 2 for chunk in chunks[:block_index])
    else:
        visible_text = re.sub(
            r"^\s*" + re.escape(key) + r"\s*",
            "",
            str(block_text or ""),
            count=1,
        )
        visible_text = re.sub(r"\s+", " ", visible_text).strip()
        first_token = visible_text.split(" ", 1)[0] if visible_text else ""
        candidate_end = page_markdown.rfind(first_token) if first_token else -1
        block_start = page_markdown.rfind("\n\n", 0, candidate_end) + 2

    for match in _NATIVE_SUP_MARKER_RE.finditer(page_markdown[:block_start]):
        marker_key = match.group("key")
        if marker_key is not None:
            if marker_key in _CIRCLED_DIGITS_MAP:
                marker_key = _CIRCLED_DIGITS_MAP[marker_key]
            if marker_key == key:
                return True
        if match.group("unicode") in marker_keys:
            return True
    return False


def candidate_for_native_block(
    page_number: int,
    block_index: int,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    bottom_ratio: float,
    bottom_intersection_ratio: float = DEFAULT_BOTTOM_INTERSECTION_RATIO,
    page_markdown: str = "",
    auto_accept: bool = DEFAULT_AUTO_ACCEPT,
    native_max_font_ratio: float = DEFAULT_NATIVE_MAX_FONT_RATIO,
    layout_profile: Optional[Mapping[str, Any]] = None,
    region_evidence: bool = False,
) -> Optional[dict[str, Any]]:
    """Build conservative native-text candidates for workspace review."""
    text = _text_from_block(block)
    marker = _leading_footnote_marker(text)
    key = marker.key if marker else None
    if not text or _NATIVE_NUMERIC_ONLY_RE.fullmatch(text) or (
        key is None and not region_evidence
    ):
        return None
    try:
        font_size = float(block.get("font_size"))
        body_font_size = float(sidecar.get("body_font_size"))
    except (TypeError, ValueError):
        font_size = body_font_size = 0.0
    candidate = candidate_for_block(
        page_number,
        block_index,
        block,
        sidecar,
        bottom_ratio=bottom_ratio,
        bottom_intersection_ratio=bottom_intersection_ratio,
        force_review=True,
        region_evidence=region_evidence,
        page_markdown=page_markdown,
        layout_profile=layout_profile,
    )
    if candidate is None:
        return None
    candidate["key"] = key
    candidate["marker_label"] = marker.label if marker else ""
    candidate["marker_identity"] = (
        {"kind": marker.kind, "label": marker.label, "key": marker.key}
        if marker else None
    )
    candidate["source_kind"] = "native_text"
    candidate["review_reason"] = (
        "continuous_numeric_region" if region_evidence else "native_layout_candidate"
    )
    if block.get("font_size") is not None:
        candidate["font_size"] = block.get("font_size")
    if font_size > 0 and body_font_size > 0:
        candidate["font_size_ratio"] = round(font_size / body_font_size, 3)
    if block.get("font_names"):
        candidate["font_names"] = block.get("font_names")

    font_ratio = candidate.get("font_size_ratio")
    marker_found = marker_before_block(
        page_markdown,
        sidecar,
        block_index,
        text,
        key=str(candidate["key"]),
    )
    candidate["same_page_superscript"] = marker_found
    candidate["auto_accept"] = bool(auto_accept)
    candidate["native_max_font_ratio"] = float(native_max_font_ratio)
    if (
        auto_accept
        and not region_evidence
        and marker_found
        and isinstance(font_ratio, (int, float))
        and font_ratio <= float(native_max_font_ratio)
        and not candidate.get("body_flow_warning")
    ):
        candidate["confidence"] = "high"
        candidate["disposition"] = "local_candidate"
        candidate["review_reason"] = "native_superscript_and_small_text"
    else:
        candidate["confidence"] = "review"
        candidate["disposition"] = "review_required"
    return candidate


__all__ = [
    "aggregate_secondary_only_candidates",
    "candidate_block_indexes",
    "candidate_for_block",
    "candidate_for_native_block",
    "candidate_signature",
    "candidates_match",
    "marker_before_block",
    "window_for_page",
]

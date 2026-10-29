"""Detect physical regions that may contain continuous page footnotes.

This module owns geometry-only region discovery and candidate expansion. It
does not score cross-page semantics or assign final footnote roles. Candidate
construction remains in :mod:`footnote_candidates` and is imported lazily so
the layout layer stays independent at import time.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping, Optional

from .footnote_signals import (
    _CITATION_LABEL_RE,
    _FOOTNOTE_LABEL_RE,
    _NUMERIC_ONLY_RE,
    leading_footnote_key as _leading_footnote_key,
)
from .layout_evidence import (
    normalized_bbox as _normalise_bbox,
    text_from_block as _text_from_block,
)

DEFAULT_CONTINUOUS_REGION_BACKTRACK_RATIO = 0.16
DEFAULT_CONTINUOUS_REGION_MAX_GAP = 0.045


def _horizontal_overlap(
    first: Optional[Iterable[float]],
    second: Optional[Iterable[float]],
) -> float:
    """Return overlap over the narrower horizontal span."""
    if not first or not second:
        return 0.0
    try:
        first_values = [float(value) for value in first]
        second_values = [float(value) for value in second]
        first_width = max(0.0, first_values[2] - first_values[0])
        second_width = max(0.0, second_values[2] - second_values[0])
        overlap = max(
            0.0,
            min(first_values[2], second_values[2])
            - max(first_values[0], second_values[0]),
        )
    except (TypeError, ValueError, IndexError):
        return 0.0
    denominator = min(first_width, second_width)
    return overlap / denominator if denominator > 0 else 0.0


def _continuous_numeric_regions(
    sidecar: Mapping[str, Any],
    *,
    bottom_ratio: float,
    backtrack_ratio: float = DEFAULT_CONTINUOUS_REGION_BACKTRACK_RATIO,
    max_gap: float = DEFAULT_CONTINUOUS_REGION_MAX_GAP,
) -> list[dict[str, Any]]:
    """Find consecutive numbered blocks that form one bottom-note region."""
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list):
        return []
    records: list[dict[str, Any]] = []
    minimum_edge = max(0.0, float(bottom_ratio) - float(backtrack_ratio))
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        text = _text_from_block(block)
        key = _leading_footnote_key(text)
        bbox = _normalise_bbox(block, sidecar)
        if not text or not bbox or not key or _NUMERIC_ONLY_RE.fullmatch(text):
            continue
        # Special markers (for example *, †, or ‡) remain ordinary candidates
        # for their owning review path, but numeric sequence inference must not
        # assume they can be converted to an integer.
        try:
            numeric_key = int(key)
        except (TypeError, ValueError):
            continue
        if bbox[3] < minimum_edge:
            continue
        label = str(block.get("label") or "").strip()
        if _CITATION_LABEL_RE.search(label):
            continue
        records.append(
            {
                "index": index,
                "key": numeric_key,
                "bbox": bbox,
                "label": label,
            }
        )

    regions: list[dict[str, Any]] = []
    current: list[dict[str, Any]] = []
    for record in records:
        if not current:
            current = [record]
            continue
        previous = current[-1]
        contiguous_key = record["key"] == previous["key"] + 1
        contiguous_block = record["index"] - previous["index"] <= 3
        vertical_gap = record["bbox"][1] - previous["bbox"][3]
        close_enough = (
            vertical_gap <= max_gap
            and _horizontal_overlap(record["bbox"], previous["bbox"]) >= 0.2
        )
        if contiguous_key and contiguous_block and close_enough:
            current.append(record)
        else:
            if len(current) >= 2:
                regions.append({"records": current})
            current = [record]
    if len(current) >= 2:
        regions.append({"records": current})

    normalized: list[dict[str, Any]] = []
    for region in regions:
        records_in_region = region["records"]
        first = records_in_region[0]
        last = records_in_region[-1]
        min_y = min(item["bbox"][1] for item in records_in_region)
        max_y = max(item["bbox"][3] for item in records_in_region)
        normalized.append(
            {
                "keys": [str(item["key"]) for item in records_in_region],
                "numeric_block_indices": [
                    item["index"] for item in records_in_region
                ],
                "first_block": first["index"],
                "last_block": last["index"],
                "bbox": [
                    min(item["bbox"][0] for item in records_in_region),
                    min_y,
                    max(item["bbox"][2] for item in records_in_region),
                    max_y,
                ],
            }
        )
    return normalized


def _expand_continuous_region_candidates(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    *,
    bottom_ratio: float,
    bottom_intersection_ratio: float,
    native_text_source: bool,
    page_markdown: str,
    auto_accept: bool,
    native_max_font_ratio: float,
    layout_profile: Optional[Mapping[str, Any]] = None,
    previous_page_footnote_evidence: bool = False,
    excluded_block_indexes: Optional[set[int]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Add review-only blocks surrounding a consecutive numeric note run."""
    from .footnote_candidates import (
        candidate_for_block as _candidate_for_block,
        candidate_for_native_block as _candidate_for_native_block,
    )

    regions = _continuous_numeric_regions(sidecar, bottom_ratio=bottom_ratio)
    if not regions:
        return candidates, []
    blocks = sidecar.get("blocks", [])
    excluded_block_indexes = set(excluded_block_indexes or set())
    existing = {int(item["block"]) for item in candidates}
    expanded: list[dict[str, Any]] = list(candidates)
    normalized_regions: list[dict[str, Any]] = []
    for region in regions:
        indexes = list(region["numeric_block_indices"])
        # A typed auxiliary unit can share a physical page with ordinary
        # prose. Do not let the numeric-region backtracker bridge into its
        # blocks; explicit candidates on the ordinary side remain safer.
        if any(index in excluded_block_indexes for index in indexes):
            continue
        region_indexes = set(indexes)
        numeric_blocks_have_footnote_label = any(
            _FOOTNOTE_LABEL_RE.search(str(blocks[index].get("label") or ""))
            for index in indexes
            if 0 <= index < len(blocks) and isinstance(blocks[index], Mapping)
        )
        region_top = float(region["bbox"][1])
        backward_indexes: list[int] = []
        cursor = int(region["first_block"]) - 1
        while cursor >= 0:
            block = blocks[cursor]
            if not isinstance(block, Mapping):
                break
            if cursor in excluded_block_indexes:
                break
            text = _text_from_block(block)
            bbox = _normalise_bbox(block, sidecar)
            if not text or not bbox or _NUMERIC_ONLY_RE.fullmatch(text):
                break
            # A different/duplicate numeric start is a region boundary. The
            # upward backtrack is for unnumbered continuation lines, not for
            # ordinary numbered prose immediately above the confirmed run.
            if _leading_footnote_key(text) is not None:
                break
            # When the numbered run already carries an explicit Footnote
            # label, an adjacent Text block is more likely to be the final
            # body line than an unlabelled continuation. Suppress that noisy
            # backtrack for an ordinary page, but keep it when the preceding
            # page has footnote evidence: ``body -> continuation -> new note``
            # is a legitimate cross-page layout, and OCR may label the
            # continuation as ordinary Text. Unlabelled numeric runs retain
            # the conservative historical backtracking rule.
            if (
                numeric_blocks_have_footnote_label
                and not _FOOTNOTE_LABEL_RE.search(str(block.get("label") or ""))
                and not previous_page_footnote_evidence
            ):
                break
            if bbox[3] < float(region_top) - DEFAULT_CONTINUOUS_REGION_MAX_GAP:
                break
            if (
                bbox[3]
                < float(bottom_ratio) - DEFAULT_CONTINUOUS_REGION_BACKTRACK_RATIO
            ):
                break
            if _horizontal_overlap(bbox, region["bbox"]) < 0.12:
                break
            backward_indexes.append(cursor)
            region_indexes.add(cursor)
            region_top = bbox[1]
            cursor -= 1

        for block_index in sorted(region_indexes):
            if block_index in existing:
                for candidate in expanded:
                    if int(candidate.get("block", -1)) == block_index:
                        candidate["continuous_region_evidence"] = True
                        candidate["continuous_region_has_footnote_label"] = (
                            numeric_blocks_have_footnote_label
                        )
                        candidate.setdefault(
                            "review_reason", "continuous_numeric_region"
                        )
                continue
            block = blocks[block_index]
            if not isinstance(block, Mapping):
                continue
            if native_text_source:
                candidate = _candidate_for_native_block(
                    page_number,
                    block_index,
                    block,
                    sidecar,
                    bottom_ratio=bottom_ratio,
                    bottom_intersection_ratio=bottom_intersection_ratio,
                    page_markdown=page_markdown,
                    auto_accept=auto_accept,
                    native_max_font_ratio=native_max_font_ratio,
                    region_evidence=True,
                    layout_profile=layout_profile,
                )
            else:
                candidate = _candidate_for_block(
                    page_number,
                    block_index,
                    block,
                    sidecar,
                    bottom_ratio=bottom_ratio,
                    bottom_intersection_ratio=bottom_intersection_ratio,
                    force_review=True,
                    source="primary",
                    region_evidence=True,
                    page_markdown=page_markdown,
                    layout_profile=layout_profile,
                )
            if candidate is None:
                continue
            candidate["confidence"] = "review"
            candidate["disposition"] = "review_required"
            candidate["review_reason"] = "continuous_numeric_region"
            candidate["continuous_region_evidence"] = True
            candidate["continuous_region_has_footnote_label"] = (
                numeric_blocks_have_footnote_label
            )
            expanded.append(candidate)

        normalized_regions.append(
            {
                **region,
                "backtracked_block_indices": sorted(backward_indexes),
                "review_block_indices": sorted(region_indexes),
            }
        )
    expanded.sort(
        key=lambda item: (item.get("order", item.get("block", 0)), item["block"])
    )
    return expanded, normalized_regions


def _auto_continuation_key(candidate: Mapping[str, Any]) -> Optional[str]:
    """Return the last physical prior-page key for strong continuation evidence."""
    evidence = candidate.get("continuation_evidence")
    if not isinstance(evidence, Mapping):
        return None
    if evidence.get("confidence") not in {"high", "medium"}:
        return None
    if candidate.get("audit_only"):
        return None
    matched_key = str(evidence.get("matched_previous_key") or "").strip()
    last_key = str(evidence.get("last_physical_key") or "").strip()
    matched_block = evidence.get("matched_previous_block")
    last_block = evidence.get("last_physical_block")
    if not matched_key or not last_key or matched_key != last_key:
        return None
    if matched_block != last_block:
        return None
    # A closed final note may still continue in unusual typography, but it is
    # not safe to auto-link it. Keep that case in the visual audit queue.
    if "previous_text_looks_complete" in evidence.get("negative_signals", []):
        return None
    return matched_key


__all__ = [
    "_auto_continuation_key",
    "_continuous_numeric_regions",
    "_expand_continuous_region_candidates",
    "_horizontal_overlap",
]

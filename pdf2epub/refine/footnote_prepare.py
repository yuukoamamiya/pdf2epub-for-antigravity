"""Prepare a compact footnote review hand-off.

The OCR backends already persist page layout sidecars next to the Markdown
view.  This module uses those sidecars to do the cheap part locally: identify
bottom-of-page footnote candidates, preserve their *within-page* order, and
create small review windows for only the ambiguous cases.  It may group a
consecutive numeric bottom region and include adjacent unnumbered blocks as
evidence, but it deliberately does not decide that a block is a continuation
of a previous footnote; that is the visual/semantic judgment reserved for the
workspace Subagent.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..workflow_contracts import atomic_write_text, sha256_file
from .layout_evidence import (
    load_layout_sidecar as _load_sidecar,
    normalized_bbox as _normalise_bbox,
    text_from_block as _text_from_block,
)
from .pdf_evidence import (
    is_native_text_source,
    pdf_evidence_mode,
    require_current_consensus,
)
FOOTNOTE_PREPARE_SCHEMA_VERSION = 1
DEFAULT_BOTTOM_RATIO = 0.64
DEFAULT_BOTTOM_INTERSECTION_RATIO = 0.25
DEFAULT_CONTINUOUS_REGION_BACKTRACK_RATIO = 0.16
DEFAULT_CONTINUOUS_REGION_MAX_GAP = 0.045
DEFAULT_CONTEXT_BLOCKS = 2
DEFAULT_AUTO_ACCEPT = True
DEFAULT_NATIVE_MAX_FONT_RATIO = 0.88
_DECISION_ROLES = frozenset(
    {
        "body",
        "citation",
        "bibliography",
        "footnote_start",
        "footnote_continuation",
        "footnote_definition",
        "review_required",
    }
)
_MOVED_ROLES = frozenset(
    {"footnote_start", "footnote_continuation", "footnote_definition"}
)
_FOOTNOTE_LABEL_RE = re.compile(
    r"(?:foot[\s_-]*note\b|注脚|脚注|页下注)", re.IGNORECASE
)
_CITATION_LABEL_RE = re.compile(
    r"(?:bibliograph(?:y|ies)|reference(?:s)?|citation|参考文献|引用|文献)",
    re.IGNORECASE,
)
_FURNITURE_LABEL_RE = re.compile(
    r"(?:page[\s_-]*(?:header|footer)|running[\s_-]*(?:header|footer))",
    re.IGNORECASE,
)
_FOOTNOTE_KEY_RE = re.compile(
    r"^\s*(?P<key>\d{1,4})(?=\s|[.)、，:：;；\-–—]|$)",
    re.IGNORECASE,
)
_FOOTNOTE_SUP_KEY_RE = re.compile(
    r"^\s*<sup\b[^>]*>\s*(?P<key>\d{1,4})\s*</sup>(?=\s|[.)、，:：;；\-–—]|$)",
    re.IGNORECASE,
)
_FOOTNOTE_UNICODE_KEY_RE = re.compile(
    r"^\s*(?P<key>[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,4})(?=\s|[.)、，:：;；\-–—]|$)"
)
_NUMERIC_ONLY_RE = re.compile(r"^\s*\d{1,4}\s*$")
_NATIVE_NUMERIC_ONLY_RE = _NUMERIC_ONLY_RE
_NATIVE_SUP_MARKER_RE = re.compile(
    r"<sup\b[^>]*>\s*(?P<key>\d{1,4})\s*</sup>|"
    r"(?P<unicode>[⁰¹²³⁴⁵⁶⁷⁸⁹]{1,4})"
)
_SUPERSCRIPT_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")


@dataclass(frozen=True)
class FootnotePrepareOptions:
    """Validated settings shared by candidate and handoff preparation."""

    bottom_ratio: float = DEFAULT_BOTTOM_RATIO
    bottom_intersection_ratio: float = DEFAULT_BOTTOM_INTERSECTION_RATIO
    context_blocks: int = DEFAULT_CONTEXT_BLOCKS
    auto_accept: bool = DEFAULT_AUTO_ACCEPT
    native_max_font_ratio: float = DEFAULT_NATIVE_MAX_FONT_RATIO


def _leading_footnote_key(text: str) -> Optional[str]:
    """Extract a conservative key from the beginning of a layout block.

    Chandra normally stores the block as plain text after HTML tags are
    stripped, but older sidecars may retain a ``sup`` tag or Unicode
    superscript digits. Requiring whitespace or punctuation after the key
    avoids treating years and embedded numbers as note definitions.
    """
    value = str(text or "")
    for pattern in (
        _FOOTNOTE_SUP_KEY_RE,
        _FOOTNOTE_KEY_RE,
        _FOOTNOTE_UNICODE_KEY_RE,
    ):
        match = pattern.match(value)
        if match:
            return match.group("key").translate(_SUPERSCRIPT_DIGITS)
    return None


def resolve_footnote_options(
    config: Optional[Mapping[str, Any]] = None,
    *,
    bottom_ratio: Optional[float] = None,
    bottom_intersection_ratio: Optional[float] = None,
    context_blocks: Optional[int] = None,
    auto_accept: Optional[bool] = None,
    native_max_font_ratio: Optional[float] = None,
) -> FootnotePrepareOptions:
    """Resolve config defaults and CLI overrides in one place."""
    footnote_config = config.get("footnotes", {}) if isinstance(config, Mapping) else {}
    if not isinstance(footnote_config, Mapping):
        footnote_config = {}

    resolved_bottom_ratio = (
        bottom_ratio
        if bottom_ratio is not None
        else footnote_config.get("bottom_ratio", DEFAULT_BOTTOM_RATIO)
    )
    try:
        resolved_bottom_ratio = float(resolved_bottom_ratio)
    except (TypeError, ValueError):
        resolved_bottom_ratio = DEFAULT_BOTTOM_RATIO
    if not 0.5 <= resolved_bottom_ratio < 1.0:
        raise ValueError("bottom_ratio must be between 0.5 and 1.0")

    resolved_bottom_intersection_ratio = (
        bottom_intersection_ratio
        if bottom_intersection_ratio is not None
        else footnote_config.get(
            "bottom_intersection_ratio", DEFAULT_BOTTOM_INTERSECTION_RATIO
        )
    )
    try:
        resolved_bottom_intersection_ratio = float(resolved_bottom_intersection_ratio)
    except (TypeError, ValueError):
        resolved_bottom_intersection_ratio = DEFAULT_BOTTOM_INTERSECTION_RATIO
    if not 0.0 < resolved_bottom_intersection_ratio <= 1.0:
        raise ValueError("bottom_intersection_ratio must be between 0 and 1")

    resolved_context_blocks = (
        context_blocks
        if context_blocks is not None
        else footnote_config.get("context_blocks", DEFAULT_CONTEXT_BLOCKS)
    )
    try:
        resolved_context_blocks = max(0, int(resolved_context_blocks))
    except (TypeError, ValueError):
        resolved_context_blocks = DEFAULT_CONTEXT_BLOCKS

    resolved_auto_accept = (
        auto_accept
        if auto_accept is not None
        else footnote_config.get("auto_accept", DEFAULT_AUTO_ACCEPT)
    )

    resolved_font_ratio = (
        native_max_font_ratio
        if native_max_font_ratio is not None
        else footnote_config.get(
            "native_max_font_ratio", DEFAULT_NATIVE_MAX_FONT_RATIO
        )
    )
    try:
        resolved_font_ratio = float(resolved_font_ratio)
    except (TypeError, ValueError):
        resolved_font_ratio = DEFAULT_NATIVE_MAX_FONT_RATIO
    if not 0.0 < resolved_font_ratio <= 1.0:
        raise ValueError("native_max_font_ratio must be between 0 and 1")

    return FootnotePrepareOptions(
        bottom_ratio=resolved_bottom_ratio,
        bottom_intersection_ratio=resolved_bottom_intersection_ratio,
        context_blocks=resolved_context_blocks,
        auto_accept=bool(resolved_auto_accept),
        native_max_font_ratio=resolved_font_ratio,
    )


def _candidate_for_block(
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
) -> Optional[dict[str, Any]]:
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
    key = _leading_footnote_key(text)

    # A candidate must have either an explicit OCR footnote label or a strong
    # bottom-of-page + numbered-start signal.  Ordinary low page text is not
    # sent to the Subagent merely because it contains a number.
    # OCR can label bibliography/reference material as a bottom block too.
    # It is never a page footnote candidate: citations and bibliography are
    # semantic book content and must remain where the chapter puts them.
    # A block containing only a short number is overwhelmingly a printed page
    # number.  This applies to visual OCR as well as native layout; allowing a
    # ``Footnote`` label to override it would reintroduce page-footer false
    # positives.
    if _NUMERIC_ONLY_RE.fullmatch(text):
        return None
    if label_is_citation and not region_evidence:
        return None
    if label_is_furniture and not region_evidence:
        return None
    # The label is semantic evidence only.  It cannot make a block outside the
    # page-bottom geometry a candidate.  A continuous numbered region may
    # extend the review window upward, but that path is always review-only.
    if not (is_bottom or region_evidence):
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
        "bottom_edge": geometry["bottom_edge"],
        "bottom_intersection_ratio": geometry["intersection_ratio"],
        "bottom_geometry": "page_bottom" if is_bottom else (
            "continuous_region" if region_evidence else "none"
        ),
        "confidence": confidence,
        "disposition": disposition,
        "text": text[:240],
    }
    if force_review:
        candidate["review_reason"] = "ocr_consensus_visual_review"
    if region_evidence:
        candidate["continuous_region_evidence"] = True
    return candidate


def _bottom_geometry(
    bbox: Optional[Iterable[float]],
    *,
    bottom_ratio: float,
    intersection_ratio: float,
) -> dict[str, Any]:
    """Return the page-bottom evidence for one normalized block.

    ``bbox[3]`` is the lower edge in normalized page coordinates.  It is
    necessary but not sufficient: a block only qualifies when the portion of
    its own height that intersects the bottom band is large enough.  This
    avoids treating a normal body paragraph that merely clips the top of the
    band as a footnote candidate.
    """
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


def _window_for_page(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidate_blocks: list[dict[str, Any]],
    *,
    context_blocks: int,
) -> dict[str, Any]:
    blocks = sidecar.get("blocks", [])
    candidate_indexes = {
        block_index
        for item in candidate_blocks
        for block_index in _candidate_block_indexes(item)
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
                            if index in _candidate_block_indexes(item)
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


def _horizontal_overlap(first: Optional[Iterable[float]], second: Optional[Iterable[float]]) -> float:
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
    """Find consecutive numbered blocks that form one bottom-note region.

    OCR layout models sometimes emit only the last few numbered definitions as
    bottom blocks.  A run such as ``18, 19, 20, 21`` is a strong signal that
    the blocks belong to one notes area, so the caller can walk upward from
    the first number and include adjacent unnumbered lines for visual review.
    This function only returns evidence; it never assigns a footnote role.
    """
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
        if bbox[3] < minimum_edge:
            continue
        label = str(block.get("label") or "").strip()
        if _CITATION_LABEL_RE.search(label):
            continue
        records.append(
            {
                "index": index,
                "key": int(key),
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
                "numeric_block_indices": [item["index"] for item in records_in_region],
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
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Add review-only blocks surrounding a consecutive numeric note run."""
    regions = _continuous_numeric_regions(
        sidecar,
        bottom_ratio=bottom_ratio,
    )
    if not regions:
        return candidates, []
    blocks = sidecar.get("blocks", [])
    existing = {int(item["block"]) for item in candidates}
    expanded: list[dict[str, Any]] = list(candidates)
    normalized_regions: list[dict[str, Any]] = []
    for region in regions:
        indexes = list(region["numeric_block_indices"])
        region_indexes = set(indexes)
        region_top = float(region["bbox"][1])
        backward_indexes: list[int] = []
        cursor = int(region["first_block"]) - 1
        while cursor >= 0:
            block = blocks[cursor]
            if not isinstance(block, Mapping):
                break
            text = _text_from_block(block)
            bbox = _normalise_bbox(block, sidecar)
            if not text or not bbox or _NUMERIC_ONLY_RE.fullmatch(text):
                break
            # A different/duplicate numeric start is a region boundary.  The
            # upward backtrack is for unnumbered continuation lines, not for
            # ordinary numbered prose immediately above the confirmed run.
            if _leading_footnote_key(text) is not None:
                break
            if bbox[3] < float(region_top) - DEFAULT_CONTINUOUS_REGION_MAX_GAP:
                break
            if bbox[3] < float(bottom_ratio) - DEFAULT_CONTINUOUS_REGION_BACKTRACK_RATIO:
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
                        candidate.setdefault("review_reason", "continuous_numeric_region")
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
                )
            if candidate is None:
                continue
            candidate["confidence"] = "review"
            candidate["disposition"] = "review_required"
            candidate["review_reason"] = "continuous_numeric_region"
            candidate["continuous_region_evidence"] = True
            expanded.append(candidate)

        normalized_regions.append(
            {
                **region,
                "backtracked_block_indices": sorted(backward_indexes),
                "review_block_indices": sorted(region_indexes),
            }
        )
    expanded.sort(key=lambda item: (item.get("order", item.get("block", 0)), item["block"]))
    return expanded, normalized_regions


def _bottom_detection_stats(
    sidecar: Mapping[str, Any],
    candidate_indexes: set[int],
    *,
    bottom_ratio: float,
) -> dict[str, int]:
    """Count bottom numeric evidence that the candidate list did not cover."""
    stats = {
        "bottom_block_count": 0,
        "bottom_numeric_block_count": 0,
        "bottom_numeric_candidate_count": 0,
        "excluded_page_furniture_count": 0,
        "excluded_numeric_page_count": 0,
        "suspected_missed_count": 0,
    }
    for index, block in enumerate(sidecar.get("blocks", [])):
        if not isinstance(block, Mapping):
            continue
        bbox = _normalise_bbox(block, sidecar)
        if not bbox or bbox[3] < float(bottom_ratio):
            continue
        stats["bottom_block_count"] += 1
        text = _text_from_block(block)
        key = _leading_footnote_key(text)
        label = str(block.get("label") or "").strip()
        is_furniture = bool(_FURNITURE_LABEL_RE.search(label))
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
        if is_furniture or _NUMERIC_ONLY_RE.fullmatch(text) or _CITATION_LABEL_RE.search(label):
            continue
        stats["suspected_missed_count"] += 1
    return stats


def _ensure_visual_review_images(
    output_dir: Path,
    review_pages: Iterable[int],
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Render/reuse page PNGs for the pages sent to the Subagent.

    OCR correction already owns freshness-aware PDF page rendering.  Reusing
    that renderer keeps image hashes and the source-PDF choice consistent
    across workflow stages.  A sidecar-only library caller (including tests)
    remains valid when no source PDF is available; it simply gets an explicit
    ``available: false`` evidence record.
    """
    pages = sorted({int(page) for page in review_pages})
    if not pages:
        return {"available": False, "reason": "no_review_pages", "pages": []}
    try:
        from ..ocr_correction import render_review_images

        correction_config = (
            config.get("ocr_correction", {})
            if isinstance(config, Mapping)
            else {}
        )
        try:
            dpi = int(correction_config.get("review_dpi", 150))
        except (TypeError, ValueError):
            dpi = 150
        manifest = render_review_images(output_dir, dpi=dpi, resume=True)
        image_dir = str(manifest.get("image_dir") or "ocr_review_images")
        image_files = {
            page: f"{image_dir}/page_{page:03d}.png"
            for page in pages
            if (output_dir / image_dir / f"page_{page:03d}.png").is_file()
        }
        if len(image_files) != len(pages):
            missing = [page for page in pages if page not in image_files]
            return {
                "available": False,
                "reason": "rendered_page_images_missing",
                "pages": pages,
                "missing_pages": missing,
            }
        return {
            "available": True,
            "pages": pages,
            "image_dir": image_dir,
            "files": {str(page): path for page, path in image_files.items()},
            "manifest": "ocr_review_images.json",
        }
    except (OSError, ValueError, RuntimeError, ImportError) as exc:
        return {
            "available": False,
            "pages": pages,
            "reason": str(exc)[:240],
        }


def _build_cross_page_windows(
    page_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair adjacent pages without assuming where the continuation appears.

    The next page's candidate blocks remain in their original visual order.
    This is important for the common ``body -> previous-note continuation ->
    new-note`` layout; the continuation is not incorrectly attached to the
    top of the next page.
    """
    windows = []
    by_page = {int(item["page"]): item for item in page_reports}
    for report in page_reports:
        page_number = int(report["page"])
        if not report["candidates"]:
            continue
        following = by_page.get(page_number + 1)
        if not following or not following["candidates"]:
            continue
        if not any(
            candidate.get("confidence") == "review"
            for candidate in report["candidates"] + following["candidates"]
        ):
            # Two pages made entirely of deterministic native candidates do
            # not need a continuation handoff.  Keep the expensive adjacent
            # page review for cases where at least one side is ambiguous.
            continue
        windows.append(
            {
                "pages": [page_number, page_number + 1],
                "left_candidates": report["candidates"],
                "right_candidates": following["candidates"],
                "reason": "adjacent_pages_contain_ordered_footnote_candidates",
            }
        )
    return windows


def _unit_context_path(output_dir: Path, source_name: str) -> Path:
    safe_name = Path(str(source_name)).name
    if (
        not safe_name
        or safe_name in {".", ".."}
        or safe_name != str(source_name)
        or Path(safe_name).suffix.lower() != ".md"
    ):
        raise ValueError(f"Invalid refinement unit filename: {source_name}")
    return output_dir / "footnote_contexts" / f"{safe_name}.json"


def _load_refined_units(output_dir: Path) -> list[dict[str, Any]]:
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    try:
        value = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    units = value.get("units", []) if isinstance(value, dict) else []
    return [item for item in units if isinstance(item, dict)]


def _load_consensus_review_pages(output_dir: Path) -> set[int]:
    """Return pages whose two OCR results require visual review."""
    try:
        value = json.loads(
            (Path(output_dir) / "ocr_consensus.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    records = value.get("records", {}) if isinstance(value, Mapping) else {}
    if not isinstance(records, Mapping):
        return set()
    pages: set[int] = set()
    for name, record in records.items():
        if not isinstance(record, Mapping) or record.get("action") != "visual_review":
            continue
        match = re.match(r"^page_(\d+)\.md$", str(name))
        if match:
            pages.add(int(match.group(1)))
    return pages


def _candidate_signature(candidates: Iterable[Mapping[str, Any]]) -> list[tuple[str, str]]:
    """Return semantic evidence without comparing OCR line fragmentation."""
    result = []
    for candidate in candidates:
        label = str(candidate.get("label") or "").strip().casefold()
        key = str(candidate.get("key") or "")
        result.append((key, label))
    return sorted(result)


def _candidate_block_indexes(candidate: Mapping[str, Any]) -> list[int]:
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


def _aggregate_secondary_only_candidates(
    candidates: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Collapse adjacent secondary OCR lines into reviewable note windows.

    A secondary OCR backend may emit one physical line per block while the
    primary backend emits a whole paragraph.  A secondary-only note therefore
    must be one review decision, not one decision per physical line.
    """
    if not candidates:
        return []
    ordered = sorted(candidates, key=lambda item: (item.get("order", 0), item["block"]))
    groups: list[list[dict[str, Any]]] = []
    for candidate in ordered:
        if not groups:
            groups.append([candidate])
            continue
        group = groups[-1]
        previous = group[-1]
        previous_block = int(previous["block"])
        current_block = int(candidate["block"])
        group_keys = {str(item.get("key") or "") for item in group if item.get("key")}
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
            str(item.get("text") or "").strip() for item in group if item.get("text")
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


def _candidates_match(
    primary: Mapping[str, Any],
    secondary: Mapping[str, Any],
) -> bool:
    """Match semantic footnote regions across paragraph/line OCR layouts."""
    primary_key = str(primary.get("key") or "")
    secondary_key = str(secondary.get("key") or "")
    overlap = _vertical_overlap(primary.get("bbox"), secondary.get("bbox"))
    if primary_key and secondary_key:
        return primary_key == secondary_key and overlap >= 0.20
    return overlap >= 0.60


def _secondary_sidecar_path(output_dir: Path, sidecar_path: Path) -> Path:
    return Path(output_dir) / "ocr_secondary" / sidecar_path.name


def _candidate_for_native_block(
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
    region_evidence: bool = False,
) -> Optional[dict[str, Any]]:
    """Find conservative native-text candidates for workspace review.

    Native extraction has no semantic OCR labels.  A bottom numbered block is
    therefore reviewed unless it also has the conservative local evidence
    described below.  A page-number-only block is excluded because it is
    common page furniture.
    A candidate is locally accepted only when the same page contains an earlier
    superscript reference with the same key and the candidate is materially
    smaller than the page body font.  Everything else remains a workspace
    review item.  This keeps the cheap path conservative without sending every
    bottom-of-page numbered block to the Subagent.
    """
    text = _text_from_block(block)
    key = _leading_footnote_key(text)
    if not text or _NATIVE_NUMERIC_ONLY_RE.fullmatch(text) or (
        key is None and not region_evidence
    ):
        return None
    try:
        font_size = float(block.get("font_size"))
        body_font_size = float(sidecar.get("body_font_size"))
    except (TypeError, ValueError):
        font_size = body_font_size = 0.0
    candidate = _candidate_for_block(
        page_number,
        block_index,
        block,
        sidecar,
        bottom_ratio=bottom_ratio,
        bottom_intersection_ratio=bottom_intersection_ratio,
        force_review=True,
        region_evidence=region_evidence,
    )
    if candidate is None:
        return None
    candidate["key"] = key
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
    marker_before_block = _native_marker_before_block(
        page_markdown,
        sidecar,
        block_index,
        text,
        key=str(candidate["key"]),
    )
    candidate["same_page_superscript"] = marker_before_block
    candidate["auto_accept"] = bool(auto_accept)
    candidate["native_max_font_ratio"] = float(native_max_font_ratio)
    if (
        auto_accept
        and not region_evidence
        and marker_before_block
        and isinstance(font_ratio, (int, float))
        and font_ratio <= float(native_max_font_ratio)
    ):
        candidate["confidence"] = "high"
        candidate["disposition"] = "local_candidate"
        candidate["review_reason"] = "native_superscript_and_small_text"
    else:
        candidate["confidence"] = "review"
        candidate["disposition"] = "review_required"
    return candidate


def _native_marker_before_block(
    page_markdown: str,
    sidecar: Mapping[str, Any],
    block_index: int,
    block_text: str,
    *,
    key: str,
) -> bool:
    """Return whether a matching superscript occurs before this note block.

    Native extraction may itself wrap a small note number in ``<sup>`` because
    the PDF exposes a superscript flag.  Looking only for ``<sup>N</sup>``
    anywhere on the page would therefore mistake the definition's own number
    for a body reference.  Native page Markdown preserves block boundaries as
    blank-line-separated chunks, so locate the candidate block and only accept
    a marker in an earlier chunk.  New native sidecars also retain the exact
    ``markdown_text`` for each block; the text fallback keeps older sidecars
    reviewable rather than failing the whole stage.
    """
    page_markdown = str(page_markdown or "")
    if not page_markdown:
        return False
    key = str(key)
    marker_keys = {key}
    unicode_key = key.translate(str.maketrans("0123456789", "⁰¹²³⁴⁵⁶⁷⁸⁹"))
    if unicode_key != key:
        marker_keys.add(unicode_key)

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
        if marker_key is not None and marker_key == key:
            return True
        if match.group("unicode") in marker_keys:
            return True
    return False


def _write_unit_contexts(
    output_dir: Path,
    report: Mapping[str, Any],
) -> dict[str, str]:
    """Write sparse per-unit projections so workers do not load the book report."""
    page_reports = report.get("pages", [])
    cross_windows = report.get("cross_page_windows", [])
    unit_context_files: dict[str, str] = {}
    for unit in _load_refined_units(output_dir):
        names = [str(name) for name in (unit.get("part_files") or [unit.get("file")]) if name]
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            continue
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            continue
        pages = [
            item for item in page_reports
            if start_page <= int(item.get("page", -1)) <= end_page
        ]
        windows = [
            item for item in cross_windows
            if any(start_page <= int(page) <= end_page for page in item.get("pages", []))
        ]
        context = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "task": "footnote-prepare",
            "source_units": names,
            "page_range": [start_page, end_page],
            "pages": pages,
            "cross_page_windows": windows,
            "decisions": [
                item
                for item in report.get("decisions", [])
                if isinstance(item, Mapping)
                and start_page <= int(item.get("page", -1)) <= end_page
            ],
        }
        for name in names:
            context_path = _unit_context_path(output_dir, name)
            atomic_write_text(context_path, json.dumps(context, ensure_ascii=False, indent=2))
            unit_context_files[name] = context_path.relative_to(output_dir).as_posix()
    return unit_context_files


def prepare_footnote_candidates(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
    bottom_ratio: Optional[float] = None,
    bottom_intersection_ratio: Optional[float] = None,
    context_blocks: Optional[int] = None,
    auto_accept: Optional[bool] = None,
    native_max_font_ratio: Optional[float] = None,
) -> dict[str, Any]:
    """Create a compact, review-only footnote candidate report.

    No Markdown or translated file is changed.  High-confidence candidates
    are recorded for deterministic later materialization; only review windows
    are intended for a Subagent.  The function reads compact page-layout
    sidecars, not the full book text, which keeps the hand-off small. Visual
    OCR and native-text PDFs use source-specific candidate detectors but share
    the same decision contract.
    """
    output_dir = Path(output_dir)
    options = resolve_footnote_options(
        config,
        bottom_ratio=bottom_ratio,
        bottom_intersection_ratio=bottom_intersection_ratio,
        context_blocks=context_blocks,
        auto_accept=auto_accept,
        native_max_font_ratio=native_max_font_ratio,
    )
    pages_dir = output_dir / "pages"
    sidecars = sorted(pages_dir.glob("page_*.ocr.json"))
    if not sidecars:
        raise ValueError(f"No page layout sidecars found in {pages_dir}")

    native_text_source = is_native_text_source(output_dir)
    source_kind = "native_text" if native_text_source else "ocr"
    evidence_mode = pdf_evidence_mode(output_dir, config)
    # The CLI always supplies config.  The config-less path is retained for
    # old library callers/tests; only that compatibility path may consult a
    # legacy consensus file.  A real single-OCR run must ignore stale
    # two-OCR artifacts completely.
    legacy_consensus = config is None
    if evidence_mode == "two_ocr":
        require_current_consensus(output_dir, config, stage="footnote")

    page_reports: list[dict[str, Any]] = []
    sidecar_hashes: dict[str, str] = {}
    secondary_sidecar_hashes: dict[str, str] = {}
    consensus_review_pages = (
        _load_consensus_review_pages(output_dir)
        if not native_text_source and (legacy_consensus or evidence_mode == "two_ocr")
        else set()
    )
    review_pages: set[int] = set()
    suspected_missed_pages: set[int] = set()
    high_confidence_count = 0
    review_count = 0
    bottom_detection_totals = {
        "bottom_block_count": 0,
        "bottom_numeric_block_count": 0,
        "bottom_numeric_candidate_count": 0,
        "excluded_page_furniture_count": 0,
        "excluded_numeric_page_count": 0,
        "suspected_missed_count": 0,
    }

    for sidecar_path in sidecars:
        sidecar = _load_sidecar(sidecar_path)
        try:
            page_number = int(sidecar.get("page_number"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"OCR sidecar has invalid page_number: {sidecar_path}") from exc
        sidecar_hashes[sidecar_path.relative_to(output_dir).as_posix()] = sha256_file(sidecar_path)
        page_markdown_path = pages_dir / f"page_{page_number:03d}.md"
        try:
            page_markdown = page_markdown_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            page_markdown = ""
        candidates = []
        for block_index, block in enumerate(sidecar["blocks"]):
            if not isinstance(block, Mapping):
                continue
            if native_text_source:
                candidate = _candidate_for_native_block(
                    page_number,
                    block_index,
                    block,
                    sidecar,
                    bottom_ratio=options.bottom_ratio,
                    bottom_intersection_ratio=options.bottom_intersection_ratio,
                    page_markdown=page_markdown,
                    auto_accept=options.auto_accept,
                    native_max_font_ratio=options.native_max_font_ratio,
                )
            else:
                local_acceptance_disabled = not options.auto_accept
                candidate = _candidate_for_block(
                    page_number,
                    block_index,
                    block,
                    sidecar,
                    bottom_ratio=options.bottom_ratio,
                    bottom_intersection_ratio=options.bottom_intersection_ratio,
                    force_review=(
                        local_acceptance_disabled
                    ),
                    source="primary",
                )
                if candidate is not None and local_acceptance_disabled:
                    candidate["review_reason"] = "local_auto_accept_disabled"
            if candidate is None:
                continue
            candidates.append(candidate)

        candidates, continuous_regions = _expand_continuous_region_candidates(
            page_number,
            sidecar,
            candidates,
            bottom_ratio=options.bottom_ratio,
            bottom_intersection_ratio=options.bottom_intersection_ratio,
            native_text_source=native_text_source,
            page_markdown=page_markdown,
            auto_accept=options.auto_accept,
            native_max_font_ratio=options.native_max_font_ratio,
        )
        candidates.sort(key=lambda item: (item["order"], item["block"]))
        for candidate in candidates:
            if candidate["confidence"] == "high":
                high_confidence_count += 1
            else:
                review_count += 1
                review_pages.add(page_number)
        bottom_stats = _bottom_detection_stats(
            sidecar,
            {
                int(index)
                for candidate in candidates
                for index in _candidate_block_indexes(candidate)
            },
            bottom_ratio=options.bottom_ratio,
        )
        for field, value in bottom_stats.items():
            bottom_detection_totals[field] += int(value)
        if bottom_stats["suspected_missed_count"]:
            suspected_missed_pages.add(page_number)

        candidates.sort(key=lambda item: (item["order"], item["block"]))
        secondary_sidecar = _secondary_sidecar_path(output_dir, sidecar_path)
        secondary_candidates: list[dict[str, Any]] = []
        candidate_disagreement = False
        if evidence_mode == "two_ocr":
            secondary = _load_sidecar(secondary_sidecar)
            secondary_sidecar_hashes[
                secondary_sidecar.relative_to(output_dir).as_posix()
            ] = sha256_file(secondary_sidecar)
            for block_index, block in enumerate(secondary["blocks"]):
                if not isinstance(block, Mapping):
                    continue
                candidate = _candidate_for_block(
                    page_number,
                    block_index,
                    block,
                    secondary,
                    bottom_ratio=options.bottom_ratio,
                    bottom_intersection_ratio=options.bottom_intersection_ratio,
                    source="secondary",
                )
                if candidate is not None:
                    secondary_candidates.append(candidate)
            secondary_candidates, secondary_regions = _expand_continuous_region_candidates(
                page_number,
                secondary,
                secondary_candidates,
                bottom_ratio=options.bottom_ratio,
                bottom_intersection_ratio=options.bottom_intersection_ratio,
                native_text_source=False,
                page_markdown="",
                auto_accept=False,
                native_max_font_ratio=options.native_max_font_ratio,
            )
            secondary_candidates.sort(key=lambda item: (item["order"], item["block"]))
            matched_secondary_blocks = {
                int(secondary_candidate["block"])
                for secondary_candidate in secondary_candidates
                if any(
                    _candidates_match(primary_candidate, secondary_candidate)
                    for primary_candidate in candidates
                )
            }
            secondary_only_candidates = [
                candidate
                for candidate in secondary_candidates
                if int(candidate["block"]) not in matched_secondary_blocks
            ]
            secondary_only_candidates = _aggregate_secondary_only_candidates(
                secondary_only_candidates
            )
            candidate_disagreement = bool(secondary_only_candidates) or (
                bool(candidates) != bool(secondary_candidates)
            )
            if candidate_disagreement:
                review_pages.add(page_number)
                for candidate in candidates:
                    if candidate.get("confidence") == "high":
                        high_confidence_count -= 1
                        review_count += 1
                    candidate["confidence"] = "review"
                    candidate["disposition"] = "review_required"
                    candidate["review_reason"] = (
                        "ocr_candidate_presence_differs"
                        if bool(candidates) != bool(secondary_candidates)
                        else "ocr_footnote_candidate_differs"
                    )
            for candidate in secondary_only_candidates:
                candidate["confidence"] = "review"
                candidate["disposition"] = "review_required"
                candidate["secondary_only"] = True
                candidate["review_reason"] = "secondary_only_footnote_candidate"
                review_count += 1

        should_emit_page = bool(candidates) or (
            evidence_mode == "two_ocr"
            and bool(secondary_candidates)
        )
        if should_emit_page:
            legacy_secondary = legacy_consensus and secondary_sidecar.is_file()
            if candidate_disagreement and not candidates:
                # The secondary OCR found a possible note that the primary
                # OCR did not expose.  Keep the page in the review hand-off,
                # but do not manufacture a primary block address that the
                # deterministic apply stage could not safely remove.
                review_pages.add(page_number)
            page_reports.append(
                {
                    "page": page_number,
                    "sidecar": sidecar_path.relative_to(output_dir).as_posix(),
                    "secondary_sidecar": (
                        secondary_sidecar.relative_to(output_dir).as_posix()
                        if evidence_mode == "two_ocr" or legacy_secondary
                        else None
                    ),
                    "consensus_visual_review": page_number in consensus_review_pages,
                    "consensus_status": (
                        "disagree" if candidate_disagreement else "agree"
                    ) if evidence_mode == "two_ocr" else None,
                    "candidates": candidates,
                    "secondary_candidates": secondary_candidates,
                    "secondary_only_candidates": secondary_only_candidates
                    if evidence_mode == "two_ocr"
                    else [],
                    "continuous_numeric_regions": continuous_regions,
                    "secondary_continuous_numeric_regions": (
                        secondary_regions if evidence_mode == "two_ocr" else []
                    ),
                    "bottom_detection": bottom_stats,
                    "window": _window_for_page(
                        page_number,
                        sidecar,
                        candidates,
                        context_blocks=options.context_blocks,
                    ),
                    "secondary_window": _window_for_page(
                        page_number,
                        secondary,
                        secondary_candidates,
                        context_blocks=options.context_blocks,
                    ) if evidence_mode == "two_ocr" else None,
                }
            )

    page_reports.sort(key=lambda item: item["page"])
    cross_page_windows = _build_cross_page_windows(page_reports)
    for window in cross_page_windows:
        review_pages.update(int(page) for page in window["pages"])

    visual_evidence = _ensure_visual_review_images(
        output_dir,
        review_pages,
        config,
    )
    visual_files = visual_evidence.get("files", {})
    if isinstance(visual_files, Mapping):
        for page_report in page_reports:
            page_report["visual_file"] = visual_files.get(str(page_report["page"]))

    report = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_dir": "pages",
        "source_kind": source_kind,
        "ocr_evidence_mode": evidence_mode,
        "bottom_ratio": options.bottom_ratio,
        "bottom_intersection_ratio": options.bottom_intersection_ratio,
        "context_blocks": options.context_blocks,
        "auto_accept": options.auto_accept,
        "native_max_font_ratio": options.native_max_font_ratio,
        "consensus_visual_review_pages": sorted(consensus_review_pages),
        "sidecar_sha256": sidecar_hashes,
        "secondary_sidecar_sha256": secondary_sidecar_hashes,
        "pages": page_reports,
        "cross_page_windows": cross_page_windows,
        "review_pages": sorted(review_pages),
        "high_confidence_candidate_count": high_confidence_count,
        "review_candidate_count": review_count,
        "bottom_detection": bottom_detection_totals,
        "suspected_missed_count": bottom_detection_totals["suspected_missed_count"],
        "suspected_missed_pages": sorted(suspected_missed_pages),
        "visual_evidence": visual_evidence,
        "review_required": bool(review_pages),
    }
    report_path = output_dir / "footnote_candidates.json"
    atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report


def prepare_footnote_subagent(
    output_dir: Path,
    *,
    book_title: str,
    config: Optional[Mapping[str, Any]] = None,
    bottom_ratio: Optional[float] = None,
    bottom_intersection_ratio: Optional[float] = None,
    context_blocks: Optional[int] = None,
    auto_accept: Optional[bool] = None,
    native_max_font_ratio: Optional[float] = None,
) -> dict[str, Path]:
    """Write the compact Subagent hand-off for ambiguous layout windows."""
    output_dir = Path(output_dir)
    report = prepare_footnote_candidates(
        output_dir,
        config=config,
        bottom_ratio=bottom_ratio,
        bottom_intersection_ratio=bottom_intersection_ratio,
        context_blocks=context_blocks,
        auto_accept=auto_accept,
        native_max_font_ratio=native_max_font_ratio,
    )
    unit_context_files = _write_unit_contexts(output_dir, report)
    manifest = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "task": "footnote-prepare",
        "book_title": book_title,
        "source_kind": report["source_kind"],
        "ocr_evidence_mode": report["ocr_evidence_mode"],
        "candidate_report": "footnote_candidates.json",
        "review_pages": report["review_pages"],
        "high_confidence_candidate_count": report["high_confidence_candidate_count"],
        "review_candidate_count": report["review_candidate_count"],
        "bottom_detection": report["bottom_detection"],
        "suspected_missed_count": report["suspected_missed_count"],
        "suspected_missed_pages": report["suspected_missed_pages"],
        "visual_evidence": report["visual_evidence"],
        "auto_accept": report["auto_accept"],
        "bottom_intersection_ratio": report["bottom_intersection_ratio"],
        "native_max_font_ratio": report["native_max_font_ratio"],
        "cross_page_window_count": len(report["cross_page_windows"]),
        "consensus_visual_review_pages": report["consensus_visual_review_pages"],
        "unit_context_files": unit_context_files,
        "decision_file": "footnote_decisions.json",
        "status": "pending_review" if report["review_required"] else "no_subagent_review_required",
    }
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    atomic_write_text(manifest_path, json.dumps(manifest, ensure_ascii=False, indent=2))

    review_pages = report["review_pages"]
    page_reports = {
        int(item["page"]): item
        for item in report.get("pages", [])
        if isinstance(item, Mapping) and str(item.get("page", "")).isdigit()
    }
    page_lines = []
    for page in review_pages:
        page_report = page_reports.get(int(page), {})
        primary = f"`{page_report.get('sidecar', f'pages/page_{int(page):03d}.ocr.json')}`"
        secondary = page_report.get("secondary_sidecar")
        line = f"- primary: {primary}"
        if secondary:
            line += f"; secondary: `{secondary}`"
        if page_report.get("consensus_visual_review"):
            line += " (OCR consensus marked this page for visual review)"
        if page_report.get("consensus_status") == "disagree":
            line += " (primary/secondary footnote candidate evidence differs)"
        if page_report.get("visual_file"):
            line += f"; visual: `{page_report['visual_file']}`"
        page_lines.append(line)
    page_text = "\n".join(page_lines) or "- 无需 Subagent 复核；仅保留本地高置信度候选。"
    source_guidance = (
        "For native-text pages, use the recorded coordinates and font metadata as layout evidence. "
        "Candidates marked confidence=high have already passed the conservative local rule: an earlier "
        "same-page superscript reference matches the key and the note text is materially smaller than "
        "the body font. Do not create a second decision for those blocks. Review only confidence=review "
        "blocks, and distinguish them from citations, page furniture, and ordinary numbered prose."
        if report["source_kind"] == "native_text"
        else "If a page is marked as an OCR-consensus visual review, treat the local label as untrusted even when it says `Footnote`; compare the primary and secondary sidecars and the page image before deciding."
    )
    prompt = f"""# Footnote review\n\nBook: {book_title}\n\nRead `footnote_subagent_manifest.json` and `footnote_candidates.json`. The local script has selected compact bottom-of-page candidate windows. Review only blocks marked `confidence: review`; blocks marked `confidence: high` are already accepted by the deterministic local rule and do not need a decision. Do not reread or rewrite the whole book. {source_guidance}\n\nCandidate page sidecars:\n{page_text}\n\nUse these meanings strictly:\n- `footnote_start`: the beginning of a real page footnote; it will be moved to the end of its logical chapter.\n- `footnote_continuation`: text continuing a real footnote from an earlier page; it will be joined to that footnote.\n- `footnote_definition`: a complete footnote definition already presented as a note block.\n- `citation`: an in-text citation, quoted source, parenthetical/numeric reference, or other scholarly reference; it must stay in the body and must never be moved.\n- `bibliography`: a reference-list/bibliography entry; it must stay in place and must never be moved.\n- `body`: ordinary prose or an uncertain block that is not a footnote.\n\nFor each candidate block marked `confidence: review`, assign exactly one role. Keep the block's page, source, and block number. `source` defaults to `primary`; use `source: "secondary"` only for a candidate listed under `secondary_only_candidates`. For `footnote_start` and `footnote_definition`, copy the visible numeric key when present. For `footnote_continuation`, provide the key of the footnote it continues. A continuation may appear after ordinary body blocks on the next page; preserve visual order and attach it only when the page image supports that decision. Do not assume that a next-page footnote continuation is at the top of the page. A numeric marker alone is not enough to call something a footnote: if it is a citation or reference, use `citation` or `bibliography`.\n\nWrite only valid JSON to `footnote_decisions.json` with this shape:\n\n```json\n{{\n  "schema_version": {FOOTNOTE_PREPARE_SCHEMA_VERSION},\n  "decisions": [\n    {{\n      "page": 125,\n      "source": "primary",\n      "block": 8,\n      "role": "footnote_continuation",\n      "key": "36",\n      "confidence": "high"\n    }}\n  ]\n}}\n```\n\nIf a candidate is genuinely ambiguous, use `role: "review_required"` and explain it in `reason`; do not guess.\n"""
    prompt = prompt.replace(
        "Candidate page sidecars:",
        "When a `visual:` path is present, open that matching page PNG and use it as the visual authority. "
        "Do not infer a footnote from the label alone. The report's "
        "`bottom_detection.suspected_missed_count` is a recall warning: inspect those pages' lower region "
        "even when no candidate was emitted.\n\nCandidate page sidecars and visual evidence:",
    )
    prompt += (
        "\n\nFor every secondary-only moved decision, also include "
        "`primary_disposition`: `absent` when the Chandra/refinement text truly "
        "does not contain this note, or `remove` when it contains a duplicate "
        "primary block. For `remove`, include `primary_blocks` (or one integer "
        "`primary_block`) so the local stage can delete only the reviewed block. "
        "If the body contains more than one possible marker with this key, include "
        "`marker_context` with a short exact surrounding phrase; the local stage "
        "will refuse to guess between repeated markers.\n\n"
        "Do not reread or rewrite the whole book.\n"
    )
    prompt_path = output_dir / "footnote_subagent_prompt.md"
    atomic_write_text(prompt_path, prompt)
    return {
        "report": output_dir / "footnote_candidates.json",
        "manifest": manifest_path,
        "prompt": prompt_path,
    }


def _candidate_index(
    report: Mapping[str, Any],
) -> dict[tuple[int, str, int], dict[str, Any]]:
    index = {}
    for page in report.get("pages", []) or []:
        if not isinstance(page, Mapping):
            continue
        for field, default_source in (
            ("candidates", "primary"),
            ("secondary_only_candidates", "secondary"),
        ):
            for candidate in page.get(field, []) or []:
                if not isinstance(candidate, Mapping):
                    continue
                try:
                    address = (
                        int(candidate["page"]),
                        str(candidate.get("source") or default_source).strip().lower(),
                        int(candidate["block"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                index[address] = dict(candidate)
    return index


def validate_footnote_decisions(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate the small JSON decision file produced by the Subagent.

    This validator checks references to candidate block addresses and basic
    continuation provenance.  It never infers a missing decision and never
    turns an unresolved visual case into an automatic acceptance.
    """
    output_dir = Path(output_dir)
    report_path = output_dir / "footnote_candidates.json"
    decision_path = output_dir / "footnote_decisions.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"valid": False, "status": "retry_required", "errors": [f"invalid candidate report: {exc}"]}
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result
    if not isinstance(report, dict):
        result = {"valid": False, "status": "retry_required", "errors": ["candidate report must be an object"]}
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    source_kind = str(report.get("source_kind") or "ocr")
    evidence_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    errors: list[str] = []
    if source_kind not in {"ocr", "native_text"}:
        errors.append("candidate report has an unsupported page source kind")
    if evidence_mode not in {"single_ocr", "two_ocr"}:
        errors.append("candidate report has an unsupported OCR evidence mode")
    if config is not None:
        configured_mode = pdf_evidence_mode(output_dir, config)
        if evidence_mode != configured_mode:
            errors.append(
                f"candidate report was prepared in {evidence_mode}, but configuration requires {configured_mode}"
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                errors.append(str(exc))

    def _hashes_current(field: str) -> bool:
        expected_hashes = report.get(field, {})
        if not isinstance(expected_hashes, Mapping):
            errors.append(f"candidate report field {field} must be an object")
            return False
        current_hashes: dict[str, str] = {}
        for relative, expected in expected_hashes.items():
            path = output_dir / str(relative)
            if path.is_file():
                current_hashes[str(relative)] = sha256_file(path)
        if current_hashes != dict(expected_hashes):
            errors.append(f"one or more {field} files changed or disappeared")
            return False
        return True

    _hashes_current("sidecar_sha256")
    secondary_hashes = report.get("secondary_sidecar_sha256", {})
    if evidence_mode == "two_ocr":
        _hashes_current("secondary_sidecar_sha256")
    elif secondary_hashes:
        errors.append("single-OCR candidate report unexpectedly contains secondary OCR files")
    if errors:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "valid": False,
            "status": "retry_required",
            "errors": errors,
            "decisions": [],
        }
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result

    required = {
        (
            int(candidate["page"]),
            "primary",
            int(candidate["block"]),
        )
        for page in report.get("pages", []) or []
        for candidate in page.get("candidates", []) or []
        if candidate.get("confidence") == "review"
    }
    required.update(
        (
            int(candidate["page"]),
            "secondary",
            int(candidate["block"]),
        )
        for page in report.get("pages", []) or []
        for candidate in page.get("secondary_only_candidates", []) or []
        if candidate.get("confidence") == "review"
    )
    if not required:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "valid": True,
            "status": "no_subagent_review_required",
            "errors": [],
            "decisions": [],
        }
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        report["decisions"] = []
        report["decisions_status"] = "validated"
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        _write_unit_contexts(output_dir, report)
        return result

    try:
        decisions_data = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"valid": False, "status": "retry_required", "errors": [f"invalid decision file: {exc}"]}
        atomic_write_text(
            output_dir / "footnote_decision_validation.json",
            json.dumps(result, ensure_ascii=False, indent=2),
        )
        return result
    decisions = decisions_data.get("decisions") if isinstance(decisions_data, dict) else None
    human_review_required: list[str] = []
    if not isinstance(decisions_data, dict) or decisions_data.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        errors.append("decision file has an unsupported schema_version")
    if not isinstance(decisions, list):
        errors.append("decision file must contain a decisions array")
        decisions = []

    candidates = _candidate_index(report)
    seen: set[tuple[int, str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, Mapping):
            errors.append("each decision must be an object")
            continue
        try:
            source = str(item.get("source") or "primary").strip().lower()
            address = (int(item["page"]), source, int(item["block"]))
        except (KeyError, TypeError, ValueError):
            errors.append("decision is missing an integer page/block address")
            continue
        if source not in {"primary", "secondary"}:
            errors.append(
                f"unsupported decision source for {address[0]}:{address[2]}: {source}"
            )
            continue
        if address in seen:
            errors.append(
                f"duplicate decision for {address[2]}:{address[0]}:{address[1]}"
            )
            continue
        seen.add(address)
        candidate = candidates.get(address)
        if candidate is None:
            errors.append(
                "decision points to a non-candidate block: "
                f"{address[0]}:{address[1]}:{address[2]}"
            )
            continue
        role = str(item.get("role") or "").strip()
        if role not in _DECISION_ROLES:
            errors.append(
                f"unsupported decision role for {address[0]}:{address[1]}:{address[2]}: {role}"
            )
            continue
        key = item.get("key")
        if key is not None:
            key = str(key).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                errors.append(
                    f"invalid footnote key for {address[0]}:{address[1]}:{address[2]}"
                )
                continue
        if role in {"footnote_start", "footnote_continuation", "footnote_definition"} and not key:
            errors.append(
                f"footnote decision is missing a key for {address[0]}:{address[1]}:{address[2]}"
            )
            continue
        if candidate.get("key") and role in {"footnote_start", "footnote_definition"} and key != candidate["key"]:
            errors.append(
                f"decision key does not match OCR key for {address[0]}:{address[1]}:{address[2]}"
            )
            continue
        if source == "secondary" and role in _MOVED_ROLES:
            text = str(item.get("text") or "").strip()
            if not text:
                errors.append(
                    "secondary-only footnote decision must include explicit text for "
                    f"{address[0]}:{address[2]}"
                )
                continue
            source_file = str(item.get("source_file") or "").strip()
            if not source_file or Path(source_file).name != source_file or not source_file.endswith(".md"):
                errors.append(
                    "secondary-only footnote decision must include a safe source_file for "
                    f"{address[0]}:{address[2]}"
                )
                continue
            primary_disposition = str(
                item.get("primary_disposition") or ""
            ).strip().lower()
            if primary_disposition not in {"absent", "remove"}:
                errors.append(
                    "secondary-only footnote decision must declare primary_disposition "
                    f"as absent or remove for {address[0]}:{address[2]}"
                )
                continue
            if primary_disposition == "remove":
                raw_primary_blocks = item.get("primary_blocks")
                if raw_primary_blocks is None:
                    raw_primary_block = item.get("primary_block")
                    raw_primary_blocks = (
                        [raw_primary_block] if raw_primary_block is not None else None
                    )
                if not isinstance(raw_primary_blocks, list) or not raw_primary_blocks:
                    errors.append(
                        "secondary-only footnote with primary_disposition=remove must "
                        f"include primary_block(s) for {address[0]}:{address[2]}"
                    )
                    continue
                normalized_primary_blocks: list[int] = []
                invalid_primary_block = False
                for raw_block in raw_primary_blocks:
                    try:
                        primary_block = int(raw_block)
                    except (TypeError, ValueError):
                        invalid_primary_block = True
                        break
                    if primary_block < 0 or primary_block in normalized_primary_blocks:
                        invalid_primary_block = True
                        break
                    normalized_primary_blocks.append(primary_block)
                if invalid_primary_block:
                    errors.append(
                        "secondary-only footnote primary_block(s) must be unique "
                        f"non-negative integers for {address[0]}:{address[2]}"
                    )
                    continue
            else:
                normalized_primary_blocks = []
                if item.get("primary_block") is not None or item.get("primary_blocks") is not None:
                    errors.append(
                        "secondary-only footnote with primary_disposition=absent must not "
                        f"include primary_block(s) for {address[0]}:{address[2]}"
                    )
                    continue
        decision = dict(item)
        decision["page"], decision["source"], decision["block"], decision["role"] = (
            address[0],
            address[1],
            address[2],
            role,
        )
        if key is not None:
            decision["key"] = key
        if source == "secondary" and role in _MOVED_ROLES:
            decision["primary_disposition"] = primary_disposition
            decision["primary_blocks"] = normalized_primary_blocks
        if role == "review_required":
            human_review_required.append(f"{address[0]}:{address[1]}:{address[2]}")
        normalized.append(decision)

    missing = sorted(required - seen)
    errors.extend(
        f"missing decision for review candidate {page}:{source}:{block}"
        for page, source, block in missing
    )

    key_sources = {
        str(candidate.get("key"))
        for candidate in candidates.values()
        if candidate.get("key")
    }
    key_sources.update(
        str(item.get("key"))
        for item in normalized
        if item.get("role") in {"footnote_start", "footnote_definition"} and item.get("key")
    )
    for item in normalized:
        if item.get("role") != "footnote_continuation" or not item.get("key"):
            continue
        if str(item["key"]) not in key_sources:
            errors.append(
                f"continuation {item['page']}:{item['block']} has no traceable footnote key"
            )

    if human_review_required:
        status = "human_review_required"
    elif errors:
        status = "retry_required"
    else:
        status = "validated"
    result = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_kind": source_kind,
        "ocr_evidence_mode": evidence_mode,
        "valid": not errors and not human_review_required,
        "status": status,
        "errors": errors,
        "human_review_required": human_review_required,
        "decisions": normalized,
    }
    atomic_write_text(
        output_dir / "footnote_decision_validation.json",
        json.dumps(result, ensure_ascii=False, indent=2),
    )
    if result["valid"]:
        report["decisions"] = normalized
        report["decisions_status"] = "validated"
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
        _write_unit_contexts(output_dir, report)
    return result


def load_footnote_unit_contexts(output_dir: Path) -> dict[str, Path]:
    """Load current sparse context projections for Markdown hand-offs."""
    output_dir = Path(output_dir)
    manifest_path = output_dir / "footnote_subagent_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    values = manifest.get("unit_context_files", {}) if isinstance(manifest, dict) else {}
    if not isinstance(values, Mapping):
        return {}
    result: dict[str, Path] = {}
    root = output_dir.resolve()
    for name, relative in values.items():
        if not isinstance(name, str) or not isinstance(relative, str):
            continue
        path = (output_dir / relative).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            continue
        if path.is_file():
            result[name] = path
    return result


__all__ = [
    "DEFAULT_BOTTOM_RATIO",
    "FootnotePrepareOptions",
    "FOOTNOTE_PREPARE_SCHEMA_VERSION",
    "load_footnote_unit_contexts",
    "prepare_footnote_candidates",
    "prepare_footnote_subagent",
    "resolve_footnote_options",
    "validate_footnote_decisions",
]

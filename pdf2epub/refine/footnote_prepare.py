"""Prepare a compact footnote review hand-off.

The OCR backends already persist page layout sidecars next to the Markdown
view.  This module uses those sidecars to do the cheap part locally: identify
bottom-of-page footnote candidates, preserve their *within-page* order, and
create small review windows for only the ambiguous cases.  It may group a
consecutive numeric bottom region and include adjacent unnumbered blocks as
evidence. A strongly supported cross-page continuation can be linked locally to
the previous page's last physical footnote; ambiguous boundaries remain a
visual/semantic judgment reserved for the workspace Subagent.
"""

from __future__ import annotations

import html
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
DEFAULT_REGION_MAX_GAP = 0.035
DEFAULT_REGION_BOUNDARY_MARGIN = 0.12
DEFAULT_RECALL_AUDIT_EDGE_MARGIN = 0.04
DEFAULT_CONTINUATION_HIGH_SCORE = 9
DEFAULT_CONTINUATION_MEDIUM_SCORE = 5
DEFAULT_SEMANTIC_MIN_SIMILARITY = 0.08
DEFAULT_SEMANTIC_WIN_MARGIN = 0.035
FOOTNOTE_MODES = frozenset({"footnotes", "endnotes_only", "disabled"})
FOOTNOTE_STRUCTURE_EXEMPT_TYPES = frozenset(
    {
        "notes",
        "bibliography",
        "index",
        "author_notes",
        "catalog",
        "ad",
    }
)
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
_SEMANTIC_TOKEN_RE = re.compile(
    r"[^\W\d_][\w’'’\-]{2,}",
    re.UNICODE,
)
_SEMANTIC_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing down
    during each few for from further had has have having he her here hers herself
    him himself his how i if in into is it its itself just me more most my myself
    no nor not now of off on once one only or other our ours ourselves out over own
    same she should so some such than that the their theirs them themselves then
    there these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your yours yourself
    yourselves
    book books chapter chapters section sections page pages work works text texts
    writing writings author authors note notes footnote footnotes reference
    references philosophy philosophical phenomenology hegel spirit consciousness
    argument arguments claim claims says say said write writes writing
    """.split()
)


@dataclass(frozen=True)
class FootnotePrepareOptions:
    """Validated settings shared by candidate and handoff preparation."""

    mode: str = "footnotes"
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
    mode: Optional[str] = None,
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

    resolved_mode = str(
        mode if mode is not None else footnote_config.get("mode", "footnotes")
    ).strip().lower()
    if resolved_mode not in FOOTNOTE_MODES:
        allowed = ", ".join(sorted(FOOTNOTE_MODES))
        raise ValueError(f"footnotes.mode must be one of: {allowed}")

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
        mode=resolved_mode,
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
    previous_page_footnote_evidence: bool = False,
    excluded_block_indexes: Optional[set[int]] = None,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Add review-only blocks surrounding a consecutive numeric note run."""
    regions = _continuous_numeric_regions(
        sidecar,
        bottom_ratio=bottom_ratio,
    )
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
            _FOOTNOTE_LABEL_RE.search(
                str(blocks[index].get("label") or "")
            )
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
            # A different/duplicate numeric start is a region boundary.  The
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
                        candidate["continuous_region_has_footnote_label"] = (
                            numeric_blocks_have_footnote_label
                        )
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
    expanded.sort(key=lambda item: (item.get("order", item.get("block", 0)), item["block"]))
    return expanded, normalized_regions


def _has_footnote_evidence(candidates: Iterable[Mapping[str, Any]]) -> bool:
    """Return whether a page has a candidate that can continue onto the next."""
    return any(
        bool(candidate.get("key"))
        or bool(_FOOTNOTE_LABEL_RE.search(str(candidate.get("label") or "")))
        for candidate in candidates
        if isinstance(candidate, Mapping)
    )


def _build_footnote_page_context(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep prior-page note evidence, with the physical tail as the target.

    A page can contain several footnotes, but only the last one in physical
    reading order can continue onto the following page without an explicit
    repeated key.  The other notes remain in the context for audit/debugging;
    they must not compete as continuation targets.
    """
    blocks = sidecar.get("blocks", [])
    notes: list[dict[str, Any]] = []
    for candidate in sorted(
        (item for item in candidates if isinstance(item, Mapping)),
        key=lambda item: (item.get("order", item.get("block", 0)), item.get("block", 0)),
    ):
        label = str(candidate.get("label") or "").strip()
        if _FURNITURE_LABEL_RE.search(label) or _CITATION_LABEL_RE.search(label):
            continue
        if not candidate.get("key") and not _FOOTNOTE_LABEL_RE.search(label):
            continue
        try:
            block_index = int(candidate.get("block"))
        except (TypeError, ValueError):
            continue
        block = blocks[block_index] if 0 <= block_index < len(blocks) else None
        if not isinstance(block, Mapping):
            continue
        notes.append(
            {
                "page": int(page_number),
                "block": block_index,
                "key": candidate.get("key"),
                "label": label,
                "text": _text_from_block(block)[:500],
                "bbox": _normalise_bbox(block, sidecar),
                "font_size": block.get("font_size"),
                "font_names": block.get("font_names"),
            }
        )
    return {
        "page": int(page_number),
        "notes": notes,
        # Keep both names explicit in the evidence contract.  ``notes`` is
        # retained for compatibility with existing reports and callers.
        "all_footnotes_for_audit": notes,
        "last_physical_footnote": notes[-1] if notes else None,
    }


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _font_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return {str(item).casefold() for item in value if str(item).strip()}
    return set()


def _text_looks_unfinished(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"(?:[-–—,:;([{]|\.\.\.)$", value):
        return True
    return not bool(re.search(r"[.!?。！？…][\"'”’)\]]*$", value))


def _text_starts_as_continuation(text: str) -> bool:
    value = str(text or "").lstrip()
    if not value:
        return False
    return value[0].islower() or value[0] in ",.;:)]}%\"'”’"


def _semantic_tokens(text: str) -> set[str]:
    """Return content words for a small, dependency-free semantic proxy.

    This is deliberately not presented as natural-language understanding.  It
    only supplies a conservative tie-breaker between two local layout
    hypotheses.  Removing common book/academic words makes a shared subject
    term more useful than the fact that both a body paragraph and a footnote
    mention ``Hegel`` or ``philosophy``.
    """
    value = str(text or "").casefold().replace("’", "'")
    return {
        token
        for token in _SEMANTIC_TOKEN_RE.findall(value)
        if token not in _SEMANTIC_STOPWORDS
    }


def _semantic_similarity(first: str, second: str) -> tuple[float, list[str]]:
    """Return a bounded lexical-semantic proxy and shared content words."""
    first_tokens = _semantic_tokens(first)
    second_tokens = _semantic_tokens(second)
    if not first_tokens or not second_tokens:
        return 0.0, []
    shared = sorted(first_tokens & second_tokens)
    denominator = (len(first_tokens) * len(second_tokens)) ** 0.5
    if denominator <= 0:
        return 0.0, shared
    return round(len(shared) / denominator, 3), shared[:12]


def _block_has_inline_superscript(block: Mapping[str, Any]) -> bool:
    """Detect a body footnote marker retained in the source HTML."""
    raw = block.get("html")
    if raw is None:
        raw = block.get("text", "")
    return bool(re.search(r"<sup\b", str(raw or ""), re.IGNORECASE))


def _previous_body_block(
    sidecar: Mapping[str, Any],
    block_index: int,
) -> Optional[dict[str, Any]]:
    """Find the nearest earlier body block on the current page.

    The block immediately before a suspected continuation is often the real
    body paragraph in the layout ``body -> possible continuation -> notes``.
    Headers, footers, citations, and already-labelled footnotes are not useful
    competitors and are skipped.
    """
    blocks = sidecar.get("blocks", [])
    for index in range(int(block_index) - 1, -1, -1):
        block = blocks[index] if index < len(blocks) else None
        if not isinstance(block, Mapping):
            continue
        text = _text_from_block(block)
        label = str(block.get("label") or "").strip()
        if not text:
            continue
        if (
            _FURNITURE_LABEL_RE.search(label)
            or _FOOTNOTE_LABEL_RE.search(label)
            or _CITATION_LABEL_RE.search(label)
        ):
            continue
        return {
            "page": sidecar.get("page_number"),
            "block": index,
            "label": label,
            "text": text[:500],
            "bbox": _normalise_bbox(block, sidecar),
            "font_size": block.get("font_size"),
            "font_names": block.get("font_names"),
            "looks_unfinished": _text_looks_unfinished(text),
        }
    return None


def _compare_continuation_hypotheses(
    sidecar: Mapping[str, Any],
    block_index: int,
    previous_note: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a possible continuation with the two local alternatives.

    ``footnote`` means the candidate is more related to the previous page's
    final note; ``body`` means it is more related to the preceding body block
    on the current page.  A small margin or no body context is intentionally
    inconclusive and must not suppress visual review.
    """
    blocks = sidecar.get("blocks", [])
    current = blocks[block_index] if 0 <= block_index < len(blocks) else None
    if not isinstance(current, Mapping):
        return {
            "winner": "inconclusive",
            "confidence": "low",
            "body_similarity": 0.0,
            "footnote_similarity": 0.0,
            "margin": 0.0,
            "shared_body_terms": [],
            "shared_footnote_terms": [],
            "previous_body": None,
            "inline_superscript": False,
            "signals": [],
        }

    current_text = _text_from_block(current)
    previous_body = _previous_body_block(sidecar, block_index)
    body_similarity, shared_body_terms = _semantic_similarity(
        current_text,
        str(previous_body.get("text") or "") if previous_body else "",
    )
    footnote_similarity, shared_footnote_terms = _semantic_similarity(
        current_text,
        str(previous_note.get("text") or ""),
    )
    margin = round(abs(body_similarity - footnote_similarity), 3)
    inline_superscript = _block_has_inline_superscript(current)
    signals: list[str] = []

    # A superscript marker inside an unnumbered Text block is a strong local
    # indication that the block is ordinary body prose.  It is not a footnote
    # definition's leading number and therefore should not be treated as a
    # continuation merely because the previous note was unfinished.
    if inline_superscript and not _FOOTNOTE_LABEL_RE.search(
        str(current.get("label") or "")
    ):
        signals.append("inline_body_superscript")
        return {
            "winner": "body",
            "confidence": "high",
            "body_similarity": body_similarity,
            "footnote_similarity": footnote_similarity,
            "margin": margin,
            "shared_body_terms": shared_body_terms,
            "shared_footnote_terms": shared_footnote_terms,
            "previous_body": previous_body,
            "inline_superscript": True,
            "signals": signals,
        }

    if previous_body and previous_body.get("looks_unfinished") and _text_starts_as_continuation(current_text):
        signals.append("current_body_context_unfinished")
        body_similarity += 0.04
        margin = round(abs(body_similarity - footnote_similarity), 3)

    body_wins = (
        previous_body is not None
        and body_similarity >= DEFAULT_SEMANTIC_MIN_SIMILARITY
        and body_similarity - footnote_similarity >= DEFAULT_SEMANTIC_WIN_MARGIN
    )
    footnote_wins = (
        footnote_similarity >= DEFAULT_SEMANTIC_MIN_SIMILARITY
        and footnote_similarity - body_similarity >= DEFAULT_SEMANTIC_WIN_MARGIN
    )
    if body_wins:
        winner = "body"
        confidence = "medium"
        signals.append("body_context_semantically_closer")
    elif footnote_wins:
        winner = "footnote"
        confidence = "medium"
        signals.append("previous_footnote_semantically_closer")
    else:
        winner = "inconclusive"
        confidence = "low"

    return {
        "winner": winner,
        "confidence": confidence,
        "body_similarity": round(body_similarity, 3),
        "footnote_similarity": footnote_similarity,
        "margin": margin,
        "shared_body_terms": shared_body_terms,
        "shared_footnote_terms": shared_footnote_terms,
        "previous_body": previous_body,
        "inline_superscript": inline_superscript,
        "signals": signals,
    }


def _score_cross_page_continuation(
    sidecar: Mapping[str, Any],
    block_index: int,
    first_numeric_block: int,
    previous_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Score a possible continuation against the previous page's physical tail.

    The preceding page may contain one or many notes.  A continuation can only
    naturally belong to the last physical note, so earlier notes are retained
    as audit context but are never selected by this scorer.
    """
    blocks = sidecar.get("blocks", [])
    current = blocks[block_index] if 0 <= block_index < len(blocks) else None
    previous_notes = [
        note for note in previous_context.get("notes", []) if isinstance(note, Mapping)
    ]
    previous = previous_context.get("last_physical_footnote")
    if not isinstance(previous, Mapping) and previous_notes:
        # Compatibility with callers that construct the old compact context.
        previous = previous_notes[-1]
    if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
        return {
            "score": 0,
            "confidence": "low",
            "previous_page": previous_context.get("page"),
            "previous_keys": [],
            "matched_previous_key": None,
            "matched_previous_block": None,
            "last_physical_key": None,
            "last_physical_block": None,
            "positive_signals": [],
            "negative_signals": [],
            "semantic_comparison": {
                "winner": "inconclusive",
                "confidence": "low",
                "body_similarity": 0.0,
                "footnote_similarity": 0.0,
                "margin": 0.0,
                "shared_body_terms": [],
                "shared_footnote_terms": [],
                "previous_body": None,
                "inline_superscript": False,
                "signals": [],
            },
        }

    previous_keys = [
        str(note.get("key"))
        for note in previous_notes
        if note.get("key") is not None
    ]
    current_text = _text_from_block(current)
    current_label = str(current.get("label") or "").strip()
    current_bbox = _normalise_bbox(current, sidecar)
    current_font_size = _safe_float(current.get("font_size"))
    body_font_size = _safe_float(sidecar.get("body_font_size"))
    score = 3
    positive: list[str] = [
        "previous_page_footnote_evidence",
        "last_physical_footnote_target",
    ]
    negative: list[str] = []
    previous_bbox = previous.get("bbox")
    previous_text = str(previous.get("text") or "")
    semantic_comparison = _compare_continuation_hypotheses(
        sidecar,
        block_index,
        previous,
    )

    if len(previous_notes) > 1:
        # This is context, not a penalty.  The existence of earlier notes says
        # nothing about whether the physical tail did or did not continue.
        positive.append("previous_page_has_multiple_footnotes")
    if _FOOTNOTE_LABEL_RE.search(current_label):
        score += 2
        positive.append("current_footnote_label")
    if _FOOTNOTE_LABEL_RE.search(str(previous.get("label") or "")):
        score += 1
        positive.append("previous_footnote_label")

    if current_bbox and previous_bbox:
        x_delta = abs(float(current_bbox[0]) - float(previous_bbox[0]))
        width_delta = abs(
            (float(current_bbox[2]) - float(current_bbox[0]))
            - (float(previous_bbox[2]) - float(previous_bbox[0]))
        )
        if x_delta <= 0.03:
            score += 2
            positive.append("left_edge_aligned")
        elif x_delta <= 0.08:
            score += 1
            positive.append("left_edge_near")
        if width_delta <= 0.08:
            score += 1
            positive.append("line_width_similar")

    previous_font_size = _safe_float(previous.get("font_size"))
    if current_font_size and previous_font_size:
        font_delta = abs(current_font_size - previous_font_size) / max(
            current_font_size, previous_font_size
        )
        if font_delta <= 0.10:
            score += 2
            positive.append("font_size_matches_previous_note")
        elif font_delta <= 0.20:
            score += 1
            positive.append("font_size_near_previous_note")
    if _font_names(current.get("font_names")) & _font_names(previous.get("font_names")):
        score += 1
        positive.append("font_family_matches_previous_note")
    if current_font_size and body_font_size:
        body_ratio = current_font_size / body_font_size
        if body_ratio <= 0.90:
            score += 1
            positive.append("smaller_than_current_body")
        elif body_ratio >= 0.98:
            score -= 1
            negative.append("same_size_as_current_body")

    if _text_looks_unfinished(previous_text):
        score += 2
        positive.append("previous_text_looks_unfinished")
    elif previous_text:
        # A visibly closed last note is a meaningful counter-signal.  It does
        # not prove that no continuation exists (OCR and typography can be
        # imperfect), so it only prevents local auto-linking; visual review
        # can still accept the continuation.
        score -= 4
        negative.append("previous_text_looks_complete")
    if _text_starts_as_continuation(current_text):
        score += 1
        positive.append("current_text_starts_as_continuation")
    semantic_winner = semantic_comparison.get("winner")
    if semantic_winner == "body":
        # The body hypothesis is allowed to overrule a strong-looking
        # unfinished-note score only when it has a concrete local reason:
        # an inline body marker or a meaningful lexical advantage.
        score -= 6 if semantic_comparison.get("confidence") == "high" else 4
        negative.extend(
            str(signal) for signal in semantic_comparison.get("signals", [])
        )
    elif semantic_winner == "footnote":
        score += 2
        positive.extend(
            str(signal) for signal in semantic_comparison.get("signals", [])
        )
    if block_index + 1 == first_numeric_block:
        score += 1
        positive.append("immediately_before_new_number")
        first_numeric = (
            blocks[first_numeric_block]
            if 0 <= first_numeric_block < len(blocks)
            else None
        )
        first_numeric_bbox = (
            _normalise_bbox(first_numeric, sidecar)
            if isinstance(first_numeric, Mapping)
            else None
        )
        if current_bbox and first_numeric_bbox:
            gap = float(first_numeric_bbox[1]) - float(current_bbox[3])
            if 0.0 <= gap <= DEFAULT_REGION_MAX_GAP:
                score += 1
                positive.append("small_gap_before_new_number")
    if _FURNITURE_LABEL_RE.search(current_label) or _CITATION_LABEL_RE.search(current_label):
        score -= 5
        negative.append("current_block_is_furniture_or_citation")

    return {
        "score": score,
        "confidence": (
            "high"
            if score >= DEFAULT_CONTINUATION_HIGH_SCORE
            else "medium"
            if score >= DEFAULT_CONTINUATION_MEDIUM_SCORE
            else "low"
        ),
        "previous_page": previous_context.get("page"),
        "previous_keys": sorted(set(previous_keys)),
        "matched_previous_key": previous.get("key"),
        "matched_previous_block": previous.get("block"),
        "last_physical_key": previous.get("key"),
        "last_physical_block": previous.get("block"),
        "positive_signals": positive,
        "negative_signals": negative,
        "semantic_comparison": semantic_comparison,
    }


def _annotate_cross_page_continuations(
    sidecar: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    continuous_regions: Iterable[Mapping[str, Any]],
    previous_context: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach continuation scores to backtracked candidates."""
    summary = {
        "mode": "gated",
        "candidate_count": 0,
        "high_confidence_count": 0,
        "medium_confidence_count": 0,
        "low_confidence_count": 0,
        "semantic_body_count": 0,
        "semantic_footnote_count": 0,
        "semantic_inconclusive_count": 0,
    }
    if not previous_context or not previous_context.get("notes"):
        return summary
    by_index = {
        int(candidate["block"]): candidate
        for candidate in candidates
        if str(candidate.get("block", "")).lstrip("-").isdigit()
    }
    for region in continuous_regions:
        first_numeric_block = int(region.get("first_block", -1))
        for raw_index in region.get("backtracked_block_indices", []) or []:
            block_index = int(raw_index)
            evidence = _score_cross_page_continuation(
                sidecar,
                block_index,
                first_numeric_block,
                previous_context,
            )
            candidate = by_index.get(block_index)
            if candidate is not None:
                candidate["continuation_evidence"] = evidence
            summary["candidate_count"] += 1
            bucket = f"{evidence['confidence']}_confidence_count"
            summary[bucket] += 1
            semantic = evidence.get("semantic_comparison", {})
            winner = semantic.get("winner") if isinstance(semantic, Mapping) else None
            if winner in {"body", "footnote", "inconclusive"}:
                summary[f"semantic_{winner}_count"] += 1
    return summary


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
    # not safe to auto-link it.  Keep that case in the visual audit queue.
    if "previous_text_looks_complete" in evidence.get("negative_signals", []):
        return None
    return matched_key


def _suppress_soft_continuation_candidates(
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep body-like backtracks as context instead of review decisions.

    Numeric runs often sit immediately below the final body paragraph.  OCR
    labels that paragraph as ``Text`` and the old expansion treated it as an
    unresolved continuation, forcing a whole-page visual audit.  Suppress
    only the weak form of that signal: an unnumbered ordinary-text block with
    no cross-page evidence, or a low-confidence block before a region that
    already contains explicit ``Footnote`` labels.  A local semantic
    comparison may also suppress a plain ``Text`` block when an inline body
    marker or a clear body-context advantage defeats the continuation
    hypothesis.  Explicit Footnote labels and medium/high continuation
    evidence remain reviewable.
    """
    active: list[dict[str, Any]] = []
    context_only: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        if candidate.get("key") or not candidate.get("continuous_region_evidence"):
            active.append(candidate)
            continue

        label = str(candidate.get("label") or "").strip()
        if _FOOTNOTE_LABEL_RE.search(label):
            active.append(candidate)
            continue

        evidence = candidate.get("continuation_evidence")
        confidence = evidence.get("confidence") if isinstance(evidence, Mapping) else None
        semantic = (
            evidence.get("semantic_comparison")
            if isinstance(evidence, Mapping)
            else None
        )
        has_explicit_note_labels = bool(
            candidate.get("continuous_region_has_footnote_label")
        )
        suppress_reason: Optional[str] = None
        if (
            isinstance(semantic, Mapping)
            and semantic.get("winner") == "body"
            and semantic.get("confidence") in {"medium", "high"}
        ):
            suppress_reason = "body_context_outvotes_cross_page_continuation"
        elif not isinstance(evidence, Mapping):
            suppress_reason = "ordinary_text_without_cross_page_evidence"
        elif (
            confidence in {"low", "medium"}
            and has_explicit_note_labels
            and "current_text_starts_as_continuation"
            not in evidence.get("positive_signals", [])
        ):
            suppress_reason = "weak_cross_page_evidence_before_labeled_notes"

        if suppress_reason is None:
            active.append(candidate)
            continue

        candidate["confidence"] = "context"
        candidate["disposition"] = "context_only"
        candidate["review_reason"] = "soft_continuation_context"
        candidate["suppression_reason"] = suppress_reason
        candidate["suppressed_as_context"] = True
        context_only.append(candidate)

    return active, context_only


def _aggregate_continuation_evidence(
    page_reports: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate continuation scores without duplicating page-level decisions."""
    summary = {
        "mode": "gated",
        "candidate_count": 0,
        "high_confidence_count": 0,
        "medium_confidence_count": 0,
        "low_confidence_count": 0,
        "semantic_body_count": 0,
        "semantic_footnote_count": 0,
        "semantic_inconclusive_count": 0,
    }
    for report in page_reports:
        evidence = report.get("continuation_evidence", {})
        if not isinstance(evidence, Mapping):
            continue
        for field in (
            "candidate_count",
            "high_confidence_count",
            "medium_confidence_count",
            "low_confidence_count",
        ):
            summary[field] += int(evidence.get(field, 0) or 0)
        for field in (
            "semantic_body_count",
            "semantic_footnote_count",
            "semantic_inconclusive_count",
        ):
            summary[field] += int(evidence.get(field, 0) or 0)
    return summary


def _bottom_detection_stats(
    sidecar: Mapping[str, Any],
    candidate_indexes: set[int],
    *,
    bottom_ratio: float,
    excluded_block_indexes: Optional[set[int]] = None,
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
    excluded_block_indexes = set(excluded_block_indexes or set())
    for index, block in enumerate(sidecar.get("blocks", [])):
        if index in excluded_block_indexes:
            continue
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


def _suspected_missed_block_indexes(
    sidecar: Mapping[str, Any],
    candidate_indexes: set[int],
    *,
    bottom_ratio: float,
    bottom_intersection_ratio: float,
    excluded_block_indexes: Optional[set[int]] = None,
) -> list[int]:
    """Return bottom numeric blocks that need visual recall auditing.

    A block that misses the local geometry threshold is not silently promoted
    to a footnote.  It is kept as an audit address so a visual reviewer can
    recover a note that OCR or a loose layout block split hid from the normal
    candidate list.  Page furniture, citations and printed page numbers stay
    excluded unless a confirmed continuous region already owns the block.
    """
    missed: list[int] = []
    excluded_block_indexes = set(excluded_block_indexes or set())
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
        geometry = _bottom_geometry(
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


def _region_union_bbox(boxes: Iterable[Optional[Iterable[float]]]) -> Optional[list[float]]:
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


def _region_compact_blocks(
    sidecar: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
    indexes: Iterable[int],
) -> list[dict[str, Any]]:
    by_index = {
        int(candidate["block"]): candidate
        for candidate in candidates
        if isinstance(candidate, Mapping) and str(candidate.get("block", "")).lstrip("-").isdigit()
    }
    blocks = sidecar.get("blocks", [])
    compact: list[dict[str, Any]] = []
    for raw_index in sorted({int(index) for index in indexes}):
        if not 0 <= raw_index < len(blocks) or not isinstance(blocks[raw_index], Mapping):
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


def _build_page_footnote_regions(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    audit_candidates: list[dict[str, Any]],
    *,
    bottom_ratio: float,
    auto_accept: bool,
    continuous_regions: list[dict[str, Any]],
    context_candidates: Iterable[Mapping[str, Any]] = (),
    ocr_conflict: bool = False,
) -> list[dict[str, Any]]:
    """Infer page-level note areas and mark only uncertain areas for vision.

    This is intentionally a cheap layout pass, not a semantic footnote
    classifier.  A tight, bottom-aligned run of numbered blocks is safe to
    accept locally when it has no unclassified neighbor or OCR conflict.  A
    continuation, a boundary collision, or a recall target keeps the whole
    small area together for one visual audit instead of creating one model
    request per block.
    """
    all_candidates = [*candidates, *audit_candidates]
    context_indexes = {
        int(item["block"])
        for item in context_candidates
        if isinstance(item, Mapping)
        and str(item.get("block", "")).lstrip("-").isdigit()
    }
    by_index = {
        int(item["block"]): item
        for item in all_candidates
        if str(item.get("block", "")).lstrip("-").isdigit()
    }
    if not by_index:
        return []
    blocks = sidecar.get("blocks", [])
    ordered = sorted(by_index)
    groups: list[list[int]] = []
    for index in ordered:
        if not groups:
            groups.append([index])
            continue
        previous = groups[-1][-1]
        previous_box = _normalise_bbox(blocks[previous], sidecar) if 0 <= previous < len(blocks) else None
        current_box = _normalise_bbox(blocks[index], sidecar) if 0 <= index < len(blocks) else None
        vertical_gap = (
            float(current_box[1]) - float(previous_box[3])
            if current_box and previous_box
            else 1.0
        )
        linked = index == previous + 1 and (
            vertical_gap <= DEFAULT_REGION_MAX_GAP
            or _horizontal_overlap(current_box, previous_box) >= 0.35
        )
        if linked:
            groups[-1].append(index)
        else:
            groups.append([index])

    continuous_indexes = {
        int(index)
        for region in continuous_regions
        for index in region.get("review_block_indices", [])
    }
    regions: list[dict[str, Any]] = []
    for region_number, indexes in enumerate(groups, 1):
        item_candidates = [by_index[index] for index in indexes if index in by_index]
        for item in item_candidates:
            continuation_key = _auto_continuation_key(item)
            if continuation_key and not item.get("key"):
                item["key"] = continuation_key
                item["auto_continuation"] = True
                item["local_role"] = "footnote_continuation"
        keys = [str(item.get("key")) for item in item_candidates if item.get("key")]
        numeric_keys: list[int] = []
        for key in keys:
            try:
                numeric_keys.append(int(key))
            except ValueError:
                continue
        has_sequence = any(
            right == left + 1
            for left, right in zip(numeric_keys, numeric_keys[1:])
        )
        reasons: list[str] = []
        if any(item.get("audit_only") for item in item_candidates):
            reasons.append("suspected_missed_bottom_numeric_block")
        if any(
            not item.get("key") and not _auto_continuation_key(item)
            for item in item_candidates
        ):
            reasons.append("unclassified_continuation_block")
        if any(
            item.get("continuous_region_evidence")
            and _FURNITURE_LABEL_RE.search(str(item.get("label") or ""))
            for item in item_candidates
        ):
            reasons.append("page_furniture_inside_continuous_region")
        # Continuous numeric evidence is positive evidence for a region.  It
        # becomes an anomaly only when the expansion also contains an
        # unnumbered block (handled above), not merely because the run exists.
        if ocr_conflict:
            reasons.append("ocr_candidate_evidence_conflict")

        first_index = indexes[0]
        if (
            first_index > 0
            and first_index - 1 not in by_index
            and first_index - 1 not in context_indexes
        ):
            previous_block = blocks[first_index - 1] if first_index - 1 < len(blocks) else None
            previous_box = _normalise_bbox(previous_block, sidecar) if isinstance(previous_block, Mapping) else None
            first_box = _normalise_bbox(blocks[first_index], sidecar) if first_index < len(blocks) else None
            previous_label = str(previous_block.get("label") or "") if isinstance(previous_block, Mapping) else ""
            first_label = (
                str(blocks[first_index].get("label") or "")
                if first_index < len(blocks)
                and isinstance(blocks[first_index], Mapping)
                else ""
            )
            if (
                previous_box
                and first_box
                and previous_box[3] >= float(bottom_ratio) - DEFAULT_REGION_BOUNDARY_MARGIN
                and _horizontal_overlap(previous_box, first_box) >= 0.35
                and not _FURNITURE_LABEL_RE.search(previous_label)
                and not _FOOTNOTE_LABEL_RE.search(first_label)
            ):
                reasons.append("footnote_region_boundary_uncertain")

        # A two-or-more numeric run is strong local evidence only when the
        # region has no unresolved continuation or boundary signal.  A single
        # block still needs the old explicit Footnote label rule.
        all_numbered = bool(item_candidates) and all(
            item.get("key") for item in item_candidates
        )
        has_auto_continuation = any(
            item.get("auto_continuation") for item in item_candidates
        )
        explicit_labels = all(
            _FOOTNOTE_LABEL_RE.search(str(item.get("label") or ""))
            for item in item_candidates
        )
        existing_high = bool(item_candidates) and all(
            item.get("confidence") == "high" for item in item_candidates
        ) and not any(item.get("audit_only") for item in item_candidates)
        locally_clear = bool(
            auto_accept
            and not reasons
            and all_numbered
            and (has_sequence or explicit_labels or existing_high or has_auto_continuation)
        )
        region_id = f"page-{int(page_number):03d}-region-{region_number:02d}"
        role = "local_footnote_region" if locally_clear else "visual_audit_required"
        bbox = _region_union_bbox(
            _normalise_bbox(blocks[index], sidecar)
            for index in indexes
            if 0 <= index < len(blocks) and isinstance(blocks[index], Mapping)
        )
        region = {
            "region_id": region_id,
            "page": int(page_number),
            "block_indices": indexes,
            "bbox": bbox,
            "keys": keys,
            "numeric_sequence": has_sequence,
            "continuous_numeric_evidence": any(
                index in continuous_indexes for index in indexes
            ),
            "confidence": "high" if locally_clear else "review",
            "disposition": role,
            "reasons": sorted(set(reasons)),
            "blocks": _region_compact_blocks(sidecar, all_candidates, indexes),
        }
        regions.append(region)
        for item in item_candidates:
            item["region_id"] = region_id
            item["region_confidence"] = region["confidence"]
            if locally_clear:
                item["confidence"] = "high"
                item["disposition"] = "local_candidate"
                item.setdefault("local_role", "footnote_start")
                item["review_reason"] = "page_region_local_accept"
            # An already high-confidence block keeps its local decision even
            # when a neighboring continuation makes the surrounding region
            # reviewable. The region audit still carries that block as visual
            # context, while only uncertain addresses require a new decision.
        region["blocks"] = _region_compact_blocks(sidecar, all_candidates, indexes)
    return regions


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


def _ensure_region_review_images(
    output_dir: Path,
    page_reports: Iterable[Mapping[str, Any]],
    config: Optional[Mapping[str, Any]],
) -> dict[str, str]:
    """Render narrow page crops for visual audit regions when a PDF exists."""
    regions = [
        region
        for report in page_reports
        for region in report.get("footnote_regions", []) or []
        if isinstance(region, Mapping)
        and region.get("disposition") == "visual_audit_required"
    ]
    if not regions:
        return {}
    pdf_path = Path(output_dir) / "input_original.pdf"
    if not pdf_path.is_file():
        pdf_path = Path(output_dir) / "input.pdf"
    if not pdf_path.is_file():
        return {}
    try:
        import pymupdf as fitz

        correction_config = (
            config.get("ocr_correction", {})
            if isinstance(config, Mapping)
            else {}
        )
        try:
            dpi = int(correction_config.get("review_dpi", 150))
        except (TypeError, ValueError):
            dpi = 150
        dpi = max(72, min(400, dpi))
        matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
        crop_dir = Path(output_dir) / "footnote_review_images"
        crop_dir.mkdir(parents=True, exist_ok=True)
        files: dict[str, str] = {}
        with fitz.open(pdf_path) as document:
            for region in regions:
                try:
                    page_number = int(region["page"])
                    bbox = [float(value) for value in region["bbox"]]
                    region_id = str(region["region_id"])
                except (KeyError, TypeError, ValueError):
                    continue
                if len(bbox) != 4 or not 1 <= page_number <= len(document):
                    continue
                page = document[page_number - 1]
                rect = page.rect
                margin_x = rect.width * 0.025
                margin_y = rect.height * 0.02
                clip = fitz.Rect(
                    max(rect.x0, rect.x0 + bbox[0] * rect.width - margin_x),
                    max(rect.y0, rect.y0 + bbox[1] * rect.height - margin_y),
                    min(rect.x1, rect.x0 + bbox[2] * rect.width + margin_x),
                    min(rect.y1, rect.y0 + bbox[3] * rect.height + margin_y),
                )
                if clip.width <= 0 or clip.height <= 0:
                    continue
                pixmap = page.get_pixmap(matrix=matrix, clip=clip, alpha=False)
                filename = f"{region_id}.png"
                (crop_dir / filename).write_bytes(pixmap.tobytes("png"))
                files[region_id] = (crop_dir / filename).relative_to(output_dir).as_posix()
        return files
    except (OSError, RuntimeError, ValueError, ImportError):
        return {}


def _write_visual_audit_manifest(
    output_dir: Path,
    page_reports: Iterable[Mapping[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    """Persist the small, region-scoped visual audit queue."""
    tasks: list[dict[str, Any]] = []
    for report in page_reports:
        page_visual = report.get("visual_file")
        for region in report.get("footnote_regions", []) or []:
            if not isinstance(region, Mapping) or region.get("disposition") != "visual_audit_required":
                continue
            task = {
                "region_id": region.get("region_id"),
                "page": region.get("page"),
                "bbox": region.get("bbox"),
                "block_indices": region.get("block_indices", []),
                "keys": region.get("keys", []),
                "reasons": region.get("reasons", []),
                "visual_file": page_visual,
                "region_visual_file": region.get("region_visual_file"),
                "blocks": region.get("blocks", []),
                "status": "pending",
            }
            tasks.append(task)
    manifest = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "task": "footnote-visual-audit",
        "status": "pending_review" if tasks else "no_audit_required",
        "tasks": tasks,
        "task_count": len(tasks),
        "decision_file": "footnote_decisions.json",
    }
    path = Path(output_dir) / "footnote_visual_audit_manifest.json"
    atomic_write_text(path, json.dumps(manifest, ensure_ascii=False, indent=2))
    return path, manifest


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
    refined = [dict(item) for item in units if isinstance(item, dict)]

    # A resumable tree_progress from before typed units may not have copied
    # the current TOC role. Recover it from the same index path used to name
    # the refinement unit, including roles inherited by typed descendants.
    toc_roles: dict[tuple[int, ...], str] = {}
    toc_path = Path(output_dir) / "toc_tree.json"
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        toc = {}

    def visit(nodes: Any, path: tuple[int, ...], inherited: str = "") -> None:
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, Mapping):
                continue
            role = str(node.get("type") or inherited).strip().lower()
            current = path + (index,)
            toc_roles[current] = role
            visit(node.get("children"), current, role)

    if isinstance(toc, Mapping):
        visit(toc.get("chapters"), ())

    for unit in refined:
        if str(unit.get("type") or "").strip():
            unit["type"] = str(unit["type"]).strip().lower()
            continue
        raw_path = unit.get("index_path")
        try:
            index_path = tuple(int(value) for value in raw_path)
        except (TypeError, ValueError):
            index_path = ()
        if index_path:
            unit["type"] = toc_roles.get(index_path, "")
    return refined


def _structure_exclusion_for_page(
    units: Iterable[Mapping[str, Any]], page_number: int
) -> dict[str, Any]:
    """Return typed refinement units that make page-footnote detection unsafe."""
    excluded: list[dict[str, Any]] = []
    for unit in units:
        role = str(unit.get("type") or "").strip().lower()
        if role not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES:
            continue
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            continue
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            continue
        if start_page <= page_number <= end_page:
            excluded.append(
                {
                    "type": role,
                    "file": str(unit.get("file") or ""),
                    "page_range": [start_page, end_page],
                    "boundary_info": dict(unit.get("boundary_info") or {})
                    if isinstance(unit.get("boundary_info"), Mapping)
                    else {},
                }
            )
    roles = sorted({str(item["type"]) for item in excluded})
    return {"roles": roles, "units": excluded}


def _structure_text_signature(value: Any) -> str:
    """Build a tolerant line-search signature for a sidecar block.

    Sidecars can contain raw HTML while the page Markdown contains converted
    Markdown (and OCR may escape emphasis punctuation).  For boundary mapping
    we only need a stable alphanumeric anchor, not an editable source span.
    """
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\\([*_<>{}#$`~])", r"\1", text)
    text = re.sub(r"\[\^[A-Za-z0-9_-]+\]\s*:\s*", " ", text)
    return "".join(char.casefold() for char in text if char.isalnum())


def _block_line_span(
    page_markdown: str,
    sidecar: Mapping[str, Any],
    block_index: int,
) -> Optional[tuple[int, int]]:
    """Locate a sidecar block in page Markdown using a line-aware signature."""
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list) or not 0 <= block_index < len(blocks):
        return None
    block = blocks[block_index]
    if not isinstance(block, Mapping):
        return None
    needle = _structure_text_signature(_text_from_block(block))
    if len(needle) < 3:
        return None

    page_signature: list[str] = []
    signature_lines: list[int] = []
    for line_number, line in enumerate(str(page_markdown or "").splitlines(), 1):
        line_signature = _structure_text_signature(line)
        page_signature.extend(line_signature)
        signature_lines.extend([line_number] * len(line_signature))
    if not page_signature or len(needle) > len(page_signature):
        return None

    page_value = "".join(page_signature)
    matches: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = page_value.find(needle, cursor)
        if found < 0:
            break
        end = found + len(needle)
        matches.append((signature_lines[found], signature_lines[end - 1] + 1))
        cursor = found + 1
    return matches[-1] if matches else None


def _structure_excluded_block_indexes(
    page_markdown: str,
    sidecar: Mapping[str, Any],
    page_number: int,
    structure_exclusion: Mapping[str, Any],
) -> set[int]:
    """Map typed TOC units to sidecar blocks on a possibly shared page."""
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list):
        return set()
    excluded: set[int] = set()
    for raw_unit in structure_exclusion.get("units", []) or []:
        if not isinstance(raw_unit, Mapping):
            continue
        page_range = raw_unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            continue
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            continue
        boundary = raw_unit.get("boundary_info")
        if not isinstance(boundary, Mapping):
            excluded.update(range(len(blocks)))
            continue
        try:
            start_line = int(boundary["start_line"]) if "start_line" in boundary else 1
            end_line = (
                int(boundary["end_line"])
                if "end_line" in boundary
                else len(str(page_markdown or "").splitlines()) + 1
            )
        except (TypeError, ValueError):
            excluded.update(range(len(blocks)))
            continue
        if page_number == start_page and page_number == end_page:
            lower, upper = start_line, end_line
        elif page_number == start_page:
            lower, upper = start_line, len(str(page_markdown or "").splitlines()) + 1
        elif page_number == end_page:
            lower, upper = 1, end_line
        else:
            lower, upper = 1, len(str(page_markdown or "").splitlines()) + 1
        for block_index in range(len(blocks)):
            span = _block_line_span(page_markdown, sidecar, block_index)
            if span is None:
                # Without a reliable text anchor, preserve the previous
                # conservative page-level behavior for that block.
                if lower == 1 and upper > len(str(page_markdown or "").splitlines()):
                    excluded.add(block_index)
                continue
            if span[0] < upper and span[1] > lower:
                excluded.add(block_index)
    return excluded


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
    mode: Optional[str] = None,
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
        mode=mode,
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
    soft_continuation_context_pages: set[int] = set()
    soft_continuation_context_count = 0
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
    previous_page_number: Optional[int] = None
    previous_page_footnote_evidence = False
    previous_page_context: Optional[dict[str, Any]] = None
    previous_secondary_page_number: Optional[int] = None
    previous_secondary_page_footnote_evidence = False
    previous_secondary_page_context: Optional[dict[str, Any]] = None
    refined_units = _load_refined_units(output_dir)
    structure_excluded_pages: list[dict[str, Any]] = []

    for sidecar_path in sidecars:
        sidecar = _load_sidecar(sidecar_path)
        try:
            page_number = int(sidecar.get("page_number"))
        except (TypeError, ValueError) as exc:
            raise ValueError(f"OCR sidecar has invalid page_number: {sidecar_path}") from exc
        sidecar_hashes[sidecar_path.relative_to(output_dir).as_posix()] = sha256_file(sidecar_path)
        secondary_sidecar = _secondary_sidecar_path(output_dir, sidecar_path)
        if evidence_mode == "two_ocr" and secondary_sidecar.is_file():
            secondary_sidecar_hashes[
                secondary_sidecar.relative_to(output_dir).as_posix()
            ] = sha256_file(secondary_sidecar)

        structure_exclusion = _structure_exclusion_for_page(
            refined_units, page_number
        )
        if options.mode != "footnotes":
            if structure_exclusion["roles"]:
                structure_excluded_pages.append(
                    {
                        "page": page_number,
                        **structure_exclusion,
                    }
                )
            # Do not let a notes/bibliography page create a cross-page
            # continuation context for an adjacent ordinary-text page.
            previous_page_number = page_number
            previous_page_footnote_evidence = False
            previous_page_context = None
            previous_secondary_page_number = page_number
            previous_secondary_page_footnote_evidence = False
            previous_secondary_page_context = None
            continue

        page_markdown_path = pages_dir / f"page_{page_number:03d}.md"
        try:
            page_markdown = page_markdown_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            page_markdown = ""
        structure_excluded_blocks = _structure_excluded_block_indexes(
            page_markdown,
            sidecar,
            page_number,
            structure_exclusion,
        )
        if structure_exclusion["roles"]:
            structure_excluded_pages.append(
                {
                    "page": page_number,
                    **structure_exclusion,
                    "excluded_block_indexes": sorted(structure_excluded_blocks),
                }
            )
        candidates = []
        for block_index, block in enumerate(sidecar["blocks"]):
            if not isinstance(block, Mapping):
                continue
            if block_index in structure_excluded_blocks:
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
            previous_page_footnote_evidence=(
                previous_page_number == page_number - 1
                and previous_page_footnote_evidence
            ),
            excluded_block_indexes=structure_excluded_blocks,
        )
        continuation_evidence = _annotate_cross_page_continuations(
            sidecar,
            candidates,
            continuous_regions,
            previous_page_context
            if previous_page_number == page_number - 1
            else None,
        )
        candidates, suppressed_continuation_candidates = (
            _suppress_soft_continuation_candidates(candidates)
        )
        if suppressed_continuation_candidates:
            soft_continuation_context_pages.add(page_number)
            soft_continuation_context_count += len(suppressed_continuation_candidates)
        candidates.sort(key=lambda item: (item["order"], item["block"]))
        candidate_indexes = {
            int(index)
            for candidate in candidates
            for index in _candidate_block_indexes(candidate)
        }
        bottom_stats = _bottom_detection_stats(
            sidecar,
            candidate_indexes,
            bottom_ratio=options.bottom_ratio,
            excluded_block_indexes=structure_excluded_blocks,
        )
        for field, value in bottom_stats.items():
            bottom_detection_totals[field] += int(value)
        if bottom_stats["suspected_missed_count"]:
            suspected_missed_pages.add(page_number)

        # A bottom numeric block that failed the cheap geometry rule becomes
        # an explicit visual-audit address instead of disappearing into a
        # recall warning. It is never locally accepted.
        audit_candidates: list[dict[str, Any]] = []
        for missed_index in _suspected_missed_block_indexes(
            sidecar,
            candidate_indexes,
            bottom_ratio=options.bottom_ratio,
            bottom_intersection_ratio=options.bottom_intersection_ratio,
            excluded_block_indexes=structure_excluded_blocks,
        ):
            block = sidecar["blocks"][missed_index]
            if native_text_source:
                audit_candidate = _candidate_for_native_block(
                    page_number,
                    missed_index,
                    block,
                    sidecar,
                    bottom_ratio=options.bottom_ratio,
                    bottom_intersection_ratio=options.bottom_intersection_ratio,
                    page_markdown=page_markdown,
                    auto_accept=False,
                    native_max_font_ratio=options.native_max_font_ratio,
                    region_evidence=True,
                )
            else:
                audit_candidate = _candidate_for_block(
                    page_number,
                    missed_index,
                    block,
                    sidecar,
                    bottom_ratio=options.bottom_ratio,
                    bottom_intersection_ratio=options.bottom_intersection_ratio,
                    force_review=True,
                    source="primary",
                    region_evidence=True,
                )
            if audit_candidate is not None:
                audit_candidate["audit_only"] = True
                audit_candidate["confidence"] = "review"
                audit_candidate["disposition"] = "review_required"
                audit_candidate["review_reason"] = "suspected_missed_bottom_numeric_block"
                audit_candidates.append(audit_candidate)

        candidates.sort(key=lambda item: (item["order"], item["block"]))
        secondary_candidates: list[dict[str, Any]] = []
        secondary_regions: list[dict[str, Any]] = []
        secondary_continuation_evidence = {
            "mode": "gated",
            "candidate_count": 0,
            "high_confidence_count": 0,
            "medium_confidence_count": 0,
            "low_confidence_count": 0,
        }
        candidate_disagreement = False
        if evidence_mode == "two_ocr":
            secondary = _load_sidecar(secondary_sidecar)
            secondary_excluded_blocks = _structure_excluded_block_indexes(
                page_markdown,
                secondary,
                page_number,
                structure_exclusion,
            )
            secondary_sidecar_hashes[
                secondary_sidecar.relative_to(output_dir).as_posix()
            ] = sha256_file(secondary_sidecar)
            for block_index, block in enumerate(secondary["blocks"]):
                if not isinstance(block, Mapping):
                    continue
                if block_index in secondary_excluded_blocks:
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
                previous_page_footnote_evidence=(
                    previous_secondary_page_number == page_number - 1
                    and previous_secondary_page_footnote_evidence
                ),
                excluded_block_indexes=secondary_excluded_blocks,
            )
            secondary_continuation_evidence = _annotate_cross_page_continuations(
                secondary,
                secondary_candidates,
                secondary_regions,
                previous_secondary_page_context
                if previous_secondary_page_number == page_number - 1
                else None,
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

        footnote_regions = _build_page_footnote_regions(
            page_number,
            sidecar,
            candidates,
            audit_candidates,
            bottom_ratio=options.bottom_ratio,
            auto_accept=options.auto_accept,
            continuous_regions=continuous_regions,
            context_candidates=suppressed_continuation_candidates,
            ocr_conflict=candidate_disagreement,
        )
        for region in footnote_regions:
            if region["disposition"] == "visual_audit_required":
                review_pages.add(page_number)

        should_emit_page = bool(candidates) or bool(audit_candidates) or (
            evidence_mode == "two_ocr" and bool(secondary_candidates)
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
                    "suppressed_continuation_candidates": (
                        suppressed_continuation_candidates
                    ),
                    "audit_candidates": audit_candidates,
                    "secondary_candidates": secondary_candidates,
                    "secondary_only_candidates": secondary_only_candidates
                    if evidence_mode == "two_ocr"
                    else [],
                    "continuous_numeric_regions": continuous_regions,
                    "continuation_evidence": continuation_evidence,
                    "secondary_continuous_numeric_regions": (
                        secondary_regions if evidence_mode == "two_ocr" else []
                    ),
                    "secondary_continuation_evidence": secondary_continuation_evidence,
                    "footnote_regions": footnote_regions,
                    "bottom_detection": bottom_stats,
                    "window": _window_for_page(
                        page_number,
                        sidecar,
                        [
                            *candidates,
                            *suppressed_continuation_candidates,
                            *audit_candidates,
                        ],
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

        previous_page_number = page_number
        previous_page_context = _build_footnote_page_context(
            page_number,
            sidecar,
            candidates,
        )
        previous_page_footnote_evidence = _has_footnote_evidence(candidates)
        if evidence_mode == "two_ocr":
            previous_secondary_page_number = page_number
            previous_secondary_page_context = _build_footnote_page_context(
                page_number,
                secondary,
                secondary_candidates,
            )
            previous_secondary_page_footnote_evidence = _has_footnote_evidence(
                secondary_candidates
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

    region_visual_files = _ensure_region_review_images(output_dir, page_reports, config)
    for page_report in page_reports:
        for region in page_report.get("footnote_regions", []) or []:
            if isinstance(region, Mapping):
                region["region_visual_file"] = region_visual_files.get(
                    str(region.get("region_id"))
                )

    visual_audit_path, visual_audit_manifest = _write_visual_audit_manifest(
        output_dir, page_reports
    )
    high_confidence_count = sum(
        1
        for page_report in page_reports
        for candidate in page_report.get("candidates", []) or []
        if candidate.get("confidence") == "high"
    )
    review_count = sum(
        1
        for page_report in page_reports
        for candidate in [
            *(page_report.get("candidates", []) or []),
            *(page_report.get("audit_candidates", []) or []),
            *(page_report.get("secondary_only_candidates", []) or []),
        ]
        if candidate.get("confidence") == "review"
    )

    report = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_dir": "pages",
        "source_kind": source_kind,
        "ocr_evidence_mode": evidence_mode,
        "footnote_mode": options.mode,
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
        "continuation_evidence_summary": _aggregate_continuation_evidence(
            page_reports
        ),
        "review_pages": sorted(review_pages),
        "high_confidence_candidate_count": high_confidence_count,
        "review_candidate_count": review_count,
        "soft_continuation_context_count": soft_continuation_context_count,
        "soft_continuation_context_pages": sorted(soft_continuation_context_pages),
        "bottom_detection": bottom_detection_totals,
        "suspected_missed_count": bottom_detection_totals["suspected_missed_count"],
        "suspected_missed_pages": sorted(suspected_missed_pages),
        "structure_excluded_pages": structure_excluded_pages,
        "structure_exempt_unit_types": sorted(FOOTNOTE_STRUCTURE_EXEMPT_TYPES),
        "visual_evidence": visual_evidence,
        "visual_audit_manifest": visual_audit_path.name,
        "visual_audit_task_count": visual_audit_manifest["task_count"],
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
    mode: Optional[str] = None,
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
        mode=mode,
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
        "footnote_mode": report["footnote_mode"],
        "candidate_report": "footnote_candidates.json",
        "review_pages": report["review_pages"],
        "high_confidence_candidate_count": report["high_confidence_candidate_count"],
        "review_candidate_count": report["review_candidate_count"],
        "soft_continuation_context_count": report[
            "soft_continuation_context_count"
        ],
        "soft_continuation_context_pages": report[
            "soft_continuation_context_pages"
        ],
        "bottom_detection": report["bottom_detection"],
        "suspected_missed_count": report["suspected_missed_count"],
        "suspected_missed_pages": report["suspected_missed_pages"],
        "structure_excluded_pages": report["structure_excluded_pages"],
        "visual_evidence": report["visual_evidence"],
        "visual_audit_manifest": report["visual_audit_manifest"],
        "visual_audit_task_count": report["visual_audit_task_count"],
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
        region_lines = []
        for region in page_report.get("footnote_regions", []) or []:
            if not isinstance(region, Mapping) or region.get("disposition") != "visual_audit_required":
                continue
            region_line = str(region.get("region_id"))
            if region.get("region_visual_file"):
                region_line += f" crop=`{region['region_visual_file']}`"
            reasons = ", ".join(str(item) for item in region.get("reasons", []) or [])
            if reasons:
                region_line += f" ({reasons})"
            region_lines.append(region_line)
        if region_lines:
            line += "; audit regions: " + ", ".join(region_lines)
        page_lines.append(line)
    page_text = "\n".join(page_lines) or "- 无需 Subagent 复核；仅保留本地高置信度候选。"
    source_guidance = (
        "Footnote extraction is disabled by the configured footnotes.mode. "
        "Do not create a decision file or classify any page content as a page footnote. "
        "The local apply stage will preserve the endnote/source Markdown unchanged."
        if report["footnote_mode"] != "footnotes"
        else (
            "For native-text pages, use the recorded coordinates and font metadata as layout evidence. "
            "Candidates marked confidence=high have already passed the conservative local rule: an earlier "
            "same-page superscript reference matches the key and the note text is materially smaller than "
            "the body font. Do not create a second decision for those blocks. Review only confidence=review "
            "blocks, and distinguish them from citations, page furniture, and ordinary numbered prose."
            if report["source_kind"] == "native_text"
            else "If a page is marked as an OCR-consensus visual review, treat the local label as untrusted even when it says `Footnote`; compare the primary and secondary sidecars and the page image before deciding."
        )
    )
    prompt = f"""# Footnote review\n\nBook: {book_title}\n\nRead `footnote_subagent_manifest.json`, `footnote_candidates.json`, and `footnote_visual_audit_manifest.json`. The local script first inferred page-level bottom-note regions. Review only the regions listed as `visual_audit_required` and their blocks marked `confidence: review`; blocks marked `confidence: high` are already accepted by the deterministic local rule and do not need a decision. Do not reread or rewrite the whole book. {source_guidance}\n\nCandidate page sidecars and visual evidence:\n{page_text}\n\nFor every listed review page, open the full-page `visual` PNG first. It is authoritative for the overall body/footnote boundary, page furniture, reading order, and cross-page context. When a `crop` PNG is present, open it as a zoomed supplement for small text and block-level details; never use the crop alone to decide whether a region belongs to the page footer or body. If the full-page image is unavailable, use the crop and sidecar coordinates.\n\nA region audit is one visual decision for the area, but the output must still contain one block decision for every listed review/audit block so the deterministic apply stage can address exact sidecar coordinates. Treat the page image as authoritative for region boundaries, continuations, headers, footers, and OCR omissions.\n\nUse these meanings strictly:\n- `footnote_start`: the beginning of a real page footnote; it will be moved to the end of its logical chapter.\n- `footnote_continuation`: text continuing a real footnote from an earlier page; it will be joined to that footnote.\n- `footnote_definition`: a complete footnote definition already presented as a note block.\n- `citation`: an in-text citation, quoted source, parenthetical/numeric reference, or other scholarly reference; it must stay in the body and must never be moved.\n- `bibliography`: a reference-list/bibliography entry; it must stay in place and must never be moved.\n- `body`: ordinary prose or an uncertain block that is not a footnote.\n\nWrite one `region_audits` entry for every audit region, with its exact `region_id`, a `disposition` of `footnote_region`, `body_region`, or `review_required`, and nested block decisions. A `review_required` region or block is unresolved and will block the workflow. Also keep a flat `decisions` array for compatibility; it may repeat the nested decisions, but do not emit duplicates. For each review/audit block assign exactly one role and keep its page, source, and block number. `source` defaults to `primary`; use `source: "secondary"` only for a candidate listed under `secondary_only_candidates`. For `footnote_start` and `footnote_definition`, copy the visible numeric key when present. For `footnote_continuation`, provide the key of the footnote it continues. A numeric marker alone is not enough to call something a footnote: if it is a citation or reference, use `citation` or `bibliography`.\n\nWrite only valid JSON to `footnote_decisions.json` with this shape:\n\n```json\n{{\n  "schema_version": {FOOTNOTE_PREPARE_SCHEMA_VERSION},\n  "region_audits": [\n    {{\n      "region_id": "page-125-region-01",\n      "disposition": "footnote_region",\n      "decisions": [\n        {{\n          "page": 125,\n          "source": "primary",\n          "block": 8,\n          "role": "footnote_continuation",\n          "key": "36",\n          "confidence": "high"\n        }}\n      ]\n    }}\n  ],\n  "decisions": []\n}}\n```\n\nIf a block or region is genuinely ambiguous, use `role` or `disposition: "review_required"` and explain it in `reason`; do not guess.\n"""
    prompt = prompt.replace(
        "Candidate page sidecars and visual evidence:",
        "When a `visual:` or `crop=` path is present, open that matching PNG and use it as the visual authority. "
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
            ("audit_candidates", "primary"),
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
    footnote_mode = str(report.get("footnote_mode") or "footnotes").strip().lower()
    errors: list[str] = []
    if source_kind not in {"ocr", "native_text"}:
        errors.append("candidate report has an unsupported page source kind")
    if evidence_mode not in {"single_ocr", "two_ocr"}:
        errors.append("candidate report has an unsupported OCR evidence mode")
    if footnote_mode not in FOOTNOTE_MODES:
        errors.append("candidate report has an unsupported footnote mode")
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
        try:
            configured_footnote_mode = resolve_footnote_options(config).mode
            if footnote_mode != configured_footnote_mode:
                errors.append(
                    "candidate report was prepared with footnotes.mode="
                    f"{footnote_mode}, but configuration requires "
                    f"{configured_footnote_mode}"
                )
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
            "footnote_mode": footnote_mode,
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
    required.update(
        (
            int(candidate["page"]),
            "primary",
            int(candidate["block"]),
        )
        for page in report.get("pages", []) or []
        for candidate in page.get("audit_candidates", []) or []
        if candidate.get("confidence") == "review"
    )
    if not required:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "footnote_mode": footnote_mode,
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

    # New hand-offs are region-first.  Keep accepting the historical flat
    # array so completed projects can resume, but validate every new region
    # audit and merge its nested block decisions into the same exact-address
    # contract consumed by footnote-apply.
    visual_manifest_path = output_dir / str(
        report.get("visual_audit_manifest") or "footnote_visual_audit_manifest.json"
    )
    visual_manifest: Mapping[str, Any] = {}
    if visual_manifest_path.is_file():
        try:
            loaded_manifest = json.loads(visual_manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded_manifest, Mapping):
                visual_manifest = loaded_manifest
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append("footnote visual audit manifest is invalid")
    audit_tasks = {
        str(task.get("region_id"))
        for task in visual_manifest.get("tasks", []) or []
        if isinstance(task, Mapping) and task.get("region_id")
    }
    region_audits = (
        decisions_data.get("region_audits")
        if isinstance(decisions_data, Mapping)
        else None
    )
    if region_audits is not None and not isinstance(region_audits, list):
        errors.append("region_audits must be an array")
        region_audits = []
    region_audit_ids: set[str] = set()
    nested_decisions: list[Mapping[str, Any]] = []
    for audit in region_audits or []:
        if not isinstance(audit, Mapping):
            errors.append("each region audit must be an object")
            continue
        region_id = str(audit.get("region_id") or "").strip()
        if not region_id or region_id not in audit_tasks:
            errors.append(f"region audit references unknown region: {region_id or '<missing>'}")
            continue
        if region_id in region_audit_ids:
            errors.append(f"duplicate region audit: {region_id}")
            continue
        region_audit_ids.add(region_id)
        disposition = str(audit.get("disposition") or "").strip()
        if disposition not in {"footnote_region", "body_region", "review_required"}:
            errors.append(f"unsupported region audit disposition for {region_id}: {disposition}")
        if disposition == "review_required":
            human_review_required.append(f"region:{region_id}")
        region_decisions = audit.get("decisions")
        if not isinstance(region_decisions, list):
            errors.append(f"region audit {region_id} must contain a decisions array")
            continue
        nested_decisions.extend(
            item for item in region_decisions if isinstance(item, Mapping)
        )
    if region_audits is not None:
        missing_regions = sorted(audit_tasks - region_audit_ids)
        errors.extend(f"missing region audit for {region_id}" for region_id in missing_regions)

    # Merge nested and flat arrays without making a compatible hand-off fail
    # merely because the same exact decision was copied into both places.
    merged_decisions: list[Mapping[str, Any]] = []
    merged_addresses: set[tuple[int, str, int]] = set()
    for item in [*nested_decisions, *decisions]:
        if not isinstance(item, Mapping):
            merged_decisions.append(item)
            continue
        try:
            address = (
                int(item["page"]),
                str(item.get("source") or "primary").strip().lower(),
                int(item["block"]),
            )
        except (KeyError, TypeError, ValueError):
            merged_decisions.append(item)
            continue
        if address in merged_addresses:
            continue
        merged_addresses.add(address)
        merged_decisions.append(item)
    decisions = merged_decisions

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
        "footnote_mode": footnote_mode,
        "valid": not errors and not human_review_required,
        "status": status,
        "errors": errors,
        "human_review_required": human_review_required,
        "region_audits": sorted(region_audit_ids),
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

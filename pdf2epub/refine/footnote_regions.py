"""Group candidate blocks into page-level footnote review regions.

Candidate modules report local facts. This module applies the page-region
policy on top of those facts: it groups adjacent blocks, gathers independent
layout/marker evidence, and decides whether a region is locally clear or must
be visually audited. It does not build candidates, render images, or validate
Subagent decisions.
"""

from __future__ import annotations

from typing import Any, Iterable, Mapping

from .footnote_candidates import DEFAULT_NATIVE_MAX_FONT_RATIO
from .footnote_continuation_regions import (
    _auto_continuation_key,
    _horizontal_overlap,
)
from .footnote_layout import (
    DEFAULT_REGION_MAX_GAP,
    region_compact_blocks as _region_compact_blocks,
    region_layout_evidence as _region_layout_evidence,
    region_union_bbox as _region_union_bbox,
)
from .footnote_signals import _FOOTNOTE_LABEL_RE, _FURNITURE_LABEL_RE
from .layout_evidence import normalized_bbox as _normalise_bbox


DEFAULT_REGION_BOUNDARY_MARGIN = 0.12


def build_page_footnote_regions(
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
    """Infer page-level note areas and mark only uncertain areas for vision."""
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
        previous_box = (
            _normalise_bbox(blocks[previous], sidecar)
            if 0 <= previous < len(blocks)
            else None
        )
        current_box = (
            _normalise_bbox(blocks[index], sidecar)
            if 0 <= index < len(blocks)
            else None
        )
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
        if ocr_conflict:
            reasons.append("ocr_candidate_evidence_conflict")
        if any(item.get("body_flow_warning") for item in item_candidates):
            reasons.append("ordinary_text_below_numeric_block")

        first_index = indexes[0]
        if (
            first_index > 0
            and first_index - 1 not in by_index
            and first_index - 1 not in context_indexes
        ):
            previous_block = (
                blocks[first_index - 1]
                if first_index - 1 < len(blocks)
                else None
            )
            previous_box = (
                _normalise_bbox(previous_block, sidecar)
                if isinstance(previous_block, Mapping)
                else None
            )
            first_box = (
                _normalise_bbox(blocks[first_index], sidecar)
                if first_index < len(blocks)
                else None
            )
            previous_label = (
                str(previous_block.get("label") or "")
                if isinstance(previous_block, Mapping)
                else ""
            )
            first_label = (
                str(blocks[first_index].get("label") or "")
                if first_index < len(blocks)
                and isinstance(blocks[first_index], Mapping)
                else ""
            )
            if (
                previous_box
                and first_box
                and previous_box[3]
                >= float(bottom_ratio) - DEFAULT_REGION_BOUNDARY_MARGIN
                and _horizontal_overlap(previous_box, first_box) >= 0.35
                and not _FURNITURE_LABEL_RE.search(previous_label)
                and not _FOOTNOTE_LABEL_RE.search(first_label)
            ):
                reasons.append("footnote_region_boundary_uncertain")

        layout_evidence = _region_layout_evidence(
            sidecar,
            indexes,
            context_indexes=context_indexes,
        )
        # A numeric run is not semantic proof. Local acceptance requires an
        # independent signal plus page-relative tail evidence.
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
        native_candidates = any(
            str(item.get("source_kind") or "").strip().lower() == "native_text"
            for item in item_candidates
        )
        keyed_candidates = [item for item in item_candidates if item.get("key")]
        marker_support = bool(keyed_candidates) and all(
            bool(item.get("same_page_marker")) for item in keyed_candidates
        )
        native_structural_support = native_candidates and all(
            bool(item.get("same_page_superscript"))
            and isinstance(item.get("font_size_ratio"), (int, float))
            and float(item["font_size_ratio"])
            <= float(item.get("native_max_font_ratio", DEFAULT_NATIVE_MAX_FONT_RATIO))
            for item in keyed_candidates
        )
        explicit_layout_support = bool(
            explicit_labels and layout_evidence.get("tail_aligned")
        )
        marker_layout_support = bool(
            has_sequence
            and marker_support
            and layout_evidence.get("tail_aligned")
            and layout_evidence.get("detached_from_body")
        )
        continuation_layout_support = bool(
            has_auto_continuation
            and keyed_candidates
            and any(not item.get("auto_continuation") for item in keyed_candidates)
            and layout_evidence.get("tail_aligned")
            and all(
                _FOOTNOTE_LABEL_RE.search(str(item.get("label") or ""))
                for item in keyed_candidates
                if not item.get("auto_continuation")
            )
        )
        native_layout_support = bool(
            native_structural_support and layout_evidence.get("tail_aligned")
        )
        local_acceptance_evidence: list[str] = []
        if explicit_layout_support:
            local_acceptance_evidence.append("consistent_footnote_labels")
        if marker_layout_support:
            local_acceptance_evidence.append("same_page_markers_and_detached_tail")
        if continuation_layout_support:
            local_acceptance_evidence.append(
                "cross_page_continuation_and_labeled_tail"
            )
        if native_layout_support:
            local_acceptance_evidence.append("native_marker_font_and_detached_tail")
        if all_numbered and not local_acceptance_evidence:
            reasons.append("insufficient_independent_footnote_evidence")
        locally_clear = bool(
            auto_accept
            and not reasons
            and all_numbered
            and bool(local_acceptance_evidence)
        )
        region_id = f"page-{int(page_number):03d}-region-{region_number:02d}"
        role = "local_footnote_region" if locally_clear else "visual_audit_required"
        bbox = _region_union_bbox(
            _normalise_bbox(blocks[index], sidecar)
            for index in indexes
            if 0 <= index < len(blocks)
            and isinstance(blocks[index], Mapping)
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
            "layout_evidence": layout_evidence,
            "local_acceptance_evidence": local_acceptance_evidence,
            "blocks": _region_compact_blocks(sidecar, all_candidates, indexes),
        }
        regions.append(region)
        for item in item_candidates:
            item["region_id"] = region_id
            item["region_confidence"] = region["confidence"]
            item["layout_evidence"] = layout_evidence
            if "ordinary_text_below_numeric_block" in reasons:
                item["confidence"] = "review"
                item["disposition"] = "review_required"
                item["review_reason"] = "ordinary_text_below_numeric_block"
            elif locally_clear:
                item["confidence"] = "high"
                item["disposition"] = "local_candidate"
                item.setdefault("local_role", "footnote_start")
                item["review_reason"] = "page_region_local_accept"
            else:
                mixed_region_only = set(reasons) <= {
                    "unclassified_continuation_block"
                }
                if (
                    mixed_region_only
                    and item.get("confidence") == "high"
                    and item.get("key")
                    and _FOOTNOTE_LABEL_RE.search(str(item.get("label") or ""))
                    and not item.get("audit_only")
                ):
                    continue
                existing_reason = str(item.get("review_reason") or "").strip()
                item["confidence"] = "review"
                item["disposition"] = "review_required"
                item["review_reason"] = existing_reason or (
                    reasons[0] if reasons else "region_requires_visual_review"
                )
        region["blocks"] = _region_compact_blocks(sidecar, all_candidates, indexes)
    return regions


_build_page_footnote_regions = build_page_footnote_regions


__all__ = ["build_page_footnote_regions"]

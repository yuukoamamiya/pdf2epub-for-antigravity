"""Scan one page for footnote evidence.

The scanner is deliberately stateful only across adjacent pages.  It owns the
page-level evidence pipeline (primary OCR, optional secondary OCR, region
grouping, and compact windows) while the orchestration layer owns aggregation
and artifact writing.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import sha256_file
from .footnote_candidates import (
    aggregate_secondary_only_candidates,
    candidate_block_indexes,
    candidate_for_block,
    candidate_for_native_block,
    candidates_match,
    window_for_page,
)
from .footnote_continuation import (
    _annotate_cross_page_continuations as annotate_cross_page_continuations,
    _build_footnote_page_context as build_footnote_page_context,
    _has_footnote_evidence as has_footnote_evidence,
    _suppress_soft_continuation_candidates as suppress_soft_continuation_candidates,
)
from .footnote_continuation_regions import (
    _expand_continuous_region_candidates as expand_continuous_region_candidates,
)
from .footnote_layout import (
    bottom_detection_stats,
    page_layout_profile,
    suspected_missed_block_indexes,
)
from .footnote_font_evidence import native_body_font_excluded_blocks
from .footnote_options import FootnotePrepareOptions
from .footnote_regions import build_page_footnote_regions
from .footnote_structure import (
    structure_excluded_block_indexes,
    structure_exclusion_for_page,
)
from .layout_evidence import load_layout_sidecar


@dataclass
class FootnoteScanState:
    """Previous-page facts needed to score continuation evidence."""

    page_number: Optional[int] = None
    footnote_evidence: bool = False
    page_context: Optional[dict[str, Any]] = None
    secondary_page_number: Optional[int] = None
    secondary_footnote_evidence: bool = False
    secondary_page_context: Optional[dict[str, Any]] = None

    def reset(self, page_number: int) -> None:
        """Break continuation links at a typed/exempt unit boundary."""
        self.page_number = page_number
        self.footnote_evidence = False
        self.page_context = None
        self.secondary_page_number = page_number
        self.secondary_footnote_evidence = False
        self.secondary_page_context = None


@dataclass(frozen=True)
class FootnotePageScanResult:
    """Facts the orchestrator needs after scanning one sidecar."""

    page_number: int
    page_report: Optional[dict[str, Any]]
    structure_exclusion: Optional[dict[str, Any]]
    sidecar_hash: tuple[str, str]
    secondary_sidecar_hash: Optional[tuple[str, str]]
    review_required: bool
    suspected_missed: bool
    soft_context_count: int
    bottom_detection: dict[str, int]


def _candidate_for_audit_block(
    page_number: int,
    block_index: int,
    block: Mapping[str, Any],
    sidecar: Mapping[str, Any],
    *,
    native_text_source: bool,
    options: FootnotePrepareOptions,
    page_markdown: str,
    layout_profile: Mapping[str, Any],
) -> Optional[dict[str, Any]]:
    """Turn a geometry recall miss into a review-only candidate."""
    if native_text_source:
        candidate = candidate_for_native_block(
            page_number,
            block_index,
            block,
            sidecar,
            bottom_ratio=options.bottom_ratio,
            bottom_intersection_ratio=options.bottom_intersection_ratio,
            page_markdown=page_markdown,
            auto_accept=False,
            native_max_font_ratio=options.native_max_font_ratio,
            layout_profile=layout_profile,
            region_evidence=True,
        )
    else:
        candidate = candidate_for_block(
            page_number,
            block_index,
            block,
            sidecar,
            bottom_ratio=options.bottom_ratio,
            bottom_intersection_ratio=options.bottom_intersection_ratio,
            force_review=True,
            source="primary",
            region_evidence=True,
            page_markdown=page_markdown,
            layout_profile=layout_profile,
        )
    if candidate is None:
        return None
    candidate["audit_only"] = True
    candidate["confidence"] = "review"
    candidate["disposition"] = "review_required"
    candidate["review_reason"] = "suspected_missed_bottom_numeric_block"
    return candidate


def _scan_primary_candidates(
    page_number: int,
    sidecar: Mapping[str, Any],
    *,
    options: FootnotePrepareOptions,
    native_text_source: bool,
    page_markdown: str,
    layout_profile: Mapping[str, Any],
    previous_page_footnote_evidence: bool,
    previous_page_context: Optional[Mapping[str, Any]],
    structure_excluded_blocks: set[int],
    native_font_profile: Mapping[str, Any],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    dict[str, Any],
    int,
]:
    """Build primary candidates, continuation context, and audit misses."""
    candidates: list[dict[str, Any]] = []
    blocks = sidecar.get("blocks", [])
    native_font_excluded = (
        native_body_font_excluded_blocks(
            sidecar,
            native_font_profile,
            bottom_ratio=options.bottom_ratio,
        )
        if native_text_source
        else set()
    )
    excluded_block_indexes = set(structure_excluded_blocks) | native_font_excluded
    for block_index, block in enumerate(blocks):
        if not isinstance(block, Mapping) or block_index in excluded_block_indexes:
            continue
        if native_text_source:
            candidate = candidate_for_native_block(
                page_number,
                block_index,
                block,
                sidecar,
                bottom_ratio=options.bottom_ratio,
                bottom_intersection_ratio=options.bottom_intersection_ratio,
                page_markdown=page_markdown,
                auto_accept=options.auto_accept,
                native_max_font_ratio=options.native_max_font_ratio,
                layout_profile=layout_profile,
            )
        else:
            local_acceptance_disabled = not options.auto_accept
            candidate = candidate_for_block(
                page_number,
                block_index,
                block,
                sidecar,
                bottom_ratio=options.bottom_ratio,
                bottom_intersection_ratio=options.bottom_intersection_ratio,
                force_review=local_acceptance_disabled,
                source="primary",
                page_markdown=page_markdown,
                layout_profile=layout_profile,
            )
            if candidate is not None and local_acceptance_disabled:
                candidate["review_reason"] = "local_auto_accept_disabled"
        if candidate is not None:
            candidates.append(candidate)

    candidates, continuous_regions = expand_continuous_region_candidates(
        page_number,
        sidecar,
        candidates,
        bottom_ratio=options.bottom_ratio,
        bottom_intersection_ratio=options.bottom_intersection_ratio,
        native_text_source=native_text_source,
        page_markdown=page_markdown,
        auto_accept=options.auto_accept,
        native_max_font_ratio=options.native_max_font_ratio,
        layout_profile=layout_profile,
        previous_page_footnote_evidence=previous_page_footnote_evidence,
        excluded_block_indexes=excluded_block_indexes,
    )
    continuation_evidence = annotate_cross_page_continuations(
        sidecar,
        candidates,
        continuous_regions,
        previous_page_context,
    )
    candidates, suppressed = suppress_soft_continuation_candidates(candidates)
    candidates.sort(key=lambda item: (item["order"], item["block"]))
    candidate_indexes = {
        int(index)
        for candidate in candidates
        for index in candidate_block_indexes(candidate)
    }
    bottom_stats = bottom_detection_stats(
        sidecar,
        candidate_indexes,
        bottom_ratio=options.bottom_ratio,
        excluded_block_indexes=excluded_block_indexes,
    )
    audit_candidates = [
        candidate
        for missed_index in suspected_missed_block_indexes(
            sidecar,
            candidate_indexes,
            bottom_ratio=options.bottom_ratio,
            bottom_intersection_ratio=options.bottom_intersection_ratio,
            excluded_block_indexes=excluded_block_indexes,
        )
        if (
            candidate := _candidate_for_audit_block(
                page_number,
                missed_index,
                sidecar["blocks"][missed_index],
                sidecar,
                native_text_source=native_text_source,
                options=options,
                page_markdown=page_markdown,
                layout_profile=layout_profile,
            )
        )
    ]
    return (
        candidates,
        suppressed,
        audit_candidates,
        continuous_regions,
        continuation_evidence,
        bottom_stats,
        len(suppressed),
    )


def _scan_secondary_candidates(
    page_number: int,
    secondary: Mapping[str, Any],
    *,
    options: FootnotePrepareOptions,
    page_markdown: str,
    structure_exclusion: Mapping[str, Any],
    previous_page_footnote_evidence: bool,
    previous_page_context: Optional[Mapping[str, Any]],
) -> tuple[
    list[dict[str, Any]],
    list[dict[str, Any]],
    dict[str, Any],
    set[int],
]:
    """Build gated secondary candidates and compare them with primary later."""
    layout_profile = page_layout_profile(secondary)
    excluded_blocks = structure_excluded_block_indexes(
        page_markdown,
        secondary,
        page_number,
        structure_exclusion,
    )
    candidates: list[dict[str, Any]] = []
    for block_index, block in enumerate(secondary.get("blocks", [])):
        if not isinstance(block, Mapping) or block_index in excluded_blocks:
            continue
        candidate = candidate_for_block(
            page_number,
            block_index,
            block,
            secondary,
            bottom_ratio=options.bottom_ratio,
            bottom_intersection_ratio=options.bottom_intersection_ratio,
            source="secondary",
            layout_profile=layout_profile,
        )
        if candidate is not None:
            candidates.append(candidate)
    candidates, regions = expand_continuous_region_candidates(
        page_number,
        secondary,
        candidates,
        bottom_ratio=options.bottom_ratio,
        bottom_intersection_ratio=options.bottom_intersection_ratio,
        native_text_source=False,
        page_markdown="",
        auto_accept=False,
        native_max_font_ratio=options.native_max_font_ratio,
        layout_profile=layout_profile,
        previous_page_footnote_evidence=previous_page_footnote_evidence,
        excluded_block_indexes=excluded_blocks,
    )
    continuation_evidence = annotate_cross_page_continuations(
        secondary,
        candidates,
        regions,
        previous_page_context,
    )
    candidates.sort(key=lambda item: (item["order"], item["block"]))
    return candidates, regions, continuation_evidence, excluded_blocks


def scan_footnote_page(
    output_dir: Path,
    sidecar_path: Path,
    *,
    options: FootnotePrepareOptions,
    native_text_source: bool,
    evidence_mode: str,
    legacy_consensus: bool,
    structure_units: list[dict[str, Any]],
    state: FootnoteScanState,
    native_font_profile: Optional[Mapping[str, Any]] = None,
) -> FootnotePageScanResult:
    """Scan a primary sidecar and update adjacent-page continuation state."""
    sidecar = load_layout_sidecar(sidecar_path)
    try:
        page_number = int(sidecar.get("page_number"))
    except (TypeError, ValueError) as exc:
        raise ValueError(f"OCR sidecar has invalid page_number: {sidecar_path}") from exc
    relative_sidecar = sidecar_path.relative_to(output_dir).as_posix()
    sidecar_hash = (relative_sidecar, sha256_file(sidecar_path))
    secondary_sidecar = Path(output_dir) / "ocr_secondary" / sidecar_path.name
    secondary_hash = None
    if evidence_mode == "two_ocr" and secondary_sidecar.is_file():
        secondary_hash = (
            secondary_sidecar.relative_to(output_dir).as_posix(),
            sha256_file(secondary_sidecar),
        )

    structure_exclusion = structure_exclusion_for_page(structure_units, page_number)
    if options.mode != "footnotes":
        record = (
            {"page": page_number, **structure_exclusion}
            if structure_exclusion["roles"]
            else None
        )
        state.reset(page_number)
        return FootnotePageScanResult(
            page_number=page_number,
            page_report=None,
            structure_exclusion=record,
            sidecar_hash=sidecar_hash,
            secondary_sidecar_hash=secondary_hash,
            review_required=False,
            suspected_missed=False,
            soft_context_count=0,
            bottom_detection={
                "bottom_block_count": 0,
                "bottom_numeric_block_count": 0,
                "bottom_numeric_candidate_count": 0,
                "excluded_page_furniture_count": 0,
                "excluded_numeric_page_count": 0,
                "suspected_missed_count": 0,
            },
        )

    page_markdown_path = output_dir / "pages" / f"page_{page_number:03d}.md"
    try:
        page_markdown = page_markdown_path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        page_markdown = ""
    layout_profile = page_layout_profile(sidecar)
    excluded_blocks = structure_excluded_block_indexes(
        page_markdown,
        sidecar,
        page_number,
        structure_exclusion,
    )
    structure_record = (
        {
            "page": page_number,
            **structure_exclusion,
            "excluded_block_indexes": sorted(excluded_blocks),
        }
        if structure_exclusion["roles"]
        else None
    )
    (
        candidates,
        suppressed,
        audit_candidates,
        continuous_regions,
        continuation_evidence,
        bottom_stats,
        soft_count,
    ) = (
        _scan_primary_candidates(
            page_number,
            sidecar,
            options=options,
            native_text_source=native_text_source,
            page_markdown=page_markdown,
            layout_profile=layout_profile,
            previous_page_footnote_evidence=(
                state.page_number == page_number - 1 and state.footnote_evidence
            ),
            previous_page_context=(
                state.page_context if state.page_number == page_number - 1 else None
            ),
            structure_excluded_blocks=excluded_blocks,
            native_font_profile=native_font_profile or {},
        )
    )

    secondary_candidates: list[dict[str, Any]] = []
    secondary_regions: list[dict[str, Any]] = []
    secondary_continuation_evidence: dict[str, Any] = {
        "mode": "gated",
        "candidate_count": 0,
        "high_confidence_count": 0,
        "medium_confidence_count": 0,
        "low_confidence_count": 0,
    }
    secondary = None
    candidate_disagreement = False
    secondary_only_candidates: list[dict[str, Any]] = []
    if evidence_mode == "two_ocr":
        secondary = load_layout_sidecar(secondary_sidecar)
        (
            secondary_candidates,
            secondary_regions,
            secondary_continuation_evidence,
            _secondary_excluded_blocks,
        ) = _scan_secondary_candidates(
            page_number,
            secondary,
            options=options,
            page_markdown=page_markdown,
            structure_exclusion=structure_exclusion,
            previous_page_footnote_evidence=(
                state.secondary_page_number == page_number - 1
                and state.secondary_footnote_evidence
            ),
            previous_page_context=(
                state.secondary_page_context
                if state.secondary_page_number == page_number - 1
                else None
            ),
        )
        matched_secondary_blocks = {
            int(candidate["block"])
            for candidate in secondary_candidates
            if any(candidates_match(primary, candidate) for primary in candidates)
        }
        secondary_only_candidates = aggregate_secondary_only_candidates(
            [
                candidate
                for candidate in secondary_candidates
                if int(candidate["block"]) not in matched_secondary_blocks
            ]
        )
        candidate_disagreement = bool(secondary_only_candidates) or (
            bool(candidates) != bool(secondary_candidates)
        )
        if candidate_disagreement:
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

    footnote_regions = build_page_footnote_regions(
        page_number,
        sidecar,
        candidates,
        audit_candidates,
        bottom_ratio=options.bottom_ratio,
        auto_accept=options.auto_accept,
        continuous_regions=continuous_regions,
        context_candidates=suppressed,
        ocr_conflict=candidate_disagreement,
    )

    review_required = candidate_disagreement or any(
        region.get("disposition") == "visual_audit_required"
        for region in footnote_regions
    )
    should_emit_page = bool(candidates) or bool(audit_candidates) or (
        evidence_mode == "two_ocr" and bool(secondary_candidates)
    )
    page_report = None
    if should_emit_page:
        legacy_secondary = legacy_consensus and secondary_sidecar.is_file()
        page_report = {
            "page": page_number,
            "sidecar": relative_sidecar,
            "secondary_sidecar": (
                secondary_sidecar.relative_to(output_dir).as_posix()
                if evidence_mode == "two_ocr" or legacy_secondary
                else None
            ),
            "consensus_visual_review": False,
            "consensus_status": (
                "disagree" if candidate_disagreement else "agree"
            ) if evidence_mode == "two_ocr" else None,
            "candidates": candidates,
            "suppressed_continuation_candidates": suppressed,
            "audit_candidates": audit_candidates,
            "secondary_candidates": secondary_candidates,
            "secondary_only_candidates": (
                secondary_only_candidates if evidence_mode == "two_ocr" else []
            ),
            "continuous_numeric_regions": continuous_regions,
            "continuation_evidence": continuation_evidence,
            "secondary_continuous_numeric_regions": (
                secondary_regions if evidence_mode == "two_ocr" else []
            ),
            "secondary_continuation_evidence": secondary_continuation_evidence,
            "footnote_regions": footnote_regions,
            "bottom_detection": bottom_stats,
            "window": window_for_page(
                page_number,
                sidecar,
                [*candidates, *suppressed, *audit_candidates],
                context_blocks=options.context_blocks,
            ),
            "secondary_window": (
                window_for_page(
                    page_number,
                    secondary,
                    secondary_candidates,
                    context_blocks=options.context_blocks,
                )
                if evidence_mode == "two_ocr"
                else None
            ),
        }

    state.page_number = page_number
    state.page_context = build_footnote_page_context(
        page_number,
        sidecar,
        candidates,
    )
    state.footnote_evidence = has_footnote_evidence(candidates)
    if evidence_mode == "two_ocr":
        state.secondary_page_number = page_number
        state.secondary_page_context = build_footnote_page_context(
            page_number,
            secondary,
            secondary_candidates,
        )
        state.secondary_footnote_evidence = has_footnote_evidence(
            secondary_candidates
        )

    return FootnotePageScanResult(
        page_number=page_number,
        page_report=page_report,
        structure_exclusion=structure_record,
        sidecar_hash=sidecar_hash,
        secondary_sidecar_hash=secondary_hash,
        review_required=review_required,
        suspected_missed=bool(bottom_stats.get("suspected_missed_count")),
        soft_context_count=soft_count,
        bottom_detection={key: int(value) for key, value in bottom_stats.items()},
    )


__all__ = [
    "FootnotePageScanResult",
    "FootnoteScanState",
    "scan_footnote_page",
]

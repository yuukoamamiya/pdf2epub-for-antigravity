"""Orchestrate footnote candidate preparation.

The implementation is intentionally a small coordinator.  Configuration,
current OCR evidence, page scanning, visual evidence, and report persistence
live in focused modules; this module exposes only the candidate-preparation
coordinator.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from .footnote_consensus import load_consensus_review_pages
from .footnote_options import (
    DEFAULT_AUTO_ACCEPT,
    DEFAULT_BOTTOM_INTERSECTION_RATIO,
    DEFAULT_BOTTOM_RATIO,
    DEFAULT_CONTEXT_BLOCKS,
    DEFAULT_NATIVE_MAX_FONT_RATIO,
    FOOTNOTE_MODES,
    FootnotePrepareOptions,
    resolve_footnote_options,
)
from .footnote_page_scan import FootnoteScanState, scan_footnote_page
from .footnote_font_evidence import native_font_profile
from .footnote_report import (
    FOOTNOTE_PREPARE_SCHEMA_VERSION,
    build_footnote_report,
)
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES, load_refined_units
from .pdf_evidence import (
    is_native_text_source,
    pdf_evidence_mode,
    require_current_consensus,
)


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
    """Create and persist the compact footnote candidate report."""
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
    # A real CLI call always supplies config.  The config-less path remains a
    # narrow compatibility mode for old library callers that have legacy
    # consensus artifacts but no current evidence contract.
    legacy_consensus = config is None
    if evidence_mode == "two_ocr":
        require_current_consensus(output_dir, config, stage="footnote")
    consensus_review_pages = (
        load_consensus_review_pages(output_dir)
        if not native_text_source
        and (legacy_consensus or evidence_mode == "two_ocr")
        else set()
    )

    page_reports: list[dict[str, Any]] = []
    sidecar_hashes: dict[str, str] = {}
    secondary_sidecar_hashes: dict[str, str] = {}
    review_pages: set[int] = set()
    suspected_missed_pages: set[int] = set()
    soft_continuation_context_pages: set[int] = set()
    soft_continuation_context_count = 0
    bottom_detection_totals = {
        "bottom_block_count": 0,
        "bottom_numeric_block_count": 0,
        "bottom_numeric_candidate_count": 0,
        "excluded_page_furniture_count": 0,
        "excluded_numeric_page_count": 0,
        "suspected_missed_count": 0,
        "suppressed_native_citation_count": 0,
    }
    structure_excluded_pages: list[dict[str, Any]] = []
    state = FootnoteScanState()
    refined_units = load_refined_units(output_dir)
    native_profile = (
        native_font_profile(sidecars, bottom_ratio=options.bottom_ratio)
        if native_text_source
        else {}
    )

    for sidecar_path in sidecars:
        result = scan_footnote_page(
            output_dir,
            sidecar_path,
            options=options,
            native_text_source=native_text_source,
            evidence_mode=evidence_mode,
            legacy_consensus=legacy_consensus,
            structure_units=refined_units,
            native_font_profile=native_profile,
            state=state,
        )
        sidecar_name, sidecar_hash = result.sidecar_hash
        sidecar_hashes[sidecar_name] = sidecar_hash
        if result.secondary_sidecar_hash is not None:
            secondary_name, secondary_hash = result.secondary_sidecar_hash
            secondary_sidecar_hashes[secondary_name] = secondary_hash
        if result.structure_exclusion is not None:
            structure_excluded_pages.append(result.structure_exclusion)
        if result.page_report is not None:
            result.page_report["consensus_visual_review"] = (
                result.page_number in consensus_review_pages
            )
            page_reports.append(result.page_report)
        if result.review_required:
            review_pages.add(result.page_number)
        if result.suspected_missed:
            suspected_missed_pages.add(result.page_number)
        if result.soft_context_count:
            soft_continuation_context_pages.add(result.page_number)
            soft_continuation_context_count += result.soft_context_count
        for field, value in result.bottom_detection.items():
            bottom_detection_totals[field] += int(value)

    return build_footnote_report(
        output_dir,
        source_kind=source_kind,
        evidence_mode=evidence_mode,
        footnote_mode=options.mode,
        bottom_ratio=options.bottom_ratio,
        bottom_intersection_ratio=options.bottom_intersection_ratio,
        context_blocks=options.context_blocks,
        auto_accept=options.auto_accept,
        native_max_font_ratio=options.native_max_font_ratio,
        consensus_review_pages=consensus_review_pages,
        sidecar_hashes=sidecar_hashes,
        secondary_sidecar_hashes=secondary_sidecar_hashes,
        page_reports=page_reports,
        review_pages=review_pages,
        soft_continuation_context_count=soft_continuation_context_count,
        soft_continuation_context_pages=soft_continuation_context_pages,
        bottom_detection=bottom_detection_totals,
        suspected_missed_pages=suspected_missed_pages,
        structure_excluded_pages=structure_excluded_pages,
        native_font_profile=native_profile,
        config=config,
    )

__all__ = [
    "DEFAULT_BOTTOM_RATIO",
    "FootnotePrepareOptions",
    "FOOTNOTE_PREPARE_SCHEMA_VERSION",
    "FOOTNOTE_STRUCTURE_EXEMPT_TYPES",
    "prepare_footnote_candidates",
    "resolve_footnote_options",
]

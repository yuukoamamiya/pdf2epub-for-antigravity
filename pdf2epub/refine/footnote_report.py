"""Aggregate scanned footnote pages and persist the candidate report."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..workflow_contracts import atomic_write_json
from .footnote_continuation import (
    _aggregate_continuation_evidence as aggregate_continuation_evidence,
)
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES
from .footnote_visual import (
    ensure_region_review_images,
    ensure_visual_review_images,
    write_visual_audit_manifest,
)
from .footnote_windows import build_cross_page_windows


def _attach_visual_evidence(
    output_dir: Path,
    page_reports: list[dict[str, Any]],
    review_pages: set[int],
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach reusable page/crop image paths and write the audit queue."""
    visual_evidence = ensure_visual_review_images(output_dir, review_pages, config)
    visual_files = visual_evidence.get("files", {})
    if isinstance(visual_files, Mapping):
        for page_report in page_reports:
            page_report["visual_file"] = visual_files.get(str(page_report["page"]))

    region_visual_files = ensure_region_review_images(output_dir, page_reports, config)
    for page_report in page_reports:
        for region in page_report.get("footnote_regions", []) or []:
            if isinstance(region, Mapping):
                region["region_visual_file"] = region_visual_files.get(
                    str(region.get("region_id"))
                )

    visual_audit_path, visual_audit_manifest = write_visual_audit_manifest(
        output_dir, page_reports
    )
    return {
        "visual_evidence": visual_evidence,
        "visual_audit_path": visual_audit_path,
        "visual_audit_manifest": visual_audit_manifest,
    }


def build_footnote_report(
    output_dir: Path,
    *,
    source_kind: str,
    evidence_mode: str,
    footnote_mode: str,
    configured_footnote_mode: str,
    footnote_mode_source: str,
    bottom_ratio: float,
    bottom_intersection_ratio: float,
    context_blocks: int,
    auto_accept: bool,
    native_max_font_ratio: float,
    consensus_review_pages: Iterable[int],
    sidecar_hashes: Mapping[str, str],
    secondary_sidecar_hashes: Mapping[str, str],
    page_reports: list[dict[str, Any]],
    review_pages: set[int],
    soft_continuation_context_count: int,
    soft_continuation_context_pages: set[int],
    bottom_detection: Mapping[str, int],
    suspected_missed_pages: set[int],
    structure_excluded_pages: list[dict[str, Any]],
    native_font_profile: Optional[Mapping[str, Any]],
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach final evidence, summarize counts, and atomically write JSON."""
    page_reports.sort(key=lambda item: item["page"])
    cross_page_windows = build_cross_page_windows(page_reports)
    for window in cross_page_windows:
        review_pages.update(int(page) for page in window["pages"])

    visual = _attach_visual_evidence(
        output_dir,
        page_reports,
        review_pages,
        config,
    )
    visual_audit_manifest = visual["visual_audit_manifest"]
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
        "footnote_mode": footnote_mode,
        "configured_footnote_mode": configured_footnote_mode,
        "footnote_mode_source": footnote_mode_source,
        "bottom_ratio": bottom_ratio,
        "bottom_intersection_ratio": bottom_intersection_ratio,
        "context_blocks": context_blocks,
        "auto_accept": auto_accept,
        "native_max_font_ratio": native_max_font_ratio,
        "native_font_profile": dict(native_font_profile or {}),
        "consensus_visual_review_pages": sorted({int(page) for page in consensus_review_pages}),
        "sidecar_sha256": dict(sidecar_hashes),
        "secondary_sidecar_sha256": dict(secondary_sidecar_hashes),
        "pages": page_reports,
        "cross_page_windows": cross_page_windows,
        "continuation_evidence_summary": aggregate_continuation_evidence(page_reports),
        "review_pages": sorted(review_pages),
        "high_confidence_candidate_count": high_confidence_count,
        "review_candidate_count": review_count,
        "soft_continuation_context_count": soft_continuation_context_count,
        "soft_continuation_context_pages": sorted(soft_continuation_context_pages),
        "bottom_detection": dict(bottom_detection),
        "suspected_missed_count": int(bottom_detection.get("suspected_missed_count", 0)),
        "suspected_missed_pages": sorted(suspected_missed_pages),
        "structure_excluded_pages": structure_excluded_pages,
        "structure_exempt_unit_types": sorted(FOOTNOTE_STRUCTURE_EXEMPT_TYPES),
        "visual_evidence": visual["visual_evidence"],
        "visual_audit_manifest": visual_audit_manifest["path"]
        if "path" in visual_audit_manifest
        else visual["visual_audit_path"].name,
        "visual_audit_task_count": visual_audit_manifest["task_count"],
        "review_required": bool(review_pages),
    }
    atomic_write_json(output_dir / "footnote_candidates.json", report)
    return report


__all__ = ["FOOTNOTE_PREPARE_SCHEMA_VERSION", "build_footnote_report"]

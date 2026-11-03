"""Aggregate scanned footnote pages and persist the candidate report."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..workflow_contracts import atomic_write_json
from ..workflow_contracts import sha256_file
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
from .footnote_windows import build_cross_page_windows, build_missing_continuation_reviews
from .footnote_options import footnote_mode_warnings


def _attach_visual_evidence(
    output_dir: Path,
    page_reports: list[dict[str, Any]],
    review_pages: set[int],
    config: Optional[Mapping[str, Any]],
    continuation_reviews: list[dict[str, Any]],
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

    for review in continuation_reviews:
        files = {}
        for page in review["pages"]:
            path = visual_files.get(str(page)) if isinstance(visual_files, Mapping) else None
            if path:
                file_path = output_dir / str(path)
                files[str(page)] = {
                    "path": str(path),
                    "sha256": sha256_file(file_path) if file_path.is_file() else None,
                }
        review["visual_files"] = files
        image_manifest_path = output_dir / "ocr_review_images.json"
        if image_manifest_path.is_file():
            try:
                import json
                image_manifest = json.loads(image_manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                image_manifest = {}
            review["visual_image_manifest_sha256"] = sha256_file(image_manifest_path)
            review["source_pdf_sha256"] = (
                image_manifest.get("source_sha256") if isinstance(image_manifest, Mapping) else None
            )
    visual_audit_path, visual_audit_manifest = write_visual_audit_manifest(
        output_dir, page_reports, continuation_reviews
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
    page_count = 0
    try:
        import json
        probe = json.loads((Path(output_dir) / "pdf_text_probe.json").read_text(encoding="utf-8"))
        page_count = int(probe.get("page_count", 0)) if isinstance(probe, Mapping) else 0
        if page_count <= 0:
            raise ValueError("probe did not contain a usable PDF page count")
    except (OSError, ValueError, TypeError):
        page_count = max(
            (int(match.group(1)) for name in sidecar_hashes if (match := re.search(r"page_(\d+)", str(name)))),
            default=max((int(item.get("page", 0)) for item in page_reports), default=0),
        )
    continuation_reviews = (
        build_missing_continuation_reviews(page_reports, page_count=page_count)
        if footnote_mode != "disabled"
        else []
    )
    for window in cross_page_windows:
        review_pages.update(int(page) for page in window["pages"])
    for review in continuation_reviews:
        review_pages.update(int(page) for page in review["pages"])

    visual = _attach_visual_evidence(
        output_dir,
        page_reports,
        review_pages,
        config,
        continuation_reviews,
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
        "warnings": footnote_mode_warnings(
            output_dir,
            config,
            resolution={
                "configured": configured_footnote_mode,
                "effective": footnote_mode,
                "source": footnote_mode_source,
            },
        ),
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
        "continuation_reviews": continuation_reviews,
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

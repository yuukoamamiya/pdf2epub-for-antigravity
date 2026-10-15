"""Prepare visual evidence for ambiguous footnote regions.

This module owns only the visual side of the footnote hand-off: rendering or
reusing full-page review images, creating narrow region crops, and persisting
the region-scoped audit manifest. Candidate detection and decision validation
stay in their respective modules so image generation cannot influence the
semantic classification of a block.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from ..workflow_contracts import atomic_write_json
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION


def ensure_visual_review_images(
    output_dir: Path,
    review_pages: Iterable[int],
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Render or reuse full-page PNGs for pages sent to the Subagent.

    OCR correction already owns freshness-aware PDF page rendering. Reusing
    that renderer keeps image hashes and source-PDF selection consistent
    across workflow stages. A sidecar-only library caller remains valid when
    no source PDF is available; it receives an explicit unavailable record.
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


def ensure_region_review_images(
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


def write_visual_audit_manifest(
    output_dir: Path,
    page_reports: Iterable[Mapping[str, Any]],
) -> tuple[Path, dict[str, Any]]:
    """Persist the small, region-scoped visual audit queue."""
    tasks: list[dict[str, Any]] = []
    for report in page_reports:
        page_visual = report.get("visual_file")
        for region in report.get("footnote_regions", []) or []:
            if (
                not isinstance(region, Mapping)
                or region.get("disposition") != "visual_audit_required"
            ):
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
    atomic_write_json(path, manifest)
    return path, manifest


# Private aliases keep the naming stable for older callers while the public
# module API uses verbs that describe these functions' side effects.
_ensure_visual_review_images = ensure_visual_review_images
_ensure_region_review_images = ensure_region_review_images
_write_visual_audit_manifest = write_visual_audit_manifest


__all__ = [
    "ensure_region_review_images",
    "ensure_visual_review_images",
    "write_visual_audit_manifest",
]

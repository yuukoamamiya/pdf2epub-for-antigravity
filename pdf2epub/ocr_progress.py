"""Durable OCR progress and completeness checks.

The OCR stage is page-addressable, so its checkpoint must prove more than the
presence of a few Markdown files.  This module keeps the completion contract
shared by the OCR runner and the later PDF workflow gates.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Optional


OCR_PROGRESS_SCHEMA_VERSION = 2


def _page_list(value: Any, total_pages: int) -> list[int]:
    """Normalize a page list, dropping malformed or out-of-range values."""
    if not isinstance(value, (list, tuple, set)):
        return []
    return sorted(
        {
            int(page)
            for page in value
            if isinstance(page, int)
            and not isinstance(page, bool)
            and 1 <= int(page) <= total_pages
        }
    )


def new_progress(
    *,
    source_sha256: str,
    total_pages: int,
    backend: str,
    mode: str = "ocr",
) -> Dict[str, Any]:
    """Create a v2 OCR checkpoint for one source PDF."""
    total_pages = max(0, int(total_pages))
    return {
        "schema_version": OCR_PROGRESS_SCHEMA_VERSION,
        "mode": mode,
        "backend": backend,
        "source_sha256": str(source_sha256),
        "total_pages": total_pages,
        "pages_processed": [],
        "failed_pages": [],
        "empty_pages": [],
        "allowed_empty_pages": [],
        "missing_pages": list(range(1, total_pages + 1)),
        "global_image_counter": 0,
    }


def normalize_progress(progress: Dict[str, Any], total_pages: int) -> Dict[str, Any]:
    """Normalize a loaded v2 checkpoint without treating it as complete."""
    normalized = dict(progress)
    normalized["schema_version"] = int(progress.get("schema_version", 0) or 0)
    normalized["total_pages"] = int(progress.get("total_pages", total_pages) or 0)
    normalized["pages_processed"] = _page_list(
        progress.get("pages_processed"), total_pages
    )
    normalized["failed_pages"] = _page_list(progress.get("failed_pages"), total_pages)
    normalized["empty_pages"] = _page_list(progress.get("empty_pages"), total_pages)
    normalized["allowed_empty_pages"] = _page_list(
        progress.get("allowed_empty_pages"), total_pages
    )
    try:
        normalized["global_image_counter"] = int(
            progress.get("global_image_counter", 0) or 0
        )
    except (TypeError, ValueError):
        normalized["global_image_counter"] = 0
    return normalized


def load_progress(path: Path) -> Optional[Dict[str, Any]]:
    """Load a progress object, returning ``None`` for missing/corrupt JSON."""
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return value if isinstance(value, dict) else None


def assess_progress(
    pages_dir: Path,
    *,
    expected_total_pages: Optional[int] = None,
    expected_source_sha256: Optional[str] = None,
    require_sidecars: bool = False,
) -> Dict[str, Any]:
    """Assess whether the OCR page set is safe for downstream processing."""
    pages_dir = Path(pages_dir)
    progress_path = pages_dir / "ocr_progress.json"
    progress = load_progress(progress_path)
    errors: list[str] = []

    if progress is None:
        return {
            "ready": False,
            "errors": ["ocr_progress.json is missing or invalid"],
            "progress": {},
            "available_pages": [],
            "processed_pages": [],
            "failed_pages": [],
            "missing_pages": [],
            "empty_pages": [],
            "unacknowledged_empty_pages": [],
        }

    raw_total = progress.get("total_pages")
    try:
        total_pages = int(raw_total)
    except (TypeError, ValueError):
        total_pages = 0
    if total_pages <= 0:
        errors.append("OCR progress does not record a positive total_pages")

    if progress.get("schema_version") != OCR_PROGRESS_SCHEMA_VERSION:
        errors.append(
            "OCR progress uses an old schema; rerun ocr-pages --resume to rebuild it"
        )

    if expected_total_pages is not None and total_pages != int(expected_total_pages):
        errors.append(
            f"OCR page count mismatch: progress={total_pages}, "
            f"source={int(expected_total_pages)}"
        )

    if expected_source_sha256:
        recorded_source = str(progress.get("source_sha256") or "")
        if recorded_source != str(expected_source_sha256):
            errors.append("OCR progress does not match the current source PDF")

    available = []
    for path in pages_dir.glob("page_*.md"):
        try:
            number = int(path.stem.split("_")[-1])
        except (ValueError, IndexError):
            continue
        if number >= 1:
            available.append(number)
    available = sorted(set(available))

    processed = _page_list(progress.get("pages_processed"), max(total_pages, 0))
    failed = _page_list(progress.get("failed_pages"), max(total_pages, 0))
    empty = _page_list(progress.get("empty_pages"), max(total_pages, 0))
    allowed_empty = _page_list(
        progress.get("allowed_empty_pages"), max(total_pages, 0)
    )
    unacknowledged_empty = sorted(set(empty) - set(allowed_empty))
    missing = (
        sorted(set(range(1, total_pages + 1)) - set(processed))
        if total_pages > 0
        else []
    )

    if total_pages > 0 and available != list(range(1, total_pages + 1)):
        errors.append("OCR Markdown pages are missing or non-contiguous")
    if processed != available:
        errors.append("OCR progress does not match the available Markdown pages")
    if failed:
        errors.append(f"OCR has failed pages: {failed[:10]}")
    if missing:
        errors.append(f"OCR has unprocessed pages: {missing[:10]}")
    if unacknowledged_empty:
        errors.append(
            "OCR has empty pages requiring review: "
            f"{unacknowledged_empty[:10]}"
        )

    if require_sidecars and progress.get("mode", "ocr") != "native_text":
        missing_sidecars = [
            page
            for page in available
            if not (pages_dir / f"page_{page:03d}.ocr.json").is_file()
        ]
        if missing_sidecars:
            errors.append(
                "OCR sidecars are missing for page(s): "
                f"{missing_sidecars[:10]}"
            )
        invalid_sidecars = []
        for page in available:
            sidecar_path = pages_dir / f"page_{page:03d}.ocr.json"
            if not sidecar_path.is_file():
                continue
            try:
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                invalid_sidecars.append(page)
                continue
            if not isinstance(sidecar, dict) or sidecar.get("page_number") != page:
                invalid_sidecars.append(page)
        if invalid_sidecars:
            errors.append(
                "OCR sidecars are invalid for page(s): "
                f"{invalid_sidecars[:10]}"
            )

    return {
        "ready": not errors,
        "errors": errors,
        "progress": progress,
        "available_pages": available,
        "processed_pages": processed,
        "failed_pages": failed,
        "missing_pages": missing,
        "empty_pages": empty,
        "unacknowledged_empty_pages": unacknowledged_empty,
    }


__all__ = [
    "OCR_PROGRESS_SCHEMA_VERSION",
    "assess_progress",
    "load_progress",
    "new_progress",
    "normalize_progress",
]

"""Shared source-stage selection for PDF command workflows."""

import hashlib
import json
from pathlib import Path


def _resolve_pdf_markdown_source(output_dir: Path, config: dict):
    """Choose the Markdown stage shared by PDF workflows.

    Polished Markdown is the default source for PDF work.  ``auto`` remains
    available for diagnostic/legacy source inspection, but translation and
    readiness gates reject the OCR fallback when polishing is missing.
    """
    translation = config.get("translation", {}) or {}
    requested_stage = str(translation.get("source_stage", "polished")).strip().lower()
    if requested_stage not in {"auto", "ocr", "polished"}:
        raise ValueError(
            "translation.source_stage must be one of: auto, ocr, polished"
        )

    polished_dir = output_dir / "polished_markdown" / "validated"
    ocr_dir = output_dir / "ocr_markdown"
    polished_available = _polished_stage_is_current(output_dir, polished_dir, ocr_dir)

    if requested_stage == "polished":
        return polished_dir, "polished"
    if requested_stage == "ocr":
        return ocr_dir, "ocr"
    if polished_available:
        return polished_dir, "polished"
    return ocr_dir, "ocr"


def _polished_stage_is_current(
    output_dir: Path,
    polished_dir: Path,
    ocr_dir: Path,
) -> bool:
    """Reject a polished stage left behind after OCR/refine inputs changed."""
    if not polished_dir.is_dir() or not any(polished_dir.glob("*.md")):
        return False
    report_path = output_dir / "polish_validation.json"
    if not report_path.is_file():
        return False
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if not isinstance(report, dict) or not report.get("all_passed"):
        return False
    recorded_hashes = report.get("source_sha256")
    if not isinstance(recorded_hashes, dict):
        return False
    current_hashes = {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(ocr_dir.glob("*.md"))
        if path.is_file()
    }
    return bool(current_hashes) and current_hashes == recorded_hashes


__all__ = ["_polished_stage_is_current", "_resolve_pdf_markdown_source"]

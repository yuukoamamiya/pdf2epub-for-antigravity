"""Shared source-stage selection for PDF command workflows."""

import hashlib
import json
from pathlib import Path


def _resolve_pdf_markdown_source(output_dir: Path, config: dict):
    """Choose the Markdown stage shared by PDF workflows.

    Polished Markdown is the source for every PDF workflow. A high-confidence
    native-text PDF only bypasses visual OCR; its extracted layout still goes
    through the Subagent polish gate so visual line wraps can be distinguished
    from semantic paragraph breaks.
    """
    translation = config.get("translation", {}) or {}
    requested_stage = str(translation.get("source_stage", "auto")).strip().lower()
    if requested_stage not in {"auto", "ocr", "polished", "native_text"}:
        raise ValueError(
            "translation.source_stage must be one of: auto, ocr, polished, native_text"
        )

    polished_dir = output_dir / "polished_markdown" / "validated"
    ocr_dir = output_dir / "ocr_markdown"
    polished_available = _polished_stage_is_current(output_dir, polished_dir, ocr_dir)

    if requested_stage == "polished":
        return polished_dir, "polished"
    if requested_stage == "ocr":
        return ocr_dir, "ocr"
    # ``native_text`` is retained as a backwards-compatible configuration
    # alias for the raw native extraction.  It must not bypass polishing:
    # native PDF text has visual lines but generally no semantic paragraph
    # boundaries.
    if requested_stage == "native_text":
        return ocr_dir, "ocr"
    if polished_available:
        return polished_dir, "polished"
    return ocr_dir, "ocr"


def _native_text_stage_is_current(output_dir: Path, pages_dir: Path) -> bool:
    """Return whether native text extraction produced the current page set."""
    probe_path = output_dir / "pdf_text_probe.json"
    progress_path = pages_dir / "ocr_progress.json"
    if not probe_path.is_file() or not progress_path.is_file():
        return False
    try:
        probe = json.loads(probe_path.read_text(encoding="utf-8"))
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return False
    if (
        probe.get("recommendation") != "use_text_layer"
        or probe.get("classification") != "native_text"
        or progress.get("mode") != "native_text"
    ):
        return False
    original_pdf = output_dir / "input_original.pdf"
    if original_pdf.is_file() and probe.get("source_sha256"):
        try:
            current_hash = hashlib.sha256(original_pdf.read_bytes()).hexdigest()
        except OSError:
            return False
        if current_hash != probe.get("source_sha256"):
            return False
    return bool(list(pages_dir.glob("page_*.md"))) and not progress.get("failed_pages")


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


__all__ = [
    "_native_text_stage_is_current",
    "_polished_stage_is_current",
    "_resolve_pdf_markdown_source",
]

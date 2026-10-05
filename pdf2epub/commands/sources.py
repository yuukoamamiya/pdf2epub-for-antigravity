"""Shared source-stage selection for PDF command workflows."""

import hashlib
import json
from pathlib import Path

from pdf2epub.ocr_progress import assess_progress
from pdf2epub.refine.footnote_apply import footnote_normalization_is_current
from pdf2epub.validation_receipts import validation_receipt_is_current


def _resolve_pdf_polish_source(output_dir: Path, config: dict | None = None):
    """Choose the source that the PDF polish gate must inspect.

    A validated footnote-normalized stage is preferred when present.  The
    fallback keeps older runs usable, while the recommended workflow can run
    ``footnote-apply`` before ``polish`` to remove page-level note disruption.
    """
    output_dir = Path(output_dir)
    normalized_dir = output_dir / "footnote_normalized"
    if footnote_normalization_is_current(output_dir, config=config):
        return normalized_dir, "footnote_normalized"
    return output_dir / "ocr_markdown", "ocr"


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
    polish_input_dir, _polish_input_stage = _resolve_pdf_polish_source(output_dir, config)
    ocr_dir = output_dir / "ocr_markdown"
    polished_available = _polished_stage_is_current(
        output_dir, polished_dir, polish_input_dir
    )

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
    except (OSError, json.JSONDecodeError):
        return False
    if (
        probe.get("recommendation") != "use_text_layer"
        or probe.get("classification") != "native_text"
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
    report = assess_progress(
        pages_dir,
        expected_total_pages=probe.get("page_count"),
        expected_source_sha256=probe.get("source_sha256"),
        require_sidecars=False,
    )
    return report["progress"].get("mode") == "native_text" and report["ready"]


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
    return validation_receipt_is_current(
        report_path,
        ocr_dir,
        polished_dir,
        task="polish",
    )


__all__ = [
    "_native_text_stage_is_current",
    "_resolve_pdf_polish_source",
    "_polished_stage_is_current",
    "_resolve_pdf_markdown_source",
]

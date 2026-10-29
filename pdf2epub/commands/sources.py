"""Shared source-stage selection for PDF command workflows."""

from pathlib import Path

from pdf2epub.refine.footnote_status import footnote_normalization_is_current
from pdf2epub.validation_receipts import validation_receipt_is_current


def _resolve_pdf_polish_source(output_dir: Path, config: dict | None = None):
    """Choose the source that the PDF polish gate must inspect.

    A validated footnote-normalized stage is preferred when present.  The
    fallback keeps older runs usable, while the recommended workflow can run
    ``footnote-apply`` before ``polish`` to remove page-level note disruption.
    """
    output_dir = Path(output_dir)
    _require_reviewed_ocr_anomalies(output_dir, config)
    normalized_dir = output_dir / "footnote_normalized"
    if footnote_normalization_is_current(output_dir, config=config):
        return normalized_dir, "footnote_normalized"
    return output_dir / "ocr_markdown", "ocr"


def _resolve_pdf_markdown_source(output_dir: Path, config: dict):
    """Choose the Markdown stage shared by PDF workflows.

    Polished Markdown is the source for every new PDF workflow.  Legacy
    native-text runs still carry raw page layout that must pass the Subagent
    polish gate before translation.
    """
    translation = config.get("translation", {}) or {}
    _require_reviewed_ocr_anomalies(output_dir, config)
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


def _require_reviewed_ocr_anomalies(output_dir: Path, config: dict | None) -> None:
    """Prevent polished or translated stages bypassing pending raw-page anomalies."""
    pages_dir = Path(output_dir) / "pages"
    if not any(pages_dir.glob("page_*.md")):
        return
    from pdf2epub.ocr_source import is_legacy_native_source
    if is_legacy_native_source(output_dir):
        return
    from pdf2epub.ocr_anomalies import scan_ocr_pages
    from pdf2epub.ocr_correction import ocr_correction_is_current

    anomaly_report = scan_ocr_pages(output_dir, persist=False)
    anomalies = anomaly_report["review_required_files"]
    if anomaly_report.get("scan_errors"):
        raise ValueError("OCR anomaly scan could not read every raw page")
    if anomalies and not ocr_correction_is_current(output_dir, config):
        raise ValueError(
            "OCR commentary anomaly requires visual review before polish or downstream use: "
            + ", ".join(anomalies[:10])
        )
    if anomalies:
        from pdf2epub.ocr_correction import corrected_page_dir
        from pdf2epub.utils.fingerprints import pages_fingerprint

        progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
        try:
            import json

            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            current_fingerprint = pages_fingerprint(corrected_page_dir(output_dir))
            if progress.get("fingerprint", {}).get("pages_sha256") != current_fingerprint:
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            raise ValueError(
                "OCR anomaly correction is current but refined Markdown is stale; rerun refine-local before downstream use"
            )


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
    "_resolve_pdf_polish_source",
    "_polished_stage_is_current",
    "_require_reviewed_ocr_anomalies",
    "_resolve_pdf_markdown_source",
]

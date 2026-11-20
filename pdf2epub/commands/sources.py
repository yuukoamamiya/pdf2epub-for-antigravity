"""Shared source-stage selection for PDF command workflows."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Optional

from pdf2epub.refine.footnote_status import footnote_normalization_is_current
from pdf2epub.validation_receipts import validation_receipt_is_current
from pdf2epub.workflow_contracts import sha256_file


PDF_SOURCE_SIGNATURE = b"%PDF-"
_SOURCE_DIGEST_SCAN_BYTES = 1024


def validate_pdf_source(
    path: Path,
    *,
    output_dir: Optional[Path] = None,
    allow_source_change: bool = False,
) -> str:
    """Validate a PDF source before a write workflow binds it to a book.

    ``ocr-pages`` resolves its output directory from the configured book title,
    not from ``-i``.  Without this gate, pointing ``-i`` at another file (or at
    an EPUB/MOBI that PyMuPDF happily opens page-by-page) silently writes a
    probe and preprocessed copy into an unrelated book's workspace, replacing
    the source evidence every downstream checkpoint is bound to.

    Returns the SHA-256 of the accepted source.  Raises ``ValueError`` with an
    operator-facing message when the file is not a PDF or when it conflicts
    with the source already recorded for ``output_dir``.
    """
    candidate = Path(path)
    if not candidate.is_file():
        raise ValueError(f"input PDF does not exist: {candidate}")
    if candidate.suffix.lower() != ".pdf":
        raise ValueError(
            "ocr-pages requires a PDF input; "
            f"got {candidate.name!r} ({candidate.suffix or 'no suffix'}). "
            "PDF-only commands cannot consume EPUB/MOBI/AZW3 input."
        )
    with candidate.open("rb") as handle:
        head = handle.read(_SOURCE_DIGEST_SCAN_BYTES)
    if PDF_SOURCE_SIGNATURE not in head:
        raise ValueError(
            f"input is not a PDF: {candidate} does not start with a %PDF- header"
        )

    digest = sha256_file(candidate)
    if output_dir is None or allow_source_change:
        return digest

    recorded = _recorded_source_digests(Path(output_dir), candidate)
    conflicts = {
        label: value for label, value in recorded.items() if value and value != digest
    }
    if conflicts:
        details = ", ".join(f"{label}={value[:12]}…" for label, value in conflicts.items())
        raise ValueError(
            "input PDF does not match the source recorded for this book "
            f"({details}). Refusing to mix a different PDF into "
            f"{Path(output_dir)}. Use a separate title/output directory, or pass "
            "--allow-source-change if you intentionally replaced the source of "
            "this book (this invalidates existing OCR, footnote, polish, and "
            "translation checkpoints)."
        )
    return digest


def _recorded_source_digests(output_dir: Path, candidate: Path) -> dict[str, str]:
    """Return the source hashes already attested inside ``output_dir``."""
    recorded: dict[str, str] = {}
    progress = _load_json_mapping(output_dir / "pages" / "ocr_progress.json")
    probe = _load_json_mapping(output_dir / "pdf_text_probe.json")
    for label, payload in (("ocr_progress", progress), ("pdf_text_probe", probe)):
        value = payload.get("source_sha256") if payload else None
        if isinstance(value, str) and len(value) == 64:
            recorded[label] = value
    original = output_dir / "input_original.pdf"
    if original.is_file() and original.resolve() != candidate.resolve():
        recorded["input_original.pdf"] = sha256_file(original)
    return recorded


def _load_json_mapping(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _resolve_pdf_polish_source(
    output_dir: Path, config: dict | None = None, ocr_evidence=None
):
    """Choose the source that the PDF polish gate must inspect.

    A validated footnote-normalized stage is preferred when present.  The
    fallback keeps older runs usable, while the recommended workflow can run
    ``footnote-apply`` before ``polish`` to remove page-level note disruption.
    """
    output_dir = Path(output_dir)
    _require_reviewed_ocr_anomalies(output_dir, config, ocr_evidence)
    normalized_dir = output_dir / "footnote_normalized"
    if footnote_normalization_is_current(output_dir, config=config):
        return normalized_dir, "footnote_normalized"
    return output_dir / "ocr_markdown", "ocr"


def _resolve_pdf_markdown_source(
    output_dir: Path, config: dict, ocr_evidence=None
):
    """Choose the Markdown stage shared by PDF workflows.

    Polished Markdown is the source for every new PDF workflow.  Legacy
    native-text runs still carry raw page layout that must pass the Subagent
    polish gate before translation.
    """
    translation = config.get("translation", {}) or {}
    _require_reviewed_ocr_anomalies(output_dir, config, ocr_evidence)
    requested_stage = str(translation.get("source_stage", "auto")).strip().lower()
    if requested_stage not in {"auto", "ocr", "polished", "native_text"}:
        raise ValueError(
            "translation.source_stage must be one of: auto, ocr, polished, native_text"
        )

    polished_dir = output_dir / "polished_markdown" / "validated"
    polish_input_dir, _polish_input_stage = _resolve_pdf_polish_source(
        output_dir, config, ocr_evidence
    )
    ocr_dir = output_dir / "ocr_markdown"
    polished_available = _polished_stage_is_current(
        output_dir, polished_dir, polish_input_dir, config=config
    )

    if requested_stage == "polished":
        if not polished_available:
            raise ValueError(
                "translation.source_stage: polished requires a current polish validation receipt; run polish-validate"
            )
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
    if polished_dir.is_dir() and any(polished_dir.glob("*.md")):
        raise ValueError(
            "polished_markdown/validated exists but its validation receipt is missing "
            "or stale; rerun polish-validate (or set translation.source_stage: ocr to "
            "explicitly use raw OCR Markdown)"
        )
    return ocr_dir, "ocr"


def _require_reviewed_ocr_anomalies(
    output_dir: Path, config: dict | None, ocr_evidence=None
) -> None:
    """Prevent polished or translated stages bypassing pending raw-page anomalies."""
    pages_dir = Path(output_dir) / "pages"
    if not any(pages_dir.glob("page_*.md")):
        return
    from pdf2epub.ocr_review_selection import manual_review_selection_state

    selection = manual_review_selection_state(output_dir)
    from pdf2epub.ocr_source import is_legacy_native_source
    if is_legacy_native_source(output_dir):
        if selection["present"]:
            raise ValueError(
                "manual visual OCR selection cannot apply to a legacy native-text run"
            )
        return
    from pdf2epub.ocr_anomalies import evidence_report_for_output, scan_ocr_pages
    from pdf2epub.ocr_correction import ocr_correction_is_current

    anomaly_report = evidence_report_for_output(ocr_evidence, output_dir)
    if anomaly_report is None:
        anomaly_report = scan_ocr_pages(output_dir, persist=False)
    anomalies = anomaly_report["review_required_files"]
    if anomaly_report.get("scan_errors"):
        raise ValueError("OCR anomaly scan could not read every raw page")
    if selection["present"] and not selection["current"]:
        raise ValueError(selection["detail"])
    correction_required = bool(anomalies or selection["present"])
    if correction_required and not ocr_correction_is_current(
        output_dir, config, ocr_evidence=ocr_evidence
    ):
        if selection["present"] and not anomalies:
            raise ValueError(
                "manual OCR page review is missing, stale, or unvalidated before downstream use: "
                + ", ".join(selection["files"][:10])
            )
        raise ValueError(
            "OCR commentary anomaly requires visual review before polish or downstream use: "
            + ", ".join(anomalies[:10])
        )
    if correction_required:
        from pdf2epub.ocr_correction import select_refinement_pages
        from pdf2epub.utils.fingerprints import pages_fingerprint

        progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
        try:
            import json

            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            selected_pages, _ = select_refinement_pages(
                output_dir, require_correction=True, config=config,
                ocr_evidence=ocr_evidence,
            )
            current_fingerprint = pages_fingerprint(selected_pages)
            if progress.get("fingerprint", {}).get("pages_sha256") != current_fingerprint:
                raise ValueError
        except (OSError, ValueError, TypeError, AttributeError, json.JSONDecodeError):
            raise ValueError(
                "OCR correction is current but refined Markdown is stale; rerun refine-local before downstream use"
            )


def _polished_stage_is_current(
    output_dir: Path,
    polished_dir: Path,
    ocr_dir: Path,
    *,
    config: dict | None = None,
    config_path: Path | None = None,
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
        output_dir=output_dir,
        config=config,
        config_path=config_path,
    )


__all__ = [
    "_resolve_pdf_polish_source",
    "_polished_stage_is_current",
    "_require_reviewed_ocr_anomalies",
    "_resolve_pdf_markdown_source",
]

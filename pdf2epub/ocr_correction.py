"""Contracts for the visual OCR-correction quality gate.

The OCR backend writes the immutable raw page set in ``pages/``.  This module
keeps the deterministic parts of the next stage in one place: rendering page
images for visual review, checking the correction checkpoint, and selecting
the page directory consumed by TOC refinement.

The actual correction is intentionally not implemented here.  It is performed
by the workspace Subagent from the hand-off prepared by ``ocr-correct``.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import shutil
import tempfile
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional, Tuple

from pdf2epub.workflow_contracts import (
    MARKDOWN_VALIDATION_SCHEMA_VERSION,
    atomic_write_text,
    sha256_file,
)
from pdf2epub.ocr_consensus import (
    consensus_is_current,
    load_consensus_manifest,
    review_required_files,
    secondary_ocr_enabled,
)


OCR_CORRECTION_VALIDATION_SCHEMA_VERSION = MARKDOWN_VALIDATION_SCHEMA_VERSION
OCR_REVIEW_IMAGE_SCHEMA_VERSION = 1
OCR_PAGE_REVIEW_SCHEMA_VERSION = 1
DEFAULT_REVIEW_IMAGE_DPI = 150


def raw_page_dir(output_dir: Path) -> Path:
    """Return the immutable page-level OCR output directory."""
    return Path(output_dir) / "pages"


def corrected_page_dir(output_dir: Path) -> Path:
    """Return the validated OCR-correction output directory."""
    return Path(output_dir) / "ocr_corrected_pages" / "validated"


def correction_work_dir(output_dir: Path) -> Path:
    """Return the Subagent's writable OCR-correction directory."""
    return Path(output_dir) / "ocr_corrected_pages"


def materialize_auto_accepted_pages(
    output_dir: Path,
    files: Iterable[str],
) -> list[str]:
    """Copy consensus-approved raw pages into the correction work directory."""
    source_dir = raw_page_dir(output_dir)
    target_dir = correction_work_dir(output_dir)
    target_dir.mkdir(parents=True, exist_ok=True)
    copied: list[str] = []
    for name in sorted({str(value) for value in files}):
        source = source_dir / name
        if not source.is_file() or source.name != name or source.suffix.lower() != ".md":
            continue
        shutil.copy2(source, target_dir / name)
        copied.append(name)
    return copied


def review_image_dir(output_dir: Path) -> Path:
    """Return the directory containing rendered page images."""
    return Path(output_dir) / "ocr_review_images"


def review_record_dir(output_dir: Path) -> Path:
    """Return the per-page visual-review record directory."""
    return Path(output_dir) / "ocr_correction_reviews"


def _review_pdf(output_dir: Path) -> Path:
    original = Path(output_dir) / "input_original.pdf"
    return original if original.is_file() else Path(output_dir) / "input.pdf"


def _page_hashes(directory: Path) -> Dict[str, str]:
    return {
        path.name: sha256_file(path)
        for path in sorted(Path(directory).glob("*.md"))
        if path.is_file()
    }


def _atomic_write_bytes(path: Path, content: bytes) -> None:
    """Write binary output through a sibling temporary file."""
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _load_review_manifest(output_dir: Path) -> Dict[str, Any]:
    path = Path(output_dir) / "ocr_review_images.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def render_review_images(
    output_dir: Path,
    *,
    dpi: int = DEFAULT_REVIEW_IMAGE_DPI,
    resume: bool = False,
) -> Dict[str, Any]:
    """Render one visual-review PNG per source PDF page.

    The original PDF is preferred so synthetic OCR page stamps and any
    pre-processing used by the OCR backend are not mistaken for book content.
    Existing images are reused only when the PDF hash, page count, and DPI all
    match the current render manifest.
    """
    import pymupdf as fitz

    output_dir = Path(output_dir)
    pdf_path = _review_pdf(output_dir)
    if not pdf_path.is_file():
        raise ValueError(f"Source PDF not found for OCR review images: {pdf_path}")
    try:
        dpi = int(dpi)
    except (TypeError, ValueError):
        dpi = DEFAULT_REVIEW_IMAGE_DPI
    if dpi < 72 or dpi > 400:
        raise ValueError("ocr_correction.review_dpi must be between 72 and 400")

    source_sha256 = sha256_file(pdf_path)
    image_dir = review_image_dir(output_dir)
    image_dir.mkdir(parents=True, exist_ok=True)
    with fitz.open(pdf_path) as document:
        total_pages = len(document)
        expected_names = [f"page_{number:03d}.png" for number in range(1, total_pages + 1)]

        previous = _load_review_manifest(output_dir) if resume else {}
        try:
            previous_dpi = int(previous.get("dpi", 0) or 0)
            previous_page_count = int(previous.get("page_count", 0) or 0)
        except (TypeError, ValueError):
            previous_dpi = 0
            previous_page_count = 0
        reusable = (
            previous.get("schema_version") == OCR_REVIEW_IMAGE_SCHEMA_VERSION
            and previous.get("source_sha256") == source_sha256
            and previous_dpi == dpi
            and previous_page_count == total_pages
            and all((image_dir / name).is_file() for name in expected_names)
        )
        if not reusable:
            matrix = fitz.Matrix(dpi / 72.0, dpi / 72.0)
            for page_number, page in enumerate(document, 1):
                pixmap = page.get_pixmap(matrix=matrix, alpha=False)
                _atomic_write_bytes(
                    image_dir / f"page_{page_number:03d}.png",
                    pixmap.tobytes("png"),
                )

    manifest = {
        "schema_version": OCR_REVIEW_IMAGE_SCHEMA_VERSION,
        "source_pdf": pdf_path.name,
        "source_sha256": source_sha256,
        "dpi": dpi,
        "page_count": total_pages,
        "image_dir": image_dir.name,
        "images": expected_names,
    }
    atomic_write_text(
        Path(output_dir) / "ocr_review_images.json",
        json.dumps(manifest, ensure_ascii=False, indent=2),
    )
    return manifest


def review_images_are_current(output_dir: Path) -> bool:
    """Return whether every visual-review image matches the current PDF."""
    manifest = _load_review_manifest(output_dir)
    pdf_path = _review_pdf(Path(output_dir))
    if (
        manifest.get("schema_version") != OCR_REVIEW_IMAGE_SCHEMA_VERSION
        or not pdf_path.is_file()
    ):
        return False
    try:
        page_count = int(manifest.get("page_count", 0) or 0)
        dpi = int(manifest.get("dpi", 0) or 0)
    except (TypeError, ValueError):
        return False
    names = manifest.get("images")
    if not isinstance(names, list) or page_count <= 0 or dpi < 72:
        return False
    if manifest.get("source_sha256") != sha256_file(pdf_path):
        return False
    expected_names = [f"page_{number:03d}.png" for number in range(1, page_count + 1)]
    if [str(name) for name in names] != expected_names:
        return False
    image_dir = review_image_dir(output_dir)
    return all((image_dir / name).is_file() for name in expected_names)


def _load_correction_report(output_dir: Path) -> Dict[str, Any]:
    path = Path(output_dir) / "ocr-correct_validation.json"
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _line_stats(path: Path) -> Dict[str, int]:
    """Return deterministic line counts for one UTF-8 Markdown page."""
    text = Path(path).read_text(encoding="utf-8")
    lines = text.splitlines()
    return {
        "line_count": len(lines),
        "nonempty_line_count": sum(1 for line in lines if line.strip()),
    }


def _is_subsequence(source: list[Any], target: list[Any]) -> bool:
    """Return whether source items occur in target in the same order."""
    iterator = iter(target)
    return all(any(candidate == item for candidate in iterator) for item in source)


def _validate_page_structure(source_text: str, target_text: str) -> list[str]:
    """Reject structural loss while allowing visually recovered additions."""
    errors: list[str] = []
    heading_re = re.compile(r"^(#{1,6})\s", re.MULTILINE)
    source_levels = [len(match.group(1)) for match in heading_re.finditer(source_text)]
    target_levels = [len(match.group(1)) for match in heading_re.finditer(target_text)]
    if len(target_levels) < len(source_levels) or not _is_subsequence(
        source_levels, target_levels
    ):
        errors.append("corrected page loses or reorders Markdown headings")

    image_re = re.compile(r"!\[[^\]]*\]\(([^)]+)\)")
    source_images = [match.group(1) for match in image_re.finditer(source_text)]
    target_images = [match.group(1) for match in image_re.finditer(target_text)]
    if len(target_images) < len(source_images) or not _is_subsequence(
        source_images, target_images
    ):
        errors.append("corrected page loses or reorders Markdown image destinations")

    footnote_re = re.compile(r"\[\^[^\]]+\]")
    if len(footnote_re.findall(target_text)) < len(footnote_re.findall(source_text)):
        errors.append("corrected page loses Markdown footnote markers")

    if target_text.count("```") != source_text.count("```"):
        errors.append("corrected page changes Markdown code-fence count")
    return errors


def validate_ocr_correction_reviews(
    output_dir: Path,
    source_dir: Path,
    target_dir: Path,
    selected_files: Optional[Iterable[str]] = None,
) -> Dict[str, Any]:
    """Validate page-review evidence and reject content-line loss.

    OCR correction is allowed to add text when the page image proves that the
    raw OCR omitted a line, but it is not allowed to delete source lines.  The
    Subagent must also leave one complete review record for every page.  The
    record is evidence of the visual comparison; counts are independently
    recomputed here so a stale or fabricated count cannot become a checkpoint.
    """
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    record_dir = review_record_dir(output_dir)
    all_source_names = sorted(
        path.name for path in source_dir.glob("*.md") if path.is_file()
    )
    selected_names = (
        sorted({str(name) for name in selected_files})
        if selected_files is not None
        else all_source_names
    )
    errors: list[Dict[str, str]] = []
    checked: list[str] = []

    for name in selected_names:
        if name not in all_source_names:
            errors.append({"file": name, "reason": "unknown OCR source page"})

    for name in selected_names:
        if name not in all_source_names:
            continue
        source = source_dir / name
        target = target_dir / name
        record_path = record_dir / f"{Path(name).stem}.json"
        if not record_path.is_file():
            errors.append({"file": name, "reason": "missing per-page OCR review record"})
            continue
        if not target.is_file():
            errors.append({"file": name, "reason": "target page is missing"})
            continue
        try:
            record = json.loads(record_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError) as exc:
            errors.append({"file": name, "reason": f"invalid OCR review record: {exc}"})
            continue
        if not isinstance(record, dict):
            errors.append({"file": name, "reason": "OCR review record must be a JSON object"})
            continue
        if record.get("schema_version") != OCR_PAGE_REVIEW_SCHEMA_VERSION:
            errors.append({"file": name, "reason": "unsupported OCR review record schema"})
            continue
        if record.get("source_file") != name:
            errors.append({"file": name, "reason": "OCR review record source_file mismatch"})
            continue
        expected_visual = f"ocr_review_images/{Path(name).stem}.png"
        if record.get("visual_file") != expected_visual:
            errors.append({"file": name, "reason": "OCR review record visual_file mismatch"})
            continue
        if record.get("reviewed") is not True:
            errors.append({"file": name, "reason": "page was not marked visually reviewed"})
            continue
        if record.get("coverage") != "complete" or record.get("uncertain") is not False:
            errors.append({"file": name, "reason": "page review is incomplete or uncertain"})
            continue
        try:
            source_stats = _line_stats(source)
            target_stats = _line_stats(target)
            recorded_source = {
                "line_count": int(record.get("source_line_count")),
                "nonempty_line_count": int(record.get("source_nonempty_line_count")),
            }
            recorded_target = {
                "line_count": int(record.get("target_line_count")),
                "nonempty_line_count": int(record.get("target_nonempty_line_count")),
            }
        except (OSError, UnicodeError, TypeError, ValueError) as exc:
            errors.append({"file": name, "reason": f"invalid OCR review line counts: {exc}"})
            continue
        structure_errors = _validate_page_structure(
            source.read_text(encoding="utf-8"),
            target.read_text(encoding="utf-8"),
        )
        if structure_errors:
            errors.extend({"file": name, "reason": reason} for reason in structure_errors)
            continue
        if recorded_source != source_stats or recorded_target != target_stats:
            errors.append({"file": name, "reason": "OCR review line counts do not match the files"})
            continue
        if target_stats["line_count"] < source_stats["line_count"]:
            errors.append({"file": name, "reason": "corrected page has fewer total lines than raw OCR"})
            continue
        if target_stats["nonempty_line_count"] < source_stats["nonempty_line_count"]:
            errors.append({"file": name, "reason": "corrected page has fewer non-empty lines than raw OCR"})
            continue
        checked.append(name)

    if selected_files is None and record_dir.is_dir():
        expected_records = {f"{Path(name).stem}.json" for name in all_source_names}
        extras = sorted(
            path.name
            for path in record_dir.glob("*.json")
            if path.is_file() and path.name not in expected_records
        )
        for name in extras:
            errors.append({"file": name, "reason": "unexpected extra OCR review record"})

    return {
        "schema_version": OCR_PAGE_REVIEW_SCHEMA_VERSION,
        "valid": bool(selected_names) and not errors and len(checked) == len(selected_names),
        "files_checked": checked,
        "errors": errors,
    }


def ocr_correction_is_current(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether validated corrections match the current raw OCR pages."""
    if config is not None and not secondary_ocr_enabled(config):
        return False
    report = _load_correction_report(output_dir)
    if (
        report.get("schema_version") != OCR_CORRECTION_VALIDATION_SCHEMA_VERSION
        or report.get("task") != "ocr-correct"
        or report.get("all_passed") is not True
    ):
        return False
    source_hashes = report.get("source_sha256")
    target_hashes = report.get("target_sha256")
    valid_files = report.get("valid_files")
    if not isinstance(source_hashes, Mapping) or not isinstance(target_hashes, Mapping):
        return False
    if not isinstance(valid_files, list):
        return False
    current = _page_hashes(raw_page_dir(output_dir))
    if not current or dict(source_hashes) != current or set(valid_files) != set(current):
        return False
    target_dir = corrected_page_dir(output_dir)
    for name in current:
        target = target_dir / name
        if not target.is_file() or target_hashes.get(name) != sha256_file(target):
            return False
    consensus_manifest = load_consensus_manifest(output_dir)
    if config is not None and secondary_ocr_enabled(config):
        # A correction checkpoint is only meaningful when it is tied to the
        # current two-OCR comparison. Do not let a pre-consensus or partially
        # migrated checkpoint enter refinement merely because its page hashes
        # and visual review records still happen to match.
        if (
            not consensus_manifest
            or not consensus_manifest.get("complete")
            or not consensus_is_current(output_dir, config)
        ):
            return False
        review_files = review_required_files(output_dir)
    elif consensus_manifest:
        if not consensus_manifest.get("complete"):
            return False
        if config is not None and not consensus_is_current(output_dir, config):
            return False
        review_files = review_required_files(output_dir)
    else:
        # Compatibility path for callers that do not provide configuration.
        review_files = list(current)
    # When every page was accepted by the two local OCR results, no visual
    # review image or per-page visual record is needed. The auto-accepted
    # checkpoint still has to contain every current page and matching hashes.
    if review_files and not review_images_are_current(output_dir):
        return False
    return validate_ocr_correction_reviews(
        output_dir,
        raw_page_dir(output_dir),
        target_dir,
        selected_files=review_files,
    )["valid"] if review_files else True


def select_refinement_pages(
    output_dir: Path,
    *,
    require_correction: bool = False,
    config: Optional[Mapping[str, Any]] = None,
) -> Tuple[Path, str]:
    """Select the page set for TOC/refinement and report its provenance."""
    output_dir = Path(output_dir)
    raw = raw_page_dir(output_dir)
    probe_path = output_dir / "pdf_text_probe.json"
    probe: Dict[str, Any] = {}
    try:
        value = json.loads(probe_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            probe = value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    if (
        probe.get("classification") == "native_text"
        and probe.get("recommendation") == "use_text_layer"
    ):
        return raw, "native_text"
    if config is not None and not secondary_ocr_enabled(config):
        return raw, "ocr"
    if ocr_correction_is_current(output_dir, config=config):
        return corrected_page_dir(output_dir), "ocr_corrected"
    if require_correction:
        raise ValueError(
            "visual OCR correction is not validated; run ocr-correct, let the "
            "workspace Subagent write ocr_corrected_pages/*.md, then run "
            "ocr-correct-validate"
        )
    return raw, "ocr"


__all__ = [
    "DEFAULT_REVIEW_IMAGE_DPI",
    "OCR_CORRECTION_VALIDATION_SCHEMA_VERSION",
    "OCR_PAGE_REVIEW_SCHEMA_VERSION",
    "corrected_page_dir",
    "correction_work_dir",
    "materialize_auto_accepted_pages",
    "ocr_correction_is_current",
    "raw_page_dir",
    "render_review_images",
    "review_image_dir",
    "review_record_dir",
    "review_images_are_current",
    "select_refinement_pages",
    "validate_ocr_correction_reviews",
]

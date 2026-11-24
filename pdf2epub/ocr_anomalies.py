"""Conservative detection of OCR model commentary in page transcription."""

from __future__ import annotations

import json
import hashlib
import io
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from pdf2epub.workflow_contracts import atomic_write_json

SCHEMA_VERSION = 1
LONG_OCR_WORD_THRESHOLD = 80
LONG_OCR_CHAR_THRESHOLD = 400
BODY_BLANK_RATIO_THRESHOLD = 0.0005
FULL_PAGE_CONTENT_RATIO_THRESHOLD = 0.005
DETECTOR_POLICY = "long-ocr-blankness-v1"


@dataclass(frozen=True)
class OcrPageEvidence:
    """One invocation's anomaly findings and raw-page hashes, bound to a book."""

    output_dir: Path
    report: dict[str, Any]

    @property
    def page_hashes(self) -> dict[str, str]:
        return self.report["page_hashes"]


def evidence_report_for_output(
    evidence: OcrPageEvidence | None, output_dir: Path
) -> Mapping[str, Any] | None:
    """Return evidence only when it belongs to this output directory."""
    if evidence is None:
        return None
    if Path(evidence.output_dir).resolve() != Path(output_dir).resolve():
        raise ValueError("OCR page evidence belongs to a different output directory")
    return evidence.report

# Require a page/text/image reference together with a transcription failure or
# an explicit description-of-content construction. These patterns deliberately
# avoid standalone words such as "unclear" and "illegible".
_EN_PAGE = r"(?:this|the|above|following)?\s*(?:page|text|image|scan|document|writing)"
_EN_FAILURE = r"(?:cannot|can't|unable\s+to|impossible\s+to|too\s+(?:faint|faded|illegible)\s+to|not\s+possible\s+to)\s+(?:be\s+)?(?:transcrib(?:e|ed)|decipher|make\s+out|identify)"
_EN_UNREADABLE = r"(?:cannot|can't|unable\s+to|impossible\s+to)\s+(?:be\s+)?read(?!\s+(?:without\s+understanding|in\s+isolation|without\s+context|on\s+its\s+own))"
_EN_COMMENTARY = re.compile(
    rf"(?:{_EN_PAGE}.{{0,100}}{_EN_FAILURE}|{_EN_FAILURE}.{{0,100}}{_EN_PAGE}|"
    rf"{_EN_PAGE}.{{0,100}}{_EN_UNREADABLE}|{_EN_UNREADABLE}.{{0,100}}{_EN_PAGE}|"
    rf"{_EN_PAGE}.{{0,80}}(?:cannot|can't|unable\s+to)\s+be\s+read\s+or\s+transcrib(?:e|ed)|"
    rf"the\s+content\s+is\s+too\s+(?:faint|faded)\s+to\s+be\s+transcribed)", re.I | re.S)
_ZH_COMMENTARY = re.compile(
    r"(?:此页|该页|页面上的文字|以下文字|图像中的文字).{0,40}(?:无法|不能|难以).{0,20}(?:准确)?(?:转录|抄录|识别)|"
    r"(?:此页|该页|页面).{0,30}(?:太淡|过淡|模糊).{0,24}(?:无法|不能).{0,20}(?:转录|辨认)", re.S)


def detect_ocr_commentary(text: str) -> list[str]:
    """Return conservative reasons when text reads like OCR narration."""
    normalized = re.sub(r"\s+", " ", str(text or ""))
    reasons = []
    if _EN_COMMENTARY.search(normalized):
        reasons.append("ocr_commentary_english_unreadable_or_descriptive_text")
    if _ZH_COMMENTARY.search(normalized):
        reasons.append("ocr_commentary_chinese_unreadable_text")
    return reasons


def _is_long_ocr(text: str) -> bool:
    return len(re.findall(r"\S+", text)) >= LONG_OCR_WORD_THRESHOLD or len(
        re.sub(r"\s+", "", text)
    ) >= LONG_OCR_CHAR_THRESHOLD


def _current_ocr_pdf(output_dir: Path, source_sha256: str) -> tuple[Path | None, str]:
    """Select only the PDF whose exact bytes produced the current OCR pages."""
    for candidate in (output_dir / "input.pdf", output_dir / "input_original.pdf"):
        if not candidate.is_file():
            continue
        try:
            digest = hashlib.sha256(candidate.read_bytes()).hexdigest()
        except OSError:
            continue
        if source_sha256 and digest == source_sha256:
            return candidate, digest
    return None, ""


def _render_page(pdf_path: Path, page_number: int) -> bytes:
    import pymupdf as fitz

    with fitz.open(pdf_path) as document:
        if page_number < 1 or page_number > len(document):
            raise ValueError("page number is outside the PDF")
        pixmap = document[page_number - 1].get_pixmap(
            matrix=fitz.Matrix(150 / 72, 150 / 72), alpha=False
        )
        return pixmap.tobytes("png")


def _trusted_existing_image(
    output_dir: Path, page_number: int, pdf_sha256: str
) -> bytes | None:
    """Read an image only when its render receipt is bound to the current PDF."""
    image_path = output_dir / "ocr_review_images" / f"page_{page_number:03d}.png"
    try:
        manifest = json.loads(
            (output_dir / "ocr_review_images.json").read_text(encoding="utf-8")
        )
        if not isinstance(manifest, dict):
            return None
        if (
            manifest.get("source_sha256") != pdf_sha256
            or int(manifest.get("page_count", 0)) < page_number
            or image_path.name not in manifest.get("images", [])
            or not image_path.is_file()
        ):
            return None
        image_bytes = image_path.read_bytes()
        image_hashes = manifest.get("image_sha256")
        if not isinstance(image_hashes, Mapping):
            return None
        expected_hash = image_hashes.get(image_path.name)
        if expected_hash != hashlib.sha256(image_bytes).hexdigest():
            return None
        return image_bytes
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, AttributeError):
        return None


def _ink_ratios(image_bytes: bytes) -> tuple[float, float, float]:
    from PIL import Image

    with Image.open(io.BytesIO(image_bytes)) as image:
        grayscale = image.convert("L")
        width, height = grayscale.size
        if width < 3 or height < 3:
            raise ValueError("rendered page image is too small")
        histogram = grayscale.histogram()
        full_ink = sum(histogram[:200])
        full_ratio = full_ink / (width * height)
        x_margin, y_margin = max(1, round(width * 0.05)), max(1, round(height * 0.05))
        right, bottom = max(x_margin + 1, width - x_margin), max(y_margin + 1, height - y_margin)
        body = grayscale.crop((x_margin, y_margin, right, bottom))
        body_histogram = body.histogram()
        body_ink = sum(body_histogram[:200])
        body_pixels = body.width * body.height
        body_ratio = body_ink / body_pixels
        edge_pixels = width * height - body_pixels
        edge_ink = full_ink - body_ink
        edge_ratio = edge_ink / edge_pixels if edge_pixels else 0.0
        return body_ratio, full_ratio, edge_ratio


def _page_number(name: str) -> int | None:
    match = re.fullmatch(r"page_(\d+)\.md", name)
    return int(match.group(1)) if match else None


def scan_ocr_page_evidence(
    output_dir: Path, *, persist: bool = True
) -> OcrPageEvidence:
    """Scan page text and use current PDF pixels for narrow blank-page checks.

    Read-only callers never render or write. Write-enabled scans render only
    long-text candidates. Missing or stale pixels are diagnostics, never a
    blank-page finding.
    """
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    pages: dict[str, Any] = {}
    advisory_pages: dict[str, list[str]] = {}
    scan_errors = []
    diagnostics = []
    page_hashes = {}
    contents: dict[str, str] = {}
    for path in sorted(pages_dir.glob("page_*.md")):
        if not path.is_file():
            continue
        try:
            raw_content = path.read_bytes()
            content = raw_content.decode("utf-8")
            digest = hashlib.sha256(raw_content).hexdigest()
        except (OSError, UnicodeError) as exc:
            scan_errors.append({"file": path.name, "reason": f"page scan failed: {exc}"})
            continue
        page_hashes[path.name] = digest
        contents[path.name] = content
        reasons = detect_ocr_commentary(content)
        if reasons:
            pages[path.name] = {"sha256": digest, "reasons": reasons}

    progress_path = pages_dir / "ocr_progress.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        progress = {}
    if not isinstance(progress, dict):
        progress = {}
    source_sha256 = str(progress.get("source_sha256") or "")

    # Repeated text is diagnostic only; it never creates review work.
    repeated: dict[str, list[str]] = {}
    for name, content in contents.items():
        normalized = re.sub(r"\s+", " ", content).strip().casefold()
        if normalized:
            repeated.setdefault(normalized, []).append(name)
    for names in repeated.values():
        if len(names) > 1:
            for name in names:
                advisory_pages.setdefault(name, []).append("repeated_ocr_text")

    pdf_path, pdf_sha256 = _current_ocr_pdf(output_dir, source_sha256)
    pdf_current = pdf_path is not None

    previous_report = {}
    try:
        previous_report = json.loads(
            (output_dir / "ocr_anomaly_report.json").read_text(encoding="utf-8")
        )
        if not isinstance(previous_report, dict):
            previous_report = {}
    except (OSError, UnicodeError, json.JSONDecodeError):
        previous_report = {}
    cached_candidates = previous_report.get("visual_candidates", {})
    if not isinstance(cached_candidates, dict):
        cached_candidates = {}
    visual_candidates: dict[str, dict[str, Any]] = {}

    for name, content in contents.items():
        if not _is_long_ocr(content):
            continue
        number = _page_number(name)
        if not pdf_current or number is None:
            reason = "current source PDF/page image unavailable; blankness was not assessed"
            diagnostics.append({"file": name, "reason": reason})
            advisory_pages.setdefault(name, []).append("blankness_evidence_insufficient")
            continue
        cached = cached_candidates.get(name, {})
        cached_current = (
            isinstance(cached, dict)
            and cached.get("image_pdf_sha256") == pdf_sha256
            and cached.get("sha256") == page_hashes[name]
            and cached.get("detector_policy") == DETECTOR_POLICY
        )
        if cached_current:
            try:
                body_ratio = float(cached["body_ink_ratio"])
                full_ratio = float(cached["full_page_ink_ratio"])
                edge_ratio = float(cached.get("edge_ink_ratio", 0.0))
                image_sha256 = str(cached.get("image_sha256") or "")
                cached_current = bool(image_sha256)
            except (KeyError, TypeError, ValueError):
                cached_current = False
        if not cached_current:
            body_ratio = full_ratio = edge_ratio = 0.0
            image_sha256 = ""
            image_bytes = None
            try:
                image_bytes = (
                    _render_page(pdf_path, number)
                    if persist
                    else _trusted_existing_image(output_dir, number, pdf_sha256)
                )
            except Exception as exc:
                diagnostics.append({"file": name, "reason": f"candidate page render unavailable: {exc}"})
            if image_bytes is None:
                diagnostics.append({"file": name, "reason": "current rendered page image unavailable; blankness was not assessed"})
                advisory_pages.setdefault(name, []).append("blankness_evidence_insufficient")
                continue
            try:
                body_ratio, full_ratio, edge_ratio = _ink_ratios(image_bytes)
            except Exception as exc:
                diagnostics.append({"file": name, "reason": f"page image could not be measured: {exc}"})
                advisory_pages.setdefault(name, []).append("blankness_evidence_insufficient")
                continue
            image_sha256 = hashlib.sha256(image_bytes).hexdigest()
        candidate_evidence = {
            "sha256": page_hashes[name],
            "image_pdf_sha256": pdf_sha256,
            "image_sha256": image_sha256,
            "detector_policy": DETECTOR_POLICY,
            "body_ink_ratio": body_ratio,
            "full_page_ink_ratio": full_ratio,
            "edge_ink_ratio": edge_ratio,
        }
        visual_candidates[name] = candidate_evidence
        if body_ratio <= BODY_BLANK_RATIO_THRESHOLD:
            if (
                full_ratio >= FULL_PAGE_CONTENT_RATIO_THRESHOLD
                or edge_ratio >= 0.0001
            ):
                advisory_pages.setdefault(name, []).append("body_sparse_but_full_page_content_present")
            else:
                pages[name] = {
                    **candidate_evidence,
                    "reasons": ["long_ocr_text_on_visually_blank_page"],
                    "image_evidence": "blank_body",
                }
                advisory_pages.setdefault(name, []).append("image_backed_blank_page")
        else:
            advisory_pages.setdefault(name, []).append("long_ocr_candidate_with_visible_body_content")

    report = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": source_sha256,
        "page_hashes": page_hashes,
        "anomalies": pages,
        "visual_candidates": visual_candidates,
        "advisory_pages": advisory_pages,
        "scan_diagnostics": diagnostics,
        "scan_errors": scan_errors,
        "review_required_files": sorted(pages),
        "all_clear": not pages and not scan_errors,
    }
    if persist:
        atomic_write_json(output_dir / "ocr_anomaly_report.json", report)
    return OcrPageEvidence(output_dir.resolve(), report)


def scan_ocr_pages(output_dir: Path, *, persist: bool = True) -> dict[str, Any]:
    """Hash and scan every current raw page, including resumed checkpoints.

    Retains the original dictionary-returning API for standalone callers.
    """
    return scan_ocr_page_evidence(output_dir, persist=persist).report


__all__ = [
    "OcrPageEvidence",
    "detect_ocr_commentary",
    "evidence_report_for_output",
    "scan_ocr_page_evidence",
    "scan_ocr_pages",
    "SCHEMA_VERSION",
]

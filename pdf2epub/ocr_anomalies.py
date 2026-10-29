"""Conservative detection of OCR model commentary in page transcription."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any

from pdf2epub.workflow_contracts import atomic_write_json, sha256_file

SCHEMA_VERSION = 1

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


def scan_ocr_pages(output_dir: Path, *, persist: bool = True) -> dict[str, Any]:
    """Hash and scan every current raw page, including resumed checkpoints."""
    output_dir = Path(output_dir)
    pages_dir = output_dir / "pages"
    pages = {}
    scan_errors = []
    page_hashes = {}
    for path in sorted(pages_dir.glob("page_*.md")):
        if not path.is_file():
            continue
        try:
            content = path.read_text(encoding="utf-8")
            digest = sha256_file(path)
        except (OSError, UnicodeError) as exc:
            scan_errors.append({"file": path.name, "reason": f"page scan failed: {exc}"})
            continue
        page_hashes[path.name] = digest
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
    report = {
        "schema_version": SCHEMA_VERSION,
        "source_sha256": str(progress.get("source_sha256") or ""),
        "page_hashes": page_hashes,
        "anomalies": pages,
        "scan_errors": scan_errors,
        "review_required_files": sorted(pages),
        "all_clear": not pages and not scan_errors,
    }
    if persist:
        atomic_write_json(output_dir / "ocr_anomaly_report.json", report)
    return report


__all__ = ["detect_ocr_commentary", "scan_ocr_pages", "SCHEMA_VERSION"]

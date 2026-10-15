"""Pure page-furniture candidate rules shared by repair and validation.

The rules only produce review hints.  They never delete or rewrite document
content, which keeps layout heuristics independent from the repair workflow.
"""

from __future__ import annotations

import re
from typing import Any, Optional


_STANDALONE_PAGE_LABEL_RE = re.compile(
    r"^(?:page\s*[:#-]?\s*)?(\d{1,4}|[ivxlcdm]{1,12})$",
    re.IGNORECASE,
)
_SYNTHETIC_PAGE_LABEL_RE = re.compile(
    r"^pdf\s+page\s*[:#-]?\s*\d{1,4}$", re.IGNORECASE
)
_STANDALONE_SUP_PAGE_LABEL_RE = re.compile(
    r"^<sup\b[^>]*>\s*(?:page\s*[:#-]?\s*)?"
    r"(\d{1,4}|[ivxlcdm]{1,12})\s*</sup>$",
    re.IGNORECASE,
)
_PAGE_LABEL_CORE = (
    r"(?:<sup\b[^>]*>\s*)?(?P<label>\d{1,4}|[ivxlcdm]{1,12})(?:\s*</sup>)?"
)
_COMBINED_PAGE_LABEL_RE = re.compile(
    rf"^(?P<title>.+?)\s+{_PAGE_LABEL_CORE}$",
    re.IGNORECASE,
)
_PREFIXED_PAGE_LABEL_RE = re.compile(
    rf"^{_PAGE_LABEL_CORE}\s+(?P<title>.+?)$",
    re.IGNORECASE,
)
_NUMBERED_RUNNING_HEADER_RE = re.compile(
    r"^(?:(?:[ivxlcdm]{1,8}|\d{1,2})\s+[A-Za-zÄÖÜäöüß\s–-]+|"
    r"第[一二三四五六七八九十百千万零〇两\d]+(?:部分|[章卷])"
    r"(?:\s+|[:：])?[^。！？；.!?;]+)(?:\s*\(\d{4}[–-]\d{4}\))?$",
    re.IGNORECASE,
)
_PUBLISHER_METADATA_RE = re.compile(
    r"^(?:(?:W\.\s+)?Jaeschke,\s+Hegel-Handbuch,?|DOI:?\s+10\.\d{4}.*|[©\xa9]\s*\d{4}\s+J\.\s*B\.\s*Metzler.*)$",
    re.IGNORECASE,
)
_DATE_SUFFIX_RE = re.compile(
    r"\b(?:im\s+)?(?:Frühjahr|Fruehjahr|Sommer|Herbst|Winter|Januar|Februar|März|Maerz|April|Mai|Juni|Juli|August|September|Oktober|November|Dezember)\b",
    re.IGNORECASE,
)
_ORNAMENTED_HEADER_RE = re.compile(
    r"^(?P<ornament>[•*~])\s*(?P=ornament)\s*(?P=ornament)\s+"
    r"(?P<title>.+?)\s+(?P=ornament)\s*(?P=ornament)\s*(?P=ornament)$",
    re.IGNORECASE,
)
_MAX_COMBINED_LINE_LENGTH = 96


def candidate_for_line(line: str) -> Optional[dict[str, Any]]:
    """Return a conservative page-furniture candidate for one Markdown line."""
    raw_stripped = line.strip()
    ornament_match = _ORNAMENTED_HEADER_RE.fullmatch(raw_stripped)
    if ornament_match and not re.search(
        r"[。！？；.!?;]", ornament_match.group("title")
    ):
        return {
            "kind": "running_header",
            "text": raw_stripped,
            "confidence": "high",
            "ornamented": True,
        }
    stripped = raw_stripped.strip("|•·—–-").strip()
    if not stripped or stripped.startswith(("[", "!")):
        return None
    if _PUBLISHER_METADATA_RE.fullmatch(stripped):
        return {"kind": "publisher_metadata", "text": stripped, "confidence": "high"}
    if _SYNTHETIC_PAGE_LABEL_RE.fullmatch(stripped):
        return {"kind": "synthetic_page_label", "text": stripped, "confidence": "high"}
    sup_match = _STANDALONE_SUP_PAGE_LABEL_RE.fullmatch(stripped)
    if sup_match:
        return {
            "kind": "standalone_page_label",
            "text": stripped,
            "label": sup_match.group(1),
            "confidence": "high",
        }
    if _STANDALONE_PAGE_LABEL_RE.fullmatch(stripped):
        return {"kind": "standalone_page_label", "text": stripped, "confidence": "medium"}
    if len(stripped) > _MAX_COMBINED_LINE_LENGTH:
        return None

    raw_stripped = line.strip()
    title_line = re.sub(r"^#{1,6}\s+", "", stripped).strip()
    if _NUMBERED_RUNNING_HEADER_RE.fullmatch(title_line) and not re.search(
        r"[。！？；.!?;]", title_line
    ):
        return {"kind": "running_header", "text": stripped, "confidence": "high"}

    match = _COMBINED_PAGE_LABEL_RE.fullmatch(title_line)
    if not match:
        match = _PREFIXED_PAGE_LABEL_RE.fullmatch(title_line)
    if match and not raw_stripped.endswith(("-", "–", "—", ",", ":", ";")):
        title = match.group("title").strip()
        label = match.group("label")
        if _DATE_SUFFIX_RE.search(title) and label.isdigit() and len(label) == 4:
            return None
        if title and not title.isdigit() and not re.search(r"[。！？；.!?;,:]", title):
            return {
                "kind": "running_title_plus_page_label",
                "text": stripped,
                "label": label,
                "confidence": "high",
            }
    return None


__all__ = ["candidate_for_line"]

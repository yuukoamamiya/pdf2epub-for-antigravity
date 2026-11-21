"""Shared normalization helpers for refine-stage matching.

The four historical ``_normalize_*`` helpers had overlapping names but
different semantics:

- decision/unit file names: POSIX serialization only (no case folding);
- endnote scope titles: whitespace collapse + outer-markdown stripping;
- TOC evidence titles: aggressive punctuation stripping for exact matching;
- OCR heading lines: light ``#`` + whitespace normalization.

Centralizing them here keeps the semantics explicit and prevents the
``None``/numeric-filename edge cases from diverging again.
"""

from __future__ import annotations

import re
from typing import Any

from pdf2epub.workflow_contracts import posix_path

_SCOPE_MARKDOWN_TOP_RE = re.compile(r"^[*_`]+|[*_`]+$")
_SCOPE_TITLE_OUTER_MARK_PAIRS = {
    ("《", "》"),
    ("〈", "〉"),
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ("‘", "’"),
    ('"', '"'),
    ("'", "'"),
}


def normalize_decision_file(value: Any) -> str:
    """Normalize a manifest/decision ``file`` field to a POSIX key."""
    return posix_path(value or "")


def normalize_title_for_scope(value: Any) -> str:
    """Normalize a title for endnote scope matching (casefolded)."""
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = _SCOPE_MARKDOWN_TOP_RE.sub("", text).strip()
    text = re.sub(r"(?<=\w)[ \t]+-[ \t]*(?=\w)", "-", text)
    while len(text) >= 2 and (text[0], text[-1]) in _SCOPE_TITLE_OUTER_MARK_PAIRS:
        text = text[1:-1].strip()
    return text.casefold()


def normalize_title_for_match(value: Any) -> str:
    """Aggressive TOC-evidence normalization for exact/near-exact matching."""
    text = re.sub(r"^\s*#{1,6}\s*", "", str(value or ""))
    text = re.sub(r"\s+", "", text)
    return re.sub(r"[^\w\u3400-\u9fff]", "", text, flags=re.UNICODE).casefold()


def normalize_heading_for_ocr(value: Any) -> str:
    """Light heading normalization to recognize its own OCR Markdown line."""
    text = re.sub(r"^#{1,6}\s*", "", str(value or ""))
    return re.sub(r"\s+", " ", text).strip().casefold()

"""Validation helpers for normalizing OCR endnotes to Markdown footnotes."""

from __future__ import annotations

import re
from collections import Counter
from typing import List


_NOTE_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})\s+(?:notes?|endnotes?|注释|注|脚注)\s*$",
    re.IGNORECASE,
)
_HTML_SUP_RE = re.compile(r"<sup>\s*(?P<key>\d+)\s*</sup>", re.IGNORECASE)
_MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\](?!:)")
_MARKDOWN_DEF_RE = re.compile(r"^\s*\[\^(?P<key>[A-Za-z0-9_-]+)\]:\s*", re.MULTILINE)
_LEGACY_DEF_RE = re.compile(r"^\s*(?P<key>\d+)\.\s+", re.MULTILINE)


def _note_section_keys(text: str) -> List[str]:
    """Return legacy numbered note definitions under a Notes heading."""
    lines = text.splitlines()
    in_notes = False
    note_level = 0
    keys: List[str] = []
    for line in lines:
        heading = _NOTE_HEADING_RE.match(line.strip())
        if heading:
            in_notes = True
            note_level = len(heading.group("marks"))
            continue
        if in_notes:
            next_heading = re.match(r"^(#{1,6})\s+", line)
            if next_heading and len(next_heading.group(1)) <= note_level:
                in_notes = False
                continue
            match = _LEGACY_DEF_RE.match(line)
            if match:
                keys.append(match.group("key"))
    return keys


def validate_polish_footnote_normalization(source_text: str, target_text: str) -> List[str]:
    """Validate a safe OCR ``<sup>`` to Markdown-footnote migration.

    The check activates only when the source contains both numbered superscripts
    and a recognizable Notes/注释 section. This prevents mathematical and table
    superscripts from being treated as footnotes by default.
    """
    source_refs = [match.group("key") for match in _HTML_SUP_RE.finditer(source_text)]
    source_defs = _note_section_keys(source_text)
    if not source_refs or not source_defs:
        return []

    target_refs = [match.group("key") for match in _MARKDOWN_REF_RE.finditer(target_text)]
    target_defs = [match.group("key") for match in _MARKDOWN_DEF_RE.finditer(target_text)]
    errors: List[str] = []
    if Counter(target_refs) != Counter(source_refs):
        errors.append(
            "footnote reference migration mismatch: "
            f"expected {Counter(source_refs)}, found {Counter(target_refs)}"
        )
    if Counter(target_defs) != Counter(source_defs):
        errors.append(
            "footnote definition migration mismatch: "
            f"expected {Counter(source_defs)}, found {Counter(target_defs)}"
        )
    if _HTML_SUP_RE.search(target_text):
        errors.append("numbered HTML <sup> footnote markers remain after polishing")
    return errors

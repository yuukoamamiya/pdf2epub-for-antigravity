"""Validation helpers for normalizing OCR endnotes to Markdown footnotes."""

from __future__ import annotations

import re
from collections import Counter
from typing import List


_NOTE_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})\s+(?:notes?|endnotes?|注释|注|脚注)\s*$",
    re.IGNORECASE,
)
_HTML_SUP_RE = re.compile(
    r"<sup\b[^>]*>\s*(?P<key>\d+)\s*</sup>", re.IGNORECASE
)
_UNICODE_SUP_RE = re.compile(r"(?P<value>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
_UNICODE_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>[A-Za-z0-9_-]+)\](?!:)")
_MARKDOWN_DEF_RE = re.compile(r"^\s*\[\^(?P<key>[A-Za-z0-9_-]+)\]:\s*", re.MULTILINE)
_LEGACY_DEF_RE = re.compile(
    r"^\s*(?:[-*+][ \t]+)?(?P<key>\d+)\.\s+", re.MULTILINE
)
_RAW_NOTE_DEF_RE = re.compile(
    r"^[ \t]{0,3}(?:"
    r"<sup\b[^>]*>[ \t]*(?P<html_key>\d+)[ \t]*</sup>"
    r"|(?:[-*+][ \t]+)?(?P<number_key>\d+)[ \t]*(?:[.)。：：]|(?=\s))"
    r")[ \t]+",
    re.IGNORECASE | re.MULTILINE,
)


def endnote_definition_keys(text: str, *, file_role: str | None = None) -> List[str]:
    """Return centralized endnote keys from a typed Notes unit.

    A TOC role is authoritative here because refined Markdown often omits the
    structural ``Notes`` heading.  The heading-based legacy path remains for
    older callers that do not have file-role metadata.
    """
    keys = [match.group("key") for match in _MARKDOWN_DEF_RE.finditer(text)]
    if str(file_role or "").strip().lower() == "notes":
        keys.extend(
            match.group("html_key") or match.group("number_key")
            for match in _RAW_NOTE_DEF_RE.finditer(text)
        )
    else:
        keys.extend(_note_section_keys(text))
    return keys


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


def validate_polish_footnote_normalization(
    source_text: str,
    target_text: str,
    *,
    file_role: str | None = None,
    expected_endnote_keys: set[str] | None = None,
) -> List[str]:
    """Validate a safe OCR ``<sup>`` to Markdown-footnote migration.

    The check activates only when the source contains both numbered superscripts
    and a recognizable Notes/注释 section. This prevents mathematical and table
    superscripts from being treated as footnotes by default.
    """
    source_refs = [match.group("key") for match in _MARKDOWN_REF_RE.finditer(source_text)]
    source_defs = [match.group("key") for match in _MARKDOWN_DEF_RE.finditer(source_text)]
    expected_keys = set(expected_endnote_keys or ())
    role = str(file_role or "").strip().lower()
    if role == "notes":
        source_defs = endnote_definition_keys(source_text, file_role="notes")
    else:
        legacy_refs = [match.group("key") for match in _HTML_SUP_RE.finditer(source_text)]
        legacy_defs = _note_section_keys(source_text)
        if legacy_refs and legacy_defs:
            source_refs.extend(legacy_refs)
            source_defs.extend(legacy_defs)
        if expected_keys:
            source_refs.extend(
                match.group("key")
                for match in _HTML_SUP_RE.finditer(source_text)
                if match.group("key") in expected_keys
            )
            source_refs.extend(
                match.group("value").translate(_UNICODE_DIGITS)
                for match in _UNICODE_SUP_RE.finditer(source_text)
                if match.group("value").translate(_UNICODE_DIGITS) in expected_keys
            )
    if not source_refs and not source_defs:
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
    if role == "notes" and expected_keys:
        remaining_raw_defs = [
            match.group("html_key") or match.group("number_key")
            for match in _RAW_NOTE_DEF_RE.finditer(target_text)
            if (match.group("html_key") or match.group("number_key")) in expected_keys
        ]
        if remaining_raw_defs:
            errors.append(
                "centralized endnote definitions remain in legacy numbered form: "
                f"{Counter(remaining_raw_defs)}"
            )
        remaining_refs = [
            match.group("key")
            for match in _HTML_SUP_RE.finditer(target_text)
            if match.group("key") in expected_keys
        ]
        if remaining_refs:
            errors.append(
                "centralized endnote superscript references remain after polishing: "
                f"{Counter(remaining_refs)}"
            )
    elif source_defs and _HTML_SUP_RE.search(target_text):
        errors.append("numbered HTML <sup> footnote markers remain after polishing")
    return errors

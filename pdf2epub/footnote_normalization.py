"""Validation helpers for normalizing OCR endnotes to Markdown footnotes."""

from __future__ import annotations

import re
import html
from collections import Counter
from typing import List

from .footnote_keys import key_from_printed_marker


_NOTE_HEADING_RE = re.compile(
    r"^(?P<marks>#{1,6})\s+(?:notes?|endnotes?|注释|注|脚注)\s*$",
    re.IGNORECASE,
)
_HTML_SUP_RE = re.compile(
    r"<sup\b[^>]*>\s*(?P<key>[A-Za-z0-9_-]+)\s*</sup>", re.IGNORECASE
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
_RAW_SUP_NOTE_DEF_RE = re.compile(
    r"^[ \t]{0,3}<sup\b[^>]*>[ \t]*(?P<key>[^<>]+?)[ \t]*</sup>"
    r"[ \t]*(?:[.:：。）)])?[ \t]+",
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
            key
            for match in _RAW_SUP_NOTE_DEF_RE.finditer(text)
            if (key := key_from_printed_marker(html.unescape(match.group("key"))))
            and key != html.unescape(match.group("key")).strip()
        )
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
            match = _RAW_NOTE_DEF_RE.match(line)
            if match:
                k = (
                    match.group("key")
                    if "key" in match.groupdict()
                    else (match.group("html_key") or match.group("number_key"))
                )
                if k:
                    keys.append(k)
            else:
                match = _LEGACY_DEF_RE.match(line)
                if match:
                    keys.append(match.group("key"))
    return keys


def _is_math_or_formula_superscript(
    text: str, start: int, end: int | None = None, *, key: str | None = None
) -> bool:
    """Identify formula or citation-edition superscripts, never year adjacency alone."""
    variable = re.search(r"(?:^|[^A-Za-z0-9])([A-Za-z])$", text[:start])
    if variable and (
        key is None or key.startswith("fnm-letter-")
        or (key.isdigit() and variable.group(1).islower())
    ):
        return True
    if end is None:
        m = re.match(r"<sup[^>]*>.*?</sup>", text[start:], flags=re.IGNORECASE)
        if m:
            end = start + m.end()
    if end is None:
        return False

    line_start = text.rfind("\n", 0, start) + 1
    line_end = text.find("\n", end)
    if line_end < 0:
        line_end = len(text)
    before = text[line_start:start]
    after = text[end:line_end]
    follows_year = bool(re.search(r"\b\d{4}$", before))
    precedes_year = bool(re.match(r"^\d{4}\b", after))
    if not (follows_year or precedes_year):
        return False

    # A citation cue must accompany the year. A bare year plus superscript is
    # ambiguous and remains eligible for normal footnote validation/conversion.
    if _is_explicit_citation_list(text[line_start:line_end]):
        return True
    # A year immediately followed by its page locator is strong local evidence
    # even when this citation is embedded in a longer bibliographic sentence.
    if follows_year and re.match(r"^,\s*pp?\.\s*\d", after[:24], re.IGNORECASE):
        return True
    return not follows_year and _follows_cited_item_in_list(before)


def _is_explicit_citation_list(line: str) -> bool:
    """Accept only a complete, same-line list explicitly introduced as citations."""
    match = re.search(r"\bCitations?:\s*(?P<items>.+)$", line, re.IGNORECASE)
    if not match:
        return False
    sup = r"<sup\b[^>]*>\s*\d+\s*</sup>"
    item = rf"(?:{sup}\s*\d{{4}}|\d{{4}}\s*{sup})(?:,\s*pp?\.\s*\d+(?:[-–]\d+)?)?"
    listing = re.compile(rf"\s*{item}(?:(?:\s*,\s*|\s+and\s+){item})*\s*[.;]?\s*", re.IGNORECASE)
    return bool(listing.fullmatch(match.group("items")))


def _follows_cited_item_in_list(before: str) -> bool:
    """Recognize a superscript-year continuation after a nearby cited year/page item."""
    return bool(
        re.search(
            r"\b\d{4}<sup\b[^>]*>\s*\d+\s*</sup>,\s*pp?\.\s*\d+(?:[-–]\d+)?"
            r"\s*(?:,\s*|and\s+)$",
            before,
            re.IGNORECASE,
        )
    )


def validate_polish_footnote_normalization(
    source_text: str,
    target_text: str,
    *,
    file_role: str | None = None,
    expected_endnote_keys: set[str] | None = None,
) -> List[str]:
    """Validate references and definitions selected by key or Notes structure."""
    # Definitions are excluded by the negative colon lookahead; references
    # after a Notes section still count as references in later body chapters.
    markdown_ref_matches = list(_MARKDOWN_REF_RE.finditer(source_text))
    source_refs = [match.group("key") for match in markdown_ref_matches]
    source_defs = [match.group("key") for match in _MARKDOWN_DEF_RE.finditer(source_text)]
    expected_keys = set(expected_endnote_keys or ())
    role = str(file_role or "").strip().lower()
    if role == "notes":
        source_defs = endnote_definition_keys(source_text, file_role="notes")
        for match in _HTML_SUP_RE.finditer(source_text):
            key = match.group("key")
            if (
                key in expected_keys
                and not _is_math_or_formula_superscript(
                    source_text, match.start(), match.end(), key=key
                )
                and source_text[
                    source_text.rfind("\n", 0, match.start()) + 1:match.start()
                ].strip() != ""
            ):
                source_refs.append(key)
    else:
        legacy_defs = _note_section_keys(source_text)
        # Preserve every definition occurrence.  Numbered source definitions
        # only count inside a confirmed Notes section; Markdown definitions
        # were collected independently above and remain local definitions.
        source_defs.extend(legacy_defs)
        recognized_keys = expected_keys | set(source_defs)
        note_ranges = _note_section_ranges(source_text)
        occupied = [(m.start(), m.end()) for m in markdown_ref_matches]
        for pattern, key_getter, allowed_keys in (
            (_HTML_SUP_RE, lambda m: m.group("key"), recognized_keys),
            (_UNICODE_SUP_RE, lambda m: m.group("value").translate(_UNICODE_DIGITS), expected_keys),
        ):
            for match in pattern.finditer(source_text):
                key = key_getter(match)
                if key not in allowed_keys:
                    continue
                if any(a < match.end() and match.start() < b for a, b in occupied):
                    continue
                if _is_note_definition_occurrence(source_text, match.start(), match.end(), note_ranges):
                    continue
                if _is_math_or_formula_superscript(
                    source_text, match.start(), match.end(), key=key
                ):
                    continue
                source_refs.append(key)
                occupied.append((match.start(), match.end()))
    source_preserved = [
        match.group("key")
        for match in _HTML_SUP_RE.finditer(source_text)
        if _is_math_or_formula_superscript(
            source_text, match.start(), match.end(), key=match.group("key")
        )
    ]
    target_preserved = [
        match.group("key")
        for match in _HTML_SUP_RE.finditer(target_text)
        if _is_math_or_formula_superscript(
            target_text, match.start(), match.end(), key=match.group("key")
        )
    ]
    preservation_errors = (
        [
            "preserved superscript mismatch: "
            f"expected {source_preserved}, found {target_preserved}"
        ]
        if source_preserved != target_preserved
        else []
    )
    if not source_refs and not source_defs:
        return preservation_errors
    target_refs = [match.group("key") for match in _MARKDOWN_REF_RE.finditer(target_text)]
    target_defs = [match.group("key") for match in _MARKDOWN_DEF_RE.finditer(target_text)]
    errors: List[str] = list(preservation_errors)
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
            key_from_printed_marker(match.group("key")) or match.group("key")
            for match in _HTML_SUP_RE.finditer(target_text)
            if (key_from_printed_marker(match.group("key")) or match.group("key")) in expected_keys
            and not _is_math_or_formula_superscript(
                target_text, match.start(), match.end(),
                key=key_from_printed_marker(match.group("key")) or match.group("key")
            )
        ]
        if remaining_refs:
            errors.append(
                "centralized endnote superscript references remain after polishing: "
                f"{Counter(remaining_refs)}"
            )
    else:
        remaining = [
            match.group("key") for match in _HTML_SUP_RE.finditer(target_text)
            if match.group("key") in (expected_keys | set(source_refs))
            and not _is_math_or_formula_superscript(
                target_text, match.start(), match.end(), key=match.group("key")
            )
        ]
        if remaining:
            errors.append("numbered HTML <sup> footnote markers remain after polishing")
    return errors


def _note_section_ranges(text: str) -> list[tuple[int, int]]:
    ranges: list[tuple[int, int]] = []
    active_start: int | None = None
    level = 0
    offset = 0
    for line in text.splitlines(keepends=True):
        heading = _NOTE_HEADING_RE.match(line.strip())
        next_heading = re.match(r"^(#{1,6})\s+", line)
        if active_start is not None and next_heading and len(next_heading.group(1)) <= level:
            ranges.append((active_start, offset))
            active_start = None
        if heading:
            active_start = offset + len(line)
            level = len(heading.group("marks"))
        offset += len(line)
    if active_start is not None:
        ranges.append((active_start, len(text)))
    return ranges


def _is_note_definition_occurrence(
    text: str, start: int, end: int, note_ranges: list[tuple[int, int]]
) -> bool:
    if not any(a <= start < b for a, b in note_ranges):
        return False
    line_start = text.rfind("\n", 0, start) + 1
    prefix = text[line_start:start]
    return bool(re.fullmatch(r"[ \t]{0,3}(?:[-*+][ \t]+)?", prefix))

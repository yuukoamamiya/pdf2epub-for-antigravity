"""Normalize centralized endnotes into Markdown footnote syntax.

Page-footnote detection deliberately excludes typed ``notes`` units.  This
module handles the other side of that boundary: definitions in a ``notes``
unit and matching superscript references in ordinary units.  It is
deterministic and only operates on a derived Markdown stage; source OCR is
never modified.
"""

from __future__ import annotations

import re
from collections import Counter
from typing import Any, Mapping

from ..footnote_normalization import endnote_definition_keys


_KEY = r"[A-Za-z0-9_-]+"
_MARKDOWN_DEF_RE = re.compile(
    rf"^[ \t]*\[\^(?P<key>{_KEY})\]:[ \t]*(?P<content>.*)$",
    re.MULTILINE,
)
_HTML_DEF_RE = re.compile(
    rf"^[ \t]*<sup\b[^>]*>[ \t]*(?P<key>\d+)[ \t]*</sup>"
    rf"[ \t]*(?:[.:：。）)]\s*)?(?P<content>\S.*)$",
    re.IGNORECASE,
)
_NUMBERED_PUNCT_DEF_RE = re.compile(
    r"^[ \t]{0,3}(?P<key>\d+)[ \t]*[.)。：：][ \t]+(?P<content>\S.*)$"
)
_NUMBERED_SPACE_DEF_RE = re.compile(
    r"^[ \t]{0,3}(?P<key>\d+)[ \t]+(?P<content>\S.*)$"
)
_HTML_SUP_RE = re.compile(
    r"<sup\b[^>]*>[ \t]*(?P<key>\d+)[ \t]*</sup>",
    re.IGNORECASE,
)
_UNICODE_SUP_RE = re.compile(r"(?P<value>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
_UNICODE_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_MARKDOWN_REF_RE = re.compile(rf"\[\^(?P<key>{_KEY})\](?!:)")
_RAW_NOTE_DEF_RE = re.compile(
    rf"^[ \t]{0,3}(?:"
    rf"<sup\b[^>]*>[ \t]*(?P<html_key>\d+)[ \t]*</sup>"
    rf"[ \t]*(?:[.:：。）)])?"
    rf"|(?P<number_key>\d+)[ \t]*(?:[.)。：：]|(?=\s))"
    rf")[ \t]+",
    re.IGNORECASE | re.MULTILINE,
)


class EndnoteNormalizationError(ValueError):
    """Raised when centralized endnotes cannot be normalized safely."""


def _definition_line(line: str, *, body_keys: set[str]) -> tuple[str, str, str] | None:
    """Return ``(key, content, kind)`` for one endnote definition line."""
    match = _MARKDOWN_DEF_RE.match(line)
    if match:
        return match.group("key"), match.group("content").strip(), "markdown"

    match = _HTML_DEF_RE.match(line)
    if match:
        return match.group("key"), match.group("content").strip(), "html"

    match = _NUMBERED_PUNCT_DEF_RE.match(line)
    if match:
        return match.group("key"), match.group("content").strip(), "numbered"

    # ``N text`` is intentionally narrower than the other forms.  It is only
    # accepted when the same key is visibly referenced by an ordinary unit;
    # otherwise a numbered prose line in a Notes section could be retyped as a
    # footnote merely because it starts with a number.
    match = _NUMBERED_SPACE_DEF_RE.match(line)
    if match and match.group("key") in body_keys:
        return match.group("key"), match.group("content").strip(), "numbered_space"
    return None


def _raw_body_keys(texts: Mapping[str, str], note_files: set[str]) -> set[str]:
    keys: set[str] = set()
    for name, text in texts.items():
        if name in note_files:
            continue
        keys.update(match.group("key") for match in _MARKDOWN_REF_RE.finditer(text))
        keys.update(match.group("key") for match in _HTML_SUP_RE.finditer(text))
        for match in _UNICODE_SUP_RE.finditer(text):
            keys.add(match.group("value").translate(_UNICODE_DIGITS))
    return keys


def _replace_body_markers(text: str, definition_keys: set[str]) -> tuple[str, int]:
    converted = 0

    def replace_html(match: re.Match[str]) -> str:
        nonlocal converted
        key = match.group("key")
        if key not in definition_keys:
            return match.group(0)
        converted += 1
        return f"[^{key}]"

    text = _HTML_SUP_RE.sub(replace_html, text)

    def replace_unicode(match: re.Match[str]) -> str:
        nonlocal converted
        key = match.group("value").translate(_UNICODE_DIGITS)
        if key not in definition_keys:
            return match.group(0)
        converted += 1
        return f"[^{key}]"

    text = _UNICODE_SUP_RE.sub(replace_unicode, text)
    return text, converted


def normalize_endnotes(
    source_texts: Mapping[str, str],
    file_roles: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, Any]]:
    """Normalize definitions from ``notes`` files and matching body markers.

    The returned report is suitable for inclusion in the normalization
    checkpoint.  Unrelated superscripts remain untouched when their key has no
    corresponding centralized definition.
    """
    note_files = {
        str(name)
        for name, role in file_roles.items()
        if str(role).strip().lower() == "notes" and str(name) in source_texts
    }
    if not note_files:
        return dict(source_texts), {
            "status": "not_applicable",
            "valid": True,
            "source_files": [],
            "definition_keys": [],
            "definition_count": 0,
            "normalized_definition_count": 0,
            "converted_reference_count": 0,
            "reference_counts": {},
            "orphan_definition_keys": [],
            "errors": [],
        }

    body_keys = _raw_body_keys(source_texts, note_files)
    definitions: dict[str, tuple[str, str, str]] = {}
    definition_edits: dict[str, dict[int, str]] = {}
    errors: list[str] = []

    for name in sorted(note_files):
        edits: dict[int, str] = {}
        for line_number, line in enumerate(source_texts[name].splitlines(), 1):
            parsed = _definition_line(line, body_keys=body_keys)
            if parsed is None:
                continue
            key, content, kind = parsed
            if key in definitions:
                previous_name, previous_line, _ = definitions[key]
                errors.append(
                    f"duplicate centralized endnote key {key}: "
                    f"{previous_name}:{previous_line} and {name}:{line_number}"
                )
                continue
            definitions[key] = (name, line_number, kind)
            if kind != "markdown":
                edits[line_number] = f"[^{key}]: {content}".rstrip()
        if edits:
            definition_edits[name] = edits

    if errors:
        raise EndnoteNormalizationError("; ".join(errors))

    if not definitions:
        return dict(source_texts), {
            "status": "no_definitions",
            "valid": True,
            "source_files": sorted(note_files),
            "definition_keys": [],
            "definition_count": 0,
            "normalized_definition_count": 0,
            "converted_reference_count": 0,
            "reference_counts": {},
            "orphan_definition_keys": [],
            "errors": [],
        }

    definition_keys = set(definitions)
    transformed = dict(source_texts)
    normalized_definition_count = 0
    for name, edits in definition_edits.items():
        lines = transformed[name].splitlines()
        for line_number, replacement in edits.items():
            lines[line_number - 1] = replacement
            normalized_definition_count += 1
        trailing_newline = transformed[name].endswith(("\n", "\r"))
        transformed[name] = "\n".join(lines) + ("\n" if trailing_newline else "")

    converted_reference_count = 0
    reference_counts: Counter[str] = Counter()
    for name, text in list(transformed.items()):
        if name in note_files:
            continue
        updated, converted = _replace_body_markers(text, definition_keys)
        transformed[name] = updated
        converted_reference_count += converted
        reference_counts.update(
            match.group("key") for match in _MARKDOWN_REF_RE.finditer(updated)
        )

    orphan_definition_keys = sorted(
        key for key in definition_keys if reference_counts.get(key, 0) == 0
    )
    return transformed, {
        "status": "validated",
        "valid": True,
        "source_files": sorted(note_files),
        "definition_keys": sorted(definition_keys),
        "definition_count": len(definition_keys),
        "normalized_definition_count": normalized_definition_count,
        "converted_reference_count": converted_reference_count,
        "reference_counts": dict(sorted(reference_counts.items())),
        "orphan_definition_keys": orphan_definition_keys,
        "errors": [],
    }


def validate_normalized_endnotes(
    source_texts: Mapping[str, str],
    file_roles: Mapping[str, str],
) -> dict[str, Any]:
    """Check that a build source no longer contains raw centralized endnotes."""
    note_files = {
        str(name)
        for name, role in file_roles.items()
        if str(role).strip().lower() == "notes" and str(name) in source_texts
    }
    if not note_files:
        return {"valid": True, "definition_keys": [], "errors": []}

    definition_keys: set[str] = set()
    errors: list[str] = []
    for name in sorted(note_files):
        keys = set(endnote_definition_keys(source_texts[name], file_role="notes"))
        definition_keys.update(keys)
        raw_defs = [
            match.group("html_key") or match.group("number_key")
            for match in _RAW_NOTE_DEF_RE.finditer(source_texts[name])
        ]
        if raw_defs:
            errors.append(
                f"raw centralized endnote definitions remain in {name}: "
                f"{sorted(set(raw_defs))[:20]}"
            )

    for name, text in source_texts.items():
        if name in note_files:
            continue
        raw_refs = [
            match.group("key")
            for match in _HTML_SUP_RE.finditer(text)
            if match.group("key") in definition_keys
        ]
        raw_refs.extend(
            match.group("value").translate(_UNICODE_DIGITS)
            for match in _UNICODE_SUP_RE.finditer(text)
            if match.group("value").translate(_UNICODE_DIGITS) in definition_keys
        )
        if raw_refs:
            errors.append(
                f"raw centralized endnote references remain in {name}: "
                f"{dict(sorted(Counter(raw_refs).items()))}"
            )

    return {
        "valid": not errors,
        "definition_keys": sorted(definition_keys),
        "errors": errors,
    }


__all__ = [
    "EndnoteNormalizationError",
    "normalize_endnotes",
    "validate_normalized_endnotes",
]

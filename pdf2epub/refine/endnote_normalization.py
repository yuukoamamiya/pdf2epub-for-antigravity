"""Normalize centralized endnotes into Markdown footnote syntax.

Page-footnote detection deliberately excludes typed ``notes`` units.  This
module handles the other side of that boundary: definitions in a ``notes``
unit and matching superscript references in ordinary units.  It is
deterministic and only operates on a derived Markdown stage; source OCR is
never modified.

When the same printed note number restarts in several chapters, a scope
directory (``endnote_scopes``) assigns each definition and reference to its
chapter before keys are rewritten to globally unique ``fnc-...`` keys.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Optional

from ..footnote_normalization import endnote_definition_keys
from .endnote_scopes import scope_for_line, scope_report


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
_INNER_KEY_RE = re.compile(
    r"^(?P<key>\d+)[ \t]*[.)。：:、][ \t]+(?P<content>\S.*)$"
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
_SCOPED_KEY_RE = re.compile(
    r"^fnc-(?P<scope>[A-Za-z0-9]+)-(?P<number>\d+)(?:-o\d+)?$"
)


class EndnoteNormalizationError(ValueError):
    """Raised when centralized endnotes cannot be normalized safely."""


def _definition_line(
    line: str,
    *,
    body_keys: set[str],
    previous_line_blank: bool = True,
) -> tuple[str, str, str] | None:
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
    # accepted when the same key is visibly referenced by an ordinary unit and
    # the line starts a new paragraph; otherwise a wrapped date or citation
    # continuation could be retyped as a footnote.
    if previous_line_blank:
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


def _empty_report(status: str, note_files: set[str]) -> dict[str, Any]:
    return {
        "status": status,
        "valid": True,
        "source_files": sorted(note_files),
        "definition_keys": [],
        "definition_count": 0,
        "normalized_definition_count": 0,
        "converted_reference_count": 0,
        "reference_counts": {},
        "orphan_definition_keys": [],
        "scope_mode": "global",
        "scope_count": 0,
        "scopes": [],
        "scoped_keys": [],
        "double_numbered_definition_count": 0,
        "duplicate_definition_occurrences": [],
        "orphan_scoped_definitions": [],
        "errors": [],
    }


def _note_file_order(
    scopes: Mapping[str, Any],
    note_files: set[str],
    source_texts: Mapping[str, str],
) -> list[str]:
    ordered: list[str] = []
    for name in scopes.get("notes_files", []) or []:
        if name in note_files and name not in ordered:
            ordered.append(name)
    for name in sorted(note_files):
        if name not in ordered:
            ordered.append(name)
    return ordered


def _scope_ref_map(scopes: Mapping[str, Any]) -> dict[str, dict[str, list[dict[str, Any]]]]:
    grouped: dict[str, dict[str, list[dict[str, Any]]]] = {}
    for scope_key, items in (scopes.get("scope_ref_keys", {}) or {}).items():
        by_key: dict[str, list[dict[str, Any]]] = defaultdict(list)
        for item in items or []:
            by_key[str(item.get("key"))].append(item)
        grouped[str(scope_key)] = dict(by_key)
    return grouped


def _apply_double_numbering(
    *,
    key: str,
    content: str,
    scope_key: Optional[str],
    ref_keys: Mapping[str, list[dict[str, Any]]],
) -> tuple[str, str, bool]:
    """Return ``(key, content, applied)`` for a two-level numbered line.

    The outer marker is a page-local counter; the inner marker is the real
    note number.  The inner value is accepted only when the chapter's own body
    references contain it and it does not precede the outer counter.  That
    keeps ordinary content such as ``3. 2005 was ...`` from becoming key 2005.
    """
    if not scope_key or not key.isdigit():
        return key, content, False
    match = _INNER_KEY_RE.match(content)
    if match is None:
        return key, content, False
    inner = match.group("key")
    if inner not in ref_keys.get(scope_key, {}):
        return key, content, False
    if int(inner) < int(key):
        return key, content, False
    return inner, match.group("content").strip(), True


def _scoped_keys(occurrences: list[dict[str, Any]]) -> set[str]:
    scopes_by_key: dict[str, set[Optional[str]]] = defaultdict(set)
    counts: Counter[tuple[Optional[str], str]] = Counter()
    for occurrence in occurrences:
        key = occurrence["key"]
        scope_key = occurrence["scope_key"]
        scopes_by_key[key].add(scope_key)
        counts[(scope_key, key)] += 1

    scoped: set[str] = set()
    for key, values in scopes_by_key.items():
        real_scopes = {value for value in values if value is not None}
        if len(real_scopes) >= 2:
            scoped.add(key)
        elif None in values and len(values) >= 2:
            scoped.add(key)
    for (_scope_key, key), count in counts.items():
        if count > 1:
            scoped.add(key)
    return scoped


def _apply_span_edits(
    text: str, edits: list[tuple[int, int, str]]
) -> str:
    if not edits:
        return text
    lines = text.splitlines(keepends=True)
    line_starts: list[int] = []
    offset = 0
    for line in lines:
        line_starts.append(offset)
        offset += len(line)
    absolute = [
        (line_starts[start_line - 1] + start, line_starts[start_line - 1] + end, replacement)
        for start_line, start, end, replacement in edits
    ]
    for start, end, replacement in sorted(absolute, reverse=True):
        text = text[:start] + replacement + text[end:]
    return text


def _replace_scoped_references(
    transformed: dict[str, str],
    *,
    scope_refs: Mapping[str, Mapping[str, list[dict[str, Any]]]],
    scoped_keys: set[str],
    definitions_by_scope_key: Mapping[
        tuple[str, str], list[dict[str, Any]]
    ],
) -> tuple[int, list[str]]:
    edits_by_file: dict[str, list[tuple[int, int, int, str]]] = defaultdict(list)
    errors: list[str] = []
    converted = 0
    for scope_key, by_key in scope_refs.items():
        for key, references in by_key.items():
            if key not in scoped_keys:
                continue
            definitions = definitions_by_scope_key.get((scope_key, key), [])
            if not definitions:
                errors.append(
                    f"endnote key {key} is referenced in scope {scope_key} "
                    "but has no definition in that chapter"
                )
                continue
            if len(references) > len(definitions):
                errors.append(
                    f"endnote key {key} has {len(references)} references but only "
                    f"{len(definitions)} definitions in scope {scope_key}"
                )
                continue
            for index, reference in enumerate(references):
                target = definitions[index]["output_key"]
                edits_by_file[reference["file"]].append(
                    (
                        int(reference["line"]),
                        int(reference["start"]),
                        int(reference["end"]),
                        f"[^{target}]",
                    )
                )
                converted += 1
    for name, edits in edits_by_file.items():
        transformed[name] = _apply_span_edits(transformed[name], edits)
    return converted, errors


def _scope_summary(
    scopes: Mapping[str, Any],
    occurrences: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    summary = scope_report(scopes)
    counts = Counter(
        occurrence["scope_key"]
        for occurrence in occurrences
        if occurrence.get("scope_key")
    )
    for item in summary:
        item["definition_count"] = counts.get(item["scope_key"], 0)
    return summary


def _normalize_scoped(
    source_texts: Mapping[str, str],
    note_files: set[str],
    scopes: Mapping[str, Any],
    *,
    body_keys: set[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    file_order = _note_file_order(scopes, note_files, source_texts)
    file_order_index = {name: index for index, name in enumerate(file_order)}
    scope_refs = _scope_ref_map(scopes)

    occurrences: list[dict[str, Any]] = []
    for name in file_order:
        lines = source_texts[name].splitlines()
        for line_number, line in enumerate(lines, 1):
            previous_line_blank = line_number == 1 or not lines[line_number - 2].strip()
            parsed = _definition_line(
                line,
                body_keys=body_keys,
                previous_line_blank=previous_line_blank,
            )
            if parsed is None:
                continue
            raw_key, content, kind = parsed
            scope_key = scope_for_line(scopes, name, line_number)
            key, content, double_numbered = _apply_double_numbering(
                key=raw_key,
                content=content,
                scope_key=scope_key,
                ref_keys=scope_refs,
            )
            occurrences.append(
                {
                    "file": name,
                    "line": line_number,
                    "raw_key": raw_key,
                    "key": key,
                    "content": content,
                    "kind": kind,
                    "scope_key": scope_key,
                    "double_numbered": double_numbered,
                    "output_key": key,
                }
            )

    if not occurrences:
        report = _empty_report("no_definitions", note_files)
        report["scope_mode"] = "global"
        report["scope_count"] = len(scopes.get("scopes", {}))
        report["scopes"] = _scope_summary(scopes, occurrences)
        return dict(source_texts), report

    scoped_keys = _scoped_keys(occurrences)
    scope_mode = "per_chapter" if scoped_keys else "global"
    errors: list[str] = []

    definitions_by_scope_key: dict[
        tuple[Optional[str], str], list[dict[str, Any]]
    ] = defaultdict(list)
    for occurrence in occurrences:
        definitions_by_scope_key[
            (occurrence["scope_key"], occurrence["key"])
        ].append(occurrence)

    if scoped_keys:
        for occurrence in occurrences:
            if (
                occurrence["key"] in scoped_keys
                and occurrence["scope_key"] is None
            ):
                errors.append(
                    f"endnote key {occurrence['key']} at "
                    f"{occurrence['file']}:{occurrence['line']} has no chapter "
                    "scope; restore the missing chapter heading or fix the TOC"
                )
        for (scope_key, key), definitions in definitions_by_scope_key.items():
            if key not in scoped_keys or scope_key is None:
                continue
            scope = scopes.get("scopes", {}).get(scope_key) or {}
            if not scope.get("body_files"):
                errors.append(
                    f"endnote scope {scope_key} ({scope.get('title', '')}) has "
                    "definitions but no body span; restore the chapter heading "
                    "or fix the TOC"
                )
        for scope_key, by_key in scope_refs.items():
            for key, references in by_key.items():
                if key not in scoped_keys:
                    continue
                definitions = definitions_by_scope_key.get((scope_key, key), [])
                if not definitions:
                    errors.append(
                        f"endnote key {key} is referenced in scope {scope_key} "
                        "but has no definition in that chapter; check the TOC, "
                        "chapter headings, or scope assignment"
                    )
                elif len(references) > len(definitions):
                    errors.append(
                        f"endnote key {key} has {len(references)} references but "
                        f"only {len(definitions)} definitions in scope "
                        f"{scope_key}; check the scope assignment"
                    )
        if errors:
            raise EndnoteNormalizationError("; ".join(errors))

    scoped_definitions = sorted(
        occurrences,
        key=lambda item: (file_order_index.get(item["file"], 0), item["line"]),
    )
    for occurrence in scoped_definitions:
        if occurrence["key"] not in scoped_keys:
            occurrence["output_key"] = occurrence["key"]
            continue
        if occurrence["scope_key"] is None:
            occurrence["output_key"] = occurrence["key"]
            continue
        siblings = definitions_by_scope_key[
            (occurrence["scope_key"], occurrence["key"])
        ]
        siblings.sort(key=lambda item: (file_order_index.get(item["file"], 0), item["line"]))
        index = siblings.index(occurrence) + 1
        base = f"fnc-{occurrence['scope_key']}-{occurrence['key']}"
        occurrence["output_key"] = base if index == 1 else f"{base}-o{index}"

    definition_edits: dict[str, dict[int, str]] = defaultdict(dict)
    for occurrence in occurrences:
        changed = (
            occurrence["output_key"] != occurrence["raw_key"]
            or occurrence["double_numbered"]
        )
        if occurrence["kind"] == "markdown" and not changed:
            continue
        definition_edits[occurrence["file"]][occurrence["line"]] = (
            f"[^{occurrence['output_key']}]: {occurrence['content']}".rstrip()
        )

    transformed = dict(source_texts)
    normalized_definition_count = 0
    for name, edits in definition_edits.items():
        lines = transformed[name].splitlines()
        for line_number, replacement in edits.items():
            lines[line_number - 1] = replacement
            normalized_definition_count += 1
        trailing_newline = transformed[name].endswith(("\n", "\r"))
        transformed[name] = "\n".join(lines) + ("\n" if trailing_newline else "")

    converted_reference_count, ref_errors = _replace_scoped_references(
        transformed,
        scope_refs=scope_refs,
        scoped_keys=scoped_keys,
        definitions_by_scope_key={
            key: sorted(
                value,
                key=lambda item: (
                    file_order_index.get(item["file"], 0),
                    item["line"],
                ),
            )
            for key, value in definitions_by_scope_key.items()
        },
    )
    if ref_errors:
        raise EndnoteNormalizationError("; ".join(ref_errors))

    plain_definition_keys = {
        occurrence["output_key"]
        for occurrence in occurrences
        if occurrence["key"] not in scoped_keys
    }
    for name, text in list(transformed.items()):
        if name in note_files:
            continue
        updated, converted = _replace_body_markers(text, plain_definition_keys)
        transformed[name] = updated
        converted_reference_count += converted

    reference_counts: Counter[str] = Counter()
    for name, text in transformed.items():
        if name in note_files:
            continue
        reference_counts.update(
            match.group("key") for match in _MARKDOWN_REF_RE.finditer(text)
        )

    output_keys = {occurrence["output_key"] for occurrence in occurrences}
    orphan_keys = sorted(
        key for key in output_keys if reference_counts.get(key, 0) == 0
    )
    duplicate_occurrences = []
    orphan_scoped = []
    ref_counts_by_scope_key = {
        (scope_key, key): len(references)
        for scope_key, by_key in scope_refs.items()
        for key, references in by_key.items()
    }
    for (scope_key, key), definitions in definitions_by_scope_key.items():
        if key not in scoped_keys or scope_key is None:
            continue
        definitions.sort(
            key=lambda item: (file_order_index.get(item["file"], 0), item["line"])
        )
        if len(definitions) > 1:
            duplicate_occurrences.append(
                {
                    "scope_key": scope_key,
                    "key": key,
                    "occurrences": [
                        {"file": item["file"], "line": item["line"]}
                        for item in definitions
                    ],
                    "output_keys": [item["output_key"] for item in definitions],
                }
            )
        reference_count = ref_counts_by_scope_key.get((scope_key, key), 0)
        for item in definitions[reference_count:]:
            orphan_scoped.append(
                {
                    "scope_key": scope_key,
                    "key": key,
                    "output_key": item["output_key"],
                    "file": item["file"],
                    "line": item["line"],
                }
            )

    return transformed, {
        "status": "validated",
        "valid": True,
        "source_files": list(file_order),
        "definition_keys": sorted(output_keys),
        "definition_count": len(output_keys),
        "normalized_definition_count": normalized_definition_count,
        "converted_reference_count": converted_reference_count,
        "reference_counts": dict(sorted(reference_counts.items())),
        "orphan_definition_keys": orphan_keys,
        "scope_mode": scope_mode,
        "scope_count": len(scopes.get("scopes", {})),
        "scopes": _scope_summary(scopes, occurrences),
        "scoped_keys": sorted(scoped_keys),
        "double_numbered_definition_count": sum(
            1 for occurrence in occurrences if occurrence["double_numbered"]
        ),
        "duplicate_definition_occurrences": duplicate_occurrences,
        "orphan_scoped_definitions": orphan_scoped,
        "errors": [],
    }


def _normalize_legacy(
    source_texts: Mapping[str, str],
    note_files: set[str],
) -> tuple[dict[str, str], dict[str, Any]]:
    body_keys = _raw_body_keys(source_texts, note_files)
    definitions: dict[str, tuple[str, str, str]] = {}
    definition_edits: dict[str, dict[int, str]] = {}
    errors: list[str] = []

    for name in sorted(note_files):
        edits: dict[int, str] = {}
        lines = source_texts[name].splitlines()
        for line_number, line in enumerate(lines, 1):
            previous_line_blank = line_number == 1 or not lines[line_number - 2].strip()
            parsed = _definition_line(
                line,
                body_keys=body_keys,
                previous_line_blank=previous_line_blank,
            )
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
        return dict(source_texts), _empty_report("no_definitions", note_files)

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
    report = _empty_report("validated", note_files)
    report.update(
        {
            "definition_keys": sorted(definition_keys),
            "definition_count": len(definition_keys),
            "normalized_definition_count": normalized_definition_count,
            "converted_reference_count": converted_reference_count,
            "reference_counts": dict(sorted(reference_counts.items())),
            "orphan_definition_keys": orphan_definition_keys,
        }
    )
    return transformed, report


def normalize_endnotes(
    source_texts: Mapping[str, str],
    file_roles: Mapping[str, str],
    *,
    scopes: Optional[Mapping[str, Any]] = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Normalize definitions from ``notes`` files and matching body markers.

    Without a scope directory the historical global behavior is preserved.
    With scopes, only printed keys that restart across chapters are rewritten;
    all other keys keep their original numeric form.
    """
    note_files = {
        str(name)
        for name, role in file_roles.items()
        if str(role).strip().lower() == "notes" and str(name) in source_texts
    }
    if not note_files:
        return dict(source_texts), _empty_report("not_applicable", note_files)
    if scopes is None:
        return _normalize_legacy(source_texts, note_files)
    body_keys = _raw_body_keys(source_texts, note_files)
    return _normalize_scoped(
        source_texts,
        note_files,
        scopes,
        body_keys=body_keys,
    )


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

    scoped_numbers: set[str] = set()
    for key in definition_keys:
        match = _SCOPED_KEY_RE.match(key)
        if match:
            scoped_numbers.add(match.group("number"))

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
            continue
        residual_scoped = [
            match.group("key")
            for match in _HTML_SUP_RE.finditer(text)
            if match.group("key") in scoped_numbers
        ]
        residual_scoped.extend(
            match.group("value").translate(_UNICODE_DIGITS)
            for match in _UNICODE_SUP_RE.finditer(text)
            if match.group("value").translate(_UNICODE_DIGITS) in scoped_numbers
        )
        if residual_scoped:
            errors.append(
                f"raw scoped endnote references remain in {name}: "
                f"{dict(sorted(Counter(residual_scoped).items()))}"
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

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

import hashlib
import html
import json
import re
from collections import Counter, defaultdict
from typing import Any, Mapping, Optional

from ..footnote_normalization import (
    _is_math_or_formula_superscript,
    endnote_definition_keys,
)
from ..footnote_keys import key_from_printed_marker
from .endnote_review import NUMBERED_PUNCT_DEF_RE as _NUMBERED_PUNCT_DEF_RE
from .endnote_review import is_ambiguous_definition_line
from .endnote_review import inline_endnote_candidates
from .endnote_scopes import markdown_fence_mask, scope_for_line, scope_report


_KEY = r"[A-Za-z0-9_-]+"
_MARKDOWN_DEF_RE = re.compile(
    rf"^[ \t]*\[\^(?P<key>{_KEY})\]:[ \t]*(?P<content>.*)$",
    re.MULTILINE,
)
_HTML_DEF_RE = re.compile(
    rf"^[ \t]*<sup\b[^>]*>[ \t]*(?P<key>[^<>]+?)[ \t]*</sup>"
    rf"[ \t]*(?:[.:：。）)]\s*)?(?P<content>\S.*)$",
    re.IGNORECASE,
)
_NUMBERED_SPACE_DEF_RE = re.compile(
    r"^[ \t]{0,3}(?P<key>\d+)[ \t]+(?P<content>\S.*)$"
)
_INNER_KEY_RE = re.compile(
    r"^(?P<key>\d+)[ \t]*[.)。：:、][ \t]+(?P<content>\S.*)$"
)
_HTML_SUP_RE = re.compile(
    r"<sup\b[^>]*>[ \t]*(?P<key>[^<>]+?)[ \t]*</sup>",
    re.IGNORECASE,
)
_UNICODE_SUP_RE = re.compile(r"(?P<value>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
_UNICODE_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_MARKDOWN_REF_RE = re.compile(rf"\[\^(?P<key>{_KEY})\](?!:)")
_RAW_NOTE_DEF_RE = re.compile(
    rf"^[ \t]{0,3}(?:"
    rf"<sup\b[^>]*>[ \t]*(?P<html_key>\d+)[ \t]*</sup>"
    rf"[ \t]*(?:[.:：。）)])?"
    rf"|(?:[-*+][ \t]+)?(?P<number_key>\d+)[ \t]*(?:[.)。：：]|(?=\s))"
    rf")[ \t]+",
    re.IGNORECASE | re.MULTILINE,
)
_RAW_SUP_NOTE_DEF_RE = re.compile(
    r"^[ \t]{0,3}<sup\b[^>]*>[ \t]*(?P<key>[^<>]+?)[ \t]*</sup>"
    r"[ \t]*(?:[.:：。）)])?[ \t]+\S.*$",
    re.IGNORECASE | re.MULTILINE,
)
_SCOPED_KEY_RE = re.compile(
    r"^fnc-(?P<scope>[A-Za-z0-9]+)-(?P<number>\d+)(?:-o\d+)?$"
)
_SCOPED_MARKER_RE = re.compile(
    r"^fnc-[A-Za-z0-9]+-(?P<marker>fnm-.+?)(?:-o\d+)?$"
)
_INLINE_NOTE_RE = re.compile(r" {2,}<sup\b[^>]*>[ \t]*(?P<key>\d+)[ \t]*</sup>[ \t]+", re.I)


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
        key = key_from_printed_marker(html.unescape(match.group("key")))
        if key and (
            not key.startswith("fnm-letter-")
            or key in body_keys
        ):
            return key, match.group("content").strip(), "html"

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


def _normalized_decision_file(value: Any) -> str:
    return str(value or "").replace("\\", "/")


def _definition_decision_map(
    definition_decisions: Any,
) -> dict[tuple[str, int], str]:
    """Normalize reviewed decisions to ``(file, line) -> role``."""
    result: dict[tuple[str, int], str] = {}
    if definition_decisions is None:
        return result
    values: Any = definition_decisions
    if isinstance(values, Mapping) and isinstance(values.get("decisions"), list):
        values = values["decisions"]
    if isinstance(values, Mapping):
        for raw_key, role in values.items():
            if isinstance(raw_key, tuple) and len(raw_key) == 2:
                try:
                    identity = (
                        _normalized_decision_file(raw_key[0]),
                        int(raw_key[1]),
                    )
                except (TypeError, ValueError):
                    continue
                result[identity] = str(role).strip().lower()
        return result
    if isinstance(values, (list, tuple)):
        for item in values:
            if not isinstance(item, Mapping):
                continue
            if item.get("kind", "line") != "line":
                continue
            try:
                identity = (
                    _normalized_decision_file(item["file"]),
                    int(item["line"]),
                )
            except (KeyError, TypeError, ValueError):
                continue
            result[identity] = str(item.get("role") or "").strip().lower()
    return result


def _expand_reviewed_inline_definitions(source_texts: Mapping[str, str], decisions: Any):
    """Create virtual definition lines while retaining original line ownership.

    This is an in-memory projection only. Every emitted character comes from
    the source; only the reviewed two-space boundary is represented as a line
    boundary. Continuation markers remain literal content.
    """
    values = decisions.get("decisions", []) if isinstance(decisions, Mapping) else decisions
    inline = {}
    if isinstance(values, (list, tuple)):
        for item in values:
            if isinstance(item, Mapping) and item.get("kind") == "inline":
                try:
                    if any(isinstance(item.get(field), bool) or not isinstance(item.get(field), int) for field in ("line", "column")):
                        raise ValueError
                    inline[(_normalized_decision_file(item["file"]), item["line"], item["column"])] = str(item.get("role") or "").lower()
                except EndnoteNormalizationError:
                    raise
                except (KeyError, TypeError, ValueError):
                    continue
    if not inline:
        return dict(source_texts), decisions, {}, {}
    expanded, origins, columns = dict(source_texts), {}, {}
    for name, text in source_texts.items():
        out, origin_map, col_map = [], {}, {}
        for original_line, line in enumerate(text.splitlines(), 1):
            matches = list(_INLINE_NOTE_RE.finditer(line))
            selected = [(i, m) for i, m in enumerate(matches) if (name, original_line, m.start("key") + 1) in inline]
            if not selected:
                out.append(line); origin_map[len(out)] = original_line; continue
            definitions = [(i, m) for i, m in selected if inline[(name, original_line, m.start("key") + 1)] == "definition"]
            if not definitions:
                out.append(line); origin_map[len(out)] = original_line; continue
            cursor = 0
            first_definition = definitions[0][1]
            out.append(line[:first_definition.start()].rstrip())
            origin_map[len(out)] = original_line
            for definition_index, (match_index, match) in enumerate(definitions):
                content_end = (
                    definitions[definition_index + 1][1].start()
                    if definition_index + 1 < len(definitions) else len(line)
                )
                content = line[match.end():content_end].strip()
                marker_line = f"[^{match.group('key')}]: {content}"
                out.append(marker_line)
                origin_map[len(out)] = original_line
                col_map[(name, original_line, match.start("key") + 1)] = len(out)
                cursor = content_end
            tail = line[cursor:].strip()
            if tail:
                out.append(tail); origin_map[len(out)] = original_line
        ending = "\n" if text.endswith(("\n", "\r")) else ""
        expanded[name] = "\n".join(out) + ending
        origins[name] = origin_map
        columns[name] = col_map
    return expanded, decisions, origins, columns


def _remap_inline_scope_directory(scopes: Optional[Mapping[str, Any]], origins: Mapping[str, Any]):
    if scopes is None or not origins:
        return scopes
    result = dict(scopes)
    spans_by_file = {}
    for name, spans in (scopes.get("notes_spans", {}) or {}).items():
        origin = origins.get(name)
        if not origin:
            spans_by_file[name] = spans
            continue
        mapped = []
        for span in spans:
            members = [virtual for virtual, original in origin.items() if int(span["start_line"]) <= original < int(span["end_line"])]
            if members:
                mapped.append({**span, "start_line": min(members), "end_line": max(members) + 1})
        spans_by_file[name] = mapped
    result["notes_spans"] = spans_by_file
    unmatched = []
    for item in scopes.get("unmatched_notes_headings", []) or []:
        name = str(item.get("file") or "")
        try:
            line_number = int(item.get("line", -1))
        except (TypeError, ValueError):
            unmatched.append(item)
            continue
        remapped_lines = [
            virtual for virtual, original in origins.get(name, {}).items()
            if original == line_number
        ]
        unmatched.extend({**item, "line": virtual} for virtual in remapped_lines)
    result["unmatched_notes_headings"] = unmatched
    return result


def _decision_digest(decisions: Mapping[tuple[str, int], str]) -> Optional[str]:
    if not decisions:
        return None
    canonical = [
        {"file": file, "line": line, "role": role}
        for (file, line), role in sorted(decisions.items())
    ]
    payload = json.dumps(canonical, ensure_ascii=False, sort_keys=True)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


def _definition_decision_errors(
    *,
    name: str,
    line_number: int,
    line: str,
    previous_line: Optional[str],
    decisions: Mapping[tuple[str, int], str],
) -> tuple[bool, Optional[str]]:
    """Return ``(skip, error)`` for one candidate definition line.

    Only numbered punctuation definitions with an unclear preceding boundary
    consult the reviewed decisions.  Everything else keeps the historical
    deterministic behavior.
    """
    if not is_ambiguous_definition_line(line, previous_line, line_number):
        return False, None
    decision = decisions.get((_normalized_decision_file(name), line_number))
    if decision == "continuation":
        return True, None
    if decision == "definition":
        return False, None
    return True, (
        f"ambiguous endnote definition at {name}:{line_number} has no reviewed "
        "definition/continuation decision"
    )


def _is_notes_heading(
    lines: list[str],
    index: int,
    *,
    scopes: Optional[Mapping[str, Any]] = None,
    name: Optional[str] = None,
) -> bool:
    """Recognize Markdown headings and headings explicitly found by scope review."""
    line = lines[index]
    if re.match(r"^[ \t]{0,3}#{1,6}\s+\S", line):
        return True
    if scopes is None or name is None:
        return False
    line_number = index + 1
    if any(
        str(item.get("file")) == name and int(item.get("line", -1)) == line_number
        for item in scopes.get("unmatched_notes_headings", []) or []
    ):
        return True
    spans = (scopes.get("notes_spans", {}) or {}).get(name, [])
    return any(
        int(span.get("start_line", -1)) == line_number
        for span in spans
        if isinstance(span, Mapping)
    )


def _definition_continuations(
    lines: list[str],
    fenced_lines: list[bool],
    start: int,
    end: int,
    *,
    name: str,
    scopes: Optional[Mapping[str, Any]],
    scope_key: Optional[str],
) -> list[int]:
    """Collect note continuation blocks without crossing a heading or scope.

    In typed Notes units, unindented text after a blank line is part of the
    current note until another definition or standalone heading starts. The
    returned source lines are later indented as Markdown footnote content.
    """
    continuation_indexes: list[int] = []
    for index in range(start + 1, end):
        line = lines[index]
        if not fenced_lines[index] and _is_notes_heading(
            lines, index, scopes=scopes, name=name
        ):
            break
        if scopes is not None and scope_key is not None:
            line_scope = scope_for_line(scopes, name, index + 1)
            if line_scope != scope_key:
                break
        if line.strip():
            continuation_indexes.append(index)
    return continuation_indexes


def _next_definition_line(
    lines: list[str],
    fenced_lines: list[bool],
    start: int,
    body_keys: set[str],
    *,
    name: str,
    decisions: Mapping[tuple[str, int], str],
    scopes: Optional[Mapping[str, Any]],
) -> int:
    """Find the next typed endnote boundary after a definition marker."""
    for index in range(start + 1, len(lines)):
        if fenced_lines[index]:
            continue
        if _is_notes_heading(lines, index, scopes=scopes, name=name):
            return index
        previous_blank = index == 0 or not lines[index - 1].strip()
        if _definition_line(
            lines[index], body_keys=body_keys, previous_line_blank=previous_blank
        ) is not None and decisions.get((_normalized_decision_file(name), index + 1)) != "continuation":
            return index
    return len(lines)


def _raw_body_keys(texts: Mapping[str, str], note_files: set[str]) -> set[str]:
    keys: set[str] = set()
    for name, text in texts.items():
        if name in note_files:
            continue
        lines = text.splitlines()
        fenced_lines = markdown_fence_mask(lines)
        body_text = "\n".join(
            line for index, line in enumerate(lines) if not fenced_lines[index]
        )
        keys.update(match.group("key") for match in _MARKDOWN_REF_RE.finditer(body_text))
        keys.update(
            key
            for match in _HTML_SUP_RE.finditer(body_text)
            if (key := key_from_printed_marker(html.unescape(match.group("key"))))
            and not _is_math_or_formula_superscript(
                body_text, match.start(), match.end(), key=key
            )
        )
        for match in _UNICODE_SUP_RE.finditer(body_text):
            keys.add(match.group("value").translate(_UNICODE_DIGITS))
    return keys


def _replace_body_markers(text: str, definition_keys: set[str]) -> tuple[str, int]:
    converted = 0

    def replace_html(match: re.Match[str]) -> str:
        nonlocal converted
        key = key_from_printed_marker(html.unescape(match.group("key")))
        if not key:
            return match.group(0)
        if _is_math_or_formula_superscript(
            match.string, match.start(), match.end(), key=key
        ):
            return match.group(0)
        if key not in definition_keys:
            return match.group(0)
        converted += 1
        return f"[^{key}]"

    def replace_unicode(match: re.Match[str]) -> str:
        nonlocal converted
        key = match.group("value").translate(_UNICODE_DIGITS)
        if key not in definition_keys:
            return match.group(0)
        converted += 1
        return f"[^{key}]"

    lines = text.splitlines(keepends=True)
    fenced = markdown_fence_mask([line.rstrip("\r\n") for line in lines])
    output: list[str] = []
    for index, line in enumerate(lines):
        if fenced[index]:
            output.append(line)
            continue
        output.append(
            _UNICODE_SUP_RE.sub(replace_unicode, _HTML_SUP_RE.sub(replace_html, line))
        )
    return "".join(output), converted


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
        "endnote_definition_decisions_sha256": None,
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
            if len(definitions) > 1 and len(references) > len(definitions):
                errors.append(
                    f"endnote key {key} has {len(references)} references but only "
                    f"{len(definitions)} definitions in scope {scope_key}"
                )
                continue
            targets = (
                [definitions[0]] * len(references)
                if len(definitions) == 1
                else definitions[: len(references)]
            )
            for definition, reference in zip(targets, references):
                target = definition["output_key"]
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
    definition_decisions: Optional[Mapping[tuple[str, int], str]] = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    file_order = _note_file_order(scopes, note_files, source_texts)
    file_order_index = {name: index for index, name in enumerate(file_order)}
    scope_refs = _scope_ref_map(scopes)
    decisions = dict(definition_decisions or {})

    occurrences: list[dict[str, Any]] = []
    definition_errors: list[str] = []
    for name in file_order:
        lines = source_texts[name].splitlines()
        fenced_lines = markdown_fence_mask(lines)
        for line_index, line in enumerate(lines):
            if fenced_lines[line_index]:
                continue
            line_number = line_index + 1
            previous_line_blank = line_number == 1 or not lines[line_number - 2].strip()
            previous_line = lines[line_number - 2] if line_number > 1 else None
            parsed = _definition_line(
                line,
                body_keys=body_keys,
                previous_line_blank=previous_line_blank,
            )
            if parsed is None:
                continue
            skip, error = _definition_decision_errors(
                name=name,
                line_number=line_number,
                line=line,
                previous_line=previous_line,
                decisions=decisions,
            )
            if error:
                definition_errors.append(error)
                continue
            if skip:
                continue
            raw_key, content, kind = parsed
            scope_key = scope_for_line(scopes, name, line_number)
            continuation_indexes = _definition_continuations(
                lines,
                fenced_lines,
                line_index,
                _next_definition_line(
                    lines,
                    fenced_lines,
                    line_index,
                    body_keys,
                    name=name,
                    decisions=decisions,
                    scopes=scopes,
                ),
                name=name,
                scopes=scopes,
                scope_key=scope_key,
            )
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
                    "continuation_lines": continuation_indexes,
                    "kind": kind,
                    "scope_key": scope_key,
                    "double_numbered": double_numbered,
                    "output_key": key,
                }
            )

    if definition_errors:
        raise EndnoteNormalizationError("; ".join(definition_errors))

    if not occurrences:
        report = _empty_report("no_definitions", note_files)
        report["scope_mode"] = "global"
        report["scope_count"] = len(scopes.get("scopes", {}))
        report["scopes"] = _scope_summary(scopes, occurrences)
        report["endnote_definition_decisions_sha256"] = _decision_digest(decisions)
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
                elif len(definitions) > 1 and len(references) > len(definitions):
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
    lines_by_file = {name: text.splitlines() for name, text in source_texts.items()}
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
        if occurrence["kind"] != "markdown":
            for line_index in occurrence["continuation_lines"]:
                line = lines_by_file[occurrence["file"]][line_index]
                definition_edits[occurrence["file"]][line_index + 1] = (
                    f"    {line}"
                )

    transformed = dict(source_texts)
    normalized_definition_count = 0
    for name, edits in definition_edits.items():
        lines = transformed[name].splitlines()
        for line_number, replacement in edits.items():
            lines[line_number - 1] = replacement
            if re.match(r"^\[\^[^]]+\]:", replacement):
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
        "endnote_definition_decisions_sha256": _decision_digest(decisions),
        "errors": [],
    }


def _normalize_legacy(
    source_texts: Mapping[str, str],
    note_files: set[str],
    *,
    definition_decisions: Optional[Mapping[tuple[str, int], str]] = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    body_keys = _raw_body_keys(source_texts, note_files)
    decisions = dict(definition_decisions or {})
    definitions: dict[str, tuple[str, str, str]] = {}
    definition_edits: dict[str, dict[int, str]] = {}
    errors: list[str] = []

    for name in sorted(note_files):
        edits: dict[int, str] = {}
        lines = source_texts[name].splitlines()
        fenced_lines = markdown_fence_mask(lines)
        for line_index, line in enumerate(lines):
            if fenced_lines[line_index]:
                continue
            line_number = line_index + 1
            previous_line_blank = line_number == 1 or not lines[line_number - 2].strip()
            previous_line = lines[line_number - 2] if line_number > 1 else None
            parsed = _definition_line(
                line,
                body_keys=body_keys,
                previous_line_blank=previous_line_blank,
            )
            if parsed is None:
                continue
            skip, error = _definition_decision_errors(
                name=name,
                line_number=line_number,
                line=line,
                previous_line=previous_line,
                decisions=decisions,
            )
            if error:
                errors.append(error)
                continue
            if skip:
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
                continuation_indexes = _definition_continuations(
                    lines,
                    fenced_lines,
                    line_index,
                    _next_definition_line(
                        lines,
                        fenced_lines,
                        line_index,
                        body_keys,
                        name=name,
                        decisions=decisions,
                        scopes=None,
                    ),
                    name=name,
                    scopes=None,
                    scope_key=None,
                )
                for continuation_index in continuation_indexes:
                    continuation_line = lines[continuation_index]
                    edits[continuation_index + 1] = f"    {continuation_line}"
        if edits:
            definition_edits[name] = edits

    if errors:
        raise EndnoteNormalizationError("; ".join(errors))

    if not definitions:
        report = _empty_report("no_definitions", note_files)
        report["endnote_definition_decisions_sha256"] = _decision_digest(decisions)
        return dict(source_texts), report

    definition_keys = set(definitions)
    transformed = dict(source_texts)
    normalized_definition_count = 0
    for name, edits in definition_edits.items():
        lines = transformed[name].splitlines()
        for line_number, replacement in edits.items():
            lines[line_number - 1] = replacement
            if re.match(r"^\[\^[^]]+\]:", replacement):
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
            "endnote_definition_decisions_sha256": _decision_digest(decisions),
        }
    )
    return transformed, report


def normalize_endnotes(
    source_texts: Mapping[str, str],
    file_roles: Mapping[str, str],
    *,
    scopes: Optional[Mapping[str, Any]] = None,
    definition_decisions: Any = None,
) -> tuple[dict[str, str], dict[str, Any]]:
    """Normalize definitions from ``notes`` files and matching body markers.

    Without a scope directory the historical global behavior is preserved.
    With scopes, only printed keys that restart across chapters are rewritten;
    all other keys keep their original numeric form.  Numbered punctuation
    definitions with an unclear preceding boundary require a reviewed
    ``definition``/``continuation`` decision and otherwise fail closed.
    """
    note_files = {
        str(name)
        for name, role in file_roles.items()
        if str(role).strip().lower() == "notes" and str(name) in source_texts
    }
    if not note_files:
        return dict(source_texts), _empty_report("not_applicable", note_files)
    inline_decisions = definition_decisions.get("decisions", []) if isinstance(definition_decisions, Mapping) else definition_decisions
    supplied_inline: dict[tuple[str, int, int], Mapping[str, Any]] = {}
    if isinstance(inline_decisions, (list, tuple)):
        for item in inline_decisions:
            if isinstance(item, Mapping) and item.get("kind") == "inline":
                try:
                    if any(isinstance(item.get(field), bool) or not isinstance(item.get(field), int) for field in ("line", "column")):
                        raise EndnoteNormalizationError("inline endnote line and column must be exact integers")
                    identity = (_normalized_decision_file(item["file"]), item["line"], item["column"])
                except EndnoteNormalizationError:
                    raise
                except (KeyError, TypeError, ValueError):
                    raise EndnoteNormalizationError("inline endnote decision is missing an exact file/line/column identity")
                if identity in supplied_inline:
                    raise EndnoteNormalizationError(f"duplicate inline endnote decision at {identity[0]}:{identity[1]}:{identity[2]}")
                supplied_inline[identity] = item
    discovered: dict[tuple[str, int, int], str] = {}
    for name in note_files:
        source_lines = source_texts[name].splitlines()
        source_fenced = markdown_fence_mask(source_lines)
        for line_number, line in enumerate(source_lines, 1):
            if source_fenced[line_number - 1] or _definition_line(line, body_keys=set(), previous_line_blank=True) is None:
                continue
            for candidate in inline_endnote_candidates(line, line_number):
                identity = (name, line_number, int(candidate["column"]))
                discovered[identity] = str(candidate["key"])
                decision = supplied_inline.get(identity)
                if decision is None:
                    raise EndnoteNormalizationError(
                        f"unreviewed inline endnote boundary at {name}:{line_number}:{candidate['column']}"
                    )
                if str(decision.get("key") or "") != str(candidate["key"]):
                    raise EndnoteNormalizationError(f"inline endnote key mismatch at {name}:{line_number}:{candidate['column']}")
                if str(decision.get("role") or "").lower() not in {"definition", "continuation"}:
                    raise EndnoteNormalizationError(f"inline endnote decision is unresolved at {name}:{line_number}:{candidate['column']}")
    extra_inline = sorted(set(supplied_inline) - set(discovered))
    if extra_inline:
        item = extra_inline[0]
        raise EndnoteNormalizationError(f"stale inline endnote decision at {item[0]}:{item[1]}:{item[2]}")
    expanded_texts, decision_source, origins, virtual_inline_lines = _expand_reviewed_inline_definitions(
        source_texts, definition_decisions
    )
    if origins:
        scopes = _remap_inline_scope_directory(scopes, origins)
    decisions = _definition_decision_map(decision_source)
    if origins and decisions:
        remapped = {}
        for (name, line_number), role in decisions.items():
            origin = origins.get(name, {})
            virtual = next((v for v, original in origin.items() if original == line_number), line_number)
            remapped[(name, virtual)] = role
        decisions = remapped
    if virtual_inline_lines:
        values = decision_source.get("decisions", []) if isinstance(decision_source, Mapping) else decision_source
        if isinstance(values, (list, tuple)):
            for item in values:
                if not isinstance(item, Mapping) or item.get("kind") != "inline" or item.get("role") != "definition":
                    continue
                try:
                    identity = (_normalized_decision_file(item["file"]), item["line"], item["column"])
                    virtual_line = virtual_inline_lines.get(identity[0], {}).get(identity)
                    if virtual_line is not None:
                        decisions[(identity[0], virtual_line)] = "definition"
                except (KeyError, TypeError):
                    continue
    source_texts = expanded_texts
    if scopes is None:
        transformed, report = _normalize_legacy(
            source_texts, note_files, definition_decisions=decisions
        )
    else:
        body_keys = _raw_body_keys(source_texts, note_files)
        transformed, report = _normalize_scoped(
            source_texts, note_files, scopes, body_keys=body_keys,
            definition_decisions=decisions,
        )
    inline_records = [
        {
            "file": _normalized_decision_file(item.get("file")),
            "line": item.get("line"), "column": item.get("column"),
            "key": item.get("key"), "role": str(item.get("role") or "").lower(),
        }
        for item in (inline_decisions or [])
        if isinstance(item, Mapping) and item.get("kind") == "inline"
    ] if isinstance(inline_decisions, (list, tuple)) else []
    if inline_records:
        report["endnote_inline_definition_decisions_sha256"] = hashlib.sha256(
            json.dumps(sorted(inline_records, key=lambda item: (item["file"], item["line"], item["column"])), sort_keys=True).encode("utf-8")
        ).hexdigest()
    return transformed, report


_PHYSICAL_UNIT_EXPONENT_RE = re.compile(
    r"(?:[a-zA-Z]/[a-zA-Z]+|\b(?:m|cm|mm|km|s|h|kg|g|ft|in))\s*$"
)


def validate_normalized_endnotes(
    source_texts: Mapping[str, str],
    file_roles: Mapping[str, str],
    *,
    reviewed_inline_continuations: Optional[list[Mapping[str, Any]]] = None,
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
        source = source_texts[name]
        keys = set(endnote_definition_keys(source, file_role="notes"))
        definition_keys.update(keys)
        raw_defs = [
            match.group("html_key") or match.group("number_key")
            for match in _RAW_NOTE_DEF_RE.finditer(source)
        ]
        lines = source.splitlines()
        fenced_lines = markdown_fence_mask(lines)
        approved = [item for item in (reviewed_inline_continuations or []) if isinstance(item, Mapping) and str(item.get("file") or "") == name]
        approved_counts: Counter[tuple[str, str]] = Counter()
        approved_text_counts: Counter[tuple[str, str]] = Counter()
        for item in approved:
            if item.get("occurrences") is not None:
                approved_counts[(str(item.get("owner_key") or ""), str(item.get("key") or ""))] += int(item.get("occurrences", 0))
            elif item.get("text") is not None:
                approved_text_counts[(str(item.get("key") or ""), str(item.get("text") or ""))] += 1
        current_owner = ""
        for line_number, line in enumerate(lines, 1):
            if fenced_lines[line_number - 1]:
                continue
            owner_match = re.match(r"^[ \t]*\[\^(?P<key>[^]]+)\]:", line)
            if owner_match is None:
                owner_match = re.match(r"^[ \t]{0,3}(?:[-*+][ \t]+)?(?P<key>\d+)[ \t]*[.)。：:][ \t]+", line)
            if owner_match is not None:
                current_owner = owner_match.group("key")
            if _definition_line(line, body_keys=set(), previous_line_blank=True) is not None:
                for candidate in inline_endnote_candidates(line, line_number):
                    exact_text = str(candidate.get("marker") or "") + " " + str(candidate.get("content") or "")
                    key = str(candidate.get("key") or "")
                    approval = (current_owner, key)
                    if approved_counts.get(approval, 0) > 0:
                        approved_counts[approval] -= 1
                        continue
                    if approved_text_counts[(key, exact_text)] > 0:
                        approved_text_counts[(key, exact_text)] -= 1
                        continue
                    errors.append(f"unresolved inline endnote definition boundary remains in {name}:{line_number}:{candidate['column']}")
        if any(approved_counts.values()) or any(approved_text_counts.values()):
            errors.append(f"reviewed inline endnote continuation evidence did not match the derived Notes source in {name}")
        for index, line in enumerate(lines):
            if fenced_lines[index]:
                continue
            match = _RAW_SUP_NOTE_DEF_RE.match(line)
            if match is None:
                continue
            key = key_from_printed_marker(html.unescape(match.group("key")))
            if key and key.startswith("fnm-"):
                raw_defs.append(key)
        if raw_defs:
            errors.append(
                f"raw centralized endnote definitions remain in {name}: "
                f"{sorted(set(raw_defs))[:20]}"
            )

    scoped_numbers: set[str] = set()
    scoped_markers: set[str] = set()
    for key in definition_keys:
        match = _SCOPED_KEY_RE.match(key)
        if match:
            scoped_numbers.add(match.group("number"))
        marker_match = _SCOPED_MARKER_RE.match(key)
        if marker_match:
            scoped_markers.add(marker_match.group("marker"))

    for name, text in source_texts.items():
        if name in note_files:
            continue
        lines = text.splitlines()
        fenced_lines = markdown_fence_mask(lines)
        body_text = "\n".join(
            line for index, line in enumerate(lines) if not fenced_lines[index]
        )
        raw_refs = []
        for match in _HTML_SUP_RE.finditer(body_text):
            key = key_from_printed_marker(html.unescape(match.group("key")))
            if (
                key
                and (key in definition_keys or key in scoped_markers)
                and not _is_math_or_formula_superscript(
                    body_text, match.start(), match.end(), key=key
                )
            ):
                raw_refs.append(match.group("key").strip())
        raw_refs.extend(
            match.group("value").translate(_UNICODE_DIGITS)
            for match in _UNICODE_SUP_RE.finditer(body_text)
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
            for match in _HTML_SUP_RE.finditer(body_text)
            if match.group("key") in scoped_numbers
            and not _is_math_or_formula_superscript(
                body_text, match.start(), match.end(), key=match.group("key")
            )
        ]
        residual_scoped.extend(
            match.group("key").strip()
            for match in _HTML_SUP_RE.finditer(body_text)
            if key_from_printed_marker(html.unescape(match.group("key"))) in scoped_markers
            and not _is_math_or_formula_superscript(
                body_text, match.start(), match.end(),
                key=key_from_printed_marker(html.unescape(match.group("key"))) or match.group("key")
            )
        )
        for match in _UNICODE_SUP_RE.finditer(body_text):
            val = match.group("value").translate(_UNICODE_DIGITS)
            if val in scoped_numbers:
                if val in {"2", "3"}:
                    prefix = body_text[max(0, match.start() - 10) : match.start()]
                    if _PHYSICAL_UNIT_EXPONENT_RE.search(prefix):
                        continue
                residual_scoped.append(val)
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

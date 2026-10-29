"""Build deterministic per-chapter scopes for centralized endnotes.

The scope directory is a pure, read-only view of the refined TOC, the
``tree_progress`` unit order, and the Markdown source.  It deliberately does
not write files or choose between competing chapter headings: ambiguous and
unmatched headings are reported so normalization can fail closed.
"""

from __future__ import annotations

import re
from collections import defaultdict
from typing import Any, Iterable, Mapping, Optional

from .footnote_structure import FOOTNOTE_STRUCTURE_EXEMPT_TYPES


_HEADING_RE = re.compile(r"^(?P<marks>#{1,6})\s+(?P<title>\S.*?)\s*$")
_CHAPTER_LABEL_RE = re.compile(
    r"^(?:chapter|chapitre|kapitel)\s+(?P<number>\d+)(?:\s*[:.\-–—]\s*|\s*$)",
    re.IGNORECASE,
)
_PART_LABEL_RE = re.compile(
    r"^(?:part|teil|partie)\s+(?P<number>\d+)(?:\s*[:.\-–—]\s*|\s*$)",
    re.IGNORECASE,
)
_LEADING_NUMBER_RE = re.compile(r"^(?P<number>\d+)\s*[.)：:]\s*")
_LEADING_SPACED_NUMBER_RE = re.compile(
    r"^(?P<number>\d{1,3})\s+(?=\S)"
)
_PLAIN_HEADING_MAX_LENGTH = 120
_PLAIN_HEADING_FORBIDDEN_PREFIXES = ("#", "-", "*", ">", "|", "!", "<")
_PLAIN_HEADING_NUMBER_PREFIX_RE = re.compile(r"^\d+[.)、]")
_PLAIN_HEADING_TERMINAL_RE = re.compile(r"[.,;!?。，；！？]\s*$")
_MARKDOWN_TOP_RE = re.compile(r"^[*_`]+|[*_`]+$")
_TITLE_OUTER_MARK_PAIRS = {
    ("《", "》"),
    ("〈", "〉"),
    ("「", "」"),
    ("『", "』"),
    ("“", "”"),
    ("‘", "’"),
    ('"', '"'),
    ("'", "'"),
}
_HTML_SUP_RE = re.compile(
    r"<sup\b[^>]*>[ \t]*(?P<key>\d+)[ \t]*</sup>",
    re.IGNORECASE,
)
_UNICODE_SUP_RE = re.compile(r"(?P<value>[⁰¹²³⁴⁵⁶⁷⁸⁹]+)")
_UNICODE_DIGITS = str.maketrans("⁰¹²³⁴⁵⁶⁷⁸⁹", "0123456789")
_MARKDOWN_REF_RE = re.compile(r"\[\^(?P<key>\d+)\](?!:)")
_MARKDOWN_DEF_RE = re.compile(r"^[ \t]*\[\^(?P<key>\d+)\]:", re.MULTILINE)


def markdown_fence_mask(lines: list[str]) -> list[bool]:
    """Mark fenced-code lines so source-like code is not read as structure."""
    mask = [False] * len(lines)
    fence_char: Optional[str] = None
    fence_length = 0
    for index, line in enumerate(lines):
        if fence_char is not None:
            mask[index] = True
            close = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})[ \t]*$", line)
            if (
                close
                and close.group(1)[0] == fence_char
                and len(close.group(1)) >= fence_length
            ):
                fence_char = None
                fence_length = 0
            continue
        opening = re.match(r"^[ \t]{0,3}(`{3,}|~{3,})(.*)$", line)
        if opening is None:
            continue
        run = opening.group(1)
        if run[0] == "`" and "`" in opening.group(2):
            continue
        mask[index] = True
        fence_char = run[0]
        fence_length = len(run)
    return mask


class EndnoteScopeError(ValueError):
    """Raised when the scope directory cannot be built safely."""


def _normalize_text(value: Any) -> str:
    text = re.sub(r"\s+", " ", str(value or "").strip())
    text = _MARKDOWN_TOP_RE.sub("", text).strip()
    text = re.sub(r"(?<=\w)[ \t]+-[ \t]*(?=\w)", "-", text)
    while len(text) >= 2 and (text[0], text[-1]) in _TITLE_OUTER_MARK_PAIRS:
        text = text[1:-1].strip()
    return text.casefold()


def scope_title_variants(value: Any) -> tuple[str, ...]:
    """Return conservative normalized variants for a TOC or heading label.

    Variants remove only presentation-level chapter/part labels and a leading
    ordinal.  They never drop words, so two differently worded titles cannot
    become the same scope by accident.
    """
    raw = _normalize_text(value)
    if not raw:
        return ()
    variants: set[str] = {raw}

    def add(candidate: str) -> None:
        normalized = _normalize_text(candidate)
        if normalized:
            variants.add(normalized)

    chapter = _CHAPTER_LABEL_RE.match(raw)
    if chapter:
        add(raw[chapter.end() :])
        add(chapter.group("number"))

    part = _PART_LABEL_RE.match(raw)
    if part:
        add(raw[part.end() :])
        add(f"part {part.group('number')}")

    leading = _LEADING_NUMBER_RE.match(raw)
    if leading:
        add(raw[leading.end() :])
        add(leading.group("number"))

    spaced = _LEADING_SPACED_NUMBER_RE.match(raw)
    if spaced:
        add(raw[spaced.end() :])
        add(spaced.group("number"))

    if ":" in raw:
        before, after = raw.split(":", 1)
        add(before.strip())
        add(after.strip())

    return tuple(sorted(variants))


def _as_list(value: Any) -> list[Any]:
    return list(value) if isinstance(value, (list, tuple)) else []


def _node_at(chapters: Any, path: Iterable[int]) -> Optional[Mapping[str, Any]]:
    nodes = chapters
    node: Optional[Mapping[str, Any]] = None
    for index in path:
        if not isinstance(nodes, list) or not (1 <= index <= len(nodes)):
            return None
        candidate = nodes[index - 1]
        if not isinstance(candidate, Mapping):
            return None
        node = candidate
        nodes = candidate.get("children", [])
    return node


def _unit_files(unit: Mapping[str, Any]) -> list[str]:
    raw = unit.get("part_files")
    files = [str(item) for item in _as_list(raw) if str(item).strip()]
    if files:
        return files
    name = str(unit.get("file") or "").strip()
    return [name] if name else []


def _unit_type(unit: Mapping[str, Any]) -> str:
    return str(unit.get("type") or "").strip().lower()


def _is_body_unit(unit: Mapping[str, Any]) -> bool:
    return _unit_type(unit) not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES


def _add_descendant_scopes(
    scopes: dict[str, dict[str, Any]],
    order: list[str],
    node: Optional[Mapping[str, Any]],
    parent_key: str,
    *,
    unit_order: int,
    unit_type: str,
) -> None:
    children = node.get("children", []) if isinstance(node, Mapping) else []
    if not isinstance(children, list):
        return
    for index, child in enumerate(children, 1):
        if not isinstance(child, Mapping):
            continue
        title = str(child.get("title") or "").strip()
        scope_key = f"{parent_key}c{index}"
        scopes[scope_key] = {
            "scope_key": scope_key,
            "unit_order": unit_order,
            "unit_type": unit_type,
            "title": title,
            "title_variants": scope_title_variants(title),
            "kind": "descendant",
            "body_files": [],
            "notes_files": [],
            "definition_count": 0,
            "reference_count": 0,
        }
        order.append(scope_key)
        _add_descendant_scopes(
            scopes,
            order,
            child,
            scope_key,
            unit_order=unit_order,
            unit_type=unit_type,
        )


def _scope_for_line(
    spans: Mapping[str, list[dict[str, Any]]], name: str, line_number: int
) -> Optional[str]:
    for span in spans.get(name, []):
        if span["start_line"] <= line_number < span["end_line"]:
            return span.get("scope_key")
    return None


def _match_scopes(
    heading: str, candidates: list[dict[str, Any]]
) -> list[dict[str, Any]]:
    variants = set(scope_title_variants(heading))
    if not variants:
        return []
    return [
        scope
        for scope in candidates
        if variants.intersection(scope.get("title_variants") or ())
    ]


def _build_body_spans(
    units: list[Mapping[str, Any]],
    scopes: dict[str, dict[str, Any]],
    scope_order: list[str],
    source_texts: Mapping[str, str],
) -> tuple[dict[str, list[dict[str, Any]]], list[str], list[dict[str, Any]]]:
    body_spans: dict[str, list[dict[str, Any]]] = {}
    body_files: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    scopes_by_unit: dict[int, list[dict[str, Any]]] = defaultdict(list)
    for scope_key in scope_order:
        scopes_by_unit[scopes[scope_key]["unit_order"]].append(scopes[scope_key])

    last_scope_by_unit: dict[int, str] = {}
    for unit_order, unit in enumerate(units):
        if not _is_body_unit(unit):
            continue
        unit_key = f"u{unit_order + 1}"
        candidates = scopes_by_unit.get(unit_order, [])
        current = last_scope_by_unit.get(unit_order, unit_key)
        for name in _unit_files(unit):
            if name not in source_texts:
                continue
            body_files.append(name)
            lines = source_texts[name].splitlines()
            fenced_lines = markdown_fence_mask(lines)
            spans: list[dict[str, Any]] = []
            span_start = 1
            span_scope = current
            for line_number, line in enumerate(lines, 1):
                heading = None if fenced_lines[line_number - 1] else _HEADING_RE.match(line)
                if heading:
                    matches = _match_scopes(heading.group("title"), candidates)
                    if len(matches) == 1:
                        current = str(matches[0]["scope_key"])
                    elif len(matches) > 1:
                        ambiguous.append(
                            {
                                "file": name,
                                "line": line_number,
                                "heading": line.strip(),
                                "candidates": [
                                    item["scope_key"] for item in matches
                                ],
                            }
                        )
                if current != span_scope:
                    if line_number > span_start:
                        spans.append(
                            {
                                "start_line": span_start,
                                "end_line": line_number,
                                "scope_key": span_scope,
                            }
                        )
                    span_start = line_number
                    span_scope = current
            end_line = len(lines) + 1
            if end_line > span_start:
                spans.append(
                    {
                        "start_line": span_start,
                        "end_line": end_line,
                        "scope_key": span_scope,
                    }
                )
            body_spans[name] = spans
            last_scope_by_unit[unit_order] = span_scope
    return body_spans, body_files, ambiguous


def _is_plain_text_heading(lines: list[str], index: int) -> bool:
    """Return True for a conservative standalone Notes chapter heading.

    Native-text PDFs often keep chapter headings in Notes as plain text.  Only
    a short standalone line surrounded by blank lines qualifies; numbered note
    definitions, page furniture, and sentences that end with punctuation are
    rejected so the matcher never has to guess.
    """
    line = lines[index].strip()
    if not line or len(line) > _PLAIN_HEADING_MAX_LENGTH:
        return False
    if line.startswith(_PLAIN_HEADING_FORBIDDEN_PREFIXES):
        return False
    if _PLAIN_HEADING_NUMBER_PREFIX_RE.match(line):
        return False
    if _PLAIN_HEADING_TERMINAL_RE.search(line):
        return False
    if index > 0 and lines[index - 1].strip():
        return False
    if index + 1 < len(lines) and lines[index + 1].strip():
        return False
    return True


def _build_notes_spans(
    units: list[Mapping[str, Any]],
    scopes: dict[str, dict[str, Any]],
    scope_order: list[str],
    source_texts: Mapping[str, str],
) -> tuple[
    dict[str, list[dict[str, Any]]],
    list[str],
    list[dict[str, Any]],
    list[dict[str, Any]],
]:
    notes_spans: dict[str, list[dict[str, Any]]] = {}
    notes_files: list[str] = []
    ambiguous: list[dict[str, Any]] = []
    unmatched: list[dict[str, Any]] = []
    candidates = [
        scopes[key]
        for key in scope_order
        if scopes[key].get("unit_type") not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES
    ]
    current: Optional[str] = None
    for unit in units:
        if _unit_type(unit) != "notes":
            continue
        for name in _unit_files(unit):
            if name not in source_texts:
                continue
            notes_files.append(name)
            lines = source_texts[name].splitlines()
            fenced_lines = markdown_fence_mask(lines)
            spans: list[dict[str, Any]] = []
            span_start = 1
            span_scope = current
            for line_number, line in enumerate(lines, 1):
                heading = None if fenced_lines[line_number - 1] else _HEADING_RE.match(line)
                if heading is not None:
                    heading_title: Optional[str] = heading.group("title")
                    plain_text = False
                elif not fenced_lines[line_number - 1] and _is_plain_text_heading(lines, line_number - 1):
                    heading_title = line.strip()
                    plain_text = True
                else:
                    heading_title = None
                    plain_text = False
                if heading_title is not None:
                    matches = _match_scopes(heading_title, candidates)
                    if len(matches) == 1:
                        current = str(matches[0]["scope_key"])
                    elif len(matches) > 1:
                        ambiguous.append(
                            {
                                "file": name,
                                "line": line_number,
                                "heading": line.strip(),
                                "candidates": [
                                    item["scope_key"] for item in matches
                                ],
                            }
                        )
                    elif not plain_text:
                        unmatched.append(
                            {
                                "file": name,
                                "line": line_number,
                                "heading": line.strip(),
                            }
                        )
                if current != span_scope:
                    if line_number > span_start:
                        spans.append(
                            {
                                "start_line": span_start,
                                "end_line": line_number,
                                "scope_key": span_scope,
                            }
                        )
                    span_start = line_number
                    span_scope = current
            end_line = len(lines) + 1
            notes_spans[name] = spans
            if end_line > span_start:
                spans.append(
                    {
                        "start_line": span_start,
                        "end_line": end_line,
                        "scope_key": span_scope,
                    }
                )
    return notes_spans, notes_files, ambiguous, unmatched


def _collect_scope_references(
    body_spans: Mapping[str, list[dict[str, Any]]],
    body_files: list[str],
    source_texts: Mapping[str, str],
) -> dict[str, list[dict[str, Any]]]:
    references: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for name in body_files:
        text = source_texts[name]
        lines = text.splitlines()
        fenced_lines = markdown_fence_mask(lines)
        page_note_keys = {
            match.group("key")
            for line_index, line in enumerate(lines)
            if not fenced_lines[line_index]
            for match in [_MARKDOWN_DEF_RE.match(line)]
            if match
        }
        for line_number, line in enumerate(lines, 1):
            if fenced_lines[line_number - 1]:
                continue
            scope_key = _scope_for_line(body_spans, name, line_number)
            if not scope_key:
                continue
            for match in _HTML_SUP_RE.finditer(line):
                references[scope_key].append(
                    {
                        "key": match.group("key"),
                        "file": name,
                        "line": line_number,
                        "start": match.start(),
                        "end": match.end(),
                        "kind": "html",
                    }
                )
            for match in _UNICODE_SUP_RE.finditer(line):
                references[scope_key].append(
                    {
                        "key": match.group("value").translate(_UNICODE_DIGITS),
                        "file": name,
                        "line": line_number,
                        "start": match.start(),
                        "end": match.end(),
                        "kind": "unicode",
                    }
                )
            for match in _MARKDOWN_REF_RE.finditer(line):
                if match.group("key") in page_note_keys:
                    continue
                references[scope_key].append(
                    {
                        "key": match.group("key"),
                        "file": name,
                        "line": line_number,
                        "start": match.start(),
                        "end": match.end(),
                        "kind": "markdown",
                    }
                )
    return dict(references)


def build_endnote_scope_directory(
    toc_tree: Mapping[str, Any],
    progress: Mapping[str, Any],
    source_texts: Mapping[str, str],
) -> dict[str, Any]:
    """Build scopes, per-file spans, and ordered body reference facts."""
    if not isinstance(toc_tree, Mapping):
        raise EndnoteScopeError("toc_tree.json must be an object")
    if not isinstance(progress, Mapping):
        raise EndnoteScopeError("tree_progress.json must be an object")
    units = [unit for unit in progress.get("units", []) or [] if isinstance(unit, Mapping)]
    if not units:
        raise EndnoteScopeError("tree_progress.json contains no units")
    chapters = toc_tree.get("chapters")
    if chapters is None and isinstance(toc_tree.get("book"), Mapping):
        chapters = toc_tree["book"].get("chapters")
    if not isinstance(chapters, list):
        raise EndnoteScopeError("toc_tree.json contains no chapters")

    scopes: dict[str, dict[str, Any]] = {}
    scope_order: list[str] = []
    for unit_order, unit in enumerate(units):
        index_path = _as_list(unit.get("index_path"))
        try:
            index_path = [int(value) for value in index_path]
        except (TypeError, ValueError) as exc:
            raise EndnoteScopeError(
                f"unit {unit.get('unit_id') or unit_order} has an invalid index_path"
            ) from exc
        node = _node_at(chapters, index_path) if index_path else None
        title = str(
            (node or {}).get("title")
            or unit.get("title")
            or ""
        ).strip()
        scope_key = f"u{unit_order + 1}"
        unit_type = _unit_type(unit)
        scopes[scope_key] = {
            "scope_key": scope_key,
            "unit_order": unit_order,
            "unit_type": unit_type,
            "index_path": index_path,
            "title": title,
            "title_variants": scope_title_variants(title),
            "kind": "unit",
            "body_files": [],
            "notes_files": [],
            "definition_count": 0,
            "reference_count": 0,
        }
        scope_order.append(scope_key)
        _add_descendant_scopes(
            scopes,
            scope_order,
            node,
            scope_key,
            unit_order=unit_order,
            unit_type=unit_type,
        )

    body_spans, body_files, body_ambiguous = _build_body_spans(
        units, scopes, scope_order, source_texts
    )
    notes_spans, notes_files, notes_ambiguous, unmatched = _build_notes_spans(
        units, scopes, scope_order, source_texts
    )
    references = _collect_scope_references(
        body_spans, body_files, source_texts
    )

    for name, spans in body_spans.items():
        for span in spans:
            scope_key = span.get("scope_key")
            if scope_key in scopes and name not in scopes[scope_key]["body_files"]:
                scopes[scope_key]["body_files"].append(name)
    for name, spans in notes_spans.items():
        for span in spans:
            scope_key = span.get("scope_key")
            if scope_key in scopes and name not in scopes[scope_key]["notes_files"]:
                scopes[scope_key]["notes_files"].append(name)
    for scope_key, items in references.items():
        if scope_key in scopes:
            scopes[scope_key]["reference_count"] = len(items)

    return {
        "schema_version": 1,
        "scopes": scopes,
        "scope_order": scope_order,
        "body_spans": body_spans,
        "notes_spans": notes_spans,
        "body_files": body_files,
        "notes_files": notes_files,
        "scope_ref_keys": references,
        "ambiguous_headings": body_ambiguous + notes_ambiguous,
        "unmatched_notes_headings": unmatched,
    }


def scope_for_line(
    directory: Mapping[str, Any], file_name: str, line_number: int
) -> Optional[str]:
    return _scope_for_line(directory.get("notes_spans", {}), file_name, line_number)


def scope_report(directory: Mapping[str, Any]) -> list[dict[str, Any]]:
    scopes = directory.get("scopes", {})
    report: list[dict[str, Any]] = []
    for scope_key in directory.get("scope_order", []):
        scope = scopes.get(scope_key)
        if not isinstance(scope, Mapping):
            continue
        report.append(
            {
                "scope_key": scope_key,
                "key": scope_key,
                "title": scope.get("title", ""),
                "body_files": list(scope.get("body_files", [])),
                "notes_files": list(scope.get("notes_files", [])),
                "definition_count": int(scope.get("definition_count", 0)),
                "reference_count": int(scope.get("reference_count", 0)),
            }
        )
    return report


__all__ = [
    "EndnoteScopeError",
    "build_endnote_scope_directory",
    "scope_for_line",
    "scope_report",
    "scope_title_variants",
]

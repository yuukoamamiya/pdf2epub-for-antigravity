"""Resolve refinement-unit structure boundaries for footnote detection.

Typed auxiliary units such as TOCs, bibliographies and indexes can share a
physical page with ordinary prose. This module maps those unit boundaries back
to sidecar blocks so the footnote detector can fail closed for the unsafe
portion of the page. It does not classify candidates or write workflow
reports.
"""

from __future__ import annotations

import html
import json
import re
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional

from .layout_evidence import text_from_block as _text_from_block


FOOTNOTE_STRUCTURE_EXEMPT_TYPES = frozenset(
    {
        "toc",
        "notes",
        "bibliography",
        "index",
        "author_notes",
        "catalog",
        "ad",
        "metadata",
        "frontmatter",
        "copyright",
        "cover",
    }
)

_TOC_TITLE_ALIASES = frozenset(
    {
        "toc",
        "contents",
        "table of contents",
        "table contents",
        "contents page",
    }
)


def _role_from_title(title: Any) -> str:
    """Recover only the unambiguous TOC role from legacy TOC output.

    Older Subagent TOCs were instructed to type notes, bibliographies and
    indexes, but not always the contents unit.  This narrow compatibility
    fallback is limited to exact conventional labels; all other semantic
    roles still require an explicit ``type`` from the TOC handoff.
    """
    value = " ".join(str(title or "").casefold().split())
    return "toc" if value in _TOC_TITLE_ALIASES else ""


def load_refined_units(output_dir: Path) -> list[dict[str, Any]]:
    """Load refinement units and recover missing TOC roles from index paths."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    try:
        value = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    units = value.get("units", []) if isinstance(value, dict) else []
    refined = [dict(item) for item in units if isinstance(item, dict)]

    toc_roles: dict[tuple[int, ...], str] = {}
    toc_path = Path(output_dir) / "toc_tree.json"
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        toc = {}

    def visit(nodes: Any, path: tuple[int, ...], inherited: str = "") -> None:
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, Mapping):
                continue
            role = str(node.get("type") or inherited).strip().lower()
            if not role:
                role = _role_from_title(node.get("title"))
            current = path + (index,)
            toc_roles[current] = role
            visit(node.get("children"), current, role)

    if isinstance(toc, Mapping):
        visit(toc.get("chapters"), ())

    for unit in refined:
        if str(unit.get("type") or "").strip():
            unit["type"] = str(unit["type"]).strip().lower()
            continue
        raw_path = unit.get("index_path")
        try:
            index_path = tuple(int(value) for value in raw_path)
        except (TypeError, ValueError):
            index_path = ()
        if index_path:
            unit["type"] = toc_roles.get(index_path, "")
    return refined


def structure_exclusion_for_page(
    units: Iterable[Mapping[str, Any]], page_number: int
) -> dict[str, Any]:
    """Return typed refinement units that make page-footnote detection unsafe."""
    excluded: list[dict[str, Any]] = []
    for unit in units:
        role = str(unit.get("type") or "").strip().lower()
        if role not in FOOTNOTE_STRUCTURE_EXEMPT_TYPES:
            continue
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            continue
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            continue
        if start_page <= page_number <= end_page:
            excluded.append(
                {
                    "type": role,
                    "file": str(unit.get("file") or ""),
                    "page_range": [start_page, end_page],
                    "boundary_info": dict(unit.get("boundary_info") or {})
                    if isinstance(unit.get("boundary_info"), Mapping)
                    else {},
                }
            )
    roles = sorted({str(item["type"]) for item in excluded})
    return {"roles": roles, "units": excluded}


def structure_text_signature(value: Any) -> str:
    """Build a tolerant line-search signature for a sidecar block."""
    text = html.unescape(str(value or ""))
    text = re.sub(r"<[^>]+>", " ", text)
    text = re.sub(r"\\([*_<>{}#$`~])", r"\1", text)
    text = re.sub(r"\[\^[A-Za-z0-9_-]+\]\s*:\s*", " ", text)
    return "".join(char.casefold() for char in text if char.isalnum())


def block_line_span(
    page_markdown: str,
    sidecar: Mapping[str, Any],
    block_index: int,
) -> Optional[tuple[int, int]]:
    """Locate a sidecar block in page Markdown using a line-aware signature."""
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list) or not 0 <= block_index < len(blocks):
        return None
    block = blocks[block_index]
    if not isinstance(block, Mapping):
        return None
    needle = structure_text_signature(_text_from_block(block))
    if len(needle) < 3:
        return None

    page_signature: list[str] = []
    signature_lines: list[int] = []
    for line_number, line in enumerate(str(page_markdown or "").splitlines(), 1):
        line_signature = structure_text_signature(line)
        page_signature.extend(line_signature)
        signature_lines.extend([line_number] * len(line_signature))
    if not page_signature or len(needle) > len(page_signature):
        return None

    page_value = "".join(page_signature)
    matches: list[tuple[int, int]] = []
    cursor = 0
    while True:
        found = page_value.find(needle, cursor)
        if found < 0:
            break
        end = found + len(needle)
        matches.append((signature_lines[found], signature_lines[end - 1] + 1))
        cursor = found + 1
    return matches[-1] if matches else None


def structure_excluded_block_indexes(
    page_markdown: str,
    sidecar: Mapping[str, Any],
    page_number: int,
    structure_exclusion: Mapping[str, Any],
) -> set[int]:
    """Map typed TOC units to sidecar blocks on a possibly shared page."""
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list):
        return set()
    excluded: set[int] = set()
    for raw_unit in structure_exclusion.get("units", []) or []:
        if not isinstance(raw_unit, Mapping):
            continue
        page_range = raw_unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            continue
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError):
            continue
        boundary = raw_unit.get("boundary_info")
        if not isinstance(boundary, Mapping):
            excluded.update(range(len(blocks)))
            continue
        try:
            start_line = int(boundary["start_line"]) if "start_line" in boundary else 1
            end_line = (
                int(boundary["end_line"])
                if "end_line" in boundary
                else len(str(page_markdown or "").splitlines()) + 1
            )
        except (TypeError, ValueError):
            excluded.update(range(len(blocks)))
            continue
        if page_number == start_page and page_number == end_page:
            lower, upper = start_line, end_line
        elif page_number == start_page:
            lower, upper = start_line, len(str(page_markdown or "").splitlines()) + 1
        elif page_number == end_page:
            lower, upper = 1, end_line
        else:
            lower, upper = 1, len(str(page_markdown or "").splitlines()) + 1
        for block_index in range(len(blocks)):
            span = block_line_span(page_markdown, sidecar, block_index)
            if span is None:
                if lower == 1 and upper > len(str(page_markdown or "").splitlines()):
                    excluded.add(block_index)
                continue
            if span[0] < upper and span[1] > lower:
                excluded.add(block_index)
    return excluded


_load_refined_units = load_refined_units
_structure_exclusion_for_page = structure_exclusion_for_page
_structure_text_signature = structure_text_signature
_block_line_span = block_line_span
_structure_excluded_block_indexes = structure_excluded_block_indexes


__all__ = [
    "FOOTNOTE_STRUCTURE_EXEMPT_TYPES",
    "block_line_span",
    "load_refined_units",
    "structure_excluded_block_indexes",
    "structure_exclusion_for_page",
    "structure_text_signature",
]

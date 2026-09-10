"""Extract a PDF's native outline as reviewable TOC evidence.

The native outline is useful when publishers supplied accurate bookmarks, but
it is not authoritative enough to replace OCR-based structure analysis.  This
module therefore writes a draft artifact that a workspace Subagent can review
and correct before producing ``toc_tree.json``.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


def _empty_draft(pdf_path: Path, total_pages: int, warning: str) -> Dict[str, Any]:
    return {
        "schema_version": 1,
        "source": "pdf-native-outline",
        "pdf_file": pdf_path.name,
        "physical_page_is_authoritative": True,
        "total_pages": total_pages,
        "extracted": False,
        "entry_count": 0,
        "chapters": [],
        "warnings": [warning],
    }


def _assign_end_pages(nodes: List[Dict[str, Any]], parent_end: int) -> None:
    """Derive inclusive ranges from the next sibling's start page."""
    for index, node in enumerate(nodes):
        next_start = (
            nodes[index + 1]["start_page"] if index + 1 < len(nodes) else parent_end + 1
        )
        node["end_page"] = max(
            node["start_page"], min(parent_end, next_start - 1)
        )
        _assign_end_pages(node["children"], node["end_page"])


def extract_pdf_outline(
    pdf_path: Path,
    output_path: Path,
    total_pages: int,
) -> Dict[str, Any]:
    """Extract and persist a native PDF outline draft.

    Invalid or missing outlines are represented as a valid empty draft so
    ``refine-prepare`` remains usable for scanned PDFs without bookmarks.
    """
    pdf_path = Path(pdf_path)
    output_path = Path(output_path)
    total_pages = max(0, int(total_pages))

    if not pdf_path.is_file():
        draft = _empty_draft(pdf_path, total_pages, "PDF input was not found")
        output_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        return draft

    try:
        import pymupdf as fitz

        with fitz.open(pdf_path) as document:
            raw_outline = document.get_toc(simple=True) or []
            pdf_page_count = int(document.page_count)
    except Exception as exc:
        draft = _empty_draft(pdf_path, total_pages, f"Could not read PDF outline: {exc}")
        output_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
        return draft

    warnings: List[str] = []
    chapters: List[Dict[str, Any]] = []
    stack: List[tuple[int, Dict[str, Any]]] = []
    seen_titles: set[tuple[str, int]] = set()
    usable_pages = total_pages or pdf_page_count

    for entry_index, entry in enumerate(raw_outline, 1):
        if not isinstance(entry, (list, tuple)) or len(entry) < 3:
            warnings.append(f"Skipped malformed outline entry {entry_index}")
            continue
        try:
            level = int(entry[0])
            page = int(entry[2])
        except (TypeError, ValueError):
            warnings.append(f"Skipped outline entry {entry_index} with invalid level/page")
            continue
        title = str(entry[1] or "").strip()
        if not title or level < 1:
            warnings.append(f"Skipped outline entry {entry_index} with empty title or invalid level")
            continue
        if page < 1 or page > usable_pages:
            warnings.append(
                f"Skipped outline entry {entry_index} with page {page}; "
                f"expected 1-{usable_pages}"
            )
            continue
        signature = (title.casefold(), page)
        if signature in seen_titles:
            warnings.append(f"Duplicate outline entry retained: {title} (page {page})")
        seen_titles.add(signature)

        node: Dict[str, Any] = {
            "title": title,
            "level": level,
            "start_page": page,
            "end_page": usable_pages,
            "children": [],
            "outline_entry": entry_index,
        }
        while stack and level <= stack[-1][0]:
            stack.pop()
        if stack:
            stack[-1][1]["children"].append(node)
        else:
            chapters.append(node)
        stack.append((level, node))

    if not chapters:
        warnings.append("PDF contains no usable native outline entries")
    else:
        _assign_end_pages(chapters, usable_pages)

    draft = {
        "schema_version": 1,
        "source": "pdf-native-outline",
        "pdf_file": pdf_path.name,
        "physical_page_is_authoritative": True,
        "total_pages": usable_pages,
        "extracted": bool(chapters),
        "entry_count": sum(1 for _ in _walk(chapters)),
        "chapters": chapters,
        "warnings": warnings,
    }
    output_path.write_text(json.dumps(draft, ensure_ascii=False, indent=2), encoding="utf-8")
    return draft


def _walk(nodes: List[Dict[str, Any]]):
    for node in nodes:
        yield node
        yield from _walk(node.get("children", []))

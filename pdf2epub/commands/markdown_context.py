"""Read-only PDF Markdown context projections used by workflow commands."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Mapping


def _load_pdf_file_contexts(output_dir: Path) -> dict:
    """Map each generated unit to its human-readable TOC hierarchy."""
    toc_path = Path(output_dir) / "toc_tree.json"
    if not toc_path.is_file():
        return {}
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
        from pdf2epub.utils.unit_id import generate_unit_id
    except (OSError, json.JSONDecodeError, ImportError):
        return {}

    contexts: Dict[str, str] = {}

    def visit(nodes: Any, path: list[int], parents: list[str]) -> None:
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            current = parents + ([title] if title else [])
            unit_name = f"{generate_unit_id(path + [index])}.md"
            if current:
                contexts[unit_name] = " → ".join(current)
            visit(node.get("children", []), path + [index], current)

    visit(toc.get("chapters", []), [], [])

    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            for unit in progress.get("units", []):
                base = str(unit.get("file") or "")
                context = contexts.get(base)
                if context:
                    for part in unit.get("part_files") or [base]:
                        contexts[str(part)] = context
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    return contexts


def _load_pdf_toc_titles(output_dir: Path) -> list[str]:
    """Return source-language TOC labels used to protect polish headings."""
    toc_path = Path(output_dir) / "toc_tree.json"
    if not toc_path.is_file():
        return []
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    titles: list[str] = []

    def visit(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            if title and title not in titles:
                titles.append(title)
            visit(node.get("children", []))

    visit(toc.get("chapters", []))
    return titles


def _load_pdf_continuation_files(output_dir: Path, source_dir: Path) -> dict:
    """Read continuation metadata from refinement rather than filenames."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if not progress_path.is_file():
        return {}
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}

    available = {path.name for path in Path(source_dir).glob("*.md")}
    metadata: dict[str, dict[str, Any]] = {}
    for unit in progress.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        names = [str(name) for name in (unit.get("part_files") or []) if name]
        if len(names) < 2:
            continue
        part_count = len(names)
        for index, name in enumerate(names, 1):
            if index > 1 and name in available:
                metadata[name] = {
                    "is_continuation": True,
                    "part_number": index,
                    "part_count": part_count,
                }
    return metadata


def _load_pdf_continuation_toc_titles(output_dir: Path) -> dict[str, list[str]]:
    """Map split parts to the top-level ancestor labels they may repeat."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    toc_path = Path(output_dir) / "toc_tree.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(progress, Mapping) or not isinstance(toc, Mapping):
        return {}

    chapters = toc.get("chapters", [])

    def top_level_title(index_path: Any) -> str:
        if not isinstance(index_path, list) or not index_path:
            return ""
        nodes = chapters
        first_title = ""
        try:
            for depth, raw_index in enumerate(index_path):
                if not isinstance(nodes, list):
                    return ""
                index = int(raw_index) - 1
                node = nodes[index]
                if not isinstance(node, Mapping):
                    return ""
                if depth == 0:
                    first_title = str(node.get("title") or "").strip()
                nodes = node.get("children", [])
        except (IndexError, TypeError, ValueError):
            return ""
        return first_title

    result: dict[str, list[str]] = {}
    for unit in progress.get("units", []) or []:
        if not isinstance(unit, Mapping):
            continue
        names = [
            str(name)
            for name in (unit.get("part_files") or [])
            if str(name).strip()
        ]
        if len(names) < 2:
            continue
        ancestor = top_level_title(unit.get("index_path"))
        own_title = str(unit.get("title") or "").strip()
        if not ancestor or ancestor.casefold() == own_title.casefold():
            continue
        for name in names[1:]:
            result[name] = [ancestor]
    return result


def _load_pdf_chapter_groups(output_dir: Path, source_dir: Path) -> dict:
    """Group PDF units by top-level chapter, including split part files."""
    from pdf2epub.chapter_identity import ChapterIdentity

    source_names = [path.name for path in sorted(Path(source_dir).glob("*.md"))]
    groups = {}

    def chapter_id(name: str) -> str:
        identity = ChapterIdentity.parse(name)
        if identity is None:
            return f"unit:{name}"
        if identity.number:
            return f"{identity.prefix}_{identity.index_path[0]}"
        return identity.prefix

    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            progress = {}
        for unit in progress.get("units", []) or []:
            if not isinstance(unit, dict):
                continue
            names = unit.get("part_files") or [unit.get("file")]
            names = [str(name) for name in names if name]
            if not names:
                continue
            group = groups.setdefault(chapter_id(names[0]), [])
            for name in names:
                if name in source_names and name not in group:
                    group.append(name)

    for name in source_names:
        group = groups.setdefault(chapter_id(name), [])
        if name not in group:
            group.append(name)
    return groups


__all__ = [
    "_load_pdf_chapter_groups",
    "_load_pdf_continuation_files",
    "_load_pdf_continuation_toc_titles",
    "_load_pdf_file_contexts",
    "_load_pdf_toc_titles",
]

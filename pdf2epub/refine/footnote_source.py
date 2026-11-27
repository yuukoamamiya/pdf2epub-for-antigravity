"""Load the current Markdown units used by footnote planning.

This is a narrow source boundary shared by validation and apply.  It records
byte hashes from disk while exposing normalized text for the in-memory planner.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from ..workflow_contracts import sha256_file
from .footnote_edit_plan import split_multi_superscripts

_SAFE_FILENAME_RE = re.compile(r"^[^/\\]+\.md$")


def markdown_inventory(directory: Path) -> dict[str, str]:
    """Hash the Markdown files in one source or derived directory."""
    return {
        path.name: sha256_file(path)
        for path in sorted(Path(directory).glob("*.md"))
        if path.is_file()
    }


def _safe_unit_filename(value: Any) -> str:
    name = str(value or "")
    if not _SAFE_FILENAME_RE.fullmatch(name):
        raise ValueError(f"invalid Markdown unit filename: {name!r}")
    return name


def _validated_part_page_ranges(
    unit: Mapping[str, Any],
    names: list[str],
    chapter_start: int,
    chapter_end: int,
) -> dict[str, tuple[int, int]] | None:
    """Return validated per-part ``(start_page, end_page)`` bounds when explicit."""
    if len(names) <= 1:
        return None
    raw = unit.get("part_page_ranges")
    if not isinstance(raw, Mapping):
        return None
    parsed: dict[str, tuple[int, int]] = {}
    for name in names:
        bounds = raw.get(name)
        if not isinstance(bounds, (list, tuple)) or len(bounds) != 2:
            return None
        p_start, p_end = bounds
        if (
            isinstance(p_start, bool)
            or isinstance(p_end, bool)
            or not isinstance(p_start, int)
            or not isinstance(p_end, int)
        ):
            return None
        if not (chapter_start <= p_start <= p_end <= chapter_end):
            return None
        parsed[name] = (p_start, p_end)
    return parsed


def unit_records(output_dir: Path) -> list[dict[str, Any]]:
    """Return refined unit records indexed from ``tree_progress.json``."""
    output_dir = Path(output_dir)
    try:
        progress = json.loads((output_dir / "ocr_markdown" / "tree_progress.json").read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError("ocr_markdown/tree_progress.json is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read ocr_markdown/tree_progress.json: {exc}") from exc
    if not isinstance(progress, Mapping):
        raise ValueError("tree_progress.json must contain an object")

    records: list[dict[str, Any]] = []
    for unit_order, unit in enumerate(progress.get("units", []) or []):
        if not isinstance(unit, Mapping):
            continue
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            raise ValueError("tree_progress unit has an invalid page_range")
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("tree_progress unit has a non-integer page_range") from exc
        index_path = unit.get("index_path")
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id:
            chapter_id = f"toc-unit:{unit_id}"
        elif isinstance(index_path, list) and index_path:
            chapter_id = f"toc-path:{'.'.join(str(value) for value in index_path)}"
        else:
            chapter_id = f"unit:{unit.get('file')}"
        names = [_safe_unit_filename(raw_name) for raw_name in (unit.get("part_files") or [unit.get("file")])]
        explicit_ranges = _validated_part_page_ranges(unit, names, start_page, end_page)
        for part_index, name in enumerate(names, 1):
            p_start, p_end = (
                explicit_ranges[name]
                if explicit_ranges is not None
                else (start_page, end_page)
            )
            records.append({
                "name": name,
                "start_page": p_start,
                "end_page": p_end,
                "chapter_start_page": start_page,
                "chapter_end_page": end_page,
                "chapter_id": chapter_id,
                "unit_order": unit_order,
                "part_index": part_index,
                "part_count": len(names),
                "type": str(unit.get("type") or "").strip().lower(),
                "index_path": index_path,
            })
    if not records:
        raise ValueError("tree_progress.json contains no Markdown units")
    return records


@dataclass(frozen=True)
class FootnoteSourceSnapshot:
    source_dir: Path
    records: tuple[dict[str, Any], ...]
    source_texts: Mapping[str, str]
    source_hashes: Mapping[str, str]


def project_footnote_source_text(raw: bytes | str) -> str:
    """Return the coordinate space used by footnote edit-plan source spans."""
    text = raw.decode("utf-8") if isinstance(raw, bytes) else str(raw)
    text = text.replace("\r\n", "\n").replace("\r", "\n")
    return split_multi_superscripts(text)


def load_footnote_source_snapshot(output_dir: Path) -> FootnoteSourceSnapshot:
    """Read all refined Markdown units and fail if the refined map is incomplete."""
    output_dir = Path(output_dir)
    source_dir = output_dir / "ocr_markdown"
    if not source_dir.is_dir():
        raise ValueError(f"source directory is missing: {source_dir}")

    records = tuple(unit_records(output_dir))
    if not records:
        raise ValueError("tree_progress contains no refined Markdown units")

    mapped_names = {record.get("name") for record in records}
    if len(mapped_names) != len(records):
        raise ValueError("tree_progress maps a Markdown unit more than once")
    disk_names = {path.name for path in source_dir.glob("*.md") if path.is_file()}
    if disk_names != mapped_names:
        missing = sorted(mapped_names - disk_names)
        orphaned = sorted(disk_names - mapped_names)
        details = []
        if missing:
            details.append("missing mapped files: " + ", ".join(missing))
        if orphaned:
            details.append("unmapped Markdown files: " + ", ".join(orphaned))
        raise ValueError("refined source inventory does not match tree_progress (" + "; ".join(details) + ")")

    source_texts: dict[str, str] = {}
    source_hashes: dict[str, str] = {}
    for record in records:
        name = record.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tree_progress contains an invalid Markdown unit filename")
        if name in source_texts:
            raise ValueError("tree_progress contains duplicate Markdown unit filenames")
        path = source_dir / name
        data = path.read_bytes()
        source_hashes[name] = hashlib.sha256(data).hexdigest()
        source_texts[name] = project_footnote_source_text(data)

    if len(source_texts) != len(records):
        raise ValueError("tree_progress source inventory is incomplete")
    return FootnoteSourceSnapshot(source_dir, records, source_texts, source_hashes)


__all__ = [
    "FootnoteSourceSnapshot",
    "project_footnote_source_text",
    "load_footnote_source_snapshot",
    "markdown_inventory",
    "unit_records",
]

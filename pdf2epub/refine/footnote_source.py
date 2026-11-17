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


def unit_records(output_dir: Path) -> list[dict[str, Any]]:
    """Read refined unit mapping and expand ordered split parts."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise ValueError(f"ocr_markdown/tree_progress.json is missing") from exc
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
        names = unit.get("part_files") or [unit.get("file")]
        for part_index, raw_name in enumerate(names, 1):
            records.append({
                "name": _safe_unit_filename(raw_name),
                "start_page": start_page,
                "end_page": end_page,
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
        text = data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
        source_hashes[name] = hashlib.sha256(data).hexdigest()
        source_texts[name] = split_multi_superscripts(text)

    if len(source_texts) != len(records):
        raise ValueError("tree_progress source inventory is incomplete")
    return FootnoteSourceSnapshot(source_dir, records, source_texts, source_hashes)


__all__ = [
    "FootnoteSourceSnapshot",
    "load_footnote_source_snapshot",
    "markdown_inventory",
    "unit_records",
]

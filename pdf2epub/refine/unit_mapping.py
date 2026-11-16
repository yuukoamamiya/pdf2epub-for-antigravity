"""Read refined PDF unit-to-part mappings shared by workflow layers."""
from __future__ import annotations

import json
from pathlib import Path
from collections.abc import Mapping
from typing import Any


def load_pdf_continuation_files(output_dir: Path, source_dir: Path) -> dict[str, dict[str, Any]]:
    """Return continuation metadata from the current refined unit mapping."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if not progress_path.is_file():
        return {}
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"refined unit mapping cannot be read: {progress_path}") from exc
    if not isinstance(progress, Mapping):
        raise ValueError("refined unit mapping must be a JSON object")
    units = progress.get("units", [])
    if not isinstance(units, list):
        raise ValueError("refined unit mapping units must be a list")

    available = {path.name for path in Path(source_dir).glob("*.md")}
    metadata: dict[str, dict[str, Any]] = {}
    for index, unit in enumerate(units):
        if not isinstance(unit, Mapping):
            raise ValueError(f"refined unit mapping units[{index}] must be an object")
        names_value = unit.get("part_files", [])
        if not isinstance(names_value, list):
            raise ValueError(f"refined unit mapping units[{index}].part_files must be a list")
        names: list[str] = []
        for part_index, name in enumerate(names_value):
            if not isinstance(name, str) or not name or Path(name).name != name or name in {".", ".."}:
                raise ValueError(
                    f"refined unit mapping units[{index}].part_files[{part_index}] must be a filename"
                )
            names.append(name)
        if len(names) < 2:
            continue
        part_count = len(names)
        for index, name in enumerate(names, 1):
            if index > 1 and name in available:
                metadata[name] = {
                    "is_continuation": True,
                    "part_number": index,
                    "part_count": part_count,
                    "previous_file": names[index - 2],
                }
    return metadata

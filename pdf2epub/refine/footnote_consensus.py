"""Small helpers for current two-OCR evidence used by footnote preparation."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping


_PAGE_NAME_RE = re.compile(r"^page_(\d+)\.md$")


def load_consensus_review_pages(output_dir: Path) -> set[int]:
    """Return pages explicitly marked for visual review by OCR consensus."""
    try:
        value = json.loads(
            (Path(output_dir) / "ocr_consensus.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return set()
    records = value.get("records", {}) if isinstance(value, Mapping) else {}
    if not isinstance(records, Mapping):
        return set()
    pages: set[int] = set()
    for name, record in records.items():
        if not isinstance(record, Mapping) or record.get("action") != "visual_review":
            continue
        match = _PAGE_NAME_RE.match(str(name))
        if match:
            pages.add(int(match.group(1)))
    return pages


def secondary_sidecar_path(output_dir: Path, sidecar_path: Path) -> Path:
    """Return the secondary OCR sidecar matching a primary sidecar."""
    return Path(output_dir) / "ocr_secondary" / sidecar_path.name


__all__ = ["load_consensus_review_pages", "secondary_sidecar_path"]

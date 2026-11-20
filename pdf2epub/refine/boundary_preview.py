"""Read-only preview of cross-page boundaries that may join footnotes to body text.

``refine-local`` merges visual lines across pages before any footnote decision
exists.  When a page ends with a footnote-style line and the next page starts
with body text, the merge can hide a boundary that polish later has to
rediscover, which is the footnote → polish loop.  This module records the
suspect boundaries as advisory evidence for the polish prompt; it never edits
Markdown or creates footnote decisions.
"""
from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping

from .footnote_source import unit_records
from ..workflow_contracts import atomic_write_json, sha256_file


BOUNDARY_PREVIEW_SCHEMA_VERSION = 1
BOUNDARY_PREVIEW_FILENAME = "refine_boundary_preview.json"

_NOTE_LINE_RE = re.compile(
    r"^\s*(?:[-*+]\s+)?(?:"
    r"\d{1,3}[.)、：:]"
    r"|\[\^[^\]]+\]"
    r"|<sup\b"
    r"|footnotes?\b"
    r"|注\s*[:：]?"
    r")",
    re.IGNORECASE,
)
_OPEN_TAIL_RE = re.compile(r"[-‐‑—,，:：;；]$")
_HEADING_RE = re.compile(r"^\s*#{1,6}\s+")


def _page_path(pages_dir: Path, page: int) -> Path:
    return Path(pages_dir) / f"page_{page:03d}.md"


def _nonempty_lines(text: str) -> list[str]:
    return [line.rstrip() for line in text.splitlines() if line.strip()]


def _normalise(text: str) -> str:
    return re.sub(r"\s+", "", text)


def _paragraphs(text: str) -> list[str]:
    """Split on blank lines without depending on a regex escape."""
    paragraphs: list[str] = []
    current: list[str] = []
    for line in text.split(chr(10)):
        if line.strip():
            current.append(line)
        elif current:
            paragraphs.append(" ".join(current))
            current = []
    if current:
        paragraphs.append(" ".join(current))
    return paragraphs


def _joined_in_unit(unit_text: str, tail: str, head: str) -> bool:
    """Return True when the merged unit contains the two lines adjacent.

    A blank line in the refined unit is a real paragraph boundary; only a
    single newline or space between the two lines counts as a merge.
    """
    tail_key = _normalise(tail)[-40:]
    head_key = _normalise(head)[:40]
    if not tail_key or not head_key:
        return False
    combined = tail_key + head_key
    return any(combined in _normalise(item) for item in _paragraphs(unit_text))


def _boundary_suspect(
    unit: Mapping[str, Any],
    page: int,
    tail_lines: list[str],
    head_lines: list[str],
    unit_text: str,
) -> dict[str, Any] | None:
    if not tail_lines or not head_lines:
        return None
    tail = tail_lines[-1]
    head = head_lines[0]
    if not tail.strip() or not head.strip():
        return None
    if not _joined_in_unit(unit_text, tail, head):
        # The merger already kept a paragraph boundary; nothing to preview.
        return None
    tail_note = bool(_NOTE_LINE_RE.search(tail))
    head_note = bool(_NOTE_LINE_RE.search(head))
    head_heading = bool(_HEADING_RE.search(head))
    reasons: list[str] = []
    severity = "medium"
    if tail_note and not head_note and not head_heading:
        reasons.append("footnote_then_body_joined_without_paragraph_break")
        severity = "high"
    elif not tail_note and head_note:
        reasons.append("body_then_footnote_joined_without_paragraph_break")
    elif _OPEN_TAIL_RE.search(tail) and head[:1].islower():
        reasons.append("open_sentence_joined_across_page_break")
    if not reasons:
        return None
    return {
        "unit": str(unit.get("name") or ""),
        "chapter_id": str(unit.get("chapter_id") or ""),
        "pages": [page, page + 1],
        "severity": severity,
        "reasons": reasons,
        "tail": tail[-240:],
        "head": head[:240],
    }


def build_boundary_preview(output_dir: Path, pages_dir: Path) -> dict[str, Any]:
    """Scan refined units for suspect cross-page joins (read-only)."""
    output_dir = Path(output_dir)
    pages_dir = Path(pages_dir)
    units = unit_records(output_dir)
    suspects: list[dict[str, Any]] = []
    seen: set[tuple[str, int]] = set()
    for unit in units:
        name = str(unit.get("name") or "")
        source = output_dir / "ocr_markdown" / name
        if not name or not source.is_file():
            continue
        unit_text = source.read_text(encoding="utf-8")
        start = int(unit.get("start_page") or 0)
        end = int(unit.get("end_page") or 0)
        for page in range(start, end):
            key = (name, page)
            if key in seen:
                continue
            seen.add(key)
            earlier = _page_path(pages_dir, page)
            later = _page_path(pages_dir, page + 1)
            if not earlier.is_file() or not later.is_file():
                continue
            try:
                tail_lines = _nonempty_lines(earlier.read_text(encoding="utf-8"))
                head_lines = _nonempty_lines(later.read_text(encoding="utf-8"))
            except (OSError, UnicodeError):
                continue
            suspect = _boundary_suspect(unit, page, tail_lines, head_lines, unit_text)
            if suspect is not None:
                suspects.append(suspect)
    return {
        "schema_version": BOUNDARY_PREVIEW_SCHEMA_VERSION,
        "source_sha256": {
            name: sha256_file(output_dir / "ocr_markdown" / name)
            for name in sorted({item["unit"] for item in suspects})
            if (output_dir / "ocr_markdown" / name).is_file()
        },
        "suspect_count": len(suspects),
        "suspects": suspects,
    }


def write_boundary_preview(output_dir: Path, pages_dir: Path) -> dict[str, Any]:
    """Write ``refine_boundary_preview.json`` and return its payload."""
    report = build_boundary_preview(output_dir, pages_dir)
    atomic_write_json(Path(output_dir) / BOUNDARY_PREVIEW_FILENAME, report)
    return report


def load_boundary_preview(output_dir: Path) -> dict[str, Any]:
    """Read the preview leniently; a missing report means no suspects."""
    path = Path(output_dir) / BOUNDARY_PREVIEW_FILENAME
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {"suspect_count": 0, "suspects": []}
    return value if isinstance(value, dict) else {"suspect_count": 0, "suspects": []}


__all__ = [
    "BOUNDARY_PREVIEW_FILENAME",
    "BOUNDARY_PREVIEW_SCHEMA_VERSION",
    "build_boundary_preview",
    "load_boundary_preview",
    "write_boundary_preview",
]

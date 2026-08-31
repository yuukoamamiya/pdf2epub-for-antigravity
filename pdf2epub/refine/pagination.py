"""Local printed-page-number hints for PDF TOC analysis.

The OCR page filename is the authoritative physical page.  This module only
extracts likely printed labels so a structure Subagent can reason about Roman
front matter and Arabic body pagination without silently changing ranges.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


_ARABIC = re.compile(r"^(?:page\s*)?(\d{1,4})$", re.IGNORECASE)
_ROMAN = re.compile(r"^(?:page\s*)?([ivxlcdm]{1,12})$", re.IGNORECASE)


def _roman_value(value: str) -> Optional[int]:
    value = value.lower()
    if not re.fullmatch(r"[ivxlcdm]+", value):
        return None
    values = {"i": 1, "v": 5, "x": 10, "l": 50, "c": 100, "d": 500, "m": 1000}
    total = 0
    previous = 0
    for char in reversed(value):
        current = values[char]
        total += -current if current < previous else current
        previous = max(previous, current)
    # Reject non-canonical forms such as xxxx or ic.
    numeric_value = total
    canonical = ""
    remaining = numeric_value
    for amount, symbol in ((1000, "m"), (900, "cm"), (500, "d"), (400, "cd"),
                           (100, "c"), (90, "xc"), (50, "l"), (40, "xl"),
                           (10, "x"), (9, "ix"), (5, "v"), (4, "iv"), (1, "i")):
        count, remaining = remaining // amount, remaining % amount
        canonical += symbol * count
    return numeric_value if canonical == value else None


def _printed_candidates(text: str) -> List[Dict[str, Any]]:
    candidates = []
    for line_number, raw in enumerate(text.splitlines(), 1):
        line = raw.strip().strip("|•·—–-").strip()
        if not line or len(line) > 24 or re.search(r"[A-Za-z]{3,}\s+[A-Za-z]{3,}", line):
            continue
        arabic = _ARABIC.fullmatch(line)
        if arabic and 1 <= int(arabic.group(1)) <= 9999:
            candidates.append({"label": arabic.group(1), "value": int(arabic.group(1)), "kind": "arabic", "line": line_number})
            continue
        roman = _ROMAN.fullmatch(line)
        if roman:
            value = _roman_value(roman.group(1))
            if value is not None and 1 <= value <= 999:
                candidates.append({"label": roman.group(1), "value": value, "kind": "roman", "line": line_number})
    return candidates


def build_pagination_map(pages_dir: Path, output_path: Path) -> Dict[str, Any]:
    """Write a JSON map of likely printed page labels and offset hints."""
    pages = []
    for page in sorted(Path(pages_dir).glob("page_*.md")):
        match = re.fullmatch(r"page_(\d+)\.md", page.name)
        if not match:
            continue
        candidates = _printed_candidates(page.read_text(encoding="utf-8", errors="replace"))
        pages.append({"physical_page": int(match.group(1)), "candidates": candidates})

    observations = []
    for item in pages:
        for candidate in item["candidates"]:
            if candidate["kind"] == "arabic":
                observations.append(item["physical_page"] - candidate["value"])
    offset = None
    confidence = "none"
    if observations:
        counts = {value: observations.count(value) for value in set(observations)}
        offset, count = max(counts.items(), key=lambda pair: pair[1])
        confidence = "high" if count >= 3 else "medium" if count >= 2 else "low"

    result = {
        "schema_version": 1,
        "physical_page_is_authoritative": True,
        "pages": pages,
        "arabic_offset_hint": offset,
        "offset_confidence": confidence,
        "notes": "Printed labels are OCR heuristics for Subagent review; do not use them as physical page ranges.",
    }
    output_path = Path(output_path)
    output_path.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    return result

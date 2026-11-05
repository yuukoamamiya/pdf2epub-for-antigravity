"""Exact source-range checks for reviewed headings and layout insertions."""
from __future__ import annotations

import re
from typing import Sequence

from ..toc_model import normalize_toc_source_label


def source_range_matches_title(
    lines: Sequence[str], start_line: int, end_line: int, title: str, *, anchor_line: int | None = None
) -> bool:
    """Check an exact ATX heading sequence at a 1-based source anchor.

    Context in the checked range never authorizes a match elsewhere. Only the
    contiguous heading block beginning at ``anchor_line`` can prove that an
    insertion would duplicate a source heading.
    """
    if type(start_line) is not int or type(end_line) is not int or start_line < 1 or end_line <= start_line:
        return False
    anchor = start_line if anchor_line is None else anchor_line
    if not start_line <= anchor <= end_line:
        return False
    region = list(lines[anchor - 1:end_line - 1])
    target = normalize_toc_source_label(str(title or ""), markdown_source=False)
    if not target:
        return False
    visible: list[str] = []
    for line in region:
        if not line.strip():
            continue
        is_heading = bool(
            re.match(r"^\s*#{1,6}\s+", line)
            or re.match(r"^\s*(?:\*\*?[^*]+\*\*?|__?[^_]+__?|<(?:em|i)\b)", line, re.I)
            or re.match(r"^\s*(?:chapter|part|book|volume|section)\s+", line, re.I)
        )
        if not is_heading:
            # A plain title can only prove duplication at the first nonblank
            # line anchored for this insertion. Later text in the checked
            # window is context, not an alternate search location.
            if not visible and normalize_toc_source_label(line, markdown_source=True) == target:
                return True
            break
        visible.append(re.sub(r"^\s*#{1,6}\s*", "", line).strip())
        candidate = normalize_toc_source_label(" ".join(visible), markdown_source=True)
        if candidate == target:
            return True
    return False

"""Deterministic splitting of oversized refined Markdown units.

The PDF refine stage normally emits one Markdown file per TOC unit.  A large
chapter can exceed the safe Subagent transaction size even when its TOC entry
is valid.  This module splits such files at Markdown block boundaries (which
also preserve the page separators inserted by :class:`PageMerger`) and falls
back to physical line boundaries for an unusually large atomic block.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Callable, List


SPECIAL_UNIT_TYPES = frozenset({"notes", "bibliography", "index"})


@dataclass(frozen=True)
class SplitMarkdownResult:
    """The parts and the boundary strategy used to create them."""

    parts: List[str]
    strategy: str


def _blocks(text: str) -> List[str]:
    """Split Markdown into paragraph-like blocks without losing separators.

    A heading starts a new block even when OCR omitted the blank line before
    it.  This makes ordinary body chapters split cleanly at section headings,
    while retaining every source character in the concatenated result.
    """
    lines = text.splitlines(keepends=True)
    if not lines:
        return []

    blocks: List[str] = []
    current: List[str] = []
    heading = re.compile(r"^\s{0,3}#{1,6}\s+\S")
    for index, line in enumerate(lines):
        if current and line.strip() and heading.match(line) and any(
            item.strip() for item in current
        ):
            blocks.append("".join(current))
            current = []
        current.append(line)
        next_line = lines[index + 1] if index + 1 < len(lines) else None
        if not line.strip() and next_line is not None and next_line.strip():
            blocks.append("".join(current))
            current = []
    if current:
        blocks.append("".join(current))
    return blocks


def _merge_index_continuations(blocks: List[str]) -> List[str]:
    """Keep an OCR ``*(continued)*`` index entry with its preceding entry."""
    merged: List[str] = []
    continuation = re.compile(r"^\s*[^\n]*\*\(continued\)\*", re.IGNORECASE)
    for block in blocks:
        if merged and continuation.search(block):
            merged[-1] += block
        else:
            merged.append(block)
    return merged


def _split_by_lines(
    text: str,
    target_tokens: int,
    estimate_tokens: Callable[[str], int],
) -> List[str]:
    """Split an atomic block at complete source-line boundaries."""
    lines = text.splitlines(keepends=True)
    parts: List[str] = []
    current: List[str] = []
    for line in lines:
        candidate = "".join(current) + line
        if current and estimate_tokens(candidate) > target_tokens:
            parts.append("".join(current))
            current = [line]
        else:
            current.append(line)
    if current:
        parts.append("".join(current))
    return parts or [text]


def split_markdown_unit(
    text: str,
    target_tokens: int,
    role: str,
    estimate_tokens: Callable[[str], int],
) -> SplitMarkdownResult:
    """Split an oversized special unit while preserving source order.

    ``role`` is used to preserve an index continuation entry.  Notes and
    bibliography entries are naturally kept together by their blank-line
    boundaries.  If a single block is larger than the target, the final
    fallback uses complete source lines so the result remains deterministic
    and never silently drops content.
    """
    if target_tokens <= 0:
        raise ValueError("target_tokens must be positive")
    if estimate_tokens(text) <= target_tokens:
        return SplitMarkdownResult([text], "none")

    blocks = _blocks(text)
    if role == "index":
        blocks = _merge_index_continuations(blocks)
    if not blocks:
        return SplitMarkdownResult([text], "none")

    parts: List[str] = []
    current: List[str] = []
    current_tokens = 0
    used_line_fallback = False

    for block in blocks:
        block_tokens = estimate_tokens(block)
        if block_tokens > target_tokens:
            if current:
                parts.append("".join(current))
                current = []
                current_tokens = 0
            line_parts = _split_by_lines(block, target_tokens, estimate_tokens)
            parts.extend(line_parts[:-1])
            current = [line_parts[-1]]
            current_tokens = estimate_tokens(line_parts[-1])
            used_line_fallback = True
            continue

        candidate = "".join(current) + block
        candidate_tokens = estimate_tokens(candidate)
        if current and candidate_tokens > target_tokens:
            parts.append("".join(current))
            current = []
            current_tokens = 0
        current.append(block)
        current_tokens = estimate_tokens("".join(current))

    if current:
        parts.append("".join(current))

    strategy = "line-fallback" if used_line_fallback else "entry-boundary"
    return SplitMarkdownResult(parts or [text], strategy)

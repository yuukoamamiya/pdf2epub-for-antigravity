"""
Page merging with precise boundary cutting.

Merges page content for TOC nodes, using boundary_info (start_line/end_line)
to precisely cut content at section boundaries.
"""

from pathlib import Path
import re
from typing import Iterable, List, Optional, Set, Tuple
from loguru import logger

from .toc_tree import TOCNode
from ..utils.ocr_artifacts import (
    clean_ocr_page_artifacts,
    remove_printed_page_number_lines,
    remove_repeated_page_header,
)


class PageMerger:
    """
    Merges pages for TOC nodes with precise line-based boundary cutting.

    Uses start_line/end_line from boundary_info to handle mid-page splits.
    """

    def merge_node_content(
        self,
        node: TOCNode,
        pages_dir: Path,
        next_node: TOCNode = None,
        illustration_pages: Optional[Set[int]] = None,
    ) -> str:
        """
        Merge page content for a node.

        Uses boundary_info.start_line/end_line for precise cutting when
        sections share a page.

        Args:
            node: TOCNode to merge content for
            pages_dir: Directory containing page files
            next_node: Next sibling node (to get its start_line for end boundary)

        Returns:
            Merged content string
        """
        content_parts: list[tuple[int, str]] = []
        illustration_pages = set(illustration_pages or set())
        boundary = node.boundary_info or {}
        previous_header = None

        for page_num in range(node.start_page, node.end_page + 1):
            page_file = pages_dir / f"page_{page_num:03d}.md"
            if not page_file.exists():
                logger.warning(f"Page file not found: {page_file}")
                continue

            page_content = page_file.read_text(encoding='utf-8')
            lines = page_content.split('\n')

            # Boundary line numbers are absolute within the original page.
            # Compute both offsets before slicing so a node that starts and
            # ends on one page does not apply end_line to an already-trimmed
            # list (which used to retain/drop the wrong lines).
            start_index = 0
            end_index = len(lines)
            if page_num == node.start_page:
                start_line = boundary.get('start_line')
                if isinstance(start_line, int) and start_line > 1:
                    start_index = start_line - 1
                    logger.debug(f"Node '{node.title}' starts at line {start_line}")

            # Handle last page - end at end_line if set, or at next_node's start_line
            if page_num == node.end_page:
                end_line = boundary.get('end_line')
                if isinstance(end_line, int):
                    # end_line is 1-indexed and exclusive.
                    end_index = min(end_index, end_line - 1)
                    logger.debug(f"Node '{node.title}' ends at line {end_line}")
                elif next_node and next_node.start_page == node.end_page:
                    # Next section starts on same page - cut before it
                    next_boundary = next_node.boundary_info or {}
                    next_start_line = next_boundary.get('start_line')
                    if isinstance(next_start_line, int):
                        end_index = min(end_index, next_start_line - 1)
                        logger.debug(f"Cutting before next section at line {next_start_line}")

            lines = lines[start_index:end_index] if end_index >= start_index else []

            page_content = '\n'.join(lines)
            page_content = clean_ocr_page_artifacts(page_content)
            lines = page_content.split('\n')
            lines = remove_printed_page_number_lines(lines)
            lines, current_header = remove_repeated_page_header(lines, previous_header)
            if current_header is not None and current_header == previous_header:
                logger.debug(f"Removed repeated running header on page {page_num}")
            previous_header = current_header
            page_content = '\n'.join(lines)
            if page_content.strip():
                content_parts.append((page_num, page_content))

        return _merge_full_page_insertions(content_parts, illustration_pages)

    def merge_nodes_content(
        self,
        nodes: List[TOCNode],
        pages_dir: Path,
        next_node: TOCNode = None,
        illustration_pages: Optional[Set[int]] = None,
    ) -> str:
        """
        Merge content for multiple consecutive nodes.

        Used when a parent node is treated as a single unit.

        Args:
            nodes: List of TOCNodes to merge
            pages_dir: Directory containing page files
            next_node: Next sibling node (to get its start_line for end boundary)

        Returns:
            Merged content string
        """
        if not nodes:
            return ""

        # Get the full page range (use min/max in case nodes are not in page order)
        start_page = min(n.start_page for n in nodes)
        end_page = max(n.end_page for n in nodes)
        first_boundary = nodes[0].boundary_info or {}
        previous_header = None

        content_parts: list[tuple[int, str]] = []
        illustration_pages = set(illustration_pages or set())

        for page_num in range(start_page, end_page + 1):
            page_file = pages_dir / f"page_{page_num:03d}.md"
            if not page_file.exists():
                continue

            page_content = page_file.read_text(encoding='utf-8')
            lines = page_content.split('\n')

            # Handle first page of first node
            start_index = 0
            end_index = len(lines)
            if page_num == start_page:
                start_line = first_boundary.get('start_line')
                if isinstance(start_line, int) and start_line > 1:
                    start_index = start_line - 1

            # Handle last page - apply the node boundary and then the next
            # unit boundary, both measured against the original page.
            if page_num == end_page:
                end_line = first_boundary.get('end_line')
                if isinstance(end_line, int):
                    end_index = min(end_index, end_line - 1)
            if page_num == end_page and next_node and next_node.start_page == end_page:
                next_boundary = next_node.boundary_info or {}
                next_start_line = next_boundary.get('start_line')
                if isinstance(next_start_line, int):
                    end_index = min(end_index, next_start_line - 1)

            lines = lines[start_index:end_index] if end_index >= start_index else []

            page_content = '\n'.join(lines)
            page_content = clean_ocr_page_artifacts(page_content)
            lines = page_content.split('\n')
            lines = remove_printed_page_number_lines(lines)
            lines, current_header = remove_repeated_page_header(lines, previous_header)
            if current_header is not None and current_header == previous_header:
                logger.debug(f"Removed repeated running header on page {page_num}")
            previous_header = current_header
            page_content = '\n'.join(lines)
            if page_content.strip():
                content_parts.append((page_num, page_content))

        return _merge_full_page_insertions(content_parts, illustration_pages)


_SENTENCE_END = frozenset("。！？!?；;：:….!?")
_CLOSING_MARKS = frozenset("\"'”’》）)]】〉〕」』»\u3009\u300b\u300d\u300f\u3011")
_NEXT_BLOCK_RE = re.compile(
    r"^(?:#{1,6}\s|[-*+]\s+|\d+[.)]\s+|>\s+|!\[|<img\b|\|)",
    re.IGNORECASE,
)


def _last_significant_character(value: str) -> str:
    """Return the last prose character, ignoring closing quote/bracket marks."""
    value = str(value or "").rstrip()
    while value and value[-1] in _CLOSING_MARKS:
        value = value[:-1].rstrip()
    return value[-1:] if value else ""


def _looks_like_new_block(value: str) -> bool:
    stripped = str(value or "").lstrip()
    return not stripped or bool(_NEXT_BLOCK_RE.match(stripped)) or stripped.startswith("[^")


def _can_join_across_full_page(previous: str, following: str) -> bool:
    """Conservatively recognize a sentence interrupted by a visual page."""
    previous = str(previous or "").strip()
    following = str(following or "").strip()
    if not previous or not following or _looks_like_new_block(following):
        return False
    previous_last_line = next(
        (line.strip() for line in reversed(previous.splitlines()) if line.strip()),
        "",
    )
    if _looks_like_new_block(previous_last_line):
        return False
    last = _last_significant_character(previous_last_line)
    return bool(last) and last not in _SENTENCE_END


def _join_page_fragments(previous: str, following: str) -> str:
    """Join OCR fragments without inserting a space into CJK or hyphenation."""
    previous = str(previous or "").rstrip()
    following = str(following or "").lstrip()
    if not previous:
        return following
    if not following:
        return previous
    if previous.endswith("-") or following[0] in ",.;:!?\uff0c\u3002\uff1b\uff1a\uff01\uff1f\u3001)]}\u3011\u300b\u300d\u300f\u201d\u2019\"":
        separator = ""
    elif re.search(r"[\u3400-\u9fff\u3040-\u30ff]$", previous) or re.match(
        r"^[\u3400-\u9fff\u3040-\u30ff]", following
    ):
        separator = ""
    else:
        separator = " "
    return previous + separator + following


def _merge_full_page_insertions(
    entries: Iterable[Tuple[int, str]],
    illustration_pages: Set[int],
) -> str:
    """Keep full-page images, but move them after a repaired interrupted sentence.

    Only pages explicitly classified by the reviewed illustration binding are
    eligible.  Ordinary figures, captions, and all unreviewed pages retain the
    physical page order.
    """
    ordered = [(int(page), str(content)) for page, content in entries]
    merged: list[str] = []
    index = 0
    while index < len(ordered):
        page, content = ordered[index]
        is_full_page = page in illustration_pages
        if (
            is_full_page
            and index > 0
            and index + 1 < len(ordered)
            and ordered[index - 1][0] not in illustration_pages
            and ordered[index + 1][0] not in illustration_pages
            and _can_join_across_full_page(merged[-1] if merged else "", ordered[index + 1][1])
        ):
            merged[-1] = _join_page_fragments(merged[-1], ordered[index + 1][1])
            if content.strip():
                merged.append(content)
            index += 2
            continue
        if content.strip():
            merged.append(content)
        index += 1
    return "\n\n".join(merged)

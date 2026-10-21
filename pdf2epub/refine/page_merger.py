"""
Page merging with precise boundary cutting.

Merges page content for TOC nodes, using boundary_info (start_line/end_line)
to precisely cut content at section boundaries.
"""

import json
import html
from pathlib import Path
import re
from typing import Iterable, List, Optional, Set, Tuple
from loguru import logger

from .toc_tree import TOCNode
from .toc_heading_recovery import title_present_in_lines
from .layout_evidence import normalized_bbox, text_from_block
from ..utils.ocr_artifacts import (
    clean_ocr_page_artifacts,
    native_repeated_edge_furniture_index,
    remove_printed_page_number_lines,
    remove_repeated_page_header,
    remove_native_page_furniture,
)


def _plain_layout_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"^\s*#{1,6}\s+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    return re.sub(r"^\d{1,4}\s+", "", value).strip()


def _running_header_signature(value: str) -> str:
    plain = _plain_layout_text(value).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", " ", plain, flags=re.UNICODE).strip()


def _remove_running_header_prefix(line: str, header_text: str) -> tuple[bool, str]:
    """Remove a sidecar-confirmed header prefix, preserving any body suffix."""
    plain_header = _plain_layout_text(header_text)
    plain_line = html.unescape(re.sub(r"<[^>]+>", " ", str(line or "")))
    plain_line = re.sub(r"^\s*#{1,6}\s+", "", plain_line)
    plain_line = re.sub(r"[`*_~]", "", plain_line)
    plain_line = re.sub(r"^\s*\d{1,4}\s+", "", plain_line).lstrip()
    if not plain_header:
        return False, line

    header_folded = plain_header.casefold()
    line_folded = plain_line.casefold()
    if not line_folded.startswith(header_folded):
        return False, line
    if len(plain_line) > len(plain_header) and not plain_line[len(plain_header)].isspace():
        return False, line
    remainder = plain_line[len(plain_header):].lstrip()
    return True, remainder


class PageMerger:
    """
    Merges pages for TOC nodes with precise line-based boundary cutting.

    Uses start_line/end_line from boundary_info to handle mid-page splits.
    """

    def __init__(self) -> None:
        self._native_furniture_cache: dict[Path, dict[str, int]] = {}
        self._visual_running_header_cache: dict[Path, dict[int, list[str]]] = {}
        self._visual_sidecar_pages_cache: dict[Path, set[int]] = {}
        self._toc_heading_recoveries: dict[int, list[dict]] = {}

    @staticmethod
    def _evidence_pages_dir(pages_dir: Path) -> Path:
        pages_dir = Path(pages_dir).resolve()
        if any(pages_dir.glob("page_*.ocr.json")):
            return pages_dir
        for parent in pages_dir.parents:
            candidate = parent / "pages"
            if any(candidate.glob("page_*.ocr.json")):
                return candidate
        return pages_dir

    def _visual_running_headers(self, pages_dir: Path) -> dict[int, list[str]]:
        """Index repeated top-margin Section-Header blocks from OCR sidecars.

        The OCR model may label a running title as a section heading and prefix
        it with the printed page number. Only a short, geometrically topmost
        Section-Header repeated on multiple physical pages is eligible.
        """
        evidence_dir = self._evidence_pages_dir(pages_dir)
        cached = self._visual_running_header_cache.get(evidence_dir)
        if cached is not None:
            return cached

        by_page: dict[int, list[tuple[str, str]]] = {}
        pages_by_signature: dict[str, set[int]] = {}
        visual_sidecar_pages: set[int] = set()
        for path in sorted(evidence_dir.glob("page_*.ocr.json")):
            try:
                sidecar = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if not isinstance(sidecar, dict) or str(
                sidecar.get("source_kind") or ""
            ).strip().lower() == "native_text":
                continue
            try:
                page_number = int(sidecar.get("page_number"))
            except (TypeError, ValueError):
                match = re.search(r"page_(\d+)\.ocr\.json$", path.name)
                if not match:
                    continue
                page_number = int(match.group(1))
            visual_sidecar_pages.add(page_number)

            for block in sidecar.get("blocks", []) or []:
                if not isinstance(block, dict) or str(
                    block.get("label") or ""
                ).strip().casefold() != "section-header":
                    continue
                bbox = normalized_bbox(block, sidecar)
                if bbox is None:
                    continue
                _x0, y0, _x1, y1 = bbox
                if y0 > 0.15 or y1 - y0 > 0.06:
                    continue
                text = text_from_block(block)
                signature = _running_header_signature(text)
                if len(signature) < 5 or len(signature) > 120:
                    continue
                by_page.setdefault(page_number, []).append((signature, text))
                pages_by_signature.setdefault(signature, set()).add(page_number)

        repeated = {
            signature
            for signature, page_numbers in pages_by_signature.items()
            if len(page_numbers) >= 2
        }
        indexed = {
            page_number: [
                text
                for signature, text in blocks
                if signature in repeated
            ]
            for page_number, blocks in by_page.items()
        }
        self._visual_running_header_cache[evidence_dir] = indexed
        self._visual_sidecar_pages_cache[evidence_dir] = visual_sidecar_pages
        return indexed

    def _has_visual_sidecar(self, pages_dir: Path, page_number: int) -> bool:
        self._visual_running_headers(pages_dir)
        evidence_dir = self._evidence_pages_dir(pages_dir)
        return page_number in self._visual_sidecar_pages_cache.get(evidence_dir, set())

    def _remove_visual_running_headers(
        self, lines: list[str], pages_dir: Path, page_number: int
    ) -> list[str]:
        candidates = self._visual_running_headers(pages_dir).get(page_number, [])
        if not candidates:
            return lines
        result = list(lines)
        for header_text in candidates:
            for index, line in enumerate(result):
                matched, remainder = _remove_running_header_prefix(
                    line, header_text
                )
                if not matched:
                    continue
                if remainder:
                    result[index] = remainder
                else:
                    del result[index]
                logger.debug(
                    "Removed repeated OCR running header on page %s: %s",
                    page_number,
                    header_text,
                )
                break
        return result

    def set_toc_heading_recoveries(
        self,
        recoveries: Optional[dict[int, list[dict]]],
    ) -> None:
        """Set per-page TOC headings recovered from page evidence."""
        normalized: dict[int, list[dict]] = {}
        for page, items in (recoveries or {}).items():
            try:
                page_number = int(page)
            except (TypeError, ValueError):
                continue
            if not isinstance(items, (list, tuple)):
                continue
            normalized[page_number] = [
                dict(item) for item in items if isinstance(item, dict)
            ]
        self._toc_heading_recoveries = normalized

    def _inject_toc_heading_recoveries(
        self,
        page_num: int,
        lines: list[str],
        fragment_start_line: int,
        fragment_end_line: int,
    ) -> list[str]:
        insertions = self._toc_heading_recoveries.get(page_num)
        if not insertions:
            return lines

        positioned: list[tuple[int, int, str, dict]] = []
        for order, item in enumerate(insertions):
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            if title_present_in_lines(lines, title):
                continue
            start_line = item.get("start_line")
            if isinstance(start_line, int):
                if not (fragment_start_line <= start_line <= fragment_end_line):
                    logger.debug(
                        "Skipping recovered heading %r on page %s: start_line %s "
                        "outside fragment %s..%s",
                        title,
                        page_num,
                        start_line,
                        fragment_start_line,
                        fragment_end_line,
                    )
                    continue
                offset = start_line - fragment_start_line
            else:
                offset = 0
            positioned.append((offset, order, title, item))

        for offset, _order, title, item in sorted(
            positioned,
            key=lambda entry: (entry[0], entry[1]),
            reverse=True,
        ):
            if title_present_in_lines(lines, title):
                continue
            level = item.get("level")
            try:
                level = int(level)
            except (TypeError, ValueError):
                level = 1
            level = max(1, min(6, level))
            block = ["", f"{'#' * level} {title}", ""]
            index = max(0, min(offset, len(lines)))
            lines[index:index] = block
        return lines

    def _native_furniture_index(self, pages_dir: Path) -> dict[str, int]:
        pages_dir = Path(pages_dir).resolve()
        cached = self._native_furniture_cache.get(pages_dir)
        if cached is not None:
            return cached
        sidecars = []
        for path in sorted(pages_dir.glob("page_*.ocr.json")):
            try:
                value = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                continue
            if isinstance(value, dict):
                sidecars.append(value)
        index = native_repeated_edge_furniture_index(sidecars)
        self._native_furniture_cache[pages_dir] = index
        return index

    @staticmethod
    def _native_sidecar(pages_dir: Path, page_num: int) -> dict:
        path = Path(pages_dir) / f"page_{page_num:03d}.ocr.json"
        try:
            value = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            return {}
        return value if isinstance(value, dict) else {}

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
            fragment_start_line = start_index + 1
            fragment_end_line = end_index

            page_content = '\n'.join(lines)
            page_content = clean_ocr_page_artifacts(page_content)
            lines = page_content.split('\n')
            lines = remove_printed_page_number_lines(lines)
            page_content, removed_furniture = remove_native_page_furniture(
                '\n'.join(lines),
                self._native_sidecar(pages_dir, page_num),
                repeated_index=self._native_furniture_index(pages_dir),
                page_number=page_num,
            )
            for item in removed_furniture:
                logger.debug(
                    "Removed deterministic native page furniture on page %s: %s",
                    page_num,
                    item["kind"],
                )
            lines = page_content.split('\n')
            lines = self._remove_visual_running_headers(
                lines, pages_dir, page_num
            )
            if self._has_visual_sidecar(pages_dir, page_num):
                current_header = None
            else:
                lines, current_header = remove_repeated_page_header(
                    lines, previous_header
                )
                if current_header is not None and current_header == previous_header:
                    logger.debug(f"Removed repeated running header on page {page_num}")
            previous_header = current_header
            lines = self._inject_toc_heading_recoveries(
                page_num,
                lines,
                fragment_start_line,
                fragment_end_line,
            )
            page_content = '\n'.join(lines)
            if page_content.strip():
                content_parts.append((page_num, page_content))

        return _merge_full_page_insertions(
            content_parts,
            illustration_pages,
            join_soft_page_breaks=True,
        )

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
            fragment_start_line = start_index + 1
            fragment_end_line = end_index

            page_content = '\n'.join(lines)
            page_content = clean_ocr_page_artifacts(page_content)
            lines = page_content.split('\n')
            lines = remove_printed_page_number_lines(lines)
            page_content, removed_furniture = remove_native_page_furniture(
                '\n'.join(lines),
                self._native_sidecar(pages_dir, page_num),
                repeated_index=self._native_furniture_index(pages_dir),
                page_number=page_num,
            )
            for item in removed_furniture:
                logger.debug(
                    "Removed deterministic native page furniture on page %s: %s",
                    page_num,
                    item["kind"],
                )
            lines = page_content.split('\n')
            lines = self._remove_visual_running_headers(
                lines, pages_dir, page_num
            )
            if self._has_visual_sidecar(pages_dir, page_num):
                current_header = None
            else:
                lines, current_header = remove_repeated_page_header(
                    lines, previous_header
                )
                if current_header is not None and current_header == previous_header:
                    logger.debug(f"Removed repeated running header on page {page_num}")
            previous_header = current_header
            lines = self._inject_toc_heading_recoveries(
                page_num,
                lines,
                fragment_start_line,
                fragment_end_line,
            )
            page_content = '\n'.join(lines)
            if page_content.strip():
                content_parts.append((page_num, page_content))

        return _merge_full_page_insertions(
            content_parts,
            illustration_pages,
            join_soft_page_breaks=True,
        )


_SENTENCE_END = frozenset("。！？!?；;：:….!?")
_CLOSING_MARKS = frozenset("\"'”’》）)]】〉〕」』»\u3009\u300b\u300d\u300f\u3011")
_NEXT_BLOCK_RE = re.compile(
    r"^(?:#{1,6}\s|[-*+]\s+|\d+[.)]\s+|>\s+|!\[|<img\b|<sup\b|<div\b|<table\b|\|)",
    re.IGNORECASE,
)


def _last_significant_character(value: str) -> str:
    """Return the last prose character, ignoring closing quote/bracket marks."""
    value = str(value or "").rstrip()
    while value and value[-1] in _CLOSING_MARKS:
        value = value[:-1].rstrip()
    return value[-1:] if value else ""


def _looks_like_new_block(value: str, *, numbered_is_block: bool = True) -> bool:
    stripped = str(value or "").lstrip()
    if not numbered_is_block:
        stripped = re.sub(r"^\d+[.)]\s+", "", stripped)
    return not stripped or bool(_NEXT_BLOCK_RE.match(stripped)) or stripped.startswith("[^")


def _can_join_across_page(previous: str, following: str) -> bool:
    """Conservatively recognize a prose block interrupted by a page break."""
    previous = str(previous or "").strip()
    following = str(following or "").strip()
    if not previous or not following or _looks_like_new_block(following):
        return False
    previous_last_line = next(
        (line.strip() for line in reversed(previous.splitlines()) if line.strip()),
        "",
    )
    if _looks_like_new_block(previous_last_line, numbered_is_block=False):
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
    *,
    join_soft_page_breaks: bool = False,
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
            and _can_join_across_page(merged[-1] if merged else "", ordered[index + 1][1])
        ):
            merged[-1] = _join_page_fragments(merged[-1], ordered[index + 1][1])
            if content.strip():
                merged.append(content)
            index += 2
            continue
        if content.strip():
            if (
                join_soft_page_breaks
                and merged
                and _can_join_across_page(merged[-1], content)
            ):
                merged[-1] = _join_page_fragments(merged[-1], content)
            else:
                merged.append(content)
        index += 1
    return "\n\n".join(merged)

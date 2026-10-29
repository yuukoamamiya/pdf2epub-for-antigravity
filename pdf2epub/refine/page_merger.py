"""
Page merging with precise boundary cutting.

Merges page content for TOC nodes, using boundary_info (start_line/end_line)
to precisely cut content at section boundaries.
"""

import json
import html
from pathlib import Path
import re
from typing import Any, Iterable, List, Optional, Set, Tuple
from loguru import logger

from .toc_tree import TOCNode
from .toc_heading_recovery import _iter_nodes, _titles_match, title_present_in_lines
from ..toc_model import normalize_toc_source_label
from .layout_evidence import normalized_bbox, text_from_block
from ..utils.ocr_artifacts import (
    clean_ocr_page_artifacts,
    native_repeated_edge_furniture_index,
    remove_printed_page_number_lines,
    remove_repeated_page_header,
    remove_native_page_furniture,
)


def _matches_protected_heading(value: str, titles: Iterable[str]) -> bool:
    """Match a TOC title without conflating titles with different numbers."""
    normalized = "".join(char for char in str(value or "").casefold() if char.isalnum())
    digits = tuple(re.findall(r"\d+", str(value or "")))
    if not normalized:
        return False
    matches = []
    for title in titles:
        title_text = str(title or "")
        title_normalized = "".join(char for char in title_text.casefold() if char.isalnum())
        if normalized == title_normalized:
            matches.append((title_text, tuple(re.findall(r"\d+", title_text))))
            continue
        # Keep the existing exact-normalized variants (for example, an OCR
        # "Chapter 2: Title" against a TOC "2. Title"), but require identical
        # Arabic numbering so adjacent numbered headings cannot protect each other.
        title_digits = tuple(re.findall(r"\d+", title_text))
        if _titles_match(value, title_text) and (digits == title_digits or not digits or not title_digits):
            matches.append((title_text, title_digits))
    if len(matches) != 1:
        return False
    title_digits = matches[0][1]
    return digits == title_digits or not digits or not title_digits

_ROMAN_PAGE_LABEL = r"(?=[ivxlcdm])m{0,3}(?:cm|cd|d?c{0,3})(?:xc|xl|l?x{0,3})(?:ix|iv|v?i{0,3})"
_PAGE_LABEL = rf"(?:\d{{1,4}}|{_ROMAN_PAGE_LABEL})"
_PAGE_LABEL_RE = re.compile(rf"^(?:{_PAGE_LABEL})$", re.IGNORECASE)
_PAGE_LABEL_PREFIX_RE = re.compile(rf"^\s*{_PAGE_LABEL}\s+", re.IGNORECASE)
_PAGE_LABEL_SUFFIX_RE = re.compile(rf"\s+{_PAGE_LABEL}\s*$", re.IGNORECASE)
_STRUCTURAL_NUMBERED_TITLE_RE = re.compile(
    rf"^(?:chapter|part|section|book|volume|vol\.?)\s+(?:\d{{1,4}}|{_ROMAN_PAGE_LABEL})(?:$|\s|[.:)—-])",
    re.IGNORECASE,
)
_STRUCTURAL_NUMBERED_IDENTIFIER_RE = re.compile(
    rf"^(?:chapter|part|section|book|volume|vol\.?)\s+(?:\d{{1,4}}|{_ROMAN_PAGE_LABEL})$",
    re.IGNORECASE,
)
_PAGE_LABEL_PREFIX_CAPTURE_RE = re.compile(
    rf"^\s*({_PAGE_LABEL})(?=\s|$)\s*", re.IGNORECASE
)
_HTML_TAG_RE = re.compile(r"<[^>]+>")
_HTML_ENTITY_RE = re.compile(r"&(?:#\d+|#x[\da-f]+|[a-z][\da-z]+);", re.IGNORECASE)
_INLINE_MARKUP_CLOSER_RE = re.compile(
    r"^(?:(?:[*_~`])|(?:</(?:em|strong|b|i|span)\s*>))+", re.IGNORECASE
)


def _unformatted_layout_text(value: str) -> str:
    value = html.unescape(_HTML_TAG_RE.sub(" ", str(value or "")))
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"^\s*#{1,6}\s+", "", value)
    return re.sub(r"\s+", " ", value).strip()


def _header_page_label(value: str, *, suffix: bool = False) -> str | None:
    plain = _unformatted_layout_text(value)
    if suffix:
        match = _PAGE_LABEL_SUFFIX_RE.search(plain)
        if not match or _STRUCTURAL_NUMBERED_IDENTIFIER_RE.fullmatch(plain):
            return None
        return match.group().strip()
    match = _PAGE_LABEL_PREFIX_CAPTURE_RE.match(plain)
    return match.group(1) if match else None


def _visible_text_with_offsets(value: str) -> tuple[str, list[int]]:
    """Return formatting-free text and raw end offsets for its characters."""
    visible: list[tuple[str, int]] = []
    index = 0
    while index < len(value):
        tag = _HTML_TAG_RE.match(value, index)
        if tag:
            index = tag.end()
            continue
        entity = _HTML_ENTITY_RE.match(value, index)
        if entity:
            decoded = html.unescape(entity.group())
            visible.extend((char, entity.end()) for char in decoded)
            index = entity.end()
            continue
        char = value[index]
        if char not in "`*_~":
            visible.append((char, index + 1))
        index += 1

    collapsed: list[str] = []
    offsets: list[int] = []
    for char, end in visible:
        folded = char.casefold()
        if char.isspace():
            if not collapsed:
                continue
            if collapsed[-1] == " ":
                offsets[-1] = end
            else:
                collapsed.append(" ")
                offsets.append(end)
            continue
        collapsed.extend(folded)
        offsets.extend([end] * len(folded))
    while collapsed and collapsed[-1] == " ":
        collapsed.pop()
        offsets.pop()
    return "".join(collapsed), offsets


def _plain_layout_text(value: str) -> str:
    value = html.unescape(re.sub(r"<[^>]+>", " ", str(value or "")))
    value = re.sub(r"[`*_~]", "", value)
    value = re.sub(r"^\s*#{1,6}\s+", "", value)
    value = re.sub(r"\s+", " ", value).strip()
    value = _PAGE_LABEL_PREFIX_RE.sub("", value, count=1)
    suffix = _PAGE_LABEL_SUFFIX_RE.search(value)
    if suffix and not _STRUCTURAL_NUMBERED_IDENTIFIER_RE.fullmatch(value):
        value = value[: suffix.start()].rstrip()
    return value.strip()


def _running_header_signature(value: str) -> str:
    plain = _plain_layout_text(value).casefold()
    return re.sub(r"[^\w\u3400-\u9fff]+", " ", plain, flags=re.UNICODE).strip()


def _remove_running_header_prefix(line: str, header_text: str) -> tuple[bool, str]:
    """Remove a sidecar-confirmed header prefix, preserving body markup/text."""
    plain_header = _plain_layout_text(header_text)
    if not plain_header:
        return False, line

    raw_line = str(line or "")
    raw_line = re.sub(r"^\s*#{1,6}\s+", "", raw_line, count=1)
    header_prefix_label = _header_page_label(header_text)
    line_prefix_match = _PAGE_LABEL_PREFIX_CAPTURE_RE.match(
        _unformatted_layout_text(raw_line)
    )
    if (
        header_prefix_label
        and line_prefix_match
        and line_prefix_match.group(1).casefold() == header_prefix_label.casefold()
    ):
        raw_prefix = _PAGE_LABEL_PREFIX_CAPTURE_RE.match(raw_line)
        if raw_prefix:
            raw_line = raw_line[raw_prefix.end():]
    line_folded, offsets = _visible_text_with_offsets(raw_line)
    header_folded = plain_header.casefold()
    if not line_folded.startswith(header_folded):
        return False, line
    if len(line_folded) > len(header_folded) and not line_folded[len(header_folded)].isspace():
        return False, line
    if not header_folded or len(offsets) < len(header_folded):
        return False, line
    raw_end = offsets[len(header_folded) - 1]
    remainder = raw_line[raw_end:]
    remainder = _INLINE_MARKUP_CLOSER_RE.sub("", remainder, count=1).lstrip()
    header_suffix_label = _header_page_label(header_text, suffix=True)
    suffix_match = _PAGE_LABEL_PREFIX_CAPTURE_RE.match(remainder)
    if (
        header_suffix_label
        and suffix_match
        and suffix_match.group(1).casefold() == header_suffix_label.casefold()
    ):
        remainder = remainder[suffix_match.end():].lstrip()
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
        self._toc_start_page_titles: dict[int, set[str]] = {}
        self._toc_nodes: list[TOCNode] = []

    def set_toc_nodes(self, nodes: Any) -> None:
        """Register TOC nodes so titles on their start pages are never stripped."""
        start_page_titles: dict[int, set[str]] = {}
        node_list = getattr(nodes, "chapters", None) if hasattr(nodes, "chapters") else nodes
        if not isinstance(node_list, (list, tuple)):
            node_list = [nodes] if nodes else []
        self._toc_nodes = list(_iter_nodes(node_list))
        for node in self._toc_nodes:
            start_page = getattr(node, "start_page", None)
            title = getattr(node, "title", None)
            if start_page is not None and title:
                try:
                    start_page_titles.setdefault(int(start_page), set()).add(str(title))
                except (TypeError, ValueError):
                    pass
        self._toc_start_page_titles = start_page_titles

    def _materialize_reviewed_headings(
        self, page_num: int, source_lines: list[str]
    ) -> tuple[list[str], dict[int, int], dict[int, int]]:
        """Render only validated TOC source regions as ATX headings.

        Returns transformed lines, original-line to output-line positions, and
        original exclusive-boundary to output offsets. Source files remain
        untouched; inline markup and footnote references are retained.
        """
        regions = []
        for node in self._toc_nodes:
            if getattr(node, "start_page", None) != page_num:
                continue
            if getattr(node, "binding_mode", "visible") != "visible":
                continue
            node_id = str(getattr(node, "node_id", "") or "")
            boundary = getattr(node, "boundary_info", None) or {}
            start_line = boundary.get("start_line") if isinstance(boundary, dict) else None
            if type(start_line) is not int or not 1 <= start_line <= len(source_lines) + 1:
                continue
            if node_id.startswith("layout-"):
                level = getattr(node, "level", 1)
                if type(level) is int and 1 <= level <= 6:
                    regions.append((start_line, start_line, level, str(node.title), True))
                continue
            if start_line > len(source_lines):
                continue
            end_exclusive = boundary.get("heading_end_line", start_line + 1)
            if type(end_exclusive) is not int or not start_line < end_exclusive <= len(source_lines) + 1:
                continue
            raw_region = source_lines[start_line - 1:end_exclusive - 1]
            if not raw_region:
                continue
            label_lines = list(raw_region)
            label_lines[0] = re.sub(r"^\s*#{1,6}\s*", "", label_lines[0])
            if normalize_toc_source_label(" ".join(label_lines)) != normalize_toc_source_label(node.title):
                continue
            level = getattr(node, "level", 1)
            if type(level) is not int or not 1 <= level <= 6:
                continue
            heading_text = " ".join(label_lines).strip()
            regions.append((start_line, end_exclusive, level, heading_text, False))

        by_start = {start: (end, level, title, layout_only) for start, end, level, title, layout_only in regions}
        output: list[str] = []
        line_positions: dict[int, int] = {}
        boundary_positions: dict[int, int] = {1: 0}
        line_no = 1
        while line_no <= len(source_lines) + 1:
            if line_no in by_start:
                end_exclusive, level, title, layout_only = by_start[line_no]
                if output and output[-1].strip():
                    output.append("")
                heading_position = len(output)
                output.append(f"{'#' * level} {title}")
                following_index = end_exclusive - 1 if not layout_only else line_no - 1
                if (following_index < len(source_lines)
                        and source_lines[following_index].strip()):
                    output.append("")
                boundary_positions[line_no] = heading_position
                if not layout_only:
                    for consumed in range(line_no, end_exclusive):
                        line_positions[consumed] = heading_position
                    line_no = end_exclusive
                    boundary_positions[line_no] = len(output)
                    continue
                line_positions[line_no] = heading_position
                if line_no > len(source_lines):
                    break
            if line_no not in by_start:
                boundary_positions[line_no] = len(output)
            line_positions.setdefault(line_no, len(output))
            if line_no > len(source_lines):
                break
            output.append(source_lines[line_no - 1])
            line_no += 1
        boundary_positions[len(source_lines) + 1] = len(output)
        return output, line_positions, boundary_positions

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

        by_page: dict[int, list[tuple[str, str, str]]] = {}
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

            body_blocks = [
                b
                for b in (sidecar.get("blocks", []) or [])
                if isinstance(b, dict)
                and str(b.get("label") or "").strip().casefold()
                in {"text", "paragraph", "list-group", "table"}
            ]
            min_body_y0 = min(
                (
                    normalized_bbox(b, sidecar)[1]
                    for b in body_blocks
                    if normalized_bbox(b, sidecar) is not None
                ),
                default=1.0,
            )

            for block in sidecar.get("blocks", []) or []:
                if not isinstance(block, dict):
                    continue
                label = str(block.get("label") or "").strip().casefold()
                if label not in {"section-header", "page-header"}:
                    continue
                bbox = normalized_bbox(block, sidecar)
                if bbox is None:
                    continue
                _x0, y0, _x1, y1 = bbox
                if y0 > 0.15 or y1 - y0 > 0.06 or y0 >= min_body_y0:
                    continue
                text = text_from_block(block)
                signature = _running_header_signature(text)
                if len(signature) < 5 or len(signature) > 120:
                    continue
                if _STRUCTURAL_NUMBERED_TITLE_RE.match(_plain_layout_text(text)):
                    continue
                by_page.setdefault(page_number, []).append((signature, text, label))
                pages_by_signature.setdefault(signature, set()).add(page_number)

        repeated = {
            signature
            for signature, page_numbers in pages_by_signature.items()
            if len(page_numbers) >= 2
        }
        indexed = {
            page_number: [
                text
                for signature, text, label in blocks
                if signature in repeated and label == "section-header"
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
        self,
        lines: list[str],
        pages_dir: Path,
        page_number: int,
        protected_titles: Optional[Iterable[str]] = None,
    ) -> list[str]:
        candidates = self._visual_running_headers(pages_dir).get(page_number, [])
        if not candidates:
            return lines
        protected = set(self._toc_start_page_titles.get(page_number, set()))
        if protected_titles:
            protected.update(str(t).strip() for t in protected_titles if str(t).strip())
        result = list(lines)
        for header_text in candidates:
            if _matches_protected_heading(header_text, protected):
                continue
            for index, line in enumerate(result):
                if _matches_protected_heading(line, protected):
                    continue
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
        line_positions: Optional[dict[int, int]] = None,
        fragment_start_offset: Optional[int] = None,
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
                if line_positions is not None and fragment_start_offset is not None:
                    offset = line_positions.get(start_line, fragment_start_offset) - fragment_start_offset
                else:
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
            source_lines = page_content.split('\n')

            # Boundary line numbers are absolute within the original page.
            # Compute both offsets before slicing so a node that starts and
            # ends on one page does not apply end_line to an already-trimmed
            # list (which used to retain/drop the wrong lines).
            start_index = 0
            end_index = len(source_lines)
            if page_num == node.start_page:
                start_line = boundary.get('start_line')
                if isinstance(start_line, int) and start_line > 1:
                    start_index = start_line - 1
                    logger.debug(f"Node '{node.title}' starts at line {start_line}")

            # Handle last page - end at end_line if set, or at next_node's start_line
            explicit_end_boundary = False
            if page_num == node.end_page:
                end_line = boundary.get('end_line')
                if isinstance(end_line, int):
                    # end_line is 1-indexed and exclusive.
                    end_index = min(end_index, end_line - 1)
                    explicit_end_boundary = True
                    logger.debug(f"Node '{node.title}' ends at line {end_line}")
                elif next_node and next_node.start_page == node.end_page:
                    # Next section starts on same page - cut before it
                    next_boundary = next_node.boundary_info or {}
                    next_start_line = next_boundary.get('start_line')
                    if isinstance(next_start_line, int):
                        end_index = min(end_index, next_start_line - 1)
                        explicit_end_boundary = True
                        logger.debug(f"Cutting before next section at line {next_start_line}")

            original_start_index = start_index
            original_end_index = end_index
            lines, line_positions, boundary_positions = self._materialize_reviewed_headings(
                page_num, source_lines
            )
            if page_num == node.start_page:
                start_index = line_positions.get(start_index + 1, boundary_positions.get(start_index + 1, start_index))
            else:
                start_index = boundary_positions.get(start_index + 1, start_index)
            if explicit_end_boundary:
                # Explicit source cuts, including a next-node start, stay
                # before a reviewed heading inserted at that position.
                end_index = line_positions.get(
                    end_index + 1,
                    boundary_positions.get(end_index + 1, end_index),
                )
            else:
                end_index = len(lines)
            lines = lines[start_index:end_index] if end_index >= start_index else []
            fragment_start_line = original_start_index + 1
            fragment_end_line = original_end_index

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
            node_start_titles = [
                getattr(n, "title", None)
                for n in _iter_nodes([node])
                if getattr(n, "start_page", None) == page_num and getattr(n, "title", None)
            ]
            lines = self._remove_visual_running_headers(
                lines, pages_dir, page_num, protected_titles=node_start_titles
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
                original_start_index + 1,
                original_end_index,
                line_positions=line_positions,
                fragment_start_offset=start_index,
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
            source_lines = page_content.split('\n')

            # Handle first page of first node
            start_index = 0
            end_index = len(source_lines)
            if page_num == start_page:
                start_line = first_boundary.get('start_line')
                if isinstance(start_line, int) and start_line > 1:
                    start_index = start_line - 1

            # Handle last page - apply the node boundary and then the next
            # unit boundary, both measured against the original page.
            explicit_end_boundary = False
            if page_num == end_page:
                end_line = first_boundary.get('end_line')
                if isinstance(end_line, int):
                    end_index = min(end_index, end_line - 1)
                    explicit_end_boundary = True
            if page_num == end_page and next_node and next_node.start_page == end_page:
                next_boundary = next_node.boundary_info or {}
                next_start_line = next_boundary.get('start_line')
                if isinstance(next_start_line, int):
                    end_index = min(end_index, next_start_line - 1)
                    explicit_end_boundary = True

            original_start_index = start_index
            original_end_index = end_index
            lines, line_positions, boundary_positions = self._materialize_reviewed_headings(
                page_num, source_lines
            )
            start_index = line_positions.get(
                start_index + 1, boundary_positions.get(start_index + 1, start_index)
            )
            end_index = (
                line_positions.get(end_index + 1, boundary_positions.get(end_index + 1, end_index))
                if explicit_end_boundary else len(lines)
            )
            lines = lines[start_index:end_index] if end_index >= start_index else []
            fragment_start_line = original_start_index + 1
            fragment_end_line = original_end_index

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
            node_start_titles = [
                getattr(n, "title", None)
                for n in _iter_nodes(nodes)
                if getattr(n, "start_page", None) == page_num and getattr(n, "title", None)
            ]
            lines = self._remove_visual_running_headers(
                lines, pages_dir, page_num, protected_titles=node_start_titles
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
                line_positions=line_positions,
                fragment_start_offset=start_index,
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
    if re.search(r"(?:^|\n)\s*---\s*(?:\n|$)", previous):
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

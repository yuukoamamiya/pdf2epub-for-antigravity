"""Deterministic cleanup for common OCR-only page artifacts."""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping
from ..numbering import standalone_number_label


_MARKDOWN_IMAGE_RE = re.compile(r"!\[([^\]]*)\]\(([^)]+)\)")
_PRINTED_PAGE_LABEL_RE = re.compile(
    r"^(?:page\s*[:#-]?\s*)?(\d{1,4}|[ivxlcdm]{1,12})$",
    re.IGNORECASE,
)
_SYNTHETIC_PAGE_STAMP_RE = re.compile(
    r"^pdf\s+page\s*[:#-]?\s*\d{1,4}$", re.IGNORECASE
)
_BLANK_PAGE_TEXT_RE = re.compile(
    r"(?:\bblank\s+(?:white\s+)?page\b|"
    r"\bscan\s+of\s+a\s+blank\s+page\b|"
    r"\bno\s+text\b.{0,100}\b(?:lines|graphical\s+elements)\b|"
    r"\bno\s+visible\s+(?:content|text|markings?)\b)",
    re.IGNORECASE,
)
_HEADING_RE = re.compile(r"^(#{2,3})\s+(.+?)\s*$")
_NATIVE_DOI_RE = re.compile(r"^\s*DOI\s*:?\s*10\.\d{4,9}/\S+\s*$", re.IGNORECASE)
_NATIVE_EDGE_RATIO = 0.05


def _native_furniture_signature(value: Any) -> str:
    """Normalize a native sidecar block for repeated-edge comparison."""
    value = re.sub(r"<[^>]+>", " ", str(value or ""))
    return "".join(char.casefold() for char in value if char.isalnum())


def _native_edge_block(
    block: Mapping[str, Any], sidecar: Mapping[str, Any], *, edge_ratio: float
) -> tuple[bool, bool]:
    """Return whether a native block touches the physical top or bottom edge."""
    bbox = block.get("bbox")
    page_box = sidecar.get("page_box")
    if not isinstance(bbox, (list, tuple)) or len(bbox) != 4:
        return False, False
    if not isinstance(page_box, (list, tuple)) or len(page_box) != 4:
        return False, False
    try:
        _x0, y0, _x1, y1 = (float(value) for value in bbox)
        _px0, py0, _px1, py1 = (float(value) for value in page_box)
    except (TypeError, ValueError):
        return False, False
    height = py1 - py0
    if height <= 0 or y1 <= y0:
        return False, False
    return (
        (y0 - py0) / height <= edge_ratio,
        (py1 - y1) / height <= edge_ratio,
    )


def native_repeated_edge_furniture_index(
    sidecars: Iterable[Mapping[str, Any]], *, edge_ratio: float = _NATIVE_EDGE_RATIO
) -> dict[str, int]:
    """Index repeated native edge blocks by signature and first page number."""
    pages_by_signature: dict[str, set[int]] = {}
    for sidecar in sidecars:
        if str(sidecar.get("source_kind") or "").strip().lower() != "native_text":
            continue
        try:
            page_number = int(sidecar.get("page_number"))
        except (TypeError, ValueError):
            continue
        for block in sidecar.get("blocks", []) or []:
            if not isinstance(block, Mapping):
                continue
            top, bottom = _native_edge_block(block, sidecar, edge_ratio=edge_ratio)
            if not (top or bottom):
                continue
            text = str(block.get("markdown_text") or block.get("text") or "").strip()
            signature = _native_furniture_signature(text)
            if not signature or len(signature) > 160:
                continue
            pages_by_signature.setdefault(signature, set()).add(page_number)
    return {
        signature: min(pages)
        for signature, pages in pages_by_signature.items()
        if len(pages) >= 2
    }


def _native_line_span(lines: list[str], value: str) -> set[int]:
    """Find the physical lines containing one sidecar block or line."""
    needle = _native_furniture_signature(value)
    if not needle:
        return set()
    page_signature: list[str] = []
    line_for_character: list[int] = []
    for line_number, line in enumerate(lines):
        signature = _native_furniture_signature(line)
        page_signature.extend(signature)
        line_for_character.extend([line_number] * len(signature))
    joined = "".join(page_signature)
    start = joined.find(needle)
    if start < 0:
        return set()
    end = start + len(needle)
    return set(line_for_character[start:end])


def remove_native_page_furniture(
    content: str,
    sidecar: Mapping[str, Any],
    *,
    repeated_index: Mapping[str, int] | None = None,
    page_number: int | None = None,
    protected_titles: Iterable[str] = (),
    edge_ratio: float = _NATIVE_EDGE_RATIO,
) -> tuple[str, list[dict[str, Any]]]:
    """Remove deterministic native edge furniture from one page fragment.

    The original page Markdown remains immutable. Repeated text is retained
    on its first physical page so a legitimate section opening is preserved.
    """
    if str(sidecar.get("source_kind") or "").strip().lower() != "native_text":
        return str(content), []
    lines = str(content).split("\n")
    repeated_index = repeated_index or {}
    protected_title_values = tuple(protected_titles)
    remove_lines: dict[int, dict[str, Any]] = {}
    for block_index, block in enumerate(sidecar.get("blocks", []) or []):
        if not isinstance(block, Mapping):
            continue
        top, bottom = _native_edge_block(block, sidecar, edge_ratio=edge_ratio)
        if not (top or bottom):
            continue
        block_text = str(block.get("markdown_text") or block.get("text") or "").strip()
        if not block_text:
            continue
        normalized_block = re.sub(r"<[^>]+>", " ", block_text)
        normalized_block = re.sub(r"^\s*#{1,6}\s*", "", normalized_block)
        normalized_block = re.sub(r"\s+", " ", normalized_block).strip().casefold()
        protected = {
            re.sub(r"\s+", " ", re.sub(r"^\s*#{1,6}\s*", "", str(title or ""))).strip().casefold()
            for title in protected_title_values
        }
        if normalized_block in protected:
            continue
        if standalone_number_label(re.sub(r"<[^>]+>", "", block_text).strip()):
            # Numeral blocks are ambiguous; only an exact structure-review
            # line disposition may remove them.
            continue
        signature = _native_furniture_signature(block_text)
        candidates: list[tuple[str, str]] = []
        candidate_lines = [block_text, *block_text.splitlines()]
        for candidate_line in candidate_lines:
            if _NATIVE_DOI_RE.fullmatch(candidate_line):
                candidates.append(("doi_edge", candidate_line))
            elif _PRINTED_PAGE_LABEL_RE.fullmatch(
                re.sub(r"<[^>]+>", "", candidate_line).strip()
            ):
                candidates.append(("edge_page_label", candidate_line))
        if not candidates and (
            signature in repeated_index
            and page_number is not None
            and int(page_number) != int(repeated_index[signature])
        ):
            candidates.append(("repeated_edge_text", block_text))
        for kind, candidate_text in candidates:
            span = _native_line_span(lines, candidate_text)
            if span and protected_title_values:
                # A native sidecar may describe only one component of a
                # reviewed multi-line heading. After materialization that
                # component can match a substring of the combined heading;
                # protect the actual ATX line by its full rendered title.
                from ..toc_model import normalize_toc_source_label, toc_title_match_key

                protected_labels = {
                    toc_title_match_key(
                        normalize_toc_source_label(str(title), markdown_source=False)
                    )
                    for title in protected_title_values
                }
                if any(
                    line_number in span
                    and re.match(r"^\s{0,3}#{1,6}\s+", lines[line_number])
                    and toc_title_match_key(
                        normalize_toc_source_label(lines[line_number], markdown_source=True)
                    ) in protected_labels
                    for line_number in range(len(lines))
                ):
                    continue
            for line_number in span:
                remove_lines.setdefault(
                    line_number,
                    {"block": block_index, "kind": kind, "text": candidate_text},
                )
    if not remove_lines:
        return str(content), []
    cleaned = "\n".join(
        line for index, line in enumerate(lines) if index not in remove_lines
    )
    return cleaned, [remove_lines[index] for index in sorted(remove_lines)]


def is_blank_page_marker(line: str) -> bool:
    """Return whether a Markdown image line describes an OCR blank page."""
    image = _MARKDOWN_IMAGE_RE.search(line)
    if not image:
        return False
    return bool(
        _BLANK_PAGE_TEXT_RE.search(image.group(1))
        or _BLANK_PAGE_TEXT_RE.search(line)
    )


def _is_blank_page_description(line: str) -> bool:
    return bool(line.strip()) and bool(_BLANK_PAGE_TEXT_RE.search(line))


def clean_ocr_page_artifacts(content: str) -> str:
    """Remove known blank-page image placeholders from one page fragment.

    The original page Markdown is intentionally left untouched. This helper
    is applied only to content after page-boundary slicing, so line numbers
    supplied by the structure agent remain valid.
    """
    lines = content.split("\n")
    cleaned: list[str] = []
    skip_description = False
    for line in lines:
        if skip_description:
            if not line.strip():
                cleaned.append(line)
                continue
            if _is_blank_page_description(line):
                skip_description = False
                continue
            skip_description = False

        if is_blank_page_marker(line):
            remainder = _MARKDOWN_IMAGE_RE.sub("", line).strip()
            # A Chandra blank-page response can put the description on the
            # following line. Remove that line too, but preserve unrelated
            # text if the image was embedded in a larger line.
            if not remainder:
                skip_description = True
                continue
            line = remainder
            if _is_blank_page_description(line):
                continue
        cleaned.append(line)
    return "\n".join(cleaned)


def remove_printed_page_number_lines(
    lines: list[str], *, reviewed_page_lines: set[int] | frozenset[int] = frozenset()
) -> list[str]:
    """Remove only page labels explicitly reviewed at 1-based source lines.

    A standalone number is also a valid section heading or body content. Its
    position near a page edge is not sufficient evidence to delete it.
    ``reviewed_page_lines`` is projected from the current structure review and
    is intentionally expressed in the original page coordinate system.
    """
    def is_page_label(line: str) -> bool:
        stripped = line.strip().strip("|•·—–-").strip()
        if not stripped or stripped.startswith(("#", "!", "[")):
            return False
        return bool(
            _PRINTED_PAGE_LABEL_RE.fullmatch(stripped)
            or _SYNTHETIC_PAGE_STAMP_RE.fullmatch(stripped)
        )

    return [line for index, line in enumerate(lines, 1)
            if index not in reviewed_page_lines or not is_page_label(line)]


def heading_signature(line: str) -> tuple[int, str] | None:
    """Return a normalized H2/H3 signature for repeated running headers."""
    match = _HEADING_RE.match(line.strip())
    if not match:
        return None
    text = re.sub(r"\s+", " ", match.group(2)).strip().casefold()
    return len(match.group(1)), text


def leading_h2_h3_heading(
    lines: list[str], max_lines: int = 6
) -> tuple[int, tuple[int, str]] | None:
    """Find an H2/H3 heading near the physical top of a page."""
    for index, line in enumerate(lines[:max_lines]):
        if not line.strip():
            continue
        signature = heading_signature(line)
        if signature is not None:
            return index, signature
        # A non-heading before the first heading means this is not a running
        # header at the top of the page.
        return None
    return None


def remove_repeated_page_header(
    lines: list[str],
    previous_header: tuple[int, str] | None,
    *,
    protected_titles: Iterable[str] = (),
) -> tuple[list[str], tuple[int, str] | None]:
    """Remove a repeated H2/H3 heading at the top of an adjacent page."""
    current = leading_h2_h3_heading(lines)
    if current is not None and previous_header is not None and current[1] == previous_header:
        index, _signature = current
        protected = {
            re.sub(r"\s+", " ", re.sub(r"[`*_~]", "", re.sub(r"\[\^[^\]]+\]", "", re.sub(r"^\s*#{1,6}\s+", "", str(title or ""))))).strip().casefold()
            for title in protected_titles
        }
        current_text = re.sub(r"[`*_~]", "", re.sub(r"\[\^[^\]]+\]", "", current[1][1])).strip().casefold()
        if current_text in protected:
            return lines, current[1]
        del lines[index]
        # Keep the removed header as the page signature. A running header can
        # occur on three or more consecutive pages.
        return lines, current[1]
    return lines, current[1] if current is not None else None

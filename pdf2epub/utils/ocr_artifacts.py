"""Deterministic cleanup for common OCR-only page artifacts."""

from __future__ import annotations

import re


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
    lines: list[str], *, edge_window: int = 4
) -> list[str]:
    """Remove unambiguous page labels at the physical page edges.

    Page-level OCR and native PDF extraction can expose a printed Arabic or
    Roman page label as a standalone Markdown line.  The same is true for the
    synthetic ``PDF Page: N`` labels added to scanned-PDF preprocessing.  Only
    short labels in the first or last few non-empty lines are removed; numbers
    inside prose, citations, lists, footnotes, bibliography, and index entries
    are left untouched.

    This deliberately does not remove a running title such as ``Preface XII``.
    That requires layout judgment and remains part of the Subagent polish gate.
    """
    if not lines or edge_window <= 0:
        return list(lines)

    non_empty = [index for index, line in enumerate(lines) if line.strip()]
    if not non_empty:
        return list(lines)
    edge_indexes = set(non_empty[:edge_window] + non_empty[-edge_window:])

    def is_page_label(line: str) -> bool:
        stripped = line.strip().strip("|•·—–-").strip()
        if not stripped or stripped.startswith(("#", "!", "[")):
            return False
        return bool(
            _PRINTED_PAGE_LABEL_RE.fullmatch(stripped)
            or _SYNTHETIC_PAGE_STAMP_RE.fullmatch(stripped)
        )

    return [
        line
        for index, line in enumerate(lines)
        if index not in edge_indexes or not is_page_label(line)
    ]


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
) -> tuple[list[str], tuple[int, str] | None]:
    """Remove a repeated H2/H3 heading at the top of an adjacent page."""
    current = leading_h2_h3_heading(lines)
    if current is not None and previous_header is not None and current[1] == previous_header:
        index, _signature = current
        del lines[index]
        # Keep the removed header as the page signature. A running header can
        # occur on three or more consecutive pages.
        return lines, current[1]
    return lines, current[1] if current is not None else None

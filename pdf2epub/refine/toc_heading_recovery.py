"""Recover TOC headings that visual OCR discarded as page furniture.

Chandra can label a real chapter/section title as ``Page-Header`` or
``Page-Footer``.  When ``include_headers_footers`` is disabled those blocks are
removed from ``page_NNN.md``, but the raw HTML/sidecar still contains them.
This module reconstructs only headings that exactly correspond to a TOC title;
everything ambiguous is reported as skipped instead of guessed.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Tuple

from bs4 import BeautifulSoup

from ..utils.fingerprints import evidence_pages_fingerprint, pages_fingerprint
from .toc_tree import TOCNode


SCHEMA_VERSION = 1
FURNITURE_LABELS = frozenset({"Page-Header", "Page-Footer", "Section-Header"})

_CHAPTER_PREFIX_RE = re.compile(
    r"^\s*(?:chapter|kapitel|chapitre)\s+[0-9ivxlcdm]+[:.\-\s]*",
    re.IGNORECASE,
)
_LEADING_NUMBER_RE = re.compile(r"^\s*\d+\s*[.):]?\s*")


def _alnum(value: str) -> str:
    return "".join(character for character in value.casefold() if character.isalnum())


def _title_variants(value: str) -> set[str]:
    """Return exact normalized forms accepted for a heading.

    Comparison is always exact after normalization; this never performs fuzzy
    or edit-distance matching.
    """
    text = str(value or "")
    variants = {_alnum(text)}
    without_chapter = _CHAPTER_PREFIX_RE.sub("", text)
    variants.add(_alnum(without_chapter))
    without_number = _LEADING_NUMBER_RE.sub("", text)
    variants.add(_alnum(without_number))
    variants.add(_alnum(_LEADING_NUMBER_RE.sub("", without_chapter)))
    variants.discard("")
    return variants


def _titles_match(left: str, right: str) -> bool:
    return bool(_title_variants(left) & _title_variants(right))


def title_present_in_lines(lines: Iterable[str], title: str) -> bool:
    title_variants = _title_variants(title)
    if not title_variants:
        return False
    for line in lines:
        candidate = re.sub(r"^\s*#{1,6}\s*", "", str(line or ""))
        if _title_variants(candidate) & title_variants:
            return True
    return False


def _iter_nodes(nodes: Iterable[TOCNode]) -> Iterable[Tuple[int, TOCNode]]:
    for node in nodes:
        yield from _iter_node(node)


def _iter_node(node: TOCNode) -> Iterable[Tuple[int, TOCNode]]:
    yield node
    for child in getattr(node, "children", []) or []:
        yield from _iter_node(child)


def _load_sidecar(path: Path) -> Tuple[Optional[dict], Optional[str]]:
    if not path.is_file():
        return None, None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None, "invalid_sidecar"
    if not isinstance(value, dict):
        return None, "invalid_sidecar"
    return value, None


def _header_blocks(html_text: str) -> Tuple[list[dict], str]:
    """Return top-level furniture blocks in DOM order and their joined text."""
    soup = BeautifulSoup(html_text, "html.parser")
    top_level = [
        div
        for div in soup.find_all("div", attrs={"data-label": True})
        if div.find_parent("div", attrs={"data-label": True}) is None
    ]
    blocks: list[dict] = []
    for index, div in enumerate(top_level):
        label = str(div.get("data-label") or "")
        if label not in FURNITURE_LABELS:
            continue
        text = re.sub(r"\s+", " ", div.get_text(" ", strip=True)).strip()
        if not text:
            continue
        blocks.append({"index": index, "label": label, "text": text})
    combined = " ".join(block["text"] for block in blocks).strip()
    return blocks, combined


def _load_page_evidence(
    evidence_dir: Path,
    page: int,
    sidecar: Optional[dict],
) -> Tuple[Optional[list[dict]], str, Optional[str]]:
    html_path = evidence_dir / f"page_{page:03d}.html"
    html_text = ""
    if html_path.is_file():
        try:
            html_text = html_path.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            html_text = ""
    if not html_text and isinstance(sidecar, dict):
        raw_html = sidecar.get("raw_html")
        if isinstance(raw_html, str):
            html_text = raw_html
    if not html_text:
        return None, "", None
    blocks, combined = _header_blocks(html_text)
    return blocks, combined, html_path.name


def _matches_any_header(header_text: str, title: str) -> bool:
    return _titles_match(header_text, title)


def collect_toc_heading_recoveries(
    nodes: Iterable[TOCNode],
    *,
    page_markdown_dir: Path,
    evidence_dir: Path,
) -> Tuple[Dict[int, List[dict]], dict]:
    """Collect deterministic TOC heading recoveries from page evidence.

    ``page_markdown_dir`` is the refinement source used for ``page_NNN.md`` and
    boundary line numbers.  ``evidence_dir`` is the visual OCR artifact
    directory (normally ``output/<title>/pages``).  They may be identical.
    """
    page_markdown_dir = Path(page_markdown_dir)
    evidence_dir = Path(evidence_dir)

    skipped: list[dict] = []
    candidates: list[dict] = []
    seen_titles: set[tuple[int, str]] = set()
    considered_pages: set[int] = set()

    ordered_nodes = list(_iter_nodes(nodes))
    for toc_index, node in enumerate(ordered_nodes):
        start_page = getattr(node, "start_page", None)
        if not isinstance(start_page, int):
            continue
        title = str(getattr(node, "title", "") or "").strip()
        if not title:
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "empty_title",
                }
            )
            continue

        considered_pages.add(start_page)
        page_markdown_path = page_markdown_dir / f"page_{start_page:03d}.md"
        if not page_markdown_path.is_file():
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "page_markdown_missing",
                }
            )
            continue
        try:
            page_lines = page_markdown_path.read_text(encoding="utf-8").split("\n")
        except (OSError, UnicodeError):
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "page_markdown_unreadable",
                }
            )
            continue

        if title_present_in_lines(page_lines, title):
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "title_already_present",
                }
            )
            continue

        sidecar, sidecar_error = _load_sidecar(
            evidence_dir / f"page_{start_page:03d}.ocr.json"
        )
        if sidecar_error:
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": sidecar_error,
                }
            )
            continue
        if isinstance(sidecar, dict) and sidecar.get("source_kind") == "native_text":
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "native_sidecar",
                }
            )
            continue

        blocks, header_text, evidence_file = _load_page_evidence(
            evidence_dir,
            start_page,
            sidecar,
        )
        if not evidence_file or blocks is None:
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "no_page_evidence",
                }
            )
            continue
        if not blocks or not header_text:
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "no_header_blocks",
                }
            )
            continue
        if not _matches_any_header(header_text, title):
            skipped.append(
                {
                    "page": start_page,
                    "title": title,
                    "level": getattr(node, "level", None),
                    "reason": "no_heading_match",
                }
            )
            continue

        boundary = getattr(node, "boundary_info", None) or {}
        start_line = boundary.get("start_line") if isinstance(boundary, dict) else None
        if not isinstance(start_line, int):
            start_line = None
        candidates.append(
            {
                "page": start_page,
                "title": title,
                "level": getattr(node, "level", 1),
                "block_text": header_text,
                "evidence": {
                    "file": evidence_file,
                    "blocks": [
                        {"index": block["index"], "label": block["label"]}
                        for block in blocks
                    ],
                },
                "start_line": start_line,
                "toc_index": toc_index,
                "variants": _title_variants(title),
            }
        )

    by_page: dict[int, list[dict]] = {}
    for candidate in candidates:
        by_page.setdefault(candidate["page"], []).append(candidate)

    recoveries: dict[int, list[dict]] = {}
    for page, page_candidates in by_page.items():
        conflicting: set[int] = set()
        for left_index, left in enumerate(page_candidates):
            for right_index in range(left_index + 1, len(page_candidates)):
                right = page_candidates[right_index]
                if _alnum(left["title"]) == _alnum(right["title"]):
                    continue
                if left["variants"] & right["variants"]:
                    conflicting.add(left_index)
                    conflicting.add(right_index)

        ordered = sorted(
            enumerate(page_candidates),
            key=lambda entry: (
                entry[1]["toc_index"],
                entry[1]["start_line"] if entry[1]["start_line"] is not None else 10**9,
            ),
        )
        page_recoveries: list[dict] = []
        for index, candidate in ordered:
            if index in conflicting:
                skipped.append(
                    {
                        "page": page,
                        "title": candidate["title"],
                        "level": candidate["level"],
                        "reason": "ambiguity",
                    }
                )
                continue
            key = (page, _alnum(candidate["title"]))
            if key in seen_titles:
                skipped.append(
                    {
                        "page": page,
                        "title": candidate["title"],
                        "level": candidate["level"],
                        "reason": "duplicate_recovery",
                    }
                )
                continue
            seen_titles.add(key)
            page_recoveries.append(
                {
                    "page": page,
                    "title": candidate["title"],
                    "level": candidate["level"],
                    "block_text": candidate["block_text"],
                    "evidence": candidate["evidence"],
                    "start_line": candidate["start_line"],
                }
            )
        if page_recoveries:
            recoveries[page] = page_recoveries

    recovered_records = [
        record for page in sorted(recoveries) for record in recoveries[page]
    ]
    report = {
        "schema_version": SCHEMA_VERSION,
        "status": "validated" if recovered_records else "no_recovery",
        "recovered": recovered_records,
        "skipped": skipped,
        "pages_considered": sorted(considered_pages),
        "page_markdown_fingerprint": pages_fingerprint(page_markdown_dir),
        "evidence_pages_fingerprint": evidence_pages_fingerprint(evidence_dir),
    }
    return recoveries, report


def toc_heading_recovery_fingerprint(recoveries: Dict[int, List[dict]]) -> str:
    """Return a stable hash of the recovery mapping for resume checkpoints."""
    payload = {
        str(page): [
            {
                "title": item.get("title"),
                "level": item.get("level"),
                "start_line": item.get("start_line"),
                "block_text": item.get("block_text"),
            }
            for item in items
        ]
        for page, items in sorted(recoveries.items())
    }
    encoded = json.dumps(
        payload,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


__all__ = [
    "collect_toc_heading_recoveries",
    "title_present_in_lines",
    "toc_heading_recovery_fingerprint",
]

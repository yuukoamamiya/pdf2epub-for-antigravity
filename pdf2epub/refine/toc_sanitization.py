"""Conservative, auditable cleanup for OCR titles copied from a PDF TOC."""

from __future__ import annotations

import copy
import difflib
import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Tuple


_TRAILING_ARABIC_PAGE = re.compile(
    r"^(?P<title>.+?)[ \t]+(?P<page>\d{1,4})\s*$"
)
_MIN_SIMILARITY_LENGTH = 8
_HEADING_SIMILARITY = 0.96


def _normalize_text(value: str) -> str:
    """Normalize a title enough for exact/near-exact evidence matching."""
    value = re.sub(r"^\s*#{1,6}\s*", "", str(value or ""))
    value = re.sub(r"\s+", "", value)
    return re.sub(r"[^\w\u3400-\u9fff]", "", value, flags=re.UNICODE).casefold()


def _iter_nodes(entries: Iterable[Mapping[str, Any]], path: str = "chapters"):
    """Yield every TOC node with a stable JSON-like path."""
    for index, entry in enumerate(entries):
        if not isinstance(entry, Mapping):
            continue
        node_path = f"{path}[{index}]"
        yield node_path, entry
        children = entry.get("children")
        if isinstance(children, list):
            yield from _iter_nodes(children, f"{node_path}.children")


def _load_pagination_map(path: Path) -> Dict[int, list[dict[str, Any]]]:
    """Load page-label evidence keyed by the physical OCR page number."""
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    pages = data.get("pages") if isinstance(data, Mapping) else None
    if not isinstance(pages, list):
        return {}

    result: Dict[int, list[dict[str, Any]]] = {}
    for item in pages:
        if not isinstance(item, Mapping):
            continue
        try:
            physical_page = int(item["physical_page"])
        except (KeyError, TypeError, ValueError):
            continue
        candidates = item.get("candidates")
        if not isinstance(candidates, list):
            continue
        result[physical_page] = [
            dict(candidate)
            for candidate in candidates
            if isinstance(candidate, Mapping)
        ]
    return result


def _source_heading_evidence(
    title: str,
    page_text: str,
) -> Dict[str, Any] | None:
    """Find an exact or very close title occurrence on the unit's first page."""
    target = _normalize_text(title)
    if not target:
        return None

    best: Dict[str, Any] | None = None
    for line_number, raw_line in enumerate(page_text.splitlines(), 1):
        line = raw_line.strip()
        if not line:
            continue
        normalized = _normalize_text(line)
        if not normalized:
            continue
        if normalized == target:
            return {
                "line": line_number,
                "text": raw_line,
                "match": "exact",
                "similarity": 1.0,
            }

        if len(target) < _MIN_SIMILARITY_LENGTH or len(normalized) < _MIN_SIMILARITY_LENGTH:
            continue
        similarity = difflib.SequenceMatcher(None, target, normalized).ratio()
        if similarity >= _HEADING_SIMILARITY and (
            best is None or similarity > best["similarity"]
        ):
            best = {
                "line": line_number,
                "text": raw_line,
                "match": "high_similarity",
                "similarity": round(similarity, 6),
            }
    return best


def _canonical_hash(value: Mapping[str, Any]) -> str:
    payload = json.dumps(
        value,
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def sanitize_toc_titles(
    toc_data: Mapping[str, Any],
    pages_dir: Path,
    pagination_map_path: Path,
) -> Tuple[Dict[str, Any], Dict[str, Any]]:
    """Remove only strongly evidenced trailing printed page labels.

    A title is changed only when its trailing Arabic number is both present as
    a standalone printed-page candidate on the node's physical start page and
    the same page contains the title without that number.  Everything else is
    retained and reported as an unresolved candidate for audit.
    """
    normalized = copy.deepcopy(dict(toc_data))
    chapters = normalized.get("chapters")
    pagination = _load_pagination_map(pagination_map_path)
    candidates: list[dict[str, Any]] = []
    changes: list[dict[str, Any]] = []

    if not isinstance(chapters, list):
        report = {
            "schema_version": 1,
            "status": "no_changes",
            "changed_count": 0,
            "candidate_count": 0,
            "changes": [],
            "candidates": [],
            "pagination_map_available": bool(pagination),
        }
        return normalized, report

    for node_path, node in _iter_nodes(chapters):
        title = node.get("title")
        if not isinstance(title, str):
            continue
        match = _TRAILING_ARABIC_PAGE.fullmatch(title.strip())
        if not match:
            continue

        base_title = match.group("title").rstrip()
        page_label = int(match.group("page"))
        try:
            start_page = int(node.get("start_page"))
        except (TypeError, ValueError):
            start_page = None

        record: dict[str, Any] = {
            "path": node_path,
            "title": title,
            "base_title": base_title,
            "page_label": page_label,
            "start_page": start_page,
            "action": "kept",
            "reason": "insufficient_evidence",
        }

        page_candidates = pagination.get(start_page, []) if start_page is not None else []
        matching_page_candidates = [
            candidate
            for candidate in page_candidates
            if str(candidate.get("kind", "")).lower() == "arabic"
            and candidate.get("value") == page_label
        ]
        if matching_page_candidates:
            record["pagination_evidence"] = {
                "physical_page": start_page,
                "candidates": matching_page_candidates,
            }
        else:
            record["reason"] = "start_page_has_no_matching_printed_label"

        heading_evidence = None
        if matching_page_candidates and start_page is not None:
            source_page = Path(pages_dir) / f"page_{start_page:03d}.md"
            if source_page.is_file():
                heading_evidence = _source_heading_evidence(
                    base_title,
                    source_page.read_text(encoding="utf-8", errors="replace"),
                )
        if heading_evidence:
            record["source_heading_evidence"] = heading_evidence
        elif matching_page_candidates:
            record["reason"] = "source_heading_not_found"

        if matching_page_candidates and heading_evidence:
            node["title"] = base_title
            record["action"] = "removed"
            record["reason"] = "printed_label_and_source_heading_match"
            changes.append(
                {
                    "path": node_path,
                    "old_title": title,
                    "new_title": base_title,
                    "start_page": start_page,
                    "page_label": page_label,
                    "evidence": {
                        "pagination": record["pagination_evidence"],
                        "source_heading": heading_evidence,
                    },
                }
            )
        candidates.append(record)

    report = {
        "schema_version": 1,
        "status": "applied" if changes else "no_changes",
        "changed_count": len(changes),
        "candidate_count": len(candidates),
        "pagination_map_available": bool(pagination),
        "changes": changes,
        "candidates": candidates,
        "toc_semantic_sha256": _canonical_hash(normalized),
    }
    return normalized, report

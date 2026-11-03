"""Build adjacent-page footnote review windows."""

from __future__ import annotations

import re
from typing import Any

from .footnote_signals import _FOOTNOTE_LABEL_RE


def build_cross_page_windows(
    page_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Pair adjacent pages without assuming where the continuation appears."""
    windows = []
    by_page = {int(item["page"]): item for item in page_reports}
    for report in page_reports:
        page_number = int(report["page"])
        if not report["candidates"]:
            continue
        following = by_page.get(page_number + 1)
        if not following or not following["candidates"]:
            continue
        if not any(
            candidate.get("confidence") == "review"
            for candidate in report["candidates"] + following["candidates"]
        ):
            continue
        windows.append(
            {
                "pages": [page_number, page_number + 1],
                "left_candidates": report["candidates"],
                "right_candidates": following["candidates"],
                "reason": "adjacent_pages_contain_ordered_footnote_candidates",
            }
        )
    return windows


def build_missing_continuation_reviews(
    page_reports: list[dict[str, Any]], *, page_count: int
) -> list[dict[str, Any]]:
    """Flag the final labelled note candidate when it ends in an open word and
    no traceable continuation candidate covers the following physical page.

    Only the last physical candidate on a page is eligible.  This deliberately
    avoids general prose/math hyphen heuristics. The final PDF page still gets
    an exception-only review when its last note ends in an open word.
    """
    by_page = {int(item["page"]): item for item in page_reports}
    result: list[dict[str, Any]] = []
    for page, report in sorted(by_page.items()):
        candidates = [
            item for item in report.get("candidates", []) or []
            if isinstance(item, dict)
            and _FOOTNOTE_LABEL_RE.search(str(item.get("label") or ""))
        ]
        if not candidates:
            continue
        candidates.sort(key=lambda item: (float(item.get("order", item.get("block", -1))), int(item.get("block", -1))))
        candidate = candidates[-1]
        if candidate.get("confidence") not in {"high", "review"}:
            continue
        text = str(candidate.get("text_ending") or candidate.get("text") or "").rstrip()
        if not re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", text):
            continue
        terminal_page = page >= page_count
        following = by_page.get(page + 1)
        if not terminal_page and following:
            key = str(candidate.get("key") or "")
            matched_continuation = any(
                isinstance(item, dict)
                and str(item.get("local_role") or "") == "footnote_continuation"
                and (
                    str(item.get("key") or "") == key
                    or str((item.get("continuation_evidence") or {}).get("matched_previous_key") or "") == key
                )
                for item in following.get("candidates", []) or []
            )
            if matched_continuation:
                continue
        result.append({
            "task_id": f"continuation-gap-{page:03d}-{int(candidate.get('block', -1)):03d}",
            "kind": "missing_page_footnote_continuation",
            "source_note": {
                "page": page,
                "source": str(candidate.get("source") or "primary"),
                "block": int(candidate.get("block", -1)),
                "key": str(candidate.get("key") or ""),
                "text_ending": text[-100:],
            },
            "pages": [page] if terminal_page else [page, page + 1],
            "expected_next_page": None if terminal_page else page + 1,
            "terminal_page": terminal_page,
            "reason": "last_physical_footnote_ends_with_dangling_word_hyphen_without_a_traceable_continuation_candidate",
            "status": "pending_review",
        })
    return result


_build_cross_page_windows = build_cross_page_windows


__all__ = ["build_cross_page_windows", "build_missing_continuation_reviews"]

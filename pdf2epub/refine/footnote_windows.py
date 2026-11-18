"""Build adjacent-page footnote review windows."""

from __future__ import annotations

import re
from typing import Any, Mapping

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


def build_linked_continuation_windows(
    page_reports: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Keep every page in an explicitly linked, multi-page note chain.

    A link must come from the continuation classifier/evidence; matching
    punctuation or a repeated key alone never extends a chain. There is no
    fixed page cap, so long open chains remain visible to the reviewer.
    """
    by_page = {int(row["page"]): row for row in page_reports}
    chains: list[dict[str, Any]] = []
    for page, report in sorted(by_page.items()):
        starts = [
            row for row in report.get("candidates", []) or []
            if isinstance(row, dict)
            and row.get("key")
            and row.get("local_role") == "footnote_start"
        ]
        for start in starts:
            key = str(start["key"])
            linked = [start]
            current_page = page + 1
            while current_page in by_page:
                continuations = [
                    row for row in by_page[current_page].get("candidates", []) or []
                    if isinstance(row, dict)
                    and row.get("local_role") == "footnote_continuation"
                    and (
                        str(row.get("key") or "") == key
                        or str((row.get("continuation_evidence") or {}).get("matched_previous_key") or "") == key
                    )
                ]
                if not continuations:
                    break
                linked.extend(continuations)
                current_page += 1
            if current_page - page < 3:
                continue
            # Preserve any separately labeled note that shares a page with
            # the continuation chain; it is essential context when notes are
            # interleaved across pages.
            for chain_page in range(page, current_page):
                for item in by_page[chain_page].get("candidates", []) or []:
                    if (
                        isinstance(item, dict)
                        and item.get("local_role") in {"footnote_start", "footnote_continuation", "footnote_definition"}
                        and item not in linked
                    ):
                        linked.append(item)
            chains.append({
                "pages": list(range(page, current_page)),
                "key": key,
                "candidate_refs": [
                    {
                        "page": int(row.get("page", page)),
                        "source": str(row.get("source") or "primary"),
                        "block": int(row.get("block", -1)),
                    }
                    for row in linked
                ],
                "reason": "explicitly_linked_footnote_continuation_spans_three_or_more_pages",
            })
    return chains


def build_missing_continuation_reviews(
    page_reports: list[dict[str, Any]], *, page_count: int
) -> list[dict[str, Any]]:
    """Flag the final labelled note candidate when it has an open boundary and
    no traceable continuation candidate covers the following physical page.

    Only the last physical candidate on a page is eligible.  This deliberately
    avoids general prose/math heuristics. Non-hyphen signals are limited to
    unmatched delimiters and explicit unfinished clauses. The final PDF page
    still gets exception-only review.
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
        dangling_hyphen = bool(re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", text))
        # A broken OCR block can lose the hyphen entirely. Require a strong
        # open-boundary cue (unbalanced delimiter or a clause-ending marker)
        # on the last labelled note; ordinary sentence-final text is ignored.
        open_paren = text.count("(") > text.count(")")
        open_bracket = text.count("[") > text.count("]")
        dangling_clause = text.endswith((",", ":", ";", "—", "…"))
        if not (dangling_hyphen or open_paren or open_bracket or dangling_clause):
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
            exact_review_candidate = any(
                isinstance(item, dict)
                and str(item.get("source") or "primary") == str(candidate.get("source") or "primary")
                and item.get("confidence") == "review"
                and isinstance(item.get("continuation_evidence"), Mapping)
                and str(item["continuation_evidence"].get("matched_previous_key") or "") == key
                and int(item.get("continuation_evidence", {}).get("matched_previous_block", -1))
                == int(candidate.get("block", -2))
                for item in following.get("candidates", []) or []
            )
            if matched_continuation or exact_review_candidate:
                # The following page already has an exact candidate address
                # queued for visual review; a page-gap task would duplicate it
                # without identifying the block that may continue the note.
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
            "boundary_kind": "hyphen" if dangling_hyphen else "open_boundary",
            "pages": [page] if terminal_page else [page, page + 1],
            "expected_next_page": None if terminal_page else page + 1,
            "terminal_page": terminal_page,
            "reason": (
                "last_physical_footnote_ends_with_dangling_word_hyphen_without_a_traceable_continuation_candidate"
                if dangling_hyphen else
                "last_physical_footnote_has_an_open_boundary_without_a_traceable_continuation_candidate"
            ),
            "status": "pending_review",
        })
    return result


_build_cross_page_windows = build_cross_page_windows


__all__ = ["build_cross_page_windows", "build_linked_continuation_windows", "build_missing_continuation_reviews"]

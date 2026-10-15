"""Detect and score cross-page footnote continuation evidence.

This module owns layout-only continuation heuristics. It does not assign final
footnote roles and does not write Markdown. Candidate construction remains in
``footnote_candidates``; the expansion function imports those constructors
lazily to keep this module independent at import time.
"""

from __future__ import annotations

import re
from typing import Any, Iterable, Mapping, Optional

from .layout_evidence import normalized_bbox as _normalise_bbox
from .layout_evidence import text_from_block as _text_from_block
from .footnote_signals import (
    _CITATION_LABEL_RE,
    _FOOTNOTE_LABEL_RE,
    _FURNITURE_LABEL_RE,
)

DEFAULT_REGION_MAX_GAP = 0.035
DEFAULT_CONTINUATION_HIGH_SCORE = 9
DEFAULT_CONTINUATION_MEDIUM_SCORE = 5
DEFAULT_SEMANTIC_MIN_SIMILARITY = 0.08
DEFAULT_SEMANTIC_WIN_MARGIN = 0.035
DEFAULT_RECALL_AUDIT_EDGE_MARGIN = 0.04

_SEMANTIC_TOKEN_RE = re.compile(
    r"[^\W\d_][\w’'’\-]{2,}",
    re.UNICODE,
)
_SEMANTIC_STOPWORDS = frozenset(
    """
    a about above after again against all also am an and any are as at be because
    been before being below between both but by can could did do does doing down
    during each few for from further had has have having he her here hers herself
    him himself his how i if in into is it its itself just me more most my myself
    no nor not now of off on once one only or other our ours ourselves out over own
    same she should so some such than that the their theirs them themselves then
    there these they this those through to too under until up very was we were what
    when where which while who whom why will with would you your yours yourself
    yourselves
    book books chapter chapters section sections page pages work works text texts
    writing writings author authors note notes footnote footnotes reference
    references philosophy philosophical phenomenology hegel spirit consciousness
    argument arguments claim claims says say said write writes writing
    """.split()
)


def _has_footnote_evidence(candidates: Iterable[Mapping[str, Any]]) -> bool:
    """Return whether a page has a candidate that can continue onto the next."""
    return any(
        bool(candidate.get("key"))
        or bool(_FOOTNOTE_LABEL_RE.search(str(candidate.get("label") or "")))
        for candidate in candidates
        if isinstance(candidate, Mapping)
    )


def _build_footnote_page_context(
    page_number: int,
    sidecar: Mapping[str, Any],
    candidates: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Keep prior-page note evidence, with the physical tail as the target.

    A page can contain several footnotes, but only the last one in physical
    reading order can continue onto the following page without an explicit
    repeated key.  The other notes remain in the context for audit/debugging;
    they must not compete as continuation targets.
    """
    blocks = sidecar.get("blocks", [])
    notes: list[dict[str, Any]] = []
    for candidate in sorted(
        (item for item in candidates if isinstance(item, Mapping)),
        key=lambda item: (item.get("order", item.get("block", 0)), item.get("block", 0)),
    ):
        label = str(candidate.get("label") or "").strip()
        if _FURNITURE_LABEL_RE.search(label) or _CITATION_LABEL_RE.search(label):
            continue
        if not candidate.get("key") and not _FOOTNOTE_LABEL_RE.search(label):
            continue
        try:
            block_index = int(candidate.get("block"))
        except (TypeError, ValueError):
            continue
        block = blocks[block_index] if 0 <= block_index < len(blocks) else None
        if not isinstance(block, Mapping):
            continue
        notes.append(
            {
                "page": int(page_number),
                "block": block_index,
                "key": candidate.get("key"),
                "label": label,
                "text": _text_from_block(block)[:500],
                "bbox": _normalise_bbox(block, sidecar),
                "font_size": block.get("font_size"),
                "font_names": block.get("font_names"),
            }
        )
    return {
        "page": int(page_number),
        "notes": notes,
        # Keep both names explicit in the evidence contract.  ``notes`` is
        # retained for compatibility with existing reports and callers.
        "all_footnotes_for_audit": notes,
        "last_physical_footnote": notes[-1] if notes else None,
    }


def _safe_float(value: Any) -> Optional[float]:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def _font_names(value: Any) -> set[str]:
    if isinstance(value, str):
        return {value.casefold()}
    if isinstance(value, Iterable) and not isinstance(value, (bytes, Mapping)):
        return {str(item).casefold() for item in value if str(item).strip()}
    return set()


def _text_looks_unfinished(text: str) -> bool:
    value = str(text or "").strip()
    if not value:
        return False
    if re.search(r"(?:[-–—,:;([{]|\.\.\.)$", value):
        return True
    return not bool(re.search(r"[.!?。！？…][\"'”’)\]]*$", value))


def _text_starts_as_continuation(text: str) -> bool:
    value = str(text or "").lstrip()
    if not value:
        return False
    return value[0].islower() or value[0] in ",.;:)]}%\"'”’"


def _semantic_tokens(text: str) -> set[str]:
    """Return content words for a small, dependency-free semantic proxy.

    This is deliberately not presented as natural-language understanding.  It
    only supplies a conservative tie-breaker between two local layout
    hypotheses.  Removing common book/academic words makes a shared subject
    term more useful than the fact that both a body paragraph and a footnote
    mention ``Hegel`` or ``philosophy``.
    """
    value = str(text or "").casefold().replace("’", "'")
    return {
        token
        for token in _SEMANTIC_TOKEN_RE.findall(value)
        if token not in _SEMANTIC_STOPWORDS
    }


def _semantic_similarity(first: str, second: str) -> tuple[float, list[str]]:
    """Return a bounded lexical-semantic proxy and shared content words."""
    first_tokens = _semantic_tokens(first)
    second_tokens = _semantic_tokens(second)
    if not first_tokens or not second_tokens:
        return 0.0, []
    shared = sorted(first_tokens & second_tokens)
    denominator = (len(first_tokens) * len(second_tokens)) ** 0.5
    if denominator <= 0:
        return 0.0, shared
    return round(len(shared) / denominator, 3), shared[:12]


def _block_has_inline_superscript(block: Mapping[str, Any]) -> bool:
    """Detect a body footnote marker retained in the source HTML."""
    raw = block.get("html")
    if raw is None:
        raw = block.get("text", "")
    return bool(re.search(r"<sup\b", str(raw or ""), re.IGNORECASE))


def _previous_body_block(
    sidecar: Mapping[str, Any],
    block_index: int,
) -> Optional[dict[str, Any]]:
    """Find the nearest earlier body block on the current page.

    The block immediately before a suspected continuation is often the real
    body paragraph in the layout ``body -> possible continuation -> notes``.
    Headers, footers, citations, and already-labelled footnotes are not useful
    competitors and are skipped.
    """
    blocks = sidecar.get("blocks", [])
    for index in range(int(block_index) - 1, -1, -1):
        block = blocks[index] if index < len(blocks) else None
        if not isinstance(block, Mapping):
            continue
        text = _text_from_block(block)
        label = str(block.get("label") or "").strip()
        if not text:
            continue
        if (
            _FURNITURE_LABEL_RE.search(label)
            or _FOOTNOTE_LABEL_RE.search(label)
            or _CITATION_LABEL_RE.search(label)
        ):
            continue
        return {
            "page": sidecar.get("page_number"),
            "block": index,
            "label": label,
            "text": text[:500],
            "bbox": _normalise_bbox(block, sidecar),
            "font_size": block.get("font_size"),
            "font_names": block.get("font_names"),
            "looks_unfinished": _text_looks_unfinished(text),
        }
    return None


def _compare_continuation_hypotheses(
    sidecar: Mapping[str, Any],
    block_index: int,
    previous_note: Mapping[str, Any],
) -> dict[str, Any]:
    """Compare a possible continuation with the two local alternatives.

    ``footnote`` means the candidate is more related to the previous page's
    final note; ``body`` means it is more related to the preceding body block
    on the current page.  A small margin or no body context is intentionally
    inconclusive and must not suppress visual review.
    """
    blocks = sidecar.get("blocks", [])
    current = blocks[block_index] if 0 <= block_index < len(blocks) else None
    if not isinstance(current, Mapping):
        return {
            "winner": "inconclusive",
            "confidence": "low",
            "body_similarity": 0.0,
            "footnote_similarity": 0.0,
            "margin": 0.0,
            "shared_body_terms": [],
            "shared_footnote_terms": [],
            "previous_body": None,
            "inline_superscript": False,
            "signals": [],
        }

    current_text = _text_from_block(current)
    previous_body = _previous_body_block(sidecar, block_index)
    body_similarity, shared_body_terms = _semantic_similarity(
        current_text,
        str(previous_body.get("text") or "") if previous_body else "",
    )
    footnote_similarity, shared_footnote_terms = _semantic_similarity(
        current_text,
        str(previous_note.get("text") or ""),
    )
    margin = round(abs(body_similarity - footnote_similarity), 3)
    inline_superscript = _block_has_inline_superscript(current)
    signals: list[str] = []

    # A superscript marker inside an unnumbered Text block is a strong local
    # indication that the block is ordinary body prose.  It is not a footnote
    # definition's leading number and therefore should not be treated as a
    # continuation merely because the previous note was unfinished.
    if inline_superscript and not _FOOTNOTE_LABEL_RE.search(
        str(current.get("label") or "")
    ):
        signals.append("inline_body_superscript")
        return {
            "winner": "body",
            "confidence": "high",
            "body_similarity": body_similarity,
            "footnote_similarity": footnote_similarity,
            "margin": margin,
            "shared_body_terms": shared_body_terms,
            "shared_footnote_terms": shared_footnote_terms,
            "previous_body": previous_body,
            "inline_superscript": True,
            "signals": signals,
        }

    if previous_body and previous_body.get("looks_unfinished") and _text_starts_as_continuation(current_text):
        signals.append("current_body_context_unfinished")
        body_similarity += 0.04
        margin = round(abs(body_similarity - footnote_similarity), 3)

    body_wins = (
        previous_body is not None
        and body_similarity >= DEFAULT_SEMANTIC_MIN_SIMILARITY
        and body_similarity - footnote_similarity >= DEFAULT_SEMANTIC_WIN_MARGIN
    )
    footnote_wins = (
        footnote_similarity >= DEFAULT_SEMANTIC_MIN_SIMILARITY
        and footnote_similarity - body_similarity >= DEFAULT_SEMANTIC_WIN_MARGIN
    )
    if body_wins:
        winner = "body"
        confidence = "medium"
        signals.append("body_context_semantically_closer")
    elif footnote_wins:
        winner = "footnote"
        confidence = "medium"
        signals.append("previous_footnote_semantically_closer")
    else:
        winner = "inconclusive"
        confidence = "low"

    return {
        "winner": winner,
        "confidence": confidence,
        "body_similarity": round(body_similarity, 3),
        "footnote_similarity": footnote_similarity,
        "margin": margin,
        "shared_body_terms": shared_body_terms,
        "shared_footnote_terms": shared_footnote_terms,
        "previous_body": previous_body,
        "inline_superscript": inline_superscript,
        "signals": signals,
    }


def _score_cross_page_continuation(
    sidecar: Mapping[str, Any],
    block_index: int,
    first_numeric_block: int,
    previous_context: Mapping[str, Any],
) -> dict[str, Any]:
    """Score a possible continuation against the previous page's physical tail.

    The preceding page may contain one or many notes.  A continuation can only
    naturally belong to the last physical note, so earlier notes are retained
    as audit context but are never selected by this scorer.
    """
    blocks = sidecar.get("blocks", [])
    current = blocks[block_index] if 0 <= block_index < len(blocks) else None
    previous_notes = [
        note for note in previous_context.get("notes", []) if isinstance(note, Mapping)
    ]
    previous = previous_context.get("last_physical_footnote")
    if not isinstance(previous, Mapping) and previous_notes:
        # Compatibility with callers that construct the old compact context.
        previous = previous_notes[-1]
    if not isinstance(current, Mapping) or not isinstance(previous, Mapping):
        return {
            "score": 0,
            "confidence": "low",
            "previous_page": previous_context.get("page"),
            "previous_keys": [],
            "matched_previous_key": None,
            "matched_previous_block": None,
            "last_physical_key": None,
            "last_physical_block": None,
            "positive_signals": [],
            "negative_signals": [],
            "semantic_comparison": {
                "winner": "inconclusive",
                "confidence": "low",
                "body_similarity": 0.0,
                "footnote_similarity": 0.0,
                "margin": 0.0,
                "shared_body_terms": [],
                "shared_footnote_terms": [],
                "previous_body": None,
                "inline_superscript": False,
                "signals": [],
            },
        }

    previous_keys = [
        str(note.get("key"))
        for note in previous_notes
        if note.get("key") is not None
    ]
    current_text = _text_from_block(current)
    current_label = str(current.get("label") or "").strip()
    current_bbox = _normalise_bbox(current, sidecar)
    current_font_size = _safe_float(current.get("font_size"))
    body_font_size = _safe_float(sidecar.get("body_font_size"))
    score = 3
    positive: list[str] = [
        "previous_page_footnote_evidence",
        "last_physical_footnote_target",
    ]
    negative: list[str] = []
    previous_bbox = previous.get("bbox")
    previous_text = str(previous.get("text") or "")
    semantic_comparison = _compare_continuation_hypotheses(
        sidecar,
        block_index,
        previous,
    )

    if len(previous_notes) > 1:
        # This is context, not a penalty.  The existence of earlier notes says
        # nothing about whether the physical tail did or did not continue.
        positive.append("previous_page_has_multiple_footnotes")
    if _FOOTNOTE_LABEL_RE.search(current_label):
        score += 2
        positive.append("current_footnote_label")
    if _FOOTNOTE_LABEL_RE.search(str(previous.get("label") or "")):
        score += 1
        positive.append("previous_footnote_label")

    if current_bbox and previous_bbox:
        x_delta = abs(float(current_bbox[0]) - float(previous_bbox[0]))
        width_delta = abs(
            (float(current_bbox[2]) - float(current_bbox[0]))
            - (float(previous_bbox[2]) - float(previous_bbox[0]))
        )
        if x_delta <= 0.03:
            score += 2
            positive.append("left_edge_aligned")
        elif x_delta <= 0.08:
            score += 1
            positive.append("left_edge_near")
        if width_delta <= 0.08:
            score += 1
            positive.append("line_width_similar")

    previous_font_size = _safe_float(previous.get("font_size"))
    if current_font_size and previous_font_size:
        font_delta = abs(current_font_size - previous_font_size) / max(
            current_font_size, previous_font_size
        )
        if font_delta <= 0.10:
            score += 2
            positive.append("font_size_matches_previous_note")
        elif font_delta <= 0.20:
            score += 1
            positive.append("font_size_near_previous_note")
    if _font_names(current.get("font_names")) & _font_names(previous.get("font_names")):
        score += 1
        positive.append("font_family_matches_previous_note")
    if current_font_size and body_font_size:
        body_ratio = current_font_size / body_font_size
        if body_ratio <= 0.90:
            score += 1
            positive.append("smaller_than_current_body")
        elif body_ratio >= 0.98:
            score -= 1
            negative.append("same_size_as_current_body")

    if _text_looks_unfinished(previous_text):
        score += 2
        positive.append("previous_text_looks_unfinished")
    elif previous_text:
        # A visibly closed last note is a meaningful counter-signal.  It does
        # not prove that no continuation exists (OCR and typography can be
        # imperfect), so it only prevents local auto-linking; visual review
        # can still accept the continuation.
        score -= 4
        negative.append("previous_text_looks_complete")
    if _text_starts_as_continuation(current_text):
        score += 1
        positive.append("current_text_starts_as_continuation")
    semantic_winner = semantic_comparison.get("winner")
    if semantic_winner == "body":
        # The body hypothesis is allowed to overrule a strong-looking
        # unfinished-note score only when it has a concrete local reason:
        # an inline body marker or a meaningful lexical advantage.
        score -= 6 if semantic_comparison.get("confidence") == "high" else 4
        negative.extend(
            str(signal) for signal in semantic_comparison.get("signals", [])
        )
    elif semantic_winner == "footnote":
        score += 2
        positive.extend(
            str(signal) for signal in semantic_comparison.get("signals", [])
        )
    if block_index + 1 == first_numeric_block:
        score += 1
        positive.append("immediately_before_new_number")
        first_numeric = (
            blocks[first_numeric_block]
            if 0 <= first_numeric_block < len(blocks)
            else None
        )
        first_numeric_bbox = (
            _normalise_bbox(first_numeric, sidecar)
            if isinstance(first_numeric, Mapping)
            else None
        )
        if current_bbox and first_numeric_bbox:
            gap = float(first_numeric_bbox[1]) - float(current_bbox[3])
            if 0.0 <= gap <= DEFAULT_REGION_MAX_GAP:
                score += 1
                positive.append("small_gap_before_new_number")
    if _FURNITURE_LABEL_RE.search(current_label) or _CITATION_LABEL_RE.search(current_label):
        score -= 5
        negative.append("current_block_is_furniture_or_citation")

    return {
        "score": score,
        "confidence": (
            "high"
            if score >= DEFAULT_CONTINUATION_HIGH_SCORE
            else "medium"
            if score >= DEFAULT_CONTINUATION_MEDIUM_SCORE
            else "low"
        ),
        "previous_page": previous_context.get("page"),
        "previous_keys": sorted(set(previous_keys)),
        "matched_previous_key": previous.get("key"),
        "matched_previous_block": previous.get("block"),
        "last_physical_key": previous.get("key"),
        "last_physical_block": previous.get("block"),
        "positive_signals": positive,
        "negative_signals": negative,
        "semantic_comparison": semantic_comparison,
    }


def _annotate_cross_page_continuations(
    sidecar: Mapping[str, Any],
    candidates: list[dict[str, Any]],
    continuous_regions: Iterable[Mapping[str, Any]],
    previous_context: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Attach continuation scores to backtracked candidates."""
    summary = {
        "mode": "gated",
        "candidate_count": 0,
        "high_confidence_count": 0,
        "medium_confidence_count": 0,
        "low_confidence_count": 0,
        "semantic_body_count": 0,
        "semantic_footnote_count": 0,
        "semantic_inconclusive_count": 0,
    }
    if not previous_context or not previous_context.get("notes"):
        return summary
    by_index = {
        int(candidate["block"]): candidate
        for candidate in candidates
        if str(candidate.get("block", "")).lstrip("-").isdigit()
    }
    for region in continuous_regions:
        first_numeric_block = int(region.get("first_block", -1))
        for raw_index in region.get("backtracked_block_indices", []) or []:
            block_index = int(raw_index)
            evidence = _score_cross_page_continuation(
                sidecar,
                block_index,
                first_numeric_block,
                previous_context,
            )
            candidate = by_index.get(block_index)
            if candidate is not None:
                candidate["continuation_evidence"] = evidence
            summary["candidate_count"] += 1
            bucket = f"{evidence['confidence']}_confidence_count"
            summary[bucket] += 1
            semantic = evidence.get("semantic_comparison", {})
            winner = semantic.get("winner") if isinstance(semantic, Mapping) else None
            if winner in {"body", "footnote", "inconclusive"}:
                summary[f"semantic_{winner}_count"] += 1
    return summary


def _suppress_soft_continuation_candidates(
    candidates: Iterable[Mapping[str, Any]],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Keep body-like backtracks as context instead of review decisions.

    Numeric runs often sit immediately below the final body paragraph.  OCR
    labels that paragraph as ``Text`` and the old expansion treated it as an
    unresolved continuation, forcing a whole-page visual audit.  Suppress
    only the weak form of that signal: an unnumbered ordinary-text block with
    no cross-page evidence, or a low-confidence block before a region that
    already contains explicit ``Footnote`` labels.  A local semantic
    comparison may also suppress a plain ``Text`` block when an inline body
    marker or a clear body-context advantage defeats the continuation
    hypothesis.  Explicit Footnote labels and medium/high continuation
    evidence remain reviewable.
    """
    active: list[dict[str, Any]] = []
    context_only: list[dict[str, Any]] = []
    for raw_candidate in candidates:
        candidate = dict(raw_candidate)
        if candidate.get("key") or not candidate.get("continuous_region_evidence"):
            active.append(candidate)
            continue

        label = str(candidate.get("label") or "").strip()
        if _FOOTNOTE_LABEL_RE.search(label):
            active.append(candidate)
            continue

        evidence = candidate.get("continuation_evidence")
        confidence = evidence.get("confidence") if isinstance(evidence, Mapping) else None
        semantic = (
            evidence.get("semantic_comparison")
            if isinstance(evidence, Mapping)
            else None
        )
        has_explicit_note_labels = bool(
            candidate.get("continuous_region_has_footnote_label")
        )
        suppress_reason: Optional[str] = None
        if (
            isinstance(semantic, Mapping)
            and semantic.get("winner") == "body"
            and semantic.get("confidence") in {"medium", "high"}
        ):
            suppress_reason = "body_context_outvotes_cross_page_continuation"
        elif not isinstance(evidence, Mapping):
            suppress_reason = "ordinary_text_without_cross_page_evidence"
        elif (
            confidence in {"low", "medium"}
            and has_explicit_note_labels
            and "current_text_starts_as_continuation"
            not in evidence.get("positive_signals", [])
        ):
            suppress_reason = "weak_cross_page_evidence_before_labeled_notes"

        if suppress_reason is None:
            active.append(candidate)
            continue

        candidate["confidence"] = "context"
        candidate["disposition"] = "context_only"
        candidate["review_reason"] = "soft_continuation_context"
        candidate["suppression_reason"] = suppress_reason
        candidate["suppressed_as_context"] = True
        context_only.append(candidate)

    return active, context_only


def _aggregate_continuation_evidence(
    page_reports: Iterable[Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate continuation scores without duplicating page-level decisions."""
    summary = {
        "mode": "gated",
        "candidate_count": 0,
        "high_confidence_count": 0,
        "medium_confidence_count": 0,
        "low_confidence_count": 0,
        "semantic_body_count": 0,
        "semantic_footnote_count": 0,
        "semantic_inconclusive_count": 0,
    }
    for report in page_reports:
        evidence = report.get("continuation_evidence", {})
        if not isinstance(evidence, Mapping):
            continue
        for field in (
            "candidate_count",
            "high_confidence_count",
            "medium_confidence_count",
            "low_confidence_count",
        ):
            summary[field] += int(evidence.get(field, 0) or 0)
        for field in (
            "semantic_body_count",
            "semantic_footnote_count",
            "semantic_inconclusive_count",
        ):
            summary[field] += int(evidence.get(field, 0) or 0)
    return summary

__all__ = [
    "_aggregate_continuation_evidence",
    "_annotate_cross_page_continuations",
    "_build_footnote_page_context",
    "_has_footnote_evidence",
    "_suppress_soft_continuation_candidates",
]

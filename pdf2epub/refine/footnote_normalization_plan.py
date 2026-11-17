"""Build the exact in-memory normalization plan used by validate and apply."""

from __future__ import annotations

import re
from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from .footnote_apply_resolution import bind_note_markers, resolve_page_decisions
from .footnote_edit_plan import (
    FootnoteEditPlan,
    FootnotePlanTransformError,
    create_footnote_edit_plan,
    transform_footnote_edit_plan,
)
from .footnote_source import FootnoteSourceSnapshot
from .footnote_apply_resolution import FootnoteDecisionResolution


@dataclass(frozen=True)
class FootnoteNormalizationPlanningInput:
    """Evidence and reviewed decisions needed to construct a normalization plan."""

    output_dir: Path
    report: Mapping[str, Any]
    decisions: Mapping[str, Any]
    source: FootnoteSourceSnapshot
    footnote_mode: str
    toc_tree: Optional[Mapping[str, Any]] = None
    progress: Optional[Mapping[str, Any]] = None
    scope_error: Optional[str] = None
    endnote_definition_decisions: Optional[list[dict[str, Any]]] = None


@dataclass(frozen=True)
class FootnoteNormalizationPlan:
    """Result of resolving, binding, editing, and transforming reviewed decisions."""

    edit_plan: Optional[FootnoteEditPlan]
    validation_decisions: tuple[dict[str, Any], ...]
    errors: tuple[str, ...] = ()

    @property
    def valid(self) -> bool:
        return not self.errors and self.edit_plan is not None and self.edit_plan.valid


def _resolution_inputs(
    report: Mapping[str, Any],
    decisions: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Project reviewed body exceptions into the ordinary resolver contract."""
    projected_report = deepcopy(dict(report))
    projected_decisions = deepcopy(dict(decisions))
    continuation_reviews = projected_decisions.get("continuation_reviews", [])
    body_exceptions: list[dict[str, Any]] = []
    pages = []
    for page_report in report.get("pages", []) or []:
        if not isinstance(page_report, Mapping):
            pages.append(page_report)
            continue
        page = dict(page_report)
        candidates = []
        for candidate in page_report.get("candidates", []) or []:
            item = dict(candidate)
            reviewed_as_body = any(
                isinstance(review, Mapping)
                and review.get("outcome") == "confirmed_not_a_footnote"
                and int((review.get("source_note") or {}).get("page", -1)) == int(item.get("page", -2))
                and int((review.get("source_note") or {}).get("block", -1)) == int(item.get("block", -2))
                and str((review.get("source_note") or {}).get("source") or "primary") == str(item.get("source") or "primary")
                for review in continuation_reviews if isinstance(continuation_reviews, list)
            )
            if reviewed_as_body:
                item["confidence"] = "review"
                candidate_page = int(item["page"])
                chapter_id = next((
                    str(record.get("chapter_id") or "") for record in records
                    if int(record.get("start_page", 0)) <= candidate_page <= int(record.get("end_page", 0))
                ), "")
                body_exceptions.append({
                    "page": candidate_page,
                    "source": str(item.get("source") or "primary"),
                    "block": int(item["block"]),
                    "role": "body",
                    "key": str(item.get("key") or ""),
                    "chapter_id": chapter_id,
                })
            candidates.append(item)
        page["candidates"] = candidates
        pages.append(page)
    if body_exceptions:
        projected_report["pages"] = pages
        projected_decisions["decisions"] = [
            *(projected_decisions.get("decisions", []) or []), *body_exceptions
        ]
    return projected_report, projected_decisions


def _recover_continuations(
    resolution: FootnoteDecisionResolution,
    report: Mapping[str, Any],
    decisions: Mapping[str, Any],
) -> Optional[str]:
    """Append only reviewed text whose exact source note and open boundary remain."""
    reviews = decisions.get("continuation_reviews", [])
    tasks = report.get("continuation_reviews", [])
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, Mapping) or review.get("outcome") != "recovered_continuation":
            continue
        task = next((
            item for item in tasks if isinstance(item, Mapping)
            and item.get("task_id") == review.get("task_id")
        ), None)
        source_note = task.get("source_note", {}) if isinstance(task, Mapping) else {}
        matches = [
            note for notes in resolution.notes_by_chapter.values() for note in notes
            if str(note.get("key")) == str(review.get("key"))
            and int(note.get("source_page", -1)) == int(source_note.get("page", -2))
        ]
        if len(matches) != 1:
            return f"{review.get('task_id')}: recovered continuation does not identify one exact source note"
        note = matches[0]
        text = str(review.get("text") or "").strip()
        boundary_kind = str(task.get("boundary_kind") or "") if isinstance(task, Mapping) else ""
        if not boundary_kind:
            ending_hint = str(source_note.get("text_ending") or "")
            boundary_kind = "hyphen" if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", ending_hint) else "open_boundary"
        if not note.get("parts"):
            return f"{review.get('task_id')}: source note has no reviewed continuation boundary; rerun footnote-prepare"
        if boundary_kind == "hyphen" and not re.match(r"^[a-zà-öø-ÿ]", text):
            return f"{review.get('task_id')}: recovered continuation must begin with the visible lowercase word suffix"
        ending = str(note["parts"][-1]).rstrip()
        if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", ending):
            note["parts"][-1] = ending[:-1] + text
        elif ending.count("(") > ending.count(")") or ending.count("[") > ending.count("]"):
            closing = ")" if ending.count("(") > ending.count(")") else "]"
            note["parts"][-1] = ending + ("" if text.startswith(closing) else " ") + text
        elif ending.endswith((",", ":", ";", "—", "…")):
            note["parts"][-1] = ending + " " + text
        else:
            return f"{review.get('task_id')}: source note no longer has its reviewed open boundary; rerun footnote-prepare"
        note.setdefault("recovered_continuations", []).append(str(review.get("task_id")))
    return None


def _invalid_plan(decisions: Mapping[str, Any], error: str) -> FootnoteNormalizationPlan:
    return FootnoteNormalizationPlan(None, tuple(decisions.get("decisions", []) or []), (error,))


def build_footnote_normalization_plan(
    inputs: FootnoteNormalizationPlanningInput,
) -> FootnoteNormalizationPlan:
    """Resolve decisions and produce transformed text without writing files.

    The resolver consults current page sidecars through ``output_dir``; the
    resulting plan is therefore filesystem-backed evidence resolution, followed
    by in-memory edit validation/transformation.
    """
    records = list(inputs.source.records)
    source_texts = inputs.source.source_texts
    report, decisions = _resolution_inputs(inputs.report, inputs.decisions, records)
    try:
        resolution = resolve_page_decisions(
            inputs.output_dir,
            report,
            decisions,
            source_texts,
            records,
            inputs.footnote_mode,
        )
    except (OSError, ValueError, TypeError, KeyError) as exc:
        return _invalid_plan(decisions, str(exc))
    if resolution.errors:
        return FootnoteNormalizationPlan(None, tuple(decisions.get("decisions", []) or []), tuple(resolution.errors))

    continuation_error = _recover_continuations(resolution, inputs.report, decisions)
    if continuation_error:
        return _invalid_plan(decisions, continuation_error)

    try:
        bind_note_markers(resolution, source_texts, records)
        edit_plan = create_footnote_edit_plan(
            source_texts, resolution, source_hashes=inputs.source.source_hashes
        )
        if not edit_plan.valid:
            return FootnoteNormalizationPlan(edit_plan, tuple(decisions.get("decisions", []) or []), edit_plan.errors)
        edit_plan = transform_footnote_edit_plan(
            edit_plan,
            records,
            footnote_mode=inputs.footnote_mode,
            toc_tree=inputs.toc_tree,
            progress=inputs.progress,
            scope_error=inputs.scope_error,
            definition_decisions=inputs.endnote_definition_decisions,
        )
    except (FootnotePlanTransformError, OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        return _invalid_plan(decisions, str(exc))

    return FootnoteNormalizationPlan(
        edit_plan,
        tuple(decisions.get("decisions", []) or []),
        tuple(edit_plan.errors),
    )


__all__ = [
    "FootnoteNormalizationPlan",
    "FootnoteNormalizationPlanningInput",
    "build_footnote_normalization_plan",
]

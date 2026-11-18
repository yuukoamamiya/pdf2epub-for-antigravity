"""Build the exact in-memory normalization plan used by validate and apply."""

from __future__ import annotations

import re
from dataclasses import dataclass, field
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
from .footnote_effective_decisions import (
    EFFECTIVE_DECISIONS_VERSION,
    compile_effective_decisions,
    project_confirmed_not_a_footnote,
)


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
    expected_effective_decisions: Optional[list[dict[str, Any]]] = None
    expected_source_sha256: Optional[Mapping[str, str]] = None


@dataclass(frozen=True)
class FootnoteNormalizationPlan:
    """Result of resolving, binding, editing, and transforming reviewed decisions."""

    edit_plan: Optional[FootnoteEditPlan]
    validation_decisions: tuple[dict[str, Any], ...]
    errors: tuple[str, ...] = ()
    effective_decisions: tuple[dict[str, Any], ...] = ()
    effective_decisions_version: int = EFFECTIVE_DECISIONS_VERSION
    source_sha256: Mapping[str, str] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors and self.edit_plan is not None and self.edit_plan.valid


def _resolution_inputs(
    report: Mapping[str, Any],
    decisions: Mapping[str, Any],
    records: list[dict[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Use the shared explicit projection for confirmed body exceptions."""
    return project_confirmed_not_a_footnote(report, decisions, records)

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
    compiled = compile_effective_decisions(report, decisions.get("decisions", []))
    if compiled["errors"]:
        return _invalid_plan(decisions, "; ".join(compiled["errors"]))
    effective_decisions = compiled["decisions"]
    if inputs.expected_effective_decisions is not None and list(inputs.expected_effective_decisions) != effective_decisions:
        return _invalid_plan(decisions, "effective footnote decisions changed since validation; rerun footnote-validate")
    if inputs.expected_source_sha256 is not None and dict(inputs.expected_source_sha256) != dict(inputs.source.source_hashes):
        return _invalid_plan(decisions, "footnote source Markdown changed since validation; rerun footnote-validate")
    decisions["effective_decisions"] = effective_decisions
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
        tuple(effective_decisions),
        EFFECTIVE_DECISIONS_VERSION,
        dict(inputs.source.source_hashes),
    )


__all__ = [
    "FootnoteNormalizationPlan",
    "FootnoteNormalizationPlanningInput",
    "build_footnote_normalization_plan",
]

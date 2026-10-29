"""Pure, in-memory edit-plan data, validation, and text transformation."""

from __future__ import annotations

from dataclasses import dataclass, field, replace
import re
from typing import Any, Mapping, Optional, Protocol

from .endnote_normalization import normalize_endnotes
from .endnote_scopes import build_endnote_scope_directory


def split_multi_superscripts(text: str) -> str:
    """Split OCR lists such as ``<sup>52, 53</sup>`` into separate markers."""
    def repl(match: re.Match[str]) -> str:
        inner = match.group(1)
        symbol = re.fullmatch(r"([A-Za-z0-9_-]+)\s*(\\\*+|\*+|[†‡§]+)", inner.strip())
        if symbol:
            return f"<sup>{symbol.group(1)}</sup>{symbol.group(2)}"
        parts = [part.strip() for part in re.split(r"[,;]\s*", inner) if part.strip()]
        if len(parts) > 1 and all(re.fullmatch(r"[A-Za-z0-9_-]+", part) for part in parts):
            return "".join(f"<sup>{part}</sup>" for part in parts)
        return match.group(0)

    return re.sub(r"<sup\b[^>]*>(.*?)</sup>", repl, text, flags=re.IGNORECASE | re.DOTALL)


@dataclass(frozen=True)
class FootnoteEdit:
    """One source-relative replacement."""

    start: int
    end: int
    replacement: str
    purpose: str


@dataclass(frozen=True)
class FootnoteEditPlan:
    """A fully resolved, validated set of source edits and derived unit text."""

    source_texts: Mapping[str, str]
    edits_by_file: Mapping[str, tuple[FootnoteEdit, ...]]
    transformed_texts: Mapping[str, str]
    errors: tuple[str, ...] = ()
    source_hashes: Mapping[str, str] = field(default_factory=dict)
    notes_by_chapter: Mapping[str, tuple[Mapping[str, Any], ...]] = field(default_factory=dict)
    resolved_blocks: tuple[Mapping[str, Any], ...] = ()
    preserved_roles: Mapping[str, int] = field(default_factory=dict)
    endnotes_report: Mapping[str, Any] = field(default_factory=dict)

    @property
    def valid(self) -> bool:
        return not self.errors


class _DecisionResolution(Protocol):
    edits_by_file: Mapping[str, list[tuple[int, int, str]]]
    notes_by_chapter: Mapping[str, list[dict[str, Any]]]
    resolved: list[dict[str, Any]]
    preserved_roles: Mapping[str, int]
    errors: list[str]


def create_footnote_edit_plan(
    source_texts: Mapping[str, str],
    resolution: _DecisionResolution,
    *,
    source_hashes: Optional[Mapping[str, str]] = None,
) -> FootnoteEditPlan:
    """Turn resolved block and marker bindings into a validated edit plan."""
    edits = {
        name: tuple(
            FootnoteEdit(
                start=start,
                end=end,
                replacement=replacement,
                purpose="remove-note-source" if replacement == "" else "bind-body-marker",
            )
            for start, end, replacement in items
        )
        for name, items in resolution.edits_by_file.items()
    }
    plan = create_edit_plan(source_texts, edits)
    errors = tuple(resolution.errors) + plan.errors
    return replace(
        plan,
        errors=errors,
        source_hashes=dict(source_hashes or {}),
        notes_by_chapter={
            chapter: tuple(notes) for chapter, notes in resolution.notes_by_chapter.items()
        },
        resolved_blocks=tuple(resolution.resolved),
        preserved_roles=dict(resolution.preserved_roles),
    )


def create_edit_plan(
    source_texts: Mapping[str, str],
    edits_by_file: Mapping[str, tuple[FootnoteEdit, ...] | list[FootnoteEdit]],
) -> FootnoteEditPlan:
    """Validate all edits and produce transformed texts, or an invalid plan."""
    errors = validate_edits(source_texts, edits_by_file)
    frozen_edits = {name: tuple(edits) for name, edits in edits_by_file.items()}
    if errors:
        return FootnoteEditPlan(
            source_texts=dict(source_texts),
            edits_by_file=frozen_edits,
            transformed_texts={},
            errors=errors,
        )
    return FootnoteEditPlan(
        source_texts=dict(source_texts),
        edits_by_file=frozen_edits,
        transformed_texts=transform_edits(source_texts, frozen_edits),
    )


def validate_edits(
    source_texts: Mapping[str, str],
    edits_by_file: Mapping[str, tuple[FootnoteEdit, ...] | list[FootnoteEdit]],
) -> tuple[str, ...]:
    """Reject out-of-range, overlapping, or ambiguous zero-width edits."""
    errors: list[str] = []
    for name, edits in edits_by_file.items():
        source = source_texts.get(name)
        if source is None:
            errors.append(f"edit targets unknown source unit {name!r}")
            continue
        ordered = sorted(edits, key=lambda item: (item.start, item.end))
        previous: FootnoteEdit | None = None
        for edit in ordered:
            if edit.start < 0 or edit.end < edit.start or edit.end > len(source):
                errors.append(
                    f"{name}: {edit.purpose} span [{edit.start}, {edit.end}) "
                    f"is outside source length {len(source)}"
                )
                continue
            if previous is not None and edit.start < previous.end:
                errors.append(
                    f"{name}: overlapping edits ({previous.purpose} and "
                    f"{edit.purpose}) at [{edit.start}, {edit.end})"
                )
            elif previous is not None and edit.start == previous.start and (
                edit.start == edit.end or previous.start == previous.end
            ):
                errors.append(
                    f"{name}: multiple insertions or replacement at offset {edit.start}"
                )
            elif (
                previous is not None
                and edit.start == previous.start
                and edit.end == previous.end
            ):
                errors.append(
                    f"{name}: duplicate edits ({previous.purpose} and "
                    f"{edit.purpose}) target [{edit.start}, {edit.end})"
                )
            previous = edit
    return tuple(errors)


def transform_edits(
    source_texts: Mapping[str, str],
    edits_by_file: Mapping[str, tuple[FootnoteEdit, ...] | list[FootnoteEdit]],
) -> dict[str, str]:
    """Apply an already validated plan without mutating inputs or doing I/O."""
    errors = validate_edits(source_texts, edits_by_file)
    if errors:
        raise ValueError("; ".join(errors))
    transformed = dict(source_texts)
    for name, edits in edits_by_file.items():
        source = source_texts[name]
        for edit in sorted(edits, key=lambda item: (item.start, item.end), reverse=True):
            source = source[: edit.start] + edit.replacement + source[edit.end :]
        transformed[name] = source
    return transformed


class FootnotePlanTransformError(ValueError):
    """The bound edit plan cannot be transformed safely."""


def _format_definition(key: str, content: str) -> str:
    lines = content.strip().splitlines()
    if not lines:
        return f"[^{key}]:"
    if re.match(r"^(?:>|[-*+]\s+|\d+[.)]\s+|\|.*\||```|~~~)", lines[0].lstrip()):
        nested = [f"    {line}" if line.strip() else "" for line in lines]
        return f"[^{key}]:\n" + "\n".join(nested)
    nested = [f"    {line}" if line.strip() else "" for line in lines[1:]]
    return f"[^{key}]: {lines[0]}" + ("\n" + "\n".join(nested) if nested else "")


def _join_parts(parts: list[str]) -> str:
    result = ""
    block_start = re.compile(
        r"^(?:>|[-*+]\s+|\d+[.)]\s+|\|.*\||```|~~~|</?(?:blockquote|table|pre|ul|ol)\b)",
        re.IGNORECASE,
    )
    for part in (value.strip() for value in parts if value and value.strip()):
        if result:
            result += "\n\n" if block_start.match(part.lstrip()) else "\n"
        result += part
    return result.strip()


def transform_footnote_edit_plan(
    plan: FootnoteEditPlan,
    records: list[dict[str, Any]],
    *,
    footnote_mode: str,
    toc_tree: Optional[Mapping[str, Any]],
    progress: Optional[Mapping[str, Any]],
    scope_error: Optional[str],
    definition_decisions: Optional[list[dict[str, Any]]],
) -> FootnoteEditPlan:
    """Append page notes and normalize centralized endnotes in memory."""
    if not plan.valid:
        return plan
    transformed = dict(plan.transformed_texts)
    final_file_by_chapter: dict[str, dict[str, Any]] = {}
    for record in records:
        current = final_file_by_chapter.get(record["chapter_id"])
        if current is None or (record["unit_order"], record["part_index"]) >= (
            current["unit_order"], current["part_index"]
        ):
            final_file_by_chapter[record["chapter_id"]] = record
    for chapter_id, notes in plan.notes_by_chapter.items():
        target = final_file_by_chapter.get(chapter_id)
        if target is None:
            raise FootnotePlanTransformError(f"no chapter-end unit found for {chapter_id}")
        definitions = [
            _format_definition(str(note["output_key"]), _join_parts(list(note["parts"])))
            for note in notes
        ]
        if definitions:
            current = transformed[target["name"]].rstrip()
            transformed[target["name"]] = current + "\n\n" + "\n\n".join(definitions) + "\n"

    endnote_report: Mapping[str, Any] = {
        "status": "disabled" if footnote_mode == "disabled" else "not_started",
        "valid": True,
        "source_files": [],
        "definition_keys": [],
        "definition_count": 0,
        "normalized_definition_count": 0,
        "converted_reference_count": 0,
        "reference_counts": {},
        "orphan_definition_keys": [],
        "scope_mode": "global",
        "scope_count": 0,
        "scopes": [],
        "scoped_keys": [],
        "double_numbered_definition_count": 0,
        "duplicate_definition_occurrences": [],
        "orphan_scoped_definitions": [],
        "errors": [],
    }
    if footnote_mode != "disabled":
        scope_directory = None
        local_scope_error = scope_error
        if toc_tree is not None and progress is not None:
            try:
                scope_directory = build_endnote_scope_directory(toc_tree, progress, transformed)
                local_scope_error = None
            except (ValueError, TypeError) as exc:
                local_scope_error = str(exc)
        try:
            transformed, endnote_report = normalize_endnotes(
                transformed,
                {record["name"]: record["type"] for record in records},
                scopes=scope_directory,
                definition_decisions=definition_decisions,
            )
        except (OSError, UnicodeError, ValueError) as exc:
            detail = f"could not normalize centralized endnotes: {exc}"
            if scope_directory is None and local_scope_error:
                detail += f"; per-chapter endnote scopes are unavailable: {local_scope_error}"
            raise FootnotePlanTransformError(detail) from exc
    return replace(
        plan,
        transformed_texts=transformed,
        endnotes_report=endnote_report,
    )


__all__ = [
    "FootnoteEdit",
    "FootnoteEditPlan",
    "FootnotePlanTransformError",
    "create_edit_plan",
    "create_footnote_edit_plan",
    "split_multi_superscripts",
    "transform_edits",
    "transform_footnote_edit_plan",
    "validate_edits",
]

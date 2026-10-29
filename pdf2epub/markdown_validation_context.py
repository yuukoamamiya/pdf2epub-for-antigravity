"""Read-only inputs for Markdown Subagent validation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable, Mapping

from .batching_service import markdown_files
from .document_roles import TRANSLATION_SPECIAL_ROLES
from .footnote_normalization import endnote_definition_keys
from .subagent_safety import protected_workspace_changes


@dataclass(frozen=True)
class ManifestReviewSnapshot:
    files: frozenset[str]
    keys: frozenset[tuple[str, str]]
    protected_workspace_blocked: tuple[str, ...]


@dataclass(frozen=True)
class MarkdownValidationPaths:
    output_dir: Path
    source_dir: Path
    target_dir: Path
    validated_dir: Path
    report_path: Path


@dataclass(frozen=True)
class MarkdownAuditOptions:
    task: str
    structural_patterns: tuple[str, ...]
    file_roles: Mapping[str, str]
    invalid_file_roles: Mapping[str, str]
    tolerate_duplicate_headings: bool
    validate_footnote_normalization: bool
    fix_reference_headings: bool
    target_language: str | None
    protected_toc_titles: tuple[str, ...]
    continuation_toc_titles: Mapping[str, frozenset[str]]
    expected_endnote_keys: frozenset[str]


@dataclass(frozen=True)
class MarkdownValidationContext:
    paths: MarkdownValidationPaths
    sources: tuple[Path, ...]
    partial: bool
    create_validated_copy: bool
    allow_review_warnings: bool
    options: MarkdownAuditOptions
    review_snapshot: ManifestReviewSnapshot


def _manifest_snapshot(output_dir: Path, task: str) -> ManifestReviewSnapshot:
    path = output_dir / f"{task}_subagent_manifest.json"
    files: set[str] = set()
    keys: set[tuple[str, str]] = set()
    blocked: list[str] = []
    if path.is_file():
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(data, Mapping):
                previous = data.get("previous_review_required", {})
                if isinstance(previous, Mapping):
                    for raw_name, items in previous.items():
                        name = str(raw_name)
                        if not name.strip():
                            continue
                        files.add(name)
                        if isinstance(items, list):
                            keys.update(
                                (name, str(item["kind"]))
                                for item in items
                                if isinstance(item, Mapping) and item.get("kind")
                            )
                blocked = protected_workspace_changes(
                    output_dir, data.get("protected_workspace", {})
                )
        except (OSError, json.JSONDecodeError, UnicodeError):
            pass
    return ManifestReviewSnapshot(frozenset(files), frozenset(keys), tuple(blocked))


def load_markdown_validation_context(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    *,
    structural_patterns: Iterable[str] = (),
    create_validated_copy: bool = True,
    file_roles: Mapping[str, str] | None = None,
    tolerate_duplicate_headings: bool = False,
    validate_footnote_normalization: bool = False,
    fix_reference_headings: bool = False,
    selected_files: Iterable[str] | None = None,
    target_language: str | None = None,
    allow_review_warnings: bool = False,
    protected_toc_titles: Iterable[str] = (),
    continuation_toc_titles: Mapping[str, Iterable[str]] | None = None,
) -> MarkdownValidationContext:
    output_dir, source_dir, target_dir = map(Path, (output_dir, source_dir, target_dir))
    all_sources = markdown_files(source_dir)
    selected = None if selected_files is None else {str(name) for name in selected_files}
    if selected is not None:
        unknown = sorted(selected - {p.name for p in all_sources})
        if unknown:
            raise ValueError(f"Unknown Markdown source file(s): {', '.join(unknown)}")
    sources = tuple(p for p in all_sources if selected is None or p.name in selected)
    roles = {
        str(name): str(role).strip().lower()
        for name, role in (file_roles or {}).items()
        if str(role).strip().lower() in TRANSLATION_SPECIAL_ROLES
    }
    invalid_roles = {
        str(name): str(role).strip().lower()
        for name, role in (file_roles or {}).items()
        if str(role).strip() and str(role).strip().lower() not in TRANSLATION_SPECIAL_ROLES
    }
    expected: set[str] = set()
    if task == "polish":
        for source in sources:
            if roles.get(source.name) == "notes":
                try:
                    expected.update(endnote_definition_keys(source.read_text(encoding="utf-8"), file_role="notes"))
                except (OSError, UnicodeError):
                    pass
    titles = tuple(dict.fromkeys(str(t).strip() for t in protected_toc_titles if str(t).strip()))
    continuation = {
        str(name): frozenset(str(t).strip() for t in values if str(t).strip())
        for name, values in (continuation_toc_titles or {}).items()
        if str(name).strip() and values is not None
    }
    paths = MarkdownValidationPaths(
        output_dir, source_dir, target_dir, target_dir / "validated",
        output_dir / (f"{task}_file_validation.json" if selected is not None else f"{task}_validation.json"),
    )
    options = MarkdownAuditOptions(
        task, tuple(structural_patterns), roles, invalid_roles, tolerate_duplicate_headings,
        validate_footnote_normalization, fix_reference_headings, target_language,
        titles, continuation, frozenset(expected),
    )
    return MarkdownValidationContext(
        paths, sources, selected is not None, create_validated_copy,
        allow_review_warnings, options, _manifest_snapshot(output_dir, task),
    )

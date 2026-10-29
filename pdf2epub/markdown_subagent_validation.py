"""Markdown Subagent output validation and staging."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .batching_service import markdown_files
from .markdown_validation_context import load_markdown_validation_context
from .markdown_validation_audit import audit_markdown_files
from .markdown_validation_state import resolve_review_state
from .markdown_validation_persistence import (
    begin_validation_io,
    stage_normalized_and_validated_targets,
    persist_validation_report,
)
from .workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION


# Changing the warning gate is a validation-contract change.  Older reports
# were allowed to stage files while carrying advisory warnings, so callers
# must not treat a pre-v2 report as proof that the new gate was evaluated.
VALIDATION_SCHEMA_VERSION = MARKDOWN_VALIDATION_SCHEMA_VERSION
def validate_markdown_subagent(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    structural_patterns: Iterable[str] = (),
    create_validated_copy: bool = True,
    file_roles: Optional[Mapping[str, str]] = None,
    tolerate_duplicate_headings: bool = False,
    validate_footnote_normalization: bool = False,
    fix_reference_headings: bool = False,
    selected_files: Optional[Iterable[str]] = None,
    target_language: Optional[str] = None,
    allow_review_warnings: bool = False,
    protected_toc_titles: Optional[Iterable[str]] = None,
    continuation_toc_titles: Optional[
        Mapping[str, Iterable[str]]
    ] = None,
) -> Dict:
    """Validate a Subagent markdown hand-off and optionally stage it."""
    context = load_markdown_validation_context(
        output_dir, task, source_dir, target_dir,
        structural_patterns=structural_patterns,
        create_validated_copy=create_validated_copy,
        file_roles=file_roles,
        tolerate_duplicate_headings=tolerate_duplicate_headings,
        validate_footnote_normalization=validate_footnote_normalization,
        fix_reference_headings=fix_reference_headings,
        selected_files=selected_files,
        target_language=target_language,
        allow_review_warnings=allow_review_warnings,
        protected_toc_titles=protected_toc_titles or (),
        continuation_toc_titles=continuation_toc_titles,
    )
    target_dir = context.paths.target_dir
    validated_dir = context.paths.validated_dir
    sources = context.sources
    partial = context.partial
    allow_review_warnings = context.allow_review_warnings
    previous_review_files = set(context.review_snapshot.files)
    previous_review_keys = set(context.review_snapshot.keys)
    protected_workspace_blocked = list(context.review_snapshot.protected_workspace_blocked)
    task = context.options.task
    normalized_roles = dict(context.options.file_roles)
    normalized_protected_toc_titles = list(context.options.protected_toc_titles)
    normalized_continuation_toc_titles = context.options.continuation_toc_titles
    discarded_temp_files = begin_validation_io(context)
    audit = audit_markdown_files(context)
    missing = audit.missing
    invalid = audit.invalid
    safety_blocked = list(audit.safety_blocked)
    if protected_workspace_blocked:
        safety_blocked.append(
            "protected workspace changed during Subagent hand-off: "
            + ", ".join(protected_workspace_blocked)
        )
    valid_files = audit.valid_files
    source_sha256 = audit.source_sha256
    target_sha256 = audit.target_sha256
    normalized_files = audit.normalized_files
    normalized_targets = audit.normalized_targets
    reference_heading_fixes = audit.reference_heading_fixes
    diff_summary = audit.diff_summary
    structural_warnings = audit.structural_warnings
    target_language_audits = audit.target_language_audits
    target_language_blocked = audit.target_language_blocked
    polish_page_furniture_warnings = audit.polish_page_furniture_warnings
    polish_toc_label_exemptions = audit.polish_toc_label_exemptions
    polish_content_integrity = audit.polish_content_integrity
    polish_structure_audits = audit.polish_structure_audits
    review_required = audit.review_required
    bilingual_warnings = audit.bilingual_warnings
    normalized_roles = audit.file_roles

    extras = [] if partial else sorted(
        path.name for path in markdown_files(target_dir)
        if path.name not in {p.name for p in sources}
    )
    if extras:
        invalid.extend(
            {"file": name, "reason": "unexpected extra target file"}
            for name in extras
        )
    review_state = resolve_review_state(
        review_required, missing, invalid, previous_review_files, previous_review_keys
    )
    review_required_files = list(review_state.review_required_files)
    human_review_required = list(review_state.human_review_required)
    retry_required = list(review_state.retry_required)
    retry_required_files = list(review_state.retry_required_files)
    human_review_required_files = list(review_state.human_review_required_files)
    if not allow_review_warnings:
        for name in review_required_files:
            if name in valid_files:
                valid_files.remove(name)
    if protected_workspace_blocked:
        # A guard failure is never allowed to appear as a successful file checkpoint.
        valid_files.clear()
    invalid_names = {str(item.get("file")) for item in invalid if item.get("file")}
    target_hashes = stage_normalized_and_validated_targets(
        context, normalized_targets, sources, invalid_names, set(review_required_files),
        missing=missing, invalid=invalid, safety_blocked=safety_blocked,
        review_required=review_required,
        protected_workspace_blocked=bool(protected_workspace_blocked),
    )
    target_sha256.update(target_hashes)

    report = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "task": task,
        "total": len(sources),
        "completed": len(sources) - len(missing),
        "missing": missing,
        "invalid": invalid,
        "safety_blocked": safety_blocked,
        "protected_workspace_blocked": protected_workspace_blocked,
        "target_language_blocked": target_language_blocked,
        "target_language_audits": target_language_audits,
        "bilingual_warnings": bilingual_warnings,
        "polish_page_furniture_warnings": polish_page_furniture_warnings,
        "polish_toc_label_exemptions": polish_toc_label_exemptions,
        "polish_content_integrity": polish_content_integrity,
        "polish_structure_audits": polish_structure_audits,
        "review_required": review_required,
        "review_required_files": review_required_files,
        "retry_required": retry_required,
        "retry_required_files": retry_required_files,
        "human_review_required": human_review_required,
        "human_review_required_files": human_review_required_files,
        "previous_review_files": sorted(previous_review_files),
        "allow_review_warnings": bool(allow_review_warnings),
        "review_warnings_acknowledged": bool(
            allow_review_warnings and review_required
        ),
        "normalized_files": normalized_files,
        "reference_heading_fixes": reference_heading_fixes,
        "structural_warnings": structural_warnings,
        "diff_summary": diff_summary,
        "file_roles": normalized_roles,
        "protected_toc_titles": normalized_protected_toc_titles,
        "continuation_toc_titles": {
            name: sorted(titles)
            for name, titles in normalized_continuation_toc_titles.items()
        },
        "extra": extras,
        "valid_files": valid_files,
        "source_sha256": source_sha256,
        "target_sha256": target_sha256,
        "validated_dir": str(validated_dir),
        "scope": "files" if partial else "full",
        "files_checked": [source.name for source in sources],
        "all_passed": (
            bool(sources)
            and not missing
            and not invalid
            and not safety_blocked
            and not extras
            and (allow_review_warnings or not review_required)
        ),
    }
    return persist_validation_report(
        context,
        report,
        discarded_temp_files=discarded_temp_files,
        polish_content_integrity=polish_content_integrity,
        polish_structure_audits=polish_structure_audits,
    )

__all__ = ["validate_markdown_subagent"]

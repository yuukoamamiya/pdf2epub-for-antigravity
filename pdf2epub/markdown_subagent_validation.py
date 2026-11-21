"""Markdown Subagent output validation and staging."""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .batching_service import markdown_files
from .markdown_validation_context import load_markdown_validation_context
from .markdown_validation_audit import audit_markdown_files
from .markdown_validation_state import resolve_review_state
from .review_escalation import load_review_escalations, record_review_signals
from .markdown_validation_persistence import (
    begin_validation_io,
    capture_pdf_resume_contexts,
    validation_snapshot_changes,
    stage_normalized_and_validated_targets,
    persist_validation_report,
)
from .workflow_contracts import markdown_validation_schema_version


# Changing the warning gate is a validation-contract change.  Older reports
# were allowed to stage files while carrying advisory warnings, so callers
# must not treat a pre-v2 report as proof that the new gate was evaluated.
def validate_markdown_subagent(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    structural_patterns: Iterable[str] = (),
    create_validated_copy: bool = True,
    file_roles: Optional[Mapping[str, str]] = None,
    file_role_sources: Optional[Mapping[str, Mapping[str, Any]]] = None,
    validate_footnote_normalization: bool = False,
    fix_reference_headings: bool = False,
    selected_files: Optional[Iterable[str]] = None,
    target_language: Optional[str] = None,
    allow_review_warnings: bool = False,
    human_decision_id: Optional[str] = None,
    protected_toc_titles: Optional[Iterable[str]] = None,
    continuation_toc_titles: Optional[
        Mapping[str, Iterable[str]]
    ] = None,
    dynamic_terminology_config: Optional[Mapping[str, Any]] = None,
    require_footnote_semantic_review: bool = False,
) -> Dict:
    """Validate a Subagent markdown hand-off and optionally stage it."""
    context = load_markdown_validation_context(
        output_dir, task, source_dir, target_dir,
        structural_patterns=structural_patterns,
        create_validated_copy=create_validated_copy,
        file_roles=file_roles,
        file_role_sources=file_role_sources,
        validate_footnote_normalization=validate_footnote_normalization,
        fix_reference_headings=fix_reference_headings,
        selected_files=selected_files,
        target_language=target_language,
        allow_review_warnings=allow_review_warnings,
        human_decision_id=human_decision_id,
        protected_toc_titles=protected_toc_titles or (),
        continuation_toc_titles=continuation_toc_titles,
        require_footnote_semantic_review=require_footnote_semantic_review,
    )
    target_dir = context.paths.target_dir
    validated_dir = context.paths.validated_dir
    sources = context.sources
    partial = context.partial
    allow_review_warnings = context.allow_review_warnings
    human_decision_id = context.human_decision_id
    previous_review_files = set(context.review_snapshot.files)
    previous_review_files.update(
        filename
        for (signal_task, filename, _kind) in load_review_escalations(context.paths.output_dir)
        if signal_task == task
    )
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

    semantic_reviews: dict[str, Any] = {}
    semantic_blocked_names: set[str] = set()
    reflow_state: dict[str, Any] = {}
    if task == "polish" and context.options.require_footnote_semantic_review:
        from .polish_footnote_semantics import (
            clear_reflow_round,
            polish_issue_task_id,
            record_reflow_round,
            validate_prepared_file_review,
        )

        for source in sources:
            name = source.name
            try:
                source_text = source.read_bytes().decode("utf-8")
                target_text = audit.normalized_targets.get(name)
                if target_text is None:
                    target_text = (target_dir / name).read_bytes().decode("utf-8")
            except (OSError, UnicodeError) as exc:
                result = {"valid": False, "contract_valid": False, "status": "missing", "errors": [f"source or target unavailable: {exc}"], "issues": []}
            else:
                result = validate_prepared_file_review(
                    context.paths.output_dir, name, source_text=source_text,
                    target_text=target_text, required=True,
                ) or {"valid": False, "contract_valid": False, "status": "missing", "errors": ["semantic review unavailable"], "issues": []}
                if result.get("contract_valid") is True and result.get("status") == "issues":
                    reflow_state[name] = record_reflow_round(
                        context.paths.output_dir,
                        name,
                        source_sha256=str(result.get("source_sha256") or ""),
                        task_ids=[
                            polish_issue_task_id(name, issue)
                            for issue in result.get("issues", [])
                            if isinstance(issue, Mapping)
                        ],
                    )
                elif result.get("valid") is True:
                    clear_reflow_round(context.paths.output_dir, name)
            semantic_reviews[name] = result
            if not result.get("valid"):
                semantic_blocked_names.add(name)
                valid_files = [item for item in valid_files if item != name]
                semantic_issue = result.get("contract_valid") is True and result.get("status") == "issues"
                invalid.append({
                    "file": name,
                    "kind": "polish_footnote_semantics",
                    "action": "footnote_prepare_validate_apply" if semantic_issue else "refresh_polish_semantic_review",
                    "reason": "; ".join(result.get("errors", [])) or "semantic issues were reported; preserve this polish output and return to the footnote workflow",
                    "status": result.get("status"),
                    "issues": result.get("issues", []),
                    "recovery": "footnote-prepare -> footnote-validate -> footnote-apply" if semantic_issue else "rerun the assigned Polish semantic review and refresh its sidecar",
                })

    resume_contexts, stale_contexts = capture_pdf_resume_contexts(
        context.paths.output_dir, task, [source.name for source in sources],
        # Read-only worker self-checks verify only this unit's own context
        # pointer. Formal single-file checkpoints keep the boundary guard.
        scoped=partial and not context.create_validated_copy,
    )
    for name, reason in stale_contexts.items():
        invalid.append({
            "file": name,
            "kind": "prepared_context",
            "action": "refresh_context_and_revalidate",
            "reason": reason,
        })
        valid_files = [item for item in valid_files if item != name]

    dynamic_report: dict[str, Any] = {"enabled": False, "valid": True, "files": {}, "errors": []}
    dynamic_blocked_names: set[str] = set()
    if task == "translate" and dynamic_terminology_config is not None:
        try:
            from .terminology_memory_validation import validate_dynamic_terminology_units

            dynamic_report = validate_dynamic_terminology_units(
                context.paths.output_dir,
                task=task,
                source_dir=context.paths.source_dir,
                target_dir=context.paths.target_dir,
                config=dynamic_terminology_config,
                selected_files=[path.name for path in sources] if partial else None,
            )
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            dynamic_report = {
                "enabled": True, "valid": False, "all_passed": False,
                "files": {}, "errors": [{"file": "<dynamic-terminology>", "error": str(exc)}],
            }
        if dynamic_report.get("enabled") and not dynamic_report.get("valid"):
            dynamic_blocked_names = {
                str(name) for name, item in dynamic_report.get("files", {}).items()
                if isinstance(item, Mapping) and item.get("valid") is False
            }
            if any(
                not isinstance(item, Mapping) or item.get("file") in {None, "<dynamic-terminology>", "<source-inventory>"}
                for item in dynamic_report.get("errors", [])
            ):
                dynamic_blocked_names.update(path.name for path in sources)
            valid_files = [name for name in valid_files if name not in dynamic_blocked_names]
            for name in dynamic_blocked_names:
                normalized_targets.pop(name, None)

    extras = [] if partial else sorted(
        path.name for path in markdown_files(target_dir)
        if path.name not in {p.name for p in sources}
    )
    if extras:
        invalid.extend(
            {"file": name, "reason": "unexpected extra target file"}
            for name in extras
        )
    if allow_review_warnings and not str(human_decision_id or "").strip():
        raise ValueError(
            "acknowledging review warnings requires an explicit --human-decision-id"
        )
    review_signals = [
        {
            "file": str(item.get("file") or ""),
            "kind": str(item.get("kind") or ""),
            "evidence_hash": str(
                target_sha256.get(str(item.get("file") or ""))
                or source_sha256.get(str(item.get("file") or ""))
                or ""
            ),
        }
        for item in review_required
        if isinstance(item, Mapping) and item.get("file") and item.get("kind")
    ]
    # A read-only self-check must not advance the persisted retry budget.
    persist_escalation = not (context.partial and not context.create_validated_copy)
    if persist_escalation:
        recorded = record_review_signals(
            context.paths.output_dir,
            task=task,
            signals=review_signals,
            scope_files=[source.name for source in sources],
            human_decision_id=human_decision_id,
            acknowledge=bool(allow_review_warnings),
        )
        escalation_rows = [
            {
                "signal_id": item.signal_id,
                "file": item.file,
                "kind": item.kind,
                "state": item.state,
                "retry_count": item.retry_count,
                "human_decision_id": item.human_decision_id,
            }
            for item in recorded.values()
        ]
    else:
        prior = load_review_escalations(context.paths.output_dir)
        escalation_rows = [
            {
                "signal_id": item.signal_id,
                "file": item.file,
                "kind": item.kind,
                "state": item.state,
                "retry_count": item.retry_count,
                "human_decision_id": item.human_decision_id,
            }
            for (signal_task, _name, _kind), item in prior.items()
            if signal_task == task
            and any(
                signal["file"] == item.file and signal["kind"] == item.kind
                for signal in review_signals
            )
        ]
    review_state = resolve_review_state(
        review_required, missing, invalid, escalation_rows
    )
    review_required_files = list(review_state.review_required_files)
    human_review_required = list(review_state.human_review_required)
    retry_required = list(review_state.retry_required)
    retry_required_files = list(review_state.retry_required_files)
    human_review_required_files = list(review_state.human_review_required_files)
    reflow_escalated = {
        name: entry for name, entry in reflow_state.items()
        if isinstance(entry, Mapping) and entry.get("escalated") is True
    }
    if reflow_escalated:
        escalated_names = set(reflow_escalated)
        retry_required = [
            item for item in retry_required
            if str(item.get("file") or "") not in escalated_names
        ]
        retry_required_files = [
            name for name in retry_required_files if name not in escalated_names
        ]
        already_human = {
            str(item.get("file") or "") for item in human_review_required
            if isinstance(item, Mapping)
        }
        for name, entry in sorted(reflow_escalated.items()):
            for item in invalid:
                if str(item.get("file") or "") == name and item.get("kind") == "polish_footnote_semantics":
                    item["action"] = "ask_human"
            if name in already_human:
                continue
            human_review_required.append({
                "file": name,
                "kind": "polish_footnote_reflow",
                "action": "ask_human",
                "rounds": int(entry.get("rounds") or 0),
                "max_rounds": int(entry.get("max_rounds") or 0) or None,
                "reason": (
                    "the polish->footnote reflow already exceeded its bounded rounds; "
                    "automatic prepare/validate/polish retries are paused"
                ),
            })
            human_review_required_files.append(name)
        human_review_required_files = sorted(set(human_review_required_files))
    if not allow_review_warnings:
        for name in review_required_files:
            if name in valid_files:
                valid_files.remove(name)
    if protected_workspace_blocked:
        # A guard failure is never allowed to appear as a successful file checkpoint.
        valid_files.clear()
    snapshot_changes = validation_snapshot_changes(
        context.paths.output_dir, task, context.paths.source_dir, target_dir,
        source_sha256, audit.target_sha256, semantic_reviews,
        target_texts=normalized_targets,
    ) if context.create_validated_copy else {}
    for name, (changed_kind, reason) in snapshot_changes.items():
        valid_files = [item for item in valid_files if item != name]
        normalized_targets.pop(name, None)
        if changed_kind == "semantic_sidecar":
            action = "refresh_polish_semantic_review"
        elif changed_kind == "target":
            action = "revalidate_current_target"
        else:
            action = "refresh_context_and_revalidate"
        finding = {
            "file": name,
            "kind": "validation_snapshot_changed",
            "action": action,
            "reason": reason,
        }
        invalid.append(finding)
        retry_required.append(dict(finding))
        if name not in retry_required_files:
            retry_required_files.append(name)
    invalid_names = {str(item.get("file")) for item in invalid if item.get("file")}
    target_hashes = stage_normalized_and_validated_targets(
        context, normalized_targets, sources, invalid_names, set(review_required_files),
        missing=missing, invalid=invalid, safety_blocked=safety_blocked,
        review_required=review_required,
        protected_workspace_blocked=bool(protected_workspace_blocked),
        hard_blocked_names=dynamic_blocked_names | semantic_blocked_names,
    )
    target_sha256.update(target_hashes)

    report = {
        "schema_version": markdown_validation_schema_version(task),
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
        "polish_footnote_semantic_reviews": semantic_reviews,
        "polish_footnote_semantic_review_sha256": {
            name: item.get("sidecar_sha256")
            for name, item in semantic_reviews.items()
            if isinstance(item, Mapping) and item.get("sidecar_sha256")
        },
        "polish_footnote_reflow": reflow_state,
        "review_required": review_required,
        "review_required_files": review_required_files,
        "retry_required": retry_required,
        "retry_required_files": retry_required_files,
        "human_review_required": human_review_required,
        "human_review_required_files": human_review_required_files,
        "previous_review_files": sorted(previous_review_files),
        "allow_review_warnings": bool(allow_review_warnings),
        "human_decision_id": str(human_decision_id).strip() or None,
        "review_escalation": escalation_rows,
        "review_warnings_acknowledged": bool(
            allow_review_warnings and review_required
        ),
        "normalized_files": normalized_files,
        "reference_heading_fixes": reference_heading_fixes,
        "structural_warnings": structural_warnings,
        "diff_summary": diff_summary,
        "file_roles": normalized_roles,
        "file_role_sources": dict(context.options.file_role_sources),
        "protected_toc_titles": normalized_protected_toc_titles,
        "continuation_toc_titles": {
            name: sorted(titles)
            for name, titles in normalized_continuation_toc_titles.items()
        },
        "extra": extras,
        "valid_files": valid_files,
        "dynamic_terminology": dynamic_report,
        "source_sha256": source_sha256,
        "target_sha256": target_sha256,
        "resume_contexts": {
            name: snapshot for name, snapshot in resume_contexts.items()
            if name in valid_files
        },
        "validated_dir": str(validated_dir),
        "scope": "files" if partial else "full",
        "files_checked": [source.name for source in sources],
        "all_passed": (
            bool(sources)
            and not missing
            and not invalid
            and not safety_blocked
            and not extras
            and (not dynamic_report.get("enabled") or dynamic_report.get("valid", False))
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

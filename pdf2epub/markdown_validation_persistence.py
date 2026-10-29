"""Filesystem effects for formal Markdown validation runs."""
from __future__ import annotations

import hashlib
import json
import shutil
from pathlib import Path
from typing import Any, Mapping

from .markdown_validation_checkpoint import discard_abandoned_temp_targets, quarantine_invalid_targets
from .markdown_validation_context import MarkdownValidationContext
from .workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION, atomic_write_json, atomic_write_text


VALIDATION_SCHEMA_VERSION = MARKDOWN_VALIDATION_SCHEMA_VERSION


def begin_validation_io(context: MarkdownValidationContext) -> list[str]:
    """Clean only current assigned temp siblings and invalidate stale full staging."""
    if not context.create_validated_copy:
        return []
    if context.paths.validated_dir.exists() and not context.partial:
        shutil.rmtree(context.paths.validated_dir)
    return discard_abandoned_temp_targets(
        context.paths.target_dir, [source.name for source in context.sources]
    )


def stage_normalized_and_validated_targets(
    context: MarkdownValidationContext,
    normalized_targets: Mapping[str, str],
    sources: tuple[Path, ...],
    invalid_names: set[str],
    review_names: set[str],
    *,
    missing: list[str],
    invalid: list[dict[str, Any]],
    safety_blocked: list[str],
    review_required: list[dict[str, Any]],
    protected_workspace_blocked: bool,
) -> dict[str, str]:
    """Apply normalization plans, then stage only a passing current scope."""
    if not context.create_validated_copy:
        return {}
    target_dir, validated_dir = context.paths.target_dir, context.paths.validated_dir
    target_hashes: dict[str, str] = {}
    for name, text in normalized_targets.items():
        atomic_write_text(target_dir / name, text)
        target_hashes[name] = hashlib.sha256(text.encode("utf-8")).hexdigest()
    if context.partial:
        remove_names = set(invalid_names) | set(missing)
        if protected_workspace_blocked:
            # A changed protected workspace invalidates every checkpoint in this
            # submitted scope, even when the Markdown itself passed its audit.
            remove_names.update(source.name for source in sources)
        if not context.allow_review_warnings:
            remove_names.update(review_names)
        for name in remove_names:
            (validated_dir / name).unlink(missing_ok=True)
    if not missing and not invalid and not safety_blocked and (
        context.allow_review_warnings or not review_required
    ):
        validated_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            shutil.copy2(target_dir / source.name, validated_dir / source.name)
    return target_hashes


def persist_validation_report(
    context: MarkdownValidationContext,
    report: dict[str, Any],
    *,
    discarded_temp_files: list[str],
    polish_content_integrity: Mapping[str, Any],
    polish_structure_audits: Mapping[str, Any],
) -> dict[str, Any]:
    """Merge partial checkpoints or write the full report, then quarantine failures."""
    if not context.create_validated_copy:
        report["discarded_temp_files"] = []
        report["quarantined_files"] = {}
        return report
    report_path = context.paths.report_path
    partial = context.partial
    ledger: dict[str, Any] | None = None
    if partial:
        existing: dict[str, Any] = {}
        if report_path.is_file():
            try:
                loaded = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                pass
        records = existing.get("files", {})
        if not isinstance(records, dict):
            records = {}
        for name in report["files_checked"]:
            records[name] = {
                "schema_version": VALIDATION_SCHEMA_VERSION,
                "valid": name in report["valid_files"] and name not in report["missing"],
                "source_sha256": report["source_sha256"].get(name),
                "target_sha256": report["target_sha256"].get(name),
                "invalid": [item for item in report["invalid"] if item.get("file") == name],
                "safety_blocked": name in report["safety_blocked"],
                "target_language_blocked": name in report["target_language_blocked"],
                "target_language_audit": report["target_language_audits"].get(name),
                "review_required": [item for item in report["review_required"] if item.get("file") == name],
                "retry_required": [item for item in report["retry_required"] if item.get("file") == name],
                "human_review_required": [item for item in report["human_review_required"] if item.get("file") == name],
                "review_warnings_acknowledged": bool(
                    report.get("allow_review_warnings")
                    and any(item.get("file") == name for item in report["review_required"])
                ),
                "polish_content_integrity": polish_content_integrity.get(name),
                "polish_structure_audit": polish_structure_audits.get(name),
            }
        ledger = {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "task": report["task"],
            "scope": "file-checkpoints",
            "files": records,
        }
        atomic_write_json(report_path, ledger)
    else:
        atomic_write_json(report_path, report)

    invalid_names = {
        str(item.get("file"))
        for item in report["invalid"]
        if isinstance(item, Mapping) and item.get("file")
    }
    quarantined = quarantine_invalid_targets(
        context.paths.output_dir, report["task"], context.paths.target_dir, invalid_names
    )
    report["discarded_temp_files"] = discarded_temp_files
    report["quarantined_files"] = quarantined
    if ledger is not None:
        ledger["discarded_temp_files"] = discarded_temp_files
        ledger["quarantined_files"] = quarantined
        atomic_write_json(report_path, ledger)
    else:
        atomic_write_json(report_path, report)
    return report

"""Filesystem effects for formal Markdown validation runs."""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from pathlib import Path
from typing import Any, Mapping

from .markdown_validation_checkpoint import discard_abandoned_temp_targets, quarantine_invalid_targets
from .markdown_validation_context import MarkdownValidationContext
from .workflow_contracts import markdown_validation_schema_version, atomic_write_json, atomic_write_text


def capture_pdf_resume_contexts(output_dir: Path, task: str, filenames: list[str]) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Capture per-file PDF context receipts and reject stale prepared inputs."""
    if task not in {"translate", "polish"}:
        return {}, {}
    manifest_path = output_dir / f"{task}_subagent_manifest.json"
    if not manifest_path.exists():
        # Validation is also a public standalone API. With no prepared manifest
        # there is no declared PDF context to snapshot or validate.
        return {}, {}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, {name: "prepared manifest is unreadable" for name in filenames}
    if not isinstance(manifest, Mapping):
        return {}, {name: "prepared manifest is malformed" for name in filenames}
    context_inventory_keys = (
        "unit_context_files", "unit_context_sha256", "toc_heading_contexts",
        "special_unit_modes",
    )
    has_declared_context = any(key in manifest for key in context_inventory_keys)
    if manifest.get("task") != task:
        if not has_declared_context:
            return {}, {}
        return {}, {name: "prepared manifest task does not match validation" for name in filenames}
    if not all(isinstance(manifest.get(key), str) and manifest.get(key) for key in ("source_language", "target_language")):
        if not has_declared_context:
            return {}, {}
        return {}, {name: "prepared manifest language context is malformed" for name in filenames}

    root = output_dir.resolve()
    file_contexts = manifest.get("unit_context_files", {})
    unit_hashes = manifest.get("unit_context_sha256", {})
    heading_contexts = manifest.get("toc_heading_contexts", {})
    special_modes = manifest.get("special_unit_modes", {})
    roles = manifest.get("file_roles", {})
    role_sources = manifest.get("file_role_sources", {})
    raw_maps = (file_contexts, unit_hashes, heading_contexts, special_modes, roles, role_sources)
    bad_map = any(value is not None and not isinstance(value, Mapping) for value in raw_maps)
    if bad_map:
        return {}, {name: "prepared manifest context inventory is malformed" for name in filenames}
    maps = tuple(value if isinstance(value, Mapping) else {} for value in raw_maps)
    file_contexts, unit_hashes, heading_contexts, special_modes, roles, role_sources = maps
    snapshots: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for name in filenames:
        expected = unit_hashes.get(name)
        relative = file_contexts.get(name)
        if expected is not None:
            if not isinstance(expected, str) or not isinstance(relative, str):
                errors[name] = "prepared unit context hash or path is malformed"
                continue
            if len(expected) != 64 or any(char not in "0123456789abcdefABCDEF" for char in expected):
                errors[name] = "prepared unit context hash is malformed"
                continue
            candidate = Path(relative)
            candidate = candidate if candidate.is_absolute() else root / candidate
            path = candidate.resolve()
            if path != root and root not in path.parents:
                errors[name] = "prepared unit context path escapes output workspace"
                continue
            try:
                actual = hashlib.sha256(path.read_bytes()).hexdigest()
            except OSError as exc:
                errors[name] = f"prepared unit context cannot be read: {exc}"
                continue
            if actual != expected:
                errors[name] = "prepared unit context changed after preparation"
                continue
        elif relative is not None:
            errors[name] = "prepared unit context has no declared hash"
            continue
        snapshot: dict[str, Any] = {
            "unit_context_sha256": ({name: expected} if expected is not None else {}),
            "toc_heading_contexts": ({name: heading_contexts[name]} if name in heading_contexts else {}),
            "context_sha256": manifest.get("context_sha256", {}),
            "special_unit_modes": ({name: special_modes[name]} if name in special_modes else {}),
            "file_roles": ({name: roles[name]} if name in roles else {}),
            "file_role_sources": ({name: role_sources[name]} if name in role_sources else {}),
            "source_language": manifest.get("source_language"),
            "target_language": manifest.get("target_language"),
        }
        snapshots[name] = snapshot
    return snapshots, errors


def _archive_superseded_partial_checkpoint(output_dir: Path, task: str) -> dict[str, str] | None:
    """Preserve the exact prior ledger bytes before a full PDF report replaces it."""
    ledger_path = Path(output_dir) / f"{task}_file_validation.json"
    if not ledger_path.is_file():
        return None
    payload = ledger_path.read_bytes()
    digest = hashlib.sha256(payload).hexdigest()
    root = Path(output_dir).resolve()
    directory = root / "scratch" / task / "superseded_checkpoints"
    directory.mkdir(parents=True, exist_ok=True)
    resolved_directory = directory.resolve()
    if resolved_directory != root and root not in resolved_directory.parents:
        raise ValueError("superseded checkpoint archive escapes output workspace")
    candidate = resolved_directory / f"{digest}.json"
    suffix = 0
    while True:
        if candidate.exists():
            if candidate.read_bytes() == payload:
                break
            suffix += 1
            candidate = resolved_directory / f"{digest}.{suffix}.json"
            continue
        try:
            with candidate.open("xb") as handle:
                handle.write(payload)
                handle.flush()
                os.fsync(handle.fileno())
            break
        except FileExistsError:
            continue
    return {
        "path": candidate.relative_to(root).as_posix(),
        "sha256": digest,
    }


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
    hard_blocked_names: set[str] | None = None,
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
        remove_names = set(invalid_names) | set(missing) | set(hard_blocked_names or ())
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
    ) and not hard_blocked_names:
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
                "schema_version": markdown_validation_schema_version(report["task"]),
                "valid": name in report["valid_files"] and name not in report["missing"],
                "source_sha256": report["source_sha256"].get(name),
                "target_sha256": report["target_sha256"].get(name),
                "file_role": report.get("file_roles", {}).get(name),
                "file_role_source": report.get("file_role_sources", {}).get(name),
                "invalid": [item for item in report["invalid"] if item.get("file") == name],
                "safety_blocked": name in report["safety_blocked"],
                "target_language_blocked": name in report["target_language_blocked"],
                "target_language_audit": report["target_language_audits"].get(name),
                "review_required": [item for item in report["review_required"] if item.get("file") == name],
                "retry_required": [item for item in report["retry_required"] if item.get("file") == name],
                "human_review_required": [item for item in report["human_review_required"] if item.get("file") == name],
                "dynamic_terminology": report.get("dynamic_terminology", {}).get("files", {}).get(name),
                "dynamic_unit_context_sha256": report.get("dynamic_terminology", {}).get("prepared_unit_context_sha256", {}).get(name),
                "review_warnings_acknowledged": bool(
                    report.get("allow_review_warnings")
                    and any(item.get("file") == name for item in report["review_required"])
                ),
                "polish_content_integrity": polish_content_integrity.get(name),
                "polish_structure_audit": polish_structure_audits.get(name),
                "resume_context": report.get("resume_contexts", {}).get(name),
            }
        ledger = {
            "schema_version": markdown_validation_schema_version(report["task"]),
            "task": report["task"],
            "scope": "file-checkpoints",
            "files": records,
        }
        atomic_write_json(report_path, ledger)
    else:
        if report["task"] in {"translate", "polish"}:
            # A full PDF validation covers every source unit, so any older
            # partial ledger entries are archived before the complete report
            # supersedes them as the active checkpoint.
            archive = _archive_superseded_partial_checkpoint(
                context.paths.output_dir, report["task"]
            )
            if archive is not None:
                report["superseded_partial_checkpoint_archive"] = archive
            (context.paths.output_dir / f"{report['task']}_file_validation.json").unlink(missing_ok=True)
        atomic_write_json(report_path, report)

    invalid_names = {
        str(item.get("file"))
        for item in report["invalid"]
        if isinstance(item, Mapping) and item.get("file")
    }
    errors_by_file: dict[str, list[Mapping[str, Any]]] = {}
    for item in report["invalid"]:
        if isinstance(item, Mapping) and item.get("file"):
            errors_by_file.setdefault(str(item["file"]), []).append(item)
    stale_context_names = {
        name for name, items in errors_by_file.items()
        if items and all(item.get("kind") == "prepared_context" for item in items)
    }
    quarantined = quarantine_invalid_targets(
        context.paths.output_dir, report["task"], context.paths.target_dir,
        invalid_names - stale_context_names,
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

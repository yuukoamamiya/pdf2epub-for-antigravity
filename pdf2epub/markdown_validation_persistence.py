"""Filesystem effects for formal Markdown validation runs."""
from __future__ import annotations

import hashlib
from pdf2epub.workflow_contracts import sha256_file
import json
import os
import shutil
import tempfile
from pathlib import Path
from typing import Any, Mapping

from .markdown_validation_checkpoint import discard_abandoned_temp_targets, quarantine_invalid_targets
from .markdown_validation_context import MarkdownValidationContext
from .workflow_contracts import markdown_validation_schema_version, atomic_write_json, atomic_write_text


def validation_snapshot_changes(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    source_hashes: Mapping[str, str],
    target_hashes: Mapping[str, str],
    semantic_reviews: Mapping[str, Any] | None = None,
    *,
    target_texts: Mapping[str, str] | None = None,
) -> dict[str, tuple[str, str]]:
    """Find inputs that changed after their validation audit snapshot."""
    changes: dict[str, tuple[str, str]] = {}
    semantic_reviews = semantic_reviews or {}
    target_texts = target_texts or {}
    for name, expected in source_hashes.items():
        try:
            actual = sha256_file((Path(source_dir) / name))
        except OSError:
            changes[name] = ("source", "source file became unavailable during validation")
            continue
        if actual != expected:
            changes[name] = ("source", "source file changed during validation")
    for name, expected in target_hashes.items():
        try:
            target_path = Path(target_dir) / name
            raw_target = target_path.read_bytes()
        except OSError:
            changes.setdefault(name, ("target", "target file became unavailable during validation"))
            continue
        actual = hashlib.sha256(raw_target).hexdigest()
        if actual != expected:
            changes.setdefault(name, ("target", "target file changed during validation"))
    if task != "polish":
        return changes

    semantic_hashes = {}
    for name, review in semantic_reviews.items():
        if isinstance(review, Mapping) and review.get("sidecar_sha256"):
            semantic_hashes[name] = review["sidecar_sha256"]
    if not semantic_hashes:
        return changes
    from .polish_footnote_semantics import validate_prepared_file_review

    for name, expected in semantic_hashes.items():
        original_review = semantic_reviews.get(name)
        try:
            source_text = (Path(source_dir) / name).read_bytes().decode("utf-8")
            target_text = target_texts.get(name)
            if target_text is None:
                target_text = (Path(target_dir) / name).read_bytes().decode("utf-8")
        except (OSError, UnicodeError):
            changes.setdefault(name, ("semantic_sidecar", "source or target became unavailable during semantic review"))
            continue
        current = validate_prepared_file_review(
            output_dir, name, source_text=source_text,
            target_text=target_text, required=True,
        )
        original_contract_valid = (
            isinstance(original_review, Mapping)
            and original_review.get("contract_valid") is True
        )
        original_valid = (
            isinstance(original_review, Mapping)
            and original_review.get("valid") is True
        )
        if (
            not isinstance(current, Mapping)
            or current.get("sidecar_sha256") != expected
            or (original_contract_valid and current.get("contract_valid") is not True)
            or (original_valid and current.get("valid") is not True)
        ):
            changes.setdefault(name, ("semantic_sidecar", "semantic review sidecar changed during validation"))
    return changes


def _atomic_copy(source: Path, target: Path) -> None:
    """Copy exact bytes to a sibling temporary file, then atomically replace."""
    target.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        prefix=f".{target.name}.", suffix=".tmp", dir=target.parent
    )
    try:
        with os.fdopen(descriptor, "wb") as output, Path(source).open("rb") as input_file:
            shutil.copyfileobj(input_file, output)
            output.flush()
            os.fsync(output.fileno())
        os.replace(temporary_name, target)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except OSError:
            pass
        raise


def _record_snapshot_changes(
    report: dict[str, Any], changes: Mapping[str, tuple[str, str]]
) -> set[str]:
    """Downgrade files whose audited inputs changed before checkpoint commit."""
    recorded: set[str] = set()
    for name, (changed_kind, reason) in changes.items():
        recorded.add(name)
        action = (
            "refresh_polish_semantic_review" if changed_kind == "semantic_sidecar"
            else "revalidate_current_target" if changed_kind == "target"
            else "refresh_context_and_revalidate"
        )
        finding = {
            "file": name,
            "kind": "validation_snapshot_changed",
            "action": action,
            "reason": reason,
        }
        if not any(
            isinstance(item, Mapping)
            and item.get("file") == name
            and item.get("kind") == "validation_snapshot_changed"
            for item in report.get("invalid", [])
        ):
            report.setdefault("invalid", []).append(finding)
        report["valid_files"] = [
            item for item in report.get("valid_files", []) if item != name
        ]
        if not any(
            isinstance(item, Mapping)
            and item.get("file") == name
            and item.get("kind") == "validation_snapshot_changed"
            for item in report.get("retry_required", [])
        ):
            report.setdefault("retry_required", []).append(dict(finding))
        retry_files = report.setdefault("retry_required_files", [])
        if name not in retry_files:
            retry_files.append(name)
    if recorded:
        report["all_passed"] = False
    return recorded


def capture_pdf_resume_contexts(
    output_dir: Path,
    task: str,
    filenames: list[str],
    *,
    scoped: bool = False,
) -> tuple[dict[str, dict[str, Any]], dict[str, str]]:
    """Capture per-file PDF context receipts and reject stale prepared inputs.

    ``scoped`` is the read-only single-file self-check path.  It verifies
    only the named file's own prepared unit-context pointer; neighbor
    boundary sources and the book-wide refined mapping are book-level gates
    that a full validation (and a formal ``--file`` checkpoint) evaluates.
    """
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
        "special_unit_modes", "boundary_context_sha256",
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
    boundary_hashes = manifest.get("boundary_context_sha256", {})
    boundary_sources = manifest.get("boundary_context_sources", {})
    boundary_mapping_hashes = manifest.get("boundary_context_mapping_sha256", {})
    raw_maps = (file_contexts, unit_hashes, heading_contexts, special_modes, roles, role_sources, boundary_hashes)
    bad_map = any(value is not None and not isinstance(value, Mapping) for value in raw_maps)
    if bad_map:
        return {}, {name: "prepared manifest context inventory is malformed" for name in filenames}
    maps = tuple(value if isinstance(value, Mapping) else {} for value in raw_maps)
    file_contexts, unit_hashes, heading_contexts, special_modes, roles, role_sources, boundary_hashes = maps
    if boundary_sources is not None and not isinstance(boundary_sources, Mapping):
        return {}, {name: "prepared boundary context source inventory is malformed" for name in filenames}
    if boundary_mapping_hashes is not None and not isinstance(boundary_mapping_hashes, Mapping):
        return {}, {name: "prepared boundary mapping inventory is malformed" for name in filenames}
    current_pdf_continuations: Mapping[str, Any] | None = None
    current_pdf_mapping_error: str | None = None
    current_pdf_mapping = (
        root / "ocr_markdown" / "tree_progress.json"
    )
    has_boundary_context = any(name in boundary_hashes for name in filenames)
    mapping_source = manifest.get("boundary_context_mapping_source")
    should_load_refined_mapping = False
    if task == "polish" and has_boundary_context:
        if mapping_source == "tree_progress":
            if not current_pdf_mapping.is_file():
                current_pdf_mapping_error = "refined unit mapping is unavailable"
            else:
                should_load_refined_mapping = True
        elif mapping_source == "explicit":
            if current_pdf_mapping.is_file():
                current_pdf_mapping_error = "refined unit mapping appeared after explicit mapping preparation"
        elif mapping_source is None:
            current_pdf_mapping_error = "prepared boundary mapping source is missing; refresh context and revalidate"
        else:
            current_pdf_mapping_error = "prepared boundary mapping source is malformed"
    if task == "polish" and should_load_refined_mapping and not scoped:
        try:
            from .refine.unit_mapping import load_pdf_continuation_files

            source_dir_value = manifest.get("source_dir")
            source_root_value = Path(str(source_dir_value or ""))
            source_root_value = source_root_value if source_root_value.is_absolute() else root / source_root_value
            current_pdf_continuations = load_pdf_continuation_files(output_dir, source_root_value)
        except (OSError, ValueError, TypeError) as exc:
            current_pdf_mapping_error = str(exc) or "refined unit mapping is malformed"
    snapshots: dict[str, dict[str, Any]] = {}
    errors: dict[str, str] = {}
    for name in filenames:
        if not scoped and current_pdf_mapping_error is not None and name in boundary_hashes:
            errors[name] = f"prepared boundary mapping is unreadable: {current_pdf_mapping_error}"
            continue
        if not scoped and current_pdf_continuations is not None and name in boundary_hashes:
            from .markdown_handoff_planning import _boundary_mapping_projection

            mapping_value = _boundary_mapping_projection(name, current_pdf_continuations)
            mapping_payload = json.dumps(mapping_value, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
            current_mapping_hash = hashlib.sha256(mapping_payload.encode("utf-8")).hexdigest()
            expected_mapping_hash = boundary_mapping_hashes.get(name) if isinstance(boundary_mapping_hashes, Mapping) else None
            if current_mapping_hash != expected_mapping_hash:
                errors[name] = "prepared boundary mapping changed after preparation"
                continue
        links = boundary_sources.get(name, []) if isinstance(boundary_sources, Mapping) else []
        source_dir_value = manifest.get("source_dir")
        if links and not scoped:
            source_root_candidate = Path(str(source_dir_value or ""))
            source_root = source_root_candidate if source_root_candidate.is_absolute() else root / source_root_candidate
            source_root = source_root.resolve()
            if source_root != root and root not in source_root.parents:
                errors[name] = "prepared boundary context source directory escapes output workspace"
                continue
            stale_link = False
            for link in links:
                if not isinstance(link, Mapping):
                    stale_link = True
                    break
                neighbor_name = link.get("source_file")
                expected_neighbor_hash = link.get("source_sha256")
                if not isinstance(neighbor_name, str) or Path(neighbor_name).name != neighbor_name or not isinstance(expected_neighbor_hash, str):
                    stale_link = True
                    break
                try:
                    actual_neighbor_hash = sha256_file((source_root / neighbor_name))
                except OSError:
                    stale_link = True
                    break
                if actual_neighbor_hash != expected_neighbor_hash:
                    stale_link = True
                    break
            if stale_link:
                errors[name] = "prepared boundary context source changed after preparation"
                continue
        expected = None if task == "polish" else unit_hashes.get(name)
        relative = None if task == "polish" else file_contexts.get(name)
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
                actual = sha256_file(path)
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
            "boundary_context_sha256": ({name: boundary_hashes[name]} if name in boundary_hashes else {}),
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


def resume_context_snapshot_changes(
    output_dir: Path, task: str, expected_contexts: Mapping[str, Any]
) -> dict[str, tuple[str, str]]:
    """Detect prepared PDF context changes since the audit snapshot was captured."""
    if not expected_contexts:
        return {}
    current, errors = capture_pdf_resume_contexts(
        output_dir, task, list(expected_contexts)
    )
    changes: dict[str, tuple[str, str]] = {}
    for name, expected in expected_contexts.items():
        if name in errors:
            changes[name] = ("context", errors[name])
            continue
        actual = current.get(name)
        try:
            same = json.dumps(expected, ensure_ascii=False, sort_keys=True, separators=(",", ":")) == json.dumps(
                actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            )
        except (TypeError, ValueError):
            same = False
        if not same:
            changes[name] = ("context", "prepared validation context changed during validation")
    return changes


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
            _atomic_copy(target_dir / source.name, validated_dir / source.name)
    return target_hashes


def _sync_partial_polish_into_full_report(
    output_dir: Path,
    partial_report: Mapping[str, Any],
) -> None:
    """Merge single-file polish validation results into polish_validation.json.

    Keeping the primary polish_validation.json synchronized prevents status
    split-brain between full validation snapshots and --file checkpoints.
    """
    full_path = Path(output_dir) / "polish_validation.json"
    if not full_path.is_file():
        return
    try:
        full = json.loads(full_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if (
        not isinstance(full, dict)
        or full.get("task") != "polish"
        or full.get("schema_version") != markdown_validation_schema_version("polish")
    ):
        return
    checked = [str(name) for name in (partial_report.get("files_checked") or []) if name]
    if not checked:
        return
    checked_set = set(checked)

    valid_set = set(str(name) for name in (full.get("valid_files") or []))
    partial_valid = set(str(name) for name in (partial_report.get("valid_files") or []))
    partial_missing = set(str(name) for name in (partial_report.get("missing") or []))
    for name in checked:
        if name in partial_valid and name not in partial_missing:
            valid_set.add(name)
        else:
            valid_set.discard(name)
    full["valid_files"] = sorted(valid_set)

    for list_key in ("missing", "safety_blocked", "target_language_blocked"):
        existing_items = [str(item) for item in (full.get(list_key) or []) if str(item) not in checked_set]
        new_items = [str(item) for item in (partial_report.get(list_key) or []) if str(item) in checked_set]
        full[list_key] = sorted(set(existing_items) | set(new_items))

    for record_list_key in ("invalid", "review_required", "retry_required", "human_review_required"):
        kept = [
            item for item in (full.get(record_list_key) or [])
            if not (isinstance(item, Mapping) and str(item.get("file") or "") in checked_set)
        ]
        added = [
            dict(item) for item in (partial_report.get(record_list_key) or [])
            if isinstance(item, Mapping) and str(item.get("file") or "") in checked_set
        ]
        full[record_list_key] = kept + added

    for derived_key, src_key in (
        ("review_required_files", "review_required"),
        ("retry_required_files", "retry_required"),
        ("human_review_required_files", "human_review_required"),
    ):
        full[derived_key] = sorted({
            str(item.get("file"))
            for item in full.get(src_key, [])
            if isinstance(item, Mapping) and item.get("file")
        })

    for map_key in (
        "source_sha256",
        "target_sha256",
        "resume_contexts",
        "file_roles",
        "file_role_sources",
        "polish_footnote_semantic_reviews",
        "polish_footnote_semantic_review_sha256",
    ):
        existing_map = dict(full.get(map_key) or {}) if isinstance(full.get(map_key), Mapping) else {}
        partial_map = partial_report.get(map_key)
        if isinstance(partial_map, Mapping):
            for name in checked:
                if name in partial_map:
                    existing_map[name] = partial_map[name]
                else:
                    existing_map.pop(name, None)
        full[map_key] = existing_map

    total = int(full.get("total") or 0)
    allow_warnings = bool(full.get("allow_review_warnings") or partial_report.get("allow_review_warnings"))
    full["all_passed"] = bool(
        total > 0
        and len(full["valid_files"]) == total
        and not full.get("missing")
        and not full.get("invalid")
        and not full.get("safety_blocked")
        and not full.get("protected_workspace_blocked")
        and (allow_warnings or not full.get("review_required"))
    )
    atomic_write_json(full_path, full)


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
    snapshot_changes = validation_snapshot_changes(
        context.paths.output_dir,
        str(report.get("task") or ""),
        context.paths.source_dir,
        context.paths.target_dir,
        report.get("source_sha256", {}),
        report.get("target_sha256", {}),
        report.get("polish_footnote_semantic_reviews", {}),
    )
    snapshot_changes.update(resume_context_snapshot_changes(
        context.paths.output_dir,
        str(report.get("task") or ""),
        report.get("resume_contexts", {}),
    ))
    changed_files = _record_snapshot_changes(report, snapshot_changes)
    if changed_files:
        remove_files = (
            set(report.get("files_checked", [])) if not partial else changed_files
        )
        for name in remove_files:
            (context.paths.validated_dir / name).unlink(missing_ok=True)
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
                "polish_footnote_semantic_review": report.get("polish_footnote_semantic_reviews", {}).get(name),
                "polish_footnote_semantic_review_sha256": (
                    report.get("polish_footnote_semantic_reviews", {}).get(name, {}).get("sidecar_sha256")
                    if isinstance(report.get("polish_footnote_semantic_reviews", {}).get(name), Mapping)
                    else None
                ),
                "resume_context": report.get("resume_contexts", {}).get(name),
            }
        ledger = {
            "schema_version": markdown_validation_schema_version(report["task"]),
            "task": report["task"],
            "scope": "file-checkpoints",
            "files": records,
        }
        atomic_write_json(report_path, ledger)
        if report["task"] == "polish":
            _sync_partial_polish_into_full_report(context.paths.output_dir, report)
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
    # Semantic findings are review evidence. Keep the polished file in place so
    # the footnote workflow can locate exact excerpts and repair upstream.
    semantic_issue_names = {
        str(item.get("file")) for item in report["invalid"]
        if isinstance(item, Mapping) and item.get("kind") == "polish_footnote_semantics" and item.get("file")
    }
    errors_by_file: dict[str, list[Mapping[str, Any]]] = {}
    for item in report["invalid"]:
        if isinstance(item, Mapping) and item.get("file"):
            errors_by_file.setdefault(str(item["file"]), []).append(item)
    stale_context_names = {
        name for name, items in errors_by_file.items()
        if items and all(item.get("kind") in {"prepared_context", "validation_snapshot_changed"} for item in items)
    }
    quarantined = quarantine_invalid_targets(
        context.paths.output_dir, report["task"], context.paths.target_dir,
        invalid_names - stale_context_names - semantic_issue_names,
    )
    report["discarded_temp_files"] = discarded_temp_files
    report["quarantined_files"] = quarantined
    if ledger is not None:
        ledger["discarded_temp_files"] = discarded_temp_files
        ledger["quarantined_files"] = quarantined
        atomic_write_json(report_path, ledger)
        if report["task"] == "polish":
            _sync_partial_polish_into_full_report(context.paths.output_dir, report)
    else:
        atomic_write_json(report_path, report)
    return report

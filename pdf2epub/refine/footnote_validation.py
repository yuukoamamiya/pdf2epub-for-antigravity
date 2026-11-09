"""Coordinate evidence checks, pure audits, and persistence for footnote decisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, sha256_file
from .endnote_review import (
    load_endnote_definition_ambiguities,
    validate_endnote_definition_decisions,
)
from .footnote_report import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_validation_audit import (
    audit_region_decisions,
    candidate_index,
    merge_region_and_flat_decisions,
    normalize_decisions,
    required_review_addresses,
    validate_continuation_traceability,
)
from .footnote_validation_evidence import (
    load_visual_audit_manifest,
    validate_candidate_evidence,
)
from .footnote_continuation_review import validate_continuation_review_decisions


def _endnote_definition_result(output_dir: Path) -> dict[str, Any]:
    """Validate the optional centralized-endnote boundary decisions."""
    try:
        manifest = load_endnote_definition_ambiguities(output_dir)
    except ValueError as exc:
        return {
            "valid": False,
            "status": "retry_required",
            "errors": [str(exc)],
            "decisions": [],
        }
    if manifest is None:
        manifest = {"schema_version": 1, "items": []}
    return validate_endnote_definition_decisions(output_dir, manifest)


def _attach_endnote_result(
    result: dict[str, Any], endnote_result: Mapping[str, Any]
) -> dict[str, Any]:
    result["endnote_definitions"] = dict(endnote_result)
    if endnote_result.get("valid") is not True:
        result["valid"] = False
        if result.get("status") in {"validated", "no_subagent_review_required"}:
            result["status"] = str(endnote_result.get("status") or "retry_required")
        errors = result.setdefault("errors", [])
        for error in endnote_result.get("errors", []) or []:
            message = f"endnote definitions: {error}"
            if message not in errors:
                errors.append(message)
    return result


def _write_footnote_unit_contexts(
    output_dir: Path, report: Mapping[str, Any]
) -> dict[str, str]:
    """Write validated sparse contexts without importing the hand-off eagerly."""
    from .footnote_handoff import _write_unit_contexts

    return _write_unit_contexts(output_dir, report)


def _persist_result(
    output_dir: Path,
    report_path: Path,
    report: Mapping[str, Any],
    result: dict[str, Any],
    *,
    validated_decisions: bool = False,
) -> dict[str, Any]:
    if validated_decisions:
        updated_report = dict(report)
        updated_report["decisions"] = result["decisions"]
        updated_report["decisions_status"] = "validated"
        atomic_write_json(report_path, updated_report)
        manifest_path = output_dir / "footnote_subagent_manifest.json"
        if manifest_path.is_file():
            context_files = _write_footnote_unit_contexts(output_dir, updated_report)
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            manifest["unit_context_files"] = context_files
            manifest["unit_context_sha256"] = {
                name: sha256_file(output_dir / relative)
                for name, relative in context_files.items()
            }
            atomic_write_json(manifest_path, manifest)
    if report_path.is_file():
        result["candidate_report_sha256"] = sha256_file(report_path)
    atomic_write_json(output_dir / "footnote_decision_validation.json", result)
    return result


def _preflight_footnote_apply(
    output_dir: Path,
    report: Mapping[str, Any],
    normalized_decisions: list[dict[str, Any]],
) -> list[str]:
    """Run the pure apply bindings so binding errors surface at validate time.

    The apply stage resolves page decisions and binds body markers without
    writing anything.  Reusing those pure functions here keeps ``footnote-validate``
    and ``footnote-apply`` in agreement instead of accepting decisions that the
    next command rejects.
    """
    from .footnote_apply_resolution import bind_note_markers, resolve_page_decisions
    from .footnote_edit_plan import split_multi_superscripts
    from .footnote_status import unit_records

    try:
        records = unit_records(output_dir)
    except Exception:
        return []
    source_dir = output_dir / "ocr_markdown"
    source_texts: dict[str, str] = {}
    for record in records:
        try:
            text = (source_dir / record["name"]).read_text(encoding="utf-8")
        except (OSError, UnicodeError, KeyError):
            continue
        source_texts[record["name"]] = split_multi_superscripts(
            text.replace("\r\n", "\n").replace("\r", "\n")
        )
    if not source_texts:
        return []
    validation = {"decisions": list(normalized_decisions)}
    resolution = resolve_page_decisions(
        output_dir,
        report,
        validation,
        source_texts,
        list(records),
        str(report.get("footnote_mode") or "footnotes"),
    )
    errors = list(resolution.errors)
    if not errors:
        errors = list(bind_note_markers(resolution, source_texts, list(records)))
    return errors


def validate_footnote_decisions(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate the small JSON decision file produced by the Subagent.

    The orchestration stays here; filesystem/config evidence and pure contract
    audits live in their dedicated modules.
    """
    output_dir = Path(output_dir)
    report_path = output_dir / "footnote_candidates.json"
    decision_path = output_dir / "footnote_decisions.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {
            "valid": False,
            "status": "retry_required",
            "errors": [f"invalid candidate report: {exc}"],
        }
        return _persist_result(
            output_dir, report_path, {},
            _attach_endnote_result(result, _endnote_definition_result(output_dir)),
        )
    if not isinstance(report, dict):
        result = {
            "valid": False,
            "status": "retry_required",
            "errors": ["candidate report must be an object"],
        }
        return _persist_result(
            output_dir, report_path, {},
            _attach_endnote_result(result, _endnote_definition_result(output_dir)),
        )

    endnote_result = _endnote_definition_result(output_dir)
    evidence = validate_candidate_evidence(output_dir, report, config)
    errors = list(evidence["errors"])
    base_fields = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_kind": evidence["source_kind"],
        "ocr_evidence_mode": evidence["evidence_mode"],
        "footnote_mode": evidence["footnote_mode"],
        "configured_footnote_mode": evidence["configured_footnote_mode"],
        "footnote_mode_source": evidence["footnote_mode_source"],
        "warnings": list(report.get("warnings", [])),
    }
    if errors:
        result = {
            **base_fields,
            "valid": False,
            "status": "retry_required",
            "errors": errors,
            "decisions": [],
        }
        _attach_endnote_result(result, endnote_result)
        return _persist_result(output_dir, report_path, report, result)

    required = required_review_addresses(report)
    continuation_tasks = report.get("continuation_reviews", [])
    if not required and not continuation_tasks:
        result = {
            **base_fields,
            "valid": True,
            "status": "no_subagent_review_required",
            "errors": [],
            "decisions": [],
            "continuation_reviews": [],
        }
        _attach_endnote_result(result, endnote_result)
        return _persist_result(
            output_dir, report_path, report, result, validated_decisions=True
        )

    try:
        decisions_data = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {
            "valid": False,
            "status": "retry_required",
            "errors": [f"invalid decision file: {exc}"],
        }
        _attach_endnote_result(result, endnote_result)
        return _persist_result(output_dir, report_path, report, result)

    decisions = decisions_data.get("decisions") if isinstance(decisions_data, dict) else None
    human_review_required: list[str] = []
    if (
        not isinstance(decisions_data, dict)
        or decisions_data.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION
    ):
        errors.append("decision file has an unsupported schema_version")
    if not isinstance(decisions, list):
        errors.append("decision file must contain a decisions array")
        decisions = []

    visual_manifest, manifest_error = load_visual_audit_manifest(output_dir, report)
    if manifest_error:
        errors.append(manifest_error)
    audit_tasks = {
        str(task.get("region_id"))
        for task in visual_manifest.get("tasks", []) or []
        if isinstance(task, Mapping) and task.get("region_id")
    }
    region_ids, nested_decisions, region_review, region_errors = audit_region_decisions(
        decisions_data if isinstance(decisions_data, Mapping) else {}, audit_tasks
    )
    human_review_required.extend(region_review)
    errors.extend(region_errors)
    decisions = merge_region_and_flat_decisions(nested_decisions, decisions)

    candidates = candidate_index(report)
    normalized, decision_errors, decision_review, seen = normalize_decisions(
        decisions, candidates
    )
    errors.extend(decision_errors)
    human_review_required.extend(decision_review)
    missing = sorted(required - seen)
    errors.extend(
        f"missing decision for review candidate {page}:{source}:{block}"
        for page, source, block in missing
    )
    errors.extend(validate_continuation_traceability(candidates, normalized))
    continuation_decisions, continuation_errors = validate_continuation_review_decisions(
        output_dir,
        continuation_tasks,
        decisions_data.get("continuation_reviews") if isinstance(decisions_data, dict) else None,
    )
    errors.extend(continuation_errors)

    if (
        report.get("footnote_mode") == "footnotes"
        and not errors
        and not human_review_required
    ):
        try:
            errors.extend(_preflight_footnote_apply(output_dir, report, normalized))
        except Exception as exc:
            errors.append(f"footnote apply preflight failed: {exc}")

    if human_review_required:
        status = "human_review_required"
    elif errors:
        status = "retry_required"
    else:
        status = "validated"
    result = {
        **base_fields,
        "valid": not errors and not human_review_required,
        "status": status,
        "errors": errors,
        "human_review_required": human_review_required,
        "region_audits": sorted(region_ids),
        "decisions": normalized,
        "continuation_reviews": continuation_decisions,
    }
    _attach_endnote_result(result, endnote_result)
    return _persist_result(
        output_dir, report_path, report, result,
        validated_decisions=result["valid"],
    )


__all__ = ["validate_footnote_decisions"]

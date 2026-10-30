"""Coordinate evidence checks, pure audits, and persistence for footnote decisions."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json
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
    atomic_write_json(output_dir / "footnote_decision_validation.json", result)
    if validated_decisions:
        updated_report = dict(report)
        updated_report["decisions"] = result["decisions"]
        updated_report["decisions_status"] = "validated"
        atomic_write_json(report_path, updated_report)
        _write_footnote_unit_contexts(output_dir, updated_report)
    return result


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
    if not required:
        result = {
            **base_fields,
            "valid": True,
            "status": "no_subagent_review_required",
            "errors": [],
            "decisions": [],
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
    }
    _attach_endnote_result(result, endnote_result)
    return _persist_result(
        output_dir, report_path, report, result,
        validated_decisions=result["valid"],
    )


__all__ = ["validate_footnote_decisions"]

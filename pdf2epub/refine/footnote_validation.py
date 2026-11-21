"""Coordinate evidence checks, pure audits, and persistence for footnote decisions."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, schema_major_matches, sha256_file
from .endnote_review import (
    load_endnote_definition_ambiguities,
    validate_endnote_definition_decisions,
)
from .footnote_contracts import FOOTNOTE_SHORTCUT_POLICY_VERSION
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
    load_candidate_full_texts,
    load_visual_audit_manifest,
    validate_candidate_evidence,
)
from .footnote_continuation_review import validate_continuation_review_decisions
from .footnote_result_review import validate_result_audit_reviews
from .footnote_effective_decisions import EFFECTIVE_DECISIONS_VERSION


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
        if result.get("status") in {
            "validated",
            "no_subagent_review_required",
            "not_applicable",
        }:
            result["status"] = str(endnote_result.get("status") or "retry_required")
        errors = result.setdefault("errors", [])
        for error in endnote_result.get("errors", []) or []:
            message = f"endnote definitions: {error}"
            if message not in errors:
                errors.append(message)
    return result


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
        updated_report["result_audit_reviews"] = result.get("result_audit_reviews", [])
        updated_report["decisions_status"] = "validated"
        atomic_write_json(report_path, updated_report)
        manifest_path = output_dir / "footnote_subagent_manifest.json"
        if manifest_path.is_file():
            from .footnote_unit_contexts import write_footnote_unit_contexts

            context_files = write_footnote_unit_contexts(output_dir, updated_report)
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
    continuation_reviews: Optional[list[dict[str, Any]]] = None,
    endnote_definition_decisions: Optional[list[dict[str, Any]]] = None,
) -> tuple[list[str], list[dict[str, Any]], dict[str, str]]:
    """Build the same complete normalization plan that apply will materialize."""
    from .footnote_normalization_plan import (
        FootnoteNormalizationPlanningInput,
        build_footnote_normalization_plan,
    )
    from .footnote_source import load_footnote_source_snapshot

    try:
        source = load_footnote_source_snapshot(output_dir)
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        return [f"could not load current footnote source snapshot: {exc}"], [], {}
    try:
        toc_tree = json.loads((output_dir / "toc_tree.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        toc_tree = None
    try:
        progress = json.loads((source.source_dir / "tree_progress.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        progress = None
    scope_error = None
    if toc_tree is None or progress is None:
        scope_error = "toc_tree.json or ocr_markdown/tree_progress.json is unavailable"
    decisions: dict[str, Any] = {
        "decisions": list(normalized_decisions),
        "continuation_reviews": list(continuation_reviews or []),
    }
    plan = build_footnote_normalization_plan(FootnoteNormalizationPlanningInput(
        output_dir=output_dir,
        report=report,
        decisions=decisions,
        source=source,
        footnote_mode=str(report.get("footnote_mode") or "footnotes"),
        toc_tree=toc_tree if isinstance(toc_tree, Mapping) else None,
        progress=progress if isinstance(progress, Mapping) else None,
        scope_error=scope_error,
        endnote_definition_decisions=endnote_definition_decisions,
    ))
    return list(plan.errors), list(plan.effective_decisions), dict(plan.source_sha256)


def _current_source_hashes(output_dir: Path) -> tuple[dict[str, str], Optional[str]]:
    from .footnote_source import load_footnote_source_snapshot

    try:
        snapshot = load_footnote_source_snapshot(output_dir)
    except (OSError, UnicodeError, ValueError, KeyError) as exc:
        return {}, f"could not load current footnote source snapshot: {exc}"
    return dict(snapshot.source_hashes), None


def _report_has_no_candidates(report: Mapping[str, Any]) -> bool:
    """Return whether the candidate report contains no reviewable content.

    A high-confidence candidate that was auto-accepted is still a candidate:
    it needs deterministic normalization, so it must not take the zero-work
    shortcut.
    """
    for page in report.get("pages", []) or []:
        if not isinstance(page, Mapping):
            continue
        for key in ("candidates", "audit_candidates", "secondary_only_candidates"):
            value = page.get(key)
            if isinstance(value, list) and value:
                return False
    if report.get("continuation_reviews"):
        return False
    if report.get("review_required"):
        return False
    audit = report.get("result_audit")
    if isinstance(audit, Mapping):
        for key in ("findings", "reviewed_candidate_tasks"):
            value = audit.get(key)
            if isinstance(value, list) and value:
                return False
    return True


def _zero_candidate_shortcut(
    report: Mapping[str, Any],
    endnote_result: Mapping[str, Any],
    decision_path: Path,
) -> bool:
    """Zero page candidates and no endnote ambiguity need no Subagent."""
    return (
        _report_has_no_candidates(report)
        and str(report.get("footnote_mode") or "footnotes").strip().lower()
        == "footnotes"
        and not endnote_result.get("source_sha256")
        and not decision_path.exists()
    )


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
    zero_candidate_report = _report_has_no_candidates(report)
    evidence = validate_candidate_evidence(
        output_dir, report, config, verify_hashes=not zero_candidate_report
    )
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
    audit_data = report.get("result_audit") if isinstance(report.get("result_audit"), Mapping) else {}
    polish_blockers = audit_data.get("polish_semantic_blockers", []) if isinstance(audit_data, Mapping) else []
    for blocker in polish_blockers if isinstance(polish_blockers, list) else []:
        if not isinstance(blocker, Mapping):
            continue
        errors.append(
            "unresolved current Polish semantic review blocker "
            f"({blocker.get('kind')}, {blocker.get('file')}, {blocker.get('issue_id')}, "
            f"{blocker.get('mapping_stage')}): "
            f"{blocker.get('message')}; {blocker.get('recovery')}"
        )
    polish_escalated = audit_data.get("polish_semantic_escalated", []) if isinstance(audit_data, Mapping) else []
    polish_escalated = polish_escalated if isinstance(polish_escalated, list) else []
    for item in polish_escalated:
        if not isinstance(item, Mapping):
            continue
        errors.append(
            "Polish semantic reflow requires a human decision "
            f"({item.get('file')}, issue {item.get('issue_id') or 'unknown'}, "
            f"{item.get('rounds')}/{item.get('max_rounds')} rounds): {item.get('recovery')}"
        )
    if errors:
        result = {
            **base_fields,
            "valid": False,
            "status": "human_review_required" if polish_escalated else "retry_required",
            "errors": errors,
            "decisions": [],
        }
        if polish_escalated:
            result["human_review_required"] = sorted({
                str(item.get("task_id") or item.get("file") or "")
                for item in polish_escalated if isinstance(item, Mapping)
            } - {""})
        _attach_endnote_result(result, endnote_result)
        return _persist_result(output_dir, report_path, report, result)

    if _zero_candidate_shortcut(report, endnote_result, decision_path):
        # No page footnote and no endnote ambiguity: the receipt binds only the
        # candidate report hash.  Skip per-sidecar evidence hashing; a minor
        # schema bump keeps this shortcut current.
        source_sha256, source_error = _current_source_hashes(output_dir)
        shortcut_errors = [source_error] if source_error else []
        result = {
            **base_fields,
            "valid": not shortcut_errors,
            "status": "not_applicable" if not shortcut_errors else "retry_required",
            "errors": shortcut_errors,
            "decisions": [],
            "effective_decisions_version": EFFECTIVE_DECISIONS_VERSION,
            "effective_decisions": [],
            "source_sha256": source_sha256,
            "continuation_reviews": [],
            "shortcut": {
                "kind": "zero_candidates",
                "policy_version": FOOTNOTE_SHORTCUT_POLICY_VERSION,
            },
        }
        _attach_endnote_result(result, endnote_result)
        return _persist_result(output_dir, report_path, report, result)

    effective_decisions: list[dict[str, Any]] = []
    source_sha256: dict[str, str] = {}
    required = required_review_addresses(report)
    continuation_tasks = report.get("continuation_reviews", [])
    result_audit_tasks = audit_data.get("findings", []) if isinstance(audit_data, Mapping) else []
    if isinstance(audit_data, Mapping) and isinstance(audit_data.get("reviewed_candidate_tasks"), list):
        result_audit_tasks = [
            *(result_audit_tasks if isinstance(result_audit_tasks, list) else []),
            *audit_data["reviewed_candidate_tasks"],
        ]
    if not required and not continuation_tasks and not result_audit_tasks and not decision_path.exists():
        preflight_errors = []
        effective_decisions: list[dict[str, Any]] = []
        source_sha256: dict[str, str] = {}
        if str(report.get("footnote_mode") or "footnotes") != "disabled":
            endnote_decisions = (
                endnote_result.get("decisions", [])
                if endnote_result.get("valid") is True
                and isinstance(endnote_result.get("decisions"), list)
                else None
            )
            preflight_errors, effective_decisions, source_sha256 = _preflight_footnote_apply(
                output_dir, report, [], [], endnote_decisions
            )
        else:
            source_sha256, source_error = _current_source_hashes(output_dir)
            if source_error:
                preflight_errors.append(source_error)
        result = {
            **base_fields,
            "valid": not preflight_errors,
            "status": "retry_required" if preflight_errors else "no_subagent_review_required",
            "errors": preflight_errors,
            "decisions": [],
            "effective_decisions_version": EFFECTIVE_DECISIONS_VERSION,
            "effective_decisions": effective_decisions,
            "source_sha256": source_sha256,
            "continuation_reviews": [],
        }
        _attach_endnote_result(result, endnote_result)
        return _persist_result(
            output_dir, report_path, report, result,
            validated_decisions=result["valid"],
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
        or not schema_major_matches(
            decisions_data.get("schema_version"), FOOTNOTE_PREPARE_SCHEMA_VERSION
        )
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
    if any(
        isinstance(decision, Mapping) and decision.get("segments") is not None
        for decision in decisions
    ):
        segment_addresses = {
            (int(decision["page"]), str(decision.get("source") or "primary").strip().lower(), int(decision["block"]))
            for decision in decisions
            if isinstance(decision, Mapping)
            and decision.get("segments") is not None
            and str(decision.get("page", "")).lstrip("-").isdigit()
            and str(decision.get("block", "")).lstrip("-").isdigit()
        }
        hydrate_addresses = set()
        for address in segment_addresses & candidates.keys():
            candidate = candidates[address]
            block_indices = candidate.get("block_indices")
            if isinstance(block_indices, list) and len(block_indices) > 1:
                errors.append(
                    "segmented excerpts are not supported for an aggregated multi-block OCR candidate: "
                    f"{address[0]}:{address[1]}:{address[2]}"
                )
                continue
            hydrate_addresses.add(address)
        full_texts, full_text_errors = load_candidate_full_texts(
            output_dir, report, hydrate_addresses
        )
        errors.extend(full_text_errors)
        for address, full_text in full_texts.items():
            if address in candidates:
                # Keep compact report previews intact; only this validation-time
                # projection receives full text from the current sidecar evidence.
                candidates[address]["text"] = full_text
    normalized, decision_errors, decision_review, seen = normalize_decisions(
        decisions, candidates
    )
    errors.extend(decision_errors)
    human_review_required.extend(decision_review)
    from ..review_escalation import record_footnote_adjudications

    adjudications = record_footnote_adjudications(
        output_dir,
        decisions=normalized,
        effective_decisions_version=EFFECTIVE_DECISIONS_VERSION,
    )
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
    result_audit_reviews, result_audit_errors = validate_result_audit_reviews(
        output_dir,
        result_audit_tasks,
        decisions_data.get("result_audit_reviews") if isinstance(decisions_data, Mapping) else None,
        ordinary_decisions=normalized,
    )
    errors.extend(result_audit_errors)
    from ..review_escalation import record_footnote_adjudications
    from .footnote_result_review import record_result_audit_outcomes

    audit_outcomes = record_result_audit_outcomes(
        output_dir,
        result_audit_tasks,
        result_audit_reviews,
        decisions_digest=(
            hashlib.sha256(decision_path.read_bytes()).hexdigest()
            if decision_path.is_file() else ""
        ),
    )
    audit_human_review_required = list(audit_outcomes["human_review_required"])
    audit_review_required = list(audit_outcomes["review_required"])
    for task_id in audit_human_review_required:
        errors.append(
            f"result audit task {task_id} already survived a scoped visual review; "
            "human review is required before another automatic retry"
        )
    for task_id in audit_review_required:
        errors.append(
            f"result audit task {task_id} remains ambiguous; rerun footnote-prepare for another scoped visual review"
        )
    human_review_required.extend(audit_human_review_required)

    if (
        report.get("footnote_mode") != "disabled"
        and not errors
        and not human_review_required
    ):
        try:
            endnote_decisions = (
                endnote_result.get("decisions", [])
                if endnote_result.get("valid") is True
                and isinstance(endnote_result.get("decisions"), list)
                else None
            )
            preflight_errors, effective_decisions, source_sha256 = _preflight_footnote_apply(
                output_dir, report, normalized, continuation_decisions,
                endnote_decisions,
            )
            errors.extend(preflight_errors)
        except Exception as exc:
            errors.append(f"footnote apply preflight failed: {exc}")

    if not source_sha256:
        source_sha256, source_error = _current_source_hashes(output_dir)
        if source_error:
            errors.append(source_error)

    if human_review_required:
        status = "human_review_required"
    elif audit_review_required:
        status = "review_required"
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
        "effective_decisions_version": EFFECTIVE_DECISIONS_VERSION,
        "effective_decisions": effective_decisions,
        "source_sha256": source_sha256,
        "continuation_reviews": continuation_decisions,
        "result_audit_reviews": result_audit_reviews,
        "result_audit_version": audit_data.get("schema_version") if isinstance(audit_data, Mapping) else None,
        "footnote_adjudications": adjudications,
    }
    _attach_endnote_result(result, endnote_result)
    return _persist_result(
        output_dir, report_path, report, result,
        validated_decisions=result["valid"],
    )


__all__ = ["validate_footnote_decisions"]

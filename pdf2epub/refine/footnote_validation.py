"""Validate the workspace Subagent's footnote decision contract.

Candidate detection belongs to the footnote_prepare module; materialization belongs
to the footnote_apply module. This module owns only the boundary between the
visual review hand-off and the deterministic apply stage.
"""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, sha256_file
from .endnote_review import (
    load_endnote_definition_ambiguities,
    validate_endnote_definition_decisions,
)
from .footnote_contracts import (
    DECISION_ROLES as _DECISION_ROLES,
    MOVED_ROLES as _MOVED_ROLES,
)
from .footnote_options import (
    EFFECTIVE_FOOTNOTE_MODES,
    resolve_effective_footnote_mode,
)
from .footnote_report import FOOTNOTE_PREPARE_SCHEMA_VERSION
from ..footnote_keys import canonical_numeric_label
from .pdf_evidence import pdf_evidence_mode, require_current_consensus


def _candidate_index(
    report: Mapping[str, Any],
) -> dict[tuple[int, str, int], dict[str, Any]]:
    index = {}
    for page in report.get("pages", []) or []:
        if not isinstance(page, Mapping):
            continue
        for field, default_source in (
            ("candidates", "primary"),
            ("audit_candidates", "primary"),
            ("secondary_only_candidates", "secondary"),
        ):
            for candidate in page.get(field, []) or []:
                if not isinstance(candidate, Mapping):
                    continue
                try:
                    address = (
                        int(candidate["page"]),
                        str(candidate.get("source") or default_source).strip().lower(),
                        int(candidate["block"]),
                    )
                except (KeyError, TypeError, ValueError):
                    continue
                index[address] = dict(candidate)
    return index


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
    result: dict[str, Any],
    endnote_result: Mapping[str, Any],
) -> dict[str, Any]:
    result["endnote_definitions"] = dict(endnote_result)
    if endnote_result.get("valid") is not True:
        result["valid"] = False
        if result.get("status") in {"validated", "no_subagent_review_required"}:
            result["status"] = str(
                endnote_result.get("status") or "retry_required"
            )
        errors = result.setdefault("errors", [])
        for error in endnote_result.get("errors", []) or []:
            message = f"endnote definitions: {error}"
            if message not in errors:
                errors.append(message)
    return result


def validate_footnote_decisions(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Validate the small JSON decision file produced by the Subagent.

    This validator checks references to candidate block addresses and basic
    continuation provenance.  It never infers a missing decision and never
    turns an unresolved visual case into an automatic acceptance.
    """
    output_dir = Path(output_dir)
    report_path = output_dir / "footnote_candidates.json"
    decision_path = output_dir / "footnote_decisions.json"
    try:
        report = json.loads(report_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"valid": False, "status": "retry_required", "errors": [f"invalid candidate report: {exc}"]}
        _attach_endnote_result(result, _endnote_definition_result(output_dir))
        atomic_write_json(output_dir / "footnote_decision_validation.json", result)
        return result
    if not isinstance(report, dict):
        result = {"valid": False, "status": "retry_required", "errors": ["candidate report must be an object"]}
        _attach_endnote_result(result, _endnote_definition_result(output_dir))
        atomic_write_json(output_dir / "footnote_decision_validation.json", result)
        return result

    endnote_result = _endnote_definition_result(output_dir)

    source_kind = str(report.get("source_kind") or "ocr")
    evidence_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    footnote_mode = str(report.get("footnote_mode") or "footnotes").strip().lower()
    configured_footnote_mode: Optional[str] = None
    footnote_mode_source: Optional[str] = None
    errors: list[str] = []
    if source_kind not in {"ocr", "native_text"}:
        errors.append("candidate report has an unsupported page source kind")
    if evidence_mode not in {"single_ocr", "two_ocr"}:
        errors.append("candidate report has an unsupported OCR evidence mode")
    if footnote_mode not in EFFECTIVE_FOOTNOTE_MODES:
        errors.append("candidate report has an unsupported footnote mode")
    if config is not None:
        configured_mode = pdf_evidence_mode(output_dir, config)
        if evidence_mode != configured_mode:
            errors.append(
                f"candidate report was prepared in {evidence_mode}, but configuration requires {configured_mode}"
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                errors.append(str(exc))
    try:
        resolution = resolve_effective_footnote_mode(output_dir, config)
        configured_footnote_mode = resolution["configured"]
        footnote_mode_source = resolution["source"]
        if footnote_mode != resolution["effective"]:
            errors.append(
                "candidate report was prepared with footnotes.mode="
                f"{footnote_mode}, but the current configuration/TOC requires "
                f"{resolution['effective']} (source: {resolution['source']})"
            )
        recorded_configured = report.get("configured_footnote_mode")
        if (
            isinstance(recorded_configured, str)
            and recorded_configured.strip().lower() != configured_footnote_mode
        ):
            errors.append(
                "candidate report was prepared with configured footnotes.mode="
                f"{recorded_configured}, but configuration now resolves "
                f"{configured_footnote_mode}"
            )
    except ValueError as exc:
        errors.append(str(exc))

    def _hashes_current(field: str) -> bool:
        expected_hashes = report.get(field, {})
        if not isinstance(expected_hashes, Mapping):
            errors.append(f"candidate report field {field} must be an object")
            return False
        current_hashes: dict[str, str] = {}
        for relative, expected in expected_hashes.items():
            path = output_dir / str(relative)
            if path.is_file():
                current_hashes[str(relative)] = sha256_file(path)
        if current_hashes != dict(expected_hashes):
            errors.append(f"one or more {field} files changed or disappeared")
            return False
        return True

    _hashes_current("sidecar_sha256")
    secondary_hashes = report.get("secondary_sidecar_sha256", {})
    if evidence_mode == "two_ocr":
        _hashes_current("secondary_sidecar_sha256")
    elif secondary_hashes:
        errors.append("single-OCR candidate report unexpectedly contains secondary OCR files")
    if errors:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "footnote_mode": footnote_mode,
            "configured_footnote_mode": configured_footnote_mode,
            "footnote_mode_source": footnote_mode_source,
            "valid": False,
            "status": "retry_required",
            "errors": errors,
            "decisions": [],
        }
        _attach_endnote_result(result, endnote_result)
        atomic_write_json(output_dir / "footnote_decision_validation.json", result)
        return result

    required = {
        (
            int(candidate["page"]),
            "primary",
            int(candidate["block"]),
        )
        for page in report.get("pages", []) or []
        for candidate in page.get("candidates", []) or []
        if candidate.get("confidence") == "review"
    }
    required.update(
        (
            int(candidate["page"]),
            "secondary",
            int(candidate["block"]),
        )
        for page in report.get("pages", []) or []
        for candidate in page.get("secondary_only_candidates", []) or []
        if candidate.get("confidence") == "review"
    )
    required.update(
        (
            int(candidate["page"]),
            "primary",
            int(candidate["block"]),
        )
        for page in report.get("pages", []) or []
        for candidate in page.get("audit_candidates", []) or []
        if candidate.get("confidence") == "review"
    )
    if not required:
        result = {
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "source_kind": source_kind,
            "ocr_evidence_mode": evidence_mode,
            "footnote_mode": footnote_mode,
            "configured_footnote_mode": configured_footnote_mode,
            "footnote_mode_source": footnote_mode_source,
            "valid": True,
            "status": "no_subagent_review_required",
            "errors": [],
            "decisions": [],
        }
        _attach_endnote_result(result, endnote_result)
        atomic_write_json(output_dir / "footnote_decision_validation.json", result)
        report["decisions"] = []
        report["decisions_status"] = "validated"
        atomic_write_json(report_path, report)
        _write_footnote_unit_contexts(output_dir, report)
        return result

    try:
        decisions_data = json.loads(decision_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        result = {"valid": False, "status": "retry_required", "errors": [f"invalid decision file: {exc}"]}
        _attach_endnote_result(result, endnote_result)
        atomic_write_json(output_dir / "footnote_decision_validation.json", result)
        return result
    decisions = decisions_data.get("decisions") if isinstance(decisions_data, dict) else None
    human_review_required: list[str] = []
    if not isinstance(decisions_data, dict) or decisions_data.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        errors.append("decision file has an unsupported schema_version")
    if not isinstance(decisions, list):
        errors.append("decision file must contain a decisions array")
        decisions = []

    # New hand-offs are region-first.  Keep accepting the historical flat
    # array so completed projects can resume, but validate every new region
    # audit and merge its nested block decisions into the same exact-address
    # contract consumed by footnote-apply.
    visual_manifest_path = output_dir / str(
        report.get("visual_audit_manifest") or "footnote_visual_audit_manifest.json"
    )
    visual_manifest: Mapping[str, Any] = {}
    if visual_manifest_path.is_file():
        try:
            loaded_manifest = json.loads(visual_manifest_path.read_text(encoding="utf-8"))
            if isinstance(loaded_manifest, Mapping):
                visual_manifest = loaded_manifest
        except (OSError, UnicodeError, json.JSONDecodeError):
            errors.append("footnote visual audit manifest is invalid")
    audit_tasks = {
        str(task.get("region_id"))
        for task in visual_manifest.get("tasks", []) or []
        if isinstance(task, Mapping) and task.get("region_id")
    }
    region_audits = (
        decisions_data.get("region_audits")
        if isinstance(decisions_data, Mapping)
        else None
    )
    if region_audits is not None and not isinstance(region_audits, list):
        errors.append("region_audits must be an array")
        region_audits = []
    region_audit_ids: set[str] = set()
    nested_decisions: list[Mapping[str, Any]] = []
    for audit in region_audits or []:
        if not isinstance(audit, Mapping):
            errors.append("each region audit must be an object")
            continue
        region_id = str(audit.get("region_id") or "").strip()
        if not region_id or region_id not in audit_tasks:
            errors.append(f"region audit references unknown region: {region_id or '<missing>'}")
            continue
        if region_id in region_audit_ids:
            errors.append(f"duplicate region audit: {region_id}")
            continue
        region_audit_ids.add(region_id)
        disposition = str(audit.get("disposition") or "").strip()
        if disposition not in {"footnote_region", "body_region", "review_required"}:
            errors.append(f"unsupported region audit disposition for {region_id}: {disposition}")
        if disposition == "review_required":
            human_review_required.append(f"region:{region_id}")
        region_decisions = audit.get("decisions")
        if not isinstance(region_decisions, list):
            errors.append(f"region audit {region_id} must contain a decisions array")
            continue
        nested_decisions.extend(
            item for item in region_decisions if isinstance(item, Mapping)
        )
    if region_audits is not None:
        missing_regions = sorted(audit_tasks - region_audit_ids)
        errors.extend(f"missing region audit for {region_id}" for region_id in missing_regions)

    # Merge nested and flat arrays without making a compatible hand-off fail
    # merely because the same exact decision was copied into both places.
    merged_decisions: list[Mapping[str, Any]] = []
    merged_addresses: set[tuple[int, str, int]] = set()
    for item in [*nested_decisions, *decisions]:
        if not isinstance(item, Mapping):
            merged_decisions.append(item)
            continue
        try:
            address = (
                int(item["page"]),
                str(item.get("source") or "primary").strip().lower(),
                int(item["block"]),
            )
        except (KeyError, TypeError, ValueError):
            merged_decisions.append(item)
            continue
        if address in merged_addresses:
            continue
        merged_addresses.add(address)
        merged_decisions.append(item)
    decisions = merged_decisions

    candidates = _candidate_index(report)
    seen: set[tuple[int, str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for item in decisions:
        if not isinstance(item, Mapping):
            errors.append("each decision must be an object")
            continue
        try:
            source = str(item.get("source") or "primary").strip().lower()
            address = (int(item["page"]), source, int(item["block"]))
        except (KeyError, TypeError, ValueError):
            errors.append("decision is missing an integer page/block address")
            continue
        if source not in {"primary", "secondary"}:
            errors.append(
                f"unsupported decision source for {address[0]}:{address[2]}: {source}"
            )
            continue
        if address in seen:
            errors.append(
                f"duplicate decision for {address[2]}:{address[0]}:{address[1]}"
            )
            continue
        seen.add(address)
        candidate = candidates.get(address)
        if candidate is None:
            errors.append(
                "decision points to a non-candidate block: "
                f"{address[0]}:{address[1]}:{address[2]}"
            )
            continue
        if (
            candidate.get("requires_marker_context") is True
            and str(item.get("role") or "").strip()
            in {"footnote_start", "footnote_definition"}
            and not str(item.get("marker_context") or "").strip()
        ):
            errors.append(
                "candidate requires an exact marker_context to disambiguate its layout scope: "
                f"{address[0]}:{address[1]}:{address[2]}"
            )
            continue
        role = str(item.get("role") or "").strip()
        if role not in _DECISION_ROLES:
            errors.append(
                f"unsupported decision role for {address[0]}:{address[1]}:{address[2]}: {role}"
            )
            continue
        key = item.get("key")
        if key is not None:
            key = str(key).strip()
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                canonical_numeric = canonical_numeric_label(key)
                if canonical_numeric is not None:
                    key = canonical_numeric
            if not re.fullmatch(r"[A-Za-z0-9_-]+", key):
                errors.append(
                    f"invalid footnote key for {address[0]}:{address[1]}:{address[2]}"
                )
                continue
        if role in {"footnote_start", "footnote_continuation", "footnote_definition"} and not key:
            errors.append(
                f"footnote decision is missing a key for {address[0]}:{address[1]}:{address[2]}"
            )
            continue
        if candidate.get("key") and role in {"footnote_start", "footnote_definition"} and key != candidate["key"]:
            errors.append(
                f"decision key does not match OCR key for {address[0]}:{address[1]}:{address[2]}"
            )
            continue
        candidate_marker_label = str(candidate.get("marker_label") or "")
        decision_marker_label = str(item.get("marker_label") or "")
        marker_identity = candidate.get("marker_identity")
        marker_kind = (
            str(marker_identity.get("kind") or "")
            if isinstance(marker_identity, Mapping)
            else ""
        )
        if (
            role in {"footnote_start", "footnote_definition"}
            and candidate_marker_label
            and marker_kind != "numeric"
        ):
            if decision_marker_label != candidate_marker_label:
                errors.append(
                    f"decision marker_label must preserve printed marker for "
                    f"{address[0]}:{address[1]}:{address[2]}"
                )
                continue
        if source == "secondary" and role in _MOVED_ROLES:
            text = str(item.get("text") or "").strip()
            if not text:
                errors.append(
                    "secondary-only footnote decision must include explicit text for "
                    f"{address[0]}:{address[2]}"
                )
                continue
            source_file = str(item.get("source_file") or "").strip()
            if not source_file or Path(source_file).name != source_file or not source_file.endswith(".md"):
                errors.append(
                    "secondary-only footnote decision must include a safe source_file for "
                    f"{address[0]}:{address[2]}"
                )
                continue
            primary_disposition = str(
                item.get("primary_disposition") or ""
            ).strip().lower()
            if primary_disposition not in {"absent", "remove"}:
                errors.append(
                    "secondary-only footnote decision must declare primary_disposition "
                    f"as absent or remove for {address[0]}:{address[2]}"
                )
                continue
            if primary_disposition == "remove":
                raw_primary_blocks = item.get("primary_blocks")
                if raw_primary_blocks is None:
                    raw_primary_block = item.get("primary_block")
                    raw_primary_blocks = (
                        [raw_primary_block] if raw_primary_block is not None else None
                    )
                if not isinstance(raw_primary_blocks, list) or not raw_primary_blocks:
                    errors.append(
                        "secondary-only footnote with primary_disposition=remove must "
                        f"include primary_block(s) for {address[0]}:{address[2]}"
                    )
                    continue
                normalized_primary_blocks: list[int] = []
                invalid_primary_block = False
                for raw_block in raw_primary_blocks:
                    try:
                        primary_block = int(raw_block)
                    except (TypeError, ValueError):
                        invalid_primary_block = True
                        break
                    if primary_block < 0 or primary_block in normalized_primary_blocks:
                        invalid_primary_block = True
                        break
                    normalized_primary_blocks.append(primary_block)
                if invalid_primary_block:
                    errors.append(
                        "secondary-only footnote primary_block(s) must be unique "
                        f"non-negative integers for {address[0]}:{address[2]}"
                    )
                    continue
            else:
                normalized_primary_blocks = []
                if item.get("primary_block") is not None or item.get("primary_blocks") is not None:
                    errors.append(
                        "secondary-only footnote with primary_disposition=absent must not "
                        f"include primary_block(s) for {address[0]}:{address[2]}"
                    )
                    continue
        decision = dict(item)
        decision["page"], decision["source"], decision["block"], decision["role"] = (
            address[0],
            address[1],
            address[2],
            role,
        )
        if key is not None:
            decision["key"] = key
        if candidate_marker_label:
            decision["marker_label"] = candidate_marker_label
        if candidate.get("requires_marker_context") is True:
            decision["requires_marker_context"] = True
        if source == "secondary" and role in _MOVED_ROLES:
            decision["primary_disposition"] = primary_disposition
            decision["primary_blocks"] = normalized_primary_blocks
        if role == "review_required":
            human_review_required.append(f"{address[0]}:{address[1]}:{address[2]}")
        normalized.append(decision)

    missing = sorted(required - seen)
    errors.extend(
        f"missing decision for review candidate {page}:{source}:{block}"
        for page, source, block in missing
    )

    key_sources = {
        str(candidate.get("key"))
        for candidate in candidates.values()
        if candidate.get("key")
    }
    key_sources.update(
        str(item.get("key"))
        for item in normalized
        if item.get("role") in {"footnote_start", "footnote_definition"} and item.get("key")
    )
    for item in normalized:
        if item.get("role") != "footnote_continuation" or not item.get("key"):
            continue
        if str(item["key"]) not in key_sources:
            errors.append(
                f"continuation {item['page']}:{item['block']} has no traceable footnote key"
            )

    if human_review_required:
        status = "human_review_required"
    elif errors:
        status = "retry_required"
    else:
        status = "validated"
    result = {
        "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
        "source_kind": source_kind,
        "ocr_evidence_mode": evidence_mode,
        "footnote_mode": footnote_mode,
        "configured_footnote_mode": configured_footnote_mode,
        "footnote_mode_source": footnote_mode_source,
        "valid": not errors and not human_review_required,
        "status": status,
        "errors": errors,
        "human_review_required": human_review_required,
        "region_audits": sorted(region_audit_ids),
        "decisions": normalized,
    }
    _attach_endnote_result(result, endnote_result)
    atomic_write_json(output_dir / "footnote_decision_validation.json", result)
    if result["valid"]:
        report["decisions"] = normalized
        report["decisions_status"] = "validated"
        atomic_write_json(report_path, report)
        _write_footnote_unit_contexts(output_dir, report)
    return result


def _write_footnote_unit_contexts(
    output_dir: Path,
    report: Mapping[str, Any],
) -> dict[str, str]:
    """Write validated sparse contexts without importing the hand-off eagerly."""
    from .footnote_handoff import _write_unit_contexts

    return _write_unit_contexts(output_dir, report)


__all__ = ["validate_footnote_decisions"]

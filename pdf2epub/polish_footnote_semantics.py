"""Contracts for model-authored footnote semantic reviews during PDF polish.

The local validator checks evidence shape and freshness. It does not decide
whether prose belongs to a footnote; that decision remains with the reviewer
and, when needed, the upstream footnote workflow.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping


SCHEMA_VERSION = 1
SIDECAR_DIRECTORY = "polish_footnote_reviews"
_FOOTNOTE_TOKEN = re.compile(r"\[\^([^\]\r\n]+)\]")
_HEX = re.compile(r"^[0-9a-f]{64}$")
_UPSTREAM_CLOSING_OUTCOMES = frozenset({
    "confirmed_not_a_footnote",
    "legitimate_non_footnote",
    "truncated_source_exception",
    "deferred_to_polish",
})


def polish_issue_task_id(filename: str, issue: Mapping[str, Any]) -> str:
    """Compute the canonical upstream footnote audit task_id for one polish issue."""
    identity = "\0".join((
        str(filename),
        str(issue.get("issue_id") or ""),
        str(issue.get("source_sha256") or ""),
        str(issue.get("target_sha256") or ""),
        str(issue.get("context_sha256") or ""),
    ))
    return "fra-" + hashlib.sha256(identity.encode("utf-8")).hexdigest()[:20]


def _upstream_resolved_issue_task_ids(
    output_dir: Path,
    filename: str,
    issues: list[Mapping[str, Any]],
) -> list[str]:
    """Return upstream-closed task IDs when a valid footnote_normalization closed every issue."""
    if not issues:
        return []
    root = Path(output_dir)
    norm_path = root / "footnote_normalization.json"
    try:
        norm = json.loads(norm_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    if not isinstance(norm, Mapping) or norm.get("valid") is not True:
        return []

    closed_task_ids: set[str] = set()
    for candidate_path, keys in (
        (root / "footnote_result_audit.json", ("applied_result_audit_reviews", "reviewed_tasks", "result_audit_reviews")),
        (root / "footnote_decision_validation.json", ("result_audit_reviews",)),
        (norm_path, ("applied_result_audit_reviews", "result_audit_reviews")),
    ):
        try:
            payload = json.loads(candidate_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        for key in keys:
            rows = payload.get(key)
            if not isinstance(rows, list):
                continue
            for row in rows:
                if not isinstance(row, Mapping):
                    continue
                task_id = str(row.get("task_id") or "").strip()
                outcome = str(row.get("outcome") or "").strip()
                if task_id and outcome in _UPSTREAM_CLOSING_OUTCOMES:
                    closed_task_ids.add(task_id)

    expected_task_ids = [polish_issue_task_id(filename, issue) for issue in issues]
    if expected_task_ids and all(tid in closed_task_ids for tid in expected_task_ids):
        return sorted(set(expected_task_ids))
    return []


def sha256_text(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def footnote_identities(text: str) -> list[str]:
    """Return stable marker identities found in references or definitions."""
    from .refine.footnote_result_audit import _visible

    text = _visible(text)
    return sorted({match.group(1).strip() for match in _FOOTNOTE_TOKEN.finditer(text) if match.group(1).strip()})


def semantic_context_sha256(
    *, unit_context_sha256: str | None = None,
    boundary_context_sha256: str | None = None,
) -> str:
    """Bind the review to exact prepared layout and cross-file context inputs."""
    payload = {
        "unit_context_sha256": unit_context_sha256 or "",
        "boundary_context_sha256": boundary_context_sha256 or "",
    }
    encoded = json.dumps(payload, sort_keys=True, separators=(",", ":")).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def build_semantic_review_contract(
    filename: str,
    source_text: str,
    *,
    unit_context_sha256: str | None = None,
    boundary_context_sha256: str | None = None,
    has_boundary_context: bool | None = None,
    sidecar_directory: str = SIDECAR_DIRECTORY,
) -> dict[str, Any]:
    """Build the expected per-file contract placed in the polish manifest."""
    source_hash = sha256_text(source_text)
    safe_stem = Path(filename).stem
    return {
        "schema_version": SCHEMA_VERSION,
        "file": filename,
        "sidecar": f"{sidecar_directory}/{safe_stem}.json",
        "source_sha256": source_hash,
        "context_sha256": semantic_context_sha256(
            unit_context_sha256=unit_context_sha256,
            boundary_context_sha256=boundary_context_sha256,
        ),
        "unit_context_sha256": unit_context_sha256 or "",
        "boundary_context_sha256": boundary_context_sha256 or "",
        "expected_note_ids": footnote_identities(source_text),
        "has_boundary_context": (
            bool(boundary_context_sha256)
            if has_boundary_context is None
            else bool(has_boundary_context)
        ),
        "coverage_requirements": {
            "body_reviewed": True,
            "definitions_reviewed": True,
            "cross_boundary_reviewed": True,
        },
    }


def _nonempty_string(value: Any) -> bool:
    return isinstance(value, str) and bool(value.strip())


def _valid_hash(value: Any) -> bool:
    return isinstance(value, str) and bool(_HEX.fullmatch(value))


def validate_semantic_review_sidecar(
    sidecar_path: Path,
    contract: Mapping[str, Any],
    *,
    source_text: str,
    target_text: str,
) -> dict[str, Any]:
    """Validate a model-authored review sidecar against prepared and current data."""
    errors: list[str] = []
    stale_fields: list[str] = []
    try:
        data = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"review sidecar is missing or unreadable: {exc}"], "stale_fields": []}
    if not isinstance(data, Mapping):
        return {"valid": False, "errors": ["review sidecar must be a JSON object"]}

    if contract.get("schema_version") != SCHEMA_VERSION or data.get("schema_version") != SCHEMA_VERSION:
        errors.append("review contract or sidecar schema version is unsupported")
    filename = str(contract.get("file") or "")
    if data.get("file") != filename:
        errors.append("review sidecar file identity does not match the assigned file")
    expected_source = sha256_text(source_text)
    expected_target = sha256_text(target_text)
    expected_context = contract.get("context_sha256")
    for key, expected in (
        ("source_sha256", expected_source),
        ("target_sha256", expected_target),
        ("context_sha256", expected_context),
    ):
        if not _valid_hash(expected) or data.get(key) != expected:
            errors.append(f"review sidecar {key} is missing, malformed, or stale")
            stale_fields.append(key)
    if contract.get("source_sha256") != expected_source:
        errors.append("prepared source changed after semantic review contract creation")
        stale_fields.append("contract.source_sha256")

    status = data.get("status")
    if not isinstance(status, str) or status not in {"clear", "issues"}:
        errors.append("review sidecar status must be clear or issues")
    if data.get("reviewed_all") is not True:
        errors.append("review sidecar must explicitly attest that the assigned file was fully reviewed")

    coverage = data.get("coverage")
    if not isinstance(coverage, Mapping):
        errors.append("review sidecar coverage must be an object")
        coverage = {}
    for key in ("body_reviewed", "definitions_reviewed", "cross_boundary_reviewed"):
        item = coverage.get(key)
        if not isinstance(item, Mapping):
            errors.append(f"review coverage {key} is missing")
            continue
        state = item.get("status")
        if key == "cross_boundary_reviewed" and state == "not_applicable" and contract.get("has_boundary_context") is not True:
            if not _nonempty_string(item.get("reason")):
                errors.append("cross-boundary not_applicable requires a reason")
        elif state != "reviewed":
            errors.append(f"review coverage {key} must be reviewed")
    expected_ids = contract.get("expected_note_ids")
    reviewed_ids = data.get("reviewed_note_ids")
    if not isinstance(expected_ids, list) or not all(isinstance(item, str) for item in expected_ids):
        errors.append("prepared expected_note_ids contract is malformed")
    elif (
        not isinstance(reviewed_ids, list)
        or not all(isinstance(item, str) for item in reviewed_ids)
        or sorted(set(reviewed_ids)) != sorted(set(expected_ids))
    ):
        errors.append("review sidecar does not cover every expected note identity")

    issues = data.get("issues")
    if not isinstance(issues, list):
        errors.append("review sidecar issues must be a list")
        issues = []
    normalized_issues: list[dict[str, Any]] = []
    issue_ids: set[str] = set()
    for index, item in enumerate(issues):
        if not isinstance(item, Mapping):
            errors.append(f"issue {index + 1} must be an object")
            continue
        for key in ("issue_id", "code", "source_excerpt", "target_excerpt", "reason"):
            if not _nonempty_string(item.get(key)):
                errors.append(f"issue {index + 1} requires {key}")
        issue_id = item.get("issue_id")
        if _nonempty_string(issue_id):
            if issue_id in issue_ids:
                errors.append(f"issue {index + 1} duplicates issue_id")
            issue_ids.add(issue_id)
        if "unit_id" not in item:
            errors.append(f"issue {index + 1} requires unit_id (use null when unknown)")
        if item.get("file") != filename:
            errors.append(f"issue {index + 1} requires the assigned file identity")
        for key, expected in (
            ("source_sha256", expected_source),
            ("target_sha256", expected_target),
            ("context_sha256", expected_context),
        ):
            if item.get(key) != expected:
                errors.append(f"issue {index + 1} {key} is missing or stale")
        source_excerpt = item.get("source_excerpt")
        target_excerpt = item.get("target_excerpt")
        if _nonempty_string(source_excerpt) and source_excerpt not in source_text:
            errors.append(f"issue {index + 1} source_excerpt is not an exact source quotation")
        if _nonempty_string(target_excerpt) and target_excerpt not in target_text:
            errors.append(f"issue {index + 1} target_excerpt is not an exact target quotation")
        normalized_issues.append(dict(item))
    if status == "clear" and issues:
        errors.append("clear review status cannot contain issues")
    if status == "issues" and not issues:
        errors.append("issues review status requires at least one issue")

    return {
        "valid": not errors and status == "clear",
        "contract_valid": not errors,
        "status": status,
        "errors": errors,
        "stale_fields": sorted(set(stale_fields)),
        "issues": normalized_issues,
        "source_sha256": expected_source,
        "target_sha256": expected_target,
        "context_sha256": expected_context,
    }


def validate_prepared_file_review(
    output_dir: Path,
    filename: str,
    *,
    source_text: str,
    target_text: str,
    required: bool,
) -> dict[str, Any] | None:
    """Load and verify the prepared manifest contract for one polish file."""
    manifest_path = Path(output_dir) / "polish_subagent_manifest.json"
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        if required:
            return {"valid": False, "contract_valid": False, "status": "missing", "errors": ["prepared semantic review manifest is missing or unreadable"], "issues": []}
        return None
    root = Path(output_dir).resolve()
    if not isinstance(manifest, Mapping) or manifest.get("task") != "polish":
        return {"valid": False, "contract_valid": False, "status": "missing", "errors": ["prepared semantic review manifest task is not polish"], "issues": []}
    semantic = manifest.get("footnote_semantics") if isinstance(manifest, Mapping) else None
    if not isinstance(semantic, Mapping) or semantic.get("enabled") is not True:
        if required:
            return {"valid": False, "contract_valid": False, "status": "missing", "errors": ["prepared manifest has no required footnote semantic review contract"], "issues": []}
        return None
    contracts = semantic.get("files")
    contract = contracts.get(filename) if isinstance(contracts, Mapping) else None
    if not isinstance(contract, Mapping):
        return {"valid": False, "contract_valid": False, "status": "missing", "errors": ["assigned file has no semantic review contract"], "issues": []}
    errors: list[str] = []
    stale_fields: list[str] = []
    unit_hashes = manifest.get("unit_context_sha256", {})
    boundary_hashes = manifest.get("boundary_context_sha256", {})
    boundary_sources = manifest.get("boundary_context_sources", {})
    unit_hash = unit_hashes.get(filename) if isinstance(unit_hashes, Mapping) else None
    boundary_hash = boundary_hashes.get(filename) if isinstance(boundary_hashes, Mapping) else None
    boundary_links = boundary_sources.get(filename, []) if isinstance(boundary_sources, Mapping) else []
    has_boundary = isinstance(boundary_links, list) and bool(boundary_links)
    expected = build_semantic_review_contract(
        filename, source_text, unit_context_sha256=unit_hash,
        boundary_context_sha256=boundary_hash,
        has_boundary_context=has_boundary,
    )
    for key in ("source_sha256", "context_sha256", "unit_context_sha256", "boundary_context_sha256", "expected_note_ids", "has_boundary_context", "sidecar", "coverage_requirements"):
        if contract.get(key) != expected.get(key):
            errors.append(f"prepared semantic review contract {key} is stale or inconsistent")
            stale_fields.append(f"contract.{key}")
    parent_source_hashes = manifest.get("source_sha256", {})
    if not isinstance(parent_source_hashes, Mapping) or parent_source_hashes.get(filename) != expected["source_sha256"]:
        errors.append("parent prepared source hash is missing or stale")
        stale_fields.append("manifest.source_sha256")
    sidecar_relative = contract.get("sidecar")
    if not isinstance(sidecar_relative, str):
        errors.append("prepared semantic review sidecar path is malformed")
        sidecar = root / "__missing_semantic_review__.json"
    else:
        sidecar_candidate = Path(sidecar_relative)
        unresolved_sidecar = sidecar_candidate if sidecar_candidate.is_absolute() else root / sidecar_candidate
        sidecar = unresolved_sidecar.resolve()
        try:
            relative_candidate = unresolved_sidecar.relative_to(root)
            if unresolved_sidecar.is_symlink() or any(
                (root / part).is_symlink()
                for part in relative_candidate.parents
                if str(part) != "."
            ):
                errors.append("prepared semantic review sidecar may not traverse symlinks")
        except ValueError:
            pass
        if sidecar != root and root not in sidecar.parents:
            errors.append("prepared semantic review sidecar path escapes output workspace")
            sidecar = root / "__missing_semantic_review__.json"
    result = validate_semantic_review_sidecar(
        sidecar, contract, source_text=source_text, target_text=target_text,
    )
    if errors:
        result["errors"] = errors + list(result.get("errors", []))
        result["valid"] = False
        result["contract_valid"] = False
    elif result.get("contract_valid") is True and result.get("status") == "issues":
        resolved_ids = _upstream_resolved_issue_task_ids(
            root, filename, result.get("issues", [])
        )
        if resolved_ids:
            result["valid"] = True
            result["resolved_by_upstream_audit"] = True
            result["resolved_issue_task_ids"] = resolved_ids
    result["stale_fields"] = sorted(set([*result.get("stale_fields", []), *stale_fields]))
    result["sidecar"] = str(sidecar_relative or "")
    try:
        result["sidecar_sha256"] = hashlib.sha256(sidecar.read_bytes()).hexdigest()
    except OSError:
        result["sidecar_sha256"] = None
    return result


__all__ = [
    "SCHEMA_VERSION", "SIDECAR_DIRECTORY", "build_semantic_review_contract",
    "footnote_identities", "semantic_context_sha256", "sha256_text",
    "validate_semantic_review_sidecar",
    "validate_prepared_file_review",
]

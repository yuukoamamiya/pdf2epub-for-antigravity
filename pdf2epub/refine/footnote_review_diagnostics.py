"""Read-only cost and coverage summary for existing footnote review artifacts."""
from __future__ import annotations

import hashlib
import json
from collections import Counter, defaultdict
from pathlib import Path
from typing import Any, Mapping

from pdf2epub.footnote_keys import canonical_numeric_label

DIAGNOSTIC_SCHEMA_VERSION = 1
ARTIFACTS = {
    "candidates": "footnote_candidates.json",
    "visual_audit_manifest": "footnote_visual_audit_manifest.json",
    "subagent_manifest": "footnote_subagent_manifest.json",
    "decisions": "footnote_decisions.json",
    "validation": "footnote_decision_validation.json",
    "page_probe": "pdf_text_probe.json",
}
ROLES = {"body", "citation", "bibliography", "footnote_start", "footnote_continuation", "footnote_definition", "review_required"}


class ArtifactError(ValueError):
    """An existing diagnostic artifact cannot be safely interpreted."""


def _load(path: Path) -> dict[str, Any] | None:
    if not path.exists():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ArtifactError(f"{path.name} is unreadable or invalid JSON: {exc}") from exc
    if not isinstance(value, dict):
        raise ArtifactError(f"{path.name} must contain a JSON object")
    return value


def _artifact_inventory(output_dir: Path) -> tuple[dict[str, dict[str, Any]], dict[str, dict[str, Any]]]:
    loaded: dict[str, dict[str, Any] | None] = {}
    inventory: dict[str, dict[str, Any]] = {}
    for key, name in ARTIFACTS.items():
        path = output_dir / name
        loaded[key] = _load(path)
        if path.is_file():
            inventory[name] = {"path": str(path), "status": "readable", "bytes": path.stat().st_size, "sha256": hashlib.sha256(path.read_bytes()).hexdigest()}
        else:
            inventory[name] = {"path": str(path), "status": "missing", "bytes": None, "sha256": None}
    return loaded, inventory


def _valid_address(row: Mapping[str, Any], *, page_default: Any = None, source_default: str = "primary") -> tuple[int, str, int] | None:
    try:
        page = int(row.get("page", row.get("page_number", page_default)))
        block = int(row.get("block", row.get("block_index", row.get("block_id"))))
        source = str(row.get("source") or row.get("source_type") or source_default).strip().lower()
    except (TypeError, ValueError):
        return None
    if page <= 0 or block < 0 or source not in {"primary", "secondary"}:
        return None
    return page, source, block


def _candidate_data(report: Mapping[str, Any], tasks: list[Mapping[str, Any]]) -> dict[str, Any]:
    pages = report.get("pages")
    if not isinstance(pages, list):
        raise ArtifactError("footnote_candidates.json is missing a pages array")
    addresses: set[tuple[int, str, int]] = set()
    required: set[tuple[int, str, int]] = set()
    high: set[tuple[int, str, int]] = set()
    by_address: dict[tuple[int, str, int], Mapping[str, Any]] = {}
    review_reasons: Counter[str] = Counter()
    reason_addresses: set[tuple[tuple[int, str, int], str]] = set()
    invalid = 0
    fields = (("candidates", "primary"), ("audit_candidates", "primary"), ("secondary_only_candidates", "secondary"))
    for page in pages:
        if not isinstance(page, Mapping):
            invalid += 1
            continue
        for field, default_source in fields:
            rows = page.get(field, [])
            if not isinstance(rows, list):
                raise ArtifactError(f"footnote_candidates.json pages[{page.get('page', '?')}].{field} must be an array")
            for row in rows:
                if not isinstance(row, Mapping):
                    invalid += 1
                    continue
                address = _valid_address(row, page_default=page.get("page"), source_default=default_source)
                if address is None:
                    invalid += 1
                    continue
                addresses.add(address)
                by_address[address] = row
                if row.get("confidence") == "review":
                    required.add(address)
                    reason = row.get("review_reason")
                    values = reason if isinstance(reason, list) else [reason]
                    for value in set(str(x) for x in values if x is not None):
                        reason_addresses.add((address, value))
                elif row.get("confidence") == "high":
                    high.add(address)
    # Region block copies provide address evidence but never widen the review queue.
    for task in tasks:
        if task.get("region_id") is None:
            continue
        blocks = task.get("blocks", [])
        if not isinstance(blocks, list):
            invalid += 1
            continue
        for row in blocks:
            if not isinstance(row, Mapping):
                invalid += 1
                continue
            address = _valid_address(row, page_default=task.get("page"))
            if address is not None:
                addresses.add(address)
    if "review_pages" in report and not isinstance(report["review_pages"], list):
        raise ArtifactError("footnote_candidates.json review_pages must be an array")
    review_pages: set[int] = set()
    for value in report.get("review_pages", []) or []:
        try:
            page_number = int(value)
            if page_number <= 0: raise ValueError("page must be positive")
            review_pages.add(page_number)
        except (TypeError, ValueError):
            invalid += 1
    for _, reason in reason_addresses: review_reasons[reason] += 1
    return {"addresses": addresses, "required": required, "high": high, "by_address": by_address,
        "review_reasons": review_reasons, "review_pages": review_pages, "invalid": invalid}


def _decision_rows(data: Mapping[str, Any]) -> tuple[list[Any], list[Any], int]:
    raw_flat = data.get("decisions", [])
    if not isinstance(raw_flat, list):
        raise ArtifactError("footnote_decisions.json decisions must be an array")
    flat = list(raw_flat)
    raw_regions = data.get("region_audits", [])
    malformed = 0
    nested: list[Any] = []
    if isinstance(raw_regions, list):
        region_rows = [(None, item) for item in raw_regions]
    elif isinstance(raw_regions, Mapping):
        region_rows = list(raw_regions.items())
    elif raw_regions is None:
        region_rows = []
    else:
        raise ArtifactError("footnote_decisions.json region_audits must be an array or object")
    for inherited_id, audit in region_rows:
        if not isinstance(audit, Mapping):
            malformed += 1
            continue
        region_id = audit.get("region_id", inherited_id)
        rows = audit.get("decisions", [])
        if not isinstance(rows, list):
            malformed += 1
            continue
        for row in rows:
            if isinstance(row, Mapping):
                nested.append({**row, "region_id": row.get("region_id", region_id)})
            else:
                nested.append(row)
    return flat, nested, malformed


def _material_signature(row: Mapping[str, Any], candidate: Mapping[str, Any] | None = None) -> str:
    address = _valid_address(row)
    role = row.get("role")
    raw_key = row.get("key")
    key = canonical_numeric_label(str(raw_key).strip()) if raw_key is not None else None
    if raw_key is not None and key is None:
        key = str(raw_key).strip()
    marker_label = row.get("marker_label")
    if marker_label is None and candidate is not None:
        marker_label = candidate.get("marker_label")
    primary_blocks = row.get("primary_blocks")
    if primary_blocks is None and row.get("primary_block") is not None:
        primary_blocks = [row["primary_block"]]
    if primary_blocks is None and str(row.get("primary_disposition") or "").strip().lower() == "absent":
        primary_blocks = []
    if isinstance(primary_blocks, list):
        try: primary_blocks = sorted(set(int(x) for x in primary_blocks))
        except (TypeError, ValueError): pass
    signature = {
        "address": address, "role": role, "key": key, "marker_label": marker_label,
        "marker_context": row.get("marker_context"), "text": row.get("text"),
        "source_file": row.get("source_file"),
        "primary_disposition": str(row.get("primary_disposition") or "").strip().lower() or None,
        "primary_blocks": primary_blocks,
    }
    return json.dumps(signature, sort_keys=True, ensure_ascii=False)


def _decision_aggregate(data: Mapping[str, Any] | None, candidate_data: Mapping[str, Any], validation: Mapping[str, Any] | None, tasks: list[Mapping[str, Any]]) -> dict[str, Any]:
    if data is None:
        return {"diagnostics": {"flat_entries": None, "nested_entries": None, "duplicate_entries": None, "conflicting_addresses": None, "missing_decision_addresses": None, "unknown_addresses": None, "invalid_entries": None, "unknown_region_ids": None, "conflicting_region_ids": None, "unmatched_region_decisions": None}, "roles": {}, "dispositions": {}, "by_reason": {}, "snapshot": "unavailable"}
    flat, nested, malformed = _decision_rows(data)
    addresses = candidate_data["addresses"]
    by_address: dict[tuple[int, str, int], list[Mapping[str, Any]]] = defaultdict(list)
    invalid = malformed
    unknown = 0
    candidate_by_address = candidate_data["by_address"]
    for row in [*flat, *nested]:
        if not isinstance(row, Mapping):
            invalid += 1
            continue
        address = _valid_address(row)
        role = row.get("role")
        if address is None or not isinstance(role, str) or role not in ROLES:
            invalid += 1
            continue
        if address not in addresses:
            unknown += 1
        by_address[address].append(row)
    conflict_addresses = set()
    duplicates = 0
    for address, rows in by_address.items():
        sigs = {_material_signature(row, candidate_by_address.get(address)) for row in rows}
        duplicates += len(rows) - len(sigs)
        if len(sigs) > 1:
            conflict_addresses.add(address)
    missing = len(candidate_data["required"] - set(by_address))
    roles = Counter()
    for address, rows in by_address.items():
        if address not in conflict_addresses:
            roles[rows[0]["role"]] += 1

    known_regions = {str(task["region_id"]) for task in tasks if task.get("region_id") is not None}
    region_tasks = {str(task["region_id"]): task for task in tasks if task.get("region_id") is not None}
    audits = data.get("region_audits", [])
    if audits is None:
        audits = []
    audit_rows = list(audits) if isinstance(audits, list) else [{"region_id": k, **v} for k, v in audits.items() if isinstance(v, Mapping)]
    dispositions_by_id: dict[str, set[str]] = defaultdict(set)
    audit_counts = Counter()
    unknown_region_ids = 0
    invalid_audits = 0
    for audit in audit_rows:
        if not isinstance(audit, Mapping):
            invalid_audits += 1
            continue
        region_id = str(audit.get("region_id") or "")
        disposition = audit.get("disposition")
        if not region_id or region_id not in known_regions:
            unknown_region_ids += 1
            continue
        if not isinstance(disposition, str) or disposition not in {"footnote_region", "body_region", "review_required"}:
            invalid_audits += 1
            continue
        dispositions_by_id[region_id].add(disposition)
        audit_counts[region_id] += 1
    conflicting_regions = {rid for rid, values in dispositions_by_id.items() if len(values) > 1}
    dispositions = Counter()
    by_reason: dict[str, Counter] = defaultdict(Counter)
    duplicate_region_audits = sum(max(0, count - 1) for count in audit_counts.values())
    for rid, values in dispositions_by_id.items():
        if rid in conflicting_regions or len(values) != 1:
            continue
        disposition = next(iter(values))
        dispositions[disposition] += 1
        reasons = region_tasks[rid].get("reasons", [])
        if isinstance(reasons, str): reasons = [reasons]
        if isinstance(reasons, list):
            for reason in set(str(x) for x in reasons): by_reason[reason][disposition] += 1
    unmatched = len(known_regions - set(dispositions_by_id)) if data is not None else None

    validated = validation.get("decisions") if validation else None
    if isinstance(validated, list):
        current = {_material_signature(row, candidate_by_address.get(_valid_address(row))) for row in [*flat, *nested] if isinstance(row, Mapping) and _valid_address(row) and isinstance(row.get("role"), str) and row.get("role") in ROLES}
        previous = {_material_signature(row, candidate_by_address.get(_valid_address(row))) for row in validated if isinstance(row, Mapping) and _valid_address(row) and isinstance(row.get("role"), str)}
        snapshot = "matches_after_candidate_defaults" if current == previous else "changed_since_validation"
    else:
        snapshot = "unavailable"
    return {
        "diagnostics": {"flat_entries": len(flat), "nested_entries": len(nested), "duplicate_entries": duplicates,
            "conflicting_addresses": len(conflict_addresses), "missing_decision_addresses": missing,
            "unknown_addresses": unknown, "invalid_entries": invalid + invalid_audits,
            "unknown_region_ids": unknown_region_ids, "duplicate_region_audits": duplicate_region_audits,
            "conflicting_region_ids": sorted(conflicting_regions), "unmatched_region_decisions": unmatched},
        "roles": dict(roles), "dispositions": dict(dispositions), "by_reason": {k: dict(v) for k, v in by_reason.items()}, "snapshot": snapshot,
    }


def _region_aggregate(manifest: Mapping[str, Any]) -> tuple[list[Mapping[str, Any]], dict[str, Any]]:
    tasks = manifest.get("tasks")
    if tasks is None:
        for legacy_key in ("regions", "audit_regions", "region_audits"):
            value = manifest.get(legacy_key)
            if isinstance(value, list):
                tasks = value
                break
            if isinstance(value, Mapping):
                tasks = [{**item, "region_id": key} for key, item in value.items() if isinstance(item, Mapping)]
                break
    if not isinstance(tasks, list):
        raise ArtifactError("footnote_visual_audit_manifest.json has no recognized task/region array")
    if any(not isinstance(task, Mapping) for task in tasks):
        raise ArtifactError("footnote_visual_audit_manifest.json tasks must contain objects")
    regions = [task for task in tasks if task.get("region_id") is not None]
    ids = [str(task["region_id"]) for task in regions]
    reason_counts = Counter()
    reason_combinations = Counter()
    page_set = set()
    for task in regions:
        try:
            page = int(task.get("page"))
            if page > 0: page_set.add(page)
        except (TypeError, ValueError): pass
        reasons = task.get("reasons", [])
        if isinstance(reasons, str): reasons = [reasons]
        if isinstance(reasons, list):
            normalized = tuple(sorted(set(str(reason) for reason in reasons)))
            for reason in normalized: reason_counts[reason] += 1
            reason_combinations[" + ".join(normalized) or "(no reason)"] += 1
    return regions, {"region_count": len(regions), "duplicate_region_ids": len(ids) - len(set(ids)),
        "actual_review_region_pages": len(page_set), "reason_counts": dict(reason_counts),
        "reason_combinations": dict(reason_combinations)}


def _cost_inventory(output_dir: Path, loaded: Mapping[str, Any], artifacts: Mapping[str, Any]) -> dict[str, Any]:
    files: dict[str, dict[str, Any]] = {}
    for name, detail in artifacts.items():
        if detail["status"] == "readable":
            category = ("subagent_manifest" if name == ARTIFACTS["subagent_manifest"] else
                        "visual_audit_manifest" if name == ARTIFACTS["visual_audit_manifest"] else "input_artifact")
            files[name] = {"bytes": detail["bytes"], "category": category}
    candidates = loaded.get("candidates") or {}
    evidence = candidates.get("visual_evidence", {})
    image_files = evidence.get("files") if isinstance(evidence, Mapping) else None
    image_refs = list(image_files.values()) if isinstance(image_files, Mapping) else None
    manifest = loaded.get("visual_audit_manifest") or {}
    crop_refs: list[str] = []
    for task in manifest.get("tasks", []) if isinstance(manifest.get("tasks", []), list) else []:
        if not isinstance(task, Mapping): continue
        if isinstance(task.get("visual_file"), str):
            if image_refs is None: image_refs = []
            image_refs.append(task["visual_file"])
        crop = task.get("region_visual_file")
        if isinstance(crop, str): crop_refs.append(crop)
        vf = task.get("visual_files")
        if isinstance(vf, Mapping):
            for item in vf.values():
                if isinstance(item, Mapping) and isinstance(item.get("path"), str):
                    if image_refs is None: image_refs = []
                    image_refs.append(item["path"])
    for name, refs, category in (("page_images", image_refs, "page_image"), ("region_crop_images", crop_refs, "crop_image")):
        if refs is None: continue
        for ref in set(x for x in refs if isinstance(x, str)):
            path = (output_dir / ref).resolve()
            try: path.relative_to(output_dir.resolve())
            except ValueError: continue
            if path.is_file(): files[path.relative_to(output_dir.resolve()).as_posix()] = {"bytes": path.stat().st_size, "category": category}
    prompt_path = output_dir / "footnote_subagent_prompt.md"
    if prompt_path.is_file(): files[prompt_path.name] = {"bytes": prompt_path.stat().st_size, "category": "prompt"}
    subagent = loaded.get("subagent_manifest") or {}
    contexts = subagent.get("unit_context_files", {})
    if isinstance(contexts, Mapping):
        for relative in contexts.values():
            if not isinstance(relative, str): continue
            path = (output_dir / relative).resolve()
            try: path.relative_to(output_dir.resolve())
            except ValueError: continue
            if path.is_file(): files[path.relative_to(output_dir.resolve()).as_posix()] = {"bytes": path.stat().st_size, "category": "unit_context"}
    totals = Counter()
    for detail in files.values(): totals[detail["category"]] += detail["bytes"]
    handoff_bytes = sum(totals[k] for k in ("subagent_manifest", "visual_audit_manifest", "prompt", "unit_context"))
    valid_image_refs = [x for x in image_refs if isinstance(x, str)] if image_refs is not None else None
    return {"files": files, "bytes_by_category": dict(totals), "total_unique_bytes": sum(totals.values()),
        "handoff_unique_bytes": handoff_bytes,
        "unique_page_images": len(set(valid_image_refs)) if valid_image_refs is not None else None,
        "repeated_page_image_references": len(valid_image_refs) - len(set(valid_image_refs)) if valid_image_refs is not None else None,
        "unique_region_crop_images": len(set(crop_refs)) if crop_refs else None}


def build_footnote_review_summary(output_dir: Path) -> dict[str, Any]:
    output_dir = Path(output_dir)
    loaded, artifacts = _artifact_inventory(output_dir)
    candidates = loaded["candidates"]
    manifest = loaded["visual_audit_manifest"]
    if candidates is None or manifest is None:
        raise ArtifactError("required footnote candidate report or visual audit manifest is missing")
    regions, region_summary = _region_aggregate(manifest)
    candidate_data = _candidate_data(candidates, regions)
    decision = _decision_aggregate(loaded["decisions"], candidate_data, loaded["validation"], regions)
    costs = _cost_inventory(output_dir, loaded, artifacts)
    probe = loaded["page_probe"] or {}
    page_count = probe.get("page_count") if isinstance(probe.get("page_count"), int) else None
    review_pages = candidates.get("review_pages")
    continuation = candidates.get("continuation_reviews")
    windows = candidates.get("cross_page_windows")
    return {
        "schema_version": DIAGNOSTIC_SCHEMA_VERSION, "diagnostic_only": True, "production_validated": False,
        "output_dir": str(output_dir), "artifacts": artifacts,
        "missing_artifacts": [name for name, detail in artifacts.items() if detail["status"] == "missing"],
        "counts": {"pdf_total_pages": page_count, "actual_review_region_pages": region_summary["actual_review_region_pages"],
            "review_pages_including_context": len(candidate_data["review_pages"]) if isinstance(review_pages, list) else None,
            "regions": region_summary["region_count"], "duplicate_region_ids": region_summary["duplicate_region_ids"],
            "review_candidate_blocks": len(candidate_data["required"]), "high_confidence_candidates": len(candidate_data["high"]),
            "continuation_reviews": len(continuation) if isinstance(continuation, list) else None,
            "cross_page_windows": len(windows) if isinstance(windows, list) else None,
            "unique_supporting_page_images": costs["unique_page_images"],
            "repeated_page_image_references": costs["repeated_page_image_references"],
            "unique_region_crop_images": costs["unique_region_crop_images"],
            "handoff_files_bytes": costs["handoff_unique_bytes"] if any(costs["bytes_by_category"].get(k, 0) for k in ("subagent_manifest", "visual_audit_manifest", "prompt", "unit_context")) else None},
        "region_reasons_multi_select": region_summary["reason_counts"],
        "region_reason_combinations": region_summary["reason_combinations"],
        "candidate_review_reason_distribution": dict(candidate_data["review_reasons"]),
        "decision_diagnostics": {**decision["diagnostics"], "invalid_candidate_entries": candidate_data["invalid"]},
        "reviewer_region_disposition_distribution": decision["dispositions"], "reviewer_role_distribution": decision["roles"],
        "region_dispositions_by_region_reason": decision["by_reason"],
        "validation_freshness": "candidate_report_stale" if loaded["validation"] and loaded["validation"].get("candidate_report_sha256") and loaded["validation"]["candidate_report_sha256"] != artifacts[ARTIFACTS["candidates"]]["sha256"] else ("candidate_report_hash_matches" if loaded["validation"] and loaded["validation"].get("candidate_report_sha256") else "candidate_hash_unavailable"),
        "decision_snapshot": decision["snapshot"],
        "decision_snapshot_note": "Canonical semantic comparison after candidate marker defaults; this is not a raw decision-file hash guarantee.",
        "cost_inventory": costs,
        "notes": ["Reviewer dispositions and roles are descriptive review records, not ground truth.", "Candidate review_reason is separate from region blocking reasons.", "Missing legacy fields are unknown, not zero.", "Context-only adjacent pages remain evidence; this diagnostic does not recommend removing windows.", "No source excerpts, worker counts, or token estimates are emitted."],
    }

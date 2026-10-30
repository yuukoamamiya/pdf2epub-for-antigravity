"""Pure audits for the exact-address footnote decision contract."""
from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Sequence

from ..footnote_keys import canonical_numeric_label
from .footnote_contracts import DECISION_ROLES, MOVED_ROLES

Address = tuple[int, str, int]


def candidate_index(report: Mapping[str, Any]) -> dict[Address, dict[str, Any]]:
    index: dict[Address, dict[str, Any]] = {}
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


def required_review_addresses(report: Mapping[str, Any]) -> set[Address]:
    # Preserve the original comprehension's fail-closed behavior: malformed
    # review entries raise instead of disappearing from the required set.
    required = {
        (int(candidate["page"]), "primary", int(candidate["block"]))
        for page in report.get("pages", []) or []
        for candidate in page.get("candidates", []) or []
        if candidate.get("confidence") == "review"
    }
    required.update(
        (int(candidate["page"]), "secondary", int(candidate["block"]))
        for page in report.get("pages", []) or []
        for candidate in page.get("secondary_only_candidates", []) or []
        if candidate.get("confidence") == "review"
    )
    required.update(
        (int(candidate["page"]), "primary", int(candidate["block"]))
        for page in report.get("pages", []) or []
        for candidate in page.get("audit_candidates", []) or []
        if candidate.get("confidence") == "review"
    )
    return required


def audit_region_decisions(
    decisions_data: Mapping[str, Any], audit_tasks: set[str]
) -> tuple[set[str], list[Mapping[str, Any]], list[str], list[str]]:
    """Validate region decisions and return IDs, nested items, review, errors."""
    errors: list[str] = []
    human_review_required: list[str] = []
    region_audits = decisions_data.get("region_audits")
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
        errors.extend(
            f"missing region audit for {region_id}"
            for region_id in sorted(audit_tasks - region_audit_ids)
        )
    return region_audit_ids, nested_decisions, human_review_required, errors


def merge_region_and_flat_decisions(
    nested_decisions: Sequence[Mapping[str, Any]], decisions: Sequence[Any]
) -> list[Any]:
    """Merge nested and legacy flat decisions, retaining first exact address."""
    merged: list[Any] = []
    merged_addresses: set[Address] = set()
    for item in [*nested_decisions, *decisions]:
        if not isinstance(item, Mapping):
            merged.append(item)
            continue
        try:
            address = (
                int(item["page"]),
                str(item.get("source") or "primary").strip().lower(),
                int(item["block"]),
            )
        except (KeyError, TypeError, ValueError):
            merged.append(item)
            continue
        if address in merged_addresses:
            continue
        merged_addresses.add(address)
        merged.append(item)
    return merged


def normalize_decisions(
    decisions: Sequence[Any], candidates: Mapping[Address, Mapping[str, Any]]
) -> tuple[list[dict[str, Any]], list[str], list[str], set[Address]]:
    """Validate and normalize each decision without reading or writing files."""
    errors: list[str] = []
    human_review_required: list[str] = []
    seen: set[Address] = set()
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
        if role not in DECISION_ROLES:
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
                errors.append(f"invalid footnote key for {address[0]}:{address[1]}:{address[2]}")
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
        marker_kind = str(marker_identity.get("kind") or "") if isinstance(marker_identity, Mapping) else ""
        if (
            role in {"footnote_start", "footnote_definition"}
            and candidate_marker_label
            and marker_kind != "numeric"
            and decision_marker_label != candidate_marker_label
        ):
            errors.append(
                "decision marker_label must preserve printed marker for "
                f"{address[0]}:{address[1]}:{address[2]}"
            )
            continue

        primary_disposition = ""
        normalized_primary_blocks: list[int] = []
        if source == "secondary" and role in MOVED_ROLES:
            if not str(item.get("text") or "").strip():
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
            primary_disposition = str(item.get("primary_disposition") or "").strip().lower()
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
                    raw_primary_blocks = [raw_primary_block] if raw_primary_block is not None else None
                if not isinstance(raw_primary_blocks, list) or not raw_primary_blocks:
                    errors.append(
                        "secondary-only footnote with primary_disposition=remove must "
                        f"include primary_block(s) for {address[0]}:{address[2]}"
                    )
                    continue
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
            elif item.get("primary_block") is not None or item.get("primary_blocks") is not None:
                errors.append(
                    "secondary-only footnote with primary_disposition=absent must "
                    f"not include primary_block(s) for {address[0]}:{address[2]}"
                )
                continue

        decision = dict(item)
        decision["page"], decision["source"], decision["block"], decision["role"] = (
            address[0], address[1], address[2], role
        )
        if key is not None:
            decision["key"] = key
        if candidate_marker_label:
            decision["marker_label"] = candidate_marker_label
        if candidate.get("requires_marker_context") is True:
            decision["requires_marker_context"] = True
        if source == "secondary" and role in MOVED_ROLES:
            decision["primary_disposition"] = primary_disposition
            decision["primary_blocks"] = normalized_primary_blocks
        if role == "review_required":
            human_review_required.append(f"{address[0]}:{address[1]}:{address[2]}")
        normalized.append(decision)
    return normalized, errors, human_review_required, seen


def validate_continuation_traceability(
    candidates: Mapping[Address, Mapping[str, Any]],
    normalized: Sequence[Mapping[str, Any]],
) -> list[str]:
    key_sources = {
        str(candidate.get("key"))
        for candidate in candidates.values()
        if candidate.get("key")
    }
    key_sources.update(
        str(item.get("key"))
        for item in normalized
        if item.get("role") in {"footnote_start", "footnote_definition"}
        and item.get("key")
    )
    errors: list[str] = []
    for item in normalized:
        if item.get("role") != "footnote_continuation" or not item.get("key"):
            continue
        if str(item["key"]) not in key_sources:
            errors.append(
                f"continuation {item['page']}:{item['block']} has no traceable footnote key"
            )
    return errors

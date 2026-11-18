"""Compile exact-address footnote candidates and explicit reviews into one audited set."""
from __future__ import annotations

import hashlib
import json
from copy import deepcopy
from typing import Any, Mapping, Sequence

EFFECTIVE_DECISIONS_VERSION = 1


def _address(item: Mapping[str, Any], default_source: str = "primary") -> tuple[int, str, int] | None:
    try:
        return (
            int(item["page"]),
            str(item.get("source") or default_source).strip().lower(),
            int(item["block"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


def candidate_index(report: Mapping[str, Any]) -> dict[tuple[int, str, int], dict[str, Any]]:
    """Index every candidate collection by its normalized exact address."""
    candidates: dict[tuple[int, str, int], dict[str, Any]] = {}
    for page_report in report.get("pages", []) or []:
        if not isinstance(page_report, Mapping):
            continue
        for field, default_source in (
            ("candidates", "primary"),
            ("audit_candidates", "primary"),
            ("secondary_only_candidates", "secondary"),
        ):
            for item in page_report.get(field, []) or []:
                if not isinstance(item, Mapping):
                    continue
                address = _address(item, default_source)
                if address is not None:
                    # Match the established audit behavior: a later duplicate
                    # exact address replaces its earlier projection.
                    candidates[address] = dict(item)
    return candidates


def _candidate_evidence_id(
    report: Mapping[str, Any], address: tuple[int, str, int], candidate: Mapping[str, Any]
) -> str:
    page, source, block = address
    hash_field = "secondary_sidecar_sha256" if source == "secondary" else "sidecar_sha256"
    from .footnote_page_evidence import resolve_page_sidecar_path

    inventory = report.get(hash_field) or {}
    source_root = "ocr_secondary" if source == "secondary" else "pages"
    page_name = resolve_page_sidecar_path(inventory, page, source_root)
    sidecar_hash = inventory.get(page_name) if page_name else None
    payload = {
        "address": [page, source, block],
        "sidecar_sha256": sidecar_hash,
        "candidate": dict(candidate),
    }
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def project_confirmed_not_a_footnote(
    report: Mapping[str, Any],
    decisions: Mapping[str, Any],
    records: Sequence[Mapping[str, Any]],
) -> tuple[dict[str, Any], dict[str, Any]]:
    """Merge confirmed body-gap outcomes into exact candidate decisions."""
    projected_report = deepcopy(dict(report))
    projected_decisions = deepcopy(dict(decisions))
    reviews = projected_decisions.get("continuation_reviews", [])
    explicit = list(projected_decisions.get("decisions", []) or [])
    candidates = candidate_index(report)
    body_exceptions: list[dict[str, Any]] = []
    for review in reviews if isinstance(reviews, list) else []:
        if not isinstance(review, Mapping) or review.get("outcome") != "confirmed_not_a_footnote":
            continue
        source_note = review.get("source_note")
        if not isinstance(source_note, Mapping):
            continue
        address = _address(source_note)
        if address is None:
            continue
        candidate = candidates.get(address)
        candidate_key = str(candidate.get("key") or "") if candidate else ""
        chapter_id = next((
            str(record.get("chapter_id") or "") for record in records
            if int(record.get("start_page", 0)) <= address[0] <= int(record.get("end_page", 0))
        ), "")
        body_exception = {
            "page": address[0], "source": address[1], "block": address[2],
            "role": "body", "key": candidate_key, "override": True,
            "_trusted_decision_source": "confirmed_not_a_footnote",
            "chapter_id": chapter_id,
            **({"continuation_task_id": review["task_id"]} if review.get("task_id") else {}),
        }
        matches: list[Mapping[str, Any]] = []
        for item in explicit:
            if not isinstance(item, Mapping):
                continue
            item_address = _address(item)
            if item_address is None:
                continue
            if item_address == address:
                matches.append(item)
        if len(matches) == 1:
            existing = matches[0]
            existing_role = str(existing.get("role") or "").strip()
            existing_key = str(existing.get("key") or candidate_key).strip()
            if existing_role == "body" and existing_key == candidate_key:
                merged = dict(existing)
                merged.update({
                    "override": True,
                    "_trusted_decision_source": "confirmed_not_a_footnote",
                })
                if review.get("task_id"):
                    merged["continuation_task_id"] = review.get("task_id")
                explicit[explicit.index(existing)] = merged
                continue
            explicit.append({
                **body_exception,
                "_projection_conflict": (
                    f"confirmed_not_a_footnote says body but explicit decision says "
                    f"{existing_role or '<missing>'} at {address[0]}:{address[1]}:{address[2]}"
                ),
            })
            continue
        if len(matches) > 1:
            explicit.append({
                **body_exception,
                "_projection_conflict": f"confirmed_not_a_footnote conflicts with duplicate explicit decisions at {address[0]}:{address[1]}:{address[2]}",
            })
            continue
        body_exceptions.append(body_exception)
    projected_decisions["decisions"] = [*explicit, *body_exceptions]
    return projected_report, projected_decisions


def compile_effective_decisions(
    report: Mapping[str, Any], explicit_decisions: Sequence[Any] | None
) -> dict[str, Any]:
    """Return one decision per exact candidate address, with provenance and identity.

    High confidence local roles form the baseline. Explicit decisions replace that
    baseline only when their role/key changes are marked as an override.
    """
    candidates = candidate_index(report)

    explicit_by_address: dict[tuple[int, str, int], Mapping[str, Any]] = {}
    errors: list[str] = []
    for item in explicit_decisions or []:
        if isinstance(item, Mapping) and item.get("_projection_conflict"):
            errors.append(str(item["_projection_conflict"]))
            continue
        if not isinstance(item, Mapping):
            errors.append("each explicit effective decision must be an object")
            continue
        address = _address(item)
        if address is None:
            errors.append("explicit effective decision is missing an integer page/block address")
            continue
        if address not in candidates:
            errors.append(f"override points to a non-candidate block: {address[0]}:{address[1]}:{address[2]}")
            continue
        if address in explicit_by_address:
            errors.append(f"duplicate effective decision for {address[0]}:{address[1]}:{address[2]}")
            continue
        explicit_by_address[address] = item

    effective: list[dict[str, Any]] = []
    for address, candidate in sorted(candidates.items()):
        confidence = str(candidate.get("confidence") or "")
        local_role = str(candidate.get("local_role") or "").strip()
        label = str(candidate.get("label") or "").strip().lower()
        baseline_role = local_role
        if confidence == "high" and not baseline_role and label == "footnote" and candidate.get("key"):
            baseline_role = "footnote_start"
        if address not in explicit_by_address and confidence != "high":
            continue
        # Some candidates have no deterministic role and must be explicitly reviewed.
        explicit = explicit_by_address.get(address)
        if explicit is None:
            synthesized_role = baseline_role
            if not synthesized_role:
                errors.append(f"high-confidence candidate has no local_role or deterministic footnote label: {address[0]}:{address[1]}:{address[2]}")
                continue
            bbox = candidate.get("bbox")
            if label != "footnote" and isinstance(bbox, (list, tuple)) and len(bbox) > 1:
                try:
                    if float(bbox[1]) < 0.45:
                        continue
                except (TypeError, ValueError):
                    pass
            decision = {
                "page": address[0], "source": address[1], "block": address[2],
                "role": synthesized_role, "key": str(candidate.get("key") or ""),
                "marker_label": str(candidate.get("marker_label") or ""),
                "requires_marker_context": candidate.get("requires_marker_context") is True,
                "decision_source": "local_high" if local_role else "local_high_footnote_label",
            }
        else:
            decision = dict(explicit)
            decision["page"], decision["source"], decision["block"] = address
            trusted_source = decision.pop("_trusted_decision_source", None)
        role = str(decision.get("role") or "").strip()
        decision["decision_source"] = (
            trusted_source if explicit is not None and trusted_source == "confirmed_not_a_footnote"
            else "explicit_review" if explicit is not None
            else "local_high" if local_role
            else "local_high_footnote_label"
        )
        decision["review_role"] = role if explicit is not None else None
        candidate_key = str(candidate.get("key") or "").strip()
        decision_key = str(decision.get("key") or "").strip()
        candidate_marker = str(candidate.get("marker_label") or "").strip()
        decision_marker = str(decision.get("marker_label") or "").strip()
        changes = []
        if confidence == "high" and baseline_role and role and baseline_role != role:
            changes.append(f"role ({baseline_role} -> {role})")
        if candidate_key and decision_key and candidate_key != decision_key:
            changes.append(f"key ({candidate_key} -> {decision_key})")
        if candidate_marker and decision_marker and candidate_marker != decision_marker:
            changes.append(f"marker_label ({candidate_marker} -> {decision_marker})")
        if changes and decision.get("override") is not True:
            errors.append(f"effective {','.join(changes)} change requires override=true for {address[0]}:{address[1]}:{address[2]}")
            continue
        decision.setdefault("key", candidate.get("key"))
        if candidate.get("marker_label"):
            decision.setdefault("marker_label", candidate.get("marker_label"))
        if candidate.get("requires_marker_context") is True:
            decision.setdefault("requires_marker_context", True)
        decision["local_role"] = baseline_role or None
        decision["effective_role"] = role
        decision["override"] = decision.get("override") is True
        try:
            decision["candidate_evidence_id"] = _candidate_evidence_id(report, address, candidate)
        except (TypeError, ValueError) as exc:
            errors.append(f"candidate evidence identity is invalid at {address[0]}:{address[1]}:{address[2]}: {exc}")
            continue
        effective.append(decision)

    if errors:
        effective = []
    return {
        "version": EFFECTIVE_DECISIONS_VERSION,
        "decisions": effective,
        "errors": errors,
    }


__all__ = ["EFFECTIVE_DECISIONS_VERSION", "compile_effective_decisions", "project_confirmed_not_a_footnote"]

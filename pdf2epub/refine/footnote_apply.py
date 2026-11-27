"""Materialize reviewed page footnotes as logical chapter-end notes.

The preparation step deliberately stops at a small layout decision contract.
This module is the deterministic half that consumes that contract.  It never
decides whether a numbered block is a footnote: only decisions whose role is a
``footnote_*`` role are moved.  Citations, bibliography entries, quotations,
and ordinary body blocks stay in their original unit.
"""

from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from ..command_errors import located_reason
from .footnote_normalization_plan import (
    FootnoteNormalizationPlanningInput,
    build_footnote_normalization_plan,
)
from .footnote_apply_inputs import FootnoteApplyInputError, load_footnote_apply_inputs
from .footnote_status import FOOTNOTE_NORMALIZATION_SCHEMA_VERSION
from .footnote_source import markdown_inventory
from .footnote_result_audit import AUDIT_FILENAME, AUDIT_VERSION, audit_footnote_result
from .footnote_effective_decisions import EFFECTIVE_DECISIONS_VERSION
from .footnote_page_evidence import page_sidecar_index


from .footnote_versions import (
    capture_last_good,
    publish_success,
    recover_interrupted_apply,
    preserve_failed_attempt,
    mark_apply_running,
    mark_apply_complete,
)


def _write_failure(output_dir: Path, errors: list[str], *, status: str = "human_review_required") -> dict[str, Any]:
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "status": status,
        "valid": False,
        "errors": errors,
    }
    output_dir = Path(output_dir)
    receipt_path = output_dir / "footnote_normalization.json"
    try:
        import json
        previous = json.loads(receipt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        previous = {}
    if previous.get("valid") is True and previous.get("status") == "validated":
        attempt_dir = preserve_failed_attempt(output_dir)
        if attempt_dir:
            result["attempt_evidence_dir"] = attempt_dir
        restored = recover_interrupted_apply(output_dir)
        if not restored:
            result["errors"].append("last successful normalized baseline could not be restored; manual recovery is required")
            atomic_write_json(receipt_path, result)
            atomic_write_json(output_dir / "footnote_normalization_attempt.json", result)
            return result
        atomic_write_json(output_dir / "footnote_normalization_attempt.json", result)
        return result
    atomic_write_json(receipt_path, result)
    return result


def _write_success_report(output_dir: Path, result: dict[str, Any]) -> dict[str, Any]:
    """Commit the validated normalization checkpoint after all outputs exist."""
    atomic_write_json(Path(output_dir) / "footnote_normalization.json", result)
    return result


def _write_normalized_units(
    source_dir: Path,
    target_dir: Path,
    transformed: Mapping[str, str],
    expected_source_hashes: Mapping[str, str],
) -> tuple[dict[str, str], dict[str, str]]:
    """Preflight destination inventory, then atomically materialize all units."""
    source_hashes = markdown_inventory(source_dir)
    if source_hashes != dict(expected_source_hashes):
        mismatched = sorted(
            name
            for name in set(source_hashes) | set(expected_source_hashes)
            if source_hashes.get(name) != expected_source_hashes.get(name)
        )
        first = mismatched[0] if mismatched else "<ocr_markdown>"
        raise ValueError(
            located_reason(
                source_dir / first,
                reason="ocr_markdown changed after footnote planning",
                details=(
                    f"file={first} "
                    f"manifest_sha={expected_source_hashes.get(first) or 'missing'} "
                    f"source_sha={source_hashes.get(first) or 'missing'}"
                ),
                recovery="footnote-prepare then footnote-validate",
            )
        )
    target_hashes = markdown_inventory(target_dir) if target_dir.is_dir() else {}
    extra_files = sorted(set(target_hashes) - set(source_hashes))
    if extra_files:
        raise ValueError(
            located_reason(
                target_dir / extra_files[0],
                reason=(
                    "footnote_normalized contains stale Markdown units not present "
                    "in ocr_markdown"
                ),
                details="files=" + ", ".join(extra_files[:20]),
                recovery="remove stale normalized units then footnote-apply --resume",
            )
        )
    if set(transformed) != set(source_hashes):
        missing = sorted(set(source_hashes) - set(transformed))
        extra = sorted(set(transformed) - set(source_hashes))
        first = (missing or extra or ["<ocr_markdown>"])[0]
        raise ValueError(
            located_reason(
                source_dir / first,
                reason="planned Markdown inventory does not match ocr_markdown",
                details=f"missing={missing[:10]} extra={extra[:10]} file={first}",
                recovery="footnote-prepare --resume",
            )
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, text in transformed.items():
        atomic_write_text(target_dir / name, text)
    target_hashes = markdown_inventory(target_dir)
    return dict(expected_source_hashes), target_hashes




def _expected_notes(
    edit_plan: Any,
    records: list[Mapping[str, Any]],
) -> list[dict[str, Any]]:
    """Project the applied notes back to their chapter target files."""
    final_file_by_chapter = {}
    for record in records:
        chapter = str(record.get("chapter_id") or "")
        current = final_file_by_chapter.get(chapter)
        if current is None or (record.get("unit_order", 0), record.get("part_index", 0)) >= (
            current.get("unit_order", 0), current.get("part_index", 0)
        ):
            final_file_by_chapter[chapter] = record
    expected_notes = [
        {
            "chapter_id": chapter,
            "target_file": str(
                edit_plan.note_target_files.get(
                    (chapter, str(note.get("output_key") or note.get("key") or ""))
                )
                or final_file_by_chapter.get(chapter, {}).get("name")
                or ""
            ),
            "output_key": str(note.get("output_key") or note.get("key") or ""),
            "parts": list(note.get("parts") or []),
            "candidate_refs": [
                {
                    "page": int(block["page"]),
                    "source": str(block.get("source") or "primary"),
                    "block": int(block["block"]),
                    "source_file": str(block.get("file") or ""),
                }
                for block in edit_plan.resolved_blocks
                if isinstance(block, Mapping)
                and str(block.get("output_key") or "") == str(note.get("output_key") or note.get("key") or "")
                and str(block.get("chapter_id") or chapter) == chapter
                and str(block.get("page", "")).lstrip("-").isdigit()
                and str(block.get("block", "")).lstrip("-").isdigit()
            ],
        }
        for chapter, notes in edit_plan.notes_by_chapter.items()
        for note in notes
    ]
    return expected_notes

def _merge_reviewed_audit_tasks(
    prepared_audit: Mapping[str, Any],
    validation: Mapping[str, Any],
    target_hashes: Mapping[str, Any],
    candidate_audit_tasks: Any,
) -> tuple[list[dict[str, Any]], dict[str, dict[str, Any]]]:
    """Merge carried and current audit reviews, dropping stale target evidence."""
    prior_applied_reviews = [
        dict(item)
        for item in (prepared_audit.get("applied_result_audit_reviews") or [])
        if isinstance(item, Mapping) and item.get("task_id")
    ]
    current_reviewed_tasks = [
        dict(item)
        for item in (validation.get("result_audit_reviews") or [])
        if isinstance(item, Mapping) and item.get("task_id")
    ]
    merged_by_id: dict[str, dict[str, Any]] = {
        str(item["task_id"]): item for item in prior_applied_reviews
    }
    for item in current_reviewed_tasks:
        merged_by_id[str(item["task_id"])] = item
    reviewed_audit_tasks = list(merged_by_id.values())
    prior_review_tasks = prepared_audit.get("applied_result_audit_review_tasks", [])
    current_review_tasks = [
        *(prepared_audit.get("reviewed_candidate_tasks", [])
          if isinstance(prepared_audit.get("reviewed_candidate_tasks"), list) else []),
        *(candidate_audit_tasks if isinstance(candidate_audit_tasks, list) else []),
    ]
    review_task_by_id = {
        str(item.get("task_id")): dict(item)
        for item in (prior_review_tasks if isinstance(prior_review_tasks, list) else [])
        if isinstance(item, Mapping) and item.get("task_id")
    }
    # Current prepared tasks override carried snapshots so the applied review
    # is bound to the exact evidence revalidated in this preparation.
    review_task_by_id.update({
        str(item.get("task_id")): dict(item)
        for item in current_review_tasks
        if isinstance(item, Mapping) and item.get("task_id")
    })
    current_exception_reviews: list[dict[str, Any]] = []
    for review in reviewed_audit_tasks:
        review_task = review_task_by_id.get(str(review.get("task_id") or ""))
        target_unchanged = (
            review_task is None
            or review_task.get("evidence_origin") != "normalized_target"
            or str(review_task.get("evidence_target_sha256") or "")
            == str(target_hashes.get(str(review_task.get("file") or "")) or "")
        )
        if target_unchanged:
            current_exception_reviews.append(review)
    reviewed_audit_tasks = current_exception_reviews
    return reviewed_audit_tasks, review_task_by_id

def _bind_admitted_candidates(
    result_audit: dict[str, Any],
    *,
    candidate_audit_tasks: Any,
    reviewed_audit_tasks: list[dict[str, Any]],
    review_task_by_id: Mapping[str, dict[str, Any]],
    prepared_audit: Mapping[str, Any],
    edit_plan: Any,
    report: Mapping[str, Any],
    output_dir: Path,
) -> None:
    """Bind reviewed and supplemental candidates to the applied plan."""
    resolved_addresses = {
        (int(item.get("page", -1)), str(item.get("source") or "primary"), int(item.get("block", -1)))
        for item in edit_plan.resolved_blocks if isinstance(item, Mapping)
        and str(item.get("page", "")).lstrip("-").isdigit()
        and str(item.get("block", "")).lstrip("-").isdigit()
    }
    review_by_id = {
        str(item.get("task_id")): item for item in reviewed_audit_tasks
        if isinstance(item, Mapping) and item.get("task_id")
    }
    for task in candidate_audit_tasks if isinstance(candidate_audit_tasks, list) else []:
        if not isinstance(task, Mapping):
            continue
        review = review_by_id.get(str(task.get("task_id")), {})
        if review.get("outcome") == "confirmed_footnote":
            ref = review.get("candidate_ref") if isinstance(review.get("candidate_ref"), Mapping) else {}
            try:
                address = (int(ref["page"]), str(ref.get("source") or "primary"), int(ref["block"]))
            except (KeyError, TypeError, ValueError):
                address = None
            if address is None or address not in resolved_addresses:
                result_audit.setdefault("findings", []).append({
                    "task_id": str(task.get("task_id")),
                    "code": "confirmed_note_not_materialized",
                    "file": str(task.get("file") or ""),
                    "line": task.get("line"),
                    "page": task.get("page"),
                    "evidence_sha256": str(task.get("evidence_sha256") or ""),
                    "detail": "The reviewed note candidate was not found in the applied edit plan; it must remain unresolved.",
                })
    prior_reviewed_tasks = prepared_audit.get("reviewed_candidate_tasks", []) if isinstance(prepared_audit, Mapping) else []
    newly_admitted = [
        {
            **dict(task),
            **({"candidate_ref": dict(review_by_id[str(task.get("task_id"))].get("candidate_ref", {}))}
               if isinstance(review_by_id.get(str(task.get("task_id"))), Mapping)
               and isinstance(review_by_id[str(task.get("task_id"))].get("candidate_ref"), Mapping)
               else {}),
            **({"outcome": str(review_by_id[str(task.get("task_id"))].get("outcome") or "")}
               if isinstance(review_by_id.get(str(task.get("task_id"))), Mapping)
               else {}),
            **({"review_reason": str(review_by_id[str(task.get("task_id"))].get("reason") or "")}
               if isinstance(review_by_id.get(str(task.get("task_id"))), Mapping)
               else {}),
        } for task in candidate_audit_tasks
        if isinstance(task, Mapping)
        and task.get("context_status") == "supplemental_candidate_admitted"
        and isinstance(task.get("source_address"), Mapping)
    ] if isinstance(candidate_audit_tasks, list) else []
    admitted_addrs = {
        (
            int(item["source_address"]["page"]),
            str(item["source_address"].get("source") or "primary"),
            int(item["source_address"]["block"]),
        )
        for item in newly_admitted
        if isinstance(item.get("source_address"), Mapping)
        and str(item["source_address"].get("page", "")).lstrip("-").isdigit()
        and str(item["source_address"].get("block", "")).lstrip("-").isdigit()
    }
    try:
        sidecar_index = page_sidecar_index(output_dir, report)
    except Exception:
        sidecar_index = {}
    task_by_address = {}
    for task in candidate_audit_tasks if isinstance(candidate_audit_tasks, list) else []:
        if not isinstance(task, Mapping) or not isinstance(task.get("source_address"), Mapping):
            continue
        try:
            addr = task["source_address"]
            task_by_address[(int(addr["page"]), str(addr.get("source") or "primary"), int(addr["block"]))] = task
        except (KeyError, TypeError, ValueError):
            continue
    for page_row in report.get("pages", []) or []:
        if not isinstance(page_row, Mapping):
            continue
        for field in ("candidates", "secondary_only_candidates"):
            for cand in page_row.get(field, []) or []:
                if not isinstance(cand, Mapping) or not cand.get("result_audit_admitted"):
                    continue
                try:
                    c_page = int(cand["page"])
                    c_source = str(cand.get("source") or "primary")
                    c_block = int(cand["block"])
                except (KeyError, TypeError, ValueError):
                    continue
                if (c_page, c_source, c_block) in admitted_addrs:
                    continue
                sidecar_entry = sidecar_index.get(c_page, {}).get(c_source)
                if not isinstance(sidecar_entry, Mapping):
                    continue
                admitted_addrs.add((c_page, c_source, c_block))
                try:
                    sidecar_data = json.loads((output_dir / str(sidecar_entry["path"])).read_text(encoding="utf-8"))
                    block_data = sidecar_data["blocks"][c_block]
                except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
                    block_data = None
                task = task_by_address.get((c_page, c_source, c_block), {})
                newly_admitted.append({
                    "preserved_candidate_only": True,
                    "key": str(cand.get("key") or ""),
                    "task_id": str(task.get("task_id") or ""),
                    "file": str(task.get("file") or ""),
                    "block_content_sha256": _footnote_block_content_sha256(block_data),
                    "source_address": {
                        "page": c_page,
                        "source": c_source,
                        "block": c_block,
                        "sidecar": sidecar_entry.get("path"),
                        "sidecar_sha256": sidecar_entry.get("sha256"),
                    },
                })
    for item in newly_admitted:
        if not isinstance(item, dict) or not isinstance(item.get("source_address"), Mapping):
            continue
        address = item["source_address"]
        if item.get("block_content_sha256"):
            continue
        try:
            page, source, block = int(address["page"]), str(address.get("source") or "primary"), int(address["block"])
            entry = sidecar_index.get(page, {}).get(source)
            data = json.loads((output_dir / str(entry["path"])).read_text(encoding="utf-8")) if isinstance(entry, Mapping) else {}
            block_data = data["blocks"][block]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            continue
        item["block_content_sha256"] = _footnote_block_content_sha256(block_data)
        task = task_by_address.get((page, source, block), {})
        item.setdefault("task_id", str(task.get("task_id") or ""))
        item.setdefault("file", str(task.get("file") or ""))
    result_audit["reviewed_candidate_tasks"] = [
        *(prior_reviewed_tasks if isinstance(prior_reviewed_tasks, list) else []),
        *newly_admitted,
    ]
    result_audit["admitted_candidates"] = newly_admitted
    result_audit["applied_result_audit_reviews"] = list(reviewed_audit_tasks) if isinstance(reviewed_audit_tasks, list) else []
    result_audit["applied_result_audit_review_tasks"] = [
        review_task_by_id[str(item["task_id"])]
        for item in reviewed_audit_tasks
        if isinstance(item, Mapping) and str(item.get("task_id") or "") in review_task_by_id
    ]
    # Persist exact page-block identity for every successful visual review,
    # including marker tasks whose candidate_ref points at a different note.
    for item in result_audit["applied_result_audit_review_tasks"]:
        if not isinstance(item, dict) or not isinstance(item.get("source_address"), Mapping):
            continue
        address = item["source_address"]
        try:
            page, source, block = int(address["page"]), str(address.get("source") or "primary"), int(address["block"])
            entry = sidecar_index.get(page, {}).get(source)
            data = json.loads((output_dir / str(entry["path"])).read_text(encoding="utf-8")) if isinstance(entry, Mapping) else {}
            block_data = data["blocks"][block]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError, ValueError):
            continue
        item["block_content_sha256"] = _footnote_block_content_sha256(block_data)
        task = task_by_address.get((page, source, block), {})
        item.setdefault("file", str(task.get("file") or ""))


def _footnote_block_content_sha256(block: Any) -> str:
    if not isinstance(block, Mapping):
        return ""
    payload = {key: str(block.get(key) or "") for key in ("text", "html", "label")}
    return hashlib.sha256(json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()

@dataclass(frozen=True)
class _SuccessReceiptInputs:
    """Inputs for the validated normalization receipt."""

    output_dir: Path
    report: Mapping[str, Any]
    planning: Any
    edit_plan: Any
    validation: Mapping[str, Any]
    target_hashes: Mapping[str, str]
    audit_path: Path
    report_mode: str
    report_footnote_mode: str
    endnote_ambiguities_sha256: str
    endnote_decisions_sha256: str
    continuation_results: Any


def _build_success_result(inputs: _SuccessReceiptInputs) -> dict[str, Any]:
    """Build the validated normalization receipt from the applied plan."""
    output_dir = inputs.output_dir
    report = inputs.report
    planning = inputs.planning
    edit_plan = inputs.edit_plan
    validation = inputs.validation
    target_hashes = inputs.target_hashes
    audit_path = inputs.audit_path
    report_mode = inputs.report_mode
    report_footnote_mode = inputs.report_footnote_mode
    endnote_ambiguities_sha256 = inputs.endnote_ambiguities_sha256
    endnote_decisions_sha256 = inputs.endnote_decisions_sha256
    continuation_results = inputs.continuation_results
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "source_kind": report.get("source_kind", "ocr"),
        "ocr_evidence_mode": report_mode,
        "footnote_mode": report_footnote_mode,
        "configured_footnote_mode": report.get("configured_footnote_mode"),
        "footnote_mode_source": report.get("footnote_mode_source"),
        "warnings": list(report.get("warnings", [])),
        "status": "validated",
        "valid": True,
        "source_dir": "ocr_markdown",
        "target_dir": "footnote_normalized",
        "source_sha256": dict(planning.source_sha256),
        "effective_decisions_version": planning.effective_decisions_version,
        "effective_decisions": list(planning.effective_decisions),
        "target_sha256": target_hashes,
        "candidate_report_sha256": sha256_file(output_dir / "footnote_candidates.json"),
        "decision_validation_sha256": sha256_file(
            output_dir / "footnote_decision_validation.json"
        ),
        "result_audit_sha256": sha256_file(audit_path),
        "result_audit_version": AUDIT_VERSION,
        "endnote_definition_ambiguities_sha256": endnote_ambiguities_sha256,
        "endnote_definition_decisions_sha256": endnote_decisions_sha256,
        "moved_count": len(edit_plan.resolved_blocks),
        "continuation_reviews": list(continuation_results)
        if isinstance(continuation_results, list) else [],
        "preserved_roles": dict(edit_plan.preserved_roles),
        "notes": [
            {
                "chapter_id": note["chapter_id"],
                "key": note["key"],
                "marker_label": note["marker_label"],
                "output_key": note["output_key"],
                "source_page": note["source_page"],
                "part_count": len(note["parts"]),
                "recovered_continuations": list(note.get("recovered_continuations", [])),
            }
            for notes in edit_plan.notes_by_chapter.values()
            for note in notes
        ],
        "decision_evidence": _successful_decision_evidence(output_dir, report, planning),
        "resolved_blocks": list(edit_plan.resolved_blocks),
        "endnotes": dict(edit_plan.endnotes_report),
        "reviewed_inline_continuations": [
            {
                "file": str(item.get("file") or ""),
                "key": str(item.get("key") or ""),
                "text": str(item.get("inline_text") or ""),
            }
            for item in ((validation.get("endnote_definitions") or {}).get("decisions", []) or [])
            if isinstance(item, Mapping)
            and item.get("kind") == "inline"
            and item.get("role") == "continuation"
            and item.get("inline_text")
        ],
        "tree_progress_sha256": sha256_file(
            output_dir / "ocr_markdown" / "tree_progress.json"
        ),
        "toc_tree_sha256": (
            sha256_file(output_dir / "toc_tree.json")
            if (output_dir / "toc_tree.json").is_file()
            else None
        ),
    }
    return result


def _resolve_unique_block_owner(
    output_dir: Path,
    planning: Any,
    owner_records: list[Mapping[str, Any]],
    *,
    page: int,
    source: str,
    block_index: int,
    block: Any,
) -> Mapping[str, Any]:
    """Identify the unique owner part file on a shared boundary page when possible."""
    owner_by_name = {
        str(item.get("name") or ""): item
        for item in owner_records
        if str(item.get("name") or "")
    }
    resolved_blocks = getattr(getattr(planning, "edit_plan", None), "resolved_blocks", None) or []
    matched_from_plan = {
        str(entry.get("file") or "")
        for entry in resolved_blocks
        if isinstance(entry, Mapping)
        and str(entry.get("file") or "") in owner_by_name
        and str(entry.get("page", "")).lstrip("-").isdigit()
        and int(entry["page"]) == page
        and str(entry.get("source") or "primary") == source
        and str(entry.get("block", "")).lstrip("-").isdigit()
        and int(entry["block"]) == block_index
    }
    if len(matched_from_plan) == 1:
        return owner_by_name[next(iter(matched_from_plan))]
    if not isinstance(block, Mapping):
        return {}
    snippets: list[str] = []
    for key in ("text", "markdown", "html"):
        raw = block.get(key)
        if not isinstance(raw, str) or not raw.strip():
            continue
        plain = re.sub(r"<[^>]+>", " ", raw)
        plain = re.sub(r"\s+", " ", plain).strip()
        if plain:
            snippets.append(plain)
            if len(plain) > 80:
                snippets.append(plain[:80].strip())
    if not snippets:
        return {}
    source_texts = getattr(getattr(planning, "source_snapshot", None), "source_texts", None) or {}
    for snippet in snippets:
        if len(snippet) < 12 and len(snippets) > 1:
            continue
        matched_owners: set[str] = set()
        for owner_name in owner_by_name:
            text = source_texts.get(owner_name)
            if text is None:
                try:
                    text = (output_dir / "ocr_markdown" / owner_name).read_text(encoding="utf-8")
                except (OSError, UnicodeError):
                    text = ""
            normalized_owner = re.sub(r"\s+", " ", str(text))
            if snippet in normalized_owner:
                matched_owners.add(owner_name)
        if len(matched_owners) == 1:
            return owner_by_name[next(iter(matched_owners))]
    return {}


def _successful_decision_evidence(output_dir: Path, report: Mapping[str, Any], planning: Any) -> list[dict[str, Any]]:
    """Persist content identities for decisions from the successful apply."""
    from .footnote_source import unit_records
    try:
        records = unit_records(output_dir)
        sidecars = page_sidecar_index(output_dir, report)
    except (OSError, ValueError, TypeError):
        return []
    output = []
    for decision in getattr(planning, "effective_decisions", []) or []:
        if not isinstance(decision, Mapping):
            continue
        try:
            page, source, block_index = int(decision["page"]), str(decision.get("source") or "primary"), int(decision["block"])
        except (KeyError, TypeError, ValueError):
            continue
        owner_records = [
            item
            for item in records
            if int(item.get("start_page", -1)) <= page <= int(item.get("end_page", -1))
        ]
        owners = {str(item.get("name") or "") for item in owner_records if str(item.get("name") or "")}
        record = next((item for item in owner_records if str(item.get("name") or "") in owners), {}) if len(owners) == 1 else {}
        entry = sidecars.get(page, {}).get(source)
        if not isinstance(entry, Mapping):
            continue
        try:
            data = json.loads((output_dir / str(entry["path"])).read_text(encoding="utf-8"))
            block = data["blocks"][block_index]
        except (OSError, UnicodeError, json.JSONDecodeError, KeyError, IndexError, TypeError):
            continue
        if not record and len(owners) > 1:
            record = _resolve_unique_block_owner(
                output_dir,
                planning,
                owner_records,
                page=page,
                source=source,
                block_index=block_index,
                block=block,
            )
        content_hash = _footnote_block_content_sha256(block)
        if not content_hash:
            continue
        output.append({
            "file": str(record.get("name") or ""), "page": page, "source": source,
            "block": block_index, "role": str(decision.get("effective_role") or decision.get("role") or ""),
            "key": str(decision.get("key") or ""), "block_content_sha256": content_hash,
        })
    return output


def apply_footnote_normalization(
    output_dir: Path,
    *,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Create ``footnote_normalized/`` from validated decisions.

    The operation is preflighted before writing the derived stage.  A missing
    body marker, ambiguous source match, cross-chapter continuation, or a
    footnote decision inside bibliography/index content blocks the stage and
    leaves the original OCR Markdown untouched.
    """

    output_dir = Path(output_dir)
    recover_interrupted_apply(output_dir)
    previous_version = capture_last_good(output_dir)
    try:
        import json
        prior_receipt = json.loads((output_dir / "footnote_normalization.json").read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError, TypeError):
        prior_receipt = {}
    if (prior_receipt.get("valid") is True and prior_receipt.get("status") == "validated"
            and previous_version is None):
        failure = {
            "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
            "status": "retry_required", "valid": False,
            "errors": ["could not snapshot the successful normalized baseline; apply was not started"],
        }
        atomic_write_json(output_dir / "footnote_normalization_attempt.json", failure)
        return failure
    try:
        inputs = load_footnote_apply_inputs(output_dir, config)
    except FootnoteApplyInputError as exc:
        return _write_failure(output_dir, [str(exc)])

    report = inputs.report
    validation = inputs.validation
    candidate_report_hash_at_start = sha256_file(output_dir / "footnote_candidates.json")
    decision_validation_hash_at_start = sha256_file(output_dir / "footnote_decision_validation.json")
    tree_progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    toc_tree_path = output_dir / "toc_tree.json"
    tree_progress_hash_at_start = sha256_file(tree_progress_path) if tree_progress_path.is_file() else None
    toc_tree_hash_at_start = sha256_file(toc_tree_path) if toc_tree_path.is_file() else None
    report_mode = inputs.report_mode
    report_footnote_mode = inputs.footnote_mode
    endnote_definition_decisions = inputs.endnote_definition_decisions
    endnote_ambiguities_sha256 = inputs.endnote_ambiguities_sha256
    endnote_decisions_sha256 = inputs.endnote_decisions_sha256
    source = inputs.source_snapshot
    source_dir = source.source_dir
    target_dir = inputs.target_dir
    records = list(source.records)
    source_texts = source.source_texts
    continuation_results = validation.get("continuation_reviews", [])

    if (
        validation.get("effective_decisions_version") != EFFECTIVE_DECISIONS_VERSION
        or not isinstance(validation.get("effective_decisions"), list)
    ):
        return _write_failure(output_dir, ["footnote validation lacks the current effective decision set; rerun footnote-validate"])

    planning = build_footnote_normalization_plan(FootnoteNormalizationPlanningInput(
        output_dir=output_dir,
        report=report,
        decisions=validation,
        source=inputs.source_snapshot,
        footnote_mode=report_footnote_mode,
        toc_tree=inputs.toc_tree,
        progress=inputs.progress,
        scope_error=inputs.scope_error,
        endnote_definition_decisions=endnote_definition_decisions,
        expected_effective_decisions=validation["effective_decisions"],
        expected_source_sha256=validation.get("source_sha256"),
    ))
    if not planning.valid or planning.edit_plan is None:
        return _write_failure(output_dir, list(planning.errors) or ["footnote normalization plan is invalid"])
    edit_plan = planning.edit_plan
    validation_for_resolution = {"decisions": list(planning.validation_decisions)}

    mark_apply_running(output_dir, edit_plan.transformed_texts, previous_version)
    try:
        source_hashes, target_hashes = _write_normalized_units(
            source_dir, target_dir, edit_plan.transformed_texts, edit_plan.source_hashes
        )
    except (OSError, UnicodeError, ValueError) as exc:
        failing_file = getattr(exc, "filename", None) or source_dir
        return _write_failure(output_dir, [located_reason(
            failing_file,
            reason=f"could not write normalized source: {exc}",
            details=f"file={Path(str(failing_file)).name}",
            recovery="footnote-apply --resume",
        )])

    # Audit only after every unit has been freshly transformed from the
    # original OCR source. The audit report is written before the success
    # receipt; unresolved findings leave no valid normalization checkpoint.
    materialized_targets = {}
    for name in source_texts:
        target_path = target_dir / name
        if target_path.is_file():
            try:
                materialized_targets[name] = target_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
    expected_notes = _expected_notes(edit_plan, records)
    prepared_audit = report.get("result_audit") if isinstance(report.get("result_audit"), Mapping) else {}
    candidate_audit_tasks = prepared_audit.get("findings", []) if isinstance(prepared_audit, Mapping) else []
    reviewed_audit_tasks, review_task_by_id = _merge_reviewed_audit_tasks(
        prepared_audit, validation, target_hashes, candidate_audit_tasks
    )
    page_texts: dict[int, str] = {}
    try:
        from ..ocr_correction import select_refinement_pages

        page_dir, _page_source_kind = select_refinement_pages(output_dir, config=config)
    except (OSError, ValueError, TypeError) as exc:
        failing_page = getattr(exc, "filename", None) or (output_dir / "pages")
        return _write_failure(output_dir, [located_reason(
            failing_page,
            reason=f"could not load current page evidence for result audit: {exc}",
            details=f"page/file={Path(str(failing_page)).name}",
            recovery="ocr-pages --resume",
        )])
    if page_dir.is_dir():
        for page_path in page_dir.glob("page_*.md"):
            try:
                page_number = int(page_path.stem.removeprefix("page_"))
                page_texts[page_number] = page_path.read_text(encoding="utf-8")
            except (ValueError, OSError, UnicodeError):
                continue
    result_audit = audit_footnote_result(
        source_texts,
        records=records,
        target_texts=materialized_targets,
        mode=report_footnote_mode,
        decisions=validation_for_resolution.get("decisions", []),
        expected_notes=expected_notes,
        current_exceptions=reviewed_audit_tasks,
        resolved_blocks=edit_plan.resolved_blocks,
        toc_tree=inputs.toc_tree,
        page_texts=page_texts,
    )
    # Bind to byte-level inventories (including original newline convention)
    # that status checks on disk, rather than normalized in-memory strings.
    result_audit["source_sha256"] = source_hashes
    result_audit["target_sha256"] = target_hashes
    _bind_admitted_candidates(
        result_audit,
        candidate_audit_tasks=candidate_audit_tasks,
        reviewed_audit_tasks=reviewed_audit_tasks,
        review_task_by_id=review_task_by_id,
        prepared_audit=prepared_audit,
        edit_plan=edit_plan,
        report=report,
        output_dir=output_dir,
    )
    if result_audit.get("findings"):
        result_audit["valid"] = False
        result_audit["status"] = "review_required"
    result_audit["candidate_report_sha256"] = sha256_file(output_dir / "footnote_candidates.json")
    result_audit["decision_validation_sha256"] = sha256_file(output_dir / "footnote_decision_validation.json")
    audit_path = output_dir / AUDIT_FILENAME
    atomic_write_json(audit_path, result_audit)
    if result_audit.get("valid") is not True:
        findings = result_audit.get("findings", [])
        repeated_review = any(
            isinstance(item, Mapping) and int(item.get("review_attempt") or 1) >= 2
            for item in findings
        )
        technical_retry = any(
            isinstance(item, Mapping) and str(item.get("code") or "").startswith("applied_note_")
            for item in findings
        )
        failure_status = "human_review_required" if repeated_review else "retry_required" if technical_retry else "review_required"
        return _write_failure(
            output_dir,
            [
                f"{item.get('task_id')}: {item.get('detail')} (rerun footnote-prepare for scoped visual review)"
                for item in result_audit.get("findings", [])
            ] or ["footnote result audit requires review"],
            status=failure_status,
        )

    result = _build_success_result(_SuccessReceiptInputs(
        output_dir=output_dir,
        report=report,
        planning=planning,
        edit_plan=edit_plan,
        validation=validation,
        target_hashes=target_hashes,
        audit_path=audit_path,
        report_mode=report_mode,
        report_footnote_mode=report_footnote_mode,
        endnote_ambiguities_sha256=endnote_ambiguities_sha256,
        endnote_decisions_sha256=endnote_decisions_sha256,
        continuation_results=continuation_results,
    ))
    # Revalidate the complete evidence snapshot immediately before committing
    # the final success receipt. Hashes captured only at this point would let a
    # mid-run edit certify a plan made against older evidence.
    try:
        current_inputs = load_footnote_apply_inputs(output_dir, config)
    except FootnoteApplyInputError as exc:
        return _write_failure(output_dir, [f"evidence changed during footnote apply: {exc}"])
    if (
        sha256_file(output_dir / "footnote_candidates.json") != candidate_report_hash_at_start
        or sha256_file(output_dir / "footnote_decision_validation.json") != decision_validation_hash_at_start
        or markdown_inventory(source_dir) != source_hashes
        or markdown_inventory(target_dir) != target_hashes
        or (sha256_file(tree_progress_path) if tree_progress_path.is_file() else None) != tree_progress_hash_at_start
        or (sha256_file(toc_tree_path) if toc_tree_path.is_file() else None) != toc_tree_hash_at_start
        or dict(current_inputs.source_snapshot.source_hashes) != source_hashes
    ):
        return _write_failure(output_dir, [
            "footnote source, target, decision, candidate, TOC, or page evidence changed during apply; rerun footnote-prepare and footnote-validate"
        ])
    saved = _write_success_report(output_dir, result)
    publish_success(output_dir, previous_version)
    mark_apply_complete(output_dir, (output_dir / "footnote_normalization.json").is_file() and json.loads(
        (output_dir / "footnote_normalization.json").read_text(encoding="utf-8")
    ).get("version_id"))
    attempt_path = output_dir / "footnote_normalization_attempt.json"
    if attempt_path.is_file():
        atomic_write_json(attempt_path, {"schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
                                         "status": "resolved", "valid": True,
                                         "resolved_by_version": saved.get("version_id")})
    return saved


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "apply_footnote_normalization",
]

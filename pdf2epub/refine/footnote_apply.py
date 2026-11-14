"""Materialize reviewed page footnotes as logical chapter-end notes.

The preparation step deliberately stops at a small layout decision contract.
This module is the deterministic half that consumes that contract.  It never
decides whether a numbered block is a footnote: only decisions whose role is a
``footnote_*`` role are moved.  Citations, bibliography entries, quotations,
and ordinary body blocks stay in their original unit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from .footnote_edit_plan import (
    FootnotePlanTransformError,
    create_footnote_edit_plan,
    transform_footnote_edit_plan,
)
from .footnote_apply_resolution import (
    _candidate_decisions,
    _locate_block,
    bind_note_markers,
    resolve_page_decisions,
)
from .footnote_apply_inputs import FootnoteApplyInputError, load_footnote_apply_inputs
from .footnote_status import FOOTNOTE_NORMALIZATION_SCHEMA_VERSION, markdown_inventory
from .footnote_result_audit import AUDIT_FILENAME, AUDIT_VERSION, audit_footnote_result


def _write_failure(output_dir: Path, errors: list[str], *, status: str = "human_review_required") -> dict[str, Any]:
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "status": status,
        "valid": False,
        "errors": errors,
    }
    atomic_write_json(Path(output_dir) / "footnote_normalization.json", result)
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
        raise ValueError(
            "ocr_markdown changed after footnote planning; rerun footnote-prepare "
            "and footnote-validate"
        )
    target_hashes = markdown_inventory(target_dir) if target_dir.is_dir() else {}
    extra_files = sorted(set(target_hashes) - set(source_hashes))
    if extra_files:
        raise ValueError(
            "footnote_normalized contains stale Markdown units not present "
            f"in ocr_markdown: {', '.join(extra_files[:20])}"
        )
    if set(transformed) != set(source_hashes):
        missing = sorted(set(source_hashes) - set(transformed))
        extra = sorted(set(transformed) - set(source_hashes))
        raise ValueError(
            "planned Markdown inventory does not match ocr_markdown "
            f"(missing={missing[:10]}, extra={extra[:10]})"
        )
    target_dir.mkdir(parents=True, exist_ok=True)
    for name, text in transformed.items():
        atomic_write_text(target_dir / name, text)
    target_hashes = markdown_inventory(target_dir)
    return dict(expected_source_hashes), target_hashes




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
    source_dir = inputs.source_dir
    target_dir = inputs.target_dir
    records = list(inputs.records)
    source_texts = dict(inputs.source_texts)
    continuation_results = validation.get("continuation_reviews", [])

    body_exception_decisions = []
    report_for_resolution = dict(report)
    adjusted_pages = []
    for page_report in report.get("pages", []) or []:
        if not isinstance(page_report, Mapping):
            adjusted_pages.append(page_report)
            continue
        adjusted = dict(page_report)
        candidates = []
        for candidate in page_report.get("candidates", []) or []:
            candidate_copy = dict(candidate)
            matching_review = next((
                item for item in continuation_results if isinstance(item, Mapping)
                and item.get("outcome") == "confirmed_not_a_footnote"
                and int((item.get("source_note") or {}).get("page", -1)) == int(candidate_copy.get("page", -2))
                and int((item.get("source_note") or {}).get("block", -1)) == int(candidate_copy.get("block", -2))
                and str((item.get("source_note") or {}).get("source") or "primary") == str(candidate_copy.get("source") or "primary")
            ), None) if isinstance(continuation_results, list) else None
            if matching_review:
                candidate_copy["confidence"] = "review"
                candidate_page = int(candidate_copy["page"])
                candidate_scope = next((
                    str(record.get("chapter_id") or "") for record in records
                    if int(record.get("start_page", 0)) <= candidate_page <= int(record.get("end_page", 0))
                ), "")
                body_exception_decisions.append({
                    "page": candidate_page,
                    "source": str(candidate_copy.get("source") or "primary"),
                    "block": int(candidate_copy["block"]),
                    "role": "body",
                    "key": str(candidate_copy.get("key") or ""),
                    "chapter_id": candidate_scope,
                })
            candidates.append(candidate_copy)
        adjusted["candidates"] = candidates
        adjusted_pages.append(adjusted)
    if body_exception_decisions:
        report_for_resolution["pages"] = adjusted_pages
        validation_for_resolution = dict(validation)
        validation_for_resolution["decisions"] = [
            *(validation.get("decisions", []) or []),
            *body_exception_decisions,
        ]
    else:
        validation_for_resolution = validation

    decision_plan = resolve_page_decisions(
        output_dir, report_for_resolution, validation_for_resolution,
        source_texts, records, report_footnote_mode
    )
    for review in continuation_results if isinstance(continuation_results, list) else []:
        if not isinstance(review, Mapping) or review.get("outcome") != "recovered_continuation":
            continue
        source_task = next((
            task for task in report.get("continuation_reviews", []) or []
            if isinstance(task, Mapping) and task.get("task_id") == review.get("task_id")
        ), None)
        source_note = source_task.get("source_note", {}) if isinstance(source_task, Mapping) else {}
        matches = [
            note
            for notes in decision_plan.notes_by_chapter.values()
            for note in notes
            if str(note.get("key")) == str(review.get("key"))
            and int(note.get("source_page", -1)) == int(source_note.get("page", -2))
        ]
        if len(matches) != 1:
            return _write_failure(output_dir, [
                f"{review.get('task_id')}: recovered continuation does not identify one exact source note"
            ])
        note = matches[0]
        text = str(review.get("text") or "").strip()
        boundary_kind = str(source_task.get("boundary_kind") or "") if isinstance(source_task, Mapping) else ""
        if not boundary_kind:
            ending_hint = str(source_note.get("text_ending") or "")
            boundary_kind = "hyphen" if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", ending_hint) else "open_boundary"
        if not note.get("parts"):
            return _write_failure(output_dir, [
                f"{review.get('task_id')}: source note has no reviewed continuation boundary; rerun footnote-prepare"
            ])
        if boundary_kind == "hyphen" and not re.match(r"^[a-zà-öø-ÿ]", text):
            return _write_failure(output_dir, [
                f"{review.get('task_id')}: recovered continuation must begin with the visible lowercase word suffix"
            ])
        ending = str(note["parts"][-1]).rstrip()
        if re.search(r"[A-Za-zÀ-ÖØ-öø-ÿ]{2,}[-‐‑]$", ending):
            note["parts"][-1] = ending[:-1] + text
        elif ending.count("(") > ending.count(")") or ending.count("[") > ending.count("]"):
            matching_close = ")" if ending.count("(") > ending.count(")") else "]"
            joiner = "" if text.startswith(matching_close) else " "
            note["parts"][-1] = ending + joiner + text
        elif ending.endswith((",", ":", ";", "—", "…")):
            note["parts"][-1] = ending + " " + text
        else:
            return _write_failure(output_dir, [
                f"{review.get('task_id')}: source note no longer has its reviewed open boundary; rerun footnote-prepare"
            ])
        note.setdefault("recovered_continuations", []).append(str(review.get("task_id")))
    bind_note_markers(decision_plan, source_texts, records)
    edit_plan = create_footnote_edit_plan(
        source_texts, decision_plan, source_hashes=inputs.source_hashes
    )
    if not edit_plan.valid:
        return _write_failure(output_dir, list(edit_plan.errors))
    try:
        edit_plan = transform_footnote_edit_plan(
            edit_plan,
            records,
            footnote_mode=report_footnote_mode,
            toc_tree=inputs.toc_tree,
            progress=inputs.progress,
            scope_error=inputs.scope_error,
            definition_decisions=endnote_definition_decisions,
        )
    except (FootnotePlanTransformError, OSError, UnicodeError, ValueError) as exc:
        return _write_failure(output_dir, [str(exc)])

    try:
        source_hashes, target_hashes = _write_normalized_units(
            source_dir, target_dir, edit_plan.transformed_texts, edit_plan.source_hashes
        )
    except (OSError, UnicodeError, ValueError) as exc:
        return _write_failure(output_dir, [f"could not write normalized source: {exc}"])

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
            "target_file": str(final_file_by_chapter.get(chapter, {}).get("name") or ""),
            "output_key": str(note.get("output_key") or note.get("key") or ""),
            "parts": list(note.get("parts") or []),
            "candidate_refs": [
                {
                    "page": int(block["page"]),
                    "source": str(block.get("source") or "primary"),
                    "block": int(block["block"]),
                }
                for block in edit_plan.resolved_blocks
                if isinstance(block, Mapping)
                and str(block.get("key") or "") == str(note.get("key") or "")
                and str(block.get("chapter_id") or chapter) == chapter
                and str(block.get("page", "")).lstrip("-").isdigit()
                and str(block.get("block", "")).lstrip("-").isdigit()
            ],
        }
        for chapter, notes in edit_plan.notes_by_chapter.items()
        for note in notes
    ]
    result_audit = audit_footnote_result(
        source_texts,
        records=records,
        target_texts=materialized_targets,
        mode=report_footnote_mode,
        decisions=validation_for_resolution.get("decisions", []),
        expected_notes=expected_notes,
        current_exceptions=validation.get("result_audit_reviews", []),
    )
    # Bind to byte-level inventories (including original newline convention)
    # that status checks on disk, rather than normalized in-memory strings.
    result_audit["source_sha256"] = source_hashes
    result_audit["target_sha256"] = target_hashes
    prepared_audit = report.get("result_audit") if isinstance(report.get("result_audit"), Mapping) else {}
    candidate_audit_tasks = prepared_audit.get("findings", []) if isinstance(prepared_audit, Mapping) else []
    reviewed_audit_tasks = validation.get("result_audit_reviews", [])
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
    result_audit["reviewed_candidate_tasks"] = [
        *(prior_reviewed_tasks if isinstance(prior_reviewed_tasks, list) else []),
        *newly_admitted,
    ]
    result_audit["admitted_candidates"] = newly_admitted
    result_audit["applied_result_audit_reviews"] = list(reviewed_audit_tasks) if isinstance(reviewed_audit_tasks, list) else []
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
        "source_sha256": source_hashes,
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
        or dict(current_inputs.source_hashes) != source_hashes
    ):
        return _write_failure(output_dir, [
            "footnote source, target, decision, candidate, TOC, or page evidence changed during apply; rerun footnote-prepare and footnote-validate"
        ])
    return _write_success_report(output_dir, result)


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "apply_footnote_normalization",
]

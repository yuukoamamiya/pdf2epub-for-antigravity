"""Materialize reviewed page footnotes as logical chapter-end notes.

The preparation step deliberately stops at a small layout decision contract.
This module is the deterministic half that consumes that contract.  It never
decides whether a numbered block is a footnote: only decisions whose role is a
``footnote_*`` role are moved.  Citations, bibliography entries, quotations,
and ordinary body blocks stay in their original unit.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, atomic_write_text, sha256_file
from .footnote_normalization_plan import (
    FootnoteNormalizationPlanningInput,
    build_footnote_normalization_plan,
)
from .footnote_apply_inputs import FootnoteApplyInputError, load_footnote_apply_inputs
from .footnote_status import FOOTNOTE_NORMALIZATION_SCHEMA_VERSION
from .footnote_source import markdown_inventory
from .footnote_result_audit import AUDIT_FILENAME, AUDIT_VERSION, audit_footnote_result
from .footnote_effective_decisions import EFFECTIVE_DECISIONS_VERSION


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
    prepared_audit = report.get("result_audit") if isinstance(report.get("result_audit"), Mapping) else {}
    candidate_audit_tasks = prepared_audit.get("findings", []) if isinstance(prepared_audit, Mapping) else []
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
    page_texts: dict[int, str] = {}
    try:
        from ..ocr_correction import select_refinement_pages

        page_dir, _page_source_kind = select_refinement_pages(output_dir, config=config)
    except (OSError, ValueError, TypeError) as exc:
        return _write_failure(output_dir, [f"could not load current page evidence for result audit: {exc}"])
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
    result_audit["applied_result_audit_review_tasks"] = [
        review_task_by_id[str(item["task_id"])]
        for item in reviewed_audit_tasks
        if isinstance(item, Mapping) and str(item.get("task_id") or "") in review_task_by_id
    ]
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
        or dict(current_inputs.source_snapshot.source_hashes) != source_hashes
    ):
        return _write_failure(output_dir, [
            "footnote source, target, decision, candidate, TOC, or page evidence changed during apply; rerun footnote-prepare and footnote-validate"
        ])
    return _write_success_report(output_dir, result)


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "apply_footnote_normalization",
]

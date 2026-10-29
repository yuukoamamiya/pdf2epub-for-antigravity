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


def _write_failure(output_dir: Path, errors: list[str]) -> dict[str, Any]:
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "status": "human_review_required",
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
    report_mode = inputs.report_mode
    report_footnote_mode = inputs.footnote_mode
    endnote_definition_decisions = inputs.endnote_definition_decisions
    endnote_ambiguities_sha256 = inputs.endnote_ambiguities_sha256
    endnote_decisions_sha256 = inputs.endnote_decisions_sha256
    source_dir = inputs.source_dir
    target_dir = inputs.target_dir
    records = list(inputs.records)
    source_texts = dict(inputs.source_texts)

    decision_plan = resolve_page_decisions(
        output_dir, report, validation, source_texts, records, report_footnote_mode
    )
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
    result = {
        "schema_version": FOOTNOTE_NORMALIZATION_SCHEMA_VERSION,
        "source_kind": report.get("source_kind", "ocr"),
        "ocr_evidence_mode": report_mode,
        "footnote_mode": report_footnote_mode,
        "configured_footnote_mode": report.get("configured_footnote_mode"),
        "footnote_mode_source": report.get("footnote_mode_source"),
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
        "endnote_definition_ambiguities_sha256": endnote_ambiguities_sha256,
        "endnote_definition_decisions_sha256": endnote_decisions_sha256,
        "moved_count": len(edit_plan.resolved_blocks),
        "preserved_roles": dict(edit_plan.preserved_roles),
        "notes": [
            {
                "chapter_id": note["chapter_id"],
                "key": note["key"],
                "marker_label": note["marker_label"],
                "output_key": note["output_key"],
                "source_page": note["source_page"],
                "part_count": len(note["parts"]),
            }
            for notes in edit_plan.notes_by_chapter.values()
            for note in notes
        ],
        "resolved_blocks": list(edit_plan.resolved_blocks),
        "endnotes": dict(edit_plan.endnotes_report),
        "tree_progress_sha256": sha256_file(
            output_dir / "ocr_markdown" / "tree_progress.json"
        ),
        "toc_tree_sha256": (
            sha256_file(output_dir / "toc_tree.json")
            if (output_dir / "toc_tree.json").is_file()
            else None
        ),
    }
    return _write_success_report(output_dir, result)


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "apply_footnote_normalization",
]

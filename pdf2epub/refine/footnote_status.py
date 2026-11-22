"""Freshness and unit-scope checks for normalized footnote output."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import schema_major_matches, sha256_file
from .footnote_matching import _load_json
from .footnote_source import markdown_inventory
from .footnote_options import (
    EFFECTIVE_FOOTNOTE_MODES,
    resolve_effective_footnote_mode,
)
from .footnote_continuation_review import validate_continuation_review_decisions
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .pdf_evidence import pdf_evidence_mode, require_current_consensus
from .footnote_result_review import (
    validate_applied_result_audit_reviews,
    validate_result_audit_reviews,
)
from .footnote_result_audit import AUDIT_VERSION
from .footnote_validation_evidence import validate_candidate_evidence
from .footnote_effective_decisions import (
    EFFECTIVE_DECISIONS_VERSION,
    compile_effective_decisions,
    project_confirmed_not_a_footnote,
)
from .footnote_source import load_footnote_source_snapshot


# Version 10 binds normalized output to the compiled effective decision set.
FOOTNOTE_NORMALIZATION_SCHEMA_VERSION = 10
def footnote_normalization_status(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Explain whether derived chapter-end source still matches its inputs."""
    output_dir = Path(output_dir)
    failures: list[dict[str, str]] = []

    def fail(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    try:
        report = _load_json(
            output_dir / "footnote_normalization.json",
            "footnote_normalization.json",
        )
        validation = _load_json(
            output_dir / "footnote_decision_validation.json",
            "footnote_decision_validation.json",
        )
        candidate_report = _load_json(
            output_dir / "footnote_candidates.json",
            "footnote_candidates.json",
        )
        audit_report = _load_json(
            output_dir / "footnote_result_audit.json",
            "footnote_result_audit.json",
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        fail("checkpoint_missing_or_invalid", str(exc))
        return {
            "current": False,
            "status": "stale",
            "failures": failures,
            "detail": "footnote normalization checkpoint is missing or invalid; "
            "run footnote-prepare, footnote-validate, and footnote-apply",
        }

    if report.get("schema_version") != FOOTNOTE_NORMALIZATION_SCHEMA_VERSION:
        fail(
            "normalization_schema_mismatch",
            "footnote_normalization.json uses an older normalization contract; "
            "rerun footnote-prepare, footnote-validate, and footnote-apply",
        )
    if report.get("valid") is not True or report.get("status") != "validated":
        fail(
            "normalization_not_validated",
            "footnote_normalization.json is not a validated checkpoint",
        )
    if (output_dir / "footnote_normalization_attempt.json").is_file():
        try:
            attempt = _load_json(output_dir / "footnote_normalization_attempt.json", "footnote_normalization_attempt.json")
            if attempt.get("valid") is not True or attempt.get("status") != "resolved":
                fail("normalization_attempt_failed", "a later footnote apply attempt failed; rerun footnote-apply successfully")
        except (OSError, UnicodeError, ValueError, TypeError) as exc:
            fail("normalization_attempt_invalid", f"footnote apply attempt receipt is unreadable: {exc}")
    if validation.get("valid") is not True:
        fail(
            "decision_validation_failed",
            "footnote_decision_validation.json is missing or invalid",
        )
    expected_audit_status = (
        "not_applicable" if str(report.get("footnote_mode") or "") == "disabled"
        else "validated"
    )
    if audit_report.get("schema_version") != AUDIT_VERSION or audit_report.get("valid") is not True or audit_report.get("status") != expected_audit_status:
        fail("result_audit_failed", "footnote_result_audit.json is missing, stale, or has unresolved findings")
    if report.get("result_audit_sha256") != sha256_file(output_dir / "footnote_result_audit.json"):
        fail("result_audit_stale", "footnote result audit changed after normalization")
    if audit_report.get("source_sha256") != markdown_inventory(output_dir / "ocr_markdown"):
        fail("result_audit_source_stale", "footnote result audit is bound to different source Markdown")
    if audit_report.get("target_sha256") != markdown_inventory(output_dir / "footnote_normalized"):
        fail("result_audit_target_stale", "footnote result audit is bound to different normalized Markdown")
    if audit_report.get("candidate_report_sha256") != sha256_file(output_dir / "footnote_candidates.json"):
        fail("result_audit_candidate_stale", "footnote result audit is bound to a different candidate report")
    if audit_report.get("decision_validation_sha256") != sha256_file(output_dir / "footnote_decision_validation.json"):
        fail("result_audit_validation_stale", "footnote result audit is bound to a different decision validation")
    if not schema_major_matches(
        candidate_report.get("schema_version"), FOOTNOTE_PREPARE_SCHEMA_VERSION
    ):
        fail(
            "candidate_report_schema_mismatch",
            "footnote_candidates.json uses an older or missing prepare contract; rerun footnote-prepare",
        )
    if str(validation.get("status") or "") == "not_applicable":
        candidate_evidence = {"errors": []}
    else:
        candidate_evidence = validate_candidate_evidence(output_dir, candidate_report, config)
    if candidate_evidence.get("errors"):
        fail(
            "candidate_page_evidence_stale",
            "footnote sidecar, consensus, or image evidence changed after preparation: "
            + "; ".join(str(item) for item in candidate_evidence.get("errors", [])[:5]),
        )
    prepared_audit = candidate_report.get("result_audit") if isinstance(candidate_report.get("result_audit"), Mapping) else {}
    current_audit_tasks = [
        *(prepared_audit.get("findings", []) if isinstance(prepared_audit.get("findings"), list) else []),
        *(prepared_audit.get("reviewed_candidate_tasks", []) if isinstance(prepared_audit.get("reviewed_candidate_tasks"), list) else []),
    ]
    audit_reviews, audit_errors = validate_result_audit_reviews(
        output_dir,
        current_audit_tasks,
        validation.get("result_audit_reviews", []),
        ordinary_decisions=validation.get("decisions", []),
    )
    if audit_errors or audit_reviews != validation.get("result_audit_reviews", []):
        fail(
            "result_audit_review_stale",
            "supplementary footnote visual decisions are stale or incomplete: "
            + "; ".join(audit_errors[:5] or ["review records no longer match validation"]),
        )
    history_reviews, _, history_errors = validate_applied_result_audit_reviews(
        output_dir,
        prepared_audit.get("applied_result_audit_reviews"),
        prepared_audit.get("applied_result_audit_review_tasks"),
        current_tasks=current_audit_tasks,
    )
    if history_errors or history_reviews != prepared_audit.get("applied_result_audit_reviews", []):
        fail(
            "result_audit_history_stale",
            "applied result audit history no longer matches its original/current visual evidence: "
            + "; ".join(history_errors[:5] or ["historical reviews no longer match their evidence"]),
        )
    if not schema_major_matches(
        validation.get("schema_version"), FOOTNOTE_PREPARE_SCHEMA_VERSION
    ):
        fail(
            "decision_validation_schema_mismatch",
            "footnote_decision_validation.json uses an older or missing prepare contract; rerun footnote-validate",
        )
    continuation_tasks = candidate_report.get("continuation_reviews")
    if not isinstance(continuation_tasks, list):
        fail(
            "continuation_review_queue_missing",
            "footnote candidate report is missing its continuation_reviews queue; rerun footnote-prepare",
        )
        continuation_tasks = []
    if not isinstance(validation.get("continuation_reviews"), list):
        fail(
            "continuation_review_resolution_missing",
            "footnote decision validation is missing continuation review outcomes; rerun footnote-validate",
        )
    validation_candidate_hash = validation.get("candidate_report_sha256")
    if not isinstance(validation_candidate_hash, str) or len(validation_candidate_hash) != 64:
        fail(
            "decision_validation_candidate_hash_missing",
            "decision validation is not bound to the candidate report; rerun footnote-validate",
        )
    _, continuation_errors = validate_continuation_review_decisions(
        output_dir,
        continuation_tasks,
        validation.get("continuation_reviews", []),
    )
    if continuation_errors:
        fail(
            "continuation_review_stale",
            "image-backed footnote continuation review is missing or stale: "
            + "; ".join(continuation_errors[:5]),
        )
    report_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    validation_mode = str(validation.get("ocr_evidence_mode") or report_mode)
    if report_mode != validation_mode:
        fail(
            "evidence_mode_mismatch",
            "footnote normalization and decision validation use different OCR evidence modes",
        )
    report_footnote_mode = str(
        report.get("footnote_mode") or "footnotes"
    ).strip().lower()
    validation_footnote_mode = str(
        validation.get("footnote_mode") or report_footnote_mode
    ).strip().lower()
    if report_footnote_mode not in EFFECTIVE_FOOTNOTE_MODES:
        fail(
            "unsupported_footnote_mode",
            f"footnote normalization uses unsupported mode {report_footnote_mode}",
        )
    if report_footnote_mode != validation_footnote_mode:
        fail(
            "footnote_mode_mismatch",
            "footnote normalization and decision validation use different footnotes.mode values",
        )
    if config is not None:
        try:
            configured_mode = pdf_evidence_mode(output_dir, config)
        except ValueError as exc:
            fail("invalid_evidence_metadata", str(exc))
            return {
                "current": False,
                "status": "stale",
                "failures": failures,
                "detail": "footnote evidence metadata is invalid; rerun OCR and footnote preparation",
            }
        if report_mode != configured_mode:
            fail(
                "configured_evidence_mode_mismatch",
                f"checkpoint uses {report_mode}, current configuration requires {configured_mode}",
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                fail("ocr_consensus_stale", str(exc))
    try:
        resolution = resolve_effective_footnote_mode(output_dir, config)
    except ValueError as exc:
        fail("invalid_footnote_mode", str(exc))
    else:
        if report_footnote_mode != resolution["effective"]:
            fail(
                "configured_footnote_mode_mismatch",
                f"checkpoint uses footnotes.mode={report_footnote_mode}, "
                "but the current configuration/TOC requires "
                f"{resolution['effective']} (source: {resolution['source']})",
            )
        recorded_configured = report.get("configured_footnote_mode")
        if (
            isinstance(recorded_configured, str)
            and recorded_configured.strip().lower() != resolution["configured"]
        ):
            fail(
                "configured_footnote_mode_mismatch",
                "checkpoint was prepared with configured footnotes.mode="
                f"{recorded_configured}, but configuration now resolves "
                f"{resolution['configured']}",
            )
    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    source_names = set(markdown_inventory(source_dir))
    target_names = set(markdown_inventory(target_dir))
    extra_files = sorted(target_names - source_names)
    if extra_files:
        fail(
            "normalized_output_extra_files",
            "footnote_normalized contains stale Markdown units not present in "
            f"ocr_markdown: {', '.join(extra_files[:20])}",
        )
    if report.get("source_sha256") != markdown_inventory(source_dir):
        fail(
            "source_markdown_changed",
            "ocr_markdown changed after footnote normalization",
        )
    if report.get("target_sha256") != markdown_inventory(target_dir):
        fail(
            "normalized_output_changed",
            "footnote_normalized changed after footnote normalization",
        )
    if report.get("candidate_report_sha256") != sha256_file(
        output_dir / "footnote_candidates.json"
    ):
        fail(
            "candidate_report_stale",
            "footnote_candidates.json changed after footnote-apply",
        )
    if report.get("result_audit_version") != audit_report.get("schema_version"):
        fail("result_audit_version_mismatch", "normalization receipt uses a different result audit version")
    if validation_candidate_hash != sha256_file(output_dir / "footnote_candidates.json"):
        fail(
            "decision_validation_candidate_hash_stale",
            "decision validation is bound to an older candidate report; rerun footnote-validate",
        )
    if (
        validation.get("effective_decisions_version") != EFFECTIVE_DECISIONS_VERSION
        or report.get("effective_decisions_version") != EFFECTIVE_DECISIONS_VERSION
    ):
        fail(
            "effective_decisions_version_mismatch",
            "normalization or decision validation lacks the current effective decision contract; rerun footnote-validate and footnote-apply",
        )
    if validation.get("effective_decisions") != report.get("effective_decisions"):
        fail(
            "effective_decisions_stale",
            "normalized output and validation receipt contain different effective footnote decisions",
        )
    try:
        current_source_hashes = markdown_inventory(output_dir / "ocr_markdown")
        continuation_reviews = validation.get("continuation_reviews", [])
        confirmed_body_reviews = [
            review for review in continuation_reviews if isinstance(review, Mapping)
            and review.get("outcome") == "confirmed_not_a_footnote"
        ] if isinstance(continuation_reviews, list) else []
        records = []
        if confirmed_body_reviews:
            current_source = load_footnote_source_snapshot(output_dir)
            records = list(current_source.records)
        projected_report, projected_decisions = project_confirmed_not_a_footnote(
            candidate_report,
            {
                "decisions": validation.get("decisions", []),
                "continuation_reviews": continuation_reviews,
            },
            records,
        )
        compiled = compile_effective_decisions(
            projected_report, projected_decisions.get("decisions", [])
        )
        if compiled["errors"] or compiled["decisions"] != validation.get("effective_decisions"):
            fail(
                "effective_decisions_stale",
                "effective footnote decisions no longer compile from current candidates and reviews; rerun footnote-validate",
            )
        if dict(validation.get("source_sha256") or {}) != dict(current_source_hashes):
            fail(
                "decision_validation_source_stale",
                "decision validation is bound to different source Markdown; rerun footnote-validate",
            )
    except (OSError, UnicodeError, ValueError, TypeError, KeyError) as exc:
        fail("effective_decisions_unavailable", f"could not verify effective footnote decisions: {exc}")
    if report.get("decision_validation_sha256") != sha256_file(
        output_dir / "footnote_decision_validation.json"
    ):
        fail(
            "decision_validation_stale",
            "footnote_decision_validation.json changed after footnote-apply",
        )
    for field, filename, code in (
        (
            "endnote_definition_ambiguities_sha256",
            "endnote_definition_ambiguities.json",
            "endnote_definition_ambiguities_stale",
        ),
        (
            "endnote_definition_decisions_sha256",
            "endnote_definition_decisions.json",
            "endnote_definition_decisions_stale",
        ),
    ):
        expected = report.get(field)
        if not expected:
            continue
        path = output_dir / filename
        if not path.is_file() or expected != sha256_file(path):
            fail(
                code,
                f"{filename} is missing or changed after endnote definition review",
            )
    tree_progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    expected_tree_progress_hash = report.get("tree_progress_sha256")
    if expected_tree_progress_hash and (
        not tree_progress_path.is_file()
        or expected_tree_progress_hash != sha256_file(tree_progress_path)
    ):
        fail(
            "tree_progress_stale",
            "tree_progress.json changed after footnote normalization",
        )
    endnotes = report.get("endnotes")
    if (
        isinstance(endnotes, Mapping)
        and str(endnotes.get("scope_mode") or "").strip().lower()
        == "per_chapter"
    ):
        toc_tree_path = output_dir / "toc_tree.json"
        expected_toc_hash = report.get("toc_tree_sha256")
        if (
            not expected_toc_hash
            or not toc_tree_path.is_file()
            or expected_toc_hash != sha256_file(toc_tree_path)
        ):
            fail(
                "toc_tree_stale",
                "toc_tree.json is missing or changed after scoped endnote "
                "normalization",
            )

    current = not failures
    if current:
        detail = "current validated footnote normalization matches all inputs"
    else:
        detail = failures[0]["detail"]
        if any(
            item["code"]
            in {
                "candidate_report_stale",
                "toc_tree_stale",
                "configured_footnote_mode_mismatch",
                "endnote_definition_ambiguities_stale",
                "endnote_definition_decisions_stale",
            }
            for item in failures
        ):
            detail += "; rerun footnote-prepare, footnote-validate, and footnote-apply"
        elif any(item["code"] == "decision_validation_stale" for item in failures):
            detail += "; rerun footnote-validate and footnote-apply"
    return {
        "current": current,
        "status": "validated" if current else "stale",
        "failures": failures,
        "detail": detail,
    }


def footnote_normalization_is_current(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether derived chapter-end source still matches its inputs."""
    return bool(footnote_normalization_status(output_dir, config=config)["current"])


def footnote_normalization_baseline_is_attested(output_dir: Path) -> bool:
    """Check whether an older successful normalization remains a safe source.

    Candidate and decision receipts are intentionally excluded: they are refreshed
    during reflow and their freshness belongs to final readiness, not source choice.
    The applied baseline remains usable only while its raw/normalized inventories
    and the structural inputs recorded by the successful apply receipt are intact.
    """
    output_dir = Path(output_dir)
    try:
        report = _load_json(
            output_dir / "footnote_normalization.json",
            "footnote_normalization.json",
        )
    except (OSError, UnicodeError, ValueError, TypeError):
        return False
    if (
        report.get("schema_version") != FOOTNOTE_NORMALIZATION_SCHEMA_VERSION
        or report.get("valid") is not True
        or report.get("status") != "validated"
        or report.get("source_dir") != "ocr_markdown"
        or report.get("target_dir") != "footnote_normalized"
    ):
        return False
    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    source = markdown_inventory(source_dir)
    target = markdown_inventory(target_dir)
    if not source or set(target) != set(source):
        return False
    if report.get("source_sha256") != source or report.get("target_sha256") != target:
        return False
    tree_hash = report.get("tree_progress_sha256")
    tree_path = source_dir / "tree_progress.json"
    if (not isinstance(tree_hash, str) or len(tree_hash) != 64
            or not tree_path.is_file() or sha256_file(tree_path) != tree_hash):
        return False
    toc_hash = report.get("toc_tree_sha256")
    toc_path = output_dir / "toc_tree.json"
    if toc_hash is not None and (
        not isinstance(toc_hash, str) or len(toc_hash) != 64
        or not toc_path.is_file() or sha256_file(toc_path) != toc_hash
    ):
        return False
    if toc_hash is None and toc_path.is_file():
        return False
    return True


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "footnote_normalization_is_current",
    "footnote_normalization_status",
    "footnote_normalization_baseline_is_attested",
]

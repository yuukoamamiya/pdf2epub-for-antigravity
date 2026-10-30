"""Deterministic readiness gates for PDF Markdown workflows."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Dict

from pdf2epub.commands.sources import (
    _polished_stage_is_current,
    _resolve_pdf_polish_source,
    _resolve_pdf_markdown_source,
)
from pdf2epub.entity_context import entity_context_is_current
from pdf2epub.ocr_correction import ocr_correction_is_current, select_refinement_pages
from pdf2epub.ocr_progress import assess_progress
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.ocr_source import is_legacy_native_source
from pdf2epub.utils.paths import book_output_dir
from pdf2epub.utils.configuration import load_config
from pdf2epub.utils.fingerprints import pages_fingerprint
from pdf2epub.validation_receipts import validation_receipt_is_current
from pdf2epub.workflow_contracts import atomic_write_json
from pdf2epub.refine.subagent_workflow import validate_toc_tree_data


def run_readiness_check(
    config_path: str,
    stage: str = "translate",
    skip_entities: bool = False,
) -> dict:
    """Run deterministic gates before translation or packaging."""
    config = load_config(config_path)
    policy = PipelinePolicy.from_config(config)
    conversion_pipeline = policy.is_conversion
    book_title = config.get("title")
    output_dir = book_output_dir(book_title) if book_title else Path("output")
    checks: Dict[str, dict] = {}
    errors: list[str] = []

    def record(
        name: str,
        ready: bool,
        detail: str,
        *,
        code: str | None = None,
        blocked_by: list[str] | None = None,
        emit_error: bool = True,
    ) -> None:
        checks[name] = {"ready": bool(ready), "detail": detail}
        if code:
            checks[name]["code"] = code
        if blocked_by:
            checks[name]["blocked_by"] = list(blocked_by)
        if not ready and emit_error:
            errors.append(f"{name}: {detail}")

    if not book_title:
        record("config", False, "title is missing")
        report = {
            "schema_version": 1,
            "stage": stage,
            "ready": False,
            "checks": checks,
            "errors": errors,
        }
        output_dir.mkdir(parents=True, exist_ok=True)
        atomic_write_json(output_dir / "readiness.json", report)
        return report

    raw_pages_dir = output_dir / "pages"
    probe_path = output_dir / "pdf_text_probe.json"
    expected_total_pages = None
    expected_source_sha256 = None
    if probe_path.is_file():
        try:
            probe = json.loads(probe_path.read_text(encoding="utf-8"))
            expected_total_pages = probe.get("page_count")
            expected_source_sha256 = probe.get("source_sha256")
        except (OSError, json.JSONDecodeError, AttributeError):
            pass
    ocr_report = assess_progress(
        raw_pages_dir,
        expected_total_pages=expected_total_pages,
        expected_source_sha256=expected_source_sha256,
        require_sidecars=True,
    )
    available = ocr_report["available_pages"]
    page_ready = ocr_report["ready"]
    record(
        "ocr_pages",
        page_ready,
        f"{len(available)} OCR pages are complete"
        if page_ready
        else "; ".join(ocr_report["errors"][:5]),
    )

    from pdf2epub.ocr_anomalies import scan_ocr_page_evidence

    ocr_evidence = scan_ocr_page_evidence(output_dir)
    anomaly_report = ocr_evidence.report
    try:
        legacy_native_text = is_legacy_native_source(output_dir)
        source_error = None
    except ValueError as exc:
        legacy_native_text = False
        source_error = str(exc)
        record("ocr_page_source", False, source_error)
    if legacy_native_text:
        anomaly_report = {**anomaly_report, "review_required_files": [], "scan_errors": []}
    anomaly_files = anomaly_report["review_required_files"]
    anomaly_correction_ready = not anomaly_report.get("scan_errors") and (
        not anomaly_files
        or ocr_correction_is_current(
            output_dir, config, ocr_evidence=ocr_evidence
        )
    )
    record(
        "ocr_anomalies",
        anomaly_correction_ready,
        "no OCR commentary anomalies found"
        if not anomaly_files and not anomaly_report.get("scan_errors")
        else (
            "anomaly pages have current visual review"
            if anomaly_correction_ready
            else (
                "raw OCR pages could not all be scanned"
                if anomaly_report.get("scan_errors")
                else "OCR commentary requires visual review: " + ", ".join(anomaly_files[:10])
            )
        ),
    )

    native_text = legacy_native_text
    try:
        correction_required = policy.requires_visual_ocr_review(
            output_dir, ocr_evidence
        )
    except ValueError as exc:
        correction_required = True
        if source_error is None:
            record("ocr_page_source", False, str(exc))
    correction_ready = native_text or not correction_required or ocr_correction_is_current(
        output_dir, config, ocr_evidence=ocr_evidence
    )
    record(
        "ocr_correction",
        correction_ready,
        "not applicable for a high-confidence native-text PDF"
        if native_text
        else (
            "not required because neither dual-OCR consensus nor page anomalies require visual review"
            if not correction_required
            else (
                "validated visual OCR correction matches the current page set"
                if correction_ready
                else "visual OCR correction is missing, stale, or unvalidated; run ocr-correct and ocr-correct-validate"
            )
        ),
    )
    try:
        pages_dir, _page_source_kind = select_refinement_pages(
            output_dir, config=config, ocr_evidence=ocr_evidence
        )
    except ValueError as exc:
        pages_dir = raw_pages_dir
        if source_error is None:
            record("ocr_page_source", False, str(exc))

    toc_path = output_dir / "toc_tree.json"
    toc_errors: list[str] = []
    if toc_path.is_file() and available:
        try:
            toc_data = json.loads(toc_path.read_text(encoding="utf-8"))
            toc_errors = validate_toc_tree_data(toc_data, max(available), available)
        except (OSError, json.JSONDecodeError) as exc:
            toc_errors = [f"invalid toc_tree.json: {exc}"]
    else:
        toc_errors = ["toc_tree.json or OCR pages are missing"]
    record("toc_tree", not toc_errors, "; ".join(toc_errors[:5]) or "TOC structure is valid")

    tree_progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    fingerprint_ready = False
    if tree_progress_path.is_file() and toc_path.is_file() and pages_dir.is_dir():
        try:
            tree_progress = json.loads(tree_progress_path.read_text(encoding="utf-8"))
            fingerprint = tree_progress.get("fingerprint", {})
            fingerprint_ready = (
                fingerprint.get("toc_sha256")
                == hashlib.sha256(toc_path.read_bytes()).hexdigest()
                and fingerprint.get("pages_sha256") == pages_fingerprint(pages_dir)
            )
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            fingerprint_ready = False
    record(
        "refine_local",
        fingerprint_ready,
        "OCR work units match the current TOC and page snapshot"
        if fingerprint_ready
        else "ocr_markdown/tree_progress.json is missing or stale",
    )

    footnote_status = {"current": True, "detail": "not required at this stage"}
    if stage in {"translate", "package"} and policy.requires_polish:
        from pdf2epub.refine.footnote_status import footnote_normalization_status

        footnote_status = footnote_normalization_status(output_dir, config=config)
        record(
            "footnote_normalization",
            bool(footnote_status.get("current")),
            str(footnote_status.get("detail") or "footnote checkpoint is stale"),
            code=(
                None
                if footnote_status.get("current")
                else str(
                    (footnote_status.get("failures") or [{"code": "stale"}])[0].get(
                        "code", "stale"
                    )
                )
            ),
        )

    try:
        source_dir, source_stage = _resolve_pdf_markdown_source(
            output_dir, config, ocr_evidence
        )
    except ValueError as exc:
        source_dir, source_stage = output_dir / "ocr_markdown", "blocked"
        record("source_stage", False, str(exc))
    source_files = list(source_dir.glob("*.md")) if source_dir.is_dir() else []
    source_ready = bool(source_files)
    if source_stage == "polished":
        try:
            polish_input_dir, _ = _resolve_pdf_polish_source(
                output_dir, config, ocr_evidence
            )
            source_ready = source_ready and _polished_stage_is_current(
                output_dir, source_dir, polish_input_dir
            )
        except ValueError as exc:
            source_ready = False
            record("source_stage", False, str(exc))
    if "source_stage" not in checks:
        record(
            "source_stage",
            source_ready,
            f"{source_stage}: {len(source_files)} Markdown units"
            if source_ready
            else f"{source_stage} source is missing or not validated",
        )
    if stage in {"translate", "package"} and policy.requires_polish:
        record(
            "polish_required",
            source_stage == "polished" and source_ready,
            (
                "PDF packaging requires a current validated polished source"
                if conversion_pipeline
                else "PDF translation requires a current validated polished source for both OCR-derived and native-text PDFs"
            )
            if source_stage != "polished" or not source_ready
            else f"current validated {source_stage} source is selected",
            blocked_by=(
                ["footnote_normalization"]
                if not footnote_status.get("current")
                else None
            ),
            emit_error=bool(footnote_status.get("current")),
        )

    translation = config.get("translation", {}) or {}
    if not policy.requires_translation:
        record("translation_entities", True, "not applicable for pipeline: epub_conversion")
    elif policy.requires_entities and not skip_entities:
        entity_ready = (
            False
            if not footnote_status.get("current")
            else entity_context_is_current(
                output_dir,
                source_dir,
                policy.source_language,
                policy.target_language,
            )
        )
        entity_validation_path = output_dir / "translation_entities_validation.json"
        if entity_validation_path.is_file():
            try:
                entity_report = json.loads(entity_validation_path.read_text(encoding="utf-8"))
                entity_ready = entity_ready and entity_report.get("valid") is True
            except (OSError, json.JSONDecodeError, AttributeError):
                entity_ready = False
        else:
            entity_ready = False
        record(
            "translation_entities",
            entity_ready,
            (
                "entity glossary matches the selected source stage"
                if entity_ready
                else (
                    "not evaluated because footnote normalization is stale"
                    if not footnote_status.get("current")
                    else "entity glossary is missing, unvalidated, or stale"
                )
            ),
            blocked_by=(
                ["footnote_normalization"]
                if not footnote_status.get("current")
                else None
            ),
            emit_error=bool(footnote_status.get("current")),
        )
    else:
        record("translation_entities", True, "explicitly skipped for this task")

    glossary_ready = True
    configured_glossaries = translation.get("glossaries", []) or []
    configured_reference_glossaries = translation.get("reference_glossaries", []) or []
    if not policy.requires_translation:
        glossary_detail = "not applicable for pipeline: epub_conversion"
    elif configured_glossaries or configured_reference_glossaries:
        try:
            from pdf2epub.glossary import load_selected_glossaries

            bundle = load_selected_glossaries(
                config,
                output_dir,
                policy.source_language or "English",
                policy.target_language or "Chinese",
                Path(config_path),
            )
            glossary_ready = all(path.is_file() for path in bundle.context_files.values())
            glossary_detail = (
                f"{bundle.entries} authoritative entries and {len(configured_reference_glossaries)} reference glossary(s)"
            )
        except Exception as exc:
            glossary_ready = False
            glossary_detail = str(exc)
    else:
        glossary_detail = "no external glossary configured"
    record("external_glossaries", glossary_ready, glossary_detail)

    toc_ready = True
    toc_detail = "not required at this stage"
    if stage in {"translate", "package"} and policy.requires_translated_toc:
        from pdf2epub.toc_translation_workflow import validate_toc_translation_subagent

        toc_report = validate_toc_translation_subagent(output_dir)
        toc_ready = toc_report["valid"]
        toc_detail = (
            "translated TOC structure is valid"
            if toc_ready
            else "; ".join(toc_report["errors"][:5])
        )
        record("translated_toc", toc_ready, toc_detail)
    elif stage in {"translate", "package"}:
        record("translated_toc", True, "not required for pipeline: epub_conversion")

    if stage == "package" and policy.requires_translation:
        translated_dir = output_dir / "translated" / "validated"
        translated_ready = translated_dir.is_dir() and any(translated_dir.glob("*.md"))
        record(
            "translated_output",
            translated_ready,
            "validated translated Markdown is available"
            if translated_ready
            else "translated/validated is missing",
        )
        validation_path = output_dir / "translate_validation.json"
        validation_ready = False
        validation_detail = "translate_validation.json is missing or incomplete"
        if validation_path.is_file():
            try:
                json.loads(validation_path.read_text(encoding="utf-8"))
                validation_ready = validation_receipt_is_current(
                    validation_path,
                    source_dir,
                    translated_dir,
                    task="translate",
                    output_dir=output_dir,
                    config_path=Path(config_path),
                    require_build_inputs=True,
                )
                validation_detail = (
                    "full translation validation matches the current source and staged target snapshots"
                    if validation_ready
                    else "full translation validation is stale, failed, or uses an old gate"
                )
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                validation_detail = "translate_validation.json is invalid"
        record("translation_validation", validation_ready, validation_detail)
    elif stage == "package":
        record("translated_output", True, "not required for pipeline: epub_conversion")
        record("translation_validation", True, "not required for pipeline: epub_conversion")

    if stage == "package" and policy.requires_polish:
        polish_report_path = output_dir / "polish_validation.json"
        polish_dir = output_dir / "polished_markdown" / "validated"
        polish_input_dir, _ = _resolve_pdf_polish_source(
            output_dir, config, ocr_evidence
        )
        polish_ready = validation_receipt_is_current(
            polish_report_path,
            polish_input_dir,
            polish_dir,
            task="polish",
            output_dir=output_dir,
            config_path=Path(config_path),
            require_build_inputs=True,
        )
        record(
            "polish_package_receipt",
            polish_ready,
            "polish receipt covers current TOC, refinement, pages, and config"
            if polish_ready
            else "polish receipt is missing, stale, or does not attest package inputs",
        )

    report = {
        "schema_version": 1,
        "stage": stage,
        "book_title": book_title,
        "pipeline": policy.kind,
        "source_stage": source_stage,
        "ready": not errors,
        "checks": checks,
        "errors": errors,
    }
    output_dir.mkdir(parents=True, exist_ok=True)
    atomic_write_json(output_dir / "readiness.json", report)
    return report


__all__ = ["run_readiness_check"]

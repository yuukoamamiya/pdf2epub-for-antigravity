"""Read-only workflow diagnostics for ``status`` and ``doctor``.

The normal workflow commands intentionally fail at their first unsafe gate.
These commands provide the operator with the same information without writing
readiness reports or attempting to repair checkpoints.
"""

from __future__ import annotations

import hashlib
import importlib.util
import json
from pathlib import Path
from typing import Any, Mapping

import yaml

from pdf2epub.ocr.backends import supported_backends
from pdf2epub.workflow_contracts import sha256_file
from pdf2epub.ocr_consensus import (
    ocr_evidence_mode,
    secondary_ocr_enabled,
    secondary_backend_name,
)
from pdf2epub.ocr_progress import assess_progress
from pdf2epub.pipeline_policy import PDF_PIPELINE_KINDS, PipelinePolicy
from pdf2epub.workflow_stages import (
    HTML_WORKFLOW_STAGES,
    NOVEL_WORKFLOW_STAGES,
    TEX_WORKFLOW_STAGES,
    workflow_stages_for,
)
from pdf2epub.refine.pdf_evidence import is_native_text_source
from pdf2epub.utils.paths import book_output_dir
from pdf2epub.utils.input_paths import resolve_book_input_path


STATUS_SCHEMA_VERSION = 4
DOCTOR_SCHEMA_VERSION = 1


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _read_config(path: Path) -> tuple[dict[str, Any], list[str]]:
    try:
        value = yaml.safe_load(path.read_text(encoding="utf-8")) or {}
    except FileNotFoundError:
        return {}, [f"configuration file does not exist: {path}"]
    except (OSError, UnicodeError, yaml.YAMLError) as exc:
        return {}, [f"configuration could not be read: {exc}"]
    if not isinstance(value, dict):
        return {}, ["configuration root must be a YAML mapping"]
    return value, []


def _status_record(
    name: str,
    command: str,
    status: str,
    detail: str,
    *,
    pending_files: list[str] | None = None,
    blocked_by: list[str] | None = None,
) -> dict[str, Any]:
    record: dict[str, Any] = {
        "name": name,
        "command": command,
        "status": status,
        "detail": detail,
    }
    if pending_files:
        record["pending_files"] = sorted(set(pending_files))
    if blocked_by:
        record["blocked_by"] = blocked_by
    return record


def _file_list(directory: Path, suffix: str = ".md") -> list[Path]:
    return sorted(path for path in directory.glob(f"*{suffix}") if path.is_file())


def _source_sha256(path: Path) -> str | None:
    try:
        return sha256_file(path)
    except OSError:
        return None


def _resolve_input(config: Mapping[str, Any], config_path: Path, output_dir: Path) -> tuple[str, Path]:
    if config.get("input_epub"):
        return "epub", resolve_book_input_path(
            config_value=config.get("input_epub"),
            config_path=config_path,
            output_dir=output_dir,
            extensions=(".epub", ".azw3", ".mobi"),
            output_names=("input.epub", "original.epub"),
        )
    return "pdf", resolve_book_input_path(
        config_value=config.get("input_pdf") or config.get("input"),
        config_path=config_path,
        output_dir=output_dir,
        extensions=(".pdf",),
        output_names=("input_original.pdf", "input.pdf"),
    )


def _pdf_ocr_status(output_dir: Path, input_path: Path) -> tuple[str, str]:
    if not input_path.is_file():
        return "blocked", "input PDF is missing"
    probe = _read_json(output_dir / "pdf_text_probe.json")
    progress = assess_progress(
        output_dir / "pages",
        expected_total_pages=probe.get("page_count") or None,
        expected_source_sha256=probe.get("source_sha256") or _source_sha256(input_path),
        require_sidecars=False,
    )
    if progress["ready"]:
        mode = str(progress["progress"].get("mode") or "ocr")
        return "passed", f"{mode} pages are complete ({len(progress['available_pages'])} pages)"
    if not (output_dir / "pages" / "ocr_progress.json").exists():
        return "pending", "page OCR has not been prepared"
    return "pending", "; ".join(progress["errors"][:3]) or "page OCR is incomplete"


def _pdf_tree_status(output_dir: Path, config: Mapping[str, Any]) -> tuple[str, str]:
    toc_path = output_dir / "toc_tree.json"
    progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    if not toc_path.is_file():
        return "pending", "toc_tree.json is missing; run refine-prepare and use a Subagent"
    if not progress_path.is_file():
        return "pending", "refine-local has not generated tree_progress.json"
    progress = _read_json(progress_path)
    fingerprint = progress.get("fingerprint")
    if not isinstance(fingerprint, Mapping):
        return "pending", "tree_progress.json is invalid or lacks its input fingerprint"
    try:
        from pdf2epub.ocr_correction import select_refinement_pages
        from pdf2epub.refine.pdf_evidence import (
            evidence_receipt_is_current,
            load_evidence_receipt,
        )
        from pdf2epub.utils.fingerprints import pages_fingerprint

        current_toc = sha256_file(toc_path)
        pages_dir, _ = select_refinement_pages(output_dir, config=config)
        receipt = load_evidence_receipt(output_dir)
        if receipt is not None:
            current_evidence, evidence_errors = evidence_receipt_is_current(
                receipt,
                output_dir,
                config,
                pages_dir=pages_dir,
                sidecar_dir=output_dir / "pages",
            )
            if not current_evidence:
                return "pending", (
                    "evidence receipt is stale: "
                    + "; ".join(evidence_errors)
                    + "; rerun refine-local"
                )
        else:
            current_pages = pages_fingerprint(pages_dir)
            if fingerprint.get("pages_sha256") != current_pages:
                return "pending", "tree_progress.json is stale after a page change"
        if fingerprint.get("toc_sha256") != current_toc:
            return "pending", "tree_progress.json is stale after a TOC change"
        binding_path = output_dir / "illustration_bindings.json"
        expected_binding = fingerprint.get("illustration_bindings_sha256")
        actual_binding = sha256_file(binding_path) if binding_path.is_file() else None
        if expected_binding != actual_binding:
            return "pending", "tree_progress.json is stale after an illustration binding change"
    except (OSError, TypeError, ValueError):
        return "pending", "could not verify the current refinement fingerprint"
    return "passed", "TOC and merged Markdown checkpoint are current"


def _current_polish(output_dir: Path, config: Mapping[str, Any]) -> bool:
    try:
        from pdf2epub.commands.sources import _polished_stage_is_current, _resolve_pdf_polish_source

        polish_dir = output_dir / "polished_markdown" / "validated"
        source_dir, _ = _resolve_pdf_polish_source(output_dir, dict(config))
        return _polished_stage_is_current(output_dir, polish_dir, source_dir, config=dict(config))
    except (OSError, TypeError, ValueError):
        return False


def _entity_stage_status(output_dir: Path, config: Mapping[str, Any]) -> tuple[bool, str]:
    try:
        from pdf2epub.entity_context import entity_context_status, validate_entity_source_manifest
        from pdf2epub.commands.sources import _resolve_pdf_markdown_source

        source_dir, _ = _resolve_pdf_markdown_source(output_dir, dict(config))
        report = _read_json(output_dir / "translation_entities_validation.json")
        current, reason = entity_context_status(
            output_dir,
            source_dir,
            (config.get("translation") or {}).get("source_language", "English"),
            (config.get("translation") or {}).get("target_language", "Chinese"),
        )
        if report.get("valid") is True and current:
            return True, "validated book terminology is current"
        manifest = _read_json(output_dir / "entity_subagent_manifest.json")
        if manifest:
            if validate_entity_source_manifest(manifest, output_dir, source_dir):
                return False, "entity source changed after preparation; rerun extract-entities to refresh the scoped change inventory"
            mode = manifest.get("mode")
            pending = manifest.get("pending_files", [])
            removed = manifest.get("removed_files", [])
            if mode == "refresh":
                return False, "source formatting refresh only; run extract-entities and extract-entities-validate (no Subagent review needed)"
            if mode == "legacy_reuse" and not pending and not removed:
                return False, "legacy entity baseline is reusable; run extract-entities-validate to retain it"
            if mode in {"incremental_review", "bootstrap"}:
                units = [*pending, *(f"removed: {name}" for name in removed)]
                label = ", ".join(str(name) for name in units) or "bootstrap source set"
                return False, f"{mode.replace('_', ' ')} required for: {label}; run extract-entities and review only assigned units"
        return False, reason
    except (OSError, TypeError, ValueError):
        return False, "entity baseline is missing or could not be verified; prepare an entity bootstrap task"


def _current_translation(output_dir: Path, config_path: Path, config: Mapping[str, Any]) -> bool:
    try:
        from pdf2epub.commands.sources import _resolve_pdf_markdown_source
        from pdf2epub.validation_receipts import validation_receipt_is_current

        source_dir, _ = _resolve_pdf_markdown_source(output_dir, dict(config))
        target_dir = output_dir / "translated" / "validated"
        return validation_receipt_is_current(
            output_dir / "translate_validation.json",
            source_dir,
            target_dir,
            task="translate",
            output_dir=output_dir,
            config_path=config_path,
            require_build_inputs=False,
        )
    except (OSError, TypeError, ValueError):
        return False


def _pdf_stages(
    output_dir: Path,
    config_path: Path,
    config: Mapping[str, Any],
    input_path: Path,
) -> list[dict[str, Any]]:
    policy = PipelinePolicy.from_config(config)
    from pdf2epub.ocr_review_selection import manual_review_selection_state

    manual_selection = manual_review_selection_state(output_dir)
    stages: list[dict[str, Any]] = []
    ocr_status, ocr_detail = _pdf_ocr_status(output_dir, input_path)
    stages.append(_status_record("ocr-pages", "ocr-pages --resume", ocr_status, ocr_detail))

    try:
        native_text = is_native_text_source(output_dir)
    except ValueError as exc:
        stages.append(_status_record("ocr-source", "ocr-pages --resume", "blocked", str(exc)))
        native_text = False
    try:
        review_required = policy.requires_visual_ocr_review(output_dir)
    except ValueError as exc:
        review_required = True
        if not any(stage["name"] == "ocr-source" for stage in stages):
            stages.append(_status_record("ocr-source", "ocr-pages --resume", "blocked", str(exc)))
    if native_text and manual_selection["present"]:
        stages.append(
            _status_record(
                "ocr-correct",
                "ocr-correct",
                "blocked",
                "manual visual OCR selection cannot apply to a legacy native-text run",
                pending_files=manual_selection["files"],
            )
        )
    elif native_text or not review_required:
        stages.append(
            _status_record(
                "ocr-correct",
                "ocr-correct",
                "skipped",
                "not required for native-text PDF or single OCR without detected anomalies",
            )
        )
    else:
        try:
            from pdf2epub.ocr_correction import ocr_correction_is_current

            current = ocr_correction_is_current(output_dir, config)
        except (OSError, TypeError, ValueError):
            current = False
        if manual_selection["present"]:
            pending_files = manual_selection["files"]
            if manual_selection["current"]:
                correction_command = "ocr-correct --resume"
                pending_detail = (
                    "manual visual review is selected for "
                    + ", ".join(pending_files)
                    + "; complete the Subagent hand-off and run full ocr-correct-validate"
                )
            else:
                flags = " ".join(f"--file {name}" for name in pending_files)
                correction_command = "ocr-correct " + flags
                pending_detail = manual_selection["detail"]
            stages.append(
                _status_record(
                    "ocr-correct",
                    correction_command,
                    "passed" if current else "pending",
                    "manual OCR page review checkpoint is current"
                    if current
                    else pending_detail,
                    pending_files=pending_files,
                )
            )
        else:
            stages.append(
                _status_record(
                    "ocr-correct",
                    "ocr-correct",
                    "passed" if current else "pending",
                    "visual OCR correction checkpoint is current"
                    if current
                    else "run ocr-correct for flagged pages, have the Subagent write corrections, then run ocr-correct-validate",
                )
            )

    toc_status, toc_detail = _pdf_tree_status(output_dir, config)
    stages.append(_status_record("refine-prepare", "refine-prepare", toc_status, toc_detail))

    illustration_report = _read_json(output_dir / "illustration_candidate_report.json")
    bindings = _read_json(output_dir / "illustration_bindings.json")
    illustration_current = bool(illustration_report) and bindings.get("status") == "validated"
    stages.append(
        _status_record(
            "illustration",
            "illustration-prepare",
            "passed" if illustration_current else "pending",
            "illustration candidates were applied"
            if illustration_current
            else "run illustration-prepare, illustration-validate, and illustration-apply",
        )
    )

    tree_ready = bool(
        (output_dir / "ocr_markdown" / "tree_progress.json").is_file()
        and _file_list(output_dir / "ocr_markdown")
    )
    stages.append(
        _status_record(
            "refine-local",
            "refine-local --resume",
            "passed" if tree_ready else "pending",
            "OCR pages are merged into current Markdown units"
            if tree_ready
            else "run refine-local after the TOC and illustration checkpoints are complete",
        )
    )

    try:
        from pdf2epub.refine.footnote_status import footnote_normalization_is_current

        footnotes_current = footnote_normalization_is_current(output_dir, config=config)
    except (OSError, TypeError, ValueError):
        footnotes_current = False
    stages.append(
        _status_record(
            "footnote",
            "footnote-prepare",
            "passed" if footnotes_current else "pending",
            "footnote normalization checkpoint is current"
            if footnotes_current
            else "run footnote-prepare, footnote-validate, and footnote-apply",
        )
    )

    polished_current = _current_polish(output_dir, config)
    stages.append(
        _status_record(
            "polish",
            "polish",
            "passed" if polished_current else "pending",
            "validated polished Markdown is current"
            if polished_current
            else "run polish, have the Subagent write outputs, then run polish-validate",
        )
    )

    if not policy.requires_translation:
        stages.extend(
            [
                _status_record("extract-entities", "extract-entities", "skipped", "not used by pipeline: epub_conversion"),
                _status_record("translate-toc", "translate-toc", "skipped", "not used by pipeline: epub_conversion"),
                _status_record("translate", "translate", "skipped", "not used by pipeline: epub_conversion"),
            ]
        )
    else:
        entities_current, entity_detail = _entity_stage_status(output_dir, config) if policy.requires_entities else (True, "not required")
        stages.append(
            _status_record(
                "extract-entities",
                "extract-entities",
                "passed" if entities_current else "pending",
                entity_detail,
            )
        )
        try:
            from pdf2epub.toc_translation_workflow import validate_toc_translation_subagent

            toc_translation = validate_toc_translation_subagent(
                output_dir, materialize=False
            )
            toc_translation_current = toc_translation.get("valid") is True
        except (OSError, TypeError, ValueError):
            toc_translation_current = False
        stages.append(
            _status_record(
                "translate-toc",
                "translate-toc",
                "passed" if toc_translation_current else "pending",
                "translated TOC checkpoint is valid"
                if toc_translation_current
                else "run translate-toc and translate-toc-validate",
            )
        )
        translated_current = _current_translation(output_dir, config_path, config)
        stages.append(
            _status_record(
                "translate",
                "translate",
                "passed" if translated_current else "pending",
                "validated translated Markdown is current"
                if translated_current
                else "run translate, have the Subagent write outputs, then run translate-validate",
            )
        )

    epub_files = [path for path in output_dir.glob("*.epub") if path.is_file()]
    package_kinds = (
        ("pdf-translated",)
        if policy.requires_translation
        else ("pdf-source",)
    )
    package_command = (
        "build-epub --only-translated"
        if policy.requires_translation
        else "build-epub"
    ) if input_path.suffix.lower() == ".pdf" else "build-html-epub"
    stages.append(
        _package_stage(
            output_dir,
            epub_files,
            command=package_command,
            kinds=package_kinds,
        )
    )
    return _apply_stage_blockers(stages)


def _boundary_preview_summary(output_dir: Path) -> dict[str, Any] | None:
    """Summarize the read-only cross-page boundary preview written by refine-local."""
    report = _read_json(output_dir / "refine_boundary_preview.json")
    if not report:
        return None
    suspects = report.get("suspects")
    suspects = suspects if isinstance(suspects, list) else []
    return {
        "report": "refine_boundary_preview.json",
        "suspect_count": int(report.get("suspect_count") or len(suspects)),
        "units": sorted({
            str(item.get("unit")) for item in suspects
            if isinstance(item, Mapping) and item.get("unit")
        }),
    }


def _package_stage(
    output_dir: Path,
    epub_files: list[Path],
    *,
    command: str,
    kinds: tuple[str, ...],
) -> dict[str, Any]:
    """Report every pipeline-required EPUB target against its own receipt."""
    from pdf2epub.build_receipt import receipt_path, receipt_status

    receipt_file = receipt_path(output_dir)
    try:
        receipt = json.loads(receipt_file.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        receipt = {}
    builds = receipt.get("builds") if isinstance(receipt, Mapping) else None
    builds = builds if isinstance(builds, Mapping) else {}
    target_states: list[dict[str, Any]] = []
    for kind in kinds:
        entry = builds.get(kind)
        relative = str(entry.get("epub") or "") if isinstance(entry, Mapping) else ""
        state, detail = receipt_status(output_dir, kind)
        if state == "missing":
            # A receiptless directory may contain old EPUBs, but their language
            # cannot be inferred from names. A receipt that omits this kind is
            # an explicitly missing target, not a legacy artifact.
            if not receipt_file.exists() and epub_files:
                state = "legacy"
                detail = "legacy EPUB artifact(s) present; target attribution unknown"
            else:
                state = "pending"
                detail = f"{kind} EPUB has no build receipt"
        target_states.append({
            "kind": kind,
            "status": state,
            "artifact": relative if relative else None,
            "detail": detail,
        })

    statuses = [item["status"] for item in target_states]
    if "stale" in statuses:
        aggregate = "stale"
    elif "pending" in statuses:
        aggregate = "pending"
    elif "legacy" in statuses:
        # Preserve the historic pass behavior for receiptless artifacts while
        # making clear that no output language/target can be established.
        aggregate = "passed"
    else:
        aggregate = "passed"
    summary = "; ".join(
        f"{item['kind']}: {item['status']}"
        + (f" ({item['artifact']})" if item["artifact"] else "")
        + f"; {item['detail']}"
        for item in target_states
    ) or "no package target is required"
    if "legacy" in statuses:
        summary = "legacy artifact accepted without a build receipt; target attribution unknown; " + summary
    record = _status_record("package", command, aggregate, summary)
    record["targets"] = target_states
    return record


_HTML_STAGE_BY_KEY = {stage.key: stage for stage in HTML_WORKFLOW_STAGES}


def _html_stages(output_dir: Path) -> list[dict[str, Any]]:
    compressed = output_dir / "compressed_units"
    prepared = compressed.is_dir() and bool(_file_list(compressed)) and (
        output_dir / "translate-html_subagent_manifest.json"
    ).is_file()
    stages = [
        _status_record(
            "html-prepare",
            _HTML_STAGE_BY_KEY["html-prepare"].command,
            "passed" if prepared else "pending",
            "compressed HTML units and hand-off are present"
            if prepared
            else "run html-prepare",
        )
    ]
    validation = _read_json(output_dir / "translate-html_validation.json")
    validated = validation.get("all_passed") is True
    stages.extend(
        [
            _status_record(
                "translate-html",
                _HTML_STAGE_BY_KEY["translate-html"].command,
                "passed" if validated else "pending",
                "translated HTML checkpoint is valid"
                if validated
                else "have the Subagent write translated_compressed units",
            ),
            _status_record(
                "html-validate",
                _HTML_STAGE_BY_KEY["html-validate"].command,
                "passed" if validated else "pending",
                "all HTML units pass validation" if validated else "run html-validate",
            ),
        ]
    )
    epub_files = [path for path in output_dir.glob("*.epub") if path.is_file()]
    stages.append(
        _package_stage(
            output_dir,
            epub_files,
            command=_HTML_STAGE_BY_KEY["package"].command,
            kinds=("html-translated",),
        )
    )
    return _apply_stage_blockers(stages)


_REVIEW_MANIFESTS: tuple[tuple[str, str], ...] = (
    ("refine", "refine_subagent_manifest.json"),
    ("ocr-correct", "ocr-correct_subagent_manifest.json"),
    ("illustration", "illustration_subagent_manifest.json"),
    ("footnote", "footnote_subagent_manifest.json"),
    ("polish", "polish_subagent_manifest.json"),
    ("entities", "entity_subagent_manifest.json"),
    ("translate", "translate_subagent_manifest.json"),
    ("translate-html", "translate-html_subagent_manifest.json"),
)

_REVIEW_VALIDATIONS: dict[str, str] = {
    "ocr-correct": "ocr-correct_validation.json",
    "illustration": "illustration_validation.json",
    "footnote": "footnote_decision_validation.json",
    "polish": "polish_validation.json",
    "entities": "translation_entities_validation.json",
    "translate": "translate_validation.json",
    "translate-html": "translate-html_validation.json",
}

_PREPARE_COMMANDS: dict[str, str] = {
    "refine": "refine-prepare",
    "ocr-correct": "ocr-correct",
    "illustration": "illustration-prepare",
    "footnote": "footnote-prepare",
    "polish": "polish",
    "entities": "extract-entities",
    "translate": "translate",
    "translate-html": "html-prepare",
}


def _review_validation_summary(
    output_dir: Path,
    name: str,
    manifest_pending: list[str],
) -> dict[str, Any]:
    """Reference an existing validation receipt; never recompute its result."""
    filename = _REVIEW_VALIDATIONS.get(name)
    if not filename:
        return {}
    path = output_dir / filename
    report = _read_json(path)
    valid_values = report.get("valid_files")
    completed = sorted(
        {str(item) for item in valid_values if item is not None and str(item).strip()}
    ) if isinstance(valid_values, list) else []
    pending = sorted(set(manifest_pending) - set(completed))
    source_sha256 = report.get("source_sha256")
    return {
        "file": filename,
        "present": path.is_file(),
        "completed": completed,
        "pending": pending,
        "source_sha256": dict(source_sha256) if isinstance(source_sha256, Mapping) else {},
    }


def _review_manifest_source_mismatch(
    manifest: Mapping[str, Any],
    validation: Mapping[str, Any],
) -> list[str]:
    """Return units whose recorded source hashes disagree with the receipt."""
    manifest_hashes = manifest.get("source_sha256")
    validation_hashes = validation.get("source_sha256")
    if not isinstance(manifest_hashes, Mapping) or not isinstance(validation_hashes, Mapping):
        return []
    mismatched = []
    for name in set(manifest_hashes) & set(validation_hashes):
        if manifest_hashes.get(name) != validation_hashes.get(name):
            mismatched.append(str(name))
    return sorted(mismatched)


def _review_gates(output_dir: Path) -> list[dict[str, Any]]:
    """Summarize which Subagent hand-offs still need an operator decision.

    The manifest is written by ``decide_resume`` and already carries the
    pending taxonomy, while the validation receipt carries the authoritative
    completed set.  ``status`` therefore references those artifacts instead of
    recomputing pending batches or waves.
    """
    from pdf2epub.markdown_handoff_resume import pending_reason_counts

    gates: list[dict[str, Any]] = []
    for name, filename in _REVIEW_MANIFESTS:
        path = output_dir / filename
        if not path.is_file():
            continue
        manifest = _read_json(path)
        if not manifest:
            gates.append(
                {
                    "name": name,
                    "manifest": filename,
                    "source_manifest_sha": sha256_file(path),
                    "status": "unreadable",
                    "needs_subagent": True,
                    "detail": "manifest exists but could not be parsed; rerun the prepare step",
                }
            )
            continue
        pending_files = manifest.get("pending_files")
        pending = sorted(pending_files) if isinstance(pending_files, list) else []
        pending_reasons = manifest.get("pending_reasons")
        pending_reasons = (
            {str(key): str(value) for key, value in pending_reasons.items()}
            if isinstance(pending_reasons, Mapping)
            else {}
        )
        reason_counts = manifest.get("pending_reason_counts")
        if not isinstance(reason_counts, Mapping) or not reason_counts:
            reason_counts = pending_reason_counts(pending_reasons)
        handoffs = manifest.get("worker_handoffs")
        workers = [item for item in handoffs if isinstance(item, dict)] if isinstance(handoffs, list) else []
        worker_status: dict[str, int] = {}
        for worker in workers:
            key = str(worker.get("dispatch_status") or "unknown")
            worker_status[key] = worker_status.get(key, 0) + 1
        waves = manifest.get("dispatch_plan")
        wave_count = len(waves) if isinstance(waves, list) else 0
        validation = _review_validation_summary(output_dir, name, pending)
        mismatched = _review_manifest_source_mismatch(manifest, validation)
        declared_status = str(manifest.get("status") or "").strip()
        if mismatched:
            status = "stale_manifest"
        elif declared_status:
            status = declared_status
        elif pending_reasons or pending:
            status = "pending_review"
        elif workers:
            status = "assigned"
        else:
            status = "clear"
        needs_subagent = (
            status in {"pending_review", "unreadable", "stale_manifest"}
            or status.startswith("human_review")
        )
        detail_parts: list[str] = []
        if pending:
            detail_parts.append(f"{len(pending)} pending file(s)")
        for label, key in (
            ("review candidate(s)", "review_candidate_count"),
            ("visual audit task(s)", "visual_audit_task_count"),
        ):
            value = manifest.get(key)
            if isinstance(value, int) and value > 0:
                detail_parts.append(f"{value} {label}")
        if reason_counts:
            detail_parts.append(
                "pending reasons: "
                + ", ".join(f"{key}={value}" for key, value in sorted(reason_counts.items()))
            )
        if workers:
            detail_parts.append(f"{len(workers)} worker hand-off(s)")
        if wave_count:
            detail_parts.append(f"{wave_count} dispatch wave(s)")
        if worker_status:
            detail_parts.append(
                ", ".join(f"{key}={value}" for key, value in sorted(worker_status.items()))
            )
        if mismatched:
            prepare = _PREPARE_COMMANDS.get(name, "the prepare step")
            detail_parts.append(
                "manifest source hashes differ from the validation receipt for "
                + ", ".join(mismatched[:5])
                + f"; rerun {prepare}"
            )
        gates.append(
            {
                "name": name,
                "manifest": filename,
                "source_manifest_sha": sha256_file(path),
                "status": status,
                "needs_subagent": needs_subagent,
                "pending_files": len(pending),
                "pending_file_names": pending[:10],
                "pending_reasons": pending_reasons,
                "pending_reason_counts": dict(reason_counts),
                "validation": validation,
                "workers": len(workers),
                "worker_status": worker_status,
                "waves": wave_count,
                "detail": "; ".join(detail_parts) or "nothing outstanding",
            }
        )
    return gates


def _apply_stage_blockers(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Propagate currency and reachability through the stage ladder.

    ``status`` answers two different questions per stage: whether the stage's
    own artifact is *current* (its checkpoint still matches its inputs) and
    whether the stage is *reachable* (every earlier stage is current, so the
    command can actually do something).  A ``passed`` stage downstream of a
    stale or blocked stage is reported as ``stale`` instead of leaking an
    "everything is fine" signal, and downstream pending stages are no longer
    offered as the next command when an earlier stage cannot run.
    """
    upstream_current = True
    blocking_stages: list[str] = []
    for stage in stages:
        if stage["status"] == "skipped":
            stage["current"] = True
            stage["reachable"] = upstream_current
            continue
        stage["current"] = upstream_current and stage["status"] == "passed"
        stage["reachable"] = upstream_current and stage["status"] != "blocked"
        if stage["status"] == "passed" and not stage["current"]:
            stage["status"] = "stale"
            if any(
                isinstance(target, Mapping) and target.get("status") == "legacy"
                for target in stage.get("targets", [])
            ):
                stage["detail"] += (
                    "; legacy artifact is not verified against current upstream stages"
                )
            else:
                stage["detail"] = (
                    "artifact exists but is stale because an earlier stage is not "
                    "current; rebuild it after resolving that stage"
                )
        if stage["status"] == "stale" and blocking_stages:
            stage["blocked_by"] = list(blocking_stages)
        if stage["status"] == "blocked" and blocking_stages:
            stage["blocked_by"] = list(blocking_stages)
        if not stage["current"]:
            upstream_current = False
            blocking_stages.append(stage["name"])
    return stages


def _ocr_evidence(
    output_dir: Path,
    config: Mapping[str, Any],
    input_type: str | None,
) -> dict[str, Any] | None:
    """Describe the page-transcription evidence level behind the pipeline.

    Single OCR is a legitimate cost trade-off, but it must not read as
    ``cross-checked``.  Making the evidence level explicit in ``status`` keeps
    the gate strength visible instead of hiding it in the configuration.
    """
    if input_type != "pdf":
        return None
    try:
        legacy = is_native_text_source(output_dir)
    except ValueError as exc:
        return {
            "mode": "indeterminate",
            "cross_verified": False,
            "detail": f"源页面证据互相矛盾: {exc}",
        }
    if legacy:
        return {
            "mode": "legacy_native",
            "cross_verified": False,
            "detail": "旧原生版面证据；没有视觉 OCR 交叉验证",
        }
    if ocr_evidence_mode(config) == "two_ocr":
        return {
            "mode": "two_ocr",
            "cross_verified": True,
            "detail": "布局门禁要求当前双 OCR 共识检查点",
        }
    return {
        "mode": "single_ocr",
        "cross_verified": False,
        "detail": "单 OCR 视觉转录；正常页未经第二套 OCR 交叉验证，只有异常页进入视觉复核",
    }


def build_status(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    """Build a read-only status snapshot for the configured book."""
    path = Path(config_path).expanduser()
    config, errors = _read_config(path)
    report: dict[str, Any] = {
        "schema_version": STATUS_SCHEMA_VERSION,
        "config_path": str(path),
        "config_valid": not errors,
        "errors": errors,
        "input_type": None,
        "input_path": None,
        "input_exists": False,
        "pipeline": None,
        "output_dir": None,
        "stages": [],
        "pending_files": [],
        "next_command": None,
        "blocked_stage": None,
        "blocked_reason": None,
        "review_gates": [],
        "ocr_evidence": None,
        "boundary_preview": None,
    }
    if errors:
        return report
    title = str(config.get("title") or "").strip()
    if not title:
        report["errors"] = ["configuration title is missing"]
        report["config_valid"] = False
        return report
    output_dir = book_output_dir(title)
    report["output_dir"] = str(output_dir)
    try:
        input_type, input_path = _resolve_input(config, path, output_dir)
    except (OSError, ValueError) as exc:
        report["errors"] = [str(exc)]
        report["config_valid"] = False
        return report
    report["input_type"] = input_type
    report["input_path"] = str(input_path)
    report["input_exists"] = input_path.is_file()
    try:
        policy = PipelinePolicy.from_config(config)
    except ValueError as exc:
        report["config_valid"] = False
        report["errors"] = [str(exc)]
        report["stages"] = [_status_record("pipeline", "doctor", "blocked", str(exc))]
        return report
    report["pipeline"] = policy.kind
    # The documented stage ladder comes from the shared table; the status
    # command only decides which artifact checks apply to the configured input.
    workflow_stages_for(policy.kind)
    if policy.kind in {"novel", "tex"}:
        stages = [
            _status_record(
                "status",
                "status",
                "passed",
                "stage ladder is documented in pdf2epub/workflow_stages.py; "
                f"run {workflow_stages_for(policy.kind)[1].invocation}",
            ),
            _status_record(
                "package",
                workflow_stages_for(policy.kind)[-1].command,
                "pending",
                "no delivered EPUB artifact is tracked for this pipeline yet",
            ),
        ]
    elif input_type == "epub":
        stages = _html_stages(output_dir)
    else:
        stages = _pdf_stages(output_dir, path, config, input_path)
    if policy.kind in {"novel", "tex"}:
        stages = _apply_stage_blockers(stages)
    report["stages"] = stages
    report["ocr_evidence"] = _ocr_evidence(output_dir, config, input_type)
    report["review_gates"] = _review_gates(output_dir) if output_dir.is_dir() else []
    report["boundary_preview"] = (
        _boundary_preview_summary(output_dir) if output_dir.is_dir() else None
    )
    pending = []
    for stage in stages:
        pending.extend(stage.get("pending_files", []))
    report["pending_files"] = sorted(set(pending))
    actionable = next(
        (
            stage
            for stage in stages
            if stage["status"] in {"pending", "stale"} and stage.get("reachable")
        ),
        None,
    )
    if actionable is not None:
        report["next_command"] = (
            f"uv run pdf2epub -c {path} {actionable['command']}"
        )
    else:
        # Nothing downstream can run: surface the earliest stage that blocks
        # the ladder (usually a missing or replaced input file).
        blocker = next(
            (stage for stage in stages if stage["status"] == "blocked"), None
        )
        if blocker is not None:
            report["blocked_stage"] = blocker["name"]
            report["blocked_reason"] = blocker["detail"]
    return report


def _dependency_available(module_name: str) -> bool:
    return importlib.util.find_spec(module_name) is not None


def build_doctor(config_path: str | Path = "config.yaml") -> dict[str, Any]:
    """Check configuration, selected provider dependencies and local paths."""
    path = Path(config_path).expanduser()
    config, errors = _read_config(path)
    checks: list[dict[str, Any]] = []

    def check(name: str, ok: bool, detail: str, *, severity: str = "error") -> None:
        checks.append({
            "name": name,
            "status": "passed" if ok else ("info" if severity == "info" else "blocked"),
            "detail": detail,
        })

    check("config_file", not errors, "; ".join(errors) or "configuration file is readable")
    if errors:
        return {
            "schema_version": DOCTOR_SCHEMA_VERSION,
            "config_path": str(path),
            "ok": False,
            "checks": checks,
        }

    title = str(config.get("title") or "").strip()
    warnings = []
    check("title", bool(title), "title is configured" if title else "title is missing")
    try:
        policy = PipelinePolicy.from_config(config)
    except ValueError as exc:
        policy = None
        check("pipeline", False, str(exc))
    else:
        check("pipeline", True, f"pipeline: {policy.kind}")
    output_dir = book_output_dir(title) if title else Path("output") / "untitled"
    try:
        from pdf2epub.refine.footnote_options import footnote_mode_warnings
        warnings = footnote_mode_warnings(output_dir, config)
    except ValueError:
        warnings = []
    try:
        input_type, input_path = _resolve_input(config, path, output_dir)
        check("input", input_path.is_file(), str(input_path) if input_path.is_file() else f"input file is missing: {input_path}")
    except (OSError, ValueError) as exc:
        input_type, input_path = "unknown", Path()
        check("input", False, str(exc))

    if policy is not None:
        stages = workflow_stages_for(policy.kind)
        check(
            "pipeline_stages",
            bool(stages),
            f"{policy.kind}: "
            + " → ".join(stage.command for stage in stages if stage.key != "status"),
            severity="info",
        )
        if policy.requires_compressed_cache and input_type not in {"epub", "unknown"}:
            check(
                "pipeline_input",
                False,
                "pipeline: html expects an EPUB/MOBI/AZW3 input",
            )

    ocr = config.get("ocr") if isinstance(config.get("ocr"), Mapping) else {}
    primary = str(ocr.get("backend") or "chandra").strip().lower()
    supported = set(supported_backends())
    primary_ok = primary in supported
    check("ocr.primary_backend", primary_ok, f"{primary} (supported: {', '.join(sorted(supported))})")

    secondary_enabled = secondary_ocr_enabled(config)
    secondary = secondary_backend_name(config)
    if secondary_enabled:
        check(
            "ocr.secondary_backend",
            secondary in supported,
            f"{secondary or 'missing'} (supported: {', '.join(sorted(supported))})",
        )
        check("ocr.secondary_distinct", secondary != primary, "secondary backend differs from primary")
    else:
        check("ocr.secondary_backend", True, "secondary OCR is disabled", severity="info")

    try:
        from pdf2epub.ocr_consensus import validate_ocr_config

        validate_ocr_config(config)
        check("ocr.configuration", True, "OCR configuration is internally consistent")
    except ValueError as exc:
        check("ocr.configuration", False, str(exc))

    required_modules: dict[str, str] = {
        "chandra": "openai",
        "vllm": "anthropic, google.genai, openai, tenacity",
    }
    selected_backends = [primary]
    if secondary_enabled and secondary:
        selected_backends.append(secondary)
    for backend in dict.fromkeys(selected_backends):
        if backend not in required_modules:
            check(
                f"dependency.{backend}",
                False,
                "backend is not registered; remove the retired or unknown backend configuration",
            )
            continue
        module_names = [item.strip() for item in required_modules[backend].split(",") if item.strip()]
        missing = [module for module in module_names if not _dependency_available(module)]
        check(
            f"dependency.{backend}",
            not missing,
            "selected backend dependencies are importable"
            if not missing
            else "missing: " + ", ".join(missing),
        )

    check(
        "output_directory",
        output_dir.is_dir(),
        str(output_dir) if output_dir.is_dir() else "output directory does not exist yet",
        severity="info",
    )
    status = build_status(path)
    check(
        "workflow_status",
        not status.get("errors"),
        "workflow status can be evaluated"
        if not status.get("errors")
        else "; ".join(status["errors"]),
    )
    return {
        "schema_version": DOCTOR_SCHEMA_VERSION,
        "config_path": str(path),
        "ok": not any(item["status"] == "blocked" for item in checks),
        "checks": checks,
        "warnings": warnings,
        "status": status,
    }


def _print_json(report: Mapping[str, Any]) -> None:
    print(json.dumps(report, ensure_ascii=False, indent=2))


def status_command(args: Any) -> int:
    report = build_status(getattr(args, "config", "config.yaml"))
    if getattr(args, "json", False):
        _print_json(report)
    else:
        print(f"配置: {report['config_path']}")
        if report.get("pipeline"):
            print(f"流程: {report['pipeline']} ({report.get('input_type') or 'unknown'})")
        if report.get("output_dir"):
            print(f"输出: {report['output_dir']}")
        evidence = report.get("ocr_evidence")
        if evidence:
            verified = "已交叉验证" if evidence["cross_verified"] else "未交叉验证"
            print(f"OCR 证据: {evidence['mode']} ({verified}) — {evidence['detail']}")
        boundary = report.get("boundary_preview")
        if boundary and boundary.get("suspect_count"):
            print(
                f"边界预检: {boundary['suspect_count']} 处可疑跨页合并（"
                + ", ".join(boundary.get("units", [])[:5])
                + "）"
            )
        if report.get("errors"):
            for error in report["errors"]:
                print(f"阻断: {error}")
        for stage in report.get("stages", []):
            print(f"[{stage['status']}] {stage['name']}: {stage['detail']}")
        gates = report.get("review_gates") or []
        if gates:
            print("Subagent 复核门:")
            for gate in gates:
                flag = "需要复核" if gate["needs_subagent"] else "无需复核"
                print(f"  [{gate['status']}] {gate['name']} ({flag}): {gate['detail']}")
        if report.get("next_command"):
            print(f"下一步: {report['next_command']}")
        elif report.get("blocked_stage"):
            print(
                f"阻塞: {report['blocked_stage']} — {report.get('blocked_reason') or 'unavailable'}"
            )
            print("下一步: 先解决上述阻塞；无法执行的阶段不会给出可运行命令")
    return 1 if report.get("errors") else 0


def doctor_command(args: Any) -> int:
    report = build_doctor(getattr(args, "config", "config.yaml"))
    if getattr(args, "json", False):
        _print_json(report)
    else:
        print(f"配置: {report['config_path']}")
        for check in report.get("checks", []):
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
        for warning in report.get("warnings", []):
            if isinstance(warning, Mapping) and warning.get("message"):
                print(f"[warning] {warning['message']}")
        print("doctor: " + ("通过" if report.get("ok") else "存在阻断"))
    return 0 if report.get("ok") else 1


__all__ = [
    "build_doctor",
    "build_status",
    "doctor_command",
    "status_command",
]

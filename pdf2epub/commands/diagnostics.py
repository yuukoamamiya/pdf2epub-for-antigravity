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
from pdf2epub.ocr_consensus import secondary_ocr_enabled, secondary_backend_name
from pdf2epub.ocr_progress import assess_progress
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.common import (
    book_output_dir,
    resolve_book_input_path,
)


STATUS_SCHEMA_VERSION = 1
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
        return hashlib.sha256(path.read_bytes()).hexdigest()
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
        from pdf2epub.refine.main import _pages_fingerprint

        current_toc = hashlib.sha256(toc_path.read_bytes()).hexdigest()
        pages_dir, _ = select_refinement_pages(output_dir, config=config)
        current_pages = _pages_fingerprint(pages_dir)
        if fingerprint.get("toc_sha256") != current_toc or fingerprint.get("pages_sha256") != current_pages:
            return "pending", "tree_progress.json is stale after a TOC or page change"
        binding_path = output_dir / "illustration_bindings.json"
        expected_binding = fingerprint.get("illustration_bindings_sha256")
        actual_binding = hashlib.sha256(binding_path.read_bytes()).hexdigest() if binding_path.is_file() else None
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
        return _polished_stage_is_current(output_dir, polish_dir, source_dir)
    except (OSError, TypeError, ValueError):
        return False


def _current_entities(output_dir: Path, config: Mapping[str, Any]) -> bool:
    try:
        from pdf2epub.commands.entities import _entity_context_is_current
        from pdf2epub.commands.sources import _resolve_pdf_markdown_source

        source_dir, _ = _resolve_pdf_markdown_source(output_dir, dict(config))
        report = _read_json(output_dir / "translation_entities_validation.json")
        return report.get("valid") is True and _entity_context_is_current(
            output_dir,
            source_dir,
            (config.get("translation") or {}).get("source_language", "English"),
            (config.get("translation") or {}).get("target_language", "Chinese"),
        )
    except (OSError, TypeError, ValueError):
        return False


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
    stages: list[dict[str, Any]] = []
    ocr_status, ocr_detail = _pdf_ocr_status(output_dir, input_path)
    stages.append(_status_record("ocr-pages", "ocr-pages --resume", ocr_status, ocr_detail))

    probe = _read_json(output_dir / "pdf_text_probe.json")
    native_text = probe.get("classification") == "native_text"
    if native_text or not policy.requires_ocr_correction:
        stages.append(
            _status_record(
                "ocr-correct",
                "ocr-correct",
                "skipped",
                "not required for native-text PDF or single-OCR mode",
            )
        )
    else:
        try:
            from pdf2epub.ocr_correction import ocr_correction_is_current

            current = ocr_correction_is_current(output_dir, config)
        except (OSError, TypeError, ValueError):
            current = False
        stages.append(
            _status_record(
                "ocr-correct",
                "ocr-correct",
                "passed" if current else "pending",
                "visual OCR correction checkpoint is current"
                if current
                else "run ocr-correct, have the Subagent write corrections, then run ocr-correct-validate",
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
        from pdf2epub.refine.footnote_apply import footnote_normalization_is_current

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
        entities_current = _current_entities(output_dir, config) if policy.requires_entities else True
        stages.append(
            _status_record(
                "extract-entities",
                "extract-entities",
                "passed" if entities_current else "pending",
                "validated book terminology is current"
                if entities_current
                else "run extract-entities and validate the Subagent output",
            )
        )
        try:
            from pdf2epub.toc_translation_workflow import validate_toc_translation_subagent

            toc_translation = validate_toc_translation_subagent(output_dir)
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
    stages.append(
        _status_record(
            "package",
            "build-epub" if input_path.suffix.lower() == ".pdf" else "build-html-epub",
            "passed" if epub_files else "pending",
            f"{len(epub_files)} EPUB artifact(s) found" if epub_files else "no generated EPUB found",
        )
    )
    return _apply_stage_blockers(stages)


def _html_stages(output_dir: Path) -> list[dict[str, Any]]:
    compressed = output_dir / "compressed_units"
    prepared = compressed.is_dir() and bool(_file_list(compressed)) and (
        output_dir / "translate-html_subagent_manifest.json"
    ).is_file()
    stages = [
        _status_record(
            "html-prepare",
            "html-prepare",
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
                "html-prepare",
                "passed" if validated else "pending",
                "translated HTML checkpoint is valid"
                if validated
                else "have the Subagent write translated_compressed units",
            ),
            _status_record(
                "html-validate",
                "html-validate",
                "passed" if validated else "pending",
                "all HTML units pass validation" if validated else "run html-validate",
            ),
        ]
    )
    epub_files = [path for path in output_dir.glob("*.epub") if path.is_file()]
    stages.append(
        _status_record(
            "package",
            "build-html-epub",
            "passed" if epub_files else "pending",
            f"{len(epub_files)} EPUB artifact(s) found" if epub_files else "no generated EPUB found",
        )
    )
    return _apply_stage_blockers(stages)


def _apply_stage_blockers(stages: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Mark downstream work as blocked when an earlier actionable stage is pending."""
    blocked_by: list[str] = []
    for stage in stages:
        if stage["status"] == "skipped":
            continue
        if blocked_by and stage["status"] not in {"passed", "skipped"}:
            stage["status"] = "blocked"
            stage["blocked_by"] = list(blocked_by)
        elif stage["status"] in {"pending", "blocked"}:
            blocked_by.append(stage["name"])
    return stages


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
    policy = PipelinePolicy.from_config(config)
    report["pipeline"] = policy.kind
    if input_type == "epub":
        stages = _html_stages(output_dir)
    else:
        stages = _pdf_stages(output_dir, path, config, input_path)
    report["stages"] = stages
    pending = []
    for stage in stages:
        pending.extend(stage.get("pending_files", []))
    report["pending_files"] = sorted(set(pending))
    for stage in stages:
        if stage["status"] in {"pending", "blocked"}:
            report["next_command"] = f"uv run pdf2epub -c {path} {stage['command']}"
            break
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
    check("title", bool(title), "title is configured" if title else "title is missing")
    configured_kind = str(config.get("pipeline") or config.get("mode") or "translation").strip().lower()
    check(
        "pipeline",
        configured_kind in {"translation", "epub_conversion", "ocr_to_epub"},
        f"pipeline: {configured_kind or 'translation'}",
    )
    output_dir = book_output_dir(title) if title else Path("output") / "untitled"
    try:
        input_type, input_path = _resolve_input(config, path, output_dir)
        check("input", input_path.is_file(), str(input_path) if input_path.is_file() else f"input file is missing: {input_path}")
    except (OSError, ValueError) as exc:
        input_type, input_path = "unknown", Path()
        check("input", False, str(exc))

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
        "azure": "azure.ai.documentintelligence",
        "vision": "google.cloud.vision_v1",
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
        if report.get("errors"):
            for error in report["errors"]:
                print(f"阻断: {error}")
        for stage in report.get("stages", []):
            print(f"[{stage['status']}] {stage['name']}: {stage['detail']}")
        if report.get("next_command"):
            print(f"下一步: {report['next_command']}")
    return 1 if report.get("errors") else 0


def doctor_command(args: Any) -> int:
    report = build_doctor(getattr(args, "config", "config.yaml"))
    if getattr(args, "json", False):
        _print_json(report)
    else:
        print(f"配置: {report['config_path']}")
        for check in report.get("checks", []):
            print(f"[{check['status']}] {check['name']}: {check['detail']}")
        print("doctor: " + ("通过" if report.get("ok") else "存在阻断"))
    return 0 if report.get("ok") else 1


__all__ = [
    "build_doctor",
    "build_status",
    "doctor_command",
    "status_command",
]

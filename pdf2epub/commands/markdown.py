"""PDF Markdown preparation, validation, and readiness commands.

This module owns the Markdown hand-off contract for PDF workflows. It performs
local preparation and validation only; translation remains delegated to the
workspace Subagent.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict

from loguru import logger

from pdf2epub.commands.entities import _entity_context_is_current
from pdf2epub.commands.runtime import load_book_context
from pdf2epub.commands.sources import (
    _polished_stage_is_current,
    _resolve_pdf_markdown_source,
)
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.common import book_output_dir, load_config
from pdf2epub.workflow_contracts import atomic_write_text, relative_posix_path


def _load_pdf_file_roles(output_dir: Path) -> dict:
    """Load chapter roles produced by refine-local for translation prompts."""
    progress = output_dir / "ocr_markdown" / "tree_progress.json"
    if not progress.is_file():
        return {}
    try:
        data = json.loads(progress.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    roles = {}
    for unit in data.get("units", []):
        role = str(unit.get("type") or "").strip().lower()
        if role in {"bibliography", "index"}:
            files = unit.get("part_files") or [unit.get("file")]
            for file_name in files:
                if file_name:
                    roles[str(file_name)] = role
    if roles:
        return roles

    # Older tree_progress files predate the ``type`` field.  Recover roles
    # from the current TOC so adding a type to toc_tree.json does not require
    # deleting a resumable refinement checkpoint.
    toc_path = output_dir / "toc_tree.json"
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
        from pdf2epub.utils.unit_id import generate_unit_id

        def visit(nodes, path):
            for index, node in enumerate(nodes or [], 1):
                node_path = path + [index]
                role = str(node.get("type") or "").strip().lower()
                if role in {"bibliography", "index"}:
                    roles[f"{generate_unit_id(node_path)}.md"] = role
                visit(node.get("children", []), node_path)

        visit(toc.get("chapters", []), [])
    except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError):
        pass
    return roles


def _load_pdf_file_contexts(output_dir: Path) -> dict:
    """Map each generated unit to its human-readable TOC hierarchy."""
    toc_path = Path(output_dir) / "toc_tree.json"
    if not toc_path.is_file():
        return {}
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
        from pdf2epub.utils.unit_id import generate_unit_id
    except (OSError, json.JSONDecodeError, ImportError):
        return {}

    contexts: Dict[str, str] = {}

    def visit(nodes: Any, path: list[int], parents: list[str]) -> None:
        if not isinstance(nodes, list):
            return
        for index, node in enumerate(nodes, 1):
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            current = parents + ([title] if title else [])
            unit_name = f"{generate_unit_id(path + [index])}.md"
            if current:
                contexts[unit_name] = " → ".join(current)
            visit(node.get("children", []), path + [index], current)

    visit(toc.get("chapters", []), [], [])

    # Refinement may split a unit into chapter_N.partM.md files.  Reuse the
    # same hierarchy for every part without making the Subagent infer it.
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            for unit in progress.get("units", []):
                base = str(unit.get("file") or "")
                context = contexts.get(base)
                if context:
                    for part in unit.get("part_files") or [base]:
                        contexts[str(part)] = context
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass
    return contexts


def polish_command(args):
    """Prepare a local Markdown hand-off for a polishing Subagent."""
    return _prepare_pdf_markdown_task(args, "polish")


def _prepare_pdf_markdown_task(args, task: str):
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.subagent_runtime import resolve_subagent_model

    context = load_book_context(args, f"{task}-prepare")
    if context is None:
        return 1
    config = context.config
    policy = PipelinePolicy.from_config(config)
    conversion_pipeline = policy.is_conversion
    if task == "translate" and conversion_pipeline:
        logger.error(
            "pipeline: epub_conversion is language-neutral; remove that setting "
            "before running translate."
        )
        return 1
    book_title = context.book_title
    output_dir = context.output_dir
    translation = config.get("translation", {})
    source_language = getattr(args, "source_language", None) or policy.source_language or (
        "Original" if conversion_pipeline else "English"
    )
    target_language = getattr(args, "target_language", None) or policy.target_language or (
        "Original" if conversion_pipeline else "Chinese"
    )
    glossary_bundle = None
    context_files = {}
    skipped_context_files = []
    unit_context_files = {}
    heading_contexts = {}
    prompt_context_files = None
    if task == "translate":
        from pdf2epub.glossary import load_selected_glossaries

        try:
            glossary_bundle = load_selected_glossaries(
                config,
                output_dir,
                source_language,
                target_language,
                context.config_path,
            )
        except Exception as exc:
            logger.error(f"Could not load external glossary: {exc}")
            return 1
    if task == "polish":
        source_dir = output_dir / "ocr_markdown"
        target_dir = output_dir / "polished_markdown"
        rules = [
            "Repair source layout and line wrapping while preserving meaning and document structure.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations.",
            "Never add a # heading marker to an ordinary paragraph, bold line, italic line, Roman numeral, or numbered section that does not already begin with #. Preserve the source heading marker and level exactly.",
            "Only remove a heading when it is an obvious duplicated running header; never remove a unique section heading.",
            "When a Notes/注释 section contains numbered endnotes, convert only verified footnote superscripts from <sup>N</sup> to [^N], and convert the matching endnote lines to [^N]: text. Do not convert mathematical, table, ordinal, or other non-footnote superscripts.",
        ]
        rules.insert(
            1,
            "For OCR-derived sources, fix obvious OCR errors and OCR-induced line breaks without inventing content. For native vector-text sources, the shared handoff rules instead require layout-only paragraph reconstruction.",
        )
        content_type = getattr(args, "content_type", "auto")
        if content_type and content_type != "auto":
            rules.append(f"Treat this as {content_type} content and preserve its domain-specific conventions.")
    else:
        source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
        if source_stage != "polished":
            logger.error(
                "PDF translation requires a current validated polished source for "
                "both OCR-derived and native-text PDFs. Run polish and "
                "polish-validate before translate."
            )
            return 1
        from pdf2epub.toc_translation_workflow import (
            build_toc_heading_contexts,
            validate_toc_translation_subagent,
        )

        toc_report = validate_toc_translation_subagent(output_dir)
        if not toc_report["valid"]:
            logger.error(
                "TOC translation must be completed and validated before PDF "
                "translation: " + "; ".join(toc_report["errors"][:5])
            )
            return 1
        heading_contexts = build_toc_heading_contexts(output_dir)
        target_dir = output_dir / "translated"
        require_entities = PipelinePolicy.from_config(config).requires_entities
        entity_path = output_dir / "translation_entities.json"
        skip_entities = bool(getattr(args, "skip_entities", False))
        if require_entities and not skip_entities:
            if not entity_path.is_file():
                logger.error(
                    "translation_entities.json is required before PDF translation. "
                    "Run extract-entities, let the Subagent write the file, then "
                    "run extract-entities-validate; use --skip-entities only "
                    "when a glossary is genuinely unnecessary."
                )
                return 1
            try:
                from pdf2epub.entity_extractor import validate_entities

                entity_data = json.loads(entity_path.read_text(encoding="utf-8"))
                entity_errors = validate_entities(
                    entity_data, book_title, source_language, target_language
                )
            except (OSError, json.JSONDecodeError) as exc:
                entity_errors = [f"invalid translation_entities.json: {exc}"]
            if entity_errors:
                for error in entity_errors:
                    logger.error(f"Entity glossary: {error}")
                return 1
        rules = [
            "Translate prose to the target language; do not summarize, censor, or add commentary.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations exactly.",
            "Keep one output file for every source file and keep filenames unchanged.",
            "Output only the target-language replacement: never add bilingual paragraphs, parallel English titles, or the original text beside the translation.",
            "Do not upgrade ordinary paragraphs, italic text, or bold text into Markdown headings: preserve exactly whether the source line begins with #.",
            "If a standalone REFERENCES, Bibliography, Literatur, Notes, or equivalent end-of-book label has no # prefix in the source, keep it as an ordinary paragraph in the translation; never upgrade it to a Markdown heading.",
            "Preserve Markdown code fences (```) exactly: the translated output must contain the same number of fence markers as the source; if the source has none, do not add any.",
        ]
        if skip_entities:
            rules.append(
                "The translation glossary gate was explicitly skipped for this task; do not invent or expect a translation_entities.json context file."
            )
        else:
            rules.append(
                "Read the unit-specific or worker-deduplicated terminology context before translating. Full entity and domain snapshots are audit-only; consult them only to resolve an explicit context gap or conflict, not as routine input. Do not modify any context file."
            )
        if glossary_bundle and glossary_bundle.rules:
            rules.extend(glossary_bundle.rules)
            rules.append(
                "When book-specific entities and domain glossary entries overlap, apply domain fixed, then domain preferred, then book-entity precedence; prefer the longest matching source form and report any unresolved conflict instead of silently inventing a third translation."
            )
        context_files = dict(glossary_bundle.context_files if glossary_bundle else {})
        if task == "translate" and not skip_entities and entity_path.is_file():
            context_files["translation_entities"] = entity_path
        if task == "translate" and (skip_entities or not entity_path.is_file()):
            skipped_context_files.append("translation_entities")
        from pdf2epub.glossary import build_unit_glossary_contexts

        unit_context_files = build_unit_glossary_contexts(
            output_dir,
            source_dir,
            glossary_bundle.context_files if glossary_bundle else {},
            None if skip_entities else entity_path,
        )
        prompt_context_files = {
            name: path
            for name, path in context_files.items()
            if str(name).startswith("reference_glossary_")
        }
    try:
        paths = prepare_markdown_subagent(
            output_dir,
            task,
            source_dir,
            target_dir,
            source_language,
            target_language,
            rules,
            config=config,
            resume=getattr(args, "resume", False),
            file_roles=_load_pdf_file_roles(output_dir) if task == "translate" else None,
            context_files=context_files or None,
            skipped_context_files=skipped_context_files,
            prompt_context_files=prompt_context_files,
            file_contexts=(
                _load_pdf_file_contexts(output_dir) if task == "translate" else None
            ),
            heading_contexts=heading_contexts or None,
            unit_context_files=unit_context_files or None,
        )
        from pdf2epub.subagent_runtime import write_worker_handoffs

        handoffs = write_worker_handoffs(
            output_dir,
            paths["manifest"],
            paths["prompt"],
        )
        if handoffs:
            handoff_dir_name = (
                "worker_handoffs" if task == "translate" else f"{task}_worker_handoffs"
            )
            paths["worker_handoffs"] = output_dir / handoff_dir_name
    except Exception as exc:
        logger.error(f"Could not prepare {task} task: {exc}")
        return 1
    logger.success(f"Wrote Subagent prompt: {paths['prompt']}")
    logger.info(f"Recommended Antigravity model: {resolve_subagent_model(config, task)}")
    if task in {"translate", "polish"}:
        handoff_dir_label = (
            "worker_handoffs" if task == "translate" else f"{task}_worker_handoffs"
        )
        logger.warning(
            "此步骤只生成交接文件，没有执行翻译；现在必须在 Antigravity 中打开工作区 "
            f"Subagent，并按 {handoff_dir_label}/ 中的 assigned_files 执行。"
            + ("TOC 已由独立前置任务完成。" if task == "translate" else "")
        )
    logger.info(
        f"在 Antigravity 中让 Subagent 执行提示词，完成后运行 {task}-validate。"
    )
    return 0


def polish_validate_command(args):
    return _validate_pdf_markdown_task(args, "polish")


def _validate_translation_entities(output_dir: Path, config: dict) -> dict:
    """Validate the glossary contract recorded by the translation manifest."""
    manifest_path = output_dir / "translate_subagent_manifest.json"
    required = PipelinePolicy.from_config(config).requires_entities
    if not manifest_path.is_file():
        return {"valid": not required, "errors": ["translation manifest is missing"]}
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"invalid translation manifest: {exc}"]}

    context_files = manifest.get("context_files", {})
    entity_relative = context_files.get("translation_entities")
    if not entity_relative:
        if "translation_entities" in manifest.get("skipped_context_files", []):
            return {"valid": True, "skipped": True, "errors": []}
        if required:
            return {
                "valid": False,
                "errors": [
                    "translation manifest has no translation_entities context; "
                    "rerun translate after extract-entities-validate or use --skip-entities"
                ],
            }
        return {"valid": True, "skipped": True, "errors": []}

    entity_path = (output_dir / entity_relative).resolve()
    try:
        entity_path.relative_to(output_dir.resolve())
    except ValueError:
        return {"valid": False, "errors": ["translation entity context escapes output directory"]}
    if not entity_path.is_file():
        return {"valid": False, "errors": [f"entity context is missing: {entity_relative}"]}
    expected_hash = manifest.get("context_sha256", {}).get("translation_entities")
    actual_hash = hashlib.sha256(entity_path.read_bytes()).hexdigest()
    errors = []
    if expected_hash and expected_hash != actual_hash:
        errors.append("translation_entities.json changed after the translation task was prepared")
    try:
        from pdf2epub.entity_extractor import validate_entities

        entity_data = json.loads(entity_path.read_text(encoding="utf-8"))
        translation = config.get("translation", {}) or {}
        errors.extend(
            validate_entities(
                entity_data,
                config.get("title"),
                translation.get("source_language"),
                translation.get("target_language"),
            )
        )
    except (OSError, json.JSONDecodeError) as exc:
        errors.append(f"invalid translation_entities.json: {exc}")
    entity_manifest_path = output_dir / "entity_subagent_manifest.json"
    if entity_manifest_path.is_file():
        try:
            entity_manifest = json.loads(
                entity_manifest_path.read_text(encoding="utf-8")
            )
            source_dir, _ = _resolve_pdf_markdown_source(output_dir, config)
            expected_dir = relative_posix_path(source_dir, output_dir)
            manifest_source_dir = str(entity_manifest.get("source_dir") or "").replace("\\", "/")
            if manifest_source_dir != expected_dir:
                errors.append(
                    "entity glossary was extracted from a different source stage"
                )
            for name, expected in (entity_manifest.get("source_sha256", {}) or {}).items():
                source_path = source_dir / str(name)
                if not source_path.is_file():
                    errors.append(f"entity source file is missing: {name}")
                elif hashlib.sha256(source_path.read_bytes()).hexdigest() != expected:
                    errors.append(f"entity source file changed after extraction: {name}")
        except (OSError, json.JSONDecodeError, AttributeError, TypeError, ValueError) as exc:
            errors.append(f"invalid entity_subagent_manifest.json: {exc}")
    return {
        "valid": not errors,
        "errors": errors,
        "file": str(entity_path),
        "sha256": actual_hash,
    }


def _validate_pdf_markdown_task(args, task: str):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    context = load_book_context(args, f"{task}-validate")
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir
    if task == "polish":
        source_dir = output_dir / "ocr_markdown"
        target_dir = output_dir / "polished_markdown"
    else:
        source_dir, _ = _resolve_pdf_markdown_source(output_dir, config)
        target_dir = output_dir / "translated"
    selected_file = getattr(args, "file", None)
    if selected_file:
        selected_path = Path(str(selected_file))
        if (
            selected_path.name != str(selected_file)
            or selected_path.suffix.lower() != ".md"
            or not (source_dir / selected_path.name).is_file()
        ):
            logger.error(f"Unknown Markdown source file: {selected_file}")
            return 1
    report = validate_markdown_subagent(
        output_dir,
        task,
        source_dir,
        target_dir,
        structural_patterns=(
            r"^#{1,6}\s",
            r"!\[[^\]]*\]\([^)]+\)",
            *( () if task == "polish" else (r"\[\^[^\]]+\]",) ),
        ),
        file_roles=_load_pdf_file_roles(output_dir) if task == "translate" else None,
        tolerate_duplicate_headings=task == "polish",
        validate_footnote_normalization=task == "polish",
        fix_reference_headings=task == "translate" and bool(
            getattr(args, "fix_reference_heading", False)
        ),
        selected_files=[selected_file] if selected_file else None,
    )
    if task == "translate":
        # File mode is intentionally cheap: the Subagent can close the loop
        # on one unit without making a book-level TOC/context decision.
        if getattr(args, "file", None):
            _persist_file_validation_checkpoint(output_dir, report, task)
        else:
            entity_report = _validate_translation_entities(output_dir, config)
            report["entities"] = entity_report
            report["all_passed"] = report["all_passed"] and entity_report["valid"]
            if not entity_report["valid"]:
                for error in entity_report["errors"]:
                    logger.error(f"Entities: {error}")
            from pdf2epub.toc_translation_workflow import validate_toc_translation_subagent
            toc_report = validate_toc_translation_subagent(output_dir)
            report["toc"] = toc_report
            report["all_passed"] = report["all_passed"] and toc_report["valid"]
            if not toc_report["valid"]:
                for error in toc_report["errors"]:
                    logger.error(f"TOC: {error}")
            from pdf2epub.toc_translation_workflow import validate_toc_heading_bindings

            heading_report = validate_toc_heading_bindings(output_dir)
            report["toc_heading_bindings"] = heading_report
            report["all_passed"] = report["all_passed"] and heading_report["valid"]
            if not heading_report["valid"]:
                for error in heading_report["errors"]:
                    logger.error(f"TOC heading: {error}")
            from pdf2epub.glossary import validate_translation_context

            context_report = validate_translation_context(
                output_dir, f"{task}_subagent_manifest.json", config
            )
            report["translation_context"] = context_report
            report["all_passed"] = report["all_passed"] and context_report["valid"]
            if not context_report["valid"]:
                for error in context_report["errors"]:
                    logger.error(f"Translation context: {error}")
            _persist_full_validation_report(output_dir, task, report)
    elif getattr(args, "file", None):
        _persist_file_validation_checkpoint(output_dir, report, task)
    logger.info(
        f"{task} 校验: {report['completed']}/{report['total']} completed, "
        f"{len(report['invalid'])} invalid"
    )
    for name in report["missing"][:10]:
        logger.error(f"Missing: {name}")
    for item in report["invalid"][:10]:
        logger.error(f"Invalid: {item['file']}: {item['reason']}")
    if report.get("safety_blocked"):
        logger.error(
            f"Safety/refusal blocked units: {report['safety_blocked'][:10]}"
        )
    if report.get("bilingual_warnings"):
        logger.warning(
            f"Bilingual output warnings: {len(report['bilingual_warnings'])} "
            "(warning only; inspect the validation JSON before building)"
        )
    if report.get("structural_warnings"):
        logger.warning(
            f"Structural changes tolerated: {len(report['structural_warnings'])} "
            "duplicate heading/image artifact adjustment(s)"
        )
    if report.get("reference_heading_fixes"):
        logger.warning(
            f"Applied {len(report['reference_heading_fixes'])} high-confidence "
            "reference-heading repair(s); inspect the validation JSON"
        )
    if report["all_passed"]:
        logger.success(f"{task} Subagent output validated: {report['validated_dir']}")
        return 0
    return 1


def _persist_full_validation_report(output_dir: Path, task: str, report: dict) -> None:
    """Persist the final report after all book-level gates were evaluated."""
    atomic_write_text(
        Path(output_dir) / f"{task}_validation.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )


def _persist_file_validation_checkpoint(output_dir: Path, report: dict, task: str) -> None:
    """Merge a single-file result into the resumable checkpoint ledger."""
    path = Path(output_dir) / f"{task}_file_validation.json"
    existing: dict = {}
    if path.is_file():
        try:
            loaded = json.loads(path.read_text(encoding="utf-8"))
            if isinstance(loaded, dict):
                existing = loaded
        except (OSError, json.JSONDecodeError):
            existing = {}
    records = existing.get("files", {})
    if not isinstance(records, dict):
        records = {}
    for name in report.get("files_checked", []):
        records[name] = {
            "valid": name in report.get("valid_files", [])
            and not report.get("missing"),
            "source_sha256": report.get("source_sha256", {}).get(name),
            "target_sha256": report.get("target_sha256", {}).get(name),
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "invalid": [
                item for item in report.get("invalid", []) if item.get("file") == name
            ],
            "safety_blocked": name in report.get("safety_blocked", []),
        }
    atomic_write_text(
        path,
        json.dumps(
            {"task": task, "scope": "file-checkpoints", "files": records},
            ensure_ascii=False,
            indent=2,
        ),
    )



def _run_readiness_check(
    config_path: str,
    stage: str = "translate",
    skip_entities: bool = False,
) -> dict:
    """Run deterministic gates before translation or packaging."""
    from pdf2epub.refine.subagent_workflow import page_numbers, validate_toc_tree_data
    from pdf2epub.refine.main import _pages_fingerprint

    config = load_config(config_path)
    policy = PipelinePolicy.from_config(config)
    conversion_pipeline = policy.is_conversion
    book_title = config.get("title")
    output_dir = book_output_dir(book_title) if book_title else Path("output")
    checks: Dict[str, dict] = {}
    errors: list[str] = []

    def record(name: str, ready: bool, detail: str) -> None:
        checks[name] = {"ready": bool(ready), "detail": detail}
        if not ready:
            errors.append(f"{name}: {detail}")

    if not book_title:
        record("config", False, "title is missing")
        report = {"schema_version": 1, "stage": stage, "ready": False, "checks": checks, "errors": errors}
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "readiness.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return report

    pages_dir = output_dir / "pages"
    available = page_numbers(pages_dir) if pages_dir.is_dir() else []
    progress_path = pages_dir / "ocr_progress.json"
    failed_pages = []
    processed = []
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
            failed_pages = progress.get("failed_pages", []) or []
            processed = sorted(set(progress.get("pages_processed", []) or []))
        except (OSError, json.JSONDecodeError, AttributeError):
            failed_pages = ["invalid ocr_progress.json"]
    expected_pages = list(range(1, max(available) + 1)) if available else []
    page_ready = (
        bool(available)
        and available == expected_pages
        and processed == available
        and not failed_pages
    )
    record(
        "ocr_pages",
        page_ready,
        (
            f"{len(available)} contiguous OCR pages; {len(processed)} marked processed"
            if page_ready
            else "OCR pages are missing, non-contiguous, failed, or progress is unavailable"
        ),
    )

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
                and fingerprint.get("pages_sha256") == _pages_fingerprint(pages_dir)
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

    source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
    source_files = list(source_dir.glob("*.md")) if source_dir.is_dir() else []
    source_ready = bool(source_files)
    if source_stage == "polished":
        source_ready = source_ready and _polished_stage_is_current(
            output_dir, source_dir, output_dir / "ocr_markdown"
        )
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
                else "PDF translation requires a current validated polished source for "
                "both OCR-derived and native-text PDFs"
            )
            if source_stage != "polished" or not source_ready
            else f"current validated {source_stage} source is selected",
        )

    translation = config.get("translation", {}) or {}
    if not policy.requires_translation:
        record(
            "translation_entities",
            True,
            "not applicable for pipeline: epub_conversion",
        )
    elif policy.requires_entities and not skip_entities:
        entity_ready = _entity_context_is_current(
            output_dir,
            source_dir,
            policy.source_language,
            policy.target_language,
        )
        entity_validation_path = output_dir / "translation_entities_validation.json"
        if entity_validation_path.is_file():
            try:
                entity_report = json.loads(
                    entity_validation_path.read_text(encoding="utf-8")
                )
                entity_ready = entity_ready and entity_report.get("valid") is True
            except (OSError, json.JSONDecodeError, AttributeError):
                entity_ready = False
        else:
            entity_ready = False
        record(
            "translation_entities",
            entity_ready,
            "entity glossary matches the selected source stage"
            if entity_ready
            else "entity glossary is missing, unvalidated, or stale",
        )
    else:
        record("translation_entities", True, "explicitly skipped for this task")

    glossary_ready = True
    configured_glossaries = translation.get("glossaries", []) or []
    if not policy.requires_translation:
        glossary_detail = "not applicable for pipeline: epub_conversion"
    elif configured_glossaries:
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
            glossary_detail = f"{bundle.entries} entries in {len(bundle.context_files)} snapshot(s)"
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
        translated_ready = translated_dir.is_dir() and any(
            translated_dir.glob("*.md")
        )
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
                validation = json.loads(validation_path.read_text(encoding="utf-8"))
                recorded = validation.get("source_sha256", {})
                current = {
                    path.name: hashlib.sha256(path.read_bytes()).hexdigest()
                    for path in source_files
                }
                validation_ready = validation.get("all_passed") is True and recorded == current
                validation_detail = (
                    "full translation validation matches the current source snapshot"
                    if validation_ready
                    else "full translation validation is stale or failed"
                )
            except (OSError, json.JSONDecodeError, AttributeError, TypeError):
                validation_detail = "translate_validation.json is invalid"
        record("translation_validation", validation_ready, validation_detail)
    elif stage == "package":
        record(
            "translated_output",
            True,
            "not required for pipeline: epub_conversion",
        )
        record(
            "translation_validation",
            True,
            "not required for pipeline: epub_conversion",
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
    (output_dir / "readiness.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return report


def check_ready_command(args):
    """Expose the deterministic pre-flight readiness gate."""
    report = _run_readiness_check(
        args.config,
        getattr(args, "stage", "translate"),
        bool(getattr(args, "skip_entities", False)),
    )
    for error in report["errors"]:
        logger.error(error)
    if report["ready"]:
        logger.success(f"Pre-flight check passed for {report['stage']}")
        return 0
    logger.error(f"Pre-flight check failed for {report['stage']}")
    return 1



def translate_command(args):
    """Prepare a local Markdown hand-off for a translation Subagent."""
    if not PipelinePolicy.from_config(load_config(args.config)).requires_translation:
        logger.error(
            "pipeline: epub_conversion is a pure conversion workflow; "
            "translation hand-off is disabled."
        )
        return 1
    readiness = _run_readiness_check(
        args.config,
        "translate",
        bool(getattr(args, "skip_entities", False)),
    )
    if not readiness["ready"]:
        for error in readiness["errors"]:
            logger.error(error)
        logger.error("Pre-flight check failed; translation hand-off was not created")
        return 1
    return _prepare_pdf_markdown_task(args, "translate")
def translate_validate_command(args):
    if not PipelinePolicy.from_config(load_config(args.config)).requires_translation:
        logger.error(
            "pipeline: epub_conversion has no translated Markdown to validate."
        )
        return 1
    return _validate_pdf_markdown_task(args, "translate")

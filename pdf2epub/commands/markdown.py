"""PDF Markdown preparation, validation, and readiness commands.

This module owns the Markdown hand-off contract for PDF workflows. It performs
local preparation and validation only; translation remains delegated to the
workspace Subagent.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from loguru import logger

from pdf2epub.command_errors import log_command_error
from pdf2epub.entity_context import entity_context_status
from pdf2epub.commands.runtime import load_book_context
from pdf2epub.commands.markdown_readiness import run_readiness_check
from pdf2epub.commands.sources import (
    _polished_stage_is_current,
    _resolve_pdf_polish_source,
    _resolve_pdf_markdown_source,
)
from pdf2epub.commands.markdown_context import (
    _load_pdf_chapter_groups,
    _load_pdf_continuation_files,
    _load_pdf_continuation_toc_titles,
    _load_pdf_file_contexts,
    _load_pdf_toc_titles,
)
from pdf2epub.pdf_unit_roles import load_pdf_file_role_details, load_pdf_file_roles
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.utils.configuration import load_config
from pdf2epub.workflow_contracts import (
    markdown_validation_schema_version,
    atomic_write_json,
    atomic_write_text,
    posix_path,
    relative_posix_path,
    sha256_file,
)
from pdf2epub.validation_receipts import (
    build_input_snapshot,
    validation_receipt_is_current,
)


def _pdf_file_role_sources(output_dir: Path) -> dict[str, dict[str, Any]]:
    return {name: dict(record["role_origin"]) for name, record in load_pdf_file_role_details(output_dir).items()}


def _human_decision_id(args) -> str | None:
    value = str(getattr(args, "human_decision_id", "") or "").strip()
    return value or None


def polish_command(args):
    """Prepare a local Markdown hand-off for a polishing Subagent."""
    return _prepare_pdf_markdown_task(args, "polish")


def _print_handoff_preview(preview: Mapping[str, Any]) -> None:
    """Print a read-only prepare preview for an operator."""
    logger.info(
        f"[dry-run] {preview['task']}: "
        f"{len(preview['completed_files'])} completed, "
        f"{len(preview['pending_files'])} pending, "
        f"{len(preview.get('controlled_files') or {})} controlled isolated"
    )
    counts = preview.get("pending_reason_counts") or {}
    if counts:
        logger.info(
            "pending reasons: "
            + ", ".join(f"{key}={value}" for key, value in counts.items())
        )
    for index, batch in enumerate(preview.get("worker_batches") or [], 1):
        logger.info(f"worker batch {index}: " + ", ".join(batch))
    logger.info(
        f"concurrency: {preview['effective_concurrency']} "
        f"({preview['concurrency_reason']})"
    )
    logger.warning(
        "[dry-run] no manifest, prompt, worker hand-off, or dispatch lease was written"
    )


def _print_readiness_preview(readiness: Mapping[str, Any]) -> None:
    """Print the check-ready gates inside a ``--dry-run`` preview.

    The dry run is the single operator command for "what is pending and what
    is missing"; it reuses the same readiness service instead of asking the
    operator to run ``check-ready`` separately first.
    """
    logger.info(
        f"[dry-run] readiness stage {readiness['stage']}: "
        + ("ready" if readiness["ready"] else "NOT ready")
    )
    for name, check in readiness.get("checks", {}).items():
        status = "ready" if check.get("ready") else "blocked"
        logger.info(f"[dry-run] {name}: {status} - {check.get('detail', '')}")
    for error in readiness.get("errors", []):
        logger.error(f"[dry-run] readiness: {error}")


def _pdf_translation_footnote_hints(
    output_dir: Path, source_dir: Path, target_dir: Path,
) -> dict[str, list[dict[str, Any]]]:
    """Carry validated upstream move hints into existing translation targets.

    A changed polished target is expected after polish review. Freshness is
    instead anchored to the unchanged normalized source recorded by the polish
    manifest, then rebound to the actual polished and translated bytes.
    """
    try:
        polish_manifest = json.loads((output_dir / "polish_subagent_manifest.json").read_text(encoding="utf-8"))
        hints = polish_manifest.get("footnote_change_hints", {})
        normalized_hashes = polish_manifest.get("source_sha256", {})
        if not isinstance(hints, Mapping) or not isinstance(normalized_hashes, Mapping):
            return {}
        result = {}
        normalized_dir = output_dir / "footnote_normalized"
        for name, rows in hints.items():
            source_path, target_path = source_dir / str(name), target_dir / str(name)
            normalized_path = normalized_dir / str(name)
            if not source_path.is_file() or not target_path.is_file() or not normalized_path.is_file() or not isinstance(rows, list):
                continue
            normalized_hash = sha256_file(normalized_path)
            if normalized_hash != normalized_hashes.get(str(name)):
                continue
            source_hash = sha256_file(source_path)
            translation_hash = sha256_file(target_path)
            current = [
                {
                    **dict(row),
                    "polished_source_sha256": source_hash,
                    "existing_translation_sha256": translation_hash,
                }
                for row in rows
                if isinstance(row, Mapping) and row.get("new_source_sha256") == normalized_hash
            ]
            if current:
                result[str(name)] = current
        return result
    except (OSError, UnicodeError, ValueError, TypeError):
        return {}


def _prepare_task_source(
    args,
    task: str,
    *,
    config: Mapping[str, Any],
    policy: PipelinePolicy,
    output_dir: Path,
    book_title: str,
    source_language: str,
    target_language: str,
    footnote_mode: Optional[str],
    glossary_bundle: Any,
    dynamic_attestation: Any,
    config_path: Path,
) -> Optional[dict[str, Any]]:
    """Resolve source, rules and per-unit contexts for polish or translate."""
    context_files = {}
    skipped_context_files = []
    unit_context_files = {}
    heading_contexts = {}
    global_toc_outline = ""
    base_unit_context_files = {}
    preexisting_dynamic_targets = {}
    prompt_context_files = None
    chapter_groups = {}
    dynamic_snapshot_sha256 = None
    dynamic_entries = []
    footnote_change_hints = {}
    footnote_translation_change_hints = {}
    if task == "polish":
        if getattr(args, "resume", False) and not getattr(args, "dry_run", False):
            from pdf2epub.refine.footnote_versions import recover_interrupted_apply

            recover_interrupted_apply(output_dir)
        try:
            source_dir, _ = _resolve_pdf_polish_source(output_dir, config)
        except ValueError as exc:
            logger.error(str(exc))
            return None
        if (getattr(args, "resume", False) and not getattr(args, "dry_run", False)
                and source_dir.name == "footnote_normalized"):
            from pdf2epub.refine.footnote_versions import sync_polished_targets

            footnote_change_hints = sync_polished_targets(output_dir)
        target_dir = output_dir / "polished_markdown"
        rules = [
            "Repair source layout and line wrapping while preserving meaning and document structure.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations.",
            "Treat a single newline caused by PDF visual layout as a soft line wrap. In ordinary prose, join visual lines with spaces and use a blank line only for a real semantic paragraph boundary; never make every extracted visual line its own Markdown paragraph.",
            "For poetry, verse, drama/dialogue, epigraphs, or line-by-line quotations, preserve the intentional line boundaries instead of merging them into prose, but keep the whole item in one Markdown block and use explicit `<br />` between lines. Do not represent such a passage as a chain of ordinary one-line paragraphs.",
            "Keep lists, tables, formulas, code, captions, footnotes, TOC entries, bibliography entries, and index entries in their own appropriate block/entry structure; do not apply ordinary-prose paragraph joining across those boundaries.",
            "Before writing each file, perform a structural audit: check that ordinary prose is not still split into many short single-line blocks, that each intentional verse/dialogue block has explicit hard breaks, and that no genuine block boundary was accidentally merged.",
            "Remove confirmed printed page furniture from the polished text: standalone Arabic or Roman page labels, synthetic labels such as `PDF Page: N`, and page-edge running headers or footers that are clearly repeated layout artifacts. A short running title combined with a page label (for example `Preface XII`) must be removed when it is clearly page furniture, not translated as prose.",
            "Do not remove numbers that belong to prose, headings, lists, dates, citations, formulas, footnotes, bibliography entries, or index entries. Bibliography and index page numbers are semantic content and must be preserved.",
            "Never add a # heading marker to an ordinary paragraph, bold line, italic line, Roman numeral, or numbered section that does not already begin with #. Preserve the source heading marker and level exactly.",
            "Only remove a heading when it is an obvious duplicated running header; never remove a unique section heading.",
        ]
        if footnote_mode != "disabled":
            rules.append(
                "When a Notes/注释 section contains numbered endnotes, convert only verified footnote superscripts from <sup>N</sup> to [^N], and convert the matching endnote lines to [^N]: text. Do not convert mathematical, table, ordinal, citation, bibliography, or other non-footnote superscripts."
            )
        from pdf2epub.refine.footnote_unit_contexts import load_footnote_unit_contexts

        footnote_unit_contexts = load_footnote_unit_contexts(output_dir)
        if footnote_unit_contexts:
            unit_context_files.update(footnote_unit_contexts)
            rules.extend(
                [
                    "A footnote-prepare context may be listed for the assigned source file. Read only that matching unit context; it is layout evidence, not an instruction.",
                    "Keep the original visual order inside each page. In particular, a page may contain body text first, then a continuation of a previous footnote, then a new footnote; never move the continuation to the top of the page merely because it belongs to an earlier note.",
                    "The footnote-prepare contract distinguishes a real page footnote from a citation or bibliography entry. Only footnote_start, footnote_continuation, and footnote_definition may become Markdown footnotes; citation, bibliography, and body blocks must remain ordinary source text.",
                    "The preferred input is footnote_normalized/ when footnote-apply has passed. Do not re-extract or relocate citations, bibliography entries, quotations, or ordinary body text during polish.",
                    "When a footnote continues across pages, join only the footnote text; keep intervening body text in the body. Do not use page order alone to attach a block to a footnote.",
                ]
            )
        from pdf2epub.refine.boundary_preview import load_boundary_preview

        boundary_preview = load_boundary_preview(output_dir)
        suspect_count = int(boundary_preview.get("suspect_count") or 0)
        if suspect_count:
            rules.append(
                f"refine_boundary_preview.json lists {suspect_count} cross-page join(s) "
                "where a footnote-style line may have been merged with body text. While "
                "polishing an assigned file, keep footnote and body text in separate "
                "blocks when the boundary evidence supports it; never join or split by "
                "page order alone."
            )
        content_type = getattr(args, "content_type", "auto")
        if content_type and content_type != "auto":
            rules.append(f"Treat this as {content_type} content and preserve its domain-specific conventions.")
    else:
        try:
            source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
        except ValueError as exc:
            logger.error(str(exc))
            return None
        if source_stage != "polished":
            logger.error(
                "PDF translation requires a current validated polished source for "
                "both OCR-derived and native-text PDFs. Run polish and "
                "polish-validate before translate."
            )
            return None
        from pdf2epub.toc_translation_workflow import (
            build_global_toc_outline,
            build_toc_heading_contexts,
            validate_toc_translation_subagent,
        )
        from pdf2epub.batching_service import batching_config

        toc_report = validate_toc_translation_subagent(output_dir)
        if not toc_report["valid"]:
            logger.error(
                "TOC translation must be completed and validated before PDF "
                "translation: " + "; ".join(toc_report["errors"][:5])
            )
            return None
        heading_contexts = build_toc_heading_contexts(
            output_dir,
            source_dir=source_dir,
        )
        global_toc_outline = build_global_toc_outline(
            output_dir,
            token_budget=batching_config(config)["global_toc_tokens"],
        )
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
                return None
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
                return None
        rules = [
            "Translate prose to the target language; do not summarize, censor, or add commentary.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations exactly.",
            "Keep one output file for every source file and keep filenames unchanged.",
            "Keep each semantic prose paragraph as one Markdown block. Do not insert visual line wraps inside ordinary paragraphs; use a blank line only for a real paragraph boundary. Preserve intentional line structure in lists, quotations, tables, formulas, poetry, and explicit hard-break markup.",
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
                "Read the chapter-scoped terminology context before translating. It contains only terms matched in this chapter; apply its direct entries consistently to every assigned unit. Full entity and domain snapshots are audit-only; consult them only to resolve an explicit context gap or conflict, not as routine input. Do not modify any context file."
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

        chapter_groups = _load_pdf_chapter_groups(output_dir, source_dir)
        if policy.dynamic_terminology_enabled:
            from pdf2epub.dynamic_terminology import (
                build_dynamic_unit_contexts,
                load_current_snapshot,
                verify_session_attestation,
            )
            dynamic_snapshot_sha256, dynamic_entries = load_current_snapshot(
                output_dir / "dynamic_terminology", book_id=book_title,
                source_language=source_language, target_language=target_language,
            )
        else:
            dynamic_entries = []

        if policy.dynamic_terminology_enabled:
            verify_session_attestation(output_dir, dynamic_attestation)
        unit_context_files = build_unit_glossary_contexts(
            output_dir,
            source_dir,
            glossary_bundle.context_files if glossary_bundle else {},
            None if skip_entities else entity_path,
            source_language=source_language,
            target_language=target_language,
        )
        base_unit_context_files = dict(unit_context_files)
        if policy.dynamic_terminology_enabled:
            from pdf2epub.dynamic_terminology import preexisting_target_inventory

            preexisting_dynamic_targets = preexisting_target_inventory(target_dir)
            verify_session_attestation(output_dir, dynamic_attestation)
            unit_context_files = build_dynamic_unit_contexts(
                output_dir, source_dir, unit_context_files, dynamic_entries,
                source_language=source_language, target_language=target_language,
                chapter_groups=chapter_groups,
            )
            rules.append(
                "Dynamic terminology memory is frozen for this handoff. Read only the sparse context listed for your assigned file; never inspect the global snapshot or another unit. Optional unreviewed candidate suggestions belong only in the assignment-bound sidecar specified in your scoped manifest. Do not change TOC or metadata contracts."
            )
        # Native glossary snapshots are audit inputs; workers receive only
        # per-unit projections through unit_context_files.
        prompt_context_files = {}
    if task == "translate" and getattr(args, "resume", False):
        footnote_translation_change_hints = _pdf_translation_footnote_hints(
            output_dir, source_dir, target_dir,
        )
    return {
        "source_dir": source_dir,
        "target_dir": target_dir,
        "rules": rules,
        "unit_context_files": unit_context_files,
        "heading_contexts": heading_contexts,
        "global_toc_outline": global_toc_outline,
        "context_files": context_files,
        "skipped_context_files": skipped_context_files,
        "prompt_context_files": prompt_context_files,
        "base_unit_context_files": base_unit_context_files,
        "preexisting_dynamic_targets": preexisting_dynamic_targets,
        "dynamic_snapshot_sha256": dynamic_snapshot_sha256,
        "dynamic_entries": dynamic_entries,
        "chapter_groups": chapter_groups,
        "footnote_change_hints": footnote_change_hints,
        "footnote_translation_change_hints": footnote_translation_change_hints,
    }


def _prepare_pdf_markdown_task(args, task: str):
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.markdown_handoff_context import MarkdownHandoffRequest
    from pdf2epub.subagent_runtime import resolve_subagent_model

    if getattr(args, "dry_run", False) and getattr(args, "refresh_checkpoints", False):
        logger.error(
            "--dry-run and --refresh-checkpoints are mutually exclusive: "
            "dry-run must not write manifests, prompts, hand-offs, or checkpoints"
        )
        return 1
    if getattr(args, "retry_after_human_review", False) and not _human_decision_id(args):
        logger.error(
            "--retry-after-human-review is one explicit human adjudication action "
            "and requires --human-decision-id."
        )
        return 1
    if task == "translate" and getattr(args, "dry_run", False):
        readiness = run_readiness_check(
            args.config,
            "translate",
            bool(getattr(args, "skip_entities", False)),
        )
        _print_readiness_preview(readiness)

    dynamic_attestation = None
    if task == "translate":
        try:
            early_config = load_config(args.config)
            early_policy = PipelinePolicy.from_config(early_config)
            if early_policy.dynamic_terminology_enabled:
                from pdf2epub.dynamic_terminology import session_attestation, verify_session_attestation
                from pdf2epub.utils.paths import book_output_dir

                title = str(early_config.get("title") or "").strip()
                if not title:
                    raise ValueError("title is required before dynamic terminology can prepare translation")
                early_output = book_output_dir(title)
                dynamic_attestation = session_attestation(
                    early_output, "translate",
                    confirmed=bool(getattr(args, "confirm_no_active_sessions", False)),
                )
                verify_session_attestation(early_output, dynamic_attestation)
        except Exception as exc:
            log_command_error(f"Dynamic terminology pre-write gate blocked {task}", exc, args=args)
            return 1

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
    dynamic_snapshot_sha256 = None
    dynamic_entries = []
    if task == "translate" and policy.dynamic_terminology_enabled:
        from pdf2epub.dynamic_terminology import verify_session_attestation
        verify_session_attestation(output_dir, dynamic_attestation)
    glossary_bundle = None
    # Source-preparation state is initialized in _prepare_task_source().
    footnote_mode = None
    if task == "polish":
        from pdf2epub.refine.footnote_options import resolve_footnote_options

        try:
            footnote_mode = resolve_footnote_options(config).mode
        except ValueError as exc:
            logger.error(f"Invalid footnotes configuration: {exc}")
            return 1
        if getattr(args, "resume", False):
            from pdf2epub.polish_footnote_semantics import (
                escalated_reflow_files,
                reset_reflow_round,
            )

            escalated = escalated_reflow_files(output_dir)
            if escalated:
                if not getattr(args, "retry_after_human_review", False):
                    logger.error(
                        "Human review is required before another polish->footnote reflow round for: "
                        + ", ".join(sorted(escalated))
                        + ". The reflow is bounded; make one explicit decision, then rerun "
                        "polish --resume --retry-after-human-review only if another round is authorized."
                    )
                    return 1
                for name in sorted(escalated):
                    reset_reflow_round(
                        output_dir, name, decision_id=_human_decision_id(args) or "",
                    )
                logger.warning(
                    "Human decision accepted; reset the bounded polish->footnote reflow counter for: "
                    + ", ".join(sorted(escalated))
                )
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
            log_command_error("Could not load external glossary", exc, args=args)
            return 1
    prepared = _prepare_task_source(
        args, task,
        config=config,
        policy=policy,
        output_dir=output_dir,
        book_title=book_title,
        source_language=source_language,
        target_language=target_language,
        footnote_mode=footnote_mode,
        glossary_bundle=glossary_bundle,
        dynamic_attestation=dynamic_attestation,
        config_path=context.config_path,
    )
    if prepared is None:
        return 1
    source_dir = prepared["source_dir"]
    target_dir = prepared["target_dir"]
    rules = prepared["rules"]
    unit_context_files = prepared["unit_context_files"]
    heading_contexts = prepared["heading_contexts"]
    global_toc_outline = prepared["global_toc_outline"]
    context_files = prepared["context_files"]
    skipped_context_files = prepared["skipped_context_files"]
    prompt_context_files = prepared["prompt_context_files"]
    base_unit_context_files = prepared["base_unit_context_files"]
    preexisting_dynamic_targets = prepared["preexisting_dynamic_targets"]
    dynamic_snapshot_sha256 = prepared["dynamic_snapshot_sha256"]
    dynamic_entries = prepared["dynamic_entries"]
    chapter_groups = prepared["chapter_groups"]
    footnote_change_hints = prepared.get("footnote_change_hints", {})
    footnote_translation_change_hints = prepared.get("footnote_translation_change_hints", {})
    try:
        controlled_files = {}
        if task == "translate":
            if policy.dynamic_terminology_enabled:
                from pdf2epub.dynamic_terminology import verify_session_attestation
                verify_session_attestation(output_dir, dynamic_attestation)
            configured_special = policy.special_unit_modes or {}
            if configured_special:
                from pdf2epub.special_unit_translation import controlled_unit_is_supported
                roles = load_pdf_file_roles(output_dir)
                for filename, role in roles.items():
                    kind = str(role).lower()
                    if configured_special.get(kind) != "controlled" or kind not in {"bibliography", "index"}:
                        continue
                    candidate = source_dir / filename
                    unit_context = None
                    context_path = (unit_context_files or {}).get(filename)
                    if context_path and Path(context_path).is_file():
                        try:
                            unit_context = json.loads(Path(context_path).read_text(encoding="utf-8")).get("entries", [])
                        except (OSError, json.JSONDecodeError):
                            unit_context = None
                    if candidate.is_file() and controlled_unit_is_supported(
                        kind, candidate.read_bytes().decode("utf-8"),
                        heading_binding=(heading_contexts or {}).get(filename),
                        context_entries=unit_context,
                        source_language=source_language,
                    ):
                        controlled_files[filename] = kind
        handoff_request = MarkdownHandoffRequest(
            output_dir=output_dir,
            task=task,
            source_dir=source_dir,
            target_dir=target_dir,
            source_language=source_language,
            target_language=target_language,
            extra_rules=rules,
            config=config,
            resume=getattr(args, "resume", False),
            allow_human_review_retry=bool(
                getattr(args, "retry_after_human_review", False)
            ),
            human_decision_id=_human_decision_id(args),
            file_roles=load_pdf_file_roles(output_dir) if task in {"translate", "polish"} else None,
            file_role_sources=_pdf_file_role_sources(output_dir) if task in {"translate", "polish"} else None,
            context_files=context_files or None,
            skipped_context_files=skipped_context_files,
            prompt_context_files=prompt_context_files,
            file_contexts=_load_pdf_file_contexts(output_dir) if task == "translate" else None,
            heading_contexts=heading_contexts or None,
            global_toc_outline=global_toc_outline or None,
            unit_context_files=unit_context_files or None,
            chapter_groups=chapter_groups if task == "translate" else None,
            continuation_files=_load_pdf_continuation_files(output_dir, source_dir),
            protected_toc_titles=_load_pdf_toc_titles(output_dir) if task == "polish" else None,
            config_path=context.config_path,
            controlled_files=controlled_files or None,
            footnote_semantics_enabled=task == "polish" and footnote_mode != "disabled",
            refresh_checkpoints=bool(getattr(args, "refresh_checkpoints", False)),
            footnote_change_hints=footnote_change_hints or None,
            footnote_translation_change_hints=footnote_translation_change_hints or None,
        )
        if getattr(args, "dry_run", False):
            from pdf2epub.markdown_handoff import preview_markdown_handoff

            _print_handoff_preview(preview_markdown_handoff(handoff_request))
            return 0
        paths = prepare_markdown_subagent(handoff_request)
        from pdf2epub.subagent_runtime import write_worker_handoffs

        if task == "translate" and policy.dynamic_terminology_enabled:
            from pdf2epub.dynamic_terminology import verify_session_attestation
            verify_session_attestation(output_dir, dynamic_attestation)
        handoff_options = {}
        if task == "translate" and policy.dynamic_terminology_enabled:
            from pdf2epub.dynamic_terminology import dynamic_handoff_dir_name

            handoff_options = {
                "handoff_dir_name": dynamic_handoff_dir_name(
                    task, dynamic_snapshot_sha256, source_dir, unit_context_files or {},
                    manifest_path=paths["manifest"],
                ),
                "retire_previous": False,
            }
        handoffs = write_worker_handoffs(
            output_dir, paths["manifest"], paths["prompt"], **handoff_options,
        )
        if task == "translate" and policy.dynamic_terminology_enabled:
            from pdf2epub.dynamic_terminology import attach_candidate_sidecars

            attach_candidate_sidecars(
                output_dir, handoffs, task=task,
                chapter_by_file={name: chapter for chapter, files in chapter_groups.items() for name in files},
                source_language=source_language, target_language=target_language, book_id=book_title,
            )
            from pdf2epub.dynamic_terminology import manifest_dynamic_terminology_stamp

            manifest_data = json.loads(Path(paths["manifest"]).read_text(encoding="utf-8"))
            manifest_data["dynamic_terminology"] = manifest_dynamic_terminology_stamp(
                snapshot_sha256=dynamic_snapshot_sha256,
                operator_attestation_sha256=dynamic_attestation["evidence_sha256"],
                base_unit_context_files=base_unit_context_files,
                output_dir=output_dir,
                preexisting_target_inventory=preexisting_dynamic_targets,
            )
            atomic_write_json(Path(paths["manifest"]), manifest_data)
        if handoffs:
            paths["worker_handoffs"] = output_dir / Path(handoffs[0]["manifest"]).parent
    except Exception as exc:
        log_command_error(f"Could not prepare {task} task", exc, args=args)
        return 1
    logger.success(f"Wrote Subagent prompt: {paths['prompt']}")
    logger.info(f"Recommended Antigravity model: {resolve_subagent_model(config, task)}")
    if task in {"translate", "polish"}:
        handoff_dir = paths.get("worker_handoffs")
        logger.warning(
            "此步骤只生成交接文件，没有执行翻译；现在必须在 Antigravity 中打开工作区 "
            f"Subagent，并只按实际 scoped handoff 目录 {handoff_dir or '(no pending workers)'} 中的 assigned_files 执行。"
            + ("TOC 已由独立前置任务完成。" if task == "translate" else "")
        )
        for item in handoffs:
            if item.get("dispatch_wave") == (handoffs[0].get("dispatch_wave") if handoffs else None):
                logger.info(f"Worker prompt: {output_dir / item['prompt']}")
                logger.info(f"Worker manifest: {output_dir / item['manifest']}")
    logger.info(
        f"在 Antigravity 中让 Subagent 执行提示词，完成后运行 {task}-validate。"
    )
    if task == "translate" and controlled_files:
        logger.info(
            "Controlled units stay in the isolated worker handoffs with a "
            "controlled_bibliography/controlled_index role and dedicated rules: "
            + ", ".join(sorted(controlled_files))
            + ". Accept them with ordinary translate-validate (optionally --file); "
            "the special-unit result JSON is audit-only."
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
    actual_hash = sha256_file(entity_path)
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
    try:
        source_dir, _ = _resolve_pdf_markdown_source(output_dir, config)
        current, detail = entity_context_status(
            output_dir,
            source_dir,
            (config.get("translation", {}) or {}).get("source_language"),
            (config.get("translation", {}) or {}).get("target_language"),
        )
        if not current:
            errors.append(detail)
    except (OSError, ValueError, TypeError) as exc:
        errors.append(f"could not verify entity source snapshot: {exc}")
    return {
        "valid": not errors,
        "errors": errors,
        "file": str(entity_path),
        "sha256": actual_hash,
    }


def _validate_pdf_markdown_task(args, task: str):
    check_only = bool(getattr(args, "check_only", False))
    if check_only:
        return _validate_pdf_markdown_task_impl(args, task)

    context = load_book_context(args, f"{task}-validate", init_logging=True)
    if context is None:
        return 1
    from pdf2epub.state_lock import state_lock

    report_path = context.output_dir / f"{task}_validation.json"
    with state_lock(report_path):
        return _validate_pdf_markdown_task_impl(args, task, context=context)


def _validate_pdf_markdown_task_impl(args, task: str, *, context=None):
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

    check_only = bool(getattr(args, "check_only", False))
    if check_only and not getattr(args, "file", None):
        logger.error("--check-only requires --file")
        return 1
    if getattr(args, "allow_review_warnings", False) and not _human_decision_id(args):
        logger.error(
            "--allow-review-warnings is one explicit human adjudication action and "
            "requires --human-decision-id."
        )
        return 1
    context = context or load_book_context(args, f"{task}-validate", init_logging=not check_only)
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir
    policy = PipelinePolicy.from_config(config)
    target_language = (
        getattr(args, "target_language", None)
        or policy.target_language
        or "Chinese"
    )
    footnote_validation_enabled = task == "polish"
    if task == "polish":
        from pdf2epub.refine.footnote_options import resolve_footnote_options

        try:
            footnote_validation_enabled = (
                resolve_footnote_options(config).mode != "disabled"
            )
        except ValueError as exc:
            logger.error(f"Invalid footnotes configuration: {exc}")
            return 1
    if task == "polish":
        try:
            source_dir, _ = _resolve_pdf_polish_source(output_dir, config)
        except ValueError as exc:
            logger.error(str(exc))
            return 1
        target_dir = output_dir / "polished_markdown"
    else:
        try:
            source_dir, _ = _resolve_pdf_markdown_source(output_dir, config)
        except ValueError as exc:
            logger.error(str(exc))
            return 1
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
    if task == "translate" and policy.special_unit_modes:
        logger.info(
            "Controlled bibliography/index units stay in the isolated worker "
            "channel; their JSON result is an audit attachment only. Normal "
            "translate-validate (including --file) is the acceptance gate."
        )
    report = validate_markdown_subagent(
        output_dir,
        task,
        source_dir,
        target_dir,
        structural_patterns=(
            r"^#{1,6}\s",
            r"!\[[^\]]*\]\([^)]+\)",
            *( (r"^\d+\\\.\s",) if task == "translate" else () ),
            *( () if task == "polish" else (r"\[\^[^\]]+\]",) ),
        ),
        create_validated_copy=not check_only,
        file_roles=load_pdf_file_roles(output_dir) if task in {"translate", "polish"} else None,
        file_role_sources=_pdf_file_role_sources(output_dir) if task in {"translate", "polish"} else None,
        validate_footnote_normalization=footnote_validation_enabled,
        fix_reference_headings=task == "translate" and bool(
            getattr(args, "fix_reference_heading", False)
        ),
        selected_files=[selected_file] if selected_file else None,
        target_language=target_language if task == "translate" else None,
        protected_toc_titles=(
            _load_pdf_toc_titles(output_dir) if task == "polish" else None
        ),
        continuation_toc_titles=(
            _load_pdf_continuation_toc_titles(output_dir)
            if task == "polish"
            else None
        ),
        allow_review_warnings=bool(
            getattr(args, "allow_review_warnings", False)
        ),
        human_decision_id=_human_decision_id(args),
        dynamic_terminology_config=config if task == "translate" else None,
        require_footnote_semantic_review=(task == "polish" and footnote_validation_enabled),
        pre_persist_hook=(
            lambda candidate_report: _apply_local_toc_heading_binding(
                output_dir,
                candidate_report,
                str(selected_file),
                target_dir=target_dir,
            )
            if task == "translate" and selected_file
            else None
        ),
    )
    selected_scope = bool(getattr(args, "file", None))
    book_outputs_complete = not selected_scope and not report.get("missing")
    force_full_final = bool(getattr(args, "final", False)) and not selected_scope
    report["completion"] = {
        "ready_for_book_level_gates": bool(book_outputs_complete),
        "completed_files": int(report.get("completed", 0) or 0),
        "total_files": int(report.get("total", 0) or 0),
        "pending_files": list(report.get("missing", []) or []),
        "book_level_gates_forced": force_full_final,
    }
    if task == "translate":
        # File mode stays scoped: the Subagent can close the loop on one unit
        # without making a book-level TOC/context decision, while still
        # checking that unit's own visible TOC heading bindings.
        if getattr(args, "file", None):
            if not check_only:
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
            heading_report = _aggregate_toc_heading_bindings(
                output_dir,
                report,
                force_full_final=force_full_final,
            )
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
            _persist_full_validation_report(
                output_dir,
                task,
                report,
                config_path=context.config_path,
            )
    elif getattr(args, "file", None):
        if not check_only:
            _persist_file_validation_checkpoint(output_dir, report, task)
    else:
        # ``validate_markdown_subagent`` writes the full polish report itself.
        # Rewrite it here with the package-input attestation after all local
        # gates have completed.
        _persist_full_validation_report(
            output_dir,
            task,
            report,
            config_path=context.config_path,
        )
    logger.info(
        f"{task} 校验: {report['completed']}/{report['total']} completed, "
        f"{len(report['invalid'])} invalid"
    )
    if report.get("missing"):
        pending = report["missing"]
        logger.info(
            f"{task.capitalize()} progress: {report.get('completed', 0)}/{report.get('total', 0)} "
            f"files present; {len(pending)} pending. "
            + (
                "Book-level TOC binding is deferred."
                if task == "translate"
                else "Book-level validation is deferred."
            )
        )
        for name in pending[:10]:
            logger.info(f"Pending: {name}")
    for item in report["invalid"][:10]:
        logger.error(f"Invalid: {item['file']}: {item['reason']}")
    quarantined = report.get("quarantined_files")
    if isinstance(quarantined, dict) and quarantined:
        items = list(quarantined.items())
        for name, rel_path in items[:10]:
            logger.info(
                f"Quarantined invalid output (preserved for editing): {name} -> {rel_path}"
            )
        if len(items) > 10:
            logger.info(
                f"... and {len(items) - 10} more quarantined invalid output(s)"
            )
    if report.get("safety_blocked"):
        logger.error(
            f"Safety/refusal blocked units: {report['safety_blocked'][:10]}"
        )
    if report.get("target_language_blocked"):
        logger.error(
            "Target-language audit blocked units: "
            f"{report['target_language_blocked'][:10]}"
        )
    if report.get("bilingual_warnings"):
        logger.warning(
            f"Bilingual output warnings: {len(report['bilingual_warnings'])} "
            "(these require Subagent review for ordinary translation units)"
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
    if report.get("polish_page_furniture_warnings"):
        logger.warning(
            "Polish residual page-furniture candidates: "
            f"{len(report['polish_page_furniture_warnings'])}; "
            "these require Subagent or human review before continuing"
        )
    polish_structure_audits = report.get("polish_structure_audits", {})
    fragmented_units = [
        name
        for name, audit in polish_structure_audits.items()
        if isinstance(audit, dict) and audit.get("status") == "review"
    ]
    if fragmented_units:
        logger.warning(
            "Polish paragraph-fragmentation review required for: "
            f"{fragmented_units[:10]}"
        )
    first_pass_review_retries = [
        item
        for item in report.get("retry_required", [])
        if item.get("kind") in {
            "bilingual_output",
            "polish_page_furniture",
            "polish_paragraph_fragmentation",
            "polish_unique_toc_label_removed",
        }
    ]
    if first_pass_review_retries and not report.get(
        "review_warnings_acknowledged"
    ):
        logger.error(
            "Subagent rework required for review signals: these files were not "
            "staged in validated/. Reassign them with --resume; if the signal "
            "persists, the next result will require human review."
        )
        for item in first_pass_review_retries[:20]:
            logger.error(
                f"Subagent retry: {item.get('file')}: "
                f"{item.get('reason', item.get('kind', 'warning'))}"
            )
    if report.get("human_review_required") and not report.get(
        "review_warnings_acknowledged"
    ):
        logger.error(
            "Human review required: the persisted review-escalation receipt shows "
            "these signals already survived a Subagent retry. Stop automatic retries "
            "and make one explicit decision; then rerun with --human-decision-id and "
            "either --retry-after-human-review or --allow-review-warnings."
        )
        for item in report["human_review_required"][:20]:
            logger.error(
                f"Human decision: {item.get('file')}: "
                f"{item.get('reason', item.get('kind', 'warning'))}"
            )
    if report.get("review_warnings_acknowledged"):
        logger.warning(
            "Review warnings were explicitly acknowledged by human decision "
            f"'{report.get('human_decision_id')}'; continuing and staging the "
            "reviewed files."
        )
    if report["all_passed"]:
        if check_only:
            logger.success(
                f"{task} read-only check passed for {selected_file}; "
                "no checkpoint or validated copy was written"
            )
        else:
            logger.success(f"{task} Subagent output validated: {report['validated_dir']}")
        return 0
    return 1


def _persist_full_validation_report(
    output_dir: Path,
    task: str,
    report: dict,
    *,
    config_path: Path | None = None,
) -> None:
    """Persist the final report after all book-level gates were evaluated."""
    if task in {"polish", "translate"} and config_path is not None:
        report["build_inputs"] = build_input_snapshot(
            output_dir,
            config_path,
            translated=task == "translate",
        )
    atomic_write_json(Path(output_dir) / f"{task}_validation.json", report)
    _sync_subagent_manifest_progress(output_dir, task, report)


def _sync_subagent_manifest_progress(
    output_dir: Path,
    task: str,
    report: Mapping[str, Any],
) -> None:
    """Reconcile parent hand-off progress after a full validation run."""
    manifest_path = Path(output_dir) / f"{task}_subagent_manifest.json"
    if not manifest_path.is_file() or report.get("scope") != "full":
        return
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return
    if not isinstance(manifest, dict):
        return
    files = [str(name) for name in manifest.get("files", [])]
    valid = {str(name) for name in report.get("valid_files", [])}
    manifest["completed_files"] = [name for name in files if name in valid]
    manifest["pending_files"] = [name for name in files if name not in valid]
    pending = set(manifest["pending_files"])
    for key in ("batch_queue", "worker_queue", "worker_handoffs", "batch_handoffs"):
        items = manifest.get(key)
        if not isinstance(items, list):
            continue
        for item in items:
            if not isinstance(item, dict):
                continue
            assigned = {str(name) for name in item.get("files", [])}
            if not assigned:
                continue
            if assigned <= valid:
                item["status"] = "completed"
            elif assigned & pending:
                item["status"] = "pending"
    atomic_write_json(manifest_path, manifest)


def _aggregate_toc_heading_bindings(
    output_dir: Path,
    report: Mapping[str, Any],
    *,
    force_full_final: bool = False,
) -> dict:
    """Aggregate the scoped TOC heading gate without deferring it.

    Each single-file ``translate-validate`` already checks that file's own
    visible TOC headings.  The book-level run must summarise those same gates
    instead of raising every missing heading only at the moment the last file
    lands: it checks every currently validated file and reports the others as
    pending, not as binding errors.
    """
    from pdf2epub.toc_translation_workflow import validate_toc_heading_bindings

    if force_full_final:
        return validate_toc_heading_bindings(output_dir)
    valid_files = [
        str(name) for name in (report.get("valid_files") or []) if str(name).strip()
    ]
    if not valid_files:
        return {
            "valid": True,
            "skipped": True,
            "reason": "no validated translated files yet",
            "pending_files": list(report.get("missing", []) or []),
        }
    heading_report = validate_toc_heading_bindings(
        output_dir, only_files=valid_files
    )
    pending = [
        str(name)
        for name in (report.get("missing") or [])
        if str(name) not in set(valid_files)
    ]
    if pending:
        heading_report["pending_files"] = pending
    return heading_report


def _apply_local_toc_heading_binding(
    output_dir: Path,
    report: dict,
    selected_file: str,
    *,
    target_dir: Path | None = None,
) -> dict:
    """Apply the scoped TOC heading gate to a single-file validation report."""
    from pdf2epub.toc_translation_workflow import validate_toc_heading_bindings

    heading_report = validate_toc_heading_bindings(
        output_dir, only_files=[selected_file], target_dir=target_dir
    )
    report["toc_heading_bindings_local"] = heading_report
    if heading_report.get("valid", True):
        return heading_report
    report["all_passed"] = False
    valid_files = report.get("valid_files")
    if isinstance(valid_files, list) and selected_file in valid_files:
        valid_files.remove(selected_file)
    reason = "; ".join(str(error) for error in heading_report.get("errors", [])) or (
        "local TOC heading binding failed"
    )
    retry_required = report.setdefault("retry_required", [])
    retry_required.append(
        {
            "file": selected_file,
            "kind": "toc_heading_binding",
            "action": "retry_subagent",
            "reason": reason,
        }
    )
    report["retry_required_files"] = sorted(
        {
            str(item["file"])
            for item in retry_required
            if isinstance(item, dict) and item.get("file")
        }
    )
    logger.error(f"TOC heading binding failed for {selected_file}: {reason}")
    return heading_report


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
        manifest_path = Path(output_dir) / f"{task}_subagent_manifest.json"
        try:
            manifest_value = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            manifest_value = {}
        dynamic_context_hash = (
            (manifest_value.get("unit_context_sha256", {}) or {}).get(name)
            if (manifest_value.get("dynamic_terminology", {}) or {}).get("enabled") is True
            else None
        )
        records[name] = {
            "schema_version": markdown_validation_schema_version(task),
            "valid": name in report.get("valid_files", [])
            and not report.get("missing"),
            "source_sha256": report.get("source_sha256", {}).get(name),
            "target_sha256": report.get("target_sha256", {}).get(name),
            "file_role": report.get("file_roles", {}).get(name),
            "file_role_source": report.get("file_role_sources", {}).get(name),
            "resume_context": report.get("resume_contexts", {}).get(name),
            "polish_footnote_semantic_review": report.get("polish_footnote_semantic_reviews", {}).get(name),
            "polish_footnote_semantic_review_sha256": (
                report.get("polish_footnote_semantic_reviews", {}).get(name, {}).get("sidecar_sha256")
                if isinstance(report.get("polish_footnote_semantic_reviews", {}).get(name), dict)
                else None
            ),
            **({"dynamic_unit_context_sha256": dynamic_context_hash} if dynamic_context_hash else {}),
            "validated_at": datetime.now(timezone.utc).isoformat(),
            "invalid": [
                item for item in report.get("invalid", []) if item.get("file") == name
            ],
            "safety_blocked": name in report.get("safety_blocked", []),
            "target_language_blocked": name in report.get(
                "target_language_blocked", []
            ),
            "target_language_audit": report.get(
                "target_language_audits", {}
            ).get(name),
            "review_required": [
                item
                for item in report.get("review_required", [])
                if item.get("file") == name
            ],
            "retry_required": [
                item
                for item in report.get("retry_required", [])
                if item.get("file") == name
            ],
            "human_review_required": [
                item
                for item in report.get("human_review_required", [])
                if item.get("file") == name
            ],
            "review_warnings_acknowledged": bool(
                report.get("review_warnings_acknowledged")
                and any(
                    item.get("file") == name
                    for item in report.get("review_required", [])
                )
            ),
        }
    atomic_write_json(
        path,
        {
            "schema_version": markdown_validation_schema_version(task),
            "task": task,
            "scope": "file-checkpoints",
            "files": records,
        },
    )
    if task in {"translate", "polish"}:
        # The translate file path applies its local TOC binding after the
        # validator's persistence hook. Reconcile again here so that local
        # failures cannot leave an older full receipt current.
        from pdf2epub.markdown_validation_persistence import _sync_partial_into_full_report

        _sync_partial_into_full_report(Path(output_dir), report)



def check_ready_command(args):
    """Expose the deterministic pre-flight readiness gate."""
    report = run_readiness_check(
        args.config,
        getattr(args, "stage", "translate"),
        bool(getattr(args, "skip_entities", False)),
    )
    for error in report["errors"]:
        logger.error(error)
    for warning in report.get("warnings", []):
        if isinstance(warning, dict) and warning.get("message"):
            logger.warning(str(warning["message"]))
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
    if getattr(args, "dry_run", False):
        # The dry-run preview embeds check-ready gates itself and never writes
        # a manifest, prompt, or hand-off; a missing gate must not hide the
        # preview the operator asked for.
        return _prepare_pdf_markdown_task(args, "translate")
    readiness = run_readiness_check(
        args.config,
        "translate",
        bool(getattr(args, "skip_entities", False)),
    )
    if not readiness["ready"]:
        for warning in readiness.get("warnings", []):
            if isinstance(warning, dict) and warning.get("message"):
                logger.warning(str(warning["message"]))
        for error in readiness["errors"]:
            logger.error(error)
        logger.error("Pre-flight check failed; translation hand-off was not created")
        return 1
    for warning in readiness.get("warnings", []):
        if isinstance(warning, dict) and warning.get("message"):
            logger.warning(str(warning["message"]))
    return _prepare_pdf_markdown_task(args, "translate")
def translate_validate_command(args):
    if not PipelinePolicy.from_config(load_config(args.config)).requires_translation:
        logger.error(
            "pipeline: epub_conversion has no translated Markdown to validate."
        )
        return 1
    return _validate_pdf_markdown_task(args, "translate")


def special_unit_prepare_command(args):
    """Deprecated forwarding shim for the ordinary translate channel."""
    logger.warning(
        "special-unit-prepare is deprecated: controlled bibliography/index units "
        "now use the ordinary translate worker channel. Forwarding to `translate`."
    )
    return translate_command(args)


def special_unit_apply_command(args):
    """Deprecated forwarding shim for ordinary translate-validate --file."""
    logger.warning(
        "special-unit-apply is deprecated: acceptance is normal translate-validate. "
        f"Forwarding to `translate-validate --file {args.file}`."
    )
    return translate_validate_command(args)

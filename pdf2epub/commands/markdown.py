"""PDF Markdown preparation, validation, and readiness commands.

This module owns the Markdown hand-off contract for PDF workflows. It performs
local preparation and validation only; translation remains delegated to the
workspace Subagent.
"""

import hashlib
import json
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Mapping

from loguru import logger

from pdf2epub.commands.entities import _entity_context_is_current
from pdf2epub.commands.runtime import load_book_context
from pdf2epub.commands.sources import (
    _polished_stage_is_current,
    _resolve_pdf_polish_source,
    _resolve_pdf_markdown_source,
)
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.ocr_correction import (
    ocr_correction_is_current,
    select_refinement_pages,
)
from pdf2epub.ocr_progress import assess_progress
from pdf2epub.utils.common import book_output_dir, load_config
from pdf2epub.workflow_contracts import (
    MARKDOWN_VALIDATION_SCHEMA_VERSION,
    atomic_write_text,
    relative_posix_path,
)
from pdf2epub.validation_receipts import (
    build_input_snapshot,
    validation_receipt_is_current,
)


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


def _load_pdf_toc_titles(output_dir: Path) -> list[str]:
    """Return source-language TOC labels used to protect polish headings."""
    toc_path = Path(output_dir) / "toc_tree.json"
    if not toc_path.is_file():
        return []
    try:
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return []
    titles: list[str] = []

    def visit(nodes: Any) -> None:
        if not isinstance(nodes, list):
            return
        for node in nodes:
            if not isinstance(node, dict):
                continue
            title = str(node.get("title") or "").strip()
            if title and title not in titles:
                titles.append(title)
            visit(node.get("children", []))

    visit(toc.get("chapters", []))
    return titles


def _load_pdf_continuation_files(output_dir: Path, source_dir: Path) -> dict:
    """Read continuation metadata from refinement rather than filenames."""
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if not progress_path.is_file():
        return {}
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}

    available = {path.name for path in Path(source_dir).glob("*.md")}
    metadata: dict[str, dict[str, Any]] = {}
    for unit in progress.get("units", []) or []:
        if not isinstance(unit, dict):
            continue
        names = [str(name) for name in (unit.get("part_files") or []) if name]
        if len(names) < 2:
            continue
        part_count = len(names)
        for index, name in enumerate(names, 1):
            if index > 1 and name in available:
                metadata[name] = {
                    "is_continuation": True,
                    "part_number": index,
                    "part_count": part_count,
                }
    return metadata


def _load_pdf_continuation_toc_titles(output_dir: Path) -> dict[str, list[str]]:
    """Map split parts to the top-level ancestor labels they may repeat.

    A split Markdown unit can begin with a repeated top-level part label that
    was present at the top of the source page.  That label is not the unit's
    own heading, so polish validation may accept its removal when it is the
    unique opening line of a continuation part.  Keep this exception narrow:
    only the actual top-level ancestor from the TOC is eligible, never every
    protected title in the book.
    """
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    toc_path = Path(output_dir) / "toc_tree.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        toc = json.loads(toc_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    if not isinstance(progress, Mapping) or not isinstance(toc, Mapping):
        return {}

    chapters = toc.get("chapters", [])

    def top_level_title(index_path: Any) -> str:
        if not isinstance(index_path, list) or not index_path:
            return ""
        nodes = chapters
        first_title = ""
        try:
            for depth, raw_index in enumerate(index_path):
                if not isinstance(nodes, list):
                    return ""
                index = int(raw_index) - 1
                node = nodes[index]
                if not isinstance(node, Mapping):
                    return ""
                if depth == 0:
                    first_title = str(node.get("title") or "").strip()
                nodes = node.get("children", [])
        except (IndexError, TypeError, ValueError):
            return ""
        return first_title

    result: dict[str, list[str]] = {}
    for unit in progress.get("units", []) or []:
        if not isinstance(unit, Mapping):
            continue
        names = [
            str(name)
            for name in (unit.get("part_files") or [])
            if str(name).strip()
        ]
        if len(names) < 2:
            continue
        ancestor = top_level_title(unit.get("index_path"))
        own_title = str(unit.get("title") or "").strip()
        if not ancestor or ancestor.casefold() == own_title.casefold():
            continue
        for name in names[1:]:
            result[name] = [ancestor]
    return result


def _load_pdf_chapter_groups(output_dir: Path, source_dir: Path) -> dict:
    """Group PDF units by top-level chapter, including split part files."""
    from pdf2epub.chapter_identity import ChapterIdentity

    source_names = [path.name for path in sorted(Path(source_dir).glob("*.md"))]
    groups = {}

    def chapter_id(name: str) -> str:
        identity = ChapterIdentity.parse(name)
        if identity is None:
            return f"unit:{name}"
        if identity.number:
            return f"{identity.prefix}_{identity.index_path[0]}"
        return identity.prefix

    # tree_progress is the authoritative source for split membership.  The
    # filename fallback below keeps old/refined outputs usable when the
    # progress file predates part_files.
    progress_path = Path(output_dir) / "ocr_markdown" / "tree_progress.json"
    if progress_path.is_file():
        try:
            progress = json.loads(progress_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError):
            progress = {}
        for unit in progress.get("units", []) or []:
            if not isinstance(unit, dict):
                continue
            names = unit.get("part_files") or [unit.get("file")]
            names = [str(name) for name in names if name]
            if not names:
                continue
            group = groups.setdefault(chapter_id(names[0]), [])
            for name in names:
                if name in source_names and name not in group:
                    group.append(name)

    for name in source_names:
        group = groups.setdefault(chapter_id(name), [])
        if name not in group:
            group.append(name)
    return groups


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
    global_toc_outline = ""
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
        source_dir, _ = _resolve_pdf_polish_source(output_dir)
        target_dir = output_dir / "polished_markdown"
        rules = [
            "Repair source layout and line wrapping while preserving meaning and document structure.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations.",
            "Remove confirmed printed page furniture from the polished text: standalone Arabic or Roman page labels, synthetic labels such as `PDF Page: N`, and page-edge running headers or footers that are clearly repeated layout artifacts. A short running title combined with a page label (for example `Preface XII`) must be removed when it is clearly page furniture, not translated as prose.",
            "Do not remove numbers that belong to prose, headings, lists, dates, citations, formulas, footnotes, bibliography entries, or index entries. Bibliography and index page numbers are semantic content and must be preserved.",
            "Never add a # heading marker to an ordinary paragraph, bold line, italic line, Roman numeral, or numbered section that does not already begin with #. Preserve the source heading marker and level exactly.",
            "Only remove a heading when it is an obvious duplicated running header; never remove a unique section heading.",
            "When a Notes/注释 section contains numbered endnotes, convert only verified footnote superscripts from <sup>N</sup> to [^N], and convert the matching endnote lines to [^N]: text. Do not convert mathematical, table, ordinal, citation, bibliography, or other non-footnote superscripts.",
        ]
        from pdf2epub.refine.footnote_prepare import load_footnote_unit_contexts

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
            build_global_toc_outline,
            build_toc_heading_contexts,
            validate_toc_translation_subagent,
        )
        from pdf2epub.subagent_runtime import _batching_config

        toc_report = validate_toc_translation_subagent(output_dir)
        if not toc_report["valid"]:
            logger.error(
                "TOC translation must be completed and validated before PDF "
                "translation: " + "; ".join(toc_report["errors"][:5])
            )
            return 1
        heading_contexts = build_toc_heading_contexts(
            output_dir,
            source_dir=source_dir,
        )
        global_toc_outline = build_global_toc_outline(
            output_dir,
            token_budget=_batching_config(config)["global_toc_tokens"],
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

        unit_context_files = build_unit_glossary_contexts(
            output_dir,
            source_dir,
            glossary_bundle.context_files if glossary_bundle else {},
            None if skip_entities else entity_path,
            source_language=source_language,
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
            allow_human_review_retry=bool(
                getattr(args, "retry_after_human_review", False)
            ),
            file_roles=_load_pdf_file_roles(output_dir) if task in {"translate", "polish"} else None,
            context_files=context_files or None,
            skipped_context_files=skipped_context_files,
            prompt_context_files=prompt_context_files,
            file_contexts=(
                _load_pdf_file_contexts(output_dir) if task == "translate" else None
            ),
            heading_contexts=heading_contexts or None,
            global_toc_outline=global_toc_outline or None,
            unit_context_files=unit_context_files or None,
            chapter_groups=(
                _load_pdf_chapter_groups(output_dir, source_dir)
                if task == "translate"
                else None
            ),
            continuation_files=_load_pdf_continuation_files(output_dir, source_dir),
            protected_toc_titles=(
                _load_pdf_toc_titles(output_dir) if task == "polish" else None
            ),
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
    policy = PipelinePolicy.from_config(config)
    target_language = (
        getattr(args, "target_language", None)
        or policy.target_language
        or "Chinese"
    )
    if task == "polish":
        source_dir, _ = _resolve_pdf_polish_source(output_dir)
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
        file_roles=_load_pdf_file_roles(output_dir) if task in {"translate", "polish"} else None,
        tolerate_duplicate_headings=task == "polish",
        validate_footnote_normalization=task == "polish",
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
            _persist_full_validation_report(
                output_dir,
                task,
                report,
                config_path=context.config_path,
            )
    elif getattr(args, "file", None):
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
    for name in report["missing"][:10]:
        logger.error(f"Missing: {name}")
    for item in report["invalid"][:10]:
        logger.error(f"Invalid: {item['file']}: {item['reason']}")
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
    first_pass_review_retries = [
        item
        for item in report.get("retry_required", [])
        if item.get("kind") in {
            "bilingual_output",
            "polish_page_furniture",
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
            "Human review required: the same review signal persisted after a "
            "Subagent retry. Stop automatic retries and ask for a decision; "
            "do not use --allow-review-warnings without that decision."
        )
        for item in report["human_review_required"][:20]:
            logger.error(
                f"Human decision: {item.get('file')}: "
                f"{item.get('reason', item.get('kind', 'warning'))}"
            )
    if report.get("review_warnings_acknowledged"):
        logger.warning(
            "Review warnings were explicitly acknowledged; continuing and staging "
            "the reviewed files."
        )
    if report["all_passed"]:
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
    atomic_write_text(
        Path(output_dir) / f"{task}_validation.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )
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
    atomic_write_text(
        manifest_path,
        json.dumps(manifest, ensure_ascii=False, indent=2),
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
            "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
            "valid": name in report.get("valid_files", [])
            and not report.get("missing"),
            "source_sha256": report.get("source_sha256", {}).get(name),
            "target_sha256": report.get("target_sha256", {}).get(name),
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
    atomic_write_text(
        path,
        json.dumps(
            {
                "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
                "task": task,
                "scope": "file-checkpoints",
                "files": records,
            },
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
        report = {"schema_version": 1, "stage": stage, "ready": False, "checks": checks, "errors": errors}
        output_dir.mkdir(parents=True, exist_ok=True)
        (output_dir / "readiness.json").write_text(
            json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
        )
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

    probe = {}
    try:
        value = json.loads(probe_path.read_text(encoding="utf-8"))
        if isinstance(value, dict):
            probe = value
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    native_text = (
        probe.get("classification") == "native_text"
        and probe.get("recommendation") == "use_text_layer"
    )
    correction_ready = native_text or not policy.requires_ocr_correction or ocr_correction_is_current(
        output_dir, config
    )
    record(
        "ocr_correction",
        correction_ready,
        "not applicable for a high-confidence native-text PDF"
        if native_text
        else (
            "not required because secondary OCR consensus is disabled"
            if not policy.requires_ocr_correction
            else (
                "validated visual OCR correction matches the current page set"
                if correction_ready
                else "visual OCR correction is missing, stale, or unvalidated; run ocr-correct and ocr-correct-validate"
            )
        ),
    )
    pages_dir, _page_source_kind = select_refinement_pages(output_dir, config=config)

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

    footnote_status = {"current": True, "detail": "not required at this stage"}
    if stage in {"translate", "package"} and policy.requires_polish:
        from pdf2epub.refine.footnote_apply import footnote_normalization_status

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

    source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
    source_files = list(source_dir.glob("*.md")) if source_dir.is_dir() else []
    source_ready = bool(source_files)
    if source_stage == "polished":
        polish_input_dir, _ = _resolve_pdf_polish_source(output_dir)
        source_ready = source_ready and _polished_stage_is_current(
            output_dir, source_dir, polish_input_dir
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
            blocked_by=(
                ["footnote_normalization"]
                if not footnote_status.get("current")
                else None
            ),
            emit_error=bool(footnote_status.get("current")),
        )

    translation = config.get("translation", {}) or {}
    if not policy.requires_translation:
        record(
            "translation_entities",
            True,
            "not applicable for pipeline: epub_conversion",
        )
    elif policy.requires_entities and not skip_entities:
        entity_ready = (
            False
            if not footnote_status.get("current")
            else _entity_context_is_current(
                output_dir,
                source_dir,
                policy.source_language,
                policy.target_language,
            )
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
                f"{bundle.entries} authoritative entries and "
                f"{len(configured_reference_glossaries)} reference glossary(s)"
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

    if stage == "package" and policy.requires_polish:
        polish_report_path = output_dir / "polish_validation.json"
        polish_dir = output_dir / "polished_markdown" / "validated"
        polish_input_dir, _ = _resolve_pdf_polish_source(output_dir)
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

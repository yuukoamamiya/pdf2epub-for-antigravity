"""HTML/EPUB translation command handlers.

Extraction, validation, and packaging remain local operations; translation is
delegated to the workspace Subagent through the generated hand-off.
"""

import json
from pathlib import Path

from loguru import logger

from pdf2epub.commands.entities import (
    _entity_context_is_current,
    _prepare_entity_subagent_task,
)
from pdf2epub.utils.common import book_output_dir, load_config, resolve_book_input_path
from pdf2epub.utils.logging_config import configure_logging


def _load_html_book_context(args, operation: str):
    """Resolve the EPUB, title, output directory, and operation logging once."""
    config = load_config(args.config)
    book_title = config.get("title")

    def resolve_input(title):
        return resolve_book_input_path(
            getattr(args, "input", None),
            config_value=config.get("input_epub"),
            config_path=args.config,
            output_dir=book_output_dir(title) if title else None,
            extensions=(".epub", ".azw3", ".mobi"),
            output_names=("input.epub", "original.epub"),
        )

    epub_path = resolve_input(book_title)
    if not book_title:
        if epub_path.exists():
            try:
                from pdf2epub.html_translation.epub_parser import EPUBParser

                book_title = EPUBParser(epub_path).get_metadata().get("title") or epub_path.stem
            except Exception:
                book_title = epub_path.stem
            logger.info(f"Auto-inferred book title from input file: {book_title}")
        else:
            logger.error("No title found in config and no input file to infer from.")
            return None

    configure_logging(book_title, operation)
    output_dir = book_output_dir(book_title)
    if not epub_path.exists():
        epub_path = resolve_input(book_title)
    if not epub_path.exists():
        logger.error(
            "Input file not found. Specify input_epub in config or place file in input/."
        )
        return None
    return config, book_title, output_dir, epub_path


def _prepare_html_command(args):
    """Prepare EPUB HTML for a Subagent; this function never translates."""
    from pdf2epub.html_translation import HTMLEpubPipeline

    # This guard is intentional: the former in-process translation branch is
    # no longer reachable from any command.
    args.skip_translate = True

    context = _load_html_book_context(args, "html-prepare")
    if context is None:
        return 1
    config, book_title, output_dir, epub_path = context

    # 格式转换或复制到 output 目录
    from pdf2epub.utils.ebook_converter import needs_conversion, convert_to_epub
    import shutil

    output_dir.mkdir(parents=True, exist_ok=True)
    input_epub = output_dir / "input.epub"

    if needs_conversion(epub_path):
        try:
            epub_path, _ = convert_to_epub(epub_path, output_dir)
        except Exception as e:
            logger.error(f"Format conversion failed: {e}")
            return 1
    elif epub_path.resolve() != input_epub.resolve():
        # EPUB 输入也复制到 output 目录，方便 build-html-epub 找到
        shutil.copy2(epub_path, input_epub)
        logger.info(f"Copied input EPUB to: {input_epub}")
        epub_path = input_epub

    try:
        # Create pipeline
        pipeline = HTMLEpubPipeline(
            epub_path=epub_path,
            output_dir=output_dir,
            config=config
        )

        # Auto-detect source language from EPUB metadata
        source_language = (
            args.source_language
            or config.get("translation", {}).get("source_language")
            or pipeline.source_language
        )
        target_language = args.target_language or config.get("translation", {}).get("target_language", "Chinese")

        from pdf2epub.glossary import (
            build_unit_glossary_contexts,
            load_selected_glossaries,
        )

        try:
            glossary_bundle = load_selected_glossaries(
                config,
                output_dir,
                source_language,
                target_language,
                Path(args.config),
            )
        except Exception as exc:
            logger.error(f"Could not load external glossary: {exc}")
            return 1

        translation_config = config.get("translation", {}) or {}
        require_entities = translation_config.get("require_entities", True)
        entity_path = output_dir / "translation_entities.json"
        entity_ready = _entity_context_is_current(
            output_dir,
            output_dir / "compressed_units",
            source_language,
            target_language,
        )
        context_files = dict(glossary_bundle.context_files)
        if entity_ready:
            context_files["translation_entities"] = entity_path
        skip_entities = bool(getattr(args, "skip_entities", False))
        if skip_entities or not require_entities:
            skipped_context_files = [] if entity_ready else ["translation_entities"]
        else:
            skipped_context_files = []

        logger.info(f"Starting HTML translation for: {pipeline.book_title}")
        logger.info(f"Source EPUB: {epub_path}")
        logger.info(f"Translation: {source_language} → {target_language}")

        # Step 1: Extract and preprocess
        if not getattr(args, "skip_extract", False):
            extracted = pipeline.extract_and_preprocess(
                target_language=target_language,
                translation_context=context_files or None,
            )
            logger.info(f"Extracted {extracted} XHTML files")

        # EPUB translations use the same book-wide entity hand-off as PDF
        # translations.  The first html-prepare creates this task; rerunning
        # html-prepare after the Subagent writes the entities attaches them to
        # the body and metadata translation contracts.
        if not skip_entities and require_entities:
            try:
                _prepare_entity_subagent_task(
                    output_dir,
                    book_title or pipeline.book_title,
                    output_dir / "compressed_units",
                    "epub-compressed",
                    source_language,
                    target_language,
                    config,
                )
            except Exception as exc:
                logger.error(f"Could not prepare EPUB entity task: {exc}")
                return 1
            entity_ready = _entity_context_is_current(
                output_dir,
                output_dir / "compressed_units",
                source_language,
                target_language,
            )
            if not entity_ready:
                logger.info(
                    "EPUB terminology task is pending. Let the Subagent write "
                    "translation_entities.json, then rerun html-prepare."
                )
                return 0
            context_files["translation_entities"] = entity_path
            skipped_context_files = []
            # Refresh metadata hand-off so it references the newly validated
            # book entity glossary as well as any selected domain glossaries.
            pipeline.create_metadata_translation_source(
                target_language=target_language,
                context_files=context_files,
            )

        # Create the body translation contract alongside the metadata contract.
        # Both are workspace Subagent tasks; this command never translates.
        from pdf2epub.markdown_handoff import prepare_markdown_subagent
        body_paths = prepare_markdown_subagent(
            output_dir,
            "translate-html",
            pipeline.compressed_units_dir,
            pipeline.translated_dir,
            source_language,
            target_language,
            extra_rules=(
                "【1:1 Line Count Consistency】: Keep exactly one non-empty output line for every non-empty source translation unit. Never insert internal newlines or line breaks inside a paragraph.",
                "【Exact Tag Sequence】: Preserve every HTML tag, attribute, and entity (<span ...>, <a ...>, <em>, <i>, <b>, <ruby>, etc.) in the exact same sequence. NEVER delete or merge adjacent tags (e.g. `<span>A</span> (<span>B</span>)` MUST remain two separate tags `<span>甲</span> (<span>乙</span>)`, not merged into one).",
                "【No Invented Containers】: Copy the source line's outer shape exactly. If a source line does not begin and end with `<div>...</div>`, NEVER add a `<div>` wrapper yourself. The compressor mapping restores structural containers; wrappers are not translation content.",
                "【Italic Tag 1:1 Preservation】: Preserve the exact count, order, and nesting of `<i>...</i>` tags. Example: `<i>Gidra</i>` may become `<i>《基多拉》（Gidra）</i>`, and `<i>A, B</i>` may become `<i>《甲》、《乙》</i>`; in both cases it remains one `<i>` pair, never two pairs or none.",
                "【Entity Preservation】: Copy protected entities and anchors literally, including `&amp;`, `&lt;`, `&gt;`, `<a/>`, and other self-closing or placeholder tokens. Translate surrounding text only; never replace an entity with its prose meaning.",
                "【Direct File Writing】: Write output directly to the designated target file without markdown code fences.",
                "【Self-Validation】: After writing each target file, run `uv run pdf2epub -c <same-config> html-validate --file <filename>` from the repository root. Only report that file complete when the command exits with code 0; this single-file check does not replace the final full-book html-validate.",
                *glossary_bundle.rules,
            ),
            config=config,
            resume=getattr(args, "resume", False),
            context_files=context_files or None,
            skipped_context_files=skipped_context_files,
            unit_context_files=build_unit_glossary_contexts(
                output_dir,
                output_dir / "compressed_units",
                glossary_bundle.context_files,
                None if skip_entities else entity_path,
            ),
            declared_files=pipeline.declared_translation_files(),
        )
        from pdf2epub.subagent_runtime import write_worker_handoffs

        worker_handoffs = write_worker_handoffs(
            output_dir, body_paths["manifest"], body_paths["prompt"]
        )

        logger.success("EPUB HTML preparation complete")
        logger.info(f"Output: {output_dir / 'compressed_units'}")
        logger.info(f"Body Subagent prompt: {body_paths['prompt']}")
        if worker_handoffs:
            logger.info(f"Worker handoffs: {output_dir / 'worker_handoffs'}")
        logger.info(
            "Recommended models: body/metadata are declared in their manifests "
            "and default to the configured translation model."
        )
        logger.info("Next step: use the Subagent, then run html-validate and build-html-epub")
        return 0

    except Exception as e:
        logger.error(f"HTML translation failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


def html_prepare_command(args):
    """Handle html-prepare subcommand: extract EPUB and prepare compressed units (pure local, no LLM)."""
    args.skip_extract = False
    args.skip_translate = True
    args.limit = None
    args.use_entities = None
    args.no_entities = False
    args.resume = getattr(args, "resume", False)
    args.source_language = getattr(args, 'source_language', None)
    args.target_language = getattr(args, 'target_language', None)
    args.max_workers = 1
    return _prepare_html_command(args)


def html_validate_command(args):
    """Handle html-validate subcommand: validate translated compressed units (pure local, no LLM)."""
    from pdf2epub.html_translation import HTMLEpubPipeline

    file_name = getattr(args, "file", None)
    if file_name:
        candidate = Path(file_name)
        if candidate.name != file_name or candidate.suffix.lower() != ".md":
            logger.error("--file must be a direct .md filename, such as 20_Chapter1.md")
            return 1

    context = _load_html_book_context(args, "html-validate")
    if context is None:
        return 1
    config, book_title, output_dir, epub_path = context

    pipeline = HTMLEpubPipeline(
        epub_path=epub_path,
        output_dir=output_dir,
        config=config
    )

    report = pipeline.validate_translated_units(file_name=file_name)
    report_path = output_dir / (
        "translate-html_validation.json"
        if not file_name
        else "translate-html_validation.single.json"
    )
    report_path.write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    if file_name:
        pipeline.persist_file_validation_checkpoint(report)

    logger.info("=" * 60)
    logger.info(f"翻译单元验证结果: {book_title}")
    logger.info("=" * 60)
    logger.info(f"总单元数: {report['total']}")
    logger.info(f"已完成:   {report['completed']}/{report['total']}")
    logger.info(f"通过校验: {report['valid']}/{report['total']}")
    metadata_report = report.get("metadata", {})
    if report.get("scope") == "file":
        logger.info(f"单文件校验范围: {report.get('file')}")
        logger.info("单文件模式跳过元数据和全书完整性检查")
    elif metadata_report.get("valid"):
        logger.info("元数据翻译校验通过（作者和出版社保持原文）")
    else:
        logger.error("元数据翻译校验未通过:")
        for error in metadata_report.get("errors", []):
            logger.error(f"   - {error}")
    context_report = report.get("translation_context", {})
    if context_report.get("valid"):
        if context_report.get("glossaries"):
            logger.info(
                "术语上下文校验通过: "
                + ", ".join(context_report["glossaries"])
            )
        elif context_report.get("skipped_context_files"):
            logger.info("已按任务配置跳过书内实体术语表")
    elif context_report:
        logger.error("术语上下文校验未通过:")
        for error in context_report.get("errors", []):
            logger.error(f"   - {error}")
    if report['missing']:
        logger.warning(f"未翻译单元 ({len(report['missing'])}):")
        for m in report['missing'][:10]:
            logger.warning(f"   - {m}")
        if len(report['missing']) > 10:
            logger.warning(f"   ...以及其余 {len(report['missing']) - 10} 个")
    if report['invalid']:
        logger.error(f"校验未通过单元 ({len(report['invalid'])}):")
        for inv in report['invalid']:
            logger.error(f"   - {inv['file']}: {inv['reason']}")
    if report.get("safety_blocked"):
        logger.error(f"检测到拒答/免责声明单元: {report['safety_blocked'][:10]}")

    logger.info("=" * 60)
    if report['all_passed']:
        if report.get("scope") == "file":
            logger.success("该翻译文件校验通过；全书仍需运行不带 --file 的 html-validate。")
        else:
            logger.success("所有翻译单元校验通过！可执行 pdf2epub build-html-epub 进行打包。")
        return 0
    else:
        logger.warning("存在未完成或未通过校验的单元。")
        return 1


def html_skeleton_retry_command(args):
    """Prepare one complex HTML unit for protected-token retry translation."""
    from pdf2epub.html_translation.skeleton import write_masked_unit
    from pdf2epub.markdown_handoff import prepare_markdown_subagent

    file_name = str(args.file)
    candidate = Path(file_name)
    if candidate.name != file_name or candidate.suffix.lower() != ".md":
        logger.error("--file must be a direct .md filename")
        return 1
    context = _load_html_book_context(args, "html-skeleton-retry")
    if context is None:
        return 1
    config, book_title, output_dir, epub_path = context
    from pdf2epub.html_translation import HTMLEpubPipeline

    pipeline = HTMLEpubPipeline(epub_path=epub_path, output_dir=output_dir, config=config)
    if file_name not in pipeline._declared_html_source_names():
        logger.error(f"Unknown HTML translation unit: {file_name}")
        return 1

    source_path = pipeline.compressed_units_dir / file_name
    skeleton_dir = output_dir / "skeleton_units"
    contract_dir = output_dir / "skeleton_contracts"
    target_dir = output_dir / "translated_skeleton"
    write_masked_unit(
        source_path,
        skeleton_dir / file_name,
        contract_dir / f"{Path(file_name).stem}.json",
    )
    paths = prepare_markdown_subagent(
        output_dir,
        "translate-html-skeleton",
        skeleton_dir,
        target_dir,
        config.get("translation", {}).get("source_language", "English"),
        config.get("translation", {}).get("target_language", "Chinese"),
        extra_rules=(
            "Translate the complete line with its surrounding sentence context.",
            "Preserve every placeholder such as ⟦HTML_0001⟧ exactly, including count and order.",
            "Never translate, remove, duplicate, or reorder placeholders.",
            "Write only the translated masked lines to the assigned target file.",
        ),
        config=config,
        resume=getattr(args, "resume", False),
        declared_files=[file_name],
    )
    logger.success(f"Skeleton retry prompt: {paths['prompt']}")
    logger.info(
        "After the Subagent finishes, run html-skeleton-restore and then html-validate --file."
    )
    return 0


def html_skeleton_restore_command(args):
    """Restore one validated masked translation into translated_compressed."""
    from pdf2epub.html_translation.skeleton import restore_unit

    file_name = str(args.file)
    candidate = Path(file_name)
    if candidate.name != file_name or candidate.suffix.lower() != ".md":
        logger.error("--file must be a direct .md filename")
        return 1
    context = _load_html_book_context(args, "html-skeleton-restore")
    if context is None:
        return 1
    _config, _book_title, output_dir, _epub_path = context
    translated_path = output_dir / "translated_skeleton" / file_name
    contract_path = output_dir / "skeleton_contracts" / f"{Path(file_name).stem}.json"
    output_path = output_dir / "translated_compressed" / file_name
    if not translated_path.is_file() or not contract_path.is_file():
        logger.error("Skeleton translation or restore contract is missing")
        return 1
    try:
        restore_unit(translated_path, contract_path, output_path)
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
        logger.error(f"Skeleton restore failed: {exc}")
        return 1
    logger.success(f"Restored translated HTML unit: {file_name}")
    return 0


def build_html_epub_command(args):
    """Handle the build-html-epub subcommand (rebuild EPUB with translated HTML)."""
    from pdf2epub.html_translation import HTMLEpubPipeline

    context = _load_html_book_context(args, "build-html-epub")
    if context is None:
        return 1
    config, book_title, output_dir, epub_path = context

    # 格式转换或复制到 output 目录
    from pdf2epub.utils.ebook_converter import needs_conversion, convert_to_epub
    import shutil

    output_dir.mkdir(parents=True, exist_ok=True)
    input_epub = output_dir / "input.epub"

    if needs_conversion(epub_path):
        try:
            epub_path, _ = convert_to_epub(epub_path, output_dir)
        except Exception as e:
            logger.error(f"Format conversion failed: {e}")
            return 1
    elif epub_path.resolve() != input_epub.resolve():
        shutil.copy2(epub_path, input_epub)
        logger.info(f"Copied input EPUB to: {input_epub}")
        epub_path = input_epub

    logger.info(f"Building translated EPUB for: {book_title}")

    try:
        # Create pipeline
        pipeline = HTMLEpubPipeline(
            epub_path=epub_path,
            output_dir=output_dir,
            config=config
        )

        # Determine output path (None = let postprocess_and_build use translated title)
        output_epub = Path(args.output) if args.output else None

        # Build EPUB (restore attrs + repackage)
        validation = pipeline.validate_translated_units()
        if not validation["all_passed"] and not args.allow_partial:
            logger.error(
                "翻译校验未通过，拒绝打包。请先运行 html-validate；"
                "如确实需要生成部分译文，请显式使用 --allow-partial。"
            )
            return 1

        result_path = pipeline.postprocess_and_build(
            output_epub,
            allow_partial=args.allow_partial,
        )

        logger.success(f"EPUB created: {result_path}")
        return 0

    except Exception as e:
        logger.error(f"EPUB build failed: {e}")
        import traceback
        traceback.print_exc()
        return 1

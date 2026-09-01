#!/usr/bin/env python3
"""
Unified CLI for pdf2epub markdown processing.

This module provides a single entrypoint for all markdown processing operations
including polishing OCR output and translating content.
"""

import argparse
import json
import sys
from pathlib import Path
from loguru import logger
from pdf2epub.utils.logging_config import configure_logging
from pdf2epub.utils.common import (
    load_config,
    resolve_book_input_path,
    sanitize_filename,
)
from pdf2epub.utils.encoding import configure_utf8_stdio

# Windows consoles may still default to an active code page such as GBK.
# Configure before loguru captures stderr so international filenames cannot
# abort an otherwise successful CLI command.
configure_utf8_stdio()

# Configure logger
logger = configure_logging()


def _resolve_pdf_markdown_source(output_dir: Path, config):
    """Choose the Markdown stage shared by PDF translation and source builds."""
    translation = config.get("translation", {})
    requested_stage = str(translation.get("source_stage", "auto")).strip().lower()
    if requested_stage not in {"auto", "ocr", "polished"}:
        raise ValueError(
            "translation.source_stage must be one of: auto, ocr, polished"
        )

    polished_dir = output_dir / "polished_markdown" / "validated"
    ocr_dir = output_dir / "ocr_markdown"
    polished_available = polished_dir.is_dir() and any(polished_dir.glob("*.md"))

    if requested_stage == "polished":
        return polished_dir, "polished"
    if requested_stage == "ocr":
        return ocr_dir, "ocr"
    if polished_available:
        return polished_dir, "polished"
    return ocr_dir, "ocr"


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
            roles[str(unit.get("file") or "")] = role
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


def _validate_pdf_source_stage(args, source_stage: str) -> int:
    """Validate a Subagent-produced source stage when one is selected."""
    if source_stage == "polished":
        return polish_validate_command(args)
    return 0


def polish_command(args):
    """Prepare a local Markdown hand-off for a polishing Subagent."""
    return _prepare_pdf_markdown_task(args, "polish")


def _prepare_pdf_markdown_task(args, task: str):
    from pdf2epub.subagent_workflow import prepare_markdown_subagent, resolve_subagent_model

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    output_dir = Path("output") / book_title
    translation = config.get("translation", {})
    source_language = getattr(args, "source_language", None) or translation.get(
        "source_language", "English"
    )
    target_language = getattr(args, "target_language", None) or translation.get(
        "target_language", "Chinese"
    )
    if task == "polish":
        source_dir = output_dir / "ocr_markdown"
        target_dir = output_dir / "polished_markdown"
        rules = [
            "Fix OCR line breaks, obvious OCR errors, and formatting while preserving meaning.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations.",
        ]
        content_type = getattr(args, "content_type", "auto")
        if content_type and content_type != "auto":
            rules.append(f"Treat this as {content_type} content and preserve its domain-specific conventions.")
    else:
        source_dir, _ = _resolve_pdf_markdown_source(output_dir, config)
        target_dir = output_dir / "translated"
        rules = [
            "Translate prose to the target language; do not summarize, censor, or add commentary.",
            "Preserve Markdown heading levels, image links, footnote references, formulas, and link destinations exactly.",
            "Keep one output file for every source file and keep filenames unchanged.",
            "Output only the target-language replacement: never add bilingual paragraphs, parallel English titles, or the original text beside the translation.",
            "Do not upgrade ordinary paragraphs, italic text, or bold text into Markdown headings: preserve exactly whether the source line begins with #.",
            "Never add Markdown code fences (```); if the source has no fence, the translated output must have no fence.",
            "If translation_entities.json exists, use it as a terminology reference without modifying it.",
        ]
    configure_logging(book_title, f"{task}-prepare")
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
        )
        if task == "translate":
            from pdf2epub.subagent_workflow import prepare_toc_translation_subagent
            toc_paths = prepare_toc_translation_subagent(
                output_dir, source_language, target_language, config=config
            )
            paths.update({"toc_source": toc_paths["source"], "toc_prompt": toc_paths["prompt"]})
    except Exception as exc:
        logger.error(f"Could not prepare {task} task: {exc}")
        return 1
    logger.success(f"Wrote Subagent prompt: {paths['prompt']}")
    logger.info(f"Recommended Antigravity model: {resolve_subagent_model(config, task)}")
    logger.info(
        f"在 Antigravity 中让 Subagent 执行提示词，完成后运行 {task}-validate。"
    )
    return 0


def polish_validate_command(args):
    return _validate_pdf_markdown_task(args, "polish")


def _validate_pdf_markdown_task(args, task: str):
    from pdf2epub.subagent_workflow import validate_markdown_subagent

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    output_dir = Path("output") / book_title
    if task == "polish":
        source_dir = output_dir / "ocr_markdown"
        target_dir = output_dir / "polished_markdown"
    else:
        source_dir, _ = _resolve_pdf_markdown_source(output_dir, config)
        target_dir = output_dir / "translated"
    report = validate_markdown_subagent(
        output_dir,
        task,
        source_dir,
        target_dir,
        structural_patterns=(r"^#{1,6}\s", r"!\[[^\]]*\]\([^)]+\)", r"\[\^[^\]]+\]"),
        file_roles=_load_pdf_file_roles(output_dir) if task == "translate" else None,
        tolerate_duplicate_headings=task == "polish",
    )
    if task == "translate":
        from pdf2epub.subagent_workflow import validate_toc_translation_subagent
        toc_report = validate_toc_translation_subagent(output_dir)
        report["toc"] = toc_report
        report["all_passed"] = report["all_passed"] and toc_report["valid"]
        if not toc_report["valid"]:
            for error in toc_report["errors"]:
                logger.error(f"TOC: {error}")
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
    if report["all_passed"]:
        logger.success(f"{task} Subagent output validated: {report['validated_dir']}")
        return 0
    return 1



def refine_command(args):
    """Prepare the PDF structure task for an Antigravity Subagent."""
    return refine_prepare_command(args)


def refine_prepare_command(args):
    """Prepare a PDF TOC task for an Antigravity workspace subagent."""
    from pathlib import Path
    from pdf2epub.refine.subagent_workflow import prepare_refine_subagent

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1

    output_dir = Path("output") / book_title
    configure_logging(book_title, "refine-prepare")
    refine_config = config.get("refine", {})
    max_tokens = args.max_tokens or refine_config.get("max_tokens", 8000)
    try:
        paths = prepare_refine_subagent(output_dir, book_title, max_tokens, config=config)
    except Exception as exc:
        logger.error(f"Could not prepare refine task: {exc}")
        return 1

    logger.success(f"Wrote subagent prompt: {paths['prompt']}")
    logger.success(f"Wrote subagent manifest: {paths['manifest']}")
    logger.info(
        "请在 Antigravity 中让子 Agent 阅读 refine_subagent_prompt.md，"
        "并在同一目录写入 toc_tree.json。完成后运行 pdf2epub refine-local。"
    )
    return 0


def refine_local_command(args):
    """Consume a subagent TOC and generate PDF work units without an LLM."""
    from pathlib import Path
    from pdf2epub.refine import RefinedBreakdown

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1

    configure_logging(book_title, "refine-local")
    output_dir = Path("output") / book_title
    refine_config = config.get("refine", {})
    max_tokens = args.max_tokens or refine_config.get("max_tokens", 8000)
    try:
        refiner = RefinedBreakdown(
            config=config,
            max_tokens=max_tokens,
        )
        units = refiner.process_from_toc(
            pdf_path=output_dir / "input.pdf",
            output_dir=output_dir,
            book_title=book_title,
            resume=args.resume,
        )
    except Exception as exc:
        logger.error(f"Local refine failed: {exc}")
        return 1

    logger.success(f"Local refine complete: {len(units)} units generated")
    return 0


def ocr_pages_command(args):
    """Handle the ocr-pages subcommand (page-level OCR)."""
    from pdf2epub.ocr_pages import ocr_full_book_pagewise
    from pathlib import Path

    # Load configuration
    config = load_config(args.config)
    book_title = config.get("title")

    if not book_title:
        logger.error("No title found in config.yaml")
        return 1

    # Configure file logging
    configure_logging(book_title, "ocr-pages")

    # Setup paths
    output_dir = Path("output") / book_title

    # Find PDF
    pdf_path = resolve_book_input_path(
        args.input,
        config_value=config.get("input_pdf") or config.get("input"),
        config_path=args.config,
        output_dir=output_dir,
        extensions=(".pdf",),
        output_names=("input_original.pdf", "input.pdf"),
    )

    if not pdf_path.exists():
        logger.error(f"PDF not found: {pdf_path}")
        logger.info("Specify --input with the path to your PDF file")
        return 1

    # Preprocess PDF: copy to output dir + add page stamps + compress
    from pdf2epub.utils.pdf_utils import preprocess_pdf
    pdf_path = preprocess_pdf(pdf_path, output_dir)

    logger.info(f"Starting page-level OCR for: {book_title}")

    # Get OCR settings from config
    ocr_config = config.get('ocr', {})
    backend = ocr_config.get('backend', 'mistral')
    backend_config = ocr_config.get('backends', {}).get(backend, {})
    max_workers = args.max_workers or backend_config.get(
        'max_workers',
        ocr_config.get('vision', {}).get('max_workers', 5),
    )

    # Get credentials
    credentials = config.get('credentials', {}).get('providers', {})

    # Setup backend-specific parameters
    api_key = None
    base_url = None

    if backend == 'mistral':
        mistral_config = credentials.get('mistral', {})
        api_key = mistral_config.get('api_key')
        base_url = mistral_config.get('base_url')
    elif backend == 'azure':
        azure_config = credentials.get('azure', {})
        api_key = azure_config.get('api_key')
        base_url = azure_config.get('endpoint')

    try:
        ocr_full_book_pagewise(
            pdf_path=pdf_path,
            output_dir=output_dir,
            start_page=args.start_page or 1,
            end_page=args.end_page,
            backend=backend,
            api_key=api_key,
            base_url=base_url,
            resume=args.resume,
            config=config,
            max_workers=max_workers
        )

        logger.success(f"Page-level OCR complete!")
        logger.info(f"Output: {output_dir / 'pages'}")
        logger.info("Next step: pdf2epub refine-prepare")
        return 0

    except Exception as e:
        logger.error(f"OCR failed: {e}")
        import traceback
        traceback.print_exc()
        return 1



def extract_entities_command(args):
    """Prepare entity extraction for an Antigravity Subagent (local only)."""
    from pdf2epub.subagent_workflow import resolve_subagent_model

    config = load_config(args.config)
    book_title = config.get("title") or (Path(args.input).stem if args.input else None)
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    output_dir = Path("output") / book_title
    source_dir = output_dir / "ocr_markdown"
    if not source_dir.exists():
        source_dir = output_dir / "pages"
    if not list(source_dir.glob("*.md")):
        logger.error(f"No OCR Markdown found in {source_dir}; run OCR and refine first")
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    model = resolve_subagent_model(config, "extract-entities")
    manifest = {
        "schema_version": 1,
        "workflow": "antigravity-subagent",
        "task": "extract-entities",
        "source_language": args.source_lang,
        "target_language": args.target_lang,
        "model": model,
        "source_dir": str(source_dir.relative_to(output_dir)),
        "output_file": "translation_entities.json",
    }
    (output_dir / "entity_subagent_manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    from pdf2epub.entity_extractor import create_entity_extraction_prompt
    (output_dir / "entity_subagent_prompt.md").write_text(
        create_entity_extraction_prompt(book_title, (args.source_lang, args.target_lang))
        + f"\n\nRecommended Antigravity model: `{model}`\n",
        encoding="utf-8",
    )
    logger.info(
        f"已生成实体提取 Subagent 任务：{output_dir / 'entity_subagent_prompt.md'}；"
        "完成后由 Subagent 写入 translation_entities.json。"
    )
    return 0


def extract_entities_validate_command(args):
    """Validate the optional entity JSON written by a workspace Subagent."""
    from pdf2epub.entity_extractor import validate_entities

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    entity_path = Path("output") / book_title / "translation_entities.json"
    if not entity_path.exists():
        logger.error(f"Entity output not found: {entity_path}")
        return 1
    try:
        data = json.loads(entity_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        logger.error(f"Invalid entity JSON: {exc}")
        return 1
    errors = validate_entities(data, book_title)
    if errors:
        for error in errors:
            logger.error(error)
        return 1
    logger.success(f"Entity Subagent output validated: {entity_path}")
    return 0



def build_epub_command(args):
    """Handle the build-epub subcommand (toc_tree.json driven)."""
    import asyncio
    from pathlib import Path
    from .build_epub import build_epub, BuildEpubConfig

    # Load configuration
    config = load_config(args.config)
    book_title = config.get("title")

    if not book_title:
        logger.error("No title found in config.yaml")
        return 1

    # Configure file logging
    configure_logging(book_title, "build-epub")

    # Set up paths
    output_dir = Path("output") / book_title
    toc_tree_path = output_dir / "toc_tree.json"

    if not toc_tree_path.exists():
        logger.error(f"toc_tree.json not found at {toc_tree_path}")
        logger.info("Run 'refine-prepare', use a Subagent, then 'refine-local' first to generate toc_tree.json")
        return 1

    # V2 architecture stores Subagent results in validated/ subdirectories.
    source_dir, source_stage = _resolve_pdf_markdown_source(output_dir, config)
    if args.translated:
        markdown_dir = output_dir / "translated" / "validated"
        logger.info("Building EPUB from translated markdown...")
        source_validation = _validate_pdf_source_stage(args, source_stage)
        if source_validation != 0:
            logger.error("Refusing to build: the English source stage is not validated")
            return 1
        if not source_dir.is_dir() or not any(source_dir.glob("*.md")):
            logger.error(f"English source Markdown not found: {source_dir}")
            return 1
    else:
        markdown_dir = source_dir
        logger.info(f"Building EPUB from {source_stage} markdown...")

    if not markdown_dir.is_dir() or not any(markdown_dir.glob("*.md")):
        logger.error(f"Markdown directory not found: {markdown_dir}")
        logger.info("Run the corresponding Subagent task and its -validate command first")
        return 1

    validation_result = translate_validate_command(args) if args.translated else 0
    if not args.translated:
        validation_result = _validate_pdf_source_stage(args, source_stage)
    if validation_result != 0:
        logger.error("Refusing to build from unvalidated Subagent output")
        return 1

    # Set up images directory
    images_dir = output_dir / "images"
    if not images_dir.exists():
        images_dir = None

    # Set up cover image
    cover_image = None
    if args.cover:
        cover_path = Path(args.cover)
        if cover_path.exists():
            cover_image = cover_path
        else:
            logger.warning(f"Cover image not found: {args.cover}")
    else:
        # Auto-detect cover in images directory
        if images_dir:
            for cover_name in ["cover.jpg", "cover.jpeg", "cover.png", "cover.gif"]:
                cover_path = images_dir / cover_name
                if cover_path.exists():
                    cover_image = cover_path
                    logger.info(f"Auto-detected cover image: {cover_path}")
                    break

    # Get target language from config
    target_language = config.get("translation", {}).get("target_language", "Chinese")

    if args.translated:
        source_language = config.get("translation", {}).get(
            "source_language", "English"
        )
        safe_title = sanitize_filename(book_title)
        english_epub = output_dir / f"{safe_title}_en.epub"
        english_config = BuildEpubConfig(
            book_title=book_title,
            output_dir=output_dir,
            markdown_dir=source_dir,
            toc_tree_path=toc_tree_path,
            images_dir=images_dir,
            cover_image=cover_image,
            translated=False,
            target_language=source_language,
            config=config,
            output_epub=english_epub,
        )
        try:
            english_path = build_epub(english_config)
            logger.success(f"English EPUB created: {english_path}")
        except Exception as e:
            logger.error(f"English EPUB build failed: {e}")
            import traceback
            traceback.print_exc()
            return 1

    # Create config
    build_config = BuildEpubConfig(
        book_title=book_title,
        output_dir=output_dir,
        markdown_dir=markdown_dir,
        toc_tree_path=toc_tree_path,
        images_dir=images_dir,
        cover_image=cover_image,
        translated=args.translated,
        target_language=target_language,
        config=config
    )

    try:
        # Build EPUB
        epub_path = build_epub(build_config)
        logger.success(f"EPUB created: {epub_path}")
        return 0
    except Exception as e:
        logger.error(f"EPUB build failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _prepare_html_command(args):
    """Prepare EPUB HTML for a Subagent; this function never translates."""
    from pathlib import Path
    from pdf2epub.html_translation import HTMLEpubPipeline

    # This guard is intentional: the former in-process translation branch is
    # no longer reachable from any command.
    args.skip_translate = True

    # Load configuration
    config = load_config(args.config)
    book_title = config.get("title")

    epub_path = resolve_book_input_path(
        getattr(args, "input", None),
        config_value=config.get("input_epub"),
        config_path=args.config,
        output_dir=Path("output") / book_title if book_title else None,
        extensions=(".epub", ".azw3", ".mobi"),
        output_names=("input.epub", "original.epub"),
    )

    # Auto-infer book_title from metadata or filename if missing
    if not book_title:
        if epub_path and epub_path.exists():
            try:
                from pdf2epub.html_translation.epub_parser import EPUBParser
                parser = EPUBParser(epub_path)
                meta = parser.get_metadata()
                book_title = meta.get("title") or epub_path.stem
            except Exception:
                book_title = epub_path.stem
            logger.info(f"Auto-inferred book title from input file: {book_title}")
        else:
            logger.error("No title found in config and no input file to infer from.")
            return 1

    # Configure file logging
    configure_logging(book_title, "html-prepare")

    # Setup paths
    output_dir = Path("output") / book_title

    # If no epub specified, look for original epub in output dir
    if not epub_path.exists():
        epub_path = resolve_book_input_path(
            getattr(args, "input", None),
            config_value=config.get("input_epub"),
            config_path=args.config,
            output_dir=output_dir,
            extensions=(".epub", ".azw3", ".mobi"),
            output_names=("input.epub", "original.epub"),
        )

    if epub_path is None or not epub_path.exists():
        logger.error("Input file not found. Specify input_epub in config or place file in input/.")
        return 1

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
        source_language = args.source_language or pipeline.source_language
        target_language = args.target_language or config.get("translation", {}).get("target_language", "Chinese")

        logger.info(f"Starting HTML translation for: {pipeline.book_title}")
        logger.info(f"Source EPUB: {epub_path}")
        logger.info(f"Translation: {source_language} → {target_language}")

        # Step 1: Extract and preprocess
        if not getattr(args, "skip_extract", False):
            extracted = pipeline.extract_and_preprocess(target_language=target_language)
            logger.info(f"Extracted {extracted} XHTML files")

        # Create the body translation contract alongside the metadata contract.
        # Both are workspace Subagent tasks; this command never translates.
        from pdf2epub.subagent_workflow import prepare_markdown_subagent
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
                "【Direct File Writing】: Write output directly to the designated target file without markdown code fences.",
                "【Self-Validation】: Subagents should verify that output line count and tag sequences match the source before marking the task complete.",
            ),
            config=config,
            resume=getattr(args, "resume", False),
        )

        logger.success("EPUB HTML preparation complete")
        logger.info(f"Output: {output_dir / 'compressed_units'}")
        logger.info(f"Body Subagent prompt: {body_paths['prompt']}")
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
    from pathlib import Path
    from pdf2epub.html_translation import HTMLEpubPipeline

    config = load_config(args.config)
    book_title = config.get("title")

    epub_path = resolve_book_input_path(
        getattr(args, "input", None),
        config_value=config.get("input_epub"),
        config_path=args.config,
        output_dir=Path("output") / book_title if book_title else None,
        extensions=(".epub", ".azw3", ".mobi"),
        output_names=("input.epub", "original.epub"),
    )

    # Auto-infer book_title from metadata or filename if missing
    if not book_title:
        if epub_path and epub_path.exists():
            try:
                from pdf2epub.html_translation.epub_parser import EPUBParser
                parser = EPUBParser(epub_path)
                meta = parser.get_metadata()
                book_title = meta.get("title") or epub_path.stem
            except Exception:
                book_title = epub_path.stem
            logger.info(f"Auto-inferred book title from input file: {book_title}")
        else:
            logger.error("No title found in config and no input file to infer from.")
            return 1

    configure_logging(book_title, "html-validate")
    output_dir = Path("output") / book_title

    if not epub_path.exists():
        epub_path = resolve_book_input_path(
            getattr(args, "input", None),
            config_value=config.get("input_epub"),
            config_path=args.config,
            output_dir=output_dir,
            extensions=(".epub", ".azw3", ".mobi"),
            output_names=("input.epub", "original.epub"),
        )

    if epub_path is None or not epub_path.exists():
        logger.error("Input file not found. Specify input_epub in config or place file in input/.")
        return 1

    pipeline = HTMLEpubPipeline(
        epub_path=epub_path,
        output_dir=output_dir,
        config=config
    )

    report = pipeline.validate_translated_units()
    (output_dir / "translate-html_validation.json").write_text(
        json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8"
    )

    logger.info("=" * 60)
    logger.info(f"翻译单元验证结果: {book_title}")
    logger.info("=" * 60)
    logger.info(f"总单元数: {report['total']}")
    logger.info(f"已完成:   {report['completed']}/{report['total']}")
    logger.info(f"通过校验: {report['valid']}/{report['total']}")
    metadata_report = report.get("metadata", {})
    if metadata_report.get("valid"):
        logger.info("元数据翻译校验通过（作者和出版社保持原文）")
    else:
        logger.error("元数据翻译校验未通过:")
        for error in metadata_report.get("errors", []):
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
        logger.success("所有翻译单元校验通过！可执行 pdf2epub build-html-epub 进行打包。")
        return 0
    else:
        logger.warning("存在未完成或未通过校验的单元。")
        return 1


def translate_novel_command(args):
    """Prepare light-novel text and metadata for a Subagent, locally only."""
    import hashlib

    from pdf2epub.subagent_workflow import resolve_subagent_model

    import shutil
    from pdf2epub.html_translation.epub_parser import EPUBParser
    from pdf2epub.html_translation.novel_extractor import NovelExtractor
    from pdf2epub.html_translation.builder import HTMLEpubPipeline
    from pdf2epub.utils.ebook_converter import needs_conversion, convert_to_epub

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    output_dir = Path("output") / book_title
    epub_path = resolve_book_input_path(
        args.input,
        config_value=config.get("input_epub"),
        config_path=args.config,
        output_dir=output_dir,
        extensions=(".epub", ".azw3", ".mobi"),
        output_names=("input.epub", "original.epub"),
    )
    if not epub_path.exists():
        logger.error("Input EPUB not found. Use -i to specify it.")
        return 1
    output_dir.mkdir(parents=True, exist_ok=True)
    model = resolve_subagent_model(config, "translate-novel")
    input_epub = output_dir / "input.epub"
    try:
        if needs_conversion(epub_path):
            epub_path, _ = convert_to_epub(epub_path, output_dir)
        elif epub_path.resolve() != input_epub.resolve():
            shutil.copy2(epub_path, input_epub)
            epub_path = input_epub
        parser = EPUBParser(str(epub_path))
        units = NovelExtractor(parser).extract_all(output_dir / "novel_units")
        content_units = [unit for unit in units if unit.has_content]
        metadata_pipeline = HTMLEpubPipeline(epub_path, output_dir, config)
        metadata_pipeline.create_metadata_translation_source(
            target_language=args.target_language
            or config.get("translation", {}).get("target_language", "Chinese")
        )
        manifest = {
            "schema_version": 1,
            "workflow": "antigravity-subagent",
            "task": "translate-novel",
            "source_language": args.source_language
            or config.get("translation", {}).get("source_language", "Japanese"),
            "target_language": args.target_language
            or config.get("translation", {}).get("target_language", "Chinese"),
            "model": model,
            "source_dir": "novel_units",
            "target_dir": "translated_novel",
            "files": [unit.text_path.name for unit in content_units],
        }
        translated_dir = output_dir / "translated_novel"
        completed_files = []
        if getattr(args, "resume", False):
            validation = {}
            validation_path = output_dir / "translate-novel_validation.json"
            if validation_path.is_file():
                try:
                    validation = json.loads(validation_path.read_text(encoding="utf-8"))
                except (OSError, json.JSONDecodeError):
                    validation = {}
            validation_available = isinstance(validation.get("valid_files"), list)
            validated_files = set(validation.get("valid_files", []))
            validation_hashes = validation.get("source_sha256", {})
            completed_files = [
                name for name in manifest["files"]
                if (translated_dir / name).is_file()
                and (translated_dir / name).read_text(encoding="utf-8").strip()
                and (
                    validation_available
                    and (
                        name in validated_files
                        and validation_hashes.get(name)
                        == hashlib.sha256((output_dir / "novel_units" / name).read_bytes()).hexdigest()
                    )
                )
            ]
        manifest["completed_files"] = completed_files
        manifest["pending_files"] = [
            name for name in manifest["files"] if name not in completed_files
        ]
        (output_dir / "novel_subagent_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (output_dir / "novel_subagent_prompt.md").write_text(
            f"""# Light-novel translation Subagent task

Recommended Antigravity model: `{model}`

Read only the files listed in `pending_files` in `novel_subagent_manifest.json` under
`novel_units/` and write its translation with the same filename under
`translated_novel/`. Preserve image markers and paragraph boundaries, and do
not add commentary or Markdown fences. If the model refuses a unit or inserts
a safety disclaimer, do not write that refusal as its translation; report the
blocked unit instead. Use `metadata_translation_prompt.md`
to create the translated metadata JSON as well. Authors and publishers must
remain byte-for-byte unchanged. Do not call an API or modify source files.
Files listed in `completed_files` are checkpoints; do not overwrite them unless
validation reports them as invalid.
""",
            encoding="utf-8",
        )
        logger.info(
            f"已生成 {len(content_units)} 个小说翻译单元和 Subagent 提示词："
            f"{output_dir / 'novel_subagent_prompt.md'}"
        )
        return 0
    except Exception as exc:
        logger.error(f"Could not prepare novel Subagent task: {exc}")
        return 1


def translate_novel_validate_command(args):
    """Validate novel text and metadata written by the Subagent."""
    from pdf2epub.subagent_workflow import detect_refusal
    from pdf2epub.html_translation.epub_parser import EPUBParser
    from pdf2epub.html_translation.novel_extractor import NovelExtractor
    from pdf2epub.html_translation.builder import HTMLEpubPipeline

    config = load_config(args.config)
    book_title = config.get("title")
    if not book_title:
        logger.error("No title found in config.yaml")
        return 1
    output_dir = Path("output") / book_title
    manifest_path = output_dir / "novel_subagent_manifest.json"
    epub_path = output_dir / "input.epub"
    if not manifest_path.exists() or not epub_path.exists():
        logger.error("Novel Subagent manifest or input.epub is missing; run translate-novel first")
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        source_dir = output_dir / manifest["source_dir"]
        target_dir = output_dir / manifest["target_dir"]
        missing = [
            name for name in manifest.get("files", [])
            if not (source_dir / name).exists() or not (target_dir / name).exists()
            or not (target_dir / name).read_text(encoding="utf-8").strip()
        ]
        import hashlib
        refusal_files = []
        for name in manifest.get("files", []):
            if name in missing:
                continue
            source_text = (source_dir / name).read_text(encoding="utf-8")
            target_text = (target_dir / name).read_text(encoding="utf-8")
            refusal = detect_refusal(source_text, target_text)
            if refusal or "```" in target_text:
                refusal_files.append(
                    {
                        "file": name,
                        "reason": (
                            f"refusal/disclaimer detected: {refusal}"
                            if refusal
                            else "Markdown code fence is not allowed"
                        ),
                    }
                )
        valid_files = [
            name
            for name in manifest.get("files", [])
            if name not in missing and not any(item["file"] == name for item in refusal_files)
        ]
        source_sha256 = {
            name: hashlib.sha256((source_dir / name).read_bytes()).hexdigest()
            for name in manifest.get("files", [])
            if (source_dir / name).is_file()
        }
        metadata_report = HTMLEpubPipeline(
            epub_path, output_dir, config
        ).validate_translated_metadata()
        if missing:
            logger.error(f"Missing or empty novel translations: {missing[:10]}")
        if refusal_files:
            logger.error(f"Novel translations containing refusal/disclaimer text: {refusal_files[:10]}")
        if not metadata_report["valid"]:
            logger.error(f"Invalid novel metadata: {metadata_report['errors']}")
        if missing or refusal_files or not metadata_report["valid"]:
            (output_dir / "translate-novel_validation.json").write_text(
                json.dumps(
                    {
                        "task": "translate-novel",
                        "valid_files": valid_files,
                        "source_sha256": source_sha256,
                        "missing": missing,
                        "invalid": refusal_files,
                        "safety_blocked": [item["file"] for item in refusal_files],
                        "metadata": metadata_report,
                        "all_passed": not missing and not refusal_files and metadata_report["valid"],
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
                encoding="utf-8",
            )
            return 1
        (output_dir / "translate-novel_validation.json").write_text(
            json.dumps(
                {
                    "task": "translate-novel",
                    "valid_files": valid_files,
                    "source_sha256": source_sha256,
                    "missing": [],
                    "invalid": [],
                    "safety_blocked": [],
                    "metadata": metadata_report,
                    "all_passed": True,
                },
                ensure_ascii=False,
                indent=2,
            ),
            encoding="utf-8",
        )
        logger.success(f"Novel Subagent output validated: {len(manifest.get('files', []))} files")
        return 0
    except Exception as exc:
        logger.error(f"Novel validation failed: {exc}")
        return 1


def build_novel_epub_command(args):
    """Handle the build-novel-epub subcommand (rebuild EPUB from translated novel text)."""
    import json
    from pathlib import Path
    from pdf2epub.html_translation.epub_parser import EPUBParser
    from pdf2epub.html_translation.novel_extractor import NovelExtractor
    from pdf2epub.html_translation.builder import BuildConfig, HTMLEpubBuilder, sanitize_filename

    config = load_config(args.config)
    book_title = config.get("title")

    if not book_title:
        logger.error("No title found in config")
        return 1

    configure_logging(book_title, "build-novel-epub")

    output_dir = Path("output") / book_title
    epub_path = output_dir / "input.epub"

    if not epub_path.exists():
        logger.error(f"Input EPUB not found: {epub_path}")
        return 1

    if translate_novel_validate_command(args) != 0:
        logger.error("Refusing to build novel EPUB before Subagent validation")
        return 1

    try:
        parser_obj = EPUBParser(str(epub_path))
        units = NovelExtractor(parser_obj).extract_all(output_dir / "novel_units")

        translated_dir = output_dir / "translated_novel"
        xhtml_dir = output_dir / "final_xhtml"
        xhtml_dir.mkdir(parents=True, exist_ok=True)

        # Validation above guarantees that every content unit has a Subagent
        # output; this conversion therefore never silently falls back to source.
        _convert_txt_to_xhtml(units, translated_dir, xhtml_dir, parser_obj)

        translated_count = sum(1 for u in units if u.has_content and (translated_dir / u.text_path.name).exists())
        total_content = sum(1 for u in units if u.has_content)
        logger.info(f"Translated {translated_count}/{total_content} content units")

        metadata_path = output_dir / "translated_metadata.json"
        translated_metadata = None
        if metadata_path.exists():
            translated_metadata = json.loads(metadata_path.read_text(encoding='utf-8'))

        if translated_metadata and translated_metadata.get('translated_title'):
            safe_title = sanitize_filename(translated_metadata['translated_title'])
            output_epub = output_dir / f"{safe_title}.epub"
        else:
            output_epub = output_dir / f"{book_title}_translated.epub"

        build_config = BuildConfig(
            original_epub=epub_path,
            translated_dir=xhtml_dir,
            output_path=output_epub,
            book_title=book_title,
            translated_metadata=translated_metadata,
            epubcheck_mode=config.get("html_translation", {}).get(
                "epubcheck_mode", "warn"
            ),
            epubcheck_path=config.get("html_translation", {}).get(
                "epubcheck_path"
            ),
        )
        builder = HTMLEpubBuilder(build_config)
        builder.build()
        logger.success(f"Built EPUB: {output_epub}")
        return 0

    except Exception as e:
        logger.error(f"Build failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


def _convert_txt_to_xhtml(units, translated_dir, xhtml_dir, parser):
    """Restore translated novel text into the original XHTML structure."""
    import html
    import re
    from pathlib import Path

    from pdf2epub.html_translation.compressor import HTMLCompressor
    from pdf2epub.html_translation.novel_extractor import NovelExtractor

    IMAGE_PATTERN = r'\[Image:\s*([^\]]+)\]'

    def nonempty_lines(text):
        return [line.strip() for line in text.splitlines() if line.strip()]

    def normalize_alignment_text(text):
        return re.sub(r'\s+', '', html.unescape(text))

    def regroup_formatting_lines(aligned_pairs, compressed_lines, extractor):
        """Collapse XHTML formatting newlines back into compressor-sized units."""
        grouped_pairs = []
        pair_idx = 0

        for compressed_line in compressed_lines:
            wrapped = (
                '<html xmlns="http://www.w3.org/1999/xhtml">'
                f'<body><p>{compressed_line}</p></body></html>'
            )
            expected_text, _ = extractor._convert_xhtml_to_text(wrapped)
            expected = normalize_alignment_text(expected_text)
            source_parts = []
            translated_parts = []
            accumulated = ''

            while pair_idx < len(aligned_pairs) and len(accumulated) < len(expected):
                source_line, translated_line = aligned_pairs[pair_idx]
                candidate = accumulated + normalize_alignment_text(source_line)
                if not expected.startswith(candidate):
                    break
                source_parts.append(source_line)
                translated_parts.append(translated_line)
                accumulated = candidate
                pair_idx += 1

            if not source_parts or accumulated != expected:
                return None

            grouped_pairs.append((''.join(source_parts), ''.join(translated_parts)))

        if pair_idx != len(aligned_pairs):
            return None
        return grouped_pairs

    def prepare_structured_line(source_line, translated_line, compressed_line, compressor):
        """Retain inline tag topology while inserting a plain-text translation."""
        source_images = re.findall(IMAGE_PATTERN, source_line)
        translated_images = re.findall(IMAGE_PATTERN, translated_line)
        if source_images != translated_images:
            raise ValueError(
                "Inline image placeholders changed during translation: "
                f"source={source_images!r}, translated={translated_images!r}"
            )

        if '<' not in compressed_line:
            return html.escape(translated_line, quote=False)

        # Novel translation intentionally uses plain text rather than exposing
        # inline markup to the model. Reuse the compressor's original tag
        # skeleton, clear only its source-language text nodes, and let
        # decompress() restore every recorded attribute.
        fragment = compressor._parse_fragment(compressed_line)
        for element in fragment.iter():
            element.text = None
            if element is not fragment:
                element.tail = None

        if not source_images:
            fragment.text = translated_line
            return compressor._serialize_fragment(fragment)

        image_elements = [
            element
            for element in fragment.iter()
            if element is not fragment
            and isinstance(element.tag, str)
            and element.tag.rsplit('}', 1)[-1].lower() in {'img', 'image'}
        ]
        if len(image_elements) != len(source_images):
            raise ValueError(
                "Cannot align inline image placeholders with original XHTML: "
                f"placeholders={len(source_images)}, elements={len(image_elements)}"
            )

        translated_parts = re.split(IMAGE_PATTERN, translated_line)
        text_parts = translated_parts[::2]
        fragment.text = text_parts[0]
        for image_element, trailing_text in zip(image_elements, text_parts[1:]):
            image_element.tail = trailing_text
        return compressor._serialize_fragment(fragment)

    css_content = ""
    for css_item in getattr(parser, 'resources', {}).get('css', []):
        content = css_item.get('content', b'')
        if isinstance(content, bytes):
            content = content.decode('utf-8')
        css_content += content + "\n"

    compressor = HTMLCompressor()
    extractor = NovelExtractor(parser)
    xhtml_dir.mkdir(parents=True, exist_ok=True)

    for unit in units:
        if not unit.text_path:
            continue

        # Skip image-only pages — let HTMLEpubBuilder preserve original XHTML
        if not unit.has_content:
            continue

        txt_path = translated_dir / unit.text_path.name
        if not txt_path.exists():
            continue

        if not unit.source_href:
            raise ValueError(f"Missing source XHTML href for {unit.file_name}")

        raw_xhtml = parser.get_raw_content(unit.source_href)
        if isinstance(raw_xhtml, bytes):
            raw_xhtml = raw_xhtml.decode('utf-8')

        source_text = unit.text_path.read_text(encoding='utf-8')
        translated_text = txt_path.read_text(encoding='utf-8')
        source_lines = nonempty_lines(source_text)
        translated_lines = nonempty_lines(translated_text)
        if len(source_lines) != len(translated_lines):
            raise ValueError(
                f"Novel line count mismatch for {unit.file_name}: "
                f"source={len(source_lines)}, translated={len(translated_lines)}"
            )

        compressed_text, mapping = compressor.compress(
            raw_xhtml,
            author_css=css_content,
        )
        compressed_lines = nonempty_lines(compressed_text)

        aligned_pairs = [
            (source_line, translated_line)
            for source_line, translated_line in zip(source_lines, translated_lines)
            if not re.fullmatch(IMAGE_PATTERN, source_line)
        ]
        if len(aligned_pairs) != len(compressed_lines):
            regrouped_pairs = regroup_formatting_lines(
                aligned_pairs,
                compressed_lines,
                extractor,
            )
            if regrouped_pairs is None:
                raise ValueError(
                    f"Novel structure mapping mismatch for {unit.file_name}: "
                    f"translated_units={len(aligned_pairs)}, "
                    f"original_xhtml_units={len(compressed_lines)}"
                )
            aligned_pairs = regrouped_pairs

        prepared_lines = []
        for (source_line, translated_line), compressed_line in zip(
            aligned_pairs,
            compressed_lines,
        ):
            prepared_lines.append(
                prepare_structured_line(
                    source_line,
                    translated_line,
                    compressed_line,
                    compressor,
                )
            )

        xhtml = compressor.decompress('\n'.join(prepared_lines), mapping)

        # Use the original XHTML filename
        if unit.source_href:
            out_name = Path(unit.source_href).name
        else:
            out_name = f"{unit.file_name}.xhtml"

        (xhtml_dir / out_name).write_text(xhtml, encoding='utf-8')


def build_html_epub_command(args):
    """Handle the build-html-epub subcommand (rebuild EPUB with translated HTML)."""
    from pathlib import Path
    from pdf2epub.html_translation import HTMLEpubPipeline

    # Load configuration
    config = load_config(args.config)
    book_title = config.get("title")

    epub_path = resolve_book_input_path(
        getattr(args, "input", None),
        config_value=config.get("input_epub"),
        config_path=args.config,
        output_dir=Path("output") / book_title if book_title else None,
        extensions=(".epub", ".azw3", ".mobi"),
        output_names=("input.epub", "original.epub"),
    )

    # Auto-infer book_title from metadata or filename if missing
    if not book_title:
        if epub_path and epub_path.exists():
            try:
                from pdf2epub.html_translation.epub_parser import EPUBParser
                parser = EPUBParser(epub_path)
                meta = parser.get_metadata()
                book_title = meta.get("title") or epub_path.stem
            except Exception:
                book_title = epub_path.stem
            logger.info(f"Auto-inferred book title from input file: {book_title}")
        else:
            logger.error("No title found in config and no input file to infer from.")
            return 1

    # Configure file logging
    configure_logging(book_title, "build-html-epub")

    # Setup paths
    output_dir = Path("output") / book_title

    # If no epub specified, look for original epub in output dir
    if not epub_path.exists():
        epub_path = resolve_book_input_path(
            getattr(args, "input", None),
            config_value=config.get("input_epub"),
            config_path=args.config,
            output_dir=output_dir,
            extensions=(".epub", ".azw3", ".mobi"),
            output_names=("input.epub", "original.epub"),
        )

    if epub_path is None or not epub_path.exists():
        logger.error("Input file not found. Specify input_epub in config or place file in input/.")
        return 1

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


def translate_command(args):
    """Prepare a local Markdown hand-off for a translation Subagent."""
    return _prepare_pdf_markdown_task(args, "translate")


def translate_validate_command(args):
    return _validate_pdf_markdown_task(args, "translate")


def translate_arxiv_command(args):
    """Materialize a TeX project and prepare a Subagent translation task."""
    from pdf2epub.subagent_workflow import estimate_tokens, resolve_subagent_model

    import shutil
    from pdf2epub.tex_translation.arxiv import ArxivSourceResolver, slugify_source_id
    from pdf2epub.tex_translation.document import discover_main_tex, scan_project

    config = load_config(args.config)
    source_language = args.source_language or config.get("tex_translation", {}).get(
        "source_language", "English"
    )
    target_language = args.target_language or config.get("tex_translation", {}).get(
        "target_language", "Simplified Chinese"
    )
    model = resolve_subagent_model(config, "translate-arxiv")
    resolver = ArxivSourceResolver()
    source_id = resolver.source_id(args.source)
    run_dir = Path(args.output_dir) if args.output_dir else Path("output") / "arxiv" / slugify_source_id(source_id)
    run_dir = run_dir.resolve()
    source_dir = run_dir / "source"
    project_dir = run_dir / "project"
    control_dir = run_dir / ".pdf2epub"
    previous_manifest = {}
    previous_manifest_path = control_dir / "tex_subagent_manifest.json"
    if getattr(args, "resume", False) and previous_manifest_path.is_file():
        try:
            previous_manifest = json.loads(
                previous_manifest_path.read_text(encoding="utf-8")
            )
        except (OSError, json.JSONDecodeError):
            previous_manifest = {}
    if not isinstance(previous_manifest, dict):
        previous_manifest = {}
    previous_manifest_units = previous_manifest.get("units", [])
    previous_units = {
        entry.get("id"): entry
        for entry in previous_manifest_units
        if isinstance(entry, dict) and entry.get("id")
    } if isinstance(previous_manifest_units, list) else {}
    previous_validated_ids = previous_manifest.get("validated_units", [])
    previously_validated = (
        set(previous_validated_ids)
        if isinstance(previous_validated_ids, list)
        else set()
    )
    try:
        resolved = resolver.materialize(args.source, source_dir)
        main_tex = discover_main_tex(source_dir, args.main_tex or resolved.suggested_main_tex)
        document = scan_project(
            source_dir,
            main_tex,
            unit_chars=args.unit_chars or config.get("tex_translation", {}).get("unit_chars", 12_000),
            target_language=target_language,
        )
        unit_chars = args.unit_chars or config.get("tex_translation", {}).get("unit_chars", 12_000)
        shutil.copytree(source_dir, project_dir, dirs_exist_ok=True)
        # Materialize the normalized source snapshot (including CJK support
        # injected by scan_project) into the editable project.  Copying the
        # raw archive alone would make a Chinese hand-off fail at XeLaTeX.
        for relative_path, source_text in document.sources.items():
            target_path = (project_dir / relative_path).resolve()
            target_path.relative_to(project_dir.resolve())
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(source_text, encoding="utf-8")
        source_units_dir = run_dir / "tex_units"
        translated_units_dir = run_dir / "translated_tex_units"
        source_units_dir.mkdir(parents=True, exist_ok=True)
        translated_units_dir.mkdir(parents=True, exist_ok=True)
        for unit in document.units:
            source_unit_path = source_units_dir / f"{unit.id}.md"
            source_unit_path.write_text(unit.source_text, encoding="utf-8")

        unit_entries = []
        for unit in document.units:
            target_name = f"{unit.id}.md"
            entry = unit.manifest_entry()
            entry.update({
                "source_file": f"tex_units/{unit.id}.md",
                "target_file": f"translated_tex_units/{target_name}",
                "size_bytes": len(unit.source_text.encode("utf-8")),
                "line_count": len(unit.source_text.splitlines()),
                "estimated_tokens": estimate_tokens(unit.source_text),
            })
            unit_entries.append(entry)
        completed_units = [
            entry["id"] for entry in unit_entries
            if (translated_units_dir / Path(entry["target_file"]).name).is_file()
            and (translated_units_dir / Path(entry["target_file"]).name).read_text(encoding="utf-8").strip()
            and entry["id"] in previously_validated
            and previous_units.get(entry["id"], {}).get("source_sha256")
            == entry.get("source_sha256")
        ]
        manifest = {
            "schema_version": 1,
            "workflow": "antigravity-subagent",
            "task": "translate-arxiv",
            "source_language": source_language,
            "target_language": target_language,
            "model": model,
            "source_dir": "source",
            "target_dir": "translated_tex_units",
            "project_dir": "project",
            "main_tex": document.main_tex,
            "unit_chars": unit_chars,
            "units": unit_entries,
            "resume": getattr(args, "resume", False),
            "completed_units": completed_units,
            "pending_units": [
                entry["id"] for entry in unit_entries if entry["id"] not in completed_units
            ],
        }
        control_dir.mkdir(parents=True, exist_ok=True)
        (control_dir / "tex_subagent_manifest.json").write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        (control_dir / "tex_subagent_prompt.md").write_text(
            f"""# TeX translation Subagent task

Recommended Antigravity model: `{model}`

Translate only the units listed in `pending_units` in
`tex_subagent_manifest.json` from {source_language} to {target_language}.
Read each `source_file` and write the complete translation to its corresponding
`target_file`. Preserve LaTeX commands, labels, references, formulas, and
document structure. Do not add Markdown fences or commentary. Files listed in
`completed_units` are checkpoints and must not be overwritten unless validation
reports them as invalid. If the model refuses a unit or inserts a safety
disclaimer, do not write that refusal as its translation; report the blocked
unit instead. Do not call an API, modify `../source/`, or edit
`../project/` directly; the local validator reconstructs it from the unit files.
""",
            encoding="utf-8",
        )
        logger.success(f"Prepared TeX Subagent task: {control_dir / 'tex_subagent_prompt.md'}")
        logger.info("完成后运行 pdf2epub translate-arxiv-validate --output-dir <run_dir>")
        return 0
    except Exception as exc:
        logger.error(f"Could not prepare TeX Subagent task: {exc}")
        return 1


def translate_arxiv_validate_command(args):
    """Rebuild and compile TeX from validated Subagent unit files locally."""
    import hashlib

    from pdf2epub.subagent_workflow import detect_refusal
    from pdf2epub.tex_translation.compiler import TexCompiler
    from pdf2epub.tex_translation.document import scan_project

    if not args.output_dir:
        logger.error("--output-dir is required for translate-arxiv-validate")
        return 1
    run_dir = Path(args.output_dir).resolve()
    manifest_path = run_dir / ".pdf2epub" / "tex_subagent_manifest.json"
    if not manifest_path.exists():
        logger.error(f"TeX Subagent manifest not found: {manifest_path}")
        return 1
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if not all("source_file" in unit and "target_file" in unit for unit in manifest.get("units", [])):
            logger.error(
                "This TeX manifest uses the old project-editing format; "
                "rerun translate-arxiv to prepare resumable unit files."
            )
            return 1
        source_dir = run_dir / "source"
        document = scan_project(
            source_dir,
            manifest["main_tex"],
            unit_chars=manifest.get("unit_chars", 12_000),
            target_language=manifest.get("target_language", "Simplified Chinese"),
        )
        document_units = {unit.id: unit for unit in document.units}
        translated = {}
        missing_units = []
        invalid_units = []
        safety_blocked_units = []
        completed_units = []
        run_root = run_dir.resolve()

        def safe_run_path(relative_name: str) -> Path:
            target = (run_root / relative_name).resolve()
            target.relative_to(run_root)
            return target

        for entry in manifest.get("units", []):
            unit_id = entry.get("id", "unknown")
            if unit_id not in document_units:
                invalid_units.append(f"{unit_id}: no matching source unit")
                continue
            source_path = safe_run_path(entry["source_file"])
            target_path = safe_run_path(entry["target_file"])
            if not source_path.is_file() or not target_path.is_file():
                missing_units.append(unit_id)
                continue
            source_text = source_path.read_text(encoding="utf-8")
            target_text = target_path.read_text(encoding="utf-8")
            if hashlib.sha256(source_text.encode("utf-8")).hexdigest() != entry.get("source_sha256"):
                invalid_units.append(f"{unit_id}: source unit changed")
                continue
            if not target_text.strip():
                invalid_units.append(f"{unit_id}: target is empty")
                continue
            refusal = detect_refusal(source_text, target_text)
            if refusal:
                invalid_units.append(f"{unit_id}: refusal/disclaimer detected ({refusal})")
                safety_blocked_units.append(unit_id)
                continue
            if "```" in target_text:
                invalid_units.append(f"{unit_id}: Markdown fence is not allowed")
                continue
            translated[unit_id] = target_text
            completed_units.append(unit_id)

        manifest["completed_units"] = completed_units
        manifest["pending_units"] = [
            entry.get("id") for entry in manifest.get("units", [])
            if entry.get("id") not in completed_units
        ]
        # A non-empty TeX unit is only a candidate until the reconstructed
        # project compiles successfully.  Keep a separate durable checkpoint
        # so --resume never trusts a truncated or compile-breaking file.
        manifest["validated_units"] = []
        manifest["safety_blocked_units"] = safety_blocked_units
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        if missing_units or invalid_units:
            if missing_units:
                logger.error(f"Missing translated TeX units: {missing_units[:10]}")
            if invalid_units:
                logger.error(f"Invalid translated TeX units: {invalid_units[:10]}")
            return 1

        project_dir = run_dir / "project"
        project_dir.mkdir(parents=True, exist_ok=True)
        for relative_path, source_text in document.render(translated).items():
            target_path = (project_dir / relative_path).resolve()
            target_path.relative_to(project_dir.resolve())
            target_path.parent.mkdir(parents=True, exist_ok=True)
            target_path.write_text(source_text, encoding="utf-8")
        result = TexCompiler(timeout_seconds=args.compile_timeout or 180).compile(
            project_dir,
            manifest["main_tex"],
            run_dir / ".pdf2epub" / "logs" / "subagent_compile.log",
        )
        if not result.success:
            logger.error(f"TeX validation failed:\n{result.tail()}")
            return 1
        manifest["validated_units"] = completed_units
        manifest_path.write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        logger.success(f"TeX Subagent output compiled successfully: {result.pdf_path}")
        return 0
    except Exception as exc:
        logger.error(f"TeX validation failed: {exc}")
        return 1



def main():
    """Main CLI entrypoint."""
    parser = argparse.ArgumentParser(
        description="PDF to EPUB markdown processor",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
===============================================================================
RECOMMENDED WORKFLOW / 推荐工作流 (uses toc_tree.json):
===============================================================================

  # Complete pipeline for a PDF book:
  pdf2epub ocr-pages -i mybook.pdf   # Page-level OCR
  pdf2epub refine-prepare            # Prepare Subagent TOC analysis
  # Antigravity Subagent writes toc_tree.json
  pdf2epub refine-local              # Validate TOC and merge OCR pages locally
  pdf2epub polish                    # Prepare Subagent polishing task
  # Antigravity Subagent writes polished_markdown/*.md
  pdf2epub polish-validate
  pdf2epub build-epub                # Generate EPUB from validated output

  # With translation:
  pdf2epub ocr-pages -i mybook.pdf
  pdf2epub refine-prepare
  # Antigravity Subagent writes toc_tree.json
  pdf2epub refine-local
  pdf2epub polish --content-type japanese
  # Antigravity Subagent writes polished_markdown/*.md
  pdf2epub polish-validate
  pdf2epub translate --target-language Chinese
  # Antigravity Subagent writes translated/*.md
  pdf2epub translate-validate
  pdf2epub build-epub --translated

  # EPUB Translation (preserves original formatting):
  pdf2epub html-prepare -i mybook.epub       # Extract locally
  # Antigravity Subagent writes translated_compressed/* and translated_metadata.json
  pdf2epub html-validate
  pdf2epub build-html-epub                    # Build translated EPUB

  # Novel Translation (text mode for light novels):
  pdf2epub translate-novel -i mybook.epub     # Prepare Subagent task
  # Subagent writes translated_novel/* and translated_metadata.json
  pdf2epub translate-novel-validate
  pdf2epub build-novel-epub                   # Rebuild EPUB locally

  # arXiv/TeX Translation (compile-gated whole mode):
  pdf2epub translate-arxiv 2503.01800
  pdf2epub translate-arxiv ./latex-source --main-tex paper.tex

===============================================================================
        """
    )
    
    # Global arguments
    parser.add_argument("-c", "--config", default="config.yaml", 
                        help="Path to config file")
    
    # Create subcommands
    subparsers = parser.add_subparsers(dest="command", help="Command to run")
    subparsers.required = True
    
    # Breakdown subcommand (DEPRECATED)

    # OCR Pages subcommand (new workflow)
    ocr_pages_parser = subparsers.add_parser(
        "ocr-pages",
        help="Page-level OCR (for refined breakdown workflow)",
        description="Extract text from each PDF page individually for refined breakdown"
    )
    ocr_pages_parser.add_argument(
        "-i", "--input",
        help="Path to PDF file (default: auto-detect from output directory)"
    )
    ocr_pages_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previous progress"
    )
    ocr_pages_parser.add_argument(
        "--start-page",
        type=int,
        help="First page to process (default: 1)"
    )
    ocr_pages_parser.add_argument(
        "--end-page",
        type=int,
        help="Last page to process (default: all pages)"
    )
    ocr_pages_parser.add_argument(
        "--max-workers",
        type=int,
        default=None,
        help="Number of parallel OCR workers (default: from config or 5)"
    )
    ocr_pages_parser.set_defaults(func=ocr_pages_command)

    # Refine subcommand (refined breakdown with boundary verification)
    refine_parser = subparsers.add_parser(
        "refine",
        help="Prepare PDF structure analysis for an Antigravity Subagent",
        description=(
            "Alias for refine-prepare. Structure analysis is performed by a "
            "workspace Subagent; the local program never calls a translation API."
        )
    )
    refine_parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum tokens per unit (default: from config or 8000)"
    )
    refine_parser.set_defaults(func=refine_command)

    # Antigravity Subagent refine workflow (no API calls in these commands)
    refine_prepare_parser = subparsers.add_parser(
        "refine-prepare",
        help="Prepare PDF TOC analysis for an Antigravity subagent",
        description=(
            "Write a prompt and manifest for a workspace subagent to inspect "
            "OCR pages and produce toc_tree.json."
        ),
    )
    refine_prepare_parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum tokens per unit (default: from config or 8000)",
    )
    refine_prepare_parser.set_defaults(func=refine_prepare_command)

    refine_local_parser = subparsers.add_parser(
        "refine-local",
        help="Generate PDF work units from a subagent TOC (no API calls)",
        description=(
            "Validate toc_tree.json and deterministically merge OCR pages into "
            "ocr_markdown work units."
        ),
    )
    refine_local_parser.add_argument(
        "--resume",
        action="store_true",
        help="Resume from previously generated local units",
    )
    refine_local_parser.add_argument(
        "--max-tokens",
        type=int,
        default=None,
        help="Maximum tokens per unit (default: from config or 8000)",
    )
    refine_local_parser.set_defaults(func=refine_local_command)
    
    # Polish subcommand
    polish_parser = subparsers.add_parser(
        "polish",
        help="Prepare OCR Markdown for a polishing Subagent",
        description="Create a local Subagent hand-off; does not call an API",
    )
    polish_parser.add_argument(
        "--content-type",
        choices=["academic", "japanese", "general", "auto"],
        default="auto",
        help="Type of content to polish (default: auto-detect)"
    )
    polish_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing non-empty Subagent outputs and prepare only pending files",
    )
    polish_parser.set_defaults(func=polish_command)

    polish_validate_parser = subparsers.add_parser(
        "polish-validate",
        help="Validate and stage Subagent polishing output (no API calls)",
    )
    polish_validate_parser.set_defaults(func=polish_validate_command)

    # Translate subcommand
    translate_parser = subparsers.add_parser(
        "translate",
        help="Prepare polished Markdown for a translation Subagent",
        description="Create a local Subagent hand-off; does not call an API",
    )
    translate_parser.add_argument(
        "--source-language",
        help="Source language (default: from config or English)"
    )
    translate_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)"
    )
    translate_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing non-empty Subagent outputs and prepare only pending files",
    )
    translate_parser.set_defaults(func=translate_command)

    translate_validate_parser = subparsers.add_parser(
        "translate-validate",
        help="Validate and stage Subagent translation output (no API calls)",
    )
    translate_validate_parser.set_defaults(func=translate_validate_command)

    # arXiv / TeX whole-mode translation
    translate_arxiv_parser = subparsers.add_parser(
        "translate-arxiv",
        help="Prepare an arXiv/local TeX project for a translation Subagent",
        description=(
            "Download or copy a TeX source tree and create a local Subagent "
            "hand-off; no translation API is called."
        ),
    )
    translate_arxiv_parser.add_argument(
        "source",
        help="arXiv ID/URL, local source directory, archive, or main .tex file",
    )
    translate_arxiv_parser.add_argument(
        "--main-tex",
        help="Compilation entry point relative to the source root (auto-detected)",
    )
    translate_arxiv_parser.add_argument(
        "--output-dir",
        help="Run directory (default: output/arxiv/<source-id>)",
    )
    translate_arxiv_parser.add_argument(
        "--source-language",
        help="Source language (default: English)",
    )
    translate_arxiv_parser.add_argument(
        "--target-language",
        help="Target language (default: Simplified Chinese)",
    )
    translate_arxiv_parser.add_argument(
        "--unit-chars",
        type=int,
        help="Approximate characters per TeX transaction (default: 12000)",
    )
    translate_arxiv_parser.add_argument(
        "--compile-timeout",
        type=int,
        help="Seconds allowed for each full-project compile (default: 180)",
    )
    translate_arxiv_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing translated TeX unit files and prepare only pending units",
    )
    translate_arxiv_parser.set_defaults(func=translate_arxiv_command)

    translate_arxiv_validate_parser = subparsers.add_parser(
        "translate-arxiv-validate",
        help="Compile a Subagent-edited TeX project locally (no API calls)",
    )
    translate_arxiv_validate_parser.add_argument(
        "--output-dir", required=True, help="Run directory created by translate-arxiv"
    )
    translate_arxiv_validate_parser.add_argument(
        "--compile-timeout", type=int, help="XeLaTeX timeout in seconds (default: 180)"
    )
    translate_arxiv_validate_parser.set_defaults(func=translate_arxiv_validate_command)

    # Entity extraction subcommand
    entity_parser = subparsers.add_parser(
        "extract-entities",
        help="Prepare entity extraction for a translation Subagent",
        description=(
            "Create a local Subagent hand-off for extracting characters, "
            "places, and terms; no model API is called."
        )
    )
    entity_parser.add_argument(
        "-i", "--input",
        default=None,  # Will be resolved to book_folder/input.pdf in the command
        help="Path to input PDF file (default: output/<book_title>/input.pdf)"
    )
    entity_parser.add_argument(
        "--source-lang",
        default="Japanese",
        help="Source language (default: Japanese)"
    )
    entity_parser.add_argument(
        "--target-lang",
        default="Chinese",
        help="Target language for translation (default: Chinese)"
    )
    entity_parser.set_defaults(func=extract_entities_command)

    entity_validate_parser = subparsers.add_parser(
        "extract-entities-validate",
        help="Validate entity JSON written by a Subagent",
        description="Validate the optional translation entity hand-off locally.",
    )
    entity_validate_parser.set_defaults(func=extract_entities_validate_command)
    
    # Build EPUB subcommand (toc_tree.json driven - new approach)
    build_epub_parser = subparsers.add_parser(
        "build-epub",
        help="Build EPUB from toc_tree.json structure (recommended)",
        description="Create EPUB file using toc_tree.json as the structure authority"
    )
    build_epub_parser.add_argument(
        "--translated",
        action="store_true",
        help="Build EPUB from translated markdown instead of polished"
    )
    build_epub_parser.add_argument(
        "--cover",
        help="Path to cover image file"
    )
    build_epub_parser.set_defaults(func=build_epub_command)

    # HTML Prepare subcommand (pure local extraction and compression)
    html_prepare_parser = subparsers.add_parser(
        "html-prepare",
        help="Extract and prepare compressed HTML units from EPUB (pure local, no LLM)",
        description="Extract XHTML from EPUB and compress into units for translation."
    )
    html_prepare_parser.add_argument(
        "-i", "--input",
        help="Input file: EPUB, AZW3, or MOBI (default: output/<book_title>/input.epub)"
    )
    html_prepare_parser.add_argument(
        "--source-language",
        help="Source language (default: from EPUB metadata or Japanese)"
    )
    html_prepare_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)"
    )
    html_prepare_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing translated HTML units and prepare only pending files",
    )
    html_prepare_parser.set_defaults(func=html_prepare_command)

    # HTML Validate subcommand (pure local validation)
    html_validate_parser = subparsers.add_parser(
        "html-validate",
        help="Validate translated compressed units against originals (pure local, no LLM)",
        description="Validate that all units are translated with matching line counts and intact HTML tag structures."
    )
    html_validate_parser.add_argument(
        "-i", "--input",
        help="Input file: EPUB, AZW3, or MOBI (default: output/<book_title>/input.epub)"
    )
    html_validate_parser.set_defaults(func=html_validate_command)

    # Build HTML EPUB subcommand (rebuild EPUB with translated HTML)
    build_html_epub_parser = subparsers.add_parser(
        "build-html-epub",
        help="Build EPUB from translated HTML (preserves original formatting)",
        description="Rebuild EPUB by replacing XHTML content with translations. AZW3/MOBI auto-converted."
    )
    build_html_epub_parser.add_argument(
        "-i", "--input",
        help="Input file: EPUB, AZW3, or MOBI (default: output/<book_title>/input.epub)"
    )
    build_html_epub_parser.add_argument(
        "-o", "--output",
        help="Path to output EPUB file (default: <book_title>_translated.epub)"
    )
    build_html_epub_parser.add_argument(
        "--allow-partial",
        action="store_true",
        help="Build despite missing/invalid units or metadata (unsafe; for previews only)",
    )
    build_html_epub_parser.set_defaults(func=build_html_epub_command)

    # Novel Translation subcommand (text-mode for light novels)
    translate_novel_parser = subparsers.add_parser(
        "translate-novel",
        help="Prepare light-novel EPUB for a translation Subagent",
        description="Extract light-novel text and create a local Subagent hand-off."
    )
    translate_novel_parser.add_argument(
        "-i", "--input",
        help="Input EPUB file"
    )
    translate_novel_parser.add_argument(
        "--source-language",
        help="Source language (default: Japanese)"
    )
    translate_novel_parser.add_argument(
        "--target-language",
        help="Target language (default: from config or Chinese)"
    )
    translate_novel_parser.add_argument(
        "--resume",
        action="store_true",
        help="Keep existing translated novel units and prepare only pending files",
    )
    translate_novel_parser.set_defaults(func=translate_novel_command)

    translate_novel_validate_parser = subparsers.add_parser(
        "translate-novel-validate",
        help="Validate Subagent light-novel output (no API calls)",
    )
    translate_novel_validate_parser.set_defaults(func=translate_novel_validate_command)

    # Build Novel EPUB subcommand (rebuild from translated text, no re-translation)
    build_novel_epub_parser = subparsers.add_parser(
        "build-novel-epub",
        help="Build EPUB from validated Subagent novel text",
        description="Rebuild EPUB only after translate-novel-validate succeeds."
    )
    build_novel_epub_parser.set_defaults(func=build_novel_epub_command)

    # Parse arguments
    args = parser.parse_args()
    
    # Execute the appropriate command
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

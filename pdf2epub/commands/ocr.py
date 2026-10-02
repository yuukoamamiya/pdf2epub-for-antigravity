"""Page-level OCR command handler.

OCR is the only workflow stage allowed to call the configured OCR service.
"""

import json
import shutil
from pathlib import Path

from loguru import logger

from pdf2epub.commands.runtime import load_book_context
from pdf2epub.ocr_consensus import (
    auto_accepted_files,
    consensus_is_current,
    review_required_files,
    secondary_backend_name,
)
from pdf2epub.ocr_progress import assess_progress
from pdf2epub.utils.common import resolve_book_input_path
from pdf2epub.workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION, atomic_write_text, sha256_file


def _load_probe(output_dir: Path) -> dict:
    try:
        value = json.loads(
            (Path(output_dir) / "pdf_text_probe.json").read_text(encoding="utf-8")
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _require_complete_ocr_pages(output_dir: Path) -> bool:
    """Stop correction until the raw page-level OCR checkpoint is complete."""
    probe = _load_probe(output_dir)
    report = assess_progress(
        Path(output_dir) / "pages",
        expected_total_pages=probe.get("page_count"),
        expected_source_sha256=probe.get("source_sha256"),
        require_sidecars=True,
    )
    if report["ready"]:
        return True
    logger.error("OCR correction is blocked because raw OCR is incomplete")
    for error in report["errors"][:10]:
        logger.error(f"OCR readiness: {error}")
    return False


def _primary_backend(output_dir: Path) -> str:
    try:
        progress = json.loads(
            (Path(output_dir) / "pages" / "ocr_progress.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return ""
    return str(progress.get("backend") or "").strip().lower()


def _consensus_is_current_for_context(output_dir: Path, config: dict) -> bool:
    secondary = secondary_backend_name(config)
    if not secondary:
        return True
    return consensus_is_current(
        output_dir,
        config,
        primary_backend=_primary_backend(output_dir),
    )


def _write_auto_accepted_validation(output_dir: Path, files: list[str]) -> None:
    """Complete the correction checkpoint without a redundant Subagent pass."""
    source_dir = Path(output_dir) / "pages"
    target_dir = Path(output_dir) / "ocr_corrected_pages" / "validated"
    target_dir.mkdir(parents=True, exist_ok=True)
    source_hashes = {}
    target_hashes = {}
    for name in sorted(files):
        source = source_dir / name
        target = target_dir / name
        shutil.copy2(source, target)
        source_hashes[name] = sha256_file(source)
        target_hashes[name] = sha256_file(target)
    report = {
        "schema_version": MARKDOWN_VALIDATION_SCHEMA_VERSION,
        "task": "ocr-correct",
        "scope": "full",
        "total": len(files),
        "completed": len(files),
        "missing": [],
        "invalid": [],
        "review_required": [],
        "review_required_files": [],
        "retry_required": [],
        "retry_required_files": [],
        "human_review_required": [],
        "human_review_required_files": [],
        "valid_files": sorted(files),
        "source_sha256": source_hashes,
        "target_sha256": target_hashes,
        "auto_accepted_files": sorted(files),
        "all_passed": True,
        "files_checked": sorted(files),
    }
    atomic_write_text(
        Path(output_dir) / "ocr-correct_validation.json",
        json.dumps(report, ensure_ascii=False, indent=2),
    )


def ocr_pages_command(args):
    """Handle the ocr-pages subcommand (page-level OCR)."""
    from pdf2epub.ocr_pages import ocr_full_book_pagewise

    context = load_book_context(args, "ocr-pages")
    if context is None:
        return 1
    config = context.config
    book_title = context.book_title
    output_dir = context.output_dir

    # Find PDF
    pdf_path = resolve_book_input_path(
        args.input,
        config_value=config.get("input_pdf") or config.get("input"),
        config_path=context.config_path,
        output_dir=output_dir,
        extensions=(".pdf",),
        output_names=("input_original.pdf", "input.pdf"),
    )

    if not pdf_path.exists():
        logger.error(f"PDF not found: {pdf_path}")
        logger.info("Specify --input with the path to your PDF file")
        return 1

    # A searchable PDF is not automatically trustworthy: scanned books often
    # carry a hidden OCR text layer.  Only a conservative native-text probe may
    # bypass visual OCR; searchable-OCR and mixed PDFs stay on Chandra.
    from pdf2epub.pdf_text_probe import (
        extract_native_text_pages,
        probe_pdf_text_layer,
    )
    from pdf2epub.workflow_contracts import atomic_write_text

    try:
        probe = probe_pdf_text_layer(pdf_path)
        atomic_write_text(
            output_dir / "pdf_text_probe.json",
            json.dumps(probe, ensure_ascii=False, indent=2),
        )
    except Exception as exc:
        logger.warning(f"PDF text probe failed; continuing with visual OCR: {exc}")
        probe = {"recommendation": "ocr_required", "classification": "probe_failed"}

    if probe.get("recommendation") == "use_text_layer":
        output_dir.mkdir(parents=True, exist_ok=True)
        original_copy = output_dir / "input_original.pdf"
        processed_copy = output_dir / "input.pdf"
        from pdf2epub.workflow_contracts import sha256_file

        if original_copy.exists() and sha256_file(original_copy) != sha256_file(pdf_path):
            logger.error(
                "The configured input PDF differs from output/input_original.pdf; "
                "use a new output title or archive the old run before starting a new book"
            )
            return 1
        if not original_copy.exists():
            shutil.copy2(pdf_path, original_copy)
        if not processed_copy.exists():
            shutil.copy2(pdf_path, processed_copy)
        try:
            extract_native_text_pages(pdf_path, output_dir, probe)
        except Exception as exc:
            logger.error(f"Native PDF text extraction failed; refusing silent fallback: {exc}")
            return 1
        logger.success(
            "Detected a high-confidence native-text PDF; skipped visual OCR and wrote page text."
        )
        logger.info(f"Output: {output_dir / 'pages'}")
        logger.info("Next step: pdf2epub refine-prepare")
        return 0

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
        retry_pages = []
        raw_retry_pages = str(getattr(args, "retry_pages", "") or "").strip()
        if raw_retry_pages:
            retry_pages = sorted(
                {
                    int(value.strip())
                    for value in raw_retry_pages.split(",")
                    if value.strip()
                }
            )
        summary = ocr_full_book_pagewise(
            pdf_path=pdf_path,
            output_dir=output_dir,
            start_page=args.start_page or 1,
            end_page=args.end_page,
            backend=backend,
            api_key=api_key,
            base_url=base_url,
            resume=args.resume,
            config=config,
            max_workers=max_workers,
            allow_empty_pages=bool(getattr(args, "allow_empty_pages", False)),
            retry_pages=retry_pages,
        )

        if (
            summary.get("failed_pages")
            or summary.get("missing_pages")
            or summary.get("empty_pages")
            or summary.get("secondary_failed_pages")
        ):
            logger.error(
                "Page-level OCR is incomplete; fix the listed pages and rerun with --resume"
            )
            return 1

        logger.success(f"Page-level OCR complete!")
        logger.info(f"Output: {output_dir / 'pages'}")
        if summary.get("secondary_ocr_enabled"):
            logger.info(
                "Secondary OCR consensus complete; only pages listed in "
                "ocr_consensus.json need visual review."
            )
            logger.info("Next step: pdf2epub ocr-correct")
        else:
            logger.info(
                "Secondary OCR is disabled; visual OCR correction is skipped. "
                "Next step: pdf2epub refine-prepare"
            )
        return 0

    except Exception as e:
        logger.error(f"OCR failed: {e}")
        import traceback
        traceback.print_exc()
        return 1


def ocr_correct_command(args):
    """Prepare visual OCR correction for a workspace Subagent."""
    from pdf2epub.markdown_handoff import prepare_markdown_subagent
    from pdf2epub.ocr_correction import (
        DEFAULT_REVIEW_IMAGE_DPI,
        render_review_images,
        review_image_dir,
        review_record_dir,
    )
    from pdf2epub.subagent_runtime import resolve_subagent_model, write_worker_handoffs

    context = load_book_context(args, "ocr-correct")
    if context is None:
        return 1
    output_dir = context.output_dir
    probe = _load_probe(output_dir)
    if (
        probe.get("classification") == "native_text"
        and probe.get("recommendation") == "use_text_layer"
    ):
        logger.info(
            "This PDF uses a high-confidence native text layer; visual OCR correction is not applicable."
        )
        return 0
    if not _require_complete_ocr_pages(output_dir):
        return 1

    consensus_enabled = bool(secondary_backend_name(context.config))
    if not consensus_enabled:
        logger.info(
            "Secondary OCR is disabled; using the primary OCR pages without visual OCR correction."
        )
        return 0
    consensus_review_files = None
    if consensus_enabled:
        if not _consensus_is_current_for_context(output_dir, context.config):
            logger.error(
                "OCR consensus is missing or stale; rerun ocr-pages --resume before ocr-correct"
            )
            return 1
        consensus_review_files = review_required_files(output_dir)
        auto_files = auto_accepted_files(output_dir)
        from pdf2epub.ocr_correction import materialize_auto_accepted_pages

        materialize_auto_accepted_pages(output_dir, auto_files)
        if not consensus_review_files:
            _write_auto_accepted_validation(output_dir, auto_files)
            logger.success(
                "All pages agreed between primary and secondary OCR; visual Subagent review was skipped."
            )
            return 0

    correction_config = context.config.get("ocr_correction", {})
    if not isinstance(correction_config, dict):
        correction_config = {}
    dpi = getattr(args, "dpi", None) or correction_config.get(
        "review_dpi", DEFAULT_REVIEW_IMAGE_DPI
    )
    try:
        render_review_images(
            output_dir,
            dpi=dpi,
            resume=bool(getattr(args, "resume", False)),
        )
        rules = [
            "This is a visual OCR-correction task, not a translation or a prose-polishing task.",
            "For every assigned page_NNN.md, compare the OCR text with the matching page_NNN.png in the visual review directory.",
            "Correct only errors directly supported by the page image: confusable glyphs, dropped or duplicated characters, broken words, and clearly misrecognized punctuation or symbols.",
            "When the image is unclear or the correction is not certain, keep the OCR text unchanged. Do not infer wording from domain knowledge or neighboring pages.",
            "Keep the physical page boundary, filename, reading order, line order, and Markdown block structure. Do not merge pages, split pages, summarize, or add prose.",
            "Preserve Markdown heading markers and levels, image links and destinations, tables, formulas, footnote markers, list markers, citations, and page furniture exactly unless the page image proves that an OCR character in them is wrong.",
            "Do not remove headers, footers, printed page numbers, or repeated running titles; the later polish stage handles confirmed page furniture.",
            "Never translate, rewrite for style, normalize spelling, expand abbreviations, or silently repair content that is not visible on the page.",
        ]
        if consensus_enabled:
            rules.extend(
                [
                    "A local secondary OCR candidate is available for each assigned page. Use it only as comparison evidence; the page image is authoritative.",
                    "This hand-off contains only pages whose primary and secondary OCR outputs materially differ. Do not process or create review records for any other page.",
                ]
            )
        source_language = (
            (context.config.get("translation", {}) or {}).get(
                "source_language", "Original"
            )
            or "Original"
        )
        paths = prepare_markdown_subagent(
            output_dir,
            "ocr-correct",
            output_dir / "pages",
            output_dir / "ocr_corrected_pages",
            str(source_language),
            "Original",
            rules,
            config=context.config,
            resume=bool(getattr(args, "resume", False)),
            declared_files=consensus_review_files,
            secondary_source_dir=(
                output_dir / "ocr_secondary" if consensus_enabled else None
            ),
            visual_review_dir=review_image_dir(output_dir),
            review_output_dir=review_record_dir(output_dir),
        )
        handoffs = write_worker_handoffs(
            output_dir,
            paths["manifest"],
            paths["prompt"],
            handoff_dir_name="ocr-correct_worker_handoffs",
        )
    except Exception as exc:
        logger.error(f"Could not prepare OCR correction task: {exc}")
        return 1
    logger.success(f"Wrote Subagent prompt: {paths['prompt']}")
    logger.info(
        f"Recommended Antigravity model: {resolve_subagent_model(context.config, 'ocr-correct')}"
    )
    logger.warning(
        "请在 Antigravity 工作区打开 Subagent，按 ocr-correct_worker_handoffs/ 中各 manifest 的 assigned_files，"
        "对照对应页图写入 ocr_corrected_pages/*.md。完成后运行 ocr-correct-validate。"
    )
    if not handoffs:
        logger.warning(
            "No pending OCR-correction files were found; run validation to confirm the checkpoint."
        )
    return 0


def ocr_correct_validate_command(args):
    """Validate and stage the OCR-correction Subagent output."""
    from pdf2epub.markdown_subagent_validation import validate_markdown_subagent
    from pdf2epub.ocr_correction import (
        review_images_are_current,
        validate_ocr_correction_reviews,
    )
    from pdf2epub.workflow_contracts import atomic_write_text

    context = load_book_context(args, "ocr-correct-validate")
    if context is None:
        return 1
    output_dir = context.output_dir
    probe = _load_probe(output_dir)
    if (
        probe.get("classification") == "native_text"
        and probe.get("recommendation") == "use_text_layer"
    ):
        logger.info("Native text PDF: OCR correction validation is not applicable.")
        return 0
    if not _require_complete_ocr_pages(output_dir):
        return 1
    consensus_enabled = bool(secondary_backend_name(context.config))
    if not consensus_enabled:
        logger.info(
            "Secondary OCR is disabled; OCR correction validation is not applicable."
        )
        return 0
    consensus_review_files = None
    if consensus_enabled:
        if not _consensus_is_current_for_context(output_dir, context.config):
            logger.error(
                "OCR consensus is missing or stale; rerun ocr-pages --resume before validation"
            )
            return 1
        consensus_review_files = review_required_files(output_dir)
        from pdf2epub.ocr_correction import materialize_auto_accepted_pages

        materialize_auto_accepted_pages(output_dir, auto_accepted_files(output_dir))
    if consensus_review_files and not review_images_are_current(output_dir):
        logger.error(
            "OCR review images are missing or stale; rerun ocr-correct before validation"
        )
        return 1

    source_dir = output_dir / "pages"
    target_dir = output_dir / "ocr_corrected_pages"
    selected_file = getattr(args, "file", None)
    if selected_file:
        selected = Path(str(selected_file))
        if (
            selected.name != str(selected_file)
            or selected.suffix.lower() != ".md"
            or not (source_dir / selected.name).is_file()
        ):
            logger.error(f"Unknown OCR page file: {selected_file}")
            return 1
    report = validate_markdown_subagent(
        output_dir,
        "ocr-correct",
        source_dir,
        target_dir,
        # OCR correction may recover a heading/image/footnote line that the
        # raw OCR omitted.  The specialized review validator below enforces
        # structural non-loss while allowing such evidence-based additions.
        structural_patterns=(),
        selected_files=[selected_file] if selected_file else None,
        target_language=None,
    )
    review_selection = (
        [selected_file]
        if selected_file and selected_file in set(consensus_review_files or [])
        else []
        if selected_file
        else consensus_review_files
    )
    review_report = (
        validate_ocr_correction_reviews(
            output_dir,
            source_dir,
            target_dir,
            selected_files=review_selection,
        )
        if review_selection
        else {"valid": True, "files_checked": [], "errors": []}
    )
    report["ocr_correction_review"] = review_report
    if consensus_enabled:
        report["auto_accepted_files"] = auto_accepted_files(output_dir)
        report["visual_review_files"] = consensus_review_files or []
    if selected_file and review_report["valid"]:
        ledger_path = output_dir / "ocr-correct_file_validation.json"
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            ledger = {}
        records = ledger.get("files", {}) if isinstance(ledger, dict) else {}
        if not isinstance(records, dict):
            records = {}
        record = records.get(selected_file, {})
        if not isinstance(record, dict):
            record = {}
        record["ocr_correction_review"] = review_report
        record["valid"] = bool(record.get("valid"))
        records[selected_file] = record
        atomic_write_text(
            ledger_path,
            json.dumps(
                {
                    "schema_version": 2,
                    "task": "ocr-correct",
                    "scope": "file-checkpoints",
                    "files": records,
                },
                ensure_ascii=False,
                indent=2,
            ),
        )
    if not review_report["valid"]:
        invalid_files = set()
        for item in review_report["errors"]:
            name = str(item.get("file") or "")
            reason = str(item.get("reason") or "invalid OCR review record")
            report["invalid"].append({"file": name, "reason": reason})
            report["retry_required"].append(
                {
                    "file": name,
                    "kind": "ocr_review_invalid",
                    "action": "retry_subagent",
                    "reason": reason,
                }
            )
            if name:
                invalid_files.add(name)
                (target_dir / "validated" / name).unlink(missing_ok=True)
        report["valid_files"] = [
            name for name in report.get("valid_files", []) if name not in invalid_files
        ]
        report["retry_required_files"] = sorted(
            {
                str(item.get("file"))
                for item in report["retry_required"]
                if item.get("file")
            }
        )
        report["all_passed"] = False
        if selected_file:
            ledger_path = output_dir / "ocr-correct_file_validation.json"
            try:
                ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError):
                ledger = {}
            records = ledger.get("files", {}) if isinstance(ledger, dict) else {}
            if not isinstance(records, dict):
                records = {}
            record = records.get(selected_file, {})
            if not isinstance(record, dict):
                record = {}
            record["ocr_correction_review"] = review_report
            record["valid"] = False
            record.setdefault("invalid", []).extend(
                item for item in report["invalid"] if item.get("file") == selected_file
            )
            record.setdefault("retry_required", []).extend(
                item
                for item in report["retry_required"]
                if item.get("file") == selected_file
            )
            records[selected_file] = record
            atomic_write_text(
                ledger_path,
                json.dumps(
                    {
                        "schema_version": 2,
                        "task": "ocr-correct",
                        "scope": "file-checkpoints",
                        "files": records,
                    },
                    ensure_ascii=False,
                    indent=2,
                ),
            )
    if not selected_file:
        atomic_write_text(
            output_dir / "ocr-correct_validation.json",
            json.dumps(report, ensure_ascii=False, indent=2),
        )
    logger.info(
        f"ocr-correct 校验: {report['completed']}/{report['total']} completed, "
        f"{len(report['invalid'])} invalid"
    )
    for name in report["missing"][:10]:
        logger.error(f"Missing: {name}")
    for item in report["invalid"][:10]:
        logger.error(f"Invalid: {item['file']}: {item['reason']}")
    if report["all_passed"]:
        logger.success("OCR correction validation passed")
        logger.info("Next step: pdf2epub refine-prepare")
        return 0
    logger.error(
        "OCR correction validation failed; retry only the listed pages with the Subagent"
    )
    return 1

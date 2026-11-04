"""CLI for scoped terminology memory review and publication."""

from __future__ import annotations

import json
from pathlib import Path

from loguru import logger

from pdf2epub.dynamic_terminology import (
    current_authority_for_task,
    load_current_snapshot,
    prepare_review_packet,
    publish_review_packet,
    session_attestation,
    verify_session_attestation,
)
from pdf2epub.utils.configuration import load_config
from pdf2epub.utils.paths import book_output_dir


def _task_context(args):
    config = load_config(args.config)
    title = config.get("title")
    if not title:
        raise ValueError("title is required for terminology memory commands")
    dynamic = (config.get("translation", {}) or {}).get("dynamic_terminology", {}) or {}
    if dynamic.get("enabled") is not True:
        raise ValueError("set translation.dynamic_terminology.enabled: true before using terminology memory")
    output_dir = book_output_dir(title)
    manifest_path = output_dir / f"{args.task}_subagent_manifest.json"
    resolved_output = output_dir.resolve()
    if not manifest_path.resolve().is_relative_to(resolved_output):
        raise ValueError("translation manifest path escapes the book workspace")
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    if not isinstance(manifest, dict) or manifest.get("task") != args.task:
        raise ValueError("translation manifest is malformed or belongs to another task")
    source_language = manifest.get("source_language") or (config.get("translation", {}) or {}).get("source_language", "English")
    target_language = manifest.get("target_language") or (config.get("translation", {}) or {}).get("target_language", "Chinese")
    chapter_groups = manifest.get("chapter_groups", {})
    if not isinstance(chapter_groups, dict):
        raise ValueError("translation manifest has no chapter group map")
    entity_relative = (manifest.get("context_files", {}) or {}).get("translation_entities")
    entity_path = (output_dir / entity_relative).resolve() if entity_relative else None
    if entity_path and not entity_path.is_relative_to(output_dir.resolve()):
        raise ValueError("book entity path escapes output directory")
    return config, title, output_dir, manifest, source_language, target_language, chapter_groups, entity_path


def _read_only_consistency_audits(output_dir, task, manifest, files, config):
    """Audit retranslated units without writing a dynamic validation receipt."""
    from pdf2epub.dynamic_terminology import _confined_path, current_effective_projection
    from pdf2epub.terminology_memory import _file_sha

    source_dir = _confined_path(output_dir, manifest["source_dir"], "source directory")
    target_dir = _confined_path(output_dir, manifest["target_dir"], "target directory")
    dynamic = manifest.get("dynamic_terminology", {}) or {}
    unit_contexts = manifest.get("unit_context_files", {}) or {}
    unit_hashes = manifest.get("unit_context_sha256", {}) or {}
    base_contexts = dynamic.get("base_unit_context_files", {}) or {}
    chapters = manifest.get("chapter_groups", {})
    book_id = str(config.get("title") or manifest.get("book_title") or "")
    source_language, target_language = manifest.get("source_language"), manifest.get("target_language")
    audits = {}
    for name in sorted(set(str(value) for value in files)):
        if Path(name).name != name or Path(name).suffix.lower() != ".md":
            raise ValueError(f"unsafe consistency-only file name: {name!r}")
        source_path = _confined_path(source_dir, name, "source unit")
        target_path = _confined_path(target_dir, name, "target unit")
        if not source_path.is_file() or not target_path.is_file():
            raise ValueError(f"consistency-only source or target is missing: {name}")
        context_rel = unit_contexts.get(name)
        if not context_rel:
            raise ValueError(f"consistency-only unit has no prepared context: {name}")
        context_path = _confined_path(output_dir, context_rel, "prepared unit context")
        if _file_sha(context_path) != unit_hashes.get(name):
            raise ValueError(f"prepared unit context hash is stale: {name}")
        prepared = json.loads(context_path.read_text(encoding="utf-8"))
        base_rel = base_contexts.get(name)
        base_path = _confined_path(output_dir, base_rel, "static unit context") if base_rel else None
        projection = current_effective_projection(
            output_dir, task, source_dir, name, book_id=book_id,
            source_language=source_language, target_language=target_language,
            chapter_groups=chapters, base_context_path=base_path,
        )
        if not isinstance(prepared, dict) or prepared.get("entries") != projection["entries"]:
            raise ValueError(f"prepared context differs from current projection: {name}")
        if task == "translate":
            from pdf2epub.markdown_subagent_validation import validate_markdown_subagent

            report = validate_markdown_subagent(
                output_dir, task, source_dir, target_dir,
                structural_patterns=(r"^#{1,6}\s", r"!\[[^\]]*\]\([^)]+\)", r"^\d+\\\.\s", r"\[\^[^\]]+\]"),
                create_validated_copy=False, selected_files=[name], target_language=target_language,
            )
        else:
            from pdf2epub.html_translation.translation_validation import (
                HTMLTranslationValidator, HTMLValidationWorkspace,
            )

            validator = HTMLTranslationValidator(HTMLValidationWorkspace(
                output_dir, source_dir, target_dir, config,
            ))
            report = validator.validate(file_name=name, _skip_dynamic_terminology_gate=True)
        if not report.get("all_passed") or name not in report.get("valid_files", []):
            details = [row.get("reason", "validation failed") for row in report.get("invalid", []) if row.get("file") == name]
            raise ValueError(f"read-only structural audit failed for {name}: {'; '.join(details) or 'unit did not pass'}")
        audits[name] = {
            "valid": True,
            "source_sha256": _file_sha(source_path),
            "target_sha256": _file_sha(target_path),
        }
    return audits


def review_prepare_command(args):
    try:
        config, title, output_dir, manifest, source_lang, target_lang, chapters, entity_path = _task_context(args)
        attestation = session_attestation(
            output_dir, args.task,
            confirmed=bool(getattr(args, "confirm_no_active_sessions", False)),
        )
        verify_session_attestation(output_dir, attestation)
        consistency_units = getattr(args, "consistency_only_file", None) or None
        consistency_audits = (
            _read_only_consistency_audits(output_dir, args.task, manifest, consistency_units, config)
            if consistency_units else None
        )
        packet, authority = prepare_review_packet(
            output_dir, args.task, book_id=str(title), source_language=source_lang,
            target_language=target_lang, chapter_groups=chapters,
            book_entities_path=entity_path, wave=getattr(args, "wave", None),
            consistency_units=consistency_units, consistency_audits=consistency_audits,
        )
        attestation["book_id"] = str(title)
        attestation["authority_sha256"] = authority.authority_sha256
        from pdf2epub.workflow_contracts import atomic_write_json
        from pdf2epub.dynamic_terminology import _confined_path

        verify_session_attestation(output_dir, attestation)
        attestation_path = _confined_path(
            output_dir, Path("dynamic_terminology") / f"operator_attestation_{args.task}.json",
            "terminology operator attestation",
        )
        atomic_write_json(attestation_path, attestation)
        logger.success(
            f"Prepared terminology review: {len(packet['candidates'])} candidates, "
            f"{len(packet['affected_targets'])} affected existing targets"
        )
        logger.info(f"Review prompt: {output_dir / 'term-memory-review_prompt.md'}")
        logger.info("After review, run term-memory-publish with the reviewer JSON and the same session confirmation.")
        return 0
    except Exception as exc:
        logger.error(f"Could not prepare terminology review: {exc}")
        return 1


def publish_command(args):
    try:
        config, title, output_dir, manifest, source_lang, target_lang, chapters, entity_path = _task_context(args)
        attestation = session_attestation(
            output_dir, args.task,
            confirmed=bool(getattr(args, "confirm_no_active_sessions", False)),
        )
        prepared_review_path = output_dir / "term-memory-review_manifest.json"
        if not prepared_review_path.resolve().is_relative_to(output_dir.resolve()):
            raise ValueError("prepared review manifest path escapes the book workspace")
        prepared_review = json.loads(prepared_review_path.read_text(encoding="utf-8"))
        consistency_units = prepared_review.get("consistency_only_files", [])
        consistency_audits = (
            _read_only_consistency_audits(output_dir, args.task, manifest, consistency_units, config)
            if consistency_units else None
        )
        _manifest, authority = current_authority_for_task(
            output_dir, args.task, book_id=str(title), source_language=source_lang,
            target_language=target_lang, chapter_groups=chapters,
            book_entities_path=entity_path,
            consistency_units=consistency_units or None, consistency_audits=consistency_audits,
        )
        requested_wave = getattr(args, "wave", None)
        if prepared_review.get("task") != args.task:
            raise ValueError("prepared terminology review belongs to a different task")
        if prepared_review.get("dispatch_wave") != requested_wave:
            raise ValueError("publish wave does not match the prepared review wave")
        attestation["book_id"] = str(title)
        attestation["authority_sha256"] = authority.authority_sha256
        from pdf2epub.dynamic_terminology import _confined_path
        memory_root = _confined_path(output_dir, "dynamic_terminology", "dynamic terminology directory")
        current_sha, _entries = load_current_snapshot(
            memory_root, book_id=str(title),
            source_language=source_lang, target_language=target_lang,
        )
        verify_session_attestation(output_dir, attestation)
        digest = publish_review_packet(
            output_dir, args.task, _confined_review_path(output_dir, args.review), authority=authority,
            current_snapshot_sha256=current_sha, operator_attestation=attestation,
        )
        logger.success(f"Published terminology snapshot {digest}")
        return 0
    except Exception as exc:
        logger.error(f"Could not publish terminology memory: {exc}")
        return 1


def _confined_review_path(output_dir: Path, value: str | Path) -> Path:
    candidate = Path(value).expanduser().resolve()
    if not candidate.is_relative_to(Path(output_dir).resolve()) or not candidate.is_file():
        raise ValueError("review JSON must be an existing file inside the book workspace")
    return candidate


__all__ = ["publish_command", "review_prepare_command"]

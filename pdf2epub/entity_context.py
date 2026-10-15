"""Shared book-entity context contracts for translation workflows.

This module contains the format-neutral parts of entity extraction: selecting
source units, preparing the Subagent hand-off, and checking freshness. PDF
and EPUB command handlers consume these services without importing each
other's private helpers.
"""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

from loguru import logger

from .document_roles import infer_translation_file_role
from .workflow_contracts import atomic_write_json, atomic_write_text


ENTITY_SOURCE_FILTER_VERSION = 1
EPUB_ENTITY_EXCLUDED_ROLES = frozenset(
    {"index", "bibliography", "copyright", "cover"}
)


def entity_source_selection(
    source_dir: Path,
    source_stage: str,
) -> tuple[list[Path], list[Path]]:
    """Select entity-extraction units and deterministic EPUB exclusions."""
    source_dir = Path(source_dir)
    all_files = sorted(source_dir.glob("*.md"))
    if source_stage != "epub-compressed":
        return all_files, []

    active_names: set[str] = set()
    for mapping_path in sorted(source_dir.glob("*.mapping.json")):
        stem = mapping_path.name[: -len(".mapping.json")]
        try:
            mapping = json.loads(mapping_path.read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            mapping = {}
        parts = mapping.get("translation_parts")
        if isinstance(parts, list) and parts:
            active_names.update(str(name) for name in parts)
        else:
            active_names.add(f"{stem}.md")
    if active_names:
        all_files = [path for path in all_files if path.name in active_names]

    included: list[Path] = []
    excluded: list[Path] = []
    for path in all_files:
        role = infer_translation_file_role(path.name)
        if role in EPUB_ENTITY_EXCLUDED_ROLES:
            excluded.append(path)
        else:
            included.append(path)
    return included, excluded


def prepare_entity_subagent_task(
    output_dir: Path,
    book_title: str,
    source_dir: Path,
    source_stage: str,
    source_language: str,
    target_language: str,
    config: dict,
) -> Path:
    """Create the reusable book-specific terminology hand-off."""
    from .subagent_runtime import resolve_subagent_model
    from .entity_extractor import (
        create_entity_extraction_prompt,
        create_entity_template,
    )

    source_files, excluded_files = entity_source_selection(source_dir, source_stage)
    if not source_files:
        raise ValueError(
            f"No eligible Markdown source found in {source_dir}; "
            "all units were excluded from EPUB entity extraction"
        )
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    model = resolve_subagent_model(config, "extract-entities")

    atomic_write_json(
        output_dir / "translation_entities.template.json",
        create_entity_template(
            book_title,
            source_language,
            target_language,
            (path.name for path in source_files),
        ),
    )
    manifest = {
        "schema_version": 1,
        "workflow": "antigravity-subagent",
        "task": "extract-entities",
        "source_language": source_language,
        "target_language": target_language,
        "model": model,
        "source_dir": source_dir.relative_to(output_dir).as_posix(),
        "source_stage": source_stage,
        "files": [path.name for path in source_files],
        "excluded_files": [path.name for path in excluded_files],
        "source_filter_version": ENTITY_SOURCE_FILTER_VERSION,
        "excluded_roles": sorted(EPUB_ENTITY_EXCLUDED_ROLES)
        if source_stage == "epub-compressed"
        else [],
        "source_sha256": {
            path.name: hashlib.sha256(path.read_bytes()).hexdigest()
            for path in source_files
        },
        "output_file": "translation_entities.json",
        "template_file": "translation_entities.template.json",
    }
    atomic_write_json(output_dir / "entity_subagent_manifest.json", manifest)
    atomic_write_text(
        output_dir / "entity_subagent_prompt.md",
        create_entity_extraction_prompt(book_title, (source_language, target_language))
        + f'\n\nRecommended Antigravity model: `{model}`\n',
    )
    logger.info(
        f"已生成实体提取 Subagent 任务：{output_dir / 'entity_subagent_prompt.md'}；"
        "完成后由 Subagent 写入 translation_entities.json。"
    )
    return output_dir / "translation_entities.json"


def entity_context_is_current(
    output_dir: Path,
    source_dir: Path,
    source_language: str | None = None,
    target_language: str | None = None,
) -> bool:
    """Return whether the book entity hand-off matches its source snapshot."""
    output_dir = Path(output_dir)
    source_dir = Path(source_dir)
    entity_path = output_dir / "translation_entities.json"
    manifest_path = output_dir / "entity_subagent_manifest.json"
    if not entity_path.is_file() or not manifest_path.is_file():
        return False
    try:
        from .entity_extractor import validate_entities

        data = json.loads(entity_path.read_text(encoding="utf-8"))
        if validate_entities(
            data,
            source_language=source_language,
            target_language=target_language,
        ):
            return False
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        if (
            Path(manifest.get("source_dir", "")).as_posix()
            != source_dir.relative_to(output_dir).as_posix()
        ):
            return False
        if manifest.get("source_stage") == "epub-compressed" and manifest.get(
            "source_filter_version"
        ) != ENTITY_SOURCE_FILTER_VERSION:
            return False
        expected_hashes = manifest.get("source_sha256", {}) or {}
        if not isinstance(expected_hashes, dict) or not expected_hashes:
            return False
        excluded_names = {
            str(name)
            for name in (manifest.get("excluded_files", []) or [])
            if str(name).strip()
        }
        selected_files, selected_excluded = entity_source_selection(
            source_dir, str(manifest.get("source_stage") or "")
        )
        actual_names = {path.name for path in selected_files + selected_excluded}
        expected_names = set(expected_hashes) | excluded_names
        if expected_names != actual_names:
            return False
        for name, expected in expected_hashes.items():
            path = source_dir / name
            if (
                not path.is_file()
                or hashlib.sha256(path.read_bytes()).hexdigest() != expected
            ):
                return False
        return bool(manifest.get("source_sha256"))
    except (OSError, json.JSONDecodeError, TypeError, ValueError):
        return False


__all__ = [
    "ENTITY_SOURCE_FILTER_VERSION",
    "EPUB_ENTITY_EXCLUDED_ROLES",
    "entity_context_is_current",
    "entity_source_selection",
    "prepare_entity_subagent_task",
]

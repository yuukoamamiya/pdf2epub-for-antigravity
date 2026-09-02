"""Workspace hand-off helpers for translation entity extraction.

Entity extraction is a local hand-off to a later translation Subagent. This
module deliberately contains no model client and no network execution path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, Iterable, List


ENTITY_COLLECTIONS = (
    "characters",
    "places",
    "organizations",
    "terms",
    "races",
    "items",
)


def create_entity_template(
    book_title: str,
    source_language: str,
    target_language: str,
    source_files: Iterable[str] = (),
) -> Dict[str, Any]:
    """Create a structural glossary scaffold for the workspace Subagent."""
    return {
        "schema_version": 1,
        "metadata": {
            "book_title": book_title,
            "source_language": source_language,
            "target_language": target_language,
            "source_files": list(source_files),
            "extraction_complete": False,
        },
        **{collection: [] for collection in ENTITY_COLLECTIONS},
    }


def create_entity_extraction_prompt(
    book_title: str,
    language_pair: tuple[str, str] = ("Japanese", "Chinese"),
) -> str:
    """Return the prompt shown to the workspace Subagent."""
    source_language, target_language = language_pair
    return f"""# Translation entity extraction

Read every Markdown file listed by the entity task manifest for **{book_title}**.
Extract recurring people, places, organizations, terms, species, and items
from {source_language} for consistent {target_language} translation.

Read `translation_entities.template.json` first, then write
`translation_entities.json` in the task directory. Return valid JSON only and
keep every collection from the template, even when a collection is empty.
Use this shape:

{{
  "schema_version": 1,
  "metadata": {{"book_title": "{book_title}", "extraction_complete": true}},
  "characters": [],
  "places": [],
  "organizations": [],
  "terms": [],
  "races": [],
  "items": []
}}

For each entity include non-empty `original` and `suggested_translation` fields.
When applicable, also include a reading, romanization, category, and short
description.
The `suggested_translation` values form the canonical terminology reference for
the later translation task. Prefer one stable translation for the same entity;
record meaningful variants in a separate note rather than creating duplicate
entries.
Do not modify source OCR files, add Markdown fences, or call an API.
"""


def validate_entities(data: Any, book_title: str | None = None) -> List[str]:
    """Validate the entity hand-off contract locally."""
    if not isinstance(data, dict):
        return ["translation_entities.json must contain an object"]
    errors: List[str] = []
    if data.get("schema_version", 1) != 1:
        errors.append("schema_version must be 1")
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata must be an object")
    else:
        metadata_title = metadata.get("book_title")
        if not isinstance(metadata_title, str) or not metadata_title.strip():
            errors.append("metadata.book_title must be a non-empty string")
        elif book_title and metadata_title != book_title:
            errors.append("metadata.book_title does not match the configured title")
        if metadata.get("extraction_complete") is not True:
            errors.append("metadata.extraction_complete must be true")
    for collection in ENTITY_COLLECTIONS:
        if collection not in data:
            errors.append(f"{collection} must be present")
            continue
        if not isinstance(data[collection], list):
            errors.append(f"{collection} must be an array")
            continue
        for index, entity in enumerate(data[collection]):
            path = f"{collection}[{index}]"
            if not isinstance(entity, dict):
                errors.append(f"{path} must be an object")
                continue
            for field in ("original", "suggested_translation"):
                value = entity.get(field)
                if not isinstance(value, str) or not value.strip():
                    errors.append(f"{path}.{field} must be a non-empty string")
    return errors


def save_entities(entities: Dict[str, Any], output_dir: Path) -> Path:
    """Persist a Subagent-produced entity document after local validation."""
    errors = validate_entities(entities)
    if errors:
        raise ValueError("; ".join(errors))
    output_dir = Path(output_dir)
    output_dir.mkdir(parents=True, exist_ok=True)
    output_path = output_dir / "translation_entities.json"
    output_path.write_text(
        json.dumps(entities, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return output_path

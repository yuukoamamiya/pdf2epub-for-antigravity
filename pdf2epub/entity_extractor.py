"""Workspace hand-off helpers for translation entity extraction.

Entity extraction is a local hand-off to a later translation Subagent. This
module deliberately contains no model client and no network execution path.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Dict, List


ENTITY_COLLECTIONS = (
    "characters",
    "places",
    "organizations",
    "terms",
    "races",
    "items",
)


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

Write `translation_entities.json` in the task directory. Return valid JSON only
and use this shape:

{{
  "metadata": {{"book_title": "{book_title}", "extraction_complete": true}},
  "characters": [],
  "places": [],
  "organizations": [],
  "terms": [],
  "races": [],
  "items": []
}}

For each entity preserve the original spelling and, when applicable, include a
reading, romanization, suggested translation, category, and short description.
The `suggested_translation` values form the canonical terminology reference for
the later translation task. Prefer one stable translation for the same entity;
record meaningful variants in a separate note rather than creating duplicate
entries.
Do not modify source OCR files, add Markdown fences, or call an API.
"""


def validate_entities(data: Any, book_title: str | None = None) -> List[str]:
    """Validate the minimal entity hand-off contract locally."""
    if not isinstance(data, dict):
        return ["translation_entities.json must contain an object"]
    errors: List[str] = []
    metadata = data.get("metadata")
    if not isinstance(metadata, dict):
        errors.append("metadata must be an object")
    elif book_title and metadata.get("book_title") not in (None, book_title):
        errors.append("metadata.book_title does not match the configured title")
    for collection in ENTITY_COLLECTIONS:
        if collection in data and not isinstance(data[collection], list):
            errors.append(f"{collection} must be an array")
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

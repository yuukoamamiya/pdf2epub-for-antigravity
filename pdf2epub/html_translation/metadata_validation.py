"""Validation of prepared and translated EPUB metadata contracts."""
from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List

from pdf2epub.subagent_safety import detect_refusal


@dataclass(frozen=True)
class MetadataValidationWorkspace:
    output_dir: Path


class HTMLMetadataValidator:
    """Validate the metadata translation receipt independently of packaging."""
    def __init__(self, workspace: MetadataValidationWorkspace):
        self.output_dir = Path(workspace.output_dir)

    def validate(self) -> Dict[str, Any]:
        """Validate the subagent metadata hand-off without contacting an LLM."""
        source_path = self.output_dir / "metadata_translation_source.json"
        translated_path = self.output_dir / "translated_metadata.json"
        errors: List[str] = []

        if not source_path.exists():
            errors.append(
                "metadata_translation_source.json is missing; run html-prepare again"
            )
        if not translated_path.exists():
            errors.append(
                "translated_metadata.json is missing; ask the subagent to create it"
            )
        if errors:
            return {"valid": False, "errors": errors}

        try:
            source = json.loads(source_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [f"invalid metadata source JSON: {exc}"]}
        for context_label in ("translation_context", "translation_audit_context"):
            translation_context = source.get(context_label, {})
            if not translation_context:
                continue
            context_files = translation_context.get("files", {})
            context_hashes = translation_context.get("sha256", {})
            root = self.output_dir.resolve()
            for name, relative in context_files.items():
                context_path = (root / str(relative)).resolve()
                try:
                    context_path.relative_to(root)
                except ValueError:
                    errors.append(
                        f"metadata {context_label} escapes output directory: {relative}"
                    )
                    continue
                if not context_path.is_file():
                    errors.append(
                        f"metadata {context_label} is missing: {relative}"
                    )
                    continue
                actual_hash = hashlib.sha256(context_path.read_bytes()).hexdigest()
                if context_hashes.get(name) != actual_hash:
                    errors.append(
                        f"metadata {context_label} changed after preparation: {relative}"
                    )
        try:
            translated = json.loads(translated_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            return {"valid": False, "errors": [f"invalid translated metadata JSON: {exc}"]}

        if not isinstance(translated, dict):
            errors.append("translated metadata must be a JSON object")
            return {"valid": False, "errors": errors}

        metadata_pairs = [
            ("translated_title", source.get("original_title", ""), translated.get("translated_title", "")),
            (
                "translated_description",
                source.get("translatable_metadata", {}).get("description", ""),
                translated.get("translated_description", ""),
            ),
            (
                "translated_rights",
                source.get("translatable_metadata", {}).get("rights", ""),
                translated.get("translated_rights", ""),
            ),
        ]
        source_toc_for_refusal = source.get("toc", [])
        translated_toc_for_refusal = translated.get("toc", [])
        if isinstance(source_toc_for_refusal, list) and isinstance(
            translated_toc_for_refusal, list
        ):
            metadata_pairs.extend(
                (
                    "toc.translated",
                    expected.get("original", ""),
                    actual.get("translated", ""),
                )
                for expected, actual in zip(
                    source_toc_for_refusal, translated_toc_for_refusal
                    )
                if isinstance(expected, dict) and isinstance(actual, dict)
            )
        metadata_safety_blocked = []
        for field, source_value, translated_value in metadata_pairs:
            refusal = detect_refusal(str(source_value), str(translated_value))
            if refusal:
                errors.append(f"metadata refusal/disclaimer detected: {refusal}")
                metadata_safety_blocked.append(field)

        if translated.get("schema_version") != source.get("schema_version", 1):
            errors.append("schema_version does not match metadata source")
        if translated.get("original_title") != source.get("original_title"):
            errors.append("original_title does not match metadata source")
        if not str(translated.get("translated_title") or "").strip():
            errors.append("translated_title is missing or empty")
        for key in ("target_language", "target_language_code"):
            if translated.get(key) != source.get(key):
                errors.append(f"{key} does not match metadata source")

        source_preserved = source.get("preserved_metadata", {})
        translated_preserved = translated.get("preserved_metadata", {})
        if not isinstance(translated_preserved, dict):
            errors.append("preserved_metadata must be an object")
        else:
            for key in ("author", "publisher"):
                if translated_preserved.get(key) != source_preserved.get(key, ""):
                    errors.append(f"preserved_metadata.{key} was changed or omitted")

        source_toc = source.get("toc", [])
        translated_toc = translated.get("toc")
        if not isinstance(translated_toc, list):
            errors.append("toc must be an array")
        elif len(translated_toc) != len(source_toc):
            errors.append(
                f"toc entry count mismatch: expected {len(source_toc)}, "
                f"got {len(translated_toc)}"
            )
        else:
            for index, (expected, actual) in enumerate(zip(source_toc, translated_toc)):
                if not isinstance(actual, dict):
                    errors.append(f"toc[{index}] must be an object")
                    continue
                actual_href = actual.get("href", "")
                actual_anchor = actual.get("anchor")
                for key, value in (
                    ("original", actual.get("original")),
                    ("href", actual_href),
                    ("anchor", actual_anchor),
                    ("level", actual.get("level")),
                ):
                    if value != expected.get(key):
                        errors.append(f"toc[{index}].{key} was changed")
                if not str(actual.get("translated") or "").strip():
                    errors.append(f"toc[{index}].translated is missing or empty")

        source_extra = source.get("translatable_metadata", {})
        for key, source_value in (
            ("translated_description", source_extra.get("description")),
            ("translated_rights", source_extra.get("rights")),
        ):
            if key not in translated:
                errors.append(f"{key} field is missing")
            elif not isinstance(translated.get(key), str):
                errors.append(f"{key} must be a string")
            elif source_value and not translated[key].strip():
                errors.append(f"{key} is missing or empty")

        return {
            "valid": not errors,
            "errors": errors,
            "safety_blocked": metadata_safety_blocked,
            "source": str(source_path),
            "translated": str(translated_path),
        }
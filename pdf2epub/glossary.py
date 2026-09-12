"""Reusable, user-managed translation glossary support.

The book-specific ``translation_entities.json`` hand-off remains separate from
this module.  A domain glossary is a read-only, reusable translation context
selected by the book configuration and snapshotted into the book output
directory so a translation run is reproducible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

import yaml

from pdf2epub.utils.common import sanitize_filename


SUPPORTED_SUFFIXES = {".yaml", ".yml", ".json"}
DEFAULT_POLICY = "preferred"
POLICIES = {"fixed", "preferred"}


class GlossaryError(ValueError):
    """Raised when a selected glossary cannot be safely used."""


def _clean(value: Any) -> str:
    return str(value or "").strip()


def _language_key(value: Any) -> str:
    value = _clean(value).lower().replace("_", "-")
    aliases = {
        "de": "german",
        "de-de": "german",
        "german": "german",
        "deutsch": "german",
        "en": "english",
        "english": "english",
        "zh": "chinese",
        "zh-cn": "chinese",
        "chinese": "chinese",
        "simplified chinese": "chinese",
        "中文": "chinese",
        "fr": "french",
        "french": "french",
        "ja": "japanese",
        "japanese": "japanese",
    }
    return aliases.get(value, value)


def _normal_form(value: str) -> str:
    return " ".join(_clean(value).casefold().split())


def _as_string_list(value: Any, field: str, entry_index: int) -> List[str]:
    if value is None:
        return []
    if isinstance(value, str):
        values = [value]
    elif isinstance(value, list):
        values = value
    else:
        raise GlossaryError(f"entries[{entry_index}].{field} must be a string or array")
    result = []
    for item in values:
        item = _clean(item)
        if item and item not in result:
            result.append(item)
    return result


def normalize_glossary(data: Any, source_path: Optional[Path] = None) -> Dict[str, Any]:
    """Validate and normalize a user glossary into the versioned schema."""
    if not isinstance(data, dict):
        raise GlossaryError("glossary must contain an object")
    if data.get("schema_version", 1) != 1:
        raise GlossaryError("glossary schema_version must be 1")

    metadata = data.get("metadata") or {}
    if not isinstance(metadata, dict):
        raise GlossaryError("glossary metadata must be an object")
    name = _clean(metadata.get("name") or metadata.get("id"))
    if not name and source_path:
        name = source_path.stem
    if not name:
        raise GlossaryError("glossary metadata.name is required")

    entries = data.get("entries")
    if not isinstance(entries, list):
        raise GlossaryError("glossary entries must be an array")

    normalized_entries: List[Dict[str, Any]] = []
    seen: Dict[str, str] = {}
    for index, raw in enumerate(entries):
        if not isinstance(raw, dict):
            raise GlossaryError(f"entries[{index}] must be an object")
        source = _clean(raw.get("source") or raw.get("original"))
        target = _clean(raw.get("target") or raw.get("suggested_translation"))
        if not source or not target:
            raise GlossaryError(
                f"entries[{index}] requires non-empty source and target fields"
            )
        policy = _clean(raw.get("policy") or DEFAULT_POLICY).lower()
        if policy not in POLICIES:
            raise GlossaryError(
                f"entries[{index}].policy must be one of: {', '.join(sorted(POLICIES))}"
            )
        variants = _as_string_list(raw.get("variants"), "variants", index)
        aliases = _as_string_list(raw.get("aliases"), "aliases", index)
        forms = [source, *variants, *aliases]
        for form in forms:
            key = _normal_form(form)
            previous = seen.get(key)
            if previous is not None and previous != target:
                raise GlossaryError(
                    f"conflicting translations for source form {form!r}: "
                    f"{previous!r} vs {target!r}"
                )
            seen[key] = target

        item: Dict[str, Any] = {
            "source": source,
            "target": target,
            "policy": policy,
        }
        if variants:
            item["variants"] = variants
        if aliases:
            item["aliases"] = aliases
        for field in ("id", "category", "note", "scope"):
            value = _clean(raw.get(field))
            if value:
                item[field] = value
        normalized_entries.append(item)

    normalized_metadata: Dict[str, Any] = {
        "name": name,
        "domain": _clean(metadata.get("domain")),
        "source_language": _clean(metadata.get("source_language")),
        "target_language": _clean(metadata.get("target_language")),
        "version": _clean(metadata.get("version")),
    }
    normalized_metadata = {
        key: value for key, value in normalized_metadata.items() if value
    }
    return {
        "schema_version": 1,
        "metadata": normalized_metadata,
        "entries": normalized_entries,
    }


def load_glossary(path: Path) -> Dict[str, Any]:
    """Read and validate a YAML or JSON glossary."""
    path = Path(path)
    if path.suffix.lower() not in SUPPORTED_SUFFIXES:
        raise GlossaryError(
            f"unsupported glossary format {path.suffix!r}; use .yaml, .yml, or .json"
        )
    if not path.is_file():
        raise GlossaryError(f"glossary file not found: {path}")
    try:
        if path.suffix.lower() == ".json":
            data = json.loads(path.read_text(encoding="utf-8"))
        else:
            data = yaml.safe_load(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError, yaml.YAMLError) as exc:
        raise GlossaryError(f"could not read glossary {path}: {exc}") from exc
    return normalize_glossary(data, path)


def validate_glossary_languages(
    glossary: Mapping[str, Any],
    source_language: str,
    target_language: str,
) -> List[str]:
    """Return language compatibility warnings/errors for a glossary."""
    metadata = glossary.get("metadata", {})
    errors = []
    glossary_source = metadata.get("source_language")
    glossary_target = metadata.get("target_language")
    if glossary_source and _language_key(glossary_source) != _language_key(source_language):
        errors.append(
            f"source language mismatch: glossary={glossary_source!r}, "
            f"translation={source_language!r}"
        )
    if glossary_target and _language_key(glossary_target) != _language_key(target_language):
        errors.append(
            f"target language mismatch: glossary={glossary_target!r}, "
            f"translation={target_language!r}"
        )
    return errors


@dataclass(frozen=True)
class GlossaryBundle:
    """Selected glossary snapshots and the prompt rules that describe them."""

    context_files: Dict[str, Path]
    context_sha256: Dict[str, str]
    entries: int
    names: List[str]
    rules: List[str]


def _configured_items(config: Mapping[str, Any]) -> List[Any]:
    translation = config.get("translation", {}) or {}
    configured = translation.get("glossaries", [])
    if configured is None:
        return []
    if isinstance(configured, (str, Path, dict)):
        return [configured]
    if not isinstance(configured, list):
        raise GlossaryError("translation.glossaries must be an array")
    return configured


def _resolve_config_path(raw_path: str, config_path: Optional[Path]) -> Path:
    path = Path(raw_path).expanduser()
    if path.is_absolute():
        return path.resolve()
    roots = []
    if config_path:
        roots.append(Path(config_path).expanduser().resolve().parent)
    roots.append(Path.cwd())
    for root in roots:
        candidate = (root / path).resolve()
        if candidate.is_file():
            return candidate
    return (roots[0] / path).resolve() if roots else path.resolve()


def load_selected_glossaries(
    config: Mapping[str, Any],
    output_dir: Path,
    source_language: str,
    target_language: str,
    config_path: Optional[Path] = None,
) -> GlossaryBundle:
    """Load configured glossaries and create immutable per-book snapshots.

    The original user file is never modified.  Normalized snapshots live under
    ``output/<book>/translation_glossaries`` so all Subagent context paths stay
    inside the book workspace and can be hash-locked in a manifest.
    """
    items = _configured_items(config)
    if not items:
        return GlossaryBundle({}, {}, 0, [], [])

    snapshot_dir = Path(output_dir) / "translation_glossaries"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    context_files: Dict[str, Path] = {}
    context_sha256: Dict[str, str] = {}
    names: List[str] = []
    total_entries = 0
    seen_sources: Dict[str, str] = {}

    for index, item in enumerate(items, 1):
        if isinstance(item, str):
            raw_path = item
            configured_id = ""
        elif isinstance(item, dict):
            raw_path = item.get("path") or item.get("file")
            configured_id = _clean(item.get("id"))
            if not raw_path:
                raise GlossaryError(
                    f"translation.glossaries[{index}] requires path"
                )
        else:
            raise GlossaryError(
                f"translation.glossaries[{index}] must be a path or object"
            )

        source_path = _resolve_config_path(str(raw_path), config_path)
        glossary = load_glossary(source_path)
        language_errors = validate_glossary_languages(
            glossary, source_language, target_language
        )
        if language_errors:
            raise GlossaryError(
                f"glossary {source_path.name}: " + "; ".join(language_errors)
            )
        metadata = glossary["metadata"]
        glossary_id = configured_id or _clean(metadata.get("name")) or source_path.stem
        safe_id = sanitize_filename(glossary_id) or f"glossary_{index:03d}"
        snapshot_path = snapshot_dir / f"{safe_id}.json"
        # A duplicate id is ambiguous and could cause one context to replace
        # another snapshot.
        if snapshot_path.name in {path.name for path in context_files.values()}:
            raise GlossaryError(f"duplicate glossary id: {glossary_id}")
        snapshot_path.write_text(
            json.dumps(glossary, ensure_ascii=False, indent=2) + "\n",
            encoding="utf-8",
        )

        context_name = f"domain_glossary_{index:03d}"
        context_files[context_name] = snapshot_path
        context_sha256[context_name] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
        names.append(glossary_id)
        total_entries += len(glossary["entries"])
        for entry in glossary["entries"]:
            for form in [entry["source"], *entry.get("variants", []), *entry.get("aliases", [])]:
                key = _normal_form(form)
                previous = seen_sources.get(key)
                if previous is not None and previous != entry["target"]:
                    raise GlossaryError(
                        f"selected glossaries conflict for source form {form!r}: "
                        f"{previous!r} vs {entry['target']!r}"
                    )
                seen_sources[key] = entry["target"]

    rules = [
        "Selected domain glossaries are read-only authoritative terminology context; never modify them.",
        "Apply glossary entries to prose, titles, TOC labels, descriptions, and other translatable metadata.",
        "For entries marked `fixed`, use the listed target translation whenever the source term is used, while respecting grammar and the entry note.",
        "For entries marked `preferred`, use the listed target translation unless the surrounding context clearly requires another form.",
        "Use variants and aliases to recognize source word forms, but do not translate glossary files themselves.",
        "Never replace text mechanically in a way that changes HTML tags, attributes, entities, anchors, formulas, or LaTeX commands.",
    ]
    return GlossaryBundle(
        context_files=context_files,
        context_sha256=context_sha256,
        entries=total_entries,
        names=names,
        rules=rules,
    )


def validate_translation_context(
    output_dir: Path,
    manifest_name: str,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate all read-only translation contexts attached to a task."""
    manifest_path = Path(output_dir) / manifest_name
    translation = config.get("translation", {}) or {}
    configured_glossaries = bool(_configured_items(config))
    require_entities = bool(translation.get("require_entities", True)) and (
        Path(output_dir) / "entity_subagent_manifest.json"
    ).is_file()
    if not manifest_path.is_file():
        errors = []
        if configured_glossaries:
            errors.append(f"missing translation task manifest: {manifest_name}")
        if require_entities:
            errors.append(f"missing translation task manifest: {manifest_name}")
        return {"valid": not errors, "errors": errors}

    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return {"valid": False, "errors": [f"invalid translation manifest: {exc}"]}

    root = Path(output_dir).resolve()
    context_files = manifest.get("context_files", {}) or {}
    context_hashes = manifest.get("context_sha256", {}) or {}
    errors: List[str] = []
    entity_path: Optional[Path] = None
    glossary_names: List[str] = []
    glossary_entries = 0

    for name, relative in context_files.items():
        path = (root / str(relative)).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(f"translation context escapes output directory: {relative}")
            continue
        if not path.is_file():
            errors.append(f"translation context is missing: {relative}")
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if context_hashes.get(name) != actual_hash:
            errors.append(f"translation context changed after task preparation: {relative}")
        if name == "translation_entities":
            entity_path = path
        elif str(name).startswith("domain_glossary_"):
            try:
                glossary = load_glossary(path)
                glossary_names.append(glossary["metadata"]["name"])
                glossary_entries += len(glossary["entries"])
            except GlossaryError as exc:
                errors.append(str(exc))

    skipped = set(manifest.get("skipped_context_files", []) or [])
    if require_entities and entity_path is None and "translation_entities" not in skipped:
        errors.append("translation_entities context is required but was not attached")
    if entity_path is not None:
        try:
            from pdf2epub.entity_extractor import validate_entities

            entity_data = json.loads(entity_path.read_text(encoding="utf-8"))
            errors.extend(validate_entities(entity_data, config.get("title")))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid translation_entities.json: {exc}")
    if configured_glossaries and not any(
        str(name).startswith("domain_glossary_") for name in context_files
    ):
        errors.append("configured external glossaries were not attached to the task")

    return {
        "valid": not errors,
        "errors": errors,
        "glossaries": glossary_names,
        "glossary_entries": glossary_entries,
        "context_files": context_files,
        "skipped_context_files": sorted(skipped),
    }

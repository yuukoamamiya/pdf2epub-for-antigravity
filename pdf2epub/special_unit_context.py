"""Fresh, sparse context for controlled special-unit translation."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Mapping

from pdf2epub.glossary import (
    _find_term_matches,
    _normalize_match_text,
    _term_forms,
    sort_glossary_entries,
)
from pdf2epub.toc_translation_workflow import build_toc_heading_contexts


class SpecialUnitContextError(ValueError):
    """Raised when a prepared special-unit handoff is missing or stale."""


def _stable_hash(value: Any) -> str:
    payload = json.dumps(
        value, ensure_ascii=False, sort_keys=True, separators=(",", ":")
    ).encode("utf-8")
    return hashlib.sha256(payload).hexdigest()


def _validate_forms(value: Any, label: str, *, allow_empty: bool = False) -> list[str]:
    if value is None and allow_empty:
        return []
    values = [value] if isinstance(value, str) else value
    if not isinstance(values, list) or any(not isinstance(item, str) or not item.strip() for item in values):
        raise SpecialUnitContextError(f"invalid {label}: expected a non-empty string or list of non-empty strings")
    result = [item.strip() for item in values]
    if not result and not allow_empty:
        raise SpecialUnitContextError(f"invalid {label}: at least one non-empty string is required")
    return result


def _validate_entry(entry: dict[str, Any], index: int) -> dict[str, Any]:
    source_key = "source" if "source" in entry else "original"
    target_key = "target" if "target" in entry else "suggested_translation"
    sources = _validate_forms(entry.get(source_key), f"entries[{index}].{source_key}")
    targets = _validate_forms(entry.get(target_key), f"entries[{index}].{target_key}")
    if len(targets) != 1:
        raise SpecialUnitContextError(f"invalid entries[{index}].{target_key}: expected one target string")
    # Keep the normalized canonical fields available to every consumer while
    # preserving the original list under `source_forms` for candidate display.
    entry["source_forms"] = sources
    entry["source"] = sources[0]
    entry["target"] = targets[0]
    for field in ("variants", "aliases"):
        if field in entry:
            entry[field] = _validate_forms(entry[field], f"entries[{index}].{field}", allow_empty=True)
    aliases_by_language = entry.get("aliases_by_language")
    if aliases_by_language is not None:
        if not isinstance(aliases_by_language, Mapping):
            raise SpecialUnitContextError(f"invalid entries[{index}].aliases_by_language")
        normalized_aliases = {}
        for language, aliases in aliases_by_language.items():
            normalized_aliases[str(language)] = _validate_forms(
                aliases, f"entries[{index}].aliases_by_language.{language}", allow_empty=True
            )
        entry["aliases_by_language"] = normalized_aliases
    return entry


def build_special_unit_context(
    output_dir: Path,
    filename: str,
    kind: str,
    *,
    source_language: str,
    target_language: str,
) -> dict[str, Any]:
    """Load the current translate-prepared sparse context for one unit.

    This deliberately refuses to discover glossary files or infer unit paths:
    the ordinary translate prepare manifest is the authority for both.
    """
    if kind not in {"index", "bibliography"}:
        raise SpecialUnitContextError(f"unsupported controlled special-unit kind: {kind}")
    if not isinstance(filename, str) or Path(filename).name != filename or not filename.endswith(".md"):
        raise SpecialUnitContextError("filename must be a plain Markdown basename")
    output = Path(output_dir).resolve()
    manifest_path = output / "translate_subagent_manifest.json"
    if not manifest_path.is_file():
        raise SpecialUnitContextError(
            "translate prepare is required before building special-unit context"
        )
    try:
        manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise SpecialUnitContextError(f"invalid translate prepare manifest: {exc}") from exc
    if not isinstance(manifest, dict):
        raise SpecialUnitContextError("invalid translate prepare manifest: expected object")
    if manifest.get("task") != "translate" or manifest.get("workflow") != "antigravity-subagent":
        raise SpecialUnitContextError("translate prepare manifest is not a translate handoff")
    declared_files = manifest.get("files")
    if not isinstance(declared_files, list) or filename not in declared_files:
        raise SpecialUnitContextError(f"{filename} is not declared by the translate prepare manifest")

    source_dir_value = manifest.get("source_dir")
    unit_files = manifest.get("unit_context_files") or {}
    unit_hashes = manifest.get("unit_context_sha256") or {}
    source_hashes = manifest.get("source_sha256") or {}
    heading_contexts = manifest.get("toc_heading_contexts") or {}
    if (
        not isinstance(source_dir_value, str)
        or not isinstance(unit_files, Mapping)
        or not isinstance(unit_hashes, Mapping)
        or not isinstance(source_hashes, Mapping)
    ):
        raise SpecialUnitContextError("translate prepare manifest lacks source/context mappings")
    expected_source_hash = source_hashes.get(filename)
    has_context_path = filename in unit_files
    has_context_hash = filename in unit_hashes
    relative_context = unit_files.get(filename)
    expected_context_hash = unit_hashes.get(filename)
    heading = heading_contexts.get(filename) if isinstance(heading_contexts, Mapping) else None
    if heading is not None and not isinstance(heading, dict):
        raise SpecialUnitContextError("invalid TOC heading context shape; rerun translate prepare")
    if not expected_source_hash:
        raise SpecialUnitContextError(f"{filename} has no current source hash; run translate prepare first")
    if has_context_path != has_context_hash:
        raise SpecialUnitContextError(
            f"{filename} has an incomplete sparse context mapping; rerun translate prepare"
        )
    if has_context_path and (not isinstance(relative_context, str) or not relative_context or not expected_context_hash):
        raise SpecialUnitContextError(
            f"{filename} has an invalid sparse context mapping; rerun translate prepare"
        )

    source_dir = (output / source_dir_value).resolve()
    try:
        source_dir.relative_to(output)
    except ValueError as exc:
        raise SpecialUnitContextError("prepared source directory escapes output directory") from exc
    source_path = (source_dir / filename).resolve()
    try:
        source_path.relative_to(source_dir)
    except ValueError as exc:
        raise SpecialUnitContextError("prepared source path escapes its authorized directory") from exc
    if not source_path.is_file():
        raise SpecialUnitContextError(f"prepared source is missing for {filename}")
    source_sha = hashlib.sha256(source_path.read_bytes()).hexdigest()
    if source_sha != expected_source_hash:
        raise SpecialUnitContextError(f"source changed after translate prepare: {filename}")
    context_path = None
    if has_context_path:
        context_path = (output / relative_context).resolve()
        try:
            context_path.relative_to(output)
        except ValueError as exc:
            raise SpecialUnitContextError("prepared unit context escapes output directory") from exc
        if not context_path.is_file():
            raise SpecialUnitContextError(f"prepared unit context is missing for {filename}")
        context_bytes = context_path.read_bytes()
        context_sha = hashlib.sha256(context_bytes).hexdigest()
        if context_sha != expected_context_hash:
            raise SpecialUnitContextError(f"unit context changed after translate prepare: {filename}")
        try:
            projected = json.loads(context_bytes.decode("utf-8"))
        except (UnicodeError, json.JSONDecodeError) as exc:
            raise SpecialUnitContextError(f"invalid sparse unit context for {filename}: {exc}") from exc
        if not isinstance(projected, dict) or not isinstance(projected.get("entries", []), list):
            raise SpecialUnitContextError(f"invalid sparse unit context shape for {filename}")
        unit_context_state = "projected"
    else:
        # A translate manifest with no per-unit file/hash pair is the canonical
        # empty-context representation. Never guess or scan glossary files.
        unit_context_state = "empty"
        projected = {"schema_version": 1, "source_file": filename, "entries": []}
        context_sha = _stable_hash(
            {"schema_version": 1, "state": "empty", "source_file": filename}
        )

    # Check every parent snapshot used to build the sparse projection. The
    # projection hash alone cannot reveal a changed entity/glossary snapshot.
    parent_context_files = manifest.get("context_files") or {}
    parent_context_hashes = manifest.get("context_sha256") or {}
    if not isinstance(parent_context_files, Mapping) or not isinstance(parent_context_hashes, Mapping):
        raise SpecialUnitContextError("invalid parent translation context mappings; rerun translate prepare")
    current_parent_hashes = {}
    for name, relative in sorted(parent_context_files.items()):
        if not isinstance(relative, str) or not relative:
            raise SpecialUnitContextError(f"invalid parent context path for {name}; rerun translate prepare")
        parent_path = (output / relative).resolve()
        try:
            parent_path.relative_to(output)
        except ValueError as exc:
            raise SpecialUnitContextError(f"parent context escapes output directory: {name}") from exc
        if not parent_path.is_file():
            raise SpecialUnitContextError(f"parent context is missing: {name}; rerun translate prepare")
        actual_hash = hashlib.sha256(parent_path.read_bytes()).hexdigest()
        if parent_context_hashes.get(name) != actual_hash:
            raise SpecialUnitContextError(f"parent context changed after translate prepare: {name}; rerun translate prepare")
        current_parent_hashes[str(name)] = actual_hash
    entries = []
    for raw_entry in projected.get("entries", []):
        if not isinstance(raw_entry, dict):
            continue
        entry = dict(raw_entry)
        entry_kind = entry.get("kind", "domain")
        # A reference glossary is background only. Keep it visibly classified.
        if entry_kind not in {"domain", "book_entity", "reference"}:
            continue
        entry["kind"] = entry_kind
        entries.append(_validate_entry(entry, len(entries)))
    entries = sort_glossary_entries(entries)

    current_headings = build_toc_heading_contexts(output, source_dir=source_dir)
    has_production_toc = (output / "toc_tree_translated.json").is_file() and (
        output / "ocr_markdown" / "tree_progress.json"
    ).is_file()
    if has_production_toc:
        current_heading = current_headings.get(filename)
        if current_heading is None or current_heading != heading:
            raise SpecialUnitContextError(f"TOC heading context changed after translate prepare for {filename}; rerun translate prepare")
        heading = current_heading
    toc_hashes = {}
    for relative in ("toc_tree.json", "toc_tree_translated.json", "ocr_markdown/tree_progress.json"):
        toc_path = output / relative
        if toc_path.is_file():
            toc_hashes[relative] = hashlib.sha256(toc_path.read_bytes()).hexdigest()
    # The full-book context and TOC inventories above are audit-only. The
    # apply freshness contract is intentionally unit-scoped so an unrelated
    # glossary/TOC change does not invalidate this unit's accepted translation.
    contract = {
        "schema_version": 1,
        "kind": kind,
        "filename": filename,
        "language": {"source": source_language, "target": target_language},
        "source_sha256": source_sha,
        "unit_context_sha256": context_sha,
        "unit_context_state": unit_context_state,
        "heading_binding": heading if isinstance(heading, dict) else None,
        "entries": entries,
    }
    return {
        **contract,
        "parent_context_sha256": current_parent_hashes,
        "toc_sha256": toc_hashes,
        "contract_sha256": _stable_hash(contract),
    }


def term_candidates(term: str, context: Mapping[str, Any]) -> list[dict[str, Any]]:
    """Return suggestions only; callers must never treat these as replacements."""
    needle = _normalize_match_text(term)
    if not needle:
        return []
    matches = []
    for entry in context.get("entries", []) or []:
        if not isinstance(entry, Mapping):
            continue
        raw_source = entry.get("source", entry.get("original"))
        raw_target = entry.get("target", entry.get("suggested_translation"))
        sources = entry.get("source_forms") or _validate_forms(raw_source, "candidate source")
        targets = _validate_forms(raw_target, "candidate target")
        source = str(sources[0])
        target = str(targets[0])
        if not source or not target:
            continue
        forms = _term_forms(entry, include_cross_language=True, source_language=(context.get("language") or {}).get("source"))
        forms.extend(str(form) for form in sources if str(form).strip())
        forms = list(dict.fromkeys(forms))
        exact_form_types = {}
        for form in forms:
            normalized_form = _normalize_match_text(form)
            if needle == normalized_form:
                if normalized_form in {
                    _normalize_match_text(value)
                    for value in sources
                }:
                    match_type = "exact_source"
                elif normalized_form in {_normalize_match_text(v) for v in entry.get("variants", []) or []}:
                    match_type = "exact_variant"
                elif normalized_form in {
                    _normalize_match_text(v)
                    for values in (entry.get("aliases_by_language") or {}).values()
                    for v in (values if isinstance(values, list) else [values])
                }:
                    match_type = "exact_cross_language_alias"
                else:
                    match_type = "exact_alias"
                exact_form_types[normalized_form] = match_type
        exact_type = exact_form_types.get(needle)
        partial = False
        if not exact_type:
            # A partial hint must start and end on token boundaries. Permit a
            # shorter prefix fragment, while never finding `art` inside `cart`.
            partial_pattern = re.compile(r"(?<!\w)" + re.escape(needle).replace(r"\ ", r"\s+"))
            partial = any(
                bool(partial_pattern.search(_normalize_match_text(form)))
                or bool(_find_term_matches(needle, _normalize_match_text(form)))
                for form in forms
                if _normalize_match_text(form)
            )
        if exact_type or partial:
            entry_id = hashlib.sha256(
                json.dumps(dict(entry), ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
            ).hexdigest()
            matches.append({
                "entry_id": entry_id,
                "source": source,
                "target": target,
                "kind": entry.get("kind", "domain"),
                "policy": entry.get("policy"),
                "match_type": exact_type or "partial",
                "ambiguous": False,
            })
    # Every exact source/variant/alias match participates in authority order.
    # A lower-priority canonical spelling cannot mask a higher-priority alias.
    # Partial matches are hints only and are considered only when there are no
    # exact matches.
    exact = [item for item in matches if item["kind"] != "reference" and item["match_type"] != "partial"]
    candidates = exact or [item for item in matches if item["kind"] != "reference"]
    if candidates:
        from pdf2epub.glossary import _entry_priority

        def priority(item):
            return _entry_priority({"kind": item.get("kind"), "policy": item.get("policy")})

        priority_value = max(priority(item) for item in candidates)
        highest = [item for item in candidates if priority(item) == priority_value]
        if len({item["target"] for item in highest}) > 1:
            for item in highest:
                item["ambiguous"] = True
    return matches

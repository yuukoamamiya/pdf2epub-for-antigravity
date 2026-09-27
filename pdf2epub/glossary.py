"""Reusable, user-managed translation glossary support.

The book-specific ``translation_entities.json`` hand-off remains separate from
this module.  A domain glossary is a read-only, reusable translation context
selected by the book configuration and snapshotted into the book output
directory so a translation run is reproducible.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import dataclass, field
from functools import lru_cache
from re import escape as regex_escape
import re
import unicodedata
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


_MATCH_PUNCTUATION = str.maketrans(
    {
        "\u2018": "'",
        "\u2019": "'",
        "\u201b": "'",
        "\u2032": "'",
        "\u201c": '"',
        "\u201d": '"',
        "\u201f": '"',
        "\u2033": '"',
        "\u2010": "-",
        "\u2011": "-",
        "\u2012": "-",
        "\u2013": "-",
        "\u2014": "-",
        "\u2212": "-",
        "\u00ad": "",
    }
)


def _normalize_match_text(value: Any) -> str:
    """Normalize text for deterministic, literal glossary matching."""
    value = unicodedata.normalize("NFKC", str(value or ""))
    value = value.translate(_MATCH_PUNCTUATION).casefold()
    return " ".join(value.split())


def _matchable_source_text(text: str) -> str:
    """Keep visible Markdown text while ignoring structural/non-prose data."""
    lines = []
    fence_character = None
    for line in str(text or "").splitlines():
        fence = re.match(r"^\s{0,3}(`{3,}|~{3,})", line)
        if fence:
            character = fence.group(1)[0]
            if fence_character is None:
                fence_character = character
            elif fence_character == character:
                fence_character = None
            continue
        if fence_character is None:
            lines.append(line)
    value = "\n".join(lines)
    value = re.sub(r"<!--.*?-->", " ", value, flags=re.DOTALL)
    value = re.sub(r"`[^`\n]*`", " ", value)
    value = re.sub(r"!\[([^\]]*)\]\([^)]*\)", r" \1 ", value)
    value = re.sub(r"\[([^\]]+)\]\([^)]*\)", r" \1 ", value)
    value = re.sub(r"https?://\S+", " ", value, flags=re.IGNORECASE)
    value = re.sub(r"<[^>]+>", " ", value)
    return value


def _term_forms(entry: Mapping[str, Any]) -> List[str]:
    """Return all source forms that can identify one context entry."""
    original = entry.get("source") or entry.get("original") or ""
    forms = [original, *_optional_match_forms(entry.get("variants")), *_optional_match_forms(entry.get("aliases"))]
    result = []
    seen = set()
    for form in forms:
        cleaned = _clean(form)
        key = _normalize_match_text(cleaned)
        if key and key not in seen:
            seen.add(key)
            result.append(cleaned)
    return result


def _optional_match_forms(value: Any) -> List[str]:
    if isinstance(value, str):
        return [_clean(value)] if _clean(value) else []
    if isinstance(value, list):
        return [_clean(item) for item in value if _clean(item)]
    return []


def _entry_priority(entry: Mapping[str, Any]) -> int:
    if entry.get("kind") == "domain":
        return 3 if entry.get("policy") == "fixed" else 2
    return 1


def _entry_max_form_length(entry: Mapping[str, Any]) -> int:
    return max((len(_normalize_match_text(form)) for form in _term_forms(entry)), default=0)


def sort_glossary_entries(entries: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    """Order context entries by precedence, then by longest source form."""
    return sorted(
        entries,
        key=lambda entry: (
            -_entry_priority(entry),
            -_entry_max_form_length(entry),
            _normalize_match_text(entry.get("source") or entry.get("original")),
        ),
    )


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
        if "allow_short" in raw:
            if not isinstance(raw["allow_short"], bool):
                raise GlossaryError(
                    f"entries[{index}].allow_short must be a boolean"
                )
            if raw["allow_short"]:
                item["allow_short"] = True
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


def validate_reference_glossary_languages(
    glossary: Mapping[str, Any],
    target_language: str,
) -> List[str]:
    """Validate a glossary that is explicitly selected as reference-only.

    A reference glossary may use a different source language, but its target
    language must still match the current translation when it declares one.
    This keeps cross-language conceptual references opt-in without weakening
    the strict language contract for authoritative glossaries.
    """
    metadata = glossary.get("metadata", {})
    glossary_target = metadata.get("target_language")
    if glossary_target and _language_key(glossary_target) != _language_key(target_language):
        return [
            f"target language mismatch: glossary={glossary_target!r}, "
            f"translation={target_language!r}"
        ]
    return []


@dataclass(frozen=True)
class GlossaryBundle:
    """Selected glossary snapshots and the prompt rules that describe them."""

    context_files: Dict[str, Path]
    context_sha256: Dict[str, str]
    entries: int
    names: List[str]
    rules: List[str]
    source_files: Dict[str, Path] = field(default_factory=dict)
    source_sha256: Dict[str, str] = field(default_factory=dict)
    metadata: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def discover_glossary_candidates(
    glossary_dir: Path,
    source_language: str,
    target_language: str,
) -> List[Dict[str, Any]]:
    """Inspect local glossary candidates without selecting any automatically.

    Domain suitability still requires book-level judgment.  This function only
    performs deterministic file discovery, schema parsing, and language checks,
    so an agent can present a finite candidate list instead of guessing from
    filenames.
    """
    root = Path(glossary_dir)
    if not root.is_dir():
        return []
    candidates: List[Dict[str, Any]] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix.lower() not in SUPPORTED_SUFFIXES:
            continue
        try:
            relative_parts = path.relative_to(root).parts
        except ValueError:
            relative_parts = path.parts
        if any(part.lower() == "output" for part in relative_parts):
            continue
        if path.name.lower().startswith("readme") or ".example." in path.name.lower():
            continue
        try:
            glossary = load_glossary(path)
            metadata = glossary.get("metadata", {})
            errors = validate_glossary_languages(
                glossary, source_language, target_language
            )
            reference_errors = validate_reference_glossary_languages(
                glossary, target_language
            )
            for field_name in ("domain", "source_language", "target_language"):
                if not _clean(metadata.get(field_name)):
                    errors.append(f"metadata.{field_name} is missing")
            for field_name in ("domain", "target_language"):
                if not _clean(metadata.get(field_name)):
                    reference_errors.append(f"metadata.{field_name} is missing")
            reference_reason = ""
            if not reference_errors and errors:
                reference_reason = (
                    "source-language mismatch is allowed only because this is "
                    "explicitly reference-only"
                )
            candidates.append(
                {
                    "path": str(path),
                    "name": metadata.get("name"),
                    "domain": metadata.get("domain"),
                    "source_language": metadata.get("source_language"),
                    "target_language": metadata.get("target_language"),
                    "version": metadata.get("version"),
                    "entries": len(glossary.get("entries", [])),
                    "eligible": not errors,
                    "errors": errors,
                    "reference_eligible": not reference_errors,
                    "reference_errors": reference_errors,
                    "reference_reason": reference_reason,
                }
            )
        except GlossaryError as exc:
            candidates.append(
                {
                    "path": str(path),
                    "eligible": False,
                    "errors": [str(exc)],
                    "reference_eligible": False,
                    "reference_errors": [str(exc)],
                }
            )
    return candidates


@lru_cache(maxsize=4096)
def _compile_term_pattern(
    normalized_term: str,
    allow_short: bool,
) -> Optional[re.Pattern]:
    """Compile and cache one normalized literal term pattern."""
    if not normalized_term or len(normalized_term) < (2 if allow_short else 3):
        return None
    pattern = (
        r"(?<!\w)"
        + regex_escape(normalized_term).replace(r"\ ", r"\s+")
        + r"(?!\w)"
    )
    return re.compile(pattern)


def _find_term_matches(
    normalized_text: str,
    term: str,
    *,
    allow_short: bool = False,
) -> List[re.Match]:
    """Find literal normalized term matches with Unicode word boundaries."""
    normalized_term = _normalize_match_text(term)
    pattern = _compile_term_pattern(normalized_term, allow_short)
    if pattern is None:
        return []
    return list(pattern.finditer(normalized_text))


def _term_occurs(text: str, term: str, *, allow_short: bool = False) -> bool:
    """Match a glossary form without matching it inside a larger word."""
    normalized_text = _normalize_match_text(_matchable_source_text(text))
    return bool(_find_term_matches(normalized_text, term, allow_short=allow_short))


def _entry_match_kind(entry: Mapping[str, Any], form: str) -> str:
    original = _normalize_match_text(entry.get("source") or entry.get("original"))
    normalized = _normalize_match_text(form)
    if normalized == original:
        return "source"
    if normalized in {
        _normalize_match_text(value)
        for value in _optional_match_forms(entry.get("variants"))
    }:
        return "variant"
    return "alias"


def _select_matching_entries(
    text: str,
    entries: List[Dict[str, Any]],
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], List[Dict[str, Any]]]:
    """Select entries and return selected plus suppressed hit audit trails."""
    normalized_text = _normalize_match_text(_matchable_source_text(text))
    candidates = []
    for index, entry in enumerate(entries):
        for form in _term_forms(entry):
            normalized_form = _normalize_match_text(form)
            for match in _find_term_matches(
                normalized_text,
                normalized_form,
                allow_short=bool(entry.get("allow_short")),
            ):
                candidates.append(
                    {
                        "entry_index": index,
                        "entry": entry,
                        "form": form,
                        "start": match.start(),
                        "end": match.end(),
                        "length": len(normalized_form),
                    }
                )

    # Resolve overlap deterministically: domain fixed > preferred > book
    # entity, then longest source form.  A short term may still be selected
    # when it occurs elsewhere without overlapping a longer term.
    candidates.sort(
        key=lambda item: (
            -_entry_priority(item["entry"]),
            -item["length"],
            item["start"],
            item["end"],
            item["entry_index"],
        )
    )
    occupied = []
    selected: Dict[int, Dict[str, Any]] = {}
    suppressed = []
    suppressed_keys = set()
    for candidate in candidates:
        start, end = candidate["start"], candidate["end"]
        overlapping = next(
            (
                winner
                for other_start, other_end, winner in occupied
                if start < other_end and other_start < end
            ),
            None,
        )
        if overlapping is not None:
            if overlapping["entry_index"] != candidate["entry_index"]:
                key = (
                    candidate["entry_index"],
                    candidate["form"],
                    overlapping["entry_index"],
                    overlapping["form"],
                )
                if key not in suppressed_keys:
                    suppressed_keys.add(key)
                    suppressed.append(
                        {
                            "source": candidate["entry"].get("source")
                            or candidate["entry"].get("original"),
                            "target": candidate["entry"].get("target")
                            or candidate["entry"].get("suggested_translation"),
                            "kind": candidate["entry"].get("kind", "domain"),
                            "matched_form": candidate["form"],
                            "reason": "overlapped_by_higher_priority_or_longer_term",
                            "winner": overlapping["entry"].get("source")
                            or overlapping["entry"].get("original"),
                            "winner_form": overlapping["form"],
                        }
                    )
            continue
        occupied.append((start, end, candidate))
        index = candidate["entry_index"]
        hit = selected.setdefault(
            index,
            {
                "entry": candidate["entry"],
                "matched_forms": [],
                "match_types": [],
            },
        )
        form = candidate["form"]
        if form not in hit["matched_forms"]:
            hit["matched_forms"].append(form)
        match_type = _entry_match_kind(candidate["entry"], form)
        if match_type not in hit["match_types"]:
            hit["match_types"].append(match_type)

    selected_hits = [selected[index] for index in sorted(selected)]
    selected_entries = sort_glossary_entries(
        [hit["entry"] for hit in selected_hits]
    )
    selected_by_key = {
        json.dumps(entry, ensure_ascii=False, sort_keys=True): entry
        for entry in selected_entries
    }
    ordered_hits = []
    for hit in selected_hits:
        entry = hit["entry"]
        key = json.dumps(entry, ensure_ascii=False, sort_keys=True)
        if key not in selected_by_key:
            continue
        ordered_hits.append(
            {
                "source": entry.get("source") or entry.get("original"),
                "target": entry.get("target") or entry.get("suggested_translation"),
                "kind": entry.get("kind", "domain"),
                "matched_forms": hit["matched_forms"],
                "match_types": hit["match_types"],
            }
        )
    return selected_entries, ordered_hits, suppressed


def build_unit_glossary_contexts(
    output_dir: Path,
    source_dir: Path,
    glossary_context_files: Mapping[str, Path],
    entity_path: Optional[Path] = None,
) -> Dict[str, Path]:
    """Write compact, per-unit terminology contexts.

    Full normalized snapshots remain available for audit, while translation
    prompts can point each Subagent at only the entries occurring in its unit.
    Exact matching is intentionally conservative; an empty subset is valid and
    tells the Subagent to fall back to normal translation judgment.
    """
    source_dir = Path(source_dir)
    context_dir = Path(output_dir) / "translation_glossaries" / "unit_contexts"
    context_dir.mkdir(parents=True, exist_ok=True)
    domain_entries: List[Dict[str, Any]] = []
    for name, path in glossary_context_files.items():
        if not str(name).startswith("domain_glossary_") or not Path(path).is_file():
            continue
        try:
            glossary = load_glossary(path)
        except GlossaryError:
            continue
        for entry in glossary["entries"]:
            domain_entries.append({"kind": "domain", **entry})

    entity_entries: List[Dict[str, Any]] = []
    if entity_path and Path(entity_path).is_file():
        try:
            data = json.loads(Path(entity_path).read_text(encoding="utf-8"))
            for collection in ("characters", "places", "organizations", "terms", "races", "items"):
                for entry in data.get(collection, []) or []:
                    if isinstance(entry, dict) and entry.get("original") and entry.get("suggested_translation"):
                        entity_entries.append(
                            {
                                "kind": "book_entity",
                                "category": collection,
                                "original": entry["original"],
                                "target": entry["suggested_translation"],
                                **(
                                    {"allow_short": True}
                                    if entry.get("allow_short") is True
                                    else {}
                                ),
                                **(
                                    {"variants": _optional_match_forms(entry.get("variants"))}
                                    if _optional_match_forms(entry.get("variants"))
                                    else {}
                                ),
                                **(
                                    {"aliases": _optional_match_forms(entry.get("aliases"))}
                                    if _optional_match_forms(entry.get("aliases"))
                                    else {}
                                ),
                                **(
                                    {"note": entry.get("description") or entry.get("note", "")}
                                    if entry.get("description") or entry.get("note")
                                    else {}
                                ),
                            }
                        )
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
            pass

    if not domain_entries and not entity_entries:
        return {}

    result: Dict[str, Path] = {}
    for source in sorted(Path(source_dir).glob("*.md")):
        try:
            text = source.read_text(encoding="utf-8")
        except (OSError, UnicodeError):
            continue
        candidates: List[Dict[str, Any]] = []
        for entry in domain_entries:
            candidates.append(entry)
        for entry in entity_entries:
            candidates.append(entry)
        selected, hits, suppressed_hits = _select_matching_entries(text, candidates)
        context = {
            "schema_version": 1,
            "source_file": source.name,
            "entries": selected,
            "selection": "normalized_exact_source_form_match",
        }
        context_path = context_dir / f"{source.stem}.json"
        context_path.write_text(
            json.dumps(context, ensure_ascii=False, separators=(",", ":")) + "\n",
            encoding="utf-8",
        )
        report_dir = Path(output_dir) / "translation_glossaries" / "keyword_hits"
        report_dir.mkdir(parents=True, exist_ok=True)
        (report_dir / f"{source.stem}.json").write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "source_file": source.name,
                    "selection": "normalized_exact_source_form_match",
                    "hits": hits,
                    "suppressed_hits": suppressed_hits,
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        result[source.name] = context_path
    return result


def build_metadata_glossary_context(
    output_dir: Path,
    context_files: Mapping[str, Path],
    source_text: str,
) -> Optional[Path]:
    """Write a small terminology context for EPUB metadata translation.

    Full snapshots remain attached to the task for provenance and validation,
    but metadata translation only needs entries occurring in the title, TOC,
    description, or rights fields.  Reference-only glossaries are handled by
    the caller because their cross-language entries cannot be selected safely
    by exact source-form matching.
    """
    candidates: List[Dict[str, Any]] = []
    for name, path in context_files.items():
        if not Path(path).is_file():
            continue
        if str(name).startswith("domain_glossary_"):
            try:
                glossary = load_glossary(path)
            except GlossaryError:
                continue
            for entry in glossary["entries"]:
                candidates.append({"kind": "domain", **entry})
        elif str(name) == "translation_entities":
            try:
                data = json.loads(Path(path).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError, TypeError):
                continue
            for collection in (
                "characters",
                "places",
                "organizations",
                "terms",
                "races",
                "items",
            ):
                for entry in data.get(collection, []) or []:
                    if not isinstance(entry, dict):
                        continue
                    original = entry.get("original")
                    target = entry.get("suggested_translation")
                    if original and target:
                        item = {
                            "kind": "book_entity",
                            "category": collection,
                            "original": original,
                            "target": target,
                        }
                        variants = _optional_match_forms(entry.get("variants"))
                        aliases = _optional_match_forms(entry.get("aliases"))
                        if variants:
                            item["variants"] = variants
                        if aliases:
                            item["aliases"] = aliases
                        note = entry.get("description") or entry.get("note")
                        if note:
                            item["note"] = note
                        candidates.append(item)

    selected, _hits, _suppressed_hits = _select_matching_entries(source_text, candidates)
    if not selected:
        return None
    context = {
        "schema_version": 1,
        "purpose": "metadata_translation",
        "entries": selected,
        "selection": "exact_source_form_match",
    }
    context_path = Path(output_dir) / "translation_glossaries" / "metadata_context.json"
    context_path.parent.mkdir(parents=True, exist_ok=True)
    context_path.write_text(
        json.dumps(context, ensure_ascii=False, separators=(",", ":")) + "\n",
        encoding="utf-8",
    )
    return context_path


def _configured_items(
    config: Mapping[str, Any], key: str = "glossaries"
) -> List[Any]:
    translation = config.get("translation", {}) or {}
    configured = translation.get(key, [])
    if configured is None:
        return []
    if isinstance(configured, (str, Path, dict)):
        return [configured]
    if not isinstance(configured, list):
        raise GlossaryError(f"translation.{key} must be an array")
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
    items = _configured_items(config, "glossaries")
    reference_items = _configured_items(config, "reference_glossaries")
    if not items and not reference_items:
        translation = config.get("translation", {}) or {}
        selection_path = Path(output_dir) / "glossary_selection.json"
        selection_path.parent.mkdir(parents=True, exist_ok=True)
        selection_path.write_text(
            json.dumps(
                {
                    "schema_version": 1,
                    "mode": (
                        "explicit_none"
                        if "glossaries" in translation or "reference_glossaries" in translation
                        else "unconfigured"
                    ),
                    "selected": [],
                },
                ensure_ascii=False,
                indent=2,
            )
            + "\n",
            encoding="utf-8",
        )
        return GlossaryBundle({}, {}, 0, [], [])

    snapshot_dir = Path(output_dir) / "translation_glossaries"
    snapshot_dir.mkdir(parents=True, exist_ok=True)
    context_files: Dict[str, Path] = {}
    context_sha256: Dict[str, str] = {}
    names: List[str] = []
    total_entries = 0
    seen_sources: Dict[str, str] = {}
    source_files: Dict[str, Path] = {}
    source_sha256: Dict[str, str] = {}
    metadata_by_context: Dict[str, Dict[str, Any]] = {}
    selected_records: List[Dict[str, Any]] = []

    used_snapshot_names = set()
    for kind, configured_items in (
        ("authoritative", items),
        ("reference", reference_items),
    ):
        for index, item in enumerate(configured_items, 1):
            if isinstance(item, str):
                raw_path = item
                configured_id = ""
            elif isinstance(item, dict):
                raw_path = item.get("path") or item.get("file")
                configured_id = _clean(item.get("id"))
                if not raw_path:
                    raise GlossaryError(
                        f"translation.{('glossaries' if kind == 'authoritative' else 'reference_glossaries')}[{index}] requires path"
                    )
            else:
                raise GlossaryError(
                    f"translation.{('glossaries' if kind == 'authoritative' else 'reference_glossaries')}[{index}] must be a path or object"
                )

            source_path = _resolve_config_path(str(raw_path), config_path)
            glossary = load_glossary(source_path)
            language_errors = (
                validate_glossary_languages(glossary, source_language, target_language)
                if kind == "authoritative"
                else validate_reference_glossary_languages(glossary, target_language)
            )
            if language_errors:
                raise GlossaryError(
                    f"glossary {source_path.name}: " + "; ".join(language_errors)
                )
            metadata = glossary["metadata"]
            glossary_id = configured_id or _clean(metadata.get("name")) or source_path.stem
            safe_id = sanitize_filename(glossary_id) or f"glossary_{index:03d}"
            snapshot_name = (
                f"reference_{safe_id}.json" if kind == "reference" else f"{safe_id}.json"
            )
            snapshot_path = snapshot_dir / snapshot_name
            if snapshot_name in used_snapshot_names:
                raise GlossaryError(f"duplicate glossary id: {glossary_id}")
            used_snapshot_names.add(snapshot_name)
            snapshot_path.write_text(
                json.dumps(glossary, ensure_ascii=False, indent=2) + "\n",
                encoding="utf-8",
            )

            prefix = "domain" if kind == "authoritative" else "reference"
            context_name = f"{prefix}_glossary_{index:03d}"
            context_files[context_name] = snapshot_path
            context_sha256[context_name] = hashlib.sha256(snapshot_path.read_bytes()).hexdigest()
            source_files[context_name] = source_path
            source_sha256[context_name] = hashlib.sha256(source_path.read_bytes()).hexdigest()
            metadata_by_context[context_name] = dict(metadata)
            selected_records.append(
                {
                    "kind": kind,
                    "context": context_name,
                    "id": glossary_id,
                    "configured_path": str(raw_path),
                    "resolved_path": str(source_path),
                    "source_sha256": source_sha256[context_name],
                    "snapshot": str(snapshot_path),
                    "snapshot_sha256": context_sha256[context_name],
                    "metadata": dict(metadata),
                }
            )
            if kind == "authoritative":
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
        "Terminology precedence is: domain `fixed`, domain `preferred`, then book-specific entities; prefer the longest matching source form and report unresolved conflicts.",
        "Never replace text mechanically in a way that changes HTML tags, attributes, entities, anchors, formulas, or LaTeX commands.",
    ]
    if reference_items:
        rules.extend(
            [
                "Reference-only glossaries are read-only background material; never modify the original files or their snapshots.",
                "Reference-only glossaries do not establish terminology precedence and must never override an authoritative glossary or book-specific entity.",
                "Consult reference-only glossaries for conceptual correspondences and established target-language names when relevant, especially across source languages; do not mechanically apply a reference entry when the source text does not support the correspondence.",
                "Do not add inferred variants, translations, or corrections back into any glossary file. If a reference is ambiguous, use normal translation judgment and keep the glossary unchanged.",
            ]
        )
    selection_path = Path(output_dir) / "glossary_selection.json"
    selection_path.write_text(
        json.dumps(
            {
                "schema_version": 1,
                "mode": "explicit",
                "selected": selected_records,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return GlossaryBundle(
        context_files=context_files,
        context_sha256=context_sha256,
        entries=total_entries,
        names=names,
        rules=rules,
        source_files=source_files,
        source_sha256=source_sha256,
        metadata=metadata_by_context,
    )


def validate_translation_context(
    output_dir: Path,
    manifest_name: str,
    config: Mapping[str, Any],
) -> Dict[str, Any]:
    """Validate all read-only translation contexts attached to a task."""
    manifest_path = Path(output_dir) / manifest_name
    translation = config.get("translation", {}) or {}
    configured_glossaries = bool(_configured_items(config, "glossaries"))
    configured_reference_glossaries = bool(
        _configured_items(config, "reference_glossaries")
    )
    source_language = translation.get("source_language")
    target_language = translation.get("target_language")
    require_entities = bool(translation.get("require_entities", True))
    if not manifest_path.is_file():
        errors = []
        if configured_glossaries or configured_reference_glossaries:
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
    unit_context_files = manifest.get("unit_context_files", {}) or {}
    unit_context_hashes = manifest.get("unit_context_sha256", {}) or {}
    errors: List[str] = []
    entity_path: Optional[Path] = None
    glossary_names: List[str] = []
    glossary_entries = 0
    reference_names: List[str] = []
    reference_entries = 0

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
                if source_language and target_language:
                    errors.extend(
                        validate_glossary_languages(
                            glossary, source_language, target_language
                        )
                    )
                glossary_names.append(glossary["metadata"]["name"])
                glossary_entries += len(glossary["entries"])
            except GlossaryError as exc:
                errors.append(str(exc))
        elif str(name).startswith("reference_glossary_"):
            try:
                glossary = load_glossary(path)
                if target_language:
                    errors.extend(
                        validate_reference_glossary_languages(
                            glossary, target_language
                        )
                    )
                reference_names.append(glossary["metadata"]["name"])
                reference_entries += len(glossary["entries"])
            except GlossaryError as exc:
                errors.append(str(exc))

    for unit_name, relative in unit_context_files.items():
        path = (root / str(relative)).resolve()
        try:
            path.relative_to(root)
        except ValueError:
            errors.append(
                f"unit translation context escapes output directory: {relative}"
            )
            continue
        if not path.is_file():
            errors.append(f"unit translation context is missing: {relative}")
            continue
        actual_hash = hashlib.sha256(path.read_bytes()).hexdigest()
        if unit_context_hashes.get(unit_name) != actual_hash:
            errors.append(
                f"unit translation context changed after task preparation: {relative}"
            )

    skipped = set(manifest.get("skipped_context_files", []) or [])
    if require_entities and entity_path is None and "translation_entities" not in skipped:
        errors.append("translation_entities context is required but was not attached")
    if entity_path is not None:
        try:
            from pdf2epub.entity_extractor import validate_entities

            entity_data = json.loads(entity_path.read_text(encoding="utf-8"))
            errors.extend(
                validate_entities(
                    entity_data,
                    config.get("title"),
                    source_language,
                    target_language,
                )
            )
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"invalid translation_entities.json: {exc}")
    if configured_glossaries and not any(
        str(name).startswith("domain_glossary_") for name in context_files
    ):
        errors.append("configured external glossaries were not attached to the task")
    if configured_reference_glossaries and not any(
        str(name).startswith("reference_glossary_") for name in context_files
    ):
        errors.append(
            "configured reference glossaries were not attached to the task"
        )

    return {
        "valid": not errors,
        "errors": errors,
        "glossaries": glossary_names,
        "glossary_entries": glossary_entries,
        "reference_glossaries": reference_names,
        "reference_glossary_entries": reference_entries,
        "context_files": context_files,
        "unit_context_files": unit_context_files,
        "skipped_context_files": sorted(skipped),
    }

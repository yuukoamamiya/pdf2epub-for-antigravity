"""Read-only per-unit validation for opt-in dynamic terminology memory."""

from __future__ import annotations

import hashlib
import json
from html.parser import HTMLParser
from pathlib import Path
from typing import Any, Iterable, Mapping

from pdf2epub.dynamic_terminology import (
    affected_target_consistency_state,
    current_effective_projection,
    publication_target_inventory,
    publication_target_inventory_complete,
)
from pdf2epub.glossary import _normalize_match_text
from pdf2epub.pipeline_policy import PipelinePolicy


class _VisibleHTML(HTMLParser):
    """Extract visible XHTML text, omitting comments, attributes and code-like nodes."""

    HIDDEN = {"script", "style", "code", "pre", "rt", "rp"}
    BLOCK = {"address", "article", "aside", "blockquote", "br", "div", "dl", "fieldset", "figcaption", "figure", "footer", "form", "h1", "h2", "h3", "h4", "h5", "h6", "header", "hr", "li", "main", "nav", "ol", "p", "section", "table", "td", "th", "tr", "ul"}

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.hidden = 0

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag.lower() in self.BLOCK:
            self.parts.append(" ")
        if tag.lower() in self.HIDDEN:
            self.hidden += 1

    def handle_endtag(self, tag: str) -> None:
        if tag.lower() in self.HIDDEN and self.hidden:
            self.hidden -= 1
        if tag.lower() in self.BLOCK:
            self.parts.append(" ")

    def handle_data(self, data: str) -> None:
        if not self.hidden:
            self.parts.append(data)


def _sha_file(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def _confined(root: Path, path: str | Path, label: str) -> Path:
    base = Path(root).resolve()
    raw = Path(path)
    candidate = raw if raw.is_absolute() else base / raw
    resolved = candidate.resolve()
    if not resolved.is_relative_to(base):
        raise ValueError(f"{label} escapes book workspace")
    return resolved


def _visible(text: str, task: str) -> str:
    if task == "translate":
        from pdf2epub.markdown_to_html import convert_markdown_to_html

        text = convert_markdown_to_html(text, standalone=False)
    parser = _VisibleHTML()
    parser.feed(text)
    parser.close()
    return "".join(parser.parts)


def _target_has_term(text: str, entry: Mapping[str, Any], task: str) -> bool:
    visible = _normalize_match_text(_visible(text, task))
    target = _normalize_match_text(entry.get("target", ""))
    # Target terms may be CJK or other scripts without whitespace word
    # boundaries. Source-side selection already applied the glossary's exact
    # normalized boundary contract; target attestation is literal normalized
    # containment in visible prose.
    return bool(target and target in visible)


def _manifest(output_dir: Path, task: str) -> dict[str, Any]:
    if task not in {"translate", "translate-html"}:
        raise ValueError("task must be translate or translate-html")
    path = _confined(output_dir, f"{task}_subagent_manifest.json", "task manifest")
    value = json.loads(path.read_text(encoding="utf-8"))
    if not isinstance(value, dict) or value.get("schema_version") != 1 or value.get("task") != task:
        raise ValueError("task manifest has invalid schema or task")
    return value


def validate_dynamic_terminology_units(
    output_dir: Path,
    *,
    task: str,
    source_dir: Path,
    target_dir: Path,
    config: Mapping[str, Any],
    selected_files: Iterable[str] | None = None,
    authoritative_files: Iterable[str] | None = None,
) -> dict[str, Any]:
    """Validate frozen/current memory context and existing-target reviews.

    This function is intentionally read-only. It returns per-file attestations
    and errors; callers must gate validated-copy creation and receipt writes on
    ``all_passed``. Disabled or absent configuration leaves legacy workflows
    unchanged.
    """
    if not PipelinePolicy.from_config(config).dynamic_terminology_enabled:
        return {"enabled": False, "valid": True, "files": {}, "errors": []}
    output_dir = Path(output_dir).resolve()
    source_dir = _confined(output_dir, source_dir, "source directory")
    target_dir = _confined(output_dir, target_dir, "target directory")
    manifest = _manifest(output_dir, task)
    dynamic_manifest = manifest.get("dynamic_terminology")
    if not isinstance(dynamic_manifest, Mapping) or dynamic_manifest.get("schema_version") != 1 or dynamic_manifest.get("enabled") is not True:
        raise ValueError("enabled dynamic terminology is missing from the authoritative task manifest")
    if not isinstance(manifest.get("source_dir"), str) or not isinstance(manifest.get("target_dir"), str):
        raise ValueError("task manifest is missing authoritative source/target directories")
    if _confined(output_dir, manifest["source_dir"], "manifest source directory") != source_dir:
        raise ValueError("source directory differs from authoritative task manifest")
    if _confined(output_dir, manifest["target_dir"], "manifest target directory") != target_dir:
        raise ValueError("target directory differs from authoritative task manifest")
    policy = config.get("translation", {}) or {}
    source_language = manifest.get("source_language")
    target_language = manifest.get("target_language")
    if not isinstance(source_language, str) or not source_language.strip() or not isinstance(target_language, str) or not target_language.strip():
        raise ValueError("task manifest is missing the explicit source/target language pair")
    book_id = str(config.get("title") or manifest.get("book_title") or manifest.get("book_id") or "")
    if not book_id:
        raise ValueError("book identity is missing for dynamic terminology validation")
    chapter_groups = manifest.get("chapter_groups", {})
    if not isinstance(chapter_groups, Mapping):
        raise ValueError("task manifest chapter_groups is malformed")
    source_inventory = manifest.get("source_sha256", {})
    unit_contexts = manifest.get("unit_context_files", {}) or {}
    unit_context_hashes = manifest.get("unit_context_sha256", {}) or {}
    base_contexts = (
        dynamic_manifest.get("base_unit_context_files", {})
        or manifest.get("base_unit_context_files", {})
        or manifest.get("static_unit_context_files", {})
        or {}
    )
    if not isinstance(unit_contexts, Mapping) or not isinstance(unit_context_hashes, Mapping) or not isinstance(base_contexts, Mapping):
        raise ValueError("task manifest unit context mappings are malformed")
    if not isinstance(source_inventory, Mapping) or not source_inventory:
        raise ValueError("authoritative source inventory is missing or empty")
    declared_files = manifest.get("files")
    if not isinstance(declared_files, list) or set(declared_files) != set(source_inventory):
        raise ValueError("task manifest declared files and source hashes disagree")
    if set(unit_contexts) != set(source_inventory) or set(unit_context_hashes) != set(source_inventory):
        raise ValueError("dynamic unit context path/hash inventory is incomplete")
    if any(not isinstance(value, str) or len(value) != 64 or any(c not in "0123456789abcdef" for c in value) for value in (*source_inventory.values(), *unit_context_hashes.values())):
        raise ValueError("source or unit context inventory contains an invalid SHA-256")
    preexisting_targets = dynamic_manifest.get("preexisting_target_inventory")
    preexisting_sha = dynamic_manifest.get("preexisting_target_inventory_sha256")
    if not isinstance(preexisting_targets, Mapping) or preexisting_sha != hashlib.sha256(
        json.dumps(preexisting_targets, ensure_ascii=False, sort_keys=True, separators=(",", ":")).encode("utf-8")
    ).hexdigest():
        raise ValueError("preexisting target inventory hash is missing or stale")
    lineage = affected_target_consistency_state(
        output_dir, book_id=book_id, source_language=source_language, target_language=target_language,
    )
    published_inventory = publication_target_inventory(
        output_dir, book_id=book_id, source_language=source_language,
        target_language=target_language,
    )
    inventory_complete = publication_target_inventory_complete(
        output_dir, book_id=book_id, source_language=source_language,
        target_language=target_language,
    )
    authoritative = None if authoritative_files is None else set(authoritative_files)
    if authoritative is not None and authoritative != set(source_inventory):
        raise ValueError("current declared source inventory differs from the prepared manifest")
    names = list(selected_files) if selected_files is not None else sorted(authoritative or source_inventory)
    if not names or any(name not in source_inventory for name in names) or (authoritative is not None and any(name not in authoritative for name in names)):
        raise ValueError("selected files must be present in the authoritative source inventory")
    files: dict[str, Any] = {}
    all_errors: list[dict[str, str]] = []
    if selected_files is None and authoritative is None:
        actual_sources = {path.name for path in source_dir.glob("*.md") if path.is_file()}
        if actual_sources != set(source_inventory):
            all_errors.append({
                "file": "<source-inventory>",
                "error": "source unit inventory changed after handoff; rerun translate --resume",
            })
    for name in names:
        errors: list[str] = []
        try:
            source = _confined(source_dir, name, "source unit")
            target = _confined(target_dir, name, "target unit")
            if source.parent != source_dir or target.parent != target_dir or source.suffix != ".md":
                raise ValueError("unit path must be a direct Markdown child")
            source_hash = _sha_file(source)
            if source_inventory.get(name) != source_hash:
                errors.append("source hash differs from prepared manifest; prepare/resume again")
            base_path = None
            rel = base_contexts.get(name)
            if rel:
                base_path = _confined(output_dir, rel, "static unit context")
            projection = current_effective_projection(
                output_dir, task, source_dir, name, book_id=book_id,
                source_language=source_language, target_language=target_language,
                chapter_groups=chapter_groups, base_context_path=base_path,
            )
            prepared_rel = unit_contexts.get(name)
            if not prepared_rel:
                errors.append("prepared terminology unit context is missing; run translate --resume")
            else:
                prepared_path = _confined(output_dir, prepared_rel, "prepared unit context")
                prepared_bytes = prepared_path.read_bytes()
                expected_context_hash = unit_context_hashes.get(name)
                if expected_context_hash != hashlib.sha256(prepared_bytes).hexdigest():
                    errors.append("prepared unit context hash is stale or missing")
                prepared = json.loads(prepared_bytes.decode("utf-8"))
                if not isinstance(prepared, dict) or prepared.get("schema_version") != 1 or not isinstance(prepared.get("entries"), list):
                    errors.append("prepared dynamic terminology context has unsupported schema")
                elif prepared.get("source_file") != name or prepared.get("language_pair") != {"source": source_language, "target": target_language}:
                    errors.append("prepared unit context identity or language pair does not match this task")
                elif prepared["entries"] != projection["entries"]:
                    errors.append("effective terminology changed since handoff; run translate --resume")
            target_hash = _sha_file(target) if target.is_file() else None
            attestation = {
                "source_sha256": source_hash,
                "target_sha256": target_hash,
                "projection_sha256": projection["projection_sha256"],
                "memory_entries": projection["memory_entries"],
                "prepared_unit_context_sha256": unit_context_hashes.get(name),
            }
            if target_hash:
                target_text = target.read_text(encoding="utf-8")
                for entry in projection["memory_entries"]:
                    # The dynamic projection contains only entries that matched
                    # the source through the shared glossary selector. Do not
                    # repeat matching with a second parser here: renderer
                    # inline boundaries can differ while the target obligation
                    # remains attached to this prepared effective projection.
                    if not _target_has_term(target_text, entry, task):
                        errors.append(f"required memory target term is missing for source form {entry.get('source')!r}")
                publication_rows = published_inventory.get(name, [])
                has_effective_memory_hit = bool(projection["memory_entries"])
                if has_effective_memory_hit and not inventory_complete:
                    errors.append("published target inventory is incomplete; refusing to classify this target as new")
                current_scope = {"kind": "chapter", "id": next((c for c, fs in chapter_groups.items() if name in fs), None)}
                review_required = has_effective_memory_hit and any(
                    row.get("review_required_change") is True
                    and row.get("projection_before_sha256") != row.get("projection_after_sha256")
                    and row.get("projection_after_sha256", row.get("approved_projection_sha256")) == projection["projection_sha256"]
                    and row.get("scope") == current_scope
                    for row in publication_rows
                )
                reviews = [
                    row for row in lineage.get(name, [])
                    if row.get("source_sha256") == source_hash
                    and row.get("target_sha256") == target_hash
                    and row.get("scope") == current_scope
                    and row.get("language_pair") == {"source": source_language, "target": target_language}
                    and row.get("approved_projection_sha256", row.get("effective_projection_sha256")) == projection["projection_sha256"]
                ]
                review = reviews[0] if reviews else None
                historical_block = any(
                    row.get("decision") == "retranslate_required"
                    and row.get("scope") == current_scope
                    and row.get("language_pair") == {"source": source_language, "target": target_language}
                    for row in lineage.get(name, [])
                ) and has_effective_memory_hit
                if historical_block and (review is None or review.get("decision") != "consistent"):
                    errors.append("existing target has an unresolved retranslate_required review; a current consistency review is required")
                elif review_required and (review is None or review.get("decision") != "consistent"):
                    errors.append("existing target lacks consistency review bound to current source, target, scope, language, and effective projection")
            files[name] = {**attestation, "valid": not errors, "errors": errors}
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError) as exc:
            errors.append(str(exc))
            files[name] = {"valid": False, "errors": errors}
        all_errors.extend({"file": name, "error": error} for error in errors)
    return {
        "enabled": True,
        "valid": not all_errors,
        "all_passed": not all_errors,
        "files": files,
        "prepared_unit_context_sha256": {
            name: item["prepared_unit_context_sha256"]
            for name, item in files.items()
            if isinstance(item.get("prepared_unit_context_sha256"), str)
        },
        "errors": all_errors,
    }


__all__ = ["validate_dynamic_terminology_units"]

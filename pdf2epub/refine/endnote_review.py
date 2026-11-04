"""Review ambiguous centralized endnote definition boundaries.

Most numbered note definitions start at a clear boundary: the beginning of a
file, after a blank line, or after a sentence-ending line.  A small number of
``N.`` lines follow a comma, colon, or wrapped word and may actually be a page
number plus the start of a new sentence.  Only those ambiguous boundary lines
are handed to the workspace Subagent; clear definitions stay deterministic.
"""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from pathlib import PurePosixPath
from typing import Any, Mapping, Optional

from ..workflow_contracts import atomic_write_json, sha256_file
from ..footnote_normalization import _is_math_or_formula_superscript
from .endnote_scopes import (
    EndnoteScopeError,
    build_endnote_scope_directory,
    markdown_fence_mask,
    scope_for_line,
)


ENDNOTE_REVIEW_SCHEMA_VERSION = 2
AMBIGUITIES_FILENAME = "endnote_definition_ambiguities.json"
DECISIONS_FILENAME = "endnote_definition_decisions.json"
VALIDATION_FILENAME = "endnote_definition_validation.json"

NUMBERED_PUNCT_DEF_RE = re.compile(
    r"^[ \t]{0,3}(?:[-*+][ \t]+)?(?P<key>\d+)[ \t]*[.)。：：][ \t]+(?P<content>\S.*)$"
)
_TERMINAL_PUNCTUATION = frozenset(". ! ? 。 ！ ？ ” ’ \" '".split())
_MARKDOWN_HEADING_RE = re.compile(r"^#{1,6}\s+\S")
_INLINE_SUP_RE = re.compile(r" {2,}<sup\b[^>]*>[ \t]*(?P<key>\d+)[ \t]*</sup>[ \t]+", re.IGNORECASE)
DECISION_ROLES = frozenset({"definition", "continuation"})
_UNRESOLVED_ROLES = frozenset({"review_required"})


def _inside_inline_code_or_math(line: str, position: int) -> bool:
    for delimiter_re in (
        re.compile(r"(?<!\\)(`+)(?!`)"),
        re.compile(r"(?<!\\)(\$\$|(?<!\$)\$(?!\$))"),
    ):
        opened: Optional[tuple[str, int]] = None
        for match in delimiter_re.finditer(line):
            delimiter = match.group(1)
            if opened is None:
                opened = (delimiter, match.start())
            elif opened[0] == delimiter:
                if opened[1] < position < match.end():
                    return True
                opened = None
    return False


def inline_endnote_candidates(line: str, line_number: int) -> list[dict[str, Any]]:
    """Return conservative numeric inline note candidates on one definition line."""
    matches = list(_INLINE_SUP_RE.finditer(line))
    result = []
    for index, match in enumerate(matches):
        if _inside_inline_code_or_math(line, match.start()):
            continue
        key = match.group("key")
        if _is_math_or_formula_superscript(line, match.start(), match.end(), key=key):
            continue
        content_end = matches[index + 1].start() if index + 1 < len(matches) else len(line)
        content = line[match.end():content_end].strip()
        if not content:
            continue
        result.append({
            "line": line_number,
            "kind": "inline",
            "column": match.start("key") + 1,
            "key": key,
            "marker": match.group(0).strip(),
            "content": content,
        })
    return result


def _normalized_file(value: Any) -> str:
    return str(value or "").replace("\\", "/")


def _definition_owner_key(line: str) -> str:
    match = re.match(r"^[ \t]*\[\^(?P<key>[^]]+)\]:", line)
    if match:
        return match.group("key")
    match = NUMBERED_PUNCT_DEF_RE.match(line)
    if match:
        return match.group("key")
    match = re.match(r"^[ \t]*<sup\b[^>]*>[ \t]*(?P<key>\d+)[ \t]*</sup>", line, re.I)
    return match.group("key") if match else ""


def is_ambiguous_definition_line(
    line: str,
    previous_line: Optional[str],
    line_number: int,
) -> bool:
    """Return True when a numbered punctuation line has an unclear boundary."""
    if NUMBERED_PUNCT_DEF_RE.match(line) is None:
        return False
    if line_number <= 1 or previous_line is None:
        return False
    previous = previous_line.strip()
    if not previous:
        return False
    if _MARKDOWN_HEADING_RE.match(previous):
        return False
    return previous[-1] not in _TERMINAL_PUNCTUATION


def _read_json(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def _notes_files(output_dir: Path) -> list[str]:
    progress = _read_json(output_dir / "ocr_markdown" / "tree_progress.json")
    files: list[str] = []
    for unit in progress.get("units", []) or []:
        if not isinstance(unit, Mapping):
            continue
        if str(unit.get("type") or "").strip().lower() != "notes":
            continue
        raw_names = unit.get("part_files") or [unit.get("file")]
        for raw_name in raw_names:
            name = _normalized_file(raw_name)
            if name and name not in files:
                files.append(name)
    return files


def _source_texts(
    output_dir: Path,
    names: list[str],
    override: Optional[Mapping[str, str]],
) -> dict[str, str]:
    texts: dict[str, str] = {}
    if override:
        for name, text in override.items():
            texts[_normalized_file(name)] = str(text)
    source_dir = output_dir / "ocr_markdown"
    for name in names:
        if name not in texts:
            try:
                texts[name] = (source_dir / name).read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                continue
    return texts


def _scope_directory(
    output_dir: Path,
    source_texts: Mapping[str, str],
) -> Optional[dict[str, Any]]:
    source_dir = output_dir / "ocr_markdown"
    texts = dict(source_texts)
    try:
        for path in source_dir.glob("*.md"):
            if path.name not in texts and path.is_file():
                texts[path.name] = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        pass
    toc_tree = _read_json(output_dir / "toc_tree.json")
    progress = _read_json(source_dir / "tree_progress.json")
    if not toc_tree or not progress:
        return None
    try:
        return build_endnote_scope_directory(toc_tree, progress, texts)
    except (EndnoteScopeError, OSError, UnicodeError, TypeError, ValueError):
        return None


def _source_hashes(
    output_dir: Path,
    names: list[str],
    source_texts: Mapping[str, str],
) -> dict[str, str]:
    hashes: dict[str, str] = {}
    source_dir = output_dir / "ocr_markdown"
    for name in names:
        path = source_dir / name
        if path.is_file():
            hashes[name] = sha256_file(path)
        elif name in source_texts:
            hashes[name] = hashlib.sha256(
                source_texts[name].encode("utf-8")
            ).hexdigest()
    return hashes


def collect_endnote_definition_ambiguities(
    output_dir: Path,
    source_texts: Optional[Mapping[str, str]] = None,
) -> dict[str, Any]:
    """Collect numbered definitions whose preceding boundary is ambiguous."""
    output_dir = Path(output_dir)
    names = _notes_files(output_dir)
    texts = _source_texts(output_dir, names, source_texts)
    scope_directory = _scope_directory(output_dir, texts)

    items: list[dict[str, Any]] = []
    for name in names:
        text = texts.get(name)
        if text is None:
            continue
        lines = text.splitlines()
        fenced_lines = markdown_fence_mask(lines)
        for line_number, line in enumerate(lines, 1):
            if fenced_lines[line_number - 1]:
                continue
            # Only inspect lines already recognized as a centralized note
            # definition. The two-space separator is intentional evidence.
            if re.match(r"^[ \t]{0,3}(?:\[\^[^]]+\]:|(?:[-*+][ \t]+)?\d+[ \t]*[.)。：:][ \t]+|<sup\b[^>]*>\s*\d+\s*</sup>)", line, re.I):
                for inline in inline_endnote_candidates(line, line_number):
                    items.append({
                        **inline, "file": name, "line_text": line,
                        "owner_key": _definition_owner_key(line),
                        "previous_line": lines[line_number - 2] if line_number > 1 else "",
                        "next_line": lines[line_number] if line_number < len(lines) else "",
                    })
            previous_line = lines[line_number - 2] if line_number > 1 else None
            if not is_ambiguous_definition_line(line, previous_line, line_number):
                continue
            match = NUMBERED_PUNCT_DEF_RE.match(line)
            if match is None:
                continue
            item = {
                "file": name,
                "line": line_number,
                "kind": "line",
                "column": 1,
                "key": match.group("key"),
                "previous_line": previous_line or "",
                "line_text": line,
                "next_line": (
                    lines[line_number] if line_number < len(lines) else ""
                ),
            }
            if scope_directory is not None:
                scope_key = scope_for_line(scope_directory, name, line_number)
                if scope_key:
                    item["scope_key"] = scope_key
            items.append(item)

    return {
        "schema_version": ENDNOTE_REVIEW_SCHEMA_VERSION,
        "items": items,
        "source_sha256": _source_hashes(output_dir, names, texts),
    }


def write_endnote_definition_ambiguities(
    output_dir: Path,
    report: Mapping[str, Any],
) -> Path:
    path = Path(output_dir) / AMBIGUITIES_FILENAME
    atomic_write_json(path, dict(report))
    return path


def load_endnote_definition_ambiguities(output_dir: Path) -> Optional[dict[str, Any]]:
    path = Path(output_dir) / AMBIGUITIES_FILENAME
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid endnote ambiguity manifest: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("endnote ambiguity manifest must be an object")
    return value


def load_endnote_definition_decisions(output_dir: Path) -> Optional[dict[str, Any]]:
    path = Path(output_dir) / DECISIONS_FILENAME
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"invalid endnote definition decisions: {exc}") from exc
    if not isinstance(value, dict):
        raise ValueError("endnote definition decisions must be an object")
    return value


def _manifest_items(manifest: Mapping[str, Any]) -> list[Mapping[str, Any]]:
    items = manifest.get("items", []) if isinstance(manifest, Mapping) else []
    return [item for item in items if isinstance(item, Mapping)] if isinstance(items, list) else []


def validate_endnote_definition_decisions(
    output_dir: Path,
    manifest: Mapping[str, Any],
) -> dict[str, Any]:
    """Validate one decision for every collected ambiguity."""
    output_dir = Path(output_dir)
    errors: list[str] = []
    items = _manifest_items(manifest)
    source_hashes = manifest.get("source_sha256", {}) if isinstance(manifest, Mapping) else {}
    if not isinstance(source_hashes, Mapping):
        errors.append("ambiguity manifest source_sha256 must be an object")
        source_hashes = {}
    item_files = {_normalized_file(item.get("file")) for item in items if item.get("file")}
    if item_files - {_normalized_file(name) for name in source_hashes}:
        errors.append("ambiguity manifest source_sha256 is missing one or more reviewed files")
    for raw_name, expected_hash in source_hashes.items():
        name = _normalized_file(raw_name)
        path_parts = PurePosixPath(name)
        if not name or path_parts.is_absolute() or ".." in path_parts.parts or ":" in name:
            errors.append(f"ambiguity manifest contains an unsafe source filename: {name!r}")
            continue
        if not re.fullmatch(r"[0-9a-f]{64}", str(expected_hash)):
            errors.append(f"ambiguity manifest has an invalid source hash for {name}")
            continue
        source_path = output_dir / "ocr_markdown" / name
        if not source_path.is_file():
            errors.append(f"endnote ambiguity source is missing; rerun footnote-prepare: {name}")
        elif sha256_file(source_path) != str(expected_hash):
            errors.append(f"endnote source changed after footnote-prepare at {name}; rerun footnote-prepare")
    manifest_path = output_dir / AMBIGUITIES_FILENAME
    decisions_path = output_dir / DECISIONS_FILENAME
    manifest_digest = sha256_file(manifest_path) if manifest_path.is_file() else None
    decisions_digest = sha256_file(decisions_path) if decisions_path.is_file() else None
    expected: dict[tuple[str, int, str, int], Mapping[str, Any]] = {}
    for item in items:
        try:
            line = item["line"]
            column = item.get("column", 1)
            if isinstance(line, bool) or not isinstance(line, int) or isinstance(column, bool) or not isinstance(column, int):
                raise ValueError
            identity = (_normalized_file(item["file"]), line, str(item.get("kind") or "line"), column)
        except (KeyError, TypeError, ValueError):
            errors.append("ambiguity manifest item is missing a file/line identity")
            continue
        if identity in expected:
            errors.append(f"duplicate ambiguity manifest item: {identity[0]}:{identity[1]}")
            continue
        expected[identity] = item

    try:
        value = load_endnote_definition_decisions(output_dir)
    except ValueError as exc:
        value = None
        errors.append(str(exc))

    if not expected:
        result = {
            "schema_version": ENDNOTE_REVIEW_SCHEMA_VERSION,
            "valid": not errors,
            "status": "retry_required" if errors else "no_subagent_review_required",
            "errors": errors,
            "decisions": [],
            "manifest_sha256": manifest_digest,
            "decisions_sha256": decisions_digest,
            "source_sha256": dict(source_hashes),
        }
        atomic_write_json(output_dir / VALIDATION_FILENAME, result)
        return result

    if value is None:
        errors.append(
            f"{DECISIONS_FILENAME} is missing; review {AMBIGUITIES_FILENAME} "
            "and write one decision per item"
        )
        decisions: list[Any] = []
    else:
        legacy_line_only = value.get("schema_version") == 1 and all(key[2] == "line" for key in expected)
        if value.get("schema_version") != ENDNOTE_REVIEW_SCHEMA_VERSION and not legacy_line_only:
            errors.append("endnote definition decisions have an unsupported schema_version")
        decisions = value.get("decisions")
        if not isinstance(decisions, list):
            errors.append("endnote definition decisions must contain a decisions array")
            decisions = []

    seen: set[tuple[str, int, str, int]] = set()
    normalized: list[dict[str, Any]] = []
    for raw in decisions:
        if not isinstance(raw, Mapping):
            errors.append("each endnote definition decision must be an object")
            continue
        try:
            line = raw["line"]
            column = raw.get("column", 1)
            if isinstance(line, bool) or not isinstance(line, int) or isinstance(column, bool) or not isinstance(column, int):
                raise ValueError
            identity = (_normalized_file(raw["file"]), line, str(raw.get("kind") or "line"), column)
        except (KeyError, TypeError, ValueError):
            errors.append("endnote definition decision is missing a file/line identity")
            continue
        if identity in seen:
            errors.append(
                f"duplicate endnote definition decision for {identity[0]}:{identity[1]}"
            )
            continue
        seen.add(identity)
        if identity not in expected:
            errors.append(
                "endnote definition decision does not match an ambiguity: "
                f"{identity[0]}:{identity[1]}"
            )
            continue
        role = str(raw.get("role") or "").strip().lower()
        if identity[2] == "inline" and (raw.get("kind") != "inline" or raw.get("column") is None):
            errors.append(f"inline endnote decision must include kind and column at {identity[0]}:{identity[1]}")
            continue
        if role in _UNRESOLVED_ROLES:
            errors.append(
                f"endnote definition decision is unresolved at "
                f"{identity[0]}:{identity[1]}; choose definition or continuation"
            )
            continue
        if role not in DECISION_ROLES:
            errors.append(
                f"unsupported endnote definition role at "
                f"{identity[0]}:{identity[1]}: {role}"
            )
            continue
        expected_key = str(expected[identity].get("key") or "")
        recorded_key = raw.get("key")
        if recorded_key is not None and str(recorded_key) != expected_key:
            errors.append(
                f"endnote definition key does not match at "
                f"{identity[0]}:{identity[1]}"
            )
            continue
        decision = dict(raw)
        decision["file"] = identity[0]
        decision["line"] = identity[1]
        decision["kind"] = identity[2]
        decision["column"] = identity[3]
        decision["role"] = role
        if expected_key:
            decision["key"] = expected_key
        if identity[2] == "inline":
            item = expected[identity]
            decision["inline_text"] = str(item.get("marker") or "") + " " + str(item.get("content") or "")
            decision["owner_key"] = str(item.get("owner_key") or "")
        normalized.append(decision)

    for identity in sorted(set(expected) - seen):
        errors.append(
            f"missing endnote definition decision for {identity[0]}:{identity[1]}:{identity[2]}:{identity[3]}"
        )

    result = {
        "schema_version": ENDNOTE_REVIEW_SCHEMA_VERSION,
        "valid": not errors,
        "status": "validated" if not errors else "retry_required",
        "errors": errors,
        "decisions": normalized,
        "manifest_sha256": manifest_digest,
        "decisions_sha256": decisions_digest,
        "source_sha256": dict(source_hashes),
    }
    atomic_write_json(output_dir / VALIDATION_FILENAME, result)
    return result


__all__ = [
    "AMBIGUITIES_FILENAME",
    "DECISION_ROLES",
    "DECISIONS_FILENAME",
    "ENDNOTE_REVIEW_SCHEMA_VERSION",
    "NUMBERED_PUNCT_DEF_RE",
    "VALIDATION_FILENAME",
    "collect_endnote_definition_ambiguities",
    "is_ambiguous_definition_line",
    "inline_endnote_candidates",
    "load_endnote_definition_ambiguities",
    "load_endnote_definition_decisions",
    "validate_endnote_definition_decisions",
    "write_endnote_definition_ambiguities",
]

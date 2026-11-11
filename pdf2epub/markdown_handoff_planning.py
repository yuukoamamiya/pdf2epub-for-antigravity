"""Inventory and batch planning for Markdown Subagent hand-offs."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .batching_service import (
    POLISH_ISOLATED_ROLES,
    batching_config,
    estimate_tokens,
    markdown_files,
    recommended_batches,
)
from .document_roles import TRANSLATION_SPECIAL_ROLES, resolve_translation_file_role


@dataclass(frozen=True)
class MarkdownHandoffPlan:
    sources: list[Path]
    batching: Dict[str, Any]
    file_stats: Dict[str, Dict[str, int]]
    source_sha256: Dict[str, str]
    normalized_roles: Dict[str, str]
    controlled_files: Dict[str, str]
    continuation_files: Dict[str, Dict[str, Any]]
    isolated_files: Optional[set[str]]
    task_max_files: int
    recommended_batches: list[list[str]]
    oversized_files: list[str]
    preceding_contexts: Dict[str, Dict[str, str]] = field(default_factory=dict)
    role_sources: Dict[str, Dict[str, Any]] = field(default_factory=dict)


def _preceding_context_tail(text: str, limit: int = 600) -> str:
    """Return the last readable paragraphs of a previous source part."""
    from .footnote_markdown import collect_footnote_definition_blocks

    lines = text.splitlines()
    definitions, consumed = collect_footnote_definition_blocks(lines)
    body_lines = [
        line
        for index, line in enumerate(lines)
        if index not in consumed and index not in definitions
    ]
    body = "\n".join(body_lines).strip()
    if not body:
        return ""
    paragraphs = [part.strip() for part in re.split(r"\n\s*\n", body) if part.strip()]
    tail = ""
    for paragraph in reversed(paragraphs):
        candidate = paragraph if not tail else f"{paragraph}\n\n{tail}"
        if len(candidate) > limit:
            break
        tail = candidate
    if not tail:
        tail = body[-limit:]
    return tail.strip()


def plan_markdown_handoff(
    source_dir: Path,
    task: str,
    config: Optional[Mapping[str, Any]] = None,
    *,
    declared_files: Optional[Iterable[str]] = None,
    file_roles: Optional[Mapping[str, str]] = None,
    file_role_sources: Optional[Mapping[str, Mapping[str, Any]]] = None,
    controlled_files: Optional[Mapping[str, str]] = None,
    continuation_files: Optional[Mapping[str, Mapping[str, Any]]] = None,
) -> MarkdownHandoffPlan:
    """Validate source inventory and produce deterministic batching inputs."""
    source_dir = Path(source_dir)
    if declared_files is None:
        sources = markdown_files(source_dir)
    else:
        names = list(dict.fromkeys(str(name) for name in declared_files))
        sources = []
        for name in names:
            candidate = source_dir / name
            if Path(name).name != name or candidate.suffix.lower() != ".md":
                raise ValueError(f"Invalid declared Markdown filename: {name}")
            if not candidate.is_file():
                raise ValueError(f"Declared Markdown source is missing: {name}")
            sources.append(candidate)
    if not sources:
        raise ValueError(f"No Markdown source units found in {source_dir}")

    batching = batching_config(config)
    file_stats: Dict[str, Dict[str, int]] = {}
    source_sha256: Dict[str, str] = {}
    source_texts: dict[str, str] = {}
    for source in sources:
        raw = source.read_bytes()
        text = raw.decode("utf-8")
        source_texts[source.name] = text
        source_sha256[source.name] = hashlib.sha256(raw).hexdigest()
        lines = text.splitlines()
        file_stats[source.name] = {
            "size_bytes": len(raw),
            "line_count": len(lines),
            "nonempty_line_count": sum(bool(line.strip()) for line in lines),
            "estimated_tokens": estimate_tokens(text),
        }
    explicit_roles = {
        str(name): str(role).strip().lower()
        for name, role in (file_roles or {}).items()
        if str(role).strip().lower() in TRANSLATION_SPECIAL_ROLES
    }
    normalized_roles: dict[str, str] = {}
    role_sources: dict[str, dict[str, Any]] = {}
    for source in sources:
        resolved_role, role_source = resolve_translation_file_role(
            source.name, source_texts[source.name],
            declared_role=explicit_roles.get(source.name),
            declared_source=dict(file_role_sources[source.name])
            if file_role_sources and source.name in file_role_sources else None,
        )
        if resolved_role in TRANSLATION_SPECIAL_ROLES:
            normalized_roles[source.name] = resolved_role
            if role_source:
                role_sources[source.name] = role_source
    controlled = {str(name): str(kind) for name, kind in (controlled_files or {}).items()}
    if set(controlled) - {source.name for source in sources}:
        raise ValueError("controlled special-unit files must be declared Markdown sources")
    if any(kind not in {"bibliography", "index"} for kind in controlled.values()):
        raise ValueError("unsupported controlled special-unit kind")
    isolated_files = (
        {name for name, role in normalized_roles.items() if role in POLISH_ISOLATED_ROLES}
        if task == "polish"
        else None
    )
    task_max_files = batching["polish_max_files"] if task == "polish" else batching["max_files"]
    continuation_metadata = {
        str(name): dict(value)
        for name, value in (continuation_files or {}).items()
        if str(name).strip() and isinstance(value, Mapping)
    }
    for source in sources:
        if source.name in continuation_metadata:
            continue
        match = re.search(r"\.part(?P<number>[2-9][0-9]*)\.md$", source.name, re.IGNORECASE)
        if match:
            continuation_metadata[source.name] = {
                "is_continuation": True,
                "part_number": int(match.group("number")),
            }
    for name, metadata in continuation_metadata.items():
        previous = str(metadata.get("previous_file") or "").strip()
        if previous and previous in source_texts:
            continue
        match = re.match(r"^(?P<base>.+)\.part(?P<number>[2-9][0-9]*)\.md$", name, re.IGNORECASE)
        if not match:
            continue
        inferred = f"{match.group('base')}.part{int(match.group('number')) - 1}.md"
        if inferred in source_texts:
            metadata["previous_file"] = inferred
    preceding_contexts: dict[str, dict[str, str]] = {}
    for name, metadata in continuation_metadata.items():
        previous = str(metadata.get("previous_file") or "").strip()
        if not previous or previous not in source_texts:
            continue
        tail = _preceding_context_tail(source_texts[previous])
        if tail:
            preceding_contexts[name] = {
                "source_file": previous,
                "text": tail,
            }
    batches = recommended_batches(
        file_stats,
        task_max_files,
        batching["max_source_tokens"],
        batching["single_file_max_bytes"],
        batching["max_nonempty_lines"],
        isolated_files=isolated_files,
    )
    oversized = [
        name for name, stats in file_stats.items()
        if stats["estimated_tokens"] > batching["max_source_tokens"]
        or stats["size_bytes"] > batching["single_file_max_bytes"]
        or stats["nonempty_line_count"] > batching["max_nonempty_lines"]
    ]
    return MarkdownHandoffPlan(
        sources=sources, batching=batching, file_stats=file_stats,
        source_sha256=source_sha256, normalized_roles=normalized_roles,
        controlled_files=controlled, continuation_files=continuation_metadata,
        isolated_files=isolated_files, task_max_files=task_max_files,
        recommended_batches=batches, oversized_files=oversized,
        preceding_contexts=preceding_contexts,
        role_sources=role_sources,
    )

"""Inventory and batch planning for Markdown Subagent hand-offs."""
from __future__ import annotations

import hashlib
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .batching_service import (
    POLISH_ISOLATED_ROLES,
    batching_config,
    estimate_tokens,
    markdown_files,
    recommended_batches,
)
from .document_roles import TRANSLATION_SPECIAL_ROLES


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


def plan_markdown_handoff(
    source_dir: Path,
    task: str,
    config: Optional[Mapping[str, Any]] = None,
    *,
    declared_files: Optional[Iterable[str]] = None,
    file_roles: Optional[Mapping[str, str]] = None,
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
    for source in sources:
        raw = source.read_bytes()
        text = raw.decode("utf-8")
        source_sha256[source.name] = hashlib.sha256(raw).hexdigest()
        lines = text.splitlines()
        file_stats[source.name] = {
            "size_bytes": len(raw),
            "line_count": len(lines),
            "nonempty_line_count": sum(bool(line.strip()) for line in lines),
            "estimated_tokens": estimate_tokens(text),
        }
    normalized_roles = {
        str(name): str(role).strip().lower()
        for name, role in (file_roles or {}).items()
        if str(role).strip().lower() in TRANSLATION_SPECIAL_ROLES
    }
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
        sources, batching, file_stats, source_sha256, normalized_roles, controlled,
        continuation_metadata, isolated_files, task_max_files, batches, oversized,
    )

"""Typed request and context projections for Markdown hand-off preparation."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Mapping, Optional


@dataclass(frozen=True)
class MarkdownHandoffRequest:
    output_dir: Path
    task: str
    source_dir: Path
    target_dir: Path
    source_language: str
    target_language: str
    extra_rules: tuple[str, ...] = ()
    config: Optional[Mapping[str, Any]] = None
    resume: bool = False
    file_roles: Optional[Mapping[str, str]] = None
    context_files: Optional[Mapping[str, Path]] = None
    skipped_context_files: tuple[str, ...] = ()
    file_contexts: Optional[Mapping[str, str]] = None
    heading_contexts: Optional[Mapping[str, Mapping[str, Any]]] = None
    global_toc_outline: Optional[str] = None
    unit_context_files: Optional[Mapping[str, Path]] = None
    chapter_groups: Optional[Mapping[str, Iterable[str]]] = None
    prompt_context_files: Optional[Mapping[str, Path]] = None
    declared_files: Optional[Iterable[str]] = None
    allow_human_review_retry: bool = False
    secondary_source_dir: Optional[Path] = None
    visual_review_dir: Optional[Path] = None
    review_output_dir: Optional[Path] = None
    continuation_files: Optional[Mapping[str, Mapping[str, Any]]] = None
    protected_toc_titles: Optional[Iterable[str]] = None
    config_path: Optional[str | Path] = None
    controlled_files: Optional[Mapping[str, str]] = None

    def __post_init__(self) -> None:
        object.__setattr__(self, "output_dir", Path(self.output_dir))
        object.__setattr__(self, "source_dir", Path(self.source_dir))
        object.__setattr__(self, "target_dir", Path(self.target_dir))
        object.__setattr__(self, "extra_rules", tuple(self.extra_rules or ()))
        object.__setattr__(self, "skipped_context_files", tuple(self.skipped_context_files or ()))


@dataclass(frozen=True)
class ContextProjection:
    context_files: Mapping[str, str] = field(default_factory=dict)
    context_sha256: Mapping[str, str] = field(default_factory=dict)
    unit_context_files: Mapping[str, str] = field(default_factory=dict)
    unit_context_sha256: Mapping[str, str] = field(default_factory=dict)
    heading_contexts: Mapping[str, Mapping[str, Any]] = field(default_factory=dict)
    file_contexts: Mapping[str, str] = field(default_factory=dict)
    skipped_context_files: tuple[str, ...] = ()
    prompt_context_files: Optional[Mapping[str, str]] = None
    audit_only_context_files: Mapping[str, str] = field(default_factory=dict)
    protected_toc_titles: tuple[str, ...] = ()
    global_toc_outline: str = ""
    global_toc_outline_sha256: str = ""
    chapter_groups: Mapping[str, tuple[str, ...]] = field(default_factory=dict)


def _project_files(output_dir: Path, files: Optional[Mapping[str, Path]], label: str) -> tuple[dict[str, str], dict[str, str]]:
    paths: dict[str, str] = {}
    hashes: dict[str, str] = {}
    root = output_dir.resolve()
    for name, value in (files or {}).items():
        path = Path(value).resolve()
        try:
            relative = path.relative_to(root)
        except ValueError as exc:
            raise ValueError(f"{label} file must be inside output directory: {value}") from exc
        if not path.is_file():
            raise ValueError(f"{label} file not found: {path}")
        key = str(name)
        paths[key] = relative.as_posix()
        hashes[key] = hashlib.sha256(path.read_bytes()).hexdigest()
    return paths, hashes


def normalize_handoff_context(request: MarkdownHandoffRequest, declared_names: Iterable[str]) -> ContextProjection:
    """Normalize sparse contexts, hashes, and chapter projections for this hand-off."""
    output_dir = request.output_dir
    declared_names = tuple(declared_names)
    context_files, context_sha256 = _project_files(output_dir, request.context_files, "Context")
    unit_files, unit_hashes = _project_files(output_dir, request.unit_context_files, "Unit context")
    prompt_files = None
    audit_files: dict[str, str] = {}
    if request.prompt_context_files is not None:
        prompt_files, _ = _project_files(output_dir, request.prompt_context_files, "Prompt context")
        audit_files = {name: path for name, path in context_files.items() if name not in prompt_files}
    heading_contexts = {
        str(name): dict(context)
        for name, context in (request.heading_contexts or {}).items()
        if str(name).strip() and isinstance(context, Mapping)
    }
    file_contexts = {
        str(name): str(context).strip()
        for name, context in (request.file_contexts or {}).items()
        if str(name).strip() and str(context).strip()
    }
    protected_titles = tuple(dict.fromkeys(
        str(title).strip() for title in (request.protected_toc_titles or ()) if str(title).strip()
    ))
    outline = str(request.global_toc_outline or "").strip()
    chapter_groups: dict[str, tuple[str, ...]] = {}
    declared = set(declared_names)
    for chapter_id, names in (request.chapter_groups or {}).items():
        normalized = tuple(dict.fromkeys(str(name) for name in (names or ()) if str(name) in declared))
        if normalized:
            chapter_groups[str(chapter_id)] = normalized
    if chapter_groups:
        assigned = {name for names in chapter_groups.values() for name in names}
        for name in declared_names:
            if name not in assigned:
                chapter_groups[f"unit:{name}"] = (name,)
    return ContextProjection(
        context_files=context_files,
        context_sha256=context_sha256,
        unit_context_files=unit_files,
        unit_context_sha256=unit_hashes,
        heading_contexts=heading_contexts,
        file_contexts=file_contexts,
        skipped_context_files=tuple(sorted({str(name) for name in request.skipped_context_files if str(name).strip()})),
        prompt_context_files=prompt_files,
        audit_only_context_files=audit_files,
        protected_toc_titles=protected_titles,
        global_toc_outline=outline,
        global_toc_outline_sha256=hashlib.sha256(outline.encode("utf-8")).hexdigest() if outline else "",
        chapter_groups=chapter_groups,
    )

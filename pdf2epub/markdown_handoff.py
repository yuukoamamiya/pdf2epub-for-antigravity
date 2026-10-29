"""Markdown Subagent hand-off preparation and public compatibility entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, Mapping, Optional

from .markdown_handoff_context import MarkdownHandoffRequest, normalize_handoff_context
from .markdown_handoff_manifest import (
    ManifestBuild,
    build_markdown_manifest,
    validate_manifest_directories,
)
from .markdown_handoff_planning import plan_markdown_handoff
from .markdown_handoff_prompt import (
    PromptBatching,
    PromptIdentity,
    PromptOperationalPaths,
    PromptRenderData,
    render_markdown_prompt,
)
from .markdown_handoff_resume import decide_resume
from .ocr_source import is_legacy_native_source
from .subagent_runtime import resolve_subagent_model
from .subagent_safety import protected_workspace_snapshot
from .workflow_contracts import atomic_write_json, atomic_write_text, relative_posix_path


def prepare_markdown_subagent(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    source_language: str,
    target_language: str,
    extra_rules: Iterable[str] = (),
    config: Optional[Mapping[str, Any]] = None,
    resume: bool = False,
    file_roles: Optional[Mapping[str, str]] = None,
    context_files: Optional[Mapping[str, Path]] = None,
    skipped_context_files: Iterable[str] = (),
    file_contexts: Optional[Mapping[str, str]] = None,
    heading_contexts: Optional[Mapping[str, Mapping[str, Any]]] = None,
    global_toc_outline: Optional[str] = None,
    unit_context_files: Optional[Mapping[str, Path]] = None,
    chapter_groups: Optional[Mapping[str, Iterable[str]]] = None,
    prompt_context_files: Optional[Mapping[str, Path]] = None,
    declared_files: Optional[Iterable[str]] = None,
    allow_human_review_retry: bool = False,
    secondary_source_dir: Optional[Path] = None,
    visual_review_dir: Optional[Path] = None,
    review_output_dir: Optional[Path] = None,
    continuation_files: Optional[Mapping[str, Mapping[str, Any]]] = None,
    protected_toc_titles: Optional[Iterable[str]] = None,
    config_path: Optional[str | Path] = None,
    controlled_files: Optional[Mapping[str, str]] = None,
) -> Dict[str, Path]:
    """Adapt the stable public signature to the focused hand-off services."""
    request = MarkdownHandoffRequest(
        output_dir=Path(output_dir),
        task=task,
        source_dir=Path(source_dir),
        target_dir=Path(target_dir),
        source_language=source_language,
        target_language=target_language,
        extra_rules=tuple(extra_rules or ()),
        config=config,
        resume=resume,
        file_roles=file_roles,
        context_files=context_files,
        skipped_context_files=tuple(skipped_context_files or ()),
        file_contexts=file_contexts,
        heading_contexts=heading_contexts,
        global_toc_outline=global_toc_outline,
        unit_context_files=unit_context_files,
        chapter_groups=chapter_groups,
        prompt_context_files=prompt_context_files,
        declared_files=declared_files,
        allow_human_review_retry=allow_human_review_retry,
        secondary_source_dir=secondary_source_dir,
        visual_review_dir=visual_review_dir,
        review_output_dir=review_output_dir,
        continuation_files=continuation_files,
        protected_toc_titles=protected_toc_titles,
        config_path=config_path,
        controlled_files=controlled_files,
    )
    plan = plan_markdown_handoff(
        request.source_dir,
        request.task,
        request.config,
        declared_files=request.declared_files,
        file_roles=request.file_roles,
        controlled_files=request.controlled_files,
        continuation_files=request.continuation_files,
    )
    context = normalize_handoff_context(request, (source.name for source in plan.sources))
    resume_decision = decide_resume(request, plan, context)
    model = resolve_subagent_model(request.config, request.task)
    visual_dir, secondary_dir, review_dir = validate_manifest_directories(request)
    manifest = build_markdown_manifest(
        ManifestBuild(
            request=request,
            plan=plan,
            context=context,
            resume=resume_decision,
            model=model,
            visual_review_dir=visual_dir,
            secondary_source_dir=secondary_dir,
            review_output_dir=review_dir,
            protected_workspace=protected_workspace_snapshot(request.output_dir),
            source_dir=relative_posix_path(request.source_dir, request.output_dir),
            target_dir=relative_posix_path(request.target_dir, request.output_dir),
            scratch_dir=relative_posix_path(
                request.output_dir / "scratch" / request.task, request.output_dir
            ),
        )
    )
    manifest_path = request.output_dir / f"{request.task}_subagent_manifest.json"
    atomic_write_json(manifest_path, manifest)
    prompt_path = request.output_dir / f"{request.task}_subagent_prompt.md"
    prompt = render_markdown_prompt(
        PromptRenderData(
            identity=PromptIdentity(
                task=request.task,
                model=model,
                source_language=request.source_language,
                target_language=request.target_language,
                source_dir=str(manifest["source_dir"]),
                target_dir=str(manifest["target_dir"]),
                manifest_name=manifest_path.name,
                scratch_dir=str(manifest["scratch_dir"]),
                effective_max_concurrency=int(manifest["effective_max_concurrency"]),
                concurrency_reason=str(manifest["concurrency_reason"]),
            ),
            batching=PromptBatching(plan.batching, plan.task_max_files),
            context=context,
            operational_paths=PromptOperationalPaths(
                visual_review_dir=manifest.get("visual_review_dir"),
                secondary_source_dir=manifest.get("secondary_source_dir"),
                review_output_dir=manifest.get("review_output_dir"),
            ),
            normalized_roles=plan.normalized_roles,
            extra_rules=request.extra_rules,
            config=request.config,
            previous_review_required=resume_decision.previous_review_required,
            legacy_native_source=(
                request.task == "polish" and is_legacy_native_source(request.output_dir)
            ),
        )
    )
    atomic_write_text(prompt_path, prompt)
    return {"manifest": manifest_path, "prompt": prompt_path}


__all__ = ["prepare_markdown_subagent"]

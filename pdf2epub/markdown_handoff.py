"""Markdown Subagent hand-off preparation entry point."""
from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Mapping

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


def preview_markdown_handoff(request: MarkdownHandoffRequest) -> Dict[str, Any]:
    """Describe what a prepare run would do, without writing any file.

    The first ``--resume`` after a projection or schema upgrade lists every
    unit as pending, and operators are told not to dispatch from it.  A
    read-only preview makes the pending reasons and worker batches visible
    before any manifest, prompt or hand-off is replaced.
    """
    plan = plan_markdown_handoff(
        request.source_dir,
        request.task,
        request.config,
        declared_files=request.declared_files,
        file_roles=request.file_roles,
        file_role_sources=request.file_role_sources,
        controlled_files=request.controlled_files,
        continuation_files=request.continuation_files,
    )
    context = normalize_handoff_context(request, (source.name for source in plan.sources))
    decision = decide_resume(request, plan, context)
    reason_counts: Dict[str, int] = {}
    for reason in decision.pending_reasons.values():
        reason_counts[reason] = reason_counts.get(reason, 0) + 1
    return {
        "task": request.task,
        "completed_files": list(decision.completed_files),
        "pending_files": list(decision.pending_files),
        "special_pending_files": list(decision.special_pending_files),
        "pending_reasons": dict(decision.pending_reasons),
        "pending_reason_counts": dict(sorted(reason_counts.items())),
        "worker_batches": [list(batch) for batch in decision.pending_batches],
        "effective_concurrency": decision.effective_concurrency,
        "concurrency_reason": decision.concurrency_reason,
    }


def prepare_markdown_subagent(request: MarkdownHandoffRequest) -> Dict[str, Path]:
    """Write the manifest and prompt for one Markdown hand-off request."""
    plan = plan_markdown_handoff(
        request.source_dir,
        request.task,
        request.config,
        declared_files=request.declared_files,
        file_roles=request.file_roles,
        file_role_sources=request.file_role_sources,
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
    if request.task == "polish" and request.footnote_semantics_enabled:
        from .polish_footnote_semantics import build_semantic_review_contract

        boundary_hashes = manifest.get("boundary_context_sha256", {})
        boundary_sources = manifest.get("boundary_context_sources", {})
        if not isinstance(boundary_hashes, Mapping):
            boundary_hashes = {}
        if not isinstance(boundary_sources, Mapping):
            boundary_sources = {}
        semantic_contracts = {}
        for source in plan.sources:
            source_text = source.read_bytes().decode("utf-8")
            semantic_contracts[source.name] = build_semantic_review_contract(
                source.name,
                source_text,
                unit_context_sha256=context.unit_context_sha256.get(source.name),
                boundary_context_sha256=boundary_hashes.get(source.name),
                has_boundary_context=bool(boundary_sources.get(source.name, [])),
            )
        manifest["footnote_semantics"] = {
            "schema_version": 1,
            "enabled": True,
            "sidecar_directory": "polish_footnote_reviews",
            "files": semantic_contracts,
            "recovery": "footnote-prepare, footnote-validate, footnote-apply",
        }
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

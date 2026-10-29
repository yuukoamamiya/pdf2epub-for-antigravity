"""Manifest construction for Markdown Subagent hand-offs."""
from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping

from .batching_service import batch_queue
from .markdown_handoff_context import ContextProjection, MarkdownHandoffRequest
from .markdown_handoff_planning import MarkdownHandoffPlan
from .markdown_handoff_resume import ResumeDecision
@dataclass(frozen=True)
class ManifestBuild:
    request: MarkdownHandoffRequest
    plan: MarkdownHandoffPlan
    context: ContextProjection
    resume: ResumeDecision
    model: str
    visual_review_dir: str | None
    secondary_source_dir: str | None
    review_output_dir: str | None
    protected_workspace: Mapping[str, Any]
    source_dir: str
    target_dir: str
    scratch_dir: str


def _optional_directory(output_dir: Path, value: Path | None, label: str, *, create: bool = False) -> str | None:
    if value is None:
        return None
    path = Path(value).resolve()
    try:
        relative = path.relative_to(output_dir.resolve())
    except ValueError as exc:
        raise ValueError(f"{label} must be inside output directory: {value}") from exc
    if not path.is_dir():
        if create:
            path.mkdir(parents=True, exist_ok=True)
        else:
            raise ValueError(f"{label} not found: {path}")
    return relative.as_posix()


def validate_manifest_directories(request: MarkdownHandoffRequest) -> tuple[str | None, str | None, str | None]:
    """Check optional artifact directories before constructing the manifest."""
    output_dir = request.output_dir
    visual = _optional_directory(output_dir, request.visual_review_dir, "Visual review directory")
    secondary = _optional_directory(output_dir, request.secondary_source_dir, "Secondary OCR directory")
    review = _optional_directory(output_dir, request.review_output_dir, "Review output directory", create=True)
    return visual, secondary, review


def build_markdown_manifest(data: ManifestBuild) -> dict[str, Any]:
    """Construct the manifest value without performing file I/O."""
    request, plan, context, resume = data.request, data.plan, data.context, data.resume
    manifest: dict[str, Any] = {
        "schema_version": 1,
        "workflow": "antigravity-subagent",
        "task": request.task,
        "execution_mode": "workspace_subagent_required",
        "subagent_required": True,
        "source_language": request.source_language,
        "target_language": request.target_language,
        "model": data.model,
        "resume": request.resume,
        "source_dir": data.source_dir,
        "target_dir": data.target_dir,
        "files": [path.name for path in plan.sources],
        "source_sha256": plan.source_sha256,
        "file_stats": plan.file_stats,
        "batching": plan.batching,
        "recommended_batches": plan.recommended_batches,
        "pending_batches": [list(batch) for batch in resume.pending_batches],
        "batch_queue": batch_queue(
            [list(batch) for batch in resume.pending_batches],
            resume.pending_stats,
            isolated_files=plan.isolated_files,
        ),
        "oversized_files": plan.oversized_files,
        "completed_files": list(resume.completed_files),
        "pending_files": list(resume.pending_files),
        "special_pending_files": list(resume.special_pending_files),
        "special_unit_modes": dict(plan.controlled_files),
        "atomic_output_contract": {
            "temporary_pattern": ".<assigned_filename>.tmp",
            "final_pattern": "<assigned_filename>",
            "promotion": "atomic_replace_after_complete_write",
            "validator_checkpoint": "validated/<assigned_filename>",
        },
        "scratch_dir": data.scratch_dir,
        "effective_max_concurrency": resume.effective_concurrency,
        "concurrency_reason": resume.concurrency_reason,
        "protected_workspace": dict(data.protected_workspace),
    }
    if request.config_path is not None:
        manifest["config_path"] = Path(request.config_path).expanduser().as_posix()
    old_workers = resume.previous_manifest.get("worker_handoffs", [])
    if request.resume and isinstance(old_workers, list) and old_workers:
        manifest["previous_worker_handoffs"] = [dict(item) for item in old_workers if isinstance(item, Mapping)]
    continuation_files = {name: value for name, value in plan.continuation_files.items() if name in plan.file_stats}
    if continuation_files:
        manifest["continuation_files"] = continuation_files
    if data.visual_review_dir is not None:
        manifest["visual_review_dir"] = data.visual_review_dir
    if data.secondary_source_dir is not None:
        manifest["secondary_source_dir"] = data.secondary_source_dir
    if data.review_output_dir is not None:
        manifest["review_output_dir"] = data.review_output_dir
    if plan.normalized_roles:
        manifest["file_roles"] = plan.normalized_roles
    if context.context_files:
        manifest["context_files"] = dict(context.context_files)
        manifest["context_sha256"] = dict(context.context_sha256)
    manifest["context_is_current"] = resume.previous_manifest.get("context_sha256", {}) == context.context_sha256
    if resume.previous_review_required:
        manifest["previous_review_required"] = {
            name: [dict(item) for item in items]
            for name, items in resume.previous_review_required.items()
        }
    if context.skipped_context_files:
        manifest["skipped_context_files"] = list(context.skipped_context_files)
    if context.file_contexts:
        manifest["file_contexts"] = dict(context.file_contexts)
    if context.heading_contexts:
        manifest["toc_heading_contexts"] = dict(context.heading_contexts)
    if context.protected_toc_titles:
        manifest["protected_toc_titles"] = list(context.protected_toc_titles)
    if context.global_toc_outline_sha256:
        manifest["global_toc_outline_sha256"] = context.global_toc_outline_sha256
    if context.unit_context_files:
        manifest["unit_context_files"] = dict(context.unit_context_files)
        manifest["unit_context_sha256"] = dict(context.unit_context_sha256)
    if context.chapter_groups:
        manifest["chapter_groups"] = {
            chapter_id: list(names) for chapter_id, names in context.chapter_groups.items()
        }
    if context.prompt_context_files is not None:
        prompt_context = dict(context.prompt_context_files)
        manifest["prompt_context_files"] = prompt_context
        manifest["audit_only_context_files"] = dict(context.audit_only_context_files)
    return manifest

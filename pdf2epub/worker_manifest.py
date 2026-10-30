"""Pure projection of a worker assignment into its scoped manifest."""
from __future__ import annotations

from typing import Any, Dict, List, Mapping

def _worker_manifest_projection(
    manifest: Mapping[str, Any],
    *,
    worker_id: str,
    files: List[str],
    batches: List[Mapping[str, Any]],
    chapter_mode: bool,
    group: Mapping[str, Any],
) -> Dict[str, Any]:
    """Build the small operational manifest handed to one worker.

    The parent manifest remains the complete audit/resume inventory.  Copying
    it into every worker used to repeat the full book's file stats, contexts,
    queues, and chapter map in every model input.
    """
    files = list(files)
    assigned = set(files)
    projected: Dict[str, Any] = {}
    for key in (
        "schema_version",
        "workflow",
        "task",
        "execution_mode",
        "subagent_required",
        "source_language",
        "target_language",
        "model",
        "source_dir",
        "target_dir",
        "scratch_dir",
        "atomic_output_contract",
        "batching",
        "effective_max_concurrency",
        "concurrency_reason",
        "global_toc_outline_sha256",
        "skipped_context_files",
        "visual_review_dir",
        "review_output_dir",
        "secondary_source_dir",
        "config_path",
    ):
        if key in manifest:
            projected[key] = manifest[key]

    manifest_completed = set(manifest.get("completed_files", []))
    worker_completed = [f for f in files if f in manifest_completed]
    worker_pending = [f for f in files if f not in manifest_completed]
    worker_pending_batches = [
        [f for f in batch.get("files", []) if f not in manifest_completed]
        for batch in batches
    ]
    worker_pending_batches = [b for b in worker_pending_batches if b]

    projected.update(
        {
            # Keep ``files`` for older worker tooling, but scope it exactly as
            # tightly as the explicit assignment fields.
            "files": files,
            "assigned_files": files,
            "pending_files": worker_pending,
            "completed_files": worker_completed,
            "pending_batches": worker_pending_batches,
            "batch_queue": [dict(batch, status="assigned") for batch in batches],
            "worker_id": worker_id,
            "worker_queue": [
                {
                    "worker_id": worker_id,
                    "status": "assigned",
                    "batch_ids": [batch.get("batch_id") for batch in batches],
                }
            ],
            "toc_owner": False,
        }
    )

    for key in (
        "source_sha256",
        "file_stats",
        "repair_file_stats",
        "file_roles",
        "file_contexts",
        "toc_heading_contexts",
        "repair_candidates",
    ):
        value = manifest.get(key)
        if isinstance(value, Mapping):
            subset = {name: value[name] for name in files if name in value}
            if subset:
                projected[key] = subset

    continuation = manifest.get("continuation_files")
    if isinstance(continuation, Mapping):
        subset = {name: continuation[name] for name in files if name in continuation}
        if subset:
            projected["continuation_files"] = subset
            projected["is_continuation"] = True

    for key in ("unit_context_files", "unit_context_sha256"):
        value = manifest.get(key)
        if isinstance(value, Mapping):
            subset = {name: value[name] for name in files if name in value}
            if subset:
                projected[key] = subset

    oversized = manifest.get("oversized_files")
    if isinstance(oversized, list):
        projected["oversized_files"] = [name for name in oversized if name in assigned]

    # Reference-only contexts are operationally readable; authoritative
    # snapshots/entities stay out of worker manifests and remain parent-level
    # provenance for validation.  Generic non-translation callers without an
    # audit split retain their small context map for compatibility.
    prompt_contexts = manifest.get("prompt_context_files")
    if isinstance(prompt_contexts, Mapping):
        projected["prompt_context_files"] = dict(prompt_contexts)
        context_hashes = manifest.get("context_sha256")
        if isinstance(context_hashes, Mapping):
            projected["context_sha256"] = {
                name: context_hashes[name]
                for name in prompt_contexts
                if name in context_hashes
            }
    elif "audit_only_context_files" not in manifest:
        context_files = manifest.get("context_files")
        if isinstance(context_files, Mapping):
            projected["context_files"] = dict(context_files)
            context_hashes = manifest.get("context_sha256")
            if isinstance(context_hashes, Mapping):
                projected["context_sha256"] = dict(context_hashes)

    if chapter_mode:
        chapter_ids = [
            str(value)
            for value in (group.get("chapter_ids") or [])
            if str(value).strip()
        ]
        if not chapter_ids and group.get("chapter_id"):
            chapter_ids = [str(group["chapter_id"])]
        projected.update(
            {
                "chapter_ids": chapter_ids,
                "chapter_count": int(group.get("chapter_count", len(chapter_ids) or 1)),
                "chapter_split": bool(group.get("chapter_split")),
                "chapter_part_count": int(group.get("chapter_part_count", 1)),
            }
        )
        if len(chapter_ids) == 1:
            projected.update(
                {
                    "chapter_id": chapter_ids[0],
                    "chapter_file_count": int(
                        group.get("chapter_file_count", len(files))
                    ),
                }
            )
        else:
            projected["chapter_file_counts"] = {
                str(chapter_id): int(count)
                for chapter_id, count in (group.get("chapter_file_counts") or {}).items()
            }
    return projected

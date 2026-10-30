"""Pure worker scheduling and scoped manifest projection."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping

from .batching_service import (
    DEFAULT_BATCH_MAX_FILES, DEFAULT_BATCH_MAX_SOURCE_TOKENS,
    DEFAULT_BATCH_MAX_NONEMPTY_LINES, DEFAULT_SINGLE_FILE_MAX_BYTES,
    DEFAULT_PACKED_SOURCE_TOKEN_MULTIPLIER, DEFAULT_PACK_ADJACENT_CHAPTERS,
    DEFAULT_MAX_CHAPTERS_PER_WORKER, DEFAULT_MAX_TINY_CHAPTERS_PER_WORKER,
    DEFAULT_TINY_CHAPTER_MAX_TOKENS, MAX_BATCH_FILES, MAX_ACTIVE_SUBAGENTS,
    positive_int, recommended_batches,
)
from .document_roles import infer_translation_file_role

def _balanced_worker_groups(
    queue: List[Dict[str, Any]],
    max_workers: int,
) -> List[Dict[str, Any]]:
    """Pack safe batches into weighted worker groups using LPT balancing."""
    worker_count = min(MAX_ACTIVE_SUBAGENTS, max(1, int(max_workers)), len(queue))
    groups = [
        {"worker_id": f"worker_{index:03d}", "batches": [], "estimated_tokens": 0}
        for index in range(1, worker_count + 1)
    ]
    # Largest-processing-time first keeps the worker loads close while
    # retaining each existing safety batch as an indivisible scheduling unit.
    ordered = sorted(
        queue,
        key=lambda item: int(item.get("estimated_tokens", 0)),
        reverse=True,
    )
    for batch in ordered:
        group = min(groups, key=lambda item: item["estimated_tokens"])
        group["batches"].append(batch)
        group["estimated_tokens"] += int(batch.get("estimated_tokens", 0))
    return groups


def _worker_groups(
    queue: List[Dict[str, Any]],
    max_workers: int,
) -> List[Dict[str, Any]]:
    """Pack batches into workers while forcing special units to stay alone.

    Ordinary batches remain eligible for LPT packing up to the configured
    concurrency.  A batch marked ``isolated`` gets its own worker so an index,
    bibliography, notes, or front-matter unit never shares a Subagent with
    ordinary chapters.  Worker ids remain stable ``worker_NNN`` values.
    """
    if not queue:
        return []
    isolated_batches = [
        batch for batch in queue if bool(batch.get("isolated"))
    ]
    if not isolated_batches:
        groups = _balanced_worker_groups(queue, max_workers)
    else:
        ordinary_batches = [
            batch for batch in queue if not bool(batch.get("isolated"))
        ]
        groups = [
            {
                "batches": [batch],
                "estimated_tokens": int(batch.get("estimated_tokens", 0)),
            }
            for batch in isolated_batches
        ]
        if ordinary_batches:
            groups.extend(
                _balanced_worker_groups(ordinary_batches, max_workers)
            )
    for index, group in enumerate(groups, 1):
        group["worker_id"] = f"worker_{index:03d}"
        group["batches"].sort(key=lambda item: str(item.get("batch_id", "")))
        group["files"] = [
            name
            for batch in group["batches"]
            for name in batch.get("files", [])
        ]
    return groups


def _chapter_worker_groups(manifest: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Turn ordered chapter groups into scoped worker hand-offs.

    A normal chapter remains an atomic semantic group, but adjacent groups may
    be packed into one worker while the combined source stays within the
    configured file/token budget.  A chapter that already needs multiple
    batches is always isolated and split only within itself.
    """
    chapter_groups = manifest.get("chapter_groups")
    if not isinstance(chapter_groups, Mapping):
        return []
    pending = {
        str(name) for name in (manifest.get("pending_files") or [])
    }
    file_stats = manifest.get("file_stats", {}) or {}
    batching = manifest.get("batching", {}) or {}
    max_files = min(
        MAX_BATCH_FILES,
        positive_int(batching.get("max_files"), DEFAULT_BATCH_MAX_FILES),
    )
    max_tokens = int(
        batching.get("max_source_tokens", DEFAULT_BATCH_MAX_SOURCE_TOKENS)
    )
    max_packed_tokens = max(
        max_tokens,
        positive_int(
            batching.get("max_packed_source_tokens"),
            max_tokens * DEFAULT_PACKED_SOURCE_TOKEN_MULTIPLIER,
        ),
    )
    max_bytes = int(
        batching.get("single_file_max_bytes", DEFAULT_SINGLE_FILE_MAX_BYTES)
    )
    max_nonempty_lines = positive_int(
        batching.get("max_nonempty_lines"), DEFAULT_BATCH_MAX_NONEMPTY_LINES
    )
    pack_adjacent = batching.get(
        "pack_adjacent_chapters", DEFAULT_PACK_ADJACENT_CHAPTERS
    ) is not False
    max_chapters = min(
        MAX_BATCH_FILES,
        positive_int(
            batching.get("max_chapters_per_worker"),
            DEFAULT_MAX_CHAPTERS_PER_WORKER,
        ),
    )
    max_tiny_chapters = min(
        MAX_BATCH_FILES,
        positive_int(
            batching.get("max_tiny_chapters_per_worker"),
            DEFAULT_MAX_TINY_CHAPTERS_PER_WORKER,
        ),
    )
    tiny_chapter_max_tokens = positive_int(
        batching.get("tiny_chapter_max_tokens"),
        DEFAULT_TINY_CHAPTER_MAX_TOKENS,
    )
    manifest_roles = manifest.get("file_roles", {})
    if not isinstance(manifest_roles, Mapping):
        manifest_roles = {}

    def make_group(
        chapter_records: List[Dict[str, Any]],
        batch_files: List[str],
        *,
        part_index: int = 1,
        part_count: int = 1,
        chapter_split: bool = False,
    ) -> Dict[str, Any]:
        chapter_ids = [str(record["chapter_id"]) for record in chapter_records]
        chapter_file_counts = {
            str(record["chapter_id"]): int(record["chapter_file_count"])
            for record in chapter_records
        }
        if len(chapter_ids) == 1:
            safe_id = re.sub(r"[^A-Za-z0-9_.-]+", "_", chapter_ids[0]).strip("_")
            safe_id = safe_id or "chapter"
            worker_id = f"chapter_{safe_id}_part_{part_index:03d}"
        else:
            safe_ids = [
                re.sub(r"[^A-Za-z0-9_.-]+", "_", chapter_id).strip("_") or "chapter"
                for chapter_id in chapter_ids
            ]
            worker_id = (
                f"chapters_{safe_ids[0]}_to_{safe_ids[-1]}"
                f"_part_{part_index:03d}"
            )
        batch_id = worker_id
        estimated_tokens = sum(
            int(file_stats[name].get("estimated_tokens", 0)) for name in batch_files
        )
        group: Dict[str, Any] = {
            "worker_id": worker_id,
            "chapter_ids": chapter_ids,
            "chapter_records": chapter_records,
            "chapter_file_counts": chapter_file_counts,
            "chapter_count": len(chapter_ids),
            "chapter_split": chapter_split,
            "chapter_part_count": part_count,
            "batches": [
                {
                    "batch_id": batch_id,
                    "chapter_ids": chapter_ids,
                    "files": batch_files,
                    "estimated_tokens": estimated_tokens,
                    "status": "assigned",
                }
            ],
            "files": batch_files,
            "estimated_tokens": estimated_tokens,
        }
        # Preserve the old singular fields for consumers and manifests that
        # handle a single chapter.  Packed workers use the plural fields.
        if len(chapter_ids) == 1:
            group["chapter_id"] = chapter_ids[0]
            group["chapter_file_count"] = chapter_file_counts[chapter_ids[0]]
            group["chapter_context_files"] = chapter_records[0]["files"]
        return group

    groups: List[Dict[str, Any]] = []
    current_records: List[Dict[str, Any]] = []
    current_files: List[str] = []
    current_tokens = 0
    current_nonempty_lines = 0
    current_tiny_count = 0

    def flush_current() -> None:
        nonlocal current_records, current_files, current_tokens
        nonlocal current_nonempty_lines, current_tiny_count
        if current_records:
            groups.append(make_group(current_records, current_files))
            current_records = []
            current_files = []
            current_tokens = 0
            current_nonempty_lines = 0
            current_tiny_count = 0

    for chapter_id, raw_names in chapter_groups.items():
        if isinstance(raw_names, Mapping):
            raw_names = raw_names.get("files", [])
        if not isinstance(raw_names, (list, tuple)):
            continue
        chapter_files = [str(name) for name in raw_names]
        assigned = [name for name in chapter_files if name in pending]
        if not assigned:
            # Keep completed chapters as adjacency barriers on resume; do not
            # silently pack two pending chapters across a skipped TOC branch.
            flush_current()
            continue
        stats = {
            name: file_stats[name]
            for name in assigned
            if isinstance(file_stats.get(name), Mapping)
        }
        batches = recommended_batches(
            stats,
            max_files,
            max_tokens,
            max_bytes,
            max_nonempty_lines,
        )
        chapter_split = len(batches) > 1
        chapter_oversized = any(
            int(stats[name].get("estimated_tokens", 0)) > max_tokens
            or int(stats[name].get("size_bytes", 0)) > max_bytes
            or int(stats[name].get("nonempty_line_count", 0)) > max_nonempty_lines
            for name in assigned
        )
        chapter_has_special_appendix = any(
            str(manifest_roles.get(name) or infer_translation_file_role(name) or "")
            in {"index", "bibliography"}
            for name in assigned
        )
        record = {
            "chapter_id": str(chapter_id),
            "files": chapter_files,
            "chapter_file_count": len(chapter_files),
        }

        # A split or oversized chapter is a hard boundary.  Flush any short
        # chapters before it, then keep every part isolated from neighbours.
        if (
            chapter_split
            or chapter_oversized
            or chapter_has_special_appendix
            or not pack_adjacent
        ):
            flush_current()
            for part_index, batch_files in enumerate(batches, 1):
                groups.append(
                    make_group(
                        [record],
                        list(batch_files),
                        part_index=part_index,
                        part_count=len(batches),
                        chapter_split=chapter_split,
                    )
                )
            continue

        batch_files = list(batches[0])
        chapter_tokens = sum(
            int(stats[name].get("estimated_tokens", 0)) for name in batch_files
        )
        chapter_nonempty_lines = sum(
            int(stats[name].get("nonempty_line_count", 0)) for name in batch_files
        )
        chapter_is_tiny = chapter_tokens <= tiny_chapter_max_tokens
        tiny_pack_limit_applies = (
            bool(current_records)
            and current_tiny_count == len(current_records)
            and chapter_is_tiny
        )
        chapter_limit = max_tiny_chapters if tiny_pack_limit_applies else max_chapters
        can_pack = (
            current_records
            and len(current_records) < chapter_limit
            and len(current_files) + len(batch_files) <= max_files
            and current_tokens + chapter_tokens <= max_packed_tokens
            and current_nonempty_lines + chapter_nonempty_lines <= max_nonempty_lines
        )
        if current_records and not can_pack:
            flush_current()
        current_records.append(record)
        current_files.extend(batch_files)
        current_tokens += chapter_tokens
        current_nonempty_lines += chapter_nonempty_lines
        if chapter_is_tiny:
            current_tiny_count += 1

    flush_current()
    return groups

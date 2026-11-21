"""Pure worker scheduling and scoped manifest projection."""
from __future__ import annotations

import re
from typing import Any, Dict, List, Mapping, Optional

from .batching_service import (
    DEFAULT_BATCH_MAX_FILES, DEFAULT_BATCH_MAX_SOURCE_TOKENS,
    DEFAULT_BATCH_MAX_NONEMPTY_LINES, DEFAULT_SINGLE_FILE_MAX_BYTES,
    DEFAULT_PACKED_SOURCE_TOKEN_MULTIPLIER, DEFAULT_PACK_ADJACENT_CHAPTERS,
    DEFAULT_MAX_CHAPTERS_PER_WORKER, DEFAULT_MAX_TINY_CHAPTERS_PER_WORKER,
    DEFAULT_TINY_CHAPTER_MAX_TOKENS, MAX_BATCH_FILES, MAX_ACTIVE_SUBAGENTS,
    DEFAULT_BATCH_MAX_CONCURRENCY, METADATA_LANE_MAX_CONCURRENCY,
    DEFAULT_LARGE_FILE_TOKEN_THRESHOLD, DEFAULT_EXTREME_FILE_TOKEN_THRESHOLD,
    positive_int, recommended_batches, POLISH_ISOLATED_ROLES,
    batching_config, classify_unit_severity,
)
from .document_roles import infer_translation_file_role


def _normalized_batching(config: Mapping[str, Any]) -> Dict[str, Any]:
    """Accept either a full book config or an already-flattened batching map."""
    if not isinstance(config, Mapping):
        return batching_config({})
    source = config
    if isinstance(config.get("subagent"), Mapping) or isinstance(config.get("batching"), Mapping):
        normalized = batching_config(config)
    else:
        normalized = batching_config({"subagent": {"batching": dict(config)}})
    # Scheduling-only flags are not part of the persisted batching block.
    if "metadata_lane" in source:
        normalized["metadata_lane"] = bool(source.get("metadata_lane"))
    return normalized


def plan_waves(
    groups: List[Dict[str, Any]],
    file_stats: Mapping[str, Mapping[str, Any]],
    config: Mapping[str, Any],
    *,
    strategy: str = "source_order",
) -> List[Dict[str, Any]]:
    """Plan ordered dispatch waves from worker groups (pure, no annotations).

    Severity is based on each group's largest individual file, not its total
    assigned tokens.  The returned plan has no runtime semaphore semantics; it
    is an explicit scheduling contract for operators.  Metadata work is planned
    in its own low-priority lane and never consumes a body-wave slot.

    ``config`` may be the full book configuration (with ``subagent.batching``)
    or an already normalized batching mapping.
    """
    batching = _normalized_batching(config)
    configured_cap = min(
        MAX_ACTIVE_SUBAGENTS,
        positive_int(batching.get("max_concurrency"), DEFAULT_BATCH_MAX_CONCURRENCY),
    )
    if strategy not in {"source_order", "ordinary_before_large"}:
        raise ValueError(f"Unknown dispatch strategy: {strategy}")
    reordered = strategy == "ordinary_before_large"

    def severity(group: Mapping[str, Any]) -> str:
        largest_file = max(
            (
                int((file_stats.get(name, {}) or {}).get("estimated_tokens", 0))
                for name in group.get("files", [])
            ),
            default=0,
        )
        return classify_unit_severity(largest_file, batching)

    # HTML translation can keep ordinary IDE slots busy while avoiding an
    # ordinary/large alternation that would otherwise create many singleton
    # waves. Extreme units remain hard ordering barriers. Only the wave order
    # and per-group annotations follow this scheduling view; the caller's group
    # list is never reordered.
    scheduling_groups = list(groups)
    if reordered:
        scheduling_groups = []
        segment: List[Dict[str, Any]] = []

        def flush_segment() -> None:
            if not segment:
                return
            scheduling_groups.extend(
                group for group in segment if severity(group) == "ordinary"
            )
            scheduling_groups.extend(
                group for group in segment if severity(group) == "large_units"
            )
            segment.clear()

        for group in groups:
            if severity(group) == "extreme_unit":
                flush_segment()
                scheduling_groups.append(group)
            else:
                segment.append(group)
        flush_segment()

    plan: List[Dict[str, Any]] = []
    wave_groups: List[Dict[str, Any]] = []
    wave_severity: Optional[str] = None

    def finish_wave() -> None:
        nonlocal wave_groups, wave_severity
        if not wave_groups or wave_severity is None:
            return
        cap = (
            1 if wave_severity == "extreme_unit"
            else min(2, configured_cap) if wave_severity == "large_units"
            else configured_cap
        )
        plan.append(
            {
                "wave": len(plan) + 1,
                "worker_ids": [group["worker_id"] for group in wave_groups],
                "max_concurrency": cap,
                "reason": wave_severity,
                "lane": "body",
                "reordered_for_dispatch": reordered,
            }
        )
        wave_groups = []
        wave_severity = None

    for group in scheduling_groups:
        kind = severity(group)
        if kind == "extreme_unit":
            finish_wave()
            wave_severity = kind
            wave_groups = [group]
            finish_wave()
            continue
        cap = min(2, configured_cap) if kind == "large_units" else configured_cap
        if wave_groups and (wave_severity != kind or len(wave_groups) >= cap):
            finish_wave()
        wave_severity = kind
        wave_groups.append(group)
        if len(wave_groups) >= cap:
            finish_wave()
    finish_wave()

    # Metadata is a separate low-priority lane: it does not use body-wave
    # slots, is capped at one worker, and must not overlap an extreme unit.
    if batching.get("metadata_lane"):
        plan.append(
            {
                "wave": len(plan) + 1,
                "worker_ids": [],
                "max_concurrency": METADATA_LANE_MAX_CONCURRENCY,
                "reason": "metadata",
                "lane": "metadata",
                "priority": "low",
                "overlaps_extreme_wave": False,
                "reordered_for_dispatch": False,
            }
        )
    return plan


def _plan_dispatch_waves(
    groups: List[Dict[str, Any]],
    file_stats: Mapping[str, Mapping[str, Any]],
    batching: Mapping[str, Any],
    *,
    strategy: str = "source_order",
) -> tuple[List[Dict[str, Any]], List[Dict[str, Any]], int]:
    """Compatibility wrapper: annotate group copies with the shared plan."""
    planned_groups = [dict(group) for group in groups]
    waves = plan_waves(planned_groups, file_stats, batching, strategy=strategy)
    wave_by_worker = {
        worker_id: wave
        for wave in waves
        if wave.get("lane") == "body"
        for worker_id in wave.get("worker_ids", [])
    }
    for group in planned_groups:
        wave = wave_by_worker.get(group["worker_id"])
        if wave is None:  # pragma: no cover - every body group gets a wave
            continue
        group["dispatch_wave"] = wave["wave"]
        group["dispatch_wave_max_concurrency"] = wave["max_concurrency"]
        group["dispatch_wave_reason"] = wave["reason"]
    normalized = _normalized_batching(batching)
    configured_cap = min(
        MAX_ACTIVE_SUBAGENTS,
        positive_int(normalized.get("max_concurrency"), DEFAULT_BATCH_MAX_CONCURRENCY),
    )
    return planned_groups, waves, configured_cap


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


def _pack_ordinary_batches_within_budget(
    batches: List[Dict[str, Any]],
    max_workers: int,
    *,
    max_files: int,
    max_tokens: int,
) -> List[Dict[str, Any]]:
    """Pack ordinary batches into workers without exceeding per-worker budgets."""
    total_files = sum(len(batch.get("files", [])) for batch in batches)
    total_tokens = sum(int(batch.get("estimated_tokens", 0)) for batch in batches)
    if (
        len(batches) <= max_workers
        or (total_files <= max_workers * max_files and total_tokens <= max_workers * max_tokens)
    ):
        # Try LPT balancing first; keep it only if every resulting worker stays
        # within both file and token caps.
        balanced = _balanced_worker_groups(batches, max_workers)
        if all(
            sum(len(b.get("files", [])) for b in g["batches"]) <= max_files
            and int(g["estimated_tokens"]) <= max_tokens
            for g in balanced
        ):
            return balanced

    groups: List[Dict[str, Any]] = []
    current_batches: List[Dict[str, Any]] = []
    current_files = 0
    current_tokens = 0
    for batch in batches:
        batch_files = len(batch.get("files", []))
        batch_tokens = int(batch.get("estimated_tokens", 0))
        if current_batches and (
            current_files + batch_files > max_files
            or current_tokens + batch_tokens > max_tokens
        ):
            groups.append(
                {
                    "batches": current_batches,
                    "estimated_tokens": current_tokens,
                }
            )
            current_batches = []
            current_files = 0
            current_tokens = 0
        current_batches.append(batch)
        current_files += batch_files
        current_tokens += batch_tokens
    if current_batches:
        groups.append(
            {
                "batches": current_batches,
                "estimated_tokens": current_tokens,
            }
        )
    return groups


def _worker_groups(
    queue: List[Dict[str, Any]],
    max_workers: int,
    *,
    file_stats: Optional[Mapping[str, Mapping[str, Any]]] = None,
    batching: Optional[Mapping[str, Any]] = None,
    task: str = "",
) -> List[Dict[str, Any]]:
    """Pack batches into workers while isolating special and oversized units.

    Ordinary batches remain eligible for packing up to the configured per-worker
    budget.  A batch marked ``isolated``—or containing an extreme/large/oversized
    file when ``file_stats`` and ``batching`` are supplied—gets its own worker
    so an index, bibliography, notes, front-matter, or giant endnote unit never
    shares a Subagent with ordinary chapters.  Worker ids remain stable
    ``worker_NNN`` values.
    """
    if not queue:
        return []
    large_threshold = (
        positive_int(
            batching.get("large_file_token_threshold"),
            DEFAULT_LARGE_FILE_TOKEN_THRESHOLD,
        )
        if isinstance(batching, Mapping)
        else None
    )
    single_file_max_bytes = (
        positive_int(
            batching.get("single_file_max_bytes"),
            DEFAULT_SINGLE_FILE_MAX_BYTES,
        )
        if isinstance(batching, Mapping)
        else None
    )

    def _is_isolated_batch(batch: Mapping[str, Any]) -> bool:
        if bool(batch.get("isolated")):
            return True
        if task != "polish" or not isinstance(file_stats, Mapping) or large_threshold is None:
            return False
        for name in batch.get("files", []) or []:
            stats = file_stats.get(name, {}) or {}
            if int(stats.get("estimated_tokens", 0)) >= large_threshold:
                return True
            if (
                single_file_max_bytes is not None
                and int(stats.get("size_bytes", 0)) > single_file_max_bytes
            ):
                return True
        return False

    isolated_batches = [batch for batch in queue if _is_isolated_batch(batch)]
    ordinary_batches = [batch for batch in queue if not _is_isolated_batch(batch)]
    groups: List[Dict[str, Any]] = [
        {
            "batches": [batch],
            "estimated_tokens": int(batch.get("estimated_tokens", 0)),
        }
        for batch in isolated_batches
    ]
    if ordinary_batches:
        if task == "polish" and isinstance(batching, Mapping) and file_stats is not None:
            max_files = min(
                MAX_BATCH_FILES,
                positive_int(batching.get("polish_max_files"), DEFAULT_BATCH_MAX_FILES),
            )
            max_source_tokens = positive_int(
                batching.get("max_source_tokens"), DEFAULT_BATCH_MAX_SOURCE_TOKENS
            )
            max_packed_tokens = max(
                max_source_tokens,
                positive_int(
                    batching.get("max_packed_source_tokens"),
                    max_source_tokens * DEFAULT_PACKED_SOURCE_TOKEN_MULTIPLIER,
                ),
            )
            groups.extend(
                _pack_ordinary_batches_within_budget(
                    ordinary_batches,
                    max_workers,
                    max_files=max_files,
                    max_tokens=max_packed_tokens,
                )
            )
        else:
            groups.extend(_balanced_worker_groups(ordinary_batches, max_workers))
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
    pack_tiny_special = batching.get("pack_tiny_special_units", False) is True
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
    special_records: List[Dict[str, Any]] = []
    special_files: List[str] = []
    special_tokens = 0
    special_nonempty_lines = 0
    special_tiny_only = True

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

    def flush_special() -> None:
        nonlocal special_records, special_files, special_tokens
        nonlocal special_nonempty_lines, special_tiny_only
        if special_records:
            groups.append(make_group(special_records, special_files))
            special_records = []
            special_files = []
            special_tokens = 0
            special_nonempty_lines = 0
            special_tiny_only = True

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
            flush_special()
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
        chapter_has_isolated_role = any(
            str(manifest_roles.get(name) or infer_translation_file_role(name) or "")
            in POLISH_ISOLATED_ROLES
            for name in assigned
        )
        record = {
            "chapter_id": str(chapter_id),
            "files": chapter_files,
            "chapter_file_count": len(chapter_files),
        }

        if chapter_has_isolated_role:
            flush_current()
            # A split or oversized special unit is always its own worker.
            if chapter_split or chapter_oversized:
                flush_special()
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
            can_pack_special = (
                pack_tiny_special
                and chapter_is_tiny
                and special_tiny_only
                and special_records
                and len(special_records) < max_tiny_chapters
                and len(special_files) + len(batch_files) <= max_files
                and special_tokens + chapter_tokens <= max_packed_tokens
                and special_nonempty_lines + chapter_nonempty_lines <= max_nonempty_lines
            )
            if special_records and not can_pack_special:
                flush_special()
            special_records.append(record)
            special_files.extend(batch_files)
            special_tokens += chapter_tokens
            special_nonempty_lines += chapter_nonempty_lines
            special_tiny_only = special_tiny_only and chapter_is_tiny
            continue

        flush_special()
        # A split or oversized ordinary chapter is a hard boundary.  Flush
        # any short chapters before it, then keep every part isolated.
        if chapter_split or chapter_oversized or not pack_adjacent:
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
    flush_special()
    return groups

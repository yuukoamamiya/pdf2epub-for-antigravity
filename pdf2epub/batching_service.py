"""Public planning services for scoped Subagent work batches."""
from __future__ import annotations
import re
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional

DEFAULT_BATCH_MAX_FILES = 5
DEFAULT_POLISH_BATCH_MAX_FILES = 4
MAX_BATCH_FILES = 8
DEFAULT_BATCH_MAX_SOURCE_TOKENS = 12_000
DEFAULT_BATCH_MAX_CONCURRENCY = 3
# The default remains conservative, but configurations may opt into more
# parallel independent hand-offs when the IDE/model quota permits it.
MAX_ACTIVE_SUBAGENTS = 6
DEFAULT_SINGLE_FILE_MAX_BYTES = 30_000
DEFAULT_BATCH_MAX_NONEMPTY_LINES = 350
DEFAULT_LARGE_FILE_TOKEN_THRESHOLD = 12_000
DEFAULT_EXTREME_FILE_TOKEN_THRESHOLD = 24_000
DEFAULT_GLOBAL_TOC_TOKENS = 1_200
DEFAULT_PACK_ADJACENT_CHAPTERS = True
DEFAULT_MAX_CHAPTERS_PER_WORKER = 3
DEFAULT_MAX_TINY_CHAPTERS_PER_WORKER = 6
DEFAULT_TINY_CHAPTER_MAX_TOKENS = 2_000
DEFAULT_PACKED_SOURCE_TOKEN_MULTIPLIER = 2
POLISH_ISOLATED_ROLES = frozenset({
    "index", "bibliography", "notes", "author_notes", "toc", "catalog",
    "ad", "copyright", "frontmatter", "metadata", "cover",
})


def markdown_files(directory: Path) -> List[Path]:
    return sorted(path for path in directory.glob("*.md") if path.is_file())


@lru_cache(maxsize=1)
def _get_tokenizer():
    """Load the local tokenizer once for manifest estimates."""
    try:
        import tiktoken

        return tiktoken.get_encoding("cl100k_base")
    except Exception:
        # The estimate is advisory only.  Keep task preparation usable if a
        # downstream installation omits the optional tokenizer package.
        return None


def estimate_tokens(text: str) -> int:
    tokenizer = _get_tokenizer()
    if tokenizer is not None:
        return len(tokenizer.encode(text))
    return max(1, (len(text) + 3) // 4) if text else 0

def positive_int(value: Any, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def batching_config(config: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    subagent = config.get("subagent", {}) if isinstance(config, Mapping) else {}
    batching = subagent.get("batching", {}) if isinstance(subagent, Mapping) else {}
    if not isinstance(batching, Mapping):
        batching = {}
    refine = config.get("refine", {}) if isinstance(config, Mapping) else {}
    refine_batching = refine.get("batching", {}) if isinstance(refine, Mapping) else {}
    if not isinstance(refine_batching, Mapping):
        refine_batching = {}

    def configured(name: str, default: Any = None) -> Any:
        if name in batching:
            return batching[name]
        if name in refine_batching:
            return refine_batching[name]
        return default

    explicit_max_chapters = "max_chapters_per_worker" in batching or "max_chapters_per_worker" in refine_batching
    configured_max_chapters = configured(
        "max_chapters_per_worker", DEFAULT_MAX_CHAPTERS_PER_WORKER
    )
    max_source_tokens = positive_int(
        batching.get("max_source_tokens"), DEFAULT_BATCH_MAX_SOURCE_TOKENS
    )
    max_files = min(
        MAX_BATCH_FILES,
        positive_int(batching.get("max_files"), DEFAULT_BATCH_MAX_FILES),
    )
    polish_max_files = min(
        MAX_BATCH_FILES,
        positive_int(
            configured("polish_max_files"),
            min(max_files, DEFAULT_POLISH_BATCH_MAX_FILES),
        ),
    )
    max_packed_source_tokens = max(
        max_source_tokens,
        positive_int(
            batching.get("max_packed_source_tokens"),
            max_source_tokens * DEFAULT_PACKED_SOURCE_TOKEN_MULTIPLIER,
        ),
    )
    return {
        "max_files": max_files,
        "polish_max_files": polish_max_files,
        "max_source_tokens": max_source_tokens,
        "max_packed_source_tokens": max_packed_source_tokens,
        "max_concurrency": min(
            MAX_ACTIVE_SUBAGENTS,
            positive_int(
                batching.get("max_concurrency"), DEFAULT_BATCH_MAX_CONCURRENCY
            ),
        ),
        "single_file_max_bytes": positive_int(
            batching.get("single_file_max_bytes"), DEFAULT_SINGLE_FILE_MAX_BYTES
        ),
        "max_nonempty_lines": positive_int(
            batching.get("max_nonempty_lines"), DEFAULT_BATCH_MAX_NONEMPTY_LINES
        ),
        "large_file_token_threshold": positive_int(
            batching.get("large_file_token_threshold"), DEFAULT_LARGE_FILE_TOKEN_THRESHOLD
        ),
        "extreme_file_token_threshold": positive_int(
            batching.get("extreme_file_token_threshold"), DEFAULT_EXTREME_FILE_TOKEN_THRESHOLD
        ),
        "global_toc_tokens": positive_int(
            configured("global_toc_tokens"), DEFAULT_GLOBAL_TOC_TOKENS
        ),
        "pack_adjacent_chapters": (
            configured("pack_adjacent_chapters", DEFAULT_PACK_ADJACENT_CHAPTERS)
            is not False
        ),
        "max_chapters_per_worker": min(
            MAX_BATCH_FILES,
            positive_int(configured_max_chapters, DEFAULT_MAX_CHAPTERS_PER_WORKER),
        ),
        "max_tiny_chapters_per_worker": min(
            MAX_BATCH_FILES,
            positive_int(
                configured(
                    "max_tiny_chapters_per_worker",
                    DEFAULT_MAX_TINY_CHAPTERS_PER_WORKER
                    if not explicit_max_chapters
                    else configured_max_chapters,
                ),
                DEFAULT_MAX_TINY_CHAPTERS_PER_WORKER,
            ),
        ),
        "tiny_chapter_max_tokens": positive_int(
            configured("tiny_chapter_max_tokens"),
            DEFAULT_TINY_CHAPTER_MAX_TOKENS,
        ),
    }


def effective_max_concurrency(
    file_stats: Mapping[str, Mapping[str, int]],
    configured: int,
    large_threshold: int = DEFAULT_LARGE_FILE_TOKEN_THRESHOLD,
    extreme_threshold: int = DEFAULT_EXTREME_FILE_TOKEN_THRESHOLD,
) -> tuple[int, str]:
    """Choose a conservative worker cap from the pending source inventory."""
    large = [
        name for name, stats in file_stats.items()
        if int(stats.get("estimated_tokens", 0)) >= large_threshold
    ]
    extreme = [
        name for name, stats in file_stats.items()
        if int(stats.get("estimated_tokens", 0)) >= extreme_threshold
    ]
    configured = min(MAX_ACTIVE_SUBAGENTS, max(1, int(configured)))
    if extreme or len(large) >= 2:
        return min(configured, 1), "extreme_or_multiple_large_units"
    if large:
        return min(configured, 2), "large_unit_present"
    return configured, "no_large_units"


def recommended_batches(
    file_stats: Mapping[str, Mapping[str, int]],
    max_files: int,
    max_source_tokens: int,
    single_file_max_bytes: int = DEFAULT_SINGLE_FILE_MAX_BYTES,
    max_nonempty_lines: int = DEFAULT_BATCH_MAX_NONEMPTY_LINES,
    isolated_files: Optional[set[str]] = None,
) -> List[List[str]]:
    """Create advisory, file-safe batches without splitting source lines."""
    batches: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0
    current_nonempty_lines = 0
    for name, stats in file_stats.items():
        tokens = stats["estimated_tokens"]
        isolated = (
            stats.get("size_bytes", 0) > single_file_max_bytes
            or tokens > max_source_tokens
            or stats.get("nonempty_line_count", 0) > max_nonempty_lines
        )
        force_isolated = bool(isolated_files and name in isolated_files)
        if force_isolated:
            if current:
                batches.append(current)
                current = []
                current_tokens = 0
                current_nonempty_lines = 0
            batches.append([name])
            continue
        if isolated and current:
            batches.append(current)
            current = []
            current_tokens = 0
            current_nonempty_lines = 0
        if current and (
            len(current) >= max_files
            or current_tokens + tokens > max_source_tokens
            or current_nonempty_lines + stats.get("nonempty_line_count", 0)
            > max_nonempty_lines
        ):
            batches.append(current)
            current = []
            current_tokens = 0
            current_nonempty_lines = 0
        current.append(name)
        current_tokens += tokens
        current_nonempty_lines += stats.get("nonempty_line_count", 0)
        # A large file remains alone; the manifest explicitly flags it so the
        # operator can give it an independent Subagent task.
        if isolated:
            batches.append(current)
            current = []
            current_tokens = 0
            current_nonempty_lines = 0
    if current:
        batches.append(current)
    return batches


def batch_queue(
    batches: List[List[str]],
    file_stats: Mapping[str, Mapping[str, int]],
    isolated_files: Optional[set[str]] = None,
) -> List[Dict[str, Any]]:
    """Materialize an IDE-friendly pending queue without starting models."""
    queue: List[Dict[str, Any]] = []
    for index, batch in enumerate(batches, 1):
        entry: Dict[str, Any] = {
            "batch_id": f"batch_{index:03d}",
            "files": batch,
            "estimated_tokens": sum(
                file_stats[name]["estimated_tokens"] for name in batch
            ),
            "status": "pending",
        }
        if isolated_files and any(name in isolated_files for name in batch):
            entry["isolated"] = True
        queue.append(entry)
    return queue

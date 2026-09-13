"""Shared runtime contracts for Subagent model selection and batching."""

from __future__ import annotations

import json
from functools import lru_cache
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

DEFAULT_TRANSLATION_MODEL = "gemini-3.1-pro-preview"
DEFAULT_SUBAGENT_MODEL = "gemini-3.6-flash"
DEFAULT_BATCH_MAX_FILES = 5
DEFAULT_BATCH_MAX_SOURCE_TOKENS = 12_000
DEFAULT_BATCH_MAX_CONCURRENCY = 3
DEFAULT_SINGLE_FILE_MAX_BYTES = 30_000

_TRANSLATION_TASKS = {
    "translate",
    "translate-novel",
    "translate-arxiv",
    "toc-translation",
    "metadata-translation",
}



def resolve_subagent_model(
    config: Optional[Mapping[str, Any]],
    task: str,
) -> str:
    """Resolve the model requested by a workspace Subagent task.

    The model is a task contract for Antigravity, not an API setting.  Exact
    task overrides are supported for exceptional cases; otherwise translation
    tasks use ``models.translation`` and every other task uses
    ``models.default``.
    """
    subagent = config.get("subagent", {}) if isinstance(config, Mapping) else {}
    if not isinstance(subagent, Mapping):
        subagent = {}
    models = subagent.get("models", {})
    if not isinstance(models, Mapping):
        models = {}
    task_models = subagent.get("task_models", {})
    if not isinstance(task_models, Mapping):
        task_models = {}

    for value in (task_models.get(task), models.get(task)):
        if isinstance(value, str) and value.strip():
            return value.strip()

    is_translation = task in _TRANSLATION_TASKS or task.startswith("translate-")
    fallback = DEFAULT_TRANSLATION_MODEL if is_translation else DEFAULT_SUBAGENT_MODEL
    configured = models.get("translation" if is_translation else "default")
    return configured.strip() if isinstance(configured, str) and configured.strip() else fallback

def _markdown_files(directory: Path) -> List[Path]:
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

def _positive_int(value: Any, default: int) -> int:
    try:
        value = int(value)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


def _batching_config(config: Optional[Mapping[str, Any]]) -> Dict[str, int]:
    subagent = config.get("subagent", {}) if isinstance(config, Mapping) else {}
    batching = subagent.get("batching", {}) if isinstance(subagent, Mapping) else {}
    if not isinstance(batching, Mapping):
        batching = {}
    return {
        "max_files": _positive_int(batching.get("max_files"), DEFAULT_BATCH_MAX_FILES),
        "max_source_tokens": _positive_int(
            batching.get("max_source_tokens"), DEFAULT_BATCH_MAX_SOURCE_TOKENS
        ),
        "max_concurrency": _positive_int(
            batching.get("max_concurrency"), DEFAULT_BATCH_MAX_CONCURRENCY
        ),
        "single_file_max_bytes": _positive_int(
            batching.get("single_file_max_bytes"), DEFAULT_SINGLE_FILE_MAX_BYTES
        ),
    }


def _recommended_batches(
    file_stats: Mapping[str, Mapping[str, int]],
    max_files: int,
    max_source_tokens: int,
    single_file_max_bytes: int = DEFAULT_SINGLE_FILE_MAX_BYTES,
) -> List[List[str]]:
    """Create advisory, file-safe batches without splitting source lines."""
    batches: List[List[str]] = []
    current: List[str] = []
    current_tokens = 0
    for name, stats in file_stats.items():
        tokens = stats["estimated_tokens"]
        isolated = (
            stats.get("size_bytes", 0) > single_file_max_bytes
            or tokens > max_source_tokens
        )
        if isolated and current:
            batches.append(current)
            current = []
            current_tokens = 0
        if current and (
            len(current) >= max_files or current_tokens + tokens > max_source_tokens
        ):
            batches.append(current)
            current = []
            current_tokens = 0
        current.append(name)
        current_tokens += tokens
        # A large file remains alone; the manifest explicitly flags it so the
        # operator can give it an independent Subagent task.
        if isolated:
            batches.append(current)
            current = []
            current_tokens = 0
    if current:
        batches.append(current)
    return batches


def _batch_queue(
    batches: List[List[str]],
    file_stats: Mapping[str, Mapping[str, int]],
) -> List[Dict[str, Any]]:
    """Materialize an IDE-friendly pending queue without starting models."""
    return [
        {
            "batch_id": f"batch_{index:03d}",
            "files": batch,
            "estimated_tokens": sum(file_stats[name]["estimated_tokens"] for name in batch),
            "status": "pending",
        }
        for index, batch in enumerate(batches, 1)
    ]

def write_batch_handoffs(
    output_dir: Path,
    manifest_path: Path,
    prompt_path: Path,
) -> List[Dict[str, Any]]:
    """Create one explicit, non-overlapping hand-off per pending batch.

    The main manifest remains the resumable inventory.  These scoped hand-offs
    prevent parallel Subagents from interpreting the global ``pending_files``
    list as permission to process every batch, and assign TOC writing to one
    owner only.
    """
    output_dir = Path(output_dir)
    manifest_path = Path(manifest_path)
    prompt_path = Path(prompt_path)
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    prompt = prompt_path.read_text(encoding="utf-8")
    queue = manifest.get("batch_queue", [])
    if not isinstance(queue, list):
        return []

    handoff_dir = output_dir / "batch_handoffs"
    handoff_dir.mkdir(parents=True, exist_ok=True)
    owner_id = queue[0].get("batch_id") if queue else None
    handoffs: List[Dict[str, Any]] = []
    for batch in queue:
        if not isinstance(batch, dict):
            continue
        batch_id = str(batch.get("batch_id") or "").strip()
        files = [str(name) for name in batch.get("files", [])]
        if not batch_id or not files:
            continue
        scoped = dict(manifest)
        scoped["batch_id"] = batch_id
        scoped["assigned_files"] = files
        scoped["pending_files"] = files
        scoped["completed_files"] = []
        scoped["batch_queue"] = [dict(batch, status="assigned")]
        scoped["toc_owner"] = batch_id == owner_id
        if not scoped["toc_owner"]:
            scoped.pop("toc_translation", None)
        scoped_name = f"translate_subagent_manifest_{batch_id}.json"
        scoped_prompt_name = f"translate_subagent_prompt_{batch_id}.md"
        scoped_path = handoff_dir / scoped_name
        scoped_prompt_path = handoff_dir / scoped_prompt_name
        scoped_path.write_text(
            json.dumps(scoped, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        toc_instruction = (
            "You are the sole TOC owner for this task. Complete the required "
            "TOC translation and write toc_tree_translated.json."
            if scoped["toc_owner"]
            else
            "This batch is not the TOC owner. Do not create or modify "
            "toc_tree_translated.json."
        )
        scoped_prompt_path.write_text(
            prompt
            + f"\n\n## Assigned batch: {batch_id}\n\n"
            + f"Use the scoped manifest `{scoped_name}` in this directory.\n"
            + f"Process only these files: {', '.join(files)}.\n"
            + "Do not process files from any other batch, even if they appear "
            + "in the parent manifest.\n"
            + toc_instruction
            + "\n",
            encoding="utf-8",
        )
        handoffs.append(
            {
                "batch_id": batch_id,
                "files": files,
                "manifest": str(scoped_path.relative_to(output_dir)).replace("\\", "/"),
                "prompt": str(scoped_prompt_path.relative_to(output_dir)).replace("\\", "/"),
                "toc_owner": scoped["toc_owner"],
                "status": "pending",
            }
        )
    manifest["batch_handoffs"] = handoffs
    manifest["toc_owner_batch_id"] = owner_id
    manifest_path.write_text(
        json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
    )
    return handoffs

__all__ = [
    "DEFAULT_BATCH_MAX_CONCURRENCY",
    "DEFAULT_BATCH_MAX_FILES",
    "DEFAULT_BATCH_MAX_SOURCE_TOKENS",
    "DEFAULT_SINGLE_FILE_MAX_BYTES",
    "DEFAULT_SUBAGENT_MODEL",
    "DEFAULT_TRANSLATION_MODEL",
    "estimate_tokens",
    "resolve_subagent_model",
    "write_batch_handoffs",
]

"""Compatibility facade for the split Subagent workflow modules."""

from .markdown_handoff import prepare_markdown_subagent
from .markdown_subagent_validation import validate_markdown_subagent
from .markdown_validation import (
    _heading_reduction_is_duplicate_only,
    _special_role_numeric_markers,
    _validate_special_role_markers,
    detect_bilingual_output,
    fix_reference_heading_mismatch,
    strip_outer_markdown_fences,
    translation_diff_summary,
)
from .subagent_runtime import (
    DEFAULT_BATCH_MAX_CONCURRENCY,
    DEFAULT_BATCH_MAX_FILES,
    DEFAULT_BATCH_MAX_SOURCE_TOKENS,
    DEFAULT_SINGLE_FILE_MAX_BYTES,
    DEFAULT_SUBAGENT_MODEL,
    DEFAULT_TRANSLATION_MODEL,
    _batch_queue,
    _batching_config,
    _get_tokenizer,
    _markdown_files,
    _positive_int,
    _recommended_batches,
    estimate_tokens,
    resolve_subagent_model,
    write_batch_handoffs,
)
from .subagent_safety import (
    _REFUSAL_PATTERNS,
    _normalize_detection_text,
    detect_refusal,
)
from .toc_translation_workflow import (
    integrate_toc_translation_task,
    prepare_toc_translation_subagent,
    validate_toc_translation_subagent,
)

__all__ = [
    "detect_bilingual_output",
    "detect_refusal",
    "estimate_tokens",
    "fix_reference_heading_mismatch",
    "integrate_toc_translation_task",
    "prepare_markdown_subagent",
    "prepare_toc_translation_subagent",
    "resolve_subagent_model",
    "strip_outer_markdown_fences",
    "translation_diff_summary",
    "validate_markdown_subagent",
    "validate_toc_translation_subagent",
    "write_batch_handoffs",
]

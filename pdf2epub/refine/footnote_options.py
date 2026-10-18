"""Configuration and compatibility contracts for footnote preparation.

Keeping the option object in a dependency-free module prevents the page
scanner, validation, and materialization stages from importing the orchestration
module just to read a default.  The public re-exports in ``footnote_prepare``
remain in place for older callers.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Optional


DEFAULT_BOTTOM_RATIO = 0.50
DEFAULT_BOTTOM_INTERSECTION_RATIO = 0.08
DEFAULT_CONTEXT_BLOCKS = 2
DEFAULT_AUTO_ACCEPT = True
DEFAULT_NATIVE_MAX_FONT_RATIO = 0.88
DEFAULT_FOOTNOTE_MODE = "auto"
FOOTNOTE_MODES = frozenset({"auto", "footnotes", "endnotes_only", "disabled"})
# Modes stored in candidate/decision checkpoints must be resolved values.
EFFECTIVE_FOOTNOTE_MODES = frozenset({"footnotes", "endnotes_only", "disabled"})
TOC_FOOTNOTE_MODES = frozenset({"footnotes", "endnotes_only"})


@dataclass(frozen=True)
class FootnotePrepareOptions:
    """Validated settings shared by candidate and handoff preparation."""

    mode: str = DEFAULT_FOOTNOTE_MODE
    bottom_ratio: float = DEFAULT_BOTTOM_RATIO
    bottom_intersection_ratio: float = DEFAULT_BOTTOM_INTERSECTION_RATIO
    context_blocks: int = DEFAULT_CONTEXT_BLOCKS
    auto_accept: bool = DEFAULT_AUTO_ACCEPT
    native_max_font_ratio: float = DEFAULT_NATIVE_MAX_FONT_RATIO


def resolve_footnote_options(
    config: Optional[Mapping[str, Any]] = None,
    *,
    mode: Optional[str] = None,
    bottom_ratio: Optional[float] = None,
    bottom_intersection_ratio: Optional[float] = None,
    context_blocks: Optional[int] = None,
    auto_accept: Optional[bool] = None,
    native_max_font_ratio: Optional[float] = None,
) -> FootnotePrepareOptions:
    """Resolve configuration defaults and CLI overrides in one place."""
    footnote_config = config.get("footnotes", {}) if isinstance(config, Mapping) else {}
    if not isinstance(footnote_config, Mapping):
        footnote_config = {}

    resolved_mode = str(
        mode
        if mode is not None
        else footnote_config.get("mode", DEFAULT_FOOTNOTE_MODE)
    ).strip().lower()
    if resolved_mode not in FOOTNOTE_MODES:
        allowed = ", ".join(sorted(FOOTNOTE_MODES))
        raise ValueError(f"footnotes.mode must be one of: {allowed}")

    resolved_bottom_ratio = (
        bottom_ratio
        if bottom_ratio is not None
        else footnote_config.get("bottom_ratio", DEFAULT_BOTTOM_RATIO)
    )
    try:
        resolved_bottom_ratio = float(resolved_bottom_ratio)
    except (TypeError, ValueError):
        resolved_bottom_ratio = DEFAULT_BOTTOM_RATIO
    if not 0.0 <= resolved_bottom_ratio < 1.0:
        raise ValueError("bottom_ratio must be between 0 and 1")

    resolved_bottom_intersection_ratio = (
        bottom_intersection_ratio
        if bottom_intersection_ratio is not None
        else footnote_config.get(
            "bottom_intersection_ratio", DEFAULT_BOTTOM_INTERSECTION_RATIO
        )
    )
    try:
        resolved_bottom_intersection_ratio = float(resolved_bottom_intersection_ratio)
    except (TypeError, ValueError):
        resolved_bottom_intersection_ratio = DEFAULT_BOTTOM_INTERSECTION_RATIO
    if not 0.0 < resolved_bottom_intersection_ratio <= 1.0:
        raise ValueError("bottom_intersection_ratio must be between 0 and 1")

    resolved_context_blocks = (
        context_blocks
        if context_blocks is not None
        else footnote_config.get("context_blocks", DEFAULT_CONTEXT_BLOCKS)
    )
    try:
        resolved_context_blocks = max(0, int(resolved_context_blocks))
    except (TypeError, ValueError):
        resolved_context_blocks = DEFAULT_CONTEXT_BLOCKS

    resolved_auto_accept = (
        auto_accept
        if auto_accept is not None
        else footnote_config.get("auto_accept", DEFAULT_AUTO_ACCEPT)
    )

    resolved_font_ratio = (
        native_max_font_ratio
        if native_max_font_ratio is not None
        else footnote_config.get(
            "native_max_font_ratio", DEFAULT_NATIVE_MAX_FONT_RATIO
        )
    )
    try:
        resolved_font_ratio = float(resolved_font_ratio)
    except (TypeError, ValueError):
        resolved_font_ratio = DEFAULT_NATIVE_MAX_FONT_RATIO
    if not 0.0 < resolved_font_ratio <= 1.0:
        raise ValueError("native_max_font_ratio must be between 0 and 1")

    return FootnotePrepareOptions(
        mode=resolved_mode,
        bottom_ratio=resolved_bottom_ratio,
        bottom_intersection_ratio=resolved_bottom_intersection_ratio,
        context_blocks=resolved_context_blocks,
        auto_accept=bool(resolved_auto_accept),
        native_max_font_ratio=resolved_font_ratio,
    )


def _load_json_object(path: Path) -> Optional[Mapping[str, Any]]:
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return data if isinstance(data, Mapping) else None


def _normalize_toc_mode(value: Any) -> Optional[str]:
    if not isinstance(value, str):
        return None
    mode = value.strip().lower()
    return mode if mode in TOC_FOOTNOTE_MODES else None


def toc_footnote_mode(output_dir: Path) -> Optional[str]:
    """Read the TOC Subagent's footnote-mode decision, if it is usable.

    ``tree_progress.json`` stores the current refined TOC top-level metadata, so
    it wins over the raw ``toc_tree.json``.  Invalid or missing values simply
    return ``None``; the caller decides the safe fallback.
    """
    output_dir = Path(output_dir)
    progress = _load_json_object(output_dir / "ocr_markdown" / "tree_progress.json")
    if progress is not None:
        metadata = progress.get("book_metadata")
        if isinstance(metadata, Mapping):
            mode = _normalize_toc_mode(metadata.get("footnote_mode"))
            if mode is not None:
                return mode
    toc_tree = _load_json_object(output_dir / "toc_tree.json")
    if toc_tree is not None:
        return _normalize_toc_mode(toc_tree.get("footnote_mode"))
    return None


def resolve_effective_footnote_mode(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
    *,
    mode: Optional[str] = None,
) -> dict[str, str]:
    """Resolve ``footnotes.mode: auto`` against the refined TOC decision.

    Explicit configuration always wins and never consults the TOC.  ``auto``
    reads the Subagent's decision from ``tree_progress.json`` first, then
    ``toc_tree.json``, and falls back to ``footnotes`` for old TOCs that do not
    contain the field.  This function only reads metadata; it never scans pages.
    """
    configured = resolve_footnote_options(config, mode=mode).mode
    if configured != "auto":
        return {"configured": configured, "effective": configured, "source": "config"}
    toc_mode = toc_footnote_mode(output_dir)
    if toc_mode is not None:
        return {"configured": configured, "effective": toc_mode, "source": "toc"}
    return {"configured": configured, "effective": "footnotes", "source": "default"}


__all__ = [
    "DEFAULT_AUTO_ACCEPT",
    "DEFAULT_BOTTOM_INTERSECTION_RATIO",
    "DEFAULT_BOTTOM_RATIO",
    "DEFAULT_CONTEXT_BLOCKS",
    "DEFAULT_FOOTNOTE_MODE",
    "DEFAULT_NATIVE_MAX_FONT_RATIO",
    "EFFECTIVE_FOOTNOTE_MODES",
    "FOOTNOTE_MODES",
    "FootnotePrepareOptions",
    "TOC_FOOTNOTE_MODES",
    "resolve_effective_footnote_mode",
    "resolve_footnote_options",
    "toc_footnote_mode",
]

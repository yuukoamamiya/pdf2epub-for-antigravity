"""Configuration and compatibility contracts for footnote preparation.

Keeping the option object in a dependency-free module prevents the page
scanner, validation, and materialization stages from importing the orchestration
module just to read a default.  The public re-exports in ``footnote_prepare``
remain in place for older callers.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping, Optional


DEFAULT_BOTTOM_RATIO = 0.50
DEFAULT_BOTTOM_INTERSECTION_RATIO = 0.08
DEFAULT_CONTEXT_BLOCKS = 2
DEFAULT_AUTO_ACCEPT = True
DEFAULT_NATIVE_MAX_FONT_RATIO = 0.88
FOOTNOTE_MODES = frozenset({"footnotes", "endnotes_only", "disabled"})


@dataclass(frozen=True)
class FootnotePrepareOptions:
    """Validated settings shared by candidate and handoff preparation."""

    mode: str = "footnotes"
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
        mode if mode is not None else footnote_config.get("mode", "footnotes")
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


__all__ = [
    "DEFAULT_AUTO_ACCEPT",
    "DEFAULT_BOTTOM_INTERSECTION_RATIO",
    "DEFAULT_BOTTOM_RATIO",
    "DEFAULT_CONTEXT_BLOCKS",
    "DEFAULT_NATIVE_MAX_FONT_RATIO",
    "FOOTNOTE_MODES",
    "FootnotePrepareOptions",
    "resolve_footnote_options",
]

"""Conservative font evidence for native-text footnote scanning.

Font size is only useful as a recall filter when the source document shows a
stable body/note size ladder.  This module detects that ladder from the
current native sidecars; it never classifies a block as a footnote.
"""

from __future__ import annotations

import json
from collections import Counter
from pathlib import Path
from typing import Any, Iterable, Mapping

from .footnote_signals import leading_footnote_key
from .layout_evidence import normalized_bbox, text_from_block


def _as_positive_float(value: Any) -> float | None:
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if number > 0 else None


def native_font_profile(
    sidecar_paths: Iterable[Path],
    *,
    bottom_ratio: float = 0.50,
) -> dict[str, Any]:
    """Summarize whether native sidecars expose a reliable note-size ladder.

    A smaller font is counted as note evidence only when it starts with a
    numeric key and reaches the page's lower band.  Requiring two independent
    blocks avoids enabling the gate for a lone caption or display label.
    """
    body_sizes: Counter[float] = Counter()
    note_sizes: Counter[float] = Counter()
    for sidecar_path in sidecar_paths:
        try:
            payload = json.loads(Path(sidecar_path).read_text(encoding="utf-8"))
        except (OSError, UnicodeError, json.JSONDecodeError):
            continue
        if not isinstance(payload, Mapping):
            continue
        body_size = _as_positive_float(payload.get("body_font_size"))
        if body_size is None:
            continue
        body_sizes[body_size] += 1
        blocks = payload.get("blocks", [])
        if not isinstance(blocks, list):
            continue
        for block in blocks:
            if not isinstance(block, Mapping):
                continue
            font_size = _as_positive_float(block.get("font_size"))
            bbox = normalized_bbox(block, payload)
            text = text_from_block(block)
            if (
                font_size is None
                or font_size >= body_size
                or leading_footnote_key(text) is None
                or not bbox
                or float(bbox[3]) < float(bottom_ratio) - 0.16
            ):
                continue
            note_sizes[font_size] += 1

    dominant = body_sizes.most_common(1)[0][0] if body_sizes else None
    ladder_sizes = sorted(
        size for size, count in note_sizes.items()
        if dominant is not None and size < dominant and count >= 2
    )
    evidence_count = sum(note_sizes[size] for size in ladder_sizes)
    return {
        "enabled": bool(dominant is not None and ladder_sizes),
        "body_font_size": dominant,
        "note_font_sizes": ladder_sizes,
        "note_evidence_count": evidence_count,
        "body_font_size_counts": {
            str(size): count for size, count in sorted(body_sizes.items())
        },
        "note_font_size_counts": {
            str(size): count for size, count in sorted(note_sizes.items())
        },
        "rule": "native_body_font_size_or_larger_is_body_only_when_note_ladder_is_present",
    }


def native_body_font_excluded_blocks(
    sidecar: Mapping[str, Any],
    profile: Mapping[str, Any],
    *,
    bottom_ratio: float = 0.50,
) -> set[int]:
    """Return native blocks safely excluded by an active book-level ladder."""
    if not profile.get("enabled"):
        return set()
    dominant = _as_positive_float(profile.get("body_font_size"))
    page_body = _as_positive_float(sidecar.get("body_font_size"))
    if dominant is None or page_body is None or abs(page_body - dominant) > 0.01:
        return set()
    excluded: set[int] = set()
    blocks = sidecar.get("blocks", [])
    if not isinstance(blocks, list):
        return excluded
    for index, block in enumerate(blocks):
        if not isinstance(block, Mapping):
            continue
        font_size = _as_positive_float(block.get("font_size"))
        bbox = normalized_bbox(block, sidecar)
        # A key is required here because the native candidate path can still
        # retain unnumbered continuation context for visual review.  The font
        # gate must not erase that recall path just because the block sits in
        # the lower half of the page.
        has_numeric_key = leading_footnote_key(text_from_block(block)) is not None
        if (
            font_size is not None
            and font_size >= page_body
            and has_numeric_key
            and bbox
            and float(bbox[3]) >= float(bottom_ratio) - 0.16
        ):
            excluded.add(index)
    return excluded


__all__ = ["native_body_font_excluded_blocks", "native_font_profile"]

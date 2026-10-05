"""Shared evidence selection for PDF layout review stages.

Footnote and illustration review both consume the same page sidecars.  This
module owns the source/evidence decision so a native-text PDF cannot
accidentally enter the two-OCR path in one stage but not the other.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..ocr_consensus import consensus_is_current, ocr_evidence_mode


def is_native_text_source(output_dir: Path) -> bool:
    """Return whether the current page set came from native PDF text."""
    try:
        progress = json.loads(
            (Path(output_dir) / "pages" / "ocr_progress.json").read_text(
                encoding="utf-8"
            )
        )
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    return isinstance(progress, Mapping) and progress.get("mode") == "native_text"


def pdf_evidence_mode(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> str:
    """Resolve the layout evidence mode shared by PDF review stages.

    Native text has its own page-point layout evidence and therefore bypasses
    visual OCR consensus, even when the configuration enables a secondary OCR
    backend.  ``single_ocr`` is retained as the checkpoint value for this
    source kind because downstream contracts already use that vocabulary.
    """
    if is_native_text_source(output_dir):
        return "single_ocr"
    return ocr_evidence_mode(config)


def require_current_consensus(
    output_dir: Path,
    config: Mapping[str, Any],
    *,
    stage: str,
) -> None:
    """Require a current two-OCR checkpoint when the selected mode needs it."""
    if pdf_evidence_mode(output_dir, config) != "two_ocr":
        return
    if not consensus_is_current(output_dir, config):
        raise ValueError(
            f"two-OCR {stage} review requires a current ocr_consensus.json; "
            "rerun ocr-pages and ocr-correct-validate first"
        )


__all__ = [
    "is_native_text_source",
    "pdf_evidence_mode",
    "require_current_consensus",
]

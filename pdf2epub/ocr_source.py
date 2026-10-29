"""Resolve OCR source metadata shared by workflow gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def is_legacy_native_source(output_dir: Path) -> bool:
    """Resolve legacy native source metadata; contradictory evidence blocks."""
    root = Path(output_dir)
    progress_path = root / "pages" / "ocr_progress.json"
    probe_path = root / "pdf_text_probe.json"
    progress = probe = None
    try:
        if progress_path.is_file():
            value = json.loads(progress_path.read_text(encoding="utf-8"))
            progress = value if isinstance(value, Mapping) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass
    try:
        if probe_path.is_file():
            value = json.loads(probe_path.read_text(encoding="utf-8"))
            probe = value if isinstance(value, Mapping) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        pass

    progress_mode = progress.get("mode") if progress is not None else None
    if progress_mode is not None and not isinstance(progress_mode, str):
        raise ValueError("invalid PDF source metadata: ocr_progress.json mode must be a string")
    progress_native = progress_mode == "native_text"
    probe_native = probe is not None and probe.get("recommendation") == "use_text_layer"
    progress_visual = progress_mode is not None and progress_mode != "native_text"
    probe_requires_ocr = probe is not None and (
        probe.get("recommendation") == "ocr_required"
        or (isinstance(probe.get("schema_version"), int) and probe.get("schema_version") >= 3)
    )
    if progress_native and probe_requires_ocr:
        raise ValueError("conflicting PDF source metadata: ocr_progress.json says native_text but pdf_text_probe.json requires visual OCR")
    if progress_visual and probe_native:
        raise ValueError("conflicting PDF source metadata: ocr_progress.json says visual OCR but pdf_text_probe.json is a legacy native-text probe")
    # The page progress record is the authoritative evidence. A probe by itself
    # cannot bypass current OCR gates.
    return progress_native

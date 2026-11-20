"""Resolve OCR source metadata shared by workflow gates."""

from __future__ import annotations

import json
from pathlib import Path
from typing import Mapping


def _read_progress_record(path: Path) -> Mapping | None:
    """Read the authoritative page progress record; unusable data blocks.

    A missing record simply means no page progress was written yet. A record
    that exists but cannot be parsed is contradictory evidence, so it must not
    be downgraded to "absent" and silently fall back to visual OCR.
    """
    try:
        if not path.is_file():
            return None
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise ValueError(f"could not read pages/ocr_progress.json: {exc}") from exc
    if not isinstance(value, Mapping):
        raise ValueError(
            "could not read pages/ocr_progress.json: expected a JSON object, "
            f"got {type(value).__name__}"
        )
    return value


def _read_probe_record(path: Path) -> Mapping | None:
    """Read the audit-only probe leniently; it cannot gate OCR on its own."""
    try:
        if path.is_file():
            value = json.loads(path.read_text(encoding="utf-8"))
            return value if isinstance(value, Mapping) else None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return None
    return None


def is_legacy_native_source(output_dir: Path) -> bool:
    """Resolve legacy native source metadata; contradictory evidence blocks."""
    root = Path(output_dir)
    progress_path = root / "pages" / "ocr_progress.json"
    probe_path = root / "pdf_text_probe.json"
    progress = _read_progress_record(progress_path)
    probe = _read_probe_record(probe_path)

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

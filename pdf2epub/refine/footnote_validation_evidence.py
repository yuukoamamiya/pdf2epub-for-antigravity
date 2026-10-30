"""Filesystem and configuration evidence checks for footnote decisions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import sha256_file
from .footnote_options import EFFECTIVE_FOOTNOTE_MODES, resolve_effective_footnote_mode
from .pdf_evidence import pdf_evidence_mode, require_current_consensus


def validate_candidate_evidence(
    output_dir: Path,
    report: Mapping[str, Any],
    config: Optional[Mapping[str, Any]],
) -> dict[str, Any]:
    """Read current config/evidence and return fields/errors in contract order."""
    source_kind = str(report.get("source_kind") or "ocr")
    evidence_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    footnote_mode = str(report.get("footnote_mode") or "footnotes").strip().lower()
    configured_footnote_mode: Optional[str] = None
    footnote_mode_source: Optional[str] = None
    errors: list[str] = []
    if source_kind not in {"ocr", "native_text"}:
        errors.append("candidate report has an unsupported page source kind")
    if evidence_mode not in {"single_ocr", "two_ocr"}:
        errors.append("candidate report has an unsupported OCR evidence mode")
    if footnote_mode not in EFFECTIVE_FOOTNOTE_MODES:
        errors.append("candidate report has an unsupported footnote mode")
    if config is not None:
        configured_mode = pdf_evidence_mode(output_dir, config)
        if evidence_mode != configured_mode:
            errors.append(
                f"candidate report was prepared in {evidence_mode}, but configuration requires {configured_mode}"
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                errors.append(str(exc))
    try:
        resolution = resolve_effective_footnote_mode(output_dir, config)
        configured_footnote_mode = resolution["configured"]
        footnote_mode_source = resolution["source"]
        if footnote_mode != resolution["effective"]:
            errors.append(
                "candidate report was prepared with footnotes.mode="
                f"{footnote_mode}, but the current configuration/TOC requires "
                f"{resolution['effective']} (source: {resolution['source']})"
            )
        recorded_configured = report.get("configured_footnote_mode")
        if (
            isinstance(recorded_configured, str)
            and recorded_configured.strip().lower() != configured_footnote_mode
        ):
            errors.append(
                "candidate report was prepared with configured footnotes.mode="
                f"{recorded_configured}, but configuration now resolves "
                f"{configured_footnote_mode}"
            )
    except ValueError as exc:
        errors.append(str(exc))

    def hashes_current(field: str) -> bool:
        expected_hashes = report.get(field, {})
        if not isinstance(expected_hashes, Mapping):
            errors.append(f"candidate report field {field} must be an object")
            return False
        current_hashes: dict[str, str] = {}
        for relative, expected in expected_hashes.items():
            path = output_dir / str(relative)
            if path.is_file():
                current_hashes[str(relative)] = sha256_file(path)
        if current_hashes != dict(expected_hashes):
            errors.append(f"one or more {field} files changed or disappeared")
            return False
        return True

    hashes_current("sidecar_sha256")
    secondary_hashes = report.get("secondary_sidecar_sha256", {})
    if evidence_mode == "two_ocr":
        hashes_current("secondary_sidecar_sha256")
    elif secondary_hashes:
        errors.append("single-OCR candidate report unexpectedly contains secondary OCR files")
    return {
        "source_kind": source_kind,
        "evidence_mode": evidence_mode,
        "footnote_mode": footnote_mode,
        "configured_footnote_mode": configured_footnote_mode,
        "footnote_mode_source": footnote_mode_source,
        "errors": errors,
    }


def load_visual_audit_manifest(
    output_dir: Path, report: Mapping[str, Any]
) -> tuple[Mapping[str, Any], str | None]:
    """Load the optional visual audit manifest and return its contract error."""
    visual_manifest_path = output_dir / str(
        report.get("visual_audit_manifest") or "footnote_visual_audit_manifest.json"
    )
    if not visual_manifest_path.is_file():
        return {}, None
    try:
        loaded = json.loads(visual_manifest_path.read_text(encoding="utf-8"))
        if isinstance(loaded, Mapping):
            return loaded, None
        return {}, None
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}, "footnote visual audit manifest is invalid"

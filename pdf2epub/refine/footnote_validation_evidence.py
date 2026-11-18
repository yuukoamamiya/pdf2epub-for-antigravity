"""Filesystem and configuration evidence checks for footnote decisions."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import sha256_file
from .footnote_options import EFFECTIVE_FOOTNOTE_MODES, resolve_effective_footnote_mode
from .pdf_evidence import pdf_evidence_mode, require_current_consensus
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_page_evidence import page_sidecar_index
from .layout_evidence import text_from_block


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
    if report.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        errors.append(
            "candidate report uses an older footnote contract; rerun footnote-prepare"
        )
    if not isinstance(report.get("continuation_reviews"), list):
        errors.append(
            "candidate report is missing its continuation_reviews queue; rerun footnote-prepare"
        )
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
    manifest_path = Path(output_dir) / "footnote_subagent_manifest.json"
    if manifest_path.is_file():
        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            from .footnote_review_packet import validate_review_packet
            validate_review_packet(Path(output_dir), manifest)
        except (OSError, UnicodeError, json.JSONDecodeError, ValueError) as exc:
            errors.append(f"review packet/evidence contract is invalid: {exc}")
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


def load_candidate_full_texts(
    output_dir: Path,
    report: Mapping[str, Any],
    requested_addresses: set[tuple[int, str, int]],
) -> tuple[dict[tuple[int, str, int], str], list[str]]:
    """Read full candidate text from the current, hash-checked sidecars."""
    result: dict[tuple[int, str, int], str] = {}
    errors: list[str] = []
    try:
        sidecar_index = page_sidecar_index(Path(output_dir), report)
    except (OSError, ValueError) as exc:
        return {}, [f"cannot load current footnote sidecars: {exc}"]
    sidecar_cache: dict[tuple[int, str], Mapping[str, Any]] = {}
    for page, source, block in sorted(requested_addresses):
        source_evidence = sidecar_index.get(page, {}).get(source)
        if not isinstance(source_evidence, Mapping):
            errors.append(f"{source} sidecar is missing for candidate page {page}")
            continue
        cache_key = (page, source)
        sidecar = sidecar_cache.get(cache_key)
        if sidecar is None:
            try:
                loaded = json.loads((Path(output_dir) / str(source_evidence["path"])).read_text(encoding="utf-8"))
            except (OSError, UnicodeError, json.JSONDecodeError) as exc:
                errors.append(f"cannot load {source} sidecar for page {page}: {exc}")
                continue
            sidecar = loaded if isinstance(loaded, Mapping) else {}
            sidecar_cache[cache_key] = sidecar
        blocks = sidecar.get("blocks") if isinstance(sidecar, Mapping) else None
        if not isinstance(blocks, list):
            errors.append(f"{source} sidecar has no blocks for page {page}")
            continue
        if block < 0 or block >= len(blocks) or not isinstance(blocks[block], Mapping):
            errors.append(f"{source} sidecar has no exact block {block} for page {page}")
            continue
        result[(page, source, block)] = text_from_block(blocks[block])
    return result, errors

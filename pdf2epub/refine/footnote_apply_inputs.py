"""Read and validate the filesystem evidence required for footnote apply."""

from __future__ import annotations

from dataclasses import dataclass
import json
import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import sha256_file
from .footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from .footnote_edit_plan import split_multi_superscripts
from .footnote_options import EFFECTIVE_FOOTNOTE_MODES, resolve_effective_footnote_mode
from .footnote_status import unit_records
from .endnote_review import (
    AMBIGUITIES_FILENAME,
    DECISIONS_FILENAME,
    load_endnote_definition_ambiguities,
    load_endnote_definition_decisions,
)
from .pdf_evidence import pdf_evidence_mode, require_current_consensus


@dataclass(frozen=True)
class FootnoteApplyInputs:
    report: Mapping[str, Any]
    validation: Mapping[str, Any]
    report_mode: str
    footnote_mode: str
    configured_mode: str
    source_dir: Path
    target_dir: Path
    records: tuple[dict[str, Any], ...]
    source_texts: Mapping[str, str]
    source_hashes: Mapping[str, str]
    endnote_definition_decisions: Optional[list[dict[str, Any]]]
    endnote_ambiguities_sha256: Optional[str]
    endnote_decisions_sha256: Optional[str]
    toc_tree: Optional[Mapping[str, Any]]
    progress: Optional[Mapping[str, Any]]
    scope_error: Optional[str]


class FootnoteApplyInputError(ValueError):
    """Invalid or stale apply evidence."""


def _read_json(path: Path, label: str) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError as exc:
        raise FootnoteApplyInputError(f"{label} is missing") from exc
    except (OSError, UnicodeError, json.JSONDecodeError) as exc:
        raise FootnoteApplyInputError(f"could not read {label}: {exc}") from exc
    if not isinstance(value, dict):
        raise FootnoteApplyInputError(f"{label} must contain a JSON object")
    return value


def load_footnote_apply_inputs(
    output_dir: Path,
    config: Optional[Mapping[str, Any]],
) -> FootnoteApplyInputs:
    """Check validated contracts, freshness, and load source/evidence data."""
    output_dir = Path(output_dir)
    try:
        report = _read_json(output_dir / "footnote_candidates.json", "footnote_candidates.json")
        validation = _read_json(
            output_dir / "footnote_decision_validation.json",
            "footnote_decision_validation.json",
        )
    except ValueError as exc:
        raise FootnoteApplyInputError(str(exc)) from exc

    report_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    if report.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        raise FootnoteApplyInputError(
            "footnote candidate report uses an older marker contract; rerun footnote-prepare"
        )
    if validation.get("schema_version") != FOOTNOTE_PREPARE_SCHEMA_VERSION:
        raise FootnoteApplyInputError(
            "footnote decision validation uses an older marker contract; rerun footnote-validate"
        )
    footnote_mode = str(report.get("footnote_mode") or "footnotes").strip().lower()
    if footnote_mode not in EFFECTIVE_FOOTNOTE_MODES:
        raise FootnoteApplyInputError(f"unsupported footnote mode in candidate report: {footnote_mode}")
    validation_mode = str(validation.get("ocr_evidence_mode") or report_mode)
    validation_footnote_mode = str(
        validation.get("footnote_mode") or footnote_mode
    ).strip().lower()
    if validation_footnote_mode != footnote_mode:
        raise FootnoteApplyInputError(
            "footnote candidate and decision checkpoints use different footnotes.mode values; "
            "rerun footnote-prepare and footnote-validate"
        )

    if config is not None:
        try:
            configured_evidence_mode = pdf_evidence_mode(output_dir, config)
        except ValueError as exc:
            raise FootnoteApplyInputError(f"invalid PDF evidence metadata: {exc}") from exc
        if report_mode != configured_evidence_mode or validation_mode != configured_evidence_mode:
            raise FootnoteApplyInputError(
                "footnote checkpoints were prepared with a different OCR evidence mode; "
                "rerun footnote-prepare and footnote-validate"
            )
        if configured_evidence_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                raise FootnoteApplyInputError(
                    "two-OCR footnote checkpoint is stale; rerun ocr-pages and footnote-prepare"
                ) from exc
    try:
        resolution = resolve_effective_footnote_mode(output_dir, config)
    except ValueError as exc:
        raise FootnoteApplyInputError(str(exc)) from exc
    if footnote_mode != resolution["effective"]:
        raise FootnoteApplyInputError(
            "footnote checkpoints were prepared with a different footnotes.mode; "
            "rerun footnote-prepare and footnote-validate"
        )
    recorded_configured = report.get("configured_footnote_mode")
    if (
        isinstance(recorded_configured, str)
        and recorded_configured.strip().lower() != resolution["configured"]
    ):
        raise FootnoteApplyInputError(
            "footnote checkpoints were prepared with a different configured "
            "footnotes.mode; rerun footnote-prepare and footnote-validate"
        )
    if validation.get("valid") is not True or validation.get("status") not in {
        "validated",
        "no_subagent_review_required",
    }:
        raise FootnoteApplyInputError(
            "footnote decisions are not validated; run footnote-validate after the Subagent review"
        )

    endnote_decisions: Optional[list[dict[str, Any]]] = None
    ambiguity_hash: Optional[str] = None
    decisions_hash: Optional[str] = None
    try:
        ambiguity_manifest = load_endnote_definition_ambiguities(output_dir)
        ambiguity_items = (
            ambiguity_manifest.get("items", [])
            if isinstance(ambiguity_manifest, Mapping)
            else []
        )
        if isinstance(ambiguity_items, list) and ambiguity_items:
            endnote_validation = validation.get("endnote_definitions")
            if not isinstance(endnote_validation, Mapping) or endnote_validation.get("valid") is not True:
                raise FootnoteApplyInputError(
                    "centralized endnote definition decisions are not validated; "
                    f"run footnote-validate after reviewing {AMBIGUITIES_FILENAME}"
                )
            decisions_data = load_endnote_definition_decisions(output_dir)
            if decisions_data is None:
                raise FootnoteApplyInputError(f"{DECISIONS_FILENAME} is missing; run footnote-validate")
            raw_decisions = decisions_data.get("decisions")
            if not isinstance(raw_decisions, list):
                raise FootnoteApplyInputError(f"{DECISIONS_FILENAME} must contain a decisions array")
            endnote_decisions = [dict(item) for item in raw_decisions if isinstance(item, Mapping)]
            ambiguity_hash = sha256_file(output_dir / AMBIGUITIES_FILENAME)
            decisions_hash = sha256_file(output_dir / DECISIONS_FILENAME)
    except ValueError as exc:
        if isinstance(exc, FootnoteApplyInputError):
            raise
        raise FootnoteApplyInputError(str(exc)) from exc

    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    if not source_dir.is_dir():
        raise FootnoteApplyInputError(f"source directory is missing: {source_dir}")
    try:
        records = unit_records(output_dir)
        source_bytes = {
            record["name"]: (source_dir / record["name"]).read_bytes()
            for record in records
        }
        source_hashes = {
            name: hashlib.sha256(data).hexdigest()
            for name, data in source_bytes.items()
        }
        source_texts = {
            name: split_multi_superscripts(
                data.decode("utf-8").replace("\r\n", "\n").replace("\r", "\n")
            )
            for name, data in source_bytes.items()
        }
        if len(source_texts) != len(records):
            raise ValueError("tree_progress contains duplicate Markdown unit filenames")
    except (OSError, UnicodeError, ValueError) as exc:
        raise FootnoteApplyInputError(str(exc)) from exc

    toc_tree: Optional[Mapping[str, Any]] = None
    progress: Optional[Mapping[str, Any]] = None
    scope_error: Optional[str] = None
    if footnote_mode != "disabled":
        try:
            toc_tree = _read_json(output_dir / "toc_tree.json", "toc_tree.json")
            progress = _read_json(source_dir / "tree_progress.json", "ocr_markdown/tree_progress.json")
        except FootnoteApplyInputError as exc:
            scope_error = str(exc)

    return FootnoteApplyInputs(
        report=report,
        validation=validation,
        report_mode=report_mode,
        footnote_mode=footnote_mode,
        configured_mode=resolution["configured"],
        source_dir=source_dir,
        target_dir=target_dir,
        records=tuple(records),
        source_texts=source_texts,
        source_hashes=source_hashes,
        endnote_definition_decisions=endnote_decisions,
        endnote_ambiguities_sha256=ambiguity_hash,
        endnote_decisions_sha256=decisions_hash,
        toc_tree=toc_tree,
        progress=progress,
        scope_error=scope_error,
    )


__all__ = ["FootnoteApplyInputError", "FootnoteApplyInputs", "load_footnote_apply_inputs"]

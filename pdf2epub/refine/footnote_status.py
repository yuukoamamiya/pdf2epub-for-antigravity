"""Freshness and unit-scope checks for normalized footnote output."""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Mapping, Optional

from ..workflow_contracts import sha256_file
from .footnote_matching import _load_json
from .footnote_options import FOOTNOTE_MODES, resolve_footnote_options
from .pdf_evidence import pdf_evidence_mode, require_current_consensus


FOOTNOTE_NORMALIZATION_SCHEMA_VERSION = 2
_SAFE_FILENAME_RE = re.compile(r"^[^/\\]+\.md$")


def markdown_inventory(directory: Path) -> dict[str, str]:
    """Return the hash inventory used by normalization checkpoints."""
    return {
        path.name: sha256_file(path)
        for path in sorted(Path(directory).glob("*.md"))
        if path.is_file()
    }


def _safe_name(value: Any) -> str:
    name = str(value or "")
    if not _SAFE_FILENAME_RE.fullmatch(name):
        raise ValueError(f"invalid Markdown unit filename: {name!r}")
    return name


def unit_records(output_dir: Path) -> list[dict[str, Any]]:
    """Load refinement units and expand oversized units into ordered parts."""
    progress = _load_json(
        output_dir / "ocr_markdown" / "tree_progress.json",
        "ocr_markdown/tree_progress.json",
    )
    records: list[dict[str, Any]] = []
    for unit_order, unit in enumerate(progress.get("units", []) or []):
        if not isinstance(unit, Mapping):
            continue
        page_range = unit.get("page_range")
        if not isinstance(page_range, list) or len(page_range) != 2:
            raise ValueError("tree_progress unit has an invalid page_range")
        try:
            start_page, end_page = int(page_range[0]), int(page_range[1])
        except (TypeError, ValueError) as exc:
            raise ValueError("tree_progress unit has a non-integer page_range") from exc
        index_path = unit.get("index_path")
        unit_id = str(unit.get("unit_id") or "").strip()
        if unit_id:
            chapter_id = f"toc-unit:{unit_id}"
        elif isinstance(index_path, list) and index_path:
            path_key = ".".join(str(value) for value in index_path)
            chapter_id = f"toc-path:{path_key}"
        else:
            chapter_id = f"unit:{unit.get('file')}"
        names = unit.get("part_files") or [unit.get("file")]
        for part_index, raw_name in enumerate(names, 1):
            name = _safe_name(raw_name)
            records.append(
                {
                    "name": name,
                    "start_page": start_page,
                    "end_page": end_page,
                    "chapter_id": chapter_id,
                    "unit_order": unit_order,
                    "part_index": part_index,
                    "type": str(unit.get("type") or "").strip().lower(),
                    "index_path": index_path,
                }
            )
    if not records:
        raise ValueError("tree_progress.json contains no Markdown units")
    return records


def footnote_normalization_status(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> dict[str, Any]:
    """Explain whether derived chapter-end source still matches its inputs."""
    output_dir = Path(output_dir)
    failures: list[dict[str, str]] = []

    def fail(code: str, detail: str) -> None:
        failures.append({"code": code, "detail": detail})

    try:
        report = _load_json(
            output_dir / "footnote_normalization.json",
            "footnote_normalization.json",
        )
        validation = _load_json(
            output_dir / "footnote_decision_validation.json",
            "footnote_decision_validation.json",
        )
    except (OSError, UnicodeError, ValueError, TypeError) as exc:
        fail("checkpoint_missing_or_invalid", str(exc))
        return {
            "current": False,
            "status": "stale",
            "failures": failures,
            "detail": "footnote normalization checkpoint is missing or invalid; "
            "run footnote-prepare, footnote-validate, and footnote-apply",
        }

    if report.get("schema_version") != FOOTNOTE_NORMALIZATION_SCHEMA_VERSION:
        fail(
            "normalization_schema_mismatch",
            "footnote_normalization.json uses an older normalization contract; "
            "rerun footnote-prepare, footnote-validate, and footnote-apply",
        )
    if report.get("valid") is not True or report.get("status") != "validated":
        fail(
            "normalization_not_validated",
            "footnote_normalization.json is not a validated checkpoint",
        )
    if validation.get("valid") is not True:
        fail(
            "decision_validation_failed",
            "footnote_decision_validation.json is missing or invalid",
        )
    report_mode = str(report.get("ocr_evidence_mode") or "single_ocr")
    validation_mode = str(validation.get("ocr_evidence_mode") or report_mode)
    if report_mode != validation_mode:
        fail(
            "evidence_mode_mismatch",
            "footnote normalization and decision validation use different OCR evidence modes",
        )
    report_footnote_mode = str(
        report.get("footnote_mode") or "footnotes"
    ).strip().lower()
    validation_footnote_mode = str(
        validation.get("footnote_mode") or report_footnote_mode
    ).strip().lower()
    if report_footnote_mode not in FOOTNOTE_MODES:
        fail(
            "unsupported_footnote_mode",
            f"footnote normalization uses unsupported mode {report_footnote_mode}",
        )
    if report_footnote_mode != validation_footnote_mode:
        fail(
            "footnote_mode_mismatch",
            "footnote normalization and decision validation use different footnotes.mode values",
        )
    if config is not None:
        configured_mode = pdf_evidence_mode(output_dir, config)
        if report_mode != configured_mode:
            fail(
                "configured_evidence_mode_mismatch",
                f"checkpoint uses {report_mode}, current configuration requires {configured_mode}",
            )
        if configured_mode == "two_ocr":
            try:
                require_current_consensus(output_dir, config, stage="footnote")
            except ValueError as exc:
                fail("ocr_consensus_stale", str(exc))
        try:
            configured_footnote_mode = resolve_footnote_options(config).mode
            if report_footnote_mode != configured_footnote_mode:
                fail(
                    "configured_footnote_mode_mismatch",
                    f"checkpoint uses footnotes.mode={report_footnote_mode}, "
                    f"current configuration requires {configured_footnote_mode}",
                )
        except ValueError as exc:
            fail("invalid_footnote_mode", str(exc))
    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "footnote_normalized"
    source_names = set(markdown_inventory(source_dir))
    target_names = set(markdown_inventory(target_dir))
    extra_files = sorted(target_names - source_names)
    if extra_files:
        fail(
            "normalized_output_extra_files",
            "footnote_normalized contains stale Markdown units not present in "
            f"ocr_markdown: {', '.join(extra_files[:20])}",
        )
    if report.get("source_sha256") != markdown_inventory(source_dir):
        fail(
            "source_markdown_changed",
            "ocr_markdown changed after footnote normalization",
        )
    if report.get("target_sha256") != markdown_inventory(target_dir):
        fail(
            "normalized_output_changed",
            "footnote_normalized changed after footnote normalization",
        )
    if report.get("candidate_report_sha256") != sha256_file(
        output_dir / "footnote_candidates.json"
    ):
        fail(
            "candidate_report_stale",
            "footnote_candidates.json changed after footnote-apply",
        )
    if report.get("decision_validation_sha256") != sha256_file(
        output_dir / "footnote_decision_validation.json"
    ):
        fail(
            "decision_validation_stale",
            "footnote_decision_validation.json changed after footnote-apply",
        )
    tree_progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    expected_tree_progress_hash = report.get("tree_progress_sha256")
    if expected_tree_progress_hash and (
        not tree_progress_path.is_file()
        or expected_tree_progress_hash != sha256_file(tree_progress_path)
    ):
        fail(
            "tree_progress_stale",
            "tree_progress.json changed after footnote normalization",
        )
    endnotes = report.get("endnotes")
    if (
        isinstance(endnotes, Mapping)
        and str(endnotes.get("scope_mode") or "").strip().lower()
        == "per_chapter"
    ):
        toc_tree_path = output_dir / "toc_tree.json"
        expected_toc_hash = report.get("toc_tree_sha256")
        if (
            not expected_toc_hash
            or not toc_tree_path.is_file()
            or expected_toc_hash != sha256_file(toc_tree_path)
        ):
            fail(
                "toc_tree_stale",
                "toc_tree.json is missing or changed after scoped endnote "
                "normalization",
            )

    current = not failures
    if current:
        detail = "current validated footnote normalization matches all inputs"
    else:
        detail = failures[0]["detail"]
        if any(
            item["code"] in {"candidate_report_stale", "toc_tree_stale"}
            for item in failures
        ):
            detail += "; rerun footnote-prepare, footnote-validate, and footnote-apply"
        elif any(item["code"] == "decision_validation_stale" for item in failures):
            detail += "; rerun footnote-validate and footnote-apply"
    return {
        "current": current,
        "status": "validated" if current else "stale",
        "failures": failures,
        "detail": detail,
    }


def footnote_normalization_is_current(
    output_dir: Path,
    config: Optional[Mapping[str, Any]] = None,
) -> bool:
    """Return whether derived chapter-end source still matches its inputs."""
    return bool(footnote_normalization_status(output_dir, config=config)["current"])


__all__ = [
    "FOOTNOTE_NORMALIZATION_SCHEMA_VERSION",
    "footnote_normalization_is_current",
    "footnote_normalization_status",
    "markdown_inventory",
    "unit_records",
]

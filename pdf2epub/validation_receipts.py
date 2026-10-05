"""Read-only freshness checks for validated Markdown stages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .workflow_contracts import MARKDOWN_VALIDATION_SCHEMA_VERSION


def markdown_directory_sha256(directory: Path) -> Dict[str, str]:
    """Return the exact SHA-256 inventory of Markdown files in *directory*."""
    directory = Path(directory)
    if not directory.is_dir():
        return {}
    return {
        path.name: hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(directory.glob("*.md"))
        if path.is_file()
    }


def _pages_fingerprint(pages_dir: Path) -> Optional[str]:
    """Hash the page Markdown inputs using the refinement checkpoint scheme."""
    pages_dir = Path(pages_dir)
    if not pages_dir.is_dir():
        return None
    digest = hashlib.sha256()
    pages = sorted(pages_dir.glob("page_*.md"))
    if not pages:
        return None
    for page in pages:
        try:
            digest.update(page.name.encode("utf-8"))
            digest.update(hashlib.sha256(page.read_bytes()).digest())
        except OSError:
            return None
    return digest.hexdigest()


def _file_sha256_or_none(path: Path) -> Optional[str]:
    try:
        return hashlib.sha256(Path(path).read_bytes()).hexdigest()
    except OSError:
        return None


def build_input_snapshot(
    output_dir: Path,
    config_path: Path,
    *,
    translated: bool,
) -> Dict[str, Any]:
    """Return hashes for every non-Markdown input consumed by EPUB packaging.

    Markdown receipts attest to the source and target directories.  EPUB
    packaging also consumes the TOC, refinement checkpoint, page fingerprint,
    translated TOC, and configuration.  Keeping those hashes in the receipt
    makes the package gate a read-only freshness check instead of a second,
    context-dependent validation run.
    """
    output_dir = Path(output_dir)
    files = {
        "toc_tree": _file_sha256_or_none(output_dir / "toc_tree.json"),
        "tree_progress": _file_sha256_or_none(
            output_dir / "ocr_markdown" / "tree_progress.json"
        ),
        "config": _file_sha256_or_none(Path(config_path)),
    }
    if translated:
        files["toc_tree_translated"] = _file_sha256_or_none(
            output_dir / "toc_tree_translated.json"
        )
        files["toc_translation_source"] = _file_sha256_or_none(
            output_dir / "toc_translation_source.json"
        )

    fingerprint: Dict[str, Any] = {
        "toc_sha256": None,
        "pages_sha256": None,
        "current_pages_sha256": _pages_fingerprint(output_dir / "pages"),
    }
    progress_path = output_dir / "ocr_markdown" / "tree_progress.json"
    try:
        progress = json.loads(progress_path.read_text(encoding="utf-8"))
        recorded = progress.get("fingerprint", {})
        if isinstance(recorded, Mapping):
            fingerprint["toc_sha256"] = recorded.get("toc_sha256")
            fingerprint["pages_sha256"] = recorded.get("pages_sha256")
    except (OSError, UnicodeError, json.JSONDecodeError, AttributeError):
        pass

    return {"files": files, "refinement_fingerprint": fingerprint}


def _build_inputs_are_complete(snapshot: Mapping[str, Any], *, translated: bool) -> bool:
    files = snapshot.get("files")
    fingerprint = snapshot.get("refinement_fingerprint")
    if not isinstance(files, Mapping) or not isinstance(fingerprint, Mapping):
        return False
    required = {"toc_tree", "tree_progress", "config"}
    if translated:
        required.update({"toc_tree_translated", "toc_translation_source"})
    if any(not isinstance(files.get(name), str) or not files[name] for name in required):
        return False
    toc_hash = files.get("toc_tree")
    pages_hash = fingerprint.get("current_pages_sha256")
    return (
        isinstance(toc_hash, str)
        and fingerprint.get("toc_sha256") == toc_hash
        and isinstance(pages_hash, str)
        and fingerprint.get("pages_sha256") == pages_hash
    )


def validation_receipt_is_current(
    report_path: Path,
    source_dir: Path,
    target_dir: Path,
    *,
    task: str,
    output_dir: Path | None = None,
    config_path: Path | None = None,
    require_build_inputs: bool = False,
) -> bool:
    """Check whether a full receipt still covers both build input directories."""
    try:
        report: Any = json.loads(Path(report_path).read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return False
    if not isinstance(report, dict):
        return False
    if (
        report.get("schema_version") != MARKDOWN_VALIDATION_SCHEMA_VERSION
        or report.get("task") != task
        or report.get("scope") != "full"
        or report.get("all_passed") is not True
    ):
        return False
    recorded_source = report.get("source_sha256")
    recorded_target = report.get("target_sha256")
    if not isinstance(recorded_source, dict) or not isinstance(recorded_target, dict):
        return False
    current = (
        recorded_source == markdown_directory_sha256(Path(source_dir))
        and recorded_target == markdown_directory_sha256(Path(target_dir))
    )
    if not current or not require_build_inputs:
        return current
    if output_dir is None or config_path is None:
        return False
    translated = task == "translate"
    expected_inputs = build_input_snapshot(
        output_dir,
        config_path,
        translated=translated,
    )
    recorded_inputs = report.get("build_inputs")
    return (
        isinstance(recorded_inputs, Mapping)
        and dict(recorded_inputs) == expected_inputs
        and _build_inputs_are_complete(expected_inputs, translated=translated)
    )


__all__ = [
    "build_input_snapshot",
    "markdown_directory_sha256",
    "validation_receipt_is_current",
]

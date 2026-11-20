"""Build receipts that tie an EPUB artifact to the checkpoints it packaged.

``status`` used to treat any ``*.epub`` in the output directory as a passed
package stage, even when the polish/translate receipt had changed since the
build.  A build receipt records the input hashes and the artifact hash; the
package stage then reports a stale artifact instead of a finished run.
"""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Mapping, Optional

from .workflow_contracts import atomic_write_json, sha256_file


BUILD_RECEIPT_SCHEMA_VERSION = 1
BUILD_RECEIPT_FILENAME = "epub_build_receipt.json"

PDF_SOURCE = "pdf-source"
PDF_TRANSLATED = "pdf-translated"
HTML_TRANSLATED = "html-translated"

_INPUT_FILES: dict[str, tuple[str, ...]] = {
    PDF_SOURCE: ("polish_validation.json", "toc_tree.json"),
    PDF_TRANSLATED: (
        "translate_validation.json",
        "polish_validation.json",
        "toc_tree.json",
    ),
    HTML_TRANSLATED: (
        "translate-html_validation.json",
        "translated_metadata.json",
        "toc_tree.json",
    ),
}


def receipt_path(output_dir: Path) -> Path:
    return Path(output_dir) / BUILD_RECEIPT_FILENAME


def current_build_inputs(output_dir: Path, kind: str) -> dict[str, Optional[str]]:
    """Hash the files a build of ``kind`` must correspond to."""
    output_dir = Path(output_dir)
    names = _INPUT_FILES.get(kind, ())
    return {
        name: (sha256_file(output_dir / name) if (output_dir / name).is_file() else None)
        for name in names
    }


def write_build_receipt(
    output_dir: Path,
    epub_path: Path,
    *,
    kind: str,
    inputs: Optional[Mapping[str, Optional[str]]] = None,
) -> dict[str, Any]:
    """Record the artifact hash and the input hashes for one build kind."""
    output_dir = Path(output_dir)
    epub_path = Path(epub_path)
    path = receipt_path(output_dir)
    try:
        existing = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        existing = {}
    builds = existing.get("builds") if isinstance(existing, dict) else None
    if not isinstance(builds, dict):
        builds = {}
    try:
        relative = epub_path.resolve().relative_to(output_dir.resolve()).as_posix()
    except ValueError:
        relative = epub_path.name
    builds[kind] = {
        "epub": relative,
        "sha256": sha256_file(epub_path),
        "size": epub_path.stat().st_size if epub_path.is_file() else None,
        "inputs": dict(inputs) if inputs is not None else current_build_inputs(output_dir, kind),
    }
    receipt = {
        "schema_version": BUILD_RECEIPT_SCHEMA_VERSION,
        "builds": builds,
    }
    atomic_write_json(path, receipt)
    return receipt


def receipt_status(output_dir: Path, kind: str) -> tuple[str, str]:
    """Return ``(state, detail)`` for one build kind.

    ``state`` is ``missing`` (legacy artifact), ``current`` or ``stale``.
    """
    output_dir = Path(output_dir)
    path = receipt_path(output_dir)
    if not path.is_file():
        return "missing", "no build receipt"
    try:
        receipt = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return "stale", "build receipt is unreadable"
    builds = receipt.get("builds") if isinstance(receipt, Mapping) else None
    if not isinstance(builds, Mapping) or not isinstance(builds.get(kind), Mapping):
        return "missing", f"no {kind} build receipt"
    entry = builds[kind]
    epub = output_dir / str(entry.get("epub") or "")
    if not epub.is_file():
        return "stale", f"{kind} build artifact is missing"
    if sha256_file(epub) != str(entry.get("sha256") or ""):
        return "stale", f"{kind} build artifact changed after the build"
    recorded = entry.get("inputs")
    if not isinstance(recorded, Mapping):
        return "stale", f"{kind} build receipt has no input hashes"
    current = current_build_inputs(output_dir, kind)
    if dict(recorded) != current:
        return "stale", f"{kind} build receipt does not match current checkpoints"
    return "current", f"{kind} build matches current checkpoints"


__all__ = [
    "BUILD_RECEIPT_FILENAME",
    "BUILD_RECEIPT_SCHEMA_VERSION",
    "HTML_TRANSLATED",
    "PDF_SOURCE",
    "PDF_TRANSLATED",
    "current_build_inputs",
    "receipt_path",
    "receipt_status",
    "write_build_receipt",
]

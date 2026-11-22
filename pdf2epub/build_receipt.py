"""Build receipts that tie an EPUB artifact to the checkpoints it packaged.

``status`` used to treat any ``*.epub`` in the output directory as a passed
package stage, even when the polish/translate receipt had changed since the
build.  A build receipt records the input hashes and the artifact hash; the
package stage then reports a stale artifact instead of a finished run.
"""
from __future__ import annotations

import json
import hashlib
from pathlib import Path
from typing import Any, Mapping, Optional

from .workflow_contracts import atomic_write_json, sha256_file


BUILD_RECEIPT_SCHEMA_VERSION = 1
BUILD_RECEIPT_FILENAME = "epub_build_receipt.json"

PDF_SOURCE = "pdf-source"
PDF_TRANSLATED = "pdf-translated"
HTML_TRANSLATED = "html-translated"
NOVEL_TRANSLATED = "novel-translated"

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
    NOVEL_TRANSLATED: ("translate-novel_validation.json", "translated_metadata.json"),
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
    metadata_plan: Optional[Mapping[str, Any]] = None,
    metadata_outcome: Optional[Mapping[str, Any]] = None,
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
    bound_inputs = dict(inputs) if inputs is not None else current_build_inputs(output_dir, kind)
    if metadata_plan is not None:
        bound_inputs["metadata_enrichment_effective"] = effective_metadata_digest(metadata_plan)
        cover = (metadata_plan.get("metadata") or {}).get("cover_path")
        cover_path = Path(cover) if cover else None
        bound_inputs["metadata_enrichment_cover"] = sha256_file(cover_path) if cover_path and cover_path.is_file() else None
        bound_inputs["metadata_enrichment_cover_path"] = str(cover_path.resolve()) if cover_path and cover_path.is_file() else None
    builds[kind] = {
        "epub": relative,
        "sha256": sha256_file(epub_path),
        "size": epub_path.stat().st_size if epub_path.is_file() else None,
        "inputs": bound_inputs,
        **({"metadata_enrichment_outcome": dict(metadata_outcome)} if metadata_outcome is not None else {}),
    }
    receipt = {
        "schema_version": BUILD_RECEIPT_SCHEMA_VERSION,
        "builds": builds,
    }
    atomic_write_json(path, receipt)
    return receipt


def receipt_status(output_dir: Path, kind: str, *, config: Optional[Mapping[str, Any]] = None, config_path: Optional[Path] = None) -> tuple[str, str]:
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
    if "metadata_enrichment_effective" in recorded and config is None:
        return "stale", f"{kind} receipt requires configuration to verify current metadata enrichment"
    # Enrichment state is optional for older receipts. New receipts include an
    # effective metadata hash plus evidence paths, whose mutation invalidates it.
    for key, value in recorded.items():
        if key == "metadata_enrichment_cover":
            cover_name = recorded.get("metadata_enrichment_cover_path")
            cover_path = Path(str(cover_name)) if cover_name else None
            current[key] = sha256_file(cover_path) if cover_path and cover_path.is_file() else None
        elif key == "metadata_enrichment_cover_path":
            current[key] = value
        elif key == "metadata_enrichment_effective":
            if config is None:
                current[key] = value
            else:
                try:
                    from .metadata_enrichment.service import resolve_enrichment
                    plan = resolve_enrichment(output_dir, dict(config), config_path=config_path)
                    meta_options = dict(config.get("metadata_enrichment") or {})
                    plan["mode"] = meta_options.get("mode", "auto")
                    plan["cover_policy"] = meta_options.get("cover_policy", "preserve")
                    current[key] = effective_metadata_digest(plan)
                except Exception:
                    current[key] = "required-or-current-metadata-unavailable"
    if dict(recorded) != current:
        return "stale", f"{kind} build receipt does not match current checkpoints"
    return "current", f"{kind} build matches current checkpoints"


def effective_metadata_digest(plan: Mapping[str, Any]) -> str:
    payload = json.dumps({"metadata": plan.get("metadata", {}),
                          "verified_fields": plan.get("verified_fields", []),
                          "mode": plan.get("mode"),
                          "cover_policy": plan.get("cover_policy")},
                         ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


__all__ = [
    "BUILD_RECEIPT_FILENAME",
    "BUILD_RECEIPT_SCHEMA_VERSION",
    "HTML_TRANSLATED",
    "NOVEL_TRANSLATED",
    "PDF_SOURCE",
    "PDF_TRANSLATED",
    "current_build_inputs",
    "receipt_path",
    "receipt_status",
    "write_build_receipt",
]

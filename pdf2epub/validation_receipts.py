"""Read-only freshness checks for validated Markdown stages."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from typing import Any, Dict, Mapping, Optional

from .workflow_contracts import markdown_validation_schema_version
from .utils.fingerprints import pages_fingerprint

# Compatibility alias for older diagnostic integrations.
_pages_fingerprint = pages_fingerprint


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
        dynamic_binding = _dynamic_terminology_snapshot_binding(
            output_dir, Path(config_path)
        )
        if dynamic_binding is not None:
            files["dynamic_terminology_snapshot"] = dynamic_binding
        # Optional controlled-unit artifacts participate only when present,
        # preserving legacy receipt behavior for projects that never enabled
        # the feature. Any changed prompt contract, mapping result, or apply
        # receipt invalidates the full translation receipt.
        special = {}
        for path in sorted(output_dir.glob("special_unit_*_*.json")):
            if not path.name.endswith(("_manifest.json", "_result.json", "_receipt.json")):
                continue
            special[path.name] = _file_sha256_or_none(path)
        if special:
            files["special_unit_artifacts"] = special
            contracts = {}
            try:
                from .special_unit_context import build_special_unit_context
                for path in sorted(output_dir.glob("special_unit_*_manifest.json")):
                    filename = path.name
                    try:
                        manifest = json.loads(path.read_text(encoding="utf-8"))
                        kind = manifest.get("kind")
                        filename = manifest.get("source_file")
                        language = (manifest.get("context") or {}).get("language") or {}
                        if kind not in {"bibliography", "index"} or not isinstance(filename, str):
                            continue
                        context = build_special_unit_context(
                            output_dir, filename, kind,
                            source_language=language.get("source", "English"),
                            target_language=language.get("target", "Chinese"),
                        )
                        contracts[filename] = context.get("contract_sha256")
                    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, TypeError):
                        contracts[filename] = None
            except ImportError:
                contracts = {name: None for name in special}
            files["special_unit_context_contracts"] = contracts

    fingerprint: Dict[str, Any] = {
        "toc_sha256": None,
        "pages_sha256": None,
        "current_pages_sha256": pages_fingerprint(output_dir / "pages"),
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


def _dynamic_terminology_snapshot_binding(output_dir: Path, config_path: Path) -> str | None:
    """Bind translated package receipts to the verified current memory epoch."""
    try:
        from .pipeline_policy import PipelinePolicy
        from .utils.configuration import load_config

        config = load_config(str(config_path))
        enabled = PipelinePolicy.from_config(config).dynamic_terminology_enabled
    except (OSError, UnicodeError, TypeError, ValueError):
        return "invalid"
    if not enabled:
        return None
    try:
        manifest = json.loads(
            (Path(output_dir) / "translate_subagent_manifest.json").read_text(encoding="utf-8")
        )
        dynamic = manifest.get("dynamic_terminology") if isinstance(manifest, dict) else None
        if not isinstance(dynamic, Mapping) or dynamic.get("schema_version") != 1 or dynamic.get("enabled") is not True:
            return "invalid"
        pair = (manifest.get("source_language"), manifest.get("target_language"))
        title = config.get("title")
        if not title or not all(isinstance(value, str) and value for value in pair):
            return "invalid"
        from .dynamic_terminology import load_current_snapshot

        digest, _entries = load_current_snapshot(
            Path(output_dir) / "dynamic_terminology",
            book_id=str(title), source_language=pair[0], target_language=pair[1],
        )
        return digest or "none"
    except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
        return "invalid"


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
    special_contracts = files.get("special_unit_context_contracts")
    special_contracts_complete = special_contracts is None or (
        isinstance(special_contracts, Mapping)
        and bool(special_contracts)
        and all(isinstance(value, str) and value for value in special_contracts.values())
    )
    dynamic_snapshot = files.get("dynamic_terminology_snapshot")
    dynamic_snapshot_complete = dynamic_snapshot is None or (
        dynamic_snapshot == "none"
        or (isinstance(dynamic_snapshot, str) and len(dynamic_snapshot) == 64 and all(c in "0123456789abcdef" for c in dynamic_snapshot))
    )
    return (
        special_contracts_complete
        and dynamic_snapshot_complete
        and isinstance(toc_hash, str)
        and fingerprint.get("toc_sha256") == toc_hash
        and isinstance(pages_hash, str)
        and fingerprint.get("pages_sha256") == pages_hash
    )


def _has_resume_context_evidence(value: Any) -> bool:
    if not isinstance(value, Mapping):
        return False
    return any(
        isinstance(value.get(key), Mapping) and bool(value[key])
        for key in (
            "unit_context_sha256", "boundary_context_sha256", "toc_heading_contexts",
            "special_unit_modes", "file_roles", "file_role_sources", "context_sha256",
        )
    )


def _polish_resume_contexts_are_current(
    output_dir: Path, source_files: list[str], recorded_contexts: Any
) -> bool:
    """Recheck prepared context evidence at every polish receipt read."""
    from .markdown_validation_persistence import capture_pdf_resume_contexts

    current, errors = capture_pdf_resume_contexts(output_dir, "polish", source_files)
    if errors:
        return False
    if not isinstance(recorded_contexts, Mapping):
        recorded_contexts = {}
    for name in source_files:
        actual = current.get(name)
        recorded = recorded_contexts.get(name)
        if not (_has_resume_context_evidence(actual) or _has_resume_context_evidence(recorded)):
            continue
        if not isinstance(actual, Mapping) or not isinstance(recorded, Mapping):
            return False
        try:
            if json.dumps(actual, ensure_ascii=False, sort_keys=True, separators=(",", ":")) != json.dumps(
                recorded, ensure_ascii=False, sort_keys=True, separators=(",", ":")
            ):
                return False
        except (TypeError, ValueError):
            return False
    return True


def validation_receipt_is_current(
    report_path: Path,
    source_dir: Path,
    target_dir: Path,
    *,
    task: str,
    output_dir: Path | None = None,
    config_path: Path | None = None,
    config: Mapping[str, Any] | None = None,
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
        report.get("schema_version") != markdown_validation_schema_version(task)
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
    if not current:
        return current
    if task == "polish":
        semantic_output_dir = Path(output_dir) if output_dir is not None else Path(report_path).parent
        try:
            from .refine.footnote_options import resolve_footnote_options
            from .utils.configuration import load_config

            current_config = dict(config) if isinstance(config, Mapping) else (
                load_config(str(config_path)) if config_path is not None else None
            )
            semantics_required = (
                resolve_footnote_options(current_config).mode != "disabled"
                if current_config is not None
                else True
            )
            if semantics_required:
                manifest = json.loads((semantic_output_dir / "polish_subagent_manifest.json").read_text(encoding="utf-8"))
                semantic = manifest.get("footnote_semantics") if isinstance(manifest, Mapping) else None
                reviews = report.get("polish_footnote_semantic_reviews")
                if not isinstance(semantic, Mapping) or semantic.get("enabled") is not True or not isinstance(reviews, Mapping):
                    return False
                from .polish_footnote_semantics import validate_prepared_file_review

                for source in sorted(Path(source_dir).glob("*.md")):
                    name = source.name
                    try:
                        source_text = source.read_bytes().decode("utf-8")
                        target_text = (Path(target_dir) / name).read_bytes().decode("utf-8")
                    except (OSError, UnicodeError):
                        return False
                    current_review = validate_prepared_file_review(
                        semantic_output_dir, name, source_text=source_text,
                        target_text=target_text, required=True,
                    )
                    recorded_review = reviews.get(name)
                    if (
                        not isinstance(current_review, Mapping)
                        or current_review.get("valid") is not True
                        or not isinstance(recorded_review, Mapping)
                        or recorded_review.get("sidecar_sha256") != current_review.get("sidecar_sha256")
                    ):
                        return False
            source_files = sorted(path.name for path in Path(source_dir).glob("*.md"))
            if not _polish_resume_contexts_are_current(
                semantic_output_dir, source_files, report.get("resume_contexts")
            ):
                return False
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            return False
    if task == "translate" and output_dir is not None:
        try:
            configured_dynamic = False
            configured = {}
            if config_path is not None:
                from .pipeline_policy import PipelinePolicy
                from .utils.configuration import load_config

                configured = load_config(str(config_path))
                configured_dynamic = PipelinePolicy.from_config(configured).dynamic_terminology_enabled
            manifest_path = Path(output_dir) / "translate_subagent_manifest.json"
            if not manifest_path.is_file():
                if configured_dynamic:
                    return False
                manifest = {}
            else:
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            dynamic = manifest.get("dynamic_terminology") if isinstance(manifest, dict) else None
            if configured_dynamic and (
                not isinstance(dynamic, Mapping)
                or dynamic.get("schema_version") != 1
                or dynamic.get("enabled") is not True
            ):
                return False
            if isinstance(dynamic, Mapping) and dynamic.get("enabled") is True:
                from .terminology_memory_validation import validate_dynamic_terminology_units
                from .utils.configuration import load_config

                config = load_config(str(config_path)) if config_path is not None else {}
                if not config.get("title"):
                    config["title"] = manifest.get("book_title") or manifest.get("book_id")
                config.setdefault("translation", {})
                config["translation"].update({
                    "source_language": manifest.get("source_language"),
                    "target_language": manifest.get("target_language"),
                    "dynamic_terminology": {"enabled": True},
                })
                memory_report = validate_dynamic_terminology_units(
                    Path(output_dir), task="translate", source_dir=Path(source_dir),
                    target_dir=Path(target_dir), config=config,
                )
                if memory_report.get("enabled") and not memory_report.get("valid"):
                    return False
        except (OSError, UnicodeError, json.JSONDecodeError, TypeError, ValueError, KeyError):
            return False
    if not require_build_inputs:
        return True
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

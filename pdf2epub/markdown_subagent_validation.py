"""Markdown Subagent output validation and staging."""

from __future__ import annotations

import hashlib
import json
import re
import shutil
from pathlib import Path
from typing import Any, Dict, Iterable, List, Mapping, Optional

from .footnote_normalization import validate_polish_footnote_normalization
from .markdown_validation import (
    _heading_reduction_is_duplicate_only,
    _special_role_numeric_markers,
    _validate_special_role_markers,
    detect_bilingual_output,
    detect_polish_page_furniture,
    fix_reference_heading_mismatch,
    strip_outer_markdown_fences,
    target_language_ratio_check,
    translation_diff_summary,
)
from .subagent_runtime import _markdown_files
from .subagent_safety import detect_refusal
from .utils.ocr_artifacts import clean_ocr_page_artifacts
from .workflow_contracts import (
    MARKDOWN_VALIDATION_SCHEMA_VERSION,
    atomic_write_text,
)


# Changing the warning gate is a validation-contract change.  Older reports
# were allowed to stage files while carrying advisory warnings, so callers
# must not treat a pre-v2 report as proof that the new gate was evaluated.
VALIDATION_SCHEMA_VERSION = MARKDOWN_VALIDATION_SCHEMA_VERSION


def _structural_mismatch_reason(
    pattern: str,
    source_text: str,
    target_text: str,
    source_count: int,
    target_count: int,
) -> str:
    """Explain a structural mismatch with useful source/target line numbers."""
    if pattern == r"^#{1,6}\s":
        heading_pattern = re.compile(r"^(#{1,6})\s", re.MULTILINE)

        def describe(text: str) -> str:
            headings = [
                (text.count("\n", 0, match.start()) + 1, len(match.group(1)))
                for match in heading_pattern.finditer(text)
            ]
            return ", ".join(f"L{line} (level {level})" for line, level in headings) or "none"

        return (
            "structural marker mismatch: Markdown heading structure mismatch: "
            f"source={source_count} [{describe(source_text)}]; "
            f"target={target_count} [{describe(target_text)}]"
        )
    return (
        f"structural marker mismatch: {pattern} "
        f"(source={source_count}, target={target_count})"
    )

def validate_markdown_subagent(
    output_dir: Path,
    task: str,
    source_dir: Path,
    target_dir: Path,
    structural_patterns: Iterable[str] = (),
    create_validated_copy: bool = True,
    file_roles: Optional[Mapping[str, str]] = None,
    tolerate_duplicate_headings: bool = False,
    validate_footnote_normalization: bool = False,
    fix_reference_headings: bool = False,
    selected_files: Optional[Iterable[str]] = None,
    target_language: Optional[str] = None,
    allow_review_warnings: bool = False,
) -> Dict:
    """Validate a Subagent markdown hand-off and optionally stage it."""
    source_dir = Path(source_dir)
    target_dir = Path(target_dir)
    previous_review_files: set[str] = set()
    previous_review_keys: set[tuple[str, str]] = set()
    previous_manifest_path = output_dir / f"{task}_subagent_manifest.json"
    if previous_manifest_path.is_file():
        try:
            loaded_manifest = json.loads(
                previous_manifest_path.read_text(encoding="utf-8")
            )
            if isinstance(loaded_manifest, dict):
                previous_review = loaded_manifest.get("previous_review_required", {})
                if isinstance(previous_review, Mapping):
                    for name, items in previous_review.items():
                        file_name = str(name)
                        if not file_name.strip():
                            continue
                        previous_review_files.add(file_name)
                        if isinstance(items, list):
                            for item in items:
                                if isinstance(item, Mapping) and item.get("kind"):
                                    previous_review_keys.add(
                                        (file_name, str(item["kind"]))
                                    )
        except (OSError, json.JSONDecodeError, UnicodeError):
            previous_review_files = set()
    all_sources = _markdown_files(source_dir)
    selected = None
    if selected_files is not None:
        selected = {str(name) for name in selected_files}
        available = {path.name for path in all_sources}
        unknown = sorted(selected - available)
        if unknown:
            raise ValueError(f"Unknown Markdown source file(s): {', '.join(unknown)}")
        sources = [path for path in all_sources if path.name in selected]
    else:
        sources = all_sources
    partial = selected is not None
    missing: List[str] = []
    invalid: List[Dict[str, str]] = []
    safety_blocked: List[str] = []
    valid_files: List[str] = []
    source_sha256: Dict[str, str] = {}
    target_sha256: Dict[str, str] = {}
    normalized_files: List[str] = []
    reference_heading_fixes: List[Dict[str, Any]] = []
    diff_summary: Dict[str, Dict[str, Any]] = {}
    structural_warnings: List[Dict[str, Any]] = []
    target_language_audits: Dict[str, Dict[str, Any]] = {}
    target_language_blocked: List[str] = []
    polish_page_furniture_warnings: List[Dict[str, Any]] = []
    review_required: List[Dict[str, Any]] = []
    normalized_roles = {
        str(name): str(role).strip().lower()
        for name, role in (file_roles or {}).items()
        if str(role).strip().lower() in {"bibliography", "index"}
    }
    bilingual_warnings: List[Dict[str, Any]] = []
    validated_dir = target_dir / "validated"
    # Never leave a previous successful hand-off usable after a later failed
    # validation.  The validated directory is a generated staging area.
    if validated_dir.exists() and not partial:
        shutil.rmtree(validated_dir)

    for source in sources:
        target = target_dir / source.name
        if not target.exists():
            missing.append(source.name)
            if partial:
                (validated_dir / source.name).unlink(missing_ok=True)
            continue
        try:
            source_bytes = source.read_bytes()
            source_sha256[source.name] = hashlib.sha256(source_bytes).hexdigest()
            source_text = source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            invalid.append(
                {
                    "file": source.name,
                    "reason": f"source UTF-8 decode error: {exc}",
                }
            )
            continue
        try:
            target_bytes = target.read_bytes()
            target_sha256[source.name] = hashlib.sha256(target_bytes).hexdigest()
            target_text = target_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            invalid.append(
                {
                    "file": source.name,
                    "reason": f"UTF-8 decode error (truncated file?): {exc}",
                }
            )
            continue
        target_text, stripped_fence = strip_outer_markdown_fences(target_text)
        if stripped_fence:
            atomic_write_text(target, target_text)
            normalized_files.append(source.name)
        if task == "translate" and fix_reference_headings:
            target_text, fixes = fix_reference_heading_mismatch(source_text, target_text)
            if fixes:
                atomic_write_text(target, target_text)
                normalized_files.append(source.name)
                reference_heading_fixes.extend(
                    {"file": source.name, **fix} for fix in fixes
                )
        diff_summary[source.name] = translation_diff_summary(source_text, target_text)
        if not target_text.strip():
            invalid.append({"file": source.name, "reason": "target is empty"})
            continue
        role = normalized_roles.get(source.name)
        if task == "translate" and role not in {"bibliography", "index"}:
            language_audit = target_language_ratio_check(
                source_text, target_text, target_language
            )
            target_language_audits[source.name] = language_audit
            if language_audit.get("blocked"):
                invalid.append(
                    {
                        "file": source.name,
                        "reason": str(language_audit["reason"]),
                    }
                )
                target_language_blocked.append(source.name)
        refusal = detect_refusal(source_text, target_text)
        if refusal:
            invalid.append(
                {"file": source.name, "reason": f"refusal/disclaimer detected: {refusal}"}
            )
            safety_blocked.append(source.name)
            continue
        warning = detect_bilingual_output(source_text, target_text)
        language_audit = target_language_audits.get(source.name, {})
        if (
            warning
            and task == "translate"
            and language_audit.get("applicable")
            and source.name not in target_language_blocked
        ):
            invalid.append(
                {
                    "file": source.name,
                    "reason": (
                        "untranslated_source_detected: long unchanged source "
                        f"span at lines {warning['start_line']}-{warning['end_line']}"
                    ),
                }
            )
            target_language_blocked.append(source.name)
        elif warning and source.name not in normalized_roles:
            warning["file"] = source.name
            bilingual_warnings.append(warning)
            if task == "translate":
                review_required.append(
                    {
                        "file": source.name,
                        "kind": "bilingual_output",
                        "reason": warning["reason"],
                        "start_line": warning["start_line"],
                        "end_line": warning["end_line"],
                    }
                )
        for pattern in structural_patterns:
            comparison_source = source_text
            if pattern == r"!\[[^\]]*\]\([^)]+\)":
                comparison_source = clean_ocr_page_artifacts(source_text)
            source_matches = list(re.finditer(pattern, comparison_source, flags=re.MULTILINE))
            target_matches = list(re.finditer(pattern, target_text, flags=re.MULTILINE))
            source_count = len(source_matches)
            target_count = len(target_matches)
            heading_levels_changed = (
                pattern == r"^#{1,6}\s"
                and [len(match.group(0).split()[0]) for match in source_matches]
                != [len(match.group(0).split()[0]) for match in target_matches]
            )
            mismatch_allowed = (
                pattern == r"^#{1,6}\s"
                and tolerate_duplicate_headings
                and _heading_reduction_is_duplicate_only(comparison_source, target_text)
            )
            if (source_count != target_count or heading_levels_changed) and not mismatch_allowed:
                invalid.append(
                    {
                        "file": source.name,
                        "reason": _structural_mismatch_reason(
                            pattern,
                            comparison_source,
                            target_text,
                            source_count,
                            target_count,
                        ),
                    }
                )
                break
            if (source_count != target_count or heading_levels_changed) and mismatch_allowed:
                structural_warnings.append(
                    {
                        "file": source.name,
                        "reason": "duplicate Markdown heading removed during polishing",
                        "source_count": source_count,
                        "target_count": target_count,
                    }
                )
        else:
            footnote_errors = (
                validate_polish_footnote_normalization(source_text, target_text)
                if validate_footnote_normalization
                else []
            )
            if footnote_errors:
                invalid.extend(
                    {"file": source.name, "reason": error}
                    for error in footnote_errors
                )
            else:
                valid_files.append(source.name)

        if role in {"bibliography", "index"}:
            role_errors = _validate_special_role_markers(
                source_text,
                target_text,
                role,
                allow_page_furniture_deletion=True,
            )
            for role_error in role_errors:
                invalid.append({"file": source.name, "reason": role_error})
            if role_errors and source.name in valid_files:
                valid_files.remove(source.name)

        if task == "polish" and role not in {"bibliography", "index"}:
            for finding in detect_polish_page_furniture(target_text):
                item = {"file": source.name, **finding}
                polish_page_furniture_warnings.append(item)
                review_required.append(
                    {
                        "file": source.name,
                        "kind": "polish_page_furniture",
                        "reason": (
                            "high-confidence page-furniture candidate remains after polish"
                        ),
                        **finding,
                    }
                )

        if source.name in target_language_blocked and source.name in valid_files:
            valid_files.remove(source.name)

        source_fence_count = source_text.count("```")
        target_fence_count = target_text.count("```")
        if source_fence_count != target_fence_count:
            invalid.append(
                {
                    "file": source.name,
                    "reason": (
                        "Markdown code fence mismatch: "
                        f"expected {source_fence_count}, got {target_fence_count}"
                    ),
                }
            )
            if source.name in valid_files:
                valid_files.remove(source.name)

    extras = [] if partial else sorted(
        path.name for path in _markdown_files(target_dir)
        if path.name not in {p.name for p in sources}
    )
    if extras:
        invalid.extend(
            {"file": name, "reason": "unexpected extra target file"}
            for name in extras
        )
    review_required_files = sorted(
        {str(item["file"]) for item in review_required if item.get("file")}
    )
    human_review_required = [
        {
            **item,
            "action": "ask_human",
            "reason": (
                "the same review signal persisted after a Subagent retry; "
                "automatic retries are paused"
            ),
        }
        for item in review_required
        if (
            str(item.get("file") or "") in previous_review_files
            and (
                not previous_review_keys
                or (
                    str(item.get("file") or ""),
                    str(item.get("kind") or ""),
                )
                in previous_review_keys
            )
        )
    ]
    human_review_keys = {
        (str(item.get("file") or ""), str(item.get("kind") or ""))
        for item in human_review_required
    }
    retry_required = [
        {
            "file": name,
            "kind": "missing_output",
            "action": "retry_subagent",
            "reason": "Subagent did not write the assigned target file",
        }
        for name in missing
    ]
    retry_required.extend(
        {
            **item,
            "kind": "invalid_output",
            "action": "retry_subagent",
        }
        for item in invalid
    )
    retry_required.extend(
        {
            **item,
            "action": "retry_subagent",
        }
        for item in review_required
        if (
            str(item.get("file") or ""), str(item.get("kind") or "")
        ) not in human_review_keys
    )
    retry_required_files = sorted(
        {str(item["file"]) for item in retry_required if item.get("file")}
    )
    human_review_required_files = sorted(
        {
            str(item["file"])
            for item in human_review_required
            if item.get("file")
        }
    )
    if not allow_review_warnings:
        for name in review_required_files:
            if name in valid_files:
                valid_files.remove(name)
    if partial:
        invalid_names = {item["file"] for item in invalid if item.get("file")}
        if not allow_review_warnings:
            invalid_names.update(review_required_files)
        for name in invalid_names:
            (validated_dir / name).unlink(missing_ok=True)
    if create_validated_copy and not missing and not invalid and (
        allow_review_warnings or not review_required
    ):
        validated_dir.mkdir(parents=True, exist_ok=True)
        for source in sources:
            shutil.copy2(target_dir / source.name, validated_dir / source.name)

    report = {
        "schema_version": VALIDATION_SCHEMA_VERSION,
        "task": task,
        "total": len(sources),
        "completed": len(sources) - len(missing),
        "missing": missing,
        "invalid": invalid,
        "safety_blocked": safety_blocked,
        "target_language_blocked": target_language_blocked,
        "target_language_audits": target_language_audits,
        "bilingual_warnings": bilingual_warnings,
        "polish_page_furniture_warnings": polish_page_furniture_warnings,
        "review_required": review_required,
        "review_required_files": review_required_files,
        "retry_required": retry_required,
        "retry_required_files": retry_required_files,
        "human_review_required": human_review_required,
        "human_review_required_files": human_review_required_files,
        "previous_review_files": sorted(previous_review_files),
        "allow_review_warnings": bool(allow_review_warnings),
        "review_warnings_acknowledged": bool(
            allow_review_warnings and review_required
        ),
        "normalized_files": normalized_files,
        "reference_heading_fixes": reference_heading_fixes,
        "structural_warnings": structural_warnings,
        "diff_summary": diff_summary,
        "file_roles": normalized_roles,
        "extra": extras,
        "valid_files": valid_files,
        "source_sha256": source_sha256,
        "target_sha256": target_sha256,
        "validated_dir": str(validated_dir),
        "scope": "files" if partial else "full",
        "files_checked": [source.name for source in sources],
        "all_passed": (
            bool(sources)
            and not missing
            and not invalid
            and not extras
            and (allow_review_warnings or not review_required)
        ),
    }
    report_path = (
        output_dir / f"{task}_file_validation.json"
        if partial
        else output_dir / f"{task}_validation.json"
    )
    if partial:
        existing: Dict[str, Any] = {}
        if report_path.is_file():
            try:
                loaded = json.loads(report_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    existing = loaded
            except (OSError, json.JSONDecodeError):
                existing = {}
        records = existing.get("files", {})
        if not isinstance(records, dict):
            records = {}
        for name in report["files_checked"]:
            records[name] = {
                "schema_version": VALIDATION_SCHEMA_VERSION,
                "valid": name in report["valid_files"] and not report["missing"],
                "source_sha256": report["source_sha256"].get(name),
                "target_sha256": report["target_sha256"].get(name),
                "invalid": [item for item in report["invalid"] if item["file"] == name],
                "safety_blocked": name in report["safety_blocked"],
                "target_language_blocked": name in report["target_language_blocked"],
                "target_language_audit": report["target_language_audits"].get(name),
                "review_required": [
                    item
                    for item in report["review_required"]
                    if item.get("file") == name
                ],
                "retry_required": [
                    item
                    for item in report["retry_required"]
                    if item.get("file") == name
                ],
                "human_review_required": [
                    item
                    for item in report["human_review_required"]
                    if item.get("file") == name
                ],
                "review_warnings_acknowledged": bool(
                    report.get("allow_review_warnings")
                    and any(
                        item.get("file") == name
                        for item in report["review_required"]
                    )
                ),
            }
        ledger = {
            "schema_version": VALIDATION_SCHEMA_VERSION,
            "task": task,
            "scope": "file-checkpoints",
            "files": records,
        }
        atomic_write_text(report_path, json.dumps(ledger, ensure_ascii=False, indent=2))
    else:
        atomic_write_text(report_path, json.dumps(report, ensure_ascii=False, indent=2))
    return report

__all__ = ["validate_markdown_subagent"]

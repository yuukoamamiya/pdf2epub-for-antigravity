"""Pure single-file Markdown content audits."""
from __future__ import annotations
import hashlib, re
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List
from .document_roles import DENSITY_EXEMPT_ROLES
from .footnote_normalization import validate_polish_footnote_normalization
from .markdown_validation import (heading_reduction_is_duplicate_only, validate_footnote_markers, detect_bilingual_output, detect_polish_page_furniture, fix_reference_heading_mismatch, is_bibliography_text, is_front_matter_text, polish_content_integrity_check, polish_structure_audit, strip_outer_markdown_fences, target_language_ratio_check, translation_diff_summary)
from .special_role_audit import validate_special_role_markers
from .subagent_safety import detect_refusal
from .utils.ocr_artifacts import clean_ocr_page_artifacts
from .markdown_validation_context import MarkdownAuditOptions, MarkdownValidationContext

def _structural_mismatch_reason(pattern: str, source_text: str, target_text: str, source_count: int, target_count: int) -> str:
    if pattern == r"^#{1,6}\s":
        hp = re.compile(r"^(#{1,6})\s", re.MULTILINE)
        def describe(text: str) -> str:
            headings = [(text.count("\n", 0, m.start()) + 1, len(m.group(1))) for m in hp.finditer(text)]
            return ", ".join(f"L{line} (level {level})" for line, level in headings) or "none"
        return ("structural marker mismatch: Markdown heading structure mismatch: "
                f"source={source_count} [{describe(source_text)}]; target={target_count} [{describe(target_text)}]")
    return f"structural marker mismatch: {pattern} (source={source_count}, target={target_count})"


def audit_toc_label_preservation(
    name: str,
    source_text: str,
    target_text: str,
    protected_titles: tuple[str, ...],
    continuation_titles: frozenset[str],
) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    """Return review signals and exemptions for protected polish TOC labels."""
    def normalize(value: str) -> str:
        value = re.sub(r"^\s*#{1,6}\s+", "", value.strip())
        value = re.sub(r"[*_`]+", "", value).strip()
        return re.sub(r"\s+", " ", value).casefold()

    source_labels = [normalize(line) for line in source_text.splitlines() if normalize(line)]
    target_labels = [normalize(line) for line in target_text.splitlines() if normalize(line)]
    source_prefix = {normalize(line) for line in source_text.splitlines()[:5] if normalize(line)}
    continuation = {normalize(value) for value in continuation_titles}
    review, exemptions = [], []
    for title in protected_titles:
        label = normalize(title)
        if source_labels.count(label) != 1 or target_labels.count(label):
            continue
        if label in continuation and label in source_prefix:
            exemptions.append({
                "file": name,
                "kind": "polish_continuation_toc_label_removed",
                "reason": "a top-level ancestor label was removed from the opening of a continuation part",
                "title": title,
            })
            continue
        review.append({
            "file": name,
            "kind": "polish_unique_toc_label_removed",
            "reason": "a unique source TOC label disappeared during polish; confirm that it was not a running header",
            "title": title,
        })
    return review, exemptions


def audit_structural_markers(
    name: str,
    source_text: str,
    target_text: str,
    patterns: tuple[str, ...],
    tolerate_duplicate_headings: bool,
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], bool]:
    """Compare requested Markdown structure markers for one source/target pair."""
    errors, warnings = [], []
    for pattern in patterns:
        comparison_source = (
            clean_ocr_page_artifacts(source_text)
            if pattern == r"!\[[^\]]*\]\([^)]+\)"
            else source_text
        )
        source_matches = list(re.finditer(pattern, comparison_source, flags=re.MULTILINE))
        target_matches = list(re.finditer(pattern, target_text, flags=re.MULTILINE))
        source_count, target_count = len(source_matches), len(target_matches)
        level_changed = (
            pattern == r"^#{1,6}\s"
            and [len(m.group(0).split()[0]) for m in source_matches]
            != [len(m.group(0).split()[0]) for m in target_matches]
        )
        mismatch = source_count != target_count or level_changed
        allowed = (
            mismatch and pattern == r"^#{1,6}\s" and tolerate_duplicate_headings
            and heading_reduction_is_duplicate_only(comparison_source, target_text)
        )
        if mismatch and not allowed:
            errors.append({
                "file": name,
                "reason": _structural_mismatch_reason(
                    pattern, comparison_source, target_text, source_count, target_count
                ),
            })
            return errors, warnings, False
        if allowed:
            warnings.append({
                "file": name,
                "reason": "duplicate Markdown heading removed during polishing",
                "source_count": source_count,
                "target_count": target_count,
            })
    return errors, warnings, True


def audit_target_language(
    source_text: str, target_text: str, role: str | None, target_language: str | None
) -> dict[str, Any] | None:
    """Run the target-density gate with the metadata/front-matter thresholds."""
    if role in DENSITY_EXEMPT_ROLES:
        return None
    kwargs = {}
    if role in {"metadata", "frontmatter", "copyright", "cover"} or is_front_matter_text(source_text):
        kwargs = {"min_source_letters": 50, "min_target_letters": 20}
    return target_language_ratio_check(source_text, target_text, target_language, **kwargs)


def audit_role_markers(
    task: str, role: str | None, source_text: str, target_text: str
) -> list[str]:
    """Check role-specific identifiers and translation footnote markers."""
    errors: list[str] = []
    if role in {"bibliography", "index", "notes", "author_notes", "catalog", "ad"}:
        errors.extend(validate_special_role_markers(
            source_text, target_text, role, allow_page_furniture_deletion=task != "translate"
        ))
    if task == "translate":
        errors.extend(validate_footnote_markers(source_text, target_text))
    return errors


@dataclass(frozen=True)
class PolishContentAudit:
    integrity: dict[str, Any]
    page_furniture: tuple[dict[str, Any], ...]
    structure: dict[str, Any]
    review_signals: tuple[dict[str, Any], ...]


def audit_polish_content(
    name: str,
    source_text: str,
    target_text: str,
    role: str | None,
    protected_titles: tuple[str, ...],
) -> PolishContentAudit:
    """Audit polish content integrity, page furniture, and paragraph fidelity."""
    integrity = polish_content_integrity_check(source_text, target_text, file_role=role)
    furniture = tuple(
        {"file": name, **finding}
        for finding in detect_polish_page_furniture(
            target_text, role=role, protected_toc_titles=protected_titles
        )
    )
    signals = [
        {
            "file": name,
            "kind": "polish_page_furniture",
            "reason": "high-confidence page-furniture candidate remains after polish",
            **{key: value for key, value in finding.items() if key != "file"},
        }
        for finding in furniture
    ]
    structure = polish_structure_audit(source_text, target_text, role=role)
    if structure.get("status") == "review":
        signals.append({
            "file": name,
            "kind": "polish_paragraph_fragmentation",
            "reason": str(structure.get(
                "reason", "polish output may contain visual-line paragraph fragmentation"
            )),
            "audit": structure,
        })
    return PolishContentAudit(integrity, furniture, structure, tuple(signals))

@dataclass(frozen=True)
class MarkdownFileInput:
    name: str
    source_bytes: bytes | None
    target_bytes: bytes | None
    source_error: str | None = None
    target_error: str | None = None


@dataclass
class MarkdownAuditBatch:
    missing: List[str]
    invalid: List[Dict[str, str]]
    safety_blocked: List[str]
    valid_files: List[str]
    source_sha256: Dict[str, str]
    target_sha256: Dict[str, str]
    normalized_files: List[str]
    normalized_targets: Dict[str, str]
    reference_heading_fixes: List[Dict[str, Any]]
    diff_summary: Dict[str, Dict[str, Any]]
    structural_warnings: List[Dict[str, Any]]
    target_language_audits: Dict[str, Dict[str, Any]]
    target_language_blocked: List[str]
    polish_page_furniture_warnings: List[Dict[str, Any]]
    polish_toc_label_exemptions: List[Dict[str, Any]]
    polish_content_integrity: Dict[str, Dict[str, Any]]
    polish_structure_audits: Dict[str, Dict[str, Any]]
    review_required: List[Dict[str, Any]]
    bilingual_warnings: List[Dict[str, Any]]
    file_roles: Dict[str, str]


@dataclass
class MarkdownFileAuditResult:
    missing: List[str]
    invalid: List[Dict[str, str]]
    safety_blocked: List[str]
    valid_files: List[str]
    source_sha256: Dict[str, str]
    target_sha256: Dict[str, str]
    normalized_files: List[str]
    normalized_targets: Dict[str, str]
    reference_heading_fixes: List[Dict[str, Any]]
    diff_summary: Dict[str, Dict[str, Any]]
    structural_warnings: List[Dict[str, Any]]
    target_language_audits: Dict[str, Dict[str, Any]]
    target_language_blocked: List[str]
    polish_page_furniture_warnings: List[Dict[str, Any]]
    polish_toc_label_exemptions: List[Dict[str, Any]]
    polish_content_integrity: Dict[str, Dict[str, Any]]
    polish_structure_audits: Dict[str, Dict[str, Any]]
    review_required: List[Dict[str, Any]]
    bilingual_warnings: List[Dict[str, Any]]
    file_roles: Dict[str, str]


def load_file_input(source_path: Path, target_path: Path) -> MarkdownFileInput:
    """Load file bytes and capture decode/read errors before content rules run."""
    try:
        source_bytes = source_path.read_bytes()
        source_error = None
    except OSError as exc:
        source_bytes, source_error = None, str(exc)
    try:
        target_bytes = target_path.read_bytes() if target_path.exists() else None
        target_error = None
    except OSError as exc:
        target_bytes, target_error = None, str(exc)
    return MarkdownFileInput(source_path.name, source_bytes, target_bytes, source_error, target_error)


def audit_markdown_file(
    file: MarkdownFileInput, options: MarkdownAuditOptions
) -> MarkdownFileAuditResult:
    """Purely audit one byte snapshot; never reads or writes a filesystem path."""
    result_missing = []
    result_invalid = []
    result_safety_blocked = []
    result_valid_files = []
    result_source_sha256 = {}
    result_target_sha256 = {}
    result_normalized_files = []
    result_normalized_targets = {}
    result_reference_heading_fixes = []
    result_diff_summary = {}
    result_structural_warnings = []
    result_target_language_audits = {}
    result_target_language_blocked = []
    result_polish_page_furniture_warnings = []
    result_polish_toc_label_exemptions = []
    result_polish_content_integrity = {}
    result_polish_structure_audits = {}
    result_review_required = []
    result_bilingual_warnings = []
    result_file_roles = dict(options.file_roles)
    task = options.task
    structural_patterns = options.structural_patterns
    normalized_roles = result_file_roles
    normalized_protected_toc_titles = options.protected_toc_titles
    normalized_continuation_toc_titles = options.continuation_toc_titles
    expected_endnote_keys = options.expected_endnote_keys
    tolerate_duplicate_headings = options.tolerate_duplicate_headings
    validate_footnote_normalization = options.validate_footnote_normalization
    fix_reference_headings = options.fix_reference_headings
    target_language = options.target_language
    missing, invalid, safety_blocked, valid_files = result_missing, result_invalid, result_safety_blocked, result_valid_files
    source_sha256, target_sha256 = result_source_sha256, result_target_sha256
    normalized_files, normalized_targets = result_normalized_files, result_normalized_targets
    reference_heading_fixes, diff_summary = result_reference_heading_fixes, result_diff_summary
    structural_warnings = result_structural_warnings
    target_language_audits, target_language_blocked = result_target_language_audits, result_target_language_blocked
    polish_page_furniture_warnings = result_polish_page_furniture_warnings
    polish_toc_label_exemptions = result_polish_toc_label_exemptions
    polish_content_integrity, polish_structure_audits = result_polish_content_integrity, result_polish_structure_audits
    review_required, bilingual_warnings = result_review_required, result_bilingual_warnings
    if file.target_bytes is None:
        result_missing.append(file.name)
        if file.target_error:
            result_invalid.append({"file": file.name, "reason": f"target read error: {file.target_error}"})
    elif file.source_bytes is None:
        result_invalid.append({"file": file.name, "reason": f"source read error: {file.source_error}"})
    else:
        result_source_sha256[file.name] = hashlib.sha256(file.source_bytes).hexdigest()
        try:
            source_text = file.source_bytes.decode("utf-8")
        except UnicodeDecodeError as exc:
            result_invalid.append({"file": file.name, "reason": f"source UTF-8 decode error: {exc}"})
            source_text = None
        if file.target_error:
            result_invalid.append({"file": file.name, "reason": f"target read error: {file.target_error}"})
        if file.target_bytes is not None:
            result_target_sha256[file.name] = hashlib.sha256(file.target_bytes).hexdigest()
        try:
            target_text = file.target_bytes.decode("utf-8") if file.target_bytes is not None else None
        except UnicodeDecodeError as exc:
            result_invalid.append({"file": file.name, "reason": f"UTF-8 decode error (truncated file?): {exc}"})
            target_text = None
        if source_text is not None and target_text is not None:
            for _single_file in (0,):
                invalid_role = options.invalid_file_roles.get(file.name)
                if invalid_role:
                    invalid.append({
                        "file": file.name,
                        "reason": f"unknown Markdown file role: {invalid_role}",
                    })
                target_text, stripped_fence = strip_outer_markdown_fences(target_text)
                if stripped_fence:
                    normalized_targets[file.name] = target_text
                    normalized_files.append(file.name)
                if task == "polish" and normalized_protected_toc_titles:
                    toc_review, toc_exemptions = audit_toc_label_preservation(
                        file.name,
                        source_text,
                        target_text,
                        tuple(normalized_protected_toc_titles),
                        normalized_continuation_toc_titles.get(file.name, frozenset()),
                    )
                    review_required.extend(toc_review)
                    polish_toc_label_exemptions.extend(toc_exemptions)
                if task == "translate" and fix_reference_headings:
                    target_text, fixes = fix_reference_heading_mismatch(source_text, target_text)
                    if fixes:
                        normalized_targets[file.name] = target_text
                        normalized_files.append(file.name)
                        reference_heading_fixes.extend(
                            {"file": file.name, **fix} for fix in fixes
                        )
                if task == "translate":
                    escaped_number_re = re.compile(r"^(\d+)\\\.\s", re.MULTILINE)
                    source_numbered_prose = escaped_number_re.findall(source_text)
                    target_numbered_prose = escaped_number_re.findall(target_text)
                    if source_numbered_prose != target_numbered_prose:
                        invalid.append(
                            {
                                "file": file.name,
                                "reason": (
                                    "escaped numbered-prose markers changed or were lost: "
                                    f"expected {source_numbered_prose}, found {target_numbered_prose}"
                                ),
                            }
                        )
                diff_summary[file.name] = translation_diff_summary(source_text, target_text)
                if not target_text.strip() and bool(source_text.strip()):
                    invalid.append({"file": file.name, "reason": "target is empty"})
                    continue
                role = normalized_roles.get(file.name)
                if not role and is_bibliography_text(source_text):
                    role = "bibliography"
                    normalized_roles[file.name] = role
                language_audit = (
                    audit_target_language(source_text, target_text, role, target_language)
                    if task == "translate" else None
                )
                if language_audit is not None:
                    target_language_audits[file.name] = language_audit
                    if language_audit.get("blocked"):
                        invalid.append({"file": file.name, "reason": str(language_audit["reason"])})
                        target_language_blocked.append(file.name)
                refusal = detect_refusal(source_text, target_text)
                if refusal:
                    invalid.append(
                        {"file": file.name, "reason": f"refusal/disclaimer detected: {refusal}"}
                    )
                    safety_blocked.append(file.name)
                    continue
                warning = (
                    detect_bilingual_output(source_text, target_text)
                    if task == "translate"
                    else None
                )
                language_audit = target_language_audits.get(file.name, {})
                if (
                    warning
                    and task == "translate"
                    and language_audit.get("applicable")
                    and file.name not in target_language_blocked
                ):
                    invalid.append(
                        {
                            "file": file.name,
                            "reason": (
                                "untranslated_source_detected: long unchanged source "
                                f"span at lines {warning['start_line']}-{warning['end_line']}"
                            ),
                        }
                    )
                    target_language_blocked.append(file.name)
                elif warning and file.name not in normalized_roles:
                    warning["file"] = file.name
                    bilingual_warnings.append(warning)
                    if task == "translate":
                        review_required.append(
                            {
                                "file": file.name,
                                "kind": "bilingual_output",
                                "reason": warning["reason"],
                                "start_line": warning["start_line"],
                                "end_line": warning["end_line"],
                            }
                        )
                structure_errors, structure_warnings, structure_valid = audit_structural_markers(
                    file.name, source_text, target_text, tuple(structural_patterns),
                    tolerate_duplicate_headings,
                )
                invalid.extend(structure_errors)
                structural_warnings.extend(structure_warnings)
                if structure_valid:
                    footnote_errors = (
                        validate_polish_footnote_normalization(
                            source_text,
                            target_text,
                            file_role=role,
                            expected_endnote_keys=expected_endnote_keys,
                        )
                        if validate_footnote_normalization
                        else []
                    )
                    if footnote_errors:
                        invalid.extend(
                            {"file": file.name, "reason": error}
                            for error in footnote_errors
                        )
                    else:
                        valid_files.append(file.name)

                role_errors = audit_role_markers(task, role, source_text, target_text)
                invalid.extend({"file": file.name, "reason": error} for error in role_errors)
                if role_errors and file.name in valid_files:
                    valid_files.remove(file.name)

                if task == "polish":
                    polish = audit_polish_content(
                        file.name, source_text, target_text, role,
                        tuple(normalized_protected_toc_titles),
                    )
                    polish_content_integrity[file.name] = polish.integrity
                    polish_structure_audits[file.name] = polish.structure
                    polish_page_furniture_warnings.extend(polish.page_furniture)
                    review_required.extend(polish.review_signals)
                    if not polish.integrity["valid"]:
                        invalid.extend(
                            {"file": file.name, "reason": reason}
                            for reason in polish.integrity["errors"]
                        )
                        if file.name in valid_files:
                            valid_files.remove(file.name)

                if file.name in target_language_blocked and file.name in valid_files:
                    valid_files.remove(file.name)

                source_fence_count = source_text.count("```")
                target_fence_count = target_text.count("```")
                if source_fence_count != target_fence_count:
                    invalid.append(
                        {
                            "file": file.name,
                            "reason": (
                                "Markdown code fence mismatch: "
                                f"expected {source_fence_count}, got {target_fence_count}"
                            ),
                        }
                    )
                    if file.name in valid_files:
                        valid_files.remove(file.name)

                # Normalization and high-confidence heading repair may have rewritten
                # the target after its initial read.  Receipts must attest to the
                # bytes that will actually be copied into validated/ and packaged.
    if any(item.get("file") == file.name for item in result_invalid):
        if file.name in result_valid_files:
            result_valid_files.remove(file.name)
    return MarkdownFileAuditResult(
        missing=result_missing,
        invalid=result_invalid,
        safety_blocked=result_safety_blocked,
        valid_files=result_valid_files,
        source_sha256=result_source_sha256,
        target_sha256=result_target_sha256,
        normalized_files=result_normalized_files,
        normalized_targets=result_normalized_targets,
        reference_heading_fixes=result_reference_heading_fixes,
        diff_summary=result_diff_summary,
        structural_warnings=result_structural_warnings,
        target_language_audits=result_target_language_audits,
        target_language_blocked=result_target_language_blocked,
        polish_page_furniture_warnings=result_polish_page_furniture_warnings,
        polish_toc_label_exemptions=result_polish_toc_label_exemptions,
        polish_content_integrity=result_polish_content_integrity,
        polish_structure_audits=result_polish_structure_audits,
        review_required=result_review_required,
        bilingual_warnings=result_bilingual_warnings,
        file_roles=result_file_roles,
    )


def audit_markdown_files(context: MarkdownValidationContext) -> MarkdownAuditBatch:
    """Load and aggregate assigned files; each content audit receives one snapshot."""
    aggregate = {name: ({} if name in {"source_sha256", "target_sha256", "diff_summary", "target_language_audits", "polish_content_integrity", "polish_structure_audits", "normalized_targets", "file_roles"} else [])
                 for name in (['missing', 'invalid', 'safety_blocked', 'valid_files', 'source_sha256', 'target_sha256', 'normalized_files', 'normalized_targets', 'reference_heading_fixes', 'diff_summary', 'structural_warnings', 'target_language_audits', 'target_language_blocked', 'polish_page_furniture_warnings', 'polish_toc_label_exemptions', 'polish_content_integrity', 'polish_structure_audits', 'review_required', 'bilingual_warnings', 'file_roles'])}
    for source in context.sources:
        file = load_file_input(source, context.paths.target_dir / source.name)
        result = audit_markdown_file(file, context.options)
        for name, value in vars(result).items():
            if isinstance(value, dict):
                aggregate[name].update(value)
            else:
                aggregate[name].extend(value)
    return MarkdownAuditBatch(**aggregate)

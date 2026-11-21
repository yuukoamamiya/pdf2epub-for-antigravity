"""Checkpoint loading and per-unit reuse decisions for Markdown hand-offs."""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping, Optional

from .batching_service import effective_max_concurrency, recommended_batches
from .markdown_handoff_context import ContextProjection, MarkdownHandoffRequest
from .markdown_handoff_planning import MarkdownHandoffPlan
from .workflow_contracts import (
    atomic_write_json,
    is_reusable_checkpoint,
    markdown_validation_schema_version,
    sha256_file,
)


def _html_resume_context_is_current(
    name: str,
    snapshot: Mapping[str, Any],
    *,
    context: ContextProjection,
    request: MarkdownHandoffRequest,
) -> bool:
    """Compare a validation-time HTML context projection with the current one."""
    if (
        snapshot.get("source_language") != request.source_language
        or snapshot.get("target_language") != request.target_language
    ):
        return False
    return unit_context_is_current(
        name,
        previous_manifest=snapshot,
        controlled_files={},
        context_sha256=context.context_sha256,
        unit_context_sha256=context.unit_context_sha256,
        heading_contexts=context.heading_contexts,
        file_roles=context.file_roles,
        file_role_sources=context.file_role_sources,
        resume=True,
    )


def _html_checkpoint_fingerprint(name: str, validation: Mapping[str, Any]) -> Optional[str]:
    """Fingerprint an HTML receipt so legacy context invalidation survives reruns."""
    source_hashes = validation.get("source_sha256", {})
    target_hashes = validation.get("target_sha256", {})
    if not isinstance(source_hashes, Mapping) or not isinstance(target_hashes, Mapping):
        return None
    source_hash = source_hashes.get(name)
    target_hash = target_hashes.get(name)
    if not all(
        isinstance(digest, str) and re.fullmatch(r"(?i)[0-9a-f]{64}", digest)
        for digest in (source_hash, target_hash)
    ):
        return None
    contexts = validation.get("resume_contexts", {})
    resume_context = contexts.get(name) if isinstance(contexts, Mapping) else None
    value = json.dumps(
        {"source_sha256": source_hash, "target_sha256": target_hash, "context": resume_context},
        ensure_ascii=False,
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(value.encode("utf-8")).hexdigest()


def load_previous_manifest(path: Path, *, enabled: bool) -> dict[str, Any]:
    if not enabled or not path.is_file():
        return {}
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def unit_context_mismatch_reason(
    name: str,
    *,
    previous_manifest: Mapping[str, Any],
    controlled_files: Mapping[str, str],
    context_sha256: Mapping[str, str],
    unit_context_sha256: Mapping[str, str],
    heading_contexts: Mapping[str, Mapping[str, Any]],
    resume: bool,
    file_roles: Mapping[str, str] | None = None,
    file_role_sources: Mapping[str, Mapping[str, Any]] | None = None,
    boundary_context_sha256: Mapping[str, str] | None = None,
    ignore_unit_context: bool = False,
) -> str | None:
    """Return which projection component changed, or ``None`` when current.

    The component name feeds the operator-facing ``pending_reasons`` report so
    a first ``--resume`` can distinguish "the source changed" from "the
    projection was upgraded" without reading hashes by hand.  Decision logic is
    unchanged: ``unit_context_is_current()`` is exactly ``reason is None``.
    """
    previous_special_modes = previous_manifest.get("special_unit_modes", {})
    if not isinstance(previous_special_modes, Mapping):
        previous_special_modes = {}
    if previous_special_modes.get(name) != controlled_files.get(name):
        return "special_unit_mode"
    previous_roles = previous_manifest.get("file_roles", {})
    previous_sources = previous_manifest.get("file_role_sources", {})
    if not isinstance(previous_roles, Mapping):
        previous_roles = {}
    if not isinstance(previous_sources, Mapping):
        previous_sources = {}
    if previous_roles.get(name) != (file_roles or {}).get(name):
        return "file_role"
    if previous_sources.get(name) != (file_role_sources or {}).get(name):
        return "file_role_source"
    if not ignore_unit_context:
        previous_unit_hashes = previous_manifest.get("unit_context_sha256", {})
        if not isinstance(previous_unit_hashes, Mapping):
            previous_unit_hashes = {}
        # Old manifests lack unit projections, so a changed book-wide context
        # cannot be localized and must invalidate their checkpoints conservatively.
        if (
            resume
            and context_sha256
            and previous_manifest.get("context_sha256", {}) != context_sha256
            and not previous_unit_hashes
        ):
            return "book_context"
        if name in unit_context_sha256:
            if previous_unit_hashes.get(name) != unit_context_sha256[name]:
                return "unit_context"
        elif name in previous_unit_hashes:
            return "unit_context"
    previous_boundary = previous_manifest.get("boundary_context_sha256", {})
    if not isinstance(previous_boundary, Mapping):
        previous_boundary = {}
    if name in (boundary_context_sha256 or {}):
        if previous_boundary.get(name) != (boundary_context_sha256 or {}).get(name):
            return "boundary_context"
    elif name in previous_boundary:
        return "boundary_context"
    previous_heading_contexts = previous_manifest.get("toc_heading_contexts", {})
    if not isinstance(previous_heading_contexts, Mapping):
        previous_heading_contexts = {}
    if heading_contexts.get(name) != previous_heading_contexts.get(name):
        return "heading_context"
    return None


def unit_context_is_current(
    name: str,
    *,
    previous_manifest: Mapping[str, Any],
    controlled_files: Mapping[str, str],
    context_sha256: Mapping[str, str],
    unit_context_sha256: Mapping[str, str],
    heading_contexts: Mapping[str, Mapping[str, Any]],
    resume: bool,
    file_roles: Mapping[str, str] | None = None,
    file_role_sources: Mapping[str, Mapping[str, Any]] | None = None,
    boundary_context_sha256: Mapping[str, str] | None = None,
    ignore_unit_context: bool = False,
) -> bool:
    """Decide whether the context projection for one source unit is unchanged."""
    return (
        unit_context_mismatch_reason(
            name,
            previous_manifest=previous_manifest,
            controlled_files=controlled_files,
            context_sha256=context_sha256,
            unit_context_sha256=unit_context_sha256,
            heading_contexts=heading_contexts,
            resume=resume,
            file_roles=file_roles,
            file_role_sources=file_role_sources,
            boundary_context_sha256=boundary_context_sha256,
            ignore_unit_context=ignore_unit_context,
        )
        is None
    )


@dataclass(frozen=True)
class ResumeDecision:
    previous_manifest: Mapping[str, Any] = field(default_factory=dict)
    validation: Mapping[str, Any] = field(default_factory=dict)
    validated_files: Optional[frozenset[str]] = None
    previous_review_required: Mapping[str, tuple[dict[str, Any], ...]] = field(default_factory=dict)
    previous_human_review_files: frozenset[str] = frozenset()
    completed_files: tuple[str, ...] = ()
    pending_files: tuple[str, ...] = ()
    special_pending_files: tuple[str, ...] = ()
    pending_batches: tuple[tuple[str, ...], ...] = ()
    pending_stats: Mapping[str, Mapping[str, int]] = field(default_factory=dict)
    # Why each non-reusable unit is pending: source_changed,
    # version_only, projection_bytes_changed, context_changed,
    # receipt_missing, target_missing, review_reopened,
    # checkpoint_invalid or fresh_run.
    pending_reasons: Mapping[str, str] = field(default_factory=dict)
    html_invalidated_checkpoints: Mapping[str, str] = field(default_factory=dict)
    effective_concurrency: int = 1
    concurrency_reason: str = "no pending files"


def pending_reason_counts(reasons: Mapping[str, str]) -> dict[str, int]:
    """Count the per-unit pending reasons produced by :func:`decide_resume`.

    Kept beside the classifier so every consumer reads the same taxonomy
    instead of re-deriving categories from pending file names.
    """
    counts: dict[str, int] = {}
    for reason in reasons.values():
        key = str(reason)
        counts[key] = counts.get(key, 0) + 1
    return dict(sorted(counts.items()))


def version_only_pending_names(decision: ResumeDecision) -> list[str]:
    """Return pending units blocked only by a projection-version stamp.

    These units can be forward-rolled by
    :func:`refresh_version_only_receipts` without the
    ``prepare → validate → prepare`` cycle; the helper exists so callers do
    not re-derive the taxonomy.
    """
    return [
        name
        for name in decision.pending_files
        if decision.pending_reasons.get(name) == "version_only"
    ]


def _projection_version_changed(
    name: str,
    previous_manifest: Mapping[str, Any],
    heading_contexts: Mapping[str, Mapping[str, Any]],
) -> bool:
    """Return True when only the TOC heading projection version moved."""
    previous = previous_manifest.get("toc_heading_contexts", {})
    if not isinstance(previous, Mapping):
        return False
    current_entry = heading_contexts.get(name)
    previous_entry = previous.get(name)
    if not isinstance(current_entry, Mapping) or not isinstance(previous_entry, Mapping):
        return False
    current_version = current_entry.get("projection_version")
    previous_version = previous_entry.get("projection_version")
    return (
        isinstance(current_version, int)
        and isinstance(previous_version, int)
        and current_version != previous_version
    )


def _target_is_empty(target: Path) -> bool:
    try:
        return not target.is_file() or target.stat().st_size == 0
    except OSError:
        return True


def _pending_reason(
    name: str,
    *,
    request: MarkdownHandoffRequest,
    previous_manifest: Mapping[str, Any],
    validation_hashes: Mapping[str, Any],
    target: Path,
    source_hash: str,
    validated_files: Optional[frozenset[str]],
    mismatch: Optional[str],
    heading_contexts: Mapping[str, Mapping[str, Any]],
    review_reopened: bool,
) -> str:
    """Explain why one unit is not reusable, without changing the decision.

    The classification exists so an operator can tell a real content change
    from a contract/projection upgrade on the first ``--resume``, which
    otherwise lists every unit as pending.
    """
    if review_reopened:
        return "review_reopened"
    if not request.resume:
        return "fresh_run"
    receipt_hash = (
        validation_hashes.get(name) if isinstance(validation_hashes, Mapping) else None
    )
    if isinstance(receipt_hash, str) and receipt_hash != source_hash:
        return "source_changed"
    previous_sources = previous_manifest.get("source_sha256", {})
    previous_hash = (
        previous_sources.get(name) if isinstance(previous_sources, Mapping) else None
    )
    if (
        isinstance(previous_hash, str)
        and previous_hash != source_hash
        and not isinstance(receipt_hash, str)
    ):
        return "source_changed"
    if mismatch == "heading_context":
        if _projection_version_changed(name, previous_manifest, heading_contexts):
            return "version_only"
        return "projection_bytes_changed"
    if mismatch == "receipt_context":
        return "receipt_missing"
    if mismatch is not None:
        return "context_changed"
    if _target_is_empty(target):
        return "target_missing"
    if validated_files is None or name not in validated_files:
        return "receipt_missing"
    return "checkpoint_invalid"


def _without_projection_versions(value: Any) -> Any:
    """Return a copy of a receipt snapshot with version stamps removed."""
    if isinstance(value, Mapping):
        return {
            str(key): _without_projection_versions(item)
            for key, item in value.items()
            if str(key) != "projection_version"
        }
    if isinstance(value, (list, tuple)):
        return [_without_projection_versions(item) for item in value]
    return value


def _read_receipt(path: Path) -> dict[str, Any]:
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        return {}
    return value if isinstance(value, dict) else {}


def refresh_version_only_receipts(
    output_dir: Path,
    task: str,
    *,
    request: MarkdownHandoffRequest,
    plan: MarkdownHandoffPlan,
    decision: ResumeDecision,
) -> list[str]:
    """Re-stamp receipts whose only context difference is a projection version.

    A version bump (for example ``projection_version`` 5 → 6) can leave the
    projected payload byte-identical.  In that case the old receipt still
    describes exactly the current translation, so forcing ``prepare → validate
    → prepare`` only adds an operator step.  Anything beyond the version stamp
    (new or changed payload fields) is refused on purpose: accepting it would
    certify a translation that was validated under a weaker projection.
    """
    if task not in {"translate", "polish"}:
        return []
    names = [
        name
        for name in decision.pending_files
        if name not in decision.special_pending_files
    ]
    if not names:
        return []
    from .markdown_validation_persistence import capture_pdf_resume_contexts

    snapshots, capture_errors = capture_pdf_resume_contexts(
        Path(output_dir), task, list(names),
        # A single receipt refresh is a per-file operation; neighbor and
        # book-wide mapping checks belong to the next full validation.
        scoped=len(names) == 1,
    )
    current_schema = markdown_validation_schema_version(task)
    receipt_contexts = decision.validation.get("resume_contexts", {})
    if not isinstance(receipt_contexts, Mapping):
        receipt_contexts = {}
    source_hashes = decision.validation.get("source_sha256", {})
    target_hashes = decision.validation.get("target_sha256", {})
    if not isinstance(source_hashes, Mapping):
        source_hashes = {}
    if not isinstance(target_hashes, Mapping):
        target_hashes = {}
    report_path = Path(output_dir) / f"{task}_validation.json"
    ledger_path = Path(output_dir) / f"{task}_file_validation.json"
    report = _read_receipt(report_path)
    ledger = _read_receipt(ledger_path)
    ledger_current = ledger.get("schema_version") == current_schema
    refreshed: list[str] = []
    for name in names:
        if name in capture_errors or name not in snapshots:
            continue
        previous = receipt_contexts.get(name)
        if not isinstance(previous, Mapping):
            continue
        if _without_projection_versions(previous) != _without_projection_versions(
            snapshots[name]
        ):
            continue
        current_source = plan.source_sha256.get(name)
        if not isinstance(current_source, str) or source_hashes.get(name) != current_source:
            continue
        target = Path(request.target_dir) / name
        if not target.is_file():
            continue
        if target_hashes.get(name) != sha256_file(target):
            continue
        if decision.validated_files is None or name not in decision.validated_files:
            continue
        if isinstance(report.get("resume_contexts"), dict):
            report["resume_contexts"][name] = snapshots[name]
            report.setdefault("source_sha256", {})[name] = current_source
            report.setdefault("target_sha256", {})[name] = target_hashes.get(name)
        if ledger_current and isinstance(ledger.get("files"), dict):
            record = ledger["files"].get(name)
            if isinstance(record, dict):
                record["resume_context"] = snapshots[name]
                record["source_sha256"] = current_source
                record["target_sha256"] = target_hashes.get(name)
        refreshed.append(name)
    if not refreshed:
        return []
    if report.get("resume_contexts"):
        atomic_write_json(report_path, report)
    if ledger_current and ledger.get("files"):
        atomic_write_json(ledger_path, ledger)
    return refreshed


def decide_resume(
    request: MarkdownHandoffRequest,
    plan: MarkdownHandoffPlan,
    context: ContextProjection,
) -> ResumeDecision:
    """Load local receipts, then deterministically select reusable/pending units."""
    output_dir = request.output_dir
    task = request.task
    validation_schema = markdown_validation_schema_version(task)
    previous_manifest = load_previous_manifest(output_dir / f"{task}_subagent_manifest.json", enabled=request.resume)
    validation: dict[str, Any] = {}
    validated_files: Optional[set[str]] = None
    previous_reviews: dict[str, list[dict[str, Any]]] = {}
    human_review_files: set[str] = set()
    workspace_blocked = False
    html_resume_contexts: dict[str, Any] = {}
    validation_path = output_dir / f"{task}_validation.json"
    if request.resume and validation_path.is_file():
        try:
            loaded = json.loads(validation_path.read_text(encoding="utf-8"))
            if (
                isinstance(loaded, dict)
                and loaded.get("schema_version") == validation_schema
                and isinstance(loaded.get("valid_files"), list)
                and (
                    task != "translate-html"
                    or (loaded.get("source_snapshot") or {}).get("valid") is True
                )
            ):
                validation = loaded
                validation["file_roles"] = dict(loaded.get("file_roles", {})) if isinstance(loaded.get("file_roles"), Mapping) else {}
                validation["file_role_sources"] = dict(loaded.get("file_role_sources", {})) if isinstance(loaded.get("file_role_sources"), Mapping) else {}
                validated_files = set(loaded["valid_files"])
                raw_contexts = loaded.get("resume_contexts", {})
                if isinstance(raw_contexts, Mapping):
                    html_resume_contexts.update(
                        {str(name): value for name, value in raw_contexts.items()}
                    )
                    for snapshot in raw_contexts.values():
                        if isinstance(snapshot, Mapping):
                            roles = snapshot.get("file_roles", {})
                            sources = snapshot.get("file_role_sources", {})
                            if isinstance(roles, Mapping):
                                validation["file_roles"].update(roles)
                            if isinstance(sources, Mapping):
                                validation["file_role_sources"].update(sources)
                if loaded.get("safety_blocked") or loaded.get("protected_workspace_blocked"):
                    workspace_blocked = True
                    validated_files.clear()
                for item in loaded.get("review_required", []) or []:
                    if isinstance(item, Mapping) and item.get("file"):
                        previous_reviews.setdefault(str(item["file"]), []).append(dict(item))
                for item in loaded.get("human_review_required", []) or []:
                    if isinstance(item, Mapping) and item.get("file"):
                        human_review_files.add(str(item["file"]))
        except (OSError, json.JSONDecodeError):
            pass
    ledger_path = output_dir / f"{task}_file_validation.json"
    if ledger_path.is_file() and not workspace_blocked:
        try:
            ledger = json.loads(ledger_path.read_text(encoding="utf-8"))
            records = ledger.get("files", {}) if isinstance(ledger, dict) else {}
            ledger_schema = ledger.get("schema_version")
            versionless_html_ledger = (
                task == "translate-html"
                and ledger_schema is None
                and ledger.get("task") == "translate-html"
                and ledger.get("scope") == "file-checkpoints"
            )
            if (
                ledger_schema == validation_schema
                or versionless_html_ledger
            ) and isinstance(records, dict):
                if validated_files is None:
                    validated_files = set()
                source_hashes = dict(validation.get("source_sha256", {})) if isinstance(validation.get("source_sha256", {}), dict) else {}
                target_hashes = dict(validation.get("target_sha256", {})) if isinstance(validation.get("target_sha256", {}), dict) else {}
                review_hashes = (
                    dict(validation.get("polish_footnote_semantic_review_sha256", {}))
                    if isinstance(validation.get("polish_footnote_semantic_review_sha256", {}), dict)
                    else {}
                )
                for name, record in records.items():
                    if not isinstance(record, dict):
                        continue
                    name = str(name)
                    for item in record.get("review_required", []) or []:
                        if isinstance(item, Mapping):
                            previous_reviews.setdefault(str(name), []).append(dict(item))
                    if record.get("human_review_required"):
                        human_review_files.add(str(name))
                    if record.get("valid") is not True or record.get("safety_blocked"):
                        validated_files.discard(name)
                        source_hashes.pop(name, None)
                        target_hashes.pop(name, None)
                        html_resume_contexts.pop(name, None)
                        review_hashes.pop(name, None)
                        continue
                    source_digest = record.get("source_sha256")
                    target_digest = record.get("target_sha256")
                    validation.setdefault("file_roles", {})
                    validation.setdefault("file_role_sources", {})
                    validation["file_roles"].pop(str(name), None)
                    validation["file_role_sources"].pop(str(name), None)
                    if record.get("file_role") is not None:
                        validation["file_roles"][str(name)] = record["file_role"]
                    if record.get("file_role_source") is not None:
                        validation["file_role_sources"][str(name)] = record["file_role_source"]
                    if task == "translate-html" and not all(
                        isinstance(digest, str)
                        and re.fullmatch(r"(?i)[0-9a-f]{64}", digest)
                        for digest in (source_digest, target_digest)
                    ):
                        validated_files.discard(name)
                        source_hashes.pop(name, None)
                        target_hashes.pop(name, None)
                        html_resume_contexts.pop(name, None)
                        review_hashes.pop(name, None)
                        continue
                    validated_files.add(str(name))
                    source_hashes[str(name)] = source_digest
                    target_hashes[str(name)] = target_digest
                    review_digest = record.get("polish_footnote_semantic_review_sha256")
                    if isinstance(review_digest, str) and review_digest.strip():
                        review_hashes[str(name)] = review_digest
                    else:
                        review_hashes.pop(str(name), None)
                    if isinstance(record.get("resume_context"), Mapping):
                        html_resume_contexts[str(name)] = dict(record["resume_context"])
                        snapshot = record["resume_context"]
                        for snapshot_key in ("file_roles", "file_role_sources"):
                            values = snapshot.get(snapshot_key, {})
                            if isinstance(values, Mapping):
                                validation.setdefault(snapshot_key, {}).pop(str(name), None)
                                validation[snapshot_key].update(values)
                validation["source_sha256"] = source_hashes
                validation["target_sha256"] = target_hashes
                validation["resume_contexts"] = html_resume_contexts
                if review_hashes or "polish_footnote_semantic_review_sha256" in validation:
                    validation["polish_footnote_semantic_review_sha256"] = review_hashes
        except (OSError, json.JSONDecodeError, AttributeError, TypeError):
            pass

    source_names = {source.name for source in plan.sources}
    previous_reviews = {name: items for name, items in previous_reviews.items() if name in source_names and items}
    validation_hashes = validation.get("source_sha256", {})
    target_hashes = validation.get("target_sha256", {})
    if not isinstance(validation_hashes, Mapping):
        validation_hashes = {}
    if not isinstance(target_hashes, Mapping):
        target_hashes = {}
    completed: list[str] = []
    pending: list[str] = []
    pending_reasons: dict[str, str] = {}
    invalidated_html_checkpoints: dict[str, str] = {}
    previous_html_invalidations = previous_manifest.get("html_invalidated_checkpoints", {})
    if not isinstance(previous_html_invalidations, Mapping):
        previous_html_invalidations = {}
    controlled = plan.controlled_files
    for source in plan.sources:
        name = source.name
        target = request.target_dir / name
        source_hash = plan.source_sha256[name]
        effective_unit_hashes = {} if task == "polish" else context.unit_context_sha256
        ignore_unit_ctx = task == "polish"
        context_mismatch = unit_context_mismatch_reason(
            name,
            previous_manifest=previous_manifest,
            controlled_files=controlled,
            context_sha256=context.context_sha256,
            unit_context_sha256=effective_unit_hashes,
            heading_contexts=context.heading_contexts,
            file_roles=context.file_roles,
            file_role_sources=context.file_role_sources,
            boundary_context_sha256=plan.boundary_context_sha256,
            resume=request.resume,
            ignore_unit_context=ignore_unit_ctx,
        )
        context_current = context_mismatch is None
        html_fingerprint = None
        if task in {"translate", "polish"}:
            receipt_contexts = validation.get("resume_contexts", {})
            receipt_context = receipt_contexts.get(name) if isinstance(receipt_contexts, Mapping) else None
            if isinstance(receipt_context, Mapping):
                context_current = context_current and (
                    receipt_context.get("source_language") == request.source_language
                    and receipt_context.get("target_language") == request.target_language
                    and unit_context_is_current(
                        name,
                        previous_manifest=receipt_context,
                        controlled_files=controlled,
                        context_sha256=context.context_sha256,
                        unit_context_sha256=effective_unit_hashes,
                        heading_contexts=context.heading_contexts,
                        file_roles=context.file_roles,
                        file_role_sources=context.file_role_sources,
                        boundary_context_sha256=plan.boundary_context_sha256,
                        resume=True,
                        ignore_unit_context=ignore_unit_ctx,
                    )
                )
            elif (
                name in effective_unit_hashes
                or name in context.heading_contexts
                or bool(context.context_sha256)
            ):
                # A legacy receipt cannot prove which unit or book-wide
                # projection was validated. Keep context-free receipts reusable.
                context_current = False
        if task == "translate-html":
            html_fingerprint = _html_checkpoint_fingerprint(name, validation)
            receipt_contexts = validation.get("resume_contexts", {})
            receipt_context = receipt_contexts.get(name) if isinstance(receipt_contexts, Mapping) else None
            if isinstance(receipt_context, Mapping):
                context_current = context_current and _html_resume_context_is_current(
                    name, receipt_context, context=context, request=request
                )
            else:
                legacy_language_current = (
                    previous_manifest.get("source_language") == request.source_language
                    and previous_manifest.get("target_language") == request.target_language
                )
                context_current = context_current and legacy_language_current
                # Versionless HTML ledgers have no validation-time context. If
                # the first resumed preparation observes a mismatch, retain
                # that old receipt's fingerprint in the next manifest so the
                # following prepare cannot compare against its rewritten context.
                prior_fingerprint = previous_html_invalidations.get(name)
                if html_fingerprint and prior_fingerprint == html_fingerprint:
                    context_current = False
                elif not context_current and html_fingerprint:
                    invalidated_html_checkpoints[name] = html_fingerprint
        if not context_current and context_mismatch is None:
            # The projection matches, but the receipt cannot prove which
            # context it was validated against (legacy receipt).
            context_mismatch = "receipt_context"
        checkpoint_current = request.resume and context_current and is_reusable_checkpoint(
            target, name, source_hash, validated_files or (), validation_hashes, target_hashes
        )
        if checkpoint_current and task == "polish" and request.footnote_semantics_enabled:
            try:
                from .polish_footnote_semantics import validate_prepared_file_review

                source_text = source.read_bytes().decode("utf-8")
                target_text = target.read_bytes().decode("utf-8")
                semantic_review = validate_prepared_file_review(
                    request.output_dir, name, source_text=source_text,
                    target_text=target_text, required=True,
                )
                records = validation.get("files", {})
                record = records.get(name, {}) if isinstance(records, Mapping) else {}
                review_hashes = validation.get("polish_footnote_semantic_review_sha256", {})
                recorded_review = (
                    review_hashes.get(name) if isinstance(review_hashes, Mapping) else None
                ) or (record.get("polish_footnote_semantic_review_sha256") if isinstance(record, Mapping) else None)
                checkpoint_current = bool(
                    isinstance(semantic_review, Mapping)
                    and semantic_review.get("valid") is True
                    and (
                        (semantic_review.get("implicit_clear") is True and not recorded_review)
                        or (recorded_review and recorded_review == semantic_review.get("sidecar_sha256"))
                    )
                )
            except (OSError, UnicodeError, ValueError):
                checkpoint_current = False
        if checkpoint_current:
            receipt_roles = validation.get("file_roles", {})
            receipt_sources = validation.get("file_role_sources", {})
            if (not isinstance(receipt_roles, Mapping)
                    or receipt_roles.get(name) != context.file_roles.get(name)
                    or not isinstance(receipt_sources, Mapping)
                    or receipt_sources.get(name) != context.file_role_sources.get(name)):
                checkpoint_current = False
        if checkpoint_current:
            completed.append(name)
        else:
            pending.append(name)
            pending_reasons[name] = _pending_reason(
                name,
                request=request,
                previous_manifest=previous_manifest,
                validation_hashes=validation_hashes,
                target=target,
                source_hash=source_hash,
                validated_files=validated_files,
                mismatch=context_mismatch,
                heading_contexts=context.heading_contexts,
                review_reopened=(
                    name in human_review_files or bool(previous_reviews.get(name))
                ),
            )
        if task == "translate-html" and html_fingerprint:
            if previous_html_invalidations.get(name) == html_fingerprint:
                invalidated_html_checkpoints[name] = html_fingerprint

    blocked = sorted(human_review_files & set(pending))
    if request.resume and blocked and not request.allow_human_review_retry:
        raise ValueError(
            "Human review is required before another automatic retry for: "
            + ", ".join(blocked)
            + ". Ask for a decision first, then use --retry-after-human-review "
            "and --human-decision-id only if another Subagent retry is explicitly authorized."
        )
    if request.resume and blocked and request.allow_human_review_retry:
        from .review_escalation import authorize_retry

        authorize_retry(
            output_dir,
            task=task,
            files=blocked,
            human_decision_id=str(request.human_decision_id or "operator-retry-authorized"),
        )
    # Controlled bibliography/index units share the isolated-worker channel
    # with every other pending unit. Their JSON result is an audit attachment,
    # not an independent receipt or a separate pending queue.
    special_pending: list[str] = []
    previous_reviews = {name: items for name, items in previous_reviews.items() if name in pending}
    pending_stats = {name: stats for name, stats in plan.file_stats.items() if name in pending}
    batching = plan.batching
    effective, reason = effective_max_concurrency(
        pending_stats, batching["max_concurrency"], batching["large_file_token_threshold"], batching["extreme_file_token_threshold"]
    )
    batches = recommended_batches(
        pending_stats, plan.task_max_files, batching["max_source_tokens"],
        batching["single_file_max_bytes"], batching["max_nonempty_lines"],
        isolated_files=plan.isolated_files,
    )
    return ResumeDecision(
        previous_manifest=previous_manifest,
        validation=validation,
        validated_files=frozenset(validated_files) if validated_files is not None else None,
        previous_review_required={name: tuple(items) for name, items in previous_reviews.items()},
        previous_human_review_files=frozenset(human_review_files),
        completed_files=tuple(completed),
        pending_files=tuple(pending),
        special_pending_files=tuple(special_pending),
        pending_batches=tuple(tuple(batch) for batch in batches),
        pending_stats=pending_stats,
        pending_reasons=pending_reasons,
        html_invalidated_checkpoints=invalidated_html_checkpoints,
        effective_concurrency=effective,
        concurrency_reason=reason,
    )

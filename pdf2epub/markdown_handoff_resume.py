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
from .workflow_contracts import markdown_validation_schema_version, is_reusable_checkpoint


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
) -> bool:
    """Decide whether the context projection for one source unit is unchanged."""
    previous_special_modes = previous_manifest.get("special_unit_modes", {})
    if not isinstance(previous_special_modes, Mapping):
        previous_special_modes = {}
    if previous_special_modes.get(name) != controlled_files.get(name):
        return False
    previous_roles = previous_manifest.get("file_roles", {})
    previous_sources = previous_manifest.get("file_role_sources", {})
    if not isinstance(previous_roles, Mapping):
        previous_roles = {}
    if not isinstance(previous_sources, Mapping):
        previous_sources = {}
    if previous_roles.get(name) != (file_roles or {}).get(name):
        return False
    if previous_sources.get(name) != (file_role_sources or {}).get(name):
        return False
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
        return False
    if name in unit_context_sha256:
        if previous_unit_hashes.get(name) != unit_context_sha256[name]:
            return False
    elif name in previous_unit_hashes:
        return False
    previous_heading_contexts = previous_manifest.get("toc_heading_contexts", {})
    if not isinstance(previous_heading_contexts, Mapping):
        previous_heading_contexts = {}
    return heading_contexts.get(name) == previous_heading_contexts.get(name)


def controlled_special_checkpoint_is_current(
    output_dir: Path,
    target: Path,
    filename: str,
    kind: str,
    source_hash: str,
    source_language: str,
    target_language: str,
) -> bool:
    """Require a current special-unit apply receipt before reusing a unit."""
    stem = Path(filename).stem
    try:
        manifest = json.loads((output_dir / f"special_unit_{kind}_{stem}_manifest.json").read_text(encoding="utf-8"))
        if not isinstance(manifest, dict) or manifest.get("kind") != kind or manifest.get("source_file") != filename:
            return False
        from pdf2epub.special_unit_context import build_special_unit_context
        context = build_special_unit_context(
            output_dir, filename, kind,
            source_language=source_language,
            target_language=target_language,
        )
        from pdf2epub.special_unit_translation import receipt_is_current
        root = output_dir.resolve()
        source_path = (root / manifest["source_path"]).resolve()
        if source_path != root and root not in source_path.parents:
            return False
        if source_path.name != filename:
            return False
        return hashlib.sha256(source_path.read_bytes()).hexdigest() == source_hash and receipt_is_current(
            kind, source_path, output_dir, target, context
        )
    except (OSError, UnicodeError, json.JSONDecodeError, ValueError, KeyError, TypeError):
        return False


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
    html_invalidated_checkpoints: Mapping[str, str] = field(default_factory=dict)
    effective_concurrency: int = 1
    concurrency_reason: str = "no pending files"


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
                        continue
                    validated_files.add(str(name))
                    source_hashes[str(name)] = source_digest
                    target_hashes[str(name)] = target_digest
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
    invalidated_html_checkpoints: dict[str, str] = {}
    previous_html_invalidations = previous_manifest.get("html_invalidated_checkpoints", {})
    if not isinstance(previous_html_invalidations, Mapping):
        previous_html_invalidations = {}
    controlled = plan.controlled_files
    for source in plan.sources:
        name = source.name
        target = request.target_dir / name
        source_hash = plan.source_sha256[name]
        context_current = unit_context_is_current(
            name,
            previous_manifest=previous_manifest,
            controlled_files=controlled,
            context_sha256=context.context_sha256,
            unit_context_sha256=context.unit_context_sha256,
            heading_contexts=context.heading_contexts,
            file_roles=context.file_roles,
            file_role_sources=context.file_role_sources,
            resume=request.resume,
        )
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
                        unit_context_sha256=context.unit_context_sha256,
                        heading_contexts=context.heading_contexts,
                        file_roles=context.file_roles,
                        file_role_sources=context.file_role_sources,
                        resume=True,
                    )
                )
            elif (
                name in context.unit_context_sha256
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
        checkpoint_current = request.resume and context_current and is_reusable_checkpoint(
            target, name, source_hash, validated_files or (), validation_hashes, target_hashes
        )
        if checkpoint_current:
            receipt_roles = validation.get("file_roles", {})
            receipt_sources = validation.get("file_role_sources", {})
            if (not isinstance(receipt_roles, Mapping)
                    or receipt_roles.get(name) != context.file_roles.get(name)
                    or not isinstance(receipt_sources, Mapping)
                    or receipt_sources.get(name) != context.file_role_sources.get(name)):
                checkpoint_current = False
        if checkpoint_current and name in controlled:
            checkpoint_current = controlled_special_checkpoint_is_current(
                request.output_dir, target, name, controlled[name], source_hash,
                request.source_language, request.target_language,
            )
        (completed if checkpoint_current else pending).append(name)
        if task == "translate-html" and html_fingerprint:
            if previous_html_invalidations.get(name) == html_fingerprint:
                invalidated_html_checkpoints[name] = html_fingerprint

    blocked = sorted(human_review_files & set(pending))
    if request.resume and blocked and not request.allow_human_review_retry:
        raise ValueError(
            "Human review is required before another automatic retry for: "
            + ", ".join(blocked)
            + ". Ask for a decision first, then use --retry-after-human-review "
            "only if another Subagent retry is explicitly authorized."
        )
    special_pending = [name for name in pending if name in controlled]
    pending = [name for name in pending if name not in controlled]
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
        html_invalidated_checkpoints=invalidated_html_checkpoints,
        effective_concurrency=effective,
        concurrency_reason=reason,
    )

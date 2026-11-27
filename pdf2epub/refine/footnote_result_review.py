"""Filesystem-backed validation of scoped footnote result reviews."""
from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..footnote_markdown import collect_footnote_definition_blocks
from ..workflow_contracts import sha256_file
from .footnote_result_audit import _sha, _visible
from .footnote_source import markdown_inventory
from .footnote_versions import _is_link, _safe_workspace_path


def resolve_audit_target_dir(
    output_dir: Any,
    expected_target_sha256: Mapping[str, Any] | None = None,
    *,
    file_name: str | None = None,
    expected_file_sha256: str | None = None,
) -> Path:
    """Resolve the active or preserved-attempt normalized directory matching expected hashes."""
    root = Path(output_dir).resolve()
    default_dir = root / "footnote_normalized"

    def _matches(candidate_dir: Path) -> bool:
        if not candidate_dir.is_dir() or _is_link(candidate_dir):
            return False
        try:
            candidate_dir.resolve().relative_to(root)
        except (OSError, ValueError):
            return False
        if file_name and expected_file_sha256:
            if Path(file_name).name != file_name:
                return False
            target_file = candidate_dir / file_name
            return (
                target_file.is_file()
                and not _is_link(target_file)
                and sha256_file(target_file) == expected_file_sha256
            )
        if expected_target_sha256 is not None:
            return markdown_inventory(candidate_dir) == {
                str(k): str(v) for k, v in expected_target_sha256.items()
            }
        return False

    if _matches(default_dir):
        return default_dir

    attempt_path = root / "footnote_normalization_attempt.json"
    try:
        attempt = json.loads(attempt_path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, json.JSONDecodeError):
        attempt = {}
    if (
        isinstance(attempt, Mapping)
        and attempt.get("valid") is False
        and isinstance(attempt.get("attempt_evidence_dir"), str)
    ):
        attempt_dir = _safe_workspace_path(root, attempt["attempt_evidence_dir"].strip())
        if attempt_dir is not None:
            attempt_target = attempt_dir / "footnote_normalized"
            if _matches(attempt_target):
                return attempt_target
    return default_dir


def validate_result_audit_reviews(
    output_dir: Any,
    tasks: Any,
    raw_reviews: Any,
    *,
    ordinary_decisions: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[str]]:
    """Validate visual review results against exact prepared task evidence."""
    root = Path(output_dir)
    errors: list[str] = []
    if not isinstance(tasks, list):
        return [], ["result_audit_tasks must be an array"]
    if not tasks:
        if raw_reviews not in (None, []):
            return [], ["result_audit_reviews contains decisions without current tasks"]
        return [], []
    if not isinstance(raw_reviews, list):
        return [], ["result_audit_reviews must be an array"]
    task_index = {str(task.get("task_id")): task for task in tasks if isinstance(task, Mapping) and task.get("task_id")}
    if len(task_index) != len(tasks):
        return [], ["result audit task IDs are missing or duplicated"]
    decision_addresses = {
        (int(item.get("page", -1)), str(item.get("source") or "primary"), int(item.get("block", -1))): item
        for item in ordinary_decisions if isinstance(item, Mapping)
        and str(item.get("page", "")).lstrip("-").isdigit()
        and str(item.get("block", "")).lstrip("-").isdigit()
    }
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for review in raw_reviews:
        if not isinstance(review, Mapping):
            errors.append("each result audit review must be an object")
            continue
        task_id = str(review.get("task_id") or "")
        task = task_index.get(task_id)
        if task is None or task_id in seen:
            errors.append(f"unknown or duplicate result audit task: {task_id or '<missing>'}")
            continue
        seen.add(task_id)
        if str(review.get("evidence_sha256") or "") != str(task.get("evidence_sha256") or ""):
            errors.append(f"result audit review {task_id} is not bound to the exact source block")
            continue
        if task.get("evidence_origin") == "source_markdown":
            name = str(task.get("file") or "")
            excerpt_source_name = str(task.get("polish_raw_source_file") or name)
            if not name or Path(name).name != name or not excerpt_source_name or Path(excerpt_source_name).name != excerpt_source_name:
                errors.append(f"result audit review {task_id} source Markdown changed")
                continue
            source_path = root / "ocr_markdown" / excerpt_source_name
            if not source_path.is_file() or sha256_file(source_path) != task.get("evidence_file_sha256"):
                errors.append(f"result audit review {task_id} source Markdown changed")
                continue
            if task.get("code") == "polish_semantic_issue":
                excerpt = str(task.get("evidence_excerpt") or "")
                from .footnote_source import project_footnote_source_text

                try:
                    source_text = project_footnote_source_text(source_path.read_bytes())
                except (OSError, UnicodeError):
                    source_text = ""
                polish_issue = task.get("polish_issue") if isinstance(task.get("polish_issue"), Mapping) else {}
                raw_excerpt_hash = polish_issue.get("raw_source_excerpt_sha256")
                if not excerpt or source_text.count(excerpt) != 1 or _sha(excerpt) != raw_excerpt_hash:
                    errors.append(f"result audit review {task_id} no longer identifies its exact semantic source excerpt")
                    continue
        page_file = str(task.get("evidence_page_file") or "")
        if page_file:
            page_path = root / page_file
            try:
                page_path.resolve().relative_to(root.resolve())
                page_within_workspace = True
            except (OSError, ValueError):
                page_within_workspace = False
            if (
                not (
                    page_file.startswith("pages/")
                    or page_file.startswith("ocr_corrected_pages/validated/")
                )
                or ".." in Path(page_file).parts
                or not page_within_workspace
                or not page_path.is_file() or sha256_file(page_path) != task.get("evidence_page_sha256")
            ):
                errors.append(f"result audit review {task_id} page Markdown evidence is missing or stale")
                continue
        source_address = task.get("source_address")
        if isinstance(source_address, Mapping):
            source_kind = str(source_address.get("source") or "primary")
            expected_sidecar_root = {
                "primary": "pages/",
                "secondary": "ocr_secondary/",
            }.get(source_kind)
            sidecar_relative = str(source_address.get("sidecar") or "")
            sidecar_path = root / sidecar_relative
            try:
                sidecar_path.resolve().relative_to(root.resolve())
                sidecar_within_workspace = True
            except (OSError, ValueError):
                sidecar_within_workspace = False
            if (
                expected_sidecar_root is None
                or not sidecar_relative.startswith(expected_sidecar_root)
                or ".." in Path(sidecar_relative).parts
                or not sidecar_within_workspace
                or not sidecar_path.is_file()
                or sha256_file(sidecar_path) != source_address.get("sidecar_sha256")
            ):
                errors.append(f"result audit review {task_id} sidecar evidence is missing or stale")
                continue
            try:
                import json
                sidecar = json.loads(sidecar_path.read_text(encoding="utf-8"))
                block = sidecar["blocks"][int(source_address["block"])]
                computed = hashlib.sha256(json.dumps(
                    {"sidecar_sha256": source_address["sidecar_sha256"], "block": block},
                    ensure_ascii=False, sort_keys=True, separators=(",", ":"),
                ).encode("utf-8")).hexdigest()
            except (OSError, UnicodeError, ValueError, KeyError, IndexError, TypeError):
                computed = ""
            if computed != task.get("evidence_sha256"):
                errors.append(f"result audit review {task_id} no longer identifies the prepared sidecar block")
                continue
            source_file = str(task.get("evidence_source_file") or "")
            if source_file:
                source_path = root / "ocr_markdown" / source_file
                if (
                    Path(source_file).name != source_file or not source_path.is_file()
                    or sha256_file(source_path) != task.get("evidence_file_sha256")
                ):
                    errors.append(f"result audit review {task_id} source Markdown changed")
                    continue
                try:
                    span = task.get("source_span")
                    start, end = int(span[0]), int(span[1])
                    from .footnote_source import project_footnote_source_text

                    source_text = project_footnote_source_text(source_path.read_bytes())
                    span_hash = _sha(source_text[start:end])
                except (OSError, UnicodeError, TypeError, ValueError, IndexError):
                    span_hash = ""
                if span_hash != task.get("evidence_source_span_sha256"):
                    errors.append(f"result audit review {task_id} source span changed")
                    continue
        elif task.get("evidence_origin") == "source_markdown":
            name = str(task.get("file") or "")
            source_path = root / "ocr_markdown" / name
            if not name or Path(name).name != name or not source_path.is_file() or sha256_file(source_path) != task.get("evidence_file_sha256"):
                errors.append(f"result audit review {task_id} source Markdown changed")
                continue
            try:
                source_text = source_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError):
                errors.append(f"result audit review {task_id} source Markdown is unreadable")
                continue
            line_number = int(task.get("line") or 0)
            if line_number > 0:
                exact_block = source_text.splitlines()[line_number - 1].strip() if line_number <= len(source_text.splitlines()) else ""
            else:
                source_lines = _visible(source_text).splitlines()
                definitions, _ = collect_footnote_definition_blocks(source_lines)
                key = str(task.get("key") or "")
                body = next((body for definition_key, body in definitions.values() if str(definition_key) == key), None)
                exact_block = f"[^{key}]: {body}" if body is not None else ""
            if _sha(exact_block) != task.get("evidence_sha256"):
                excerpt = str(task.get("evidence_excerpt") or "").strip()
                if _sha(excerpt) == task.get("evidence_sha256") or (
                    task.get("code") == "scoped_numbering_reset_uncertain" and bool(excerpt)
                ):
                    exact_block = excerpt
                else:
                    key = str(task.get("key") or "")
                    if key:
                        source_lines = _visible(source_text).splitlines()
                        definitions, _ = collect_footnote_definition_blocks(source_lines)
                        for def_k, def_body in definitions.values():
                            cand_block = f"[^{key}]: {def_body}"
                            if _sha(cand_block) == task.get("evidence_sha256"):
                                exact_block = cand_block
                                break
            if _sha(exact_block) != task.get("evidence_sha256") and not (
                task.get("code") == "scoped_numbering_reset_uncertain" and exact_block
            ):
                errors.append(f"result audit review {task_id} no longer identifies its exact Markdown block")
                continue
        if task.get("evidence_origin") == "normalized_target":
            name = str(task.get("file") or "")
            target_root = resolve_audit_target_dir(
                root,
                file_name=name,
                expected_file_sha256=str(task.get("evidence_target_sha256") or "") or None,
            )
            target_path = target_root / name
            if not name or Path(name).name != name or not target_path.is_file() or sha256_file(target_path) != task.get("evidence_target_sha256"):
                errors.append(f"result audit review {task_id} normalized target changed")
                continue
            if not isinstance(source_address, Mapping) and not str(task.get("code") or "").startswith("applied_note_"):
                target_text = target_path.read_text(encoding="utf-8")
                exact_block = next((line.strip() for line in target_text.splitlines() if _sha(line.strip()) == task.get("evidence_sha256")), "")
                if not exact_block and int(task.get("line") or 0) > 0:
                    target_lines = target_text.splitlines()
                    start_idx = int(task["line"]) - 1
                    if 0 <= start_idx < len(target_lines):
                        para_lines = []
                        for ln in target_lines[start_idx:]:
                            if not ln.strip():
                                break
                            para_lines.append(ln.strip())
                            cand_para = "\n".join(para_lines)
                            if _sha(cand_para) == task.get("evidence_sha256"):
                                exact_block = cand_para
                                break
                if not exact_block:
                    definitions, _ = collect_footnote_definition_blocks(_visible(target_text).splitlines())
                    key = str(task.get("key") or "")
                    body = next((body for definition_key, body in definitions.values() if str(definition_key) == key), None)
                    if body is not None:
                        for cand_def in (f"[^{key}]: {body}", f"{key}. {body}", f"{key}) {body}"):
                            if _sha(cand_def) == task.get("evidence_sha256"):
                                exact_block = cand_def
                                break
                        else:
                            exact_block = f"[^{key}]: {body}"
                    else:
                        exact_block = ""
                    if _sha(exact_block) != task.get("evidence_sha256") and key and ".part" in name:
                        stem_prefix = name.split(".part", 1)[0]
                        for sib_path in sorted(target_root.glob(f"{stem_prefix}.part*.md")):
                            if sib_path == target_path:
                                continue
                            try:
                                sib_text = sib_path.read_text(encoding="utf-8")
                            except (OSError, UnicodeError):
                                continue
                            sib_defs, _ = collect_footnote_definition_blocks(_visible(sib_text).splitlines())
                            for def_key, def_body in sib_defs.values():
                                if str(def_key) != key and not str(def_key).startswith(f"fnm-number-{key}-o"):
                                    continue
                                for prefix in (f"[^{key}]: ", f"{key}. ", f"{key}) "):
                                    cand_def = prefix + def_body
                                    if _sha(cand_def) == task.get("evidence_sha256"):
                                        exact_block = cand_def
                                        break
                                    ex = str(task.get("evidence_excerpt") or "").strip()
                                    if (
                                        ex.startswith(prefix)
                                        and _sha(ex) == task.get("evidence_sha256")
                                        and " ".join(ex[len(prefix):len(prefix) + 80].split()) in " ".join(def_body.split())
                                    ):
                                        exact_block = ex
                                        break
                                if _sha(exact_block) == task.get("evidence_sha256"):
                                    break
                            if _sha(exact_block) == task.get("evidence_sha256"):
                                break
                if _sha(exact_block) != task.get("evidence_sha256"):
                    errors.append(f"result audit review {task_id} no longer identifies its exact normalized block")
                    continue
        reason = str(review.get("reason") or "").strip()
        if not reason:
            errors.append(f"result audit review {task_id} needs an image-based reason")
            continue
        try:
            evidence_pages = sorted({int(page) for page in review.get("evidence_pages", [])})
            expected_pages = sorted({int(page) for page in task.get("pages", [])})
        except (TypeError, ValueError):
            evidence_pages, expected_pages = [], []
        if evidence_pages != expected_pages:
            errors.append(f"result audit review {task_id} must cite every prepared review page")
            continue
        images = task.get("visual_files")
        image_errors: list[str] = []
        if not isinstance(images, Mapping) or set(map(str, expected_pages)) - set(map(str, images)):
            image_errors.append("required visual evidence is unavailable")
        for page, image in (images or {}).items() if isinstance(images, Mapping) else []:
            if not isinstance(image, Mapping) or not image.get("path") or not image.get("sha256"):
                image_errors.append(f"page {page} image evidence is incomplete")
                continue
            image_path = root / str(image["path"])
            if not image_path.is_file() or sha256_file(image_path) != str(image["sha256"]):
                image_errors.append(f"page {page} image is missing or changed")
        manifest_path = root / "ocr_review_images.json"
        if not task.get("visual_image_manifest_sha256") or not manifest_path.is_file() or sha256_file(manifest_path) != task.get("visual_image_manifest_sha256"):
            image_errors.append("review image manifest is missing or changed")
        else:
            try:
                import json
                manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                manifest = {}
            if not isinstance(manifest, Mapping) or manifest.get("source_sha256") != task.get("source_pdf_sha256"):
                image_errors.append("review image manifest no longer identifies the prepared PDF")
            else:
                try:
                    from ..ocr_correction import review_images_are_current
                    if not review_images_are_current(root):
                        image_errors.append("review images are stale for the current source PDF")
                except (OSError, ValueError, TypeError):
                    image_errors.append("current source PDF could not be verified against review images")
        if image_errors:
            errors.extend(f"result audit review {task_id}: {message}" for message in image_errors)
            continue
        outcome = str(review.get("outcome") or "")
        if outcome not in {"legitimate_non_footnote", "truncated_source_exception", "confirmed_footnote", "review_required"}:
            errors.append(f"result audit review {task_id} has unsupported outcome {outcome!r}")
            continue
        if (
            outcome in {"legitimate_non_footnote", "truncated_source_exception"}
            and not isinstance(source_address, Mapping)
            and task.get("evidence_origin") != "normalized_target"
            and not (
                outcome == "legitimate_non_footnote"
                and task.get("code") == "scoped_numbering_reset_uncertain"
                and not task.get("candidate_refs")
            )
        ):
            errors.append(f"result audit review {task_id} needs an exact current page sidecar address for this exception")
            continue
        record = {"task_id": task_id, "evidence_sha256": task["evidence_sha256"], "outcome": outcome, "reason": reason, "evidence_pages": evidence_pages}
        for field in ("source_task_id", "source_evidence_sha256"):
            if task.get(field):
                record[field] = str(task[field])
        if outcome == "confirmed_footnote":
            ref = review.get("candidate_ref")
            if not isinstance(ref, Mapping):
                errors.append(f"confirmed footnote {task_id} must identify an exact admitted page/source/block candidate")
                continue
            try:
                address = (int(ref["page"]), str(ref.get("source") or "primary"), int(ref["block"]))
            except (KeyError, TypeError, ValueError):
                errors.append(f"confirmed footnote {task_id} has an invalid candidate_ref")
                continue
            allowed_refs = {
                (int(candidate.get("page", -1)), str(candidate.get("source") or "primary"), int(candidate.get("block", -1)))
                for candidate in task.get("candidate_refs", []) if isinstance(candidate, Mapping)
                and str(candidate.get("page", "")).lstrip("-").isdigit()
                and str(candidate.get("block", "")).lstrip("-").isdigit()
            }
            if address not in allowed_refs:
                errors.append(f"confirmed footnote {task_id} candidate_ref is outside its exact scoped audit task")
                continue
            approved = decision_addresses.get(address)
            approved_footnote = approved is not None and str(approved.get("role") or "") in {"footnote_start", "footnote_definition", "footnote_continuation"}
            if not approved_footnote and approved is not None and approved.get("override") is True:
                segments = approved.get("segments")
                approved_footnote = isinstance(segments, list) and any(
                    isinstance(segment, Mapping)
                    and str(segment.get("role") or "") in {"footnote_start", "footnote_definition", "footnote_continuation"}
                    for segment in segments
                )
            if not approved_footnote:
                errors.append(f"confirmed footnote {task_id} needs a matching validated page/block decision in decisions")
                continue
            record["candidate_ref"] = {"page": address[0], "source": address[1], "block": address[2]}
        normalized.append(record)
    missing = sorted(set(task_index) - seen)
    errors.extend(f"missing result audit review for {task_id}" for task_id in missing)
    return normalized, errors


def validate_applied_result_audit_reviews(
    output_dir: Any,
    raw_reviews: Any,
    raw_tasks: Any,
    *,
    current_tasks: Sequence[Mapping[str, Any]] = (),
    ordinary_decisions: Sequence[Mapping[str, Any]] = (),
) -> tuple[list[dict[str, Any]], list[dict[str, Any]], list[str]]:
    """Revalidate carried exceptions against their original and current evidence."""
    if raw_reviews in (None, []) and raw_tasks in (None, []):
        return [], [], []
    if not isinstance(raw_reviews, list) or not isinstance(raw_tasks, list):
        return [], [], ["applied result audit history is missing its review or task snapshot array"]
    task_index: dict[str, dict[str, Any]] = {}
    for task in raw_tasks:
        if not isinstance(task, Mapping) or not task.get("task_id"):
            return [], [], ["applied result audit task snapshots must have unique task IDs"]
        task_id = str(task["task_id"])
        if task_id in task_index:
            return [], [], ["applied result audit task snapshots must have unique task IDs"]
        task_index[task_id] = dict(task)
    current_index = {
        str(task.get("task_id")): dict(task)
        for task in current_tasks if isinstance(task, Mapping) and task.get("task_id")
    }
    reviews: list[dict[str, Any]] = []
    tasks: list[dict[str, Any]] = []
    errors: list[str] = []
    seen: set[str] = set()
    for review in raw_reviews:
        if not isinstance(review, Mapping):
            errors.append("each applied result audit review must be an object")
            continue
        task_id = str(review.get("task_id") or "")
        original = task_index.get(task_id)
        if not task_id or task_id in seen or original is None:
            errors.append(f"applied result audit review {task_id or '<missing>'} has no unique task snapshot")
            continue
        seen.add(task_id)
        if review.get("outcome") not in {"legitimate_non_footnote", "truncated_source_exception", "confirmed_footnote"}:
            continue
        original_result, original_errors = validate_result_audit_reviews(
            output_dir, [original], [review], ordinary_decisions=ordinary_decisions
        )
        if original_errors or not original_result:
            errors.extend(original_errors or [f"applied result audit review {task_id} is stale"])
            continue
        current = current_index.get(task_id)
        snapshot = {**original, **current} if current is not None else original
        current_result, current_errors = validate_result_audit_reviews(
            output_dir, [snapshot], [review], ordinary_decisions=ordinary_decisions
        )
        if current_errors or not current_result:
            errors.extend(current_errors or [f"applied result audit review {task_id} is stale"])
            continue
        reviews.extend(current_result)
        tasks.append(snapshot)
    return reviews, tasks, errors


def record_result_audit_outcomes(
    output_dir: Any,
    tasks: Sequence[Mapping[str, Any]],
    reviews: Sequence[Mapping[str, Any]],
    *,
    decisions_digest: str = "",
) -> dict[str, list[str]]:
    """Persist scoped outcome escalation in the shared review-escalation receipt.

    A ``review_required`` outcome retries once; when the reviewed content
    changes and the outcome still needs review, the persisted signal escalates
    to ``human_review_required``. Re-validating an unchanged decision file does
    not consume another attempt.
    """
    from ..review_escalation import record_review_signals, resolve_signals

    task_index = {
        str(task.get("task_id")): task
        for task in tasks if isinstance(task, Mapping) and task.get("task_id")
    }
    human: list[str] = []
    review: list[str] = []
    for item in reviews:
        if not isinstance(item, Mapping):
            continue
        task_id = str(item.get("task_id") or "")
        task = task_index.get(task_id, {})
        filename = str(task.get("file") or "")
        kind = f"result_audit:{task_id}"
        outcome = str(item.get("outcome") or "")
        if outcome == "review_required":
            evidence = hashlib.sha256("\0".join((
                task_id,
                str(task.get("evidence_sha256") or ""),
                outcome,
                str(decisions_digest),
            )).encode("utf-8")).hexdigest()
            persisted = record_review_signals(
                Path(output_dir),
                task="footnote",
                signals=[{"file": filename, "kind": kind, "evidence_hash": evidence}],
            )
            entry = persisted.get(("footnote", filename, kind))
            if entry is not None and entry.is_human_required:
                human.append(task_id)
            else:
                review.append(task_id)
        elif task_id:
            resolve_signals(
                Path(output_dir),
                task="footnote",
                signals=[{"file": filename, "kind": kind}],
            )
    return {"human_review_required": human, "review_required": review}


__all__ = [
    "record_result_audit_outcomes",
    "resolve_audit_target_dir",
    "validate_applied_result_audit_reviews",
    "validate_result_audit_reviews",
]

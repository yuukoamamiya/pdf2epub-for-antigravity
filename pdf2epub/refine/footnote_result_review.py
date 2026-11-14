"""Filesystem-backed validation of scoped footnote result reviews."""
from __future__ import annotations

import hashlib
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import Any

from ..footnote_markdown import collect_footnote_definition_blocks
from ..workflow_contracts import sha256_file
from .footnote_result_audit import _sha, _visible


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
        source_address = task.get("source_address")
        if isinstance(source_address, Mapping):
            sidecar_relative = str(source_address.get("sidecar") or "")
            sidecar_path = root / sidecar_relative
            if (
                not sidecar_relative.startswith("pages/")
                or ".." in Path(sidecar_relative).parts
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
                errors.append(f"result audit review {task_id} no longer identifies its exact Markdown block")
                continue
        elif task.get("evidence_origin") == "normalized_target":
            name = str(task.get("file") or "")
            target_path = root / "footnote_normalized" / name
            if not name or Path(name).name != name or not target_path.is_file() or sha256_file(target_path) != task.get("evidence_target_sha256"):
                errors.append(f"result audit review {task_id} normalized target changed")
                continue
            target_text = target_path.read_text(encoding="utf-8")
            if str(task.get("code") or "").startswith("applied_note_"):
                exact_block = "plan-bound materialization evidence"
            else:
                exact_block = next((line.strip() for line in target_text.splitlines() if _sha(line.strip()) == task.get("evidence_sha256")), "")
            if not exact_block:
                definitions, _ = collect_footnote_definition_blocks(_visible(target_text).splitlines())
                key = str(task.get("key") or "")
                body = next((body for definition_key, body in definitions.values() if str(definition_key) == key), None)
                exact_block = f"[^{key}]: {body}" if body is not None else ""
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
        if not expected_pages or evidence_pages != expected_pages:
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
            if approved is None or str(approved.get("role") or "") not in {"footnote_start", "footnote_definition", "footnote_continuation"}:
                errors.append(f"confirmed footnote {task_id} needs a matching validated page/block decision in decisions")
                continue
            record["candidate_ref"] = {"page": address[0], "source": address[1], "block": address[2]}
        normalized.append(record)
    missing = sorted(set(task_index) - seen)
    errors.extend(f"missing result audit review for {task_id}" for task_id in missing)
    return normalized, errors


__all__ = ["validate_result_audit_reviews"]

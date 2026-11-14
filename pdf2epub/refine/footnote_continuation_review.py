"""Validation helpers for image-backed missing page-note continuations."""

from __future__ import annotations

from pathlib import Path
import re
from typing import Any, Mapping

from ..workflow_contracts import sha256_file


def validate_continuation_review_decisions(
    output_dir: Path,
    tasks: Any,
    raw_decisions: Any,
) -> tuple[list[dict[str, Any]], list[str]]:
    """Require one explicit image-backed outcome for every detected gap."""
    errors: list[str] = []
    if not isinstance(tasks, list):
        return [], ["continuation_reviews queue must be an array"]
    if not tasks:
        return [], []
    if not isinstance(raw_decisions, list):
        return [], ["continuation_reviews must be an array"]
    task_index: dict[str, Mapping[str, Any]] = {}
    for index, task in enumerate(tasks):
        if not isinstance(task, Mapping):
            errors.append(f"continuation review task {index} must be an object")
            continue
        task_id = str(task.get("task_id") or "")
        if not task_id:
            errors.append(f"continuation review task {index} is missing task_id")
            continue
        if task_id in task_index:
            errors.append(f"duplicate continuation review task_id: {task_id}")
            continue
        pages = task.get("pages")
        source_note = task.get("source_note")
        if (
            not isinstance(pages, list)
            or not pages
            or any(not isinstance(page, int) or page < 1 for page in pages)
            or not isinstance(source_note, Mapping)
        ):
            errors.append(f"continuation review task {task_id} has invalid pages or source_note")
            continue
        task_index[task_id] = task
    if errors:
        return [], errors
    seen: set[str] = set()
    normalized: list[dict[str, Any]] = []
    for item in raw_decisions:
        if not isinstance(item, Mapping):
            errors.append("each continuation review must be an object")
            continue
        task_id = str(item.get("task_id") or "")
        task = task_index.get(task_id)
        if task is None:
            errors.append(f"continuation review references unknown task: {task_id or '<missing>'}")
            continue
        if task_id in seen:
            errors.append(f"duplicate continuation review for {task_id}")
            continue
        seen.add(task_id)
        outcome = str(item.get("outcome") or "")
        if outcome not in {
            "truncated_source_exception",
            "confirmed_not_a_footnote",
            "recovered_continuation",
        }:
            errors.append(f"unsupported continuation review outcome for {task_id}: {outcome}")
            continue
        reason = str(item.get("reason") or "").strip()
        if not reason:
            errors.append(f"continuation review {task_id} needs an image-based reason")
            continue
        expected_pages = [int(page) for page in task.get("pages", [])]
        try:
            evidence_pages = sorted({int(page) for page in item.get("evidence_pages", [])})
        except (TypeError, ValueError):
            evidence_pages = []
        if evidence_pages != sorted(expected_pages):
            errors.append(f"continuation review {task_id} must cite every task page")
            continue
        image_errors = []
        visual_files = task.get("visual_files")
        if not isinstance(visual_files, Mapping) or set(map(str, expected_pages)) - set(map(str, visual_files)):
            image_errors.append("one or more required page images are unavailable")
        for page, image in (visual_files or {}).items() if isinstance(visual_files, Mapping) else []:
            if not isinstance(image, Mapping) or not image.get("path") or not image.get("sha256"):
                image_errors.append(f"page {page} review image is unavailable")
                continue
            path = Path(output_dir) / str(image["path"])
            if not path.is_file() or sha256_file(path) != str(image["sha256"]):
                image_errors.append(f"page {page} review image is missing or changed")
        manifest_path = Path(output_dir) / "ocr_review_images.json"
        expected_manifest_hash = task.get("visual_image_manifest_sha256")
        if not expected_manifest_hash or not manifest_path.is_file() or sha256_file(manifest_path) != expected_manifest_hash:
            image_errors.append("review image manifest is missing or changed")
        else:
            try:
                import json
                image_manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                image_manifest = {}
            if not isinstance(image_manifest, Mapping) or image_manifest.get("source_sha256") != task.get("source_pdf_sha256"):
                image_errors.append("review image manifest no longer identifies the prepared source PDF")
            else:
                try:
                    from ..ocr_correction import review_images_are_current
                    if not review_images_are_current(Path(output_dir)):
                        image_errors.append("review images are stale for the current source PDF")
                except (OSError, ValueError, RuntimeError, ImportError):
                    image_errors.append("review images cannot be verified against the current source PDF")
        if image_errors:
            errors.extend(f"continuation review {task_id}: {message}" for message in image_errors)
            continue
        record = {"task_id": task_id, "outcome": outcome, "reason": reason,
                  "evidence_pages": evidence_pages,
                  "source_note": dict(task.get("source_note", {}))}
        if outcome == "recovered_continuation":
            if task.get("terminal_page") is True:
                errors.append(f"continuation review {task_id} is on the final physical page and cannot recover a later-page continuation")
                continue
            source_note = task.get("source_note", {})
            key = str(item.get("key") or "")
            text = str(item.get("text") or "").strip()
            if not key or key != str(source_note.get("key") or ""):
                errors.append(f"continuation review {task_id} must use the exact source note key")
                continue
            if not text or len(text) > 4000:
                errors.append(f"continuation review {task_id} needs exact recovered image text (1..4000 chars)")
                continue
            if task.get("boundary_kind", "hyphen") == "hyphen" and not re.match(r"^[a-zà-öø-ÿ]", text):
                errors.append(f"continuation review {task_id} must begin with the visible lowercase word suffix")
                continue
            normalized_text = re.sub(r"\s+", " ", text).strip().casefold()
            duplicate = False
            page_source = Path(output_dir) / "pages" / f"page_{expected_pages[-1]:03d}.md"
            if page_source.is_file():
                try:
                    visible_source = re.sub(r"\s+", " ", page_source.read_text(encoding="utf-8")).casefold()
                    duplicate = normalized_text in visible_source
                except (OSError, UnicodeError):
                    pass
            if not duplicate:
                for source_path in (Path(output_dir) / "ocr_markdown").glob("*.md"):
                    try:
                        visible_source = re.sub(r"\s+", " ", source_path.read_text(encoding="utf-8")).casefold()
                    except (OSError, UnicodeError):
                        continue
                    if normalized_text in visible_source:
                        duplicate = True
                        break
            if duplicate:
                errors.append(
                    f"continuation review {task_id} recovered text already exists in primary OCR Markdown; resolve the source block first"
                )
                continue
            record.update({"key": key, "text": text})
        normalized.append(record)
    missing = sorted(set(task_index) - seen)
    errors.extend(f"missing explicit continuation review for {task_id}" for task_id in missing)
    return normalized, errors


__all__ = ["validate_continuation_review_decisions"]

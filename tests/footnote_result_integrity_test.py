from __future__ import annotations

import hashlib
import json
from dataclasses import replace
from pathlib import Path

import pytest

from pdf2epub import ocr_correction
from pdf2epub.refine import footnote_apply
from pdf2epub.refine.footnote_apply import apply_footnote_normalization
from pdf2epub.refine.footnote_contracts import FOOTNOTE_PREPARE_SCHEMA_VERSION
from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_result_review import validate_result_audit_reviews
from pdf2epub.refine.footnote_status import footnote_normalization_status
from pdf2epub.refine.footnote_validation import validate_footnote_decisions

_FIRST = "First paragraph states the governing principle with precise terms."
_SECOND = "Second paragraph preserves the detailed objection and its conditions."
_THIRD = "Third paragraph closes the note with one final qualification."


def _native_block(text: str, order: int, *, y0: float, font_size: float = 10.0) -> dict:
    return {
        "order": order,
        "label": "Text",
        "bbox": [72, y0, 540, y0 + 18],
        "text": text,
        "font_size": font_size,
    }


def _write_native_page(tmp_path: Path) -> None:
    pages = tmp_path / "pages"
    pages.mkdir(parents=True)
    (pages / "ocr_progress.json").write_text(
        json.dumps({
            "schema_version": 1,
            "mode": "native_text",
            "backend": "native_text",
            "total_pages": 1,
            "pages_processed": [1],
            "failed_pages": [],
            "empty_pages": [],
            "allowed_empty_pages": [],
            "missing_pages": [],
        }),
        encoding="utf-8",
    )
    (pages / "page_001.ocr.json").write_text(
        json.dumps({
            "schema_version": 1,
            "page_number": 1,
            "backend": "native_text",
            "source_kind": "native_text",
            "coordinate_system": "page_points",
            "page_box": [0, 0, 612, 792],
            "body_font_size": 10,
            "blocks": [
                _native_block("Body cites <sup>1</sup>.", 0, y0=90),
                {
                    "order": 1,
                    "label": "Footnote",
                    "bbox": [72, 680, 540, 770],
                    "html": f"1. {_FIRST}\n{_SECOND}\n{_THIRD}",
                    "text": f"1. {_FIRST}\n{_SECOND}\n{_THIRD}",
                    "font_size": 8,
                },
            ],
        }),
        encoding="utf-8",
    )
    (pages / "page_001.md").write_text(
        f"Body cites <sup>1</sup>.\n\n1. {_FIRST}\n{_SECOND}\n{_THIRD}\n",
        encoding="utf-8",
    )


def _prepare_book(tmp_path: Path) -> bytes:
    _write_native_page(tmp_path)
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    source_text = (
        f"Body cites <sup>1</sup>.\n\n1. {_FIRST}\n"
        f"    {_SECOND}\n    {_THIRD}\n"
    )
    source_path = source / "chapter_1.md"
    source_path.write_text(source_text, encoding="utf-8")
    (source / "tree_progress.json").write_text(
        json.dumps({"units": [{
            "unit_id": "chapter_1",
            "index_path": [1],
            "file": "chapter_1.md",
            "page_range": [1, 1],
        }]}),
        encoding="utf-8",
    )
    (tmp_path / "toc_tree.json").write_text("{}", encoding="utf-8")
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps({
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "decisions": [{
                "page": 1,
                "block": 1,
                "role": "footnote_start",
                "key": "1",
                "confidence": "high",
            }],
        }),
        encoding="utf-8",
    )
    prepare_footnote_subagent(tmp_path, book_title="Integrity Regression")
    # Reapply the exact accepted address after prepare has written its packet.
    (tmp_path / "footnote_decisions.json").write_text(
        json.dumps({
            "schema_version": FOOTNOTE_PREPARE_SCHEMA_VERSION,
            "decisions": [{
                "page": 1,
                "source": "primary",
                "block": 1,
                "role": "footnote_start",
                "key": "1",
                "confidence": "high",
            }],
        }),
        encoding="utf-8",
    )
    validation = validate_footnote_decisions(tmp_path)
    assert validation["valid"] is True, validation
    return source_path.read_bytes()


def _corrupt(note_text: str, corruption: str) -> str:
    lines = note_text.splitlines(keepends=True)
    second_index = next(index for index, line in enumerate(lines) if _SECOND in line)
    third_index = next(index for index, line in enumerate(lines) if _THIRD in line)
    if corruption == "drop":
        del lines[second_index]
        return "".join(lines)
    if corruption == "truncate":
        return note_text.replace(_THIRD, _THIRD[:24], 1)
    if corruption == "duplicate":
        lines.insert(second_index + 1, lines[second_index])
        return "".join(lines)
    if corruption == "misorder":
        lines[second_index], lines[third_index] = lines[third_index], lines[second_index]
        return "".join(lines)
    raise AssertionError(corruption)


@pytest.mark.parametrize("corruption", ["drop", "truncate", "duplicate", "misorder"])
@pytest.mark.parametrize("fault_location", ["writer", "transform"])
def test_apply_result_audit_rejects_mutated_multiline_note_materialization(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    corruption: str,
    fault_location: str,
):
    source_bytes = _prepare_book(tmp_path)
    original_write = footnote_apply._write_normalized_units
    resolved_address_seen: list[tuple[int, str, int]] = []
    original_resolution = footnote_apply.resolve_page_decisions
    original_transform = footnote_apply.transform_footnote_edit_plan

    def observe_resolution(*args, **kwargs):
        resolution = original_resolution(*args, **kwargs)
        resolved_address_seen.extend(
            (int(item["page"]), str(item.get("source") or "primary"), int(item["block"]))
            for item in resolution.resolved
        )
        return resolution

    def write_then_corrupt(source_dir, target_dir_arg, texts, source_hashes):
        source_inventory, target_inventory = original_write(
            source_dir, target_dir_arg, texts, source_hashes
        )
        path = target_dir_arg / "chapter_1.md"
        written = path.read_text(encoding="utf-8")
        assert "[^1]:" in written
        mutated = _corrupt(written, corruption)
        assert mutated != written
        path.write_text(mutated, encoding="utf-8")
        # Bind the mutated bytes into the receipt so status can only reject
        # this if result auditing checks the actual materialized content.
        target_inventory["chapter_1.md"] = hashlib.sha256(
            path.read_bytes()
        ).hexdigest()
        return source_inventory, target_inventory

    if fault_location == "transform":
        def transform_then_corrupt(*args, **kwargs):
            plan = original_transform(*args, **kwargs)
            accepted_parts = [
                str(part)
                for notes in plan.notes_by_chapter.values()
                for note in notes
                for part in note.get("parts", [])
            ]
            assert all(
                text in "\n".join(accepted_parts)
                for text in (_FIRST, _SECOND, _THIRD)
            )
            corrupted_texts = dict(plan.transformed_texts)
            corrupted_texts["chapter_1.md"] = _corrupt(
                corrupted_texts["chapter_1.md"], corruption
            )
            return replace(plan, transformed_texts=corrupted_texts)

        monkeypatch.setattr(
            footnote_apply, "transform_footnote_edit_plan", transform_then_corrupt
        )
    else:
        monkeypatch.setattr(footnote_apply, "_write_normalized_units", write_then_corrupt)

    monkeypatch.setattr(footnote_apply, "resolve_page_decisions", observe_resolution)
    result = apply_footnote_normalization(tmp_path)

    assert (1, "primary", 1) in resolved_address_seen
    assert (tmp_path / "ocr_markdown" / "chapter_1.md").read_bytes() == source_bytes
    assert result["valid"] is False
    assert footnote_normalization_status(tmp_path)["current"] is False
    checkpoint = json.loads(
        (tmp_path / "footnote_normalization.json").read_text(encoding="utf-8")
    )
    assert checkpoint["valid"] is False


def test_endnotes_only_reports_dangling_unclosed_declared_definition_without_page_scan(
    tmp_path: Path,
):
    source = tmp_path / "ocr_markdown"
    source.mkdir()
    (source / "chapter_1.md").write_text(
        "Chapter body has no note reference.\n", encoding="utf-8"
    )
    notes_text = "17. A declared note without a body reference remains open (\n"
    (source / "notes.md").write_text(notes_text, encoding="utf-8")
    (source / "tree_progress.json").write_text(
        json.dumps({"units": [
            {
                "unit_id": "chapter_1",
                "index_path": [1],
                "file": "chapter_1.md",
                "page_range": [1, 1],
            },
            {
                "unit_id": "notes",
                "index_path": [2],
                "file": "notes.md",
                "page_range": [2, 2],
                "type": "notes",
            },
        ]}),
        encoding="utf-8",
    )
    (tmp_path / "toc_tree.json").write_text(
        json.dumps({"footnote_mode": "endnotes_only"}), encoding="utf-8"
    )
    pages = tmp_path / "pages"
    pages.mkdir()
    (pages / "page_001.ocr.json").write_text(
        json.dumps({
            "schema_version": 1,
            "page_number": 1,
            "backend": "test",
            "page_box": [0, 0, 1000, 1000],
            "blocks": [],
        }),
        encoding="utf-8",
    )

    from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates

    prepared = prepare_footnote_candidates(
        tmp_path, config={"footnotes": {"mode": "endnotes_only"}}
    )
    assert prepared["pages"] == []
    assert prepared["footnote_mode"] == "endnotes_only"
    assert validate_footnote_decisions(
        tmp_path, config={"footnotes": {"mode": "endnotes_only"}}
    )["valid"] is True

    applied = apply_footnote_normalization(
        tmp_path, config={"footnotes": {"mode": "endnotes_only"}}
    )

    assert applied["valid"] is False
    assert footnote_normalization_status(
        tmp_path, config={"footnotes": {"mode": "endnotes_only"}}
    )["current"] is False
    audit = json.loads(
        (tmp_path / "footnote_result_audit.json").read_text(encoding="utf-8")
    )
    assert audit["mode"] == "endnotes_only"
    assert audit["status"] == "review_required"
    assert any(
        finding["code"] == "normalized_note_truncated"
        for finding in audit["findings"]
    )
    assert notes_text.strip() in (source / "notes.md").read_text(encoding="utf-8")


@pytest.mark.parametrize("evidence_origin", ["source_markdown", "normalized_target"])
def test_truncated_note_exception_validates_exact_markdown_evidence(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    evidence_origin: str,
):
    note_line = "[^17]: A declared note remains open ("
    note_bytes = (note_line + "\n").encode("utf-8")
    markdown_dir_name = "ocr_markdown" if evidence_origin == "source_markdown" else "footnote_normalized"
    markdown_dir = tmp_path / markdown_dir_name
    markdown_dir.mkdir()
    markdown_path = markdown_dir / "notes.md"
    markdown_path.write_bytes(note_bytes)

    image_path = tmp_path / "ocr_review_images" / "page_001.png"
    image_path.parent.mkdir()
    image_path.write_bytes(b"synthetic full-page review image")
    image_hash = hashlib.sha256(image_path.read_bytes()).hexdigest()
    image_manifest = tmp_path / "ocr_review_images.json"
    image_manifest.write_text(
        json.dumps({"source_sha256": "prepared-pdf-hash"}), encoding="utf-8"
    )
    image_manifest_hash = hashlib.sha256(image_manifest.read_bytes()).hexdigest()
    monkeypatch.setattr(ocr_correction, "review_images_are_current", lambda _root: True)

    task = {
        "task_id": "fra-truncated-note",
        "code": "normalized_note_truncated" if evidence_origin == "normalized_target" else "possibly_truncated_note",
        "file": "notes.md",
        "line": 1,
        "key": "17",
        "evidence_origin": evidence_origin,
        "evidence_sha256": hashlib.sha256(note_line.encode("utf-8")).hexdigest(),
        "source_task_id": "fra-original-source-task",
        "source_evidence_sha256": "original-source-evidence-hash",
        "pages": [1],
        "visual_files": {
            "1": {"path": "ocr_review_images/page_001.png", "sha256": image_hash}
        },
        "visual_image_manifest_sha256": image_manifest_hash,
        "source_pdf_sha256": "prepared-pdf-hash",
    }
    if evidence_origin == "source_markdown":
        task["evidence_file_sha256"] = hashlib.sha256(note_bytes).hexdigest()
    else:
        task["evidence_target_sha256"] = hashlib.sha256(note_bytes).hexdigest()
    review = {
        "task_id": task["task_id"],
        "evidence_sha256": task["evidence_sha256"],
        "outcome": "truncated_source_exception",
        "reason": "The page image shows the printed note ends at this point.",
        "evidence_pages": [1],
    }

    normalized, errors = validate_result_audit_reviews(
        tmp_path, [task], [review]
    )

    assert errors == []
    assert normalized == [{
        "task_id": task["task_id"],
        "evidence_sha256": task["evidence_sha256"],
        "outcome": "truncated_source_exception",
        "reason": review["reason"],
        "evidence_pages": [1],
        "source_task_id": task["source_task_id"],
        "source_evidence_sha256": task["source_evidence_sha256"],
    }]

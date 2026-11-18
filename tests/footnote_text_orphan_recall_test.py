from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import pytest

from pdf2epub.refine.footnote_handoff import prepare_footnote_subagent
from pdf2epub.refine.footnote_prepare import prepare_footnote_candidates


def _block(text: str, order: int, label: str = "Text", y: int = 100, *, x0: int = 80, x1: int = 920) -> dict[str, Any]:
    return {
        "order": order,
        "label": label,
        "bbox": [x0, y, x1, y + 50],
        "html": text,
    }


def _write_page(root: Path, page: int, blocks: list[dict[str, Any]]) -> None:
    pages = root / "pages"
    pages.mkdir(parents=True, exist_ok=True)
    (pages / f"page_{page:03d}.ocr.json").write_text(
        json.dumps({
            "schema_version": 1,
            "page_number": page,
            "backend": "test",
            "page_box": [0, 0, 1000, 1000],
            "blocks": blocks,
        }, ensure_ascii=False),
        encoding="utf-8",
    )


def _open_note_pages(*, inline_superscript: bool) -> list[tuple[int, list[dict[str, Any]]]]:
    inline = " <sup>41</sup>" if inline_superscript else ""
    return [
        (1, [
            _block(
                "36 The genuine philosophical substance undergoes its transforma-",
                0, "Footnote", 820,
            ),
        ]),
        (2, [
            _block("An unrelated paragraph discussing literary history.", 0, "Text", 120),
            _block(
                "tion of the genuine philosophical substance into objective spirit" + inline,
                1, "Text", 680,
            ),
            _block("42 A new note begins here.", 2, "Footnote", 760),
            _block("43 A second note begins here.", 3, "Footnote", 820),
        ]),
    ]


def _prepare(root: Path, pages: list[tuple[int, list[dict[str, Any]]]]) -> dict[str, Any]:
    for page, blocks in pages:
        _write_page(root, page, blocks)
    return prepare_footnote_candidates(root)


def test_open_last_note_plus_inline_sup_text_becomes_exact_visual_review_candidate(tmp_path: Path):
    report = _prepare(tmp_path, _open_note_pages(inline_superscript=True))
    page = next(item for item in report["pages"] if item["page"] == 2)
    candidate = next(item for item in page["candidates"] if item["block"] == 1)
    manifest = json.loads(
        (tmp_path / "footnote_visual_audit_manifest.json").read_text(encoding="utf-8")
    )

    assert candidate["source"] == "primary"
    assert candidate["confidence"] == "review"
    assert candidate.get("key") is None
    assert candidate.get("local_role") is None
    assert candidate["continuation_evidence"]["confidence"] == "low"
    semantic = candidate["continuation_evidence"]["semantic_comparison"]
    assert semantic["winner"] == "inconclusive"
    assert "inline_superscript_open_note_requires_visual_review" in semantic["signals"]
    assert all(item["block"] != 1 for item in page["suppressed_continuation_candidates"])
    assert not any(
        item.get("task_id", "").startswith("continuation-gap-")
        for item in manifest["tasks"]
    )
    region_tasks = [task for task in manifest["tasks"] if task.get("page") == 2]
    assert region_tasks
    assert any(1 in task.get("block_indices", []) for task in region_tasks)
    assert any(
        block.get("block") == 1 and block.get("candidate") is True
        for task in region_tasks for block in task.get("blocks", [])
    )

    source_dir = tmp_path / "ocr_markdown"
    source_dir.mkdir()
    (source_dir / "chapter_1.md").write_text(
        "A body paragraph with an in-text reference.\n", encoding="utf-8"
    )
    (source_dir / "tree_progress.json").write_text(json.dumps({
        "units": [{
            "unit_id": "chapter_1", "index_path": [1],
            "file": "chapter_1.md", "page_range": [1, 2],
        }],
    }), encoding="utf-8")
    prepare_footnote_subagent(tmp_path, book_title="Text Orphan Recall")
    handoff = json.loads((tmp_path / "footnote_subagent_manifest.json").read_text(encoding="utf-8"))
    context_path = tmp_path / handoff["unit_context_files"]["chapter_1.md"]
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert "2:primary:1" in context["candidates"]
    handoff_audit = json.loads(
        (tmp_path / handoff["visual_audit_manifest"]).read_text(encoding="utf-8")
    )
    assert any(
        1 in task.get("block_indices", [])
        for task in handoff_audit["tasks"]
        if task.get("page") == 2
    )


def test_closed_last_note_does_not_recall_inline_superscript_body(tmp_path: Path):
    pages = _open_note_pages(inline_superscript=True)
    pages[0] = (1, [_block(
        "36 The genuine philosophical substance reaches its final transformation.",
        0, "Footnote", 820,
    )])
    report = _prepare(tmp_path, pages)
    page = next(item for item in report["pages"] if item["page"] == 2)

    assert all(item["block"] != 1 for item in page["candidates"])
    suppressed = {item["block"]: item for item in page["suppressed_continuation_candidates"]}
    assert suppressed[1]["suppression_reason"] in {
        "complete_previous_note_without_strong_continuation",
        "body_context_outvotes_cross_page_continuation",
    }


def test_inline_superscript_in_another_column_is_not_recalled_as_continuation(tmp_path: Path):
    pages = _open_note_pages(inline_superscript=True)
    pages[0] = (1, [_block(
        "36 The genuine philosophical substance undergoes its transforma-",
        0, "Footnote", 820, x0=70, x1=450,
    )])
    pages[1][1][1] = _block(
        "tion of the genuine philosophical substance into objective spirit <sup>41</sup>",
        1, "Text", 680, x0=550, x1=930,
    )
    report = _prepare(tmp_path, pages)
    page = next(item for item in report["pages"] if item["page"] == 2)

    assert all(item["block"] != 1 for item in page["candidates"])
    assert any(item["block"] == 1 for item in page["suppressed_continuation_candidates"])


@pytest.mark.parametrize("label", ["Footer", "Citation"])
def test_inline_superscript_furniture_or_citation_is_not_recalled(tmp_path: Path, label: str):
    pages = _open_note_pages(inline_superscript=True)
    furniture_label = "Page-Footer" if label == "Footer" else label
    pages[1][1][1] = _block(
        "tion of the genuine philosophical substance into objective spirit <sup>41</sup>",
        1, furniture_label, 680,
    )
    report = _prepare(tmp_path, pages)
    page = next(item for item in report["pages"] if item["page"] == 2)

    assert all(item["block"] != 1 for item in page["candidates"])


def test_body_context_more_similar_than_prior_note_suppresses_inline_recall(tmp_path: Path):
    pages = _open_note_pages(inline_superscript=True)
    pages[1][1][0] = _block(
        "The genuine philosophical substance undergoes its transformation in this paragraph.",
        0, "Text", 640,
    )
    pages[1][1][1] = _block(
        "The genuine philosophical substance continues its transformation <sup>41</sup>.",
        1, "Text", 690,
    )
    report = _prepare(tmp_path, pages)
    page = next(item for item in report["pages"] if item["page"] == 2)

    assert all(item["block"] != 1 for item in page["candidates"])

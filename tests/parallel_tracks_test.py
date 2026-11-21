"""Parallel structure/translation-preparation readiness tracks.

The PDF structure phase runs ``refine-prepare`` and ``illustration-prepare``
as two independent tracks over the same ``pages/`` evidence, and the
translation-preparation phase runs the source-language EPUB build in parallel
with entity/TOC preparation once the polished source is current.
"""

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.commands import markdown_readiness
from pdf2epub.ocr_progress import new_progress
from pdf2epub.refine.illustration_prepare import (
    apply_illustration_bindings,
    prepare_illustration_subagent,
    validate_illustration_decisions,
)


def _write_plain_pages(output_dir: Path) -> None:
    pages_dir = output_dir / "pages"
    pages_dir.mkdir(parents=True)
    (pages_dir / "page_001.md").write_text(
        "The page should be read or transcribed.", encoding="utf-8"
    )
    (pages_dir / "page_001.ocr.json").write_text(
        json.dumps(
            {
                "schema_version": 1,
                "page_number": 1,
                "backend": "chandra",
                "page_box": [0, 0, 1000, 1000],
                "blocks": [
                    {
                        "order": 0,
                        "label": "Text",
                        "bbox": [100, 100, 900, 200],
                        "html": "The page should be read or transcribed.",
                    }
                ],
            }
        ),
        encoding="utf-8",
    )
    progress = new_progress(
        source_sha256="synthetic-pdf", total_pages=1, backend="chandra"
    )
    progress["pages_processed"] = [1]
    progress["missing_pages"] = []
    (pages_dir / "ocr_progress.json").write_text(json.dumps(progress), encoding="utf-8")


def test_illustration_prepare_without_toc_has_no_candidates(tmp_path: Path):
    """No TOC and no illustration candidate must not block the other track."""
    _write_plain_pages(tmp_path)

    paths = prepare_illustration_subagent(tmp_path, book_title="Book", config={})
    report = json.loads(paths["report"].read_text(encoding="utf-8"))
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))

    assert not (tmp_path / "toc_tree.json").exists()
    assert report["toc_tree_available"] is False
    assert report["candidate_pages"] == []
    assert report["status"] == "no_review_required"
    assert manifest["status"] == "no_review_required"

    validation = validate_illustration_decisions(tmp_path, config={})
    assert validation["valid"] is True
    applied = apply_illustration_bindings(tmp_path, config={})
    assert applied["valid"] is True
    assert applied["bindings"] == []


def _configure_readiness(monkeypatch, output_dir: Path, config: dict) -> None:
    monkeypatch.setattr(markdown_readiness, "load_config", lambda *_args: config)
    monkeypatch.setattr(markdown_readiness, "book_output_dir", lambda _title: output_dir)


def test_structure_stage_does_not_wait_for_toc_or_polish(tmp_path: Path, monkeypatch):
    _write_plain_pages(tmp_path)
    config = {
        "title": "Parallel Book",
        "pipeline": "epub_conversion",
        "footnotes": {"mode": "auto"},
        "ocr": {"secondary": {"enabled": False}},
    }
    _configure_readiness(monkeypatch, tmp_path, config)

    report = markdown_readiness.run_readiness_check("config.yaml", "structure")

    assert report["ready"] is True
    assert report["checks"]["ocr_pages"]["ready"] is True
    assert report["checks"]["toc_tree"]["ready"] is True
    assert report["checks"]["refine_local"]["ready"] is True
    assert "toc_tree.json" not in " ".join(report["errors"])


def test_translation_prep_stage_skips_entity_and_translated_toc_gates(
    tmp_path: Path, monkeypatch
):
    _write_plain_pages(tmp_path)
    (tmp_path / "ocr_markdown").mkdir()
    (tmp_path / "ocr_markdown" / "chapter_001.md").write_text(
        "Current refined source.", encoding="utf-8"
    )
    config = {
        "title": "Parallel Book",
        "pipeline": "epub_conversion",
        "footnotes": {"mode": "auto"},
        "ocr": {"secondary": {"enabled": False}},
    }
    _configure_readiness(monkeypatch, tmp_path, config)

    report = markdown_readiness.run_readiness_check("config.yaml", "translate-prep")

    # Entity extraction and TOC translation run inside this parallel track;
    # they are not prerequisites for starting it.
    assert report["checks"]["translation_entities"]["ready"] is True
    assert report["checks"]["translated_toc"]["ready"] is True
    # The polished source remains the gate for the source-EPUB build.
    assert report["checks"]["polish_required"]["ready"] is False
    assert report["ready"] is False

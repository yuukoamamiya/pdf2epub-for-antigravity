"""Read-only cross-page boundary preview for polish."""

from __future__ import annotations

import json
from pathlib import Path

from pdf2epub.refine.boundary_preview import build_boundary_preview, write_boundary_preview


def _fixture(tmp_path: Path, unit_text: str) -> tuple[Path, Path]:
    output = tmp_path / "output"
    pages = output / "pages"
    markdown = output / "ocr_markdown"
    pages.mkdir(parents=True)
    markdown.mkdir(parents=True)
    (markdown / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "file": "unit.md",
                        "part_files": ["unit.md"],
                        "page_range": [1, 2],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (markdown / "unit.md").write_text(unit_text, encoding="utf-8")
    (pages / "page_001.md").write_text(
        "Body text.\n17. A note ends.\n", encoding="utf-8"
    )
    (pages / "page_002.md").write_text(
        "Body text resumes.\n", encoding="utf-8"
    )
    return output, pages


def test_footnote_line_merged_into_body_is_reported(tmp_path: Path):
    output, pages = _fixture(
        tmp_path,
        "Body text.\n\n17. A note ends.\nBody text resumes.\n",
    )

    report = build_boundary_preview(output, pages)

    assert report["suspect_count"] == 1
    suspect = report["suspects"][0]
    assert suspect["unit"] == "unit.md"
    assert suspect["pages"] == [1, 2]
    assert suspect["severity"] == "high"
    assert "footnote_then_body_joined_without_paragraph_break" in suspect["reasons"]


def test_blank_line_between_pages_is_not_a_suspect(tmp_path: Path):
    output, pages = _fixture(
        tmp_path,
        "Body text.\n\n17. A note ends.\n\nBody text resumes.\n",
    )

    assert build_boundary_preview(output, pages)["suspect_count"] == 0


def test_write_boundary_preview_uses_the_documented_filename(tmp_path: Path):
    output, pages = _fixture(
        tmp_path,
        "Body text.\n\n17. A note ends.\nBody text resumes.\n",
    )

    write_boundary_preview(output, pages)

    saved = json.loads(
        (output / "refine_boundary_preview.json").read_text(encoding="utf-8")
    )
    assert saved["suspect_count"] == 1

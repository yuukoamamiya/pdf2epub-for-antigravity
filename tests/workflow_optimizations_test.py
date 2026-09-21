import json
from pathlib import Path

import pymupdf

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.pdf_text_probe import extract_native_text_pages, probe_pdf_text_layer
from pdf2epub.subagent_runtime import effective_max_concurrency, write_worker_handoffs
from pdf2epub.toc_translation_workflow import (
    build_toc_heading_contexts,
    validate_toc_heading_bindings,
)


def _make_native_pdf(path: Path, *, full_page_image: bool = False) -> None:
    document = pymupdf.open()
    for index in range(4):
        page = document.new_page()
        if full_page_image:
            pixmap = pymupdf.Pixmap(pymupdf.csRGB, (0, 0, 500, 700), False)
            pixmap.clear_with(240)
            page.insert_image(page.rect, pixmap=pixmap)
        for line_index in range(10):
            page.insert_text(
                (72, 72 + line_index * 35),
                f"Page {index + 1}, line {line_index}: Native vector text.",
            )
    document.save(path)
    document.close()


def test_effective_concurrency_throttles_large_pending_units():
    small, reason = effective_max_concurrency(
        {"small.md": {"estimated_tokens": 100}}, 3
    )
    one_large, one_reason = effective_max_concurrency(
        {"large.md": {"estimated_tokens": 12_000}}, 3
    )
    multiple_large, multiple_reason = effective_max_concurrency(
        {
            "large1.md": {"estimated_tokens": 12_000},
            "large2.md": {"estimated_tokens": 12_000},
        },
        3,
    )

    assert (small, reason) == (3, "no_large_units")
    assert (one_large, one_reason) == (2, "large_unit_present")
    assert (multiple_large, multiple_reason) == (1, "extreme_or_multiple_large_units")


def test_pdf_probe_only_accepts_clean_vector_text(tmp_path: Path):
    native = tmp_path / "native.pdf"
    searchable_ocr = tmp_path / "searchable-ocr.pdf"
    _make_native_pdf(native)
    _make_native_pdf(searchable_ocr, full_page_image=True)

    native_report = probe_pdf_text_layer(native)
    searchable_report = probe_pdf_text_layer(searchable_ocr)

    assert native_report["classification"] == "native_text"
    assert native_report["recommendation"] == "use_text_layer"
    assert searchable_report["classification"] == "searchable_ocr_or_mixed"
    assert searchable_report["recommendation"] == "ocr_required"


def test_native_text_extraction_writes_page_contract(tmp_path: Path):
    source = tmp_path / "native.pdf"
    _make_native_pdf(source)
    probe = probe_pdf_text_layer(source)
    extract_native_text_pages(source, tmp_path / "book", probe)

    progress = json.loads(
        (tmp_path / "book" / "pages" / "ocr_progress.json").read_text(encoding="utf-8")
    )
    assert progress["mode"] == "native_text"
    assert progress["pages_processed"] == [1, 2, 3, 4]
    assert (tmp_path / "book" / "pages" / "page_001.md").read_text(encoding="utf-8").strip()


def test_native_polish_prompt_requires_paragraph_reconstruction(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    target_dir = tmp_path / "polished_markdown"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "chapter_1.md").write_text(
        "A visual line\ncontinued in the same paragraph\n\nNew paragraph\n",
        encoding="utf-8",
    )
    (tmp_path / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "classification": "native_text",
                "recommendation": "use_text_layer",
            }
        ),
        encoding="utf-8",
    )

    paths = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        "English",
        "Chinese",
    )
    prompt = paths["prompt"].read_text(encoding="utf-8")
    assert "semantic paragraphs" in prompt
    assert "Never treat every extracted visual line" in prompt


def test_worker_handoffs_balance_pending_batches_and_keep_files_disjoint(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    for index in range(9):
        (source_dir / f"unit_{index}.md").write_text("text " * (index + 1), encoding="utf-8")
    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        config={
            "subagent": {
                "batching": {
                    "max_files": 2,
                    "max_source_tokens": 20,
                    "max_concurrency": 3,
                }
            }
        },
    )
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert len(handoffs) == 3
    assigned = [name for item in handoffs for name in item["files"]]
    assert len(assigned) == len(set(assigned)) == 9
    assert all(item["manifest"].startswith("worker_handoffs/") for item in handoffs)
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert len(manifest["worker_queue"]) == 3


def test_toc_heading_contexts_and_validation_use_first_unit_part(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "toc_tree_translated.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "title": "Chapter",
                        "children": [{"title": "Exact child", "anchor": "toc-1-1"}],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    progress_dir = output / "ocr_markdown"
    progress_dir.mkdir()
    (progress_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "index_path": [1],
                        "file": "chapter_1.part1.md",
                        "part_files": ["chapter_1.part1.md", "chapter_1.part2.md"],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (output / "translate_subagent_manifest.json").write_text(
        json.dumps(
            {
                "toc_heading_contexts": build_toc_heading_contexts(output)
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    validated = output / "translated" / "validated"
    validated.mkdir(parents=True)
    (validated / "chapter_1.part1.md").write_text(
        "# Chapter\n\nExact child\n", encoding="utf-8"
    )

    contexts = build_toc_heading_contexts(output)
    assert set(contexts) == {"chapter_1.part1.md"}
    assert validate_toc_heading_bindings(output)["valid"] is True

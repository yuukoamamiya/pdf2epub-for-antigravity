import hashlib
import json
from pathlib import Path

import pymupdf

from pdf2epub.markdown_handoff import prepare_markdown_subagent
from pdf2epub.pipeline_policy import PipelinePolicy
from pdf2epub.pdf_text_probe import extract_native_text_pages, probe_pdf_text_layer
from pdf2epub.subagent_runtime import (
    MAX_BATCH_FILES,
    effective_max_concurrency,
    write_worker_handoffs,
)
from pdf2epub.subagent_runtime import _batching_config
from pdf2epub.toc_translation_workflow import (
    build_global_toc_outline,
    build_toc_heading_contexts,
    validate_toc_heading_bindings,
)
from pdf2epub.utils.common import is_epub_conversion_pipeline
from pdf2epub.validation_receipts import validation_receipt_is_current


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


def test_epub_conversion_pipeline_is_language_neutral():
    assert is_epub_conversion_pipeline({"pipeline": "epub_conversion"}) is True
    assert is_epub_conversion_pipeline({"mode": "ocr_to_epub"}) is True
    assert is_epub_conversion_pipeline({"pipeline": "translation"}) is False
    assert is_epub_conversion_pipeline({}) is False


def test_pipeline_policy_centralizes_conversion_and_translation_requirements():
    conversion = PipelinePolicy.from_config({"mode": "ocr_to_epub"})
    assert conversion.kind == "epub_conversion"
    assert conversion.is_conversion is True
    assert conversion.requires_translation is False
    assert conversion.requires_entities is False
    assert conversion.requires_translated_toc is False
    assert conversion.requires_ocr_correction is False
    assert conversion.requires_polish is True
    assert conversion.source_language is None
    assert conversion.target_language is None

    translation = PipelinePolicy.from_config(
        {
            "pipeline": "translation",
            "translation": {
                "source_language": "German",
                "target_language": "Chinese",
                "require_entities": False,
            },
        }
    )
    assert translation.kind == "translation"
    assert translation.requires_translation is True
    assert translation.requires_entities is False
    assert translation.requires_translated_toc is True
    assert translation.source_language == "German"
    assert translation.target_language == "Chinese"

    consensus_translation = PipelinePolicy.from_config(
        {
            "translation": {"source_language": "German", "target_language": "Chinese"},
            "ocr": {"secondary": {"enabled": True, "backend": "paddle"}},
        }
    )
    assert consensus_translation.requires_ocr_correction is True


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
    sidecar = json.loads(
        (tmp_path / "book" / "pages" / "page_001.ocr.json").read_text(encoding="utf-8")
    )
    assert sidecar["source_kind"] == "native_text"
    assert sidecar["coordinate_system"] == "page_points"
    assert sidecar["page_box"][2] > 0
    assert sidecar["blocks"]
    assert json.loads(
        (tmp_path / "book" / "pages" / "page_stats.json").read_text(encoding="utf-8")
    )["1"]["artifact_file"] == "pages/page_001.ocr.json"


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


def test_validation_receipt_rejects_changed_staged_target(tmp_path: Path) -> None:
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    source = source_dir / "unit.md"
    target = target_dir / "unit.md"
    source.write_text("source", encoding="utf-8")
    target.write_text("translated", encoding="utf-8")
    report = tmp_path / "translate_validation.json"
    report.write_text(
        json.dumps(
            {
                "schema_version": 2,
                "task": "translate",
                "scope": "full",
                "all_passed": True,
                "source_sha256": {
                    source.name: hashlib.sha256(source.read_bytes()).hexdigest()
                },
                "target_sha256": {
                    target.name: hashlib.sha256(target.read_bytes()).hexdigest()
                },
            }
        ),
        encoding="utf-8",
    )

    assert validation_receipt_is_current(
        report, source_dir, target_dir, task="translate"
    )
    target.write_text("changed after validation", encoding="utf-8")
    assert not validation_receipt_is_current(
        report, source_dir, target_dir, task="translate"
    )


def test_continuation_worker_manifest_forbids_synthetic_opening_heading(
    tmp_path: Path,
) -> None:
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "chapter_3.part1.md").write_text(
        "# References\n\nPart one", encoding="utf-8"
    )
    (source_dir / "chapter_3.part2.md").write_text(
        "Entry continued", encoding="utf-8"
    )
    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "German",
        "Chinese",
        config={"subagent": {"batching": {"max_files": 1}}},
        chapter_groups={
            "chapter_3": ["chapter_3.part1.md", "chapter_3.part2.md"]
        },
    )
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    continuation = next(
        item for item in handoffs if item["files"] == ["chapter_3.part2.md"]
    )
    scoped = json.loads((tmp_path / continuation["manifest"]).read_text(encoding="utf-8"))
    prompt = (tmp_path / continuation["prompt"]).read_text(encoding="utf-8")

    assert scoped["is_continuation"] is True
    assert scoped["continuation_files"] == {
        "chapter_3.part2.md": {"is_continuation": True, "part_number": 2}
    }
    assert "do not add any Markdown heading at the file start" in prompt
    assert "'(continued)'" in prompt


def test_chapter_handoffs_keep_chapters_separate_and_aggregate_context_once(
    tmp_path: Path,
):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name in ("chapter_1.md", "chapter_1.1.md", "chapter_2.md"):
        (source_dir / name).write_text("source", encoding="utf-8")
    context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    context_dir.mkdir(parents=True)
    contexts = {
        "chapter_1.md": [{"kind": "book_entity", "original": "Hegel", "target": "黑格尔"}],
        "chapter_1.1.md": [{"kind": "book_entity", "original": "Marx", "target": "马克思"}],
        "chapter_2.md": [{"kind": "book_entity", "original": "Kant", "target": "康德"}],
    }
    context_files = {}
    for name, entries in contexts.items():
        path = context_dir / f"{Path(name).stem}.json"
        path.write_text(json.dumps({"entries": entries}, ensure_ascii=False), encoding="utf-8")
        context_files[name] = path

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        config={
            "subagent": {
                "batching": {
                    "max_concurrency": 3,
                    "pack_adjacent_chapters": False,
                }
            }
        },
        unit_context_files=context_files,
        heading_contexts={
            "chapter_1.md": {"toc_title": "Chapter One", "children": []},
            "chapter_2.md": {"toc_title": "Chapter Two", "children": []},
        },
        chapter_groups={
            "chapter_1": ["chapter_1.md", "chapter_1.1.md"],
            "chapter_2": ["chapter_2.md"],
        },
    )

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert len(handoffs) == 2
    assert {item["chapter_id"] for item in handoffs} == {"chapter_1", "chapter_2"}
    assert {tuple(item["files"]) for item in handoffs} == {
        ("chapter_1.md", "chapter_1.1.md"),
        ("chapter_2.md",),
    }
    first = next(item for item in handoffs if item["chapter_id"] == "chapter_1")
    first_manifest = json.loads((tmp_path / first["manifest"]).read_text(encoding="utf-8"))
    context_path = tmp_path / next(iter(first_manifest["worker_context_files"].values()))
    chapter_context = json.loads(context_path.read_text(encoding="utf-8"))
    assert chapter_context["selection"] == "chapter_sparse_direct_context"
    assert chapter_context["assigned_files"] == ["chapter_1.md", "chapter_1.1.md"]
    assert chapter_context["chapter_file_count"] == 2
    assert "chapter_files" not in chapter_context
    assert {entry["original"] for entry in chapter_context["entries"]} == {"Hegel", "Marx"}
    assert "Apply every entry in its `entries` list" in (
        tmp_path / first["prompt"]
    ).read_text(encoding="utf-8")
    first_prompt = (tmp_path / first["prompt"]).read_text(encoding="utf-8")
    assert "Heading binding checklist" in first_prompt
    assert '"toc_title": "Chapter One"' in first_prompt
    assert '"toc_title": "Chapter Two"' not in first_prompt
    assert "Unit-specific terminology contexts (read-only; use these for the matching file):\n- none" in first_prompt


def test_adjacent_short_chapters_pack_with_file_scoped_contexts(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    chapter_names = ["chapter_1.md", "chapter_2.md", "chapter_3.md"]
    for name in chapter_names:
        (source_dir / name).write_text("source", encoding="utf-8")

    context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    context_dir.mkdir(parents=True)
    context_files = {}
    for name, term in zip(chapter_names, ("Hegel", "Marx", "Kant")):
        path = context_dir / f"{Path(name).stem}.json"
        path.write_text(
            json.dumps(
                {"entries": [{"original": term, "target": term}]},
                ensure_ascii=False,
            ),
            encoding="utf-8",
        )
        context_files[name] = path

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        config={
            "subagent": {
                "batching": {
                    "max_files": 3,
                    "max_source_tokens": 100,
                    "max_chapters_per_worker": 2,
                    "max_concurrency": 3,
                }
            }
        },
        unit_context_files=context_files,
        chapter_groups={
            "chapter_1": ["chapter_1.md"],
            "chapter_2": ["chapter_2.md"],
            "chapter_3": ["chapter_3.md"],
        },
    )

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert [item["files"] for item in handoffs] == [
        ["chapter_1.md", "chapter_2.md"],
        ["chapter_3.md"],
    ]
    assert handoffs[0]["chapter_ids"] == ["chapter_1", "chapter_2"]
    assert handoffs[0]["chapter_count"] == 2
    scoped = json.loads((tmp_path / handoffs[0]["manifest"]).read_text(encoding="utf-8"))
    assert scoped["chapter_ids"] == ["chapter_1", "chapter_2"]
    assert scoped["chapter_file_counts"] == {"chapter_1": 1, "chapter_2": 1}
    context_path = tmp_path / next(iter(scoped["worker_context_files"].values()))
    context = json.loads(context_path.read_text(encoding="utf-8"))
    assert context["selection"] == "adjacent_chapter_direct_file_contexts"
    assert {entry["original"] for entry in context["files"]["chapter_1.md"]} == {"Hegel"}
    assert {entry["original"] for entry in context["files"]["chapter_2.md"]} == {"Marx"}
    prompt = (tmp_path / handoffs[0]["prompt"]).read_text(encoding="utf-8")
    assert "do not let terms leak across files" in prompt


def test_large_chapter_splits_without_mixing_chapter_contexts(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name in ("chapter_1.1.md", "chapter_1.2.md", "chapter_2.md"):
        (source_dir / name).write_text("source", encoding="utf-8")
    context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    context_dir.mkdir(parents=True)
    context_files = {}
    for name, term in (
        ("chapter_1.1.md", "Hegel"),
        ("chapter_1.2.md", "Marx"),
        ("chapter_2.md", "Kant"),
    ):
        path = context_dir / f"{Path(name).stem}.json"
        path.write_text(
            json.dumps({"entries": [{"original": term, "target": term}]}, ensure_ascii=False),
            encoding="utf-8",
        )
        context_files[name] = path

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        config={"subagent": {"batching": {"max_files": 1}}},
        unit_context_files=context_files,
        chapter_groups={
            "chapter_1": ["chapter_1.1.md", "chapter_1.2.md"],
            "chapter_2": ["chapter_2.md"],
        },
    )

    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    chapter_one = [item for item in handoffs if item["chapter_id"] == "chapter_1"]
    assert len(chapter_one) == 2
    assert all(
        item["chapter_file_count"] == 2
        for item in chapter_one
    )
    for item in chapter_one:
        scoped = json.loads(
            (tmp_path / item["manifest"]).read_text(encoding="utf-8")
        )
        context = json.loads(
            (
                tmp_path
                / next(iter(scoped["worker_context_files"].values()))
            ).read_text(encoding="utf-8")
        )
        assert context["chapter_file_count"] == 2
        assert "chapter_files" not in context
        assert context["selection"] == "chapter_shared_local_direct_context"
        assert context["shared_entries"] == []
        assigned_term = "Hegel" if item["files"] == ["chapter_1.1.md"] else "Marx"
        assert [entry["original"] for entry in context["local_entries"]] == [assigned_term]


def test_worker_manifest_is_a_minimal_assignment_projection(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    for name in ("a.md", "b.md", "c.md"):
        (source_dir / name).write_text("source", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        file_contexts={
            "a.md": "Chapter A",
            "b.md": "Chapter B",
            "c.md": "Chapter C",
        },
        chapter_groups={"chapter": ["a.md", "b.md", "c.md"]},
    )
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert len(handoffs) == 1
    scoped = json.loads((tmp_path / handoffs[0]["manifest"]).read_text(encoding="utf-8"))
    assert scoped["files"] == ["a.md", "b.md", "c.md"]
    assert set(scoped["file_stats"]) == set(scoped["assigned_files"])
    assert set(scoped["file_contexts"]) == set(scoped["assigned_files"])
    assert "chapter_groups" not in scoped
    assert "recommended_batches" not in scoped
    assert "worker_handoffs" not in scoped


def test_worker_context_keeps_matching_fields_but_drops_audit_metadata(tmp_path: Path):
    source_dir = tmp_path / "source"
    source_dir.mkdir()
    (source_dir / "unit.md").write_text("Baldur's Gate", encoding="utf-8")
    context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    context_dir.mkdir(parents=True)
    context = {
        "entries": [
            {
                "kind": "domain",
                "source": "Baldur's Gate",
                "target": "《博德之门》",
                "policy": "fixed",
                "variants": ["Baldur's Gate series"],
                "aliases": ["BG"],
                "id": "crpg.baldurs-gate",
                "category": "game",
                "scope": "series",
                "note": "x" * 1000,
            }
        ]
    }
    context_path = context_dir / "unit.json"
    context_path.write_text(json.dumps(context, ensure_ascii=False), encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        tmp_path / "target",
        "English",
        "Chinese",
        unit_context_files={"unit.md": context_path},
    )
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])
    scoped = json.loads((tmp_path / handoffs[0]["manifest"]).read_text(encoding="utf-8"))
    worker_context = json.loads(
        (tmp_path / next(iter(scoped["worker_context_files"].values()))).read_text(
            encoding="utf-8"
        )
    )
    entry = worker_context["files"]["unit.md"][0]
    assert entry == {
        "source": "Baldur's Gate",
        "target": "《博德之门》",
        "kind": "domain",
        "policy": "fixed",
        "variants": ["Baldur's Gate series"],
        "aliases": ["BG"],
    }


def test_resume_only_invalidates_units_with_changed_context(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    context_dir = tmp_path / "translation_glossaries" / "unit_contexts"
    source_dir.mkdir()
    target_dir.mkdir()
    context_dir.mkdir(parents=True)
    for name in ("a.md", "b.md"):
        (source_dir / name).write_text(f"source {name}", encoding="utf-8")

    context_files = {}
    for name, term in (("a.md", "Alpha"), ("b.md", "Beta")):
        path = context_dir / f"{Path(name).stem}.json"
        path.write_text(json.dumps({"entries": [{"source": term, "target": term}]}), encoding="utf-8")
        context_files[name] = path

    first = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        unit_context_files=context_files,
    )
    validation_records = {}
    for name in ("a.md", "b.md"):
        target = target_dir / name
        target.write_text("validated", encoding="utf-8")
        validation_records[name] = {
            "valid": True,
            "source_sha256": hashlib.sha256((source_dir / name).read_bytes()).hexdigest(),
            "target_sha256": hashlib.sha256(target.read_bytes()).hexdigest(),
        }
    (tmp_path / "translate_file_validation.json").write_text(
        json.dumps({"schema_version": 2, "files": validation_records}), encoding="utf-8"
    )

    # Change only the projected context for a.md.  The external/full context
    # snapshot is intentionally not part of this test: resume is unit-scoped.
    context_files["a.md"].write_text(
        json.dumps({"entries": [{"source": "Alpha", "target": "阿尔法"}]}),
        encoding="utf-8",
    )
    resumed = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        resume=True,
        unit_context_files=context_files,
    )
    manifest = json.loads(resumed["manifest"].read_text(encoding="utf-8"))
    assert manifest["pending_files"] == ["a.md"]
    assert manifest["completed_files"] == ["b.md"]


def test_polish_worker_handoffs_are_task_scoped(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    for index in range(6):
        (source_dir / f"unit_{index}.md").write_text("polish me", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "polish",
        source_dir,
        target_dir,
        "Original",
        "Original",
        config={"subagent": {"batching": {"max_files": 2, "max_concurrency": 3}}},
    )
    handoffs = write_worker_handoffs(tmp_path, paths["manifest"], paths["prompt"])

    assert len(handoffs) == 3
    assert all(item["manifest"].startswith("polish_worker_handoffs/") for item in handoffs)
    assert all(
        (tmp_path / item["manifest"]).name.startswith("polish_subagent_manifest_")
        for item in handoffs
    )


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


def test_toc_heading_binding_allows_whitespace_and_outer_quote_variants(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "translate_subagent_manifest.json").write_text(
        json.dumps(
            {
                "toc_heading_contexts": {
                    "chapter.md": {
                        "toc_title": "Chapter One",
                        "children": [
                            {"title": "Exact Child", "anchor": "toc-1-1"}
                        ],
                    }
                }
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )
    validated = output / "translated" / "validated"
    validated.mkdir(parents=True)
    (validated / "chapter.md").write_text(
        "# Chapter  One\n\n《Exact Child》\n", encoding="utf-8"
    )

    report = validate_toc_heading_bindings(output)

    assert report["valid"] is True
    assert len(report["normalized_matches"]) == 2
    assert {item["match"] for item in report["checked"]} == {"normalized"}


def test_batching_caps_configured_worker_file_count():
    batching = _batching_config(
        {"subagent": {"batching": {"max_files": MAX_BATCH_FILES + 20}}}
    )

    assert batching["max_files"] == MAX_BATCH_FILES == 8
    assert batching["pack_adjacent_chapters"] is True
    assert batching["max_chapters_per_worker"] == 3


def test_global_toc_outline_keeps_complete_small_tree(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    (output / "toc_tree_translated.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "title": "Part One",
                        "children": [
                            {"title": "Chapter One", "children": []},
                            {"title": "Chapter Two", "children": []},
                        ],
                    }
                ]
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    outline = build_global_toc_outline(output, token_budget=200)

    assert "- Part One" in outline
    assert "  - Chapter One" in outline
    assert "  - Chapter Two" in outline
    assert "deeper entries" not in outline


def test_global_toc_outline_adapts_to_deep_and_wide_trees(tmp_path: Path):
    output = tmp_path / "output"
    output.mkdir()
    deep = {"title": "Level 5", "children": []}
    for index in range(4, 0, -1):
        deep = {"title": f"Level {index}", "children": [deep]}
    roots = [
        {"title": f"Top {index}", "children": [deep]}
        for index in range(1, 12)
    ]
    (output / "toc_tree_translated.json").write_text(
        json.dumps({"chapters": roots}, ensure_ascii=False), encoding="utf-8"
    )

    outline = build_global_toc_outline(output, token_budget=35)

    assert "- Top 1" in outline
    assert "deeper entries" in outline or "top-level entries omitted" in outline
    assert "Level 5" not in outline


def test_global_toc_outline_is_injected_into_prompt_and_hashed(tmp_path: Path):
    source_dir = tmp_path / "source"
    target_dir = tmp_path / "target"
    source_dir.mkdir()
    target_dir.mkdir()
    (source_dir / "chapter_1.md").write_text("source", encoding="utf-8")

    paths = prepare_markdown_subagent(
        tmp_path,
        "translate",
        source_dir,
        target_dir,
        "English",
        "Chinese",
        global_toc_outline="- Part One\n  - Chapter One",
    )

    prompt = paths["prompt"].read_text(encoding="utf-8")
    manifest = json.loads(paths["manifest"].read_text(encoding="utf-8"))
    assert "Global book outline (orientation only" in prompt
    assert "- Part One\n  - Chapter One" in prompt
    assert manifest["global_toc_outline_sha256"]


def test_global_toc_token_budget_is_configurable():
    batching = _batching_config(
        {"subagent": {"batching": {"global_toc_tokens": 321}}}
    )

    assert batching["global_toc_tokens"] == 321

import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands import __all__ as command_modules
from pdf2epub.commands import html as html_commands
from pdf2epub.commands.markdown import (
    _load_pdf_file_contexts,
    _load_pdf_file_roles,
)
from pdf2epub.commands.pdf import _resolve_pdf_markdown_source
from pdf2epub.commands.registry import register_command_parsers
from pdf2epub.commands.runtime import load_book_context
from pdf2epub.workflow_contracts import sha256_file


def test_command_package_exposes_workflow_modules_without_eager_exports():
    assert command_modules == [
        "entities",
        "glossary",
        "html",
        "markdown",
        "novel",
        "ocr",
        "pdf",
        "refine",
        "runtime",
        "sources",
        "tex",
        "toc",
    ]


def test_command_registry_registers_every_workflow_command():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    register_command_parsers(subparsers)

    assert set(subparsers.choices) == {
        "build-epub",
        "build-html-epub",
        "build-novel-epub",
        "check-ready",
        "extract-entities",
        "extract-entities-validate",
        "glossary-candidates",
        "html-prepare",
        "html-validate",
        "ocr-pages",
        "polish",
        "polish-validate",
        "refine",
        "refine-local",
        "refine-prepare",
        "translate",
        "translate-arxiv",
        "translate-arxiv-validate",
        "translate-novel",
        "translate-novel-validate",
        "translate-toc",
        "translate-toc-validate",
        "translate-validate",
    }
    assert all(
        subparser.get_default("func") is not None
        for subparser in subparsers.choices.values()
    )


def test_pdf_source_stage_selects_only_current_polished_output(tmp_path: Path):
    ocr_dir = tmp_path / "ocr_markdown"
    polished_dir = tmp_path / "polished_markdown" / "validated"
    ocr_dir.mkdir()
    polished_dir.mkdir(parents=True)
    source = ocr_dir / "chapter_1.md"
    polished = polished_dir / source.name
    source.write_text("source", encoding="utf-8")

    assert _resolve_pdf_markdown_source(
        tmp_path, {"translation": {"source_stage": "auto"}}
    ) == (ocr_dir, "ocr")

    polished.write_text("polished", encoding="utf-8")
    (tmp_path / "polish_validation.json").write_text(
        json.dumps(
            {
                "all_passed": True,
                "source_sha256": {source.name: sha256_file(source)},
            }
        ),
        encoding="utf-8",
    )
    assert _resolve_pdf_markdown_source(
        tmp_path, {"translation": {"source_stage": "auto"}}
    ) == (polished_dir, "polished")

def test_pdf_source_stage_defaults_to_polished(tmp_path: Path):
    polished_dir = tmp_path / "polished_markdown" / "validated"
    ocr_dir = tmp_path / "ocr_markdown"
    polished_dir.mkdir(parents=True)
    ocr_dir.mkdir()
    (ocr_dir / "chapter_1.md").write_text("ocr", encoding="utf-8")

    assert _resolve_pdf_markdown_source(tmp_path, {}) == (polished_dir, "polished")


def test_pdf_markdown_context_helpers_propagate_roles_and_hierarchy(tmp_path: Path):
    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    (tmp_path / "toc_tree.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {
                        "title": "Part I",
                        "children": [{"title": "References"}],
                    }
                ]
            }
        ),
        encoding="utf-8",
    )
    (ocr_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {
                        "file": "chapter_1.1.md",
                        "part_files": ["chapter_1.1.part1.md"],
                        "type": "bibliography",
                    }
                ]
            }
        ),
        encoding="utf-8",
    )

    assert _load_pdf_file_roles(tmp_path) == {
        "chapter_1.1.part1.md": "bibliography"
    }
    contexts = _load_pdf_file_contexts(tmp_path)
    assert contexts["chapter_1.1.md"] == "Part I → References"
    assert contexts["chapter_1.1.part1.md"] == "Part I → References"


def test_pdf_source_stage_rejects_unknown_mode(tmp_path: Path):
    with pytest.raises(ValueError, match="source_stage"):
        _resolve_pdf_markdown_source(
            tmp_path, {"translation": {"source_stage": "unknown"}}
        )


def test_book_command_context_unifies_config_path_logging_and_output_dir(
    tmp_path: Path, monkeypatch
):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("title: Context Book\n", encoding="utf-8")
    logged = []
    output_dir = tmp_path / "output" / "Context Book"

    monkeypatch.setattr(
        "pdf2epub.commands.runtime.configure_logging",
        lambda title, operation: logged.append((title, operation)),
    )
    monkeypatch.setattr(
        "pdf2epub.commands.runtime.book_output_dir",
        lambda title: output_dir,
    )

    context = load_book_context(
        SimpleNamespace(config=str(config_path)), "test-command"
    )

    assert context is not None
    assert context.config["title"] == "Context Book"
    assert context.config_path == config_path
    assert context.book_title == "Context Book"
    assert context.output_dir == output_dir
    assert logged == [("Context Book", "test-command")]


def test_html_commands_share_input_and_output_context(tmp_path: Path, monkeypatch):
    config_path = tmp_path / "config.yaml"
    config_path.write_text("title: HTML Book\n", encoding="utf-8")
    input_epub = tmp_path / "book.epub"
    input_epub.write_bytes(b"epub")
    output_dir = tmp_path / "output" / "HTML Book"
    logged = []

    monkeypatch.setattr(
        html_commands,
        "book_output_dir",
        lambda title: tmp_path / "output" / title,
    )
    monkeypatch.setattr(
        html_commands,
        "configure_logging",
        lambda title, operation: logged.append((title, operation)),
    )

    context = html_commands._load_html_book_context(
        SimpleNamespace(config=str(config_path), input=str(input_epub)),
        "html-validate",
    )

    assert context == (
        {"title": "HTML Book"},
        "HTML Book",
        output_dir,
        input_epub.resolve(),
    )
    assert logged == [("HTML Book", "html-validate")]

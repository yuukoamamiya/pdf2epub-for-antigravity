import argparse
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from pdf2epub.commands import __all__ as command_modules
from pdf2epub.commands import html as html_commands
import pdf2epub.commands.registry as command_registry
from pdf2epub.commands.markdown_context import _load_pdf_file_contexts
from pdf2epub.commands.pdf import _resolve_pdf_markdown_source
from pdf2epub.pdf_unit_roles import load_pdf_file_roles
from pdf2epub.commands.sources import _resolve_pdf_polish_source
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
        "page_furniture",
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
        "doctor",
        "extract-entities",
        "extract-entities-validate",
        "footnote-apply",
        "footnote-prepare",
        "footnote-validate",
        "illustration-apply",
        "illustration-prepare",
        "illustration-validate",
        "glossary-candidates",
            "html-prepare",
            "html-skeleton-retry",
            "html-skeleton-restore",
            "html-validate",
        "ocr-pages",
        "ocr-consensus-rebuild",
        "ocr-correct",
        "ocr-correct-validate",
        "repair-page-furniture",
        "repair-page-furniture-validate",
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
        "status",
    }
    assert all(
        subparser.get_default("func") is not None
        for subparser in subparsers.choices.values()
    )


def test_command_registry_defers_workflow_imports(monkeypatch):
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")

    def fail_import(*args, **kwargs):
        raise AssertionError("workflow modules should load only when a command runs")

    monkeypatch.setattr(command_registry, "import_module", fail_import)
    register_command_parsers(subparsers)
def test_polish_validate_parser_accepts_single_file_checkpoint():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_command_parsers(subparsers)

    args = parser.parse_args(["polish-validate", "--file", "chapter_8.5.md"])

    assert args.file == "chapter_8.5.md"


def test_polish_validate_parser_accepts_check_only():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_command_parsers(subparsers)

    args = parser.parse_args(
        ["polish-validate", "--file", "chapter_8.5.md", "--check-only"]
    )

    assert args.file == "chapter_8.5.md"
    assert args.check_only is True


def test_polish_validate_check_only_requires_file():
    from pdf2epub.commands import markdown as markdown_commands

    args = SimpleNamespace(check_only=True, file=None)

    assert markdown_commands.polish_validate_command(args) == 1


def test_polish_validate_check_only_writes_no_checkpoint_or_validated_copy(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    from pdf2epub.commands import markdown as markdown_commands

    output_dir = tmp_path / "output"
    source_dir = output_dir / "ocr_markdown"
    target_dir = output_dir / "polished_markdown"
    source_dir.mkdir(parents=True)
    target_dir.mkdir(parents=True)
    text = "A short but complete paragraph for read-only polish validation.\n"
    (source_dir / "unit.md").write_text(text, encoding="utf-8")
    (target_dir / "unit.md").write_text(text, encoding="utf-8")
    context = SimpleNamespace(
        config={"footnotes": {"mode": "disabled"}},
        book_title="Book",
        output_dir=output_dir,
        config_path=tmp_path / "config.yaml",
    )
    monkeypatch.setattr(
        markdown_commands, "load_book_context", lambda args, stage: context
    )
    monkeypatch.setattr(
        markdown_commands,
        "_resolve_pdf_polish_source",
        lambda output, config: (source_dir, "ocr"),
    )
    monkeypatch.setattr(markdown_commands, "load_pdf_file_roles", lambda output: {})
    monkeypatch.setattr(markdown_commands, "_load_pdf_toc_titles", lambda output: [])
    monkeypatch.setattr(
        markdown_commands, "_load_pdf_continuation_toc_titles", lambda output: {}
    )
    args = SimpleNamespace(
        check_only=True,
        file="unit.md",
        allow_review_warnings=False,
    )

    assert markdown_commands._validate_pdf_markdown_task(args, "polish") == 0
    checkpoint = output_dir / "polish_file_validation.json"
    assert not checkpoint.exists()
    assert not (target_dir / "validated" / "unit.md").exists()

    sentinel = {"schema_version": 99, "task": "polish", "files": {"unit.md": "old"}}
    checkpoint.write_text(json.dumps(sentinel), encoding="utf-8")
    assert markdown_commands._validate_pdf_markdown_task(args, "polish") == 0
    assert json.loads(checkpoint.read_text(encoding="utf-8")) == sentinel


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
                "schema_version": 2,
                "task": "polish",
                "scope": "full",
                "all_passed": True,
                "source_sha256": {source.name: sha256_file(source)},
                "target_sha256": {polished.name: sha256_file(polished)},
            }
        ),
        encoding="utf-8",
    )
    assert _resolve_pdf_markdown_source(
        tmp_path, {"translation": {"source_stage": "auto"}}
    ) == (polished_dir, "polished")


def test_polish_prefers_current_footnote_normalized_source(tmp_path: Path):
    source_dir = tmp_path / "ocr_markdown"
    normalized_dir = tmp_path / "footnote_normalized"
    source_dir.mkdir()
    normalized_dir.mkdir()
    (source_dir / "chapter_1.md").write_text("source", encoding="utf-8")
    (normalized_dir / "chapter_1.md").write_text("normalized", encoding="utf-8")
    (tmp_path / "footnote_candidates.json").write_text("{}", encoding="utf-8")
    (tmp_path / "footnote_decision_validation.json").write_text(
        json.dumps({"valid": True, "status": "validated"}), encoding="utf-8"
    )
    from pdf2epub.workflow_contracts import atomic_write_text

    atomic_write_text(
        tmp_path / "footnote_normalization.json",
        json.dumps(
            {
                "schema_version": 2,
                "valid": True,
                "status": "validated",
                "source_sha256": {"chapter_1.md": sha256_file(source_dir / "chapter_1.md")},
                "target_sha256": {
                    "chapter_1.md": sha256_file(normalized_dir / "chapter_1.md")
                },
                "candidate_report_sha256": sha256_file(
                    tmp_path / "footnote_candidates.json"
                ),
                "decision_validation_sha256": sha256_file(
                    tmp_path / "footnote_decision_validation.json"
                ),
            }
        ),
    )

    assert _resolve_pdf_polish_source(tmp_path) == (normalized_dir, "footnote_normalized")
    assert _resolve_pdf_polish_source(
        tmp_path, {"footnotes": {"mode": "disabled"}}
    ) == (source_dir, "ocr")


def test_pdf_source_stage_auto_falls_back_to_ocr_without_native_probe(tmp_path: Path):
    polished_dir = tmp_path / "polished_markdown" / "validated"
    ocr_dir = tmp_path / "ocr_markdown"
    polished_dir.mkdir(parents=True)
    ocr_dir.mkdir()
    (ocr_dir / "chapter_1.md").write_text("ocr", encoding="utf-8")

    assert _resolve_pdf_markdown_source(tmp_path, {}) == (ocr_dir, "ocr")


def test_native_text_probe_does_not_bypass_polished_source(tmp_path: Path):
    ocr_dir = tmp_path / "ocr_markdown"
    pages_dir = tmp_path / "pages"
    ocr_dir.mkdir()
    pages_dir.mkdir()
    (ocr_dir / "chapter_1.md").write_text("raw native layout", encoding="utf-8")
    (pages_dir / "page_001.md").write_text("raw native layout", encoding="utf-8")
    (tmp_path / "pdf_text_probe.json").write_text(
        json.dumps(
            {
                "classification": "native_text",
                "recommendation": "use_text_layer",
            }
        ),
        encoding="utf-8",
    )
    (pages_dir / "ocr_progress.json").write_text(
        json.dumps({"mode": "native_text", "failed_pages": []}),
        encoding="utf-8",
    )

    # Native extraction skips visual OCR, but the raw page/layout source is
    # still not a translation-ready semantic-paragraph source.
    assert _resolve_pdf_markdown_source(tmp_path, {}) == (ocr_dir, "ocr")


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

    assert load_pdf_file_roles(tmp_path) == {
        "chapter_1.1.part1.md": "bibliography"
    }
    contexts = _load_pdf_file_contexts(tmp_path)
    assert contexts["chapter_1.1.md"] == "Part I → References"
    assert contexts["chapter_1.1.part1.md"] == "Part I → References"


def test_pdf_file_roles_read_copyright_and_frontmatter_from_tree_progress(
    tmp_path: Path,
):
    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    (ocr_dir / "tree_progress.json").write_text(
        json.dumps(
            {
                "units": [
                    {"file": "chapter_1.md", "type": "copyright"},
                    {"file": "chapter_2.md", "type": "frontmatter"},
                    {"file": "chapter_3.md", "type": "Chapter"},
                ]
            }
        ),
        encoding="utf-8",
    )

    assert load_pdf_file_roles(tmp_path) == {
        "chapter_1.md": "copyright",
        "chapter_2.md": "frontmatter",
    }


def test_pdf_file_roles_read_copyright_from_toc_fallback(tmp_path: Path):
    ocr_dir = tmp_path / "ocr_markdown"
    ocr_dir.mkdir()
    (ocr_dir / "tree_progress.json").write_text(
        json.dumps({"units": [{"file": "chapter_1.md"}]}), encoding="utf-8"
    )
    (tmp_path / "toc_tree.json").write_text(
        json.dumps(
            {
                "chapters": [
                    {"title": "Copyright", "type": "copyright"},
                    {"title": "Preface", "type": "frontmatter"},
                ]
            }
        ),
        encoding="utf-8",
    )

    roles = load_pdf_file_roles(tmp_path)

    assert roles["chapter_1.md"] == "copyright"
    assert roles["chapter_2.md"] == "frontmatter"


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

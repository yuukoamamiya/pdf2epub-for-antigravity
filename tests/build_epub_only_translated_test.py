import argparse
from pathlib import Path
from types import SimpleNamespace

import pytest
from loguru import logger

import pdf2epub.build_epub
import pdf2epub.commands.pdf as pdf_commands
from pdf2epub.commands.registry import register_command_parsers


@pytest.fixture
def log_messages():
    messages = []
    sink_id = logger.add(lambda message: messages.append(message.record["message"]))
    try:
        yield messages
    finally:
        logger.remove(sink_id)


def _args(**overrides):
    defaults = {
        "translated": False,
        "only_translated": False,
        "cover": None,
    }
    defaults.update(overrides)
    return SimpleNamespace(**defaults)


def _prepare_command(tmp_path, monkeypatch, build_impl, config=None):
    output_dir = tmp_path / "output"
    source_dir = output_dir / "polished_markdown" / "validated"
    source_dir.mkdir(parents=True)
    (source_dir / "chapter.md").write_text("source body\n", encoding="utf-8")
    translated_dir = output_dir / "translated" / "validated"
    translated_dir.mkdir(parents=True)
    (translated_dir / "chapter.md").write_text("translated body\n", encoding="utf-8")
    (output_dir / "toc_tree.json").write_text("{}", encoding="utf-8")

    context = SimpleNamespace(
        config=config or {"title": "Book", "pipeline": "translation"},
        config_path=tmp_path / "config.yaml",
        book_title="Book",
        output_dir=output_dir,
    )

    monkeypatch.setattr(pdf_commands, "load_book_context", lambda *a, **k: context)
    monkeypatch.setattr(
        pdf_commands,
        "_resolve_pdf_markdown_source",
        lambda *a, **k: (source_dir, "polished"),
    )
    monkeypatch.setattr(
        pdf_commands,
        "_resolve_pdf_polish_source",
        lambda *a, **k: (source_dir, "polished"),
    )
    monkeypatch.setattr(
        pdf_commands, "_validate_pdf_source_stage", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        pdf_commands, "_validate_translated_pdf", lambda *a, **k: 0
    )
    monkeypatch.setattr(
        pdf_commands,
        "_warn_if_translated_checkpoint_is_ignored",
        lambda *a, **k: False,
    )
    monkeypatch.setattr(pdf_commands, "_load_build_epub", lambda: build_impl)
    return output_dir


def _parser():
    parser = argparse.ArgumentParser()
    subparsers = parser.add_subparsers(dest="command")
    register_command_parsers(subparsers)
    return parser


@pytest.mark.parametrize("flag", ["--only-translated", "--target-only"])
def test_parser_accepts_only_translated_flags(flag):
    args = _parser().parse_args(["build-epub", flag])
    assert args.only_translated is True


def test_parser_defaults_only_translated_to_false():
    args = _parser().parse_args(["build-epub"])
    assert args.only_translated is False


def test_translated_mode_continues_after_english_build_failure(
    tmp_path: Path, monkeypatch, log_messages
):
    calls = []

    def build(config):
        calls.append(config.translated)
        if not config.translated:
            raise RuntimeError("english build exploded")
        return tmp_path / "book.epub"

    _prepare_command(tmp_path, monkeypatch, build)

    result = pdf_commands.build_epub_command(_args(translated=True))

    assert result == 1
    assert calls == [False, True]
    assert any("[English Build Failed]" in message for message in log_messages)
    assert any(
        "Translated EPUB created" in message for message in log_messages
    )


def test_only_translated_skips_english_build(
    tmp_path: Path, monkeypatch, log_messages
):
    calls = []

    def build(config):
        calls.append(config.translated)
        return tmp_path / "book.epub"

    _prepare_command(tmp_path, monkeypatch, build)

    result = pdf_commands.build_epub_command(
        _args(translated=True, only_translated=True)
    )

    assert result == 0
    assert calls == [True]
    assert not any("English EPUB created" in message for message in log_messages)


def test_translated_build_failure_is_reported(
    tmp_path: Path, monkeypatch, log_messages
):
    def build(config):
        raise RuntimeError("translated build exploded")

    _prepare_command(tmp_path, monkeypatch, build)

    result = pdf_commands.build_epub_command(_args(only_translated=True))

    assert result == 1
    assert any("[Translated Build Failed]" in message for message in log_messages)


def test_original_build_failure_is_reported(
    tmp_path: Path, monkeypatch, log_messages
):
    calls = []

    def build(config):
        calls.append(config.translated)
        raise RuntimeError("original build exploded")

    _prepare_command(tmp_path, monkeypatch, build)

    result = pdf_commands.build_epub_command(_args())

    assert result == 1
    assert calls == [False]
    assert any("[Original Build Failed]" in message for message in log_messages)


def test_epub_conversion_rejects_only_translated(
    tmp_path: Path, monkeypatch, log_messages
):
    def build(config):
        raise AssertionError("build must not be called")

    _prepare_command(
        tmp_path,
        monkeypatch,
        build,
        config={"title": "Book", "pipeline": "epub_conversion"},
    )

    result = pdf_commands.build_epub_command(_args(only_translated=True))

    assert result == 1
    assert any(
        "--only-translated is not available" in message
        for message in log_messages
    )
